# ARCHITETTURA E MAPPA DEL CODEBASE — injective_autopilot
Aggiornato: 2026-06-10 (sera)

## 🔧 Fix 10/06 sera (serve restart main.py)
- **decision_engine `_build_decision`**: guard ATR freddo — reject se distanza TP
  < max(2×spread, 0.30%). Prima un ATR microscopico (buffer corto) produceva TP
  dentro lo spread → fill all'ask scavalcava il TP → chiusure "TP" istantanee
  con pnl negativo (AAVE tp<fill, -0.69$) che inquinavano il learning layer.
- **risk_engine**: `paper_max_weekly_drawdown_pct=0.15` (settings) usato in
  PAPER/BACKTEST (LIVE resta 10%); `reset_kill_switch()` ora riallinea le
  baseline daily/weekly all'equity corrente (prima: con equity sotto soglia il
  kill si riattivava al primo check — trappola permanente).
- **main.py**: FileHandler ancorato al modulo (prima il log finiva in
  ~/Scrivania/code/ per CWD esterna alla repo).

## ⚡ Layer Analytics & Learning (2026-06-10)

Nuovo pacchetto `analytics/` — il sistema è auto-analizzante:

```
analytics/
├── performance.py       ← pure functions su trade chiusi: signal_ranking,
│                          combo_ranking, market_ranking, hourly/weekday/
│                          vol_regime_analysis, score_bucket_analysis,
│                          win_loss_patterns, basic_stats
├── adaptive_scorer.py   ← AdaptiveScorer: pesi per segnale via Bayesian
│                          updating (Beta prior 2,2) + EWMA expectancy su
│                          rolling window 50. Neutrale sotto 10 attivazioni.
│                          Pesi clampati [0.5, 1.5]. Applicati SOLO al
│                          ranking dei candidati (gate su raw score →
│                          numero di trade invariato).
├── postmortem.py        ← build_postmortem(): R-multiple, hold, MAE/MFE,
│                          contributi segnali, valutazione statistica auto
└── audit.py             ← `python -m analytics.audit`: backfill trade↔segnali,
                           post-mortem mancanti, report completo a console
```

Flusso learning: `PaperTradingEngine._monitoring_loop` → su ogni chiusura
salva trade + post-mortem → `_refresh_adaptive_weights()` ricalcola i pesi
da `get_closed_trades()` → `DecisionEngine.set_signal_weights()` → snapshot
DB ogni 25 trade chiusi (`signal_weight_snapshots`).

Pipeline dati arricchita: `SentinelTrigger.active_signals/signal_values` →
`TradeDecision` → `TradeRecord` → colonna `trades.signal_values` (+ JSON
`active_signals` ora popolato, prima sempre `[]`). MAE/MFE tracciati in
`Executor.monitor_positions` (campionati ogni 30s).

Nuove tabelle: `trade_postmortems`, `signal_weight_snapshots`.
Nuove colonne trades: `signal_values`, `mae_pct`, `mfe_pct` (migrazione
automatica ALTER TABLE in `Repository.init()`).

Dashboard nuove pagine: `/analytics` (ranking segnali/combo/score-coherence,
profili win vs loss), `/markets` (leaderboard mercati + analisi temporale),
`/learning` (pesi correnti, trend IMPROVING/DETERIORATING, grafico evoluzione
pesi, post-mortem). API JSON: `/api/analytics/{signals,markets,learning}`.

NOTA: il decision engine è rule-based deterministico (nessuna chiamata IA).

---

## 1. Albero dei File

