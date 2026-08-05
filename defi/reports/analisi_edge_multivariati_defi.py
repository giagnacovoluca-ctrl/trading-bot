import csv
import itertools
from collections import defaultdict

CAP = 999.0

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

closed = {}
for sig, rs in by_sig.items():
    rs_sorted = sorted(rs, key=lambda x: x['ts'])
    last = rs_sorted[-1]
    first = rs_sorted[0]
    if is_closed(last['remaining']):
        pnl = parse_float(last['pnl_eur']) or 0.0
        closed[sig] = dict(pnl=pnl, entry=first, last=last, ts_last=last['ts'])

# ---------- Load signals_log.csv ----------
sig_data = {}
with open('signals_log.csv') as f:
    r = csv.DictReader(f)
    for row in r:
        sig_data[row['signal_id']] = row

def parse_top_features(tf):
    parsed = {}
    if tf:
        for part in tf.split('|'):
            part = part.strip()
            if '=' in part:
                k, v = part.split('=', 1)
                v = parse_float(v.strip())
                if v is not None:
                    parsed[k.strip()] = v
    return parsed

# ---------- Build trade records ----------
trades = []
for sig, d in closed.items():
    t = dict(
        signal_id=sig,
        pnl=d['pnl'],
        win=d['pnl'] > 0,
        ts_last=d['ts_last'],
        vol_h1=parse_float(d['entry'].get('vol_h1')),
        bsr=parse_float(d['entry'].get('bsr')),
        change_1h_pct=None,
        pump_probability=None,
        liquidity_usd=None,
        prepump_composite_score=None,
        volume_acceleration=None,
        vol_accel_5m_vs_1h=None,
        has_join=False,
    )
    sd = sig_data.get(sig)
    if sd:
        t['has_join'] = True
        t['change_1h_pct'] = parse_float(sd.get('change_1h_pct'))
        t['pump_probability'] = parse_float(sd.get('pump_probability'))
        t['liquidity_usd'] = parse_float(sd.get('liquidity_usd'))
        tf = parse_top_features(sd.get('top_features', ''))
        t['prepump_composite_score'] = tf.get('prepump_composite_score')
        t['volume_acceleration'] = tf.get('volume_acceleration')
        t['vol_accel_5m_vs_1h'] = tf.get('vol_accel_5m_vs_1h')
    trades.append(t)

n_total = len(trades)  # 451 expected

# Outliers liquidity_usd > 1M -> exclude from join subset
outlier_sigs = set(t['signal_id'] for t in trades if t['liquidity_usd'] is not None and t['liquidity_usd'] > 1_000_000)
join_trades = [t for t in trades if t['has_join'] and t['signal_id'] not in outlier_sigs]
n_join = len(join_trades)  # 226 expected

# ---------- Metric helpers ----------
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

def max_drawdown(subset):
    ordered = sorted(subset, key=lambda t: t['ts_last'])
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

def summarize(label, subset, baseline_n, baseline_label):
    n = len(subset)
    retention = n / baseline_n * 100 if baseline_n else 0
    pnls = [t['pnl'] for t in subset]
    wins = [p for p in pnls if p > 0]
    wr = len(wins) / n * 100 if n else 0
    pf, exp = pf_and_expectancy(pnls)
    total_pnl = sum(pnls)
    mdd = max_drawdown(subset)
    return dict(
        label=label, n=n, baseline_n=baseline_n, baseline_label=baseline_label,
        retention=retention, winrate=wr, pf=pf, expectancy=exp,
        total_pnl=total_pnl, max_dd=mdd,
    )

# ---------- Feature definitions ----------
# 'universal' = available on all 451 (vol_h1, bsr)
# 'join' = available only on 226-subset
FEATURES = {
    'vol_h1':    {'kind': 'universal', 'thresholds': [('>=', 10000), ('>=', 15000), ('>=', 20000)]},
    'bsr':       {'kind': 'universal', 'thresholds': [('>=', 0.9), ('>=', 1.0), ('>=', 1.1)]},
    'change_1h_pct':      {'kind': 'join', 'thresholds': [('<=', 0.5), ('<=', 1.0), ('<=', 1.5), ('<=', 2.0)]},
    'pump_probability':   {'kind': 'join', 'thresholds': [('<', 0.20), ('<', 0.25), ('<', 0.30)]},
}

