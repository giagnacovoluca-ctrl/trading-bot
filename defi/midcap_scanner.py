"""
midcap_scanner.py
=================
Scanner mid/large cap coin con Bollinger Band Squeeze + reversal strutturale.

Strategia:
  1. Universo: top 500 coin CoinGecko → filtro mcap/volume/categoria
  2. Fetch candele daily in PARALLELO via ccxt.async_support (Binance, ~10s per 150 coin)
  3. BB Squeeze: band width ai minimi storici → "calma prima della tempesta"
     Breakout + espansione bande → segnale d'ingresso
  4. Conferme: EMA stack bullish, HH/HL, RSI, momentum 30d
  5. Score 0–100. Soglia 60 → email + CSV

Output: defi/reports/midcap_signals.csv
Integrazione run.py: --no-midcap per skippare
"""

import asyncio
import smtplib
import logging
import os
import csv
import time
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import ccxt.async_support as ccxt
import pandas as pd
import numpy as np

log = logging.getLogger("midcap")

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

MIN_MCAP_USD        = 50_000_000        # $50M mcap minimo
MAX_MCAP_USD        = 10_000_000_000    # $10B mcap massimo
MIN_VOLUME_24H      = 3_000_000         # $3M volume 24h minimo (liquidità)
SCORE_MIN           = 60                # soglia per email/CSV
SCAN_INTERVAL_H     = 4                 # ogni 4h → ~900 call/mese base (tot. ~4.200/10.000 con enrich)
CANDLES_LIMIT       = 180              # ~6 mesi di daily
FETCH_CONCURRENCY   = 20               # richieste parallele max (rispetta rate limit)
TOP_N_EMAIL         = 10               # top N coin nell'email
CG_PAGES            = 8               # pagine CoinGecko (100 coin/pagina → 800 coin universo)
ENRICH_MIN_SCORE    = 50              # score minimo per fetchare /coins/{id} (enrich fondamentali)
ENRICH_MAX          = 25              # max coin da enrichire per ciclo (cap call budget)

BB_PERIOD           = 20
BB_STD              = 2.0
BB_SQUEEZE_PCTILE   = 0.20             # bottom 20% band width storici = squeeze
BB_LOOKBACK         = 60               # finestra percentile band width

EXCLUDE_SYMBOLS = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDP",
                   "WBTC", "WETH", "STETH", "WSTETH"}
EXCLUDE_CATEGORIES = {"meme-token", "meme", "fan-token"}

REPORTS_DIR = Path(__file__).parent / "reports"
SIGNALS_CSV = REPORTS_DIR / "midcap_signals.csv"

def _env(k, d=""): return os.environ.get(k, d)

# CoinGecko Demo key — stessa di gemmeV3.py, quota condivisa ~0.5 call/min totale
CG_API_KEY  = _env("COINGECKO_API_KEY", "")
_CG_HEADERS = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}

# Email (stessa config di defi_optimized)
_EMAIL_CFG = {
    "host":     _env("SMTP_HOST",     "smtp.gmail.com"),
    "port":     int(_env("SMTP_PORT", "587")),
    "user":     _env("SMTP_USER",     "giagnacovo.luca@gmail.com"),
    "password": _env("SMTP_PASSWORD", ""),
    "from":     _env("SMTP_FROM",     "giagnacovo.luca@gmail.com"),
    "to":       _env("SMTP_TO",       "giagnacovo.luca@gmail.com"),
}


