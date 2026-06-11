"""
structural_bot.py
=================
Approccio senza ML per BTCUSDT su Binance.

Perché il bot precedente non funzionava:
  - p_long / p_short oscillavano sempre tra 46-52% → rumore puro, non segnale
  - mean-reversion in RANGE con ADX basso: 8 SL consecutivi in un downtrend
  - Hurst ~0.50 su 5m = random walk, nessun modello ML può predire la direzione

Questo bot invece:
  1. Nessuna previsione ML della direzione
  2. Trade SOLO quando 4h e 1h concordano → no conflitti di bias
  3. Solo trend following (no mean-reversion / range entries)
  4. Entry su pullback strutturale verso EMA20 in direzione del trend
  5. R:R minimo 2:1 → profittevole anche con 40% win rate
  6. Circuit breaker: 3 perdite consecutive = pausa 20 candele
  7. Persistenza completa su file JSON + dashboard HTML aggiornata live
"""

import os
import time
import json
import logging
import logging.handlers
import pandas as pd
import requests
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from enum import Enum

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURAZIONE
# ─────────────────────────────────────────────────────────────────────────────

SYMBOL                 = "BTCUSDT"

RISK_PCT               = 0.07     # alzato 5%→7%: tra Kelly/4(6%) e Kelly/2(12%), WR=50% PF=1.92 su 49 trade
MIN_RR                 = 2.0        # reward/risk minimo accettato
MIN_DYNAMIC_RR         = 4.0        # backtest 10/06 (91 trade, fee taker 0.12% rt incluse):
                                    # rr=2.0 → net -0.43$/trade (n=49), rr=3.0 → -0.27$ (n=16),
                                    # rr=4.0 → +0.36$ (n=26). Con SL~2×ATR le fee costano ~30%
                                    # del risk: sotto ADX 40 + 2h/4h allineati l'edge non le paga.
MAX_CONSECUTIVE_LOSSES = 3          # scatta il circuit breaker
COOLDOWN_BARS          = 60         # alzato 20→60: 5h di pausa dopo circuit breaker (invece di 1h40m)
ADX_MIN_TREND          = 24         # alzato 22→24: filtra zone di trend marginale
ATR_MIN_ENTRY          = 65.0       # filtro range compresso: no entry se ATR<65 (evita whipsaw May 31)
ATR_SL_MULT            = 2.0        # alzato 1.5→2.0: su BTC ATR~45, 1.5× = 67pt troppo stretto per il noise
SLIPPAGE_PCT           = 0.0002     # 0.02% slippage stimato

# Support & Resistance
SR_PIVOT_LOOKBACK  = 5      # barre a sx/dx per identificare un pivot high/low
SR_MIN_TOUCHES     = 2      # tocchi minimi per considerare il livello significativo
SR_MERGE_PCT       = 0.012  # fonde livelli entro 1.2% (era 0.8%: troppo granulare su multi-tf)
SR_PROXIMITY_PCT   = 0.015  # blocca entry trend se prezzo <1.5% da un livello S/R opposto
SR_TP_MARGIN_ATR   = 0.5    # TP snappato a livello S/R ± ATR×0.5 (lascia respiro)

# Livelli S/R statici chiave BTC (psicologici + storici) — si fondono con quelli algoritmici
STATIC_SR_LEVELS = [
    {"price": 60_000, "type": "support",    "touches": 3},  # 60k psy — floor psicologico
    {"price": 65_000, "type": "zone",       "touches": 3},  # 65k psy — zona pivot
    {"price": 69_000, "type": "resistance", "touches": 3},  # ATH 2021 — livello storico chiave
    {"price": 73_800, "type": "resistance", "touches": 3},  # ATH pre-halving 2024
    {"price": 80_000, "type": "resistance", "touches": 2},  # 80k psy
    {"price": 88_000, "type": "resistance", "touches": 2},  # 88k key zone
    {"price": 100_000,"type": "resistance", "touches": 4},  # 100k psy — mega livello
    {"price": 108_000,"type": "resistance", "touches": 4},  # ATH 2024
]

# Bounce (contrarian da S/R)
BOUNCE_PROXIMITY_PCT  = 0.015   # entro 1.5% dal livello per trigger bounce
BOUNCE_MIN_TOUCHES    = 2       # livello valido per bounce
BOUNCE_VOLUME_MULT    = 1.4     # spike volume: 1.4× media 20 barre
FUNDING_LONG_THRESH   = -0.0001 # funding ≤ -0.01% → shorts dominanti → long bias
FUNDING_SHORT_THRESH  = 0.0008  # funding ≥ 0.08% → longs overleveraged → short bias
FNG_LONG_THRESH       = 20      # F&G ≤ 20 → paura estrema → contrarian long
FNG_SHORT_THRESH      = 75      # F&G ≥ 75 → greed estrema → contrarian short

# Trailing stop
TRAIL_ACTIVATE_PCT     = 0.90       # era 0.65 — backtest 60gg: PF SHORT 1.55→1.80, R_tot +47% (validato su split 30/30gg)
TRAIL_ATR_DIST         = 0.30       # era 1.0  — trail più stretto una volta armato, lascia correre i trend forti

MIN_LOT_BTC            = 0.001  # lotto minimo Bitget BTCUSDT — trade rifiutati se size < questo valore
INITIAL_CAPITAL        = 21.0   # €20 ≈ $21 USDT su Bitget

# Cartella dove salvare stato, log trade e dati dashboard
# (relativa alla posizione dello script)
REPORTS_DIR = Path(__file__).parent / "reports bot strutturale"

STATE_FILE       = REPORTS_DIR / "state.json"       # stato bot (capitale, open trade, ecc.)
TRADES_LOG_FILE  = REPORTS_DIR / "trades_log.json"  # storico completo trade chiusi
DASHBOARD_DATA   = REPORTS_DIR / "trades_data.js"   # dati per il dashboard HTML


# ─────────────────────────────────────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────────────────────────────────────

class Bias(Enum):
    BULL    =  1
    BEAR    = -1
    NEUTRAL =  0

class Signal(Enum):
    LONG  =  1
    SHORT = -1
    HOLD  =  0


# ─────────────────────────────────────────────────────────────────────────────
# STATO DEL BOT
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BotState:
    capital:             float          = INITIAL_CAPITAL
    open_trade:          Optional[dict] = None
    consecutive_losses:  int            = 0
    cooldown_remaining:  int            = 0
    total_trades:        int            = 0
    wins:                int            = 0
    losses:              int            = 0
    daily_pnl:           float          = 0.0
    last_day:            Optional[str]  = None
    # metriche aggiuntive
    total_win_pnl:       float          = 0.0
    total_loss_pnl:      float          = 0.0
    best_trade:          float          = 0.0
    worst_trade:         float          = 0.0
    bars_since_close:    int            = 99  # barre dall'ultima chiusura (99 = nessuna apertura recente)


# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENZA — salva e carica stato
# ─────────────────────────────────────────────────────────────────────────────

def ensure_reports_dir():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

def save_state(state: BotState):
    """Salva lo stato corrente del bot su disco (per recovery al riavvio)."""
    ensure_reports_dir()
    trade_serializable = None
    if state.open_trade:
        t = dict(state.open_trade)
        t["signal"] = t["signal"].name  # Enum → stringa
        trade_serializable = t

    data = {
        "capital":            state.capital,
        "open_trade":         trade_serializable,
        "consecutive_losses": state.consecutive_losses,
        "cooldown_remaining": state.cooldown_remaining,
        "total_trades":       state.total_trades,
        "wins":               state.wins,
        "losses":             state.losses,
        "daily_pnl":          state.daily_pnl,
        "last_day":           state.last_day,
        "total_win_pnl":      state.total_win_pnl,
        "total_loss_pnl":     state.total_loss_pnl,
        "best_trade":         state.best_trade,
        "worst_trade":        state.worst_trade,
        "bars_since_close":   state.bars_since_close,
        "saved_at":           datetime.now(timezone.utc).isoformat(),
    }
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, STATE_FILE)

def load_state() -> BotState:
    """
    Carica lo stato dal file se esiste.
    Utile per riprendere dopo un riavvio senza perdere il trade aperto.
    """
    if not STATE_FILE.exists():
        logging.info("[State] Nessuno stato salvato — parto da zero.")
        return BotState()

    try:
        data = json.loads(STATE_FILE.read_text())
        state = BotState()
        state.capital            = data.get("capital",            INITIAL_CAPITAL)
        state.consecutive_losses = data.get("consecutive_losses", 0)
        state.cooldown_remaining = data.get("cooldown_remaining", 0)
        state.total_trades       = data.get("total_trades",       0)
        state.wins               = data.get("wins",               0)
        state.losses             = data.get("losses",             0)
        state.daily_pnl          = data.get("daily_pnl",          0.0)
        state.last_day           = data.get("last_day",           None)
        state.total_win_pnl      = data.get("total_win_pnl",      0.0)
        state.total_loss_pnl     = data.get("total_loss_pnl",     0.0)
        state.best_trade         = data.get("best_trade",         0.0)
        state.worst_trade        = data.get("worst_trade",        0.0)
        state.bars_since_close   = data.get("bars_since_close",   99)

        # Ripristina trade aperto
        raw_trade = data.get("open_trade")
        if raw_trade:
            raw_trade["signal"] = Signal[raw_trade["signal"]]
            state.open_trade = raw_trade
            logging.info(
                f"[State] Trade aperto recuperato: "
                f"{raw_trade['signal'].name} entry={raw_trade['entry']}"
            )

        # Riconcilia wins/losses/total_trades dal log storico (sopravvivono ai reset dello stato)
        _reconcile_counters(state)

        logging.info(
            f"[State] Stato caricato — "
            f"capitale=${state.capital:.2f} | "
            f"W/L={state.wins}/{state.losses}"
        )
        return state
    except Exception as e:
        logging.warning(f"[State] Errore caricamento stato: {e} — parto da zero.")
        s = BotState()
        _reconcile_counters(s)
        return s