```
injective_autopilot/
├── main.py                         ← entrypoint: avvia Sentinel + PaperEngine + Dashboard
├── requirements.txt
├── config/
│   ├── settings.py                 ← Pydantic-settings, prefisso env INJ_
│   └── config.yaml                 ← riferimento parametri (override via env)
├── data/
│   ├── injective_client.py         ← wrappa pyinjective v1.15 (async_client_v2)
│   └── cache.py                    ← TTLCache, RollingBuffer, FundingBuffer, OIBuffer
├── signals/
│   ├── orderbook.py                ← OBI, BidAskPressure, LiquidityVoid, SpreadState
│   ├── derivatives.py              ← FundingSignal, OIDivergenceSignal, FundingDislocation
│   ├── volume.py                   ← CVD (Cumulative Volume Delta), VolumeSurge
│   ├── volatility.py               ← ATR, VolatilityRegime, Bollinger Bands
│   └── anomaly.py                  ← ZScore, RegimeShift, VolBreakout, LiquidationRisk
├── core/
│   ├── sentinel.py                 ← polling loop 60s, compone segnali, trigger Claude
│   ├── decision_engine.py          ← chiama Claude (subprocess o SDK), parsa JSON
│   ├── risk_engine.py              ← position sizing, Kill Switch, net R:R
│   └── executor.py                 ← manda ordini (LIVE) o registra su DB (PAPER)
├── paper_trading/
│   └── engine.py                   ← simula fill, gestisce SL/TP, aggiorna equity
├── backtest/
│   ├── engine.py                   ← walk-forward 70/30, replay candles CSV
│   └── metrics.py                  ← PerformanceMetrics, compute_metrics, check_live_gate
├── database/
│   ├── models.py                   ← SQLAlchemy 2.0: Trade, Signal, AiDecision, MarginSnapshot, ErrorLog
│   └── repository.py               ← Repository (async CRUD su tutte le tabelle)
├── dashboard/
│   ├── app.py                      ← FastAPI + Jinja2, 6 pagine HTML + /api/stats /api/equity
│   ├── templates/                  ← overview, performance, journal, signals, risk, ai_analysis
│   └── static/
└── tests/
    ├── test_signals.py
    ├── test_risk_engine.py
    └── test_backtest.py
```

---

## 2. Flusso di Esecuzione

```
main.py
 └─ Sentinel.run() [loop 60s]
      ├─ InjectiveClient.fetch_orderbook()
      ├─ InjectiveClient.fetch_market_snapshot()
      ├─ segnali: OBI | CVD | funding_zscore | OI_div | vol_regime | anomaly
      ├─ se ≥2 segnali attivi (o Tier S funding da solo):
      │    └─ DecisionEngine.decide(context) → TradeDecision (JSON Claude)
      │         └─ RiskEngine.validate(decision) → position_size, SL, TP
      │              └─ Executor.execute(decision) → PAPER: PaperTradingEngine | LIVE: InjectiveClient
      └─ PaperTradingEngine.tick() [controlla SL/TP sulle posizioni aperte]
```

---

## 3. Moduli Chiave — Firme e Parametri

### `data/injective_client.py` — `InjectiveClient`
- **Market**: INJ/USDC PERP `0x790aee464fbbd02cf4476444554c71d1225f7edfe15e6dc7f874c455fd883d31` (mainnet)
- **SDK**: `pyinjective.async_client_v2.AsyncClient` (v1.15+)
- **Prezzi**: stringhe in unità 1e18 (cosmwasm Dec) → dividi per 1e18
- **Chiavi orderbook SDK**: `"Bids"` / `"Asks"` (maiuscolo, da protobuf MessageToDict)
- **Funding response**: `{"state": {"cumulativeFunding": "...", "lastTimestamp": "..."}}`
- **OI response**: `{"amount": {"balance": "18188..."}}`
- `fetch_orderbook(depth=20)` → `OrderbookSnapshot`
- `fetch_market_snapshot()` → `MarketSnapshot` (mark_price, funding_rate, open_interest)
- `fetch_positions()` → `list[PositionInfo]`
- `create_limit_order(is_buy, price, qty, reduce_only)` → `OrderResult`

### `signals/orderbook.py` — `OrderBookAnalyzer`
- `compute_obi(snap)` → `OBISignal` (value, zscore, is_bullish, is_bearish)
- `compute_bid_ask_pressure(snap)` → `BidAskPressure`
- `find_liquidity_voids(snap, gap_pct=0.003)` → `list[LiquidityVoid]`
- `compute_spread(snap)` → `SpreadState`
- Tier B signal; richiede persist ≥3 ticks

