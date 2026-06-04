"""
==============================================================================
gemmeV3.py — Quality Gem Hunter  (NO-ML, Rule-Based Scoring)
==============================================================================

Pipeline di discovery (ogni 5 minuti):
  1. Dune Analytics    → token con smart-money inflow nelle ultime 24h
  2. DexScreener       → prezzi, liquidità, volume in real-time
  3. SocialAnalyzer    → social score via ntscraper (Nitter) / fallback neutro
  4. CoinGecko         → CEX listing check (Binance / Coinbase / OKX ...)
  5. GoPlus Security   → honeypot, tasse, LP locked (solo EVM)
  6. Holder Conc.      → top-10 holder % (rug risk) — Solscan / BSCScan
  7. GemFilter         → filtri hard (mcap, liq, età, wallets, BSR ...)
  8. RuleScorer        → scoring deterministico a punti ponderati (NO ML)
  9. GemTracker        → CSV, HTML report, email

Architettura a 3 strati:
  ┌─────────────────────────────────────────────────────────────────────┐
  │  DATA LAYER    → fetch (Dune, DexScreener, Social, CEX, GoPlus)     │
  │  LOGIC LAYER   → GemFilter (gate hard), feature engineering         │
  │  SCORING LAYER → RuleScorer → tier (DIAMOND/GOLD/SILVER/BRONZE)     │
  └─────────────────────────────────────────────────────────────────────┘

Chain supportate: Solana | BSC | Ethereum

⚠️  AVVISO: Solo a scopo educativo. NON garantisce profitti.
==============================================================================
"""

# ── Librerie standard ──────────────────────────────────────────────────────
import csv
import json
import logging
import os
import smtplib
import sys
import time
import threading
import warnings
from collections import defaultdict, deque
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import lru_cache
from pathlib import Path
from typing import Optional

# ── Librerie di terze parti ────────────────────────────────────────────────
import numpy as np
import requests

# ntscraper (fallback Nitter — twscrape rimosso: IP bloccato)
try:
    from ntscraper import Nitter
    NTSCRAPER_AVAILABLE = True
except ImportError:
    NTSCRAPER_AVAILABLE = False
    warnings.warn("ntscraper non installato. Social score disabilitato.")

try:
    sys.path.insert(0, str(Path(__file__).parent.parent / "defi"))
    from rugcheck import is_safe as rugcheck_safe
except ImportError:
    def rugcheck_safe(mint, scanner, chain="solana"): return True

# GemTracker (opzionale) — usa config V3 dedicata per evitare conflitti con gemmeV2
try:
    from gem_tracker import get_gem_tracker as _get_gt_base, GEM_TRACKER_AVAILABLE, GEM_TRACKER_CONFIG_V3
    def get_gem_tracker():
        return _get_gt_base(config=GEM_TRACKER_CONFIG_V3)
except ImportError:
    GEM_TRACKER_AVAILABLE = False
    def get_gem_tracker(): return None  # noqa

# Bridge con defi_optimized
try:
    import sys as _sys
    _parent = str(Path(__file__).parent.parent)
    if _parent not in _sys.path:
        _sys.path.insert(0, _parent)
    from gem_watchlist import write_gem_to_watchlist
    GEM_WATCHLIST_AVAILABLE = True
except ImportError:
    GEM_WATCHLIST_AVAILABLE = False
    def write_gem_to_watchlist(gem, **kw): return False  # noqa

# ==============================================================================
# SEZIONE 1 – LOGGING
# ==============================================================================

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))   # sempre GIT/gemme/

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(_BASE_DIR, "gem_hunter_v3.log"), encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ==============================================================================
# SEZIONE 2 – CONFIGURAZIONE
# ==============================================================================

DUNE_API_KEY        = os.environ.get("DUNE_API_KEY", "IBx3JQpjKGUg7RhVwHOZWxcKlnTE46Wk")
GOPLUS_API_KEY      = os.environ.get("GOPLUS_KEY", "")
BSCSCAN_KEY         = os.environ.get("BSCSCAN_KEY", "")
ETHERSCAN_KEY       = os.environ.get("ETHERSCAN_KEY", "")
COINGECKO_API_KEY   = os.environ.get("COINGECKO_API_KEY", "")
CMC_API_KEY         = os.environ.get("CMC_API_KEY", "")

CHAINS = {
    "solana": {
        "dexscreener_id":  "solana",
        "goplus_chain_id": "900",
        "is_evm":          False,
        "dune_chain_name": "solana",
        "min_liquidity":   15_000,
    },
    # "bsc": disabilitato — 16% WR, allineato con ALLOWED_CHAINS in trade_simulator
    # "ethereum": {
    #     "dexscreener_id":  "ethereum",
    #     "goplus_chain_id": "1",
    #     "is_evm":          True,
    #     "dune_chain_name": "ethereum",
    #     "min_liquidity":   30_000,
    # },
    "base": {
        "dexscreener_id":  "base",
        "goplus_chain_id": "8453",
        "is_evm":          True,
        "dune_chain_name": "base",
        "min_liquidity":   20_000,
    },
}

DUNE_BASE        = "https://api.dune.com/api/v1"
DEXSCREENER_BASE = "https://api.dexscreener.com"
DEFILLAMA_BASE   = "https://api.llama.fi"
GOPLUS_BASE      = "https://api.gopluslabs.io/api/v1"
COINGECKO_BASE   = "https://api.coingecko.com/api/v3"
CMC_BASE         = "https://pro-api.coinmarketcap.com/v1"

DUNE_QUERIES = {
    "solana_smart_money":   "7417474",
    "bsc_smart_money":      "7417475",
    "base_smart_money":     "7417476",
    "ethereum_smart_money": "7417477",
}

# ── Filtri hard (gate di qualità) ─────────────────────────────────────────
FILTER_CONFIG = {
    # Market cap — nessun limite superiore: preferiamo token più capitalizzati
    "MIN_MARKET_CAP_USD":    100_000,
    # Liquidità — nessun limite superiore: liquidità alta = token più solido
    "MIN_LIQUIDITY_USD":      30_000,
    # Volume
    "MIN_VOLUME_1H_USD":      10_000,   # allineato con trade_simulator (era 5k)
    # Social score — non usato nel filtro (libreria non funzionante)
    # Età pair
    # Analisi su 141 token reali (mag 2026):
    #   <6h  → mediana -97.7%, 55% bad — catastrofico (rug/dump immediato)
    #   6-24h → mediana +33.8%, 68% >30%, 12% bad — SWEET SPOT
    #   1-7gg → mediana 11-14%, 4-16% bad — ok
    "MIN_PAIR_AGE_HOURS":        6.0,     # era 0.5 — dati reali: <6h ha 55% bad rate
    "MAX_PAIR_AGE_HOURS":        168,     # 7 giorni
    # Change 1h
    # >15% in 1h → 27.8% bad rate (momentum già consumato)
    # -10 a 0% → solo 2.1% bad, 40% >30% — quiet accumulation prima del pump
    "MAX_CHANGE_1H_PCT":         15.0,   # era 25.0 — oltre il 15% il pump è già partito
    "MIN_CHANGE_1H_PCT":        -10.0,   # era -30 → -10: token a -30% in 1h è quasi certamente morto
    # Sicurezza EVM
    "MAX_BUY_TAX_PCT":           10,
    "MAX_SELL_TAX_PCT":          10,
    # Smart money
    "MIN_INFLOW_USD":          5_000,
    "MIN_SMART_WALLETS":           5,
    "MIN_INFLOW_TO_MCAP_TIER1": 0.005,
    # Holder risk
    "MAX_TOP10_HOLDER_PCT":       60,     # > 60% top10 → rug risk
    # Wash trading
    "MAX_VOL5M_TO_VOL1H":        0.80,   # vol5m > 80% vol1h → anomalia
    "MIN_TXNS_PER_VOLUME":    0.0001,    # soglia minima txns/vol (anti wash)
    # Cooldown / blacklist
    "TOKEN_COOLDOWN_MIN":        120,
    "BLACKLIST_DROP_PCT":        -70.0,
    "BLACKLIST_DURATION_H":        8.0,
    # ── Pump.fun / PumpSwap ───────────────────────────────────────────────
    # Token su pumpswap vengono bloccati se troppo giovani E troppo piccoli:
    # i "rug" tipici pompano in <6h con mcap <$300K poi vanno -99%.
    "PUMPSWAP_MIN_AGE_HOURS":      6.0,   # età minima su pumpswap
    "PUMPSWAP_MIN_MCAP":         300_000, # mcap minimo su pumpswap
    "PUMPSWAP_MAX_CHANGE_1H":      40.0,  # change_1h max su pumpswap (già pompati → out)
    # ── Micro-cap Solana (qualsiasi DEX) ─────────────────────────────────
    "SOLANA_MICROCAP_MCAP":      500_000, # soglia micro-cap Solana
    "SOLANA_MICROCAP_MIN_AGE":     3.0,   # età minima per micro-cap su Solana
}

# ── Scoring — pesi configurabili ──────────────────────────────────────────
# I punteggi si sommano: max teorico ~100 punti
SCORE_CONFIG = {
    # Tier thresholds
    "DIAMOND_THRESHOLD": 75,
    "GOLD_THRESHOLD":    55,
    "SILVER_THRESHOLD":  35,
    "BRONZE_THRESHOLD":  18,

    # Smart money (max 35 pt)
    "SM_WALLETS_HIGH":   20,   # >= 15 wallet
    "SM_WALLETS_MED":    12,   # >= 8 wallet
    "SM_WALLETS_LOW":     6,   # >= 5 wallet
    "SM_RATIO_HIGH":     10,   # inflow/mcap >= 10%
    "SM_RATIO_MED":       6,   # >= 3%
    "SM_RATIO_LOW":       2,   # >= 1%
    "SM_PNL_HIGH":        5,   # avg wallet pnl >= 100%
    "SM_PNL_MED":         3,   # >= 50%
    # Sweet spot 20-50%: mediana +95.6%, 88% >30%, 0% bad su 9 token reali
    "SM_PNL_SWEET":       7,   # 20-50% — zona d'oro (dati reali mag 2026)

    # Momentum mercato (max 35 pt)
    "MOM_BSR_HIGH":      14,   # BSR >= 3.0
    "MOM_BSR_MED":        9,   # >= 2.0
    "MOM_BSR_LOW":        5,   # >= 1.5
    "MOM_CH1H_IDEAL":    10,   # 5% <= ch1h <= 20%
    "MOM_CH1H_NEUTRAL":   5,   # 0% <= ch1h < 5%
    "MOM_CH1H_DIP":       3,   # -15% <= ch1h < 0%
    "MOM_RAMP_HIGH":      8,   # volume ramp >= 3x
    "MOM_RAMP_MED":       5,   # >= 2x
    "MOM_VOLL":           3,   # vol/liq >= 50%

    # Età / timing (max 15 pt microcap + 8 pt midcap)
    "AGE_VERY_YOUNG":    15,   # <= 6h
    "AGE_YOUNG":         10,   # <= 24h
    "AGE_MEDIUM":         5,   # <= 72h
    # Bonus mid-cap consolidati (mcap > 5M, età 72-240h): token che crescono dopo la fase iniziale
    "AGE_MIDCAP_CONSOL":  8,   # mcap > 5M e 72h < age <= 240h

    # CEX bonus (max 15 pt)
    "CEX_TIER1":         15,   # Binance / Coinbase / OKX
    "CEX_OTHER":          7,   # Kucoin / Gate / Bybit

    # Momentum score da compute_early_metrics (max 5 pt)
    "MS_HIGH":            5,   # >= 8/10
    "MS_MED":             3,   # >= 5/10

    # Penalità
    "PENALTY_FAKE_PUMP":  -5,  # classify_gem == FAKE_PUMP
    "PENALTY_CONCENTRATED": -10,  # top10_holder > 40%
    "PENALTY_WASH":       -8,  # wash trading rilevato

    # ── Pre-pump patterns (max ~45 pt aggiuntivi) ─────────────────────────
    # Tecnici (da klines Binance/DEX)
    "PP_VOL_EXPLOSION":    10,  # volume z-score >= 3 (esplosione volumi)
    "PP_BB_SQUEEZE":        5,  # Bollinger Band width < soglia (ridotto: rumoroso su microcap)
    "PP_EMA_BREAKOUT":      5,  # prezzo sopra EMA20/50 con crossover (ridotto)
    "PP_RSI_DIVERGENCE":    4,  # RSI sale mentre prezzo piatto/scende (ridotto)
    # On-chain / derivati
    "PP_OI_SPIKE":          6,  # Open Interest +20% in 1h
    "PP_FUNDING_SQUEEZE":   5,  # funding rate negativo → short squeeze
    "PP_WHALE_ACCUM":       8,  # accumulazione balene (exchange outflow proxy)
    # Bonus capitalizzazione (più è alta, maggiore il segnale)
    "PP_LARGE_CAP_BONUS":   3,  # mcap > 50M → prepump su large cap più affidabile
    # Orderbook / derivati avanzati
    "PP_OB_IMBALANCE":      6,  # bid/ask skew > 65% → pressione direzionale
    "PP_DEPOSIT_SPIKE":     6,  # spike volume CEX vs DEX → accumulo su exchange
    "PP_OI_FUNDING_DIV":    8,  # OI sale + funding divergente → leva pre-pump

    # ── Feature di accumulo (da sell absorption / stealth / distribuzione) ──
    "SELL_ABSORB_HIGH":    12,  # sell_absorption_score >= 3.0 (forte accumulo)
    "SELL_ABSORB_MED":      7,  # >= 1.5
    "STEALTH_ACCUM":       15,  # smart money alta + social bassa + prezzo piatto
    "BUY_SIZE_WHALE":       8,  # avg_buy_size > 2000 USD (whale, non retail)
    "LIQ_STABLE":           5,  # liquidità stabile (cv < 0.10)
    "VOL_PERSISTENT":       6,  # volume persistente (non spike singolo)
    "TXNS_ACCEL":           5,  # accelerazione transazioni > 50%
    "WB_REPEAT_HIGH":      10,  # wallet_repeat_buy_score >= 4.0 (accumulatori persistenti)
    "WB_REPEAT_MED":        6,  # wallet_repeat_buy_score >= 2.0
    "PENALTY_RSI_SMALL":   -3,  # RSI/BB su microcap (<5M) → rumore

    # Soglia minima per inviare mail/report (BRONZE e superiori)
    "MIN_SCORE_TO_REPORT": 18,
}

# ── Loop ──────────────────────────────────────────────────────────────────
BOT_CONFIG = {
    "LOOP_INTERVAL_SEC":   180,    # 120s troppo aggressivo finché quota CG non si azzera
    "USE_MOCK_FALLBACK":   False,  # MAI True in produzione
    "REQUEST_TIMEOUT":      15,
    "REQUEST_RETRIES":       3,
    "REQUEST_BACKOFF":       2,
    "ENABLE_DEFILLAMA":    True,
    "ENABLE_CEX_CHECK":    True,
    "ENABLE_HOLDER_CHECK": True,
    "ENABLE_SOCIAL":       False,  # Nitter non funziona — evita timeout inutili
}

EMAIL_CONFIG = {
    "ENABLED":       True,
    "SMTP_HOST":     os.environ.get("SMTP_HOST",     "smtp.gmail.com"),
    "SMTP_PORT":     int(os.environ.get("SMTP_PORT", "587")),
    "SMTP_USER":     os.environ.get("SMTP_USER",     "giagnacovo.luca@gmail.com"),
    "SMTP_PASSWORD": os.environ.get("SMTP_PASSWORD", ""),
    "FROM_ADDR":     os.environ.get("SMTP_FROM",     "giagnacovo.luca@gmail.com"),
    "TO_ADDR":       os.environ.get("SMTP_TO",       "giagnacovo.luca@gmail.com"),
    "MIN_SCORE":     float(os.environ.get("EMAIL_MIN_SCORE", "35")),  # invia da SILVER in su
}

# ==============================================================================
# SEZIONE 3 – STATO PERSISTENTE
# ==============================================================================

_WALLET_HISTORY_PATH = os.path.join(_BASE_DIR, "reports", "wallet_history.json")
_VOLUME_HISTORY_PATH = os.path.join(_BASE_DIR, "reports", "volume_history.json")

# In-memory, caricati da disco all'avvio
WALLET_HISTORY: dict = defaultdict(list)   # addr → [{timestamp, wallet_count}]
VOLUME_HISTORY: dict = defaultdict(list)   # pair_addr → [{ts, vol1h, liq}]

_state_lock = threading.Lock()


def load_persistent_state() -> None:
    """Carica WALLET_HISTORY e VOLUME_HISTORY da disco."""
    global WALLET_HISTORY, VOLUME_HISTORY
    cutoff = (datetime.now() - timedelta(hours=48)).isoformat()

    for path, store_ref, key_ts in [
        (_WALLET_HISTORY_PATH, WALLET_HISTORY, "timestamp"),
        (_VOLUME_HISTORY_PATH, VOLUME_HISTORY, "ts"),
    ]:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            for addr, entries in raw.items():
                filtered = [e for e in entries if e.get(key_ts, "") >= cutoff]
                # Converti timestamp da isoformat a datetime per WALLET_HISTORY
                if key_ts == "timestamp" and filtered:
                    for e in filtered:
                        if isinstance(e["timestamp"], str):
                            e["timestamp"] = datetime.fromisoformat(e["timestamp"])
                if filtered:
                    store_ref[addr] = filtered
        except Exception as e:
            log.warning(f"[state] Errore caricamento {path}: {e}")

    log.info(f"[state] WALLET_HISTORY: {len(WALLET_HISTORY)} token "
             f"| VOLUME_HISTORY: {len(VOLUME_HISTORY)} pair")


