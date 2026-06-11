# Injective Autopilot

## Descrizione Generale
Bot di trading autonomo per perpetual futures su **Injective Protocol** (Cosmos SDK chain), che monitora 29 mercati in parallelo, genera segnali quantitativi (orderbook, derivati, volume, volatilità, anomalie statistiche) e li trasforma in decisioni di trading tramite un **motore di scoring rule-based deterministico**, validate da un risk engine indipendente. Include modalità BACKTEST/PAPER/LIVE, dashboard FastAPI real-time e un layer di analytics/auto-learning che ricalibra i pesi dei segnali nel tempo.

> Nota di evoluzione architetturale: la prima versione del decision engine chiamava **Claude via CLI subprocess** per sintetizzare i segnali in una decisione JSON. È stata successivamente **sostituita da uno scoring deterministico** (`model="rule_based"`) per eliminare latenza/costo/non-determinismo della chiamata LLM in un loop a 60s su 29 mercati, mantenendo però il layer di adaptive learning Bayesiano per l'auto-calibrazione dei pesi.

## Problema Risolto
Sui mercati perpetual ad alta frequenza è difficile combinare decine di segnali eterogenei (microstruttura orderbook, funding, OI, volatilità) in una decisione di trading coerente, riproducibile e veloce. Il sistema risolve questo con una formula di scoring esplicita e pesata, mantenendo tutti i controlli quantitativi (sizing, R:R netto fee-aware, kill switch) in un risk engine separato e indipendente dal motore decisionale.

## Architettura
- **Sentinel** (`core/sentinel.py`): loop a 60s, `asyncio.gather` su 29 mercati; per ciascuno calcola OBI, CVD divergence, funding z-score, OI divergence, regime di volatilità, anomalie. Trigger se ≥2 segnali Tier A/B oppure 1 segnale Tier S (funding estremo, |z|>2.5).
- **Decision Engine** (`core/decision_engine.py`, rule-based deterministico): per ogni trigger calcola uno score di confidenza con una formula esplicita — base `0.40 + (signal_count-2)*0.10`, bonus per margine voti long/short (+0.05/+0.10), |z-score|≥2.5/3.0 (+0.05/+0.10), |funding z-score|≥3 (+0.05), OBI≥0.90 (+0.05); rigetta segnali "MIXED" (margine voti ≤1) o spread eccessivo. `decide_batch()` ordina i trigger per score×peso-adattivo e approva fino a `max_open_positions - posizioni aperte`. Output: `TradeDecision` (entry/SL/TP/size/risk_score/reason) con `model="rule_based"`.
- **Risk Engine** (`core/risk_engine.py`): kill switch su DD giornaliero/settimanale, margine, errori consecutivi; calcolo R:R netto con funding; position sizing.
- **Executor** (`core/executor.py`): in PAPER scrive su DB e passa a `PaperTradingEngine`; in LIVE chiama `InjectiveClient.create_limit_order`.
- **Paper Trading Engine**: simula fill, gestisce SL/TP, aggiorna equity, espone `open_trades` (fonte di verità per `max_open_positions`, dopo un bug fix).
- **Database**: SQLAlchemy 2.0 async + aiosqlite; tabelle `trades`, `signals`, `ai_decisions`, `margin_snapshots`, `error_logs`, più `trade_postmortems` e `signal_weight_snapshots` aggiunte per il layer di learning.
- **Dashboard**: FastAPI + Jinja2, 8+ pagine (overview, performance, journal, signals, risk, ai, analytics, markets, learning), API JSON per polling, grafici Plotly.
- **Backtest Engine**: walk-forward 70/30 da CSV storici, `check_live_gate` per validare il passaggio a LIVE (≥500 trade, PF>1.5, Sharpe>1.5, max_dd<20%, ≥30 giorni stabili).
- **Analytics/Learning** (pacchetto `analytics/`, aggiunto 10/06/2026):
  - `performance.py`: pure function su trade chiusi (ranking segnali/combo/mercati, analisi orarie/settimanali/per regime di volatilità).
  - `adaptive_scorer.py`: `AdaptiveScorer` aggiorna i pesi dei segnali con **Bayesian updating (Beta prior 2,2)** + EWMA su rolling window 50, pesi clampati [0.5, 1.5], applicati solo al ranking (`decide_batch` ordina per score×peso, ma il gate di approvazione resta sullo score grezzo → no overfitting sul flusso operativo, `set_signal_weights()` collega l'output dell'analyzer al decision engine).
  - `postmortem.py`: calcola R-multiple, hold time, MAE/MFE, contributo dei segnali per ogni trade chiuso.
  - `audit.py`: CLI (`python -m analytics.audit`) per backfill e report di coerenza.

## Tecnologie
Python 3, asyncio, FastAPI, Jinja2, uvicorn, SQLAlchemy 2.0 (async) + aiosqlite + alembic, Pydantic/pydantic-settings (env prefix `INJ_`), pyinjective `async_client_v2`, structlog, tenacity, Plotly.

## Funzionalità
- Scansione multi-mercato concorrente, decisioni batch con ranking per score e capacità residua di posizioni.
- Decisioni strutturate (dataclass `TradeDecision`) con motivazione testuale leggibile (es. `"n=3, votes=4/1, z=2.7"`) salvata per audit.
- Kill switch automatico con soglie differenziate PAPER (15% DD)/LIVE (5% DD), endpoint admin per reset.
- Doppio Ctrl+C con timeout 5s: primo = stop & salva posizioni, secondo = chiusura forzata.
- Dashboard live con 8 viste, incluse pagine di self-analisi (pesi adattivi, trend IMPROVING/DETERIORATING).

