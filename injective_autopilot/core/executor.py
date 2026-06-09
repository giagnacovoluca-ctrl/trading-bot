"""
Livello 4 — Execution Engine.

Gestisce l'esecuzione degli ordini:
  - LIVE: invia ordini reali via Injective SDK + AuthZ
  - PAPER: simula ordini senza capitale reale
  - BACKTEST: replay su dati storici

Features:
  - Retry logic con backoff esponenziale
  - Slippage protection (rifiuta se prezzo si è mosso troppo)
  - Rejection handling
  - Position monitoring loop (SL/TP hit detection)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from tenacity import AsyncRetrying, stop_after_attempt, wait_exponential

from config.settings import get_settings
from core.decision_engine import TradeDecision
from data.injective_client import InjectiveClient, OrderResult

log = logging.getLogger(__name__)

# ── Trade record ─────────────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    id: str
    mode: str                # "LIVE" | "PAPER"
    direction: str           # "LONG" | "SHORT"
    market_id: str
    entry_price: float
    stop_loss: float
    take_profit: float
    quantity: float
    entry_ts: float = field(default_factory=time.time)
    exit_price: float = 0.0
    exit_ts: float = 0.0
    pnl_usdt: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""    # "SL" | "TP" | "MANUAL" | "TIMEOUT"
    tx_hash: str = ""
    confidence: float = 0.0
    reason: str = ""
    status: str = "OPEN"     # "OPEN" | "CLOSED"
    funding_paid: float = 0.0
    slippage_pct: float = 0.0


class Executor:
    def __init__(self, client: InjectiveClient, mode: str = "PAPER") -> None:
        self._client = client
        self._cfg = get_settings()
        self._mode = mode
        self._open_trades: dict[str, TradeRecord] = {}
        self._closed_trades: list[TradeRecord] = []
        self._trade_counter = 0

        # Slippage protection: max acceptable deviation from decision price
        self._max_slippage_pct = 0.005  # 0.5%

    async def execute(self, decision: TradeDecision) -> TradeRecord | None:
        """
        Execute a validated TradeDecision.
        Returns TradeRecord if executed, None if rejected.
        """
        if not decision or decision.action == "NO_TRADE":
            return None

        if self._mode == "LIVE":
            return await self._execute_live(decision)
        else:
            return await self._execute_paper(decision)

    async def _execute_live(self, decision: TradeDecision) -> TradeRecord | None:
        """Execute real order on Injective chain."""
        is_buy = decision.action == "LONG"

        # Slippage check: verify current price hasn't moved too much
        trade_market_id = getattr(decision, "market_id", "") or self._cfg.market_id
        current_snap = await self._client.fetch_orderbook(depth=5, market_id=trade_market_id)
        current_price = current_snap.mid
        price_drift = abs(current_price - decision.entry) / (decision.entry + 1e-10)

        if price_drift > self._max_slippage_pct:
            log.warning(
                "Slippage protection: price moved %.3f%% from decision entry. Aborting.",
                price_drift * 100,
            )
            return None

        # Submit order with retry
        result: OrderResult | None = None
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=1, max=8),
                reraise=True,
            ):
                with attempt:
                    result = await self._client.create_limit_order(
                        is_buy=is_buy,
                        price=decision.entry,
                        quantity=decision.quantity if hasattr(decision, "quantity") else decision.position_size,
                        reduce_only=False,
                        market_id=trade_market_id,
                    )
        except Exception as exc:
            log.error("Order submission failed after retries: %s", exc)
            return None

        if not result or not result.success:
            log.error("Order rejected: %s", result.error if result else "unknown")
            return None

        self._trade_counter += 1
        trade = TradeRecord(
            id=f"LIVE_{self._trade_counter:06d}",
            mode="LIVE",
            direction=decision.action,
            market_id=getattr(decision, "market_id", "") or self._cfg.market_id,
            entry_price=current_price,
            stop_loss=decision.stop_loss,
            take_profit=decision.take_profit,
            quantity=decision.position_size,
            tx_hash=result.tx_hash,
            confidence=decision.confidence,
            reason=decision.reason,
            slippage_pct=price_drift * 100,
        )
        self._open_trades[trade.id] = trade
        log.info("LIVE trade opened: %s %s qty=%.4f entry=%.4f", trade.id, trade.direction, trade.quantity, trade.entry_price)
        return trade

    async def _execute_paper(self, decision: TradeDecision) -> TradeRecord:
        """Simulate order execution. Uses mark price as fill price."""
        trade_market_id = getattr(decision, "market_id", "") or self._cfg.market_id
        snap = await self._client.fetch_orderbook(depth=1, market_id=trade_market_id)
        fill_price = snap.mid if snap.mid > 0 else decision.entry

        # Paper: add estimated slippage (half spread)
        if decision.action == "LONG":
            fill_price = snap.asks[0].price if snap.asks else fill_price
        else:
            fill_price = snap.bids[0].price if snap.bids else fill_price

        slippage = abs(fill_price - decision.entry) / (decision.entry + 1e-10)

        self._trade_counter += 1
        trade = TradeRecord(
            id=f"PAPER_{self._trade_counter:06d}",
            mode="PAPER",
            direction=decision.action,
            market_id=trade_market_id,
            entry_price=fill_price,
            stop_loss=decision.stop_loss,
            take_profit=decision.take_profit,
            quantity=decision.position_size,
            confidence=decision.confidence,
            reason=decision.reason,
            slippage_pct=slippage * 100,
        )
        self._open_trades[trade.id] = trade
        log.info(
            "PAPER trade opened: %s %s qty=%.4f entry=%.4f sl=%.4f tp=%.4f",
            trade.id, trade.direction, trade.quantity, trade.entry_price,
            trade.stop_loss, trade.take_profit,
        )
        return trade

    async def monitor_positions(self, funding_rate: float = 0.0) -> list[TradeRecord]:
        """
        Check open positions for SL/TP hits.
        Returns list of trades that were closed in this call.
        Call this every sentinel tick.
        """
        if not self._open_trades:
            return []

        closed: list[TradeRecord] = []

        if self._mode == "LIVE":
            positions = await self._client.fetch_positions()

        # Fetch current price per market (group open trades by market_id)
        market_prices: dict[str, float] = {}
        for trade in self._open_trades.values():
            if trade.market_id not in market_prices:
                try:
                    snap = await self._client.fetch_orderbook(depth=1, market_id=trade.market_id)
                    market_prices[trade.market_id] = snap.mid
                except Exception:
                    market_prices[trade.market_id] = 0.0

        for trade_id, trade in list(self._open_trades.items()):
            current_price = market_prices.get(trade.market_id, 0.0)
            if current_price < 1e-10:
                continue

            # Check SL/TP
            exit_reason = ""
            exit_price = current_price

            if trade.direction == "LONG":
                if current_price <= trade.stop_loss:
                    exit_reason = "SL"
                    exit_price = trade.stop_loss
                elif current_price >= trade.take_profit:
                    exit_reason = "TP"
                    exit_price = trade.take_profit
            elif trade.direction == "SHORT":
                if current_price >= trade.stop_loss:
                    exit_reason = "SL"
                    exit_price = trade.stop_loss
                elif current_price <= trade.take_profit:
                    exit_reason = "TP"
                    exit_price = trade.take_profit

            if exit_reason:
                closed_trade = await self._close_trade(trade, exit_price, exit_reason, funding_rate)
                closed.append(closed_trade)

        return closed

    async def _close_trade(
        self,
        trade: TradeRecord,
        exit_price: float,
        reason: str,
        funding_rate: float,
    ) -> TradeRecord:
        if self._mode == "LIVE":
            result = await self._client.close_position(
                direction=trade.direction,
                quantity=trade.quantity,
                price=exit_price,
            )
            if not result.success:
                log.error("Failed to close LIVE position: %s", result.error)

        # PnL calculation
        hold_hours = (time.time() - trade.entry_ts) / 3600.0
        if trade.direction == "LONG":
            gross_pnl = (exit_price - trade.entry_price) * trade.quantity
            funding_cost = funding_rate * hold_hours * trade.entry_price * trade.quantity
        else:
            gross_pnl = (trade.entry_price - exit_price) * trade.quantity
            funding_cost = -funding_rate * hold_hours * trade.entry_price * trade.quantity

        net_pnl = gross_pnl - funding_cost
        pnl_pct = net_pnl / (trade.entry_price * trade.quantity + 1e-10)

        trade.exit_price = exit_price
        trade.exit_ts = time.time()
        trade.pnl_usdt = net_pnl
        trade.pnl_pct = pnl_pct * 100
        trade.exit_reason = reason
        trade.funding_paid = funding_cost
        trade.status = "CLOSED"

        self._open_trades.pop(trade.id, None)
        self._closed_trades.append(trade)

        log.info(
            "Trade CLOSED %s: %s pnl=%.2f$ (%.2f%%) via %s",
            trade.id, trade.direction, net_pnl, pnl_pct * 100, reason,
        )
        return trade

    async def close_all(self) -> None:
        """Emergency close all positions."""
        for trade in list(self._open_trades.values()):
            try:
                snap = await self._client.fetch_orderbook(depth=1, market_id=trade.market_id)
                current_price = snap.mid
            except Exception:
                current_price = trade.entry_price
            await self._close_trade(trade, current_price, "MANUAL", 0.0)

    @property
    def open_trades(self) -> list[TradeRecord]:
        return list(self._open_trades.values())

    @property
    def closed_trades(self) -> list[TradeRecord]:
        return self._closed_trades.copy()

    @property
    def mode(self) -> str:
        return self._mode
