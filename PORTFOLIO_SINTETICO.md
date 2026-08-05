# Profilo Professionale

Sviluppatore Python autodidatta specializzato in **sistemi di trading algoritmico multi-mercato** (DeFi Solana/Base, futures BTC su Bitget, perpetual su Injective Protocol), **automazione di pipeline dati real-time** e **motori decisionali quantitativi auto-calibranti**. Il portfolio è un monorepo di produzione (non demo/tutorial) che gestisce capitale reale, esegue swap on-chain, espone dashboard web live e include un livello di self-analytics/learning automatico (adaptive scoring Bayesiano). Forte attitudine al debug sistematico, alla gestione del rischio, alla validazione statistica delle strategie prima del deploy e alla scelta pragmatica dello strumento giusto (es. sostituzione di un motore decisionale basato su LLM con uno rule-based deterministico per affidabilità/latenza/costo in produzione).

# Competenze Tecniche

**Python**
- Programmazione avanzata: `asyncio`, `threading` (orchestrazione daemon), `dataclasses`, type hints, decoratori, generatori, `lru_cache`, gestione stato persistente JSON/CSV atomico.

**JavaScript / TypeScript**
- Frontend leggero per dashboard (Jinja2 + JS vanilla per auto-refresh, grafici Plotly), nessun framework SPA — focus su backend.

**AI e Machine Learning**
- scikit-learn / XGBoost (scoring "gem" su token, modelli `.joblib`)
- Esperienza diretta con **integrazione di LLM (Claude CLI) come decision engine** in un sistema di trading live (subprocess, parsing JSON, system prompt engineering, rate limiting) — poi sostituito in produzione da un motore rule-based deterministico per maggiore affidabilità/latenza/costo
- Sistema di **adaptive learning** (Bayesian updating, EWMA, pesi dinamici per segnali) auto-calibrante sui trade chiusi, tuttora in uso

**Trading Algoritmico**
- Strategie multi-asset: scanner memecoin on-chain (Solana/Base), bot strutturale BTC (multi-timeframe EMA/RSI/ADX/S-R), autopilota rule-based su perpetual Injective con risk engine e adaptive scoring
- Risk management: kill switch, drawdown giornaliero/settimanale, position sizing, R:R dinamico fee-aware, backtest walk-forward 70/30 con live-gate (PF>1.5, Sharpe>1.5, ≥500 trade)

**Automazione**
- Orchestratori multi-thread con auto-restart, backoff esponenziale, watchdog, alerting via email
- Bot Telegram SaaS completo (publisher, subscription, billing, landing page auto-deploy)

**API e Integrazioni**
- On-chain: Jupiter (Solana swap), Uniswap V3 / Aerodrome (Base), web3.py, RPC Helius/Alchemy, WebSocket `logsSubscribe`
- Market data: Dune Analytics, DexScreener, GeckoTerminal, CoinGecko, GoPlus Security, Solscan/BSCScan
- Exchange: Binance, Bitget (live + demo futures), Bybit, ccxt async
- Comunicazione: Telegram Bot API, SMTP

**Linux**
- Processi daemon, file locking (`fcntl`), gestione log rotanti, deploy multi-processo su singola macchina

**Database**
- SQLite/`aiosqlite` + SQLAlchemy 2.0 async (modelli, migrazioni runtime), persistenza JSON/CSV per stato trading

**DevOps**
- Gestione segreti (audit e migrazione a `.env`, `.gitignore`), CI implicito via backtest gate, deploy automatico landing page su GitHub Pages (auto-commit/push)

**Cybersecurity**
- Audit e remediation di credenziali hardcoded, gestione chiavi private wallet, verifica pagamenti on-chain anti-frode, separazione processi/permessi

**Cloud**
- GitHub Pages (hosting statico auto-aggiornato)

**Altre tecnologie**
- FastAPI + Jinja2 + uvicorn (dashboard real-time), Pydantic/pydantic-settings, Plotly, structlog, tenacity, ccxt

---

# Progetti Principali

## Injective Autopilot (Trading Agent Rule-Based + Adaptive Learning)
- **Obiettivo**: bot autonomo per perpetual su Injective Protocol con motore decisionale deterministico e auto-calibrante.
- **Tecnologie**: Python asyncio, FastAPI/Jinja2, SQLAlchemy async, pyinjective SDK, Pydantic-settings.
- **Problema risolto**: trasformare segnali quantitativi multi-mercato (29 perpetual) in decisioni di trading esplicite, riproducibili e validate da un risk engine indipendente. Un primo prototipo usava Claude come decision engine: sostituito con uno scoring rule-based per eliminare latenza/costo/non-determinismo della chiamata LLM in produzione, mantenendo però il layer di adaptive learning.
- **Risultato**: sistema PAPER funzionante con dashboard live, kill switch, backtest engine con gate per passare a LIVE, motore di scoring deterministico (formula a punteggio su signal count, votes, z-score, funding, OBI) con pesi auto-calibrati via Bayesian updating sui trade chiusi.
- **Complessità**: Alta.

## DeFi Multi-Chain Scanner & Executor (Solana/Base)
- **Obiettivo**: individuare memecoin emergenti e gestire posizioni reali on-chain in automatico.
- **Tecnologie**: Python, web3.py, Jupiter API, Uniswap V3/Aerodrome, RPC Helius/Alchemy, WebSocket.
- **Problema risolto**: scouting + esecuzione + exit management su mercati ad altissima volatilità con liquidità instabile.
- **Risultato**: sistema multi-thread orchestrato (`run.py`), executor live su Base con oracle on-chain, simulator per Solana, oltre 10 fix critici di produzione documentati.
- **Complessità**: Alta.