# ─────────────────────────────────────────────────────────────────────────────
# COINGECKO — universo coin con fondamentali (sync, 1 chiamata per ciclo)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_coingecko_universe() -> dict:
    """
    Ritorna {SYMBOL: {mcap, volume_24h, change_7d, change_30d, categories, name}}.
    Fetch top 500 per market cap (5 pagine × 100). Rate limit: 0.5s tra pagine.
    """
    universe: dict = {}
    session = requests.Session()
    for page in range(1, CG_PAGES + 1):
        try:
            r = session.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 100,
                    "page": page,
                    "sparkline": "false",
                    "price_change_percentage": "7d,30d",
                },
                headers=_CG_HEADERS,
                timeout=15,
            )
            r.raise_for_status()
            for c in r.json():
                sym = (c.get("symbol") or "").upper()
                if not sym or sym in EXCLUDE_SYMBOLS:
                    continue
                universe[sym] = {
                    "id":         c.get("id", ""),
                    "name":       c.get("name", sym),
                    "mcap":       c.get("market_cap") or 0,
                    "volume_24h": c.get("total_volume") or 0,
                    "change_7d":  c.get("price_change_percentage_7d_in_currency") or 0,
                    "change_30d": c.get("price_change_percentage_30d_in_currency") or 0,
                    "categories": [],
                }
            time.sleep(0.5)
        except Exception as e:
            log.warning(f"[CoinGecko] page {page} fallita: {e}")
            break

    log.info(f"[CoinGecko] {len(universe)} coin caricate")
    return universe


# ─────────────────────────────────────────────────────────────────────────────
# FETCH OHLCV ASYNC — ccxt.async_support + semaforo per rate limit
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_one(exchange: ccxt.Exchange, symbol: str,
                     sem: asyncio.Semaphore) -> tuple[str, Optional[list]]:
    async with sem:
        try:
            ohlcv = await exchange.fetch_ohlcv(symbol, "1d", limit=CANDLES_LIMIT)
            return symbol, ohlcv
        except Exception as e:
            log.debug(f"[OHLCV] {symbol} skip: {e}")
            return symbol, None


async def fetch_all_ohlcv(symbols: list[str]) -> dict[str, list]:
    """Fetch parallelo OHLCV daily per tutti i simboli. Ritorna {symbol: ohlcv}."""
    exchange = ccxt.binance({"enableRateLimit": True})
    sem      = asyncio.Semaphore(FETCH_CONCURRENCY)
    try:
        results = await asyncio.gather(
            *[_fetch_one(exchange, s, sem) for s in symbols],
            return_exceptions=False,
        )
    finally:
        await exchange.close()
    return {sym: data for sym, data in results if data}


# ─────────────────────────────────────────────────────────────────────────────
# ANALISI TECNICA + SCORING
# ─────────────────────────────────────────────────────────────────────────────

def _ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def _rsi(s: pd.Series, p: int = 14) -> pd.Series:
    d = s.diff()
    return 100 - 100 / (1 + d.clip(lower=0).ewm(p, adjust=False).mean() /
                             (-d.clip(upper=0).ewm(p, adjust=False).mean() + 1e-9))

def _adx(df: pd.DataFrame, p: int = 14) -> float:
    prev_c = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_c).abs(),
                    (df["low"]  - prev_c).abs()], axis=1).max(axis=1)
    atr = tr.ewm(p, adjust=False).mean()
    prev_h, prev_l = df["high"].shift(1), df["low"].shift(1)
    dmp = (df["high"] - prev_h).clip(lower=0)
    dmm = (prev_l - df["low"]).clip(lower=0)
    dmp = dmp.where(dmp > dmm, 0.0)
    dmm = dmm.where(dmm > dmp,  0.0)
    eps = 1e-9
    dip = 100 * dmp.ewm(p, adjust=False).mean() / (atr + eps)
    dim = 100 * dmm.ewm(p, adjust=False).mean() / (atr + eps)
    dx  = 100 * (dip - dim).abs() / (dip + dim + eps)
    return float(dx.ewm(p, adjust=False).mean().iloc[-1])


