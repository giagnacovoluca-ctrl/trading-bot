"""
Async database repository.
All writes use INSERT OR REPLACE to be idempotent.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select, desc, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from database.models import (
    Base, Trade, Signal, AiDecision, MarginSnapshot, ErrorLog,
    TradePostMortem, SignalWeightSnapshot,
)
from core.executor import TradeRecord
from core.decision_engine import TradeDecision
from core.sentinel import SentinelTrigger

# Columns added after initial release: name → SQL type (SQLite ALTER TABLE migration)
_TRADE_MIGRATIONS: dict[str, str] = {
    "signal_values": "JSON DEFAULT '{}'",
    "mae_pct": "FLOAT DEFAULT 0.0",
    "mfe_pct": "FLOAT DEFAULT 0.0",
}


class Repository:
    def __init__(self, db_url: str) -> None:
        self._engine = create_async_engine(db_url, echo=False)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Migrate pre-existing trades table (create_all doesn't add columns)
            res = await conn.execute(text("PRAGMA table_info(trades)"))
            existing = {row[1] for row in res.fetchall()}
            for col, ddl in _TRADE_MIGRATIONS.items():
                if col not in existing:
                    await conn.execute(text(f"ALTER TABLE trades ADD COLUMN {col} {ddl}"))

    async def get_max_trade_counter(self) -> int:
        """Max suffisso numerico tra gli id trade (es. PAPER_000020 → 20)."""
        async with self._session_factory() as session:
            res = await session.execute(select(Trade.id))
            ids = [row[0] for row in res.fetchall()]
        mx = 0
        for tid in ids:
            try:
                mx = max(mx, int(str(tid).rsplit("_", 1)[-1]))
            except ValueError:
                continue
        return mx

    async def save_trade(self, trade: TradeRecord) -> None:
        async with self._session_factory() as session:
            obj = Trade(
                id=trade.id,
                mode=trade.mode,
                direction=trade.direction,
                market_id=trade.market_id,
                ticker=trade.ticker,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                stop_loss=trade.stop_loss,
                take_profit=trade.take_profit,
                quantity=trade.quantity,
                pnl_usdt=trade.pnl_usdt,
                pnl_pct=trade.pnl_pct,
                entry_ts=trade.entry_ts,
                exit_ts=trade.exit_ts,
                exit_reason=trade.exit_reason,
                status=trade.status,
                tx_hash=trade.tx_hash,
                confidence=trade.confidence,
                reason=trade.reason,
                active_signals=list(trade.active_signals),
                signal_values=dict(trade.signal_values),
                funding_paid=trade.funding_paid,
                slippage_pct=trade.slippage_pct,
                mae_pct=trade.mae_pct,
                mfe_pct=trade.mfe_pct,
            )
            await session.merge(obj)
            await session.commit()

    async def save_signal(
        self,
        trigger: SentinelTrigger,
        decision: TradeDecision | None = None,
    ) -> int:
        async with self._session_factory() as session:
            obj = Signal(
                ts=trigger.ts,
                market_id=trigger.market_id,
                direction_bias=trigger.direction_bias,
                active_signals=trigger.active_signals,
                signal_count=trigger.signal_count,
                tier_s=trigger.tier_s,
                signal_values=trigger.signal_values,
                decision_action=decision.action if decision else "",
                decision_confidence=decision.confidence if decision else 0.0,
                decision_reason=decision.reason if decision else "",
                decision_latency_ms=decision.latency_ms if decision else 0.0,
            )
            session.add(obj)
            await session.flush()
            signal_id = obj.id
            await session.commit()
            return signal_id

    async def save_ai_decision(self, decision: TradeDecision, approved: bool) -> None:
        async with self._session_factory() as session:
            obj = AiDecision(
                ts=time.time(),
                action=decision.action,
                confidence=decision.confidence,
                entry=decision.entry,
                stop_loss=decision.stop_loss,
                take_profit=decision.take_profit,
                position_size=decision.position_size,
                risk_score=decision.risk_score,
                reason=decision.reason,
                latency_ms=decision.latency_ms,
                model=decision.model,
                was_approved=approved,
            )
            session.add(obj)
            await session.commit()

    async def save_margin_snapshot(
        self,
        available: float,
        used: float,
        total: float,
        equity: float,
        daily_dd: float,
        kill_active: bool,
    ) -> None:
        async with self._session_factory() as session:
            obj = MarginSnapshot(
                ts=time.time(),
                available_usdt=available,
                used_usdt=used,
                total_usdt=total,
                equity=equity,
                daily_drawdown_pct=daily_dd,
                kill_switch_active=kill_active,
            )
            session.add(obj)
            await session.commit()

    async def log_error(self, component: str, message: str, level: str = "ERROR") -> None:
        async with self._session_factory() as session:
            obj = ErrorLog(ts=time.time(), component=component, message=message, level=level)
            session.add(obj)
            await session.commit()

    async def get_trades(self, mode: str | None = None, limit: int = 100) -> list[dict]:
        async with self._session_factory() as session:
            q = select(Trade).order_by(desc(Trade.entry_ts)).limit(limit)
            if mode:
                q = q.where(Trade.mode == mode)
            result = await session.execute(q)
            rows = result.scalars().all()
            return [self._trade_to_dict(r) for r in rows]

    async def get_ai_decisions(self, limit: int = 50) -> list[dict]:
        async with self._session_factory() as session:
            q = select(AiDecision).order_by(desc(AiDecision.ts)).limit(limit)
            result = await session.execute(q)
            rows = result.scalars().all()
            return [
                {
                    "ts": r.ts, "action": r.action, "confidence": r.confidence,
                    "entry": r.entry, "stop_loss": r.stop_loss, "take_profit": r.take_profit,
                    "reason": r.reason, "latency_ms": r.latency_ms, "model": r.model,
                    "outcome_pnl": r.outcome_pnl, "was_approved": r.was_approved,
                }
                for r in rows
            ]

    async def get_equity_curve(self) -> list[dict]:
        async with self._session_factory() as session:
            q = select(MarginSnapshot).order_by(MarginSnapshot.ts)
            result = await session.execute(q)
            rows = result.scalars().all()
            return [{"ts": r.ts, "equity": r.equity, "daily_dd": r.daily_drawdown_pct} for r in rows]

    async def get_signals(self, limit: int = 100) -> list[dict]:
        async with self._session_factory() as session:
            q = select(Signal).order_by(desc(Signal.ts)).limit(limit)
            result = await session.execute(q)
            rows = result.scalars().all()
            return [
                {
                    "ts": r.ts, "market_id": r.market_id,
                    "direction": r.direction_bias, "signals": r.active_signals,
                    "count": r.signal_count, "tier_s": r.tier_s, "values": r.signal_values or {},
                    "decision": r.decision_action, "confidence": r.decision_confidence,
                }
                for r in rows
            ]

    # ── Analytics / learning ─────────────────────────────────────────────

    async def get_open_trades(self, mode: str | None = None) -> list[dict]:
        """Trade ancora OPEN su DB (es. dopo un restart) — usati per ripopolare
        l'executor in memoria, altrimenti restano orfani e non monitorati."""
        async with self._session_factory() as session:
            q = select(Trade).where(Trade.status == "OPEN").order_by(Trade.entry_ts)
            if mode:
                q = q.where(Trade.mode == mode)
            result = await session.execute(q)
            return [self._trade_to_dict(r) for r in result.scalars().all()]

    async def get_closed_trades(self, mode: str | None = None) -> list[dict]:
        """All closed trades, oldest first (analytics need chronological order)."""
        async with self._session_factory() as session:
            q = select(Trade).where(Trade.status == "CLOSED").order_by(Trade.exit_ts)
            if mode:
                q = q.where(Trade.mode == mode)
            result = await session.execute(q)
            return [self._trade_to_dict(r) for r in result.scalars().all()]

    async def update_trade_signals(self, trade_id: str, active_signals: list, signal_values: dict) -> None:
        """Backfill signal context on a trade (used for pre-migration rows)."""
        async with self._session_factory() as session:
            obj = await session.get(Trade, trade_id)
            if obj is not None:
                obj.active_signals = active_signals
                obj.signal_values = signal_values
                await session.commit()

    async def save_postmortem(self, pm: dict) -> None:
        async with self._session_factory() as session:
            session.add(TradePostMortem(**pm))
            await session.commit()

    async def get_postmortems(self, limit: int = 100) -> list[dict]:
        async with self._session_factory() as session:
            q = select(TradePostMortem).order_by(desc(TradePostMortem.ts)).limit(limit)
            result = await session.execute(q)
            return [
                {
                    "trade_id": r.trade_id, "ts": r.ts, "ticker": r.ticker,
                    "direction": r.direction, "entry_reason": r.entry_reason,
                    "entry_confidence": r.entry_confidence,
                    "active_signals": r.active_signals or [],
                    "signal_values": r.signal_values or {},
                    "exit_reason": r.exit_reason, "pnl_usdt": r.pnl_usdt,
                    "r_multiple": r.r_multiple, "hold_hours": r.hold_hours,
                    "mae_pct": r.mae_pct, "mfe_pct": r.mfe_pct,
                    "signal_contributions": r.signal_contributions or {},
                    "evaluation": r.evaluation,
                }
                for r in result.scalars().all()
            ]

    async def save_weight_snapshot(self, n_trades: int, weights: dict, signal_stats: dict) -> None:
        async with self._session_factory() as session:
            session.add(SignalWeightSnapshot(
                ts=time.time(), n_trades=n_trades,
                weights=weights, signal_stats=signal_stats,
            ))
            await session.commit()

    async def get_weight_snapshots(self, limit: int = 200) -> list[dict]:
        async with self._session_factory() as session:
            q = select(SignalWeightSnapshot).order_by(SignalWeightSnapshot.ts).limit(limit)
            result = await session.execute(q)
            return [
                {"ts": r.ts, "n_trades": r.n_trades, "weights": r.weights or {},
                 "signal_stats": r.signal_stats or {}}
                for r in result.scalars().all()
            ]

    async def get_signals_window(self, ts_from: float, ts_to: float) -> list[dict]:
        """Signals in a time window (used to backfill trade↔signal links)."""
        async with self._session_factory() as session:
            q = select(Signal).where(Signal.ts >= ts_from, Signal.ts <= ts_to)
            result = await session.execute(q)
            return [
                {
                    "ts": r.ts, "market_id": r.market_id,
                    "direction": r.direction_bias, "signals": r.active_signals,
                    "values": r.signal_values or {},
                    "decision": r.decision_action,
                }
                for r in result.scalars().all()
            ]

    @staticmethod
    def _trade_to_dict(r: Trade) -> dict:
        return {
            "id": r.id, "mode": r.mode, "direction": r.direction,
            "market_id": r.market_id, "ticker": getattr(r, "ticker", ""),
            "entry_price": r.entry_price, "exit_price": r.exit_price,
            "stop_loss": r.stop_loss, "take_profit": r.take_profit, "quantity": r.quantity,
            "pnl_usdt": r.pnl_usdt, "pnl_pct": r.pnl_pct,
            "entry_ts": r.entry_ts, "exit_ts": r.exit_ts, "exit_reason": r.exit_reason,
            "status": r.status, "confidence": r.confidence, "reason": r.reason,
            "active_signals": getattr(r, "active_signals", None) or [],
            "signal_values": getattr(r, "signal_values", None) or {},
            "funding_paid": r.funding_paid, "slippage_pct": r.slippage_pct,
            "mae_pct": getattr(r, "mae_pct", 0.0) or 0.0,
            "mfe_pct": getattr(r, "mfe_pct", 0.0) or 0.0,
        }
