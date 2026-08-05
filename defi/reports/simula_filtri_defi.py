import csv
from collections import defaultdict

# ---------- Load live_trades.csv, filter system==defi ----------
rows = []
with open('live_trades.csv') as f:
    r = csv.DictReader(f)
    for row in r:
        if row['system'] == 'defi':
            rows.append(row)

by_sig = defaultdict(list)
for row in rows:
    by_sig[row['signal_id']].append(row)

def is_closed(remaining):
    return remaining.strip() in ('0.0', '0.00', '0')

def parse_float(x):
    try:
        if x is None or x.strip() == '':
            return None
        return float(x)
    except:
        return None

closed = {}  # signal_id -> dict(pnl, entry, last)
for sig, rs in by_sig.items():
    rs_sorted = sorted(rs, key=lambda x: x['ts'])
    last = rs_sorted[-1]
    first = rs_sorted[0]
    if is_closed(last['remaining']):
        pnl = parse_float(last['pnl_eur']) or 0.0
        closed[sig] = dict(pnl=pnl, entry=first, last=last, ts_last=last['ts'])

print(f"# Trade defi chiusi: {len(closed)}")

# ---------- Load signals_log.csv ----------
sig_data = {}
with open('signals_log.csv') as f:
    r = csv.DictReader(f)
    for row in r:
        sig_data[row['signal_id']] = row

# ---------- Build trade list (baseline 451) ----------
trades = []
for sig, d in closed.items():
    t = dict(
        signal_id=sig,
        pnl=d['pnl'],
        win=d['pnl'] > 0,
        ts_last=d['ts_last'],
        vol_h1=parse_float(d['entry'].get('vol_h1')),
        bsr=parse_float(d['entry'].get('bsr')),
    )
    sd = sig_data.get(sig)
    if sd:
        t['has_join'] = True
        t['change_1h_pct'] = parse_float(sd.get('change_1h_pct'))
        t['pump_probability'] = parse_float(sd.get('pump_probability'))
        t['liquidity_usd'] = parse_float(sd.get('liquidity_usd'))
    else:
        t['has_join'] = False
        t['change_1h_pct'] = None
        t['pump_probability'] = None
        t['liquidity_usd'] = None
    trades.append(t)

n_total = len(trades)
n_join = sum(1 for t in trades if t['has_join'])
print(f"# Trade totali: {n_total}, con join signals_log: {n_join}")

# ---------- Outliers liquidity_usd > 1M ----------
outlier_sigs = set(t['signal_id'] for t in trades if t['liquidity_usd'] is not None and t['liquidity_usd'] > 1_000_000)
print(f"# Outlier liquidity_usd > 1M: {len(outlier_sigs)} -> {outlier_sigs}")

# Join subset (233 expected), excluding outliers
join_trades = [t for t in trades if t['has_join'] and t['signal_id'] not in outlier_sigs]
print(f"# Join subset (escl. outlier): {len(join_trades)}")

# ---------- Metrics helpers ----------
CAP = 999.0

def pf_and_expectancy(pnls):
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    sum_pos = sum(wins)
    sum_neg = sum(losses)
    if sum_neg != 0:
        pf = sum_pos / abs(sum_neg)
    else:
        pf = CAP if sum_pos > 0 else 0.0
    exp = sum(pnls) / len(pnls) if pnls else 0
    return pf, exp

def max_drawdown(trade_subset):
    # order by ts_last, build equity curve, compute peak-to-trough max drawdown
    ordered = sorted(trade_subset, key=lambda t: t['ts_last'])
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in ordered:
        equity += t['pnl']
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return max_dd

def summarize(name, subset, baseline_n):
    n = len(subset)
    retention = n / baseline_n * 100 if baseline_n else 0
    pnls = [t['pnl'] for t in subset]
    wins = [p for p in pnls if p > 0]
    wr = len(wins) / n * 100 if n else 0
    pf, exp = pf_and_expectancy(pnls)
    total_pnl = sum(pnls)
    mdd = max_drawdown(subset)
    return dict(
        name=name, n=n, baseline_n=baseline_n, retention=retention,
        winrate=wr, pf=pf, expectancy=exp, total_pnl=total_pnl, max_dd=mdd,
    )

# ============================================================
# Simulazioni
# ============================================================
results = []

