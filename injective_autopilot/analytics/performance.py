"""
Analytics quantitative su trade chiusi — pure functions, nessun side effect.

Tutte le funzioni accettano la lista di trade dict prodotta da
Repository.get_closed_trades() (ordine cronologico) e restituiscono
ranking pronti per dashboard/JSON.

Convenzioni:
  - I nomi segnale sono normalizzati: "OBI(0.98,n=4)" → "OBI"
  - expectancy = wr*avg_win + (1-wr)*avg_loss   (USD per trade)
  - profit_factor = gross_profit / gross_loss
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from itertools import combinations
from typing import Any


def normalize_signal(raw: str) -> str:
    """'OBI(0.98,n=498)' → 'OBI'."""
    return raw.split("(")[0].strip()


def trade_signals(trade: dict) -> list[str]:
    """Normalized, deduplicated signal names for a trade (sorted)."""
    seen: list[str] = []
    for s in trade.get("active_signals") or []:
        name = normalize_signal(s)
        if name and name not in seen:
            seen.append(name)
    return sorted(seen)


# ── Core stats ───────────────────────────────────────────────────────────────


def basic_stats(pnls: list[float]) -> dict[str, Any]:
    """Stats di base su una sequenza cronologica di PnL."""
    n = len(pnls)
    if n == 0:
        return {
            "n": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "net_pnl": 0.0, "avg_pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
            "profit_factor": 0.0, "expectancy": 0.0, "max_drawdown": 0.0,
            "sharpe": 0.0,
        }
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    wr = len(wins) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = gross_profit / gross_loss if gross_loss > 1e-10 else (math.inf if gross_profit > 0 else 0.0)

    # Max drawdown sulla equity cumulata dei soli trade considerati
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    # Sharpe per-trade (mean/std dei PnL, non annualizzato: confrontabile tra gruppi)
    mean = sum(pnls) / n
    if n > 1:
        var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        sharpe = mean / math.sqrt(var) if var > 1e-12 else 0.0
    else:
        sharpe = 0.0

    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(wr * 100, 1),
        "net_pnl": round(sum(pnls), 2),
        "avg_pnl": round(mean, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(pf, 2) if pf != math.inf else 999.0,
        "expectancy": round(wr * avg_win + (1 - wr) * avg_loss, 2),
        "max_drawdown": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
    }


def _grouped_ranking(groups: dict[str, list[float]], key_name: str) -> list[dict]:
    rows = []
    for key, pnls in groups.items():
        row = {key_name: key}
        row.update(basic_stats(pnls))
        rows.append(row)
    # Ranking: expectancy desc, poi net pnl
    rows.sort(key=lambda r: (-r["expectancy"], -r["net_pnl"]))
    return rows


# ── Rankings ─────────────────────────────────────────────────────────────────


def signal_ranking(trades: list[dict]) -> list[dict]:
    """Per ogni segnale: attivazioni, WR, PF, expectancy, PnL medio, drawdown."""
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        for sig in trade_signals(t):
            groups[sig].append(t["pnl_usdt"])
    return _grouped_ranking(groups, "signal")


def combo_ranking(trades: list[dict], max_combo_size: int = 3, min_trades: int = 1) -> list[dict]:
    """
    Tutte le combinazioni (coppie e triple) di segnali co-attivi,
    più il set esatto di ogni trade.
    """
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        sigs = trade_signals(t)
        if not sigs:
            continue
        # set esatto
        groups[" + ".join(sigs)].append(t["pnl_usdt"])
        # sottocombinazioni (solo se il set esatto è più grande)
        for size in range(2, min(max_combo_size, len(sigs)) + 1):
            if size == len(sigs):
                continue
            for combo in combinations(sigs, size):
                groups[" + ".join(combo) + " *"].append(t["pnl_usdt"])
    rows = _grouped_ranking(groups, "combo")
    return [r for r in rows if r["n"] >= min_trades]


def market_ranking(trades: list[dict]) -> list[dict]:
    """Leaderboard per mercato: n, pnl, WR, expectancy, PF, sharpe, max DD."""
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        groups[t.get("ticker") or t.get("market_id", "?")[:8]].append(t["pnl_usdt"])
    return _grouped_ranking(groups, "ticker")


def direction_ranking(trades: list[dict]) -> list[dict]:
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        groups[t["direction"]].append(t["pnl_usdt"])
    return _grouped_ranking(groups, "direction")


# ── Temporal analysis ────────────────────────────────────────────────────────


def hourly_analysis(trades: list[dict]) -> list[dict]:
    """Performance per ora UTC del giorno (0-23)."""
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        hour = time.gmtime(t["entry_ts"]).tm_hour
        groups[f"{hour:02d}"].append(t["pnl_usdt"])
    rows = _grouped_ranking(groups, "hour")
    rows.sort(key=lambda r: r["hour"])
    return rows


_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def weekday_analysis(trades: list[dict]) -> list[dict]:
    """Performance per giorno della settimana (UTC)."""
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        wd = time.gmtime(t["entry_ts"]).tm_wday
        groups[_WEEKDAYS[wd]].append(t["pnl_usdt"])
    rows = _grouped_ranking(groups, "weekday")
    rows.sort(key=lambda r: _WEEKDAYS.index(r["weekday"]))
    return rows


def vol_regime_analysis(trades: list[dict]) -> list[dict]:
    """Performance per regime di volatilità al momento dell'ingresso."""
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        regime = (t.get("signal_values") or {}).get("vol_regime", "UNKNOWN")
        groups[regime].append(t["pnl_usdt"])
    return _grouped_ranking(groups, "regime")