def _reconcile_counters(state: BotState):
    """Ricalcola wins/losses/total_trades dal trades_log.json (fonte di verità).
    Lascia intatti capital, consecutive_losses, cooldown e open_trade.
    """
    if not TRADES_LOG_FILE.exists():
        return
    try:
        trades = json.loads(TRADES_LOG_FILE.read_text())
        wins = sum(1 for t in trades if "WIN" in t.get("result", ""))
        losses = sum(1 for t in trades if "LOSS" in t.get("result", ""))
        total = len(trades)
        total_win_pnl  = sum(t["pnl"] for t in trades if t.get("pnl", 0) > 0)
        total_loss_pnl = sum(abs(t["pnl"]) for t in trades if t.get("pnl", 0) < 0)
        best  = max((t["pnl"] for t in trades), default=0.0)
        worst = min((t["pnl"] for t in trades), default=0.0)
        if total > state.total_trades:
            state.wins          = wins
            state.losses        = losses
            state.total_trades  = total
            state.total_win_pnl  = round(total_win_pnl, 4)
            state.total_loss_pnl = round(total_loss_pnl, 4)
            state.best_trade    = best
            state.worst_trade   = worst
            logging.info(
                f"[State] Contatori riconciliati dal log: "
                f"W/L={wins}/{losses} ({total} trade totali)"
            )
    except Exception as e:
        logging.warning(f"[State] Riconciliazione fallita: {e}")

def append_closed_trade(trade: dict, pnl: float, close_price: float, result: str, bars: int):
    """Aggiunge il trade chiuso al log storico."""
    ensure_reports_dir()
    trades = []
    if TRADES_LOG_FILE.exists():
        try:
            trades = json.loads(TRADES_LOG_FILE.read_text())
        except Exception:
            trades = []

    record = {
        "id":          len(trades) + 1,
        "signal":      trade["signal"].name,
        "mode":        trade.get("mode", "trend"),
        "entry":       trade["entry"],
        "sl":          trade["sl"],
        "tp":          trade["tp"],
        "close_price": round(close_price, 2),
        "pnl":         round(pnl, 2),
        "result":      result,
        "risk_amount": trade["risk_amount"],
        "rr":          trade["rr"],
        "atr":         trade["atr"],
        "duration_bars": bars,
        "open_time":   trade["open_time"],
        "close_time":  datetime.now(timezone.utc).isoformat(),
    }
    trades.append(record)
    TRADES_LOG_FILE.write_text(json.dumps(trades, indent=2))

def update_dashboard(state: BotState, current_price: float,
                     bias_4h: Bias, bias_1h: Bias,
                     adx: float, atr: float, rsi: float,
                     sr_levels: list = None,
                     fear_greed: int = 50, funding_rate: float = 0.0):
    """
    Scrive trades_data.js — file JS incluso dal dashboard HTML.
    Viene riscritto ad ogni ciclo (ogni 5 minuti).
    """
    ensure_reports_dir()

    # Carica storico trade
    trades = []
    if TRADES_LOG_FILE.exists():
        try:
            trades = json.loads(TRADES_LOG_FILE.read_text())
        except Exception:
            trades = []

    # Trade aperto serializzabile
    open_trade_data = None
    if state.open_trade:
        t = dict(state.open_trade)
        t["signal"] = t["signal"].name
        open_trade_data = t

    wr = round(state.wins / state.total_trades * 100, 1) if state.total_trades > 0 else 0
    avg_win  = round(state.total_win_pnl  / state.wins   if state.wins   > 0 else 0, 2)
    avg_loss = round(state.total_loss_pnl / state.losses if state.losses > 0 else 0, 2)

    bot_status = "RUNNING"
    if state.cooldown_remaining > 0:
        bot_status = f"PAUSA ({state.cooldown_remaining} barre)"

    payload = {
        "last_update":        datetime.now(timezone.utc).isoformat(),
        "bot_status":         bot_status,
        "current_price":      current_price,
        "bias_4h":            bias_4h.name,
        "bias_1h":            bias_1h.name,
        "adx":                round(adx, 1),
        "atr":                round(atr, 2),
        "rsi":                round(rsi, 1),
        "state": {
            "capital":            round(state.capital, 2),
            "daily_pnl":          round(state.daily_pnl, 2),
            "consecutive_losses": state.consecutive_losses,
            "cooldown_remaining": state.cooldown_remaining,
            "open_trade":         open_trade_data,
        },
        "stats": {
            "total_trades": state.total_trades,
            "wins":         state.wins,
            "losses":       state.losses,
            "win_rate":     wr,
            "avg_win":      avg_win,
            "avg_loss":     avg_loss,
            "best_trade":   round(state.best_trade,  2),
            "worst_trade":  round(state.worst_trade, 2),
            "total_pnl":    round(state.capital - INITIAL_CAPITAL, 2),
        },
        "trades": trades[-100:],  # ultimi 100 trade
        "sr_levels":    sorted(sr_levels or [], key=lambda x: x["price"], reverse=True)[:20],
        "fear_greed":   fear_greed,
        "funding_rate": round(funding_rate * 100, 5),
    }

    js_content = f"// Auto-generato da structural_bot.py — non modificare\n"
    js_content += f"const TRADES_DATA = {json.dumps(payload, indent=2)};\n"
    DASHBOARD_DATA.write_text(js_content, encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# SLEEP ALLINEATO ALLA CANDELA
# ─────────────────────────────────────────────────────────────────────────────

def sleep_until_next_candle(interval_minutes: int = 5):
    """Dorme esattamente fino alla prossima chiusura di candela, compensando il tempo di esecuzione."""
    now = datetime.now(timezone.utc)
    elapsed_in_interval = (now.minute % interval_minutes) * 60 + now.second + now.microsecond / 1e6
    sleep_sec = interval_minutes * 60 - elapsed_in_interval + 1  # +1s buffer disponibilità candela
    if sleep_sec <= 1:
        sleep_sec = interval_minutes * 60 + 1
    time.sleep(sleep_sec)


# ─────────────────────────────────────────────────────────────────────────────
# FETCH DATI DA BINANCE
# ─────────────────────────────────────────────────────────────────────────────

_http_session = requests.Session()   # riusa connessione TCP/TLS per tutte le fetch

def fetch_candles(symbol: str, interval: str, limit: int = 200) -> Optional[pd.DataFrame]:
    url    = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}

    for attempt in range(3):
        try:
            r = _http_session.get(url, params=params, timeout=10)
            r.raise_for_status()
            cols = [
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "trades",
                "taker_buy_base", "taker_buy_quote", "ignore"
            ]
            df = pd.DataFrame(r.json(), columns=cols)
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = df[c].astype(float)
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            return df.reset_index(drop=True)
        except Exception as e:
            logging.warning(f"[Fetch {interval}] tentativo {attempt+1}/3: {e}")
            time.sleep(2 ** attempt)

    logging.error(f"[Fetch {interval}] impossibile scaricare dati.")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# MARKET CONTEXT — Fear&Greed + Funding rate (con cache)
# ─────────────────────────────────────────────────────────────────────────────

_fng_cache  = {"value": 50,  "ts": 0.0}   # aggiornato ogni ora
_fund_cache = {"value": 0.0, "ts": 0.0}   # aggiornato ogni 15 min

def fetch_fear_greed() -> int:
    """Fear & Greed index 0-100. Cache 1h. 50 = neutro come fallback."""
    if time.time() - _fng_cache["ts"] < 3600:
        return _fng_cache["value"]
    try:
        r   = _http_session.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        val = int(r.json()["data"][0]["value"])
        _fng_cache.update({"value": val, "ts": time.time()})
        logging.info(f"[FNG] Fear&Greed={val}")
        return val
    except Exception as e:
        logging.warning(f"[FNG] fetch fallito: {e}")
        return _fng_cache["value"]

def fetch_funding_rate() -> float:
    """Ultimo funding rate BTCUSDT perpetual Binance. Cache 15min. 0.0 come fallback."""
    if time.time() - _fund_cache["ts"] < 900:
        return _fund_cache["value"]
    try:
        r   = _http_session.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": "BTCUSDT", "limit": 1}, timeout=8
        )
        val = float(r.json()[0]["fundingRate"])
        _fund_cache.update({"value": val, "ts": time.time()})
        logging.info(f"[FUNDING] rate={val*100:.4f}%")
        return val
    except Exception as e:
        logging.warning(f"[FUNDING] fetch fallito: {e}")
        return _fund_cache["value"]


