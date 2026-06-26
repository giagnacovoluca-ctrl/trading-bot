# Algorithmic Trading System — DeFi / Perps / Futures

A production-grade, multi-chain algorithmic trading system built in Python. Covers the full stack: signal discovery, real-time position management, on-chain execution, risk management, and a self-calibrating decision engine. Not a demo — it manages real capital across three asset classes.

---

## Systems

### 1. Injective Autopilot (`injective_autopilot/`)
Autonomous trading agent for **perpetual futures on Injective Protocol** (Cosmos SDK). Monitors 29 markets in parallel, generates quantitative signals, and routes them through a deterministic rule-based scoring engine validated by an independent risk engine.

**Architecture:**
- **Sentinel** (`core/sentinel.py`) — `asyncio.gather` loop over 29 markets every 60s. Computes orderbook imbalance (OBI), CVD divergence, funding z-score, OI divergence, volatility regime, statistical anomalies. Fires a trigger on ≥2 Tier-A/B signals or 1 Tier-S signal (extreme funding / |z|>2.5).
- **Decision Engine** (`core/decision_engine.py`) — deterministic scoring formula: base `0.40 + (signal_count−2)×0.10`, bonuses for vote margin, |z-score|, |funding z-score|, OBI. Rejects MIXED-direction signals and excessive spread. `decide_batch()` ranks candidates by `score × adaptive_weight` and approves up to `max_open_positions − current_positions`.
- **Risk Engine** (`core/risk_engine.py`) — kill switch on daily/weekly drawdown, margin checks, fee-aware R:R calculation, position sizing.
- **Adaptive Learning** (`analytics/adaptive_scorer.py`) — Bayesian updating (Beta prior 2,2) + EWMA on a rolling 50-trade window. Weights applied only to candidate ranking, not to the approval gate — avoids overfitting the live decision flow.
- **Backtest Engine** (`backtest/engine.py`) — walk-forward 70/30 split, live-gate check (≥500 trades, PF>1.5, Sharpe>1.5, max DD<20%).
- **Dashboard** — FastAPI + Jinja2 + Plotly, 8 views: overview, performance, trade journal, signal analytics, risk, learning/adaptive weights, market analytics.
- **Database** — SQLAlchemy 2.0 async + aiosqlite. Tables: `trades`, `signals`, `ai_decisions`, `margin_snapshots`, `trade_postmortems`, `signal_weight_snapshots`.

> **Engineering note**: the first prototype used Claude (LLM) as decision engine via subprocess. Replaced with the rule-based scorer to eliminate latency, cost, and non-determinism in a 60s loop over 29 markets — while keeping the adaptive learning layer for signal weight calibration.

---

### 2. DeFi Multi-Chain Scanner & Executor (`defi/`, `executor/`, `gemme/`)
Finds emerging tokens on **Solana and Base**, manages positions in real time, and executes real on-chain swaps. Covers the full token lifecycle from bonding-curve pre-graduation to mid/large cap.

**Components:**
- `defi/defi_optimized.py` — gem hunter with anti-dump filters, BSR (buy/sell ratio) persistence, cycle diagnostics.
- `defi/pump_graduation_scanner.py` / `pre_grad_monitor.py` — WebSocket + polling for pump.fun graduation events.
- `gemme/gemmeV3.py` — multi-source token scorer (Dune Analytics, DexScreener, GoPlus Security, CoinGecko). Classifies tokens into DIAMOND/GOLD/SILVER/BRONZE tiers using 7 data sources.
- `defi/midcap_scanner.py` — Bollinger Band Squeeze scanner across 150+ coins via async ccxt, universe from CoinGecko.
- `defi/trade_simulator.py` (`LiveEngine`) — central position manager. Handles entry/exit routing, trailing stop (adaptive ATR), BSR-collapse exit, liquidity-collapse exit, TP1/TP2 ladder. Routes signals to system buckets (`pump_grad`, `v3_large`, `midcap`, etc.) based on quality gates.
- `executor/solana_executor.py` — real swaps via **Jupiter API v6** (quote → swap → confirm). Includes entry-drop circuit breaker, rugcheck validation, price impact guard.
- `executor/base_executor.py` — real swaps via **Uniswap V3 / Aerodrome** on Base with on-chain oracle (TWAP), WETH wrap/unwrap, gas reserve management.
- `defi/run.py` — orchestrator: daemon threads with exponential backoff (cap 600s), email alert after 5 fast crashes, watchdog every 5 min, auto-refresh alpha wallets every 24h.

**Quantitative validation:** all filters and exit conditions validated against historical trade data before deployment (backtest on n=1000+ trades, precision threshold >60% before implementation).

---

### 3. Wallet Mirror & Alpha Finder (`executor/wallet_mirror_bot.py`, `executor/wallet_alpha_finder.py`)
Identifies "smart money" wallets on Solana and mirrors their trades in real time.

- `wallet_alpha_finder.py` — seeds from winning trades in `live_trades.csv`, paginates transaction signatures via Helius, reconstructs early-buyer wallets, applies anti-bot-spray penalty (rank>300 → ×0.5), outputs `alpha_wallets.json` (top 30).
- `wallet_mirror_bot.py` — rewrote from `transactionSubscribe` (premium-only Atlas endpoint, 403) to `logsSubscribe` standard RPC + Enhanced Transactions API on trigger. Deduplication with 6h TTL. Cross-wallet confluence: ≥2 alpha wallets on same mint in 6h → `pump_probability = 0.80 + 0.05/wallet` (cap 0.95). "Smart money exit" alert when an alpha sells a recently signalled token.