def save_persistent_state() -> None:
    """Salva WALLET_HISTORY e VOLUME_HISTORY su disco."""
    os.makedirs(os.path.join(_BASE_DIR, "reports"), exist_ok=True)
    with _state_lock:
        # WALLET_HISTORY — converte datetime in isoformat
        try:
            wh_serial = {
                addr: [{"timestamp": (e["timestamp"].isoformat()
                                      if isinstance(e["timestamp"], datetime)
                                      else e["timestamp"]),
                        "wallet_count": e["wallet_count"]}
                       for e in entries]
                for addr, entries in WALLET_HISTORY.items() if entries
            }
            with open(_WALLET_HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(wh_serial, f)
        except Exception as e:
            log.warning(f"[state] Errore salvataggio wallet_history: {e}")

        # VOLUME_HISTORY — già serializzabile
        try:
            with open(_VOLUME_HISTORY_PATH, "w", encoding="utf-8") as f:
                json.dump(dict(VOLUME_HISTORY), f)
        except Exception as e:
            log.warning(f"[state] Errore salvataggio volume_history: {e}")


# ── Cooldown e blacklist ──────────────────────────────────────────────────
_gem_last_signal: dict = {}
_gem_blacklist:   dict = {}
_gem_lock = threading.Lock()


def _gem_cooldown_ok(pair_addr: str) -> bool:
    mins = FILTER_CONFIG.get("TOKEN_COOLDOWN_MIN", 120)
    with _gem_lock:
        last = _gem_last_signal.get(pair_addr)
        if last is None:
            return True
        return (datetime.now() - last).total_seconds() / 60 >= mins


def _set_gem_cooldown(pair_addr: str) -> None:
    with _gem_lock:
        _gem_last_signal[pair_addr] = datetime.now()


def _gem_blacklisted(pair_addr: str) -> bool:
    with _gem_lock:
        exp = _gem_blacklist.get(pair_addr)
        if exp is None:
            return False
        if datetime.now() < exp:
            return True
        del _gem_blacklist[pair_addr]
        return False


def _blacklist_gem(pair_addr: str, symbol: str = "") -> None:
    hours = FILTER_CONFIG.get("BLACKLIST_DURATION_H", 8.0)
    with _gem_lock:
        _gem_blacklist[pair_addr] = datetime.now() + timedelta(hours=hours)
    log.debug(f"[blacklist] 🚫 {symbol or pair_addr[:8]} blacklistata per {hours}h")


def check_followup_blacklist() -> None:
    """Legge gems_followup.csv e blacklista gemme con dump > soglia."""
    drop_thresh = FILTER_CONFIG.get("BLACKLIST_DROP_PCT", -70.0)
    fpath = os.path.join(_BASE_DIR, "reports", "gems_followup.csv")
    if not os.path.exists(fpath):
        return
    try:
        min_chg: dict = {}
        sym_map: dict = {}
        with open(fpath, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                pair = row.get("pair_address", "")
                chg  = row.get("change_pct", "")
                if not pair or not chg:
                    continue
                try:
                    v = float(chg)
                    min_chg[pair] = min(min_chg.get(pair, 0.0), v)
                    sym_map[pair] = row.get("token_symbol", "")
                except Exception:
                    pass
        for pair, mc in min_chg.items():
            if mc <= drop_thresh and not _gem_blacklisted(pair):
                _blacklist_gem(pair, sym_map.get(pair, ""))
    except Exception as e:
        log.warning(f"[blacklist] Errore followup: {e}")

# ==============================================================================
# SEZIONE 4 – UTILITIES
# ==============================================================================


def _safe_get(
    url: str,
    params: dict = None,
    headers: dict = None,
    timeout: int = None,
    retries: int = None,
    label: str = "",
) -> Optional[requests.Response]:
    """GET con retry esponenziale e gestione errori silenziosa."""
    timeout = timeout or BOT_CONFIG["REQUEST_TIMEOUT"]
    retries = retries or BOT_CONFIG["REQUEST_RETRIES"]
    backoff = BOT_CONFIG["REQUEST_BACKOFF"]

    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 429:
                wait = backoff ** (attempt + 2)
                log.warning(f"[{label}] Rate limit (429). Attendo {wait}s...")
                time.sleep(wait)
            elif resp.status_code >= 500:
                time.sleep(backoff ** attempt)
            else:
                log.debug(f"[{label}] HTTP {resp.status_code} per {url}")
                return None
        except requests.exceptions.Timeout:
            time.sleep(backoff ** attempt)
        except requests.exceptions.ConnectionError:
            time.sleep(backoff ** attempt)
        except Exception as e:
            log.warning(f"[{label}] Errore imprevisto: {e}")
            return None
    return None


# ── EMA (Exponential Moving Average) ─────────────────────────────────────
def ema(values: list, alpha: float = 0.3) -> float:
    """EMA su una lista di valori (più recente = ultimo elemento)."""
    if not values:
        return 0.0
    result = float(values[0])
    for v in values[1:]:
        result = alpha * float(v) + (1 - alpha) * result
    return result


def roc(values: list, period: int = 1) -> float:
    """Rate of Change: (ultimo - N periodi fa) / N periodi fa * 100."""
    if len(values) < period + 1 or values[-period - 1] == 0:
        return 0.0
    return (values[-1] - values[-period - 1]) / abs(values[-period - 1]) * 100


def z_score(value: float, history: list) -> float:
    """Z-score del valore rispetto alla storia. Clampato a [-5, 5]."""
    if len(history) < 3:
        return 0.0
    arr = np.array(history, dtype=float)
    mu, sigma = arr.mean(), arr.std()
    if sigma < 1e-9:
        return 0.0
    return float(np.clip((value - mu) / sigma, -5, 5))

# ==============================================================================
# SEZIONE 5 – EARLY METRICS & FEATURE ENGINEERING
# ==============================================================================


def compute_early_metrics(token_address: str, wallet_count: int, pair_data: dict) -> dict:
    """
    Calcola metriche di momentum precoce basate sulla storia dei wallet.
    Aggiorna WALLET_HISTORY e VOLUME_HISTORY.
    Ritorna dict con: wg, wa, pair_age_h, momentum_score, vol_ramp_ratio.
    """
    now = datetime.now()

    # ── Wallet history ─────────────────────────────────────────────────────
    with _state_lock:
        wh = WALLET_HISTORY[token_address]
        wh.append({"timestamp": now, "wallet_count": wallet_count})
        if len(wh) > 20:
            wh.pop(0)
        history = list(wh)

    def _growth(h):
        if len(h) < 2:
            return 0.0
        d = h[-1]["wallet_count"] - h[-2]["wallet_count"]
        t = (h[-1]["timestamp"] - h[-2]["timestamp"]).total_seconds() / 60
        return d / t if t > 0 else 0.0

    def _accel(h):
        if len(h) < 3:
            return 0.0
        g1 = _growth(h[-3:-1])
        g2 = _growth(h[-2:])
        return g2 - g1

    wg = _growth(history)
    wa = _accel(history)

    # Wallet Z-score (segnale di accelerazione anomala)
    wallet_counts = [e["wallet_count"] for e in history]
    wz = z_score(wallet_count, wallet_counts[:-1]) if len(wallet_counts) > 2 else 0.0

    # ── Pair age ──────────────────────────────────────────────────────────
    pair_age_h = 999.0
    age_ms = pair_data.get("pairCreatedAt") if pair_data else None
    if age_ms:
        try:
            pair_age_h = (time.time() - age_ms / 1000) / 3600
        except Exception:
            pass

    # ── Volume ramp ratio ──────────────────────────────────────────────────
    vol1h = pair_data.get("volume", {}).get("h1", 0) if pair_data else 0
    vol5m = pair_data.get("volume", {}).get("m5", 0) if pair_data else 0
    liq   = (pair_data.get("liquidity", {}).get("usd", 0)
             if pair_data else 0)
    txns  = (((pair_data.get("txns") or {}).get("h1") or {})
             if pair_data else {})
    txns_total = int(txns.get("buys", 0) or 0) + int(txns.get("sells", 0) or 0)

    pair_addr = (pair_data.get("pairAddress", token_address)
                 if pair_data else token_address)

    with _state_lock:
        vh = VOLUME_HISTORY[pair_addr]
        vh.append({"ts": now.isoformat(), "vol1h": vol1h, "liq": liq,
                   "txns": txns_total})
        if len(vh) > 24:   # tieni 24 snapshot (2h con ciclo 5min)
            vh.pop(0)
        vol_history = [e["vol1h"] for e in vh]

    # Ramp = vol1h attuale / vol1h di ~1h fa
    vol_ramp = 1.0
    if len(vol_history) >= 12 and vol_history[-12] > 0:
        vol_ramp = round(vol1h / vol_history[-12], 2)

    # Volume Z-score
    vol_z = z_score(vol1h, vol_history[:-1]) if len(vol_history) > 3 else 0.0

    # Liquidità in crescita? (confronto con 30 min fa)
    liq_change_pct = 0.0
    liq_history = [e["liq"] for e in VOLUME_HISTORY.get(pair_addr, [])]
    if len(liq_history) >= 6 and liq_history[-6] > 0:
        liq_change_pct = (liq - liq_history[-6]) / liq_history[-6] * 100

    # ── Momentum score composito (0-10) ────────────────────────────────────
    score = 0
    if wa > 0.5 or wz > 2.0:  score += 3
    elif wa > 0.2 or wz > 1.0: score += 2
    if wg > 1.0:   score += 2
    elif wg > 0.3: score += 1
    if 0.5 <= pair_age_h <= 6:     score += 3
    elif 6 < pair_age_h <= 24:     score += 1
    if vol_ramp >= 3.0: score += 2
    elif vol_ramp >= 2.0: score += 1
    if vol1h > 0 and vol5m > vol1h / 12: score += 1   # spike 5m
    if liq_change_pct > 10: score += 1  # liquidità in crescita

    # ── Liquidity Stability Index ─────────────────────────────────────────
    liq_history = [e["liq"] for e in VOLUME_HISTORY.get(pair_addr, [])]
    # Default 1.0 = sconosciuto/instabile — evita falsi positivi al primo ciclo
    # (con <4 campioni CV=0.0 farebbe scattare il segnale su qualsiasi token nuovo)
    liq_stability_cv = 1.0
    if len(liq_history) >= 4:
        lh = np.array(liq_history, dtype=float)
        lh_mean = lh.mean()
        if lh_mean > 0:
            liq_stability_cv = float(lh.std() / lh_mean)

    # ── Volume Persistence Score ──────────────────────────────────────────
    # CV basso = volume sostenuto, alto = spike singolo
    # Default 1.0 = sconosciuto/instabile — come sopra
    vol_persistence_cv = 1.0
    if len(vol_history) >= 4:
        vh_arr = np.array(vol_history, dtype=float)
        vh_mean = vh_arr.mean()
        if vh_mean > 0:
            vol_persistence_cv = float(vh_arr.std() / vh_mean)

    # ── Transaction Acceleration ──────────────────────────────────────────
    txns_history = [e.get("txns", 0) for e in VOLUME_HISTORY.get(pair_addr, [])]
    txns_accel   = 0.0
    if len(txns_history) >= 4 and txns_history[-4] > 0:
        # Confronta media ultimi 2 snapshot vs media 4 snapshot prima
        recent = sum(txns_history[-2:]) / 2
        older  = sum(txns_history[-6:-2]) / max(len(txns_history[-6:-2]), 1)
        if older > 0:
            txns_accel = (recent - older) / older  # % di accelerazione

    return {
        "wallet_growth_rate":   round(wg, 4),
        "wallet_acceleration":  round(wa, 4),
        "wallet_z_score":       round(wz, 3),
        "volume_ramp_ratio":    vol_ramp,
        "volume_z_score":       round(vol_z, 3),
        "liq_change_30m_pct":   round(liq_change_pct, 2),
        "calculated_pair_age_h": round(pair_age_h, 2),
        "momentum_score":       score,
        "liq_stability_cv":     round(liq_stability_cv, 4),
        "vol_persistence_cv":   round(vol_persistence_cv, 4),
        "txns_acceleration":    round(txns_accel, 4),
    }


def engineer_features(profile: dict) -> dict:
    """
    Deriva feature composte da un profilo già costruito.
    Aggiunge campi aggiuntivi al profile in-place e lo ritorna.

    Feature derivate:
      vol5m_to_vol1h_ratio  — concentrazione volume ultimi 5min
      buy_pressure          — % acquisti sul totale transazioni
      inflow_per_wallet     — inflow medio per smart wallet
      smart_money_quality   — pnl × wallets / 50 (qualità + quantità)
      price_momentum_5m     — velocità relativa 5m vs 1h
      age_vol_ratio         — volume per ora di vita del token
      liq_to_mcap_safe      — proxy di rug risk (liq/mcap > 0.8 → sospetto)
      vol_to_liq            — pressure di liquidità
      wash_trading_flag     — True se il pattern vol/txns è anomalo
    """
    vol1h  = max(profile.get("volume_1h_usd", 0), 0)
    vol5m  = max(profile.get("volume_5m_usd", 0), 0)
    buys   = max(profile.get("txns_1h_buys", 0), 0)
    sells  = max(profile.get("txns_1h_sells", 0), 0)
    wallets = max(profile.get("inflow_wallet_count", 0), 0)
    inflow = max(profile.get("inflow_usd", 0), 0)
    mcap   = max(profile.get("market_cap_usd", 1), 1)
    liq    = max(profile.get("liquidity_usd", 1), 1)
    age    = max(profile.get("pair_age_hours", 0.1), 0.1)
    pnl    = profile.get("avg_wallet_pnl_pct", 0)
    ch1h   = profile.get("change_1h_pct", 0)
    ch5m   = profile.get("change_5m_pct", 0)
    total_txns = buys + sells

    # Concentrazione volume 5min
    profile["vol5m_to_vol1h_ratio"] = round(vol5m / vol1h, 4) if vol1h > 0 else 0.0

    # Pressione acquisto
    profile["buy_pressure"] = round(buys / total_txns, 4) if total_txns > 0 else 0.5

    # Inflow per wallet
    profile["inflow_per_wallet"] = round(inflow / wallets, 2) if wallets > 0 else 0.0

    # Qualità smart money
    profile["smart_money_quality"] = round(
        max(pnl, 0) * min(wallets, 50) / 50, 2
    )

    # Velocità prezzo 5m normalizzata (>1 = accelera, <1 = frena)
    expected_5m_ch = ch1h / 12 if ch1h != 0 else 0.0001
    profile["price_momentum_5m"] = round(
        float(np.clip(ch5m / expected_5m_ch if expected_5m_ch != 0 else 1.0, -5, 5)), 3
    )

    # Volume per ora di vita
    profile["age_vol_ratio"] = round(vol1h / (age * 1000), 3)

    # Ratio liq/mcap (rug indicator: vicino a 1 = rug setup)
    profile["liq_to_mcap_ratio"] = round(liq / mcap, 4)

    # Vol/liq
    profile["vol_to_liq"] = round(vol1h / liq, 4)

    # ── Sell Absorption Score ──────────────────────────────────────────────
    # Prezzo piatto nonostante sell alto = qualcuno sta assorbendo tutto
    # Formula: (sell_ratio) / max(abs(price_change), 0.1)
    # Alto = forte accumulo invisibile
    sell_ratio = sells / total_txns if total_txns > 0 else 0.5
    abs_change = max(abs(ch1h), 0.1)   # evita divisione per zero
    # Normalizzato: sell_ratio > 0.5 e prezzo stabile = accumulo
    raw_absorption = (sell_ratio * 10) / abs_change
    profile["sell_absorption_score"] = round(min(raw_absorption, 10.0), 3)

    # ── Buy Size Distribution ──────────────────────────────────────────────
    # Avg buy size alto = whale. Basso = retail.
    profile["avg_buy_size_usd"] = round(vol1h / max(buys, 1), 2)

    # Wallet Repeat Buy Score — proxy del clustering da Dune v3
    # repeat_buyer_ratio va 0..1; score normalizzato 0..10
    # High score = stessi wallet tornano più volte = accumulo deliberato
    repeat_ratio = profile.get("repeat_buyer_ratio", 0.0)
    repeat_cnt   = profile.get("repeat_buyer_count", 0)
    unique_cnt   = max(profile.get("inflow_wallet_count", 1), 1)
    abs_bonus = min(repeat_cnt / max(unique_cnt, 1), 1.0)
    profile["wallet_repeat_buy_score"] = round(
        (repeat_ratio * 7.0) + (abs_bonus * 3.0), 2
    )

    # Inflow Recency Score — da Dune v4: % inflow nelle ultime 2h sulla finestra totale
    # 0 = inflow vecchio (già distribuito), 1 = tutto l'inflow è freschissimo
    # Score 0..10: segnale in momentum attivo vs. token già distribuito
    recency = profile.get("inflow_recency_ratio", 0.0)
    profile["inflow_recency_score"] = round(min(recency * 10.0, 10.0), 2)

    # Wash trading: volume altissimo, transazioni bassissime
    # Legit: almeno 1 txn ogni $5k di volume
    min_txns_expected = vol1h * FILTER_CONFIG.get("MIN_TXNS_PER_VOLUME", 0.0001)
    wash_suspicious   = (total_txns > 0 and vol1h > 50_000 and
                         total_txns < min_txns_expected)
    # Oppure: vol5m > 80% vol1h (dump artificiale concentrato)
    vol5m_spike = (vol1h > 0 and vol5m / vol1h > FILTER_CONFIG["MAX_VOL5M_TO_VOL1H"])
    profile["wash_trading_flag"] = wash_suspicious or vol5m_spike

    return profile


def classify_gem(profile: dict) -> str:
    """
    Classificazione basata su segnali di mercato affidabili.
    NON usa wallet_acceleration in-memory (buggy).
    """
    bsr     = profile.get("buy_sell_ratio_1h", 1.0)
    ch1h    = profile.get("change_1h_pct", 0)
    wallets = profile.get("inflow_wallet_count", 0)
    inflow  = profile.get("inflow_usd", 0)
    age     = profile.get("pair_age_hours", 999)
    ms      = profile.get("momentum_score", 0)
    buys    = profile.get("txns_1h_buys", 0)
    sells   = profile.get("txns_1h_sells", 0)
    has_txns = (buys + sells) > 0

    # FAKE_PUMP: prezzo sale ma venditore dominante (dati txns reali)
    if has_txns and bsr < 0.5 and ch1h > 10:
        return "FAKE_PUMP"

    # HOT_GEM: combinazione forte
    if (wallets >= 10 and bsr >= 2.0 and age <= 24 and ch1h > 0 and ms >= 5):
        return "HOT_GEM"

    # STEADY_GROWTH: smart money presente, momentum moderato
    if wallets >= 5 and inflow >= 5_000 and bsr >= 1.2:
        return "STEADY_GROWTH"

    # EARLY_SIGNAL: token giovanissimo con volume e smart money
    if age <= 6 and inflow >= 3_000 and ms >= 3:
        return "EARLY_SIGNAL"

    # STEALTH_ACCUM: accumulo silenzioso — smart money alta, social bassa,
    # prezzo quasi piatto ma sell absorbiti
    social  = profile.get("social_score", 50)
    absorb  = profile.get("sell_absorption_score", 0)
    if (wallets >= 6 and inflow >= 5_000
            and abs(ch1h) <= 5.0          # prezzo quasi piatto
            and (social < 25 or ms < 4)   # radar basso
            and absorb >= 1.5):            # sell assorbiti
        return "STEALTH_ACCUM"

    return "NEUTRAL"

# ==============================================================================
# SEZIONE 6 – DATA LAYER: DUNE
# ==============================================================================


class DuneDataFetcher:

    _EXCLUDE_SYMBOLS = {
        'USDT','USDC','BUSD','DAI','FRAX','TUSD','USDP','GUSD','LUSD','SUSD',
        'USDE','GHO','USDS','PYUSD','USDG','EURC','RLUSD','USD0','CRVUSD',
        'WBTC','CBBTC','TBTC','LBTC','EBTC','BTCB',
        'WETH','CBETH','STETH','WSTETH','RETH','FRXETH','SFRXETH','METH',
        'WEETH','RSETH','OSETH','EZETH',
        'XAUT','PAXG',
        'AAVE','CRV','LDO','MKR','UNI','LINK','RNDR','ENS',
        'PEPE','DOGE','SHIB','BONK','WIF','POPCAT',
    }

    def __init__(self):
        self._headers = {
            "X-DUNE-API-KEY": DUNE_API_KEY,
            "Content-Type":   "application/json",
        }
        self._cache: dict = {}
        self._cache_ttl = timedelta(minutes=10)

    def get_smart_money_tokens(self, chain: str) -> list[dict]:
        if chain in self._cache:
            ts, rows = self._cache[chain]
            if datetime.now() - ts < self._cache_ttl:
                return rows

        query_key = f"{chain}_smart_money"
        query_id  = DUNE_QUERIES.get(query_key)

        if not query_id or "PLACEHOLDER" in str(query_id):
            log.warning(f"[Dune] Query ID non configurato per {chain}.")
            return self._mock_tokens(chain) if BOT_CONFIG["USE_MOCK_FALLBACK"] else []

        rows = self._get_latest_results(query_id, chain)
        if rows is None:
            rows = self._execute_and_wait(query_id, chain)
        if rows is None:
            log.warning(f"[Dune] Nessun risultato per {chain}.")
            if BOT_CONFIG["USE_MOCK_FALLBACK"]:
                return self._mock_tokens(chain)
            return []

        self._cache[chain] = (datetime.now(), rows)
        log.info(f"[Dune] {len(rows)} token smart-money su {chain}.")
        return rows

    def _get_latest_results(self, query_id: str, chain: str) -> Optional[list]:
        url  = f"{DUNE_BASE}/query/{query_id}/results"
        resp = _safe_get(url, headers=self._headers, label="Dune/results")
        if resp is None:
            return None
        try:
            rows = resp.json().get("result", {}).get("rows", [])
            return self._normalize_rows(rows, chain) if rows else None
        except Exception as e:
            log.warning(f"[Dune] Parse error: {e}")
            return None

    def _execute_and_wait(self, query_id: str, chain: str, max_wait: int = 60) -> Optional[list]:
        url = f"{DUNE_BASE}/query/{query_id}/execute"
        try:
            resp = requests.post(url, headers=self._headers,
                                 json={"performance": "large"},
                                 timeout=BOT_CONFIG["REQUEST_TIMEOUT"])
            if resp.status_code != 200:
                return None
            exec_id = resp.json().get("execution_id")
            if not exec_id:
                return None
        except Exception:
            return None

        deadline = time.time() + max_wait
        while time.time() < deadline:
            time.sleep(3)
            sr = _safe_get(f"{DUNE_BASE}/execution/{exec_id}/status",
                           headers=self._headers, label="Dune/status")
            if sr is None:
                continue
            state = sr.json().get("state", "")
            if state == "QUERY_STATE_COMPLETED":
                rr = _safe_get(f"{DUNE_BASE}/execution/{exec_id}/results",
                               headers=self._headers, label="Dune/results")
                if rr is None:
                    return None
                rows = rr.json().get("result", {}).get("rows", [])
                return self._normalize_rows(rows, chain)
            if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
                return None
        return None

    def _normalize_rows(self, rows: list, chain: str) -> list[dict]:
        result = []
        for r in rows:
            addr = (r.get("token_address") or r.get("token_mint_address") or "").strip()
            if not addr:
                continue
            sym = (r.get("token_symbol") or r.get("symbol") or "").strip().upper()
            if sym in self._EXCLUDE_SYMBOLS:
                continue
            result.append({
                "token_address":       addr,
                "token_symbol":        r.get("token_symbol") or r.get("symbol") or "",
                "token_name":          r.get("token_name") or r.get("name") or "",
                "chain":               chain,
                "inflow_usd":          float(r.get("inflow_usd") or 0),
                "inflow_wallet_count": int(r.get("unique_buyers") or
                                          r.get("wallet_count") or
                                          r.get("inflow_wallet_count") or 0),
                "avg_wallet_pnl_pct":  float(r.get("avg_wallet_pnl_pct") or 0),
                # v3: wallet cluster fields
                "avg_buy_per_wallet":  float(r.get("avg_buy_per_wallet") or 0),
                "max_single_trade":    float(r.get("max_single_trade") or 0),
                "repeat_buyer_count":  int(r.get("repeat_buyers") or 0),
                "repeat_buyer_ratio":  float(r.get("repeat_buyer_ratio") or 0),
                # v4: momentum freshness (% inflow nelle ultime 2h su finestra totale)
                "inflow_last_2h":         float(r.get("inflow_last_2h") or 0),
                "inflow_recency_ratio":   float(r.get("inflow_recency_ratio") or 0),
                "buyers_last_2h":         int(r.get("buyers_last_2h") or 0),
                "source":              "dune",
            })
        return result

    def _mock_tokens(self, chain: str) -> list[dict]:
        """SOLO per sviluppo/test. Non usare in produzione."""
        import random
        rng = random.Random(42 + hash(chain) % 100)
        symbols = {
            "solana": ["DAWG2", "MOCHI", "GROK2"],
            "bsc":    ["FLOKI3", "SHIBX", "TURBO"],
            "ethereum": ["BASED2", "DEGEN2", "MOON3"],
        }.get(chain, ["TOKEN1", "TOKEN2"])
        return [{
            "token_address":       f"mock_{s.lower()}_{chain}",
            "token_symbol":        s,
            "token_name":          f"{s} Mock Token",
            "chain":               chain,
            "inflow_usd":          rng.uniform(10_000, 100_000),
            "inflow_wallet_count": rng.randint(5, 25),
            "avg_wallet_pnl_pct":  rng.uniform(20, 150),
            "source":              "mock_dune",
        } for s in symbols]

# ==============================================================================
# SEZIONE 7 – DATA LAYER: DEXSCREENER
# ==============================================================================

_dex_lock       = threading.Lock()
_dex_last_call  = 0.0
_DEX_INTERVAL   = 0.8   # max 75 req/min


def _dex_rate_limit():
    global _dex_last_call
    with _dex_lock:
        wait = _DEX_INTERVAL - (time.time() - _dex_last_call)
        if wait > 0:
            time.sleep(wait)
        _dex_last_call = time.time()


def fetch_dexscreener_token(token_address: str, chain: str) -> Optional[dict]:
    _dex_rate_limit()
    dex_chain = CHAINS.get(chain, {}).get("dexscreener_id", chain)
    resp = _safe_get(f"{DEXSCREENER_BASE}/latest/dex/tokens/{token_address}",
                     label=f"DexScreener/{chain}")
    if resp is None:
        return None
    try:
        pairs = resp.json().get("pairs") or []
        chain_pairs = [p for p in pairs
                       if (p.get("chainId") or "").lower() == dex_chain.lower()]
        if not chain_pairs:
            chain_pairs = pairs
        if not chain_pairs:
            return None
        return max(chain_pairs,
                   key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
    except Exception:
        return None


def fetch_dexscreener_boosted(chain: str, limit: int = 30) -> list[dict]:
    _dex_rate_limit()
    dex_chain = CHAINS.get(chain, {}).get("dexscreener_id", chain)
    resp = _safe_get(f"{DEXSCREENER_BASE}/token-profiles/latest/v1",
                     label="DexScreener/boosted")
    if resp is None:
        return []
    try:
        items = resp.json() if isinstance(resp.json(), list) else []
        result = []
        for item in items:
            if (item.get("chainId") or "").lower() != dex_chain.lower():
                continue
            addr = item.get("tokenAddress", "")
            if not addr:
                continue
            result.append({
                "token_address":       addr,
                "token_symbol":        item.get("symbol", ""),
                "token_name":          (item.get("description") or "")[:40],
                "chain":               chain,
                "inflow_usd":          0,
                "inflow_wallet_count": 0,
                "avg_wallet_pnl_pct":  0,
                "source":              "dexscreener_boosted",
            })
            if len(result) >= limit:
                break
        return result
    except Exception:
        return []


def fetch_dexscreener_trending(chain: str, limit: int = 20) -> list[dict]:
    _dex_rate_limit()
    dex_chain = CHAINS.get(chain, {}).get("dexscreener_id", chain)
    resp = _safe_get(f"{DEXSCREENER_BASE}/latest/dex/search",
                     params={"q": dex_chain}, label="DexScreener/trending")
    if resp is None:
        return []
    try:
        pairs = resp.json().get("pairs") or []
        chain_pairs = [p for p in pairs
                       if (p.get("chainId") or "").lower() == dex_chain.lower()]
        chain_pairs.sort(
            key=lambda p: float((p.get("volume") or {}).get("h1", 0) or 0),
            reverse=True
        )
        result = []
        for p in chain_pairs[:limit]:
            base = p.get("baseToken") or {}
            addr = base.get("address", "")
            if not addr:
                continue
            vol1h = float((p.get("volume") or {}).get("h1", 0) or 0)
            buys  = int(((p.get("txns") or {}).get("h1") or {}).get("buys", 0) or 0)
            result.append({
                "token_address":       addr,
                "token_symbol":        base.get("symbol", ""),
                "token_name":          base.get("name", ""),
                "chain":               chain,
                "inflow_usd":          vol1h * 0.3,
                "inflow_wallet_count": max(buys // 10, 1),
                "avg_wallet_pnl_pct":  0,
                "source":              "dexscreener_trending",
            })
        return result
    except Exception:
        return []


def parse_dexscreener_pair(pair: dict, chain: str) -> dict:
    base  = pair.get("baseToken") or {}
    liq   = pair.get("liquidity") or {}
    vol   = pair.get("volume") or {}
    prch  = pair.get("priceChange") or {}
    tx1h  = ((pair.get("txns") or {}).get("h1") or {})
    fdv   = float(pair.get("marketCap") or pair.get("fdv") or 0)
    age_ms = pair.get("pairCreatedAt")
    age_h  = 0.0
    if age_ms:
        try:
            age_h = (time.time() - age_ms / 1000) / 3600
        except Exception:
            pass
    buys  = int(tx1h.get("buys", 0) or 0)
    sells = int(tx1h.get("sells", 0) or 0)
    bsr   = buys / sells if sells > 0 else (2.0 if buys > 0 else 1.0)

    return {
        "token_address":       base.get("address", ""),
        "token_symbol":        base.get("symbol", ""),
        "token_name":          base.get("name", ""),
        "chain":               chain,
        "pair_address":        pair.get("pairAddress", ""),
        "dex_id":              pair.get("dexId", ""),
        "price_usd":           float(pair.get("priceUsd") or 0),
        "market_cap_usd":      fdv,
        "liquidity_usd":       float(liq.get("usd", 0) or 0),
        "volume_5m_usd":       float(vol.get("m5", 0) or 0),
        "volume_1h_usd":       float(vol.get("h1", 0) or 0),
        "volume_6h_usd":       float(vol.get("h6", 0) or 0),
        "volume_24h_usd":      float(vol.get("h24", 0) or 0),
        "change_5m_pct":       float(prch.get("m5", 0) or 0),
        "change_1h_pct":       float(prch.get("h1", 0) or 0),
        "change_6h_pct":       float(prch.get("h6", 0) or 0),
        "change_24h_pct":      float(prch.get("h24", 0) or 0),
        "txns_1h_buys":        buys,
        "txns_1h_sells":       sells,
        "buy_sell_ratio_1h":   round(bsr, 3),
        "pair_age_hours":      round(age_h, 2),
    }

# ==============================================================================
# SEZIONE 8 – DATA LAYER: SOCIAL (Nitter)
# ==============================================================================


class SocialAnalyzer:

    SPAM_KEYWORDS = [
        "100x", "1000x", "guaranteed", "moon guaranteed", "buy now",
        "last chance", "presale", "airdrop", "free token", "t.me/",
        "pump", "🚀🚀🚀", "💎💎", "lfg lfg", "don't miss", "whitelist",
        "join now", "early access", "x100", "next 100x",
    ]
    NITTER_INSTANCES = [
        "nitter.privacydev.net", "nitter.poast.org", "nitter.rawbit.ninja",
        "nitter.1d4.us", "nitter.kavin.rocks", "nitter.unixfox.eu",
        "nitter.42l.fr", "nitter.moomoo.me",
    ]

    def __init__(self):
        self._cache: dict = {}
        self._cache_ttl = timedelta(minutes=20)
        self._nitter_scraper = None
        self._nitter_instances = [{"host": h, "ok": True, "last_fail": None}
                                   for h in self.NITTER_INSTANCES]
        self._nitter_lock = threading.Lock()
        self._globally_down = False
        if NTSCRAPER_AVAILABLE:
            self._init_nitter()

    def _init_nitter(self):
        for inst in self._nitter_instances:
            try:
                r = requests.get(f"https://{inst['host']}", timeout=4,
                                 headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    self._nitter_scraper = Nitter(
                        instance=f"https://{inst['host']}", log_level=0)
                    inst["ok"] = True
                    log.info(f"[Social] Nitter su {inst['host']}")
                    return
            except Exception:
                inst["ok"] = False
        log.warning("[Social] Nessuna istanza Nitter disponibile.")

    def _rotate_nitter(self) -> bool:
        if not NTSCRAPER_AVAILABLE:
            return False
        with self._nitter_lock:
            for inst in self._nitter_instances:
                if not inst["ok"] and inst["last_fail"]:
                    if (datetime.now() - inst["last_fail"]).seconds > 600:
                        try:
                            r = requests.get(f"https://{inst['host']}", timeout=4)
                            inst["ok"] = r.status_code == 200
                        except Exception:
                            inst["ok"] = False
            for inst in self._nitter_instances:
                if not inst["ok"]:
                    continue
                try:
                    self._nitter_scraper = Nitter(
                        instance=f"https://{inst['host']}", log_level=0)
                    return True
                except Exception:
                    inst["ok"] = False
                    inst["last_fail"] = datetime.now()
        return False

    def get_social_score(self, ticker: str, token_name: str = "") -> dict:
        if not BOT_CONFIG.get("ENABLE_SOCIAL", True):
            return {"social_score": 25.0, "tweet_count": 0, "source": "disabled"}

        key = ticker.upper()
        if key in self._cache:
            ts, data = self._cache[key]
            if datetime.now() - ts < self._cache_ttl:
                return data

        result = None
        if NTSCRAPER_AVAILABLE and self._nitter_scraper and not self._globally_down:
            result = self._analyze_nitter(ticker, token_name)
            if result is None and not self._nitter_scraper:
                self._globally_down = True

        if result is None:
            result = {"social_score": 25.0, "tweet_count": 0, "source": "unavailable"}

        self._cache[key] = (datetime.now(), result)
        return result

    def _analyze_nitter(self, ticker: str, token_name: str, retries: int = 2) -> Optional[dict]:
        query = f"${ticker}" if not ticker.startswith("$") else ticker
        for attempt in range(retries):
            if self._nitter_scraper is None:
                break
            try:
                data   = self._nitter_scraper.get_tweets(query, mode="term", number=50)
                tweets = data.get("tweets", []) if isinstance(data, dict) else []
                if not tweets and token_name:
                    data2  = self._nitter_scraper.get_tweets(
                        token_name, mode="term", number=30)
                    tweets = data2.get("tweets", []) if isinstance(data2, dict) else []
                if not tweets:
                    return {"social_score": 10.0, "tweet_count": 0, "source": "nitter_empty"}
                scores = []
                for tw in tweets:
                    if not isinstance(tw, dict):
                        continue
                    text = (tw.get("text") or tw.get("content") or "").lower()
                    if any(kw.lower() in text for kw in self.SPAM_KEYWORDS):
                        continue
                    stats   = tw.get("stats", {}) or {}
                    likes   = int(stats.get("likes", 0) or 0)
                    rts     = int(stats.get("retweets", 0) or 0)
                    replies = int(stats.get("replies", 0) or 0)
                    scores.append(min(likes + rts * 2 + replies, 500))
                if not scores:
                    return {"social_score": 5.0, "tweet_count": len(tweets),
                            "source": "nitter_allspam"}
                avg   = sum(scores) / len(scores)
                raw   = min(avg / 2.0, 100.0)
                bonus = min(len(scores) / 5.0, 10.0)
                return {"social_score": round(min(raw + bonus, 100.0), 1),
                        "tweet_count": len(scores), "source": "nitter"}
            except Exception as e:
                log.warning(f"[Social] Nitter attempt {attempt+1}: {e}")
                if not self._rotate_nitter():
                    return None
                time.sleep(1)
        return None

# ==============================================================================
# SEZIONE 9 – DATA LAYER: GOPLUS SECURITY
# ==============================================================================


def fetch_goplus_security(token_address: str, chain: str) -> dict:
    return {}  # GoPlus disabilitato
    chain_meta = CHAINS.get(chain, {})
    if not chain_meta.get("is_evm"):
        return {"is_evm": False}
    chain_id = chain_meta.get("goplus_chain_id", "56")
    url      = f"{GOPLUS_BASE}/token_security/{chain_id}"
    params   = {"contract_addresses": token_address}
    headers  = {"Authorization": GOPLUS_API_KEY} if GOPLUS_API_KEY else {}
    resp = _safe_get(url, params=params, headers=headers, label="GoPlus")
    if resp is None:
        return {}
    try:
        result = resp.json().get("result", {})
        data   = result.get(token_address.lower()) or result.get(token_address) or {}
        return {
            "is_honeypot":  int(data.get("is_honeypot", 0) or 0),
            "buy_tax":      float(data.get("buy_tax", 0) or 0) * 100,
            "sell_tax":     float(data.get("sell_tax", 0) or 0) * 100,
            "is_mintable":  int(data.get("is_mintable", 0) or 0),
            "lp_locked":    int(float(data.get("lp_locked_percent", 0) or 0) > 50),
            "owner_pct":    float(data.get("creator_percent", 0) or 0) * 100,
        }
    except Exception:
        return {}

# ==============================================================================
# SEZIONE 10 – DATA LAYER: CEX CHECK (CoinGecko)
# ==============================================================================

_TIER1_EXCHANGES = {"binance", "coinbase", "okex", "bybit", "kraken",
                    "kucoin", "gate", "huobi", "bitget"}

# Cache CoinGecko — TTL 24h, quota Demo 10k/mese
_cg_cache: dict = {}
_CG_CACHE_TTL = timedelta(days=7)   # era 24h — limite CoinGecko Demo: 10K/mese, con lazy=2% budget
_cg_lock = threading.Lock()

# Cache CMC — TTL 24h, quota free 333 req/day → usata solo come fallback
_cmc_cache: dict = {}
_CMC_CACHE_TTL = timedelta(days=7)   # allineato con CG: listing CEX stabile nel tempo
_cmc_lock = threading.Lock()
_cmc_last_call = 0.0
_CMC_MIN_INTERVAL = 5.0  # max ~17k req/day teorici, ma il free è 333 → conservativo


def _cmc_rate_limit():
    global _cmc_last_call
    wait = _CMC_MIN_INTERVAL - (time.time() - _cmc_last_call)
    if wait > 0:
        time.sleep(wait)
    _cmc_last_call = time.time()


# Tag CMC (piano free) → exchange identificato
_CMC_LISTING_TAGS = {
    "binance-listing":  "binance",
    "okx-listing":      "okex",
    "kucoin-listing":   "kucoin",
    "gate.io-listing":  "gate",
    "huobi-listing":    "huobi",
    "bybit-listing":    "bybit",
    "coinbase-listing": "coinbase",
    "kraken-listing":   "kraken",
    "bitget-listing":   "bitget",
}


def _get_cex_score_cmc(symbol: str) -> dict:
    """
    Fallback CMC per CEX listing check.
    Piano free: usa tags (es. 'binance-listing') — urls.exchange non disponibile.
    Chiamato SOLO quando CoinGecko fallisce, per preservare la quota CMC (333 req/day).
    """
    key = symbol.upper()
    with _cmc_lock:
        if key in _cmc_cache:
            ts, data = _cmc_cache[key]
            if datetime.now() - ts < _CMC_CACHE_TTL:
                return data

    default = {"cex_score": 0, "exchanges": [], "is_tier1": False, "tier1_exch": [], "source": "cmc"}

    _cmc_rate_limit()
    headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY, "Accept": "application/json"}
    try:
        r = requests.get(
            f"{CMC_BASE}/cryptocurrency/info",
            params={"symbol": symbol, "aux": "tags"},
            headers=headers, timeout=8,
        )
        if r.status_code != 200:
            log.debug(f"[CEX/CMC] HTTP {r.status_code} per {symbol}")
            return default

        data_map = r.json().get("data", {})
        val = data_map.get(key)
        coin_data = val[0] if isinstance(val, list) and val else (val if isinstance(val, dict) else None)
        if not coin_data:
            return default

        # Token presente su CMC = già noto → score base 10
        tags = [t.lower() for t in (coin_data.get("tags") or [])]
        exchanges = set()
        for tag, exch in _CMC_LISTING_TAGS.items():
            if tag in tags:
                exchanges.add(exch)

        tier1_hits = exchanges & _TIER1_EXCHANGES
        # 10 punti per presenza su CMC + 20 per ogni CEX tier1 rilevato dai tag
        score = 10 + min(len(tier1_hits) * 20, 80)
        result = {
            "cex_score":  min(score, 100),
            "exchanges":  sorted(exchanges)[:6],
            "is_tier1":   bool(tier1_hits),
            "tier1_exch": sorted(tier1_hits),
            "source":     "cmc",
        }
        with _cmc_lock:
            _cmc_cache[key] = (datetime.now(), result)
        log.debug(f"[CEX/CMC] {symbol} → score={score} exch={sorted(exchanges)}")
        return result

    except Exception as e:
        log.debug(f"[CEX/CMC] Errore per {symbol}: {e}")
        return default


def get_cex_listing_score(symbol: str, token_name: str = "") -> dict:
    """
    Verifica se il token è già listato su CEX.
    Strategia: CoinGecko prima (30 req/min), CMC come fallback (333 req/day).
    """
    key = symbol.upper()
    with _cg_lock:
        if key in _cg_cache:
            ts, data = _cg_cache[key]
            if datetime.now() - ts < _CG_CACHE_TTL:
                return data

    default = {"cex_score": 0, "exchanges": [], "is_tier1": False, "tier1_exch": []}

    _cg_headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    cg_ok = False
    try:
        r = requests.get(f"{COINGECKO_BASE}/search", params={"query": symbol},
                         headers=_cg_headers, timeout=8)
        if r.status_code == 200:
            coins = r.json().get("coins", [])
            coin_id = coins[0].get("id", "") if coins else ""
            if coin_id:
                r2 = requests.get(
                    f"{COINGECKO_BASE}/coins/{coin_id}",
                    params={"tickers": "true", "market_data": "false",
                            "community_data": "false", "developer_data": "false"},
                    headers=_cg_headers, timeout=10,
                )
                if r2.status_code == 200:
                    tickers    = r2.json().get("tickers", [])
                    exchanges  = {t["market"]["identifier"].lower() for t in tickers
                                  if isinstance(t.get("market"), dict)}
                    tier1_hits = exchanges & _TIER1_EXCHANGES
                    score = (20 if exchanges else 0) + min(len(tier1_hits) * 20, 80)
                    result = {
                        "cex_score":  min(score, 100),
                        "exchanges":  sorted(exchanges)[:6],
                        "is_tier1":   bool(tier1_hits),
                        "tier1_exch": sorted(tier1_hits),
                        "source":     "coingecko",
                    }
                    with _cg_lock:
                        _cg_cache[key] = (datetime.now(), result)
                    cg_ok = True
                    return result

    except Exception as e:
        log.debug(f"[CEX] Errore CoinGecko per {symbol}: {e}")

    # Fallback CMC se CoinGecko ha fallito
    if not cg_ok:
        log.debug(f"[CEX] CoinGecko fallito per {symbol} → provo CMC")
        return _get_cex_score_cmc(symbol)

    return default

# ==============================================================================
# SEZIONE 11 – DATA LAYER: HOLDER CONCENTRATION
# ==============================================================================


def fetch_holder_concentration(token_address: str, chain: str) -> dict:
    """
    Recupera top-10 holder % da Solscan (Solana) o BSCScan / Etherscan (EVM).
    Ritorna: top10_pct, is_concentrated (> MAX_TOP10_HOLDER_PCT).

    Chiavi API necessarie (gratuite):
      BSCSCAN_KEY   → https://bscscan.com/apis
      ETHERSCAN_KEY → https://etherscan.io/apis
    """
    default = {"top10_pct": 0.0, "is_concentrated": False}

    try:
        if chain == "solana":
            # Solscan v2 public API
            resp = _safe_get(
                "https://api.solscan.io/v2/token/holders",
                params={"address": token_address, "limit": 10, "offset": 0},
                timeout=8, label="Solscan/holders",
            )
            if resp is None:
                return default
            data    = resp.json().get("data", [])
            holders = data if isinstance(data, list) else []
            top10   = sum(float(h.get("amount_percentage", 0) or 0)
                          for h in holders[:10])
            thresh  = FILTER_CONFIG.get("MAX_TOP10_HOLDER_PCT", 60)
            return {
                "top10_pct":      round(top10, 1),
                "is_concentrated": top10 > thresh,
            }

        elif chain == "bsc" and BSCSCAN_KEY:
            base   = "https://api.bscscan.com/api"
            params = {
                "module": "token", "action": "tokenholderlist",
                "contractaddress": token_address,
                "page": 1, "offset": 20,
                "apikey": BSCSCAN_KEY,
            }
            resp = _safe_get(base, params=params, timeout=8, label="BSCScan/holders")
            if resp is None:
                return default
            holders = resp.json().get("result", [])
            if not isinstance(holders, list):
                return default
            # BSCScan ritorna TokenHolderQuantity come stringa
            total_supply = sum(float(h.get("TokenHolderQuantity", 0) or 0)
                               for h in holders)
            if total_supply == 0:
                return default
            top10_qty = sum(float(h.get("TokenHolderQuantity", 0) or 0)
                            for h in holders[:10])
            top10_pct = top10_qty / total_supply * 100
            thresh = FILTER_CONFIG.get("MAX_TOP10_HOLDER_PCT", 60)
            return {
                "top10_pct":      round(top10_pct, 1),
                "is_concentrated": top10_pct > thresh,
            }

        elif chain == "ethereum" and ETHERSCAN_KEY:
            base   = "https://api.etherscan.io/api"
            params = {
                "module": "token", "action": "tokenholderlist",
                "contractaddress": token_address,
                "page": 1, "offset": 20,
                "apikey": ETHERSCAN_KEY,
            }
            resp = _safe_get(base, params=params, timeout=8, label="Etherscan/holders")
            if resp is None:
                return default
            holders = resp.json().get("result", [])
            if not isinstance(holders, list):
                return default
            total = sum(float(h.get("TokenHolderQuantity", 0) or 0) for h in holders)
            if total == 0:
                return default
            top10_qty = sum(float(h.get("TokenHolderQuantity", 0) or 0)
                            for h in holders[:10])
            top10_pct = top10_qty / total * 100
            thresh = FILTER_CONFIG.get("MAX_TOP10_HOLDER_PCT", 60)
            return {
                "top10_pct":      round(top10_pct, 1),
                "is_concentrated": top10_pct > thresh,
            }

    except Exception as e:
        log.debug(f"[HolderConc] Errore {chain}/{token_address[:8]}: {e}")

    return default

# ==============================================================================
# SEZIONE 12 – DATA LAYER: DEFILLAMA
# ==============================================================================


class DefiLlamaFetcher:
    def __init__(self):
        self._protocol_cache: dict = {}
        self._search_cache:   dict = {}

    def get_tvl(self, token_symbol: str, token_address: str = "") -> float:
        sym = token_symbol.lower()
        if sym in self._protocol_cache:
            return self._protocol_cache[sym]
        slug = self._find_protocol(sym)
        if not slug:
            self._protocol_cache[sym] = 0.0
            return 0.0
        resp = _safe_get(f"{DEFILLAMA_BASE}/tvl/{slug}", label="DefiLlama/tvl", timeout=8)
        tvl  = 0.0
        if resp:
            try:
                tvl = float(resp.json())
            except Exception:
                pass
        self._protocol_cache[sym] = tvl
        return tvl

    def _find_protocol(self, symbol: str) -> Optional[str]:
        if symbol in self._search_cache:
            return self._search_cache[symbol]
        resp = _safe_get(f"{DEFILLAMA_BASE}/protocols", label="DefiLlama/protocols", timeout=10)
        if resp is None:
            self._search_cache[symbol] = None
            return None
        try:
            for p in resp.json():
                if p.get("symbol", "").lower() == symbol or p.get("name", "").lower() == symbol:
                    self._search_cache[symbol] = p["slug"]
                    return p["slug"]
        except Exception:
            pass
        self._search_cache[symbol] = None
        return None

# ==============================================================================
# SEZIONE 12b – DATA LAYER: BINANCE (klines, funding rate, open interest)
# ==============================================================================

BINANCE_SPOT_BASE    = "https://api.binance.com"
BINANCE_FUTURES_BASE = "https://fapi.binance.com"

_binance_lock      = threading.Lock()
_binance_last_call = 0.0
_BINANCE_INTERVAL  = 0.3   # max ~3 req/s — Binance free limit molto generoso


def _binance_rate_limit():
    global _binance_last_call
    with _binance_lock:
        wait = _BINANCE_INTERVAL - (time.time() - _binance_last_call)
        if wait > 0:
            time.sleep(wait)
        _binance_last_call = time.time()


class BinanceFetcher:
    """
    Wrapper per le API gratuite di Binance.
    - Spot klines:      GET /api/v3/klines
    - Futures funding:  GET /fapi/v1/fundingRate
    - Futures OI:       GET /fapi/v1/openInterest + /fapi/v1/openInterestHist
    Tutti gli endpoint sono pubblici (no API key richiesta).
    """

    _symbol_cache: dict  = {}   # sym → "TOKENUSDT" o None
    _klines_cache: dict  = {}   # (sym, interval, limit) → (ts, data)
    _KLINES_TTL   = timedelta(minutes=5)
    _oi_hist_cache: dict = {}   # sym → (ts, list)
    _OI_TTL        = timedelta(minutes=10)

    # ── Normalizzazione simbolo ────────────────────────────────────────────
    def _resolve_symbol(self, raw: str) -> Optional[str]:
        """Converte simbolo DEX (es. 'PEPE') in coppia Binance (es. 'PEPEUSDT')."""
        key = raw.upper().strip()
        if key in self._symbol_cache:
            return self._symbol_cache[key]

        # Prova TOKENUSDT prima, poi TOKENBTC, poi TOKENBUSD
        for quote in ("USDT", "USDC", "BTC"):
            pair = f"{key}{quote}"
            _binance_rate_limit()
            resp = _safe_get(
                f"{BINANCE_SPOT_BASE}/api/v3/ticker/price",
                params={"symbol": pair},
                label=f"Binance/price/{pair}",
                timeout=5,
            )
            if resp is not None:
                self._symbol_cache[key] = pair
                return pair

        self._symbol_cache[key] = None
        return None

    # ── Klines (OHLCV) ────────────────────────────────────────────────────
    def get_klines(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 100,
    ) -> list[dict]:
        """
        Ritorna lista di candele: [{"o","h","l","c","v","ts"}, ...].
        interval: 1m | 5m | 15m | 1h | 4h | 1d
        """
        pair = self._resolve_symbol(symbol)
        if pair is None:
            return []

        cache_key = (pair, interval, limit)
        if cache_key in self._klines_cache:
            ts, data = self._klines_cache[cache_key]
            if datetime.now() - ts < self._KLINES_TTL:
                return data

        _binance_rate_limit()
        resp = _safe_get(
            f"{BINANCE_SPOT_BASE}/api/v3/klines",
            params={"symbol": pair, "interval": interval, "limit": limit},
            label=f"Binance/klines/{pair}",
            timeout=10,
        )
        if resp is None:
            return []
        try:
            raw = resp.json()
            candles = [
                {
                    "ts": int(r[0]),
                    "o":  float(r[1]),
                    "h":  float(r[2]),
                    "l":  float(r[3]),
                    "c":  float(r[4]),
                    "v":  float(r[5]),
                }
                for r in raw
            ]
            self._klines_cache[cache_key] = (datetime.now(), candles)
            return candles
        except Exception as e:
            log.debug(f"[Binance] klines parse error {pair}: {e}")
            return []

    # ── Funding Rate ──────────────────────────────────────────────────────
    def get_funding_rate(self, symbol: str) -> Optional[float]:
        """
        Ritorna funding rate corrente (float). Positivo = longs pagano shorts.
        Solo per token su Binance Futures.
        """
        pair = self._resolve_symbol(symbol)
        if pair is None:
            return None
        # Prova prima la coppia spot → futures usa lo stesso simbolo
        _binance_rate_limit()
        resp = _safe_get(
            f"{BINANCE_FUTURES_BASE}/fapi/v1/fundingRate",
            params={"symbol": pair, "limit": 2},
            label=f"Binance/funding/{pair}",
            timeout=8,
        )
        if resp is None:
            return None
        try:
            data = resp.json()
            if isinstance(data, list) and data:
                return float(data[-1].get("fundingRate", 0) or 0)
        except Exception:
            pass
        return None

    # ── Open Interest ──────────────────────────────────────────────────────
    def get_open_interest(self, symbol: str) -> dict:
        """
        Ritorna dict: {oi_usd, oi_change_pct_1h}.
        oi_change_pct_1h: variazione % dell'OI nell'ultima ora.
        """
        pair = self._resolve_symbol(symbol)
        if pair is None:
            return {"oi_usd": 0.0, "oi_change_pct_1h": 0.0}

        # OI attuale
        _binance_rate_limit()
        resp_now = _safe_get(
            f"{BINANCE_FUTURES_BASE}/fapi/v1/openInterest",
            params={"symbol": pair},
            label=f"Binance/OI/{pair}",
            timeout=8,
        )
        oi_now = 0.0
        if resp_now:
            try:
                oi_now = float(resp_now.json().get("openInterest", 0) or 0)
            except Exception:
                pass

        # Storico OI (ultimi 2 punti a intervallo 1h) per calcolare il cambio
        if pair in self._oi_hist_cache:
            ts_c, hist = self._oi_hist_cache[pair]
            if datetime.now() - ts_c > self._OI_TTL:
                hist = None
        else:
            hist = None

        if hist is None:
            _binance_rate_limit()
            resp_h = _safe_get(
                f"{BINANCE_FUTURES_BASE}/futures/data/openInterestHist",
                params={"symbol": pair, "period": "1h", "limit": 3},
                label=f"Binance/OIhist/{pair}",
                timeout=8,
            )
            hist = []
            if resp_h:
                try:
                    hist = [float(r.get("sumOpenInterest", 0) or 0)
                            for r in resp_h.json()]
                except Exception:
                    pass
            self._oi_hist_cache[pair] = (datetime.now(), hist)

        oi_change_pct = 0.0
        if hist and len(hist) >= 2 and hist[0] > 0:
            oi_change_pct = (hist[-1] - hist[0]) / hist[0] * 100

        return {"oi_usd": oi_now, "oi_change_pct_1h": round(oi_change_pct, 2)}


    # ── Orderbook ─────────────────────────────────────────────────────────
    def get_orderbook(self, symbol: str, limit: int = 20) -> dict:
        """Bid/ask orderbook da Binance Spot. imbalance_ratio > 0.5 = pressione buy."""
        default = {"bid_vol": 0.0, "ask_vol": 0.0, "imbalance_ratio": 0.5, "spread_pct": 0.0}
        pair = self._resolve_symbol(symbol)
        if pair is None:
            return default
        _binance_rate_limit()
        resp = _safe_get(
            f"{BINANCE_SPOT_BASE}/api/v3/depth",
            params={"symbol": pair, "limit": limit},
            label=f"Binance/depth/{pair}", timeout=8,
        )
        if resp is None:
            return default
        try:
            data    = resp.json()
            bids    = data.get("bids", [])
            asks    = data.get("asks", [])
            bid_vol = sum(float(b[0]) * float(b[1]) for b in bids)
            ask_vol = sum(float(a[0]) * float(a[1]) for a in asks)
            total   = bid_vol + ask_vol
            ratio   = bid_vol / total if total > 0 else 0.5
            spread  = 0.0
            if bids and asks:
                bb = float(bids[0][0]); ba = float(asks[0][0])
                spread = (ba - bb) / bb * 100 if bb > 0 else 0.0
            return {"bid_vol": round(bid_vol,2), "ask_vol": round(ask_vol,2),
                    "imbalance_ratio": round(ratio,4), "spread_pct": round(spread,4)}
        except Exception as e:
            log.debug(f"[Binance] depth error {pair}: {e}")
            return default

    # ── 24h volume CEX (deposit spike proxy) ──────────────────────────────
    def get_volume_24h(self, symbol: str) -> float:
        """Volume 24h USD su Binance Spot (proxy flusso verso exchange)."""
        pair = self._resolve_symbol(symbol)
        if pair is None:
            return 0.0
        _binance_rate_limit()
        resp = _safe_get(
            f"{BINANCE_SPOT_BASE}/api/v3/ticker/24hr",
            params={"symbol": pair}, label=f"Binance/24hr/{pair}", timeout=8,
        )
        if resp is None:
            return 0.0
        try:
            return float(resp.json().get("quoteVolume", 0) or 0)
        except Exception:
            return 0.0

    # ── Long/Short Ratio (Futures) ────────────────────────────────────────
    def get_long_short_ratio(self, symbol: str, period: str = "1h") -> float:
        """
        Ritorna il rapporto long/short accounts su Binance Futures.
        < 1.0 → più short che long → potenziale short squeeze se OI cresce.
        > 1.5 → troppi long → retail FOMO, rischio correzione.
        Endpoint pubblico: /futures/data/globalLongShortAccountRatio
        """
        pair = self._resolve_symbol(symbol)
        if pair is None:
            return 1.0
        _binance_rate_limit()
        resp = _safe_get(
            f"{BINANCE_FUTURES_BASE}/futures/data/globalLongShortAccountRatio",
            params={"symbol": pair, "period": period, "limit": 1},
            label=f"Binance/lsRatio/{pair}", timeout=8,
        )
        if resp is None:
            return 1.0
        try:
            data = resp.json()
            if isinstance(data, list) and data:
                return float(data[-1].get("longShortRatio", 1.0) or 1.0)
        except Exception:
            pass
        return 1.0

    # ── Volume Trend (multi-day) ───────────────────────────────────────────
    def get_volume_trend(self, symbol: str) -> dict:
        """
        Confronta il volume delle ultime 24h con la media degli ultimi 7 giorni.
        Ritorna:
          vol_trend_ratio  : vol_24h / media_7d (>1.5 = spike significativo)
          vol_trend_days   : quanti giorni consecutivi il volume è in crescita
        Usa klines 1d per non appesantire il rate limit.
        """
        default = {"vol_trend_ratio": 1.0, "vol_trend_days": 0}
        candles = self.get_klines(symbol, interval="1d", limit=8)
        if len(candles) < 3:
            return default
        try:
            vols = [c["v"] for c in candles]
            vol_today  = vols[-1]
            avg_7d     = sum(vols[:-1]) / len(vols[:-1])
            ratio      = vol_today / avg_7d if avg_7d > 0 else 1.0

            # Quanti giorni consecutivi di crescita del volume (escluso oggi)
            consec = 0
            for i in range(len(vols) - 2, 0, -1):
                if vols[i] > vols[i - 1]:
                    consec += 1
                else:
                    break

            return {
                "vol_trend_ratio": round(ratio, 2),
                "vol_trend_days":  consec,
            }
        except Exception as e:
            log.debug(f"[Binance] volume trend error {symbol}: {e}")
            return default

    # ── Snapshot completo per large-cap pre-pump ──────────────────────────
    def get_largecap_signals(self, symbol: str) -> dict:
        """
        Aggrega tutti i segnali rilevanti per token large-cap su futures Binance.
        Chiamata unica per BinanceScanFetcher per evitare rate-limit multipli.

        Ritorna:
          oi_usd            Open Interest in USD
          oi_change_pct_1h  Variazione OI nell'ultima ora (%)
          funding_rate      Funding rate corrente (float)
          ls_ratio          Long/Short account ratio
          vol_trend_ratio   Vol 24h / media 7d
          vol_trend_days    Giorni consecutivi di crescita volume
          ob_imbalance      Orderbook bid/ask imbalance (>0.5 = buy-side)
        """
        oi      = self.get_open_interest(symbol)
        funding = self.get_funding_rate(symbol) or 0.0
        ls      = self.get_long_short_ratio(symbol)
        vt      = self.get_volume_trend(symbol)
        ob      = self.get_orderbook(symbol, limit=20)

        return {
            "oi_usd":            oi.get("oi_usd", 0.0),
            "oi_change_pct_1h":  oi.get("oi_change_pct_1h", 0.0),
            "funding_rate":      funding,
            "ls_ratio":          ls,
            "vol_trend_ratio":   vt.get("vol_trend_ratio", 1.0),
            "vol_trend_days":    vt.get("vol_trend_days", 0),
            "ob_imbalance":      ob.get("imbalance_ratio", 0.5),
        }


# Istanza globale condivisa (threadsafe: solo letture dopo inizializzazione)
_binance = BinanceFetcher()


# ==============================================================================
# SEZIONE 12c – LOGIC LAYER: PRE-PUMP PATTERN DETECTION
# ==============================================================================

def detect_bollinger_squeeze(candles: list[dict], period: int = 20) -> dict:
    """
    Bollinger Band Squeeze: BB_width < 1.5× la propria media storica.
    Una compressione della volatilità precede spesso un'esplosione direzionale.
    Ritorna: {"squeeze": bool, "bb_width_pct": float, "bb_width_z": float}
    """
    result = {"squeeze": False, "bb_width_pct": 0.0, "bb_width_z": 0.0}
    if len(candles) < period + 5:
        return result
    closes = np.array([c["c"] for c in candles], dtype=float)
    # BB width = (upper - lower) / middle * 100
    widths = []
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1: i + 1]
        mu = window.mean()
        sd = window.std()
        if mu > 0:
            widths.append(sd * 4 / mu * 100)  # 2σ band / mid
    if len(widths) < 5:
        return result
    last_width = widths[-1]
    width_mean = float(np.mean(widths))
    width_std  = float(np.std(widths)) if len(widths) > 3 else 1.0
    wz = (last_width - width_mean) / width_std if width_std > 0 else 0.0
    result["bb_width_pct"] = round(last_width, 2)
    result["bb_width_z"]   = round(wz, 2)
    # Squeeze: width attuale < media - 0.5σ (compressione rispetto alla norma)
    result["squeeze"] = bool(last_width < (width_mean - 0.5 * width_std))
    return result


def detect_ema_breakout(candles: list[dict]) -> dict:
    """
    EMA Breakout: prezzo attuale sopra EMA20, EMA50, EMA200 con inclinazione positiva.
    Ritorna: {"breakout_20": bool, "breakout_50": bool, "breakout_200": bool,
              "ema20_slope": float, "pattern": str}
    """
    result = {"breakout_20": False, "breakout_50": False, "breakout_200": False,
              "ema20_slope": 0.0, "pattern": "none"}
    if len(candles) < 20:
        return result

    closes = [c["c"] for c in candles]
    price  = closes[-1]

    def _ema(vals, span):
        if len(vals) < span:
            return None
        k = 2 / (span + 1)
        e = vals[0]
        for v in vals[1:]:
            e = v * k + e * (1 - k)
        return e

    ema20  = _ema(closes[-20:],  20)
    ema50  = _ema(closes[-min(50, len(closes)):],  50) if len(closes) >= 50  else None
    ema200 = _ema(closes[-min(200, len(closes)):], 200) if len(closes) >= 200 else None

    # Slope EMA20: confronto con 3 periodi fa
    ema20_prev = _ema(closes[-23:-3], 20) if len(closes) >= 23 else None
    slope = 0.0
    if ema20 and ema20_prev and ema20_prev > 0:
        slope = (ema20 - ema20_prev) / ema20_prev * 100

    result["ema20_slope"] = round(slope, 3)
    if ema20:
        result["breakout_20"] = bool(price > ema20 and slope > 0)
    if ema50:
        result["breakout_50"] = bool(price > ema50)
    if ema200:
        result["breakout_200"] = bool(price > ema200)

    # Classifica il pattern
    if result["breakout_200"] and result["breakout_50"] and slope > 0.5:
        result["pattern"] = "strong_bull"
    elif result["breakout_50"] and slope > 0:
        result["pattern"] = "bull"
    elif result["breakout_20"] and slope > 0:
        result["pattern"] = "early_bull"
    elif not result["breakout_20"] and slope < 0:
        result["pattern"] = "bear"

    return result


def detect_rsi_divergence(candles: list[dict], period: int = 14) -> dict:
    """
    RSI Bullish Divergence: prezzo fa nuovi minimi ma RSI sale (bottom reversal).
    Ritorna: {"divergence": bool, "rsi": float, "rsi_trend": str}
    """
    result = {"divergence": False, "rsi": 50.0, "rsi_trend": "neutral"}
    if len(candles) < period + 5:
        return result
    closes = [c["c"] for c in candles]
    # Calcola RSI
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    if len(gains) < period:
        return result
    avg_gain = float(np.mean(gains[-period:]))
    avg_loss = float(np.mean(losses[-period:]))
    rs  = avg_gain / avg_loss if avg_loss > 1e-9 else 100
    rsi = 100 - (100 / (1 + rs))
    result["rsi"] = round(rsi, 1)

    # RSI corrente vs RSI di 5 candele fa
    gains_prev  = gains[-(period + 5): -5]
    losses_prev = losses[-(period + 5): -5]
    if len(gains_prev) >= period:
        ag2 = float(np.mean(gains_prev[-period:]))
        al2 = float(np.mean(losses_prev[-period:]))
        rs2 = ag2 / al2 if al2 > 1e-9 else 100
        rsi_prev = 100 - (100 / (1 + rs2))
        price_now  = closes[-1]
        price_prev = closes[-(5 + 1)]
        # Divergenza rialzista: prezzo scende ma RSI sale (o piatto)
        if price_now < price_prev and rsi > rsi_prev + 3:
            result["divergence"] = True
            result["rsi_trend"]  = "bullish_divergence"
        elif rsi > 60:
            result["rsi_trend"] = "overbought"
        elif rsi < 40:
            result["rsi_trend"] = "oversold"

    return result


def detect_oi_spike(oi_data: dict) -> dict:
    """
    Open Interest Spike: variazione OI > 20% in 1h = nuovo interesse speculativo.
    Ritorna: {"spike": bool, "oi_change_pct": float}
    """
    change = oi_data.get("oi_change_pct_1h", 0.0)
    return {
        "spike":         abs(change) >= 20.0,
        "oi_change_pct": change,
    }


def detect_funding_squeeze(funding_rate: Optional[float]) -> dict:
    """
    Short Squeeze Setup: funding rate negativo → shorts pagano longs → pressione
    per chiusura short → potenziale rally.
    Funding rate molto positivo → segnale di surriscaldamento (calo imminente).
    Ritorna: {"short_squeeze_setup": bool, "overheated": bool, "funding_rate": float}
    """
    if funding_rate is None:
        return {"short_squeeze_setup": False, "overheated": False, "funding_rate": 0.0}
    return {
        "short_squeeze_setup": funding_rate < -0.0003,   # < -0.03% per 8h
        "overheated":          funding_rate > 0.002,     # > +0.2% per 8h
        "funding_rate":        round(funding_rate * 100, 4),  # in %
    }


def detect_orderbook_imbalance(ob_data: dict, threshold: float = 0.65) -> dict:
    """
    Orderbook Imbalance: bid/ask skew persistente → pressione direzionale.
    imbalance_ratio > threshold = dominanza buyer (bullish).
    """
    ratio   = ob_data.get("imbalance_ratio", 0.5)
    bullish = ratio > threshold
    bearish = ratio < (1 - threshold)
    return {
        "imbalance":   bool(bullish),
        "direction":   "buy" if bullish else ("sell" if bearish else "neutral"),
        "bid_ratio":   round(ratio, 4),
    }


def detect_deposit_spike(profile: dict, binance_vol_24h: float) -> dict:
    """
    Deposit Spike proxy: CEX volume >> DEX volume → grandi player verso exchange.
    Spike se CEX vol >= 5x DEX e CEX vol > 500k USD.
    """
    dex_vol24 = max(profile.get("volume_24h_usd", 0), 0)
    total     = binance_vol_24h + dex_vol24
    if total < 10_000 or binance_vol_24h == 0:
        return {"spike": False, "cex_dex_ratio": 0.0, "cex_dominance_pct": 0.0}
    ratio  = binance_vol_24h / max(dex_vol24, 1)
    cex_dom = binance_vol_24h / total * 100
    spike  = ratio >= 5.0 and binance_vol_24h > 500_000
    return {"spike": bool(spike), "cex_dex_ratio": round(ratio, 2),
            "cex_dominance_pct": round(cex_dom, 1)}


def detect_oi_funding_divergence(oi_data: dict, funding_rate: Optional[float]) -> dict:
    """
    OI / Funding Divergence:
      OI sale + funding negativo → short squeeze buildup (bullish)
      OI sale + funding molto positivo → longs over-leveraged (rischio correzione)
    """
    oi_chg        = oi_data.get("oi_change_pct_1h", 0.0)
    fr            = funding_rate if funding_rate is not None else 0.0
    oi_rising     = oi_chg > 10.0
    fr_negative   = fr < -0.0002
    fr_very_pos   = fr > 0.0015
    fr_neutral    = abs(fr) < 0.0003
    sq = bool(oi_rising and fr_negative)
    ol = bool(oi_rising and fr_very_pos)
    div = bool(sq or ol)
    if sq:           dtype = "short_squeeze_buildup"
    elif ol:         dtype = "overleveraged_longs"
    elif oi_rising and fr_neutral: dtype = "oi_buildup_neutral"
    else:            dtype = "none"
    return {"divergence": div, "type": dtype, "squeeze_potential": sq,
            "oi_change_pct": round(oi_chg,2), "funding_pct": round(fr*100,4)}


def detect_prepump_patterns(
    symbol:         str,
    profile:        dict,
    binance:        BinanceFetcher,
) -> dict:
    """
    Aggregatore di tutti i pattern pre-pump.
    Chiama BinanceFetcher per dati OHLCV/derivati, poi invoca i singoli detector.

    Ritorna dict con tutti i flag + dati grezzi, pronto per score_gem().
    """
    out = {
        # Tecnici
        "pp_vol_explosion":    False,
        "pp_bb_squeeze":       False,
        "pp_ema_breakout":     False,
        "pp_rsi_divergence":   False,
        # Derivati
        "pp_oi_spike":         False,
        "pp_short_squeeze":    False,
        "pp_funding_rate":     0.0,
        "pp_oi_change_pct":    0.0,
        # On-chain proxy (dal volume DEX)
        "pp_whale_accum":      False,
        # Dati grezzi (per CSV / debug)
        "pp_bb_width_pct":     0.0,
        "pp_ema_pattern":      "none",
        "pp_rsi":              50.0,
        "pp_ema20_slope":      0.0,
        # Orderbook / derivati avanzati
        "pp_ob_imbalance":     False,
        "pp_ob_bid_ratio":     0.5,
        "pp_deposit_spike":    False,
        "pp_cex_dex_ratio":    0.0,
        "pp_oi_funding_div":   False,
        "pp_oi_div_type":      "none",
        "pp_patterns_count":   0,
    }

    # ── 1. Volume explosion (già calcolato da compute_early_metrics) ──────
    vol_z = profile.get("volume_z_score", 0.0)
    ramp  = profile.get("volume_ramp_ratio", 1.0)
    out["pp_vol_explosion"] = vol_z >= 2.5 or ramp >= 4.0

    # ── 2. Klines Binance ─────────────────────────────────────────────────
    candles_1h = binance.get_klines(symbol, "1h", 100)
    candles_4h = binance.get_klines(symbol, "4h", 50)

    if candles_1h:
        # Bollinger Squeeze su 1h
        bb = detect_bollinger_squeeze(candles_1h)
        out["pp_bb_squeeze"]    = bb["squeeze"]
        out["pp_bb_width_pct"]  = bb["bb_width_pct"]

        # RSI Divergence su 1h
        rsi_info = detect_rsi_divergence(candles_1h)
        out["pp_rsi_divergence"] = rsi_info["divergence"]
        out["pp_rsi"]            = rsi_info["rsi"]

    # EMA breakout: usa 4h per segnale più affidabile, fallback 1h
    candles_ema = candles_4h if len(candles_4h) >= 50 else candles_1h
    if candles_ema:
        ema_info = detect_ema_breakout(candles_ema)
        out["pp_ema_breakout"] = ema_info["breakout_20"] or ema_info["breakout_50"]
        out["pp_ema_pattern"]  = ema_info["pattern"]
        out["pp_ema20_slope"]  = ema_info["ema20_slope"]

    # ── 3. Open Interest ──────────────────────────────────────────────────
    oi_data = binance.get_open_interest(symbol)
    oi_info  = detect_oi_spike(oi_data)
    out["pp_oi_spike"]      = oi_info["spike"]
    out["pp_oi_change_pct"] = oi_info["oi_change_pct"]

    # ── 4. Funding Rate ───────────────────────────────────────────────────
    fr = binance.get_funding_rate(symbol)
    fr_info = detect_funding_squeeze(fr)
    out["pp_short_squeeze"] = fr_info["short_squeeze_setup"]
    out["pp_funding_rate"]  = fr_info["funding_rate"]
    # Nota: overheated è una penalità — la passiamo nel profilo per score_gem
    out["pp_overheated"]    = fr_info["overheated"]

    # ── 5. Whale accumulation proxy ───────────────────────────────────────
    # Proxy: inflow smart money elevato + liquidity crescente (exchange outflow)
    inflow  = profile.get("inflow_usd", 0)
    liq_chg = profile.get("liq_change_30m_pct", 0.0)
    wallets = profile.get("inflow_wallet_count", 0)
    mcap    = max(profile.get("market_cap_usd", 1), 1)
    # Balene = inflow > 1% mcap + liquidità sale + wallet significativi
    out["pp_whale_accum"] = (
        inflow / mcap >= 0.01 and
        liq_chg > 5.0 and
        wallets >= 5
    )

    # ── 6. Orderbook imbalance ────────────────────────────────────────────
    ob_data = bn.get_orderbook(symbol)
    ob_info  = detect_orderbook_imbalance(ob_data)
    out["pp_ob_imbalance"] = ob_info["imbalance"]
    out["pp_ob_bid_ratio"] = ob_info["bid_ratio"]

    # ── 7. Deposit spike (CEX volume vs DEX volume) ───────────────────────
    cex_vol24h = bn.get_volume_24h(symbol)
    ds_info    = detect_deposit_spike(profile, cex_vol24h)
    out["pp_deposit_spike"] = ds_info["spike"]
    out["pp_cex_dex_ratio"] = ds_info["cex_dex_ratio"]

    # ── 8. OI / Funding divergence ────────────────────────────────────────
    oi_div = detect_oi_funding_divergence(oi_data, fr)
    out["pp_oi_funding_div"] = oi_div["divergence"]
    out["pp_oi_div_type"]    = oi_div["type"]
    if oi_div["squeeze_potential"] and not out["pp_short_squeeze"]:
        out["pp_short_squeeze"] = True

    # ── 9. Conteggio pattern attivati ─────────────────────────────────────
    pattern_flags = [
        "pp_vol_explosion", "pp_bb_squeeze", "pp_ema_breakout",
        "pp_rsi_divergence", "pp_oi_spike", "pp_short_squeeze", "pp_whale_accum",
        "pp_ob_imbalance", "pp_deposit_spike", "pp_oi_funding_div",
    ]
    out["pp_patterns_count"] = sum(1 for k in pattern_flags if out.get(k))

    return out



# ==============================================================================
# SEZIONE 12d – DATA LAYER: COINGECKO TRENDING + BINANCE SCANNER
# ==============================================================================

_cg_trending_lock     = threading.Lock()
_cg_trending_cache: dict = {}
_CG_TRENDING_TTL      = timedelta(hours=2)    # 2h: CoinGecko aggiorna trending ogni ~2h


class CoinGeckoTrendingFetcher:
    """
    Recupera i token in tendenza da CoinGecko (top 15 ricercati nelle ultime 24h).
    API gratuita, no chiave. Limite: ~30 req/min.

    Segnale precoce: token che entrano in trending spesso registrano movimento
    di prezzo nelle ore successive.
    """

    def get_trending(self, chain_filter: str = "") -> list[dict]:
        """
        Ritorna lista di dune_token normalizzati dai CoinGecko trending coins.
        chain_filter: se specificato (es. "solana", "bsc"), filtra per chain.
        """
        cache_key = chain_filter or "all"
        with _cg_trending_lock:
            if cache_key in _cg_trending_cache:
                ts, data = _cg_trending_cache[cache_key]
                if datetime.now() - ts < _CG_TRENDING_TTL:
                    return data

        try:
            time.sleep(0.4)  # gentle rate limit
            resp = requests.get(
                f"{COINGECKO_BASE}/search/trending",
                timeout=10,
            )
            if resp.status_code != 200:
                return []
            coins = resp.json().get("coins", [])
        except Exception as e:
            log.debug(f"[CGTrending] Errore fetch: {e}")
            return []

        # Mappa chain CoinGecko → chain interna
        _CG_CHAIN_MAP = {
            "solana":          "solana",
            "binance-smart-chain": "bsc",
            "ethereum":        "ethereum",
            "base":            "ethereum",  # trattato come ETH
        }

        result = []
        for item in coins:
            coin = item.get("item", {})
            cg_chain = (coin.get("platforms") or {})
            # Prova a determinare la chain
            detected_chain = "solana"  # default
            token_addr     = ""
            for cg_key, int_chain in _CG_CHAIN_MAP.items():
                if cg_key in cg_chain:
                    detected_chain = int_chain
                    token_addr     = cg_chain[cg_key] or ""
                    break

            if chain_filter and detected_chain != chain_filter:
                continue

            sym   = (coin.get("symbol") or "").upper()
            name  = coin.get("name", "")
            score = float(coin.get("score", 0) or 0)  # ranking posizione (0=top)
            mcap  = float((coin.get("data") or {}).get("market_cap_btc") or 0)

            # Stima inflow artificiale basata sul ranking trending
            # (posizione 0 = 1° = più cercato → più segnale)
            est_inflow = max(5_001, 50_000 - score * 3_000)

            result.append({
                "token_address":       token_addr or f"cg_{sym.lower()}",
                "token_symbol":        sym,
                "token_name":          name,
                "chain":               detected_chain,
                "inflow_usd":          est_inflow,
                "inflow_wallet_count": max(5, int(15 - score)),
                "avg_wallet_pnl_pct":  0.0,
                "source":              "coingecko_trending",
                "cg_trending_rank":    int(score) + 1,
            })

        with _cg_trending_lock:
            _cg_trending_cache[cache_key] = (datetime.now(), result)

        log.info(f"[CGTrending] {len(result)} token trending CoinGecko"
                 + (f" ({chain_filter})" if chain_filter else ""))
        return result


class CoinGeckoMidCapFetcher:
    """
    Scansiona CoinGecko per token mid-cap ($5M–$300M market cap) con attività anomala.

    Criteri di selezione:
      - Price change 24h >= MIN_PRICE_CHG_24H (default 8%)
      - Volume/market-cap ratio >= MIN_VOL_MCAP (default 10%) → interesse anomalo relativo
    Per ogni candidato risolve il pair DEX via DexScreener.
    Usa la Demo API key per rate limit migliore (30 req/min stabile).

    Eseguito ogni 3 cicli del main loop (~15 min) per contenere i costi API.
    """

    _lock      = threading.Lock()
    _cache_ts: Optional[datetime] = None
    _cache_data: list = []
    _CACHE_TTL = timedelta(hours=1)     # 1h: cattura movers prima che il move sia già avvenuto

    # Soglie configurabili — mid cap ($5M–$300M)
    MIN_MCAP              = 5_000_000
    MIN_PRICE_CHG         = 5.0          # era 8% → 5%: catch accumuli settimanali precoci
    MIN_VOL_MCAP          = 0.08         # era 10% → 8%: leggermente più permissivo
    MIN_WEEKLY_CHG        = 15.0         # % change 7gg minimo (filtro alternativo al 24h)

    # Soglie large cap (>$300M) — filtri più severi orientati al price-growth
    LARGE_CAP_THRESHOLD   = 300_000_000
    LARGE_CAP_MIN_CHG24   = 15.0         # % change 24h minimo (vs 8% mid-cap)
    LARGE_CAP_MIN_VOL_MC  = 0.15         # volume/mcap minimo (vs 10% mid-cap)
    LARGE_CAP_MIN_RANGE   = 0.60         # prezzo nel 60%+ del range 24h (pressione d'acquisto sostenuta)
    LARGE_CAP_MAX_ATH_DRP = -40.0        # max distanza dall'ATH: -40% (evita rimbalzi da crash)

    PER_PAGE        = 250
    MAX_PAGES       = 2   # ripristinato da 4: risparmia quota CoinGecko (500 coin sufficienti)

    # Mappa chainId DexScreener → chain interna
    _DCHAIN_MAP = {
        "solana":   "solana",
        "bsc":      "bsc",
        "ethereum": "ethereum",
        "base":     "ethereum",
        "arbitrum": "ethereum",
        "polygon":  "ethereum",
    }

    def get_movers(self, chain_filter: str = "") -> list[dict]:
        """
        Ritorna lista di dune_token normalizzati per i mid-cap movers.
        chain_filter: se specificato, filtra per chain interna.
        """
        with self._lock:
            if self._cache_ts and datetime.now() - self._cache_ts < self._CACHE_TTL:
                data = self._cache_data
                return [t for t in data if t.get("chain") == chain_filter] if chain_filter else list(data)

        _headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
        candidates = []

        for page in range(1, self.MAX_PAGES + 1):
            try:
                time.sleep(1.2)  # Demo: ~30 req/min
                resp = requests.get(
                    f"{COINGECKO_BASE}/coins/markets",
                    headers=_headers,
                    params={
                        "vs_currency":             "usd",
                        "order":                   "volume_desc",
                        "per_page":                self.PER_PAGE,
                        "page":                    page,
                        "price_change_percentage": "24h,7d",
                        "sparkline":               "false",
                    },
                    timeout=20,
                )
                if resp.status_code != 200:
                    log.debug(f"[CGMidCap] HTTP {resp.status_code} pagina {page}")
                    break
                coins = resp.json()
                if not coins:
                    break
            except Exception as e:
                log.debug(f"[CGMidCap] Errore fetch pagina {page}: {e}")
                break

            for coin in coins:
                mcap = float(coin.get("market_cap") or 0)
                if mcap < self.MIN_MCAP:
                    continue

                vol24     = float(coin.get("total_volume") or 0)
                vol_ratio = vol24 / mcap if mcap > 0 else 0
                chg24     = float(coin.get("price_change_percentage_24h") or 0)
                chg7d     = float(coin.get("price_change_percentage_7d_in_currency") or 0)
                is_lc     = mcap > self.LARGE_CAP_THRESHOLD

                if is_lc:
                    # ── Large cap (>$300M): filtri orientati al price-growth ──
                    if chg24 < self.LARGE_CAP_MIN_CHG24:
                        continue
                    if vol_ratio < self.LARGE_CAP_MIN_VOL_MC:
                        continue
                    high_24h  = float(coin.get("high_24h") or 0)
                    low_24h   = float(coin.get("low_24h") or 0)
                    cur_price = float(coin.get("current_price") or 0)
                    ath_chg   = float(coin.get("ath_change_percentage") or 0)
                    span      = high_24h - low_24h
                    range_pos = (cur_price - low_24h) / span if span > 0 else 0
                    if range_pos < self.LARGE_CAP_MIN_RANGE:
                        continue   # prezzo nella metà bassa del range: momentum in calo
                    if ath_chg < self.LARGE_CAP_MAX_ATH_DRP:
                        continue   # troppo sotto l'ATH: rischio rimbalzo da crash, non breakout
                else:
                    # ── Mid cap ($5M–$300M): filtri standard + weekly momentum alternativo ──
                    # Passa se: (chg24 >= MIN_PRICE_CHG E vol > soglia)
                    #        OPPURE (chg7d >= MIN_WEEKLY_CHG E vol > 5%) — accumulo settimanale
                    ok_24h   = chg24 >= self.MIN_PRICE_CHG and vol_ratio >= self.MIN_VOL_MCAP
                    ok_7d    = chg7d >= self.MIN_WEEKLY_CHG and vol_ratio >= 0.05
                    if not ok_24h and not ok_7d:
                        continue
                    high_24h = low_24h = cur_price = ath_chg = range_pos = 0.0

                candidates.append({
                    "sym":       (coin.get("symbol") or "").upper(),
                    "name":      coin.get("name", ""),
                    "mcap":      mcap,
                    "vol24":     vol24,
                    "vol_ratio": vol_ratio,
                    "chg24":     chg24,
                    "chg7d":     chg7d,
                    "is_lc":     is_lc,
                    "range_pos": round(range_pos, 3),
                    "ath_chg":   round(ath_chg, 2),
                })

        n_lc  = sum(1 for c in candidates if c["is_lc"])
        n_7d  = sum(1 for c in candidates if not c["is_lc"] and c["chg7d"] >= self.MIN_WEEKLY_CHG)
        log.info(f"[CGMidCap] {len(candidates)} candidati pre-DexScreener "
                 f"(mid: {len(candidates)-n_lc} di cui {n_7d} via 7d-momentum | "
                 f"large: {n_lc} @ chg>{self.LARGE_CAP_MIN_CHG24:.0f}%/vol>{self.LARGE_CAP_MIN_VOL_MC:.0%}"
                 f"/range>{self.LARGE_CAP_MIN_RANGE:.0%})")

        result = []
        for c in candidates:
            sym = c["sym"]
            token_addr     = ""
            detected_chain = ""
            try:
                time.sleep(0.4)
                r2 = requests.get(
                    f"{DEXSCREENER_BASE}/latest/dex/search",
                    params={"q": sym},
                    timeout=8,
                )
                if r2.status_code == 200:
                    pairs = r2.json().get("pairs") or []
                    valid = [
                        p for p in pairs
                        if (p.get("baseToken", {}).get("symbol", "").upper() == sym
                            and float((p.get("liquidity") or {}).get("usd", 0) or 0) > 20_000)
                    ]
                    if valid:
                        best = max(valid, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
                        token_addr     = best.get("baseToken", {}).get("address", "")
                        dex_chain_id   = best.get("chainId", "")
                        detected_chain = self._DCHAIN_MAP.get(dex_chain_id, "")
            except Exception as e:
                log.debug(f"[CGMidCap] DexScreener lookup {sym}: {e}")

            if not token_addr or not detected_chain:
                log.debug(f"[CGMidCap] {sym}: nessun pair DEX trovato — skip")
                continue

            # Estrai BSR e volume 1h reali dal pair DexScreener già fetchato
            dex_bsr   = 1.0
            dex_vol1h = 0.0
            dex_liq   = 0.0
            if valid and best:
                _txns  = best.get("txns", {}).get("h1", {})
                _buys  = int(_txns.get("buys", 0))
                _sells = int(_txns.get("sells", 0))
                if _buys + _sells > 0:
                    dex_bsr = round(_buys / (_buys + _sells), 3)
                dex_vol1h = float((best.get("volume") or {}).get("h1") or 0)
                dex_liq   = float((best.get("liquidity") or {}).get("usd") or 0)

            # Stima inflow artificiale dal volume (5% del vol24h come proxy smart-money)
            est_inflow = max(5_001, c["vol24"] * 0.05)

            result.append({
                "token_address":       token_addr,
                "token_symbol":        sym,
                "token_name":          c["name"],
                "chain":               detected_chain,
                "inflow_usd":          est_inflow,
                "inflow_wallet_count": 10,
                "avg_wallet_pnl_pct":  0.0,
                "source":              "coingecko_midcap",
                "cg_mcap_usd":         c["mcap"],
                "cg_vol24_usd":        c["vol24"],
                "cg_vol_mcap_ratio":   round(c["vol_ratio"], 4),
                "cg_price_chg24":      round(c["chg24"], 2),
                "cg_price_chg7d":      round(c["chg7d"], 2),
                "cg_is_largecap":      c["is_lc"],
                "cg_range_position":   c["range_pos"],
                "cg_ath_chg_pct":      c["ath_chg"],
                "cg_dex_bsr":          dex_bsr,    # BSR reale da DexScreener (1h)
                "cg_dex_vol1h":        dex_vol1h,
                "cg_dex_liq":          dex_liq,
            })

        with self._lock:
            self._cache_ts   = datetime.now()
            self._cache_data = result

        log.info(f"[CGMidCap] {len(result)} mid-cap movers con pair DEX risolto")
        return [t for t in result if t.get("chain") == chain_filter] if chain_filter else result


class BinanceScanFetcher:
    """
    Scansiona tutti i futures Binance per trovare token con attività anomala:
    - Volume 24h spike (volume quoteAsset >> media storica)
    - Price change anomalo in 1h
    - Usato come fonte supplementare di scoperta per token large-cap.

    API gratuita, no chiave: GET /fapi/v1/ticker/24hr
    """

    _cache_ts:        Optional[datetime] = None
    _cache_data:      list = []
    _unresolved_data: list = []   # token Binance senza pair DEX
    _CACHE_TTL = timedelta(minutes=10)

    # Token esclusi: solo stablecoin e mega-cap non segnalabili come "gem".
    # NON escludere altcoin come INJ, SUI, ARB, OP — hanno pre-pump reali.
    _EXCLUDE = {
        # Stablecoin
        "USDTUSDT","USDCUSDT","BUSDUSDT","TUSDUSDT","DAIUSDT","FRAXUSDT",
        # Mega-cap: troppo liquidi per pump significativi
        "BTCUSDT","ETHUSDT","BNBUSDT",
    }

    def scan(
        self,
        min_vol_24h_usd:  float = 10_000_000,   # min volume 24h in USD
        min_price_chg_1h: float = 3.0,           # min variazione % assoluta 1h
        limit:            int   = 15,
    ) -> list[dict]:
        """
        Ritorna i top-N futures con volume elevato e variazione di prezzo anomala.
        """
        if (self._cache_ts is not None and
                datetime.now() - self._cache_ts < self._CACHE_TTL):
            return self._cache_data

        self._unresolved_data = []   # reset ogni fetch fresco
        try:
            resp = requests.get(
                f"{BINANCE_FUTURES_BASE}/fapi/v1/ticker/24hr",
                timeout=15,
            )
            if resp.status_code != 200:
                return []
            tickers = resp.json()
        except Exception as e:
            log.debug(f"[BinanceScan] Errore fetch: {e}")
            return []

        candidates = []
        for t in tickers:
            sym = t.get("symbol", "")
            if sym in self._EXCLUDE or not sym.endswith("USDT"):
                continue
            try:
                vol24  = float(t.get("quoteVolume", 0) or 0)
                chg1h  = abs(float(t.get("priceChangePercent", 0) or 0))
                price  = float(t.get("lastPrice", 0) or 0)
            except Exception:
                continue
            if vol24 < min_vol_24h_usd or chg1h < min_price_chg_1h:
                continue
            # Ricava token symbol: "PEPEUSDT" → "PEPE"
            token_sym = sym.replace("USDT", "").replace("PERP", "")
            candidates.append({
                "symbol":   sym,
                "token":    token_sym,
                "vol24":    vol24,
                "chg1h":    chg1h,
                "price":    price,
            })

        # Ordina per volume × |change| (momentum ponderato)
        candidates.sort(key=lambda x: x["vol24"] * x["chg1h"], reverse=True)
        top = candidates[:limit]

        # Mappa dexscreener chainId → nome interno
        _dex_to_chain = {v["dexscreener_id"]: k for k, v in CHAINS.items()}

        result = []
        for c in top:
            tok = c["token"]

            # ── Risolvi indirizzo reale via DexScreener search ──────────────
            real_address: Optional[str] = None
            real_chain:   str           = "ethereum"
            try:
                resp = requests.get(
                    f"{DEXSCREENER_BASE}/latest/dex/search",
                    params={"q": tok},
                    timeout=10,
                )
                if resp.status_code == 200:
                    pairs = resp.json().get("pairs") or []
                    # Filtra solo chain supportate e token che matchano il simbolo
                    valid = [
                        p for p in pairs
                        if (p.get("baseToken", {}).get("symbol", "").upper() == tok.upper()
                            and (p.get("chainId") or "").lower() in _dex_to_chain)
                    ]
                    if valid:
                        # Scegli il pair con liquidità più alta tra le chain supportate
                        best = max(
                            valid,
                            key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
                        )
                        real_address = (best.get("baseToken") or {}).get("address", "")
                        dex_cid      = (best.get("chainId") or "").lower()
                        real_chain   = _dex_to_chain.get(dex_cid, "ethereum")
            except Exception as e:
                log.debug(f"[BinanceScan] DexScreener lookup failed for {tok}: {e}")

            if not real_address:
                log.debug(f"[BinanceScan] {tok}: indirizzo non trovato su DexScreener — alert only")
                self._unresolved_data.append({
                    "token_symbol": tok,
                    "binance_vol24": c["vol24"],
                    "binance_chg1h": c["chg1h"],
                    "binance_price": c["price"],
                })
                continue

            # ── Segnali large-cap: OI, funding, LS ratio, vol trend ──────
            lc_signals: dict = {}
            try:
                lc_signals = _binance.get_largecap_signals(tok)
            except Exception as _lce:
                log.debug(f"[BinanceScan] largecap signals error {tok}: {_lce}")

            result.append({
                "token_address":       real_address,
                "token_symbol":        tok,
                "token_name":          tok,
                "chain":               real_chain,
                "inflow_usd":          min(c["vol24"] * 0.05, 500_000),
                "inflow_wallet_count": 10,
                "avg_wallet_pnl_pct":  0.0,
                "source":              "binance_scan",
                # Segnali CEX (vol spike)
                "binance_vol24":       c["vol24"],
                "binance_chg1h":       c["chg1h"],
                # Segnali large-cap pre-pump
                "oi_usd":              lc_signals.get("oi_usd", 0.0),
                "oi_change_pct_1h":    lc_signals.get("oi_change_pct_1h", 0.0),
                "funding_rate":        lc_signals.get("funding_rate", 0.0),
                "ls_ratio":            lc_signals.get("ls_ratio", 1.0),
                "vol_trend_ratio":     lc_signals.get("vol_trend_ratio", 1.0),
                "vol_trend_days":      lc_signals.get("vol_trend_days", 0),
                "ob_imbalance":        lc_signals.get("ob_imbalance", 0.5),
            })
            log.debug(
                f"[BinanceScan] {tok} → {real_chain}:{real_address} | "
                f"OI_chg={lc_signals.get('oi_change_pct_1h', 0):+.1f}% | "
                f"funding={lc_signals.get('funding_rate', 0)*100:.3f}% | "
                f"LS={lc_signals.get('ls_ratio', 1):.2f} | "
                f"vol_trend={lc_signals.get('vol_trend_ratio', 1):.1f}x"
            )

        self._cache_ts   = datetime.now()
        self._cache_data = result
        log.info(f"[BinanceScan] {len(result)} token con attività anomala su Binance Futures")
        return result


# ==============================================================================
# SEZIONE 13 – LOGIC LAYER: GEM AGGREGATOR
# ==============================================================================


def build_gem_profile(
    dune_token:      dict,
    social_analyzer: SocialAnalyzer,
    defillama:       DefiLlamaFetcher,
    binance:         Optional[BinanceFetcher] = None,
) -> Optional[dict]:
    """
    Costruisce il profilo completo aggregando tutti i data source.
    Ritorna None se il token non ha dati DexScreener sufficienti.
    """
    chain   = dune_token.get("chain", "")
    address = dune_token.get("token_address", "")
    symbol  = dune_token.get("token_symbol", "")

    # ── 1. DexScreener ────────────────────────────────────────────────────
    pair = fetch_dexscreener_token(address, chain)
    if pair is None and symbol:
        resp = _safe_get(f"{DEXSCREENER_BASE}/latest/dex/search",
                         params={"q": symbol}, label="DexScreener/search")
        if resp:
            try:
                pairs = resp.json().get("pairs") or []
                dex_chain = CHAINS.get(chain, {}).get("dexscreener_id", chain)
                chain_p = [p for p in pairs
                           if (p.get("chainId") or "").lower() == dex_chain.lower()]
                pair = chain_p[0] if chain_p else None
            except Exception:
                pass
    if pair is None:
        return None

    market = parse_dexscreener_pair(pair, chain)

    # Chain mismatch guard
    market_chain = market.get("chain", "")
    if market_chain and chain and market_chain != chain:
        log.debug(f"[Gem] {symbol}: chain mismatch ({chain} vs {market_chain}) — skip")
        return None

    # ── 2. Early metrics & feature engineering ────────────────────────────
    wallet_count = dune_token.get("inflow_wallet_count", 0)
    em = compute_early_metrics(address, wallet_count, pair)

    # ── 3. Social — LAZY: eseguita dopo GemFilter per non sprecare chiamate ──
    # Placeholder neutro; il main loop lo completa se il token passa il filtro.
    social = {"score": 0, "tweets": 0, "source": "deferred"}

    # ── 4. TVL (lazy — solo su token che passano il filtro) ───────────────
    tvl = 0.0

    # ── 5. GoPlus Security (solo EVM — Solana returns subito) ────────────
    security = fetch_goplus_security(address, chain)

    # ── 6+7. CEX check + Holder — LAZY: eseguiti nel main loop post-filtro ─
    # Risparmia ~2-3 API call per token scartato (maggioranza).
    cex_info:    dict = {"cex_score": 0, "exchanges": [], "is_tier1": False}
    holder_info: dict = {"top10_pct": 0.0, "is_concentrated": False}

    # ── 8. Assembla profilo ───────────────────────────────────────────────
    mcap   = market.get("market_cap_usd", 0)
    liq    = market.get("liquidity_usd", 0)
    inflow = dune_token.get("inflow_usd", 0)

    # Stima mcap se mancante (tipico Solana early-stage)
    if mcap <= 0 and liq > 0:
        mcap = liq * 3
        market["market_cap_usd"] = mcap

    n_wallets = max(int(dune_token.get("inflow_wallet_count", 1) or 1), 1)
    raw_pnl   = float(dune_token.get("avg_wallet_pnl_pct") or
                      dune_token.get("avg_pnl_pct") or 0)
    avg_pnl   = raw_pnl if raw_pnl > 0 else min((inflow / n_wallets) / 2000, 100.0)

    # Usa sempre il mint address confermato da DexScreener (baseToken.address).
    # Se address originale è un pair/pool address (es. PumpSwap bonding curve),
    # DexScreener search non lo trova — usiamo il mint reale dal pair.
    dex_mint = market.get("token_address", "")
    if dex_mint and dex_mint != address:
        log.debug(f"[Gem] {symbol}: token_address corretto {address[:8]}… → {dex_mint[:8]}…")
        address = dex_mint

    profile = {
        # Identità
        "token_address":    address,
        "token_symbol":     symbol,
        "token_name":       dune_token.get("token_name", market.get("token_name", "")),
        "chain":            chain,
        "pair_address":     market.get("pair_address", ""),
        "dex_id":           market.get("dex_id", ""),

        # Smart money (Dune)
        "inflow_usd":             inflow,
        "inflow_wallet_count":    wallet_count,
        "avg_wallet_pnl_pct":     avg_pnl,
        "inflow_to_mcap_ratio":   inflow / mcap if mcap > 0 else 0,
        # Wallet cluster (da Dune v3)
        "repeat_buyer_count":  int(dune_token.get("repeat_buyer_count", 0) or 0),
        "repeat_buyer_ratio":  float(dune_token.get("repeat_buyer_ratio", 0) or 0),
        "max_single_trade_usd": float(dune_token.get("max_single_trade", 0) or 0),
        # Momentum freshness (da Dune v4) — quota di inflow nelle ultime 2h
        "inflow_last_2h":        float(dune_token.get("inflow_last_2h", 0) or 0),
        "inflow_recency_ratio":  float(dune_token.get("inflow_recency_ratio", 0) or 0),
        "buyers_last_2h":        int(dune_token.get("buyers_last_2h", 0) or 0),

        # Mercato (DexScreener)
        "price_usd":         market.get("price_usd", 0),
        "market_cap_usd":    mcap,
        "liquidity_usd":     liq,
        "volume_5m_usd":     market.get("volume_5m_usd", 0),
        "volume_1h_usd":     market.get("volume_1h_usd", 0),
        "volume_6h_usd":     market.get("volume_6h_usd", 0),
        "volume_24h_usd":    market.get("volume_24h_usd", 0),
        "change_5m_pct":     market.get("change_5m_pct", 0),
        "change_1h_pct":     market.get("change_1h_pct", 0),
        "change_6h_pct":     market.get("change_6h_pct", 0),
        "change_24h_pct":    market.get("change_24h_pct", 0),
        "txns_1h_buys":      market.get("txns_1h_buys", 0),
        "txns_1h_sells":     market.get("txns_1h_sells", 0),
        "buy_sell_ratio_1h": market.get("buy_sell_ratio_1h", 1.0),
        "pair_age_hours":    market.get("pair_age_hours", 0),

        # Early metrics
        **em,

        # Social
        "social_score":      social.get("social_score", 25.0),
        "social_tweet_count": social.get("tweet_count", 0),
        "social_source":     social.get("source", "unavailable"),

        # TVL (sarà aggiornato dopo il filtro)
        "tvl_usd":           tvl,
        "tvl_to_mcap_ratio": 0.0,

        # Sicurezza
        "is_honeypot":   security.get("is_honeypot", 0),
        "buy_tax":       security.get("buy_tax", 0),
        "sell_tax":      security.get("sell_tax", 0),
        "is_mintable":   security.get("is_mintable", 0),
        "lp_locked":     security.get("lp_locked", 0),
        "owner_pct":     security.get("owner_pct", 0),

        # CEX
        "cex_score":     cex_info.get("cex_score", 0),
        "cex_is_tier1":  cex_info.get("is_tier1", False),
        "cex_exchanges": ",".join(cex_info.get("exchanges", []))[:80],

        # Holder concentration
        "top10_holder_pct":  holder_info.get("top10_pct", 0.0),
        "is_concentrated":   holder_info.get("is_concentrated", False),

        # Metadati
        "source":      dune_token.get("source", "dune"),
        "profile_ts":  datetime.now().isoformat(),
    }

    # Aggiunge feature derivate
    profile = engineer_features(profile)
    profile["gem_class"] = classify_gem(profile)

    # ── 9. Pre-pump pattern detection (Binance klines + derivati) ─────────
    bn = binance if binance is not None else _binance
    try:
        pp = detect_prepump_patterns(symbol, profile, bn)
        profile.update(pp)
    except Exception as e:
        log.debug(f"[prepump] Errore pattern detection {symbol}: {e}")
        # Defaults già presenti nel profilo (non critici)

    return profile

# ==============================================================================
# SEZIONE 14 – LOGIC LAYER: GEM FILTER
# ==============================================================================


class GemFilter:
    """Applica filtri hard. Ritorna (passed: bool, reason: str)."""

    def check(self, p: dict) -> tuple[bool, str]:
        sym   = p.get("token_symbol", "?")
        chain = p.get("chain", "?")

        # ── Simbolo non-ASCII ─────────────────────────────────────────────
        if sym and not sym.replace(" ", "").replace("_", "").replace("-", "").isascii():
            return False, f"simbolo non-ASCII: {sym}"

        # ── Mock data — mai in produzione ────────────────────────────────
        if p.get("source") == "mock_dune":
            return False, "token da dati mock (Dune offline)"

        # ── Market cap — nessun limite superiore (preferiamo cap più alte) ──
        mcap = p.get("market_cap_usd", 0) or 0
        if mcap <= 0:
            return False, "market_cap non disponibile"
        if mcap < FILTER_CONFIG["MIN_MARKET_CAP_USD"]:
            return False, f"mcap ${mcap:,.0f} < min"

        liq     = p.get("liquidity_usd", 0)
        vol1h   = p.get("volume_1h_usd", 0)
        bsr     = p.get("buy_sell_ratio_1h", 1.0)
        age     = p.get("pair_age_hours", 0)
        change  = p.get("change_1h_pct", 0)
        inflow  = p.get("inflow_usd", 0)
        wallets = p.get("inflow_wallet_count", 0)
        buys    = p.get("txns_1h_buys", 0)
        sells   = p.get("txns_1h_sells", 0)
        bsr_real = (buys + sells) > 0

        # ── BSR globale — nessun acquirente (dump/fake pump) ──────────────
        if bsr_real and bsr < 0.1:
            return False, f"BSR {bsr:.2f} — nessun acquirente (dump/fake pump)"

        # ── Pump.fun / PumpSwap — filtra rug precoce ─────────────────────
        dex_id = p.get("dex_id", "").lower()
        _is_pumpswap = "pump" in dex_id  # pumpswap, pump.fun, ecc.
        if _is_pumpswap:
            if age < FILTER_CONFIG["PUMPSWAP_MIN_AGE_HOURS"] and mcap < FILTER_CONFIG["PUMPSWAP_MIN_MCAP"]:
                return False, (
                    f"pumpswap: età {age:.1f}h < {FILTER_CONFIG['PUMPSWAP_MIN_AGE_HOURS']:.0f}h "
                    f"e mcap ${mcap:,.0f} < ${FILTER_CONFIG['PUMPSWAP_MIN_MCAP']:,.0f} — rug risk"
                )
            if change > FILTER_CONFIG["PUMPSWAP_MAX_CHANGE_1H"]:
                return False, (
                    f"pumpswap: change_1h {change:+.1f}% > {FILTER_CONFIG['PUMPSWAP_MAX_CHANGE_1H']:.0f}% — pump già avvenuto"
                )

        # ── Micro-cap Solana — età minima anche fuori pumpswap ────────────
        if chain == "solana" and mcap < FILTER_CONFIG["SOLANA_MICROCAP_MCAP"]:
            if age < FILTER_CONFIG["SOLANA_MICROCAP_MIN_AGE"]:
                return False, (
                    f"solana micro-cap: età {age:.1f}h < {FILTER_CONFIG['SOLANA_MICROCAP_MIN_AGE']:.0f}h "
                    f"(mcap ${mcap:,.0f})"
                )

        # ── Tier in base al market cap ────────────────────────────────────
        inflow_to_mcap = inflow / mcap if mcap > 0 else 0
        vol_to_liq     = vol1h / liq   if liq  > 0 else 0

        _src              = p.get("source", "")
        _is_binance_src   = (_src == "binance_scan")
        _is_cg_midcap_src = (_src == "coingecko_midcap")

        if mcap > 50_000_000:          # Tier 3 (large-cap > $50M)
            if _is_binance_src:
                # Per large-cap da BinanceScan usiamo un sistema a punteggio
                # multi-segnale anziché la sola soglia inflow/mcap (impossibile
                # raggiungere 2% su token da $1B+).
                # Ogni segnale contribuisce 1 punto; servono almeno 2/5 per passare.
                bn_vol24       = p.get("binance_vol24", 0)
                bn_chg1h       = abs(p.get("binance_chg1h", 0))
                oi_chg         = p.get("oi_change_pct_1h", 0.0)
                funding        = p.get("funding_rate", 0.0)
                ls_ratio       = p.get("ls_ratio", 1.0)
                vol_trend      = p.get("vol_trend_ratio", 1.0)
                vol_days       = p.get("vol_trend_days", 0)
                ob_imbalance   = p.get("ob_imbalance", 0.5)

                lc_score = 0
                lc_signals_hit = []
                if bn_vol24 >= 20_000_000:
                    lc_score += 1; lc_signals_hit.append(f"vol24={bn_vol24/1e6:.0f}M")
                if bn_chg1h >= 2.0:
                    lc_score += 1; lc_signals_hit.append(f"|chg1h|={bn_chg1h:.1f}%")
                if oi_chg >= 2.0:
                    lc_score += 1; lc_signals_hit.append(f"OI+{oi_chg:.1f}%")
                if funding <= 0.0001 and oi_chg > 0:
                    # Funding neutro/negativo con OI crescente = accumulo smart money
                    lc_score += 1; lc_signals_hit.append(f"funding={funding*100:.3f}%+OIup")
                if ls_ratio < 1.0:
                    lc_score += 1; lc_signals_hit.append(f"LS={ls_ratio:.2f}(squeeze)")
                if vol_trend >= 1.5 or vol_days >= 2:
                    lc_score += 1; lc_signals_hit.append(f"vol_trend={vol_trend:.1f}x/{vol_days}d")
                if ob_imbalance >= 0.58:
                    lc_score += 1; lc_signals_hit.append(f"OB_bid={ob_imbalance:.2f}")

                if lc_score < 2:
                    return False, (
                        f"tier3/binance: segnali insufficienti {lc_score}/2 min "
                        f"(vol={bn_vol24/1e6:.0f}M, chg={bn_chg1h:.1f}%, "
                        f"OI={oi_chg:+.1f}%, LS={ls_ratio:.2f})"
                    )
                log.debug(f"[filtro] {sym} tier3/binance: {lc_score} segnali → {lc_signals_hit}")
                # Salva il punteggio nel profilo per lo scoring successivo
                p["lc_signal_score"] = lc_score
                p["lc_signal_hits"]  = " | ".join(lc_signals_hit)
            elif _is_cg_midcap_src:
                cg_vol_ratio = p.get("cg_vol_mcap_ratio", 0)
                cg_chg24     = p.get("cg_price_chg24", 0)
                cg_is_lc     = p.get("cg_is_largecap", False)
                if cg_is_lc:
                    # Large cap: filtri già applicati in get_movers()
                    # (chg24≥15%, vol/mcap≥15%, range_pos≥60%, ath_drop>-40%)
                    rng  = p.get("cg_range_position", 0)
                    ath  = p.get("cg_ath_chg_pct", 0)
                    log.debug(f"[filtro] {sym} tier3/cg-largecap: vol/mcap={cg_vol_ratio:.1%} "
                              f"chg24={cg_chg24:.1f}% range={rng:.0%} ath={ath:+.1f}%")
                else:
                    # Mid cap: verifica attività minima (filtri a monte sono più laschi)
                    if cg_vol_ratio < 0.08 and cg_chg24 < 15.0:
                        return False, (
                            f"tier3/cgmidcap: attività insufficiente "
                            f"(vol/mcap={cg_vol_ratio:.1%}, chg24={cg_chg24:.1f}%)"
                        )
                    log.debug(f"[filtro] {sym} tier3/cgmidcap: vol/mcap={cg_vol_ratio:.1%} chg24={cg_chg24:.1f}%")
            else:
                if inflow_to_mcap < 0.02:
                    return False, f"tier3: inflow/mcap {inflow_to_mcap:.1%} < 2%"
            if bsr_real and bsr < 1.2:
                return False, f"tier3: BSR {bsr:.2f} < 1.2"
            if not _is_cg_midcap_src and age > 240:
                return False, f"tier3: età {age:.0f}h > 240h"
            if vol_to_liq < 0.05:
                return False, f"tier3: vol/liq {vol_to_liq:.1%} < 5%"

        elif mcap > 10_000_000:        # Tier 2 (mid-cap $10M–$50M)
            if _is_binance_src:
                bn_vol24     = p.get("binance_vol24", 0)
                bn_chg1h     = abs(p.get("binance_chg1h", 0))
                oi_chg       = p.get("oi_change_pct_1h", 0.0)
                ls_ratio     = p.get("ls_ratio", 1.0)
                vol_trend    = p.get("vol_trend_ratio", 1.0)

                lc_score = 0
                lc_signals_hit = []
                if bn_vol24 >= 5_000_000:
                    lc_score += 1; lc_signals_hit.append(f"vol24={bn_vol24/1e6:.0f}M")
                if bn_chg1h >= 2.0:
                    lc_score += 1; lc_signals_hit.append(f"|chg1h|={bn_chg1h:.1f}%")
                if oi_chg >= 3.0:
                    lc_score += 1; lc_signals_hit.append(f"OI+{oi_chg:.1f}%")
                if ls_ratio < 1.0:
                    lc_score += 1; lc_signals_hit.append(f"LS={ls_ratio:.2f}")
                if vol_trend >= 1.3:
                    lc_score += 1; lc_signals_hit.append(f"vol_trend={vol_trend:.1f}x")

                if lc_score < 2:
                    return False, (
                        f"tier2/binance: segnali insufficienti {lc_score}/2 min "
                        f"(vol={bn_vol24/1e6:.0f}M, OI={oi_chg:+.1f}%, LS={ls_ratio:.2f})"
                    )
                p["lc_signal_score"] = lc_score
                p["lc_signal_hits"]  = " | ".join(lc_signals_hit)
            elif _is_cg_midcap_src:
                # Filtro applicato a monte in CoinGeckoMidCapFetcher.get_movers()
                # (chg24 >= 8%, vol/mcap >= 10%). Qui logghiamo e passiamo.
                cg_vol_ratio = p.get("cg_vol_mcap_ratio", 0)
                cg_chg24     = p.get("cg_price_chg24", 0)
                log.debug(f"[filtro] {sym} tier2/cgmidcap: vol/mcap={cg_vol_ratio:.1%} chg24={cg_chg24:.1f}%")
            else:
                if inflow_to_mcap < 0.005:
                    return False, f"tier2: inflow/mcap {inflow_to_mcap:.1%} < 0.5%"
                if wallets < 10:
                    return False, f"tier2: solo {wallets} wallet smart-money (min 10 per mid-cap)"
            if bsr_real and bsr < 1.0:
                return False, f"tier2: BSR {bsr:.2f} < 1.0"
            if not _is_cg_midcap_src and age > 240:
                return False, f"tier2: età {age:.0f}h > 240h"

        # ── Liquidità per-chain ───────────────────────────────────────────
        min_liq = CHAINS.get(chain, {}).get("min_liquidity", FILTER_CONFIG["MIN_LIQUIDITY_USD"])
        if liq < min_liq:
            return False, f"liquidity ${liq:,.0f} < min ${min_liq:,.0f} ({chain})"

        # ── Liquidità/mcap anomala (rug setup) ────────────────────────────
        if liq / mcap > 0.80:
            return False, f"liq/mcap {liq/mcap:.0%} > 80% — possibile rug setup"

        # ── Volume minimo ─────────────────────────────────────────────────
        if vol1h < FILTER_CONFIG["MIN_VOLUME_1H_USD"]:
            return False, f"vol1h ${vol1h:,.0f} < min"

        # ── Wash trading ──────────────────────────────────────────────────
        if p.get("wash_trading_flag"):
            return False, "wash trading rilevato (vol/txns anomalo)"

        # ── Social score — DISABILITATO (libreria non funzionante) ───────────
        # Il filtro social è stato rimosso: il provider restituisce dati
        # inaffidabili. Il campo social_score rimane nel profilo per logging
        # ma non influenza il filtraggio.

        # ── Età pair ─────────────────────────────────────────────────────
        if age < FILTER_CONFIG["MIN_PAIR_AGE_HOURS"]:
            return False, f"pair troppo giovane ({age:.1f}h)"
        if mcap <= 10_000_000 and age > FILTER_CONFIG["MAX_PAIR_AGE_HOURS"]:
            return False, f"pair troppo vecchio ({age:.0f}h)"

        # ── Change 1h ─────────────────────────────────────────────────────
        if change > FILTER_CONFIG["MAX_CHANGE_1H_PCT"]:
            return False, f"change_1h {change:+.1f}% > {FILTER_CONFIG['MAX_CHANGE_1H_PCT']:.0f}% (pump già avvenuto)"
        if change < FILTER_CONFIG["MIN_CHANGE_1H_PCT"]:
            return False, f"change_1h {change:+.1f}% < {FILTER_CONFIG['MIN_CHANGE_1H_PCT']:.0f}% (dump massiccio)"

        # ── Anti-dump combinato: calo + sellers dominanti ─────────────────
        # change < -2% AND bsr < 0.5 = distribuzione attiva mentre il prezzo scende
        # Caso reale: VVVeity chg=-3.95% bsr=0.479 → segnalato come GOLD ma era dump
        if change < -2.0 and bsr_real and bsr < 0.50:
            return False, (
                f"anti-dump: change_1h={change:+.1f}% bsr={bsr:.2f} — "
                f"prezzo in calo con venditori dominanti"
            )

        # ── Smart money (Tier 1 — mcap <= 10M) ───────────────────────────
        _no_inflow_src = {"dexscreener_boosted", "dexscreener_trending",
                          "coingecko_trending", "binance_scan", "coingecko_midcap"}
        if p.get("source") not in _no_inflow_src and mcap <= 10_000_000:
            if inflow < FILTER_CONFIG["MIN_INFLOW_USD"]:
                return False, f"inflow ${inflow:,.0f} < min"
            if wallets < FILTER_CONFIG["MIN_SMART_WALLETS"]:
                return False, f"solo {wallets} wallet smart-money"
            if inflow_to_mcap < FILTER_CONFIG["MIN_INFLOW_TO_MCAP_TIER1"]:
                return False, f"tier1: inflow/mcap {inflow_to_mcap:.1%} < 0.5%"

        # ── Sicurezza EVM ─────────────────────────────────────────────────
        if p.get("is_honeypot"):
            return False, "HONEYPOT (GoPlus)"
        if p.get("buy_tax", 0) > FILTER_CONFIG["MAX_BUY_TAX_PCT"]:
            return False, f"buy_tax {p['buy_tax']:.0f}% > max"
        if p.get("sell_tax", 0) > FILTER_CONFIG["MAX_SELL_TAX_PCT"]:
            return False, f"sell_tax {p['sell_tax']:.0f}% > max"

        # ── Holder concentration ──────────────────────────────────────────
        top10 = p.get("top10_holder_pct", 0.0)
        if top10 > FILTER_CONFIG.get("MAX_TOP10_HOLDER_PCT", 60):
            return False, f"top10 holder {top10:.0f}% > max (rug risk)"

        # ── Cooldown + blacklist ──────────────────────────────────────────
        pair_key = p.get("pair_address") or p.get("token_address", "")
        if _gem_blacklisted(pair_key):
            return False, f"{sym} in blacklist (dump massiccio)"
        # CoinGecko Trending: bypass cooldown (validazione esterna = già filtrato dal mercato)
        _src = p.get("source", "")
        if _src != "coingecko_trending" and not _gem_cooldown_ok(pair_key):
            return False, f"{sym} in cooldown ({FILTER_CONFIG['TOKEN_COOLDOWN_MIN']}min)"

        return True, "ok"

# ==============================================================================
# SEZIONE 15 – SCORING LAYER: RULE-BASED SCORER
# ==============================================================================


def score_gem(profile: dict) -> dict:
    """
    Sistema di scoring deterministico a punti ponderati — NO ML.

    Ritorna:
      score   (float 0-100+)
      tier    (str: DIAMOND | GOLD | SILVER | BRONZE | SKIP)
      signals (list[str]: segnali specifici attivati)
      prob    (float 0-1: compatibilità con codice esterno)
    """
    sc   = SCORE_CONFIG
    pts  = 0.0
    sigs = []

    # ── Gate immediati (blocco senza score) ──────────────────────────────
    if profile.get("is_honeypot"):
        return {"score": 0, "tier": "BLOCKED", "signals": ["🚨 HONEYPOT"], "prob": 0}
    if profile.get("source") == "mock_dune":
        return {"score": 0, "tier": "BLOCKED", "signals": ["mock data"], "prob": 0}
    # EARLY_SIGNAL è statisticamente catastrofico: mediana -98.3%, 62% bad rate
    # su 141 token reali. Sono token troppo giovani (<6h) senza consolidamento.
    # Il filtro MIN_PAIR_AGE_HOURS=6h li blocca già a monte, ma aggiungiamo qui
    # come safety net nel caso arrivi un profilo con age incompleto.
    if profile.get("gem_class") == "EARLY_SIGNAL":
        return {"score": 0, "tier": "BLOCKED",
                "signals": ["🚫 EARLY_SIGNAL bloccato (62% bad rate storico)"],
                "prob": 0}

    mcap    = max(profile.get("market_cap_usd", 1), 1)
    inflow  = profile.get("inflow_usd", 0)
    wallets = profile.get("inflow_wallet_count", 0)
    pnl     = profile.get("avg_wallet_pnl_pct", 0)
    bsr     = profile.get("buy_sell_ratio_1h", 1.0)
    ch1h    = profile.get("change_1h_pct", 0)
    vol1h   = profile.get("volume_1h_usd", 0)
    liq     = max(profile.get("liquidity_usd", 1), 1)
    age     = profile.get("pair_age_hours", 999)
    ms      = profile.get("momentum_score", 0)
    ramp    = profile.get("volume_ramp_ratio", 1.0)
    cex_s   = profile.get("cex_score", 0)
    top10   = profile.get("top10_holder_pct", 0.0)
    wash    = profile.get("wash_trading_flag", False)
    gem_cls = profile.get("gem_class", "NEUTRAL")
    inflow_ratio = inflow / mcap

    # ── BLOCCO 1: Smart money (max 35 pt) ────────────────────────────────
    if wallets >= 15:
        pts += sc["SM_WALLETS_HIGH"]
        sigs.append(f"💰 {wallets} smart wallets (alta fiducia)")
    elif wallets >= 8:
        pts += sc["SM_WALLETS_MED"]
        sigs.append(f"💰 {wallets} smart wallets")
    elif wallets >= 5:
        pts += sc["SM_WALLETS_LOW"]

    if inflow_ratio >= 0.10:
        pts += sc["SM_RATIO_HIGH"]
        sigs.append(f"📈 Inflow {inflow_ratio:.0%} del mcap")
    elif inflow_ratio >= 0.03:
        pts += sc["SM_RATIO_MED"]
        sigs.append(f"📈 Inflow {inflow_ratio:.0%} del mcap")
    elif inflow_ratio >= 0.01:
        pts += sc["SM_RATIO_LOW"]

    if pnl >= 100:
        pts += sc["SM_PNL_HIGH"]
        sigs.append(f"🏆 Avg wallet PnL {pnl:.0f}%")
    elif pnl >= 50:
        pts += sc["SM_PNL_MED"]
    elif 20 <= pnl < 50:
        # Sweet spot empirico: 20-50% → mediana +95.6%, 88% >30%, 0% bad (mag 2026)
        # Wallet con PnL troppo alto (>100%) sono spesso insiders/rug — 20-50% è profilo
        # di smart money genuino che ha accumulato in precedenti run, non insider puro.
        pts += sc["SM_PNL_SWEET"]
        sigs.append(f"🎯 Avg wallet PnL {pnl:.0f}% (zona d'oro 20-50%)")

    # ── BLOCCO 2: Momentum mercato (max 35 pt) ────────────────────────────
    if bsr >= 3.0:
        pts += sc["MOM_BSR_HIGH"]
        sigs.append(f"🔥 BSR {bsr:.1f} (dominanza acquirenti)")
    elif bsr >= 2.0:
        pts += sc["MOM_BSR_MED"]
        sigs.append(f"⬆️  BSR {bsr:.1f}")
    elif bsr >= 1.5:
        pts += sc["MOM_BSR_LOW"]

    if 5 <= ch1h <= 20:
        pts += sc["MOM_CH1H_IDEAL"]
        sigs.append(f"📊 +{ch1h:.0f}% in 1h (zona ideale)")
    elif 0 <= ch1h < 5:
        pts += sc["MOM_CH1H_NEUTRAL"]
    elif -15 <= ch1h < 0:
        pts += sc["MOM_CH1H_DIP"]
        sigs.append(f"📉 Dip {ch1h:.0f}% — possibile rimbalzo")

    if ramp >= 3.0:
        pts += sc["MOM_RAMP_HIGH"]
        sigs.append(f"🚀 Volume ×{ramp:.1f} nell'ultima ora")
    elif ramp >= 2.0:
        pts += sc["MOM_RAMP_MED"]
        sigs.append(f"📈 Volume ×{ramp:.1f} nell'ultima ora")

    if vol1h / liq >= 0.50:
        pts += sc["MOM_VOLL"]

    # ── BLOCCO 3: Età / timing (max 15 pt) ───────────────────────────────
    # Nota: MIN_PAIR_AGE_HOURS=6h, quindi age<=6 non raggiungibile normalmente.
    # AGE_VERY_YOUNG mantenuto come fallback per profili con age incerto (es. DexScreener
    # non restituisce pairCreatedAt → age=999, poi filtrato più su).
    if age <= 6:
        pts += sc["AGE_VERY_YOUNG"]
        sigs.append(f"⏱️  Pair freschissima ({age:.1f}h)")
    elif age <= 24:
        pts += sc["AGE_YOUNG"]
        sigs.append(f"⏱️  Pair giovane ({age:.1f}h)")
    elif age <= 72:
        pts += sc["AGE_MEDIUM"]
    elif mcap > 5_000_000 and age <= 240:
        # Mid/large-cap consolidati (3-10 giorni): il pre-pump arriva dopo la fase di accumulo
        pts += sc["AGE_MIDCAP_CONSOL"]
        sigs.append(f"🏗️  Mid-cap consolidato ({age:.0f}h, mcap=${mcap/1e6:.1f}M)")

    # ── BLOCCO 4: CEX bonus (max 15 pt) ──────────────────────────────────
    if profile.get("cex_is_tier1"):
        pts += sc["CEX_TIER1"]
        exch = profile.get("cex_exchanges", "")
        sigs.append(f"⭐ CEX Tier1: {exch}")
    elif cex_s > 20:
        pts += sc["CEX_OTHER"]
        sigs.append(f"📦 CEX listing: {profile.get('cex_exchanges','')}")

    # ── BLOCCO 5: Momentum score da compute_early_metrics (max 5 pt) ──────
    if ms >= 8:
        pts += sc["MS_HIGH"]
        sigs.append(f"⚡ Momentum early {ms}/10")
    elif ms >= 5:
        pts += sc["MS_MED"]

    # ── BLOCCO 6: Pre-pump patterns (max ~45 pt) ─────────────────────────
    pp_count = profile.get("pp_patterns_count", 0)

    if profile.get("pp_vol_explosion"):
        pts += sc["PP_VOL_EXPLOSION"]
        vol_z = profile.get("volume_z_score", 0)
        ramp  = profile.get("volume_ramp_ratio", 1.0)
        sigs.append(f"💥 Esplosione volumi (z={vol_z:.1f}, ramp=×{ramp:.1f})")

    if profile.get("pp_bb_squeeze"):
        pts += sc["PP_BB_SQUEEZE"]
        bw = profile.get("pp_bb_width_pct", 0)
        sigs.append(f"🔧 Bollinger Squeeze (BB width={bw:.1f}%) — atteso breakout")

    if profile.get("pp_ema_breakout"):
        pts += sc["PP_EMA_BREAKOUT"]
        pat  = profile.get("pp_ema_pattern", "")
        slp  = profile.get("pp_ema20_slope", 0)
        sigs.append(f"📐 EMA Breakout [{pat}] slope={slp:+.2f}%")

    if profile.get("pp_rsi_divergence"):
        pts += sc["PP_RSI_DIVERGENCE"]
        rsi = profile.get("pp_rsi", 50)
        sigs.append(f"🔄 RSI Divergenza Rialzista (RSI={rsi:.0f})")

    if profile.get("pp_oi_spike"):
        pts += sc["PP_OI_SPIKE"]
        oi_chg = profile.get("pp_oi_change_pct", 0)
        sigs.append(f"📊 Open Interest +{oi_chg:.0f}% in 1h")

    if profile.get("pp_short_squeeze"):
        pts += sc["PP_FUNDING_SQUEEZE"]
        fr = profile.get("pp_funding_rate", 0)
        sigs.append(f"🎯 Short Squeeze Setup (funding={fr:+.3f}%)")

    if profile.get("pp_whale_accum"):
        pts += sc["PP_WHALE_ACCUM"]
        liq_chg = profile.get("liq_change_30m_pct", 0)
        sigs.append(f"🐋 Accumulazione Balene (liq +{liq_chg:.0f}% in 30m)")

    if profile.get("pp_ob_imbalance"):
        pts += sc["PP_OB_IMBALANCE"]
        ratio = profile.get("pp_ob_bid_ratio", 0.5)
        sigs.append(f"📋 Orderbook Imbalance (bid {ratio:.0%} — pressione buy)")

    if profile.get("pp_deposit_spike"):
        pts += sc["PP_DEPOSIT_SPIKE"]
        cdr = profile.get("pp_cex_dex_ratio", 0)
        sigs.append(f"🏦 Deposit Spike CEX (vol CEX {cdr:.0f}× DEX)")

    if profile.get("pp_oi_funding_div"):
        pts += sc["PP_OI_FUNDING_DIV"]
        dtype = profile.get("pp_oi_div_type", "")
        label = {"short_squeeze_buildup": "Short Squeeze Buildup",
                 "overleveraged_longs":   "Longs Over-Leveraged",
                 "oi_buildup_neutral":    "OI Buildup Neutro"}.get(dtype, dtype)
        sigs.append(f"⚡ OI/Funding Divergence [{label}]")

    # Bonus capitalizzazione: prepump su large cap è più affidabile
    mcap_real = profile.get("market_cap_usd", 0)
    if mcap_real > 50_000_000 and pp_count >= 2:
        pts += sc["PP_LARGE_CAP_BONUS"]
        sigs.append(f"💼 Large Cap PrePump (mcap ${mcap_real/1e6:.0f}M, {pp_count} pattern)")

    # Penalità overheated (funding troppo positivo = surriscaldamento)
    if profile.get("pp_overheated"):
        pts -= 5
        sigs.append("⚠️  Funding rate molto positivo — possibile correzione")

    # ── BLOCCO 7: Feature di accumulo stealth ────────────────────────────
    absorb  = profile.get("sell_absorption_score", 0)
    avg_buy = profile.get("avg_buy_size_usd", 0)
    liq_cv  = profile.get("liq_stability_cv", 1.0)
    vol_cv  = profile.get("vol_persistence_cv", 1.0)
    txns_ac = profile.get("txns_acceleration", 0.0)
    gem_cls = profile.get("gem_class", "NEUTRAL")

    if absorb >= 3.0:
        pts += sc["SELL_ABSORB_HIGH"]
        sigs.append(f"🧲 Forte Sell Absorption ({absorb:.1f}) — accumulo nascosto")
    elif absorb >= 1.5:
        pts += sc["SELL_ABSORB_MED"]
        sigs.append(f"🧲 Sell Absorption ({absorb:.1f})")

    if gem_cls == "STEALTH_ACCUM":
        pts += sc["STEALTH_ACCUM"]
        sigs.append(f"🥷 STEALTH ACCUM — smart money silenzioso, retail assente")

    if avg_buy >= 2_000:
        pts += sc["BUY_SIZE_WHALE"]
        sigs.append(f"🐳 Avg Buy ${avg_buy:,.0f} — dimensione whale")
    elif avg_buy >= 500:
        pts += sc.get("BUY_SIZE_WHALE", 0) // 2  # mezzo punto

    if liq_cv < 0.10 and liq > 20_000:
        pts += sc["LIQ_STABLE"]
        sigs.append(f"💧 Liquidità Stabile (CV={liq_cv:.2f})")

    if vol_cv < 0.40 and vol1h > 10_000:
        pts += sc["VOL_PERSISTENT"]
        sigs.append(f"📈 Volume Persistente (CV={vol_cv:.2f}) — accumulo organico")

    if txns_ac >= 0.5:
        pts += sc["TXNS_ACCEL"]
        sigs.append(f"⚡ Txns Acceleration +{txns_ac:.0%}")

    # Wallet Repeat Buy Score
    wrbs = profile.get("wallet_repeat_buy_score", 0.0)
    if wrbs >= 4.0:
        pts += sc.get("WB_REPEAT_HIGH", 10)
        sigs.append(f"🔄 Wallet Repeat Buyers ({wrbs:.1f}/10) — accumulatori persistenti")
    elif wrbs >= 2.0:
        pts += sc.get("WB_REPEAT_MED", 6)
        sigs.append(f"🔄 Wallet Repeat Buyers ({wrbs:.1f}/10)")

    # Penalità: RSI/BB su microcap (<5M mcap) → troppo rumore
    if mcap < 5_000_000 and (profile.get("pp_rsi_divergence") or
                              profile.get("pp_bb_squeeze")):
        pts += sc["PENALTY_RSI_SMALL"]

    # ── PENALITÀ ─────────────────────────────────────────────────────────
    if gem_cls == "FAKE_PUMP":
        pts += sc["PENALTY_FAKE_PUMP"]
        sigs.append("⚠️  Pattern FAKE_PUMP rilevato")
    if top10 > 40:
        pts += sc["PENALTY_CONCENTRATED"]
        sigs.append(f"⚠️  Top10 holder {top10:.0f}% — rug risk")
    if wash:
        pts += sc["PENALTY_WASH"]
        sigs.append("⚠️  Possibile wash trading")

    pts = max(pts, 0.0)

    # ── Tier ─────────────────────────────────────────────────────────────
    if pts >= sc["DIAMOND_THRESHOLD"]:
        tier = "DIAMOND"
    elif pts >= sc["GOLD_THRESHOLD"]:
        tier = "GOLD"
    elif pts >= sc["SILVER_THRESHOLD"]:
        tier = "SILVER"
    elif pts >= sc["BRONZE_THRESHOLD"]:
        tier = "BRONZE"
    else:
        tier = "SKIP"

    return {
        "score":   round(pts, 1),
        "tier":    tier,
        "signals": sigs,
        "prob":    round(min(pts / 100.0, 1.0), 4),
        "reject":  None if tier != "SKIP" else "score troppo basso",
    }

# ==============================================================================
# SEZIONE 16 – OUTPUT
# ==============================================================================

# Colonne del CSV gemme (ordine stabile)
_GEM_CSV_PATH     = os.path.join(_BASE_DIR, "reports", "gems_log_v3.csv")
_GEM_CSV_COLUMNS  = [
    "gem_id", "timestamp", "token_symbol", "chain", "tier", "score",
    "gem_class", "price_usd", "market_cap_usd", "liquidity_usd",
    "volume_1h_usd", "buy_sell_ratio_1h", "change_1h_pct", "pair_age_hours",
    "inflow_usd", "inflow_wallet_count", "avg_wallet_pnl_pct",
    "volume_ramp_ratio", "momentum_score", "volume_z_score",
    "social_score", "cex_score", "cex_is_tier1", "cex_exchanges",
    "top10_holder_pct", "wash_trading_flag",
    # Pre-pump patterns
    "pp_patterns_count", "pp_vol_explosion", "pp_bb_squeeze",
    "pp_ema_breakout", "pp_ema_pattern", "pp_rsi_divergence", "pp_rsi",
    "pp_oi_spike", "pp_oi_change_pct", "pp_short_squeeze", "pp_funding_rate",
    "pp_whale_accum", "pp_bb_width_pct", "pp_ema20_slope",
    "pp_ob_imbalance", "pp_ob_bid_ratio", "pp_deposit_spike",
    "pp_cex_dex_ratio", "pp_oi_funding_div", "pp_oi_div_type",
    # Feature accumulo
    "sell_absorption_score", "avg_buy_size_usd", "wallet_repeat_buy_score",
    "repeat_buyer_count", "repeat_buyer_ratio", "max_single_trade_usd", "gem_class",
    "liq_stability_cv", "vol_persistence_cv", "txns_acceleration",
    # v4: momentum freshness
    "inflow_last_2h", "inflow_recency_ratio", "buyers_last_2h", "inflow_recency_score",
    "token_address", "pair_address", "dex_id", "source",
    "signals",
]

_csv_lock = threading.Lock()


def save_gem_to_csv(gem: dict) -> None:
    """
    Salva una gemma trovata in reports/gems_log.csv.
    Crea il file con header se non esiste.
    Thread-safe.
    """
    os.makedirs(os.path.join(_BASE_DIR, "reports"), exist_ok=True)
    now_iso = datetime.now().isoformat(timespec="seconds")
    # ID univoco: simbolo + timestamp compresso
    gem_id  = f"{gem.get('token_symbol','X')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    row = {col: gem.get(col, "") for col in _GEM_CSV_COLUMNS}
    row["gem_id"]    = gem_id
    row["timestamp"] = now_iso
    # Signals è una lista → stringa separata da |
    sigs = gem.get("signals", [])
    row["signals"] = " | ".join(sigs) if isinstance(sigs, list) else str(sigs)
    # Bool → 1/0
    row["wash_trading_flag"] = int(bool(gem.get("wash_trading_flag", False)))
    row["cex_is_tier1"]      = int(bool(gem.get("cex_is_tier1", False)))
    for _pp_b in ("pp_vol_explosion","pp_bb_squeeze","pp_ema_breakout",
                  "pp_rsi_divergence","pp_oi_spike","pp_short_squeeze","pp_whale_accum",
                  "pp_ob_imbalance","pp_deposit_spike","pp_oi_funding_div"):
        row[_pp_b] = int(bool(gem.get(_pp_b, False)))

    # Verifica header: se il file esiste con colonne diverse → archivia
    with _csv_lock:
        write_header = not os.path.exists(_GEM_CSV_PATH)
        if not write_header:
            try:
                with open(_GEM_CSV_PATH, "r", encoding="utf-8") as fcheck:
                    first_line = fcheck.readline().strip()
                existing_cols = [c.strip() for c in first_line.split(",")]
                if existing_cols != _GEM_CSV_COLUMNS:
                    # Formato diverso → archivia il vecchio file
                    import shutil
                    archive = _GEM_CSV_PATH.replace(".csv", f"_old_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
                    shutil.move(_GEM_CSV_PATH, archive)
                    log.info(f"[csv] Header diverso — archiviato in {archive}")
                    write_header = True
            except Exception:
                write_header = True

        # ── Dedup persistente: salta se stesso sym|chain salvato nelle ultime 8 ore ──
        _CSV_DEDUP_HOURS = 8
        _sym   = gem.get("token_symbol", "").upper()
        _chain = gem.get("chain", "").lower()
        _skip_dup = False
        _existing_gem_id = None   # gem_id originale del duplicato trovato nel CSV
        if not write_header and _sym and _chain:
            try:
                _cutoff = datetime.now() - timedelta(hours=_CSV_DEDUP_HOURS)
                with open(_GEM_CSV_PATH, "r", encoding="utf-8") as _fdup:
                    _reader = csv.DictReader(_fdup)
                    for _r in _reader:
                        if (_r.get("token_symbol","").upper() == _sym
                                and _r.get("chain","").lower() == _chain):
                            _ts_str = _r.get("timestamp","")
                            try:
                                _ts = datetime.fromisoformat(_ts_str)
                                if _ts >= _cutoff:
                                    _skip_dup = True
                                    _existing_gem_id = _r.get("gem_id", "")
                                    break
                            except ValueError:
                                pass
            except Exception as _e:
                log.debug(f"[csv] dedup check error: {_e}")

        if _skip_dup:
            log.debug(f"[csv] ⏭️  {_sym} ({_chain}) già nel CSV nelle ultime {_CSV_DEDUP_HOURS}h — skip duplicato")
            # FIX: usa il gem_id ORIGINALE del CSV, non uno nuovo — altrimenti gli snapshot
            # vengono scritti con un ID diverso e il report mostra ⏳ invece dei dati reali.
            gem["gem_id"]                   = _existing_gem_id or gem_id
            gem["_tracker_csv_written"]     = True
            gem["_tracker_already_tracked"] = True   # segnale a stampa_gemma: non ri-registrare
            return

        with open(_GEM_CSV_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_GEM_CSV_COLUMNS,
                                    extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow(row)

    # Marca la gemma: CSV già scritto da gemmeV3, gem_tracker non deve riscriverlo
    gem["gem_id"]              = gem_id
    gem["_tracker_csv_written"] = True

    log.debug(f"[csv] ✅ {gem.get('token_symbol','?')} salvata in {_GEM_CSV_PATH}")


def stampa_gemma(gem: dict) -> None:
    """Logga il segnale gemma e lo registra nel GemTracker."""
    if not rugcheck_safe(gem.get("token_address", ""), "gemme",
                         chain=gem.get("chain", "solana")):
        return

    sym   = gem.get("token_symbol", "?")
    chain = gem.get("chain", "?").upper()
    addr  = gem.get("token_address", "")
    tier  = gem.get("tier", "?")
    score = gem.get("score", 0)
    sigs  = gem.get("signals", [])
    cls   = gem.get("gem_class", "NEUTRAL")

    tier_emoji = {"DIAMOND": "💎", "GOLD": "🥇", "SILVER": "🥈", "BRONZE": "🥉"}.get(tier, "📊")

    bar = "═" * 68
    log.info(f"\n{bar}")
    log.info(f"  {tier_emoji} GEM {tier}: {sym} [{chain}]  — Score: {score:.0f}/100")
    log.info(f"  Classe: {cls}  |  Indirizzo: {addr}")
    log.info(f"  Prezzo:  ${gem.get('price_usd', 0):.8f}")
    log.info(f"  MCap:    ${gem.get('market_cap_usd', 0):>12,.0f}  |  "
             f"Liq: ${gem.get('liquidity_usd', 0):>10,.0f}")
    log.info(f"  Vol 1h:  ${gem.get('volume_1h_usd', 0):>10,.0f}  |  "
             f"BSR: {gem.get('buy_sell_ratio_1h', 0):.2f}  |  "
             f"Δ1h: {gem.get('change_1h_pct', 0):+.1f}%  |  "
             f"Età: {gem.get('pair_age_hours', 0):.1f}h")
    log.info(f"  🐋 Smart Money: ${gem.get('inflow_usd', 0):,.0f} "
             f"da {gem.get('inflow_wallet_count', 0)} wallet  |  "
             f"Avg PnL: {gem.get('avg_wallet_pnl_pct', 0):+.0f}%")
    log.info(f"  Vol ramp: ×{gem.get('volume_ramp_ratio', 1):.1f}  |  "
             f"Momentum: {gem.get('momentum_score', 0)}/10  |  "
             f"Vol Z-score: {gem.get('volume_z_score', 0):.1f}")
    if gem.get("cex_is_tier1"):
        log.info(f"  ⭐ CEX: {gem.get('cex_exchanges', '')}")
    if gem.get("top10_holder_pct", 0) > 0:
        log.info(f"  Holder top10: {gem.get('top10_holder_pct', 0):.0f}%")
    if sigs:
        log.info(f"  Segnali: {' | '.join(sigs)}")
    log.info(bar)

    # ── Salvataggio CSV standalone ────────────────────────────────────────
    try:
        save_gem_to_csv(gem)
    except Exception as e:
        log.warning(f"[csv] Errore salvataggio: {e}")

    # GemTracker (opzionale, aggiunge HTML report e price followup)
    # Se la gemma è un duplicato CSV (_tracker_already_tracked) non ri-registriamo:
    # il tracker la sta già seguendo con il gem_id originale e ri-registrare
    # azzererebbe snapshots_done causando snapshot doppi e ⏳ nel report.
    if GEM_TRACKER_AVAILABLE and not gem.get("_tracker_already_tracked"):
        try:
            get_gem_tracker().registra_gemma(gem)
        except Exception as e:
            log.warning(f"[tracker] {e}")

    # Cooldown
    pair_key = gem.get("pair_address") or gem.get("token_address", "")
    if pair_key:
        _set_gem_cooldown(pair_key)

    # Email
    try:
        send_gem_email(gem)
    except Exception as e:
        log.warning(f"[email] Errore invio: {e}")

    # Bridge → defi_optimized
    # Mappa tier gemmeV3 → gem_probability per attivare il bonus in defi_optimized
    if GEM_WATCHLIST_AVAILABLE:
        try:
            _tier_prob = {"DIAMOND": 0.90, "GOLD": 0.72, "SILVER": 0.55, "BRONZE": 0.40}
            gem_wl = dict(gem)
            gem_wl["gem_probability"] = _tier_prob.get(gem.get("gem_class", ""), 0.40)
            write_gem_to_watchlist(gem_wl)
        except Exception as e:
            log.warning(f"[watchlist] {e}")


# ── Email dedup persistente ──────────────────────────────────────────────────
_EMAIL_SENT_PATH  = os.path.join(_BASE_DIR, "reports", "email_sent.json")
_EMAIL_COOLDOWN_H = 8           # allineato a TOKEN_COOLDOWN_H (8h) in trade_simulator — era 12h
_email_sent_cache: dict = {}    # {sym|chain: iso_timestamp}

def _load_email_sent():
    global _email_sent_cache
    try:
        p = Path(_EMAIL_SENT_PATH)
        if p.exists():
            _email_sent_cache = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        _email_sent_cache = {}

def _was_email_sent_recently(sym: str, chain: str) -> bool:
    """True se abbiamo già inviato mail per sym|chain nelle ultime _EMAIL_COOLDOWN_H ore."""
    key = f"{sym.upper()}|{chain.upper()}"
    ts_str = _email_sent_cache.get(key)
    if not ts_str:
        return False
    try:
        sent_at = datetime.fromisoformat(ts_str)
        return (datetime.now() - sent_at).total_seconds() < _EMAIL_COOLDOWN_H * 3600
    except Exception:
        return False

def _mark_email_sent(sym: str, chain: str):
    """Segna sym|chain come inviato ora e salva su disco."""
    key = f"{sym.upper()}|{chain.upper()}"
    _email_sent_cache[key] = datetime.now().isoformat()
    try:
        Path(_EMAIL_SENT_PATH).write_text(
            json.dumps(_email_sent_cache, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.warning(f"[email] Impossibile salvare email_sent.json: {e}")

def send_gem_email(gem: dict) -> bool:
    """Invia email per una gemma trovata (solo da MIN_SCORE in su)."""
    cfg   = EMAIL_CONFIG
    if not cfg["ENABLED"] or not cfg["SMTP_USER"] or not cfg["SMTP_PASSWORD"]:
        return False

    score = gem.get("score", 0)
    if score < cfg["MIN_SCORE"]:
        log.debug(f"[email] Score {score:.0f} < soglia {cfg['MIN_SCORE']:.0f} — skip")
        return False

    sym   = gem.get("token_symbol", "?")
    chain = gem.get("chain", "?").upper()

    # Dedup persistente: non rimandare per lo stesso token entro 12h
    if _was_email_sent_recently(sym, chain):
        log.info(f"[email] ⏭️  {sym} ({chain}) già notificato nelle ultime {_EMAIL_COOLDOWN_H}h — skip")
        return False
    addr  = gem.get("token_address", "")
    pair  = gem.get("pair_address", "")
    tier  = gem.get("tier", "SILVER")
    sigs  = gem.get("signals", [])
    price = gem.get("price_usd", 0)
    mcap  = gem.get("market_cap_usd", 0)
    liq   = gem.get("liquidity_usd", 0)
    vol1h = gem.get("volume_1h_usd", 0)
    infl  = gem.get("inflow_usd", 0)
    walls = gem.get("inflow_wallet_count", 0)
    pnlw  = gem.get("avg_wallet_pnl_pct", 0)
    bsr   = gem.get("buy_sell_ratio_1h", 0)
    ch1h  = gem.get("change_1h_pct", 0)
    age   = gem.get("pair_age_hours", 0)
    ramp  = gem.get("volume_ramp_ratio", 1)
    cex_e = gem.get("cex_exchanges", "")
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Usa sempre il token_address per DexScreener: porta direttamente alla pagina
    # del token con tutte le sue pool. Il pair_address è l'indirizzo della pool
    # specifica (es. Raydium/Uniswap) e può non essere indicizzato correttamente.
    dex_url = f"https://dexscreener.com/{chain.lower()}/{addr or pair}"

    # Pre-pump pattern summary per email
    pp_count = gem.get("pp_patterns_count", 0)
    pp_flags = {
        "💥 Esplosione Volumi":         gem.get("pp_vol_explosion"),
        "🔧 Bollinger Squeeze":         gem.get("pp_bb_squeeze"),
        "📐 EMA Breakout":              gem.get("pp_ema_breakout"),
        "🔄 RSI Divergenza Rialzista":  gem.get("pp_rsi_divergence"),
        "📊 OI Spike":                  gem.get("pp_oi_spike"),
        "🎯 Short Squeeze Setup":       gem.get("pp_short_squeeze"),
        "🐋 Whale Accumulation":        gem.get("pp_whale_accum"),
        "📋 Orderbook Imbalance":       gem.get("pp_ob_imbalance"),
        "🏦 Deposit Spike CEX":         gem.get("pp_deposit_spike"),
        "⚡ OI/Funding Divergence":     gem.get("pp_oi_funding_div"),
    }
    active_pp = [name for name, active in pp_flags.items() if active]

    # Costruisci pp_section HTML prima dell'f-string
    if active_pp:
        pp_items = "".join(
            f"<li style='margin:3px 0;color:#e2e8f0;font-size:13px'>{p}</li>"
            for p in active_pp
        )
        pp_section = f"""
  <div class="sigs"><b style="color:#f1f5f9">⚡ Pre-Pump Patterns ({pp_count}/10):</b>
    <ul style="color:#e2e8f0">{pp_items}</ul></div>"""
    else:
        pp_section = ""

    tier_color = {"DIAMOND": "#2980B9", "GOLD": "#F39C12",
                  "SILVER": "#7F8C8D", "BRONZE": "#A04000"}.get(tier, "#566573")
    tier_emoji = {"DIAMOND": "💎", "GOLD": "🥇", "SILVER": "🥈", "BRONZE": "🥉"}.get(tier, "📊")
    # Colore inline esplicito su ogni <li> — Gmail sovrascrive la color ereditata dal body
    sigs_html  = "".join(
        f"<li style='margin:4px 0;color:#e2e8f0;font-size:13px'>{s}</li>" for s in sigs
    )

    html = f"""
<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
  body{{font-family:Arial,sans-serif;background:#020617;color:#e2e8f0;margin:0;padding:20px}}
  .card{{background:#0f172a;border:1px solid #334155;border-radius:12px;
         padding:24px;max-width:600px;margin:0 auto}}
  .hdr{{background:{tier_color};color:#fff;padding:16px;border-radius:8px 8px 0 0;margin:-24px -24px 20px}}
  .h1{{font-size:22px;font-weight:800;margin:0;color:#fff}}
  .h2{{font-size:13px;color:rgba(255,255,255,0.85);margin:4px 0 0}}
  .grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:16px 0}}
  .m{{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:10px 14px}}
  .ml{{color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}}
  .mv{{color:#f1f5f9;font-weight:700;font-size:15px;font-family:monospace}}
  .sigs{{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:12px 16px;margin:12px 0;color:#e2e8f0}}
  .sigs b{{color:#f1f5f9}}
  .sigs ul{{margin:6px 0;padding-left:20px;color:#e2e8f0}}
  .addr-box{{background:#1e293b;border:1px solid #7c3aed;border-radius:8px;padding:12px 16px;margin:10px 0}}
  .cta{{display:inline-block;background:#7c3aed;color:#fff!important;padding:12px 24px;
         border-radius:8px;text-decoration:none;font-weight:700;font-size:15px;margin:12px 0}}
  .warn{{color:#fcd34d;background:#1c1a00;border:1px solid #6e5908;
          border-radius:8px;padding:10px 14px;font-size:12px;margin-top:20px}}
  .pos{{color:#4ade80}}.neg{{color:#f87171}}
</style></head><body>
<div class="card">
  <div class="hdr">
    <div class="h1">{tier_emoji} GEM {tier}: {sym} ({chain}) — Score {score:.0f}/100</div>
    <div class="h2">{now_s}</div>
  </div>

  <div class="grid">
    <div class="m"><div class="ml">💲 Prezzo</div>
      <div class="mv" style="color:#f1f5f9">${price:.8f}</div></div>
    <div class="m"><div class="ml">📊 Market Cap</div>
      <div class="mv" style="color:#f1f5f9">${mcap:,.0f}</div></div>
    <div class="m"><div class="ml">💧 Liquidità</div>
      <div class="mv" style="color:#f1f5f9">${liq:,.0f}</div></div>
    <div class="m"><div class="ml">📈 Volume 1h</div>
      <div class="mv" style="color:#f1f5f9">${vol1h:,.0f}</div></div>
    <div class="m"><div class="ml">↕️ BSR / Δ1h</div>
      <div class="mv" style="color:#f1f5f9">{bsr:.2f}
        <span style="color:{'#4ade80' if ch1h>=0 else '#f87171'}">
          {'▲' if ch1h>=0 else '▼'}{abs(ch1h):.1f}%</span></div></div>
    <div class="m"><div class="ml">⏱️ Età pair</div>
      <div class="mv" style="color:#f1f5f9">{age:.1f}h</div></div>
    <div class="m"><div class="ml">🐋 Smart Money</div>
      <div class="mv" style="color:#f1f5f9">${infl:,.0f}</div>
      <div style="font-size:11px;color:#94a3b8">{walls} wallet | PnL {pnlw:+.0f}%</div></div>
    <div class="m"><div class="ml">🚀 Vol Ramp</div>
      <div class="mv" style="color:#f1f5f9">×{ramp:.1f}</div></div>
    {f'<div class="m"><div class="ml">⭐ CEX</div><div class="mv" style="color:#f1f5f9;font-size:12px">{cex_e}</div></div>' if cex_e else ''}
  </div>

  <!-- Token address — sempre visibile, sfondo viola scuro -->
  <div class="addr-box">
    <div style="color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">
      🔑 Token Address
    </div>
    <div style="color:#c4b5fd;font-weight:700;font-size:13px;word-break:break-all;font-family:monospace">
      {addr if addr else "—"}
    </div>
    {f'<div style="color:#64748b;font-size:11px;margin-top:6px;word-break:break-all">Pair: {pair}</div>' if pair and pair != addr else ''}
  </div>

  <div class="sigs"><b style="color:#f1f5f9">📡 Segnali attivati:</b>
    <ul>{sigs_html}</ul></div>

  {pp_section}

  <div style="text-align:center;margin:16px 0">
    <a class="cta" href="{dex_url}" target="_blank">🔍 Apri su DexScreener →</a>
  </div>

  <div class="warn">⚠️ <b style="color:#fcd34d">AVVISO:</b>
    <span style="color:#fcd34d">Solo a scopo educativo.
    NON costituisce consiglio finanziario. Rischio elevato.</span></div>
</div></body></html>
"""
    plain = (f"{tier_emoji} GEM {tier}: {sym} [{chain}] — Score {score:.0f}/100\n"
             f"{now_s}\n\n"
             f"Prezzo: ${price:.8f} | MCap: ${mcap:,.0f}\n"
             f"Liq: ${liq:,.0f} | Vol1h: ${vol1h:,.0f} | BSR: {bsr:.2f}\n"
             f"Smart Money: ${infl:,.0f} da {walls} wallet | PnL {pnlw:+.0f}% | Età: {age:.1f}h\n"
             f"Vol Ramp: ×{ramp:.1f}\n\n"
             f"🔑 Token Address: {addr if addr else '(non disponibile)'}\n"
             f"{('Pair: ' + pair) if pair and pair != addr else ''}\n\n"
             f"Segnali: {' | '.join(sigs)}\n\n"
             f"DexScreener: {dex_url}\n\n"
             "⚠️ Solo a scopo educativo. NON è un consiglio finanziario.")
    try:
        msg           = MIMEMultipart("alternative")
        addr_short = (addr[:12] + "…") if addr and len(addr) > 12 else (addr or "no-addr")
        msg["Subject"] = f"{tier_emoji} GEM {tier}: {sym} [{chain}] Score {score:.0f} | {addr_short}"
        msg["From"]    = cfg["FROM_ADDR"] or cfg["SMTP_USER"]
        msg["To"]      = cfg["TO_ADDR"]
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html,  "html",  "utf-8"))
        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15) as srv:
            srv.ehlo(); srv.starttls()
            srv.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            srv.sendmail(msg["From"], [cfg["TO_ADDR"]], msg.as_string())
        _mark_email_sent(sym, chain)
        log.info(f"[email] ✅ Inviata per {sym} ({tier}) a {cfg['TO_ADDR']}")
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("[email] ❌ Autenticazione SMTP fallita.")
    except Exception as e:
        log.error(f"[email] ❌ {e}")
    return False

def send_binance_alert_email(tokens: list[dict]) -> bool:
    """
    Invia un'unica mail di alert per i token Binance Futures con volume/variazione
    anomali ma senza pair DEX trovato su DexScreener.
    """
    cfg = EMAIL_CONFIG
    if not cfg["ENABLED"] or not cfg["SMTP_USER"] or not cfg["SMTP_PASSWORD"]:
        return False
    if not tokens:
        return False

    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rows_html = ""
    rows_plain = ""
    for t in tokens:
        sym   = t.get("token_symbol", "?")
        vol   = t.get("binance_vol24", 0)
        chg   = t.get("binance_chg1h", 0)
        price = t.get("binance_price", 0)
        color = "#4ade80" if chg >= 0 else "#f87171"
        arrow = "▲" if chg >= 0 else "▼"
        rows_html += (
            f"<tr>"
            f"<td style='padding:8px 12px;color:#f1f5f9;font-weight:700'>{sym}</td>"
            f"<td style='padding:8px 12px;color:#94a3b8;font-family:monospace'>${price:.6f}</td>"
            f"<td style='padding:8px 12px;color:#94a3b8;font-family:monospace'>${vol:,.0f}</td>"
            f"<td style='padding:8px 12px;color:{color};font-weight:700'>{arrow}{abs(chg):.1f}%</td>"
            f"<td style='padding:8px 12px'>"
            f"<a href='https://www.binance.com/en/futures/{sym}USDT' "
            f"style='color:#7c3aed'>Binance</a></td>"
            f"</tr>"
        )
        rows_plain += f"  {sym:12} | ${price:.6f} | Vol ${vol:,.0f} | {arrow}{abs(chg):.1f}%\n"

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
  body{{font-family:Arial,sans-serif;background:#020617;color:#e2e8f0;margin:0;padding:20px}}
  .card{{background:#0f172a;border:1px solid #334155;border-radius:12px;padding:24px;max-width:620px;margin:0 auto}}
  .hdr{{background:#0f4c81;color:#fff;padding:16px;border-radius:8px 8px 0 0;margin:-24px -24px 20px}}
  table{{width:100%;border-collapse:collapse}}
  th{{background:#1e293b;color:#94a3b8;font-size:11px;text-transform:uppercase;
      letter-spacing:.5px;padding:8px 12px;text-align:left}}
  tr:nth-child(even){{background:#111827}}
  .warn{{color:#fcd34d;background:#1c1a00;border:1px solid #6e5908;
         border-radius:8px;padding:10px 14px;font-size:12px;margin-top:20px}}
</style></head><body>
<div class="card">
  <div class="hdr">
    <div style="font-size:20px;font-weight:800">📡 ALERT BINANCE FUTURES — No DEX Pair</div>
    <div style="font-size:13px;color:rgba(255,255,255,0.8)">{now_s} | {len(tokens)} token</div>
  </div>
  <p style="color:#94a3b8;font-size:13px">
    Questi token mostrano volume/variazione anomali su Binance Futures
    ma non sono stati trovati su DexScreener. Potrebbero essere su exchange
    centralizzato o token non ancora listati sui DEX principali.
  </p>
  <table>
    <tr>
      <th>Symbol</th><th>Prezzo</th><th>Vol 24h</th><th>Δ 1h</th><th>Link</th>
    </tr>
    {rows_html}
  </table>
  <div class="warn">⚠️ Nessun indirizzo on-chain disponibile. Solo a scopo informativo.</div>
</div></body></html>"""

    plain = (f"📡 ALERT BINANCE FUTURES — No DEX Pair\n{now_s}\n\n"
             f"{'Symbol':<12} | {'Prezzo':<12} | Vol 24h       | Δ1h\n"
             f"{'-'*60}\n"
             f"{rows_plain}\n"
             "Nessun indirizzo on-chain trovato su DexScreener.")

    try:
        msg            = MIMEMultipart("alternative")
        syms           = ", ".join(t.get("token_symbol","?") for t in tokens[:4])
        msg["Subject"] = f"📡 Binance Alert: {syms} — nessun pair DEX"
        msg["From"]    = cfg["FROM_ADDR"] or cfg["SMTP_USER"]
        msg["To"]      = cfg["TO_ADDR"]
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html,  "html",  "utf-8"))
        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15) as srv:
            srv.ehlo(); srv.starttls()
            srv.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            srv.sendmail(msg["From"], [cfg["TO_ADDR"]], msg.as_string())
        log.info(f"[email] 📡 Alert Binance inviato per {len(tokens)} token senza DEX pair")
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("[email] ❌ Autenticazione SMTP fallita (binance alert).")
    except Exception as e:
        log.error(f"[email] ❌ {e}")
    return False


# ==============================================================================
# SEZIONE 17 – MAIN LOOP
# ==============================================================================


# ── V3 open position momentum monitor ───────────────────────────────────────
_V3_LIVE_STATE  = Path(__file__).parent.parent / "defi" / "reports" / "live_state.json"
_V3_EXIT_CSV    = Path(__file__).parent.parent / "defi" / "reports" / "v3_exit_signals.csv"
_V3_MON_SYSTEMS = {"v3", "v3_large", "v3_midcap"}
_V3_WARN_COUNTS: dict = {}   # signal_id → n. cicli consecutivi di deterioramento
_V3_EXIT_FIELDS = ["signal_id","token_symbol","system","chain","reason",
                   "bsr","vol_ratio","liq_ratio","ts","severity"]


def _v3_dex_quick(pair_address: str, chain: str, token_address: str = "") -> Optional[dict]:
    """Fetch leggero DexScreener per un pair aperto."""
    import requests as _req
    try:
        if chain == "solana" and len(pair_address) == 44:
            r = _req.get(f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}",
                         timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok:
                pairs = r.json().get("pairs", [])
                return pairs[0] if pairs else None
        elif token_address:
            r = _req.get(f"https://api.dexscreener.com/tokens/v1/{chain}/{token_address}",
                         timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok:
                data = r.json()
                pairs = data if isinstance(data, list) else data.get("pairs", [])
                valid = [p for p in (pairs or []) if float((p.get("liquidity") or {}).get("usd", 0) or 0) > 0]
                return max(valid, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0)) if valid else None
    except Exception:
        pass
    return None


def _monitor_open_v3_positions():
    """
    Ri-controlla momentum delle posizioni v3/v3_large aperte ogni ciclo (~3min).

    Criteri di deterioramento:
      - BSR < 0.40 (buying pressure esaurita)
      - Volume < 15% di entry_vol (token morente)
      - Liquidity < 50% di entry_liq (pool in svuotamento)

    Severità:
      - "warn"  = 1 ciclo di deterioramento  → BSR_CONFIRM ridotto a 3
      - "exit"  = 2+ cicli consecutivi, o BSR < 0.20 immediato → BSR_CONFIRM = 2
    """
    if not _V3_LIVE_STATE.exists():
        return

    with open(_V3_LIVE_STATE, encoding="utf-8") as f:
        live_state = json.load(f)

    open_v3 = {
        sid: pos for sid, pos in live_state.items()
        if isinstance(pos, dict)
        and pos.get("system", "") in _V3_MON_SYSTEMS
        and float(pos.get("remaining", 0) or 0) > 0
        and pos.get("pair_address")
    }
    if not open_v3:
        return

    # Legge flag già scritti (per non sovrascrivere "exit" con "warn")
    existing: dict = {}   # signal_id → row
    if _V3_EXIT_CSV.exists():
        try:
            with open(_V3_EXIT_CSV, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    existing[row["signal_id"]] = row
        except Exception:
            pass

    updated = False
    now_str = datetime.now().isoformat()

    for sid, pos in open_v3.items():
        # Non retrocedere una flag già "exit"
        if existing.get(sid, {}).get("severity") == "exit":
            continue

        pair   = _v3_dex_quick(pos["pair_address"], pos.get("chain", "solana"),
                                pos.get("token_address", ""))
        if not pair:
            continue

        txns   = pair.get("txns", {}).get("h1", {})
        buys   = int(txns.get("buys", 0) or 0)
        sells  = int(txns.get("sells", 0) or 0)
        bsr    = buys / (buys + sells) if (buys + sells) >= 5 else 1.0
        cur_vol = float((pair.get("volume") or {}).get("h1", 0) or 0)
        cur_liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0)

        ev = float(pos.get("entry_vol", 0) or 0)
        el = float(pos.get("entry_liq", 0) or 0)
        vol_ratio = cur_vol / ev  if ev > 0 else 1.0
        liq_ratio = cur_liq / el if el > 0 else 1.0

        # Età posizione in ore
        _pos_age_h = 0.0
        try:
            _pos_age_h = (datetime.now() - datetime.fromisoformat(
                pos.get("position_open_ts") or pos.get("entry_ts", "")
            )).total_seconds() / 3600
        except Exception:
            pass

        cur_chg = float(pos.get("current_pct", 0) or 0)
        tp1_hit = bool(pos.get("tp1_hit", False))

        reasons = []
        if bsr < 0.40:
            reasons.append(f"bsr={bsr:.2f}<0.40")
        if ev > 0 and vol_ratio < 0.15:
            reasons.append(f"vol={vol_ratio*100:.0f}%entry")
        if el > 0 and liq_ratio < 0.50:
            reasons.append(f"liq={liq_ratio*100:.0f}%entry")
        # Profitable stall: posizione in profitto > 5% da > 18h, TP1 non hit, BSR stagnante
        if (not tp1_hit and cur_chg > 5.0 and _pos_age_h > 18
                and bsr < 0.55 and vol_ratio < 0.40):
            reasons.append(f"stall: +{cur_chg:.1f}% dopo {_pos_age_h:.0f}h, bsr={bsr:.2f}")

        sym  = pos.get("token_symbol", "?")
        sys_ = pos.get("system", "v3")

        if not reasons:
            if sid in _V3_WARN_COUNTS:
                del _V3_WARN_COUNTS[sid]   # reset se momentum recuperato
                existing.pop(sid, None)
                updated = True
                log.info(f"[v3_monitor] ✅ {sym} ({sys_}): momentum recuperato — flag rimosso")
            continue

        _V3_WARN_COUNTS[sid] = _V3_WARN_COUNTS.get(sid, 0) + 1
        count    = _V3_WARN_COUNTS[sid]
        severity = "exit" if (count >= 2 or bsr < 0.20) else "warn"
        reason   = " | ".join(reasons)

        log.info(f"[v3_monitor] ⚠ {sym} ({sys_}/{pos.get('chain','?')}): "
                 f"{reason} → {severity.upper()} (ciclo {count})")

        existing[sid] = {
            "signal_id":    sid,
            "token_symbol": sym,
            "system":       sys_,
            "chain":        pos.get("chain", "solana"),
            "reason":       reason,
            "bsr":          f"{bsr:.3f}",
            "vol_ratio":    f"{vol_ratio:.3f}",
            "liq_ratio":    f"{liq_ratio:.3f}",
            "ts":           now_str,
            "severity":     severity,
        }
        updated = True

    if updated:
        _V3_EXIT_CSV.parent.mkdir(parents=True, exist_ok=True)
        with open(_V3_EXIT_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_V3_EXIT_FIELDS)
            w.writeheader()
            w.writerows(existing.values())


def main_loop() -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    dune       = DuneDataFetcher()
    social     = SocialAnalyzer()
    defillama  = DefiLlamaFetcher()
    gem_filter = GemFilter()
    cg_trend   = CoinGeckoTrendingFetcher()
    cg_midcap  = CoinGeckoMidCapFetcher()
    bn_scan    = BinanceScanFetcher()

    if GEM_TRACKER_AVAILABLE:
        try:
            get_gem_tracker().genera_report_html()
        except Exception:
            pass

    ciclo      = 0
    seen_pairs: dict[str, datetime] = {}   # dedup cross-ciclo (TTL 24h)

    _load_email_sent()
    log.info("[loop] 🚀 gemmeV3 — Rule-Based Gem Hunter. Ctrl+C per fermare.")
    check_followup_blacklist()

    try:
        while True:
            ciclo += 1
            now = datetime.now()
            log.info(f"\n{'─'*68}")
            log.info(f"[loop] Ciclo #{ciclo} — {now.strftime('%Y-%m-%d %H:%M:%S')}")

            # Manutenzione periodica
            if ciclo % 10 == 0:
                check_followup_blacklist()
            if ciclo % 6 == 0:
                save_persistent_state()

            # Scade seen_pairs più vecchi di 24h
            seen_pairs = {k: v for k, v in seen_pairs.items()
                          if now - v < timedelta(hours=24)}

            gems_this_round: list = []

            for chain in CHAINS:
                log.info(f"[loop] ── {chain.upper()} ──")
                all_profiles = []  # reset per ogni chain — evita duplicati cross-chain

                # ── Step 1: Dune tokens ─────────────────────────────────
                dune_tokens = dune.get_smart_money_tokens(chain)

                # Piano B: DexScreener Boosted/Trending se Dune è scarso
                _no_inflow = {"mock_dune", "dexscreener_boosted", "dexscreener_trending",
                              "coingecko_trending", "binance_scan", "coingecko_midcap"}
                n_real = len([t for t in dune_tokens if t.get("source") not in _no_inflow])
                if n_real < 3:
                    log.info(f"[loop] Dune: {n_real} reali → integro con DexScreener")
                    existing = {t.get("token_address", "") for t in dune_tokens}
                    extra    = [t for t in
                                fetch_dexscreener_boosted(chain, 30) +
                                fetch_dexscreener_trending(chain, 20)
                                if t.get("token_address", "") not in existing]
                    dune_tokens = dune_tokens + extra

                # Piano C: CoinGecko Trending (ogni ciclo) + Binance Scan (ogni 2 cicli)
                #          + CoinGecko MidCap (ogni 3 cicli)
                existing_addrs = {t.get("token_address", "") for t in dune_tokens}
                cg_tokens = [t for t in cg_trend.get_trending(chain)
                             if t.get("token_address", "") not in existing_addrs]
                if cg_tokens:
                    log.info(f"[loop] +{len(cg_tokens)} da CoinGecko Trending ({chain})")
                    dune_tokens = dune_tokens + cg_tokens
                    existing_addrs.update(t.get("token_address","") for t in cg_tokens)

                # CoinGeckoMidCap: ogni ciclo (cache interna 45min gestisce il rate limit)
                mc_tokens = [
                    t for t in cg_midcap.get_movers(chain)
                    if t.get("token_address", "") not in existing_addrs
                ]
                if mc_tokens:
                    log.info(f"[loop] +{len(mc_tokens)} da CoinGecko MidCap ({chain})")
                    dune_tokens = dune_tokens + mc_tokens
                    existing_addrs.update(t.get("token_address","") for t in mc_tokens)

                if ciclo % 2 == 0:
                    # BinanceScan: token con indirizzo/chain reale risolti via DexScreener
                    # Filtra solo quelli il cui chain corrisponde al chain corrente
                    bn_tokens = [
                        t for t in bn_scan.scan()
                        if t.get("chain") == chain
                        and t.get("token_address", "") not in existing_addrs
                    ]
                    if bn_tokens:
                        log.info(f"[loop] +{len(bn_tokens)} da Binance Scan ({chain})")
                        dune_tokens = dune_tokens + bn_tokens

                if not dune_tokens:
                    log.info(f"[loop] Nessun token per {chain} — skip.")
                    continue

                log.info(f"[loop] {len(dune_tokens)} token totali per {chain}.")

                # ── Dedup intra-ciclo per token_address ───────────────────────
                seen_addr_ic: dict[str, dict] = {}
                for _dt in dune_tokens:
                    _a = _dt.get("token_address", "")
                    if not _a:
                        continue
                    if _a not in seen_addr_ic or _dt.get("inflow_usd", 0) > seen_addr_ic[_a].get("inflow_usd", 0):
                        seen_addr_ic[_a] = _dt
                dune_tokens = list(seen_addr_ic.values())
                # ── Dedup secondario per token_symbol (stesso nome, address diversi) ──
                # Evita che lo stesso token con N pool/pair address venga processato N volte
                seen_sym_ic: dict[str, dict] = {}
                for _dt in dune_tokens:
                    _s = _dt.get("token_symbol", "").upper().strip()
                    if not _s:
                        continue
                    if _s not in seen_sym_ic or _dt.get("inflow_usd", 0) > seen_sym_ic[_s].get("inflow_usd", 0):
                        seen_sym_ic[_s] = _dt
                dune_tokens = list(seen_sym_ic.values())
                log.info(f"[loop] {len(dune_tokens)} token unici dopo dedup intra-ciclo")

                # Filtra token già visti
                existing_addrs = {k for k in seen_pairs}
                dune_tokens = [t for t in dune_tokens if t.get("token_address", "") not in existing_addrs]

                # ── Build profili in parallelo ────────────────────────────────
                MAX_WORKERS    = 6
                FUTURES_TIMEOUT = 300
                _executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
                futures_map = {
                    _executor.submit(
                        build_gem_profile, dt, social, defillama, _binance
                    ): dt
                    for dt in dune_tokens
                }
                _scored_this_chain: set = set()
                try:
                    for future in as_completed(futures_map, timeout=FUTURES_TIMEOUT):
                        dt = futures_map[future]
                        try:
                            profile = future.result()
                            if profile is None:
                                continue

                            passed, reason = gem_filter.check(profile)
                            if not passed:
                                log.debug(f"[filtro] ❌ {profile.get('token_symbol', dt.get('token_symbol','?'))}: {reason}")
                                continue

                            # ── Enrichment post-filtro (solo sui token validi) ────
                            _sym  = profile.get("token_symbol", "")
                            _name = dt.get("token_name", "")
                            _addr = profile.get("token_address", "")
                            _chn  = profile.get("chain", "")
                            if BOT_CONFIG.get("ENABLE_CEX_CHECK"):
                                try:
                                    profile.update(get_cex_listing_score(_sym, _name))
                                except Exception:
                                    pass
                            if BOT_CONFIG.get("ENABLE_HOLDER_CHECK"):
                                try:
                                    profile.update(fetch_holder_concentration(_addr, _chn))
                                except Exception:
                                    pass
                            if BOT_CONFIG.get("ENABLE_SOCIAL", True):
                                try:
                                    _soc = social_analyzer.get_social_score(_sym, _name)
                                    profile["social_score"]  = _soc.get("score", 0)
                                    profile["social_tweets"] = _soc.get("tweets", 0)
                                    profile["social_source"] = _soc.get("source", "")
                                except Exception:
                                    pass

                            # Scoring
                            _sc_key = (f"{profile.get('token_symbol','').upper()}"
                                       f"|{profile.get('chain','')}")
                            if _sc_key in _scored_this_chain:
                                log.debug(f"[score] {profile.get('token_symbol','?')}: già segnalato questo ciclo — skip duplicato")
                                continue
                            scored = score_gem(profile)
                            if scored is None:
                                continue
                            tier = scored.get("tier", "SKIP")
                            if tier in ("BLOCKED", "SKIP"):
                                log.info(
                                    f"[score] ⬇️  {profile.get('token_symbol','?')}: "
                                    f"tier={tier} — score troppo basso"
                                )
                                continue
                            sc = scored.get("score", 0)
                            # CoinGecko Trending: soglia score abbassata a 25 (validazione esterna)
                            _min_sc = 25 if profile.get("source") == "coingecko_trending" \
                                      else SCORE_CONFIG["MIN_SCORE_TO_REPORT"]
                            if sc < _min_sc:
                                log.info(
                                    f"[score] ⬇️  {profile.get('token_symbol','?')}: "
                                    f"score={sc:.0f} < {_min_sc} min"
                                )
                                continue
                            profile.update(scored)
                            _scored_this_chain.add(_sc_key)
                            gems_this_round.append(profile)
                            stampa_gemma(profile)

                        except Exception as _fe:
                            log.warning(f"[loop] errore future {dt.get('token_symbol','?')}: {_fe}")

                except TimeoutError:
                    log.warning("[loop] ⏱️  Timeout futures — alcuni token saltati.")
                except Exception as _exc:
                    log.error(f"[loop] Errore executor: {_exc}")
                finally:
                    _executor.shutdown(wait=False, cancel_futures=True)

            # ── Summary ciclo ─────────────────────────────────────────────────
            n_gems = len(gems_this_round)
            if n_gems:
                log.info(f"[loop] ✅ Ciclo #{ciclo}: {n_gems} gemme segnalate.")
            else:
                log.info(f"[loop] Ciclo #{ciclo}: nessuna gemma trovata.")

            # ── Alert Binance (token senza DEX pair) ──────────────────────────
            if ciclo % 2 == 0:
                unresolved = bn_scan._unresolved_data
                if unresolved:
                    try:
                        to_notify = [
                            t for t in unresolved
                            if not _was_email_sent_recently(
                                t.get("token_symbol", ""), "BINANCE_FUTURES"
                            )
                        ]
                        if to_notify:
                            if send_binance_alert_email(to_notify):
                                for t in to_notify:
                                    _mark_email_sent(
                                        t.get("token_symbol", ""), "BINANCE_FUTURES"
                                    )
                        else:
                            log.debug("[email] Alert Binance: tutti i token già notificati nelle ultime 12h — skip")
                    except Exception as e:
                        log.warning(f"[email] Errore alert Binance: {e}")

            # ── Monitor posizioni v3/v3_large aperte ─────────────────────────
            try:
                _monitor_open_v3_positions()
            except Exception as e:
                log.debug(f"[v3_monitor] errore: {e}")

            # ── HTML Report (fine ciclo) ───────────────────────────────────────
            if GEM_TRACKER_AVAILABLE:
                try:
                    get_gem_tracker().genera_report_html()
                except Exception as e:
                    log.warning(f"[tracker] Errore report HTML: {e}")
            interval = BOT_CONFIG.get("LOOP_INTERVAL_SEC", 300)
            log.info(f"[loop] Prossimo ciclo in {interval}s ...")
            time.sleep(interval)

    except KeyboardInterrupt:
        log.info("[loop] Interruzione manuale.")
        save_persistent_state()
    except Exception:
        log.exception("[loop] Errore critico nel main loop.")
        save_persistent_state()
        raise


if __name__ == "__main__":
    main_loop()
