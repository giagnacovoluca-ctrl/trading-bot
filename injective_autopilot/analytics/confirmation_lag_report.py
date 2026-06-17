"""
Analisi quant one-shot (read-only): "confirmation lag" — il sistema entra
troppo tardi rispetto alla prima apparizione dei segnali che giustificano
l'entry? Nessuna modifica al sistema, solo report.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

DB = Path(__file__).resolve().parent.parent / "injective_autopilot.db"


def base_name(sig: str) -> str:
    return sig.split("(")[0]


def wait_time_for_trade(cur, market_id: str, entry_ts: float, entry_signals: list[str]) -> tuple[float, float, int]:
    """Per ciascun segnale attivo all'entry (ignorando i parametri numerici,
    es. FUNDING_EXTREME(z=-3.00) -> FUNDING_EXTREME), risale lo storico
    `signals` del market e trova da quanto tempo quel tipo di segnale e'
    presente ininterrottamente.
    Ritorna (wait_min_first, wait_max_last, n_rows_storico_disponibili):
      - wait_min_first = entry_ts - earliest first-appearance tra i segnali
        attivi all'entry (quanto e' "vecchio" il piu' vecchio dei segnali
        usati per decidere)
      - wait_max_last  = entry_ts - latest first-appearance (quanto tempo fa
        e' arrivato l'ultimo segnale che ha completato la combo)
    """
    entry_bases = {base_name(s) for s in entry_signals}
    cur.execute(
        "select ts, active_signals from signals where market_id=? and ts<=? order by ts desc",
        (market_id, entry_ts),
    )
    rows = cur.fetchall()
    if not rows:
        return float("nan"), float("nan"), 0

    first_ts = {b: rows[0][0] for b in entry_bases}
    still_tracking = set(entry_bases)
    for ts, raw in rows[1:]:
        if not still_tracking:
            break
        bases = {base_name(s) for s in json.loads(raw)}
        for b in list(still_tracking):
            if b in bases:
                first_ts[b] = ts
            else:
                still_tracking.discard(b)

    wait_min_first = entry_ts - min(first_ts.values())
    wait_max_last = entry_ts - max(first_ts.values())
    return wait_min_first, wait_max_last, len(rows)


def load() -> pd.DataFrame:
    con = sqlite3.connect(DB)
    cur = con.cursor()
    trades = pd.read_sql(
        "select id, market_id, entry_ts, pnl_pct, confidence, active_signals "
        "from trades where status='CLOSED'", con)

    rows = []
    for _, t in trades.iterrows():
        active = json.loads(t["active_signals"])
        w_first, w_last, n_hist = wait_time_for_trade(cur, t["market_id"], t["entry_ts"], active)
        rows.append({
            "id": t["id"],
            "pnl_pct": t["pnl_pct"],
            "confidence": t["confidence"],
            "n_active_signals": len(active),
            "wait_minutes": w_first / 60.0,
            "wait_minutes_last": w_last / 60.0,
            "n_hist": n_hist,
        })
    con.close()
    return pd.DataFrame(rows)


def main():
    df = load()
    print(f"=== n trades closed = {len(df)} ===\n")
    print("Distribuzione wait_minutes (tempo tra prima apparizione esatta del set "
          "di segnali e apertura posizione):")
    print(df["wait_minutes"].describe())
    print()
    print(df[["id", "pnl_pct", "confidence", "n_active_signals", "wait_minutes",
              "wait_minutes_last", "n_hist"]]
          .sort_values("wait_minutes").to_string(index=False))

    print("\n##### 1) Correlazione wait_minutes vs pnl_pct #####")
    for col in ["wait_minutes"]:
        sub = df[[col, "pnl_pct"]].dropna()
        pear, pp = stats.pearsonr(sub[col], sub["pnl_pct"])
        spear, sp = stats.spearmanr(sub[col], sub["pnl_pct"])
        print(f"{col}: n={len(sub)} pearson r={pear:.3f} (p={pp:.3f}) "
              f"spearman r={spear:.3f} (p={sp:.3f})")

    print("\n##### 2) Correlazione n_active_signals vs wait_minutes #####")
    sub = df[["n_active_signals", "wait_minutes"]].dropna()
    pear, pp = stats.pearsonr(sub["n_active_signals"], sub["wait_minutes"])
    spear, sp = stats.spearmanr(sub["n_active_signals"], sub["wait_minutes"])
    print(f"n={len(sub)} pearson r={pear:.3f} (p={pp:.3f}) spearman r={spear:.3f} (p={sp:.3f})")

    print("\n##### 3) Correlazione confidence vs wait_minutes #####")
    sub = df[["confidence", "wait_minutes"]].dropna()
    pear, pp = stats.pearsonr(sub["confidence"], sub["wait_minutes"])
    spear, sp = stats.spearmanr(sub["confidence"], sub["wait_minutes"])
    print(f"n={len(sub)} pearson r={pear:.3f} (p={pp:.3f}) spearman r={spear:.3f} (p={sp:.3f})")

    print("\n##### 4) wait_minutes: trade migliori vs peggiori #####")
    q75, q25 = df["pnl_pct"].quantile([0.75, 0.25])
    top = df[df["pnl_pct"] >= q75]
    bot = df[df["pnl_pct"] <= q25]
    print(f"top25% pnl (n={len(top)}): wait_minutes mean={top['wait_minutes'].mean():.2f} "
          f"median={top['wait_minutes'].median():.2f}")
    print(f"bottom25% pnl (n={len(bot)}): wait_minutes mean={bot['wait_minutes'].mean():.2f} "
          f"median={bot['wait_minutes'].median():.2f}")
    u, p = stats.mannwhitneyu(top["wait_minutes"], bot["wait_minutes"], alternative="two-sided")
    print(f"Mann-Whitney U p={p:.3f}")
    winners = df[df["pnl_pct"] > 0]
    losers = df[df["pnl_pct"] <= 0]
    print(f"\nwinners (n={len(winners)}): wait_minutes mean={winners['wait_minutes'].mean():.2f} "
          f"median={winners['wait_minutes'].median():.2f}")
    print(f"losers  (n={len(losers)}): wait_minutes mean={losers['wait_minutes'].mean():.2f} "
          f"median={losers['wait_minutes'].median():.2f}")
    u, p = stats.mannwhitneyu(winners["wait_minutes"], losers["wait_minutes"], alternative="two-sided")
    print(f"Mann-Whitney U p={p:.3f}")

    print("\n##### 5) Distribuzione pnl per n_active_signals (1 / 2 / 3+) #####")
    def bucket(n):
        if n <= 1:
            return "1"
        if n == 2:
            return "2"
        return "3+"
    df["sig_bucket"] = df["n_active_signals"].apply(bucket)
    g = df.groupby("sig_bucket")["pnl_pct"].agg(["count", "mean", "median", "std"])
    print(g.reindex(["1", "2", "3+"]).to_string())
    groups = [g["pnl_pct"].values for _, g in df.groupby("sig_bucket")]
    if len(groups) >= 2 and all(len(x) >= 2 for x in groups):
        f, p = stats.f_oneway(*groups)
        print(f"ANOVA f={f:.3f} p={p:.3f}")

    print("\n##### 6) Distribuzione pnl per quartili di confidence #####")
    try:
        df["conf_q"] = pd.qcut(df["confidence"], 4, duplicates="drop")
    except ValueError as e:
        df["conf_q"] = pd.cut(df["confidence"], bins=df["confidence"].nunique())
    g = df.groupby("conf_q")["pnl_pct"].agg(["count", "mean", "median", "std"])
    print(g.to_string())


if __name__ == "__main__":
    main()
