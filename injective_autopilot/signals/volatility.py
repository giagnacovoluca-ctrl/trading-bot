"""
Volatility signals — Tier B (essential for sizing and regime detection).

Segnali:
  1. ATR           — absolute risk metric for SL/TP placement
  2. Realized Vol  — annualised realized volatility
  3. Vol Regime    — expansion vs contraction regime
  4. BB Squeeze    — pre-breakout detection

Nota: questi segnali NON sono direzionali da soli.
Diventano potenti in combinazione con segnali direzionali (OBI, Funding, CVD).
Il Vol Regime determina principalmente il position sizing e la durata attesa del trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from typing import Deque

import numpy as np


@dataclass
class ATRState:
    value: float
    pct_of_price: float   # ATR as % of current price


@dataclass
class VolatilityRegime:
    realized_vol_short: float   # annualised, short window
    realized_vol_long: float    # annualised, long window
    ratio: float                # short/long
    regime: str                 # "HIGH" | "LOW" | "NORMAL"
    momentum_regime: bool       # True = expansion (momentum strategies work better)
    mean_reversion_regime: bool # True = contraction (MR strategies work better)


@dataclass
class BBState:
    upper: float
    lower: float
    mid: float
    bandwidth: float     # (upper - lower) / mid
    pct_b: float         # where price is relative to band [0,1]
    squeeze: bool        # bandwidth at 6-month minimum = pending breakout


class VolatilityAnalyzer:
    def __init__(
        self,
        atr_period: int = 14,
        vol_short_window: int = 12,   # e.g. 12 × 5min = 1h
        vol_long_window: int = 48,    # e.g. 48 × 5min = 4h
        bb_period: int = 20,
        bb_std: float = 2.0,
        regime_high: float = 1.5,
        regime_low: float = 0.70,
    ) -> None:
        self.atr_period = atr_period
        self.vol_short_window = vol_short_window
        self.vol_long_window = vol_long_window
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.regime_high = regime_high
        self.regime_low = regime_low

        self._highs: Deque[float] = deque(maxlen=max(atr_period, vol_long_window, bb_period) + 10)
        self._lows: Deque[float] = deque(maxlen=max(atr_period, vol_long_window, bb_period) + 10)
        self._closes: Deque[float] = deque(maxlen=max(atr_period, vol_long_window, bb_period) + 10)
        self._bb_widths: Deque[float] = deque(maxlen=200)  # for squeeze detection

    def update(self, high: float, low: float, close: float) -> tuple[ATRState, VolatilityRegime, BBState]:
        self._highs.append(high)
        self._lows.append(low)
        self._closes.append(close)

        atr = self._compute_atr(close)
        regime = self._compute_vol_regime()
        bb = self._compute_bb(close)

        return atr, regime, bb

    def _compute_atr(self, close: float) -> ATRState:
        """
        True Range = max(H-L, |H-C_prev|, |L-C_prev|)
        ATR = EMA(TR, period)
        """
        closes = np.array(list(self._closes))
        highs = np.array(list(self._highs))
        lows = np.array(list(self._lows))

        if len(closes) < 2:
            return ATRState(value=0.0, pct_of_price=0.0)

        n = min(self.atr_period, len(closes) - 1)
        tr_values = []
        for i in range(1, n + 1):
            idx = -i
            prev_close = closes[idx - 1]
            h = highs[idx]
            l = lows[idx]
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            tr_values.append(tr)

        atr_val = float(np.mean(tr_values)) if tr_values else 0.0
        pct = atr_val / (close + 1e-10)

        return ATRState(value=atr_val, pct_of_price=pct)

    def _compute_vol_regime(self) -> VolatilityRegime:
        """
        Realized Vol (annualised) = std(log_returns) × sqrt(candles_per_year)
        For 5-minute candles: candles_per_year = 365 × 24 × 12 = 105,120
        For 1-minute candles: 525,600
        We use sqrt(252 × periods_per_day) as annualisation factor.

        Regime ratio = short_vol / long_vol
          > regime_high → expanding vol = momentum strategies
          < regime_low  → contracting vol = mean reversion strategies
        """
        closes = np.array(list(self._closes))
        if len(closes) < self.vol_long_window + 1:
            return VolatilityRegime(0, 0, 1.0, "NORMAL", False, False)

        log_rets = np.log(closes[1:] / closes[:-1])

        ann_factor = np.sqrt(365 * 24 * 12)  # 5-min candles assumed

        vol_short = float(log_rets[-self.vol_short_window:].std() * ann_factor)
        vol_long = float(log_rets[-self.vol_long_window:].std() * ann_factor)

        ratio = vol_short / (vol_long + 1e-10)

        if ratio > self.regime_high:
            regime = "HIGH"
        elif ratio < self.regime_low:
            regime = "LOW"
        else:
            regime = "NORMAL"

        return VolatilityRegime(
            realized_vol_short=vol_short,
            realized_vol_long=vol_long,
            ratio=ratio,
            regime=regime,
            momentum_regime=ratio > self.regime_high,
            mean_reversion_regime=ratio < self.regime_low,
        )

    def _compute_bb(self, close: float) -> BBState:
        """
        Bollinger Bands: mid ± bb_std × σ
        Squeeze: bandwidth at multi-period minimum → pending breakout.
        """
        closes = np.array(list(self._closes))
        if len(closes) < self.bb_period:
            return BBState(close, close, close, 0.0, 0.5, False)

        window = closes[-self.bb_period:]
        mid = float(window.mean())
        std = float(window.std())

        upper = mid + self.bb_std * std
        lower = mid - self.bb_std * std
        bandwidth = (upper - lower) / (mid + 1e-10)
        pct_b = (close - lower) / (upper - lower + 1e-10)

        self._bb_widths.append(bandwidth)
        bw_arr = np.array(list(self._bb_widths))
        squeeze = bandwidth <= float(np.percentile(bw_arr, 10)) if len(bw_arr) >= 20 else False

        return BBState(
            upper=upper,
            lower=lower,
            mid=mid,
            bandwidth=bandwidth,
            pct_b=pct_b,
            squeeze=squeeze,
        )

    def vol_adjusted_size(
        self,
        capital: float,
        risk_pct: float,
        atr: float,
        price: float,
        max_leverage: float = 5.0,
    ) -> float:
        """
        Kelly-inspired position sizing adjusted for volatility.
        size = (capital × risk_pct) / ATR

        Capped by max_leverage × capital / price.
        """
        if atr < 1e-10 or price < 1e-10:
            return 0.0
        risk_dollars = capital * risk_pct
        raw_qty = risk_dollars / atr
        max_qty = (capital * max_leverage) / price
        return min(raw_qty, max_qty)
