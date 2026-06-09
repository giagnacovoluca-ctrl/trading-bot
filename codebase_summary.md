# ARCHITETTURA E MAPPA DEL CODEBASE
Usa questa mappa per capire la struttura del progetto senza rileggere i file interi.
Aggiornato: 2026-06-09

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
        run.py              ← orchestratore: avvia tutti i componenti in thread daemon
        defi_optimized.py   ← gem hunter defi (Solana + Base, memecoins on-chain)
        trade_simulator.py  ← LiveEngine: gestione posizioni, exit, HTML reports
        midcap_scanner.py   ← scanner mid/large cap con BB Squeeze (NUOVO)
        pump_graduation_scanner.py ← token appena graduati da pump.fun → Raydium (Solana)
        base_pump_scanner.py       ← token nuovi su Base: polling Uniswap V3 + Aerodrome factory
        pre_grad_monitor.py ← intercetta token PRE-graduation sulla bonding curve
        signal_tracker.py   ← snapshot prezzi per segnali defi (followup)
        gem_watchlist.py    ← watchlist condivisa tra scanner
        rugcheck.py         ← wrapper RugCheck.xyz (LP lock, top holder check)
        binance_futures_scanner.py ← scansiona Binance Futures per segnali large-cap
        setup_dune.py       ← DEPRECATO (raise SystemExit, non eseguire)
        reports/
            signals_log.csv         ← segnali defi_optimized
            midcap_signals.csv      ← segnali midcap_scanner (NUOVO)
            pump_grad_signals.csv   ← segnali pump_graduation_scanner
            pre_grad_signals.csv    ← segnali pre_grad_monitor
            mirror_signals.csv      ← segnali wallet_mirror_bot
            live_state.json         ← stato posizioni LiveEngine (persistente)
            live_trades.csv         ← log completo azioni LiveEngine
            sim_report.html         ← dashboard trade live (auto-refresh 60s)
            exit_quality.html       ← analisi qualità exit BSR/vol (auto-refresh 10min)
            price_followup.csv      ← followup prezzi defi
    executor/
        solana_executor.py  ← esegue swap reali su Solana via Jupiter + RPC
        base_executor.py    ← executor Base chain (oracle on-chain Uniswap V3+Aerodrome+Chainlink)
        bsc_executor.py     ← executor BSC/PancakeSwap (BSC disabilitato)
        executor_report.py  ← report HTML esecuzioni reali vs simulator
        wallet_alpha_finder.py ← analizza wallet compratori early per alpha
        wallet_mirror_bot.py   ← mirror trade da alpha wallet via Helius WS
        real_state.json     ← stato posizioni reali Solana
        base_real_state.json← stato posizioni reali Base
        real_executions.csv ← log swap reali Solana
        base_executions.csv ← log swap reali Base
        alpha_wallets.json  ← wallet alpha per mirroring
        .env                ← chiavi private (SOLANA_PRIVATE_KEY, HELIUS_API_KEY, BASE_PRIVATE_KEY, EXECUTOR_CHAINS) + SMTP/CoinGecko/CMC (migrati dal sorgente 04/06)
    bot_telegram/           ← Signal SaaS Telegram (read-only sui CSV, processo isolato — NUOVO 04/06)
        config.py           ← env (riusa executor/.env per RPC); mappa SIGNAL_FILES→sistema; routing canali
                               NUOVO: BOT_USERNAME, FREE_CHANNEL_USERNAME, PREMIUM_CHANNEL_USERNAME
        csv_tail.py         ← tail incrementale CSV, offset-byte persistente (anti-repost, skip backlog)
        formatter.py        ← messaggi HTML: format_full (Premium) / format_teaser (Free)
                               AGGIORNATO: format_teaser ora mostra entry price storico con label "⏱ Entry al segnale (15m fa)"
        telegram_api.py     ← Bot API via requests, retry 429
        publisher.py        ← daemon: full su Premium/VIP, teaser FREE ritardato 15m
        bot.py              ← comandi /start /plans /subscribe /status /referral + admin /grant /stats /broadcast
        subscriptions.py    ← store abbonati (tier/scadenza/referral) in state/subscribers.json
        payments.py         ← verifica USDC on-chain (Base+Solana) via invoice a importo univoco; settle→grant
        track_record.py     ← P&L da live_trades.csv → recap FREE + state/stats.json (landing)
                               AGGIORNATO: post_recap() chiama landing.generate() dopo ogni ciclo daily
        landing.py          ← NUOVO: genera landing/index.html statico con stats baked-in (dark theme, WR%/P&L/by_system/best_worst/CTA)
                               Se LANDING_PAGES_REPO_PATH impostato: auto-commit+push su repo GitHub Pages dopo ogni rigenerazione
        run_bot.py          ← orchestratore thread daemon + gating scadenze (--no-publisher/bot/payments/track)
        store.py            ← persistenza JSON atomica
        landing/            ← output HTML generato (gitignored nel repo principale, pushato su repo separato)
        state/              ← offsets, subscribers, invoices, stats, ecc. (gitignored)
        .env / .env.example ← TELEGRAM_BOT_TOKEN, channel id, PAY_WALLET_*, prezzi tier
                               NUOVO: LANDING_PAGES_REPO_PATH, TELEGRAM_BOT_USERNAME, TELEGRAM_FREE/PREMIUM_CHANNEL_USERNAME
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
    injective_autopilot/           ← NUOVO 09/06: bot multi-market su Injective Protocol perps
        main.py                    ← entry point (PAPER/LIVE/BACKTEST mode)
        config/settings.py         ← config Pydantic (INJ_ env prefix); claude_timeout=90s, rate_limit=20/h
        core/sentinel.py           ← scansiona 29 market ogni 60s, trigger composito (≥2 segnali Tier A/B o 1 Tier S)
        core/decision_engine.py    ← chiama claude CLI (--bare --print --max-turns 1) per decisione finale JSON
                                      AGGIORNATO 09/06: --bare + stdin=DEVNULL + timeout 45→90s
        core/risk_engine.py        ← risk management (max DD, margin, position sizing)
        core/executor.py           ← esecuzione ordini su Injective
        paper_trading/engine.py    ← paper trading engine (PAPER mode default)
        signals/                   ← orderbook.py, volume.py, derivatives.py, volatility.py, anomaly.py
        data/injective_client.py   ← client gRPC/REST Injective mainnet
        data/cache.py              ← buffer rolling (prezzi, funding, OI)
        backtest/engine.py         ← backtest walk-forward + live gate (500 trades, PF>1.5, Sharpe>1.5)
        dashboard/app.py           ← FastAPI dashboard http://127.0.0.1:8080 (auto-refresh 10s)
        database/repository.py     ← SQLite async (aiosqlite)
