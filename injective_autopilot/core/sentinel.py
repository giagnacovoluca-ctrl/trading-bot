"""
Livello 1 — Sentinella Quantitativa Multi-Market.

Scansiona i top 30 perpetual di Injective in parallelo (asyncio.gather).
Ogni market ha il suo MarketContext con buffer e analyzer indipendenti.

Trigger composito per market:
  - Almeno 2 segnali Tier A/B attivi, OPPURE
  - 1 segnale Tier S (Funding Extreme)

Rate limit: max sentinel_max_triggers_per_hour chiamate Claude totali (tutti i market).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from config.settings import MARKET_TICKER, get_settings
from data.cache import FundingBuffer, OIBuffer, RollingBuffer
from data.injective_client import InjectiveClient
from signals.anomaly import AnomalyDetector
from signals.derivatives import DerivativesAnalyzer
from signals.orderbook import OrderBookAnalyzer
from signals.volatility import ATRState, BBState, VolatilityAnalyzer, VolatilityRegime
from signals.volume import VolumeAnalyzer

log = logging.getLogger(__name__)


@dataclass
class SentinelTrigger:
    ts: float
    market_id: str
    ticker: str
    direction_bias: str          # "LONG" | "SHORT" | "MIXED"
    active_signals: list[str]
    signal_count: int
    orderbook_snapshot: dict[str, Any] = field(default_factory=dict)
    market_snapshot: dict[str, Any] = field(default_factory=dict)
    signal_values: dict[str, Any] = field(default_factory=dict)
    tier_s: bool = False


@dataclass
class SentinelStats:
    total_ticks: int = 0
    total_triggers: int = 0
    last_trigger_ts: float = 0.0
    last_tick_ts: float = 0.0
    errors: int = 0


@dataclass
class MarketContext:
    """Per-market state: independent buffers and signal analyzers."""
    market_id: str
    ticker: str
    price_buffer: RollingBuffer
    funding_buffer: FundingBuffer
    oi_buffer: OIBuffer
    ob_analyzer: OrderBookAnalyzer
    vol_analyzer: VolumeAnalyzer
    deriv_analyzer: DerivativesAnalyzer
    volatility_analyzer: VolatilityAnalyzer
    anomaly_detector: AnomalyDetector
    last_signal_types: set[str] = field(default_factory=set)


class Sentinel:
    def __init__(self, client: InjectiveClient) -> None:
        self._client = client
        self._cfg = get_settings()

        # Build one MarketContext per market
        self._markets: dict[str, MarketContext] = {}
        for mid in self._cfg.market_ids:
            ticker = MARKET_TICKER.get(mid, mid[:8])
            self._markets[mid] = MarketContext(
                market_id=mid,
                ticker=ticker,
                price_buffer=RollingBuffer(maxlen=self._cfg.lookback_candles),
                funding_buffer=FundingBuffer(),
                oi_buffer=OIBuffer(),
                ob_analyzer=OrderBookAnalyzer(
                    obi_threshold=self._cfg.obi_threshold,
                    obi_min_persist=self._cfg.obi_min_persist_ticks,
                    depth=self._cfg.orderbook_depth // 2,
                ),
                vol_analyzer=VolumeAnalyzer(
                    divergence_threshold=self._cfg.cvd_divergence_threshold,
                ),
                deriv_analyzer=DerivativesAnalyzer(
                    funding_zscore_threshold=self._cfg.funding_zscore_threshold,
                    funding_lookback=self._cfg.funding_lookback,
                    oi_div_threshold=self._cfg.oi_price_div_threshold,
                ),
                volatility_analyzer=VolatilityAnalyzer(
                    regime_high=self._cfg.vol_regime_ratio_high,
                    regime_low=self._cfg.vol_regime_ratio_low,
                ),
                anomaly_detector=AnomalyDetector(
                    zscore_threshold=self._cfg.zscore_entry_threshold,
                    vol_breakout_sigma=self._cfg.vol_breakout_sigma,
                ),
            )

        self.stats = SentinelStats()
        self._consecutive_errors: int = 0
        self._trigger_timestamps: list[float] = []  # shared rate limit across all markets
        self._last_trigger_per_market: dict[str, float] = {}  # per-market cooldown

    async def run(self, on_batch: Any) -> None:
        """
        Main loop. Calls on_batch(triggers) once per cycle with ALL triggers
        collected in that cycle (empty list if none fired).
        """
        log.info(
            "Sentinel started — scanning %d markets (interval=%ds)",
            len(self._markets),
            self._cfg.sentinel_interval_sec,
        )
        while True:
            tick_start = time.monotonic()
            try:
                triggers = await self._tick_all()
                if triggers:
                    await on_batch(triggers)
                self._consecutive_errors = 0
            except Exception as exc:
                self._consecutive_errors += 1
                self.stats.errors += 1
                log.error("Sentinel tick error #%d: %s", self._consecutive_errors, exc)
                if self._consecutive_errors >= self._cfg.max_consecutive_errors:
                    log.critical("Kill switch: max consecutive errors reached. Stopping sentinel.")
                    return

            elapsed = time.monotonic() - tick_start
            await asyncio.sleep(max(0.0, self._cfg.sentinel_interval_sec - elapsed))

    def get_signal_overlap(self, market_id: str, original_signals: list[str]) -> int | None:
        """Quanti dei segnali (per tipo, es. CVD_DIV/ZSCORE) che hanno aperto un
        trade sono ancora attivi ora sul market. None se il market non è tracciato."""
        ctx = self._markets.get(market_id)
        if ctx is None:
            return None
        original_types = {s.split("(")[0] for s in original_signals}
        return len(original_types & ctx.last_signal_types)

    async def _tick_all(self) -> list[SentinelTrigger]:
        """Poll all markets in parallel, return triggers for markets that fired."""
        self.stats.total_ticks += 1
        self.stats.last_tick_ts = time.time()

        tasks = [self._tick_market(ctx) for ctx in self._markets.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        triggers: list[SentinelTrigger] = []
        for r in results:
            if isinstance(r, SentinelTrigger):
                triggers.append(r)
            elif isinstance(r, Exception):
                log.debug("Market tick error: %s", r)

        return triggers

    async def _tick_market(self, ctx: MarketContext) -> SentinelTrigger | None:
        """Single-market tick. Returns trigger or None."""
        mid = ctx.market_id

        try:
            ob_snap, market_snap, trades = await asyncio.gather(
                self._client.fetch_orderbook(depth=self._cfg.orderbook_depth, market_id=mid),
                self._client.fetch_market_snapshot(market_id=mid),
                self._client.fetch_recent_trades(limit=200, market_id=mid),
            )
        except Exception as exc:
            log.debug("[%s] fetch error: %s", ctx.ticker, exc)
            return None

        price = market_snap.mark_price
        if price < 1e-10:
            return None

        # Update buffers
        ctx.price_buffer.push(
            price=price,
            high=price * 1.0005,
            low=price * 0.9995,
            volume=sum(t.quantity * t.price for t in trades),
            ts=market_snap.ts,
        )
        ctx.funding_buffer.push(market_snap.funding_rate)
        ctx.oi_buffer.push(market_snap.open_interest)

        # Compute signals
        obi, pressure, voids, spread = ctx.ob_analyzer.analyze(ob_snap)
        cvd = ctx.vol_analyzer.update(trades, price)
        vol_surge = ctx.vol_analyzer.compute_surge()
        funding = ctx.deriv_analyzer.update_funding(market_snap.funding_rate)
        oi_div = ctx.deriv_analyzer.update_oi_price(market_snap.open_interest, price)

        prices = ctx.price_buffer.prices
        if len(prices) >= 3:
            atr, vol_regime, bb = ctx.volatility_analyzer.update(
                high=max(prices[-3:]),
                low=min(prices[-3:]),
                close=prices[-1],
            )
        else:
            atr = ATRState(0.0, 0.0)
            vol_regime = VolatilityRegime(0.0, 0.0, 1.0, "NORMAL", False, False)
            bb = BBState(price, price, price, 0.0, 0.5, False)

        z_signal, regime_shift, vol_breakout = ctx.anomaly_detector.update(
            price, vol_regime.regime
        )

        # Spread filter
        if spread.is_expanding and spread.zscore > 3.0:
            return None

        # Build signal votes
        active_signals: list[str] = []
        votes_long = 0
        votes_short = 0

        if funding.is_extreme:
            active_signals.append(f"FUNDING_EXTREME(z={funding.zscore:.2f})")
            if funding.signal == "STRONG_SHORT":
                votes_short += 2
            elif funding.signal == "STRONG_LONG":
                votes_long += 2

        if obi.is_active:
            active_signals.append(f"OBI({obi.value:.2f},n={obi.persist_count})")
            if obi.direction == "LONG":
                votes_long += 2
            elif obi.direction == "SHORT":
                votes_short += 2

        if oi_div.is_active:
            active_signals.append(f"OI_DIV({oi_div.pattern})")
            if oi_div.pattern == "GENUINE_BREAK":
                votes_long += 1
            elif oi_div.pattern == "GENUINE_BREAKDOWN":
                votes_short += 1
            elif oi_div.pattern == "SHORT_COVERING":
                votes_short += 1
            elif oi_div.pattern == "LONG_COVERING":
                votes_long += 1

        if cvd.is_active:
            active_signals.append(f"CVD_DIV({cvd.signal})")
            if cvd.signal == "BULLISH_DIV":
                votes_long += 1
            elif cvd.signal == "BEARISH_DIV":
                votes_short += 1

        if vol_breakout.is_breakout:
            active_signals.append(f"VOL_BREAKOUT({vol_breakout.direction})")
            if vol_breakout.direction == "UP":
                votes_long += 1
            elif vol_breakout.direction == "DOWN":
                votes_short += 1

        if regime_shift.is_shift and regime_shift.confidence > 0.5:
            active_signals.append(f"REGIME_SHIFT({regime_shift.direction},conf={regime_shift.confidence:.2f})")
            if regime_shift.direction == "BULLISH_SHIFT":
                votes_long += 1
            elif regime_shift.direction == "BEARISH_SHIFT":
                votes_short += 1

        if z_signal.is_active and not z_signal.regime_warning:
            active_signals.append(f"ZSCORE({z_signal.value:.2f})")
            if z_signal.direction == "LONG_MR":
                votes_long += 1
            elif z_signal.direction == "SHORT_MR":
                votes_short += 1

        # Snapshot dei "tipi" di segnale attivi in questo tick, indipendentemente
        # dal trigger: serve per il recheck periodico delle posizioni aperte
        # (verifica se la tesi di trade originale è ancora valida).
        ctx.last_signal_types = {s.split("(")[0] for s in active_signals}

        tier_s_active = funding.is_extreme
        min_signals_required = 1 if tier_s_active else 2
        if len(active_signals) < min_signals_required:
            return None

        # Blocco combo negative: FUNDING_EXTREME+REGIME_SHIFT insieme = PF 0.52 su n=35.
        # Ogni entry in blocked_combos è un insieme; se tutti i segnali sono attivi → skip.
        active_types = {s.split("(")[0] for s in active_signals}
        for combo in self._cfg.sentinel_blocked_combos:
            if all(sig in active_types for sig in combo):
                log.debug("[%s] Trigger bloccato: combo negativa %s in %s",
                          ctx.ticker, combo, active_types)
                return None

        # Filtro segnali obbligatori (vuoto di default: nessun requisito).
        required = self._cfg.sentinel_required_signals
        if required and not any(r in active_types for r in required):
            log.debug("[%s] Trigger bloccato: nessun segnale richiesto (%s) in %s",
                      ctx.ticker, required, active_types)
            return None

        now = time.time()

        # Per-market cooldown: skip if same market fired too recently
        cooldown_sec = self._cfg.sentinel_trigger_cooldown_min * 60
        last = self._last_trigger_per_market.get(ctx.market_id, 0.0)
        if now - last < cooldown_sec:
            return None

        # Global rate limit: cap Gemini calls (quota/cost) in all modes
        self._trigger_timestamps = [t for t in self._trigger_timestamps if now - t < 3600]
        if len(self._trigger_timestamps) >= self._cfg.sentinel_max_triggers_per_hour:
            log.debug("[%s] Global trigger rate limit reached (%d/h)", ctx.ticker, self._cfg.sentinel_max_triggers_per_hour)
            return None
        self._trigger_timestamps.append(now)

        self._last_trigger_per_market[ctx.market_id] = now

        self.stats.total_triggers += 1
        self.stats.last_trigger_ts = now

        if votes_long > votes_short:
            direction_bias = "LONG"
        elif votes_short > votes_long:
            direction_bias = "SHORT"
        else:
            direction_bias = "MIXED"

        log.info(
            "TRIGGER [%s]: dir=%s signals=%d [%s]",
            ctx.ticker, direction_bias, len(active_signals), ", ".join(active_signals),
        )

        return SentinelTrigger(
            ts=now,
            market_id=mid,
            ticker=ctx.ticker,
            direction_bias=direction_bias,
            active_signals=active_signals,
            signal_count=len(active_signals),
            tier_s=tier_s_active,
            orderbook_snapshot={
                "bids": [(b.price, b.quantity) for b in ob_snap.bids[:10]],
                "asks": [(a.price, a.quantity) for a in ob_snap.asks[:10]],
                "mid": ob_snap.mid,
                "spread_bps": ob_snap.spread_bps,
            },
            market_snapshot={
                "mark_price": market_snap.mark_price,
                "oracle_price": market_snap.oracle_price,
                "funding_rate": market_snap.funding_rate,
                "open_interest": market_snap.open_interest,
            },
            signal_values={
                "obi": obi.value,
                "funding_zscore": funding.zscore,
                "funding_rate": funding.current_rate,
                "cvd": cvd.value,
                "cvd_divergence": cvd.divergence,
                "atr": atr.value,
                "atr_pct": atr.pct_of_price,
                "vol_regime": vol_regime.regime,
                "vol_ratio": vol_regime.ratio,
                "oi_div_pattern": oi_div.pattern,
                "zscore": z_signal.value,
                "spread_zscore": spread.zscore,
                "spread_bps": spread.current_bps,
                "votes_long": votes_long,
                "votes_short": votes_short,
            },
        )
