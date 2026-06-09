from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Generic, TypeVar

import numpy as np

T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    value: T
    expires_at: float


class TTLCache(Generic[T]):
    """Simple in-memory cache with TTL expiry. Thread-safe via asyncio lock."""

    def __init__(self, default_ttl: float = 5.0):
        self._store: dict[str, CacheEntry[T]] = {}
        self._lock = asyncio.Lock()
        self.default_ttl = default_ttl

    async def get(self, key: str) -> T | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None or time.monotonic() > entry.expires_at:
                return None
            return entry.value

    async def set(self, key: str, value: T, ttl: float | None = None) -> None:
        ttl = ttl if ttl is not None else self.default_ttl
        async with self._lock:
            self._store[key] = CacheEntry(value=value, expires_at=time.monotonic() + ttl)

    async def invalidate(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)


class RollingBuffer:
    """
    Circular buffer for time-series data.
    Stores the last `maxlen` observations as numpy arrays for fast signal computation.
    """

    def __init__(self, maxlen: int):
        self._prices: Deque[float] = deque(maxlen=maxlen)
        self._highs: Deque[float] = deque(maxlen=maxlen)
        self._lows: Deque[float] = deque(maxlen=maxlen)
        self._volumes: Deque[float] = deque(maxlen=maxlen)
        self._timestamps: Deque[float] = deque(maxlen=maxlen)
        self.maxlen = maxlen

    def push(
        self,
        price: float,
        high: float,
        low: float,
        volume: float,
        ts: float,
    ) -> None:
        self._prices.append(price)
        self._highs.append(high)
        self._lows.append(low)
        self._volumes.append(volume)
        self._timestamps.append(ts)

    @property
    def prices(self) -> np.ndarray:
        return np.array(self._prices)

    @property
    def highs(self) -> np.ndarray:
        return np.array(self._highs)

    @property
    def lows(self) -> np.ndarray:
        return np.array(self._lows)

    @property
    def volumes(self) -> np.ndarray:
        return np.array(self._volumes)

    @property
    def timestamps(self) -> np.ndarray:
        return np.array(self._timestamps)

    def __len__(self) -> int:
        return len(self._prices)

    @property
    def is_ready(self) -> bool:
        return len(self._prices) >= self.maxlen // 2


@dataclass
class FundingBuffer:
    """Stores rolling funding rate history for z-score computation."""

    rates: Deque[float] = field(default_factory=lambda: deque(maxlen=720))  # 30 days hourly

    def push(self, rate: float) -> None:
        self.rates.append(rate)

    def zscore(self, window: int = 72) -> float:
        if len(self.rates) < 5:
            return 0.0
        arr = np.array(list(self.rates)[-window:])
        mu, sigma = arr.mean(), arr.std()
        if sigma < 1e-10:
            return 0.0
        return float((arr[-1] - mu) / sigma)

    def current(self) -> float:
        return float(self.rates[-1]) if self.rates else 0.0


@dataclass
class OIBuffer:
    """Stores rolling open interest for divergence detection."""

    values: Deque[float] = field(default_factory=lambda: deque(maxlen=200))

    def push(self, oi: float) -> None:
        self.values.append(oi)

    def pct_change(self, n: int = 1) -> float:
        if len(self.values) < n + 1:
            return 0.0
        arr = np.array(list(self.values))
        return float((arr[-1] - arr[-1 - n]) / (arr[-1 - n] + 1e-10))

    def current(self) -> float:
        return float(self.values[-1]) if self.values else 0.0