## Wallet Mirror & Alpha Finder
- **Obiettivo**: identificare wallet "smart money" e copiarne le mosse in tempo reale.
- **Tecnologie**: Helius WebSocket (`logsSubscribe`), Enhanced Transactions API, ranking euristico anti-spam.
- **Problema risolto**: scouting di alpha wallet redditizi e mirroring/alert in tempo reale con anti-spoofing (penalità su wallet bot-spray).
- **Risultato**: pipeline completa wallet discovery → ranking → mirroring → alert Telegram whale.
- **Complessità**: Alta.

## BTC Structural Bot (Bitget Futures)
- **Obiettivo**: bot trend-following multi-timeframe su BTCUSDT con risk management rigoroso.
- **Tecnologie**: Python, pandas, Binance/Bitget API, EMA/RSI/ADX, rilevamento Supporti/Resistenze.
- **Problema risolto**: evitare segnali rumorosi (mean-reversion in range) puntando solo su trend confermati multi-timeframe con R:R minimo dinamico fee-aware.
- **Risultato**: bot live con circuit breaker, lock anti-doppia istanza, sync capitale con anti-deadlock, validato su backtest a 91 trade.
- **Complessità**: Media-Alta.

## Telegram Signal SaaS
- **Obiettivo**: monetizzare i segnali di trading via abbonamento Telegram.
- **Tecnologie**: Telegram Bot API, USDC on-chain payment verification, generazione HTML statico.
- **Problema risolto**: distribuzione segnali a tier (Free/Premium/VIP), billing automatico via crypto, marketing (landing page auto-pubblicata).
- **Risultato**: prodotto SaaS end-to-end (publisher, billing, referral, landing page con auto-deploy GitHub Pages).
- **Complessità**: Media.

## Gem Hunter (gemmeV3)
- **Obiettivo**: scoring rule-based di nuovi token (no ML) basato su dati on-chain + social + sicurezza contratto.
- **Tecnologie**: Dune Analytics, DexScreener, GoPlus Security, social scraping (Nitter), CoinGecko.
- **Problema risolto**: classificare token in tier (DIAMOND/GOLD/SILVER/BRONZE) aggregando 7 fonti dati eterogenee.
- **Risultato**: pipeline a 3 strati (data/logic/scoring) integrata nel flusso di trading principale.
- **Complessità**: Media.

## Midcap Scanner (BB Squeeze)
- **Obiettivo**: individuare setup di breakout su mid/large cap CEX.
- **Tecnologie**: ccxt async (150+ coin in parallelo), CoinGecko universe, scoring tecnico multi-fattore.
- **Problema risolto**: trovare compressione di volatilità pre-breakout con penalità calibrate su backtest reale (n=28).
- **Risultato**: scanner integrato nel simulator con sistema di trading dedicato ("midcap").
- **Complessità**: Media.

---

# Risultati Più Rilevanti

- **Automazioni**: orchestratore multi-thread (`run.py`) con backoff esponenziale, watchdog, alert email automatici, refresh periodico wallet alpha.
- **Sistemi AI/adaptive**: prototipazione di un decision engine basato su Claude (poi sostituito in produzione da uno scoring rule-based deterministico per latenza/costo/affidabilità); layer di adaptive learning con Bayesian updating sui pesi dei segnali, tuttora in uso.
- **Integrazioni API**: oltre 15 servizi esterni (RPC blockchain, exchange, market data, social, sicurezza smart contract, messaggistica).
- **Sistemi di trading**: 8 strategie attive/configurate su 3 asset class (memecoin DeFi, BTC futures, perpetual Injective).
- **Dashboard**: 2 dashboard web real-time (FastAPI + Jinja2) con auto-refresh, grafici Plotly, pannelli di amministrazione (kill switch reset).
- **Scraping**: aggregazione dati da Dune, DexScreener, CoinGecko, Solscan, GoPlus, Nitter.
- **Tool DevOps**: gestione segreti, `.gitignore` da audit di sicurezza, deploy automatico GitHub Pages.
- **Applicazioni complete**: SaaS Telegram con billing on-chain end-to-end.

---

# Perché Questo Portfolio È Interessante

Non è un insieme di esercizi o tutorial: è un **sistema di produzione che gestisce denaro reale**, con tutti i problemi che questo comporta — gestione errori di rete, race condition su stato persistente, sicurezza delle chiavi, rate limiting di API a pagamento, debug di bug sottili in sistemi long-running (es. bug di chiusura posizioni mai funzionante per mesi, individuato e risolto tramite analisi dei log). Il candidato dimostra capacità di:
- progettare architetture modulari multi-componente che comunicano tramite file/CSV/DB,
- validare ipotesi con backtest e dati storici prima di shippare,
- valutare onestamente i trade-off di un'integrazione LLM in produzione e sapere quando sostituirla con una soluzione deterministica più affidabile, mantenendo comunque un layer di self-learning,
- lavorare in autonomia su uno stack full-stack (on-chain, backend, dashboard, bot di messaggistica, deploy).

È un profilo ideale per ruoli **Automation Engineer, Quant/Backend Python Developer** con forte componente di problem-solving e ownership end-to-end.
