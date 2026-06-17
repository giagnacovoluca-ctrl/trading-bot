"""
Livello 2 — Motore Decisionale (rule-based, deterministico).

Scoring per ogni trigger:
  - Base:    signal_count  → 0.40 + (n-2)*0.10
  - Bonus:   |zscore|≥2.5  +0.05, |zscore|≥3.0  +0.05
             |funding_z|≥3  +0.05
             obi≥0.90       +0.05
             votes_margin≥3 +0.05, ≥4 +0.10
  - Reject:  MIXED, spread>max_spread_bps, ATR≈0
  - Nota votes: REGIME_SHIFT(BULLISH/BEARISH_SHIFT) contribuisce +1 vote (NEUTRAL=0)

Batch: ordina per score desc, prende i migliori fino a max_new_trades.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


# ── Decision dataclass ───────────────────────────────────────────────────────


@dataclass
class TradeDecision:
    action: str             # "LONG" | "SHORT" | "NO_TRADE"
    confidence: float
    entry: float
    stop_loss: float
    take_profit: float
    position_size: float
    risk_score: float
    reason: str
    market_id: str = ""
    ticker: str = ""
    raw_response: str = ""
    latency_ms: float = 0.0
    model: str = "rule_based"
    active_signals: list = field(default_factory=list)
    signal_values: dict = field(default_factory=dict)
    weighted_score: float = 0.0  # confidence * adaptive weight (ranking only)


def _no_trade(ticker: str = "", market_id: str = "", reason: str = "No signal") -> TradeDecision:
    return TradeDecision(
        action="NO_TRADE",
        confidence=0.0,
        entry=0.0,
        stop_loss=0.0,
        take_profit=0.0,
        position_size=0.0,
        risk_score=1.0,
        reason=reason,
        ticker=ticker,
        market_id=market_id,
    )


# ── Decision engine ──────────────────────────────────────────────────────────


class DecisionEngine:
    def __init__(
        self,
        min_confidence: float = 0.55,
        max_spread_bps: float = 8.0,
        capital: float = 1000.0,
        max_leverage: float = 5.0,
        min_rr: float = 2.0,
        atr_sl_mult: float = 2.0,
        atr_tp_mult: float = 4.0,
    ) -> None:
        self.min_confidence = min_confidence
        self.max_spread_bps = max_spread_bps
        self.capital = capital
        self.max_leverage = max_leverage
        self.min_rr = min_rr
        self.atr_sl_mult = atr_sl_mult
        self.atr_tp_mult = atr_tp_mult

        self._total_calls = 0
        self._approved = 0
        self._rejected = 0

        # Adaptive weights (signal name → weight, neutral=1.0).
        # Used ONLY to rank candidates when slots are limited: the approval
        # gate stays on the raw score so the trade count is not reduced.
        self._signal_weights: dict[str, float] = {}

    def set_signal_weights(self, weights: dict[str, float]) -> None:
        self._signal_weights = dict(weights)

    def _weight_factor(self, active_signals: list[str]) -> float:
        """Mean adaptive weight of the trigger's active signals (1.0 if unknown)."""
        if not self._signal_weights or not active_signals:
            return 1.0
        ws = [self._signal_weights.get(s.split("(")[0], 1.0) for s in active_signals]
        return sum(ws) / len(ws)

    async def decide_batch(
        self,
        triggers: list[Any],
        positions: list[Any],
        margin_available: float,
        max_open_positions: int = 5,
    ) -> dict[str, TradeDecision]:
        if not triggers:
            return {}

        t0 = time.monotonic()
        self._total_calls += 1

        open_tickers = {p.ticker for p in positions} if positions else set()
        max_new = max(0, max_open_positions - len(positions))

        scored: list[tuple[float, Any, str]] = []
        for trigger in triggers:
            if trigger.ticker in open_tickers:
                continue
            if trigger.direction_bias == "MIXED":
                log.info("[%s] rejected: MIXED direction", trigger.ticker)
                self._rejected += 1
                continue
            spread = trigger.orderbook_snapshot.get("spread_bps", 999)
            if spread > self.max_spread_bps:
                log.info("[%s] rejected: spread=%.1f bps > %.0f", trigger.ticker, spread, self.max_spread_bps)
                self._rejected += 1
                continue

            score, reason = self._score(trigger)
            if score >= self.min_confidence:
                scored.append((score, trigger, reason))
            else:
                log.info("[%s] rejected: score=%.2f < %.2f (%s)", trigger.ticker, score, self.min_confidence, reason)
                self._rejected += 1

        # Best signals first, capped at max_new.
        # Ranking uses score * adaptive weight; the gate above used the raw score.
        scored.sort(key=lambda x: -(x[0] * self._weight_factor(x[1].active_signals)))
        results: dict[str, TradeDecision] = {}
        for score, trigger, reason in scored[:max_new]:
            decision = self._build_decision(trigger, score, reason)
            if decision is None:
                self._rejected += 1
                continue
            results[trigger.ticker] = decision
            self._approved += 1
            log.info(
                "Decision APPROVED: %s %s conf=%.2f entry=%.4f sl=%.4f tp=%.4f | %s",
                trigger.ticker, trigger.direction_bias, score,
                decision.entry, decision.stop_loss, decision.take_profit, reason,
            )

        latency_ms = (time.monotonic() - t0) * 1000.0
        log.info(
            "Batch decision: %d triggers → %d approved (latency=%.0fms)",
            len(triggers), len(results), latency_ms,
        )
        return results

    def _score(self, trigger: Any) -> tuple[float, str]:
        sv = trigger.signal_values
        parts: list[str] = []

        vl = sv.get("votes_long", 0)
        vs = sv.get("votes_short", 0)
        margin = abs(vl - vs)

        # Segnali contraddittori: penalità forte
        if margin <= 1:
            parts.append(f"conflict(L{vl}/S{vs})")
            return 0.30, ", ".join(parts)

        # Base: signal count (2→0.40, 3→0.50, 4→0.60, 5→0.70)
        confidence = 0.40 + (trigger.signal_count - 2) * 0.10
        parts.append(f"n={trigger.signal_count}")

        # Coerenza voti: margine ampio = segnali allineati
        if margin >= 4:
            confidence += 0.10
            parts.append(f"votes={vl}/{vs}")
        elif margin >= 3:
            confidence += 0.05
            parts.append(f"votes={vl}/{vs}")

        # Quality bonuses
        z = abs(sv.get("zscore", 0.0))
        if z >= 3.0:
            confidence += 0.10
            parts.append(f"z={z:.1f}")
        elif z >= 2.5:
            confidence += 0.05
            parts.append(f"z={z:.1f}")

        fz = abs(sv.get("funding_zscore", 0.0))
        if fz >= 3.0:
            confidence += 0.05
            parts.append(f"fz={fz:.1f}")

        obi = abs(sv.get("obi", 0.0))
        if obi >= 0.90:
            confidence += 0.05
            parts.append(f"obi={obi:.2f}")

        return min(confidence, 0.95), ", ".join(parts)

    def _build_decision(self, trigger: Any, confidence: float, reason: str) -> TradeDecision | None:
        sv = trigger.signal_values
        mkt = trigger.market_snapshot

        entry = mkt["mark_price"]
        atr = sv.get("atr", 0.0)
        if atr < 1e-10:
            log.info("[%s] rejected: ATR=0 (buffer non ancora caldo)", trigger.ticker)
            return None

        direction = trigger.direction_bias
        if direction == "LONG":
            sl = entry - atr * self.atr_sl_mult
            tp = entry + atr * self.atr_tp_mult
        else:
            sl = entry + atr * self.atr_sl_mult
            tp = entry - atr * self.atr_tp_mult

        # Guard ATR freddo: con buffer corto l'ATR è microscopico e il TP cade
        # dentro lo spread → il fill paper (all'ask) scavalca il TP → chiusura
        # istantanea etichettata "TP" con pnl negativo (visto 10/06: AAVE LONG
        # tp=61.2595 < fill=61.27, pnl=-0.69$). Il TP deve distare almeno
        # 2×spread e comunque >= 0.30% dal prezzo.
        spread_bps = float(trigger.orderbook_snapshot.get("spread_bps", 0.0) or 0.0)
        tp_dist_pct  = abs(tp - entry) / entry * 100.0
        min_dist_pct = max(2.0 * spread_bps / 100.0, 0.30)
        if tp_dist_pct < min_dist_pct:
            log.info(
                "[%s] rejected: TP dist %.3f%% < min %.3f%% (ATR freddo: atr=%.6g, spread=%.1fbps)",
                trigger.ticker, tp_dist_pct, min_dist_pct, atr, spread_bps,
            )
            return None

        risk_per_unit = abs(entry - sl)
        reward = abs(tp - entry)
        if risk_per_unit < 1e-10 or reward / risk_per_unit < self.min_rr:
            return None

        # 1% capital at risk per trade
        position_size = (self.capital * 0.01) / risk_per_unit

        return TradeDecision(
            action=direction,
            confidence=confidence,
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            position_size=position_size,
            risk_score=round(1.0 - confidence, 3),
            reason=reason,
            ticker=trigger.ticker,
            market_id=trigger.market_id,
            active_signals=list(trigger.active_signals),
            signal_values=dict(trigger.signal_values),
            weighted_score=confidence * self._weight_factor(trigger.active_signals),
        )

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "total_calls": self._total_calls,
            "approved": self._approved,
            "rejected": self._rejected,
            "approval_rate": self._approved / (self._total_calls + 1e-10),
        }
