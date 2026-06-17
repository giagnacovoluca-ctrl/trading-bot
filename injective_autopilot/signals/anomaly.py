"""
Anomaly / Regime-shift signals — Tier B/C.

Segnali:
  1. Z-Score mean reversion    (Tier C — solo in range market)
  2. Regime Shift Detection    (Tier B — leading indicator)
  3. Volatility Breakout       (Tier B — trigger confirmation)
  4. Liquidation Cluster Risk  (Tier B — per risk management)

Nota sul Z-Score:
  Z-Score da solo ha un win rate alto in range market ma devastante in trend.
  Usarlo SOLO quando Vol Regime = LOW (contracting).
  In HIGH regime, il Z-Score genera false reversals.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from typing import Deque

import numpy as np
from scipy import stats as scipy_stats


@dataclass
class ZScoreSignal:
    value: float
    direction: str          # "LONG_MR" | "SHORT_MR" | "NEUTRAL"
    is_active: bool         # only reliable in LOW vol regime
    regime_warning: bool    # True if trying to trade MR in HIGH vol regime


@dataclass
class RegimeShift:
    """
    Detects structural breaks in price distribution.
    Uses Kullback-Leibler divergence between recent vs historical distribution.
    """
    kl_divergence: float
    is_shift: bool
    direction: str          # "BULLISH_SHIFT" | "BEARISH_SHIFT" | "NEUTRAL"
    confidence: float       # [0, 1]


@dataclass
class VolBreakout:
    is_breakout: bool
    direction: str          # "UP" | "DOWN" | "NEUTRAL"
    strength: float         # breakout strength in sigma units


@dataclass
class LiquidationRisk:
    """
    Estimates cluster of liquidations near current price.
    Based on open interest concentration + leverage estimation.
    """
    long_liq_level: float   # estimated price where long cascade starts
    short_liq_level: float  # estimated price where short cascade starts
    risk_to_longs: float    # distance pct to long liquidation cluster
    risk_to_shorts: float   # distance pct to short liquidation cluster
    cascade_risk: str       # "HIGH" | "MEDIUM" | "LOW"


class AnomalyDetector:
    def __init__(
        self,
        zscore_window: int = 50,
        zscore_threshold: float = 2.0,
        regime_shift_window_short: int = 20,
        regime_shift_window_long: int = 100,
        kl_threshold: float = 0.3,
        vol_breakout_sigma: float = 2.0,
    ) -> None:
        self.zscore_window = zscore_window
        self.zscore_threshold = zscore_threshold
        self.regime_shift_window_short = regime_shift_window_short
        self.regime_shift_window_long = regime_shift_window_long
        self.kl_threshold = kl_threshold
        self.vol_breakout_sigma = vol_breakout_sigma

        self._prices: Deque[float] = deque(maxlen=regime_shift_window_long + 10)
        self._returns: Deque[float] = deque(maxlen=regime_shift_window_long + 10)

    def update(self, price: float, vol_regime: str = "NORMAL") -> tuple[ZScoreSignal, RegimeShift, VolBreakout]:
        if self._prices:
            ret = np.log(price / list(self._prices)[-1])
            self._returns.append(ret)
        self._prices.append(price)

        z_signal = self._compute_zscore(price, vol_regime)
        regime = self._detect_regime_shift()
        breakout = self._detect_vol_breakout(price)

        return z_signal, regime, breakout

    def _compute_zscore(self, price: float, vol_regime: str) -> ZScoreSignal:
        """
        z = (price - mean(prices[-window:])) / std(prices[-window:])

        Only reliable in LOW vol regime (mean-reverting market).
        In HIGH regime, price follows momentum, not mean reversion.
        """
        arr = np.array(list(self._prices))
        if len(arr) < 10:
            return ZScoreSignal(0.0, "NEUTRAL", False, False)

        window_data = arr[-self.zscore_window:]
        mu, sigma = window_data.mean(), window_data.std()
        z = float((price - mu) / (sigma + 1e-10))

        direction = "NEUTRAL"
        is_active = False
        regime_warning = vol_regime == "HIGH"

        if z <= -self.zscore_threshold:
            direction = "LONG_MR"
            is_active = not regime_warning
        elif z >= self.zscore_threshold:
            direction = "SHORT_MR"
            is_active = not regime_warning

        return ZScoreSignal(
            value=z,
            direction=direction,
            is_active=is_active,
            regime_warning=regime_warning,
        )

    def _detect_regime_shift(self) -> RegimeShift:
        """
        Detects distribution shifts using KL divergence.
        Recent distribution vs historical distribution.

        KL > threshold = regime shift detected.
        Direction: compare medians of both distributions.
        """
        arr = np.array(list(self._returns))
        if len(arr) < self.regime_shift_window_long:
            return RegimeShift(0.0, False, "NEUTRAL", 0.0)

        recent = arr[-self.regime_shift_window_short:]
        historical = arr[-self.regime_shift_window_long:-self.regime_shift_window_short]

        if len(historical) < 5:
            return RegimeShift(0.0, False, "NEUTRAL", 0.0)

        kl = self._kl_divergence(recent, historical)

        is_shift = kl > self.kl_threshold
        if not is_shift:
            return RegimeShift(kl, False, "NEUTRAL", 0.0)

        # Direction based on mean comparison
        if recent.mean() > historical.mean() + historical.std():
            direction = "BULLISH_SHIFT"
        elif recent.mean() < historical.mean() - historical.std():
            direction = "BEARISH_SHIFT"
        else:
            direction = "NEUTRAL"

        confidence = min(1.0, float((kl - self.kl_threshold) / self.kl_threshold))

        return RegimeShift(kl, True, direction, confidence)

    def _detect_vol_breakout(self, price: float) -> VolBreakout:
        """
        Volatility breakout: price exits BB boundaries.
        Works best as a CONFIRMATION signal after OBI or Funding triggers.
        """
        arr = np.array(list(self._prices))
        if len(arr) < 20:
            return VolBreakout(False, "NEUTRAL", 0.0)

        window = arr[-20:]
        mu, sigma = window.mean(), window.std()

        upper = mu + self.vol_breakout_sigma * sigma
        lower = mu - self.vol_breakout_sigma * sigma

        if price > upper:
            strength = (price - upper) / (sigma + 1e-10)
            return VolBreakout(True, "UP", float(strength))
        elif price < lower:
            strength = (lower - price) / (sigma + 1e-10)
            return VolBreakout(True, "DOWN", float(strength))

        return VolBreakout(False, "NEUTRAL", 0.0)

    @staticmethod
    def _kl_divergence(p_samples: np.ndarray, q_samples: np.ndarray, bins: int = 20) -> float:
        """
        Approximate KL divergence via histogram binning.
        KL(P||Q) = Σ P(x) log(P(x)/Q(x))
        """
        all_data = np.concatenate([p_samples, q_samples])
        data_range = all_data.max() - all_data.min()
        if data_range < 1e-12:
            return 0.0  # identical distributions
        bin_edges = np.linspace(all_data.min(), all_data.max(), bins + 1)
        eps = 1e-10

        p_hist, _ = np.histogram(p_samples, bins=bin_edges, density=True)
        q_hist, _ = np.histogram(q_samples, bins=bin_edges, density=True)

        # Normalise to probabilities
        p_hist = p_hist / (p_hist.sum() + eps)
        q_hist = q_hist / (q_hist.sum() + eps)

        # Clip to avoid log(0)
        p_hist = np.clip(p_hist, eps, None)
        q_hist = np.clip(q_hist, eps, None)

        kl = float(np.sum(p_hist * np.log(p_hist / q_hist)))
        return abs(kl)

    def estimate_liquidation_clusters(
        self,
        current_price: float,
        mark_price: float,
        open_interest: float,
        avg_leverage: float = 10.0,
    ) -> LiquidationRisk:
        """
        Rough estimation of liquidation cluster levels.
        With leverage L, longs get liquidated at ~entry × (1 - 1/L × 0.9)
        Assuming average entry near current price range.

        This is a heuristic, not precise, but useful for risk management.
        """
        if current_price < 1e-10:
            return LiquidationRisk(0, 0, 0, 0, "LOW")

        # Estimated liquidation prices for leveraged positions
        # Maintenance margin rate ≈ 0.5% for most perp markets
        maint_margin = 0.005
        liq_offset_pct = (1.0 / avg_leverage) - maint_margin

        long_liq = current_price * (1.0 - liq_offset_pct)
        short_liq = current_price * (1.0 + liq_offset_pct)

        dist_to_long_liq = (current_price - long_liq) / current_price
        dist_to_short_liq = (short_liq - current_price) / current_price

        # Risk level based on proximity
        min_dist = min(dist_to_long_liq, dist_to_short_liq)
        if min_dist < 0.02:
            cascade_risk = "HIGH"
        elif min_dist < 0.05:
            cascade_risk = "MEDIUM"
        else:
            cascade_risk = "LOW"

        return LiquidationRisk(
            long_liq_level=long_liq,
            short_liq_level=short_liq,
            risk_to_longs=dist_to_long_liq,
            risk_to_shorts=dist_to_short_liq,
            cascade_risk=cascade_risk,
        )
