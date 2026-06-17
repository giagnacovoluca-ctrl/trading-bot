"""
Simulazione offline (read-only): ricostruisce 4 varianti della formula
`confidence` di core/decision_engine.py._score sui 29 trade chiusi, e
confronta soglia/WR/PF/expectancy/ranking. Nessuna modifica al codice.

A) confidence attuale:
   base = 0.40 + (n-2)*0.10  + votes_margin + zscore_bonus + funding_zscore_bonus + obi_bonus  (cap 0.95)
B) senza il termine n_active_signals (solo bonus qualitativi):
   conf_B = votes_margin + zscore_bonus + funding_zscore_bonus + obi_bonus
C) bonus n_active_signals dimezzato:
   base_C = 0.40 + (n-2)*0.05  + (stessi bonus di A)  (cap 0.95)
D) solo funding_zscore_bonus + obi_bonus + zscore_bonus:
   conf_D = funding_zscore_bonus + obi_bonus + zscore_bonus
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pandas as pd

DB = Path(__file__).resolve().parent.parent / "injective_autopilot.db"
MIN_CONFIDENCE = 0.45  # config/settings.py decision_min_confidence (valore reale in uso)


def base_name(sig: str) -> str:
    return sig.split("(")[0]


def parse_bonuses(reason: str) -> dict[str, float]:
    b = {"votes_margin": 0.0, "zscore_bonus": 0.0, "funding_zscore_bonus": 0.0, "obi_bonus": 0.0}
    if reason.startswith("conflict"):
        return b
    if "votes=" in reason:
        m = re.search(r"votes=(-?\d+)/(-?\d+)", reason)
        vl, vs = int(m.group(1)), int(m.group(2))
        margin = abs(vl - vs)
        b["votes_margin"] = 0.10 if margin >= 4 else 0.05
    if re.search(r"\bz=", reason):
        m = re.search(r"\bz=([\d.]+)", reason)
        z = float(m.group(1))
        b["zscore_bonus"] = 0.10 if z >= 3.0 else 0.05
    if "fz=" in reason:
        b["funding_zscore_bonus"] = 0.05
    if "obi=" in reason:
        b["obi_bonus"] = 0.05
    return b


def load() -> pd.DataFrame:
    con = sqlite3.connect(DB)
    trades = pd.read_sql(
        "select id, pnl_pct, confidence, reason, active_signals, entry_ts "
        "from trades where status='CLOSED' order by entry_ts", con)
    con.close()

    rows = []
    for _, t in trades.iterrows():
        active = json.loads(t["active_signals"])
        n = len(active)
        bonuses = parse_bonuses(t["reason"])
        qual_sum = sum(bonuses.values())

        conf_a = t["confidence"]
        conf_b = round(qual_sum, 3)
        conf_c = round(min(0.40 + (n - 2) * 0.05 + qual_sum, 0.95), 3)
        conf_d = round(bonuses["funding_zscore_bonus"] + bonuses["obi_bonus"] + bonuses["zscore_bonus"], 3)

        rows.append({
            "id": t["id"], "pnl_pct": t["pnl_pct"], "n_active_signals": n,
            "A_current": round(conf_a, 3), "B_no_nsig": conf_b,
            "C_half_nsig": conf_c, "D_only_quality": conf_d,
        })
    return pd.DataFrame(rows)


def wr_pf_exp(pnls: pd.Series) -> tuple[float, float, float, float]:
    if len(pnls) == 0:
        return 0.0, 0.0, 0.0, 0.0
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    wr = len(wins) / len(pnls) * 100
    gross_win = wins.sum()
    gross_loss = -losses.sum()
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    return wr, pf, pnls.mean(), pnls.sum()


def main():
    df = load()
    pd.set_option("display.width", 200)
    print(f"=== n trades closed = {len(df)} | soglia min_confidence = {MIN_CONFIDENCE} ===\n")

    print("--- Dettaglio per trade ---")
    print(df.to_string(index=False))

    variants = ["A_current", "B_no_nsig", "C_half_nsig", "D_only_quality"]

    print("\n##### Distribuzione confidence per variante #####")
    print(df[variants].describe().to_string())

    print("\n##### Trade sopra soglia 0.55 + WR/PF/expectancy/pnl aggregato #####")
    rows = []
    for v in variants:
        above = df[df[v] >= MIN_CONFIDENCE]
        wr, pf, exp, tot = wr_pf_exp(above["pnl_pct"])
        rows.append({
            "variante": v, "n_sopra_soglia": len(above),
            "WR%": round(wr, 1), "PF": round(pf, 3),
            "expectancy_pct": round(exp, 4), "pnl_aggregato_pct": round(tot, 3),
        })
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n##### WR/PF/expectancy/pnl su TUTTI i 29 trade (per riferimento) #####")
    wr, pf, exp, tot = wr_pf_exp(df["pnl_pct"])
    print(f"WR%={wr:.1f} PF={pf:.3f} expectancy={exp:.4f} pnl_tot={tot:.3f}")

    print("\n##### Ranking: correlazione Spearman tra ranking A e ranking B/C/D #####")
    from scipy.stats import spearmanr
    rank_a = df["A_current"].rank(ascending=False, method="average")
    for v in ["B_no_nsig", "C_half_nsig", "D_only_quality"]:
        rank_v = df[v].rank(ascending=False, method="average")
        rho, p = spearmanr(rank_a, rank_v)
        print(f"A vs {v}: spearman rho={rho:.3f} (p={p:.3f})")

    print("\n##### Ranking top-10 per variante (id, pnl_pct, confidence) #####")
    for v in variants:
        top = df.sort_values(v, ascending=False).head(10)
        print(f"\n-- {v} --")
        print(top[["id", v, "pnl_pct", "n_active_signals"]].to_string(index=False))

    print("\n##### Se si usasse C come gate (soglia 0.55): quali trade *cambiano* lato soglia vs A #####")
    a_pass = set(df.loc[df["A_current"] >= MIN_CONFIDENCE, "id"])
    c_pass = set(df.loc[df["C_half_nsig"] >= MIN_CONFIDENCE, "id"])
    print("Approvati con A ma non con C:", sorted(a_pass - c_pass))
    print("Approvati con C ma non con A:", sorted(c_pass - a_pass))
    print("pnl medio (approvati A, persi con C):",
          df[df["id"].isin(a_pass - c_pass)]["pnl_pct"].mean())


if __name__ == "__main__":
    main()
