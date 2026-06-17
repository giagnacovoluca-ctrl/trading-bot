"""
Meta-analisi (read-only): leave-one-out robustness check su tutte le
correlazioni/conclusioni emerse dai report precedenti (n=29 trade chiusi).
Nessuna modifica al codice.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

DB = Path(__file__).resolve().parent.parent / "injective_autopilot.db"


def load() -> pd.DataFrame:
    con = sqlite3.connect(DB)
    trades = pd.read_sql(
        "select id, pnl_pct, confidence, reason, active_signals, signal_values, mae_pct, mfe_pct "
        "from trades where status='CLOSED' order by entry_ts", con)
    con.close()
    rows = []
    for _, t in trades.iterrows():
        sv = json.loads(t["signal_values"])
        active = json.loads(t["active_signals"])
        reason = t["reason"]
        rows.append({
            "id": t["id"], "pnl_pct": t["pnl_pct"], "confidence": t["confidence"],
            "n_active_signals": len(active),
            "spread_bps": sv.get("spread_bps"), "atr_pct": sv.get("atr_pct"),
            "mae_pct": t["mae_pct"], "mfe_pct": t["mfe_pct"],
            "has_votes_margin": "votes=" in reason,
            "has_obi_bonus": "obi=" in reason,
            "has_funding_zscore_bonus": "fz=" in reason,
        })
    return pd.DataFrame(rows)


def loo_spearman(df: pd.DataFrame, x: str, y: str = "pnl_pct"):
    full_r, full_p = stats.spearmanr(df[x], df[y])
    rs, ps = [], []
    for i in range(len(df)):
        sub = df.drop(df.index[i])
        r, p = stats.spearmanr(sub[x], sub[y])
        rs.append(r)
        ps.append(p)
    rs = np.array(rs)
    ps = np.array(ps)
    sign_flips = (np.sign(rs) != np.sign(full_r)).sum()
    return {
        "feature": x, "r_full": round(full_r, 3), "p_full": round(full_p, 3),
        "r_min": round(rs.min(), 3), "r_max": round(rs.max(), 3),
        "p_max": round(ps.max(), 3),
        "sign_flips_loo": sign_flips,
        "max_idx_removed": df.iloc[np.argmax(np.abs(rs - full_r))]["id"],
    }


def loo_mean_diff(df: pd.DataFrame, flag: str, y: str = "pnl_pct"):
    present = df[df[flag]]
    absent = df[~df[flag]]
    full_diff = present[y].mean() - absent[y].mean()
    diffs = []
    for i in range(len(df)):
        sub = df.drop(df.index[i])
        p = sub[sub[flag]][y]
        a = sub[~sub[flag]][y]
        if len(p) == 0 or len(a) == 0:
            continue
        diffs.append(p.mean() - a.mean())
    diffs = np.array(diffs)
    sign_flips = (np.sign(diffs) != np.sign(full_diff)).sum()
    return {
        "flag": flag, "n_present": len(present), "n_absent": len(absent),
        "diff_full": round(full_diff, 4),
        "diff_min": round(diffs.min(), 4), "diff_max": round(diffs.max(), 4),
        "sign_flips_loo": sign_flips,
    }


def main():
    df = load()
    pd.set_option("display.width", 220)
    print(f"=== n trades closed = {len(df)} ===\n")

    print("##### Leave-one-out: correlazioni Spearman con pnl_pct #####")
    rows = []
    for x in ["n_active_signals", "spread_bps", "atr_pct", "confidence", "mae_pct", "mfe_pct"]:
        rows.append(loo_spearman(df, x))
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n##### Leave-one-out: differenza media pnl present-vs-absent per bonus flag #####")
    rows = []
    for flag in ["has_votes_margin", "has_obi_bonus", "has_funding_zscore_bonus"]:
        rows.append(loo_mean_diff(df, flag))
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n##### n_active_signals: bucket 2 vs 3+ con LOO #####")
    df["bucket23"] = df["n_active_signals"].apply(lambda n: "2" if n == 2 else "3+")
    g2 = df[df["bucket23"] == "2"]["pnl_pct"]
    g3 = df[df["bucket23"] == "3+"]["pnl_pct"]
    print(f"n(2)={len(g2)} mean={g2.mean():.4f} | n(3+)={len(g3)} mean={g3.mean():.4f} "
          f"| diff={g2.mean()-g3.mean():.4f}")
    diffs = []
    for i in range(len(df)):
        sub = df.drop(df.index[i])
        a = sub[sub["bucket23"] == "2"]["pnl_pct"]
        b = sub[sub["bucket23"] == "3+"]["pnl_pct"]
        diffs.append(a.mean() - b.mean())
    diffs = np.array(diffs)
    print(f"LOO diff range: [{diffs.min():.4f}, {diffs.max():.4f}], "
          f"sign flips: {(np.sign(diffs) != np.sign(g2.mean()-g3.mean())).sum()}")

    print("\n##### confidence top quartile (0.6-0.8, n=4) #####")
    hi = df[df["confidence"] > 0.6]
    lo = df[df["confidence"] <= 0.6]
    print(f"n(hi)={len(hi)} mean_pnl={hi['pnl_pct'].mean():.4f} | "
          f"n(lo)={len(lo)} mean_pnl={lo['pnl_pct'].mean():.4f}")
    print("hi trades:", hi[["id", "pnl_pct", "confidence"]].to_string(index=False))

    print("\n##### bucket counts per analisi precedenti (n<5 flag) #####")
    print("spread bucket 30-35: n=1 (PAPER_000024)")
    print("spread bucket 35-40: n=1 (PAPER_000003 era 65bps; verificare quale e' nel bucket 35-40)")
    print("confidence combo a 4-5 segnali con conf 0.70-0.80: tutte n=1")
    print(df["n_active_signals"].value_counts().to_string())


if __name__ == "__main__":
    main()
