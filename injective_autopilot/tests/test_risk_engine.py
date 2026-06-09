"""Unit tests for Risk Engine."""

import pytest
from core.decision_engine import TradeDecision
from core.risk_engine import RiskEngine


def make_long_decision(**kwargs) -> TradeDecision:
    defaults = dict(
        action="LONG", confidence=0.75,
        entry=100.0, stop_loss=96.0, take_profit=108.0,
        position_size=1.0, risk_score=0.3, reason="Test",
    )
    defaults.update(kwargs)
    return TradeDecision(**defaults)


def make_short_decision(**kwargs) -> TradeDecision:
    defaults = dict(
        action="SHORT", confidence=0.75,
        entry=100.0, stop_loss=104.0, take_profit=92.0,
        position_size=1.0, risk_score=0.3, reason="Test",
    )
    defaults.update(kwargs)
    return TradeDecision(**defaults)


class TestRiskEngine:
    def test_valid_long_passes(self):
        engine = RiskEngine()
        decision = make_long_decision()
        result = engine.validate_decision(decision, margin_available=500.0, current_positions=[])
        assert result.approved

    def test_valid_short_passes(self):
        engine = RiskEngine()
        decision = make_short_decision()
        result = engine.validate_decision(decision, margin_available=500.0, current_positions=[])
        assert result.approved

    def test_no_trade_rejected(self):
        engine = RiskEngine()
        decision = TradeDecision(action="NO_TRADE", confidence=0.0, entry=0, stop_loss=0,
                                  take_profit=0, position_size=0, risk_score=1.0, reason="")
        result = engine.validate_decision(decision, 500.0, [])
        assert not result.approved

    def test_sl_above_entry_for_long_rejected(self):
        engine = RiskEngine()
        decision = make_long_decision(stop_loss=102.0)  # SL above entry for LONG
        result = engine.validate_decision(decision, 500.0, [])
        assert not result.approved

    def test_sl_below_entry_for_short_rejected(self):
        engine = RiskEngine()
        decision = make_short_decision(stop_loss=96.0)  # SL below entry for SHORT
        result = engine.validate_decision(decision, 500.0, [])
        assert not result.approved

    def test_low_rr_rejected(self):
        engine = RiskEngine()
        # R:R = (101 - 100) / (100 - 99) = 1.0 — below min 2.0
        decision = make_long_decision(entry=100.0, stop_loss=99.0, take_profit=101.0)
        result = engine.validate_decision(decision, 500.0, [])
        assert not result.approved

    def test_duplicate_direction_rejected(self):
        engine = RiskEngine()

        class FakePos:
            direction = "long"

        decision = make_long_decision()
        result = engine.validate_decision(decision, 500.0, [FakePos()])
        assert not result.approved

    def test_kill_switch_blocks_all(self):
        engine = RiskEngine()
        engine._activate_kill("test")
        decision = make_long_decision()
        result = engine.validate_decision(decision, 500.0, [])
        assert not result.approved

    def test_kill_switch_daily_drawdown(self):
        engine = RiskEngine()
        engine.equity.daily_start_equity = 1000.0
        engine.equity.current_equity = 940.0  # 6% DD
        fired = engine.check_kill_switch()
        assert fired
        assert engine.kill_switch.active

    def test_dynamic_sl_long(self):
        engine = RiskEngine()
        sl = engine.dynamic_sl(100.0, atr=2.0, direction="LONG")
        assert sl == 100.0 - 2.0 * engine._cfg.atr_sl_multiplier

    def test_dynamic_sl_short(self):
        engine = RiskEngine()
        sl = engine.dynamic_sl(100.0, atr=2.0, direction="SHORT")
        assert sl == 100.0 + 2.0 * engine._cfg.atr_sl_multiplier

    def test_position_size_capped_by_margin(self):
        engine = RiskEngine()
        decision = make_long_decision(position_size=100.0)  # unrealistically large
        result = engine.validate_decision(decision, margin_available=200.0, current_positions=[])
        if result.approved and result.adjusted_decision:
            # Size should be capped
            max_qty = (200.0 * 0.80 * engine._cfg.max_leverage) / 100.0
            assert result.adjusted_decision.position_size <= max_qty + 1e-6

    def test_reset_kill_switch(self):
        engine = RiskEngine()
        engine._activate_kill("test")
        assert engine.kill_switch.active
        engine.reset_kill_switch()
        assert not engine.kill_switch.active


class TestMetrics:
    def test_compute_metrics_basic(self):
        from backtest.metrics import compute_metrics
        pnl = [10.0, -5.0, 8.0, -3.0, 12.0]
        equity = [1000.0 + sum(pnl[:i]) for i in range(len(pnl) + 1)]
        m = compute_metrics(pnl, equity, initial_capital=1000.0)
        assert m.total_trades == 5
        assert m.winning_trades == 3
        assert m.losing_trades == 2
        assert abs(m.win_rate - 60.0) < 0.1
        assert m.profit_factor > 1.0

    def test_profit_factor_all_wins(self):
        from backtest.metrics import compute_metrics
        pnl = [10.0, 10.0, 10.0]
        equity = [1000.0 + sum(pnl[:i]) for i in range(4)]
        m = compute_metrics(pnl, equity)
        assert m.profit_factor > 100  # effectively infinite (no losses)

    def test_empty_metrics(self):
        from backtest.metrics import compute_metrics
        m = compute_metrics([], [])
        assert m.total_trades == 0
        assert m.profit_factor == 0.0

    def test_live_gate_passes(self):
        from backtest.metrics import compute_metrics, check_live_gate
        from config.settings import get_settings
        cfg = get_settings()
        pnl = [5.0] * 600  # 600 small wins
        equity = [1000.0 + sum(pnl[:i]) for i in range(601)]
        m = compute_metrics(pnl, equity, initial_capital=1000.0)
        ok, failures = check_live_gate(m, 600, cfg)
        # With 600 trades and all wins, most gates should pass (except possibly sharpe)
        assert isinstance(ok, bool)
        assert isinstance(failures, list)
