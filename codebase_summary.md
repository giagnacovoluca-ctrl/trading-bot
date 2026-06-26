# ARCHITETTURA E MAPPA DEL CODEBASE
Usa questa mappa per capire la struttura del progetto senza rileggere i file interi.
Aggiornato: 2026-06-26 sera (INJECTIVE 4 FIX + FUNDING FARMER BUG + V3_LARGE COINGECKO):

**Analisi autopilot Injective (n=74 PAPER chiusi, -55.76$):**
- INJ: 14/14 trades FE=Y → già coperti da P1 post-restart. Causa principale -54.33$.
- HOOD: n=10, WR=20%, -35.94$, FE=N → aggiunto a skip_tickers. Tokenized stock, segnali non affidabili.
- SHORT: WR=29% vs breakeven 33.3%, -55.31$ → P4: SHORT richiedono conf≥0.60 (+0.05 vs LONG).
- Confidence [0.65,0.80): WR=9%, -78.32$ → P3 già in codice (25/06). Zona "overconfidence".
- SL trades MFE medio=0.18% → entry sbagliate, non SL troppo stretto. Non allargare SL.

**Fix P4 — decision_engine.py:**
  SHORT richiedono min_confidence + 0.05 = 0.60 (vs 0.55 per LONG).
  Dato storico: LONG -0.46$ (breakeven), SHORT -55.31$ (WR 29%).

**Fix HOOD skip — config/settings.py + main.py:**
  `skip_tickers: list[str] = ["HOOD"]` in Settings.
  `decision_engine.set_skip_tickers(cfg.skip_tickers)` in main.py dopo init.
  Metodo `set_skip_tickers` aggiunto a DecisionEngine.

**Funding farmer bug fix — funding_farmer.py:**
  Bug: usava `cumulativeFunding` assoluto confrontato con soglia periodica 0.0003 → 0 segnali in 50+ cicli.
  Fix: calcola delta tra poll consecutivi, normalizza a 8h. Primo poll = baseline, dal secondo = rate reali.
  Aggiunto diagnostic log top-5 (cumulative e delta) per debug futuro.
  Variabili globali: `_prev_cumulative: dict[str, float]`, `_prev_poll_ts: float`.

**v3_large frequenza — trade_simulator.py ~riga 2007:**
  Root cause: gate CoinGecko aveva `and chain == "solana"` hardcoded. Token midcap ($10M+)
  su CoinGecko raramente hanno pool primario su Solana → 0 segnali CoinGecko in tutta la storia.
  Fix 1: rimosso `chain == "solana"` → accetta ETH/BSC. Gate: mcap>$10M+bsr≥0.60+liq≥30k+chg24≥+5%.
  Fix 2 (BinanceScan): score 65→60. `inflow=10` = placeholder hardcoded → bypass con `inflow==10`.
  Distribuzione storica v3_large: solana=85, ethereum=13, bsc=12 su 110 eventi totali.
  Restart run.py attiva i fix. Primo ciclo CoinGecko mostrerà nei log i candidati ETH/BSC.

**Filtri chiave post-26/06 sera (injective_autopilot — richiede restart run_loop.sh):**
- P1: sentinel_blocked_combos=[["FUNDING_EXTREME"]] (copre 100% INJ storico)
- P2: vol_ratio≥1.5 → skip sentinel
- P3: confidence [0.65,0.80) → skip decision_engine
- P4: SHORT min_confidence=0.60 (vs 0.55 LONG)
- HOOD: in skip_tickers

Aggiornato: 2026-06-26 (PIVOT defi+midcap+v3_large — 2 filtri implementati):

**Analisi architetturale 26/06 — pivot verso sistemi con edge reale:**
- LIQ/pump_grad: 12 trade reali Solana, -$46.91 USDC totale. entry_drop=99.9% su ~100 segnali.
  Root cause: latenza segnale→buy=35s media, rug avviene in 1-2s. Simulatore mostra +6747€
  fittizi (usa prezzi DexScreener laggati, non prezzi esecuzione reale). Nessun edge eseguibile.
- Sistemi con edge reale: defi tp1_trail (n=135, +2103€, WR 95%), midcap trailing (n=79, +292€,
  WR 94%), v3_large (PF 7.15, n=50, +231€). Questi trattano token maturi → latenza irrilevante.

**Fix 1 — defi vol_h1 cap 150k (trade_simulator.py ~2242):**
- Backtest n=605: vol 50k-150k unico bucket positivo (+238€, +1.42€/t); vol >150k: n=66, -164€
  (-2.49€/t). Token troppo liquidi = troppo maturi, upside limitato rispetto al SL -8%.
- Stima miglioramento: +165€ sul periodo storico.

**Fix 2 — midcap hh_hl=True hard gate (midcap_scanner.py ~804):**
- Backtest n=194: hh_hl=True (n=71) EV +1.48€/t tot +105€; hh_hl=False (n=123) EV -0.14€/t tot -17€.
- sl_adaptive (il killer, -542€ totale): 29/44 casi su hh_hl=False → eliminati.
- Riduce segnali/giorno da 9.8 a ~3.6 ma aumenta qualità. hh_hl = close > SMA10 > SMA20.

**Filtri chiave post-26/06:**
- defi: vol_h1 ∈ [15k, 150k] (doppio cap, nuovo) + anti-dump + prepump_composite
- midcap: hh_hl=True obbligatorio (NUOVO) + adx<=55 + score>=35

Aggiornato: 2026-06-25 (QUANT ANALYSIS + 8 FIX SU 4 SISTEMI):

**Analisi quant settimana 18-25/06 — finding principali:**
- Base LIQ simulator: 0 trade reali profittevoli. Root cause: ~20 entità deployano pool honeypot.
  Fix: blacklist 70 simboli ripetuti in base_executor.py (IMPLEMENTATO 25/06).
- Solana LIQ: TP1=25% → breakeven WR richiesto 75.7%, effettivo 70.5% → net-loss strutturale.
  Fix: tp1_pct 25%→50% → E[pnl] da -0.66€/t a +6.76€/t (shadow backtest n=266).
- Solana LIQ vol_h1 5k-15k: 555 trade, -5806€ (-10.46€/t). vol_h1=0 (pool nuovissima) = best.
  Fix: threshold alzato 5k→15k; vol_h1=0 esentato (pool brand-new).
- Midcap sl_adaptive: WR algoritmica 74.1%, pnl negativo solo per 7 loss sl_adaptive <20%.
  Fix: sl_consecutive_neg 5→8 snap, sl_threshold -12%→-20%.
- Injective (n=73 PAPER): FUNDING_EXTREME = killer (-105$, PF 0.67); senza = PF 1.18.
  Fix P1: sentinel_blocked_combos = [["FUNDING_EXTREME"]] (blocca FE da solo).
  Fix P2: vol_ratio ≥ 1.5 → skip in sentinel.py (0/6 win, -53$).
  Fix P3: confidence [0.65,0.80) → skip in decision_engine.py (WR 9%, -78$).
  Backtest post-filtri: PF 0.843→1.509, PnL -75$→+59$ (n=73→22).
- BTC Strutturale: LONG WR 8%, PF 0.27, -9.84€ → ALLOW_LONG=False in structural_bot.py.
- V3 BSR entry: bsr 0.5-0.8 = -143€ su n=12 (-11.94€/t).
  Fix: gate via_gemmeV3 bsr<0.8 → skip_routing in trade_simulator.py (riga ~2061).

**Solana executor assessment (25/06):**
- entry_drop: 119 blocchi, mediana drop 99.9%, 95% erano rug confermati. ~$1130 salvati.
- rugcheck_failed: 31 blocchi (~$310 salvati). price_impact>5%: 14 blocchi.
- Post-18/06: 235 segnali ricevuti, 0 buy eseguiti (tutti filtrati da entry_drop perché
  venivano dal bucket vol_h1 5k-15k). Con il fix vol_h1≥15k, segnali di qualità migliore
  arriveranno all'executor → dovrebbe tornare ad eseguire buy reali.
- Trade reali storici (n=4): LGBTQ(-1.54$), DABIHGAH(-1.87$), DELL(-13.75$), SOLANALIFE(+2.64$).

**Filtri chiave post-25/06 in trade_simulator.py:**
- LIQ_ Solana: liq>=25k + chg1h<=80% + (vol_h1=0 OR vol_h1>=15k) + tp1=50%
- via_gemmeV3: tier!=BRONZE + score>=50 + bsr>=0.8 (NUOVO)
- midcap: sl_consecutive_neg=8, sl_threshold=-20%

**vol_h1 >150k**: n=6 (post-18/06), dati insufficienti — non implementare soglia max.
  Rivalutare dopo 30+ trade nel bucket (stima: 4-6 settimane post-restart).

Aggiornato: 2026-06-18 pomeriggio (DRY MODE PREFLIGHT + RUGPULL DASHBOARD + COPIA ADDRESS):

**base_executor.py** — Preflight checks ora girano anche in dry mode:
  Tutti i check read-only (no_route, live_liq<$10k, reserves, honeypot sell_lock) sono stati
  spostati PRIMA del blocco `if not _is_dry()` → in dry mode il dashboard mostra `honeypot_sell_lock`,
  `live_liq<10k`, `reserves_check_failed` invece di mostrare tutto come `🔵 Dry run`.
  `_reserves_min_out` calcolato da getReserves (V2 formula) protegge anche in live.
  Honeypot sell simulation usa `acc.address` se disponibile, altrimenti indirizzo dummy.

**trade_simulator.py** — Sezione Executor vs Simulator espansa:
  - Tutti i sistemi (rimosso filtro pump_grad/mirror → ora mostra defi, v3, midcap, ecc.)
  - Filtri aggiuntivi: Sistema (bottoni per ogni sistema presente), Rugpull (🔴/✅)
  - **Colonna Rugpull**: detecta automaticamente 4 pattern:
      `honeypot (tax 100%)` → note=balance=0_post_swap
      `honeypot (sell bloccato)` → note=honeypot_sell_lock
      `sell stuck (LP rimossa?)` → outcome=stuck
      `liq_collapse senza profitto` → exit=liq_collapse + pnl<5€
  - **Bottone ⧉** copia token_address in clipboard (cambia in ✓ per 1.2s per conferma)
  - `_load_executor_map` salva anche `token_address` dal CSV executor
  - Nuovi `_WHY` mappings: balance=0_post_swap, honeypot_sell_lock, reserves_check_failed,
    live_liq<10k, via_pumpswap

**Tipi di rug identificati (Base LIQ):**
  - Tipo 1 (honeypot tax): buy ok, tokens=0 ricevuti (100% transfer fee). Bloccato da min_out>0.
  - Tipo 2 (sell_lock): buy ok, tokens ricevuti, sell reverta (contratto blocca il sell).
    Bloccato da honeypot sell simulation (eth_call prima del buy).
  - Tipo 3 (LP removal): buy ok, sell ok al momento della sim, poi deployer rimuove LP.
    NON bloccabile senza timing analysis. Visibile nel dashboard come liq_collapse senza profitto.
  - Tipo 4 (pool drenata): pool con liq>$25k rilevata, LP drenata prima del buy.
    Bloccato da live_liq<$10k check (getReserves prima dello swap).

**Stato live**: BASE_LIQ_LIVE=false (dry mode). Prima di tornare live verificare
  che i preflight in dry mode non mostrino pattern di rug nella dashboard.

Aggiornato: 2026-06-18 notte (LIQ MONITOR WS + SWAP REVERT CHECK + LATENCY FIX):

**liquidity_event_monitor.py** — WebSocket on-chain Base V2 factory (latenza ~2s):
  Aggiunto thread daemon `_start_base_ws_thread()` che sottoscrive `PairCreated` dal
  factory Uniswap V2 su Base via Alchemy WebSocket (`wss://`).
  Flusso: PairCreated → poll `getReserves` ogni 2s fino a liquidità >0 (max 30s) →
  calcola liq_usd = 2 × WETH_reserves × WETH_USD → emette segnale se >$25k.
  Parallelo al polling GeckoTerminal (che resta per Solana e come fallback Base).
  Zero API extra: 1 connessione WebSocket persistente + 3 RPC call per pool trovata.
  Limite: pool con liquidità aggiunta >30s dopo deploy → catturata da GT poll a 30s.

  Costanti: `_UNIV2_FACTORY_B`, `_PAIR_CREATED_SIG`, `_ABI_UNIV2_PAIR`, `_ABI_ERC20_SYM`
  Funzioni: `_get_weth_usd_cached()`, `_handle_pair_created()`, `_ws_base_factory()`,
            `_start_base_ws_thread()`

**base_executor.py** — receipt status check su tutti e 3 i router:
  `_swap_v3/aero/v2` ora controllano `receipt.status == 0` (TX revertita on-chain)
  → ritornano None invece di restituire il tx_hash. Prima: swap revertito veniva
  loggato come "sent" e la posizione aperta nel real_state senza token reali.
  `execute_buy`: guard `if tokens_est <= 0 after swap → log failed_onchain, return False`.
  Causa reale IMPECCABLE: swap a 08:55 (20min dopo segnale) su pool già esaurita.

**trade_simulator.py** — REFRESH_SEC 30→10s:
  Riduce latenza simulator→executor da 0-30s a 0-10s. Nessuna chiamata API aggiuntiva.

**Architettura latenza LIQ Base post-fix**:
  PairCreated WS (~2s) + simulator (0-10s) + base_executor (0-5s) = ~7s media
  vs precedente: GeckoTerminal poll (0-30s) + simulator (0-30s) + executor (0-5s) = ~32s

Aggiornato: 2026-06-18 sera (BASE EXECUTOR V2 + MAX_POS FIX + LOG CLEANUP):