def apply_cond(t, feat, op, thr):
    v = t.get(feat)
    if v is None:
        return False
    if op == '>=':
        return v >= thr
    if op == '<=':
        return v <= thr
    if op == '<':
        return v < thr
    return False

def cond_label(feat, op, thr):
    return f"{feat}{op}{thr}"

def determine_baseline(feat_list):
    # if any feature is 'join' kind -> baseline = join_trades (226)
    if any(FEATURES[f]['kind'] == 'join' for f in feat_list):
        return join_trades, n_join, "226(join)"
    else:
        return trades, n_total, "451(tutti)"

# ---------- Generate all configs: singles, pairs, triples ----------
all_results = []

feat_names = list(FEATURES.keys())

def feat_threshold_list(feat):
    return [(feat, op, thr) for (op, thr) in FEATURES[feat]['thresholds']]

# Singles
for feat in feat_names:
    for (op, thr) in FEATURES[feat]['thresholds']:
        conds = [(feat, op, thr)]
        base_pool, base_n, base_label = determine_baseline([feat])
        subset = [t for t in base_pool if apply_cond(t, feat, op, thr)]
        label = cond_label(feat, op, thr)
        res = summarize(label, subset, base_n, base_label)
        all_results.append(res)

# Pairs (different features)
for f1, f2 in itertools.combinations(feat_names, 2):
    for c1 in feat_threshold_list(f1):
        for c2 in feat_threshold_list(f2):
            conds = [c1, c2]
            base_pool, base_n, base_label = determine_baseline([f1, f2])
            subset = [t for t in base_pool if apply_cond(t, *c1) and apply_cond(t, *c2)]
            label = cond_label(*c1) + " & " + cond_label(*c2)
            res = summarize(label, subset, base_n, base_label)
            all_results.append(res)

# Triples (different features)
for f1, f2, f3 in itertools.combinations(feat_names, 3):
    for c1 in feat_threshold_list(f1):
        for c2 in feat_threshold_list(f2):
            for c3 in feat_threshold_list(f3):
                conds = [c1, c2, c3]
                base_pool, base_n, base_label = determine_baseline([f1, f2, f3])
                subset = [t for t in base_pool
                          if apply_cond(t, *c1) and apply_cond(t, *c2) and apply_cond(t, *c3)]
                label = cond_label(*c1) + " & " + cond_label(*c2) + " & " + cond_label(*c3)
                res = summarize(label, subset, base_n, base_label)
                all_results.append(res)

# ---------- Filter: n>=250 and retention>=70% ----------
kept = [r for r in all_results if r['n'] >= 250 and r['retention'] >= 70]

# ---------- Rank by PF x retention% ----------
for r in kept:
    pf_val = r['pf'] if r['pf'] != CAP else CAP
    r['rank_score'] = pf_val * r['retention']

kept_sorted = sorted(kept, key=lambda r: r['rank_score'], reverse=True)

top20 = kept_sorted[:20]

# ---------- Baselines for reference ----------
base_451 = summarize("BASELINE 451 (tutti)", trades, n_total, "451")
base_226 = summarize("BASELINE 226 (join, escl outlier)", join_trades, n_join, "226")

# Known reference: best single filter vol_h1>=10350, PF 1.188, retention 89.8%
ref_pf = 1.188
ref_ret = 89.8

# ---------- OUTPUT ----------
print("="*120)
print("TOP 20 CONFIGURAZIONI (ordinate per PF x retention%, n>=250 e retention>=70%)")
print("="*120)
hdr = f"{'#':3}{'Feature(i)+soglie':46}{'baseline':>10}{'n':>6}{'ret%':>7}{'WR%':>7}{'PF':>7}{'exp(EUR)':>10}{'pnl(EUR)':>11}{'maxDD(EUR)':>12}"
print(hdr)
for i, r in enumerate(top20, 1):
    pf_s = f"{r['pf']:.3f}" if r['pf'] != CAP else "inf"
    print(f"{i:<3}{r['label']:46}{r['baseline_label']:>10}{r['n']:>6}{r['retention']:>7.1f}{r['winrate']:>7.1f}{pf_s:>7}{r['expectancy']:>10.3f}{r['total_pnl']:>11.2f}{r['max_dd']:>12.2f}")

