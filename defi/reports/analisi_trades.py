import csv
from collections import defaultdict

SYSTEMS = ['defi','pre_grad','v3','pump_grad','v2','v3_large','v3_midcap']

rows = []
with open('live_trades.csv') as f:
    r = csv.DictReader(f)
    for row in r:
        if row['system'] in SYSTEMS:
            rows.append(row)

# group by signal_id
by_sig = defaultdict(list)
for row in rows:
    by_sig[row['signal_id']].append(row)

def is_closed(remaining):
    return remaining.strip() in ('0.0','0.00','0')

closed = {}  # signal_id -> (system, last_row, pnl)
open_count = defaultdict(int)
for sig, rs in by_sig.items():
    rs_sorted = sorted(rs, key=lambda x: x['ts'])
    last = rs_sorted[-1]
    sysname = last['system']
    if is_closed(last['remaining']):
        try:
            pnl = float(last['pnl_eur'])
        except:
            pnl = 0.0
        closed[sig] = (sysname, last, pnl, rs_sorted)
    else:
        open_count[sysname]+=1

# overview per system
stats = {}
all_pos_total = 0.0
all_neg_total = 0.0
for sysname in SYSTEMS:
    sig_list = [(sig,data) for sig,data in closed.items() if data[0]==sysname]
    n = len(sig_list)
    pnls = [d[2] for _,d in sig_list]
    wins = [p for p in pnls if p>0]
    losses = [p for p in pnls if p<=0]
    pf = (sum(wins)/abs(sum(losses))) if losses and sum(losses)!=0 else float('inf')
    expectancy = sum(pnls)/n if n>0 else 0
    total_pnl = sum(pnls)
    all_pos_total += sum(wins)
    all_neg_total += sum(losses)
    # max drawdown
    sig_sorted = sorted(sig_list, key=lambda x: x[1][1]['ts'])
    equity=0
    peak=0
    maxdd=0
    for _,d in sig_sorted:
        equity += d[2]
        if equity>peak:
            peak=equity
        dd = peak-equity
        if dd>maxdd:
            maxdd=dd
    stats[sysname] = dict(n=n, n_open=open_count[sysname], wins=len(wins), losses=len(losses),
                           pf=pf, expectancy=expectancy, total_pnl=total_pnl, maxdd=maxdd,
                           sum_pos=sum(wins), sum_neg=sum(losses), pnls=pnls)

for sysname in SYSTEMS:
    s = stats[sysname]
    s['wr'] = s['wins']/s['n']*100 if s['n']>0 else 0
    s['contrib_loss'] = (s['sum_neg']/all_neg_total*100) if all_neg_total!=0 else 0
    s['contrib_profit'] = (s['sum_pos']/all_pos_total*100) if all_pos_total!=0 else 0

print("=== PARTE 1: Overview per sistema (ordinato per contributo perdite desc) ===")
print(f"{'sistema':12} {'n_chiusi':8} {'n_open':6} {'WR%':6} {'PF':8} {'expect':8} {'pnl_tot':10} {'maxDD':8} {'contrib_loss%':12} {'contrib_profit%':14}")
for sysname in sorted(SYSTEMS, key=lambda x: -stats[x]['contrib_loss']):
    s = stats[sysname]
    pf_str = f"{s['pf']:.2f}" if s['pf']!=float('inf') else "inf"
    print(f"{sysname:12} {s['n']:8} {s['n_open']:6} {s['wr']:6.1f} {pf_str:8} {s['expectancy']:8.2f} {s['total_pnl']:10.2f} {s['maxdd']:8.2f} {s['contrib_loss']:12.1f} {s['contrib_profit']:14.1f}")

print()
print("=== PARTE 1b: ordinato per PF crescente (peggiore primo) ===")
print(f"{'sistema':12} {'n_chiusi':8} {'n_open':6} {'WR%':6} {'PF':8} {'expect':8} {'pnl_tot':10} {'maxDD':8}")
for sysname in sorted(SYSTEMS, key=lambda x: stats[x]['pf']):
    s = stats[sysname]
    pf_str = f"{s['pf']:.2f}" if s['pf']!=float('inf') else "inf"
    print(f"{sysname:12} {s['n']:8} {s['n_open']:6} {s['wr']:6.1f} {pf_str:8} {s['expectancy']:8.2f} {s['total_pnl']:10.2f} {s['maxdd']:8.2f}")