**base_executor.py** — 4 fix aggiuntivi (dopo attivazione BASE_LIQ_LIVE):
  1. **Uniswap V2 su Base**: aggiunto supporto completo (IMPECCABLE/Verity/grantr/RoboCo erano
     su V2, non su V3/Aerodrome → "no_route"). Nuovi: `UNIV2_ROUTER`, `UNIV2_FACTORY`,
     `_ABI_UNIV2_ROUTER`, `_ABI_UNIV2_FACTORY`, `_swap_v2()`.
     `_find_pool` ora cerca V3 → Aerodrome → **V2** in sequenza.
     Routing buy/sell: `pool_type=="univ2"` usa V2 Router, altrimenti Aerodrome.
  2. **Quote WETH-only**: `_find_pool` cerca solo pool WETH/token (rimosso USDC da `quotes`).
     Ragione: il wallet usa sempre WETH come quote (ETH wrappato). Pool USDC non sono
     tradabili con swap diretto WETH→token. Buy: ETH→wrap→WETH→token. Sell: token→WETH.
  3. **MAX_POS conta solo posizioni LIVE**: `open_pos` filtra `entry_tx != "DRY_RUN"`.
     Prima: le vecchie posizioni DRY_RUN (Monid/RELIQT/RVT) bloccavano tutti i nuovi
     trade live perché riempivano MAX_POS=3.
  4. **`_read_sim_pnl(signal_id)`**: legge pnl_eur dall'ultima riga closed del simulatore
     per calcolare PnL realistico in DRY_RUN invece di chiudere sempre a -100%.

**trade_simulator.py** — sezione Executor vs Simulator spostata:
  Ora appare TRA "Posizioni aperte" e "Trade chiusi" (non in fondo dopo 2500+ righe).

**Semantica DEX Base**:
  I nuovi pool LIQ su Base (<5min) sono prevalentemente su Uniswap V2, NON su V3/Aerodrome.
  DexScreener: `dexId=uniswap, labels=['v2']`. Il base_executor ora li gestisce correttamente.

Aggiornato: 2026-06-18 (BASE LIQ LIVE + PUMPSWAP FALLBACK + FIX EXECUTOR + DASHBOARD):

**solana_executor.py** — PumpSwap fallback per `no_route_raydium_jupiter`:
  quando Jupiter non trova route per LIQ_*/pump_grad (pool <5min non ancora indicizzata),
  prova PumpSwap SDK on-chain come fallback. DRY_RUN: simula con `entry_price` dal segnale.
  LIVE: chiama `_pumpswap_buy(token_address, sol_amount)`. Note=`via_pumpswap` nel log.

**base_executor.py** — 5 fix:
  1. `_is_dry(signal_id)`: per-signal DRY_RUN — LIQ_* vanno live se `BASE_LIQ_LIVE=true`,
     tutto il resto segue `BASE_DRY_RUN` globale.
  2. `tp1 frac from remaining`: legge `remaining` dal CSV per sapere se vendere 50% o 100%
     (pump_grad tp1_fraction=1.0 → remaining=0 → vende tutto).
  3. `_read_sim_pnl(signal_id)`: helper che legge pnl_eur dall'ultima riga chiusa del
     simulatore → usato per calcolare PnL realistico in DRY_RUN (invece di sempre -100%).
  4. `_ensure_approval`: rimosso guard `return DRY_RUN` (dead code nei blocchi live).
  5. Banner avvio: mostra `LIQ_* = 🟢 LIVE | altri = 🔵 dry_run` invece di solo `DRY_RUN=True`.

**trade_simulator.py** — 5 fix:
  1. `exit_reason="tp1"` quando `remaining<=0` al tp1 (invece di "open" fuorviante).
     Prima causa dashboard gonfiato e analisi PnL errata (double-count +2315€).
  2. `vol_h1<5k` filter: esentato per `chain=="base"` (pool nuovissime <2min, vol=0 sempre).
  3. Skip logs pump_grad (liq<25k, vol<5k, chg>80%) abbassati da INFO→DEBUG.
  4. Nuova sezione dashboard "⚡ Executor vs Simulator": cross-reference simulatore vs
     executor CSV con filtri chain (Solana/Base) ed esito (Exec/DryRun/Skip/Stuck/NonRaggiunto).
     Funzioni: `_load_executor_map()`, `_build_executor_section()`, costante `BASE_EXEC_CSV`.
  5. `BASE_EXEC_CSV` aggiunto come costante path.

**defi_optimized.py**: `_blacklist_token` log INFO→DEBUG; `_check_followup_blacklist` aggiunge
  summary "N token blacklistati da followup" invece di stampare ogni token singolarmente.

**run.py**: `logging.getLogger("websocket").setLevel(WARNING)` — silenzia dump header HTTP
  completi su ogni 429 dal websocket-client library.

**executor/.env**: `BASE_LIQ_LIVE=true`, `BASE_TRADE_SIZE_ETH=0.002` (~$3.46, 8% capitale),
  `BASE_MAX_OPEN_POSITIONS=3`.

**codebase_summary.md**: aggiunta sezione 2b "Semantica dei Dati" con tutti gli edge case
  critici che causano analisi errate (exit_reason="open"+remaining=0, tp1_fraction, shadow
  outliers, base DRY_RUN -100%, vol_h1 Base esenzione). Leggere prima di analizzare CSV.

