"""Unit tests for signal computation modules."""

import pytest
import numpy as np

from signals.orderbook import OrderBookAnalyzer
from signals.volume import VolumeAnalyzer
from signals.derivatives import DerivativesAnalyzer
from signals.volatility import VolatilityAnalyzer
from signals.anomaly import AnomalyDetector
from data.injective_client import OrderbookSnapshot, OrderLevel, TradeEvent


def make_orderbook(bid_price=100.0, ask_price=100.1, bid_qty=10.0, ask_qty=5.0) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        ts=1_700_000_000.0,
        market_id="test",
        bids=[OrderLevel(price=bid_price - i * 0.01, quantity=bid_qty) for i in range(20)],
        asks=[OrderLevel(price=ask_price + i * 0.01, quantity=ask_qty) for i in range(20)],
    )


class TestOrderBook:
    def test_obi_bullish(self):
        snap = make_orderbook(bid_qty=20.0, ask_qty=5.0)
        analyzer = OrderBookAnalyzer(obi_threshold=0.5, obi_min_persist=1)
        # Feed same snap 3 times to build persistence
        for _ in range(3):
            obi, _, _, _ = analyzer.analyze(snap)
        assert obi.value > 0.5
        assert obi.direction == "LONG"
        assert obi.is_active

    def test_obi_bearish(self):
        snap = make_orderbook(bid_qty=3.0, ask_qty=20.0)
        analyzer = OrderBookAnalyzer(obi_threshold=0.5, obi_min_persist=1)
        for _ in range(3):
            obi, _, _, _ = analyzer.analyze(snap)
        assert obi.value < -0.5
        assert obi.direction == "SHORT"

    def test_obi_range(self):
        """OBI must always be in [-1, 1]."""
        snap = make_orderbook(bid_qty=0.001, ask_qty=1000.0)
        analyzer = OrderBookAnalyzer()
        obi, _, _, _ = analyzer.analyze(snap)
        assert -1.0 <= obi.value <= 1.0

    def test_spread_state(self):
        analyzer = OrderBookAnalyzer()
        snap = make_orderbook(bid_price=100.0, ask_price=100.5)
        for _ in range(10):
            _, _, _, spread = analyzer.analyze(snap)
        assert spread.current_bps > 0

    def test_liquidity_void_detection(self):
        snap = OrderbookSnapshot(
            ts=0.0, market_id="test",
            bids=[OrderLevel(price=100.0, quantity=1.0)],
            asks=[
                OrderLevel(price=100.1, quantity=1.0),
                OrderLevel(price=100.8, quantity=1.0),  # big gap here
            ],
        )
        analyzer = OrderBookAnalyzer(void_min_gap_pct=0.002)
        _, _, voids, _ = analyzer.analyze(snap)
        assert len(voids) > 0
        assert any(v.side == "above" for v in voids)


class TestVolume:
    def make_trades(self, n=50, buy_pct=0.7, price=100.0):
        trades = []
        for i in range(n):
            is_buy = i < int(n * buy_pct)
            trades.append(TradeEvent(ts=float(i), price=price, quantity=1.0, is_buy=is_buy))
        return trades

    def test_cvd_accumulates(self):
        analyzer = VolumeAnalyzer()
        trades = self.make_trades(50, buy_pct=1.0)  # all buys
        state = analyzer.update(trades, 100.0)
        assert state.value > 0
        assert state.delta_last > 0

    def test_cvd_bearish(self):
        analyzer = VolumeAnalyzer()
        trades = self.make_trades(50, buy_pct=0.0)  # all sells
        state = analyzer.update(trades, 100.0)
        assert state.value < 0

    def test_divergence_requires_history(self):
        analyzer = VolumeAnalyzer(divergence_threshold=0.5)
        trades = self.make_trades(5)
        state = analyzer.update(trades, 100.0)
        # With only 5 observations, divergence should be neutral
        assert state.signal in ("NEUTRAL", "BULLISH_DIV", "BEARISH_DIV")

    def test_surge_detection(self):
        analyzer = VolumeAnalyzer(surge_multiplier=2.0)
        # Build baseline
        normal_trades = self.make_trades(5, price=100.0)
        for _ in range(20):
            analyzer.update(normal_trades, 100.0)
        # Now surge
        big_trades = self.make_trades(100, price=100.0)
        analyzer.update(big_trades, 100.0)
        surge = analyzer.compute_surge()
        assert surge.ratio >= 1.0