# ─────────────────────────────────────────────────────────────────────────────
# INDICATORI TECNICI
# ─────────────────────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def calc_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()

    prev_high  = df["high"].shift(1)
    prev_low   = df["low"].shift(1)
    dm_plus  = (df["high"] - prev_high).clip(lower=0)
    dm_minus = (prev_low   - df["low"]).clip(lower=0)
    dm_plus  = dm_plus.where(dm_plus  > dm_minus, 0.0)
    dm_minus = dm_minus.where(dm_minus > dm_plus,  0.0)

    eps      = 1e-10
    di_plus  = 100 * dm_plus.ewm(span=period, adjust=False).mean()  / (atr + eps)
    di_minus = 100 * dm_minus.ewm(span=period, adjust=False).mean() / (atr + eps)
    dx       = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus + eps)
    return dx.ewm(span=period, adjust=False).mean()

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    rs    = gain / (loss + 1e-10)
    return 100 - (100 / (1 + rs))

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema20"]  = ema(df["close"], 20)
    df["ema50"]  = ema(df["close"], 50)
    df["ema100"] = ema(df["close"], 100)
    df["atr"]    = calc_atr(df, 14)
    df["adx"]    = calc_adx(df, 14)
    df["rsi"]    = calc_rsi(df["close"], 14)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# SUPPORT & RESISTANCE — rilevamento automatico da pivot 4h
# ─────────────────────────────────────────────────────────────────────────────

def detect_sr_levels(*dfs: pd.DataFrame) -> list:
    """
    Trova livelli S/R da pivot highs/lows su uno o più dataframe (multi-timeframe).
    Fonde livelli entro SR_MERGE_PCT, richiede SR_MIN_TOUCHES tocchi TOTALI tra tutti i TF.
    Ritorna lista di dict: {price, type:'support'|'resistance', touches}.
    """
    raw = []
    lb  = SR_PIVOT_LOOKBACK

    for df in dfs:
        if df is None or len(df) < lb * 2 + 1:
            continue
        n = len(df)
        for i in range(lb, n - lb):
            window = df.iloc[i - lb : i + lb + 1]
            hi = df.iloc[i]["high"]
            lo = df.iloc[i]["low"]
            if hi == window["high"].max():
                raw.append({"price": hi, "type": "resistance"})
            if lo == window["low"].min():
                raw.append({"price": lo, "type": "support"})

    # Aggiungi livelli statici chiave (psicologici + storici)
    for s in STATIC_SR_LEVELS:
        for _ in range(s["touches"]):
            raw.append({"price": s["price"], "type": s["type"]})

    if not raw:
        return []

    # Fonde livelli vicini (media pesata per tocchi)
    merged: list = []
    for lv in sorted(raw, key=lambda x: x["price"]):
        placed = False
        for m in merged:
            if abs(lv["price"] - m["price"]) / m["price"] < SR_MERGE_PCT:
                m["price"]    = (m["price"] * m["touches"] + lv["price"]) / (m["touches"] + 1)
                m["touches"] += 1
                placed = True
                break
        if not placed:
            merged.append({"price": lv["price"], "type": lv["type"], "touches": 1})

    result = [m for m in merged if m["touches"] >= SR_MIN_TOUCHES]
    result.sort(key=lambda x: x["price"])
    sig = tuple((round(m["price"]), m["touches"]) for m in result)
    if sig != detect_sr_levels._last_sig:
        detect_sr_levels._last_sig = sig
        logging.info(f"[SR] {len(result)} livelli (1W+1D+4h+1h+static): "
                     + ", ".join(f"{m['price']:.0f}({m['touches']}t)" for m in result))
    return result


detect_sr_levels._last_sig = ()


def _nearest_opposing_sr(price: float, signal: "Signal", sr_levels: list) -> Optional[float]:
    """
    SHORT → il supporto più vicino SOTTO il prezzo (il muro che frena la discesa).
    LONG  → la resistenza più vicina SOPRA il prezzo (il muro che frena la salita).
    """
    if not sr_levels:
        return None
    if signal == Signal.SHORT:
        cands = [lv["price"] for lv in sr_levels if lv["price"] < price]
        return max(cands) if cands else None
    if signal == Signal.LONG:
        cands = [lv["price"] for lv in sr_levels if lv["price"] > price]
        return min(cands) if cands else None
    return None


def _blocking_sr(entry: float, tp: float, signal: "Signal", sr_levels: list) -> Optional[float]:
    """
    Il primo livello S/R significativo che si frappone tra entry e TP.
    SHORT → il supporto più alto tra tp e entry.
    LONG  → la resistenza più bassa tra entry e tp.
    """
    if not sr_levels:
        return None
    if signal == Signal.SHORT:
        cands = [lv["price"] for lv in sr_levels
                 if tp < lv["price"] < entry and lv["type"] == "support"]
        return max(cands) if cands else None
    if signal == Signal.LONG:
        cands = [lv["price"] for lv in sr_levels
                 if entry < lv["price"] < tp and lv["type"] == "resistance"]
        return min(cands) if cands else None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# BOUNCE HELPERS — volume spike + candele di inversione
# ─────────────────────────────────────────────────────────────────────────────

def _volume_spike(df: pd.DataFrame, bars: int = 3, mult: float = 1.4) -> bool:
    """True se il volume medio delle ultime `bars` candele supera mult × media 20."""
    if len(df) < 25:
        return False
    recent = df["volume"].iloc[-bars - 1 : -1].mean()
    avg    = df["volume"].iloc[-21 : -1].mean()
    return avg > 0 and recent > avg * mult

def _bull_reversal_candle(df_1h: pd.DataFrame) -> bool:
    """
    True se l'ultima candela 1h chiusa è un hammer o bullish engulfing.
    Usa iloc[-2] = ultima chiusa, iloc[-3] = penultima.
    """
    if len(df_1h) < 3:
        return False
    cur  = df_1h.iloc[-2]
    prev = df_1h.iloc[-3]
    body     = abs(cur["close"] - cur["open"])
    full_rng = cur["high"] - cur["low"]
    if full_rng < 1e-9:
        return False
    lower_wick = min(cur["open"], cur["close"]) - cur["low"]
    upper_wick = cur["high"] - max(cur["open"], cur["close"])
    mid        = (cur["high"] + cur["low"]) / 2
    is_hammer  = (lower_wick >= 2 * max(body, 1) and
                  upper_wick <= body * 0.5 and
                  cur["close"] > mid)
    is_engulf  = (prev["close"] < prev["open"] and
                  cur["close"]  > cur["open"]  and
                  cur["open"]   < prev["close"] and
                  cur["close"]  > prev["open"])
    return is_hammer or is_engulf

def _bear_reversal_candle(df_1h: pd.DataFrame) -> bool:
    """True se l'ultima candela 1h chiusa è uno shooting star o bearish engulfing."""
    if len(df_1h) < 3:
        return False
    cur  = df_1h.iloc[-2]
    prev = df_1h.iloc[-3]
    body       = abs(cur["close"] - cur["open"])
    full_rng   = cur["high"] - cur["low"]
    if full_rng < 1e-9:
        return False
    upper_wick = cur["high"] - max(cur["open"], cur["close"])
    lower_wick = min(cur["open"], cur["close"]) - cur["low"]
    mid        = (cur["high"] + cur["low"]) / 2
    is_star    = (upper_wick >= 2 * max(body, 1) and
                  lower_wick <= body * 0.5 and
                  cur["close"] < mid)
    is_engulf  = (prev["close"] > prev["open"] and
                  cur["close"]  < cur["open"]  and
                  cur["open"]   > prev["close"] and
                  cur["close"]  < prev["open"])
    return is_star or is_engulf


def bounce_signal(df_5m: pd.DataFrame, df_1h: pd.DataFrame,
                  sr_levels: list, funding: float, fng: int) -> Signal:
    """
    Segnale contrarian su rimbalzo da S/R.

    LONG da supporto: prezzo entro BOUNCE_PROXIMITY_PCT da supporto forte,
    spike di volume, sentiment bearish estremo (funding ≤ soglia O F&G ≤ 20),
    candela di inversione bullish su 1h, RSI 5m 25-55.

    SHORT da resistenza: speculare, con sentiment bullish estremo.
    """
    if df_5m is None or df_1h is None or not sr_levels:
        return Signal.HOLD

    last  = df_5m.iloc[-1]
    close = last["close"]
    rsi   = last["rsi"]
    atr   = last["atr"]

    if atr < ATR_MIN_ENTRY:
        return Signal.HOLD

    # ── LONG: rimbalzo da supporto ────────────────────────────────────────────
    sup_near = [lv for lv in sr_levels
                if lv["type"] == "support"
                and 0 <= (close - lv["price"]) / close < BOUNCE_PROXIMITY_PCT
                and lv["touches"] >= BOUNCE_MIN_TOUCHES]
    if sup_near:
        nearest = max(sup_near, key=lambda x: x["price"])
        vol_ok  = _volume_spike(df_1h, bars=3) or _volume_spike(df_5m, bars=5)
        sent_ok = funding <= FUNDING_LONG_THRESH or fng <= FNG_LONG_THRESH
        can_ok  = _bull_reversal_candle(df_1h)
        rsi_ok  = 25 <= rsi <= 65
        logging.debug(
            f"[BOUNCE LONG?] sup={nearest['price']:.0f}({nearest['touches']}t) "
            f"vol={vol_ok} sent={sent_ok}(f={funding*100:.4f}%,fng={fng}) "
            f"candle={can_ok} rsi={rsi:.1f}"
        )
        if vol_ok and sent_ok and can_ok and rsi_ok:
            logging.info(
                f"[BOUNCE LONG] supporto {nearest['price']:.0f} | "
                f"funding={funding*100:.4f}% fng={fng} rsi={rsi:.1f}"
            )
            return Signal.LONG

    # ── SHORT: rimbalzo da resistenza ─────────────────────────────────────────
    res_near = [lv for lv in sr_levels
                if lv["type"] == "resistance"
                and 0 <= (lv["price"] - close) / close < BOUNCE_PROXIMITY_PCT
                and lv["touches"] >= BOUNCE_MIN_TOUCHES]
    if res_near:
        nearest = min(res_near, key=lambda x: x["price"])
        vol_ok  = _volume_spike(df_1h, bars=3) or _volume_spike(df_5m, bars=5)
        sent_ok = funding >= FUNDING_SHORT_THRESH or fng >= FNG_SHORT_THRESH
        can_ok  = _bear_reversal_candle(df_1h)
        rsi_ok  = 45 <= rsi <= 75
        logging.debug(
            f"[BOUNCE SHORT?] res={nearest['price']:.0f}({nearest['touches']}t) "
            f"vol={vol_ok} sent={sent_ok}(f={funding*100:.4f}%,fng={fng}) "
            f"candle={can_ok} rsi={rsi:.1f}"
        )
        if vol_ok and sent_ok and can_ok and rsi_ok:
            logging.info(
                f"[BOUNCE SHORT] resistenza {nearest['price']:.0f} | "
                f"funding={funding*100:.4f}% fng={fng} rsi={rsi:.1f}"
            )
            return Signal.SHORT

    # ── Fallback: extreme oversold senza S/R mappato ─────────────────────────
    # Range senza struttura storica (es. BTC in zona inesplorata al ribasso):
    # RSI < 35 + F&G ≤ 20 + volume spike + reversal candle → LONG contrarian
    vol_ok  = _volume_spike(df_1h, bars=3) or _volume_spike(df_5m, bars=5)
    can_ok  = _bull_reversal_candle(df_1h)
    if rsi < 35 and fng <= FNG_LONG_THRESH and vol_ok and can_ok:
        logging.info(
            f"[BOUNCE LONG extreme] nessun S/R mappato — RSI={rsi:.1f} F&G={fng} "
            f"vol=✓ candle=✓ → long contrarian"
        )
        return Signal.LONG

    return Signal.HOLD


