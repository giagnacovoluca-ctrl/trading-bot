"""
Paper Trading Engine — simula operatività in tempo reale senza capitale reale.

Identico al LIVE in termini di pipeline decisionale:
  Sentinella → Decision Engine → Risk Engine → Executor (PAPER mode)

Differenze rispetto al LIVE:
  - Nessun ordine reale inviato
  - Fill simulato usando best bid/ask al momento del segnale
  - Statistiche identiche al LIVE (stessa struttura DB)
"""

from __future__ import annotations

import asyncio
import logging
import time

from config.settings import get_settings
from core.decision_engine import DecisionEngine
from core.executor import Executor, TradeRecord
from core.risk_engine import RiskEngine
from core.sentinel import Sentinel, SentinelTrigger
from data.injective_client import InjectiveClient
from database.repository import Repository
from signals.derivatives import DerivativesAnalyzer

log = logging.getLogger(__name__)


class PaperTradingEngine:
    """
    Orchestrates the full trading pipeline in PAPER mode.

    Architecture:
      - Sentinel runs in a background task, fires on_trigger
      - on_trigger calls DecisionEngine → RiskEngine → Executor
      - DB saves every event for analysis and dashboard
    """

    def __init__(
        self,
        client: InjectiveClient,
        repo: Repository,
        decision_engine: DecisionEngine,
    ) -> None:
        self._client = client
        self._repo = repo
        self._cfg = get_settings()

        self._deriv = DerivativesAnalyzer()
        self._risk = RiskEngine(deriv_analyzer=self._deriv)
        self._executor = Executor(client=client, mode="PAPER")
        self._sentinel = Sentinel(client=client)
        self._decision = decision_engine

        self._running = False

    async def start(self) -> None:
        self._running = True
        log.info("Paper Trading Engine started")
        await asyncio.gather(
            self._sentinel.run(on_trigger=self._on_trigger),
            self._monitoring_loop(),
        )

    async def stop(self, close_positions: bool = False) -> None:
        self._running = False
        if close_positions:
            log.info("Closing all open positions...")
            await self._executor.close_all()
            for trade in self._executor.closed_trades:
                if trade.exit_reason == "MANUAL":
                    await self._repo.save_trade(trade)
        else:
            log.info("Saving open positions to DB (not closing)...")
            for trade in self._executor.open_trades:
                await self._repo.save_trade(trade)
        log.info("Paper Trading Engine stopped")

    async def _on_trigger(self, trigger: SentinelTrigger) -> None:
        """Full pipeline: signal → decision → risk → execute."""
        try:
            # Fetch current state
            positions = await self._client.fetch_positions()
            margin = self._cfg.capital_usdt  # Paper: fixed capital, no real margin call

            # 1. Decision Engine
            decision = await self._decision.decide(trigger, positions, margin)
            await self._repo.save_signal(trigger, decision)
            await self._repo.save_ai_decision(decision, approved=False)

            if decision.action == "NO_TRADE":
                return

            # 2. Risk Engine validation
            validation = self._risk.validate_decision(decision, margin, positions)

            if not validation.approved:
                log.info("Trade BLOCKED by Risk Engine: %s", validation.reason)
                await self._repo.save_ai_decision(decision, approved=False)
                return

            final_decision = validation.adjusted_decision or decision
            await self._repo.save_ai_decision(final_decision, approved=True)

            # 3. Execute
            trade = await self._executor.execute(final_decision)
            if trade:
                await self._repo.save_trade(trade)

        except Exception as exc:
            log.error("Pipeline error: %s", exc)
            self._risk.record_error()
            await self._repo.log_error("PaperEngine", str(exc))

    async def _monitoring_loop(self) -> None:
        """Background task: checks SL/TP every 30s, saves snapshots."""
        while self._running:
            try:
                snap = await self._client.fetch_market_snapshot()
                self._deriv.update_funding(snap.funding_rate)

                closed = await self._executor.monitor_positions(snap.funding_rate)
                for trade in closed:
                    self._risk.update_equity(self._risk.equity.current_equity + trade.pnl_usdt)
                    await self._repo.save_trade(trade)

                # Kill switch check
                self._risk.check_kill_switch()

                # Save margin snapshot
                await self._repo.save_margin_snapshot(
                    available=self._risk.equity.current_equity,
                    used=sum(t.quantity * t.entry_price for t in self._executor.open_trades),
                    total=self._risk.equity.capital,
                    equity=self._risk.equity.current_equity,
                    daily_dd=self._risk.equity.daily_drawdown_pct * 100,
                    kill_active=self._risk.kill_switch.active,
                )
            except Exception as exc:
                log.warning("Monitoring loop error: %s", exc)

            await asyncio.sleep(30)

    def get_stats(self) -> dict:
        trades = self._executor.closed_trades
        if not trades:
            return {"status": "No trades yet", "equity": self._risk.equity.current_equity}

        pnl_list = [t.pnl_usdt for t in trades]
        wins = sum(1 for p in pnl_list if p > 0)
        return {
            "mode": "PAPER",
            "total_trades": len(trades),
            "open_trades": len(self._executor.open_trades),
            "win_rate": wins / len(trades) * 100,
            "total_pnl": sum(pnl_list),
            "equity": self._risk.equity.current_equity,
            "daily_dd_pct": self._risk.equity.daily_drawdown_pct * 100,
            "kill_switch": self._risk.kill_switch.active,
            "sentinel_triggers": self._sentinel.stats.total_triggers,
            "claude_calls": self._decision.stats["total_calls"],
        }