Aggiornato: 2026-06-17 sera-9 (FILTRO chg1h>20% → chg1h>80% su shadow backtest:
1084 shadow trade analizzati — chg1h>20% bloccava trade con PF=4.54 (WR 64%, avg +52%).
Bucket 20-50%: PF=7.22 ottimale. Soglia alzata a 80% in trade_simulator, publisher,
liq_event_monitor. Token 20-80% ora aprono trade reali (dry mode). Da rivalutare dopo
~1 settimana di dati. liq<25k e vol_h1<5k confermati corretti (WR 19% e spike thin pool).

Aggiornato: 2026-06-17 sera-8 (FIX WS 429 BACKOFF + DASHBOARD MIGLIORAMENTI:
trade_simulator _RugWatcher: rimosso reconnect= da run_forever (era fisso 10s anche su 429);
backoff manuale: _got_429 flag rilevato in _on_error/_on_close → wait 300s su 429 (5min)
invece di 10s. wallet_mirror_bot: stesso fix. gemmeV3: _MAX_RESULT_AGE_H 1.0→0.5h (Dune
re-execute ogni 30min invece di 1h per ridurre rischio dati congelati).
Dashboard sim_report.html: bottone MIRROR aggiunto al filtro Sistema; apertura ora mostra
"17/06 ore 18:12" (data+ora) invece del solo orario, su posizioni aperte e chiuse;
mirror escluso da tot_pnl nel JS (data-sys!=="mirror"); filtri temporali (7gg/30gg/Tutto)
unificati su _cutoff_ts+data-exitts come le 24h (invece di confronto stringa exitdate).
run.py: aggiunto _consume_shadow_queue() e _process_shadows() nel loop principale.
Serve restart run.py + run_bot.py.)

Aggiornato: 2026-06-17 sera-7 (MIRROR ISOLATO DA STATS PUBBLICHE + DASHBOARD:
trade_simulator: sistema "mirror" escluso da _compute_daily_pnl() (circuit breaker),
total_pnl/real_closed_all/_build_kpi_section() in _generate_html() — appare ancora nel
breakdown per-sistema per debug ma non inquina i totali. Aggiunto bottone "Ultimo mese"
(dateNDaysAgo(30)) nella dashboard accanto a "7 giorni".
track_record.py: mirror_signals set escluso da closed/total_pnl/pnl_24h/weekly recap
(stessa logica shadow_signals già esistente).
publisher.py: system=="mirror" → skip in _publish_full() (entry) e nel loop live_trades
(exit) — nessuna notifica mirror su Premium/FREE finché WR non validato.
Serve restart run.py + run_bot.py.)

Aggiornato: 2026-06-17 sera-6 (LIQ SHADOW QUEUE + DEDUP TELEGRAM PUBLISHER:
liq_event_monitor: pool $10k-$25k ora scritte in liq_shadow_queue.csv (nuovo file separato,
NON in pump_grad_signals.csv — evita rumore). Pool ≥$25k filtrate in _notify() con stessi
criteri simulator (chg>20% e 0<vol<5k → skip alert admin). Nuovo _build_signal_row() helper
condiviso tra _write_pump_grad_signal() e _write_shadow_queue().
trade_simulator: aggiunto LIQ_SHADOW_QUEUE costante + _shadow_queue_seen set + metodo
_consume_shadow_queue() che legge il file, chiama _shadow_register() per ogni entry nuova,
poi tronca lasciando solo l'header (cut & paste). run.py: aggiunto _consume_shadow_queue()
e _process_shadows() nel loop principale (REGOLA: ogni fix a LiveEngine.run() va replicato
qui — era il bug che impediva il funzionamento).
publisher.py: _pump_grad_notified dict → max 1 entry notify per token_symbol ogni 30min
(fix spam MOON×4/HermeWorld×3 pool multiple); liq_collapse rimosso da _EXIT_LABEL exit
notifications Premium (pool che muoiono in <60s non meritano alert). Serve restart
run.py + run_bot.py.)

Aggiornato: 2026-06-17 sera-5 (FIX EXECUTOR + SHADOW TRACKING + COINGECKO CACHE:
trade_simulator: _PROFILES["mirror"] separato da pump_grad (tp1_fraction=0.50, tp2_pct=300%);
effective_system="mirror" (non sovrascrive più pump_grad); shadow tracking segnali scartati →
defi/reports/pump_grad_shadow.csv (log controfattuale per liq<25k/chg1h>20%/vol_h1<5k, aggiornato
ogni ciclo DexScreener, chiude per TP1/SL/time_limit); midcap_scanner: cg_universe_cache.json
su disco TTL=4h per evitare 8 call CoinGecko ad ogni restart (era causa 80% quota a metà mese).
solana_executor: LIQ_* skip rugcheck (liq_monitor già filtra liq>$25k); "mirror" in SLIPPAGE_BPS=800bps;
base_executor: PUMP_GRAD_SIGNALS aggiunto a build_token_lookup() → LIQ Base ora eseguiti
(confermato: ORBIS/MRBASE/Juno/UNO comprati DRY_RUN post-restart).
Discrepanza executor vs simulator per token thin-liquidity: ATTESA (Jupiter routing vs DexScreener spot).
Serve restart per mirror SLIPPAGE_BPS.)

Aggiornato: 2026-06-17 sera-4 (INTEGRAZIONE scanner con engine:
liq_monitor: pool <5min liq>$25k → scrive DIRETTAMENTE in pump_grad_signals.csv
(bypass Dune lag ore; LiveEngine processa con config pump_grad TP1+25%/SL-12%/hold1h);
liq>$10k → solo Telegram alert. top_features: liq_monitor=true|pair_age_min|vol_h1|chg_1h.
cex_listing_watcher: get_cex_boost(ticker) pubblica + _inject_cex_boost() → data/cex_listings.json
(TTL 24h). midcap_scanner: step 11 CEX listing boost +15pt se ticker in cex_listings.json.
RIMOSSA integrazione fastpoll inutile (pool <5min non passano vol_h1>=15k né prepump>=0.55).
Notifiche: liq_monitor+cex_watcher → Telegram admin diretto via tg_alert.py (nuovo helper,
legge bot_telegram/.env; bypass email digest batch 8/14/20). Serve restart run.py.)

Aggiornato: 2026-06-17 sera-3 (NUOVI SCRIPT AUTONOMI: defi/liquidity_event_monitor.py
(GeckoTerminal new pools Solana/Base ogni 30s, liq>$10k, età<5min → Telegram+CSV),
defi/cex_listing_watcher.py (Binance announcements + Coinbase products ogni 2min → DexScreener
on-chain match → Telegram+CSV; _bootstrap() silenzioso al 1° avvio), injective_autopilot/funding_farmer.py
(ogni 4h: funding_rate>0.03%/8h → segnala SHORT/LONG + email + CSV, avviato da run_loop.sh in
background con restart 30s). trade_simulator: exit_vol_crash predittivo — buffer _vol_hist 4
campioni, se slope negativa accelerata ≥15% per 2 cicli → exit anticipato prima della soglia
assoluta. run.py: flag --no-liq e --no-cex aggiunti. run_loop.sh: avvia funding_farmer in
background. bot_telegram/formatter.py: fix _EXIT_LABEL — aggiunte 6 action prodotte dal
simulator ma non mappate (exit_adaptive, exit_momentum, exit_stagnant, exit_price_timeout,
exit_max_age, manual_close). defi/tg_alert.py: helper invio Telegram diretto (legge
bot_telegram/.env, bypass digest).)

Aggiornato: 2026-06-17 sera-2 (trade_simulator: LIVE_COLUMNS estese a 17 colonne (+pump_prob,
+prepump_score dopo bsr); BSR entry ora reale da buy_sell_ratio_1h segnale (era hardcoded 1.000);
nuova funzione _log_to_signals_csv() scrive riga in signals_log.csv per segnali v3→defi via
via_gemmeV3 (chiude gap copertura 23% vs 74%); live_trades.csv migrato a 17 colonne con swap
atomico; backup in live_trades.csv.bak_colmig. Serve restart run.py)

Aggiornato: 2026-06-17 sera (social_monitor.py: fix Telethon non riceveva update da
canali — causa: while+asyncio.sleep non sufficiente come driver; fix: run_until_disconnected()
+ asyncio.ensure_future per loop periodico; rimosso filtro chat_id manuale buggy per
ID >10 cifre; get_dialogs() post-start per sync pts sessione; ora social_events.jsonl
si popola correttamente)

Aggiornato: 2026-06-17 (trade_simulator: gate via_gemmeV3 score 35→50, backtest n=195
taglia 26 trade WR=22.7% PnL=-84€; pump_grad filtro vol_h1 1-5k, backtest n=26
WR=19% PnL=-206€; _build_kpi_section(hours=24) aggiunta a sim_report.html: griglia
3 colonne con per-sistema/exit-reason/vol_h1-bucket/BSR-bucket ultime 24h;
standalone defi/kpi_daily.py con --hours N / --all; serve restart run.py)

Aggiornato: 2026-06-16 sera-2 (injective_autopilot: zscore_entry_threshold 2.0→1.7,
vol_breakout_sigma 1.7 (nuovo param, era hardcoded 2.0 in anomaly.py), REGIME_SHIFT
contribuisce votes±1 (BULLISH→long, BEARISH→short, NEUTRAL=0 invariato);
cluster no-FUNDING n≥3 PF=2.573, conversion rate 2.5% (360 trigger/7gg → 9 trade);
serve restart injective_autopilot/main.py)

Aggiornato: 2026-06-16 sera (filtri pump_grad/mirror in trade_simulator: skip entry
se liq<$25k o chg1h>20% (riga ~1843); run.py: backup automatico live_trades.csv ad
ogni avvio → live_trades_backup_YYYYMMDD_HHMMSS.csv, ultimi 5 mantenuti; ⚠️ NOTA
SICUREZZA: non aprire mai CSV append-only in open('w') senza tmp+os.rename atomico)

Aggiornato: 2026-06-16 (nuovo defi/social_monitor.py: daemon Telethon 12 canali,
social_velocity.json rolling 1h/4h/24h, spike alert → Saved Messages; backtest RVol
midcap n=104: vol_ratio<1.0=WR 77.8% implementato ±5pt, adx>55 ora hard reject;
requirements.txt: aggiunto telethon>=1.36.0)

Aggiornato: 2026-06-14 (nuovo defi/token_outcome_logger.py: dataset multi-timeframe
T0/+15m/+1h/+4h/+24h/+72h per ogni segnale anche scartato, thread in run.py;
wallet_alpha_finder.py: seed esteso a pre_grad/defi/v3/v3_large, fix
unique_tokens_traded/days_since_last_trade=999 sempre per tutti i wallet
[matchava toUserAccount==wallet su ATA invece di feePayer])

---

## 1. Albero dei File
```
./
    requirements.txt
    CLAUDE.md
    codebase_summary.md
    .gitignore           ← NUOVO 04/06: ignora .env, state JSON, log CSV/HTML, *.joblib, venv, bot_telegram/state
    gemme/
        gemmeV2.py          ← scanner legacy (NON attivo, non avviato da run.py)
        gemmeV3.py          ← scanner principale multi-chain (Solana + Base)
        gem_tracker.py      ← traccia snapshot prezzi post-segnale (followup)
        setup_dune.py       ← aggiorna query Dune Analytics (v4, UNICO da usare)
        gem_watchlist.json
        gem_model.joblib / gem_scaler.joblib
    defi/
        run.py              ← orchestratore: avvia tutti i componenti in thread daemon;
                               ad ogni avvio crea live_trades_backup_YYYYMMDD_HHMMSS.csv
                               (ultimi 5 mantenuti) prima di istanziare LiveEngine
        data_quality.py     ← NUOVO 15/06: single source of truth MIN_VOLUME_1H_USD_DEFI
                               (15_000) + is_valid_trade_event() per classificazione data_fault
        defi_optimized.py   ← gem hunter defi (Solana + Base, memecoins on-chain)
        trade_simulator.py  ← LiveEngine: gestione posizioni, exit, HTML reports
        midcap_scanner.py   ← scanner mid/large cap con BB Squeeze (NUOVO)
        pump_graduation_scanner.py ← token appena graduati da pump.fun → Raydium (Solana)
        base_pump_scanner.py       ← token nuovi su Base: polling Uniswap V3 + Aerodrome factory
                               FIX 10/06: RPC giù all'avvio uccideva il poll thread per sempre
                               (return) → retry con backoff 15s→5min; start() non fa più bail su RPC
        pre_grad_monitor.py ← intercetta token PRE-graduation sulla bonding curve
        token_outcome_logger.py ← NUOVO 14/06: dataset multi-timeframe (T0/+15m/+1h/+4h/+24h/+72h
                               ret%) per OGNI segnale (signals_log/pre_grad/mirror/pump_grad/
                               base_pump/gemme), anche quelli scartati (skip_stale ecc.).
                               Bootstrap su 1° run marca i segnali storici come "seen" senza
                               trackarli. Thread in run.py, poll 5min, no nuove dipendenze.
        liquidity_event_monitor.py ← NUOVO 17/06: polling GeckoTerminal /networks/{chain}/new_pools
                               ogni 30s su Solana e Base. Pool nuovi <5min:
                               liq>$25k → _write_pump_grad_signal() → pump_grad_signals.csv → trade reale
                               liq $10k-$25k → _write_shadow_queue() → liq_shadow_queue.csv → shadow only
                               _notify() (admin): filtra chg>20% e 0<vol<5k (stessi criteri simulator)
                               Log storico: reports/liq_event_signals.csv. Flag --no-liq. _seen TTL 1h.
                               AGGIORNATO 17/06 sera: _build_signal_row() helper condiviso; shadow queue
                               separata da pump_grad_signals.csv (era inquinato); _notify filtra spam.
        cex_listing_watcher.py ← NUOVO 17/06: polling Binance announcements + Coinbase products
                               ogni 2 min. Ticker nuovi cercati su DexScreener (solana/base,
                               liq>$5k) → Telegram alert immediato + reports/cex_listing_signals.csv
                               + data/cex_listings.json (TTL 24h). _bootstrap() silenzioso al 1°
                               avvio (evita flood 190 token pre-esistenti Coinbase). Flag --no-cex.
                               get_cex_boost(ticker): pubblica → +15pt score midcap_scanner.
        social_monitor.py   ← NUOVO 16/06: daemon Telethon su 12 canali Telegram crypto
                               (whale_alert, lookonchain, cryptoquant_official, Unfolded,
                               CoinTelegraph, rektHQ, EveningTrader, solanadailynews,
                               wublockgroup, wublockchainenglish, DeFimillionfreesignals,
                               cryptoninjaclub). Ogni 2min: rolling velocity 1h/4h/24h per
                               ticker → reports/social_velocity.json; spike alert (m1h≥5,
                               ratio≥2.5×) → Saved Messages Telegram (max 8/h, cooldown 4h).
                               get_social_score() usato da midcap_scanner (+5/+3/-3pt).
                               Setup: TELEGRAM_API_ID+HASH in executor/.env, session via --auth.
                               Avviato da run.py (--no-social per skippare).
        signal_tracker.py   ← snapshot prezzi per segnali defi (followup)
        gem_watchlist.py    ← watchlist condivisa tra scanner
        rugcheck.py         ← wrapper RugCheck.xyz (LP lock, top holder check)
        binance_futures_scanner.py ← scansiona Binance Futures per segnali large-cap
        setup_dune.py       ← DEPRECATO (raise SystemExit, non eseguire)
        reports/
            signals_log.csv         ← segnali defi_optimized + (da 17/06) segnali v3→defi
                                  via_gemmeV3 scritti da _log_to_signals_csv() in trade_simulator.
                                  Colonne: signal_id, timestamp_entry, token_symbol, token_name,
                                  token_address, chain, pair_address, price_entry_usd, volume_1h_usd,
                                  liquidity_usd, buy_sell_ratio_1h, change_1h_pct, pump_probability,
                                  buy_tax, sell_tax, lp_locked, is_honeypot, top_features.
                                  ⚠️ pump_probability vuota per righe via_gemmeV3; top_features
                                  contiene tier/score/gem_class per le righe v3
            midcap_signals.csv      ← segnali midcap_scanner (NUOVO)
            pump_grad_signals.csv   ← segnali pump_graduation_scanner + pool LIQ ≥$25k (liq_monitor)
            liq_shadow_queue.csv    ← NUOVO 17/06: pool $10k-$25k da liq_monitor, consumato e
                                  troncato (header mantenuto) da _consume_shadow_queue() ogni ciclo;
                                  entries registrate in LiveEngine._shadows → pump_grad_shadow.csv
            liq_event_signals.csv   ← log storico tutte le pool rilevate da liq_monitor (signal_sent=0/1)
            pre_grad_signals.csv    ← segnali pre_grad_monitor
            mirror_signals.csv      ← segnali wallet_mirror_bot
            live_state.json         ← stato posizioni LiveEngine (persistente)
            live_trades.csv         ← log completo azioni LiveEngine (append-only;
                                  ⚠️ aprire sempre in modalità 'a' o con tmp+os.rename)
                                  COLONNE (17): ts, signal_id, system, token_symbol, chain,
                                  pair_address, action, price, change_pct, vol_h1, bsr,
                                  pump_prob, prepump_score, remaining, pnl_eur, exit_reason, note.
                                  pump_prob e prepump_score valorizzati solo per entry post-17/06/2026;
                                  bsr ora reale (da buy_sell_ratio_1h segnale, non più 1.000 fisso)
            live_trades_backup_*.csv ← backup datati creati da run.py ad ogni avvio
            data_fault_trades.csv   ← NUOVO 15/06: trade chiusi NON validi (is_valid_trade_event
                                  ko, es. missing_entry_event) — esclusi da PF/WR/EV, audit-only
            sim_report.html         ← dashboard trade live (auto-refresh 60s)
            exit_quality.html       ← analisi qualità exit BSR/vol (auto-refresh 10min);
                                  mostra change_pct REALE all'uscita hard_sl (include overshoot
                                  da polling 30s: threshold -8% → exit effettiva media -15/-18%)
            price_followup.csv      ← followup prezzi defi: snapshot ogni 30min dal momento del
                                  SEGNALE (non dall'entry del simulator). Colonne: signal_id,
                                  token_symbol, chain, pair_address, price_entry_usd, snapshot_num,
                                  timestamp_snapshot, minutes_since_entry, price_snapshot_usd,
                                  change_pct, status. Coverage: solo native defi via signals_log.csv
                                  (NON via_gemmeV3). ~714 signal_id, mediana 12 snap/signal.
                                  ⚠️ change_pct misurato dal prezzo SEGNALE, non dall'entry price
                                  del simulator → non mappabile direttamente su hard_sl threshold.
            token_outcomes.csv      ← NUOVO 14/06: output token_outcome_logger (ret%
                                  +15m/+1h/+4h/+24h/+72h per signal_id, status complete/partial)
            token_outcome_state.json ← stato pending/seen di token_outcome_logger
            cycle_stats.csv         ← diagnostico per ciclo: ts, chain, n_raw, n_hard_pass,
                                  n_signals, bsr_med, vol_med, chg_med (distinguere calo API vs filtri)
            # === Script di analisi (eseguire da defi/reports/, usano CWD relativo) ===
            analisi_trades.py           ← PF/WR/EV per tutti i sistemi su live_trades.csv
            analisi_entry_exit_defi.py  ← analisi entry/exit defi: hold time, BSR, vol distribuzioni
            analisi_edge_multivariati_defi.py ← combinazioni multivariata di feature (vol×BSR×chain)
            rootcause_vol_h1_defi_15k.py ← rootcause analysis filtro vol_h1 15k (15/06)
            simula_filtri_defi.py       ← simulazione impatto filtri alternativi su PF/WR
            validation_post_fix_15_06.py ← validazione PF/WR post data_fault layer (15/06)
    executor/
        solana_executor.py  ← esegue swap reali su Solana via Jupiter + RPC
        base_executor.py    ← executor Base chain (oracle on-chain Uniswap V3+Aerodrome+Chainlink)
        bsc_executor.py     ← executor BSC/PancakeSwap (BSC disabilitato)
        executor_report.py  ← report HTML esecuzioni reali vs simulator
        wallet_alpha_finder.py ← analizza wallet compratori early per alpha
                               AGGIORNATO 10/06: seed win-only (join live_trades.csv su signal_id), seed extra
                               da live_trades (mint via DexScreener — pump_grad_signals.csv viene ruotato),
                               paginazione firme con `before` fino a signal_ts (prima solo ultime 200 = mai
                               i veri early), buy in SOL nativo/wSOL valorizzati (prima solo USDC),
                               dedup (wallet,mint) nel rank, penalità avg_rank>300 ×0.5 / >100 ×0.8 (anti bot
                               spray), enrich sui top per score preliminare, fix dead code penalità >60gg,
                               flag --all-seeds
        wallet_mirror_bot.py   ← mirror trade da alpha wallet
                               RISCRITTO 10/06: transactionSubscribe Atlas (403 piano attuale) →
                               logsSubscribe standard per wallet + fetch Enhanced API on-trigger
                               (stesso pattern _RugWatcher); dedup mint con TTL 6h (era set permanente);
                               buy E sell rilevati (anche pagati in SOL); main(stop_event) → avviato da run.py
                               NUOVO: wallet_events.csv (storico completo buy/sell anche scartati),
                               confluenza cross-wallet (≥2 alpha stesso mint in 6h → pump_probability
                               0.80+0.05/wallet cap 0.95 + log 🔥), alert "smart money in uscita" se un
                               alpha vende un token segnalato di recente, risveglio post-inattività ≥30gg
                               (mirror_state.json persiste last_seen per wallet)
                               FIX 12/06: _get_fee_payer() chiamava getTransaction senza commitment
                               (default "finalized") → result=None per le firme appena notificate da
                               logsSubscribe ("confirmed"), ogni evento scartato in silenzio → 0 alert
                               whale dal 10/06. Aggiunto "commitment":"confirmed" alla request.
        mirror_state.json   ← stato mirror bot (wallet → last_seen_ts)
        alpha_wallets.json  ← GENERATO 10/06 (prima non esisteva: sistema mirror mai stato attivo)
        real_state.json     ← stato posizioni reali Solana
        base_real_state.json← stato posizioni reali Base
        real_executions.csv ← log swap reali Solana
        base_executions.csv ← log swap reali Base
        alpha_wallets.json  ← wallet alpha per mirroring
        .env                ← chiavi private (SOLANA_PRIVATE_KEY, HELIUS_API_KEY, BASE_PRIVATE_KEY, EXECUTOR_CHAINS) + SMTP/CoinGecko/CMC (migrati dal sorgente 04/06)
    bot_telegram/           ← Signal SaaS Telegram (read-only sui CSV, processo isolato — NUOVO 04/06)
        config.py           ← env (riusa executor/.env per RPC); mappa SIGNAL_FILES→sistema; routing canali
                               NUOVO: BOT_USERNAME, FREE_CHANNEL_USERNAME, PREMIUM_CHANNEL_USERNAME
                               NUOVO 12/06: blocco "Promo X" — X_PROMO_ENABLED/MIN_PNL_EUR/MIN_PNL_PCT/
                               MIN_INTERVAL_MIN/MAX_PER_DAY (nessuna credenziale: vedi x_poster.py)
        csv_tail.py         ← tail incrementale CSV, offset-byte persistente (anti-repost, skip backlog)
        formatter.py        ← messaggi HTML: format_full (Premium) / format_teaser (Free)
                               AGGIORNATO: format_teaser ora mostra entry price storico con label "⏱ Entry al segnale (15m fa)"
                               NUOVO 12/06: format_teaser_live (teaser censurato ████ per canale FREE) +
                               premium_keyboard() (bottone CTA Premium riusato da closure/teaser)
        telegram_api.py     ← Bot API via requests, retry 429
                               NUOVO 12/06: edit_message_text (navigazione inline bot.py),
                               send_photo (multipart, per x_poster.py)
        publisher.py        ← daemon: full su Premium/VIP, teaser FREE ritardato 15m
                               NUOVO 10/06: tail wallet_events.csv → alert whale su PREMIUM
                               (buy ≥ WHALE_ALERT_MIN_USD=500 | confluence ≥2 | risveglio;
                               sell solo se sell_after_signal). formatter.format_wallet_event()
                               NUOVO 12/06: _maybe_publish_teaser() → teaser live censurato su FREE dopo
                               ogni full Premium (rate limit FREE_TEASER_MIN_INTERVAL_MIN=45/
                               FREE_TEASER_MAX_PER_DAY=6, filtro FREE_MIN_PROBABILITY); su chiusura vincente
                               chiama x_poster.maybe_send_x_draft(closure); skip righe "shadow=true" (pre_grad shadow)
                               AGGIORNATO 17/06 sera: _pump_grad_notified dict → max 1 entry notify per
                               token_symbol ogni 30min per pump_grad (fix spam pool multiple stesso token);
                               liq_collapse escluso da exit notifications Premium (troppo rumoroso)
        x_poster.py         ← NUOVO 12/06: per ogni chiusura vincente sopra soglia (X_PROMO_MIN_PNL_EUR/PCT,
                               rate-limited via state/x_promo.json) genera card PNG (Pillow, dark theme
                               1200x675) + testo pronto con $TICKER/hashtag/CTA canale FREE, e li manda
                               all'admin (ADMIN_CHAT_ID) via send_photo per post manuale su X.
                               (L'API X richiede piano Basic $200/mese per scrivere: niente auto-post.)
        bot.py              ← comandi /start /plans /subscribe /status /referral + admin /grant /stats /broadcast
                               RISCRITTO 12/06: navigazione a pulsanti inline (menu/pay/stats/back) via
                               edit_message_text; callback router menu:* , pay:premium:<chain> (crea invoice
                               via payments.create_invoice, ritorna anche `ref`), chk:<ref> (bottone "I've
                               paid" → payments.get_invoice(ref)); /subscribe legacy (sub:premium) ancora
                               routato per compatibilità coi vecchi messaggi
        subscriptions.py    ← store abbonati (tier/scadenza/referral) in state/subscribers.json
        payments.py         ← verifica USDC on-chain (Base+Solana) via invoice a importo univoco; settle→grant
                               NUOVO 12/06: get_invoice(ref) per il bottone "I've paid" di bot.py
        track_record.py     ← P&L da live_trades.csv → recap FREE + state/stats.json (landing)
                               AGGIORNATO: post_recap() chiama landing.generate() dopo ogni ciclo daily
                               NUOVO 12/06: compute() produce anche stats["weekly"] (finestra 7gg:
                               trades/wins/losses/WR/pnl/best/by_system); weekly_recap_text() +
                               post_weekly_recap() agganciato a post_recap(), throttle ≥6.5gg via
                               state/weekly_recap.json. Fix ticker "$$SYM" → lstrip("$"). Recap Telegram
                               ridisegnato a colonne (best/worst rimossi dal testo, restano in stats.json)
        landing.py          ← NUOVO: genera landing/index.html statico con stats baked-in (dark theme, WR%/P&L/by_system/best_worst/CTA)
                               Se LANDING_PAGES_REPO_PATH impostato: auto-commit+push su repo GitHub Pages dopo ogni rigenerazione
        run_bot.py          ← orchestratore thread daemon + gating scadenze (--no-publisher/bot/payments/track)
        store.py            ← persistenza JSON atomica
        landing/            ← output HTML generato (gitignored nel repo principale, pushato su repo separato)
        state/              ← offsets, subscribers, invoices, stats, x_promo.json, weekly_recap.json, ecc. (gitignored)
        .env / .env.example ← TELEGRAM_BOT_TOKEN, channel id, PAY_WALLET_*, prezzi tier
                               NUOVO: LANDING_PAGES_REPO_PATH, TELEGRAM_BOT_USERNAME, TELEGRAM_FREE/PREMIUM_CHANNEL_USERNAME
                               ⚠️ PAY_WALLET_SOL/PAY_WALLET_EVM ancora VUOTI (12/06): servono wallet di
                               incasso DEDICATI (non riusare wallet executor — match invoice solo per importo)
    trade/
        structural_bot.py          ← bot strutturale BTC (multi-timeframe EMA/RSI/ADX + S/R + bounce)
                                      NUOVO 09/06: lock file in run() — impedisce doppia istanza (fcntl LOCK_EX)
        run_demo.py                ← launcher aggressivo su Bitget Demo (SUSDT-FUTURES, $1000, 15% risk)
        bitget_futures_executor.py ← executor Bitget Futures (live USDT-FUTURES + demo SUSDT-FUTURES)
        bybit_futures_executor.py  ← executor Bybit Futures
        binance_futures_executor.py← executor Binance Futures
        binance_spot_executor.py   ← executor Binance Spot
        reports bot strutturale/   ← stato e log bot live (state.json, trades_log.json, structural_bot.log)
        reports_demo/              ← stato e log bot demo (separato)
    injective_autopilot/           ← bot multi-market su Injective Protocol perps
        funding_farmer.py          ← NUOVO 17/06: monitora funding rates ogni 4h su tutti i
                                      market Injective. Se |funding_rate| > 0.03%/8h → segnala
                                      SHORT (fr>0) o LONG (fr<0), APY stimato, rating ★★/★★★.
                                      Output: reports/funding_opportunities.csv + email SMTP.
                                      Avviato da run_loop.sh in background con loop restart 30s.
                                      NO trading autonomo — solo segnali.
        main.py                    ← entry point (PAPER/LIVE/BACKTEST mode)
                                      AGGIORNATO 09/06: Ctrl+C doppio con timeout 5s (1°=stop&salva, 2°=chiudi posizioni)
                                      set_risk_engine(engine._risk) wired al dashboard
                                      uvicorn install_signal_handlers=lambda: None (no override SIGINT)
        config/settings.py         ← config Pydantic (INJ_ env prefix); claude_timeout=90s, rate_limit=20/h
                                      AGGIORNATO 09/06: paper_max_daily_drawdown_pct=0.15 (live rimane 0.05)
                                      NUOVO 12/06: recheck_after_min=90, recheck_min_overlap=1 (recheck tesi
                                      di trade, NO max-hold fisso — vedi paper_trading/engine.py)
                                      AGGIORNATO 16/06: zscore_entry_threshold 2.0→1.7; vol_breakout_sigma=1.7
                                      (nuovo campo — era 2.0 hardcoded in anomaly.py)
        core/sentinel.py           ← scansiona 29 market ogni 60s, trigger composito (≥2 segnali Tier A/B o 1 Tier S)
                                      NUOVO 12/06: MarketContext.last_signal_types (set dei "tipi" di segnale
                                      attivi nel tick, es. CVD_DIV/ZSCORE) + Sentinel.get_signal_overlap(
                                      market_id, original_signals) → quanti segnali originali sono ancora attivi
                                      AGGIORNATO 16/06: REGIME_SHIFT(BULLISH_SHIFT)→votes_long+1,
                                      REGIME_SHIFT(BEARISH_SHIFT)→votes_short+1, NEUTRAL=0 (prima sempre 0);
                                      AnomalyDetector riceve vol_breakout_sigma=cfg.vol_breakout_sigma
        core/decision_engine.py    ← chiama claude CLI (--bare --print --max-turns 1) per decisione finale JSON
                                      AGGIORNATO 09/06: --bare + stdin=DEVNULL + timeout 45→90s
        core/risk_engine.py        ← risk management (max DD, margin, position sizing)
                                      AGGIORNATO 09/06: check_kill_switch usa paper_max_daily_drawdown_pct se mode=PAPER/BACKTEST
                                      reset_kill_switch() disponibile, usata da /admin/reset-kill-switch
        core/executor.py           ← esecuzione ordini su Injective
                                      NUOVO 12/06: close_trade(trade, exit_price, reason, funding_rate) —
                                      wrapper pubblico su _close_trade, per chiusure manuali (recheck tesi)
        paper_trading/engine.py    ← paper trading engine (PAPER mode default)
                                      BUG FIX 09/06: max_open_positions mai enforced; fetch_positions() restituisce posizioni
                                      on-chain (vuote in PAPER) → ora usa self._executor.open_trades
                                      NUOVO 12/06: _recheck_open_positions() in _monitoring_loop — dopo
                                      recheck_after_min (90min) dall'apertura, se overlap tra segnali
                                      originali e segnali attivi ora sul market < recheck_min_overlap (1),
                                      chiude a mercato con reason="SIGNALS_GONE" (no max-hold fisso, si esce
                                      solo se la tesi di trade non sussiste più)
        signals/                   ← orderbook.py, volume.py, derivatives.py, volatility.py, anomaly.py
        data/injective_client.py   ← client gRPC/REST Injective mainnet
        data/cache.py              ← buffer rolling (prezzi, funding, OI)
        backtest/engine.py         ← backtest walk-forward + live gate (500 trades, PF>1.5, Sharpe>1.5)
        dashboard/app.py           ← FastAPI dashboard http://127.0.0.1:8080 (auto-refresh 10s)
                                      AGGIORNATO 09/06: set_risk_engine(engine); GET /admin/kill-switch-status;
                                      POST /admin/reset-kill-switch (reset DD baseline, log DB, redirect /risk);
                                      /risk passa dd_limit_pct (15% paper, 5% live) al template
        dashboard/templates/risk.html ← AGGIORNATO 09/06: pulsante Reset Kill Switch (form POST) quando kill attivo;
                                      DD Limit dinamico (dd_limit_pct dal backend); traffic light usa soglia corretta
        dashboard/templates/journal.html ← AGGIORNATO 09/06: colonna Token (ticker+market_id[:12]) aggiunta
        dashboard/templates/signals.html ← AGGIORNATO 09/06: colonna Market aggiunta; fix OBI/Fund Z/Vol
                                      (guard s.values and s.values.obi is not none — Jinja2 `is defined` fallisce su None)
        database/repository.py     ← SQLite async (aiosqlite)
                                      AGGIORNATO 09/06: get_signals include market_id; signal_values or {} (no None)
```

---

## 2. Chain & Sistema attivi

| Chain   | Scanner          | Simulator | Executor reale |
|---------|------------------|-----------|----------------|
| Solana  | defi_optimized + gemmeV3 + liq_monitor | ✅ | 🔵 dry_run (tutte le strategie) |
| Base    | defi_optimized + gemmeV3 + liq_monitor | ✅ | ✅ **LIQ_* LIVE** (BASE_LIQ_LIVE=true), altri dry_run |
| BSC     | disabilitato     | log storico | bsc_executor (inattivo) |
| ETH     | disabilitato     | log storico | — |

**EXECUTOR_CHAINS** in executor/.env: `base,solana`
**BASE_LIQ_LIVE=true** → solo segnali `LIQ_*` su Base vanno live, tutto il resto dry_run.
**BASE_TRADE_SIZE_ETH=0.002** (~$3.46 a ~$1728/ETH), **BASE_MAX_OPEN_POSITIONS=3**
**TRADE_SIZE_USDC=5** (Solana, pump_grad dry_run)

**ALLOWED_CHAINS** in trade_simulator: `{"solana", "base"}`

---

## 2b. Semantica dei Dati — Leggere PRIMA di analizzare qualsiasi CSV

> **Regola**: questi edge case non sono ovvi dal nome delle colonne. Ignorarli produce analisi completamente sbagliate (es. -1071€ calcolato come +394€ reale).

### `live_trades.csv` — colonne critiche

| Colonna | Valore | Significato reale |
|---|---|---|
| `exit_reason` | `"open"` + `remaining=0` | **Trade CHIUSO al TP1 (100%)** — NON posizione aperta. Tipico di pump_grad/pre_grad/mirror con `tp1_fraction=1.0`. Il campo è scritto sempre "open" al tp1, ma se `remaining=0` il trade è finito. |
| `exit_reason` | `"open"` + `remaining=0.5` | Trade parzialmente chiuso (tp1 50%), **ancora in trailing**. Corretto come "aperto". |
| `pnl_eur` (righe `tp1` con remaining>0) | es. `+15.00` | PnL **parziale** (solo la metà chiusa). NON è il pnl finale del trade. Il pnl finale è nella riga `trail_exit`/`hard_sl` successiva. |
| `pnl_eur` (riga finale, remaining=0) | es. `+23.48` | PnL **totale** del trade, comprensivo del tp1 parziale. Usare SOLO questa riga. |
| `remaining` | `0.0` | Posizione chiusa al 100% |
| `remaining` | `0.5` | 50% venduto (tp1 parziale), 50% ancora aperto |
| `action=skip_stale` | pnl=0 | Segnale mai entrato (prezzo già sceso pre-entry). **NON è una loss.** Escludere da WR/PnL. |

**Come calcolare PnL corretto per signal_id:**
```python
# CORRETTO: ultima riga con remaining <= 0
closed = [r for r in by_sid[sid] if float(r.get('remaining','1') or 1) <= 0.001]
# SBAGLIATO: somma di tutte le righe → double-count del tp1 (+2315€ inflazione su pump_grad)
```

### `tp1_fraction` per sistema — comportamento TP1

| Sistema | `tp1_fraction` | Comportamento al TP1 |
|---|---|---|
| `pump_grad`, `pre_grad`, `mirror`, `base_pump` | **1.00** | Vende **100%** al TP1. `remaining=0`, `exit_reason="open"` (edge case). Trade completamente chiuso dopo tp1. |
| `defi`, `v3`, `v3_large`, `v3_midcap`, `midcap` | **0.50** | Vende 50% al TP1, il 50% resta in trailing. `remaining=0.5`, `exit_reason="open"`. Attende trail_exit/hard_sl. |

### `pump_grad_shadow.csv` — shadow tracking filtri

Righe scritte per ogni segnale **scartato** dai filtri (liq<25k, vol_h1<5k, chg1h>80%, liq_queue). Serve per analisi controfattuale.

- `exit_pct` di righe `liq_queue` con valori >10000% → **artefatti** (oracle price anomalo su pool nascente). Cappare a 500% per analisi pulita.
- `duration_min < 1` per la maggior parte delle righe: shadow risolto al primo fetch dopo la registrazione.
- WR/PF calcolati sulle shadow **NON sono performance reali** — i token erano troppo nuovi per avere dati affidabili.

### `base_executions.csv` — DRY_RUN

- `tokens_amount=0.000000` in tutte le righe DRY_RUN → **artefatto**: in dry_run non eseguiamo lo swap, quindi non leggiamo il balance reale.
- `real_pnl_usdc=-99.9998` nel `base_real_state.json` per DRY_RUN → **falso negativo**: il sell fires con `tokens_held=0` → chiude a -100%. In LIVE mode il saldo viene letto on-chain e il pnl è corretto.
- Per valutare la performance Base LIQ usare `live_trades.csv` (simulatore), NON `base_real_state.json`.

### `real_executions.csv` — Solana executor

| `action` | `status` | Significato |
|---|---|---|
| `buy` | `dry_run` | Simulazione (nessun token reale) |
| `buy` | `sent` / `confirmed` | **Trade reale eseguito** |
| `buy_failed` | `error` | Route non trovata (Jupiter/Raydium) |
| `buy_skipped` | `skipped` | Bloccato da rugcheck o entry_drop |
| `hard_sl_stuck` / `liq_collapse_stuck` | `stuck` | Sell tentato 3 volte, fallito — posizione bloccata on-chain |

### Filtri pump_grad — esenzioni per chain

| Filtro | Solana | Base |
|---|---|---|
| `liq < $25k` | ❌ skip | ❌ skip |
| `chg_1h > 80%` | ❌ skip | ❌ skip |
| `vol_h1 < $5k` | ❌ skip | ✅ **passa** (pool nuovissime <2min, vol=0 per definizione) |

### DEX su Base — routing executor

| DEX | `pool_type` | Router | Swap function |
|---|---|---|---|
| Uniswap V3 | `"v3"` | `0x2626...` | `_swap_v3()` (exactInputSingle) |
| Aerodrome | `"v2"` | `0xcF77...` | `_swap_aero()` (routes tuple) |
| **Uniswap V2** | **`"univ2"`** | **`0x4752...`** | **`_swap_v2()`** (path array) |

**Nota critica**: i nuovi pool LIQ su Base (<5min) sono **prevalentemente su Uniswap V2**,
non su V3/Aerodrome (DexScreener: `dexId=uniswap, labels=['v2']`).
`_find_pool` cerca in ordine V3 → Aerodrome → V2, solo pool **WETH/token** (no USDC).
Il wallet opera sempre in WETH: buy `ETH→wrap→WETH→token`, sell `token→WETH`.
Le posizioni DRY_RUN **non contano** verso `MAX_POS` (filtra `entry_tx != "DRY_RUN"`).

**Pipeline segnali LIQ Base** (dopo WS on-chain):
```
PairCreated WS (~2s) ─┐
                       ├→ pump_grad_signals.csv → simulator (0-10s) → base_executor (0-5s)
GT poll 30s (fallback)─┘
```
`PairCreated` scatta al deploy pair (reserves=0). Il monitor fa retry `getReserves`
ogni 2s (max 30s) fino a weth_raw>0. GeckoTerminal resta per Solana e pool su altri DEX.

---

## 3. Sistemi di Trading (CONFIGS in trade_simulator.py)

| Sistema    | Fonte segnali         | TP1  | TP2  | Trail att. | Hard SL | BSR soglia | BSR conferme | Max hold |
|------------|-----------------------|------|------|------------|---------|------------|--------------|----------|
| defi       | defi_optimized        | 15%  | trail| **12%**    | -8%     | **0.45**   | **7**        | 3h       |
| pump_grad  | pump_graduation_scanner| 25% | 80%  | 15%        | -12%    | 0.45       | 2            | 1h       |
| pre_grad   | pre_grad_monitor      | 40%  | 100% | 15%        | -12%    | 0.45       | 4            | 20min    |
| mirror     | wallet_mirror_bot     | (usa config pump_grad) |  |  |  |  |  | 1h |
| v3         | gemmeV3               | 20%  | 50%  | 15%        | -15%    | 0.55       | 4            | 48h      |
| v3_large   | gemmeV3 (mcap>$10M)   | 20%  | 60%  | **20%**    | —       | 0.45       | 4            | 7gg      |
| v3_midcap  | gemmeV3 (CoinGecko)   | 12%  | 30%  | 8%         | —       | 0.45       | 4            | 24h      |
| v2         | gemmeV2 (NON attivo)  | 15%  | 40%  | 12%        | -8%     | 0.50       | 4            | 48h      |

**Grace period (BSR/vol exit)**: 13 min dall'apertura posizione (`position_open_ts`)
**Refresh prezzi**: 30s
**Trail drop adattivo** (defi): peak<20%→8%, peak 20-40%→11%, peak>40%→15%
**Sanity check oracle**: chg>5000% o <-99.9% → ignora prezzo (VIMAX bug fix)

---

## 4. Parametri Exit Quality (trade_simulator.py)

- **exit_bsr_collapse**: BSR < 0.45 per 7 conferme consecutive (non in grace) + chg <= -3%
- **exit_vol_crash**: volume < entry_vol × ratio, con guard BSR ≤ 0.65
- **vol_crash_grace**: v3/v3_large/v3_midcap = 25 min; defi/altri = ENTRY_GRACE_MIN (13 min)
- **liq_collapse**: liq < 5k USD per 2 cicli consecutivi (o liq relativa pump_grad/pre_grad)
- **liq_velocity** (pump_grad/pre_grad): exit immediato se liq crolla >35% in un singolo ciclo (30s)
- **exit liquidity BASE**: pool secca (liq < $500) → skip prezzo (nessun aggregatore tipo Jupiter)
- **hard_sl**: scatta anche in grace; blacklist token 12-48h post-trigger
- **adaptive snap1**: exit immediata al primo fetch se chg < soglia (defi: -20%, pump_grad: -8%)
- **Filtri anti re-entry** (NUOVO): cooldown differenziati per tipo exit; re-entry bloccata se prezzo < 75% del prev entry
- **Filtro anti-dump** (NUOVO): change_1h < -2% AND bsr < 0.50 → skip entry

---

## 5. Dune Analytics (gemme/setup_dune.py) — Query v4

| Query ID | Chain   | Finestra | Buyers min | Min trade | Inflow min | Novità v4 |
|----------|---------|----------|------------|-----------|------------|-----------|
| 7417474  | Solana  | **8h**   | 6          | $150      | $5k        | inflow_last_2h, inflow_recency_ratio, buyers_last_2h |
| 7417475  | BSC     | 24h      | 5          | $100      | $5k        | repeat_buyers (non usato: BSC disabilitato) |
| 7417476  | Base    | **12h**  | 5          | $100      | **$3k**    | idem Solana + blacklist VIRTUAL/TOSHI/NORMIE/AERO |
| 7417477  | ETH     | 24h      | 5          | $100      | $5k        | repeat_buyers (non usato: ETH disabilitato) |

---

## 6. Funzioni Principali per File

### `gemme/gemmeV3.py`
```python
# AGGIORNATO 13/06: stampa_gemma drift gate -8%→-3% (= c10_notfall di defi_optimized,
#   backtest n=46 precisione 77%). Analisi live_trades.csv (730 trade chiusi, dato
#   01/06-13/06 quasi flat +186→+244€): segnali "via_gemmeV3" instradati al sistema
#   defi avevano hard_sl rate 49.5% (n=107, -68€) vs 22.6% (n=274, +10€) dei nativi
#   defi_optimized — il drift check -8% non bastava a sopprimere i retrace post-pump.
#   Sistemi migliori: pump_grad +546€ WR54%, midcap +161€ WR69%, v3_large +203€ WR50%.
#   Da validare dopo qualche giorno: hard_sl rate via_gemmeV3 dovrebbe scendere verso
#   il 22% nativo. Serve restart run.py.
# AGGIORNATO 10/06 — FIX DUNE CONGELATO (root cause dei 0 trade v3 dal 02/06):
#   _get_latest_results ora controlla execution_ended_at: se >1h ritorna (None, stale)
#   per forzare _execute_and_wait (con fallback alle righe stale se fallisce).
#   2° bug: POST /execute con {"performance":"large"} SEMPRE rifiutato dal piano
#   free ("Invalid performance tier") → rimosso il body. Costo ~0.03 crediti/run.
# AGGIORNATO 10/06: GemFilter BSR 0.1→0.5 (backtest 330 gemme: bsr<0.5 = 0 win,
#   5 disastri, final mediano -86.6%; fascia 0.5-0.8 ha il 60% WR → non alzare oltre)
def load_persistent_state() / save_persistent_state()
def detect_sr_levels(df_4h) → list   # NUOVO: S/R levels per structural_bot
class GemFilter:
    def check(p) -> (bool, str)
    # NUOVO filtri: MIN_VOLUME_1H_USD=10k, MIN_CHANGE_1H_PCT=-10%,
    #               anti-dump: change<-2% AND bsr<0.50 → False
class CoinGeckoTrendingFetcher:
    _CG_TRENDING_TTL = 2h  # era 4h
class CoinGeckoMidCapFetcher:
    _CACHE_TTL = 1h         # era 3h
# Bridge watchlist → gem_probability mappato da tier (DIAMOND=0.90, GOLD=0.72...)
```

### `defi/midcap_scanner.py` (NUOVO)
```python
# Scanner mid/large cap con BB Squeeze + reversal strutturale
# Async fetch via ccxt.async_support (150+ coin in ~10s)
def fetch_coingecko_universe() → dict   # top 800 coin, 8 pagine
async def fetch_all_ohlcv(symbols) → dict  # parallelo con semaforo 20
def analyze_coin(symbol, ohlcv, cg) → dict # score 0-100 pre-breakout
def enrich_fundamentals(candidates, universe) → list  # /coins/{id} per top candidati
def main(stop_event)  # loop ogni 4h, avviato da run.py (--no-midcap per skippare)
# CoinGecko key: env COINGECKO_API_KEY (Demo, ~4200/10k call/mese)
# Score: squeeze(25)+duration(15)+expansion(15)+lean(8)+RSI div(10)+vol_spike(3)+EMA+HH/HL(19)+breakout(5)
#        +social(±5, step10) +CEX listing boost(+15, step11 via cex_listing_watcher.get_cex_boost())
# AGGIORNATO 17/06: step 11 CEX listing boost: se ticker in data/cex_listings.json (TTL 24h) → +15pt
# AGGIORNATO 16/06 (backtest n=104 chiusi, WR 67.3%, +288€):
#   adx > 55 → HARD REJECT (non più -5pt, return None): backtest bb_wpct≥50+adx≥55 → WR 0%
#   vol_ratio < 1.0 → +5pt (accumulo silenzioso: WR 77.8% n=27 vs ≥1.5 WR 62.5% n=24)
#   vol_ratio ≥ 1.5 → -5pt (buying già consumato, combo vol_spike+vol_ratio≥1.5 → WR 50% -20€)
#   social score da social_monitor.get_social_score() → ±5pt (dati da raccogliere, graceful se file mancante)
#   RVol puro NON implementato: vol_spike=True correla con token già pompati (ret_30d=126%, bb_wpct=78)
# AGGIORNATO 09/06 (backtest n=28): change_7d > 150% → score -= 12; adx>55 ora hard reject
# entry_score_min=35; hh_hl=True → WR 82.9% vs False 57.6% (n=68)
```

### `defi/defi_optimized.py`
```python
# AGGIORNATO 10/06 sera2: condizioni anti-retrace in generate_signals (le metriche
# 1h sono lagging: pump mediano +12% PRIMA del segnale, upside post +2.2% — il
# segnale scattava nel retrace; GutGenug/STEPHEN/KINS hard_sl in 4-10min):
#   c10_notfall: prezzo >= 97% del prezzo all'ultimo ciclo lento (_prev_cycle_px,
#     aggiornato solo con collect_nearmiss=True). Backtest n=46: precisione 77%.
#   c11_bsrshift: bsr_recent_shift >= -0.15 (venditori non in surge ora).
#     PROVVISORIA: backtest n=8 (4 bloccate tutte loss), rivalutare a n>=20.
# AGGIORNATO 10/06: FAST-POLL WATCHLIST (2-stage entry, anticipa il segnale):
#   Stage1: generate_signals raccoglie i near-miss (tutte le condizioni OK tranne
#           comp>=0.55, con comp>=0.45) → _fastpoll_add_candidates()
#   Stage2: thread fastpoll_loop() (avviato in main, daemon): tick 30s, batch
#           DexScreener /dex/pairs/{chain}/{addr1,...} (max 30), refresh campi
#           dinamici, ri-esegue generate_signals(quiet=True), emette al 2° tick
#           consecutivo sopra soglia (conferma anti-rumore) via stampa_segnale.
#           Marker "fastpoll=true" in top_features per backtest. TTL 45min.
#           NON aggiorna _update_bsr_history (bsr_trend resta sul ciclo lento).
#   generate_signals(df, modello, scaler, threshold, collect_nearmiss=True, quiet=False)
#   apply_hard_filters(df, quiet=False)
# AGGIORNATO 10/06: path ancorati al modulo (la CWD del processo era fuori repo):
#   _REPORTS_DIR = Path(__file__).parent/"reports" (cycle_stats + followup blacklist
#   che era SILENZIOSAMENTE DISATTIVATA), debug/ idem. Storico migrato.
# AGGIORNATO 10/06: log "passano ai filtri ML" → "passano alle condizioni pre-pump"
# NUOVO filtro anti-dump in generate_signals(): change_1h<-2% AND bsr<0.50 → scartato
# Label watchlist: gemmeV2 → gemmeV3
# AGGIORNATO 09/06: BSR history persistente su disco (data/bsr_history.json):
#   _BSR_HISTORY_FILE = Path("data/bsr_history.json")
#   _BSR_HISTORY_MAX_AGE_SEC = 7200  # scarta entry >2h al caricamento
#   _save_bsr_history()  # chiamata in _update_bsr_history() dopo ogni update
#   _load_bsr_history()  # chiamata in main() prima del loop
#   Fix: bsr_trend_per_min era sempre 0 dopo restart (deque in memoria)
# AGGIORNATO 09/06: diagnostic log reports/cycle_stats.csv (append ogni ciclo):
#   colonne: ts, chain, n_raw, n_hard_pass, n_signals, bsr_med, vol_med, chg_med
#   n_raw=token API pre-filtro; n_raw OK+n_hard_pass basso → filtri troppo restrittivi
#   n_raw basso → problema mercato/API
# AGGIORNATO 12/06: soglie pre-pump PER-CHAIN (blocco condizioni ~3222) — Base
#   emetteva 0 segnali in 600 cicli (soglie calibrate su Solana) nonostante
#   pump-rate Base ≥+20%/60min = 1.48% vs Solana 1.28% (backtest debug_candidates).
#   Base: accel 0.7→0.5, PVD 0.08→0.04, comp 0.55→0.45. Solana invariata.
#   Nota: fast-poll near-miss su Base di fatto disabilitato (comp_min==_FASTPOLL_COMP_MIN=0.45).
#   debug_candidates.csv esteso: volume_5m_usd, change_5m_pct, change_1h_pct, buys_5m, sells_5m
#   (rotazione automatica). DA VALIDARE ~19/06: WR>50% su trade chain=base nativi
#   (non via_gemmeV3) in live_trades.csv; se <50% ripristinare soglie Solana.
```

### `defi/rugcheck.py`
```python
# FIX 11/06: is_safe("pre_grad",...) instradato su _check_pump_grad (top-holder
#   concentration, blocca se top1>25%) invece di _check_lp_lock — quest'ultimo
#   per "pre_grad" faceva return True SEMPRE (MIN_LP_LOCKED non ha la chiave
#   "pre_grad" → min_lp=None → no-op). Causa probabile dei crolli -50/-83% su
#   hard_sl (40 segnali, WR 2.5%, -426€): rugcheck preventivo non bloccava nulla.
# ⚠️ DA VERIFICARE: _check_pump_grad è fail-CLOSED se topHolders vuoto dopo 3
#   retry (15s) — se RugCheck.xyz non indicizza token pre-graduation, TUTTI i
#   segnali pre_grad potrebbero finire bloccati. Controllare log [RugCheck/pump]
#   dopo restart. Serve restart run.py.
```

### `defi/trade_simulator.py`
```python
# FIX 15/06: gate disalignment vol_h1 risolto — guard entry defi (era hardcoded
#   10_000) ora usa MIN_VOLUME_1H_USD_DEFI=15_000 da data_quality.py (stessa
#   costante anche in defi_optimized.CONFIG["MIN_VOLUME_1H_USD"], single source
#   of truth).
# NUOVO 15/06: data_fault layer — _is_data_fault(sid,s) usa
#   data_quality.is_valid_trade_event() per escludere da sys_stats/total_pnl/
#   real_closed_all i trade senza riga action="entry" e drawdown immediato
#   <=-80% (missing_entry_event, es. WINNING/GOBLIEN/SERENA/PXC 23-24/05,
#   rehydration da live_state.json obsoleto; esclude skip/skip_stale, by-design)
#   + hard_sl con pnl_eur=0 ma chg<=-1% (zeroed_low_liquidity: simulator non ha
#   potuto stimare un'uscita realistica — 24 defi "azzerato"/duplicato/Jupiter-bug,
#   7 pump_grad liquidità bassa, 43 pre_grad "ANNULLATO 11/06 pre-rugcheck").
#   _log_data_fault_trades() li scrive (dedup) in reports/data_fault_trades.csv
#   con fault_reason. Validazione: defi n 458→422, PF 0.960→1.197, pnl -78.41→
#   +308.82€; pump_grad/pre_grad PF invariato (pnl_eur=0 non sposta PF) ma WR
#   reale migliora (denominatore corretto). Nessun tuning soglie.
# FIX 11/06: pair_address="nan" poison in active_pairs — pre_grad scrive
#   pair_address vuoto (NaN, bonding curve senza DEX pair); str(nan or "")="nan"
#   finiva in active_pairs alla 1a entry pre_grad e bloccava IN SILENZIO (nessun
#   log) tutti i segnali pre_grad successivi. Fix: pair_addr_check/pair_addr
#   normalizzati "nan"→"" in _load_new_signals. Serve restart run.py.
# FIX 11/06 bis: il fix sopra ha reso pair_address="" (falsy) per le posizioni
#   pre_grad → guard di testa _process_position (`not pos.get("pair_address")`)
#   ritornava SUBITO per OGNI posizione pre_grad → mai aggiornate (bloccate a
#   +0.0% per sempre, peggio di prima). Fix: guard ora esclude system=="pre_grad"
#   senza pair_address (gestito dal ramo bonding-curve _fetch_price_pumpfun).
#   Serve restart run.py.
# ALLOWED_CHAINS = {"solana", "base"}
# ENTRY_GRACE_MIN = 13.0
# Cooldown differenziati: hard_sl=12h, bsr_collapse/vol_crash=4h, entry=8h, liq_collapse=24h
# Re-entry price filter: new_price < prev_entry × 0.75 → skip
# Anti-dump filter: change_1h<-2% AND bsr<0.50 → skip
# Trail drop adattivo: peak<20%→8%, 20-40%→11%, >40%→15%
# Sanity oracle: chg>5000% → ignora
# _RugWatcher: WS logsSubscribe (RPC standard Helius, NON Atlas premium) su pool pump_grad
#   FAST_CHECK_WINDOW_MIN=15, FAST_CHECK_DEBOUNCE_SEC=5 — fetch fuori-turno su attività pool (gap risk rug)
# NUOVO 10/06: _smart_money_count(token_address) — legge coda wallet_events.csv (cache 60s),
#   conta wallet alpha distinti che hanno comprato il mint nelle ultime 6h; all'entry di segnali
#   solana non-mirror aggiunge "smart_money=N" al note + log 🐋. SOLO annotazione: nessun
#   filtro/boost finché un backtest non valida l'edge
# FIX 12/06 (pre_grad/pump_grad stuck-open): nel ramo `if not fetch:` di _process_position,
#   `now` era usato PRIMA di essere definito → NameError ingoiato da `except Exception: pass`
#   → exit_time_limit (20min pre_grad / 45min pump_grad senza prezzo) mai scattava. Fix:
#   `now = datetime.now()` in testa al ramo + log dell'eccezione. Inoltre exit_max_age ("mai
#   avuto prezzo, >3x max hold") era codice morto DOPO il fetch riuscito → spostato dentro
#   il ramo `if not fetch:` (elif su last_fetch None). Aggiunto _nofetch_count + log.warning
#   (1° fallimento e ogni 20) per capire perché il fetch bonding-curve fallisce solo live.
#   Le 13 posizioni pre_grad stuck verranno purgate al restart (_load_state: età>2×0.33h).
# FIX 12/06: oracle Base on-chain scartato se diverge >50% da prezzo DexScreener (~riga 2221)
#   — quote_onchain agganciava pool sbagliato/vuoto → 3 chiusure -100% fantasma (PEAK×2, ECLYPSE)
# FIX 12/06: _compute_daily_pnl sommava pnl_eur CUMULATIVO per riga → 24h gonfiato (+156 vs
#   +64 reali, stessa bug class del track record 4x del 10/06). Fix: delta per segnale
#   (ultima riga − ultima riga pre-finestra). Alimenta il circuit breaker (0/disattivato).
# FIX 12/06 dashboard sim_report.html (JS, in closed_row/applyFilter):
#   (a) filtro data tabella chiuse usava sigdate (apertura) invece di exitdate (chiusura) →
#       aggiunto data-exitdate/data-exitts; ctbody loop usa r.dataset.exitdate||sigdate
#   (b) bottone "Ultime 24h" usava preset calendario (date string, finestra 24-48h) invece di
#       rolling 86400s vero → nuova setPreset24h()/_last24h, cutoff24h=Date.now()/1000-86400,
#       confronto su data-exitts. Default DOMContentLoaded ora chiama setPreset24h().
class LiveEngine:
    def _load_new_signals()    # filtri cooldown differenziati + prezzo re-entry + anti-dump
    def _process_position(sid, pos)
    def _on_pool_activity(pa)  # callback _RugWatcher → fetch immediato prezzo (fast-check)
    # NUOVO 17/06: shadow tracking pool liq $10k-$25k da liq_monitor
    def _consume_shadow_queue() # legge liq_shadow_queue.csv, _shadow_register() per ogni entry,
                                # poi tronca il file mantenendo l'header (cut & paste in memoria)
    # NOTA: _consume_shadow_queue() e _process_shadows() chiamati dal loop di run.py
    # (NON in LiveEngine.run() — run.py ha il suo loop principale, non usa engine.run())
```

### `executor/base_executor.py`
```python
# AGGIORNATO 10/06: in DRY_RUN nessun limite MAX_POS e notional fisso $100
# (trade_size_eth = 100/weth_usd) — il dry replica il simulator, i limiti
# proteggono solo capitale reale. In modalità reale invariato.
# Oracle on-chain diretto Base + WETH unwrap dopo ogni sell
# Bug fix: WETH unwrap, eth_received corretto, ETH balance check, min_out floor
# EXIT_ACTIONS: aggiunto exit_adaptive, exit_momentum, exit_max_age, exit_price_timeout, manual_close
# _ILLIQUID_EXITS: liq_collapse, exit_vol_crash, exit_adaptive → min_out=0
def execute_buy(...)   # check ETH balance prima del wrap; gas_reserve=0.001 ETH
def execute_sell(...)  # unwrap WETH→ETH dopo sell; min_out adattivo per tipo exit
def main(stop_event)   # loop 5s su live_trades.csv, BASE_DRY_RUN=false
```

### `executor/solana_executor.py`
```python
# AGGIORNATO 10/06: segnali dry (_is_dry_run(system)) non soggetti a MAX_OPEN_POS
# e notional fisso $100 (_size_usdc=100; pre_grad: _size_sol=100/sol_px via
# _get_sol_price_usd). In reale invariato (size da .env, limiti attivi).
# 10/06: archiviate 6 posizioni STUCK (status=archived_rug, 5 rug reali 01/06
# valore residuo $0.69 + 1 fantasma dry; backup real_state.json.bak_archive_rug)
# RPC auto: Helius se HELIUS_API_KEY presente, altrimenti mainnet pubblico
RPC_URL = HELIUS_RPC if HELIUS_API_KEY else "https://api.mainnet-beta.solana.com"
# FIX 12/06: process_row (ramo entry) ora skippa righe con "shadow=true" in note —
#   le entry pre_grad shadow (size=0 nel simulator) venivano comprate dry-run lo stesso
#   (3 BUY il 12/06: ZEROHOUSE/MEMECOIN/KAI), nessun danno solo perché DRY_RUN=true.
```

### `defi/run.py`
```python
# Carica executor/.env per EXECUTOR_CHAINS
# Flag: --no-solana, --no-base, --no-midcap, --no-mirror, --no-social (separati)
# EXECUTOR_CHAINS env var: "base" | "solana" | "base,solana"
# Componenti: LiveEngine, PumpGrad, PreGrad, BasePump, defi_optimized,
#             gemmeV3, solana_executor, base_executor, midcap_scanner,
#             wallet_mirror_bot (NUOVO 10/06: avviato solo se alpha_wallets.json esiste;
#             SystemExit se piano Helius nega le subscription → no restart-loop)
# NUOVO 10/06:
#   _start_component: backoff esponenziale (raddoppia su crash <10min, cap 600s,
#     reset dopo uptime sano) + _send_alert email dopo 5 crash veloci consecutivi
#   _send_alert(subject, body): email via SMTP_* env, throttle 6h per soggetto
#   _thread_watchdog_loop: ogni 5min verifica thread critici scanner
#     (pump_ws/pump_val/pregrd_ws/pregrd_sig/pregrd_poll/base_pump) — solo quelli
#     partiti davvero; se uno muore → alert (no auto-restart: duplicherebbe i vivi)
#   _alpha_refresh_loop: ogni 24h, se alpha_wallets.json >7gg → rigenera via
#     wallet_alpha_finder.main(min_tokens=2, top=30)
```

### `trade/structural_bot.py`
```python
# S/R levels:
def detect_sr_levels(df_4h) → list      # pivot high/low, merge 0.8%, min 2 tocchi
def _nearest_opposing_sr(price, signal, levels) → float
def _blocking_sr(entry, tp, signal, levels) → float
# Entry filter S/R: SHORT bloccato se <1.5% da supporto, LONG se <1.5% da resistenza
# TP snap: porta TP a livello S/R ± ATR×0.5 se c'è un muro tra entry e TP
def bounce_signal(df_5m, df_1h, sr_levels, funding, fng) → Signal
def fetch_fear_greed() → int   # cache 1h, alternative.me
def fetch_funding_rate() → float  # cache 15min, Binance futures
# Trail activate: 12% (era 6%), trail drop adattivo
# NUOVO 09/06: lock file fcntl all'avvio di run() → SystemExit(1) se già in esecuzione
# Lock path: reports bot strutturale/structural_bot.lock
# NUOVO 10/06: MIN_DYNAMIC_RR=4.0 — gate fee-aware in calculate_trade: entra SOLO se
#   ADX≥40 + bias 2h/4h allineati (dynamic_rr=4). Backtest 91 trade con fee 0.12% rt:
#   rr=2 net -0.43$/tr (n=49), rr=3 -0.27$ (n=16), rr=4 +0.36$ (n=26). Blocca anche bounce (0/3).
# NUOVO 10/06: capital sync anti-deadlock (delta anomalo stabile ±10% per 6 cicli → accettato)
#   + Hook 1 registra P&L di posizioni orfane chiuse dall'exchange (capitale, non W/L)
```

### `injective_autopilot/core/sentinel.py`
```python
class Sentinel:
    async def run(on_trigger)   # loop ogni 60s su 29 market Injective
    async def _tick_all()       # asyncio.gather su tutti i market
    async def _tick_market(ctx) # fetch orderbook+market+trades → segnali → trigger
# Segnali Tier A/B: OBI, CVD_DIV, ZSCORE, VOL_BREAKOUT, OI_DIV
# Segnali Tier S: FUNDING_EXTREME (z>2.5) → trigger con 1 solo segnale
# Rate limit globale: sentinel_max_triggers_per_hour=20 (era 10)
# Trigger = ≥2 segnali attivi oppure 1 Tier S
# AGGIORNATO 16/06: REGIME_SHIFT contribuisce voti direzionali:
#   BULLISH_SHIFT → votes_long+1, BEARISH_SHIFT → votes_short+1, NEUTRAL → 0
#   (prima: nessun voto, aggiungeva segnale senza influenzare direction → conflict)
#   AnomalyDetector istanziato con vol_breakout_sigma=cfg.vol_breakout_sigma
```

### `injective_autopilot/signals/anomaly.py`
```python
class AnomalyDetector:
    # AGGIORNATO 16/06: __init__ accetta vol_breakout_sigma (default=2.0); usato in
    #   _detect_vol_breakout: upper/lower = mu ± vol_breakout_sigma*sigma (era hardcoded 2*)
    def update(price, vol_regime) → (ZScoreSignal, RegimeShift, VolBreakout)
    def _compute_zscore(price, vol_regime) → ZScoreSignal   # bloccato in HIGH regime
    def _detect_regime_shift() → RegimeShift                # KL divergence recent vs historical
    def _detect_vol_breakout(price) → VolBreakout           # BB (mu ± vol_breakout_sigma*sigma)
```

### `injective_autopilot/core/decision_engine.py`
```python
# Sistema rule-based deterministico (NON chiama Claude subprocess — rimossa confusione)
# AGGIORNATO 16/06: docstring aggiornata: REGIME_SHIFT contribuisce +1 vote (BULLISH/BEARISH_SHIFT)
class DecisionEngine:
    async def decide_batch(triggers, positions, margin_available) → dict[ticker, TradeDecision]
    def _score(trigger) → (float, str)   # 0.40+(n-2)*0.10, bonus z/fz/obi/margin, conflict→0.30
    def _build_decision(trigger, score) → TradeDecision | None  # ATR cold guard, RR≥2.0
# Score base: n=2→0.40, 3→0.50, 4→0.60; conflict (margin≤1) → 0.30 penalità
# Bonus: |z|≥2.5 +0.05, ≥3.0 +0.05; |fz|≥3 +0.05; obi≥0.90 +0.05; margin≥3 +0.05, ≥4 +0.10
# ATR cold: TP dist < max(2×spread_bps/100, 0.30%) → rejected
@dataclass TradeDecision: action, confidence, entry, stop_loss, take_profit, position_size, risk_score, reason
```

### `trade/bitget_futures_executor.py`
```python
DEMO_MODE = _env("BITGET_DEMO", "false").lower() == "true"
SYMBOL    = "SBTC/USDT:USDT" if DEMO_MODE else "BTC/USDT:USDT"
SYMBOL_ID = "SBTCUSDT"       if DEMO_MODE else "BTCUSDT"
# productType dinamico: SUSDT-FUTURES (demo) vs USDT-FUTURES (live) — in TUTTE le chiamate API
# Stesse chiavi API per live e demo (BITGET_API_KEY/SECRET/PASSPHRASE)
```

### `trade/run_demo.py` (NUOVO)
```python
# Launcher bot BTC aggressivo su Bitget Demo (SUSDT-FUTURES)
# RISK_PCT=15%, INITIAL_CAPITAL=1 (sync da saldo reale), ADX_MIN=20, ATR_MIN=50
# COOLDOWN_BARS=30, MAX_CONSECUTIVE_LOSSES=4
# State separato: trade/reports_demo/
# Avvio: cd trade && python run_demo.py
```

---

## 7. File di Stato Persistenti

| File | Contenuto |
|------|-----------|
| `defi/reports/live_state.json` | Posizioni aperte LiveEngine (remaining, peak_pct, bsr_consec, position_open_ts, ecc.) |
| `defi/reports/live_trades.csv` | Log completo: entry, tp1, tp2, trail_exit, exit_bsr_collapse, ecc. |
| `executor/real_state.json` | Posizioni reali Solana (executor) |
| `executor/base_real_state.json` | Posizioni reali Base (executor) |
| `executor/real_executions.csv` | Swap reali Solana |
| `executor/base_executions.csv` | Swap reali Base |
| `executor/alpha_wallets.json` | Wallet alpha per mirror bot |
| `trade/reports/state.json` | Stato bot BTC live (capitale, W/L, trade aperto) |
| `trade/reports_demo/demo_state.json` | Stato bot BTC demo (separato) |

---

## 8. Flusso Dati Completo

```
Dune / DexScreener / GeckoTerminal / pump.fun WS / CoinGecko
        │
        ▼
[Scanner] defi_optimized / gemmeV3 / pump_grad / pre_grad / midcap_scanner
        │ signal CSV + email
        ▼
[LiveEngine] trade_simulator.py
        │ live_trades.csv (entry/tp1/exit)
        ▼
[Executor] base_executor.py (Base live) / solana_executor.py (disabilitato)
        │ on-chain swap reale
        ▼
base_real_state.json + base_executions.csv

[BTC Bot] structural_bot.py → bitget_futures_executor.py → BTCUSDT live
[BTC Demo] run_demo.py → bitget_futures_executor.py → SBTCUSDT demo
```

**Mirror flow**: Helius WS → wallet_mirror_bot → mirror_signals.csv → LiveEngine → solana_executor

---

## 9-0. Note Importanti / Bug Fix Recenti (12/06/2026)

- **bot_telegram riscritto a pulsanti**: bot.py ora naviga via callback inline (menu/pay/stats/back,
  edit_message_text); i bottoni Pay creano invoice a importo univoco (gap fixato: il vecchio
  /subscribe mostrava solo il wallet, payments.py non poteva mai fare auto-settle). ⚠️
  PAY_WALLET_SOL/EVM ancora vuoti — servono wallet di incasso dedicati.
- **Funnel FREE→PREMIUM**: teaser live censurato su FREE dopo ogni full Premium (rate-limited,
  format_teaser_live) + recap settimanale "delta Premium" (track_record.weekly_recap_text,
  throttle ≥6.5gg). Fix ticker "$$SYM"→lstrip("$"). Recap Telegram ridisegnato a colonne.
- **Promo X**: x_poster.py genera card+testo (hashtag/$TICKER/CTA) per chiusure vincenti sopra
  soglia e li manda all'admin via Telegram (post manuale) — l'API X richiede piano $200/mese
  per scrivere, niente auto-post.
- **pre_grad/pump_grad stuck-open FIXATO**: NameError `now` + exit_max_age morto impedivano
  ogni exit_time_limit; 3 entry shadow comprate per errore dall'executor (fix skip shadow).
- **Base pre-pump nativo sbloccato**: soglie per-chain rilassate (comp 0.45), da validare
  WR>50% dopo il 19/06. Oracle Base: scarto se diverge >50% da DexScreener (fix -100% fantasma).
- **Dashboard sim_report.html**: "Ultime 24h" ora è una vera finestra rolling 86400s su
  exit timestamp (prima: calendar-date su entry date, disallineata dalla landing).
  _compute_daily_pnl idem (delta per segnale, non somma cumulativi).
- **INJ autopilot — recheck tesi di trade**: niente max-hold fisso; dopo recheck_after_min
  (90min), se i segnali che hanno aperto il trade non sono più attivi sul market
  (overlap < recheck_min_overlap), chiusura a mercato (SIGNALS_GONE).
- **Whale alert silenziosi dal 10/06 FIXATI**: wallet_mirror_bot._get_fee_payer() ora usa
  commitment="confirmed" (il default "finalized" ritornava sempre result=None per le firme
  fresche da logsSubscribe).

⚠️ **Restart necessari** (non ancora eseguiti a fine sessione 12/06, chiedere prima di farli):
`run_bot.py` (bot.py, publisher teaser+x_poster, track_record weekly), `run.py` (trade_simulator
fixes pre_grad/oracle/dashboard/daily_pnl, defi_optimized soglie Base, wallet_mirror_bot
commitment fix, solana_executor shadow-skip), `injective_autopilot/main.py` (recheck tesi).

## 9. Note Importanti / Bug Fix Recenti (10/06/2026)

- **Bot BTC: close Bitget SEMPRE rotto** (10/06): `on_close` invertiva il side (hedge mode v2: close long=buy, close short=sell, stesso side dell'apertura + tradeSide=close) → 22002 "No position to close" su OGNI chiusura manuale nella storia del log (0 successi). Le posizioni restavano orfane con SL/TP nativi attivi; di solito chiuse da quelli, 4 casi orfani documentati. Fix in bitget_futures_executor.py: side corretto, retry ×3 con verifica post-close, stato executor NON azzerato su fallimento (sync continua a tracciare), guard anti-stacking in enter().
- **Bot BTC: capital sync leggeva `free` non equity** (10/06): con posizione aperta il margine bloccato faceva crollare il valore → "Delta anomalo ignorato" in loop e bot inoperante (size calcolata su capitale fantasma, executor rifiuta). Fix: `_get_balance(equity=True)` legge accountEquity dal payload raw; `enter()` usa `equity=False` (available) per il cap margine. Anti-deadlock: delta anomalo stabile (±10%) per 6 cicli consecutivi → accettato come saldo reale.
- **Bot BTC: P&L orfano registrato** (10/06): Hook 1 in structural_bot.py — se sync_position ritorna P&L ma state.open_trade è None (posizione orfana chiusa da SL/TP nativo) → capitale/daily_pnl aggiornati senza toccare W/L (trade già contato). Richiede restart del bot per attivare i fix.

- **Sistema wallet mirror era SPENTO** (10/06): alpha_wallets.json inesistente, bot non avviato da run.py, WS Atlas premium → 403. Riscritto wallet_mirror_bot su logsSubscribe standard + fetch Enhanced API (pattern _RugWatcher), ora componente 8 di run.py (`--no-mirror` per saltarlo). DRY_RUN attivo di default.
- **alpha_wallets.json generato** (10/06): finder corretto (paginazione firme fino a signal_ts, buy in SOL via nativeTransfers, seed win-only da live_trades.csv, dedup wallet/mint, penalità avg_rank>300/>100 anti bot-spray). Top 30 wallet, token 3-9, avg_rank 8-245. ⚠️ enrich history Helius (/v0/addresses) non restituisce dati → recency "?" uniforme, ranking comunque valido. Rigenerazione automatica ogni 7gg da run.py.
- **Layer narrative mirror** (10/06): wallet_events.csv (storico buy/sell completo, anche scartati), confluenza cross-wallet 6h (pump_probability 0.80+0.05/wallet, cap 0.95), sell detection con warning se token segnalato di recente, risveglio post-inattività ≥30gg (mirror_state.json).
- **Alert whale Telegram** (10/06): publisher taila wallet_events.csv → PREMIUM. Buy ≥$500 / confluence ≥2 / risveglio; sell solo sell_after_signal. Funziona anche in DRY_RUN (gli eventi vengono sempre scritti; i segnali mirror no).
- **Smart money annotation** (10/06): trade_simulator annota `smart_money=N` nel note entry dei segnali solana non-mirror se N wallet alpha hanno comprato il token nelle ultime 6h. SOLO annotazione — backtestare prima di farne un filtro (regola validazione filtri).
- **run.py hardening** (10/06): backoff esponenziale su crash (cap 600s) + email dopo 5 crash veloci (throttle 6h); watchdog thread critici scanner ogni 5min (solo alert); base_pump_scanner non muore più se RPC giù all'avvio (retry 15s→5min).

## 9b. Note Importanti / Bug Fix Precedenti (09/06/2026)

- **structural_bot doppia istanza (09/06 mattina)**: due processi in parallelo causavano trade duplicati (-5.9$). Fix: lock file fcntl in `run()` → SystemExit(1) se già in esecuzione.
- **injective_autopilot (09/06 mattina)**: sistema multi-market Injective Protocol perps. Fix applicati: `--bare` elimina overhead 10-20s CLI, `stdin=DEVNULL` previene hang, timeout 45→90s, rate limit 10→20/h.
- **Kill switch attivato (09/06 sera)**: Daily DD 6.6% ≥ 5%. Root cause: max_open_positions non enforced → 9 posizioni aperte → SL multipli. Fix: paper_trading/engine.py usa open_trades invece di fetch_positions().
- **max_open_positions bug fix (09/06)**: `fetch_positions()` restituisce posizioni on-chain (vuote in PAPER) → `validate_decision` riceveva sempre 0 posizioni → nessun blocco. Fix: usa `self._executor.open_trades` (dict in-memory dell'executor paper).
- **Kill switch reset endpoint (09/06)**: POST /admin/reset-kill-switch + pulsante in /risk. Resetta daily_start_equity al valore corrente (azzera contatore DD). Richiede conferma JS.
- **DD threshold paper vs live (09/06)**: `paper_max_daily_drawdown_pct=0.15` (15%) vs `max_daily_drawdown_pct=0.05` (5%) per LIVE. risk_engine.py e dashboard usano il valore corretto per modalità.
- **Ctrl+C shutdown (09/06)**: uvicorn rubava SIGINT. Fix: `install_signal_handlers=lambda: None`. Doppio Ctrl+C: primo→stop&salva posizioni open, secondo (entro 5s)→chiudi tutto. Un singolo Ctrl+C non chiude più posizioni.
- **BSR persistence (09/06)**: bsr_trend_per_min sempre 0 dopo restart (deque in memoria). Fix: `data/bsr_history.json`, max age 2h. _save_bsr_history() ad ogni update, _load_bsr_history() all'avvio.
- **Diagnostic cycle_stats.csv (09/06)**: `reports/cycle_stats.csv` — n_raw vs n_hard_pass vs n_signals per distinguere calo segnali da problema API vs filtri troppo restrittivi.
- **Midcap score penalties (09/06)**: backtest n=28 (19 win/9 loss). Aggiunto in analyze_coin: `change_7d>150%→-12`, `adx>55→-5`. 9/9 loss bloccate, 4/19 win bloccate (tutte su token estremi che poi hanno perso). N < 30, rivalidare tra 2-3 settimane.
- **Dashboard signals fix (09/06)**: colonna Market aggiunta; OBI/Fund Z/Vol mostravano '—' per Jinja2 `is defined` che restituisce True anche su None. Fix: `s.values and s.values.obi is not none`.
- **Dashboard journal fix (09/06)**: colonna Token (ticker + market_id[:12]) aggiunta; colspan aggiornato.

## 9b-bis. bot_telegram — aggiornamenti 10/06 sera (serve restart run_bot.py)

- **Track record FIXATO**: `track_record.compute()` sommava `pnl_eur` per riga ma il
  campo è CUMULATIVO per segnale → P&L pubblicato +1236€ vs **+291€ reale** (winner
  con tp1+trail contati 2-3x); i 271 segnali mai tradati (pnl=0) contati come loss
  deprimevano il WR: 30.4% → **49.9% reale**. Fix: ultima riga per signal_id +
  esclusione pnl=0. ⚠️ REGOLA GENERALE: chiunque legga live_trades.csv deve usare
  l'ULTIMA riga per signal_id, mai sommare.
- **Closure FREE fixate**: `_TradeClosureTracker.feed` faceva `pnl +=` su valori
  cumulativi → chiusure gonfiate. Ora `=`.
- **Exit TP/SL su PREMIUM (NUOVO)**: il publisher pubblica gli eventi exit del
  simulator (tp1/tp2/trail/hard_sl/sl_adaptive/liq_collapse/exit_*) via
  `formatter.format_exit_premium` — parziale = "Sold X% riding", chiusura = P&L
  "(simulated, €100/trade)". Il segnale full annuncia "Auto-managed: updates follow".
- **Anti-flood whale alert**: criteri OR originali = ~5800 alert/gg (confl≥2 da solo
  3610, sell micro 2202 — bot-spray mirror) → storm 429 07:13. Nuovi: buy usd≥500
  AND (confl≥2 OR wake≥1) oppure usd≥2000; sell_after_signal con usd≥250; dedup
  30min per (mint,side); tetto 20/h. ~29 alert/gg stimati.
- **Recap arricchito**: riga "Top strategies" (pnl>0, n≥10) — il v2 morto (-613€)
  affossava l'aggregato e nascondeva pump_grad +536€ WR77%.
- **Dead code noto**: `format_teaser` + FREE_DELAY_MIN/FREE_MIN_PROBABILITY +
  `state/free_queue.json` = teaser FREE mai cablato nel publisher (decisione
  prodotto se attivarlo).
- I read-timeout sparsi di api.telegram.org (1-2/h, recuperati al 1° retry) sono normali.

## 9c. Note Importanti / Bug Fix Precedenti (04/06/2026)

- **AUDIT SEGRETI (04/06)**: rimossi segreti hardcoded dal sorgente → migrati in `executor/.env` (caricato da run.py via dotenv PRIMA degli import, quindi risolti a import-time). Coinvolti: password SMTP Gmail (era in 8 file), 2 chiavi CoinGecko (3 file), chiave CMC (gemmeV3). Var aggiunte: SMTP_USER/PASSWORD/FROM/TO, COINGECKO_API_KEY, COINGECKO_API_KEY_SIM, CMC_API_KEY. Creato `.gitignore` (mancava; la cartella NON è ancora un git repo). Fix: `signal_tracker.py` ora importa `os`. ⚠️ Scanner lanciati standalone (non via run.py) non vedono executor/.env → email disabilitata (nessun crash).
- **bot_telegram (NUOVO 04/06)**: modulo Signal SaaS Telegram, processo isolato, read-only sui CSV. Tier Free(ritardo 15m)/Premium($49)/VIP($149). Avvio `python bot_telegram/run_bot.py`. Pagamenti USDC on-chain via invoice a importo univoco (riusa RPC executor); finché non validato usare `/grant` manuale. Tutto testato.
- **format_teaser aggiornato (04/06)**: canale FREE ora mostra l'entry price storico al momento del segnale con label "⏱ Entry al segnale (15m fa)" — hook per upgrade Premium (utente vede quanto ha mancato).
- **landing.py (NUOVO 04/06)**: genera `bot_telegram/landing/index.html` dark-theme con WR%, P&L totale, ultime 24h, media/trade, breakdown per sistema, best/worst trade, CTA Premium/VIP. Rigenerata automaticamente da `post_recap()` ogni 24h. Se `LANDING_PAGES_REPO_PATH` impostato in .env: auto-commit+push su repo GitHub Pages separato (URL pubblico: `https://USERNAME.github.io/signals-landing`).
- **EXECUTOR_CHAINS**: variabile .env per abilitare selettivamente gli executor (es. `base` only)
- **Re-entry price filter**: nuovo in trade_simulator — blocca re-entry se prezzo < 75% del prev entry (FOUR -59% → -42€ evitato)
- **Cooldown differenziati**: hard_sl=12h, bsr_collapse=4h, entry=8h — VVVeity non rientra più 5 min dopo hard_sl
- **Anti-dump filter**: alla fonte (gemmeV3 GemFilter + defi_optimized) — change_1h<-2% AND bsr<0.5 → no segnale, no email
- **gemmeV3 filtri**: MIN_VOLUME=10k (era 5k), MIN_CHANGE_1H=-10% (era -30%), anti-dump combinato
- **base_executor**: WETH unwrap dopo sell, eth_received corretto, ETH balance check, EXIT_ACTIONS completo
- **trail_activate**: 6%→12% — trail non si arma sotto TP1 (15%), elimina exit premature (FORU -6.8%)
- **Trail drop adattivo**: peak>40% usa drop 15% — AgentFloat non uscirebbe a -1.2% dal peak 52%
- **Bitget Demo**: DEMO_MODE=true richiede chiavi API separate create in Simulated Trading; header `paptrading:1` + symbol `SBTCSUSDT` + marginCoin `SUSDT` + productType `SUSDT-FUTURES` in TUTTE le chiamate. Chiavi in .env: BITGET_DEMO_API_KEY/SECRET/PASSPHRASE
- **run_demo.py**: RISK=3%, leverage=20x, ADX_MIN=15, ATR_MIN=15, COOLDOWN=10 barre, MAX_LOSSES=5, trail 30%/0.7ATR. State in reports_demo/ separato. Capital sync forzato da saldo reale prima di run()
- **pre_grad_monitor fix critico**: subscribeTokenTrade non consegnava eventi vSol → nessun segnale mai emesso. Fix: `_poll_vsol_loop()` thread che ogni 30s chiama `frontend-api.pump.fun/coins/{mint}` per token senza aggiornamenti, aggiorna vSol, triggera segnale se HOT (≥72 SOL). Watchlist entry traccia `last_trade_ts`
- **pre_grad eviction vSol loss** (04/06): token evicted dalla watchlist perdevano il vSol accumulato → ri-aggiunto con vSol=0 → sempre evicted. Fix: `_evicted_vsol` dict preserva ultimo vSol; quando ri-aggiunto: `max(init_vsol, evicted_vsol)`. Se vSol>=72 al ri-aggiunto: rugcheck immediato + v_sol_history inizializzata nel nuovo token
- **pre_grad poll velocity=0** (04/06): segnale bloccato se un solo punto history (token veloci). Fix: emette signal se `velocity>0 OR v_sol>old_vsol`, usa `vel_use=max(0.05, v_sol-old_vsol)` come fallback
- **base_executor lookup miss** (04/06): `build_token_lookup()` si aggiornava ogni 60s ma segnali apparivano in 15s → 3 miss consecutivi → skip definitivo. Fix: al primo miss, forza rebuild immediato via `token_lookup.clear(); token_lookup.update(build_token_lookup())`. Soglia aumentata 3→6 cicli
- **Filtro Dune stale** (04/06): VVVeity segnalata 6 volte con inflow_2h=$188k e buyers_2h=198 IDENTICI su 48h → dato congelato, smart money stava distribuendo. Fix in `_load_new_signals`: per segnali v3, se `(inflow_last_2h, buyers_last_2h)` identico a segnale già processato per stesso token_address → skip con log "dato Dune stale"
- **gemmeV3 TTL**: trending 4h→2h, midcap 3h→1h — cattura movers più in anticipo
- **defi/setup_dune.py**: raise SystemExit — eseguirlo sovrascrive le query v4 con SQL v2 obsoleto.
- **base_executor WETH**: non unwrappa dopo sell, non wrappa se WETH già disponibile → solo wrap della quota mancante. Meno tx, meno rischio nonce conflict
- **base_executor RPC**: mainnet.base.org → Alchemy `base-mainnet.g.alchemy.com` (in executor/.env BASE_RPC_URL). Retry automatico su 429 con backoff 5s×3 in `_ensure_approval`
- **v3_midcap disabilitato**: routing CoinGecko $1-10M → skip (era -6€ su 33 trade, 39% WR). Sostituito da midcap_scanner (BB Squeeze)
- **Filtro Dune stale** (04/06): in `_load_new_signals` per v3, blocca re-entry se `(inflow_last_2h, buyers_last_2h)` identico a segnale già processato per stesso token_address
- **TRAIL_ATR_DIST bug fix**: trade_simulator.py riga 1896 usava `TRAIL_ATR_DIST` non definito → `2.0`
- **Capital sync cap** structural_bot: 15%→60% (ciclo), 30%→60% (avvio) — permette sync con discrepanze reali (es. -37% = 43.99→27.36)
- **S/R multi-timeframe**: detect_sr_levels ora accetta 1W+1D+4h+1h. STATIC_SR_LEVELS aggiunto: $60k,$65k,$69k(ATH2021),$73.8k,$80k,$88k,$100k,$108k(ATH2024). SR_MERGE_PCT 0.8%→1.2%. BOUNCE_PROXIMITY_PCT 1.2%→1.5%, RSI LONG bounce 55→65. Fallback extreme oversold senza S/R: RSI<35+F&G≤20+vol+candle→LONG
- **check_open_trade S/R management**: (1) break-even quando prezzo supera S/R≥3t a favore (2) trail_dist×0.5 entro 2% da S/R opposto (3) partial 50% a S/R≥5t prima di TP1
- **HTML report**: colonne Entrata (prezzo entry) e Uscita (prezzo exit) aggiunte in tabelle aperte e chiuse. Formattazione compatta via `_fmt_price()`