print()
print("="*120)
print("EVIDENZE")
print("="*120)

# Miglior PF assoluto
best_pf = max(kept, key=lambda r: (r['pf'] if r['pf'] != CAP else CAP))
pf_s = f"{best_pf['pf']:.3f}" if best_pf['pf'] != CAP else "inf"
print(f"Miglior PF assoluto:        {best_pf['label']:46} baseline={best_pf['baseline_label']:>10} n={best_pf['n']:>4} ret%={best_pf['retention']:5.1f} WR%={best_pf['winrate']:5.1f} PF={pf_s} exp={best_pf['expectancy']:.3f} pnl={best_pf['total_pnl']:.2f} maxDD={best_pf['max_dd']:.2f}")

# Miglior PF con retention>=80%
cand_80 = [r for r in kept if r['retention'] >= 80]
if cand_80:
    best_pf_80 = max(cand_80, key=lambda r: (r['pf'] if r['pf'] != CAP else CAP))
    pf_s = f"{best_pf_80['pf']:.3f}" if best_pf_80['pf'] != CAP else "inf"
    print(f"Miglior PF con ret>=80%:    {best_pf_80['label']:46} baseline={best_pf_80['baseline_label']:>10} n={best_pf_80['n']:>4} ret%={best_pf_80['retention']:5.1f} WR%={best_pf_80['winrate']:5.1f} PF={pf_s} exp={best_pf_80['expectancy']:.3f} pnl={best_pf_80['total_pnl']:.2f} maxDD={best_pf_80['max_dd']:.2f}")
else:
    print("Miglior PF con ret>=80%:    nessuna configurazione superstite con retention>=80%")

# Miglior expectancy
best_exp = max(kept, key=lambda r: r['expectancy'])
pf_s = f"{best_exp['pf']:.3f}" if best_exp['pf'] != CAP else "inf"
print(f"Miglior expectancy:         {best_exp['label']:46} baseline={best_exp['baseline_label']:>10} n={best_exp['n']:>4} ret%={best_exp['retention']:5.1f} WR%={best_exp['winrate']:5.1f} PF={pf_s} exp={best_exp['expectancy']:.3f} pnl={best_exp['total_pnl']:.2f} maxDD={best_exp['max_dd']:.2f}")

# Maggior riduzione maxDD (vs baseline corrispondente)
def dd_reduction(r):
    base_dd = base_451['max_dd'] if r['baseline_label'] == "451(tutti)" else base_226['max_dd']
    return base_dd - r['max_dd'], base_dd

best_ddred = max(kept, key=lambda r: dd_reduction(r)[0])
red, base_dd = dd_reduction(best_ddred)
pf_s = f"{best_ddred['pf']:.3f}" if best_ddred['pf'] != CAP else "inf"
print(f"Maggior riduzione maxDD:    {best_ddred['label']:46} baseline={best_ddred['baseline_label']:>10} n={best_ddred['n']:>4} ret%={best_ddred['retention']:5.1f} WR%={best_ddred['winrate']:5.1f} PF={pf_s} exp={best_ddred['expectancy']:.3f} pnl={best_ddred['total_pnl']:.2f} maxDD={best_ddred['max_dd']:.2f} (riduzione: {red:.2f} EUR vs baseline {base_dd:.2f} EUR)")

print()
print("="*120)
print("RIFERIMENTO BASELINE")
print("="*120)
print(f"Baseline 451 (tutti):              n={base_451['n']:>4}  WR%={base_451['winrate']:5.1f}  PF={base_451['pf']:.3f}  exp={base_451['expectancy']:.3f}  pnl={base_451['total_pnl']:.2f}  maxDD={base_451['max_dd']:.2f}")
print(f"Baseline 226 (join, escl outlier):  n={base_226['n']:>4}  WR%={base_226['winrate']:5.1f}  PF={base_226['pf']:.3f}  exp={base_226['expectancy']:.3f}  pnl={base_226['total_pnl']:.2f}  maxDD={base_226['max_dd']:.2f}")
print(f"Filtro singolo noto (riferimento):  vol_h1>=10350  PF={ref_pf}  retention={ref_ret}%")