print()
print("totals all_pos:", all_pos_total, "all_neg:", all_neg_total)

# identify 3 worst systems: rank by contrib_loss desc and pf asc combined
rank_loss = sorted(SYSTEMS, key=lambda x: -stats[x]['contrib_loss'])
rank_pf = sorted(SYSTEMS, key=lambda x: stats[x]['pf'])
combo = {}
for sysname in SYSTEMS:
    combo[sysname] = rank_loss.index(sysname) + rank_pf.index(sysname)
worst3 = sorted(SYSTEMS, key=lambda x: combo[x])[:3]
print("\nWORST 3 (combo rank):", worst3)
print("rank_loss:", rank_loss)
print("rank_pf:", [(s, stats[s]['pf']) for s in rank_pf])

# === PARTE 2 ===
print("\n=== PARTE 2: deep dive ===")

# action distribution on losing trades
for sysname in worst3:
    print(f"\n--- {sysname} ---")
    sig_list = [(sig,data) for sig,data in closed.items() if data[0]==sysname]
    losing = [(sig,d) for sig,d in sig_list if d[2]<=0]
    print(f"n trade chiusi: {len(sig_list)}, n in perdita: {len(losing)}")
    action_pnl = defaultdict(lambda: [0,0.0])
    for sig,d in losing:
        action = d[1]['action']
        action_pnl[action][0]+=1
        action_pnl[action][1]+=d[2]
    top_actions = sorted(action_pnl.items(), key=lambda x: -x[1][0])[:3]
    print("top 3 action su loss (n, pnl_tot, pnl_medio):")
    for act,(n,tot) in top_actions:
        print(f"  {act:20} n={n:4} pnl_tot={tot:9.2f} pnl_medio={tot/n:7.2f}")

# load signals files
def load_signals(path):
    d = {}
    with open(path) as f:
        r = csv.DictReader(f)
        for row in r:
            d[row['signal_id']] = row
    return d

signals_log = load_signals('signals_log.csv')
pre_grad_signals = load_signals('pre_grad_signals.csv')
pump_grad_signals = load_signals('pump_grad_signals.csv')

sig_files = {'defi': signals_log, 'pre_grad': pre_grad_signals, 'pump_grad': pump_grad_signals}

def to_float(v, default=None):
    try:
        return float(v)
    except:
        return default

for sysname in worst3:
    if sysname not in sig_files:
        continue
    print(f"\n--- {sysname}: join con feature file ---")
    sigfile = sig_files[sysname]
    sig_list = [(sig,data) for sig,data in closed.items() if data[0]==sysname]
    matched = [(sig,d) for sig,d in sig_list if sig in sigfile]
    print(f"n matched: {len(matched)} / {len(sig_list)}")
    if not matched:
        continue
    loss_feats = defaultdict(list)
    win_feats = defaultdict(list)
    feats = ['pump_probability','buy_sell_ratio_1h','liquidity_usd','change_1h_pct']
    for sig,d in matched:
        srow = sigfile[sig]
        target = loss_feats if d[2]<=0 else win_feats
        for feat in feats:
            v = to_float(srow.get(feat,''))
            if v is not None:
                target[feat].append(v)
    print(f"n loss matched: {sum(1 for sig,d in matched if d[2]<=0)}, n win matched: {sum(1 for sig,d in matched if d[2]>0)}")
    for feat in feats:
        lv = loss_feats[feat]
        wv = win_feats[feat]
        lavg = sum(lv)/len(lv) if lv else None
        wavg = sum(wv)/len(wv) if wv else None
        print(f"  {feat:20} loss_avg={lavg} (n={len(lv)})  win_avg={wavg} (n={len(wv)})")

    # bucket analysis examples
    for feat, thresh in [('pump_probability',0.5), ('liquidity_usd',15000)]:
        buckets = defaultdict(lambda: [0,0])  # below/above -> [n_total, n_loss]
        for sig,d in matched:
            srow = sigfile[sig]
            v = to_float(srow.get(feat,''))
            if v is None: continue
            key = f"<{thresh}" if v<thresh else f">={thresh}"
            buckets[key][0]+=1
            if d[2]<=0:
                buckets[key][1]+=1
        print(f"  bucket {feat} @ {thresh}:")
        for k,(tot,loss) in buckets.items():
            pct = loss/tot*100 if tot>0 else 0
            print(f"    {k:10} n={tot:4} n_loss={loss:4} loss%={pct:5.1f}")

