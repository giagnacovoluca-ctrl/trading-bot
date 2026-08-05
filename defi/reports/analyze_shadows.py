#!/usr/bin/env python3
"""
Analisi approfondita dei file Shadow:
- pump_grad_shadow.csv
- liq_shadow_queue.csv

Calcola Win Rate, Profit Factor, PnL per ciascuna ragione di scarto (skip_reason)
e simula l'effetto di ammorbidire/rimuovere o affinare i filtri.
"""

import pandas as pd
import numpy as np
from pathlib import Path

REPORTS_DIR = Path(__file__).parent
PUMP_SHADOW = REPORTS_DIR / "pump_grad_shadow.csv"
LIQ_SHADOW  = REPORTS_DIR / "liq_shadow_queue.csv"

def analyze_pump_shadow():
    if not PUMP_SHADOW.exists():
        print(f"File non trovato: {PUMP_SHADOW}")
        return

    print("==================================================================================")
    print(" ANALISI PUMP_GRAD SHADOW (Segnali scartati dal simulatore)")
    print("==================================================================================")
    
    df = pd.read_csv(PUMP_SHADOW)
    print(f"Totale registrazioni shadow: {len(df)}")
    
    # Pulizia colonne numeriche
    for col in ["exit_pct", "peak_pct", "skip_value"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    # Rimuovi eventuali outlier estremi per stabilità (es. >5000% dipendenti da dati illiquidi)
    df["capped_exit_pct"] = df["exit_pct"].clip(lower=-100.0, upper=300.0)
    
    print("\n--- Breakdown per skip_reason ---")
    grouped = df.groupby("skip_reason")
    
    stats = []
    for reason, group in grouped:
        n = len(group)
        wins = (group["exit_pct"] > 0).sum()
        losses = (group["exit_pct"] <= 0).sum()
        wr = (wins / n * 100) if n > 0 else 0.0
        
        pos_pnl = group[group["exit_pct"] > 0]["capped_exit_pct"].sum()
        neg_pnl = abs(group[group["exit_pct"] < 0]["capped_exit_pct"].sum())
        pf = (pos_pnl / neg_pnl) if neg_pnl > 0 else 0.0
        
        avg_exit = group["capped_exit_pct"].mean()
        avg_peak = group["peak_pct"].mean()
        
        stats.append({
            "skip_reason": reason,
            "count": n,
            "win_rate%": round(wr, 1),
            "PF": round(pf, 2),
            "avg_exit%": round(avg_exit, 2),
            "avg_peak%": round(avg_peak, 2),
            "tot_pnl_capped%": round(group["capped_exit_pct"].sum(), 1)
        })
        
    stats_df = pd.DataFrame(stats).sort_values("count", ascending=False)
    print(stats_df.to_string(index=False))

    print("\n--- Deep Dive per exit_reason su shadow ---")
    grouped_exit = df.groupby("exit_reason")["exit_pct"].agg(["count", "mean", "min", "max"])
    print(grouped_exit.to_string())

def analyze_liq_shadow():
    if not LIQ_SHADOW.exists():
        print(f"\nFile non trovato: {LIQ_SHADOW}")
        return

    print("\n==================================================================================")
    print(" ANALISI LIQ SHADOW QUEUE (Pool $10k-$25k scartate dalla coda live)")
    print("==================================================================================")
    
    df = pd.read_csv(LIQ_SHADOW)
    print(f"Totale pool in shadow queue: {len(df)}")
    if len(df) > 0:
        print(df.head())

if __name__ == "__main__":
    analyze_pump_shadow()
    analyze_liq_shadow()