# ─────────────────────────────────────────────────────────────────────────────
# BIAS STRUTTURALE (sostituisce p_long / p_short)
# ─────────────────────────────────────────────────────────────────────────────

def structural_bias(df: pd.DataFrame) -> Bias:
    if df is None or len(df) < 110:
        return Bias.NEUTRAL

    last = df.iloc[-1]
    ema20, ema50, ema100 = last["ema20"], last["ema50"], last["ema100"]
    close, rsi           = last["close"], last["rsi"]

    stack_bull = ema20 > ema50 > ema100
    price_bull = close > ema50
    rsi_bull   = rsi > 52

    stack_bear = ema20 < ema50 < ema100
    price_bear = close < ema50
    rsi_bear   = rsi < 48

    bull_score = sum([stack_bull, price_bull, rsi_bull])
    bear_score = sum([stack_bear, price_bear, rsi_bear])

    if bull_score >= 2:
        return Bias.BULL
    if bear_score >= 2:
        return Bias.BEAR
    return Bias.NEUTRAL


# ─────────────────────────────────────────────────────────────────────────────
# SEGNALE DI ENTRY
# ─────────────────────────────────────────────────────────────────────────────

def entry_signal(df_5m: pd.DataFrame, bias_1h: Bias, bias_4h: Bias,
                 bias_2h: Bias = Bias.NEUTRAL, sr_levels: list = None) -> Signal:
    """
    Sistema a 3 timeframe: 2h = direzione primaria, 4h = macro contesto, 1h = conferma.

    Regole:
      - 2h NEUTRAL → HOLD (nessuna direzione dominante sul timeframe operativo)
      - 2h e 1h in conflitto → HOLD
      - LONG: 2h BULL (o NEUTRAL+1h BULL per transizioni), 4h non fortemente opposto
      - SHORT: 2h BEAR, 4h non fortemente opposto
      - Finestra pullback estesa a 5 barre (~25 min): cattura pullback più lenti
      - Rimosso il requisito di candela corrente nella direzione del trade:
        la posizione del prezzo rispetto a EMA20 è già sufficiente come filtro direzionale
    """
    if df_5m is None or len(df_5m) < 60:
        return Signal.HOLD

    # 2h è il timeframe operativo principale
    # Se 2h è NEUTRAL e anche 1h è NEUTRAL → nessuna direzione chiara
    if bias_2h == Bias.NEUTRAL and bias_1h == Bias.NEUTRAL:
        return Signal.HOLD

    # Direzione operativa: 2h domina, 1h conferma
    # Se 2h ha una direzione, usa quella. Se 2h NEUTRAL, usa 1h (transizione precoce)
    op_bias = bias_2h if bias_2h != Bias.NEUTRAL else bias_1h

    # Conflitto 2h vs 1h → HOLD (segnali contrastanti)
    if bias_2h != Bias.NEUTRAL and bias_1h != Bias.NEUTRAL and bias_2h != bias_1h:
        return Signal.HOLD

    last  = df_5m.iloc[-1]
    adx   = last["adx"]
    close = last["close"]
    ema20 = last["ema20"]
    ema50 = last["ema50"]
    rsi   = last["rsi"]
    atr   = last["atr"]

    if adx < ADX_MIN_TREND:
        return Signal.HOLD
    if atr < ATR_MIN_ENTRY:
        return Signal.HOLD   # range compresso: SL troppo stretto, whipsaw inevitabile

    # ── EMA20 slope filter ────────────────────────────────────────────────────
    # Verifica che la media mobile 5m stia andando nella direzione del trade.
    # Su 4 barre (20 min): se EMA20 sale mentre vogliamo SHORT (o scende per LONG)
    # significa che il momentum intraday è contro di noi — skip.
    # Soglia: 0.20 × ATR (~9-10 pt su BTC ATR=45). Cattura transizioni chiare
    # senza essere troppo restrittivo sui pullback normali.
    _ema20_ref     = df_5m.iloc[-5]["ema20"]  # 20 min fa
    _ema20_rising  = ema20 > _ema20_ref + atr * 0.20
    _ema20_falling = ema20 < _ema20_ref - atr * 0.20

    recent = df_5m.iloc[-7:]

    # ── LONG ─────────────────────────────────────────────────────────────────
    if op_bias == Bias.BULL:
        if bias_4h == Bias.BEAR and bias_2h == Bias.NEUTRAL:
            return Signal.HOLD
        if rsi > 68:
            return Signal.HOLD
        if _ema20_falling:
            return Signal.HOLD   # EMA20 in calo: momentum contro il LONG
        ema_stack_ok  = ema20 > ema50
        price_above   = close > ema20
        # Fix: ogni bar confrontata con la propria EMA20 storica (non lo scalare corrente)
        # Con scalare corrente in uptrend: lows vecchi bassi vs EMA20 alta → sempre True
        touched_ema20 = (recent["low"] <= recent["ema20"] * 1.005).any()
        if ema_stack_ok and price_above and touched_ema20:
            # Filtro S/R: salta LONG se troppo vicino a una resistenza forte
            if sr_levels:
                opp = _nearest_opposing_sr(close, Signal.LONG, sr_levels)
                if opp and (opp - close) / close < SR_PROXIMITY_PCT:
                    logging.info(
                        f"[SR FILTER] LONG bloccato: resistenza a {opp:.0f} "
                        f"({(opp-close)/close*100:.1f}% < {SR_PROXIMITY_PCT*100:.1f}%)"
                    )
                    return Signal.HOLD
            return Signal.LONG

    # ── SHORT ─────────────────────────────────────────────────────────────────
    if op_bias == Bias.BEAR:
        if bias_4h == Bias.BULL and bias_2h == Bias.NEUTRAL:
            return Signal.HOLD
        if rsi < 32:
            return Signal.HOLD
        if _ema20_rising:
            return Signal.HOLD   # EMA20 in salita: momentum contro lo SHORT
        ema_stack_ok  = ema20 < ema50
        price_below   = close < ema20
        # Fix: idem — ogni bar vs sua EMA20 storica
        touched_ema20 = (recent["high"] >= recent["ema20"] * 0.995).any()
        if ema_stack_ok and price_below and touched_ema20:
            # Filtro S/R: salta SHORT se troppo vicino a un supporto forte
            if sr_levels:
                opp = _nearest_opposing_sr(close, Signal.SHORT, sr_levels)
                if opp and (close - opp) / close < SR_PROXIMITY_PCT:
                    logging.info(
                        f"[SR FILTER] SHORT bloccato: supporto a {opp:.0f} "
                        f"({(close-opp)/close*100:.1f}% < {SR_PROXIMITY_PCT*100:.1f}%)"
                    )
                    return Signal.HOLD
            return Signal.SHORT

    return Signal.HOLD


# ─────────────────────────────────────────────────────────────────────────────
# CALCOLO TRADE
# ─────────────────────────────────────────────────────────────────────────────