# v3/v3_large/v3_midcap: bsr and vol_h1 from entry row
print("\n--- v3/v3_large/v3_midcap: bsr & vol_h1 da entry (se tra worst3) ---")
for sysname in worst3:
    if sysname not in ('v3','v3_large','v3_midcap'):
        continue
    sig_list = [(sig,data) for sig,data in closed.items() if data[0]==sysname]
    loss_bsr, win_bsr, loss_vol, win_vol = [],[],[],[]
    for sig,d in sig_list:
        rs = d[3]
        entry = next((x for x in rs if x['action']=='entry'), rs[0])
        bsr = to_float(entry.get('bsr',''))
        vol = to_float(entry.get('vol_h1',''))
        target_bsr = loss_bsr if d[2]<=0 else win_bsr
        target_vol = loss_vol if d[2]<=0 else win_vol
        if bsr is not None: target_bsr.append(bsr)
        if vol is not None: target_vol.append(vol)
    print(f"{sysname}: loss bsr_avg={sum(loss_bsr)/len(loss_bsr) if loss_bsr else None:.3f} (n={len(loss_bsr)}), win bsr_avg={sum(win_bsr)/len(win_bsr) if win_bsr else None:.3f} (n={len(win_bsr)})")
    print(f"{sysname}: loss vol_avg={sum(loss_vol)/len(loss_vol) if loss_vol else None:.1f} (n={len(loss_vol)}), win vol_avg={sum(win_vol)/len(win_vol) if win_vol else None:.1f} (n={len(win_vol)})")

# filter proposals for worst3
print("\n=== Filtri proposti ===")
for sysname in worst3:
    print(f"\n--- {sysname} ---")
    sig_list = [(sig,data) for sig,data in closed.items() if data[0]==sysname]
    if sysname in sig_files:
        sigfile = sig_files[sysname]
        matched = [(sig,d) for sig,d in sig_list if sig in sigfile]
        for feat, thresholds in [('pump_probability',[0.3,0.5,0.7]), ('liquidity_usd',[10000,15000,20000]), ('buy_sell_ratio_1h',[1.0,1.2,1.5])]:
            for thresh in thresholds:
                n_loss_elim=0; eur_loss_evit=0; n_win_sacr=0; eur_profit_perso=0
                for sig,d in matched:
                    srow = sigfile[sig]
                    v = to_float(srow.get(feat,''))
                    if v is None: continue
                    if v < thresh:
                        if d[2]<=0:
                            n_loss_elim+=1; eur_loss_evit += -d[2]
                        else:
                            n_win_sacr+=1; eur_profit_perso += d[2]
                print(f"  filtro {feat}<{thresh} esclude: n_loss_elim={n_loss_elim} eur_loss_evitata={eur_loss_evit:.2f} n_win_sacr={n_win_sacr} eur_profit_perso={eur_profit_perso:.2f}")
    elif sysname in ('v3','v3_large','v3_midcap'):
        for feat, thresholds in [('bsr',[0.3,0.5,0.8]), ('vol_h1',[5000,10000,20000])]:
            for thresh in thresholds:
                n_loss_elim=0; eur_loss_evit=0; n_win_sacr=0; eur_profit_perso=0
                for sig,d in sig_list:
                    rs = d[3]
                    entry = next((x for x in rs if x['action']=='entry'), rs[0])
                    v = to_float(entry.get(feat,''))
                    if v is None: continue
                    if v < thresh:
                        if d[2]<=0:
                            n_loss_elim+=1; eur_loss_evit += -d[2]
                        else:
                            n_win_sacr+=1; eur_profit_perso += d[2]
                print(f"  filtro {feat}<{thresh} esclude: n_loss_elim={n_loss_elim} eur_loss_evitata={eur_loss_evit:.2f} n_win_sacr={n_win_sacr} eur_profit_perso={eur_profit_perso:.2f}")
