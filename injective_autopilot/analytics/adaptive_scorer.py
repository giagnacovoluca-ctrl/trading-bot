"""
Adaptive Scoring Engine — pesi per segnale derivati ESCLUSIVAMENTE dai
risultati reali dei trade chiusi.

Metodo:
  1. Bayesian Updating sul win-rate: posterior Beta(α0+wins, β0+losses)
     con prior debole Beta(2,2) centrato su 0.5. La media posterior fa
     shrinkage automatico verso 0.5 con pochi campioni → nessun segnale
     viene premiato/punito prima di avere dati.
  2. EWMA dell'expectancy normalizzata (rolling window degli ultimi
     `window` trade per segnale) → cattura il deterioramento recente.
  3. weight = 1 + k_wr*(posterior_mean - 0.5)*2 + k_ew*tanh(ewma_norm)
     clampato in [min_weight, max_weight].

IMPORTANTE (fase paper trading / raccolta dati):
  i pesi NON gateano i trade — vengono usati dal DecisionEngine solo per
  ordinare i candidati quando gli slot disponibili sono meno dei trigger.
  Il numero di trade resta invariato.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from analytics.performance import basic_stats, trade_signals


class AdaptiveScorer:
    def __init__(
        self,
        prior_alpha: float = 2.0,
        prior_beta: float = 2.0,
        ewma_lambda: float = 0.10,
        window: int = 50,
        min_weight: float = 0.5,
        max_weight: float = 1.5,
        k_winrate: float = 0.5,
        k_ewma: float = 0.25,
        min_activations: int = 10,
    ) -> None:
        self.prior_alpha = prior_alpha
        self.prior_beta = prior_beta
        self.ewma_lambda = ewma_lambda
        self.window = window
        self.min_weight = min_weight
        self.max_weight = max_weight
        self.k_winrate = k_winrate
        self.k_ewma = k_ewma
        self.min_activations = min_activations

        self.weights: dict[str, float] = {}
        self.signal_stats: dict[str, dict[str, Any]] = {}

    def update(self, closed_trades: list[dict]) -> dict[str, float]:
        """
        Ricalcola i pesi da zero sull'intera storia (deterministico e
        riproducibile — nessuno stato nascosto tra i restart).
        `closed_trades` deve essere in ordine cronologico.
        """
        per_signal: dict[str, list[float]] = defaultdict(list)
        for t in closed_trades:
            for sig in trade_signals(t):
                per_signal[sig].append(t["pnl_usdt"])

        weights: dict[str, float] = {}
        stats: dict[str, dict[str, Any]] = {}

        for sig, pnls in per_signal.items():
            recent = pnls[-self.window:]
            wins = sum(1 for p in recent if p > 0)
            losses = len(recent) - wins

            # 1. Bayesian posterior win-rate
            post_mean = (self.prior_alpha + wins) / (
                self.prior_alpha + self.prior_beta + len(recent)
            )

            # 2. EWMA dell'expectancy normalizzata sul rischio medio
            avg_abs = sum(abs(p) for p in recent) / len(recent) if recent else 1.0
            scale = max(avg_abs, 1e-6)
            ewma = 0.0
            for p in recent:
                ewma = (1 - self.ewma_lambda) * ewma + self.ewma_lambda * (p / scale)

            # 3. Peso composito
            w = 1.0 + self.k_winrate * (post_mean - 0.5) * 2.0 + self.k_ewma * math.tanh(ewma)
            # Sotto il minimo campione: resta neutrale (raccolta dati)
            if len(pnls) < self.min_activations:
                w = 1.0
            w = max(self.min_weight, min(self.max_weight, w))

            weights[sig] = round(w, 4)
            s = basic_stats(recent)
            s["total_activations"] = len(pnls)
            s["posterior_winrate"] = round(post_mean, 4)
            s["ewma_norm_expectancy"] = round(ewma, 4)
            s["weight"] = weights[sig]
            stats[sig] = s

        self.weights = weights
        self.signal_stats = stats
        return weights

    def trend(self, snapshots: list[dict]) -> dict[str, str]:
        """
        Confronta gli ultimi due snapshot di pesi: segnali in miglioramento
        (IMPROVING), deterioramento (DETERIORATING) o stabili (STABLE).
        """
        if len(snapshots) < 2:
            return {}
        prev, curr = snapshots[-2]["weights"], snapshots[-1]["weights"]
        out: dict[str, str] = {}
        for sig, w in curr.items():
            old = prev.get(sig)
            if old is None:
                out[sig] = "NEW"
            elif w > old + 0.02:
                out[sig] = "IMPROVING"
            elif w < old - 0.02:
                out[sig] = "DETERIORATING"
            else:
                out[sig] = "STABLE"
        return out