def calculate_trade(signal: Signal, last: pd.Series, capital: float,
                    bias_2h: "Bias" = None, bias_4h: "Bias" = None,
                    sr_levels: list = None) -> Optional[dict]:
    atr   = last["atr"]
    close = last["close"]
    adx   = last["adx"]
    if atr <= 0 or close <= 0:
        return None

    # ── TP dinamico basato sulla forza del trend ──────────────────────────────
    # ADX > 35 + tutti i timeframe allineati → mercato in momentum forte → TP esteso
    all_aligned = (bias_2h is not None and bias_4h is not None and
                   bias_2h.name != "NEUTRAL" and bias_4h.name != "NEUTRAL" and
                   bias_2h == bias_4h)
    if adx >= 40 and all_aligned:
        dynamic_rr = 4.0   # trend fortissimo: lascia correre fino a 4R
    elif adx >= 30 and all_aligned:
        dynamic_rr = 3.0   # trend forte: TP esteso a 3R
    else:
        dynamic_rr = MIN_RR  # default 2R

    # Gate fee-aware: vedi MIN_DYNAMIC_RR — sotto ADX 40 + allineamento 2h/4h
    # il net atteso è negativo a questa size di conto. Vale anche per i bounce
    # (3/3 loss nello storico).
    if dynamic_rr < MIN_DYNAMIC_RR:
        logging.info(
            f"[RR SKIP] dynamic_rr={dynamic_rr} < {MIN_DYNAMIC_RR} "
            f"(ADX={adx:.0f}, aligned={all_aligned}) — edge non copre le fee"
        )
        return None

    # TP1 (2R) e TP2 (dynamic_rr): chiude 50% a TP1, lascia il resto fino a TP2
    # Se dynamic_rr == MIN_RR non c'è TP2 (singolo TP)
    use_dual_tp = dynamic_rr > MIN_RR

    # SL più largo quando il mercato è in ipervolatilità (ADX > 55):
    # con ADX estremo il noise intrabar è amplificato e 1.5× ATR viene whippato
    # prima che il trend riprenda (es. SHORT 73361, ADX=72 → SL colpito in 5 min).
    sl_mult = ATR_SL_MULT * 1.25 if adx >= 55 else ATR_SL_MULT  # 2.0 → 2.5 in ipervolatilità

    slip = close * SLIPPAGE_PCT

    if signal == Signal.LONG:
        entry         = close + slip
        sl            = entry - atr * sl_mult
        risk_per_unit = entry - sl
        tp1           = entry + risk_per_unit * MIN_RR       # sempre 2R
        tp2           = entry + risk_per_unit * dynamic_rr   # esteso se trend forte
    elif signal == Signal.SHORT:
        entry         = close - slip
        sl            = entry + atr * sl_mult
        risk_per_unit = sl - entry
        tp1           = entry - risk_per_unit * MIN_RR
        tp2           = entry - risk_per_unit * dynamic_rr
    else:
        return None

    if risk_per_unit <= 0:
        return None

    # ── TP snap al livello S/R che si frappone tra entry e TP ────────────────
    # Evita di puntare oltre un supporto/resistenza strutturale forte.
    # Il TP viene portato a livello ± ATR×SR_TP_MARGIN_ATR, ma solo se
    # mantiene almeno MIN_RR×0.8 (non taglia troppo il trade).
    if sr_levels:
        blocker = _blocking_sr(entry, tp2, signal, sr_levels)
        if blocker is not None:
            if signal == Signal.SHORT:
                snapped = round(blocker + atr * SR_TP_MARGIN_ATR, 2)
                if snapped > tp2 and (entry - snapped) / risk_per_unit >= MIN_RR * 0.8:
                    logging.info(
                        f"[SR TP-SNAP] SHORT TP {tp2:.0f}→{snapped:.0f} "
                        f"(supporto @ {blocker:.0f})"
                    )
                    tp1 = tp2 = snapped
                    use_dual_tp = False
            elif signal == Signal.LONG:
                snapped = round(blocker - atr * SR_TP_MARGIN_ATR, 2)
                if snapped < tp2 and (snapped - entry) / risk_per_unit >= MIN_RR * 0.8:
                    logging.info(
                        f"[SR TP-SNAP] LONG TP {tp2:.0f}→{snapped:.0f} "
                        f"(resistenza @ {blocker:.0f})"
                    )
                    tp1 = tp2 = snapped
                    use_dual_tp = False

    risk_amount = capital * RISK_PCT
    size        = risk_amount / risk_per_unit

    if size < MIN_LOT_BTC:
        logging.warning(
            f"[TRADE SKIP] size={size:.6f} BTC < lotto minimo {MIN_LOT_BTC} BTC "
            f"(capital=${capital:.2f} risk_pct={RISK_PCT*100:.1f}% risk_per_unit={risk_per_unit:.2f})"
        )
        return None

    return {
        "signal":          signal,
        "entry":           round(entry, 2),
        "sl":              round(sl, 2),
        "tp":              round(tp2, 2),   # Bitget vede il TP finale
        "tp1":             round(tp1, 2),   # usato internamente per chiudere 50%
        "tp2":             round(tp2, 2),
        "use_dual_tp":     use_dual_tp,
        "tp1_hit":         False,
        "size":            round(size, 6),
        "risk_amount":     round(risk_amount, 2),
        "rr":              dynamic_rr,
        "atr":             round(atr, 2),
        "open_time":       datetime.now(timezone.utc).isoformat(),
        "open_bar":        0,
        "trailing_active": False,   # trailing stop non ancora attivato
        "trailing_sl":     None,    # valore trailing SL (None = non attivo)
    }


# ─────────────────────────────────────────────────────────────────────────────
# CHECK TRADE APERTO
# ─────────────────────────────────────────────────────────────────────────────

