"""
Analisi quant one-shot (read-only): potere discriminante delle feature
in signal_values rispetto al pnl_pct dei trade CLOSED.
Nessuna modifica al sistema — solo report.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

DB = Path(__file__).resolve().parent.parent / "injective_autopilot.db"


def load() -> pd.DataFrame:
    con = sqlite3.connect(DB)
    trades = pd.read_sql(
        "select id, direction, pnl_pct, pnl_usdt, confidence, mae_pct, mfe_pct, "
        "active_signals, signal_values, exit_reason "
        "from trades where status='CLOSED'", con)
    pm = pd.read_sql(
        "select trade_id, ts, hold_hours, r_multiple from trade_postmortems", con)
    con.close()
    # un trade puo' avere piu' postmortem (ri-generati): tieni l'ultimo
    pm = pm.sort_values("ts").groupby("trade_id", as_index=False).last().drop(columns=["ts"])

    sv = pd.json_normalize(trades["signal_values"].apply(json.loads))
    n_sig = trades["active_signals"].apply(lambda s: len(json.loads(s)))

    df = pd.concat([trades.drop(columns=["signal_values", "active_signals"]), sv], axis=1)
    df["n_active_signals"] = n_sig
    df = df.merge(pm, left_on="id", right_on="trade_id", how="left").drop(columns=["trade_id"])
    return df


def numeric_features(df: pd.DataFrame) -> list[str]:
    exclude = {"pnl_pct", "pnl_usdt", "id"}
    cols = []
    for c in df.columns:
        if c in exclude:
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            if df[c].nunique(dropna=True) <= 1:
                continue
            cols.append(c)
    return cols


def cohend(a, b) -> float:
    a, b = np.asarray(a, float), np.asarray(b, float)
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return float("nan")
    pooled = np.sqrt(((na - 1) * a.var(ddof=1) + (nb - 1) * b.var(ddof=1)) / (na + nb - 2))
    if pooled == 0:
        return 0.0
    return (a.mean() - b.mean()) / pooled


def auc(a, b) -> float:
    """AUC = P(b_sample > a_sample), via Mann-Whitney U."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) == 0 or len(b) == 0:
        return float("nan")
    try:
        u, _ = stats.mannwhitneyu(b, a, alternative="two-sided")
    except ValueError:
        return float("nan")
    return u / (len(a) * len(b))


def analyze_group(df: pd.DataFrame, group_col: str, pos_label, neg_label, feats: list[str]) -> pd.DataFrame:
    pos = df[df[group_col] == pos_label]
    neg = df[df[group_col] == neg_label]
    rows = []
    for f in feats:
        a = neg[f].dropna()
        b = pos[f].dropna()
        if len(a) < 2 or len(b) < 2:
            continue
        d = cohend(b, a)
        u = auc(a, b)  # >0.5 => higher in pos group
        try:
            _, p = stats.mannwhitneyu(a, b, alternative="two-sided")
        except ValueError:
            p = float("nan")
        rows.append({
            "feature": f,
            f"mean_{neg_label}": a.mean(), f"median_{neg_label}": a.median(),
            f"mean_{pos_label}": b.mean(), f"median_{pos_label}": b.median(),
            "cohend": d, "auc_sep": u, "mwu_p": p,
            "abs_cohend": abs(d) if not np.isnan(d) else -1,
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values("abs_cohend", ascending=False)


def correlations(df: pd.DataFrame, feats: list[str]) -> pd.DataFrame:
    rows = []
    for f in feats:
        sub = df[[f, "pnl_pct"]].dropna()
        if len(sub) < 3 or sub[f].nunique() <= 1:
            continue
        pear, p_pear = stats.pearsonr(sub[f], sub["pnl_pct"])
        spear, p_spear = stats.spearmanr(sub[f], sub["pnl_pct"])
        rows.append({"feature": f, "n": len(sub),
                      "pearson_r": pear, "pearson_p": p_pear,
                      "spearman_r": spear, "spearman_p": p_spear})
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out["abs_spearman"] = out["spearman_r"].abs()
    return out.sort_values("abs_spearman", ascending=False)


def main():
    df = load()
    feats = numeric_features(df)
    print(f"=== n trades closed = {len(df)} | n feature numeriche = {len(feats)} ===")
    print("features:", feats)
    print()
    print("PNL distribution:")
    print(df["pnl_pct"].describe())
    print()

    df["is_winner"] = df["pnl_pct"] > 0
    df["above_5"] = df["pnl_pct"] > 5.0
    q75, q25 = df["pnl_pct"].quantile([0.75, 0.25])
    df["top25"] = df["pnl_pct"] >= q75
    df["bottom25"] = df["pnl_pct"] <= q25
    print(f"q25={q25:.3f} q75={q75:.3f} | winners={df['is_winner'].sum()}/{len(df)} | "
          f">5%={df['above_5'].sum()}")
    print()

    print("\n##### A/C — Winner vs Loser (Cohen's d, AUC, MWU p) #####")
    g1 = analyze_group(df, "is_winner", True, False, feats)
    print(g1.to_string(index=False))

    print("\n##### Profit>5% vs <=5% #####")
    g2 = analyze_group(df, "above_5", True, False, feats)
    print(g2.to_string(index=False))

    print("\n##### Top25% vs Bottom25% (by pnl_pct) #####")
    sub = df[df["top25"] | df["bottom25"]].copy()
    g3 = analyze_group(sub, "top25", True, False, feats)
    print(g3.to_string(index=False))

    print("\n##### Correlazione con pnl_pct (Pearson/Spearman) #####")
    corr = correlations(df, feats)
    print(corr.to_string(index=False))

    # categorical features
    print("\n##### Feature categoriche: media pnl_pct per categoria #####")
    for c in ["vol_regime", "oi_div_pattern", "direction", "exit_reason"]:
        if c in df.columns:
            g = df.groupby(c)["pnl_pct"].agg(["count", "mean", "median"])
            print(f"\n-- {c} --")
            print(g.to_string())


if __name__ == "__main__":
    main()
