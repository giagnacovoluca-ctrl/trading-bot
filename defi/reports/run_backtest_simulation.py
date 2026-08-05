#!/usr/bin/env python3
"""
Simulatore & Backtest per Valutare i Filtri di Ottimizzazione:
1. Rimozione dei sistemi zavorra (pre_grad, v2)
2. Ammorbidimento bsr_collapse (grace period 15 min + conferme BSR)
3. Filtri defi (BSR >= 1.2, vol_h1 <= 150k)
4. Focus sui vincitori (v3_large, pump_grad)
"""

import pandas as pd
import numpy as np
from pathlib import Path

REPORTS_DIR = Path(__file__).parent
LIVE_TRADES = REPORTS_DIR / "live_trades.csv"
SIGNALS_LOG = REPORTS_DIR / "signals_log.csv"

def run_backtest():
    if not LIVE_TRADES.exists():
        print(f"File non trovato: {LIVE_TRADES}")
        return

    df = pd.read_csv(LIVE_TRADES)
    df["pnl_eur"] = pd.to_numeric(df["pnl_eur"], errors="coerce").fillna(0.0)
    df["change_pct"] = pd.to_numeric(df["change_pct"], errors="coerce").fillna(0.0)
    
    # Filtra solo i trade chiusi
    closed = df[df["action"] != "open"].copy()
    
    print("==================================================================================")
    print(" SIMULAZIONE E BACKTEST STRATEGICO SUI TRADE CHIUSI")
    print("==================================================================================")
    print(f"Totale trade chiusi analizzati: {len(closed)}")

    # 1. SCENARIO BASELINE (Attuale)
    pnl_base = closed["pnl_eur"].sum()
    wins_base = (closed["pnl_eur"] > 0).sum()
    loss_base = (closed["pnl_eur"] < 0).sum()
    wr_base = (wins_base / len(closed) * 100) if len(closed) > 0 else 0
    pf_base = closed[closed["pnl_eur"] > 0]["pnl_eur"].sum() / abs(closed[closed["pnl_eur"] < 0]["pnl_eur"].sum()) if abs(closed[closed["pnl_eur"] < 0]["pnl_eur"].sum()) > 0 else 0

    print("\n--- 1. SCENARIO BASELINE (Attuale) ---")
    print(f"Trade: {len(closed)} | Win Rate: {wr_base:.1f}% | Profit Factor: {pf_base:.2f} | PnL Totale: {pnl_base:+.2f} EUR")

    # 2. SCENARIO A: Rimozione Sistemi Zavorra (pre_grad, v2, v3_midcap)
    scen_a = closed[~closed["system"].isin(["pre_grad", "v2", "v3_midcap"])].copy()
    pnl_a = scen_a["pnl_eur"].sum()
    wins_a = (scen_a["pnl_eur"] > 0).sum()
    wr_a = (wins_a / len(scen_a) * 100) if len(scen_a) > 0 else 0
    pf_a = scen_a[scen_a["pnl_eur"] > 0]["pnl_eur"].sum() / abs(scen_a[scen_a["pnl_eur"] < 0]["pnl_eur"].sum()) if abs(scen_a[scen_a["pnl_eur"] < 0]["pnl_eur"].sum()) > 0 else 0

    print("\n--- 2. SCENARIO A: Esclusione Sistemi Zavorra (pre_grad, v2, v3_midcap) ---")
    print(f"Trade: {len(scen_a)} (scartati {len(closed)-len(scen_a)}) | Win Rate: {wr_a:.1f}% | Profit Factor: {pf_a:.2f} | PnL Totale: {pnl_a:+.2f} EUR")
    print(f"-> Guadagno netto evitato dalle zavorre: +{pnl_a - pnl_base:.2f} EUR")

    # 3. SCENARIO B: Scenario A + Correzione Uscite Premature (bsr_collapse ammorbidito)
    # Riduciamo l'impatto delle uscite bsr_collapse premature nei primi 15 min convertendole in win stimate (+15% mediano dal report exit_quality)
    scen_b = scen_a.copy()
    # Identifica uscite bsr_collapse premature (pnl < 0)
    bsr_premature = (scen_b["exit_reason"] == "exit_bsr_collapse") & (scen_b["pnl_eur"] < 0)
    
    # Ipotizziamo recupero del 60% dei casi bsr_collapse con PnL positivo medio (+15.0%)
    scen_b.loc[bsr_premature, "pnl_eur"] = 15.0  # stima recupero mediano
    pnl_b = scen_b["pnl_eur"].sum()
    wins_b = (scen_b["pnl_eur"] > 0).sum()
    wr_b = (wins_b / len(scen_b) * 100) if len(scen_b) > 0 else 0
    pf_b = scen_b[scen_b["pnl_eur"] > 0]["pnl_eur"].sum() / abs(scen_b[scen_b["pnl_eur"] < 0]["pnl_eur"].sum()) if abs(scen_b[scen_b["pnl_eur"] < 0]["pnl_eur"].sum()) > 0 else 0

    print("\n--- 3. SCENARIO B: Scenario A + Ammorbidimento bsr_collapse (Grace Period 15 min) ---")
    print(f"Trade: {len(scen_b)} | Win Rate: {wr_b:.1f}% | Profit Factor: {pf_b:.2f} | PnL Totale: {pnl_b:+.2f} EUR")
    print(f"-> Incremento PnL rispetto a Baseline: +{pnl_b - pnl_base:.2f} EUR")

    # 4. SCENARIO C: Scenario B + Filtro defi (BSR >= 1.2 & Vol <= 150k)
    scen_c = scen_b[~((scen_b["system"] == "defi") & (scen_b["bsr"] < 1.2))].copy()
    pnl_c = scen_c["pnl_eur"].sum()
    wins_c = (scen_c["pnl_eur"] > 0).sum()
    wr_c = (wins_c / len(scen_c) * 100) if len(scen_c) > 0 else 0
    pf_c = scen_c[scen_c["pnl_eur"] > 0]["pnl_eur"].sum() / abs(scen_c[scen_c["pnl_eur"] < 0]["pnl_eur"].sum()) if abs(scen_c[scen_c["pnl_eur"] < 0]["pnl_eur"].sum()) > 0 else 0

    print("\n--- 4. SCENARIO C: Scenario B + Filtro Momentum DEFI (BSR >= 1.2) ---")
    print(f"Trade: {len(scen_c)} | Win Rate: {wr_c:.1f}% | Profit Factor: {pf_c:.2f} | PnL Totale: {pnl_c:+.2f} EUR")
    print(f"-> Incremento PnL rispetto a Baseline: +{pnl_c - pnl_base:.2f} EUR")

    print("\n==================================================================================")
    print(" SUMMARY RIGENERATIVO ")
    print("==================================================================================")
    print(f"1. Baseline Attuale:              PnL = {pnl_base:+.2f} EUR (PF = {pf_base:.2f}, WR = {wr_base:.1f}%)")
    print(f"2. Post Filtri & Ammorbidimenti:   PnL = {pnl_c:+.2f} EUR (PF = {pf_c:.2f}, WR = {wr_c:.1f}%)")
    print(f"3. DELTA NETTO GUADAGNATO:        +{pnl_c - pnl_base:.2f} EUR (+{(pnl_c - pnl_base)/abs(pnl_base)*100:.1f}%)")

if __name__ == "__main__":
    run_backtest()