def check_open_trade(state: BotState, current_price: float,
                     sr_levels: list = None) -> Optional[float]:
    """
    Controlla se il trade aperto ha toccato SL, TP o trailing stop.
    Gestione S/R attiva:
      1. Break-even: quando il prezzo supera un S/R chiave a favore → SL → entry
      2. Trail tightening: entro 2% da S/R nella direzione del trade → trail ×0.5
      3. Partial a S/R: S/R forte (≥5 tocchi) raggiunto prima di TP1 → parziale 50%
    """
    if state.open_trade is None:
        return None

    t   = state.open_trade
    sig = t["signal"]
    t["open_bar"] = t.get("open_bar", 0) + 1

    MAX_HOLD_BARS = 36
    if t["open_bar"] >= MAX_HOLD_BARS and not t.get("tp1_hit", False):
        logging.info(
            f"[TIME EXIT] {sig} aperto da {t['open_bar']} barre (>{MAX_HOLD_BARS}) "
            f"senza TP1 → chiusura time-limit"
        )
        return 0.0

    entry = t["entry"]
    tp    = t["tp"]
    sl    = t["sl"]
    atr   = t["atr"]
    size  = t["size"]

    if sig == Signal.LONG:
        progress = (current_price - entry) / (tp - entry) if (tp - entry) > 0 else 0.0

        # ── S/R management (LONG) ────────────────────────────────────────────
        if sr_levels:
            # 1. Break-even: price ha superato una resistenza chiave tra entry e corrente
            if not t.get("be_set", False) and t["sl"] < entry:
                crossed_res = [lv for lv in sr_levels
                               if lv["type"] in ("resistance", "zone")
                               and entry < lv["price"] <= current_price
                               and lv["touches"] >= 3]
                if crossed_res:
                    strongest = max(crossed_res, key=lambda x: x["touches"])
                    t["sl"]    = entry
                    t["be_set"] = True
                    logging.info(
                        f"[SR BE] LONG: SL → breakeven {entry:.0f} "
                        f"(superata resistenza ${strongest['price']:.0f}, {strongest['touches']}t)"
                    )

            # 2. Trail tightening: resistenza entro 2% sopra
            ahead_res = [lv for lv in sr_levels
                         if lv["type"] in ("resistance", "zone")
                         and current_price < lv["price"]
                         and (lv["price"] - current_price) / current_price < 0.02]
            trail_dist = TRAIL_ATR_DIST * 0.5 if ahead_res else TRAIL_ATR_DIST
            if ahead_res and not t.get("trail_tight_logged", False):
                nearest = min(ahead_res, key=lambda x: x["price"])
                t["trail_tight_logged"] = True
                logging.info(
                    f"[SR TIGHT] LONG: trail stretto (×0.5) — "
                    f"resistenza ${nearest['price']:.0f} a "
                    f"{(nearest['price']-current_price)/current_price*100:.1f}%"
                )
            elif not ahead_res:
                t["trail_tight_logged"] = False
        else:
            trail_dist = TRAIL_ATR_DIST

        # 3. Partial a S/R forte prima di TP1
        if sr_levels and not t.get("tp1_hit", False) and not t.get("sr_partial_taken", False):
            tp1_val = t.get("tp1", tp)
            sr_partials = [lv for lv in sr_levels
                           if lv["type"] in ("resistance", "zone")
                           and entry < lv["price"] <= current_price
                           and lv["price"] < tp1_val
                           and lv["touches"] >= 5]
            if sr_partials:
                sr_lv = max(sr_partials, key=lambda x: x["touches"])
                pnl_p = round((sr_lv["price"] - entry) * size * 0.5, 2)
                if pnl_p > 0:
                    t["sr_partial_taken"] = True
                    t["partial_pnl"]      = pnl_p
                    t["trailing_active"]  = True
                    t["trailing_sl"]      = max(entry, round(current_price - atr * trail_dist, 2))
                    logging.info(
                        f"[SR PARTIAL] LONG: 50% chiuso a S/R ${sr_lv['price']:.0f} "
                        f"({sr_lv['touches']}t) → P&L+${pnl_p:.2f}"
                    )

        # ── Attivazione trailing stop ────────────────────────────────────────
        if not t.get("trailing_active", False) and progress >= TRAIL_ACTIVATE_PCT:
            t["trailing_active"] = True
            t["trailing_sl"] = max(entry, round(current_price - atr * trail_dist, 2))
            logging.info(
                f"[TRAIL ON] LONG: trailing_sl={t['trailing_sl']:.2f} "
                f"(progresso {progress*100:.0f}%, dist={'stretto' if trail_dist < TRAIL_ATR_DIST else 'normale'})"
            )

        # ── Aggiornamento trailing stop ──────────────────────────────────────
        if t.get("trailing_active", False):
            new_trail = round(current_price - atr * trail_dist, 2)
            if new_trail > t["trailing_sl"]:
                t["trailing_sl"] = new_trail
            if current_price <= t["trailing_sl"]:
                pnl = round((t["trailing_sl"] - entry) * size + t.get("partial_pnl", 0), 2)
                t["trail_exit"] = True
                logging.info(
                    f"[TRAIL HIT] LONG trailing_sl={t['trailing_sl']:.2f} "
                    f"price={current_price:.2f} P&L=${pnl:+.2f}"
                )
                return pnl

        # ── SL / TP ──────────────────────────────────────────────────────────
        if current_price <= t["sl"]:
            loss = round((t["sl"] - entry) * size, 2) if t["sl"] > entry else -t["risk_amount"]
            return loss
        tp1 = t.get("tp1", tp)
        tp2 = t.get("tp2", tp)
        use_dual = t.get("use_dual_tp", False)
        if use_dual and not t.get("tp1_hit", False) and current_price >= tp1:
            t["tp1_hit"]       = True
            t["trailing_active"] = True
            t["trailing_sl"]   = max(entry, round(current_price - atr * trail_dist, 2))
            pnl_partial = round((tp1 - entry) * size * 0.5, 2)
            t["partial_pnl"]   = t.get("partial_pnl", 0) + pnl_partial
            logging.info(f"[TP1 HIT] LONG tp1={tp1:.2f} → partial P&L=${pnl_partial:+.2f} | trail → TP2={tp2:.2f}")
            return None
        if current_price >= tp2 if use_dual else current_price >= tp:
            rr_actual  = t.get("rr", MIN_RR)
            tp2_factor = 0.5 if use_dual else 1.0
            return round(t["risk_amount"] * rr_actual * tp2_factor + t.get("partial_pnl", 0), 2)

    elif sig == Signal.SHORT:
        progress = (entry - current_price) / (entry - tp) if (entry - tp) > 0 else 0.0

        # ── S/R management (SHORT) ───────────────────────────────────────────
        if sr_levels:
            # 1. Break-even: price ha superato un supporto chiave tra entry e corrente (in discesa)
            if not t.get("be_set", False) and t["sl"] > entry:
                crossed_sup = [lv for lv in sr_levels
                               if lv["type"] in ("support", "zone")
                               and current_price <= lv["price"] < entry
                               and lv["touches"] >= 3]
                if crossed_sup:
                    strongest = min(crossed_sup, key=lambda x: x["price"])
                    t["sl"]    = entry
                    t["be_set"] = True
                    logging.info(
                        f"[SR BE] SHORT: SL → breakeven {entry:.0f} "
                        f"(superato supporto ${strongest['price']:.0f}, {strongest['touches']}t)"
                    )

            # 2. Trail tightening: supporto entro 2% sotto
            ahead_sup = [lv for lv in sr_levels
                         if lv["type"] in ("support", "zone")
                         and lv["price"] < current_price
                         and (current_price - lv["price"]) / current_price < 0.02]
            trail_dist = TRAIL_ATR_DIST * 0.5 if ahead_sup else TRAIL_ATR_DIST
            if ahead_sup and not t.get("trail_tight_logged", False):
                nearest = max(ahead_sup, key=lambda x: x["price"])
                t["trail_tight_logged"] = True
                logging.info(
                    f"[SR TIGHT] SHORT: trail stretto (×0.5) — "
                    f"supporto ${nearest['price']:.0f} a "
                    f"{(current_price-nearest['price'])/current_price*100:.1f}%"
                )
            elif not ahead_sup:
                t["trail_tight_logged"] = False
        else:
            trail_dist = TRAIL_ATR_DIST

        # 3. Partial a S/R forte prima di TP1
        if sr_levels and not t.get("tp1_hit", False) and not t.get("sr_partial_taken", False):
            tp1_val = t.get("tp1", tp)
            sr_partials = [lv for lv in sr_levels
                           if lv["type"] in ("support", "zone")
                           and current_price <= lv["price"] < entry
                           and lv["price"] > tp1_val
                           and lv["touches"] >= 5]
            if sr_partials:
                sr_lv = max(sr_partials, key=lambda x: x["touches"])
                pnl_p = round((entry - sr_lv["price"]) * size * 0.5, 2)
                if pnl_p > 0:
                    t["sr_partial_taken"] = True
                    t["partial_pnl"]      = pnl_p
                    t["trailing_active"]  = True
                    t["trailing_sl"]      = min(entry, round(current_price + atr * trail_dist, 2))
                    logging.info(
                        f"[SR PARTIAL] SHORT: 50% chiuso a S/R ${sr_lv['price']:.0f} "
                        f"({sr_lv['touches']}t) → P&L+${pnl_p:.2f}"
                    )

        # ── Attivazione trailing stop ────────────────────────────────────────
        if not t.get("trailing_active", False) and progress >= TRAIL_ACTIVATE_PCT:
            t["trailing_active"] = True
            t["trailing_sl"] = min(entry, round(current_price + atr * trail_dist, 2))
            logging.info(
                f"[TRAIL ON] SHORT: trailing_sl={t['trailing_sl']:.2f} "
                f"(progresso {progress*100:.0f}%, dist={'stretto' if trail_dist < TRAIL_ATR_DIST else 'normale'})"
            )

        # ── Aggiornamento trailing stop ──────────────────────────────────────
        if t.get("trailing_active", False):
            new_trail = round(current_price + atr * trail_dist, 2)
            if new_trail < t["trailing_sl"]:
                t["trailing_sl"] = new_trail
            if current_price >= t["trailing_sl"]:
                pnl = round((entry - t["trailing_sl"]) * size + t.get("partial_pnl", 0), 2)
                t["trail_exit"] = True
                logging.info(
                    f"[TRAIL HIT] SHORT trailing_sl={t['trailing_sl']:.2f} "
                    f"price={current_price:.2f} P&L=${pnl:+.2f}"
                )
                return pnl

        # ── SL / TP ──────────────────────────────────────────────────────────
        if current_price >= t["sl"]:
            loss = round((entry - t["sl"]) * size, 2) if t["sl"] < entry else -t["risk_amount"]
            return loss
        tp1 = t.get("tp1", tp)
        tp2 = t.get("tp2", tp)
        use_dual = t.get("use_dual_tp", False)
        if use_dual and not t.get("tp1_hit", False) and current_price <= tp1:
            t["tp1_hit"]       = True
            t["trailing_active"] = True
            t["trailing_sl"]   = min(entry, round(current_price + atr * trail_dist, 2))
            pnl_partial = round((entry - tp1) * size * 0.5, 2)
            t["partial_pnl"]   = t.get("partial_pnl", 0) + pnl_partial
            logging.info(f"[TP1 HIT] SHORT tp1={tp1:.2f} → partial P&L=${pnl_partial:+.2f} | trail → TP2={tp2:.2f}")
            return None
        if current_price <= tp2 if use_dual else current_price <= tp:
            rr_actual  = t.get("rr", MIN_RR)
            tp2_factor = 0.5 if use_dual else 1.0
            return round(t["risk_amount"] * rr_actual * tp2_factor + t.get("partial_pnl", 0), 2)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# CIRCUIT BREAKER
# ─────────────────────────────────────────────────────────────────────────────

def circuit_breaker_active(state: BotState) -> bool:
    if state.cooldown_remaining > 0:
        state.cooldown_remaining -= 1
        return True
    if state.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
        logging.warning(
            f"[CIRCUIT BREAKER] {MAX_CONSECUTIVE_LOSSES} perdite consecutive → "
            f"pausa {COOLDOWN_BARS} candele."
        )
        state.cooldown_remaining = COOLDOWN_BARS - 1
        state.consecutive_losses = 0
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# LOOP PRINCIPALE
# ─────────────────────────────────────────────────────────────────────────────

