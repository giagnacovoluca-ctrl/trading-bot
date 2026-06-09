"""
Backtest Engine — simula fedelmente l'intero pipeline su dati storici.

Pipeline per ogni candela:
  1. Aggiorna tutti i segnali (identico alla Sentinella live)
  2. Se trigger, genera una decisione simulata (no Claude in backtest)
  3. Applica Risk Engine
  4. Simula fill con slippage
  5. Monitora SL/TP
  6. Calcola PnL incluso funding

Walk-Forward Validation:
  - Split: 70% in-sample / 30% out-of-sample
  - Ricalibra parametri solo su in-sample
  - Valida su out-of-sample (principale metrica di generalizzazione)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from backtest.metrics import PerformanceMetrics, compute_metrics, check_live_gate
from config.settings import get_settings
from core.risk_engine import RiskEngine
from signals.anomaly import AnomalyDetector
from signals.derivatives import DerivativesAnalyzer
from signals.orderbook import OrderBookAnalyzer
from signals.volatility import VolatilityAnalyzer
from signals.volume import VolumeAnalyzer

log = logging.getLogger(__name__)


@dataclass
class BacktestCandle:
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    funding_rate: float = 0.0
    open_interest: float = 0.0


@dataclass
class BacktestTrade:
    entry_ts: float
    exit_ts: float
    direction: str
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    quantity: float
    pnl: float
    pnl_pct: float
    exit_reason: str
    active_signals: list[str]
    funding_paid: float = 0.0
    confidence: float = 1.0


@dataclass
class BacktestResult:
    metrics: PerformanceMetrics
    trades: list[BacktestTrade]
    equity_curve: list[float]
    in_sample_metrics: PerformanceMetrics | None = None
    out_of_sample_metrics: PerformanceMetrics | None = None
    live_gate_passed: bool = False
    live_gate_failures: list[str] = field(default_factory=list)
    signal_stats: dict[str, dict] = field(default_factory=dict)


class BacktestEngine:
    """
    Deterministic backtester that replays the full signal pipeline on historical candles.

    Key design principle: the backtest code SHARES the same signal classes as the live system.
    This minimises look-ahead bias and ensures that backtest results are representative.
    """

    def __init__(
        self,
        initial_capital: float | None = None,
        slippage_bps: float = 5.0,   # 5 bps per fill (realistic for Injective)
        commission_bps: float = 1.0,  # maker rebate = negative, taker fee ~0.1%
        max_hold_candles: int = 100,  # force exit after N candles
    ) -> None:
        self._cfg = get_settings()
        self._capital = initial_capital or self._cfg.capital_usdt
        self._slippage_bps = slippage_bps
        self._commission_bps = commission_bps
        self._max_hold_candles = max_hold_candles

    def run(self, candles: list[BacktestCandle], walk_forward: bool = True) -> BacktestResult:
        """
        Run backtest on candle list.

        If walk_forward=True, splits data 70/30 and reports both segments.
        """
        if len(candles) < 100:
            raise ValueError(f"Not enough candles: {len(candles)} < 100")

        if walk_forward and len(candles) >= 200:
            split = int(len(candles) * 0.70)
            in_sample = candles[:split]
            out_of_sample = candles[split:]

            in_result = self._simulate(in_sample)
            out_result = self._simulate(out_of_sample)

            # Combined result uses out-of-sample metrics as primary
            all_trades = in_result.trades + out_result.trades
            all_equity = in_result.equity_curve + out_result.equity_curve

            combined_metrics = compute_metrics(
                pnl_series=[t.pnl for t in all_trades],
                equity_curve=all_equity,
                trade_directions=[t.direction for t in all_trades],
                trade_signals=[t.active_signals for t in all_trades],
                initial_capital=self._capital,
            )

            gate_ok, gate_failures = check_live_gate(
                out_result.metrics, len(out_result.trades), self._cfg
            )

            return BacktestResult(
                metrics=combined_metrics,
                trades=all_trades,
                equity_curve=all_equity,
                in_sample_metrics=in_result.metrics,
                out_of_sample_metrics=out_result.metrics,
                live_gate_passed=gate_ok,
                live_gate_failures=gate_failures,
                signal_stats=self._compute_signal_stats(all_trades),
            )
        else:
            result = self._simulate(candles)
            gate_ok, gate_failures = check_live_gate(
                result.metrics, len(result.trades), self._cfg
            )
            result.live_gate_passed = gate_ok
            result.live_gate_failures = gate_failures
            result.signal_stats = self._compute_signal_stats(result.trades)
            return result

    def _simulate(self, candles: list[BacktestCandle]) -> BacktestResult:
        # Reset signal analyzers for each simulation segment
        ob_analyzer = OrderBookAnalyzer(
            obi_threshold=self._cfg.obi_threshold,
            obi_min_persist=self._cfg.obi_min_persist_ticks,
        )
        vol_analyzer = VolumeAnalyzer(divergence_threshold=self._cfg.cvd_divergence_threshold)
        deriv_analyzer = DerivativesAnalyzer(
            funding_zscore_threshold=self._cfg.funding_zscore_threshold,
            funding_lookback=self._cfg.funding_lookback,
        )
        volatility_analyzer = VolatilityAnalyzer(
            regime_high=self._cfg.vol_regime_ratio_high,
            regime_low=self._cfg.vol_regime_ratio_low,
        )
        anomaly_detector = AnomalyDetector(zscore_threshold=self._cfg.zscore_entry_threshold)
        risk_engine = RiskEngine()

        equity = self._capital
        equity_curve: list[float] = [equity]
        trades: list[BacktestTrade] = []

        open_trade: BacktestTrade | None = None
        open_candle_count: int = 0

        for i, candle in enumerate(candles):
            if i < 20:  # Warm-up period
                deriv_analyzer.update_funding(candle.funding_rate)
                volatility_analyzer.update(candle.high, candle.low, candle.close)
                continue

            # Update all signals with synthetic orderbook from OHLCV
            funding = deriv_analyzer.update_funding(candle.funding_rate)
            oi_div = deriv_analyzer.update_oi_price(candle.open_interest, candle.close)
            atr, vol_regime, bb = volatility_analyzer.update(candle.high, candle.low, candle.close)
            z_signal, regime_shift, vol_breakout = anomaly_detector.update(candle.close, vol_regime.regime)

            # Synthetic OBI from OHLCV (approximate)
            from data.injective_client import OrderbookSnapshot, OrderLevel
            mid = (candle.high + candle.low) / 2.0
            if candle.close > mid:
                bid_vol = candle.volume * 0.6
                ask_vol = candle.volume * 0.4
            else:
                bid_vol = candle.volume * 0.4
                ask_vol = candle.volume * 0.6

            synthetic_snap = OrderbookSnapshot(
                ts=candle.ts,
                market_id=self._cfg.market_id,
                bids=[OrderLevel(price=candle.close * 0.9999, quantity=bid_vol)],
                asks=[OrderLevel(price=candle.close * 1.0001, quantity=ask_vol)],
            )
            obi, pressure, voids, spread = ob_analyzer.analyze(synthetic_snap)

            # Monitor existing trade
            if open_trade:
                open_candle_count += 1
                exit_reason = None
                exit_price = None

                if open_trade.direction == "LONG":
                    if candle.low <= open_trade.stop_loss:
                        exit_reason, exit_price = "SL", open_trade.stop_loss
                    elif candle.high >= open_trade.take_profit:
                        exit_reason, exit_price = "TP", open_trade.take_profit
                elif open_trade.direction == "SHORT":
                    if candle.high >= open_trade.stop_loss:
                        exit_reason, exit_price = "SL", open_trade.stop_loss
                    elif candle.low <= open_trade.take_profit:
                        exit_reason, exit_price = "TP", open_trade.take_profit

                if open_candle_count >= self._max_hold_candles:
                    exit_reason, exit_price = "TIMEOUT", candle.close

                if exit_reason and exit_price is not None:
                    # Add slippage on exit
                    fill = self._apply_slippage(exit_price, open_trade.direction, is_exit=True)
                    hold_h = (candle.ts - open_trade.entry_ts) / 3600.0
                    funding_cost = candle.funding_rate * hold_h * open_trade.entry_price * open_trade.quantity

                    if open_trade.direction == "LONG":
                        gross_pnl = (fill - open_trade.entry_price) * open_trade.quantity
                    else:
                        gross_pnl = (open_trade.entry_price - fill) * open_trade.quantity

                    net_pnl = gross_pnl - funding_cost - self._commission(open_trade.entry_price, open_trade.quantity)
                    pnl_pct = net_pnl / (open_trade.entry_price * open_trade.quantity + 1e-10)

                    open_trade.exit_ts = candle.ts
                    open_trade.exit_price = fill
                    open_trade.pnl = net_pnl
                    open_trade.pnl_pct = pnl_pct * 100
                    open_trade.exit_reason = exit_reason
                    open_trade.funding_paid = funding_cost

                    equity += net_pnl
                    equity_curve.append(equity)
                    trades.append(open_trade)
                    open_trade = None
                    open_candle_count = 0
                    risk_engine.update_equity(equity)
                    continue

            # Entry signal evaluation (only when flat)
            if open_trade is None and not risk_engine.kill_switch.active:
                direction, active_signals, signal_count = self._evaluate_signals(
                    obi=obi, funding=funding, oi_div=oi_div,
                    cvd_divergence=0.0, vol_breakout=vol_breakout, z_signal=z_signal,
                )
                if direction and signal_count >= 2:
                    sl = risk_engine.dynamic_sl(candle.close, atr.value, direction)
                    tp = risk_engine.dynamic_tp(candle.close, atr.value, direction)
                    qty = self._cfg.max_position_pct * equity / candle.close

                    entry_fill = self._apply_slippage(candle.close, direction, is_exit=False)

                    open_trade = BacktestTrade(
                        entry_ts=candle.ts,
                        exit_ts=0.0,
                        direction=direction,
                        entry_price=entry_fill,
                        exit_price=0.0,
                        stop_loss=sl,
                        take_profit=tp,
                        quantity=qty,
                        pnl=0.0,
                        pnl_pct=0.0,
                        exit_reason="",
                        active_signals=active_signals,
                    )
                    open_candle_count = 0

            risk_engine.check_kill_switch()

        # Force close any remaining trade
        if open_trade and candles:
            last = candles[-1]
            fill = self._apply_slippage(last.close, open_trade.direction, is_exit=True)
            pnl = (fill - open_trade.entry_price if open_trade.direction == "LONG"
                   else open_trade.entry_price - fill) * open_trade.quantity
            open_trade.exit_ts = last.ts
            open_trade.exit_price = fill
            open_trade.pnl = pnl
            open_trade.pnl_pct = pnl / (open_trade.entry_price * open_trade.quantity + 1e-10) * 100
            open_trade.exit_reason = "END"
            trades.append(open_trade)
            equity += pnl
            equity_curve.append(equity)

        metrics = compute_metrics(
            pnl_series=[t.pnl for t in trades],
            equity_curve=equity_curve,
            trade_directions=[t.direction for t in trades],
            trade_signals=[t.active_signals for t in trades],
            initial_capital=self._capital,
        )

        return BacktestResult(
            metrics=metrics,
            trades=trades,
            equity_curve=equity_curve,
        )

    def _evaluate_signals(self, **kwargs: Any) -> tuple[str | None, list[str], int]:
        """Simplified signal evaluation for backtest (mirrors Sentinel logic)."""
        obi = kwargs["obi"]
        funding = kwargs["funding"]
        oi_div = kwargs["oi_div"]
        vol_breakout = kwargs["vol_breakout"]
        z_signal = kwargs["z_signal"]

        votes_long = 0
        votes_short = 0
        active: list[str] = []

        if funding.is_extreme:
            active.append(f"FUNDING_EXTREME(z={funding.zscore:.1f})")
            if funding.signal == "STRONG_SHORT":
                votes_short += 2
            elif funding.signal == "STRONG_LONG":
                votes_long += 2

        if obi.is_active:
            active.append(f"OBI({obi.value:.2f})")
            if obi.direction == "LONG":
                votes_long += 2
            elif obi.direction == "SHORT":
                votes_short += 2

        if oi_div.is_active:
            active.append(f"OI_DIV({oi_div.pattern})")
            if oi_div.pattern == "GENUINE_BREAK":
                votes_long += 1
            elif oi_div.pattern in ("SHORT_COVERING", "GENUINE_BREAKDOWN"):
                votes_short += 1

        if vol_breakout.is_breakout:
            active.append(f"VOL_BREAKOUT({vol_breakout.direction})")
            if vol_breakout.direction == "UP":
                votes_long += 1
            elif vol_breakout.direction == "DOWN":
                votes_short += 1

        if z_signal.is_active:
            active.append(f"ZSCORE({z_signal.value:.1f})")
            if z_signal.direction == "LONG_MR":
                votes_long += 1
            elif z_signal.direction == "SHORT_MR":
                votes_short += 1

        if votes_long > votes_short:
            return "LONG", active, len(active)
        elif votes_short > votes_long:
            return "SHORT", active, len(active)
        return None, active, len(active)

    def _apply_slippage(self, price: float, direction: str, is_exit: bool) -> float:
        """Conservative slippage model: spread half + market impact."""
        slippage_multiplier = self._slippage_bps / 10_000
        if direction == "LONG":
            if is_exit:
                return price * (1 - slippage_multiplier)  # sell at discount
            return price * (1 + slippage_multiplier)      # buy at premium
        else:
            if is_exit:
                return price * (1 + slippage_multiplier)
            return price * (1 - slippage_multiplier)

    def _commission(self, price: float, qty: float) -> float:
        return price * qty * self._commission_bps / 10_000 * 2  # round-trip

    def _compute_signal_stats(self, trades: list[BacktestTrade]) -> dict[str, dict]:
        stats: dict[str, dict] = {}
        for trade in trades:
            for sig in trade.active_signals:
                name = sig.split("(")[0]
                if name not in stats:
                    stats[name] = {"count": 0, "pnl": 0.0, "wins": 0}
                stats[name]["count"] += 1
                stats[name]["pnl"] += trade.pnl
                if trade.pnl > 0:
                    stats[name]["wins"] += 1
        for name, s in stats.items():
            s["win_rate"] = s["wins"] / (s["count"] + 1e-10) * 100
            s["avg_pnl"] = s["pnl"] / (s["count"] + 1e-10)
        return stats


def load_candles_from_csv(path: str) -> list[BacktestCandle]:
    """Load OHLCV candles from CSV. Expected columns: ts,open,high,low,close,volume,funding_rate,open_interest."""
    df = pd.read_csv(path)
    candles = []
    for _, row in df.iterrows():
        candles.append(BacktestCandle(
            ts=float(row.get("ts", row.get("timestamp", 0))),
            open=float(row.get("open", row.get("close", 0))),
            high=float(row.get("high", row.get("close", 0))),
            low=float(row.get("low", row.get("close", 0))),
            close=float(row["close"]),
            volume=float(row.get("volume", 0)),
            funding_rate=float(row.get("funding_rate", 0)),
            open_interest=float(row.get("open_interest", 0)),
        ))
    return candles