# ── Scoring coherence ────────────────────────────────────────────────────────


def score_bucket_analysis(trades: list[dict], bucket_width: float = 0.10) -> list[dict]:
    """
    Verifica la coerenza dello scoring: se gli score sono informativi,
    i bucket di confidence più alti devono mostrare expectancy maggiore.
    """
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        c = t.get("confidence", 0.0)
        lo = math.floor(c / bucket_width) * bucket_width
        groups[f"{lo:.2f}-{lo + bucket_width:.2f}"].append(t["pnl_usdt"])
    rows = _grouped_ranking(groups, "score_bucket")
    rows.sort(key=lambda r: r["score_bucket"])
    return rows


def exit_reason_analysis(trades: list[dict]) -> list[dict]:
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        groups[t.get("exit_reason") or "?"].append(t["pnl_usdt"])
    return _grouped_ranking(groups, "exit_reason")


# ── Win/Loss pattern diff ────────────────────────────────────────────────────


def win_loss_patterns(trades: list[dict]) -> dict[str, Any]:
    """
    Confronto statistico vincenti vs perdenti: segnali, score, volatilità,
    funding, MAE/MFE, hold time.
    """
    winners = [t for t in trades if t["pnl_usdt"] > 0]
    losers = [t for t in trades if t["pnl_usdt"] <= 0]

    def profile(group: list[dict]) -> dict[str, Any]:
        if not group:
            return {}
        sig_freq: dict[str, int] = defaultdict(int)
        for t in group:
            for s in trade_signals(t):
                sig_freq[s] += 1
        sv = [t.get("signal_values") or {} for t in group]

        def avg(key: str, absolute: bool = False) -> float:
            vals = [abs(d[key]) if absolute else d[key]
                    for d in sv if isinstance(d.get(key), (int, float))]
            return round(sum(vals) / len(vals), 4) if vals else 0.0

        hold = [(t["exit_ts"] - t["entry_ts"]) / 3600 for t in group if t.get("exit_ts")]
        return {
            "n": len(group),
            "avg_confidence": round(sum(t.get("confidence", 0) for t in group) / len(group), 3),
            "avg_pnl": round(sum(t["pnl_usdt"] for t in group) / len(group), 2),
            "avg_hold_hours": round(sum(hold) / len(hold), 2) if hold else 0.0,
            "avg_atr_pct": avg("atr_pct"),
            "avg_funding_zscore": avg("funding_zscore", absolute=True),
            "avg_obi": avg("obi", absolute=True),
            "avg_mae_pct": round(sum(t.get("mae_pct", 0) for t in group) / len(group), 2),
            "avg_mfe_pct": round(sum(t.get("mfe_pct", 0) for t in group) / len(group), 2),
            "signal_frequency": dict(sorted(sig_freq.items(), key=lambda x: -x[1])),
        }

    return {"winners": profile(winners), "losers": profile(losers)}