# 1. vol_h1 >= 10350, baseline 451
subset1 = [t for t in trades if t['vol_h1'] is not None and t['vol_h1'] >= 10350]
results.append(summarize("1. vol_h1>=10350", subset1, n_total))

# 2. pump_probability < 0.299, baseline 233 (join subset, excl outliers)
subset2 = [t for t in join_trades if t['pump_probability'] is not None and t['pump_probability'] < 0.299]
results.append(summarize("2. pump_probability<0.299", subset2, len(join_trades)))

# 3. combinato
subset3 = [t for t in join_trades
           if t['vol_h1'] is not None and t['vol_h1'] >= 10350
           and t['pump_probability'] is not None and t['pump_probability'] < 0.299]
results.append(summarize("3. vol_h1>=10350 AND pump_prob<0.299", subset3, len(join_trades)))

# 4. change_1h_pct <= X, baseline 233
for X in [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]:
    subset4 = [t for t in join_trades if t['change_1h_pct'] is not None and t['change_1h_pct'] <= X]
    results.append(summarize(f"4. change_1h_pct<={X}", subset4, len(join_trades)))

# ============================================================
# Output: tabella 9 simulazioni
# ============================================================
print()
print("="*100)
print("TABELLA 9 SIMULAZIONI")
print("="*100)
hdr = f"{'Simulazione':38}{'n':>6}{'baseline':>10}{'retention%':>12}{'WR%':>8}{'PF':>8}{'exp(EUR)':>10}{'pnl_tot(EUR)':>14}{'maxDD(EUR)':>12}{'scartata?':>12}"
print(hdr)
for res in results:
    scartata = ""
    if res['retention'] < 70 or res['n'] < 250:
        reasons = []
        if res['retention'] < 70:
            reasons.append("ret<70%")
        if res['n'] < 250:
            reasons.append("n<250")
        scartata = ",".join(reasons)
    pf_s = f"{res['pf']:.3f}" if res['pf'] != CAP else "inf"
    print(f"{res['name']:38}{res['n']:>6}{res['baseline_n']:>10}{res['retention']:>12.1f}{res['winrate']:>8.1f}{pf_s:>8}{res['expectancy']:>10.3f}{res['total_pnl']:>14.2f}{res['max_dd']:>12.2f}{scartata:>12}")

# ============================================================
# Ranking top 10 (non scartate), per PF*retention% desc
# ============================================================
kept = [r for r in results if r['retention'] >= 70 and r['n'] >= 250]
for r in kept:
    pf_val = r['pf'] if r['pf'] != CAP else CAP
    r['rank_score'] = pf_val * r['retention']

kept_sorted = sorted(kept, key=lambda r: r['rank_score'], reverse=True)[:10]

print()
print("="*100)
print("RANKING TOP 10 (configurazioni non scartate, ordinate per PF x retention%)")
print("="*100)
hdr2 = f"{'#':3}{'Simulazione':38}{'n':>6}{'retention%':>12}{'WR%':>8}{'PF':>8}{'exp(EUR)':>10}{'pnl_tot(EUR)':>14}{'maxDD(EUR)':>12}{'PFxRet':>10}"
print(hdr2)
for i, r in enumerate(kept_sorted, 1):
    pf_s = f"{r['pf']:.3f}" if r['pf'] != CAP else "inf"
    print(f"{i:<3}{r['name']:38}{r['n']:>6}{r['retention']:>12.1f}{r['winrate']:>8.1f}{pf_s:>8}{r['expectancy']:>10.3f}{r['total_pnl']:>14.2f}{r['max_dd']:>12.2f}{r['rank_score']:>10.1f}")

# Print baseline values for reference
print()
print("="*100)
print("BASELINE DI RIFERIMENTO")
print("="*100)
base_451 = summarize("BASELINE 451 (tutti)", trades, n_total)
base_233 = summarize("BASELINE 233 (join, escl outlier)", join_trades, len(join_trades))
for b in [base_451, base_233]:
    pf_s = f"{b['pf']:.3f}" if b['pf'] != CAP else "inf"
    print(f"{b['name']:38}{b['n']:>6}{'':>10}{'100.0':>12}{b['winrate']:>8.1f}{pf_s:>8}{b['expectancy']:>10.3f}{b['total_pnl']:>14.2f}{b['max_dd']:>12.2f}")