## Analisi del Codice
- **Organizzazione**: separazione netta per responsabilità (signals/core/data/database/dashboard/analytics/backtest/tests) — architettura a layer pulita e facilmente navigabile.
- **Manutenibilità**: alta, grazie a dataclass tipizzate per ogni segnale/risultato e a un file di mappa (`codebase_summary.md`) mantenuto aggiornato ad ogni sessione.
- **Modularità**: ottima — ogni analyzer di segnale è indipendente e testabile (`tests/test_signals.py`, `test_risk_engine.py`, `test_backtest.py`); il decision engine è puro/sincrono (nessuna I/O esterna), quindi facilmente unit-testabile.
- **Scalabilità**: il design `asyncio.gather` su 29 mercati scala bene; con il decision engine rule-based non ci sono più colli di bottiglia esterni (rate limit/latenza LLM) sul loop decisionale.
- **Qualità generale**: alta per un progetto solo-developer; presenza di bug fix documentati e regressioni note (es. fix Starlette `TemplateResponse`, fix chiavi protobuf maiuscole SDK) mostra debugging metodico basato su log reali.

## Aspetti di Sicurezza
- Nessuna chiave privata di trading reale attiva (modalità PAPER di default; LIVE richiederebbe configurazione esplicita).
- Decision engine deterministico e privo di dipendenze esterne in runtime: nessuna superficie di prompt injection/output non strutturato da gestire.
- Rischio noto e gestito: ogni `TradeDecision`, comunque generata, passa sempre dal Risk Engine indipendente prima dell'esecuzione.

## Aspetti DevOps
- Configurazione centralizzata via Pydantic-settings con prefisso env (`INJ_`), facilmente containerizzabile.
- Migrazioni DB runtime via `ALTER TABLE` automatico in `Repository.init()` (pragmatico ma poco ortodosso rispetto ad Alembic, già presente come dipendenza ma sotto-utilizzato).
- Logging strutturato (`structlog`).
- Nessuna pipeline CI/CD esplicita; i `tests/` esistono ma non risulta automazione di run.
- Nessun Dockerfile rilevato — deploy manuale su singola macchina.

## Sfide Tecniche Affrontate
- Valutazione costi/benefici di un CLI LLM (Claude Code) come componente real-time in un loop a 60s su 29 mercati, e decisione di sostituirlo con uno scoring deterministico per affidabilità/latenza/costo.
- Progettazione di una formula di scoring esplicita che combini segnali eterogenei (conteggio, z-score, funding, OBI, coerenza voti) in un punteggio comparabile.
- Gestione corretta dello shutdown con `uvicorn` che intercetta `SIGINT` di default.
- Bug di libreria SDK (pyinjective v1.15: chiavi protobuf maiuscole, market deprecato, async_client_v2 vs async_client).
- Bug di enforcement (`max_open_positions` mai applicato → 9 posizioni aperte → kill switch scattato a DD 6.6%).
- Persistenza di stato calcolato (BSR/trend) tra restart, evitando di azzerare metriche basate su deque in-memory.

## Soluzioni Implementate
- Decision engine rule-based sincrono e deterministico (`model="rule_based"`), con scoring trasparente e riproducibile, in sostituzione della chiamata Claude via subprocess.
- `install_signal_handlers=lambda: None` su uvicorn + gestione doppio Ctrl+C custom.
- Fix mirato alle chiavi SDK (`Bids`/`Asks`), market id aggiornato, uso di `async_client_v2`.
- `PaperTradingEngine.open_trades` come fonte di verità per il conteggio posizioni.
- Adaptive scorer con clamp dei pesi e soglia minima di attivazioni (10) per evitare overfitting su pochi trade; pesi applicati solo al ranking, non al gate di approvazione.

## Competenze Dimostrate
Async Python avanzato, progettazione di motori di scoring deterministici e spiegabili, design di risk management indipendente dal motore decisionale, full-stack dashboard (FastAPI/Jinja2/Plotly), debugging di librerie SDK blockchain, statistica applicata (Bayesian updating, Sharpe, profit factor, walk-forward validation), capacità di valutare un'integrazione LLM in produzione e sostituirla quando non è lo strumento giusto.

## Possibili Miglioramenti Futuri
- Containerizzazione (Docker) e CI per i test esistenti.
- Sostituire `ALTER TABLE` ad-hoc con migrazioni Alembic versionate.
- Tuning automatico (grid/Bayesian search) dei coefficienti della formula di scoring oltre ai soli pesi adattivi.
- Monitoring/alerting esterno (oggi solo dashboard locale).

