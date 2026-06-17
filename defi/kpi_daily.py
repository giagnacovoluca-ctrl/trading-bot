#!/usr/bin/env python3
"""
kpi_daily.py — KPI giornalieri sistema di trading
Legge live_trades.csv e stampa il report per la finestra specificata.

Usage:
    python defi/kpi_daily.py              # ultime 24h
    python defi/kpi_daily.py --hours 48   # ultime 48h
    python defi/kpi_daily.py --all        # tutti i trade storici
"""
import csv, sys, argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# --- Path ---
_DIR   = Path(__file__).parent
_LT    = _DIR / "reports" / "live_trades.csv"
_SIGS  = [
    _DIR / "reports" / "signals_log.csv",
    _DIR / "reports" / "pump_grad_signals.csv",
    _DIR / "reports" / "pre_grad_signals.csv",
    _DIR / "reports" / "mirror_signals.csv",
]

# --- CLI ---
ap = argparse.ArgumentParser()
ap.add_argument("--hours", type=float, default=24)
ap.add_argument("--all",   action="store_true")
args = ap.parse_args()

# --- Load live_trades ---
rows = list(csv.DictReader(_LT.open()))
last_by_sid  = {}
entry_by_sid = {}
for r in rows:
    last_by_sid[r["signal_id"]] = r
    if r["action"] == "entry" and r["signal_id"] not in entry_by_sid:
        entry_by_sid[r["signal_id"]] = r

# --- Load liquidity from signal CSVs ---
liq_by_sid = {}
for p in _SIGS:
    if not p.exists():
        continue
    for r in csv.DictReader(p.open()):
        sid = r.get("signal_id", "")
        liq = r.get("liquidity_usd", "") or ""
        if sid and liq:
            try:
                liq_by_sid[sid] = float(liq)
            except ValueError:
                pass

# --- Filter window ---
now = datetime.now()
if args.all:
    cutoff = datetime(2000, 1, 1)
    window_label = "STORICO COMPLETO"
else:
    cutoff = now - timedelta(hours=args.hours)
    window_label = f"Ultime {args.hours:.0f}h  ({cutoff.strftime('%d/%m %H:%M')} → ora)"

SKIP_REASONS = {"", "open", "skip_stale", "skip", "skip_routing",
                "purged_stale", "duplicate_pair", "no_pair_address",
                "expired_max_age", "-98.28"}

closed = []
for r in last_by_sid.values():
    if r["exit_reason"] in SKIP_REASONS or r["action"] in ("entry", "open"):
        continue
    try:
        ts = datetime.fromisoformat(r["ts"])
    except ValueError:
        continue
    if ts >= cutoff:
        closed.append(r)

# --- Helpers ---
def pnl(r):  return float(r.get("pnl_eur") or 0)
def is_win(r): return pnl(r) > 0

def pf(trades):
    wins  = sum(pnl(r) for r in trades if pnl(r) > 0)
    losses= sum(abs(pnl(r)) for r in trades if pnl(r) < 0)
    return wins / losses if losses > 0 else float("inf")

def summary_line(trades):
    if not trades:
        return "n=0"
    n    = len(trades)
    wr   = sum(1 for r in trades if is_win(r)) / n * 100
    tot  = sum(pnl(r) for r in trades)
    _pf  = pf(trades)
    hs   = sum(1 for r in trades if r["exit_reason"] == "hard_sl")
    return (f"n={n:3d}  WR={wr:4.0f}%  PF={_pf:4.2f}  "
            f"PnL={tot:+8.1f}€  avg={tot/n:+5.1f}€  hs%={hs/n*100:3.0f}%")

def entry_val(sid, field, default=0.0):
    r = entry_by_sid.get(sid, {})
    try:
        return float(r.get(field) or default)
    except ValueError:
        return default

# --- ANSI colors ---
G = "\033[32m"; R = "\033[31m"; Y = "\033[33m"; B = "\033[1m"; E = "\033[0m"
def colored(val, text):
    c = G if val > 0 else (R if val < 0 else "")
    return f"{c}{text}{E}"

# ============================================================
print(f"\n{B}{'='*60}{E}")
print(f"{B}  KPI TRADING — {window_label}{E}")
print(f"{B}{'='*60}{E}\n")

# === 1. Totale ===
print(f"{B}── TOTALE ──{E}")
if not closed:
    print("  Nessun trade nel periodo.\n")
    sys.exit(0)
tot_pnl = sum(pnl(r) for r in closed)
print(f"  {summary_line(closed)}")
print(f"  PnL totale: {colored(tot_pnl, f'{tot_pnl:+.1f}€')}\n")

# === 2. Per sistema ===
print(f"{B}── PnL PER SISTEMA ──{E}")
by_sys = defaultdict(list)
for r in closed:
    by_sys[r["system"]].append(r)
