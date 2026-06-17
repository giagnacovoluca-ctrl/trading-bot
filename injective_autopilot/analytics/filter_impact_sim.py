"""
Simulazione offline (read-only): impatto economico di 10 filtri candidati
sui 29 trade chiusi. Nessuna modifica al codice.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pandas as pd

DB = Path(__file__).resolve().parent.parent / "injective_autopilot.db"


def load() -> pd.DataFrame:
    con = sqlite3.connect(DB)
    trades = pd.read_sql(
        "select id, pnl_pct, confidence, reason, active_signals, signal_values "
        "from trades where status='CLOSED' order by entry_ts", con)
    con.close()

    rows = []
    for _, t in trades.iterrows():
        sv = json.loads(t["signal_values"])
        active = json.loads(t["active_signals"])
        reason = t["reason"]
        rows.append({
            "id": t["id"],
            "pnl_pct": t["pnl_pct"],
            "confidence": t["confidence"],
            "n_active_signals": len(active),
            "spread_bps": sv.get("spread_bps"),
            "atr_pct": sv.get("atr_pct"),
            "has_votes_margin": "votes=" in reason,
            "has_obi_bonus": "obi=" in reason,
        })
    return pd.DataFrame(rows)


def stats(df: pd.DataFrame) -> dict:
    pnls = df["pnl_pct"]
    n = len(pnls)
    if n == 0:
        return {"n": 0, "WR%": 0.0, "PF": 0.0, "expectancy_pct": 0.0, "pnl_aggregato_pct": 0.0}
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
    print(df.to_string(index=False))

    base = stats(df)
    print(f"\n--- Baseline (sistema attuale, tutti i 29 trade) ---\n{base}")
    median_atr = df["atr_pct"].median()
    print(f"\nmediana atr_pct = {median_atr:.6f}")

    filters = {
        "1. spread_bps < 30": df["spread_bps"] < 30,
        "2. spread_bps < 35": df["spread_bps"] < 35,
        "3. atr_pct < 0.0025": df["atr_pct"] < 0.0025,
        "4. atr_pct < mediana": df["atr_pct"] < median_atr,
        "5. spread_bps<35 AND atr_pct<0.0025": (df["spread_bps"] < 35) & (df["atr_pct"] < 0.0025),
        "6. escludi confidence > 0.60": df["confidence"] <= 0.60,
        "7. escludi n_active_signals >= 4": df["n_active_signals"] < 4,
        "8. escludi votes_margin bonus": ~df["has_votes_margin"],
        "9. escludi OBI bonus": ~df["has_obi_bonus"],
    }

    rows = []
    for name, mask in filters.items():
        sub = df[mask]
        excluded = df[~mask]
        s = stats(sub)
        s["filtro"] = name
        s["trade_eliminati"] = len(excluded)
        s["delta_pnl_vs_base"] = round(s["pnl_aggregato_pct"] - base["pnl_aggregato_pct"], 3)
        s["delta_expectancy_vs_base"] = round(s["expectancy_pct"] - base["expectancy_pct"], 4)
        rows.append(s)

    res = pd.DataFrame(rows)[["filtro", "trade_eliminati", "n", "WR%", "PF",
                               "expectancy_pct", "pnl_aggregato_pct",
                               "delta_pnl_vs_base", "delta_expectancy_vs_base"]]

    # combina le migliori 2 (per delta_pnl_vs_base) tra 1-9
    top2 = res.sort_values("delta_pnl_vs_base", ascending=False).iloc[:2]
    print(f"\nMigliori 2 filtri singoli per delta pnl: {top2['filtro'].tolist()}")

    masks_by_name = filters
    name_a, name_b = top2["filtro"].tolist()
    combo_mask = masks_by_name[name_a] & masks_by_name[name_b]
    sub = df[combo_mask]
    s = stats(sub)
    s["filtro"] = f"10. combo({name_a} + {name_b})"
    s["trade_eliminati"] = len(df) - len(sub)
    s["delta_pnl_vs_base"] = round(s["pnl_aggregato_pct"] - base["pnl_aggregato_pct"], 3)
    s["delta_expectancy_vs_base"] = round(s["expectancy_pct"] - base["expectancy_pct"], 4)
    res = pd.concat([res, pd.DataFrame([s])[res.columns]], ignore_index=True)

    res = res.sort_values("delta_pnl_vs_base", ascending=False)
    print("\n##### Risultati ordinati per delta pnl aggregato (miglior -> peggior) #####")
    print(res.to_string(index=False))

    print(f"\nBaseline: n={base['n']} WR%={base['WR%']} PF={base['PF']} "
          f"expectancy={base['expectancy_pct']} pnl_tot={base['pnl_aggregato_pct']}")


if __name__ == "__main__":
    main()
