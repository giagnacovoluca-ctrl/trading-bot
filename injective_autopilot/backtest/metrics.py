"""
Performance metrics — calcolo completo per backtest, paper e live.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np


@dataclass
class PerformanceMetrics:
    # PnL
    total_pnl: float
    total_pnl_pct: float

    # Ratios
    profit_factor: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    expectancy: float

    # Win/Loss
    win_rate: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    avg_win: float
    avg_loss: float

    # Drawdown
    max_drawdown: float
    max_drawdown_pct: float
    recovery_factor: float

    # Risk of Ruin (approximation)
    risk_of_ruin: float

    # Directional
    long_trades: int
    short_trades: int
    long_win_rate: float
    short_win_rate: float

    # Individual signal contributions (signal_name → pnl_contribution)
    signal_contributions: dict[str, float]


def compute_metrics(
    pnl_series: list[float],
    equity_curve: list[float],
    trade_directions: list[str] | None = None,
    trade_signals: list[list[str]] | None = None,
    initial_capital: float = 1000.0,
    risk_free_rate: float = 0.0,
    periods_per_year: int = 8760,  # hourly data assumption
) -> PerformanceMetrics:
    """
    Compute all performance metrics from raw trade data.

    Args:
        pnl_series: list of per-trade PnL in USD
        equity_curve: running equity curve
        trade_directions: list of "LONG" | "SHORT" per trade
        trade_signals: list of active signal lists per trade
        initial_capital: starting capital
        risk_free_rate: annualised risk-free rate
        periods_per_year: used for annualisation
    """
    if not pnl_series:
        return _empty_metrics()

    pnl = np.array(pnl_series)
    equity = np.array(equity_curve) if equity_curve else np.cumsum(pnl) + initial_capital

    n = len(pnl)
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    # Basic stats
    total_pnl = float(pnl.sum())
    total_pnl_pct = total_pnl / initial_capital * 100
    win_rate = float(len(wins) / n) if n > 0 else 0.0

    avg_win = float(wins.mean()) if len(wins) > 0 else 0.0
    avg_loss = float(losses.mean()) if len(losses) > 0 else 0.0

    # Profit Factor: gross profit / gross loss
    gross_profit = float(wins.sum()) if len(wins) > 0 else 0.0
    gross_loss = abs(float(losses.sum())) if len(losses) > 0 else 1e-10
    profit_factor = gross_profit / gross_loss

    # Expectancy
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

    # Returns for ratio computation
    equity_returns = np.diff(equity) / equity[:-1]

    # Sharpe
    if len(equity_returns) > 1 and equity_returns.std() > 1e-10:
        rf_per_period = (1 + risk_free_rate) ** (1 / periods_per_year) - 1
        excess = equity_returns - rf_per_period
        sharpe = float(excess.mean() / excess.std() * math.sqrt(periods_per_year))
    else:
        sharpe = 0.0

    # Sortino (downside deviation only)
    if len(equity_returns) > 1:
        neg_returns = equity_returns[equity_returns < 0]
        if len(neg_returns) > 1 and neg_returns.std() > 1e-10:
            sortino = float(equity_returns.mean() / neg_returns.std() * math.sqrt(periods_per_year))
        else:
            sortino = sharpe
    else:
        sortino = 0.0

    # Drawdown
    peak = np.maximum.accumulate(equity)
    drawdown = peak - equity
    max_dd = float(drawdown.max())
    max_dd_pct = float((drawdown / peak).max()) * 100 if peak.max() > 0 else 0.0

    # Calmar
    if max_dd_pct > 0:
        cagr = total_pnl_pct  # simplified (non-compounded)
        calmar = cagr / max_dd_pct
    else:
        calmar = 0.0

    # Recovery factor
    recovery_factor = total_pnl / (max_dd + 1e-10)

    # Risk of Ruin (simplified formula: see Vince, "Portfolio Management Formulas")
    if avg_win > 0 and avg_loss < 0:
        a = abs(avg_loss)
        b = avg_win
        p = win_rate
        q = 1 - p
        # RoR ≈ ((q/p) × (a/b)) ^ (capital / a)
        ratio = (q / (p + 1e-10)) * (a / (b + 1e-10))
        if ratio < 1.0:
            risk_of_ruin = ratio ** (initial_capital / (a + 1e-10))
        else:
            risk_of_ruin = 1.0
        risk_of_ruin = float(np.clip(risk_of_ruin, 0.0, 1.0))
    else:
        risk_of_ruin = 1.0

    # Directional breakdown
    directions = trade_directions or []
    long_pnl = [p for p, d in zip(pnl_series, directions) if d == "LONG"]
    short_pnl = [p for p, d in zip(pnl_series, directions) if d == "SHORT"]
    long_wins = sum(1 for p in long_pnl if p > 0)
    short_wins = sum(1 for p in short_pnl if p > 0)
    long_wr = long_wins / len(long_pnl) if long_pnl else 0.0
    short_wr = short_wins / len(short_pnl) if short_pnl else 0.0

    # Signal contribution analysis
    signal_contributions: dict[str, float] = {}
    if trade_signals:
        for trade_pnl, sigs in zip(pnl_series, trade_signals):
            for sig in sigs:
                sig_name = sig.split("(")[0]  # strip parameters
                signal_contributions[sig_name] = signal_contributions.get(sig_name, 0.0) + trade_pnl

    return PerformanceMetrics(
        total_pnl=total_pnl,
        total_pnl_pct=total_pnl_pct,
        profit_factor=profit_factor,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        expectancy=expectancy,
        win_rate=win_rate * 100,
        total_trades=n,
        winning_trades=len(wins),
        losing_trades=len(losses),
        avg_win=avg_win,
        avg_loss=avg_loss,
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
        recovery_factor=recovery_factor,
        risk_of_ruin=risk_of_ruin,
        long_trades=len(long_pnl),
        short_trades=len(short_pnl),
        long_win_rate=long_wr * 100,
        short_win_rate=short_wr * 100,
        signal_contributions=signal_contributions,
    )


def _empty_metrics() -> PerformanceMetrics:
    return PerformanceMetrics(
        total_pnl=0.0, total_pnl_pct=0.0,
        profit_factor=0.0, sharpe_ratio=0.0, sortino_ratio=0.0, calmar_ratio=0.0,
        expectancy=0.0, win_rate=0.0, total_trades=0, winning_trades=0, losing_trades=0,
        avg_win=0.0, avg_loss=0.0, max_drawdown=0.0, max_drawdown_pct=0.0,
        recovery_factor=0.0, risk_of_ruin=1.0, long_trades=0, short_trades=0,
        long_win_rate=0.0, short_win_rate=0.0, signal_contributions={},
    )


def check_live_gate(metrics: PerformanceMetrics, n_trades: int, cfg: Any) -> tuple[bool, list[str]]:
    """
    Validate if system is ready for LIVE mode.
    Returns (ready, list_of_failures).
    """
    failures: list[str] = []

    if n_trades < cfg.live_min_simulated_trades:
        failures.append(f"Insufficient trades: {n_trades} < {cfg.live_min_simulated_trades}")

    if metrics.profit_factor < cfg.live_min_profit_factor:
        failures.append(f"Profit Factor {metrics.profit_factor:.2f} < {cfg.live_min_profit_factor}")

    if metrics.sharpe_ratio < cfg.live_min_sharpe:
        failures.append(f"Sharpe {metrics.sharpe_ratio:.2f} < {cfg.live_min_sharpe}")

    if metrics.max_drawdown_pct > cfg.live_max_drawdown_pct * 100:
        failures.append(f"Max DD {metrics.max_drawdown_pct:.1f}% > {cfg.live_max_drawdown_pct*100:.1f}%")

    return len(failures) == 0, failures
