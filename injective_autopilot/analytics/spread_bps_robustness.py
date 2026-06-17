"""
Analisi quant one-shot (read-only): robustezza del filtro spread_bps<35
sui 29 trade chiusi. Nessuna modifica al codice.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

DB = Path(__file__).resolve().parent.parent / "injective_autopilot.db"


def load() -> pd.DataFrame:
    con = sqlite3.connect(DB)
    trades = pd.read_sql(
        "select id, pnl_pct, confidence, active_signals, signal_values "
        "from trades where status='CLOSED' order by entry_ts", con)
    con.close()
    rows = []
    for _, t in trades.iterrows():
        sv = json.loads(t["signal_values"])
        rows.append({
            "id": t["id"], "pnl_pct": t["pnl_pct"], "confidence": t["confidence"],
            "n_active_signals": len(json.loads(t["active_signals"])),
            "spread_bps": sv.get("spread_bps"), "atr_pct": sv.get("atr_pct"),
        })
    return pd.DataFrame(rows)


def stats(df: pd.DataFrame) -> dict:
    pnls = df["pnl_pct"]
    n = len(pnls)
    if n == 0:
        return {"n": 0, "WR%": np.nan, "PF": np.nan, "expectancy_pct": np.nan, "pnl_aggregato_pct": 0.0}
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    wr = len(wins) / n * 100
    gross_win = wins.sum()
    gross_loss = -losses.sum()
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    return {"n": n, "WR%": round(wr, 1), "PF": round(pf, 3),
            "expectancy_pct": round(pnls.mean(), 4), "pnl_aggregato_pct": round(pnls.sum(), 3)}


def main():
    df = load()
    pd.set_option("display.width", 200)
    print(f"=== n trades closed = {len(df)} ===\n")

    print("##### 1) Distribuzione spread_bps #####")
    s = df["spread_bps"]
    print(f"min={s.min():.2f} max={s.max():.2f} mean={s.mean():.2f} median={s.median():.2f}")
    for q in [0.25, 0.50, 0.75, 0.90]:
        print(f"p{int(q*100)}={s.quantile(q):.2f}")

    print("\n##### 2) Bucket analysis #####")
    bins = [-np.inf, 20, 30, 35, 40, np.inf]
    labels = ["<20", "20-30", "30-35", "35-40", ">40"]
    df["bucket"] = pd.cut(df["spread_bps"], bins=bins, labels=labels)
    rows = []
    for lab in labels:
        sub = df[df["bucket"] == lab]
        st = stats(sub)
        st["bucket"] = lab
        rows.append(st)
    print(pd.DataFrame(rows)[["bucket", "n", "WR%", "PF", "expectancy_pct", "pnl_aggregato_pct"]]
          .to_string(index=False))

    print("\n##### 3) Sensitivity analysis (soglie spread<X) #####")
    rows = []
    base_n = len(df)
    for thr in [20, 25, 30, 35, 40, 45, 50]:
        sub = df[df["spread_bps"] < thr]
        st = stats(sub)
        st["soglia"] = f"<{thr}"
        st["trade_eliminati"] = base_n - st["n"]
        rows.append(st)
    print(pd.DataFrame(rows)[["soglia", "trade_eliminati", "n", "WR%", "PF",
                               "expectancy_pct", "pnl_aggregato_pct"]].to_string(index=False))

    print("\n##### 4) Stabilita': trade ordinati per spread_bps con pnl cumulato #####")
    sorted_df = df.sort_values("spread_bps").reset_index(drop=True)
    sorted_df["pnl_cumsum"] = sorted_df["pnl_pct"].cumsum()
    print(sorted_df[["id", "spread_bps", "pnl_pct", "pnl_cumsum"]].to_string(index=False))

    print("\n##### 5) Top-10 peggiori trade per pnl #####")
    worst = df.sort_values("pnl_pct").head(10)
    print(worst[["id", "pnl_pct", "spread_bps", "atr_pct", "confidence", "n_active_signals"]]
          .to_string(index=False))
    print(f"\nspread_bps medio dei 10 peggiori: {worst['spread_bps'].mean():.2f} "
          f"(vs media generale {df['spread_bps'].mean():.2f})")
    print(f"spread_bps>=35 nei 10 peggiori: {(worst['spread_bps']>=35).sum()}/10")


if __name__ == "__main__":
    main()
