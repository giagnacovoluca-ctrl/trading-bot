"""
Analisi quant one-shot (read-only): da dove nasce la `confidence` dei trade
chiusi e se esistono "confidence inflators" (segnali/combinazioni che
spingono la confidence senza migliorare il pnl). Nessuna modifica al sistema.

Formula confidence (core/decision_engine.py _score):
  conflict (|votes_long-votes_short|<=1) -> 0.30 (fisso, mai approvato: min_confidence=0.55)
  base = 0.40 + (signal_count-2)*0.10
  + 0.10 se margin=|vl-vs|>=4, + 0.05 se margin==3
  + 0.10 se |zscore|>=3.0, + 0.05 se |zscore|>=2.5
  + 0.05 se |funding_zscore|>=3.0
  + 0.05 se |obi|>=0.90
  cap a 0.95
weight_factor = media dei pesi adattivi (signal_weight_snapshots) dei base-name
  attivi all'entry (1.0 se segnale non pesato). NON entra nella confidence,
  moltiplica solo lo score di ranking (weighted_score = confidence * weight_factor).
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

import pandas as pd

DB = Path(__file__).resolve().parent.parent / "injective_autopilot.db"


def base_name(sig: str) -> str:
    return sig.split("(")[0]


def parse_contributions(reason: str, sv: dict) -> dict[str, float]:
    """Ricostruisce il contributo additivo di ciascuna componente alla
    confidence, dalla stringa `reason` (es. 'n=3, votes=4/0, z=3.0')."""
    contrib: dict[str, float] = {}
    if reason.startswith("conflict"):
        contrib["conflict_fixed_0.30"] = 0.30
        return contrib

    m = re.search(r"n=(\d+)", reason)
    n = int(m.group(1)) if m else sv.get("votes_long", 0) + sv.get("votes_short", 0)
    contrib["base(n_signals)"] = 0.40 + (n - 2) * 0.10

    if "votes=" in reason:
        m = re.search(r"votes=(-?\d+)/(-?\d+)", reason)
        vl, vs = int(m.group(1)), int(m.group(2))
        margin = abs(vl - vs)
        contrib["votes_margin"] = 0.10 if margin >= 4 else 0.05

    if re.search(r"\bz=", reason):
        m = re.search(r"\bz=([\d.]+)", reason)
        z = float(m.group(1))
        contrib["zscore_bonus"] = 0.10 if z >= 3.0 else 0.05

    if "fz=" in reason:
        contrib["funding_zscore_bonus"] = 0.05

    if "obi=" in reason:
        contrib["obi_bonus"] = 0.05

    return contrib


def load() -> pd.DataFrame:
    con = sqlite3.connect(DB)
    trades = pd.read_sql(
        "select id, pnl_pct, confidence, reason, active_signals, signal_values, entry_ts "
        "from trades where status='CLOSED' order by entry_ts", con)
    weights = pd.read_sql("select ts, weights from signal_weight_snapshots order by ts", con)
    con.close()
    weights["weights"] = weights["weights"].apply(json.loads)

    rows = []
    for _, t in trades.iterrows():
        sv = json.loads(t["signal_values"])
        active = json.loads(t["active_signals"])
        bases = [base_name(s) for s in active]
        contrib = parse_contributions(t["reason"], sv)

        # weight_factor: ultimo snapshot con ts <= entry_ts
        snap = weights[weights["ts"] <= t["entry_ts"]]
        w = snap.iloc[-1]["weights"] if len(snap) else {}
        wf = sum(w.get(b, 1.0) for b in bases) / len(bases) if bases else 1.0

        rows.append({
            "id": t["id"],
            "pnl_pct": t["pnl_pct"],
            "confidence": t["confidence"],
            "reason": t["reason"],
            "active_signals": active,
            "bases": tuple(sorted(set(bases))),
            "votes_long": sv.get("votes_long"),
            "votes_short": sv.get("votes_short"),
            "weight_factor": wf,
            "weighted_score": t["confidence"] * wf,
            **{f"c_{k}": v for k, v in contrib.items()},
            "confidence_check": round(sum(contrib.values()), 3) if "conflict_fixed_0.30" not in contrib
                                else 0.30,
        })
    return pd.DataFrame(rows)


def wr_pf_expectancy(pnls: pd.Series) -> tuple[float, float, float]:
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    wr = len(wins) / len(pnls) * 100 if len(pnls) else 0.0
    gross_win = wins.sum()
    gross_loss = -losses.sum()
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    expectancy = pnls.mean()
    return wr, pf, expectancy


def main():
    df = load()
    pd.set_option("display.width", 200)
    print(f"=== n trades closed = {len(df)} ===\n")

    print("--- Dettaglio per trade ---")
    cols = ["id", "pnl_pct", "confidence", "votes_long", "votes_short",
            "weight_factor", "weighted_score", "bases"]
    print(df[cols].to_string(index=False))

    # sanity check: confidence_check == confidence (cap 0.95)
    df["conf_recon_ok"] = (df["confidence_check"].clip(upper=0.95) - df["confidence"]).abs() < 1e-6
    print(f"\nRicostruzione formula confidence corretta per {df['conf_recon_ok'].sum()}/{len(df)} trade")

    print("\n##### 1) Segnali piu' frequenti tra i trade con confidence > 0.60 #####")
    hi = df[df["confidence"] > 0.60]
    print(f"n={len(hi)}")
    cnt = defaultdict(int)
    for bases in hi["bases"]:
        for b in bases:
            cnt[b] += 1
    for b, c in sorted(cnt.items(), key=lambda x: -x[1]):
        print(f"  {b}: {c}/{len(hi)} ({c/len(hi)*100:.0f}%)")

    print("\n##### 2) Segnali piu' frequenti tra i trade con confidence < 0.55 #####")
    lo = df[df["confidence"] < 0.55]
    print(f"n={len(lo)}")
    cnt = defaultdict(int)
    for bases in lo["bases"]:
        for b in bases:
            cnt[b] += 1
    for b, c in sorted(cnt.items(), key=lambda x: -x[1]):
        print(f"  {b}: {c}/{len(lo)} ({c/len(lo)*100:.0f}%)")

    print("\n##### 3) Combinazioni con confidence media piu' alta #####")
    g = df.groupby("bases").agg(n=("confidence", "size"),
                                 conf_mean=("confidence", "mean"),
                                 conf_max=("confidence", "max"))
    print(g.sort_values("conf_mean", ascending=False).to_string())

    print("\n##### 4) WR / PF / expectancy per combinazione #####")
    rows = []
    for bases, sub in df.groupby("bases"):
        wr, pf, exp = wr_pf_expectancy(sub["pnl_pct"])
        rows.append({"bases": bases, "n": len(sub), "conf_mean": sub["confidence"].mean(),
                      "WR%": wr, "PF": pf, "expectancy_pct": exp})
    res = pd.DataFrame(rows).sort_values("conf_mean", ascending=False)
    print(res.to_string(index=False))

    print("\n##### 5) Quota confidence da segnali grezzi vs weight_factor #####")
    print("La 'confidence' e' calcolata SOLO da signal_count/votes/zscore/funding_zscore/obi")
    print("(componenti additive vedi sopra). weight_factor NON entra nella confidence:")
    print("moltiplica solo il ranking (weighted_score = confidence * weight_factor),")
    print("usato per scegliere tra trigger concorrenti, non per il sizing/gating della singola trade.")
    print(f"\nweight_factor: mean={df['weight_factor'].mean():.4f} "
          f"min={df['weight_factor'].min():.4f} max={df['weight_factor'].max():.4f} "
          f"std={df['weight_factor'].std():.4f}")
    print("Distribuzione componenti additive (presenza % sui trade):")
    contrib_cols = [c for c in df.columns if c.startswith("c_")]
    for c in contrib_cols:
        present = df[c].notna()
        print(f"  {c}: presente in {present.sum()}/{len(df)} trade, "
              f"valore medio quando presente={df.loc[present, c].mean():.3f}")

    print("\n##### 6) Inflators: alta confidence ma pnl peggiore? #####")
    rows = []
    for c in contrib_cols:
        present = df[c].notna()
        absent = ~present
        if present.sum() < 2 or absent.sum() < 2:
            continue
        rows.append({
            "component": c,
            "n_present": present.sum(),
            "conf_mean_present": df.loc[present, "confidence"].mean(),
            "conf_mean_absent": df.loc[absent, "confidence"].mean(),
            "pnl_mean_present": df.loc[present, "pnl_pct"].mean(),
            "pnl_mean_absent": df.loc[absent, "pnl_pct"].mean(),
            "pnl_median_present": df.loc[present, "pnl_pct"].median(),
            "pnl_median_absent": df.loc[absent, "pnl_pct"].median(),
        })
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