```

---

## 2. Chain & Sistema attivi

| Chain   | Scanner          | Simulator | Executor reale |
|---------|------------------|-----------|----------------|
| Solana  | defi_optimized + gemmeV3 | ✅ | ⛔ disabilitato (EXECUTOR_CHAINS=base) |
| Base    | defi_optimized + gemmeV3 | ✅ | ✅ base_executor live (BASE_DRY_RUN=false) |
| BSC     | disabilitato     | log storico | bsc_executor (inattivo) |
| ETH     | disabilitato     | log storico | — |

**EXECUTOR_CHAINS** in executor/.env: controlla quali executor avviano in run.py.
**BASE_TRADE_SIZE_ETH=0.006** (~$15 a ~$2500/ETH), **BASE_MAX_OPEN_POSITIONS=3**

**ALLOWED_CHAINS** in trade_simulator: `{"solana", "base"}`

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
def main(stop_event)  # loop ogni 8h, avviato da run.py (--no-midcap per skippare)
# CoinGecko key: env COINGECKO_API_KEY (Demo, ~4200/10k call/mese)
# Score: squeeze intensity(25) + duration(15) + expansion(15) + lean(8) + RSI div(10) + vol(7) + EMA(15) + breakout bonus(5)
```

### `defi/defi_optimized.py`
```python
# NUOVO filtro anti-dump in generate_signals(): change_1h<-2% AND bsr<0.50 → scartato
# Label watchlist: gemmeV2 → gemmeV3
```