def run(executor=None):
    """
    Avvia il bot. Passa un BinanceFuturesExecutor per trading reale:

        from binance_futures_executor import BinanceFuturesExecutor
        run(executor=BinanceFuturesExecutor())

    Senza executor (default): modalità simulazione pura.
    """
    ensure_reports_dir()

    # ── Lock file: impedisce due istanze simultanee ───────────────────────────
    import fcntl
    _lock_path = REPORTS_DIR / "structural_bot.lock"
    _lock_fh   = open(_lock_path, "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"[ERRORE] Un'altra istanza di structural_bot è già in esecuzione (lock: {_lock_path}). Uscita.")
        raise SystemExit(1)
    _lock_fh.write(str(os.getpid()))
    _lock_fh.flush()
    # ─────────────────────────────────────────────────────────────────────────
    _fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s",
                             datefmt="%Y-%m-%d %H:%M:%S")
    _fh = logging.handlers.RotatingFileHandler(
        REPORTS_DIR / "structural_bot.log",
        maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    _fh.setFormatter(_fmt)
    _ch = logging.StreamHandler()
    _ch.setFormatter(_fmt)
    logging.basicConfig(level=logging.INFO, handlers=[_ch, _fh], force=True)

    state = load_state()  # recupera stato da disco se esiste

    # Sync capitale reale all'avvio (indipendente da open_trade)
    # Il saldo Bitget include già P&L di chiusure manuali avvenute offline.
    if executor and hasattr(executor, '_get_balance'):
        real_bal = executor._get_balance()
        if real_bal and real_bal > 1.0:
            delta     = real_bal - state.capital
            max_delta = state.capital * 0.60   # cap 60% per spike API
            if abs(delta) <= max_delta:
                if abs(delta) > 0.01:
                    logging.info(
                        f"[CAPITAL SYNC avvio] ${state.capital:.2f} → ${real_bal:.2f} "
                        f"(Δ={delta:+.2f} dal saldo Bitget)"
                    )
                state.capital = real_bal
            else:
                logging.warning(
                    f"[CAPITAL SYNC avvio] Delta anomalo ignorato: "
                    f"${state.capital:.2f} → ${real_bal:.2f} (Δ={delta:+.2f})"
                )

    logging.info("=" * 60)
    logging.info(" STRUCTURAL BOT — nessun ML, solo struttura di mercato")
    logging.info("=" * 60)
    logging.info(
        f"Capitale: ${state.capital:.2f} | "
        f"Risk/trade: {RISK_PCT*100:.1f}% | R:R min: {MIN_RR} | "
        f"ADX min: {ADX_MIN_TREND} | Circuit breaker: {MAX_CONSECUTIVE_LOSSES} losses"
    )
    logging.info(f"Reports → {REPORTS_DIR}")

    # Capital sync: contatori per delta anomalo persistente (vedi loop)
    _anom_count = 0
    _anom_last  = None

    while True:
        try:
            now   = datetime.now(timezone.utc)
            today = now.strftime("%Y-%m-%d")

            # Reset giornaliero
            if state.last_day != today:
                wr = (state.wins / state.total_trades * 100) if state.total_trades else 0
                logging.info(
                    f"[NEW DAY] Cap=${state.capital:.2f} | "
                    f"P&L ieri=${state.daily_pnl:+.2f} | "
                    f"W/L={state.wins}/{state.losses} ({wr:.0f}% WR)"
                )
                state.daily_pnl = 0.0
                state.last_day  = today
                save_state(state)

            # Fetch dati — 4 timeframe operativi + 5m per entry
            df_5m = fetch_candles(SYMBOL, "5m",  500)   # 500: ADX/RSI warm-up più accurato
            df_1h = fetch_candles(SYMBOL, "1h",  500)
            df_2h = fetch_candles(SYMBOL, "2h",  300)
            df_4h = fetch_candles(SYMBOL, "4h",  200)
            df_1d = fetch_candles(SYMBOL, "1d",  500)   # S/R strutturali long-term
            df_1w = fetch_candles(SYMBOL, "1w",  200)   # S/R weekly: livelli macro storici

            if df_5m is None or df_1h is None or df_2h is None or df_4h is None:
                logging.error("[DATA] Dati non disponibili, retry tra 60s")
                time.sleep(60)
                continue

            # Sync capitale reale dall'exchange (compensa fees/funding, previene over-leverage)
            # Cap ±50%: blocca solo spike API anomali (es. balance momentaneamente 0 o 10x)
            if executor and state.open_trade is None and hasattr(executor, '_get_balance'):
                real_bal = executor._get_balance()
                if real_bal > 1.0:   # sanity check: >1 USDT
                    delta = real_bal - state.capital
                    max_delta = state.capital * 0.50
                    if abs(delta) > max_delta:
                        # Anti-deadlock: se il valore "anomalo" è stabile per 6 cicli
                        # consecutivi (±10%) non è uno spike API ma il saldo reale
                        # (es. prelievo, perdita non tracciata) → accettalo, altrimenti
                        # il bot resta per sempre con un capitale fantasma e size errate.
                        stable = (_anom_last is not None
                                  and abs(real_bal - _anom_last) <= max(0.50, abs(_anom_last) * 0.10))
                        _anom_count = _anom_count + 1 if stable else 1
                        _anom_last  = real_bal
                        if _anom_count >= 6:
                            logging.warning(
                                f"[CAPITAL SYNC] Delta anomalo PERSISTENTE ({_anom_count} cicli stabili): "
                                f"{state.capital:.2f} → {real_bal:.2f} USDT — accettato come saldo reale"
                            )
                            state.capital = real_bal
                            _anom_count = 0
                            _anom_last  = None
                        else:
                            logging.warning(
                                f"[CAPITAL SYNC] Delta anomalo ignorato ({_anom_count}/6): "
                                f"{state.capital:.2f} → {real_bal:.2f} USDT (Δ={delta:+.2f}, cap=±{max_delta:.2f})"
                            )
                    else:
                        _anom_count = 0
                        _anom_last  = None
                        if abs(delta) > 0.05:
                            logging.info(f"[CAPITAL SYNC] {state.capital:.2f} → {real_bal:.2f} USDT (delta={delta:+.2f})")
                        state.capital = real_bal

            df_5m = add_indicators(df_5m)
            df_1h = add_indicators(df_1h)
            df_2h = add_indicators(df_2h)
            df_4h = add_indicators(df_4h)

            last_5m       = df_5m.iloc[-1]
            current_price = last_5m["close"]
            atr           = last_5m["atr"]
            adx           = last_5m["adx"]
            rsi_5m        = last_5m["rsi"]
            bias_1h       = structural_bias(df_1h)
            bias_2h       = structural_bias(df_2h)
            bias_4h       = structural_bias(df_4h)

            # Livelli S/R: 1W+1D+4h+1h + livelli statici psicologici/storici
            sr_levels = detect_sr_levels(df_1w, df_1d, df_4h, df_1h)

            # Contesto di mercato (cache: F&G 1h, funding 15min)
            fear_greed   = fetch_fear_greed()
            funding_rate = fetch_funding_rate()

            # ── Hook 0: reconcile chiusura manuale ───────────────────────────
            # Se lo stato ha un trade aperto ma Bitget non vede posizioni → chiuso a mano.
            # Calcola P&L reale dal saldo Bitget (più preciso del prezzo corrente).
            if executor and state.open_trade is not None and not executor.has_open_position:
                sig_name = state.open_trade.get("signal", {})
                try: sig_name = sig_name.name
                except Exception: sig_name = str(sig_name)

                # P&L dalla variazione saldo reale
                pnl_manual = 0.0
                new_capital = state.capital
                if hasattr(executor, '_get_balance'):
                    real_bal = executor._get_balance()
                    if real_bal and real_bal > 1.0:
                        pnl_manual  = round(real_bal - state.capital, 4)
                        new_capital = real_bal

                # Aggiorna statistiche
                state.total_trades    += 1
                state.capital          = new_capital
                state.daily_pnl       += pnl_manual
                state.bars_since_close = 0
                if pnl_manual > 0:
                    state.wins              += 1
                    state.consecutive_losses = 0
                    state.total_win_pnl     += pnl_manual
                    state.best_trade         = max(state.best_trade, pnl_manual)
                    result = "WIN (manual)"
                elif pnl_manual < 0:
                    state.losses             += 1
                    state.consecutive_losses += 1
                    state.total_loss_pnl     += abs(pnl_manual)
                    state.worst_trade         = min(state.worst_trade, pnl_manual)
                    result = "LOSS (manual)"
                else:
                    result = "BE (manual)"

                bars = state.open_trade.get("open_bar", 0)
                append_closed_trade(state.open_trade, pnl_manual, current_price, result, bars)

                logging.warning(
                    f"[RECONCILE] {sig_name} @ {state.open_trade.get('entry', 0):.2f} "
                    f"non trovato su Bitget → chiuso manualmente. "
                    f"P&L={pnl_manual:+.4f}$ | capitale {state.capital:.2f}$ [{result}]"
                )
                state.open_trade = None
                save_state(state)

            # ── Hook 1: sync posizione da Bitget (SL/TP hit in background) ──
            if executor and executor.has_open_position:
                bnx_pnl = executor.sync_position()
                if bnx_pnl is not None and state.open_trade is None:
                    # Posizione orfana (close fallito in passato, trade già chiuso
                    # internamente): SL/TP nativo l'ha chiusa ora → registra solo il
                    # P&L reale sul capitale, niente statistiche (trade già contato).
                    state.capital   += bnx_pnl
                    state.daily_pnl += bnx_pnl
                    logging.warning(
                        f"[SYNC ORFANA] Bitget ha chiuso una posizione orfana | "
                        f"P&L={bnx_pnl:+.2f} USDT | Cap=${state.capital:.2f}"
                    )
                    save_state(state)
                elif bnx_pnl is not None and state.open_trade is not None:
                    pnl_usdt = bnx_pnl
                    state.capital    += pnl_usdt
                    state.daily_pnl  += pnl_usdt
                    state.total_trades += 1
                    bars = state.open_trade.get("open_bar", 1)
                    if pnl_usdt > 0:
                        state.wins               += 1
                        state.consecutive_losses  = 0
                        state.total_win_pnl      += pnl_usdt
                        state.best_trade          = max(state.best_trade, pnl_usdt)
                        outcome = "WIN (exch)"
                    elif pnl_usdt < 0:
                        state.losses             += 1
                        state.consecutive_losses += 1
                        state.total_loss_pnl     += abs(pnl_usdt)
                        state.worst_trade         = min(state.worst_trade, pnl_usdt)
                        outcome = "LOSS (exch)"
                    else:
                        state.consecutive_losses  = 0
                        outcome = "BE (exch)"
                    wr = state.wins / state.total_trades * 100 if state.total_trades else 0
                    logging.info(
                        f"[{outcome}] Bitget ha chiuso {state.open_trade['signal'].name} "
                        f"P&L={pnl_usdt:+.2f} USDT | Cap=${state.capital:.2f} | "
                        f"W/L={state.wins}/{state.losses} ({wr:.0f}%)"
                    )
                    append_closed_trade(state.open_trade, pnl_usdt, current_price, outcome, bars)
                    state.open_trade      = None
                    state.bars_since_close = 0
                    save_state(state)

            # Controlla trade aperto
            if state.open_trade is not None:
                pnl = check_open_trade(state, current_price, sr_levels=sr_levels)
                if pnl is not None:
                    state.capital    += pnl
                    state.daily_pnl  += pnl
                    state.total_trades += 1
                    bars = state.open_trade.get("open_bar", 1)

                    was_trail = state.open_trade.get("trail_exit", False)

                    if pnl > 0:
                        state.wins          += 1
                        state.consecutive_losses = 0
                        state.total_win_pnl += pnl
                        state.best_trade     = max(state.best_trade, pnl)
                        outcome = "WIN (trail)" if was_trail else "WIN "
                    elif pnl == 0:
                        state.consecutive_losses = 0
                        outcome = "BE  (trail)"
                    else:
                        state.losses        += 1
                        state.consecutive_losses += 1
                        state.total_loss_pnl += abs(pnl)
                        state.worst_trade    = min(state.worst_trade, pnl)
                        outcome = "LOSS"

                    wr = state.wins / state.total_trades * 100
                    logging.info(
                        f"[{outcome}] {state.open_trade['signal'].name} "
                        f"entry={state.open_trade['entry']:.2f} "
                        f"close={current_price:.2f} "
                        f"P&L=${pnl:+.2f} | Cap=${state.capital:.2f} | "
                        f"W/L={state.wins}/{state.losses} ({wr:.0f}%) | "
                        f"consec.losses={state.consecutive_losses}"
                    )
                    append_closed_trade(
                        state.open_trade, pnl, current_price,
                        outcome.strip(), bars
                    )
                    # ── Hook 2: notifica chiusura all'executor ──────────────
                    if executor:
                        executor.on_close(outcome.strip())
                    state.open_trade      = None
                    state.bars_since_close = 0
                    save_state(state)

                elif state.open_trade is not None:
                    # ── Hook 3: aggiorna trailing SL su Binance se attivato ─
                    t = state.open_trade
                    if executor and t.get("trailing_active") and t.get("trailing_sl"):
                        prev_sl = t.get("_bnx_last_sl")
                        if prev_sl != t["trailing_sl"]:
                            executor.update_sl(t["trailing_sl"])
                            t["_bnx_last_sl"] = t["trailing_sl"]

            # Circuit breaker
            if circuit_breaker_active(state):
                logging.info(
                    f"[PAUSE] Cooldown={state.cooldown_remaining} barre | "
                    f"Price={current_price:.2f} | bias4h={bias_4h.name} bias1h={bias_1h.name}"
                )
                update_dashboard(state, current_price, bias_4h, bias_1h, adx, atr, rsi_5m,
                                sr_levels, fear_greed, funding_rate)
                save_state(state)
                sleep_until_next_candle()
                continue

            # Valuta entry
            status = (
                f"Price={current_price:.2f} | ADX={adx:.1f} | "
                f"ATR={atr:.2f} | RSI={rsi_5m:.1f} | "
                f"bias4h={bias_4h.name} bias1h={bias_1h.name} | "
                f"W/L={state.wins}/{state.losses}"
            )

            if state.open_trade is None:
                # Cooldown 2 barre dopo chiusura (evita re-entry istantanea)
                MIN_BARS_AFTER_CLOSE = 2
                if state.bars_since_close < MIN_BARS_AFTER_CLOSE:
                    state.bars_since_close += 1
                    logging.info(f"[COOLDOWN] {state.bars_since_close}/{MIN_BARS_AFTER_CLOSE} barre dopo chiusura — skip entry")
                    update_dashboard(state, current_price, bias_4h, bias_1h, adx, atr, rsi_5m,
                                sr_levels, fear_greed, funding_rate)
                    sleep_until_next_candle()
                    continue

                trade_mode = "trend"
                sig = entry_signal(df_5m, bias_1h, bias_4h, bias_2h, sr_levels=sr_levels)
                if sig == Signal.HOLD:
                    sig = bounce_signal(df_5m, df_1h, sr_levels, funding_rate, fear_greed)
                    if sig != Signal.HOLD:
                        trade_mode = "bounce"
                # ── Hook 4: filtro orario executor (es. 08-22 UTC) ─────────
                if sig != Signal.HOLD and executor and not executor.can_trade():
                    sig = Signal.HOLD
                if sig != Signal.HOLD:
                    trade = calculate_trade(sig, last_5m, state.capital,
                                            bias_2h=bias_2h, bias_4h=bias_4h,
                                            sr_levels=sr_levels)
                    if trade:
                        trade["mode"] = trade_mode
                        state.open_trade = trade
                        save_state(state)
                        logging.info(
                            f"[OPEN {sig.name} {trade_mode.upper()}] "
                            f"entry={trade['entry']:.2f} "
                            f"TP={trade['tp']:.2f} SL={trade['sl']:.2f} | "
                            f"Risk=${trade['risk_amount']:.2f} R:R={trade['rr']} | {status}"
                        )
                        # ── Hook 5: esegui su Binance ───────────────────────
                        if executor:
                            ok = executor.enter(sig, trade['entry'],
                                                trade['sl'], trade['tp'],
                                                trade['size'],
                                                atr=trade.get('atr', 0.0))
                            if not ok:
                                logging.error("[OPEN FAIL] Executor ha rifiutato l'ordine — trade annullato")
                                state.open_trade = None
                                save_state(state)
                    else:
                        logging.info(f"[HOLD] Segnale ma trade non valido | {status}")
                else:
                    logging.info(f"[HOLD] {status}")
            else:
                t = state.open_trade
                dist_tp = abs(current_price - t["tp"])
                # Se trailing attivo mostra trailing_sl invece del SL originale
                if t.get("trailing_active"):
                    active_sl  = t["trailing_sl"]
                    sl_label   = "TRAIL_SL"
                else:
                    active_sl  = t["sl"]
                    sl_label   = "SL"
                dist_sl = abs(current_price - active_sl)
                progress_pct = 0.0
                if t["signal"] == Signal.LONG and (t["tp"] - t["entry"]) > 0:
                    progress_pct = (current_price - t["entry"]) / (t["tp"] - t["entry"]) * 100
                elif t["signal"] == Signal.SHORT and (t["entry"] - t["tp"]) > 0:
                    progress_pct = (t["entry"] - current_price) / (t["entry"] - t["tp"]) * 100
                logging.info(
                    f"[{t['signal'].name} OPEN] entry={t['entry']:.2f} "
                    f"now={current_price:.2f} ({progress_pct:.0f}% verso TP) "
                    f"TP={t['tp']:.2f}(Δ{dist_tp:.0f}) "
                    f"{sl_label}={active_sl:.2f}(Δ{dist_sl:.0f}) | "
                    f"bias4h={bias_4h.name} bias1h={bias_1h.name}"
                )

            # Aggiorna dashboard ogni ciclo
            update_dashboard(state, current_price, bias_4h, bias_1h, adx, atr, rsi_5m,
                             sr_levels, fear_greed, funding_rate)
            sleep_until_next_candle()

        except KeyboardInterrupt:
            wr = (state.wins / state.total_trades * 100) if state.total_trades else 0
            logging.info(
                f"[STOP] Bot fermato. Cap=${state.capital:.2f} | "
                f"W/L={state.wins}/{state.losses} ({wr:.0f}%)"
            )
            save_state(state)
            break
        except Exception as e:
            logging.error(f"[ERROR] {e}", exc_info=True)
            time.sleep(60)


if __name__ == "__main__":
    import os, logging as _log
    # Inizializza logging subito così i messaggi dell'executor appaiono all'avvio
    _log.basicConfig(level=_log.INFO,
                     format="%(asctime)s | %(levelname)-8s | %(message)s",
                     datefmt="%Y-%m-%d %H:%M:%S")

    _exchange = os.environ.get("EXECUTOR", "").lower()   # bybit | binance-futures | binance-spot | binance-margin

    if _exchange == "bybit":
        from bybit_futures_executor import BybitFuturesExecutor
        run(executor=BybitFuturesExecutor())

    elif _exchange == "binance-futures":
        from binance_futures_executor import BinanceFuturesExecutor
        run(executor=BinanceFuturesExecutor())

    elif _exchange == "bitget":
        from bitget_futures_executor import BitgetFuturesExecutor
        run(executor=BitgetFuturesExecutor())

    elif _exchange in ("binance-spot", "binance-margin"):
        os.environ.setdefault("BINANCE_MODE", "margin" if _exchange == "binance-margin" else "spot")
        from binance_spot_executor import BinanceSpotExecutor
        run(executor=BinanceSpotExecutor())

    else:
        run()   # simulazione pura (default, nessun executor)