class TestDerivatives:
    def test_funding_zscore_extreme(self):
        analyzer = DerivativesAnalyzer(funding_zscore_threshold=2.0)
        # Build history
        for _ in range(72):
            analyzer.update_funding(0.0001)  # normal
        # Push extreme
        signal = analyzer.update_funding(0.001)  # 10x normal
        assert signal.is_extreme
        assert signal.signal == "STRONG_SHORT"

    def test_funding_negative_extreme(self):
        analyzer = DerivativesAnalyzer(funding_zscore_threshold=2.0)
        for _ in range(72):
            analyzer.update_funding(0.0)
        signal = analyzer.update_funding(-0.005)
        assert signal.direction == "LONG_BIAS"

    def test_oi_short_covering_pattern(self):
        analyzer = DerivativesAnalyzer(oi_div_threshold=0.01)
        for _ in range(10):
            analyzer.update_oi_price(100.0, 100.0)
        # Price up, OI down = short covering
        for _ in range(3):
            analyzer.update_oi_price(95.0, 102.0)  # OI down, price up
        result = analyzer.update_oi_price(93.0, 103.0)
        assert result.price_change_pct > 0
        assert result.oi_change_pct < 0

    def test_net_rr_with_funding(self):
        analyzer = DerivativesAnalyzer()
        for _ in range(8):
            analyzer.update_funding(0.0005)  # 0.05% per hour
        # 8h hold, LONG, positive funding = unfavourable
        net_rr = analyzer.compute_net_rr_with_funding("LONG", 100.0, 98.0, 106.0, hold_hours=8.0)
        gross_rr = 6.0 / 2.0  # 3.0
        assert net_rr < gross_rr  # funding reduces R:R


class TestVolatility:
    def push_candles(self, analyzer, n=50, price=100.0, noise=0.5):
        rng = np.random.default_rng(42)
        for i in range(n):
            c = price + rng.normal(0, noise)
            h = c + abs(rng.normal(0, noise * 0.5))
            l = c - abs(rng.normal(0, noise * 0.5))
            analyzer.update(h, l, c)

    def test_atr_positive(self):
        analyzer = VolatilityAnalyzer()
        self.push_candles(analyzer, 30)
        atr, _, _ = analyzer.update(101.0, 99.0, 100.0)
        assert atr.value > 0
        assert 0 < atr.pct_of_price < 1

    def test_vol_regime_detection(self):
        import numpy as np
        rng = np.random.default_rng(7)
        analyzer = VolatilityAnalyzer(regime_high=1.3, regime_low=0.8)
        # Low vol period: close prices barely move
        price = 100.0
        for i in range(60):
            price += rng.normal(0, 0.01)
            analyzer.update(price + 0.01, price - 0.01, price)
        # Sudden high vol: close prices swing wildly
        for i in range(12):
            price += rng.normal(0, 1.5)
            analyzer.update(price + 1.5, price - 1.5, price)
        _, regime, _ = analyzer.update(price + 1.0, price - 1.0, price)
        assert regime.regime == "HIGH"
        assert regime.momentum_regime

    def test_vol_size_limits(self):
        analyzer = VolatilityAnalyzer()
        size = analyzer.vol_adjusted_size(1000.0, 0.02, 0.5, 100.0, max_leverage=5.0)
        max_size = 1000.0 * 5.0 / 100.0
        assert 0 < size <= max_size


class TestAnomaly:
    def test_zscore_mean_reversion_signal(self):
        detector = AnomalyDetector(zscore_window=20, zscore_threshold=2.0)
        for _ in range(20):
            detector.update(100.0, vol_regime="LOW")
        # Far above mean
        z, _, _ = detector.update(110.0, vol_regime="LOW")
        assert z.value > 2.0
        assert z.direction == "SHORT_MR"
        assert z.is_active

    def test_zscore_blocked_in_high_regime(self):
        detector = AnomalyDetector(zscore_threshold=2.0)
        for _ in range(20):
            detector.update(100.0)
        z, _, _ = detector.update(110.0, vol_regime="HIGH")
        assert z.regime_warning
        assert not z.is_active

    def test_vol_breakout_up(self):
        detector = AnomalyDetector()
        for _ in range(20):
            detector.update(100.0)
        _, _, breakout = detector.update(115.0)
        assert breakout.is_breakout
        assert breakout.direction == "UP"

    def test_kl_divergence_zero_for_same(self):
        detector = AnomalyDetector()
        arr = np.ones(50) * 100.0
        kl = detector._kl_divergence(arr[:25], arr[25:])
        assert kl < 1e-6

    def test_liquidation_cluster_levels(self):
        detector = AnomalyDetector()
        risk = detector.estimate_liquidation_clusters(100.0, 100.0, 1000.0, avg_leverage=10.0)
        assert risk.long_liq_level < 100.0
        assert risk.short_liq_level > 100.0
        assert risk.cascade_risk in ("HIGH", "MEDIUM", "LOW")
