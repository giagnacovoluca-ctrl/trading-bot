"""
Volume-based signals — Tier B.

Segnali:
  1. CVD (Cumulative Volume Delta)  — momentum confirmation/divergence
  2. Delta Volume per tick           — directional flow
  3. Volume Surge                    — anomaly detection

Rationale:
  CVD divergence dal prezzo identifica momenti di momentum exhaustion.
  Es: prezzo sale ma CVD scende = vendita aggressiva mascherata da rally passivo.
  Questo è un leading indicator rispetto ai reversal, non lagging.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque

import numpy as np

from data.injective_client import TradeEvent


@dataclass
class CVDState:
    value: float              # current CVD (cumulative)
    delta_last: float         # delta of last observation
    price_change_pct: float   # price change over same window
    divergence: float         # CVD z-score - price z-score; >0 = bullish divergence
    signal: str               # "BULLISH_DIV" | "BEARISH_DIV" | "NEUTRAL"
    is_active: bool


@dataclass
class VolumeSurge:
    ratio: float              # current / rolling_mean
    is_surge: bool            # True when ratio > threshold
    direction: str            # dominant side "BUY" | "SELL" | "NEUTRAL"


class VolumeAnalyzer:
    """
    Stateful CVD tracker.

    On Injective, trade side is determined by the SDK's trade_direction field.
    Fallback: compare trade price vs mid price (above = buyer-initiated).
    """

    def __init__(
        self,
        window: int = 50,
        surge_multiplier: float = 2.5,
        divergence_threshold: float = 0.65,
    ) -> None:
        self.window = window
        self.surge_multiplier = surge_multiplier
        self.divergence_threshold = divergence_threshold

        self._cvd: float = 0.0
        self._cvd_history: Deque[float] = deque(maxlen=window)
        self._price_history: Deque[float] = deque(maxlen=window)
        self._buy_vol: Deque[float] = deque(maxlen=window)
        self._sell_vol: Deque[float] = deque(maxlen=window)

    def update(self, trades: list[TradeEvent], current_price: float) -> CVDState:
        if not trades:
            return CVDState(
                value=self._cvd,
                delta_last=0.0,
                price_change_pct=0.0,
                divergence=0.0,
                signal="NEUTRAL",
                is_active=False,
            )

        # Aggregate this batch
        buy_vol = sum(t.quantity * t.price for t in trades if t.is_buy)
        sell_vol = sum(t.quantity * t.price for t in trades if not t.is_buy)
        delta = buy_vol - sell_vol

        self._cvd += delta
        self._cvd_history.append(self._cvd)
        self._price_history.append(current_price)
        self._buy_vol.append(buy_vol)
        self._sell_vol.append(sell_vol)

        # Compute divergence: z-score(CVD change) vs z-score(price change)
        divergence, signal = self._compute_divergence()

        return CVDState(
            value=self._cvd,
            delta_last=delta,
            price_change_pct=self._price_pct_change(),
            divergence=divergence,
            signal=signal,
            is_active=abs(divergence) >= self.divergence_threshold,
        )

    def _compute_divergence(self) -> tuple[float, str]:
        """
        Divergence = z_cvd - z_price over rolling window.
        Positive divergence: CVD stronger than price → bullish.
        Negative divergence: CVD weaker than price → bearish.
        """
        if len(self._cvd_history) < 10:
            return 0.0, "NEUTRAL"

        cvd_arr = np.array(list(self._cvd_history))
        price_arr = np.array(list(self._price_history))

        # Normalise both series to compare
        def _zscore(arr: np.ndarray) -> np.ndarray:
            mu, sigma = arr.mean(), arr.std()
            if sigma < 1e-10:
                return np.zeros_like(arr)
            return (arr - mu) / sigma

        z_cvd = _zscore(cvd_arr)
        z_price = _zscore(price_arr)
        divergence = float(z_cvd[-1] - z_price[-1])

        if divergence >= self.divergence_threshold:
            signal = "BULLISH_DIV"
        elif divergence <= -self.divergence_threshold:
            signal = "BEARISH_DIV"
        else:
            signal = "NEUTRAL"

        return divergence, signal

    def _price_pct_change(self) -> float:
        if len(self._price_history) < 2:
            return 0.0
        arr = np.array(list(self._price_history))
        return float((arr[-1] - arr[0]) / (arr[0] + 1e-10))

    def compute_surge(self) -> VolumeSurge:
        if len(self._buy_vol) < 5:
            return VolumeSurge(ratio=1.0, is_surge=False, direction="NEUTRAL")

        buy_arr = np.array(list(self._buy_vol))
        sell_arr = np.array(list(self._sell_vol))
        total_arr = buy_arr + sell_arr

        mean_vol = total_arr[:-1].mean() if len(total_arr) > 1 else total_arr[-1]
        current_vol = total_arr[-1]
        ratio = current_vol / (mean_vol + 1e-10)

        is_surge = ratio > self.surge_multiplier

        if buy_arr[-1] > sell_arr[-1] * 1.5:
            direction = "BUY"
        elif sell_arr[-1] > buy_arr[-1] * 1.5:
            direction = "SELL"
        else:
            direction = "NEUTRAL"

        return VolumeSurge(ratio=ratio, is_surge=is_surge, direction=direction)

    def reset_cvd(self) -> None:
        """Reset CVD (e.g. start of new session)."""
        self._cvd = 0.0
        self._cvd_history.clear()
