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

closed = {}  # signal_id -> dict(pnl, entry_row, last_row)
for sig, rs in by_sig.items():
    rs_sorted = sorted(rs, key=lambda x: x['ts'])
    last = rs_sorted[-1]
    first = rs_sorted[0]
    if is_closed(last['remaining']):
        try:
            pnl = float(last['pnl_eur'])
        except:
            pnl = 0.0
        closed[sig] = dict(pnl=pnl, entry=first, last=last)

print(f"# Trade defi chiusi: {len(closed)}")

# ---------- Load signals_log.csv ----------
sig_data = {}
with open('signals_log.csv') as f:
    r = csv.DictReader(f)
    for row in r:
        sig_data[row['signal_id']] = row

joined = 0
for sig in closed:
    if sig in sig_data:
        joined += 1
print(f"# Join trovati: {joined}")

# ---------- Build feature set per trade ----------
# Features from signals_log (join): change_1h_pct, buy_sell_ratio_1h, liquidity_usd, volume_1h_usd, pump_probability
# Features from top_features parse: pair_age_hours, top_10_holder_pct (and others with coverage>=100)
# Features from live_trades entry row (all 450): bsr, vol_h1

def parse_float(x):
    try:
        if x is None or x == '' or x.strip() == '':
            return None
        return float(x)
    except:
        return None

# Parse top_features for all joined signals, gather keys
top_feat_keys = defaultdict(int)
top_feat_parsed = {}  # signal_id -> dict
for sig, d in closed.items():
    sd = sig_data.get(sig)
    if not sd:
        continue
    tf = sd.get('top_features', '')
    parsed = {}
    if tf:
        for part in tf.split('|'):
            part = part.strip()
            if '=' in part:
                k, v = part.split('=', 1)
                k = k.strip()
                v = parse_float(v.strip())
                if v is not None:
                    parsed[k] = v
                    top_feat_keys[k] += 1
    top_feat_parsed[sig] = parsed

# Determine which top_features keys have coverage >= 100 in the 234-join sample
extra_tf_keys = [k for k, c in top_feat_keys.items() if c >= 100]
print(f"# top_features extra con copertura >=100: {extra_tf_keys} (coverage: {[(k,top_feat_keys[k]) for k in extra_tf_keys]})")

# Build trade records
trades = []  # each: dict pnl, win(bool), features{}
for sig, d in closed.items():
    pnl = d['pnl']
    win = pnl > 0
    feats = {}
    # from live_trades entry row (all 450)
    feats['bsr'] = parse_float(d['entry'].get('bsr'))
    feats['vol_h1'] = parse_float(d['entry'].get('vol_h1'))
    sd = sig_data.get(sig)
    if sd:
        feats['change_1h_pct'] = parse_float(sd.get('change_1h_pct'))
        feats['buy_sell_ratio_1h'] = parse_float(sd.get('buy_sell_ratio_1h'))
        feats['liquidity_usd'] = parse_float(sd.get('liquidity_usd'))
        feats['volume_1h_usd'] = parse_float(sd.get('volume_1h_usd'))
        feats['pump_probability'] = parse_float(sd.get('pump_probability'))
        for k in extra_tf_keys:
            feats[k] = top_feat_parsed.get(sig, {}).get(k)
    trades.append(dict(signal_id=sig, pnl=pnl, win=win, feats=feats))

n_total = len(trades)
print(f"# Trade totali nel campione (450 attesi): {n_total}")

# Identify ARK/USDT liquidity outliers (>1M)
outlier_sigs = set()
for t in trades:
    lq = t['feats'].get('liquidity_usd')
    if lq is not None and lq > 1_000_000:
        outlier_sigs.add(t['signal_id'])
print(f"# Outlier liquidity_usd > 1M: {len(outlier_sigs)} -> {outlier_sigs}")

# global PF/expectancy on full 450 sample
def pf_and_expectancy(pnls):
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    sum_pos = sum(wins)
    sum_neg = sum(losses)
    if sum_neg != 0:
        pf = sum_pos / abs(sum_neg)
    else:
        pf = float('inf')
    exp = sum(pnls) / len(pnls) if pnls else 0
    return pf, exp, sum_pos, sum_neg

all_pnls = [t['pnl'] for t in trades]
PF_BASE, EXP_BASE, SUMPOS_BASE, SUMNEG_BASE = pf_and_expectancy(all_pnls)
PF_BASE_CAP = PF_BASE if PF_BASE != float('inf') else 999.0
print(f"# PF base (450 trade): {PF_BASE_CAP:.4f}  expectancy: {EXP_BASE:.2f}")
print()