def analyze_coin(symbol: str, ohlcv: list, cg: dict) -> Optional[dict]:
    """
    Score 0–100 per la coin. None se dati insufficienti o filtrata.

    BB Squeeze  (40 pt): squeeze recente + espansione + breakout bullish
    Tecnico     (35 pt): EMA stack + prezzo + HH/HL semplificato + RSI
    Momentum    (25 pt): ret_30d + % giorni positivi + volume ratio verde/rosso
    """
    # ── Filtro fondamentali ───────────────────────────────────────────────────
    mcap = cg.get("mcap", 0)
    vol  = cg.get("volume_24h", 0)
    if mcap < MIN_MCAP_USD or mcap > MAX_MCAP_USD:
        return None
    if vol < MIN_VOLUME_24H:
        return None

    if not ohlcv or len(ohlcv) < BB_LOOKBACK + BB_PERIOD:
        return None

    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df[["open", "high", "low", "close", "volume"]] = \
        df[["open", "high", "low", "close", "volume"]].astype(float)
    df = df.dropna().reset_index(drop=True)
    if len(df) < 80:
        return None

    # Indicatori
    df["ema20"]  = _ema(df["close"], 20)
    df["ema50"]  = _ema(df["close"], 50)
    df["ema100"] = _ema(df["close"], 100)
    df["rsi"]    = _rsi(df["close"], 14)

    # Bollinger Bands
    df["bb_mid"]   = df["close"].rolling(BB_PERIOD).mean()
    df["bb_std"]   = df["close"].rolling(BB_PERIOD).std()
    df["bb_upper"] = df["bb_mid"] + BB_STD * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - BB_STD * df["bb_std"]
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / (df["bb_mid"] + 1e-9)

    # Percentile rank band width su BB_LOOKBACK barre
    df["bb_wpct"] = df["bb_width"].rolling(BB_LOOKBACK).rank(pct=True)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # ── BB base ───────────────────────────────────────────────────────────────
    bb_wpct_val = float(last["bb_wpct"]) if pd.notna(last["bb_wpct"]) else 0.5
    breakout_up = bool(last["close"] > last["bb_upper"])
    breakout_dn = bool(last["close"] < last["bb_lower"])

    # Durata squeeze: quante barre consecutive ai minimi di band width
    sq_duration = 0
    for i in range(len(df) - 2, max(len(df) - 31, 0), -1):
        wpct = df["bb_wpct"].iloc[i]
        if pd.notna(wpct) and wpct < BB_SQUEEZE_PCTILE:
            sq_duration += 1
        else:
            break

    # Durata espansione: quante barre consecutive in cui la width cresce
    expand_bars = 0
    for i in range(len(df) - 2, max(len(df) - 11, 0), -1):
        if df["bb_width"].iloc[i] > df["bb_width"].iloc[i - 1]:
            expand_bars += 1
        else:
            break

    # Posizione prezzo dentro le bande: 0 = banda bassa, 1 = banda alta
    band_range = float(last["bb_upper"] - last["bb_lower"])
    price_lean = (float(last["close"]) - float(last["bb_lower"])) / band_range \
                 if band_range > 1e-9 else 0.5

    # ── Struttura tecnica ─────────────────────────────────────────────────────
    ema_bull = bool(last["ema20"] > last["ema50"] > last["ema100"])
    rsi_val  = float(last["rsi"])
    adx_val  = _adx(df, 14)

    hh_hl = False
    if len(df) >= 31:
        c10   = float(df["close"].iloc[-11])
        c20   = float(df["close"].iloc[-21])
        hh_hl = float(last["close"]) > c10 > c20

    # ── RSI divergenza bullish (leading) ─────────────────────────────────────
    # Prezzo fermo/in calo, RSI sale = pressione di acquisto nascosta
    rsi_divergence = False
    rsi_slope_val  = 0.0
    if len(df) >= 7:
        price_slope    = (float(last["close"]) - float(df["close"].iloc[-7])) / \
                         (float(df["close"].iloc[-7]) + 1e-9)
        rsi_slope_val  = float(last["rsi"]) - float(df["rsi"].iloc[-7])
        rsi_divergence = price_slope <= 0.03 and rsi_slope_val > 2.0

    # ── Volume accumulation durante squeeze ───────────────────────────────────
    # Pattern ideale: volume cala mentre le bande si comprimono, poi spika
    vol_accum = False
    vol_spike  = False
    if len(df) >= 12:
        vol_squeeze_avg = float(df["volume"].iloc[-10:-2].mean())
        vol_last2_avg   = float(df["volume"].iloc[-2:].mean())
        vol_mid10       = float(df["volume"].iloc[-12:-2].mean())
        # Volume calante durante squeeze = distribuzione assente = accumulation
        vol_declining   = df["volume"].iloc[-8:-2].is_monotonic_decreasing
        vol_accum = vol_declining and sq_duration >= 2
        # Spike finale: volume ultime 2 barre > 1.3× media squeeze
        vol_spike = vol_last2_avg > vol_mid10 * 1.3

    # ── Momentum 30d (contesto, non più il driver principale) ─────────────────
    ret_30d = pct_pos = vol_ratio = 0.0
    if len(df) >= 31:
        last30  = df.iloc[-31:-1]
        ret_30d = (float(last["close"]) - float(df.iloc[-31]["close"])) / \
                  (float(df.iloc[-31]["close"]) + 1e-9)
        pct_pos = float((last30["close"] > last30["open"]).mean())
        vg = last30.loc[last30["close"] >  last30["open"], "volume"].mean()
        vr = last30.loc[last30["close"] <= last30["open"], "volume"].mean()
        vol_ratio = float(vg / (vr + 1e-9))

    # ─────────────────────────────────────────────────────────────────────────
    # SCORING — filosofia: predire il breakout, non confermarlo
    #
    # 1. Intensità squeeze    (max 25)  quanto è compressa l'energia
    # 2. Durata squeeze       (max 15)  quanto a lungo si è accumulata
    # 3. Espansione bande     (max 15)  sta iniziando a esplodere?
    # 4. Lean prezzo          (max  8)  dove è il prezzo DENTRO la compressione
    # 5. RSI divergenza       (max 10)  forza nascosta mentre il prezzo è fermo
    # 6. Volume accumulation  (max  7)  smart money pattern
    # 7. Struttura EMA + HH/HL(max 15) contesto trend
    # 8. Breakout (bonus)     (max  5)  conferma tardiva, peso ridotto
    # ─────────────────────────────────────────────────────────────────────────
    score = 0

    # 1. Intensità squeeze (25 pt) — più stretto = più energia
    if   bb_wpct_val < 0.05:  score += 25
    elif bb_wpct_val < 0.10:  score += 18
    elif bb_wpct_val < 0.20:  score += 12
    elif bb_wpct_val < 0.30:  score += 5

    # 2. Durata squeeze (15 pt) — più giorni compresso = esplosione più potente
    if   sq_duration >= 10:  score += 15
    elif sq_duration >= 5:   score += 10
    elif sq_duration >= 2:   score += 5

    # 3. Espansione bande (15 pt) — quante barre consecutive si aprono
    if   expand_bars >= 3:  score += 15
    elif expand_bars >= 2:  score += 10
    elif expand_bars >= 1:  score += 5

    # 4. Lean prezzo dentro le bande (8 pt) — posizione bullish pre-breakout
    if   price_lean > 0.70:  score += 8   # upper 30%: buyers in controllo
    elif price_lean > 0.50:  score += 4   # sopra la media: lieve vantaggio

    # 5. RSI divergenza bullish (10 pt) — il leading indicator più potente
    if rsi_divergence:
        score += 10
    elif rsi_slope_val > 1.0 and rsi_val < 60:
        score += 4   # RSI sale senza divergenza classica ma in zona neutrale

    # 6. Volume accumulation (7 pt) — smart money entra in silenzio
    if vol_accum and vol_spike:  score += 7   # pattern completo
    elif vol_accum:              score += 4   # solo calo volume in squeeze
    elif vol_spike:              score += 3   # solo spike finale

    # 7. Struttura EMA + HH/HL (15 pt) — contesto macro
    if ema_bull:  score += 10
    if hh_hl:     score += 5

    # 8. Breakout (5 pt) — bonus tardivo, segnale già visto dal mercato
    if breakout_up and ema_bull:  score += 5
    elif breakout_up:             score += 2

    # Direzione
    if (ema_bull or price_lean > 0.6) and not breakout_dn:
        direction = "LONG"
    elif breakout_dn and not ema_bull:
        direction = "SHORT"
    else:
        direction = "NEUTRAL"

    return {
        "symbol":        symbol,
        "name":          cg.get("name", symbol),
        "score":         score,
        "direction":     direction,
        "price":         round(float(last["close"]), 6),
        # BB pre-breakout
        "bb_wpct":       round(bb_wpct_val * 100, 1),
        "sq_duration":   sq_duration,
        "expand_bars":   expand_bars,
        "price_lean":    round(price_lean * 100, 1),
        "bb_breakout":   breakout_up,
        # Leading indicators
        "rsi_divergence": rsi_divergence,
        "vol_accum":      vol_accum,
        "vol_spike":      vol_spike,
        # Struttura
        "ema_bull":      ema_bull,
        "hh_hl":         hh_hl,
        "rsi":           round(rsi_val, 1),
        "adx":           round(adx_val, 1),
        # Momentum (contesto)
        "ret_30d":       round(ret_30d * 100, 1),
        "pct_pos":       round(pct_pos * 100, 1),
        "vol_ratio":     round(vol_ratio, 2),
        # Fondamentali
        "mcap_m":        round(mcap / 1e6, 1),
        "volume_24h_m":  round(vol / 1e6, 1),
        "change_7d":     round(cg.get("change_7d", 0), 1),
        "ts":            datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────────────────────────────────────

def _send_email(signals: list[dict]) -> bool:
    cfg = _EMAIL_CFG
    if not cfg["user"] or not cfg["password"]:
        log.warning("[email] Credenziali SMTP mancanti — email non inviata.")
        return False
    try:
        rows = ""
        for s in signals:
            sq_icon = "🔴" if s["bb_squeeze"] else ""
            bo_icon = "⚡" if s["bb_breakout"] else ""
            rows += (
                f"<tr>"
                f"<td><b>{s['symbol']}</b><br><small>{s['name']}</small></td>"
                f"<td align='center'><b>{s['score']}</b></td>"
                f"<td align='center'>{s['direction']}</td>"
                f"<td align='center'>${s['price']:,.4f}</td>"
                f"<td align='center'>{sq_icon}{bo_icon} {s['bb_wpct']}%ile</td>"
                f"<td align='center'>{s['ret_30d']:+.1f}%</td>"
                f"<td align='center'>{s['rsi']:.1f}</td>"
                f"<td align='center'>${s['mcap_m']:.0f}M</td>"
                f"</tr>"
            )

        html = f"""
        <html><body style="font-family:monospace;background:#0d1117;color:#c9d1d9;padding:20px">
        <h2 style="color:#58a6ff">📊 Midcap Scanner — {datetime.now().strftime('%Y-%m-%d %H:%M')}</h2>
        <p>{len(signals)} segnali (score ≥ {SCORE_MIN})</p>
        <table border="1" cellpadding="6" cellspacing="0"
               style="border-collapse:collapse;border-color:#30363d;width:100%">
          <tr style="background:#161b22;color:#8b949e">
            <th>Coin</th><th>Score</th><th>Dir.</th><th>Prezzo</th>
            <th>BB Squeeze</th><th>Ret 30d</th><th>RSI</th><th>MCap</th>
          </tr>
          {rows}
        </table>
        <br>
        <details><summary style="color:#8b949e;cursor:pointer">Legenda</summary>
        <p style="color:#8b949e;font-size:12px">
        🔴 = BB Squeeze attivo (calma prima della tempesta)<br>
        ⚡ = Breakout sopra banda superiore<br>
        Score: BB(40) + Tecnico(35) + Momentum(25)
        </p></details>
        </body></html>
        """
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[MidCap] {len(signals)} segnali — top: {signals[0]['symbol']} ({signals[0]['score']}pt)"
        msg["From"]    = cfg["from"] or cfg["user"]
        msg["To"]      = cfg["to"]
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(cfg["user"], cfg["password"])
            srv.sendmail(cfg["user"], cfg["to"], msg.as_string())
        log.info(f"[email] Inviata: {len(signals)} segnali")
        return True
    except Exception as e:
        log.error(f"[email] Fallita: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# CSV
# ─────────────────────────────────────────────────────────────────────────────

_CSV_FIELDS = [
    "ts", "symbol", "name", "score", "direction", "price",
    "bb_wpct", "sq_duration", "expand_bars", "price_lean", "bb_breakout",
    "rsi_divergence", "vol_accum", "vol_spike",
    "ema_bull", "hh_hl", "rsi", "adx",
    "ret_30d", "pct_pos", "vol_ratio",
    "mcap_m", "volume_24h_m", "change_7d",
    "dev_score", "comm_score", "age_days", "fund_delta",
]

def _append_csv(signals: list[dict]):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    write_header = not SIGNALS_CSV.exists()
    with open(SIGNALS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerows(signals)
    log.info(f"[CSV] {len(signals)} segnali → {SIGNALS_CSV}")


# ─────────────────────────────────────────────────────────────────────────────
# ENRICH FONDAMENTALI — /coins/{id} per i candidati migliori
# ─────────────────────────────────────────────────────────────────────────────

def enrich_fundamentals(candidates: list[dict], universe: dict) -> list[dict]:
    """
    Per i top candidati (score ≥ ENRICH_MIN_SCORE) fetcha /coins/{id} da CoinGecko:
      - developer_score  → attività GitHub (commit, PR, issue)
      - community_score  → engagement social
      - genesis_date     → età del progetto (filtra progetti nuovissimi/zombie)
      - coingecko_rank   → posizionamento globale

    Aggiunge un bonus/malus allo score e filtra quelli con fondamenta morte.
    Budget: max ENRICH_MAX chiamate per ciclo.
    """
    session  = requests.Session()
    enriched = []
    calls    = 0

    for sig in candidates:
        coin_sym = sig["symbol"].replace("/USDT", "")
        cg_id    = universe.get(coin_sym, {}).get("id", "")

        if not cg_id or calls >= ENRICH_MAX:
            sig["dev_score"]   = None
            sig["comm_score"]  = None
            sig["age_days"]    = None
            enriched.append(sig)
            continue

        try:
            r = session.get(
                f"https://api.coingecko.com/api/v3/coins/{cg_id}",
                params={
                    "localization":   "false",
                    "tickers":        "false",
                    "market_data":    "false",
                    "community_data": "true",
                    "developer_data": "true",
                },
                headers=_CG_HEADERS,
                timeout=12,
            )
            calls += 1
            r.raise_for_status()
            data = r.json()

            dev_score  = data.get("developer_score")   or 0.0
            comm_score = data.get("community_score")   or 0.0
            genesis    = data.get("genesis_date")      or ""
            cg_rank    = data.get("coingecko_rank")    or 999

            age_days = None
            if genesis:
                try:
                    from datetime import date
                    gd       = date.fromisoformat(genesis)
                    age_days = (date.today() - gd).days
                except Exception:
                    pass

            sig["dev_score"]   = round(float(dev_score),  1)
            sig["comm_score"]  = round(float(comm_score), 1)
            sig["age_days"]    = age_days
            sig["cg_rank"]     = cg_rank

            # Bonus/malus fondamentali (max ±15 pt)
            fund_delta = 0
            if dev_score  >= 70:  fund_delta += 8
            elif dev_score >= 50: fund_delta += 4
            elif dev_score <  20: fund_delta -= 8   # progetto zombie

            if comm_score >= 60:  fund_delta += 4
            elif comm_score >= 40: fund_delta += 2

            if age_days is not None:
                if age_days >= 730:   fund_delta += 3   # >2 anni: progetto maturo
                elif age_days < 180:  fund_delta -= 5   # <6 mesi: troppo nuovo

            sig["score"]      = max(0, sig["score"] + fund_delta)
            sig["fund_delta"] = fund_delta

            log.info(
                f"[enrich] {coin_sym}: dev={dev_score:.0f} "
                f"comm={comm_score:.0f} age={age_days}d → Δ{fund_delta:+d}"
            )
            time.sleep(0.4)   # 30 req/min CoinGecko Demo

        except Exception as e:
            log.debug(f"[enrich] {coin_sym} fallito: {e}")
            sig["dev_score"]  = None
            sig["comm_score"] = None
            sig["age_days"]   = None
            sig["fund_delta"] = 0

        enriched.append(sig)

    log.info(f"[enrich] {calls} chiamate CoinGecko per {len(enriched)} candidati")
    return enriched


# ─────────────────────────────────────────────────────────────────────────────
# SCAN PRINCIPALE
# ─────────────────────────────────────────────────────────────────────────────

async def _scan_once() -> list[dict]:
    """Esegue una singola scansione completa. Ritorna i segnali trovati."""
    t0 = time.time()
    log.info("[midcap] ── Avvio scansione ──")

    # 1. Universo CoinGecko (sync in thread per non bloccare l'event loop)
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=1) as ex:
        universe = await loop.run_in_executor(ex, fetch_coingecko_universe)

    if not universe:
        log.error("[midcap] Universo vuoto — salto scansione")
        return []

    # 2. Costruisci simboli Binance validi (filtro mcap/volume pre-fetch)
    valid_cg = {
        sym: data for sym, data in universe.items()
        if MIN_MCAP_USD <= data["mcap"] <= MAX_MCAP_USD
        and data["volume_24h"] >= MIN_VOLUME_24H
    }
    binance_symbols = [f"{sym}/USDT" for sym in valid_cg]
    log.info(f"[midcap] {len(binance_symbols)} simboli candidati dopo filtro mcap/vol")

    # 3. Fetch OHLCV async in parallelo
    ohlcv_map = await fetch_all_ohlcv(binance_symbols)
    log.info(f"[midcap] {len(ohlcv_map)} OHLCV scaricati in {time.time()-t0:.1f}s")

    # 4. Analizza ogni coin → candidati sopra soglia pre-enrich
    candidates = []
    for b_sym, ohlcv in ohlcv_map.items():
        coin_sym = b_sym.replace("/USDT", "")
        cg_data  = valid_cg.get(coin_sym, {})
        result   = analyze_coin(b_sym, ohlcv, cg_data)
        if result and result["score"] >= ENRICH_MIN_SCORE and result["direction"] == "LONG":
            candidates.append(result)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info(f"[midcap] {len(candidates)} candidati pre-enrich (score ≥ {ENRICH_MIN_SCORE})")

    # 5. Enrich fondamentali sui migliori (dev_score, age, community)
    enriched = []
    if candidates:
        with ThreadPoolExecutor(max_workers=1) as ex:
            enriched = await loop.run_in_executor(
                ex, enrich_fundamentals, candidates[:ENRICH_MAX], valid_cg
            )

    # 6. Filtro finale: score ≥ SCORE_MIN dopo enrich
    signals = [s for s in enriched if s["score"] >= SCORE_MIN]
    signals.sort(key=lambda x: x["score"], reverse=True)

    elapsed = time.time() - t0
    log.info(
        f"[midcap] Scansione completata in {elapsed:.1f}s — "
        f"{len(signals)} segnali LONG (score ≥ {SCORE_MIN})"
    )

    if signals:
        _append_csv(signals)
        _send_email(signals[:TOP_N_EMAIL])
        for s in signals[:10]:
            sq  = f"SQ{s['sq_duration']}d" if s["sq_duration"] >= 2 else ""
            exp = f"EXP{s['expand_bars']}b" if s["expand_bars"] >= 1 else ""
            div = "DIV" if s["rsi_divergence"] else ""
            vac = "VOL+" if s["vol_spike"] else ""
            log.info(
                f"  [{s['score']:3d}] {s['symbol']:<10} {s['direction']} "
                f"bb={s['bb_wpct']}%ile lean={s['price_lean']}% "
                f"rsi={s['rsi']} ret30={s['ret_30d']:+.1f}% "
                f"{sq} {exp} {div} {vac}".rstrip()
            )
    else:
        log.info("[midcap] Nessun segnale sopra soglia in questo ciclo.")

    return signals


def main(stop_event=None):
    """Entry point per run.py. Loop ogni SCAN_INTERVAL_H ore."""
    import threading
    _stop = stop_event or threading.Event()

    log.info(
        f"[midcap] Avviato — intervallo {SCAN_INTERVAL_H}h | "
        f"mcap ${MIN_MCAP_USD/1e6:.0f}M–${MAX_MCAP_USD/1e9:.0f}B | "
        f"score ≥ {SCORE_MIN} | concurrency {FETCH_CONCURRENCY}"
    )

    while not _stop.is_set():
        try:
            asyncio.run(_scan_once())
        except Exception as e:
            log.error(f"[midcap] Errore scan: {e}", exc_info=True)

        # Attendi il prossimo ciclo (interrompibile)
        _stop.wait(SCAN_INTERVAL_H * 3600)

    log.info("[midcap] Fermato.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    asyncio.run(_scan_once())
