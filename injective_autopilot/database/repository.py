"""
Async database repository.
All writes use INSERT OR REPLACE to be idempotent.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from database.models import Base, Trade, Signal, AiDecision, MarginSnapshot, ErrorLog
from core.executor import TradeRecord
from core.decision_engine import TradeDecision
from core.sentinel import SentinelTrigger


class Repository:
    def __init__(self, db_url: str) -> None:
        self._engine = create_async_engine(db_url, echo=False)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def save_trade(self, trade: TradeRecord) -> None:
        async with self._session_factory() as session:
            obj = Trade(
                id=trade.id,
                mode=trade.mode,
                direction=trade.direction,
                market_id=trade.market_id,
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
                active_signals=[],
                funding_paid=trade.funding_paid,
                slippage_pct=trade.slippage_pct,
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
                    "ts": r.ts, "direction": r.direction_bias, "signals": r.active_signals,
                    "count": r.signal_count, "tier_s": r.tier_s, "values": r.signal_values,
                    "decision": r.decision_action, "confidence": r.decision_confidence,
                }
                for r in rows
            ]

    @staticmethod
    def _trade_to_dict(r: Trade) -> dict:
        return {
            "id": r.id, "mode": r.mode, "direction": r.direction, "market_id": r.market_id,
            "entry_price": r.entry_price, "exit_price": r.exit_price,
            "stop_loss": r.stop_loss, "take_profit": r.take_profit, "quantity": r.quantity,
            "pnl_usdt": r.pnl_usdt, "pnl_pct": r.pnl_pct,
            "entry_ts": r.entry_ts, "exit_ts": r.exit_ts, "exit_reason": r.exit_reason,
            "status": r.status, "confidence": r.confidence, "reason": r.reason,
            "funding_paid": r.funding_paid, "slippage_pct": r.slippage_pct,
        }