### `signals/derivatives.py` — `DerivativesAnalyzer`
- `compute_funding_signal(funding_buffer)` → `FundingSignal` (zscore, is_extreme)
- `compute_oi_divergence(oi_buf, price_buf)` → `OIDivergenceSignal`
- `compute_net_rr_with_funding(entry, sl, tp, direction, hold_hours, funding_rate)` → float
- `detect_funding_dislocation(funding_buf)` → `FundingDislocation`
- Tier S: funding |zscore| > 2.5 triggera Claude da solo

### `signals/volume.py` — `VolumeAnalyzer`
- `update(trade_event)` → aggiorna CVD rolling
- `compute_cvd_divergence(price_series)` → `CVDState` (divergence_zscore, is_diverging)
- `detect_volume_surge(window=20)` → `VolumeSurge`

### `signals/volatility.py` — `VolatilityAnalyzer`
- `update(high, low, close)` → aggiorna ATR + BB
- `get_atr()` → float
- `get_vol_regime()` → `VolatilityRegime` (ratio, is_expansion, is_contraction)
- `get_bb_state()` → `BBState` (upper, lower, width, squeeze)

### `signals/anomaly.py` — `AnomalyDetector`
- `compute_zscore(series, window)` → `ZScoreSignal`
- `detect_regime_shift(price_series)` → `RegimeShift`
- `detect_vol_breakout(returns)` → `VolBreakout`
- `kl_divergence(p, q)` → float (early-exit se data_range < 1e-12)

### `core/sentinel.py` — `Sentinel` (MULTI-MARKET)
- Scansiona 29 market in parallelo via `asyncio.gather` ogni 60s
- `MarketContext` dataclass: per-market buffers (RollingBuffer, FundingBuffer, OIBuffer) + tutti gli analyzer
- `_tick_all()` → `asyncio.gather(*[_tick_market(ctx) for ctx in markets])`
- Rate limit globale: max `sentinel_max_triggers_per_hour=10` Claude calls/ora (tutti i market)
- Spread filter: skip market se `spread.zscore > 3.0`
- `SentinelTrigger` ora include `ticker: str` oltre a `market_id`

### `core/decision_engine.py` — `DecisionEngine`
- `decide(trigger)` → `TradeDecision` (action, direction, confidence, entry, sl, tp, reasoning)
- Chiama `claude --print --max-turns 1` (subprocess) o `anthropic.AsyncAnthropic` (SDK)
- System prompt forza output JSON-only
- `_parse_response`: strip markdown fences, cerca `{...}` nella risposta
- Soglia minima: `confidence >= claude_min_confidence (0.65)`

### `core/risk_engine.py` — `RiskEngine`
- `validate(decision, current_equity)` → `RiskValidation` (approved, size_usdt, reason)
- Kill Switch: daily DD ≥5% | weekly DD ≥10% | margin ≥80% | errori consecutivi ≥5
- Net R:R check: `net_rr >= min_rr_ratio - 0.01` (tolleranza floating point)
- Gross R:R pre-check: `gross_rr >= min_rr_ratio * 0.85`
- `EquityState`: daily_high, weekly_high, daily_dd_pct, weekly_dd_pct

### `core/executor.py` — `Executor`
- PAPER: scrive su DB + passa a PaperTradingEngine
- LIVE: chiama `InjectiveClient.create_limit_order()`
- `TradeRecord` dataclass: id, direction, entry, sl, tp, size_usdt, mode, ts_open

### `paper_trading/engine.py` — `PaperTradingEngine`
- `tick(current_price)` → chiude posizioni che hanno toccato SL/TP
- Tracking equity virtuale, scrive su DB ogni tick
- `open_position(record)` → aggiunge alla lista posizioni aperte

