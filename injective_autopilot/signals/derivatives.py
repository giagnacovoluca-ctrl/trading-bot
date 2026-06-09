"""
Derivati-specific signals — Tier S/A.

Segnali:
  1. Funding Rate Extreme (Tier S) — il segnale più potente su Injective Perp
  2. OI/Price Divergence  (Tier A) — short covering vs genuine buying
  3. Funding Dislocation  (Tier A) — funding estremo che non si corregge = trend forte

Razionale funding:
  Injective ha meno market maker di Binance → funding si sbilancia più facilmente.
  Quando funding > +2.5 sigma storico → mercato è long-heavy → alta probabilità di flush.
  Quando funding < -2 sigma storico → mercato è short-heavy → alta probabilità di short squeeze.

  IMPORTANTE: funding estremo NON è un segnale di reversal immediato se OI cresce.
  Funding estremo + OI in calo = STRONG REVERSAL SIGNAL.
  Funding estremo + OI in crescita = momentum continuation, aspettare.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from typing import Deque

import numpy as np


@dataclass
class FundingSignal:
    current_rate: float       # raw hourly rate (e.g. 0.0001)
    zscore: float             # z-score vs rolling history
    direction: str            # "SHORT_BIAS" | "LONG_BIAS" | "NEUTRAL"
    annual_cost_pct: float    # annualised funding cost (rate * 24 * 365 * 100)
    is_extreme: bool
    signal: str               # "STRONG_SHORT" | "STRONG_LONG" | "NEUTRAL"


@dataclass
class OIDivergenceSignal:
    price_change_pct: float
    oi_change_pct: float
    divergence_score: float   # positive = bullish (OI confirms price), negative = bearish
    pattern: str              # "SHORT_COVERING" | "LONG_COVERING" | "GENUINE_BREAK" | "NEUTRAL"
    is_active: bool


@dataclass
class FundingDislocation:
    """
    Persistent extreme funding without correction = strong trend signal.
    When funding stays extreme for N hours, it means strong directional conviction.
    """
    extreme_hours: int        # how many consecutive hours funding has been extreme
    is_persistent: bool       # True when extreme_hours > threshold
    bias: str                 # "LONG" | "SHORT"


class DerivativesAnalyzer:
    def __init__(
        self,
        funding_zscore_threshold: float = 2.5,
        funding_lookback: int = 72,
        oi_div_threshold: float = 0.015,
        funding_persistence_hours: int = 6,
    ) -> None:
        self.funding_zscore_threshold = funding_zscore_threshold
        self.oi_div_threshold = oi_div_threshold
        self.funding_persistence_hours = funding_persistence_hours

        self._funding_history: Deque[float] = deque(maxlen=funding_lookback)
        self._oi_history: Deque[float] = deque(maxlen=200)
        self._price_history: Deque[float] = deque(maxlen=200)

        self._funding_extreme_count: int = 0
        self._last_extreme_direction: str = "NEUTRAL"

    def update_funding(self, rate: float) -> FundingSignal:
        """
        Funding Z-Score:
          z = (rate_current - mean(rates[-N:])) / std(rates[-N:])

        Threshold: |z| > 2.5 signals mean-reversion entry.
        High funding → short bias (market is overleveraged long).
        Low funding → long bias (market is overleveraged short).

        Annual cost = rate × 24h × 365 × 100 (%)
        """
        self._funding_history.append(rate)
        arr = np.array(list(self._funding_history))

        if len(arr) < 5:
            return FundingSignal(
                current_rate=rate,
                zscore=0.0,
                direction="NEUTRAL",
                annual_cost_pct=rate * 24 * 365 * 100,
                is_extreme=False,
                signal="NEUTRAL",
            )

        mu, sigma = arr.mean(), arr.std()
        z = float((rate - mu) / (sigma + 1e-10))

        direction = "NEUTRAL"
        if rate > 0:
            direction = "SHORT_BIAS"   # longs paying → shorts favoured
        elif rate < 0:
            direction = "LONG_BIAS"    # shorts paying → longs favoured

        is_extreme = abs(z) >= self.funding_zscore_threshold

        if is_extreme and direction == "SHORT_BIAS":
            signal = "STRONG_SHORT"
            if self._last_extreme_direction == "STRONG_SHORT":
                self._funding_extreme_count += 1
            else:
                self._funding_extreme_count = 1
            self._last_extreme_direction = "STRONG_SHORT"
        elif is_extreme and direction == "LONG_BIAS":
            signal = "STRONG_LONG"
            if self._last_extreme_direction == "STRONG_LONG":
                self._funding_extreme_count += 1
            else:
                self._funding_extreme_count = 1
            self._last_extreme_direction = "STRONG_LONG"
        else:
            signal = "NEUTRAL"
            self._funding_extreme_count = 0
            self._last_extreme_direction = "NEUTRAL"

        return FundingSignal(
            current_rate=rate,
            zscore=z,
            direction=direction,
            annual_cost_pct=rate * 24 * 365 * 100,
            is_extreme=is_extreme,
            signal=signal,
        )

    def update_oi_price(self, oi: float, price: float, n: int = 3) -> OIDivergenceSignal:
        """
        OI/Price divergence:
          price_chg = (price[-1] - price[-n]) / price[-n]
          oi_chg   = (oi[-1] - oi[-n]) / oi[-n]

        Patterns:
          price ↑ + OI ↑ = GENUINE_BREAK (new money entering)
          price ↑ + OI ↓ = SHORT_COVERING (not genuine — reversal risk high)
          price ↓ + OI ↑ = GENUINE_BREAKDOWN
          price ↓ + OI ↓ = LONG_COVERING (not genuine — reversal risk high)
        """
        self._oi_history.append(oi)
        self._price_history.append(price)

        if len(self._oi_history) < n + 1:
            return OIDivergenceSignal(
                price_change_pct=0.0,
                oi_change_pct=0.0,
                divergence_score=0.0,
                pattern="NEUTRAL",
                is_active=False,
            )

        oi_arr = np.array(list(self._oi_history))
        px_arr = np.array(list(self._price_history))

        price_chg = float((px_arr[-1] - px_arr[-n - 1]) / (px_arr[-n - 1] + 1e-10))
        oi_chg = float((oi_arr[-1] - oi_arr[-n - 1]) / (oi_arr[-n - 1] + 1e-10))

        divergence = price_chg - oi_chg

        # Classify pattern
        if price_chg > self.oi_div_threshold and oi_chg > self.oi_div_threshold:
            pattern = "GENUINE_BREAK"
        elif price_chg > self.oi_div_threshold and oi_chg < -self.oi_div_threshold:
            pattern = "SHORT_COVERING"   # bearish: rally without new money
        elif price_chg < -self.oi_div_threshold and oi_chg > self.oi_div_threshold:
            pattern = "GENUINE_BREAKDOWN"
        elif price_chg < -self.oi_div_threshold and oi_chg < -self.oi_div_threshold:
            pattern = "LONG_COVERING"    # bullish: dump without new short money
        else:
            pattern = "NEUTRAL"

        is_active = pattern in ("SHORT_COVERING", "LONG_COVERING", "GENUINE_BREAK", "GENUINE_BREAKDOWN")

        return OIDivergenceSignal(
            price_change_pct=price_chg,
            oi_change_pct=oi_chg,
            divergence_score=divergence,
            pattern=pattern,
            is_active=is_active,
        )

    def funding_dislocation(self) -> FundingDislocation:
        """Persistent extreme funding = strong trend, not reversal."""
        is_persistent = self._funding_extreme_count >= self.funding_persistence_hours
        bias = self._last_extreme_direction.replace("STRONG_", "") if is_persistent else "NEUTRAL"
        return FundingDislocation(
            extreme_hours=self._funding_extreme_count,
            is_persistent=is_persistent,
            bias=bias,
        )

    def compute_net_rr_with_funding(
        self,
        direction: str,
        entry: float,
        sl: float,
        tp: float,
        hold_hours: float = 8.0,
    ) -> float:
        """
        Adjust R:R by expected funding cost over the hold period.
        Critical: a 2:1 gross trade can be <1:1 net if funding is unfavourable.

        Returns: net R:R ratio (≥2.0 required for entry).
        """
        if not self._funding_history:
            return abs(tp - entry) / (abs(entry - sl) + 1e-10)

        avg_funding = float(np.mean(list(self._funding_history)[-int(hold_hours):]))

        # Cost: if long in positive funding, you PAY; if short in positive funding, you RECEIVE
        if direction == "LONG":
            funding_cost_pct = avg_funding * hold_hours  # positive cost if avg_funding > 0
        else:
            funding_cost_pct = -avg_funding * hold_hours  # negative cost = gain for short

        gross_reward = abs(tp - entry)
        net_reward = gross_reward * (1.0 - funding_cost_pct)
        risk = abs(entry - sl)
        if risk < 1e-10:
            return 0.0

        return net_reward / risk