for sys_name, trades in sorted(by_sys.items(), key=lambda x: sum(pnl(r) for r in x[1])):
    tot = sum(pnl(r) for r in trades)
    line = summary_line(trades)
    print(f"  {sys_name:<14} {line}   {colored(tot, f'{tot:+.1f}€')}")
print()

# === 3. Per exit_reason ===
print(f"{B}── PnL PER EXIT REASON ──{E}")
by_exit = defaultdict(list)
for r in closed:
    by_exit[r["exit_reason"]].append(r)
for er, trades in sorted(by_exit.items(), key=lambda x: sum(pnl(r) for r in x[1])):
    tot = sum(pnl(r) for r in trades)
    n   = len(trades)
    avg = tot / n
    print(f"  {er:<24} n={n:3d}  {colored(tot, f'PnL={tot:+8.1f}€')}  avg={avg:+5.1f}€")
print()

# === 4. Per fascia vol_h1 (ENTRY) ===
print(f"{B}── PnL PER FASCIA vol_h1 (all'entry) ──{E}")
VOL_BUCKETS = [
    (0,       1,      "= 0    "),
    (1,       5_000,  "1-5k   "),
    (5_000,   15_000, "5-15k  "),
    (15_000,  30_000, "15-30k "),
    (30_000,  50_000, "30-50k "),
    (50_000,  999_999_999, "50k+   "),
]
for lo, hi, label in VOL_BUCKETS:
    sub = [r for r in closed
           if lo <= entry_val(r["signal_id"], "vol_h1") < hi]
    if not sub:
        continue
    tot = sum(pnl(r) for r in sub)
    wr  = sum(1 for r in sub if is_win(r)) / len(sub) * 100
    print(f"  vol_h1 {label}  {colored(tot, f'n={len(sub):3d}  WR={wr:4.0f}%  PnL={tot:+8.1f}€  avg={tot/len(sub):+5.1f}€')}")
print()

# === 5. Per fascia BSR (ENTRY) ===
print(f"{B}── PnL PER FASCIA BSR (all'entry) ──{E}")
BSR_BUCKETS = [
    (0.00, 0.45, "0.0-0.45"),
    (0.45, 0.55, "0.45-0.55"),
    (0.55, 0.65, "0.55-0.65"),
    (0.65, 0.75, "0.65-0.75"),
    (0.75, 1.01, "0.75+   "),
]
for lo, hi, label in BSR_BUCKETS:
    sub = [r for r in closed
           if lo <= entry_val(r["signal_id"], "bsr") < hi]
    if not sub:
        continue
    tot = sum(pnl(r) for r in sub)
    wr  = sum(1 for r in sub if is_win(r)) / len(sub) * 100
    hs  = sum(1 for r in sub if r["exit_reason"] == "hard_sl") / len(sub) * 100
    print(f"  BSR {label}  n={len(sub):3d}  WR={wr:4.0f}%  "
          f"{colored(tot, f'PnL={tot:+8.1f}€')}  hs%={hs:3.0f}%")
print()

# === 6. Per fascia liquidità ===
print(f"{B}── PnL PER FASCIA LIQUIDITÀ (al segnale) ──{E}")
LIQ_BUCKETS = [
    (0,       1,       "= 0    "),
    (1,       10_000,  "1-10k  "),
    (10_000,  25_000,  "10-25k "),
    (25_000,  50_000,  "25-50k "),
    (50_000,  100_000, "50-100k"),
    (100_000, 999_999_999, "100k+  "),
]
no_liq = 0
for lo, hi, label in LIQ_BUCKETS:
    sub = []
    for r in closed:
        liq = liq_by_sid.get(r["signal_id"])
        if liq is None:
            if lo == 0 and hi == 1:
                no_liq += 1
            continue
        if lo <= liq < hi:
            sub.append(r)
    if not sub:
        continue
    tot = sum(pnl(r) for r in sub)
    wr  = sum(1 for r in sub if is_win(r)) / len(sub) * 100
    print(f"  liq {label}  n={len(sub):3d}  WR={wr:4.0f}%  "
          f"{colored(tot, f'PnL={tot:+8.1f}€')}  avg={tot/len(sub):+5.1f}€")
if no_liq:
    print(f"  (liq non disponibile per {no_liq} trade)")
print()

# === 7. Hard SL breakdown giornaliero ===
hs_trades = [r for r in closed if r["exit_reason"] == "hard_sl"]
if hs_trades:
    print(f"{B}── HARD SL RATE: {len(hs_trades)}/{len(closed)} = "
          f"{len(hs_trades)/len(closed)*100:.0f}% ──{E}")
    by_sys_hs = defaultdict(int)
    for r in hs_trades:
        by_sys_hs[r["system"]] += 1
    for s, cnt in sorted(by_sys_hs.items(), key=lambda x: -x[1]):
        total_s = len(by_sys[s])
        print(f"  {s:<14} {cnt}/{total_s} = {cnt/total_s*100:.0f}%")
    print()

print(f"{B}{'='*60}{E}\n")