### `defi/trade_simulator.py`
```python
# ALLOWED_CHAINS = {"solana", "base"}
# ENTRY_GRACE_MIN = 13.0
# Cooldown differenziati: hard_sl=12h, bsr_collapse/vol_crash=4h, entry=8h, liq_collapse=24h
# Re-entry price filter: new_price < prev_entry × 0.75 → skip
# Anti-dump filter: change_1h<-2% AND bsr<0.50 → skip
# Trail drop adattivo: peak<20%→8%, 20-40%→11%, >40%→15%
# Sanity oracle: chg>5000% → ignora
# _RugWatcher: WS logsSubscribe (RPC standard Helius, NON Atlas premium) su pool pump_grad
#   FAST_CHECK_WINDOW_MIN=15, FAST_CHECK_DEBOUNCE_SEC=5 — fetch fuori-turno su attività pool (gap risk rug)
class LiveEngine:
    def _load_new_signals()    # filtri cooldown differenziati + prezzo re-entry + anti-dump
    def _process_position(sid, pos)
    def _on_pool_activity(pa)  # callback _RugWatcher → fetch immediato prezzo (fast-check)
```

### `executor/base_executor.py`
```python
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
# RPC auto: Helius se HELIUS_API_KEY presente, altrimenti mainnet pubblico
RPC_URL = HELIUS_RPC if HELIUS_API_KEY else "https://api.mainnet-beta.solana.com"
```

### `defi/run.py`
```python
# Carica executor/.env per EXECUTOR_CHAINS
# Flag: --no-solana, --no-base, --no-midcap (separati)
# EXECUTOR_CHAINS env var: "base" | "solana" | "base,solana"
# Componenti: LiveEngine, PumpGrad, PreGrad, BasePump, defi_optimized,
#             gemmeV3, solana_executor, base_executor, midcap_scanner
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
```

### `injective_autopilot/core/decision_engine.py`
```python
class DecisionEngine:
    async def decide(trigger, positions, margin_available) → TradeDecision
    async def _call_subprocess(prompt) → str  # claude --bare --print --max-turns 1 --system-prompt ...
    async def _call_sdk(prompt) → str         # Anthropic SDK (richiede API key, non usato)
# use_subprocess=True (default): chiama CLI claude, NON SDK (utente non ha API key)
# AGGIORNATO 09/06: aggiunto --bare (no CLAUDE.md/hooks overhead), stdin=DEVNULL
# timeout: 45→90s (config settings.py)
# Risposta attesa: JSON con action/confidence/entry/sl/tp/position_size/risk_score/reason
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

## 9. Note Importanti / Bug Fix Recenti (09/06/2026)

- **structural_bot doppia istanza (09/06)**: due processi in parallelo (uno avviato da Claude con nohup in sessione precedente) causavano trade duplicati (-5.9$ stimati) e cooldown dimezzato. Fix: lock file fcntl in `run()` → SystemExit(1) se già in esecuzione. Istanza spuria (PID 134862) killata manualmente.
- **injective_autopilot (NUOVO 09/06)**: sistema multi-market Injective Protocol perps. Sentinel scansiona 29 market ogni 60s, decision engine chiama `claude` CLI (subprocess, NON SDK — utente non ha API key Anthropic). Fix applicati: `--bare` elimina overhead 10-20s di cold start CLI, `stdin=DEVNULL` previene hang, timeout 45→90s, rate limit 10→20/h.

## 9b. Note Importanti / Bug Fix Precedenti (04/06/2026)

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
