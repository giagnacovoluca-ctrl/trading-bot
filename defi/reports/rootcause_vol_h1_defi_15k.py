import csv
from collections import defaultdict

CSV_PATH = 'live_trades.csv'

rows = []
with open(CSV_PATH) as f:
    r = csv.DictReader(f)
    for row in r:
        rows.append(row)

by_sig = defaultdict(list)
for row in rows:
    by_sig[row['signal_id']].append(row)

def is_closed(remaining):
    return remaining.strip() in ('0.0', '0.00', '0')

def fnum(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

# Trade defi chiusi
closed_defi = []
for sig, rs in by_sig.items():
    rs_sorted = sorted(rs, key=lambda x: x['ts'])
    last = rs_sorted[-1]
    if last['system'] != 'defi':
        continue
    if not is_closed(last['remaining']):
        continue
    closed_defi.append((sig, rs_sorted, last))

print(f"Totale trade defi chiusi: {len(closed_defi)}")

# Trova riga entry (action == 'entry' o prima riga con vol_h1 valido) per ciascun signal
def get_entry_row(rs_sorted):
    for row in rs_sorted:
        if row.get('action','').lower() == 'entry':
            return row
    return rs_sorted[0]

def last_known_vol(rs_sorted):
    # ultimo valore vol_h1 non vuoto/non-zero in tutte le righe (compresa entry)
    last_val = None
    for row in rs_sorted:
        v = row.get('vol_h1','')
        try:
            fv = float(v)
        except Exception:
            continue
        if fv != 0:
            last_val = fv
    return last_val

records = []
for sig, rs_sorted, last in closed_defi:
    entry_row = get_entry_row(rs_sorted)
    entry_vol = fnum(entry_row.get('vol_h1', 0))
    has_entry = any(r.get('action','').lower() == 'entry' for r in rs_sorted)
    last_vol = last_known_vol(rs_sorted)
    note_all = " || ".join(set(r.get('note','') for r in rs_sorted if r.get('note','')))
    pnl_final = fnum(last.get('pnl_eur', 0))
    ts_entry = entry_row.get('ts','')
    token = entry_row.get('token_symbol', last.get('token_symbol',''))

    if entry_vol < 15000:
        records.append({
            'sig': sig,
            'token': token,
            'ts_entry': ts_entry,
            'entry_vol': entry_vol,
            'last_vol': last_vol,
            'has_entry': has_entry,
            'note': note_all,
            'pnl': pnl_final,
        })

print(f"Trade defi chiusi con vol_h1 entry < 15000: {len(records)}\n")

# Classificazione
def classify(rec):
    note = rec['note']
    if 'mock' in note.lower():
        return 'mock/test data'
    if 'azzerato' in note.lower():
        return 'dato azzerato retroattivamente (cleanup one-off)'
    if 'via_gemmev3' in note.lower():
        return 'via_gemmeV3 (filtro separato)'
    if not rec['has_entry']:
        return 'crash/anomalia senza entry reale'
    if 10000 <= rec['entry_vol'] < 15000:
        return 'bypass filtro 10k vs 15k'
    if rec['entry_vol'] < 10000:
        return 'altra causa (vol<10k con entry — verificare gate)'
    return 'altra causa'

for rec in records:
    rec['cat'] = classify(rec)

# Tabella dettaglio
print("="*140)
print(f"{'token':<12} {'ts_entry':<20} {'vol_entry':>10} {'vol_ultimo':>10} {'has_entry':>9} {'pnl':>8}  categoria / note")
print("="*140)
for rec in sorted(records, key=lambda r: r['ts_entry']):
    last_vol_s = f"{rec['last_vol']:.0f}" if rec['last_vol'] is not None else "n/a"
    print(f"{rec['token']:<12} {rec['ts_entry']:<20} {rec['entry_vol']:>10.1f} {last_vol_s:>10} {str(rec['has_entry']):>9} {rec['pnl']:>8.2f}  {rec['cat']}")
    if rec['note']:
        print(f"    note: {rec['note'][:200]}")

# Conteggi per categoria
print("\n" + "="*80)
print("RIEPILOGO PER CATEGORIA")
print("="*80)
cat_stats = defaultdict(lambda: {'n':0,'pnl':0.0})
for rec in records:
    cat_stats[rec['cat']]['n'] += 1
    cat_stats[rec['cat']]['pnl'] += rec['pnl']

for cat, d in sorted(cat_stats.items(), key=lambda x:-x[1]['n']):
    print(f"{cat:<55} n={d['n']:>3}  pnl_tot={d['pnl']:>9.2f}")

print(f"\nTOTALE  n={sum(d['n'] for d in cat_stats.values())}  pnl_tot={sum(d['pnl'] for d in cat_stats.values()):.2f}")

# ── Simulazione PF / expectancy eliminando ciascuna categoria ──────────────
print("\n" + "="*100)
print("SIMULAZIONE: 451 trade defi chiusi, PF/expectancy eliminando ciascuna categoria")
print("="*100)

all_pnls = [fnum(last.get('pnl_eur',0)) for _,_,last in closed_defi]
n_total = len(all_pnls)

def pf_expect(pnls):
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    pf = (sum(wins)/abs(sum(losses))) if losses and sum(losses) != 0 else float('inf')
    expectancy = sum(pnls)/len(pnls) if pnls else 0
    return pf, expectancy, sum(pnls)

base_pf, base_exp, base_tot = pf_expect(all_pnls)
print(f"\nBASELINE (tutti i 451): PF={base_pf:.3f}  expectancy={base_exp:.3f}  pnl_tot={base_tot:.2f}  n={n_total}")

# mappa signal_id -> categoria (solo per i record < 15000; altri = None/baseline)
sig_to_cat = {rec['sig']: rec['cat'] for rec in records}

results = []
for cat in sorted(set(rec['cat'] for rec in records)):
    sigs_to_remove = {sig for sig, c in sig_to_cat.items() if c == cat}
    remaining_pnls = []
    for sig, rs_sorted, last in closed_defi:
        if sig in sigs_to_remove:
            continue
        remaining_pnls.append(fnum(last.get('pnl_eur',0)))
    pf, exp, tot = pf_expect(remaining_pnls)
    n_rem = len(remaining_pnls)
    retention = 100.0 * n_rem / n_total
    results.append((cat, len(sigs_to_remove), n_rem, retention, pf, exp, tot, pf - base_pf))

print(f"\n{'categoria eliminata':<55} {'n_elim':>6} {'n_rest':>6} {'retention%':>10} {'PF':>8} {'delta_PF':>9} {'expectancy':>10} {'pnl_tot':>9}")
for cat, n_elim, n_rem, ret, pf, exp, tot, dpf in sorted(results, key=lambda x: -x[7]):
    pf_s = f"{pf:.3f}" if pf != float('inf') else "inf"
    print(f"{cat:<55} {n_elim:>6} {n_rem:>6} {ret:>9.1f}% {pf_s:>8} {dpf:>9.3f} {exp:>10.3f} {tot:>9.2f}")

print("\nCategoria col maggior salto di PF (= maggior parte del degrado attuale):")
top = max(results, key=lambda x: x[7])
print(f"  -> {top[0]}  (delta_PF=+{top[7]:.3f}, PF base={base_pf:.3f} -> {top[4] if top[4]!=float('inf') else 'inf'})")
