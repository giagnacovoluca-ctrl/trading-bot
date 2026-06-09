"""
Order Book signals — Tier A/B.

Segnali:
  1. OBI  — Order Book Imbalance  (Tier A)
  2. BidAskPressure               (Tier B)
  3. LiquidityVoid                (Tier B)
  4. SpreadExpansion              (Tier C — filter only)

Tutti i valori restituiti sono float normalizzati in [-1, 1] o [0, 1]
per facilitare la composizione con altri segnali.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque

import numpy as np

from data.injective_client import OrderbookSnapshot, OrderLevel


@dataclass
class OBISignal:
    value: float          # [-1, +1]; >0 = buy pressure
    direction: str        # "LONG" | "SHORT" | "NEUTRAL"
    persist_count: int    # how many consecutive ticks above threshold
    is_active: bool


@dataclass
class BidAskPressure:
    bid_pressure: float   # [0, +inf]; volume-weighted distance from mid
    ask_pressure: float
    ratio: float          # bid/ask pressure ratio; >1 = bullish


@dataclass
class LiquidityVoid:
    """A gap in the orderbook that price tends to fill."""
    exists: bool
    side: str             # "above" | "below"
    gap_start: float      # price where gap starts
    gap_end: float        # price where gap ends
    gap_pct: float        # gap size as fraction of mid price


@dataclass
class SpreadState:
    current_bps: float
    mean_bps: float
    zscore: float
    is_expanding: bool    # True when spread > mean + 2*std


class OrderBookAnalyzer:
    """
    Stateful analyzer: feeds orderbook snapshots and maintains
    rolling history for persistence checks and z-score computation.
    """

    def __init__(
        self,
        obi_threshold: float = 0.60,
        obi_min_persist: int = 3,
        depth: int = 10,
        spread_window: int = 50,
        void_min_gap_pct: float = 0.003,
    ) -> None:
        self.obi_threshold = obi_threshold
        self.obi_min_persist = obi_min_persist
        self.depth = depth
        self.void_min_gap_pct = void_min_gap_pct

        # OBI persistence tracking
        self._obi_history: Deque[float] = deque(maxlen=20)
        self._obi_above_threshold_count = 0

        # Spread history
        self._spread_history: Deque[float] = deque(maxlen=spread_window)

    def analyze(self, snap: OrderbookSnapshot) -> tuple[OBISignal, BidAskPressure, list[LiquidityVoid], SpreadState]:
        obi = self._compute_obi(snap)
        pressure = self._compute_pressure(snap)
        voids = self._detect_voids(snap)
        spread = self._analyze_spread(snap)
        return obi, pressure, voids, spread

    def _compute_obi(self, snap: OrderbookSnapshot) -> OBISignal:
        """
        OBI = (Σbid_vol - Σask_vol) / (Σbid_vol + Σask_vol)
        Computed on top `depth` levels.

        On-chain orderbooks have less spoofing bias because:
        - Each order requires gas to cancel
        - No dark pools or iceberg orders
        Therefore OBI is a more honest signal than on CEX.
        """
        n = self.depth
        bid_vol = sum(b.quantity for b in snap.bids[:n])
        ask_vol = sum(a.quantity for a in snap.asks[:n])
        total = bid_vol + ask_vol
        if total < 1e-10:
            obi_val = 0.0
        else:
            obi_val = (bid_vol - ask_vol) / total

        self._obi_history.append(obi_val)

        # Persistence: count consecutive ticks above threshold
        if abs(obi_val) >= self.obi_threshold:
            self._obi_above_threshold_count += 1
        else:
            self._obi_above_threshold_count = 0

        direction = "NEUTRAL"
        if obi_val > self.obi_threshold:
            direction = "LONG"
        elif obi_val < -self.obi_threshold:
            direction = "SHORT"

        is_active = (
            self._obi_above_threshold_count >= self.obi_min_persist
            and direction != "NEUTRAL"
        )

        return OBISignal(
            value=obi_val,
            direction=direction,
            persist_count=self._obi_above_threshold_count,
            is_active=is_active,
        )

    def _compute_pressure(self, snap: OrderbookSnapshot) -> BidAskPressure:
        """
        Bid pressure = Σ (bid_qty_i × (mid - bid_price_i))   (size × distance from mid)
        Ask pressure = Σ (ask_qty_i × (ask_price_i - mid))

        Levels closer to mid have more immediate impact on price.
        We use the reciprocal of distance as weight (more weight to closer levels).
        """
        if not snap.bids or not snap.asks:
            return BidAskPressure(0.0, 0.0, 1.0)

        mid = snap.mid
        if mid < 1e-10:
            return BidAskPressure(0.0, 0.0, 1.0)

        n = self.depth

        def _pressure(levels: list[OrderLevel], is_bid: bool) -> float:
            total = 0.0
            for lvl in levels[:n]:
                dist = abs(lvl.price - mid)
                if dist < 1e-10:
                    continue
                weight = 1.0 / dist
                total += lvl.quantity * weight
            return total

        bid_p = _pressure(snap.bids, is_bid=True)
        ask_p = _pressure(snap.asks, is_bid=False)
        ratio = bid_p / (ask_p + 1e-10)

        return BidAskPressure(bid_pressure=bid_p, ask_pressure=ask_p, ratio=ratio)

    def _detect_voids(self, snap: OrderbookSnapshot) -> list[LiquidityVoid]:
        """
        Liquidity void = gap between consecutive price levels > void_min_gap_pct.
        These act as price magnets: when price approaches a void, it tends to fill it quickly.
        """
        if not snap.bids or not snap.asks or snap.mid < 1e-10:
            return []

        voids: list[LiquidityVoid] = []
        min_gap = snap.mid * self.void_min_gap_pct

        # Check ask side (above current price)
        for i in range(len(snap.asks) - 1):
            gap = snap.asks[i + 1].price - snap.asks[i].price
            if gap >= min_gap:
                voids.append(LiquidityVoid(
                    exists=True,
                    side="above",
                    gap_start=snap.asks[i].price,
                    gap_end=snap.asks[i + 1].price,
                    gap_pct=gap / snap.mid,
                ))

        # Check bid side (below current price)
        for i in range(len(snap.bids) - 1):
            gap = snap.bids[i].price - snap.bids[i + 1].price
            if gap >= min_gap:
                voids.append(LiquidityVoid(
                    exists=True,
                    side="below",
                    gap_start=snap.bids[i + 1].price,
                    gap_end=snap.bids[i].price,
                    gap_pct=gap / snap.mid,
                ))

        return voids

    def _analyze_spread(self, snap: OrderbookSnapshot) -> SpreadState:
        """
        Spread expansion = spread_bps > mean + 2*std (rolling window).
        Expanding spread = low liquidity, high manipulation risk → filter signal.
        """
        self._spread_history.append(snap.spread_bps)
        arr = np.array(list(self._spread_history))

        if len(arr) < 5:
            return SpreadState(
                current_bps=snap.spread_bps,
                mean_bps=snap.spread_bps,
                zscore=0.0,
                is_expanding=False,
            )

        mean = float(arr.mean())
        std = float(arr.std())
        z = (snap.spread_bps - mean) / (std + 1e-10)

        return SpreadState(
            current_bps=snap.spread_bps,
            mean_bps=mean,
            zscore=z,
            is_expanding=z > 2.0,
        )

    def obi_recent_mean(self) -> float:
        if not self._obi_history:
            return 0.0
        return float(np.mean(list(self._obi_history)))
