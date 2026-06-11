"""
SQLAlchemy 2.0 async models.
"""

from __future__ import annotations

import time
from sqlalchemy import Float, Integer, String, Boolean, JSON, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str] = mapped_column(String(10))          # LIVE | PAPER | BACKTEST
    direction: Mapped[str] = mapped_column(String(10))     # LONG | SHORT
    market_id: Mapped[str] = mapped_column(String(100))
    ticker: Mapped[str] = mapped_column(String(20), default="")
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float, default=0.0)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    pnl_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    entry_ts: Mapped[float] = mapped_column(Float, default=time.time)
    exit_ts: Mapped[float] = mapped_column(Float, default=0.0)
    exit_reason: Mapped[str] = mapped_column(String(20), default="")
    status: Mapped[str] = mapped_column(String(10), default="OPEN")
    tx_hash: Mapped[str] = mapped_column(String(100), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    active_signals: Mapped[list] = mapped_column(JSON, default=list)
    signal_values: Mapped[dict] = mapped_column(JSON, default=dict)  # snapshot at entry (funding, OI, vol regime, …)
    funding_paid: Mapped[float] = mapped_column(Float, default=0.0)
    slippage_pct: Mapped[float] = mapped_column(Float, default=0.0)
    mae_pct: Mapped[float] = mapped_column(Float, default=0.0)  # max adverse excursion %
    mfe_pct: Mapped[float] = mapped_column(Float, default=0.0)  # max favorable excursion %


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float)
    market_id: Mapped[str] = mapped_column(String(100))
    direction_bias: Mapped[str] = mapped_column(String(10))
    active_signals: Mapped[list] = mapped_column(JSON)
    signal_count: Mapped[int] = mapped_column(Integer)
    tier_s: Mapped[bool] = mapped_column(Boolean, default=False)
    signal_values: Mapped[dict] = mapped_column(JSON)
    # linked decision
    decision_action: Mapped[str] = mapped_column(String(10), default="")
    decision_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    decision_reason: Mapped[str] = mapped_column(Text, default="")
    decision_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)


class AiDecision(Base):
    __tablename__ = "ai_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float)
    action: Mapped[str] = mapped_column(String(10))
    confidence: Mapped[float] = mapped_column(Float)
    entry: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    position_size: Mapped[float] = mapped_column(Float)
    risk_score: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    latency_ms: Mapped[float] = mapped_column(Float)
    model: Mapped[str] = mapped_column(String(50))
    outcome_pnl: Mapped[float] = mapped_column(Float, default=0.0)  # filled after trade closes
    was_approved: Mapped[bool] = mapped_column(Boolean, default=False)


class MarginSnapshot(Base):
    __tablename__ = "margin_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float)
    available_usdt: Mapped[float] = mapped_column(Float)
    used_usdt: Mapped[float] = mapped_column(Float)
    total_usdt: Mapped[float] = mapped_column(Float)
    equity: Mapped[float] = mapped_column(Float)
    daily_drawdown_pct: Mapped[float] = mapped_column(Float)
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, default=False)


class TradePostMortem(Base):
    """Auto-generated statistical post-mortem for every closed trade."""
    __tablename__ = "trade_postmortems"

    id: Mapped[int] = mapped_column(Integer, autoincrement=True, primary_key=True)
    trade_id: Mapped[str] = mapped_column(String, index=True)
    ts: Mapped[float] = mapped_column(Float)
    ticker: Mapped[str] = mapped_column(String(20))
    direction: Mapped[str] = mapped_column(String(10))
    entry_reason: Mapped[str] = mapped_column(Text, default="")
    entry_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    active_signals: Mapped[list] = mapped_column(JSON, default=list)
    signal_values: Mapped[dict] = mapped_column(JSON, default=dict)
    exit_reason: Mapped[str] = mapped_column(String(20), default="")
    pnl_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    r_multiple: Mapped[float] = mapped_column(Float, default=0.0)   # pnl / initial risk
    hold_hours: Mapped[float] = mapped_column(Float, default=0.0)
    mae_pct: Mapped[float] = mapped_column(Float, default=0.0)
    mfe_pct: Mapped[float] = mapped_column(Float, default=0.0)
    signal_contributions: Mapped[dict] = mapped_column(JSON, default=dict)  # signal → pnl share
    evaluation: Mapped[str] = mapped_column(Text, default="")  # auto statistical assessment


class SignalWeightSnapshot(Base):
    """Adaptive scoring engine: weight history (one row per milestone)."""
    __tablename__ = "signal_weight_snapshots"

    id: Mapped[int] = mapped_column(Integer, autoincrement=True, primary_key=True)
    ts: Mapped[float] = mapped_column(Float)
    n_trades: Mapped[int] = mapped_column(Integer)  # closed trades at snapshot time
    weights: Mapped[dict] = mapped_column(JSON, default=dict)       # signal → weight
    signal_stats: Mapped[dict] = mapped_column(JSON, default=dict)  # signal → {n, wr, pf, expectancy, ewma}


class ErrorLog(Base):
    __tablename__ = "error_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float)
    component: Mapped[str] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(String(10))