---

### 4. BTC Structural Bot (`trade/structural_bot.py`)
Trend-following bot on BTCUSDT perpetual futures (Bitget/Bybit/Binance).

Multi-timeframe confirmation (EMA + RSI + ADX), dynamic Support/Resistance detection, adaptive trailing stop (ATR-based), fee-aware R:R minimum. Circuit breaker on consecutive losses, file-lock anti-dual-instance, capital sync with anti-deadlock, validated on 91-trade backtest.

---

### 5. Telegram Signal SaaS (`bot_telegram/`)
End-to-end SaaS for signal distribution via Telegram subscription.

Publisher, Free/Premium/VIP tier management, USDC on-chain payment verification, auto-generated landing page with GitHub Pages CI, weekly recap, teaser with rate limiting, track record display.

---

## Tech Stack

| Layer | Technologies |
|-------|-------------|
| Language | Python 3.11+ |
| Async | `asyncio`, `aiohttp`, `websockets` |
| On-chain (Solana) | `solders`, `base58`, Jupiter API v6, Helius RPC (`logsSubscribe`) |
| On-chain (Base/EVM) | `web3.py`, Uniswap V3, Aerodrome, Alchemy RPC |
| On-chain (Injective) | `pyinjective` async SDK (`async_client_v2`) |
| Exchange APIs | Binance, Bitget (live+demo), Bybit, `ccxt` async |
| Market Data | DexScreener, CoinGecko, GeckoTerminal, Dune Analytics, GoPlus Security |
| Database | SQLite, SQLAlchemy 2.0 async, aiosqlite |
| Dashboard | FastAPI, Jinja2, Plotly, uvicorn |
| Config | Pydantic-settings (`INJ_` env prefix) |
| Messaging | Telegram Bot API, SMTP |
| ML / Stats | Bayesian updating (Beta prior), EWMA, Sharpe, Profit Factor, walk-forward |
| Logging | `structlog` |

---

## Key Engineering Challenges

**Replaced an LLM decision engine in production.** First prototype called Claude via subprocess for signal synthesis. Replaced with a deterministic scoring formula to eliminate 300-500ms latency, unpredictable API cost, and non-determinism in a 60s loop over 29 markets. Kept the adaptive learning layer (Bayesian weight updates on closed trades) for self-calibration.

**Real-time exit management without race conditions.** `LiveEngine` manages 100+ concurrent positions with a single-thread polling loop (30s), atomic CSV writes (tmp + `os.rename`), and state persistence on disk to survive process restarts.

**Anti-honeypot / anti-rug filtering.** Entry circuit breaker that blocks tokens with >10% price drop between signal and execution (memecoin-specific pattern). Rugcheck integration (LP locked, top-holder concentration). 70-symbol honeypot blacklist derived from on-chain patterns on Base.

**Solana smart money discovery without premium endpoints.** Atlas WebSocket `transactionSubscribe` required a paid plan (403 on free). Rewrote to `logsSubscribe` (standard RPC) + on-demand Enhanced Transactions fetch on trigger. Pagination of historical signatures to reconstruct early-buyer wallets from own trade history.

**Quantitative signal validation before deployment.** Every filter is backtested against historical data (n>30 trades, precision threshold >60%) before going live. Profit Factor computed with bootstrap CI. Walk-forward 70/30 split for the injective backtest engine.

---

## Project Structure

```
├── injective_autopilot/     # Perp trading agent (Injective Protocol)
│   ├── core/                # Sentinel, Decision Engine, Risk Engine, Executor
│   ├── signals/             # Orderbook, derivatives, volume, volatility, anomaly
│   ├── analytics/           # Adaptive scorer, performance, postmortem, audit
│   ├── backtest/            # Walk-forward engine, metrics, live-gate
│   ├── dashboard/           # FastAPI + Jinja2 + Plotly (8 views)
│   ├── database/            # SQLAlchemy async models + repository
│   └── tests/               # Signal, risk engine, backtest tests
├── defi/                    # DeFi scanners + live position manager
│   ├── trade_simulator.py   # LiveEngine: central position manager
│   ├── defi_optimized.py    # Gem hunter (Solana/Base)
│   ├── pump_graduation_scanner.py
│   ├── pre_grad_monitor.py
│   ├── midcap_scanner.py    # BB Squeeze, 150+ coins async
│   └── run.py               # Orchestrator with daemon threads + watchdog
├── gemme/                   # Multi-source token scorer (gemmeV3)
├── executor/                # On-chain executors + wallet mirror system
│   ├── solana_executor.py   # Jupiter API v6
│   ├── base_executor.py     # Uniswap V3 / Aerodrome
│   └── wallet_mirror_bot.py # Alpha wallet discovery + mirroring
├── trade/                   # BTC structural bot (Bitget/Bybit/Binance)
└── bot_telegram/            # Telegram Signal SaaS
```

---

## Setup

Each subsystem has its own `.env.example`. Copy and fill in the required keys:

```bash
cp executor/.env.example executor/.env
cp injective_autopilot/.env.example injective_autopilot/.env
cp bot_telegram/.env.example bot_telegram/.env
```

Install dependencies:

```bash
pip install -r requirements.txt
# Injective autopilot has its own venv:
cd injective_autopilot && pip install -r requirements.txt
```

Run:

```bash
# DeFi system (all scanners + executor)
python defi/run.py

# Injective autopilot (PAPER mode by default)
cd injective_autopilot && python main.py --mode PAPER

# Injective backtest
python main.py --mode BACKTEST --backtest-csv path/to/candles.csv
```

---

## License

Private — all rights reserved.