# ---------- List of features to analyze ----------
FEATURES_JOIN = ['change_1h_pct', 'buy_sell_ratio_1h', 'liquidity_usd', 'volume_1h_usd', 'pump_probability'] + extra_tf_keys
FEATURES_ALL = ['bsr', 'vol_h1']

def get_values(feat, exclude_outliers=False):
    vals = []
    for t in trades:
        v = t['feats'].get(feat)
        if v is None:
            continue
        if exclude_outliers and feat == 'liquidity_usd' and t['signal_id'] in outlier_sigs:
            continue
        vals.append((t['signal_id'], v, t['pnl'], t['win']))
    return vals

def median(lst):
    s = sorted(lst)
    n = len(s)
    if n == 0:
        return None
    if n % 2 == 1:
        return s[n//2]
    return (s[n//2-1] + s[n//2]) / 2

def mean(lst):
    return sum(lst)/len(lst) if lst else None

# ============================================================
# PART 1: WIN vs LOSS comparison
# ============================================================
print("="*70)
print("PARTE 1: Confronto WIN vs LOSS (media / mediana / n)")
print("="*70)
print(f"{'feature':22}{'n_win':>7}{'media_win':>12}{'med_win':>12}{'n_loss':>8}{'media_loss':>13}{'med_loss':>12}")

ALL_FEATURES = FEATURES_JOIN + FEATURES_ALL
for feat in ALL_FEATURES:
    excl = (feat == 'liquidity_usd')
    vals = get_values(feat, exclude_outliers=excl)
    win_vals = [v for _, v, p, w in vals if w]
    loss_vals = [v for _, v, p, w in vals if not w]
    if not win_vals and not loss_vals:
        continue
    mw, medw = mean(win_vals), median(win_vals)
    ml, medl = mean(loss_vals), median(loss_vals)
    mw_s = f"{mw:.3f}" if mw is not None else "n/a"
    medw_s = f"{medw:.3f}" if medw is not None else "n/a"
    ml_s = f"{ml:.3f}" if ml is not None else "n/a"
    medl_s = f"{medl:.3f}" if medl is not None else "n/a"
    print(f"{feat:22}{len(win_vals):>7}{mw_s:>12}{medw_s:>12}{len(loss_vals):>8}{ml_s:>13}{medl_s:>12}")

print()

# ============================================================
# PART 2: Quartile buckets
# ============================================================
print("="*70)
print("PARTE 2: Bucket per quartile (Q1=valori piu' bassi -> Q4=piu' alti)")
print("="*70)

def quartile_buckets(vals_with_pnl):
    # vals_with_pnl: list of (value, pnl)
    sorted_vals = sorted(vals_with_pnl, key=lambda x: x[0])
    n = len(sorted_vals)
    if n < 4:
        return None
    q = n // 4
    buckets = []
    for i in range(4):
        start = i*q
        end = (i+1)*q if i < 3 else n
        buckets.append(sorted_vals[start:end])
    return buckets

for feat in ALL_FEATURES:
    excl = (feat == 'liquidity_usd')
    vals = get_values(feat, exclude_outliers=excl)
    if len(vals) < 8:
        continue
    vp = [(v, p) for _, v, p, w in vals]
    buckets = quartile_buckets(vp)
    if buckets is None:
        continue
    print(f"\n--- {feat} (n={len(vals)}) ---")
    print(f"{'quartile':10}{'range':28}{'n':>6}{'WR%':>8}{'PF':>10}{'expect(EUR)':>14}")
    for i, b in enumerate(buckets):
        if not b:
            continue
        pnls = [p for _, p in b]
        vmin = min(v for v, _ in b)
        vmax = max(v for v, _ in b)
        n_b = len(pnls)
        wins_b = [p for p in pnls if p > 0]
        wr = len(wins_b)/n_b*100
        pf, exp, sp, sn = pf_and_expectancy(pnls)
        pf_s = f"{pf:.2f}" if pf != float('inf') else "inf"
        range_s = f"[{vmin:.3g}, {vmax:.3g}]"
        print(f"Q{i+1:<9}{range_s:28}{n_b:>6}{wr:>8.1f}{pf_s:>10}{exp:>14.2f}")

print()

# ============================================================
# PART 3: Threshold filter simulations
# ============================================================
print("="*70)
print("PARTE 3: Simulazione filtri a soglia singola")
print("="*70)

CAP = 999.0  # cap for infinite PF

def pf_cap(pf):
    return CAP if pf == float('inf') else pf

results_part3 = []  # list of dicts for ranking later

PERCENTILES = [10,20,30,40,50,60,70,80,90]

for feat in ALL_FEATURES:
    excl = (feat == 'liquidity_usd')
    vals = get_values(feat, exclude_outliers=excl)
    if len(vals) < 20:
        continue
    all_v = sorted(v for _, v, p, w in vals)
    n_v = len(all_v)
    # candidate thresholds at percentiles
    thresholds = set()
    for pct in PERCENTILES:
        idx = int(n_v * pct / 100)
        idx = min(idx, n_v-1)
        thresholds.add(all_v[idx])

    feat_best = None
    for thr in thresholds:
        for direction in ['>=', '<']:
            if direction == '>=':
                kept = [(sig, v, p, w) for sig, v, p, w in vals if v >= thr]
            else:
                kept = [(sig, v, p, w) for sig, v, p, w in vals if v < thr]
            n_kept = len(kept)
            if n_kept == 0:
                continue
            kept_pnls = [p for _, _, p, _ in kept]
            pf_new, exp_new, sp_new, sn_new = pf_and_expectancy(kept_pnls)
            pf_new_c = pf_cap(pf_new)
            pf_improvement = pf_new_c / PF_BASE_CAP if PF_BASE_CAP != 0 else 0
            retention = n_kept / n_total
            score = pf_improvement * retention
            # eliminated trades
            kept_sigs = set(sig for sig,_,_,_ in kept)
            elim = [(sig, v, p, w) for sig, v, p, w in vals if sig not in kept_sigs]
            elim_pos = sum(p for _,_,p,w in elim if p>0)
            elim_neg = sum(abs(p) for _,_,p,w in elim if p<=0)
            cand = dict(feature=feat, threshold=thr, direction=direction, n_kept=n_kept,
                         pf_new=pf_new_c, exp_new=exp_new, score=score,
                         profitto_perso=elim_pos, perdite_evitate=elim_neg)
            if feat_best is None or cand['score'] > feat_best['score']:
                feat_best = cand
            results_part3.append(cand)

    if feat_best:
        pf_s = f"{feat_best['pf_new']:.2f}" if feat_best['pf_new'] < CAP else "inf(cap)"
        print(f"\n--- {feat} ---")
        print(f"  Soglia migliore: {feat_best['direction']} {feat_best['threshold']:.4g}")
        print(f"  n_kept={feat_best['n_kept']}  PF_nuovo={pf_s}  expectancy_nuova={feat_best['exp_new']:.2f}")
        print(f"  Profitto perso (€): {feat_best['profitto_perso']:.2f}   Perdite evitate (€): {feat_best['perdite_evitate']:.2f}")
        print(f"  Score (PF_improv x retention) = {feat_best['score']:.4f}")

print()

# ============================================================
# PART 4: Final ranking
# ============================================================
print("="*70)
print("PARTE 4: Ranking finale (Top 10)")
print("="*70)

# filter candidates: exclude those eliminating >40% of 450 (n_kept < 270) and n_kept < 30
filtered = [c for c in results_part3 if c['n_kept'] >= 270 and c['n_kept'] >= 30]
filtered_sorted = sorted(filtered, key=lambda x: -x['score'])

# dedupe near-identical (same feature+direction+threshold) -> keep best score already unique per combo
top10 = filtered_sorted[:10]

print(f"{'feature':22}{'soglia':>12}{'dir':>5}{'n_rim':>7}{'PF_nuovo':>10}{'expect_nuova':>14}{'€perso':>10}{'€evitati':>10}{'score':>9}")
for c in top10:
    pf_s = f"{c['pf_new']:.2f}" if c['pf_new'] < CAP else "inf(cap)"
    print(f"{c['feature']:22}{c['threshold']:>12.4g}{c['direction']:>5}{c['n_kept']:>7}{pf_s:>10}{c['exp_new']:>14.2f}{c['profitto_perso']:>10.2f}{c['perdite_evitate']:>10.2f}{c['score']:>9.4f}")

print()
print(f"PF base campione 450: {PF_BASE_CAP:.4f}  expectancy base: {EXP_BASE:.2f}")
print(f"Tot pnl positivi base: {SUMPOS_BASE:.2f}  Tot pnl negativi base: {SUMNEG_BASE:.2f}")