## Valutazione Finale
- **Complessità**: 8/10
- **Valore Professionale**: 8/10
- **Impressione per Recruiter Tecnico**: 8/10 (il decision engine deterministico + adaptive learning, e la storia della sostituzione dell'LLM in produzione, sono un ottimo argomento per ruoli Quant/Backend Developer)

---

# DeFi Multi-Chain Scanner & Executor (Solana/Base)

## Descrizione Generale
Sistema multi-componente che individua memecoin emergenti su Solana e Base, simula/gestisce posizioni in tempo reale e — su Base — esegue swap reali on-chain tramite Uniswap V3/Aerodrome con oracle proprietario. Comprende scanner specializzati per diverse fasi del ciclo di vita di un token (pre-graduazione pump.fun, graduazione, mid/large cap).

## Problema Risolto
I memecoin nascono e muoiono in minuti, con liquidità instabile, dati spesso "stale" o manipolati, e necessità di execution on-chain a basso costo. Il sistema affronta: discovery multi-fonte, filtraggio anti-rug/anti-dump basato su backtest reali, gestione exit automatica (trailing stop adattivo, BSR — "buy/sell ratio" — collapse, liquidity collapse), ed esecuzione reale con gestione di WETH wrap/unwrap, gas reserve, slippage.

## Architettura
- **Orchestratore** (`defi/run.py`): avvia in thread daemon: `LiveEngine`, `PumpGrad`, `PreGrad`, `BasePump`, `defi_optimized`, `gemmeV3`, `solana_executor`, `base_executor`, `midcap_scanner`, `wallet_mirror_bot`. Backoff esponenziale su crash (cap 600s), alert email dopo 5 crash veloci (throttle 6h), thread watchdog ogni 5 min, refresh automatico wallet alpha ogni 24h.
- **Scanner**:
  - `defi_optimized.py` — gem hunter Solana/Base con filtri anti-dump, persistenza storico BSR su disco, diagnostica `cycle_stats.csv`.
  - `pump_graduation_scanner.py` / `pre_grad_monitor.py` — intercettano token pump.fun rispettivamente dopo/prima della graduazione a Raydium, via WebSocket + polling di fallback.
  - `base_pump_scanner.py` — polling factory Uniswap V3/Aerodrome su Base con retry/backoff su RPC down.
  - `midcap_scanner.py` — scanner mid/large cap (BB Squeeze + reversal), async ccxt su 150+ coin, universo da CoinGecko.
  - `binance_futures_scanner.py` — segnali large-cap da Binance Futures.
- **LiveEngine** (`trade_simulator.py`): gestisce 8 "sistemi" di trading con configurazioni TP/SL/trailing/BSR differenziate (defi, pump_grad, pre_grad, mirror, v3, v3_large, v3_midcap, v2-disattivo). Cooldown differenziati per tipo di exit, filtro re-entry su prezzo, filtro anti-dump, sanity check oracle, `_RugWatcher` via WebSocket per fast-check su attività pool.
- **Executor**:
  - `solana_executor.py` — swap reali via Jupiter, RPC Helius/pubblico.
  - `base_executor.py` — oracle on-chain (Uniswap V3 + Aerodrome + Chainlink), gestione WETH wrap/unwrap, gas reserve, `EXIT_ACTIONS` differenziati per tipo di uscita.
  - `bsc_executor.py` — presente ma disattivato.
- **gemmeV3.py**: scanner "Gem Hunter" rule-based a 3 layer (data/logic/scoring) con tier DIAMOND/GOLD/SILVER/BRONZE.

## Tecnologie
Python, web3.py, requests, pandas/numpy, scikit-learn/XGBoost (modelli `.joblib` per scoring), ccxt async, WebSocket (`logsSubscribe` Helius), Jupiter API, Uniswap V3/Aerodrome ABI, Dune Analytics API, DexScreener/GeckoTerminal API, CoinGecko API, GoPlus Security API, SMTP per alerting.

## Funzionalità
- 5+ scanner specializzati con fonti dati diverse (on-chain, Dune, DexScreener, CoinGecko).
- Simulatore di posizioni con 8 strategie configurabili indipendentemente (TP1/TP2/trailing/hard SL/BSR).
- Trailing stop adattivo basato sul picco di profitto raggiunto.
- Esecuzione reale su Base con oracle proprio (no dipendenza da aggregatori per pricing in pool illiquidi).
- Report HTML auto-refresh (dashboard posizioni live, exit quality).
- Sistema di alert email su crash/anomalie.

## Analisi del Codice
- **Organizzazione**: cartelle per dominio (defi/executor/gemme), `codebase_summary.md` come mappa viva mantenuta ad ogni sessione di sviluppo — pratica che compensa l'assenza di una documentazione formale.
- **Manutenibilità**: media-alta; il volume di "fix recenti" documentati indica un codebase che evolve rapidamente tramite iterazione su dati reali, con rischio di accumulo di logica condizionale (molte soglie magiche commentate con la motivazione del backtest, però — buona pratica).
- **Modularità**: buona separazione scanner/simulator/executor, comunicazione via CSV/JSON di stato (basso accoppiamento, ma anche assenza di transazionalità forte).
- **Scalabilità**: orizzontale per nuovi scanner (pattern consolidato), verticale limitata da rate limit delle API gratuite/a pagamento (CoinGecko, Dune, Helius).
- **Qualità generale**: pragmatica e orientata ai risultati; ogni modifica è motivata da un dato di backtest o da un incidente in produzione, segno di un ciclo di sviluppo guidato da metriche reali.

## Aspetti di Sicurezza
- **Audit segreti effettuato** (04/06): rimossi segreti hardcoded da 8+ file (password SMTP, chiavi CoinGecko/CMC), migrati in `executor/.env`, creato `.gitignore` mancante.
- Chiavi private wallet (`SOLANA_PRIVATE_KEY`, `BASE_PRIVATE_KEY`) in `.env` non versionato.
- `EXECUTOR_CHAINS` consente di disabilitare selettivamente l'esecuzione reale (utile per testare in sicurezza).
- Rischio residuo: gestione chiavi private in chiaro su filesystem locale (pattern comune per bot single-user, ma da segnalare in colloquio come limite consapevole).
- `rugcheck.py` integra controlli LP lock/top holder per mitigare rug pull.

## Aspetti DevOps
- Orchestrazione multi-processo con auto-restart, backoff esponenziale, watchdog dei thread critici, alerting email.
- Stato persistito su JSON/CSV (no DB) — semplice da ispezionare ma senza garanzie ACID.
- Nessuna containerizzazione/CI rilevata.
- Logging su file + email per eventi critici.

## Sfide Tecniche Affrontate
- Dati "stale" da Dune Analytics che generavano segnali ripetuti su token in distribuzione (fix: confronto inflow/buyers tra cicli).
- Bug strutturale nel filtro età token che rendeva impossibile generare segnali per 3 giorni.
- Gestione WETH wrap/unwrap su Base con rischio di nonce conflict e gas.
- Collisione di chiave (`token_symbol=""`) che bloccava cooldown cross-sistema per 8h.
- API pump.fun ufficiale dismessa → fallback a polling DexScreener.

## Soluzioni Implementate
- Persistenza storico BSR su disco con scarto entry >2h.
- Filtri anti-dump e anti-re-entry calibrati su backtest (soglia precisione >60%).
- Retry/backoff su RPC down per `base_pump_scanner` (15s→5min) senza terminare il thread.
- `_RugWatcher` via WebSocket standard (non premium) per fast-check su attività pool.
- Trailing stop adattivo a 3 fasce in base al picco di profitto.

## Competenze Dimostrate
Integrazione blockchain (Solana + EVM), gestione di sistemi long-running con stato persistente, data engineering multi-fonte, risk management quantitativo, debugging di sistemi distribuiti basato su log/CSV, ottimizzazione iterativa guidata da backtest.

## Possibili Miglioramenti Futuri
- Migrazione da CSV/JSON a un DB leggero (SQLite) per atomicità e query.
- Test automatizzati sui filtri (oggi validati solo via backtest manuale).
- Containerizzazione per deploy riproducibile.
- Astrazione comune per gli executor multi-chain (riduzione duplicazione tra solana/base/bsc executor).

## Valutazione Finale
- **Complessità**: 8/10
- **Valore Professionale**: 8/10
- **Impressione per Recruiter Tecnico**: 8/10

---

# Wallet Mirror & Alpha Finder

## Descrizione Generale
Sottosistema che identifica wallet "smart money" su Solana analizzando i compratori early dei token vincenti, li classifica con un ranking anti-spam, e ne monitora in tempo reale l'attività per generare segnali di mirroring e alert di confluenza.

## Problema Risolto
Su Solana, copiare le mosse di trader profittevoli è un edge riconosciuto, ma identificare wallet genuinamente "alpha" (e non bot di sniping/spray) e monitorarli in tempo reale senza accesso a endpoint premium è non banale.

## Architettura
- **`wallet_alpha_finder.py`**: seed win-only da `live_trades.csv` (join su `signal_id`), paginazione firme via Helius fino al timestamp del segnale (per trovare i veri compratori "early"), valorizzazione buy in SOL nativo/wSOL, dedup `(wallet, mint)`, penalità su `avg_rank` (>300 → ×0.5, >100 → ×0.8) per filtrare bot bot-spray, output `alpha_wallets.json` (top 30).
- **`wallet_mirror_bot.py`**: riscritto da `transactionSubscribe` (Atlas, piano premium → 403) a `logsSubscribe` standard + fetch Enhanced Transactions API on-trigger. Dedup mint con TTL 6h. Rileva sia buy che sell (anche pagati in SOL nativo). Scrive `wallet_events.csv` (storico completo, anche eventi scartati). Calcola **confluenza cross-wallet**: ≥2 alpha wallet sullo stesso mint in 6h → `pump_probability` 0.80 + 0.05/wallet (cap 0.95). Alert "smart money in uscita" se un alpha vende un token segnalato di recente. Rilevamento "risveglio" dopo ≥30gg di inattività (persistito in `mirror_state.json`).
- **Integrazione**: `trade_simulator.py` annota `smart_money=N` sui segnali Solana non-mirror (solo annotazione, non ancora filtro — in attesa di backtest).
- **Refresh automatico**: `run.py` rigenera `alpha_wallets.json` ogni 7 giorni.

## Tecnologie
Python, WebSocket (Helius `logsSubscribe`), Helius Enhanced Transactions API, CSV come bus dati tra componenti, JSON per stato persistente.

## Funzionalità
- Discovery wallet alpha basata su performance reale del proprio sistema di trading (non su euristiche generiche).
- Penalizzazione anti bot-spray basata su `avg_rank`.
- Monitoraggio real-time multi-wallet con dedup e TTL.
- Segnali di confluenza cross-wallet con probabilità calibrata.
- Alert Telegram dedicato per eventi whale (buy ≥$500, confluenza, risveglio, sell post-segnale).

## Analisi del Codice
- **Organizzazione**: due moduli con responsabilità chiare (discovery offline vs monitoring real-time), entrambi orchestrati da `run.py`.
- **Manutenibilità**: buona — riscrittura recente ben documentata con motivazioni (passaggio da endpoint premium a standard).
- **Modularità**: alta, integrazione con il resto del sistema tramite file CSV/JSON (`mirror_signals.csv`, `wallet_events.csv`, `mirror_state.json`).
- **Scalabilità**: limitata dal numero di wallet monitorabili via WebSocket standard e dai rate limit Helius; il design TTL/dedup mitiga il carico.
- **Qualità generale**: buona, con attenzione esplicita a evitare overfitting (l'annotazione `smart_money` non diventa filtro finché non validata).

## Aspetti di Sicurezza
- Nessuna chiave privata coinvolta nel finder/mirror in sé (solo lettura on-chain).
- `DRY_RUN` attivo di default per il mirror bot — nessuna esecuzione automatica non supervisionata.
- Avvio condizionato all'esistenza di `alpha_wallets.json` e gestione esplicita di `SystemExit` se il piano API nega le subscription (no restart-loop infinito).

## Aspetti DevOps
- Componente 8 dell'orchestratore `run.py`, con flag `--no-mirror`.
- Refresh schedulato (24h check, rigenerazione se >7gg).
- Logging dedicato per eventi di confluenza (🔥) e smart money (🐋).

## Sfide Tecniche Affrontate
- Endpoint premium (Atlas) non disponibile sul piano corrente → 403 sistematico.
- Identificare i "veri" compratori early richiede paginazione delle firme oltre le ultime 200 (limite di default).
- Distinguere wallet alpha genuini da bot di sniping (pattern di acquisto su centinaia di token).
- Persistenza dello stato "ultimo visto" per rilevare risvegli dopo lunga inattività.

## Soluzioni Implementate
- Riscrittura completa su `logsSubscribe` standard + fetch on-trigger (pattern riutilizzato anche in `_RugWatcher`).
- Paginazione firme con parametro `before` fino al timestamp del segnale.
- Penalità moltiplicative su `avg_rank` per neutralizzare bot-spray.
- TTL 6h su dedup mint per bilanciare reattività e rumore.

## Competenze Dimostrate
Reverse engineering di API blockchain, progettazione di euristiche anti-abuso, sistemi event-driven via WebSocket, data join cross-sistema (segnali ↔ trade ↔ wallet), disciplina nel non promuovere feature non validate a filtri di produzione.

## Possibili Miglioramenti Futuri
- Backtest dell'annotazione `smart_money` per validarne il potere predittivo.
- Persistenza su DB invece di JSON/CSV per query storiche più ricche.
- Arricchimento storico wallet (oggi non disponibile via API Helius free).

## Valutazione Finale
- **Complessità**: 7/10
- **Valore Professionale**: 7/10
- **Impressione per Recruiter Tecnico**: 8/10 (ottimo per ruoli legati a blockchain analytics/on-chain intelligence)

---

# BTC Structural Bot (Bitget Futures)

## Descrizione Generale
Bot di trading trend-following su BTCUSDT (futures Bitget, live + demo) basato su analisi multi-timeframe (4h/1h/5m), filtri di trend (EMA/ADX), supporti/resistenze dinamici e statici, con risk management a circuit breaker e R:R minimo dinamico fee-aware.

## Problema Risolto
Le versioni precedenti del bot si basavano su previsioni ML di direzione (oscillazione 46-52%, sostanzialmente rumore) e su mean-reversion in mercati range con ADX basso, causando serie di stop loss consecutivi. Il nuovo approccio elimina la previsione ML, fa trading **solo** quando 4h e 1h concordano sul bias, ed entra solo su pullback strutturali verso EMA20.

## Architettura
- **`structural_bot.py`**:
  - `detect_sr_levels(df_4h)` — pivot high/low multi-timeframe (1W+1D+4h+1h), merge livelli entro 1.2%, minimo 2 tocchi; livelli statici aggiuntivi (ATH storici BTC).
  - `bounce_signal(df_5m, df_1h, sr_levels, funding, fng)` — segnale di entrata con filtri S/R (blocco se troppo vicino a supporto/resistenza opposta), TP "snappato" a livelli S/R ± 0.5×ATR.
  - `calculate_trade(...)` — gate fee-aware: entra solo se ADX≥40 e bias 2h/4h allineati (R:R dinamico minimo 4, calibrato su backtest 91 trade con fee 0.12% round-trip).
  - `check_open_trade` — gestione posizione aperta: break-even quando il prezzo supera un S/R forte a favore, trailing dimezzato vicino a S/R opposto, partial close 50% su S/R forte prima di TP1.
  - Circuit breaker: 3 perdite consecutive → pausa 60 candele (5h).
  - Lock file `fcntl` in `run()` per impedire doppie istanze (causa di trade duplicati in passato).
  - Capital sync con anti-deadlock: delta anomalo stabile ±10% per 6 cicli consecutivi → accettato come saldo reale; hook per registrare P&L di posizioni orfane chiuse dall'exchange.
- **`bitget_futures_executor.py`**: executor unificato live (`BTC/USDT:USDT`, `productType=USDT-FUTURES`) e demo (`SBTC/USDT:USDT`, `SUSDT-FUTURES`), stesse chiavi API, `productType` dinamico in tutte le chiamate.
- **`run_demo.py`**: launcher aggressivo su demo (risk 15%, ADX_MIN=20, cooldown 30 barre, max 4 perdite consecutive), stato separato in `reports_demo/`.
- Altri executor presenti ma secondari: Bybit, Binance Futures/Spot.

## Tecnologie
Python, pandas, ccxt/API REST Bitget+Binance, indicatori tecnici custom (EMA/RSI/ADX/ATR), `fcntl` per locking, `alternative.me` (Fear & Greed Index, cache 1h), Binance funding rate (cache 15min).

## Funzionalità
- Rilevamento S/R multi-timeframe con livelli statici storici.
- Entry filtrata da allineamento multi-timeframe + gate ADX/R:R fee-aware.
- Gestione posizione avanzata (break-even dinamico, trailing adattivo, partial close).
- Circuit breaker su perdite consecutive.
- Modalità demo separata per testare configurazioni aggressive senza rischio.
- Dashboard HTML con prezzi di entrata/uscita.

## Analisi del Codice
- **Organizzazione**: file singolo ben strutturato con sezioni di configurazione commentate e motivate da backtest.
- **Manutenibilità**: alta — ogni parametro ha un commento con la storia del tuning (es. "alzato 5%→7%... PF=1.92 su 49 trade").
- **Modularità**: discreta — executor separato dalla logica di segnale, ma `structural_bot.py` resta un file monolitico di logica di trading.
- **Scalabilità**: non è un requisito (single-asset, single-instance by design, rinforzato dal lock file).
- **Qualità generale**: molto buona; il codice mostra un processo di sviluppo guidato da backtest quantitativi con soglie esplicite e giustificate (rr=2 vs rr=4 → -0.43$ vs +0.36$/trade).

## Aspetti di Sicurezza
- Bug critico risolto (10/06): `on_close` invertiva il side in hedge mode, causando errore "22002 No position to close" su **ogni** chiusura manuale dalla nascita del bot — le posizioni venivano chiuse solo dagli SL/TP nativi dell'exchange, con 4 casi di posizioni orfane documentati. Fix: side corretto + retry ×3 con verifica post-close + guard anti-stacking.
- Capital sync leggeva `available` invece di `equity`, causando un loop di "delta anomalo ignorato" e blocco operativo con margine bloccato. Fix: `_get_balance(equity=True)`.
- Chiavi API Bitget (live/demo) gestite via env, separate per ambiente.

## Aspetti DevOps
- Lock file per garantire singola istanza.
- Stato persistito su JSON (`state.json` live, `demo_state.json` demo) in cartelle separate.
- Logging su file rotante (`structural_bot.log`).

## Sfide Tecniche Affrontate
- Bug di chiusura posizione mai funzionante in produzione, mascherato dal fatto che gli SL/TP nativi chiudevano comunque le posizioni nella maggior parte dei casi.
- Deadlock del capital sync con margine bloccato da posizione aperta.
- Trade duplicati da doppia istanza del processo.
- Calibrazione R:R minimo che tenga conto delle fee (30% del rischio con capitale ridotto).

## Soluzioni Implementate
- Fix del side in hedge mode + retry + stato executor non azzerato su fallimento.
- `_get_balance(equity=True)` per il sync capitale, `equity=False` per il margine disponibile.
- Anti-deadlock su delta stabile ±10% per 6 cicli.
- Lock file `fcntl.LOCK_EX` in `run()`.
- Gate `MIN_DYNAMIC_RR=4.0` validato su 91 trade storici.

## Competenze Dimostrate
Analisi tecnica multi-timeframe, debugging di un bug "silenzioso" critico in produzione tramite analisi di log storici, gestione hedge mode su exchange derivati, risk management con circuit breaker, validazione quantitativa (profit factor per scenario R:R).

## Possibili Miglioramenti Futuri
- Test automatizzati per la logica di rilevamento S/R e per `on_close` (il bug è rimasto invisibile per mesi).
- Estrazione della logica di segnale in moduli separati per favorire test unitari.
- Alerting esterno (oggi solo log file).

## Valutazione Finale
- **Complessità**: 7/10
- **Valore Professionale**: 7/10
- **Impressione per Recruiter Tecnico**: 7/10 (la storia del bug fix è un ottimo esempio di debugging per un colloquio)

---

# Telegram Signal SaaS (bot_telegram)

## Descrizione Generale
Prodotto SaaS completo che distribuisce in tempo reale i segnali generati dagli scanner (DeFi, mirror, ecc.) via Telegram, con tier di abbonamento (Free/Premium/VIP), billing on-chain in USDC, sistema di referral e landing page pubblica auto-generata e auto-deployata.

## Problema Risolto
Monetizzare il flusso di segnali generato dagli altri sistemi senza toccarne il core (processo completamente isolato, accesso **read-only** ai CSV) e senza dover gestire un sistema di pagamento centralizzato (PayPal/Stripe) — usando verifica diretta di transazioni on-chain.

## Architettura
- **`config.py`**: env condivisa con `executor/.env` per RPC, mappa `SIGNAL_FILES → sistema`, routing canali (FREE/PREMIUM/VIP).
- **`csv_tail.py`**: tail incrementale dei CSV con offset-byte persistente (anti-repost, skip backlog all'avvio).
- **`formatter.py`**: `format_full` (Premium, con prezzi/BSR/liquidità) vs `format_teaser` (Free, ritardato 15 min, mostra prezzo "storico" come leva di upsell).
- **`telegram_api.py`**: wrapper REST con retry su 429.
- **`publisher.py`**: daemon principale — full feed su Premium/VIP, teaser ritardato su Free; tail di `wallet_events.csv` per alert whale su Premium (buy ≥$500, confluenza ≥2, risveglio).
- **`bot.py`**: comandi utente (`/start /plans /subscribe /status /referral`) e admin (`/grant /stats /broadcast`).
- **`subscriptions.py`**: store JSON di abbonati (tier, scadenza, referral).
- **`payments.py`**: genera invoice USDC a importo univoco (es. 49.74), monitora on-chain (Base + Solana) il transfer in entrata, attiva l'accesso automaticamente.
- **`track_record.py`**: calcola P&L da `live_trades.csv`, pubblica recap performance sul canale Free, alimenta `state/stats.json`.
- **`landing.py`**: genera `landing/index.html` (dark theme) con statistiche live; se configurato, auto-commit + push su un repo GitHub Pages separato → sito pubblico aggiornato ogni 24h.
- **`run_bot.py`**: orchestratore thread daemon con flag per disabilitare singoli componenti e gating su scadenze abbonamento.

## Tecnologie
Python, Telegram Bot API (via `requests`), web3/RPC Solana+Base per verifica pagamenti, JSON store atomico, generazione HTML statico, Git automation (auto-commit/push), GitHub Pages.

## Funzionalità
- Distribuzione segnali differenziata per tier con ritardo configurabile.
- Sistema di abbonamento con scadenze e gating automatico.
- Pagamenti crypto trustless via importo-invoice univoco (no intermediari).
- Referral system.
- Alert "whale" basati sul wallet mirror system.
- Landing page marketing auto-aggiornata e auto-pubblicata.
- Comandi admin per gestione manuale abbonamenti durante la fase di validazione pagamenti.

## Analisi del Codice
- **Organizzazione**: ottima separazione per responsabilità (10 moduli, ognuno con un compito singolo e ben documentato in README/DESIGN).
- **Manutenibilità**: alta — README e DESIGN.md dedicati, setup documentato passo-passo.
- **Modularità**: eccellente — processo isolato dal core trading, comunicazione solo in lettura su CSV; nessun rischio di side-effect sul sistema di trading.
- **Scalabilità**: adeguata al caso d'uso (singolo operatore, pochi canali); il pattern publisher/tail scala linearmente con il numero di CSV monitorati.
- **Qualità generale**: molto buona; il design "read-only sul core, processo isolato" è una scelta architetturale matura per un side-project che evolve rapidamente.

## Aspetti di Sicurezza
- Verifica pagamenti basata su importo univoco per invoice — mitiga il rischio di doppia spesa/confusione tra pagamenti, ma richiede polling on-chain accurato (rischio: race condition tra invoice scaduta e pagamento tardivo, non documentato).
- Path manuale `/grant` riservato ad admin come fallback finché il flusso pagamenti non è validato in produzione — approccio prudente.
- Credenziali (token bot, wallet di pagamento) in `.env` non versionato, con `.env.example` di riferimento.
- Auto-push su repo GitHub separato: da verificare che le credenziali Git usate abbiano permessi minimi (solo quel repo).

## Aspetti DevOps
- Deploy statico automatizzato su GitHub Pages via auto-commit/push schedulato (24h).
- Stato runtime isolato in `state/` (gitignored): offsets, subscribers, invoices, stats.
- Avvio flessibile via flag CLI (`--no-payments --no-track`, ecc.) per testare componenti singolarmente.

## Sfide Tecniche Affrontate
- Evitare repost di segnali storici al riavvio (offset persistente byte-based).
- Bilanciare incentivo all'upgrade (teaser Free) senza dare informazioni sufficienti per operare gratuitamente.
- Pagamenti crypto senza un payment processor centralizzato.
- Automazione del deploy di un sito statico da un processo Python long-running.

## Soluzioni Implementate
- `csv_tail.py` con offset persistente per tailing incrementale sicuro.
- Importo invoice a centesimi univoci per matching automatico dei pagamenti.
- `landing.py` con generazione + git automation condizionata a una variabile d'ambiente.

## Competenze Dimostrate
Product thinking (tier, pricing, referral, marketing automation), integrazione blockchain per pagamenti, progettazione di sistemi a basso accoppiamento, automazione end-to-end (dal dato grezzo alla pagina pubblica).

## Possibili Miglioramenti Futuri
- Migrazione da JSON store a DB per gli abbonati (concorrenza, query).
- Test del flusso pagamenti in produzione (oggi gestito manualmente via `/grant`).
- Gestione esplicita di invoice scadute/duplicate.

## Valutazione Finale
- **Complessità**: 6/10
- **Valore Professionale**: 8/10 (è l'unico progetto con un chiaro modello di business end-to-end)
- **Impressione per Recruiter Tecnico**: 7/10

---

# Gem Hunter (gemmeV3) e Midcap Scanner

## Descrizione Generale
Due scanner complementari: `gemmeV3.py` è un sistema rule-based (no ML) che aggrega 7 fonti dati per assegnare a ogni token un tier (DIAMOND/GOLD/SILVER/BRONZE); `midcap_scanner.py` è uno scanner async per mid/large cap CEX basato su pattern di compressione di volatilità (BB Squeeze).

## Problema Risolto
- **gemmeV3**: valutare in modo deterministico e ripetibile la "qualità" di un nuovo token combinando smart-money flow (Dune), prezzo/liquidità real-time (DexScreener), sentiment social, listing CEX, sicurezza del contratto (honeypot/tasse/LP lock) e concentrazione holder.
- **midcap_scanner**: trovare setup di breakout pre-movimento su un universo di 800 coin CoinGecko, con throughput sufficiente (150+ coin in ~10s) per essere utile in un ciclo di 8h.

## Architettura
- **gemmeV3**: pipeline a 3 layer — DATA (Dune, DexScreener, SocialAnalyzer via Nitter, CoinGecko, GoPlus, Solscan/BSCScan) → LOGIC (`GemFilter`, gate hard su mcap/liq/età/wallet/BSR + filtro anti-dump) → SCORING (`RuleScorer` deterministico → tier). `detect_sr_levels` condiviso con `structural_bot`. Cache trending/midcap CoinGecko con TTL ridotti (4h→2h, 3h→1h) per reattività.
- **midcap_scanner**: `fetch_coingecko_universe()` (8 pagine, top 800), `fetch_all_ohlcv()` async con semaforo 20, `analyze_coin()` → score 0-100 (squeeze intensity 25, durata 15, espansione 15, lean 8, divergenza RSI 10, volume 7, EMA 15, bonus breakout 5), `enrich_fundamentals()` sui top candidati. Penalità calibrate su backtest n=28: `change_7d>150% → -12`, `adx>55 → -5`.

## Tecnologie
Python, requests, ccxt async, numpy, Dune Analytics API (query v4), DexScreener/GeckoTerminal, CoinGecko API, GoPlus Security API, Nitter (scraping social, fallback se non disponibile), scikit-learn/XGBoost (modelli `.joblib` storici).

## Funzionalità
- Classificazione token in 4 tier con punteggio ponderato trasparente (no black-box).
- Filtri di sicurezza on-chain (honeypot, tasse, LP lock, concentrazione holder).
- Scanner CEX parallelo su centinaia di asset con arricchimento selettivo (per limitare le chiamate `/coins/{id}`).
- Penalità di scoring validate empiricamente (9/9 loss bloccate, 4/19 win sacrificate su token già estremi).

## Analisi del Codice
- **Organizzazione**: gemmeV3 è un file monolitico ma con sezioni a layer ben delimitate e ampia documentazione inline.
- **Manutenibilità**: media — file molto grande (multi-fonte), ma ogni filtro è motivato e tracciabile.
- **Modularità**: discreta, classi separate per fetcher (`CoinGeckoTrendingFetcher`, `CoinGeckoMidCapFetcher`, `SocialAnalyzer`).
- **Scalabilità**: midcap_scanner usa async/semafori per gestire centinaia di richieste; gemmeV3 limitato dai rate limit delle API gratuite/free-tier.
- **Qualità generale**: buona, con un esplicito principio di validazione ("non promuovere un filtro senza controllare win bloccati vs loss evitate, soglia precisione >60%").

## Aspetti di Sicurezza
- GoPlus Security integrato per rilevare honeypot/tasse/LP non locked prima di considerare un token.
- Holder concentration check per rischio rug pull.
- Nessuna chiave privata coinvolta (solo lettura dati).

## Aspetti DevOps
- Cache con TTL configurabili per limitare consumo quota API a pagamento (CoinGecko Demo ~4200/10k call/mese).
- Esecuzione come thread del solito orchestratore `run.py`, con flag `--no-midcap`.

## Sfide Tecniche Affrontate
- API pump.fun ufficiale dismessa (nessun fix possibile, solo fallback).
- Bilanciare reattività (TTL cache) e costo API.
- Evitare overfitting sui filtri con n piccoli (n=28 per midcap, da rivalidare).

## Soluzioni Implementate
- Fallback a DexScreener per dati pump.fun.
- TTL ridotti su cache trending/midcap.
- Penalità di score calibrate e documentate con i numeri del backtest.

## Competenze Dimostrate
Aggregazione dati multi-API, scoring rule-based interpretabile, async I/O ad alto throughput, disciplina di validazione statistica.

## Possibili Miglioramenti Futuri
- Rivalidazione delle penalità midcap con n maggiore.
- Refactoring di gemmeV3 in moduli più piccoli.
- Sostituire scraping Nitter (fragile) con fonte più stabile.

## Valutazione Finale
- **Complessità**: 6/10
- **Valore Professionale**: 6/10
- **Impressione per Recruiter Tecnico**: 6/10

---

# Orchestrazione e Infrastruttura (run.py + pratiche DevOps trasversali)

## Descrizione Generale
Layer trasversale che governa l'avvio, la resilienza e l'osservabilità di tutti i componenti del monorepo: `defi/run.py` (scanner/executor DeFi), `bot_telegram/run_bot.py` (SaaS Telegram), `injective_autopilot/main.py` (autopilot rule-based).

## Problema Risolto
Eseguire 8-10 componenti indipendenti come thread daemon su una singola macchina, con resilienza a crash, visibilità su componenti "morti silenziosamente" e gestione sicura dello shutdown.

## Architettura
- `_start_component`: backoff esponenziale (raddoppia su crash <10min, cap 600s, reset dopo uptime sano).
- `_send_alert`: notifica email via SMTP con throttle 6h per soggetto, per evitare spam su crash ripetuti.
- `_thread_watchdog_loop`: verifica ogni 5 min lo stato dei thread critici, solo alert (no auto-restart per evitare duplicazione di thread vivi).
- `_alpha_refresh_loop`: task schedulato (24h) con condizione di rigenerazione (>7gg).
- Flag CLI granulari (`--no-solana`, `--no-base`, `--no-midcap`, `--no-mirror`, `--no-payments`, `--no-track`, ecc.) per avviare sottoinsiemi in test.
- Gestione doppio Ctrl+C con timeout in `injective_autopilot/main.py`.

## Tecnologie
Python `threading`, SMTP, `dotenv` (caricamento `.env` pre-import per risolvere variabili a import-time), logging.

## Analisi del Codice
- **Organizzazione/Manutenibilità**: pattern coerente replicato su 3 orchestratori, segno di una "libreria mentale" di best practice consolidata dal developer.
- **Scalabilità**: adatta a single-host; non c'è orchestrazione multi-macchina (no Kubernetes/Nomad), ma non è un requisito per il caso d'uso.

## Aspetti DevOps
- Alerting email come canale di monitoring primario (assenza di Prometheus/Grafana — punto di miglioramento).
- Nessuna containerizzazione: dipendenze gestite via `venv` + `requirements.txt` per modulo.

## Competenze Dimostrate
System design per resilienza (backoff, watchdog, circuit breaker a livello di processo), gestione del ciclo di vita di processi long-running, alerting operativo.

## Possibili Miglioramenti Futuri
- Centralizzare metriche/log (es. Loki/Grafana o anche solo un dashboard unico).
- Containerizzare ogni componente per isolamento delle dipendenze.
- Health-check HTTP invece di solo controllo thread interno.

## Valutazione Finale
- **Complessità**: 6/10
- **Valore Professionale**: 7/10 (ottimo per ruoli Automation/DevOps Engineer)
- **Impressione per Recruiter Tecnico**: 7/10