### `backtest/engine.py` — `BacktestEngine`
- `run(candles, split=0.70)` → `BacktestResult` (in_sample + out_of_sample metrics)
- Walk-forward 70/30 split
- `load_candles_from_csv(path)` → `list[BacktestCandle]`
- `BacktestCandle`: ts, open, high, low, close, volume, funding_rate, open_interest

### `backtest/metrics.py`
- `compute_metrics(pnl_series, equity_curve, trade_directions, initial_capital)` → `PerformanceMetrics`
- `check_live_gate(metrics, n_trades, cfg)` → `(bool, list[str])` — gate per passare a LIVE
- Gate LIVE: ≥500 trade, PF>1.5, Sharpe>1.5, max_dd<20%, giorni_stabili≥30

### `database/models.py`
- `Trade`: id, mode, direction, entry/exit/sl/tp price, size_usdt, pnl_usdt, status, ts_open/close
- `Signal`: ts, signal_type, tier, value, zscore, direction, market_id
- `AiDecision`: ts, prompt_hash, action, confidence, was_approved, outcome_pnl
- `MarginSnapshot`: ts, equity, margin_used_pct, daily_dd_pct, weekly_dd_pct
- `ErrorLog`: ts, component, error_type, message

### `dashboard/app.py`
- Starlette 1.2+: `TemplateResponse(request, "name.html", context_dict)` (NO "request" nel dict)
- Jinja2 filter custom: `timestamp_to_str` registrato su `_templates.env.filters`
- `/` overview | `/performance` | `/journal` | `/signals` | `/risk` | `/ai`
- `/api/stats` → JSON polling | `/api/equity` → serie temporale equity

---

## 4. Configurazione Chiave (`config/settings.py`)

| Param | Default | Note |
|-------|---------|------|
| `mode` | `PAPER` | BACKTEST \| PAPER \| LIVE |
| `network` | `mainnet` | testnet per sviluppo |
| `market_ids` | top 29 PERP | lista IDs, ranked by OI (2026-06-09) |
| `market_id` | property → `market_ids[0]` | primary market per LIVE execution |
| `capital_usdt` | 1000.0 | capitale simulato |
| `max_leverage` | 5.0 | |
| `max_open_positions` | 5 | max trade aperti simultanei (tutti i market) |
| `max_capital_per_market_pct` | 0.20 | max 20% capital per singolo market |
| `min_rr_ratio` | 2.0 | net R:R dopo funding |
| `funding_zscore_threshold` | 2.5 | Tier S trigger |
| `obi_threshold` | 0.60 | |
| `sentinel_max_triggers_per_hour` | 10 | rate limit Claude globale (tutti i market) |
| `max_daily_drawdown_pct` | 0.05 | Kill Switch 5% |

### Market IDs (top 29 per OI)
`PEPE, PUMP, OP, DOGE, TIA, ARB, WIF, S, SEI, INJ, TON, ATOM, TRUMP, DRIFT, HOOD, ADA, SUI, MANTA, HYPE, AVAX, ONDO, SCROLL, SOL, LAYER, TRX, ZRO, TAO, AAVE, ETH`

`MARKET_TICKER: dict[str, str]` → lookup market_id → ticker name (in `config/settings.py`)

---

## 5. Bug Noti e Fix Applicati

- **pyinjective v1.15**: usa `async_client_v2.AsyncClient`, non `async_client`
- **Market deprecato**: `0x17ef...` (INJ/USDT) → usare `0x790a...` (INJ/USDC)
- **Orderbook chiavi**: SDK v1.15 restituisce `"Bids"`/`"Asks"` (maiuscolo)
- **Starlette 1.2**: `TemplateResponse(request, name, ctx)` — il dict NON contiene `"request"`
- **Float slice**: `funding_persistence_hours` deve essere `int()` prima di usarlo come indice
- **KL divergence**: array costante → `data_range < 1e-12` → ritorna 0.0 anziché NaN
- **Net R:R tolerance**: `net_rr >= min_rr_ratio - 0.01` per evitare falsi rifietti da floating point
