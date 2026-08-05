"""
Validazione post-fix (15/06): ricalcola PF/WR/EV per system=="defi" usando
is_valid_trade_event() per separare data_fault da risultati di strategia.
Esegui da defi/reports/.
"""
import csv
import sys
from collections import defaultdict

sys.path.insert(0, "..")
from data_quality import is_valid_trade_event

rows = []
with open("live_trades.csv") as f:
    for row in csv.DictReader(f):
        if row["system"] == "defi":
            rows.append(row)

by_sig = defaultdict(list)
for row in rows:
    by_sig[row["signal_id"]].append(row)


def is_closed(remaining):
    return remaining.strip() in ("0.0", "0.00", "0")


def pnl_of(row):
    try:
        return float(row["pnl_eur"].replace("+", ""))
    except (ValueError, KeyError):
        return 0.0


has_entry = {sid: any(r["action"] == "entry" for r in rs) for sid, rs in by_sig.items()}

closed = {}
for sid, rs in by_sig.items():
    last = sorted(rs, key=lambda x: x["ts"])[-1]
    if is_closed(last["remaining"]):
        closed[sid] = last


def pf_wr_ev(trades):
    pos = sum(pnl_of(t) for t in trades if pnl_of(t) > 0)
    neg = sum(-pnl_of(t) for t in trades if pnl_of(t) < 0)
    n = len(trades)
    wins = sum(1 for t in trades if pnl_of(t) > 0)
    pf = pos / neg if neg else float("inf")
    wr = wins / n * 100 if n else 0
    ev = sum(pnl_of(t) for t in trades) / n if n else 0
    return n, pf, wr, ev, sum(pnl_of(t) for t in trades)


all_trades = list(closed.values())
data_fault, valid_trades = [], []
fault_reasons = defaultdict(int)
for sid, last in closed.items():
    ok, reason = is_valid_trade_event(last, has_entry[sid])
    if ok:
        valid_trades.append(last)
    else:
        data_fault.append((sid, last, reason))
        fault_reasons[reason] += 1

print("=== Baseline (tutti i trade chiusi, defi) ===")
n, pf, wr, ev, tot = pf_wr_ev(all_trades)
print(f"n={n}  PF={pf:.3f}  WR={wr:.1f}%  EV={ev:.2f}€  pnl_tot={tot:.2f}€")

print("\n=== Con esclusione data_fault (is_valid_trade_event) ===")
n, pf, wr, ev, tot = pf_wr_ev(valid_trades)
print(f"n={n}  PF={pf:.3f}  WR={wr:.1f}%  EV={ev:.2f}€  pnl_tot={tot:.2f}€")

print(f"\ndata_fault trovati: {len(data_fault)}")
for reason, count in fault_reasons.items():
    print(f"  {reason}: {count}")
for sid, last, reason in data_fault:
    print(f"  - {sid} | {last.get('token_symbol')} | {last.get('ts')} | "
          f"chg={last.get('change_pct')} | pnl={last.get('pnl_eur')} | {reason}")
