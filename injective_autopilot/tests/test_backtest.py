"""Unit tests for the Backtest Engine."""

import pytest
import numpy as np
from backtest.engine import BacktestEngine, BacktestCandle


def make_candles(n=300, start_price=100.0, trend=0.0, vol=0.5, seed=42) -> list[BacktestCandle]:
    rng = np.random.default_rng(seed)
    candles = []
    price = start_price
    for i in range(n):
        ret = trend + rng.normal(0, vol)
        price = price * (1 + ret / 100)
        h = price * (1 + abs(rng.normal(0, vol * 0.5)) / 100)
        l = price * (1 - abs(rng.normal(0, vol * 0.5)) / 100)
        funding = rng.normal(0.0001, 0.0002)
        oi = 1000.0 + rng.normal(0, 50)
        candles.append(BacktestCandle(
            ts=float(1_700_000_000 + i * 300),
            open=price, high=h, low=l, close=price,
            volume=rng.uniform(1000, 5000),
            funding_rate=funding,
            open_interest=abs(oi),
        ))
    return candles


class TestBacktestEngine:
    def test_runs_without_error(self):
        engine = BacktestEngine(initial_capital=1000.0)
        candles = make_candles(300)
        result = engine.run(candles, walk_forward=False)
        assert result is not None
        assert isinstance(result.equity_curve, list)

    def test_walk_forward_split(self):
        engine = BacktestEngine(initial_capital=1000.0)
        candles = make_candles(400)
        result = engine.run(candles, walk_forward=True)
        assert result.in_sample_metrics is not None
        assert result.out_of_sample_metrics is not None

    def test_equity_curve_starts_at_capital(self):
        engine = BacktestEngine(initial_capital=1000.0)
        candles = make_candles(100)
        result = engine.run(candles, walk_forward=False)
        assert abs(result.equity_curve[0] - 1000.0) < 0.01

    def test_trades_have_valid_pnl(self):
        engine = BacktestEngine(initial_capital=1000.0)
        candles = make_candles(300)
        result = engine.run(candles, walk_forward=False)
        for trade in result.trades:
            assert trade.direction in ("LONG", "SHORT")
            assert trade.exit_reason in ("SL", "TP", "TIMEOUT", "END")
            assert isinstance(trade.pnl, float)

    def test_slippage_is_applied(self):
        engine = BacktestEngine(slippage_bps=10.0)
        fill = engine._apply_slippage(100.0, "LONG", is_exit=False)
        assert fill > 100.0  # bought at premium

        fill_exit = engine._apply_slippage(100.0, "LONG", is_exit=True)
        assert fill_exit < 100.0  # sold at discount

    def test_commission_positive(self):
        engine = BacktestEngine(commission_bps=2.0)
        comm = engine._commission(100.0, 1.0)
        assert comm > 0

    def test_live_gate_result_populated(self):
        engine = BacktestEngine(initial_capital=1000.0)
        candles = make_candles(300)
        result = engine.run(candles, walk_forward=False)
        assert isinstance(result.live_gate_passed, bool)
        assert isinstance(result.live_gate_failures, list)

    def test_not_enough_candles_raises(self):
        engine = BacktestEngine()
        with pytest.raises(ValueError):
            engine.run(make_candles(50), walk_forward=False)

    def test_signal_stats_populated(self):
        engine = BacktestEngine(initial_capital=1000.0)
        candles = make_candles(400)
        result = engine.run(candles, walk_forward=False)
        # Signal stats should be a dict (may be empty if no trades triggered)
        assert isinstance(result.signal_stats, dict)
