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

from analytics.adaptive_scorer import AdaptiveScorer
from analytics.postmortem import build_postmortem
from config.settings import get_settings
from core.decision_engine import DecisionEngine
from core.executor import Executor, TradeRecord
from core.risk_engine import RiskEngine
from core.sentinel import Sentinel, SentinelTrigger
from data.injective_client import InjectiveClient
from database.repository import Repository
from signals.derivatives import DerivativesAnalyzer

log = logging.getLogger(__name__)

# Milestone per snapshot pesi (poi ogni 25 trade)
WEIGHT_SNAPSHOT_EVERY = 25


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
        self._scorer = AdaptiveScorer()
        self._last_snapshot_count = 0

    async def start(self) -> None:
        self._running = True
        self._executor.seed_counter(await self._repo.get_max_trade_counter())
        await self._refresh_adaptive_weights(startup=True)
        log.info("Paper Trading Engine started")
        await asyncio.gather(
            self._sentinel.run(on_batch=self._on_batch),
            self._monitoring_loop(),
        )

    async def _refresh_adaptive_weights(self, startup: bool = False) -> None:
        """
        Learning loop: ricalcola i pesi dei segnali dai trade chiusi e li
        passa al DecisionEngine (solo ranking, il gate resta sul raw score).
        Snapshot su DB ogni WEIGHT_SNAPSHOT_EVERY trade chiusi.
        """
        try:
            closed = await self._repo.get_closed_trades()
            # Esclude i trade chiusi prima del fix ATR-freddo (10/06 20:33):
            # SL/TP calcolati su ATR microscopico producevano chiusure "TP"
            # istantanee anche in perdita → esiti finti che inquinano i pesi.
            LEARNING_MIN_EXIT_TS = 1781116380.0   # 2026-06-10T20:33:00+02:00
            closed = [t for t in closed
                      if float(t.get("exit_ts", 0) or 0) >= LEARNING_MIN_EXIT_TS]
            if not closed:
                return
            weights = self._scorer.update(closed)
            self._decision.set_signal_weights(weights)

            n = len(closed)
            milestone = (n // WEIGHT_SNAPSHOT_EVERY) * WEIGHT_SNAPSHOT_EVERY
            if milestone > self._last_snapshot_count and milestone > 0:
                await self._repo.save_weight_snapshot(n, weights, self._scorer.signal_stats)
                self._last_snapshot_count = milestone
                log.info("Adaptive weights snapshot @ %d trades: %s", n, weights)
            elif startup:
                self._last_snapshot_count = milestone
                log.info("Adaptive weights loaded from %d closed trades: %s", n, weights)
        except Exception as exc:
            log.warning("Adaptive weight refresh failed: %s", exc)

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

    async def _on_batch(self, triggers: list[SentinelTrigger]) -> None:
        """Full pipeline: batch of signals → one Gemini call → risk → execute per approved trade."""
        try:
            # Paper mode: use in-memory executor trades as positions so
            # max_open_positions is enforced correctly (fetch_positions() returns
            # on-chain positions which are empty in paper mode).
            positions = self._executor.open_trades
            margin = self._cfg.capital_usdt

            # 1. One Gemini call for all triggers
            decisions = await self._decision.decide_batch(
                triggers=triggers,
                positions=positions,
                margin_available=margin,
                max_open_positions=self._cfg.max_open_positions,
            )

            # Save all signals to DB
            for trigger in triggers:
                decision = decisions.get(trigger.ticker)
                if decision is None:
                    from core.decision_engine import TradeDecision
                    decision = TradeDecision(
                        action="NO_TRADE", confidence=0.0, entry=0.0,
                        stop_loss=0.0, take_profit=0.0, position_size=0.0,
                        risk_score=1.0, reason="No signal from batch",
                        ticker=trigger.ticker, market_id=trigger.market_id,
                    )
                await self._repo.save_signal(trigger, decision)

            # 2. Risk + execute each approved trade
            for ticker, decision in decisions.items():
                try:
                    await self._repo.save_ai_decision(decision, approved=False)

                    validation = self._risk.validate_decision(decision, margin, positions)
                    if not validation.approved:
                        log.info("[%s] BLOCKED by Risk Engine: %s", ticker, validation.reason)
                        continue

                    final_decision = validation.adjusted_decision or decision
                    await self._repo.save_ai_decision(final_decision, approved=True)

                    trade = await self._executor.execute(final_decision)
                    if trade:
                        await self._repo.save_trade(trade)

                except Exception as exc:
                    log.error("[%s] trade pipeline error: %s", ticker, exc)

        except Exception as exc:
            log.error("Batch pipeline error: %s", exc)
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
                    # Post-mortem automatico
                    try:
                        pm = build_postmortem(self._record_to_dict(trade), self._scorer.signal_stats)
                        await self._repo.save_postmortem(pm)
                        log.info("Post-mortem %s: %s", trade.id, pm["evaluation"])
                    except Exception as exc:
                        log.warning("Post-mortem failed for %s: %s", trade.id, exc)

                if closed:
                    # Learning loop: aggiorna i pesi adattivi dopo ogni chiusura
                    await self._refresh_adaptive_weights()

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

    @staticmethod
    def _record_to_dict(t: TradeRecord) -> dict:
        return {
            "id": t.id, "mode": t.mode, "direction": t.direction,
            "market_id": t.market_id, "ticker": t.ticker,
            "entry_price": t.entry_price, "exit_price": t.exit_price,
            "stop_loss": t.stop_loss, "take_profit": t.take_profit,
            "quantity": t.quantity, "pnl_usdt": t.pnl_usdt, "pnl_pct": t.pnl_pct,
            "entry_ts": t.entry_ts, "exit_ts": t.exit_ts, "exit_reason": t.exit_reason,
            "status": t.status, "confidence": t.confidence, "reason": t.reason,
            "active_signals": t.active_signals, "signal_values": t.signal_values,
            "mae_pct": t.mae_pct, "mfe_pct": t.mfe_pct,
        }

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
            "gemini_calls": self._decision.stats["total_calls"],
        }
