"""
==============================================================================
defi.py — Quality Gem Hunter v2
Strategia: Smart Money Inflow + Social Validation + Fondamentali

Pipeline di discovery:
  1. Dune Analytics    → token con acquisti da wallet smart-money
  2. DexScreener       → prezzi, liquidità, volume in real-time
  3. SocialAnalyzer    → social score via ntscraper (Nitter/Twitter)
  4. DefiLlama         → TVL (opzionale, migliora scoring ML)
  5. GemFilter         → filtri qualità duri (market cap, liq, social)
  6. XGBoost/RF        → scoring ML con feature fondamentali
  7. GemTracker        → report HTML dettagliato per ogni gemma trovata

Chain supportate: Solana | BSC | Base

⚠️ AVVISO: Solo a scopo educativo. NON garantisce profitti.
   Il trading di criptovalute comporta rischi molto elevati.
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
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Librerie di terze parti ────────────────────────────────────────────────
import joblib
import numpy as np
import pandas as pd
import requests

from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, roc_auc_score
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split
from sklearn.preprocessing import RobustScaler

try:
    import xgboost as xgb
    XGB_AVAILABLE = True
except ImportError:
    XGB_AVAILABLE = False

try:
    sys.path.insert(0, str(Path(__file__).parent.parent / "defi"))
    from rugcheck import is_safe as rugcheck_safe
except ImportError:
    def rugcheck_safe(mint, scanner, chain="solana"): return True
    warnings.warn("XGBoost non installato — uso RandomForest.")

# twscrape (primario — Twitter interno, account reali)
try:
    import twscrape
    from twscrape import API as TwscrapeAPI
    import asyncio
    TWSCRAPE_AVAILABLE = True
except ImportError:
    TWSCRAPE_AVAILABLE = False
    warnings.warn("twscrape non installato (pip install twscrape). Uso fallback Nitter.")

# ntscraper (fallback — Nitter con rotazione istanze)
try:
    from ntscraper import Nitter
    NTSCRAPER_AVAILABLE = True
except ImportError:
    NTSCRAPER_AVAILABLE = False
    warnings.warn("ntscraper non installato (pip install ntscraper). Social score disabilitato.")

# ── Gem Tracker (opzionale) ────────────────────────────────────────────────
try:
    from gem_tracker import get_gem_tracker, GEM_TRACKER_AVAILABLE
except ImportError:
    GEM_TRACKER_AVAILABLE = False
    def get_gem_tracker(): return None  # noqa

# ── Bridge con defi_optimized (gem_watchlist) ──────────────────────────────
# gem_watchlist.py si trova nella cartella padre (../gem_watchlist.py).
# Aggiunge temporaneamente il parent al sys.path solo per questo import.
try:
    import sys as _sys
    _parent = str(Path(__file__).parent.parent)
    if _parent not in _sys.path:
        _sys.path.insert(0, _parent)
    from gem_watchlist import write_gem_to_watchlist, watchlist_summary
    GEM_WATCHLIST_AVAILABLE = True
    log_tmp = logging.getLogger(__name__)
    log_tmp.info("[watchlist] ✅ gem_watchlist.py caricato — bridge con defi_optimized attivo.")
except ImportError:
    GEM_WATCHLIST_AVAILABLE = False
    def write_gem_to_watchlist(gem, **kw): return False  # noqa
    def watchlist_summary(): return "gem_watchlist non disponibile"  # noqa
    logging.getLogger(__name__).warning(
        "[watchlist] gem_watchlist.py non trovato. "
        "Assicurati che sia in defi/ (cartella padre di gemme/). "
        "Il bot funziona normalmente, ma defi_optimized non riceverà le gemme."
    )

# ==============================================================================
# SEZIONE 1 – LOGGING
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("gem_hunter.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ==============================================================================
# SEZIONE 2 – CONFIGURAZIONE & COSTANTI
# ==============================================================================

# ── API Keys ──────────────────────────────────────────────────────────────
DUNE_API_KEY   = os.environ.get("DUNE_API_KEY", "IBx3JQpjKGUg7RhVwHOZWxcKlnTE46Wk")
GOPLUS_API_KEY = os.environ.get("GOPLUS_KEY", "")

if not DUNE_API_KEY:
    warnings.warn("DUNE_API_KEY non impostata. Imposta la variabile d'ambiente.")

# ── Account Twitter per twscrape ──────────────────────────────────────────
# METODO CONSIGLIATO (se il tuo IP è bloccato da Cloudflare): usa i cookie.
#
# Come ottenere auth_token e ct0 (validi per mesi):
#   1. Apri https://x.com nel tuo browser, accedi normalmente
#   2. Apri DevTools (F12) → Application → Cookies → https://x.com
#   3. Copia il valore di "auth_token" e "ct0"
#   4. Incollali qui sotto nel campo "cookies" come:
#      "auth_token=VALORE; ct0=VALORE"
#
# METODO ALTERNATIVO (solo se l'IP non è bloccato): username + password.
# Se lasci la lista vuota → usa automaticamente Nitter come fallback.
TWITTER_ACCOUNTS = [
    # twscrape disabilitato — IndexError su SearchTimeline con questo IP.
    # Il bot usa social score neutro (25) che passa il filtro 20–70.
    # Per riabilitare: aggiungi i cookie corretti di CIASCUN account (auth_token diverso per ogni account).
]

# ── Chain supportate ──────────────────────────────────────────────────────
CHAINS = {
    "solana": {
        "dexscreener_id":   "solana",
        "goplus_chain_id":  "900",
        "is_evm":           False,
        "dune_chain_name":  "solana",
        "min_liquidity":    15_000,    # Solana meme spesso ha liq $15-50k
    },
    "bsc": {
        "dexscreener_id":   "bsc",
        "goplus_chain_id":  "56",
        "is_evm":           True,
        "dune_chain_name":  "bnb",
        "min_liquidity":    20_000,    # BSC low-cap
    },
    "ethereum": {
        "dexscreener_id":   "ethereum",
        "goplus_chain_id":  "1",
        "is_evm":           True,
        "dune_chain_name":  "ethereum",
        "min_liquidity":    30_000,    # ETH ha liq media più alta
    },
    # "base": {          # disabilitato — errore "Invalid performance tier" su Dune free
    #     "dexscreener_id":  "base",
    #     "goplus_chain_id": "8453",
    #     "is_evm":          True,
    #     "dune_chain_name": "base",
    #     "min_liquidity":   100_000,
    # },
}

# ── URL base API ──────────────────────────────────────────────────────────
DUNE_BASE      = "https://api.dune.com/api/v1"
DEXSCREENER_BASE = "https://api.dexscreener.com"
DEFILLAMA_BASE = "https://api.llama.fi"
GOPLUS_BASE    = "https://api.gopluslabs.io/api/v1"

# ── Query IDs Dune (sostituisci con i tuoi) ───────────────────────────────
DUNE_QUERIES = {
    # Query che ritorna token con maggior smart-money inflow nelle ultime 24h
    # Colonne attese: token_address, token_symbol, token_name,
    #                 inflow_usd, wallet_count, avg_wallet_pnl_pct, chain
    # Questi ID vengono popolati automaticamente da setup_dune.py
    "solana_smart_money":   "7417474",
    "bsc_smart_money":      "7417475",
    "base_smart_money":     "7417476",
    "ethereum_smart_money": "7417477",

    # Query alternativa multi-chain (se preferisci una sola query)
    "multichain_smart_money": "PLACEHOLDER_QUERY_ID_MULTICHAIN",
}

# ── Configurazione filtri qualità ─────────────────────────────────────────
FILTER_CONFIG = {
    # ── Market cap ──────────────────────────────────────────────────────────
    # Calibrato su 221 gemme reali (apr-mag 2026): MC <500k → 39% pump vs >2M → 27% pump.
    "MIN_MARKET_CAP_USD":    100_000,     # era 200k — allarga la fascia early stage
    "MAX_MARKET_CAP_USD": 100_000_000,   # alzato a 100M — rimuove il blocco sui mid-cap (allineato V3)

    # ── Liquidità ────────────────────────────────────────────────────────────
    # Alzata da 30k a 60k: social filter non operativo (Nitter/twscrape down),
    # compensiamo con soglie più alte su liq/vol/wallets.
    "MIN_LIQUIDITY_USD":      60_000,     # era 30k — senza social serve bar più alto
    "MAX_LIQUIDITY_USD":   5_000_000,

    # ── Volume ───────────────────────────────────────────────────────────────
    "MIN_VOLUME_1H_USD":      20_000,     # era 5k — blocca token con vol=2/19 USD

    # ── Social score ─────────────────────────────────────────────────────────
    # Nota: social analysis non funziona (twscrape off, Nitter down) →
    # tutti i token ricevono score neutro=25. Filtro presente ma inattivo.
    "MIN_SOCIAL_SCORE":           20,
    "MAX_SOCIAL_SCORE":           70,

    # ── Età pair ─────────────────────────────────────────────────────────────
    # Analisi su 466 token reali (mag 2026):
    #   <6h  → mediana 0%, 40% bad (<-10%)  — rug/dump zone, da bloccare
    #   6-24h → mediana 25.8%, 42% >30%, solo 3.8% bad — SWEET SPOT
    #   1-7gg → mediana 6-11%, 1-11% bad — ok ma meno esplosivo
    "MIN_PAIR_AGE_HOURS":        6.0,
    "MAX_PAIR_AGE_HOURS":       168,

    # ── Change 1h all'entry ──────────────────────────────────────────────────
    "MAX_CHANGE_1H_PCT":         25.0,
    "MIN_CHANGE_1H_PCT":        -30.0,

    # ── Sicurezza (EVM) ──────────────────────────────────────────────────────
    "MAX_BUY_TAX_PCT":           10,
    "MAX_SELL_TAX_PCT":          10,
    "REJECT_HONEYPOT":          True,

    # ── Smart money ──────────────────────────────────────────────────────────
    # Alzati: senza social filter i wallet Dune sono il gate principale.
    "MIN_INFLOW_USD":         20_000,    # era 5k
    "MIN_SMART_WALLETS":           8,   # era 5 — richiede più conferme wallet
    "MIN_INFLOW_TO_MCAP_TIER1": 0.005,

    # ── Cooldown per token ───────────────────────────────────────────────────
    "TOKEN_COOLDOWN_MIN":       120,
    "BLACKLIST_DROP_PCT":       -70.0,
    "BLACKLIST_DURATION_H":       8.0,
}

# ── Configurazione ML ─────────────────────────────────────────────────────
ML_CONFIG = {
    "MODEL_PATH":       "gem_model.joblib",
    "SCALER_PATH":      "gem_scaler.joblib",
    # CRITICO: il modello è anti-predittivo su dati reali.
    # prob [0.0-0.3) → 35% pump (migliore!), prob [0.7-1.0) → 82% dump (peggiore!).
    # La soglia 0.55 selezionava quasi solo dump. Abbassiamo a 0.05 (quasi aperto)
    # e affidiamo il filtro primario ai FILTER_CONFIG (età, MC, wallets, change_1h).
    "SIGNAL_THRESHOLD": 0.05,   # era 0.55 — modello invertito su dati reali, usare filtri hard
    "PUMP_THRESHOLD_PCT": 30.0,
    "LOOKAHEAD_HOURS":    4,
    "N_ESTIMATORS":     300,
    "MAX_DEPTH":          6,
    "LEARNING_RATE":    0.05,
    "TEST_SIZE":         0.2,
    "CV_FOLDS":           5,
    "RANDOM_STATE":      42,
    "MIN_TRAINING_ROWS": 50,          # abbassato: con dati reali bastano meno campioni
    "RETRAIN_EVERY_N_CYCLES": 20,     # re-addestra ogni 20 cicli (~100 min)
    "PUMP_REAL_THRESHOLD_PCT": 30.0,  # peak >= 30% in 4h → gem=1
    "SIGNALS_CSV":        "reports/signals_log.csv",
    "PRICE_FOLLOWUP_CSV": "reports/price_followup.csv",
}

# ── Loop ──────────────────────────────────────────────────────────────────
BOT_CONFIG = {
    "LOOP_INTERVAL_SEC": 300,    # ogni 5 minuti
    "USE_MOCK_FALLBACK": False,  # meglio nessun segnale che segnali finti
    "REQUEST_TIMEOUT":    15,
    "REQUEST_RETRIES":     3,
    "REQUEST_BACKOFF":     2,
}

# ── Configurazione Email ──────────────────────────────────────────────────
# NOTA: os.environ.get("CHIAVE_ENV", "valore_default")
# → se la variabile d'ambiente non è impostata, usa il default hardcoded.
# Per sovrascrivere senza toccare il codice: export SMTP_PASSWORD="nuova_password"
EMAIL_CONFIG = {
    "ENABLED":       True,
    "SMTP_HOST":     os.environ.get("SMTP_HOST",     "smtp.gmail.com"),
    "SMTP_PORT":     int(os.environ.get("SMTP_PORT", "587")),
    "SMTP_USER":     os.environ.get("SMTP_USER",     "giagnacovo.luca@gmail.com"),
    "SMTP_PASSWORD": os.environ.get("SMTP_PASSWORD", ""),  # App Password Gmail
    "FROM_ADDR":     os.environ.get("SMTP_FROM",     "giagnacovo.luca@gmail.com"),
    "TO_ADDR":       os.environ.get("SMTP_TO",       "giagnacovo.luca@gmail.com"),
    # Invia solo se la probabilità gem supera questa soglia
    "MIN_PROBABILITY": float(os.environ.get("EMAIL_MIN_PROB", "0.60")),
}

# ── Feature columns (modello ML) ─────────────────────────────────────────
FEATURE_COLUMNS = [
    # Smart Money (Dune)
    "inflow_usd",
    "inflow_wallet_count",
    "avg_wallet_pnl_pct",
    "inflow_to_mcap_ratio",      # inflow_usd / market_cap_usd

    # Social
    "social_score",
    "social_tweet_count",

    # DeFi
    "tvl_usd",
    "tvl_to_mcap_ratio",         # tvl / market_cap

    # Market (DexScreener)
    "market_cap_usd_log",        # log10 per normalizzare
    "liquidity_usd_log",
    "volume_1h_usd",
    "volume_5m_usd",
    "buy_sell_ratio_1h",
    "change_5m_pct",
    "change_1h_pct",
    "change_6h_pct",
    "pair_age_hours",
    "liquidity_to_mcap_ratio",
    "volume_to_liquidity_ratio",
    "txns_1h_buys",
    "txns_1h_sells",
]

# analizza la crescita (w) e l'accelerazione (wa) del numero di wallet nel tempo, e calcola un punteggio basato su queste metriche e sull'età della pair.
from collections import defaultdict
from datetime import datetime, timedelta
import threading as _threading

# Inizializza la memoria storica fuori dal loop
WALLET_HISTORY = defaultdict(list)

# ── Cooldown e blacklist dinamica per gemme ───────────────────────────────────
_gem_last_signal: dict = {}   # pair_address → datetime ultimo segnale
_gem_blacklist:   dict = {}   # pair_address → datetime scadenza blacklist
_gem_lock = _threading.Lock()

def _gem_cooldown_ok(pair_address: str) -> bool:
    """True se il token NON è in cooldown (può essere segnalato)."""
    mins = FILTER_CONFIG.get("TOKEN_COOLDOWN_MIN", 120)
    with _gem_lock:
        last = _gem_last_signal.get(pair_address)
        if last is None: return True
        return (datetime.now() - last).total_seconds() / 60 >= mins

def _set_gem_cooldown(pair_address: str) -> None:
    with _gem_lock:
        _gem_last_signal[pair_address] = datetime.now()

def _gem_blacklisted(pair_address: str) -> bool:
    with _gem_lock:
        exp = _gem_blacklist.get(pair_address)
        if exp is None: return False
        if datetime.now() < exp: return True
        del _gem_blacklist[pair_address]
        return False

def _blacklist_gem(pair_address: str, symbol: str = "") -> None:
    hours = FILTER_CONFIG.get("BLACKLIST_DURATION_H", 8.0)
    with _gem_lock:
        _gem_blacklist[pair_address] = datetime.now() + timedelta(hours=hours)
    import logging
    logging.getLogger(__name__).info(
        f"[blacklist] 🚫 {symbol or pair_address[:8]} blacklistata per {hours}h (dump rilevato)")

def _check_gem_followup_blacklist() -> None:
    """Legge gems_followup.csv e blacklista gemme con dump >70%."""
    import csv as _csv, os as _os
    drop_thresh = FILTER_CONFIG.get("BLACKLIST_DROP_PCT", -70.0)
    fpath = "reports/gems_followup.csv"
    if not _os.path.exists(fpath): return
    try:
        min_chg: dict = {}
        sym_map: dict = {}
        with open(fpath, "r", encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                pair = row.get("pair_address","")
                chg  = row.get("change_pct","")
                if not pair or not chg: continue
                try:
                    v = float(chg)
                    min_chg[pair] = min(min_chg.get(pair, 0.0), v)
                    sym_map[pair] = row.get("token_symbol","")
                except: pass
        for pair, mc in min_chg.items():
            if mc <= drop_thresh and not _gem_blacklisted(pair):
                _blacklist_gem(pair, sym_map.get(pair,""))
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"[blacklist] Errore followup: {e}")

def compute_early_metrics(token_address, wallet_count, pair_data):
    now = datetime.now()
    history = WALLET_HISTORY[token_address]

    history.append({
        "timestamp": now,
        "wallet_count": wallet_count
    })

    if len(history) > 10:
        history.pop(0)

    def growth(h):
        if len(h) < 2: return 0
        d = h[-1]["wallet_count"] - h[-2]["wallet_count"]
        t = (h[-1]["timestamp"] - h[-2]["timestamp"]).total_seconds() / 60
        return d / t if t > 0 else 0

    def accel(h):
        if len(h) < 3: return 0
        # Calcola la variazione tra la crescita attuale e quella precedente
        return growth(h[-2:]) - growth(h[:-1])

    wg = growth(history)
    wa = accel(history)

    # --- pair age ---
    pair_created_at = pair_data.get("pairCreatedAt") if pair_data else None
    if pair_created_at:
        created = datetime.fromtimestamp(pair_created_at / 1000)
        pair_age = (now - created).total_seconds() / 3600
    else:
        pair_age = 999

    volume_5m = pair_data.get("volume", {}).get("m5", 0) if pair_data else 0
    volume_1h = pair_data.get("volume", {}).get("h1", 0) if pair_data else 0

    # --- score ---
    score = 0
    if wa > 0.5: score += 3
    elif wa > 0.2: score += 2
    if wg > 1: score += 2
    elif wg > 0.3: score += 1
    if 0.5 <= pair_age <= 6: score += 3
    elif 6 < pair_age <= 24: score += 1
    if volume_1h > 0 and volume_5m > (volume_1h / 12): score += 2

    return wg, wa, pair_age, score

# ==============================================================================
# SEZIONE 3 – UTILITIES
# ==============================================================================
def classify_gem(profile: dict) -> str:
    """
    Assegna una categoria operativa al token basata sul momentum e la qualità dell'inflow.
    """
    score = profile.get("momentum_score", 0)
    accel = profile.get("wallet_acceleration", 0)
    inflow = profile.get("inflow_usd", 0)
    wa_rate = profile.get("wallet_growth_rate", 0)

    # 1. ALERT: FAKE PUMP (Inflow alto ma interesse in calo)
    # Se entrano molti soldi ma la velocità dei nuovi wallet è ferma o negativa
    if inflow > 10000 and accel <= 0:
        return "FAKE_PUMP"

    # 2. HOT GEM: Esplosione in corso
    if score >= 8 or (accel > 0.5 and wa_rate > 2):
        return "HOT_GEM"

    # 3. STEADY: Crescita sana e costante
    if 4 <= score < 8:
        return "STEADY_GROWTH"

    return "NEUTRAL"

def _safe_get(
    url: str,
    params: dict = None,
    headers: dict = None,
    timeout: int = None,
    retries: int = None,
    label: str = "",
) -> Optional[requests.Response]:
    """GET con retry esponenziale e gestione errori silenziosa."""
    timeout  = timeout  or BOT_CONFIG["REQUEST_TIMEOUT"]
    retries  = retries  or BOT_CONFIG["REQUEST_RETRIES"]
    backoff  = BOT_CONFIG["REQUEST_BACKOFF"]

    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=headers,
                                timeout=timeout)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 429:
                wait = backoff ** (attempt + 2)
                log.warning(f"[{label}] Rate limit (429). Attendo {wait}s...")
                time.sleep(wait)
            elif resp.status_code >= 500:
                log.warning(f"[{label}] Server error {resp.status_code}. Retry {attempt+1}/{retries}")
                time.sleep(backoff ** attempt)
            else:
                log.debug(f"[{label}] HTTP {resp.status_code} per {url}")
                return None
        except requests.exceptions.Timeout:
            log.warning(f"[{label}] Timeout (attempt {attempt+1}/{retries})")
            time.sleep(backoff ** attempt)
        except requests.exceptions.ConnectionError:
            log.warning(f"[{label}] Connessione fallita per {url}")
            time.sleep(backoff ** attempt)
        except Exception as e:
            log.warning(f"[{label}] Errore imprevisto: {e}")
            return None
    return None

# ==============================================================================
# SEZIONE 4 – DUNE ANALYTICS (DuneDataFetcher)
# ==============================================================================

class DuneDataFetcher:
    """
    Interroga Dune Analytics per ottenere token con Smart Money Inflow.

    Setup Dune (una-tantum):
      1. Vai su dune.com e crea una query SQL per la tua chain.
      2. La query deve restituire almeno le colonne:
           token_address | token_symbol | inflow_usd | wallet_count | avg_wallet_pnl_pct
      3. Sostituisci i PLACEHOLDER_QUERY_ID_* in DUNE_QUERIES con i tuoi ID.
      4. Imposta la variabile d'ambiente: DUNE_API_KEY=<tua_chiave>

    Esempio SQL per Solana (Dune):
    ─────────────────────────────
    SELECT
        token_mint_address AS token_address,
        symbol             AS token_symbol,
        SUM(amount_usd)    AS inflow_usd,
        COUNT(DISTINCT signer) AS wallet_count,
        AVG(realized_pnl_pct)  AS avg_wallet_pnl_pct
    FROM solana.dex_trades t
    JOIN dune.smart_wallets w ON t.signer = w.address
    WHERE t.block_time > NOW() - INTERVAL '24' HOUR
      AND t.side = 'buy'
    GROUP BY 1, 2
    HAVING SUM(amount_usd) > 1000
    ORDER BY inflow_usd DESC
    LIMIT 50
    """

    def __init__(self):
        self._headers = {
            "X-DUNE-API-KEY": DUNE_API_KEY,
            "Content-Type":   "application/json",
        }
        self._cache: dict[str, tuple[datetime, list]] = {}  # chain → (ts, rows)
        self._cache_ttl = timedelta(minutes=10)

    def get_smart_money_tokens(self, chain: str) -> list[dict]:
        """
        Ritorna lista di token con smart money inflow per una chain.
        Usa cache per evitare query ripetute; fallback a mock se offline.
        """
        # Cache hit
        if chain in self._cache:
            cached_ts, cached_rows = self._cache[chain]
            if datetime.now() - cached_ts < self._cache_ttl:
                log.debug(f"[Dune] Cache hit per {chain} ({len(cached_rows)} token)")
                return cached_rows

        query_key = f"{chain}_smart_money"
        query_id  = DUNE_QUERIES.get(query_key) or DUNE_QUERIES.get("multichain_smart_money")

        if not query_id or "PLACEHOLDER" in str(query_id):
            log.warning(f"[Dune] Query ID non configurato per {chain}. "
                        f"Imposta DUNE_QUERIES['{query_key}'] con il tuo query ID. "
                        f"Usando fallback mock.")
            return self._mock_tokens(chain) if BOT_CONFIG["USE_MOCK_FALLBACK"] else []

        # Prova a ottenere i risultati cached da Dune (più veloce)
        rows = self._get_latest_results(query_id, chain)
        if rows is None:
            # Esegui la query e aspetta i risultati
            rows = self._execute_and_wait(query_id, chain)
        if rows is None:
            log.warning(f"[Dune] Nessun risultato per {chain}. Fallback mock.")
            rows = self._mock_tokens(chain) if BOT_CONFIG["USE_MOCK_FALLBACK"] else []

        self._cache[chain] = (datetime.now(), rows)
        log.info(f"[Dune] {len(rows)} token smart-money trovati su {chain}.")
        return rows

    def _get_latest_results(self, query_id: str, chain: str) -> Optional[list]:
        """Recupera l'ultimo risultato cached di una query Dune (no esecuzione)."""
        url  = f"{DUNE_BASE}/query/{query_id}/results"
        resp = _safe_get(url, headers=self._headers, label="Dune/results")
        if resp is None:
            return None
        try:
            data = resp.json()
            rows = data.get("result", {}).get("rows", [])
            if not rows:
                return None
            return self._normalize_rows(rows, chain)
        except Exception as e:
            log.warning(f"[Dune] Errore parsing risultati: {e}")
            return None

    def _execute_and_wait(self, query_id: str, chain: str,
                          max_wait_sec: int = 60) -> Optional[list]:
        """Esegue una query Dune e aspetta i risultati (polling)."""
        # Avvia esecuzione
        url  = f"{DUNE_BASE}/query/{query_id}/execute"
        try:
            resp = requests.post(url, headers=self._headers,
                                 json={"performance": "large"},
                                 timeout=BOT_CONFIG["REQUEST_TIMEOUT"])
            if resp.status_code != 200:
                log.warning(f"[Dune] Execute error {resp.status_code}: {resp.text[:200]}")
                return None
            exec_id = resp.json().get("execution_id")
            if not exec_id:
                return None
        except Exception as e:
            log.warning(f"[Dune] Execute request failed: {e}")
            return None

        # Polling risultati
        deadline = time.time() + max_wait_sec
        while time.time() < deadline:
            time.sleep(3)
            status_url  = f"{DUNE_BASE}/execution/{exec_id}/status"
            status_resp = _safe_get(status_url, headers=self._headers, label="Dune/status")
            if status_resp is None:
                continue
            state = status_resp.json().get("state", "")
            if state == "QUERY_STATE_COMPLETED":
                results_url  = f"{DUNE_BASE}/execution/{exec_id}/results"
                results_resp = _safe_get(results_url, headers=self._headers, label="Dune/exec_results")
                if results_resp is None:
                    return None
                rows = results_resp.json().get("result", {}).get("rows", [])
                return self._normalize_rows(rows, chain)
            if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
                log.warning(f"[Dune] Query {query_id} fallita: {state}")
                return None

        log.warning(f"[Dune] Timeout attesa risultati per query {query_id}")
        return None

    # Simboli da escludere sempre (stablecoin, wrapped asset, grandi blue chip)
    _EXCLUDE_SYMBOLS = {
        # Stablecoin fiat
        'USDT','USDC','BUSD','DAI','FRAX','TUSD','USDP','GUSD','LUSD','SUSD',
        'USDE','GHO','USDS','PYUSD','USDG','USDF','EURC','RLUSD','USD0','AUSD',
        'USDTB','MSUSD','PMUSD','DOLA','FRXUSD','USUSDS','SUSDS','SUSDE','USDM',
        'REUSD','USDGM','APXUSD','USDG','CRVUSD','BOLD',
        # BTC wrapped
        'WBTC','CBBTC','TBTC','LBTC','EBTC','BTCB',
        # ETH wrapped/staked
        'WETH','CBETH','STETH','WSTETH','RETH','FRXETH','SFRXETH',
        'METH','OSETH','EZETH','WEETH','RSETH','SYBTC',
        # Gold/commodities
        'XAUT','PAXG',
        # Large cap DeFi (non sono "gem")
        'AAVE','CRV','LDO','MKR','UNI','LINK','RNDR','ENS',
        'PEPE','DOGE','SHIB','BONK','WIF','POPCAT',
    }

    def _normalize_rows(self, rows: list, chain: str) -> list[dict]:
        """Normalizza le colonne Dune verso il formato interno."""
        result = []
        for r in rows:
            token_addr = (r.get("token_address") or r.get("token_mint_address") or "").strip()
            if not token_addr:
                continue
            sym = (r.get("token_symbol") or r.get("symbol") or "").strip().upper()
            # Salta stablecoin e wrapped asset noti — sprecano chiamate API
            if sym in self._EXCLUDE_SYMBOLS:
                continue
            result.append({
                "token_address":    token_addr,
                "token_symbol":     r.get("token_symbol") or r.get("symbol") or "",
                "token_name":       r.get("token_name")   or r.get("name")   or "",
                "chain":            chain,
                "inflow_usd":       float(r.get("inflow_usd") or 0),
                "inflow_wallet_count": int(r.get("unique_buyers") or r.get("wallet_count") or r.get("inflow_wallet_count") or 0),
                "avg_wallet_pnl_pct": float(r.get("avg_wallet_pnl_pct") or 0),
                "source":           "dune",
            })
        return result

    def _mock_tokens(self, chain: str) -> list[dict]:
        """Dati simulati per sviluppo/test quando Dune è offline o non configurato."""
        import random
        rng = random.Random(42 + hash(chain) % 100)
        symbols = {
            "solana": ["PEPE2", "DAWG", "MOCHI", "LUNA2", "GROK"],
            "bsc":    ["BABYDOGE", "FLOKI2", "SHIBX", "BONK2", "TURBO"],
            "base":   ["TOSHI", "BRETT", "DEGEN", "BASE_APE", "NORM"],
        }.get(chain, ["TOKEN1", "TOKEN2", "TOKEN3"])

        mocks = []
        for sym in symbols:
            addr = "mock_" + sym.lower() + "_" + chain
            mocks.append({
                "token_address":    addr,
                "token_symbol":     sym,
                "token_name":       sym + " Token",
                "chain":            chain,
                "inflow_usd":       rng.uniform(5_000, 200_000),
                "inflow_wallet_count": rng.randint(3, 30),
                "avg_wallet_pnl_pct": rng.uniform(10, 120),
                "source":           "mock_dune",
            })
        return mocks

# ==============================================================================
# SEZIONE 5 – SOCIAL ANALYZER (SocialAnalyzer)
# ==============================================================================

class SocialAnalyzer:
    """
    Analizza il sentiment social di un token su Twitter/X.

    Fonti (in ordine di priorità):
      1. twscrape   — API interna Twitter, dati reali, richiede account Twitter
      2. ntscraper  — Nitter con rotazione automatica istanze, fallback gratuito

    Social Score (0–100):
      • Base:      engagement medio per tweet (likes + RT×2 + replies)
      • Bonus:     volume di tweet non spam
      • Penalità:  tweet spam/pump filtrati
      • Early Adopter Rule: 20–70 → valido | >70 → già in iper-hype → scarta

    Setup twscrape (una-tantum):
      from twscrape import API
      import asyncio
      api = API()
      asyncio.run(api.pool.add_account("user","pass","email","email_pass"))
      asyncio.run(api.pool.login_all())
    """

    SPAM_KEYWORDS = [
        "100x", "1000x", "guaranteed", "moon guaranteed", "buy now",
        "last chance", "presale", "airdrop", "free token", "t.me/",
        "pump", "🚀🚀🚀", "💎💎", "lfg lfg", "don't miss", "whitelist",
        "join now", "early access", "x100", "next 100x",
    ]

    # Istanze Nitter pubbliche — ruotate automaticamente
    NITTER_INSTANCES = [
        "nitter.privacydev.net",
        "nitter.poast.org",
        "nitter.rawbit.ninja",
        "nitter.1d4.us",
        "nitter.kavin.rocks",
        "nitter.unixfox.eu",
        "nitter.42l.fr",
        "nitter.moomoo.me",
        "nitter.esmailelbob.xyz",
        "nitter.tiekoetter.com",
    ]

    def __init__(self):
        self._cache: dict[str, tuple[datetime, dict]] = {}
        self._cache_ttl = timedelta(minutes=20)

        # twscrape setup
        self._twscrape_api = None
        self._twscrape_loop = None
        if TWSCRAPE_AVAILABLE:
            try:
                self._twscrape_loop = asyncio.new_event_loop()
                self._twscrape_api  = TwscrapeAPI()
                # Login automatico dagli account configurati in TWITTER_ACCOUNTS
                if TWITTER_ACCOUNTS:
                    self._twscrape_loop.run_until_complete(
                        self._setup_twscrape_accounts()
                    )
                else:
                    log.info("[Social] twscrape: nessun account in TWITTER_ACCOUNTS → "
                             "usa Nitter come fallback.")
                    self._twscrape_api = None
            except Exception as e:
                log.warning(f"[Social] twscrape init fallito: {e}")
                self._twscrape_api = None

        # ntscraper — istanze Nitter con stato
        self._nitter_instances: list[dict] = [
            {"host": h, "ok": True, "last_fail": None}
            for h in self.NITTER_INSTANCES
        ]
        self._nitter_lock = threading.Lock()
        self._nitter_scraper = None  # istanza corrente
        self._nitter_globally_down = False  # True = tutte le istanze down, salta i retry
        self._init_nitter()

    # ── Setup Nitter ────────────────────────────────────────────────────────

    def _init_nitter(self):
        """Inizializza ntscraper sulla prima istanza disponibile."""
        if not NTSCRAPER_AVAILABLE:
            return
        for inst in self._nitter_instances:
            if self._test_nitter(inst["host"]):
                try:
                    self._nitter_scraper = Nitter(instance=f"https://{inst['host']}",
                                                  log_level=0)
                    inst["ok"] = True
                    log.info(f"[Social] Nitter attivo su {inst['host']}")
                    return
                except Exception:
                    inst["ok"] = False
        log.warning("[Social] Nessuna istanza Nitter disponibile.")

    def _test_nitter(self, host: str, timeout: int = 5) -> bool:
        """Testa se un'istanza Nitter risponde."""
        try:
            r = requests.get(f"https://{host}", timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0"})
            return r.status_code == 200
        except Exception:
            return False

    def _rotate_nitter(self) -> bool:
        """
        Ruota a una nuova istanza Nitter funzionante.
        Ritorna True se riesce a trovarne una, False altrimenti.
        """
        if not NTSCRAPER_AVAILABLE:
            return False
        with self._nitter_lock:
            # Prima ri-testa le istanze fallite da > 10 minuti
            for inst in self._nitter_instances:
                if not inst["ok"] and inst["last_fail"]:
                    if (datetime.now() - inst["last_fail"]).seconds > 600:
                        inst["ok"] = self._test_nitter(inst["host"])

            # Cerca la prima istanza ok
            for inst in self._nitter_instances:
                if not inst["ok"]:
                    continue
                try:
                    self._nitter_scraper = Nitter(
                        instance=f"https://{inst['host']}",
                        log_level=0
                    )
                    log.info(f"[Social] Rotazione Nitter → {inst['host']}")
                    return True
                except Exception:
                    inst["ok"] = False
                    inst["last_fail"] = datetime.now()

        log.warning("[Social] Nessuna istanza Nitter disponibile dopo rotazione.")
        return False

    def _mark_nitter_failed(self, host: str):
        """Segna un'istanza Nitter come non funzionante."""
        with self._nitter_lock:
            for inst in self._nitter_instances:
                if inst["host"] == host:
                    inst["ok"] = False
                    inst["last_fail"] = datetime.now()

    def _patch_twscrape_db_cookies(self, accounts_with_cookies: list):
        """Aggiorna direttamente il DB SQLite di twscrape per gli account con cookie.

        twscrape usa INSERT OR IGNORE → i vecchi record con active=False non vengono
        aggiornati da add_account(). Questo metodo forza l'aggiornamento di cookies e
        active=1 per gli account già presenti.
        """
        import sqlite3, json

        # twscrape cerca accounts.db nella cwd o in ~/.twscrape/
        db_candidates = [
            Path("accounts.db"),
            Path.home() / ".twscrape" / "accounts.db",
            Path(".twscrape") / "accounts.db",
        ]
        db_path = next((p for p in db_candidates if p.exists()), None)
        if db_path is None:
            log.debug("[Social] accounts.db non trovato, skip patch cookie.")
            return

        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()

            for acc in accounts_with_cookies:
                username = acc["username"]
                raw_cookies = acc["cookies"]

                # twscrape salva i cookie come JSON dict {name: value}
                cookie_dict = {}
                for part in raw_cookies.split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        cookie_dict[k.strip()] = v.strip()
                cookies_json = json.dumps(cookie_dict)

                # Controlla se l'account esiste già
                cur.execute("SELECT username FROM accounts WHERE username=?", (username,))
                row = cur.fetchone()
                if row:
                    cur.execute(
                        "UPDATE accounts SET cookies=?, active=1 WHERE username=?",
                        (cookies_json, username),
                    )
                    log.info(f"[Social] twscrape DB: cookie aggiornati per '{username}' (active=1).")
                # Se non esiste, add_account() lo creerà normalmente.

            conn.commit()
            conn.close()
        except Exception as e:
            log.warning(f"[Social] Errore patch DB twscrape: {e}")

    async def _setup_twscrape_accounts(self):
        """Aggiunge e logga gli account Twitter configurati in TWITTER_ACCOUNTS.

        Supporta due metodi:
          - Cookie (consigliato): fornisci 'cookies' con auth_token e ct0 → bypassa Cloudflare
          - Password: login classico (fallisce se l'IP è bloccato da Cloudflare)
        """
        cookie_accounts = [a for a in TWITTER_ACCOUNTS if a.get("cookies")]
        passwd_accounts = [a for a in TWITTER_ACCOUNTS if not a.get("cookies")]

        # ── Step 1: patch DB per account già esistenti con cookie ────────────
        if cookie_accounts:
            self._patch_twscrape_db_cookies(cookie_accounts)

        # ── Step 2: aggiungi account (INSERT OR IGNORE per i già esistenti) ──
        added = 0
        for acc in TWITTER_ACCOUNTS:
            try:
                cookies = acc.get("cookies", "")
                if cookies:
                    await self._twscrape_api.pool.add_account(
                        acc["username"],
                        acc["password"],
                        acc["email"],
                        acc.get("email_password", ""),
                        cookies=cookies,
                    )
                    log.debug(f"[Social] twscrape: add_account cookie per '{acc['username']}'.")
                else:
                    await self._twscrape_api.pool.add_account(
                        acc["username"],
                        acc["password"],
                        acc["email"],
                        acc.get("email_password", ""),
                    )
                    log.info(f"[Social] twscrape: account '{acc['username']}' aggiunto (verrà fatto login).")
                added += 1
            except Exception as e:
                log.warning(f"[Social] Errore aggiunta account {acc.get('username','?')}: {e}")

        if added == 0:
            log.warning("[Social] Nessun account twscrape aggiunto. Fallback a Nitter.")
            self._twscrape_api = None
            return

        # ── Step 3: reset lock residui da run precedenti ─────────────────────
        try:
            await self._twscrape_api.pool.reset_locks()
            log.info("[Social] twscrape: lock residui resettati.")
        except Exception as e:
            log.debug(f"[Social] reset_locks non disponibile: {e}")

        # ── Step 4: login_all() per tutti — popola campo 'user' nel DB ────────
        # Anche con cookie serve login_all(): twscrape usa i cookie per autenticarsi
        # e poi salva i dati utente (bearer_token, user_id ecc.) nel campo 'user'.
        # Senza questi dati la SearchTimeline fallisce con IndexError.
        log.info(f"[Social] twscrape: login in corso (popola metadati utente)...")
        try:
            await self._twscrape_api.pool.login_all()
            log.info("[Social] twscrape: login_all completato.")
        except Exception as e:
            log.warning(f"[Social] login_all eccezione: {e}. Continuo con account patchati.")

        # ── Step 4: verifica account attivi ──────────────────────────────────
        try:
            all_accounts = await self._twscrape_api.pool.get_all()
            active_accounts = [a for a in all_accounts if getattr(a, "active", False)]
        except Exception as e:
            log.warning(f"[Social] Impossibile verificare account attivi: {e}")
            active_accounts = []

        if active_accounts:
            log.info(f"[Social] ✅ twscrape: {len(active_accounts)}/{added} account attivi.")
        else:
            log.warning(
                "[Social] twscrape: nessun account attivo "
                "(cookie scaduti o IP bloccato). Fallback a Nitter."
            )
            self._twscrape_api = None

    # ── Interfaccia pubblica ────────────────────────────────────────────────

    def get_social_score(self, ticker: str, token_name: str = "") -> dict:
        """
        Ritorna {social_score, tweet_count, source}.
        Prova twscrape → ntscraper/Nitter → score neutro (25).
        """
        cache_key = ticker.upper()
        if cache_key in self._cache:
            ts, data = self._cache[cache_key]
            if datetime.now() - ts < self._cache_ttl:
                return data

        result = None

        # 1. twscrape (primario)
        if TWSCRAPE_AVAILABLE and self._twscrape_api and self._twscrape_loop:
            try:
                result = self._analyze_twscrape(ticker, token_name)
            except IndexError:
                log.warning("[Social] twscrape IndexError — disabilito per questa sessione.")
                self._twscrape_api = None
            except Exception as e:
                err_str = str(e).lower()
                # "No account available" = tutti gli account in timeout → disabilita subito
                # invece di bloccare il ciclo ad aspettare il timeout di 15 minuti
                if "no account" in err_str or "queue" in err_str or "timeout" in err_str:
                    log.warning(
                        f"[Social] twscrape: nessun account disponibile "
                        f"(IndexError interno / timeout). Disabilito per questa sessione. "
                        f"Il bot usa score neutro (25) e continua."
                    )
                    self._twscrape_api = None
                else:
                    log.debug(f"[Social] twscrape errore per {ticker}: {e}")

        # 2. ntscraper/Nitter (fallback) — salta se tutte le istanze sono già note come down
        if result is None and NTSCRAPER_AVAILABLE and not self._nitter_globally_down:
            result = self._analyze_nitter(ticker, token_name, retries=3)
            if result is None and not self._nitter_scraper:
                # Nessuna istanza disponibile dopo il tentativo → marca down per la sessione
                self._nitter_globally_down = True
                log.warning("[Social] Tutte le istanze Nitter down — uso score neutro per questa sessione.")

        # 3. Score neutro
        if result is None:
            result = {"social_score": 25.0, "tweet_count": 0, "source": "unavailable"}

        self._cache[cache_key] = (datetime.now(), result)
        log.debug(f"[Social] {ticker}: score={result['social_score']:.0f} "
                  f"({result['tweet_count']} tweet, src={result['source']})")
        return result

    # ── twscrape ───────────────────────────────────────────────────────────

    def _analyze_twscrape(self, ticker: str, token_name: str) -> Optional[dict]:
        """Analisi via twscrape (Twitter interno).

        Usa asyncio.wait_for per imporre un timeout massimo di 8s per fetch,
        così un account bloccato non congela il ciclo principale.
        """
        import asyncio
        query  = f"${ticker} lang:en -is:retweet"
        limit  = 50
        tweets = []

        async def _fetch():
            async for tw in self._twscrape_api.search(query, limit=limit):
                tweets.append(tw)

        async def _fetch_with_timeout(coro, seconds=8):
            try:
                await asyncio.wait_for(coro, timeout=seconds)
            except asyncio.TimeoutError:
                raise Exception("twscrape timeout")

        try:
            self._twscrape_loop.run_until_complete(_fetch_with_timeout(_fetch()))
        except Exception as e:
            err = str(e).lower()
            if "no account" in err or "timeout" in err or "queue" in err:
                # Rilancia come NoAccountError in modo che get_social_score
                # possa catturarlo e disabilitare twscrape immediatamente
                raise Exception(f"no account available: {e}")
            log.debug(f"[Social/twscrape] fetch error: {e}")
            return None

        if not tweets and token_name:
            async def _fetch2():
                async for tw in self._twscrape_api.search(
                    f"{token_name} crypto lang:en -is:retweet", limit=30
                ):
                    tweets.append(tw)
            try:
                self._twscrape_loop.run_until_complete(
                    _fetch_with_timeout(_fetch2(), seconds=6)
                )
            except Exception:
                pass

        if not tweets:
            return None

        scores = []
        for tw in tweets:
            text = (tw.rawContent or "").lower()
            if self._is_spam_text(text):
                continue
            eng = tw.likeCount + tw.retweetCount * 2 + tw.replyCount
            scores.append(min(eng, 500))

        if not scores:
            return {"social_score": 5.0, "tweet_count": len(tweets), "source": "twscrape_allspam"}

        return self._compute_score(scores, len(tweets), "twscrape")

    # ── ntscraper / Nitter ─────────────────────────────────────────────────

    def _analyze_nitter(self, ticker: str, token_name: str,
                        retries: int = 3) -> Optional[dict]:
        """Analisi via ntscraper con rotazione automatica istanze Nitter."""
        if self._nitter_scraper is None:
            if not self._rotate_nitter():
                return None

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
                    return {"social_score": 10.0, "tweet_count": 0, "source": "nitter_no_results"}

                scores = []
                for tw in tweets:
                    if not isinstance(tw, dict):
                        continue
                    text = (tw.get("text") or tw.get("content") or "").lower()
                    if self._is_spam_text(text):
                        continue
                    stats   = tw.get("stats", {}) or {}
                    likes   = int(stats.get("likes", 0) or 0)
                    rts     = int(stats.get("retweets", 0) or 0)
                    replies = int(stats.get("replies", 0) or 0)
                    scores.append(min(likes + rts * 2 + replies, 500))

                if not scores:
                    return {"social_score": 5.0, "tweet_count": len(tweets),
                            "source": "nitter_allspam"}

                return self._compute_score(scores, len(tweets), "nitter")

            except Exception as e:
                log.warning(f"[Social/Nitter] Attempt {attempt+1}/{retries} fallito: {e}. "
                            f"Rotazione istanza...")
                # Segna corrente come fallita e ruota
                current = next(
                    (i["host"] for i in self._nitter_instances if i["ok"]), None
                )
                if current:
                    self._mark_nitter_failed(current)
                if not self._rotate_nitter():
                    log.warning("[Social/Nitter] Nessuna istanza disponibile.")
                    return None
                time.sleep(1)

        return None

    # ── Utilities ──────────────────────────────────────────────────────────

    def _compute_score(self, scores: list, total_tweets: int, source: str) -> dict:
        """Calcola il social score finale da una lista di engagement."""
        avg  = sum(scores) / len(scores)
        raw  = min(avg / 2.0, 100.0)          # 200 eng → 100 score
        bonus = min(len(scores) / 5.0, 10.0)  # più tweet validi → bonus
        final = min(raw + bonus, 100.0)
        return {
            "social_score": round(final, 1),
            "tweet_count":  len(scores),
            "source":       source,
        }

    def _is_spam_text(self, text: str) -> bool:
        """True se il testo sembra spam/pump."""
        if text.count("http") >= 2:
            return True
        for kw in self.SPAM_KEYWORDS:
            if kw.lower() in text:
                return True
        return False

# ==============================================================================
# SEZIONE 6 – DEFILLAMA (DefiLlamaFetcher)
# ==============================================================================

class DefiLlamaFetcher:
    """
    Recupera TVL da DefiLlama per arricchire il profilo di un token.
    Opzionale: se il token non è su DefiLlama, TVL = 0.
    """

    def __init__(self):
        self._protocol_cache: dict[str, float] = {}
        self._search_cache:   dict[str, Optional[str]] = {}

    def get_tvl(self, token_symbol: str, token_address: str = "") -> float:
        """Ritorna TVL in USD oppure 0.0 se non trovato."""
        sym_lower = token_symbol.lower()
        if sym_lower in self._protocol_cache:
            return self._protocol_cache[sym_lower]

        protocol_slug = self._find_protocol(sym_lower)
        if not protocol_slug:
            self._protocol_cache[sym_lower] = 0.0
            return 0.0

        tvl = self._fetch_protocol_tvl(protocol_slug)
        self._protocol_cache[sym_lower] = tvl
        return tvl

    def _find_protocol(self, symbol: str) -> Optional[str]:
        """Cerca il protocollo su DefiLlama per nome/simbolo."""
        if symbol in self._search_cache:
            return self._search_cache[symbol]

        resp = _safe_get(f"{DEFILLAMA_BASE}/protocols", label="DefiLlama/protocols",
                         timeout=10)
        if resp is None:
            self._search_cache[symbol] = None
            return None
        try:
            protocols = resp.json()
            for p in protocols:
                slug = p.get("slug", "")
                name = p.get("name", "").lower()
                sym  = p.get("symbol", "").lower()
                if sym == symbol or name == symbol:
                    self._search_cache[symbol] = slug
                    return slug
        except Exception:
            pass
        self._search_cache[symbol] = None
        return None

    def _fetch_protocol_tvl(self, slug: str) -> float:
        resp = _safe_get(f"{DEFILLAMA_BASE}/tvl/{slug}", label="DefiLlama/tvl", timeout=10)
        if resp is None:
            return 0.0
        try:
            return float(resp.json())
        except Exception:
            return 0.0

# ==============================================================================
# SEZIONE 7 – DEXSCREENER (fetch dati pair/token)
# ==============================================================================

# Rate limiter globale DexScreener: max 1 richiesta ogni 0.8s (≈75 req/min)
_dex_lock      = threading.Lock()
_dex_last_call = 0.0
_DEX_MIN_INTERVAL = 0.8   # secondi tra una call e l'altra

def _dex_rate_limit():
    """Blocca il thread corrente finché non è trascorso _DEX_MIN_INTERVAL dall'ultima call."""
    global _dex_last_call
    with _dex_lock:
        now  = time.time()
        wait = _DEX_MIN_INTERVAL - (now - _dex_last_call)
        if wait > 0:
            time.sleep(wait)
        _dex_last_call = time.time()

def fetch_dexscreener_token(token_address: str, chain: str) -> Optional[dict]:
    """
    Recupera dati pair da DexScreener per un token specifico.
    Ritorna il pair con liquidità maggiore o None.
    """
    _dex_rate_limit()
    dex_chain = CHAINS.get(chain, {}).get("dexscreener_id", chain)
    url  = f"{DEXSCREENER_BASE}/latest/dex/tokens/{token_address}"
    resp = _safe_get(url, label=f"DexScreener/{chain}")
    if resp is None:
        return None
    try:
        pairs = resp.json().get("pairs") or []
        # Filtra per chain e prendi la pair con più liquidità
        chain_pairs = [
            p for p in pairs
            if (p.get("chainId") or "").lower() == dex_chain.lower()
        ]
        if not chain_pairs:
            chain_pairs = pairs  # fallback
        if not chain_pairs:
            return None
        return max(chain_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
    except Exception as e:
        log.debug(f"[DexScreener] Errore parsing {token_address}: {e}")
        return None


def fetch_dexscreener_boosted(chain: str, limit: int = 30) -> list[dict]:
    """
    Recupera i token con 'boost' recente da DexScreener (endpoint gratuito).
    Usato come fonte alternativa quando Dune non restituisce token sufficienti.
    Endpoint: GET /token-profiles/latest/v1
    """
    _dex_rate_limit()
    url  = f"{DEXSCREENER_BASE}/token-profiles/latest/v1"
    resp = _safe_get(url, label="DexScreener/boosted")
    if resp is None:
        return []
    try:
        data  = resp.json()
        items = data if isinstance(data, list) else []
        dex_chain = CHAINS.get(chain, {}).get("dexscreener_id", chain)
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
        log.info(f"[DexBoosted] {len(result)} token boosted su {chain}.")
        return result
    except Exception as e:
        log.debug(f"[DexBoosted] Errore: {e}")
        return []


def fetch_dexscreener_trending(chain: str, limit: int = 20) -> list[dict]:
    """
    Recupera i token trending/ad alto volume da DexScreener.
    Usato come fonte supplementare quando Dune porta pochi risultati.
    """
    _dex_rate_limit()
    dex_chain = CHAINS.get(chain, {}).get("dexscreener_id", chain)
    # Cerca token recenti con alto volume sulla chain
    url  = f"{DEXSCREENER_BASE}/latest/dex/search"
    resp = _safe_get(url, params={"q": dex_chain}, label="DexScreener/trending_search")
    if resp is None:
        return []
    try:
        pairs = resp.json().get("pairs") or []
        # Filtra per chain e ordina per volume 1h decrescente
        chain_pairs = [
            p for p in pairs
            if (p.get("chainId") or "").lower() == dex_chain.lower()
        ]
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
                "inflow_usd":          vol1h * 0.3,   # stima: ~30% vol1h è inflow netto
                "inflow_wallet_count": max(buys // 10, 1),
                "avg_wallet_pnl_pct":  0,
                "source":              "dexscreener_trending",
            })
        log.info(f"[DexTrending] {len(result)} token trending su {chain}.")
        return result
    except Exception as e:
        log.debug(f"[DexTrending] Errore: {e}")
        return []


def fetch_dexscreener_search(query: str, chain: str) -> list[dict]:
    """Cerca token per simbolo/nome su DexScreener."""
    _dex_rate_limit()
    url  = f"{DEXSCREENER_BASE}/latest/dex/search"
    resp = _safe_get(url, params={"q": query}, label="DexScreener/search")
    if resp is None:
        return []
    try:
        pairs = resp.json().get("pairs") or []
        dex_chain = CHAINS.get(chain, {}).get("dexscreener_id", chain)
        return [
            p for p in pairs
            if (p.get("chainId") or "").lower() == dex_chain.lower()
        ]
    except Exception:
        return []

def parse_dexscreener_pair(pair: dict, chain: str) -> dict:
    """Estrae i campi rilevanti da un pair DexScreener."""
    base_token = pair.get("baseToken") or {}
    price_usd  = float(pair.get("priceUsd") or 0)
    liq        = pair.get("liquidity") or {}
    vol        = pair.get("volume")    or {}
    price_ch   = pair.get("priceChange") or {}
    txns_1h    = (pair.get("txns") or {}).get("h1") or {}
    # Preferisce marketCap (circolante) rispetto a fdv (fully diluted).
    # fdv è spesso gonfiato (es. BILL $370M fdv ma mcap circolante molto minore).
    fdv        = float(pair.get("marketCap") or pair.get("fdv") or 0)
    age_ms     = pair.get("pairCreatedAt")
    age_hours  = 0.0
    if age_ms:
        try:
            age_hours = (time.time() - age_ms / 1000) / 3600
        except Exception:
            pass

    buys  = int(txns_1h.get("buys", 0) or 0)
    sells = int(txns_1h.get("sells", 0) or 0)
    bsr   = buys / sells if sells > 0 else (2.0 if buys > 0 else 1.0)

    return {
        "token_address":    base_token.get("address", ""),
        "token_symbol":     base_token.get("symbol", ""),
        "token_name":       base_token.get("name", ""),
        "chain":            chain,
        "pair_address":     pair.get("pairAddress", ""),
        "dex_id":           pair.get("dexId", ""),
        "price_usd":        price_usd,
        "market_cap_usd":   fdv,
        "liquidity_usd":    float(liq.get("usd", 0) or 0),
        "volume_5m_usd":    float(vol.get("m5", 0) or 0),
        "volume_1h_usd":    float(vol.get("h1", 0) or 0),
        "volume_6h_usd":    float(vol.get("h6", 0) or 0),
        "volume_24h_usd":   float(vol.get("h24", 0) or 0),
        "change_5m_pct":    float(price_ch.get("m5", 0) or 0),
        "change_1h_pct":    float(price_ch.get("h1", 0) or 0),
        "change_6h_pct":    float(price_ch.get("h6", 0) or 0),
        "change_24h_pct":   float(price_ch.get("h24", 0) or 0),
        "txns_1h_buys":     buys,
        "txns_1h_sells":    sells,
        "buy_sell_ratio_1h": round(bsr, 3),
        "pair_age_hours":   round(age_hours, 2),
    }

# ==============================================================================
# SEZIONE 8 – GOPLUS SECURITY (solo EVM: BSC, Base)
# ==============================================================================

def fetch_goplus_security(token_address: str, chain: str) -> dict:
    return {}  # GoPlus disabilitato
    chain_meta = CHAINS.get(chain, {})
    if not chain_meta.get("is_evm"):
        return {"is_evm": False}  # Solana non supportata da GoPlus

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
    except Exception as e:
        log.debug(f"[GoPlus] Errore parsing {token_address}: {e}")
        return {}

# ==============================================================================
# SEZIONE 9 – GEM AGGREGATOR
# ==============================================================================

def build_gem_profile(
    dune_token: dict,
    social_analyzer: SocialAnalyzer,
    defillama:      DefiLlamaFetcher,
) -> Optional[dict]:
    """
    Costruisce il profilo completo di una potenziale gemma aggregando:
    Dune (smart money) + DexScreener (mercato) + Social + TVL + GoPlus (sicurezza EVM)
    + Momentum Analysis (Early Metrics)
    """
    chain   = dune_token.get("chain", "")
    address = dune_token.get("token_address", "")
    symbol  = dune_token.get("token_symbol", "")

    # 1. DexScreener
    pair = fetch_dexscreener_token(address, chain)
    if pair is None and symbol:
        pairs = fetch_dexscreener_search(symbol, chain)
        pair  = pairs[0] if pairs else None

    if pair is None:
        log.info(f"[Gem] {symbol} ({chain}): nessun pair su DexScreener — skip.")
        return None

    market = parse_dexscreener_pair(pair, chain)

    # 2. Social score — con timeout hard (ntscraper/twscrape possono bloccarsi)
    try:
        from concurrent.futures import ThreadPoolExecutor as _SocExec, TimeoutError as _SocTimeout
        with _SocExec(max_workers=1) as _sex:
            _sfut = _sex.submit(
                social_analyzer.get_social_score, symbol, dune_token.get("token_name", "")
            )
            try:
                social = _sfut.result(timeout=20)
            except _SocTimeout:
                log.warning(f"[Social] {symbol}: timeout 20s — social score = 0")
                social = {"social_score": 0.0, "tweet_count": 0, "source": "timeout"}
    except Exception as _se:
        log.debug(f"[Social] {symbol}: errore — {_se}")
        social = {"social_score": 0.0, "tweet_count": 0, "source": "error"}

    # 3. TVL (DefiLlama) — caricato solo dopo il filtro qualità (lazy)
    tvl = 0.0

    # 4. GoPlus (sicurezza, solo EVM)
    security = fetch_goplus_security(address, chain)

    # 5. MOMENTUM ANALYSIS (Integrazione della nuova funzione)
    # Recuperiamo il numero di wallet attuali da Dune
    wallet_count = dune_token.get("inflow_wallet_count", 0)
    
    # Eseguiamo il calcolo (Assicurati che compute_early_metrics sia definita nel file)
    wg, wa, p_age, momentum_score = compute_early_metrics(address, wallet_count, pair)

    # 6. Assembla profilo
    mcap = market.get("market_cap_usd", 0)
    liq  = market.get("liquidity_usd", 0)
    inflow = dune_token.get("inflow_usd", 0)
    
    profile = {
        # Identità
        "token_address":    address,
        "token_symbol":     symbol,
        "token_name":       dune_token.get("token_name", market.get("token_name", "")),
        "chain":            chain,
        "pair_address":     market.get("pair_address", ""),
        "dex_id":           market.get("dex_id", ""),

        # Smart money (Dune)
        "inflow_usd":               inflow,
        "inflow_wallet_count":      wallet_count,
        # avg_wallet_pnl_pct viene da Dune come "avg_wallet_pnl_pct" o "avg_pnl_pct".
        # Se assente usa proxy: inflow medio per wallet (lineare $0→0% $50k→25% $200k→100%).
        # Fix Dune SQL: aggiungi AVG(realized_pnl_pct) AS avg_wallet_pnl_pct nel SELECT.
        "avg_wallet_pnl_pct":       (lambda _rp=float(dune_token.get("avg_wallet_pnl_pct") or
                                         dune_token.get("avg_pnl_pct") or 0),
                                         _nw=max(int(dune_token.get("inflow_wallet_count",1) or 1),1):
                                     _rp if _rp > 0 else min((inflow / _nw) / 2000, 100.0))(),
        "inflow_to_mcap_ratio":     inflow / mcap if mcap > 0 else 0,

        # --- SEZIONE MOMENTUM (Dati aggiunti ora) ---
        "wallet_growth_rate":       wg,             # Velocità di ingresso wallet
        "wallet_acceleration":      wa,             # Accelerazione (FOMO indicator)
        "momentum_score":           momentum_score, # Punteggio sintetico (0-10)
        "calculated_pair_age_h":    p_age,          # Età ricalcolata
        # --------------------------------------------

        # Social
        "social_score":      social.get("social_score", 25.0),
        "social_tweet_count": social.get("tweet_count", 0),
        "social_source":     social.get("source", ""),

        # TVL
        "tvl_usd":           tvl,
        "tvl_to_mcap_ratio": tvl / mcap if mcap > 0 else 0,

        # Mercato
        "price_usd":         market.get("price_usd", 0),
        "market_cap_usd":    mcap,
        "liquidity_usd":     liq,
        "volume_1h_usd":     market.get("volume_1h_usd", 0),
        "change_1h_pct":     market.get("change_1h_pct", 0),
        "pair_age_hours":    market.get("pair_age_hours", 0), # Dato originale DexScreener
        "liquidity_to_mcap_ratio":   liq / mcap if mcap > 0 else 0,

        # Sicurezza (EVM)
        "is_honeypot":  security.get("is_honeypot", 0),
        "buy_tax":      security.get("buy_tax", 0),
        "sell_tax":     security.get("sell_tax", 0),

        # Feature ML derivate (Assicurati di aver importato numpy as np)
        "market_cap_usd_log":  np.log10(max(mcap, 1)),
        "liquidity_usd_log":   np.log10(max(liq, 1)),

        # Metadati
        "source":       dune_token.get("source", "dune"),
        "profile_ts":   datetime.now().isoformat(),
    }
    
    profile["gem_class"] = classify_gem(profile)
    
    return profile

# ==============================================================================
# SEZIONE 10 – FILTRI QUALITÀ (GemFilter)
# ==============================================================================

class GemFilter:
    """
    Applica filtri hard sul profilo di una gemma.
    Ritorna (passed: bool, reason: str).
    """

    def check(self, p: dict) -> tuple[bool, str]:
        sym   = p.get("token_symbol", "?")
        chain = p.get("chain", "?")

        # Simbolo non-ASCII (segnale di bassa qualità / token clone)
        if sym and not sym.replace(" ", "").replace("_", "").replace("-", "").isascii():
            return False, f"simbolo non-ASCII: {sym}"

        # ── Market cap ────────────────────────────────────────────────────────
        # usa marketCap circolante (non fdv gonfiato).
        # Fallback: se mcap=0 usa liq*3 come stima minimale conservative
        # (evita di scartare token Solana nuovi dove DexScreener non ha supply)
        mcap = p.get("market_cap_usd", 0) or 0
        if mcap <= 0:
            liq_est = p.get("liquidity_usd", 0) or 0
            if liq_est > 0:
                mcap = liq_est * 3   # stima molto conservativa
                p["market_cap_usd"] = mcap
                log.debug(f"[filtro] {sym} mcap=0 → stima da liq: ${mcap:,.0f}")
            else:
                return False, "market_cap non disponibile (0 o nullo)"
        if mcap < FILTER_CONFIG["MIN_MARKET_CAP_USD"]:
            return False, f"market_cap ${mcap:,.0f} < min ${FILTER_CONFIG['MIN_MARKET_CAP_USD']:,.0f}"
        if mcap > FILTER_CONFIG["MAX_MARKET_CAP_USD"]:
            return False, f"market_cap ${mcap:,.0f} > max ${FILTER_CONFIG['MAX_MARKET_CAP_USD']:,.0f}"

        # ── Tier basato sul market cap ────────────────────────────────────────
        # Tier 1: $200k–$10M  → filtri standard (inflow/mcap ≥ 1%)
        # Tier 2: $10M–$50M   → inflow/mcap ≥ 3%, BSR ≥ 1.3 (se disponibile), età ≤ 120h
        # Tier 3: $50M–$100M  → inflow/mcap ≥ 5%, BSR ≥ 1.8 (se disponibile), età ≤ 72h, vol/liq ≥ 15%
        #
        # NOTA: BSR = 1.0 esatto è il default quando DexScreener non restituisce
        # dati txns (es. molti token Solana). In quel caso il filtro BSR è saltato
        # e si usa change_1h_pct come proxy del momentum.

        inflow    = p.get("inflow_usd", 0)
        n_wallets = p.get("inflow_wallet_count", 0)
        bsr       = p.get("buy_sell_ratio_1h", 1.0)
        age       = p.get("pair_age_hours", 0)
        liq       = p.get("liquidity_usd", 0)
        vol1h     = p.get("volume_1h_usd", 0)
        change_1h = p.get("change_1h_pct", 0)

        # BSR è reale solo se abbiamo dati txns effettivi
        buys  = p.get("txns_1h_buys", 0)
        sells = p.get("txns_1h_sells", 0)
        bsr_available = (buys + sells) > 0

        inflow_to_mcap = inflow / mcap if mcap > 0 else 0
        vol_to_liq     = vol1h / liq   if liq  > 0 else 0

        # ── BSR globale: se abbiamo dati txns reali e BSR < 0.1 → solo sell, dump in corso ──
        # BSR=0 con buys>0 è impossibile; BSR=0 con buys=0 e sells>0 = zero acquirenti.
        # Questo blocca i fake pump (prezzo su ma nessuno compra).
        if bsr_available and bsr < 0.1:
            return False, (f"BSR {bsr:.2f} — nessun acquirente attivo "
                           f"(buys={buys}, sells={sells}): possibile dump/fake pump")

        # ── BSR equilibrio 1.0-1.2 → zona pericolosa ─────────────────────────
        # Analisi su 466 token reali: BSR 1.0-1.2 ha 52.9% bad rate (<-10%).
        # Quando compratori e venditori sono quasi pari senza dominanza bullish,
        # il token tende al dump. BSR=1.0 esatto con buys=sells=0 è il default
        # quando DexScreener non ha dati txns → filtriamo solo se bsr_available.
        if bsr_available and 1.0 <= bsr < 1.2:
            return False, (f"BSR {bsr:.2f} in zona equilibrio (1.0–1.2): "
                           f"52% bad rate storico — nessuna dominanza acquirenti")

        if mcap > 50_000_000:           # ── Tier 3: $50M–$100M ──
            if inflow_to_mcap < 0.03:   # abbassato da 5% → 3%
                return False, (f"tier3: inflow/mcap {inflow_to_mcap:.1%} < 3% "
                               f"(inflow=${inflow:,.0f} su mcap=${mcap:,.0f})")
            if bsr_available:
                if bsr < 1.2:           # abbassato da 1.8 → 1.2
                    return False, f"tier3: BSR {bsr:.2f} < 1.2 richiesto sopra $50M"
            else:
                if change_1h < -10:     # allentato da -5 → -10
                    return False, f"tier3: no txns data, prezzo {change_1h:+.1f}% in 1h"
            if age > 96:                # alzato da 72h → 96h
                return False, f"tier3: pair troppo vecchio ({age:.0f}h > 96h) per mcap $50M+"
            if vol_to_liq < 0.05:       # abbassato da 15% → 5%
                return False, f"tier3: vol/liq {vol_to_liq:.1%} < 5% — trading insufficiente"

        elif mcap > 10_000_000:         # ── Tier 2: $10M–$50M ──
            if inflow_to_mcap < 0.02:   # abbassato da 3% → 2%
                return False, (f"tier2: inflow/mcap {inflow_to_mcap:.1%} < 2% "
                               f"(inflow=${inflow:,.0f} su mcap=${mcap:,.0f})")
            if bsr_available:
                if bsr < 1.0:           # abbassato da 1.3 → 1.0
                    return False, f"tier2: BSR {bsr:.2f} < 1.0 richiesto sopra $10M"
            else:
                if change_1h < -10:     # allentato da -5 → -10
                    return False, f"tier2: no txns data, prezzo {change_1h:+.1f}% in 1h"
            if age > 144:               # alzato da 120h → 144h
                return False, f"tier2: pair troppo vecchio ({age:.0f}h > 144h) per mcap $10M+"

        # ── Liquidità per-chain (Tier 1, 2, 3) ───────────────────────────────
        chain_meta = CHAINS.get(chain, {})
        min_liq    = chain_meta.get("min_liquidity", FILTER_CONFIG["MIN_LIQUIDITY_USD"])
        if liq < min_liq:
            return False, f"liquidity ${liq:,.0f} < min ${min_liq:,.0f} ({chain})"

        # ── Volume minimo ─────────────────────────────────────────────────────
        if vol1h < FILTER_CONFIG["MIN_VOLUME_1H_USD"]:
            return False, f"vol1h ${vol1h:,.0f} < min"

        # ── Volume 5min spike (early pump signal) ─────────────────────────────
        # vol5m >= 20% del vol1h → attività concentrata negli ultimi 5 min.
        # Non blocca, ma arricchisce il profilo con vol5m_spike per il ML.
        vol5m = p.get("volume_5m_usd", 0)
        if vol1h > 0 and vol5m > 0:
            v5_ratio = vol5m / vol1h
            p["vol5m_spike"] = round(v5_ratio, 4)
            if v5_ratio >= 0.20:
                log.debug(f"[filtro] 🔥 {sym} vol5m spike: "
                          f"${vol5m:,.0f} = {v5_ratio:.0%} del vol1h")
        else:
            p["vol5m_spike"] = 0.0

        # ── Social score (Early Adopter Rule) ─────────────────────────────────
        ss        = p.get("social_score", 25.0)
        ss_source = p.get("social_source", "unavailable")
        if ss_source != "unavailable":
            if ss < FILTER_CONFIG["MIN_SOCIAL_SCORE"]:
                return False, f"social_score {ss:.0f} < {FILTER_CONFIG['MIN_SOCIAL_SCORE']}"
            if ss > FILTER_CONFIG["MAX_SOCIAL_SCORE"]:
                return False, f"social_score {ss:.0f} > {FILTER_CONFIG['MAX_SOCIAL_SCORE']} (iper-hype)"

        # ── Età pair (Tier 1 — gli altri tier usano già soglie più basse) ────
        if age < FILTER_CONFIG["MIN_PAIR_AGE_HOURS"]:
            return False, f"pair troppo giovane ({age:.1f}h)"
        if mcap <= 10_000_000 and age > FILTER_CONFIG["MAX_PAIR_AGE_HOURS"]:
            return False, f"pair troppo vecchio ({age:.0f}h)"

        # ── Smart money Tier 1 ($500k–$10M) ─────────────────────────────────
        # Controlla sia importo assoluto sia rapporto inflow/mcap.
        # Il rapporto cattura token con inflow piccolo in assoluto ma
        # enorme rispetto alla cap (es. $50k su $200k mcap = 25%).
        _no_inflow_src = {"mock_dune", "dexscreener_boosted", "dexscreener_trending"}
        if p.get("source") not in _no_inflow_src and mcap <= 10_000_000:
            if inflow < FILTER_CONFIG["MIN_INFLOW_USD"]:
                return False, f"smart money inflow ${inflow:,.0f} < min"
            if n_wallets < FILTER_CONFIG["MIN_SMART_WALLETS"]:
                return False, f"solo {n_wallets} wallet smart-money"
            tier1_ratio_min = FILTER_CONFIG.get("MIN_INFLOW_TO_MCAP_TIER1", 0.005)
            if inflow_to_mcap < tier1_ratio_min:
                return False, (f"tier1: inflow/mcap {inflow_to_mcap:.1%} < "
                               f"{tier1_ratio_min:.0%} "
                               f"(inflow=${inflow:,.0f} su mcap=${mcap:,.0f})")

        # ── Sicurezza EVM ─────────────────────────────────────────────────────
        if p.get("is_honeypot"):
            return False, "HONEYPOT rilevato da GoPlus"
        if p.get("buy_tax", 0) > FILTER_CONFIG["MAX_BUY_TAX_PCT"]:
            return False, f"buy_tax {p['buy_tax']:.0f}% > max"
        if p.get("sell_tax", 0) > FILTER_CONFIG["MAX_SELL_TAX_PCT"]:
            return False, f"sell_tax {p['sell_tax']:.0f}% > max"

        # ── Liquidità massima (NUOVO) ─────────────────────────────────────────
        max_liq = FILTER_CONFIG.get("MAX_LIQUIDITY_USD", 500_000)
        if liq > max_liq:
            return False, f"liquidity ${liq:,.0f} > max ${max_liq:,.0f} (token stabilizzato)"

        # ── Change 1h massima (NUOVO) ─────────────────────────────────────────
        max_ch1h = FILTER_CONFIG.get("MAX_CHANGE_1H_PCT", 10.0)
        min_ch1h = FILTER_CONFIG.get("MIN_CHANGE_1H_PCT", -30.0)
        if change_1h > max_ch1h:
            return False, f"change_1h {change_1h:+.1f}% > {max_ch1h:.0f}% (pump già avvenuto)"
        if change_1h < min_ch1h:
            return False, f"change_1h {change_1h:+.1f}% < {min_ch1h:.0f}% (dump massiccio)"

        # ── Cooldown + Blacklist dinamica (NUOVO) ─────────────────────────────
        pair = p.get("pair_address", p.get("token_address", ""))
        if _gem_blacklisted(pair):
            return False, f"{sym} in blacklist (dump massiccio rilevato nei followup)"
        if not _gem_cooldown_ok(pair):
            mins = FILTER_CONFIG.get("TOKEN_COOLDOWN_MIN", 120)
            return False, f"{sym} in cooldown ({mins}min tra segnali sullo stesso token)"

        tier = ("tier3" if mcap > 50_000_000 else
                "tier2" if mcap > 10_000_000 else "tier1")
        log.debug(f"[filtro] ✅ {sym} ({chain}) [{tier}] — "
                  f"mcap=${mcap:,.0f} | inflow/mcap={inflow_to_mcap:.1%} | "
                  f"BSR={bsr:.2f} | età={age:.0f}h | ch1h={change_1h:+.1f}%")
        return True, "ok"

# ==============================================================================
# SEZIONE 11 – FEATURE ENGINEERING
# ==============================================================================

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """Prepara il DataFrame per il modello ML."""
    df = df.copy()

    for col in FEATURE_COLUMNS:
        if col not in df.columns:
            df[col] = 0.0

    # Gestisci infiniti e NaN
    df[FEATURE_COLUMNS] = df[FEATURE_COLUMNS].replace(
        [np.inf, -np.inf], np.nan
    ).fillna(0.0)

    return df


def auto_label_from_csvs() -> pd.DataFrame:
    """
    Etichetta automaticamente i CSV reali per il training ML.

    Legge:
      - reports/gems_log.csv  + reports/gems_followup.csv  (bot attuale)
      - reports/signals_log.csv + reports/price_followup.csv (vecchio bot)

    Label: peak Δ% >= PUMP_REAL_THRESHOLD_PCT (default 30%) → gem=1 else 0.
    """
    threshold = ML_CONFIG["PUMP_REAL_THRESHOLD_PCT"]

    def _read_peaks(followup_path: str, id_col: str) -> dict:
        peaks = {}
        p = Path(followup_path)
        if not p.exists():
            return peaks
        try:
            with p.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    gid    = row.get(id_col, "")
                    chg    = row.get("change_pct", "")
                    status = row.get("status", "")
                    if not gid or chg == "":
                        continue
                    if status not in ("ok", "ok_low_liq", "recovered_historical"):
                        continue
                    try:
                        peaks[gid] = max(peaks.get(gid, -999.0), float(chg))
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            log.warning(f"[auto_label] Errore {followup_path}: {e}")
        return peaks

    def _row_to_features(row: dict, peak: float) -> Optional[dict]:
        try:
            mcap  = float(row.get("market_cap_usd", 0) or 0)
            liq   = float(row.get("liquidity_usd",  0) or 0)
            if mcap <= 0 or liq <= 0:
                return None
            inflow = float(row.get("inflow_usd", 0) or 0)
            vol1h  = float(row.get("volume_1h_usd", 0) or 0)
            return {
                "inflow_usd":               inflow,
                "inflow_wallet_count":      float(row.get("inflow_wallet_count", 0) or 0),
                "avg_wallet_pnl_pct":       float(row.get("avg_wallet_pnl_pct", 0) or 0),
                "inflow_to_mcap_ratio":     inflow / max(mcap, 1),
                "social_score":             float(row.get("social_score", 25) or 25),
                "social_tweet_count":       float(row.get("social_tweet_count", 0) or 0),
                "tvl_usd":                  float(row.get("tvl_usd", 0) or 0),
                "tvl_to_mcap_ratio":        float(row.get("tvl_usd", 0) or 0) / max(mcap, 1),
                "market_cap_usd_log":       np.log10(max(mcap, 1)),
                "liquidity_usd_log":        np.log10(max(liq, 1)),
                "volume_1h_usd":            vol1h,
                "volume_5m_usd":            float(row.get("volume_5m_usd", 0) or 0),
                "buy_sell_ratio_1h":        float(row.get("buy_sell_ratio_1h", 1) or 1),
                "change_5m_pct":            float(row.get("change_5m_pct", 0) or 0),
                "change_1h_pct":            float(row.get("change_1h_pct", 0) or 0),
                "change_6h_pct":            float(row.get("change_6h_pct", 0) or 0),
                "pair_age_hours":           float(row.get("pair_age_hours", 0) or 0),
                "liquidity_to_mcap_ratio":  liq / max(mcap, 1),
                "volume_to_liquidity_ratio": vol1h / max(liq, 1),
                "txns_1h_buys":             float(row.get("txns_1h_buys", 0) or 0),
                "txns_1h_sells":            float(row.get("txns_1h_sells", 0) or 0),
                "target":                   int(peak >= threshold),
            }
        except Exception as e:
            log.debug(f"[auto_label] parse error: {e}")
            return None

    rows = []

    # 1. Bot attuale: gems_log + gems_followup
    # GEM_TRACKER_CONFIG è in gem_tracker.py — usa try/get per evitare NameError
    try:
        _gtcfg = GEM_TRACKER_CONFIG
    except NameError:
        _gtcfg = {}
    peaks_gems = _read_peaks(
        _gtcfg.get("FOLLOWUP_CSV", "reports/gems_followup.csv"), "gem_id"
    )
    gems_csv = _gtcfg.get("GEMS_CSV", "reports/gems_log.csv")
    if Path(gems_csv).exists() and peaks_gems:
        try:
            with open(gems_csv, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    gid  = row.get("gem_id", "")
                    peak = peaks_gems.get(gid)
                    if peak is not None:
                        feat = _row_to_features(row, peak)
                        if feat:
                            rows.append(feat)
        except Exception as e:
            log.warning(f"[auto_label] {gems_csv}: {e}")

    # 2. Vecchio bot: signals_log + price_followup
    peaks_old = _read_peaks(
        ML_CONFIG.get("PRICE_FOLLOWUP_CSV", "reports/price_followup.csv"), "signal_id"
    )
    signals_csv = ML_CONFIG.get("SIGNALS_CSV", "reports/signals_log.csv")
    if Path(signals_csv).exists() and peaks_old:
        try:
            with open(signals_csv, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    sid  = row.get("signal_id", "")
                    peak = peaks_old.get(sid)
                    if peak is None:
                        continue
                    # Mappa campi vecchio bot → formato corrente
                    mapped = {
                        "market_cap_usd":     row.get("market_cap_usd", 0),
                        "liquidity_usd":      row.get("liquidity_usd", 0),
                        "inflow_usd":         0,
                        "inflow_wallet_count": 0,
                        "avg_wallet_pnl_pct": 0,
                        "social_score":       25,
                        "social_tweet_count": 0,
                        "tvl_usd":            0,
                        "volume_1h_usd":      row.get("volume_1h_usd", 0),
                        "volume_5m_usd":      0,
                        "buy_sell_ratio_1h":  row.get("buy_sell_ratio_1h", 1),
                        "change_5m_pct":      0,
                        "change_1h_pct":      row.get("change_1h_pct", 0),
                        "change_6h_pct":      0,
                        "pair_age_hours":     0,
                        "txns_1h_buys":       0,
                        "txns_1h_sells":      0,
                    }
                    feat = _row_to_features(mapped, peak)
                    if feat:
                        rows.append(feat)
        except Exception as e:
            log.warning(f"[auto_label] {signals_csv}: {e}")

    if not rows:
        log.info("[auto_label] Nessun dato reale disponibile.")
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    n_gem = int(df["target"].sum())
    log.info(f"[auto_label] ✅ {len(df)} campioni reali | "
             f"{n_gem} gem ({n_gem/len(df)*100:.1f}%) | soglia peak ≥ {threshold:.0f}%")
    return df

def build_training_dataset(n_samples: int = 500) -> pd.DataFrame:
    """
    Costruisce un dataset di training simulato con le nuove feature.
    In produzione, sostituisci con dati reali raccolti dal DataCollector.
    """
    log.info(f"[training] Generazione dataset simulato ({n_samples} campioni)...")
    rng = np.random.default_rng(42)

    rows = []
    for _ in range(n_samples):
        # Regime token
        regime = rng.choice(["gem", "hype", "dump", "rug"], p=[0.25, 0.30, 0.30, 0.15])

        if regime == "gem":
            inflow_usd          = rng.uniform(10_000, 500_000)
            inflow_wallet_count = rng.integers(5, 40)
            avg_wallet_pnl_pct  = rng.uniform(30, 200)
            social_score        = rng.uniform(25, 65)
            social_tweet_count  = rng.integers(10, 80)
            mcap                = rng.uniform(500_000, 5_000_000)
            liq                 = rng.uniform(100_000, 1_500_000)
            vol1h               = rng.uniform(50_000, 500_000)
            bsr                 = rng.uniform(1.5, 4.0)
            change_1h           = rng.uniform(5, 40)
            # TVL realistico: la maggior parte dei micro-gem non ha TVL su DefiLlama.
            # Solo ~20% dei gem ha TVL (quelli con protocollo DeFi associato).
            tvl                 = rng.uniform(mcap * 0.5, mcap * 3) if rng.random() < 0.20 else 0
            target              = 1

        elif regime == "hype":
            inflow_usd          = rng.uniform(5_000, 100_000)
            inflow_wallet_count = rng.integers(2, 15)
            avg_wallet_pnl_pct  = rng.uniform(-10, 50)
            social_score        = rng.uniform(70, 95)  # già esploso
            social_tweet_count  = rng.integers(100, 500)
            mcap                = rng.uniform(2_000_000, 10_000_000)
            liq                 = rng.uniform(80_000, 500_000)
            vol1h               = rng.uniform(10_000, 200_000)
            bsr                 = rng.uniform(0.8, 1.5)
            change_1h           = rng.uniform(-10, 10)
            # Anche hype/dump/rug possono avere TVL (token fork di protocolli noti)
            tvl                 = rng.uniform(mcap * 0.1, mcap) if rng.random() < 0.15 else 0
            target              = 0

        elif regime == "dump":
            inflow_usd          = rng.uniform(0, 5_000)
            inflow_wallet_count = rng.integers(0, 5)
            avg_wallet_pnl_pct  = rng.uniform(-50, 0)
            social_score        = rng.uniform(5, 30)
            social_tweet_count  = rng.integers(0, 20)
            mcap                = rng.uniform(500_000, 3_000_000)
            liq                 = rng.uniform(100_000, 400_000)
            vol1h               = rng.uniform(1_000, 30_000)
            bsr                 = rng.uniform(0.3, 0.9)
            change_1h           = rng.uniform(-40, -5)
            tvl                 = rng.uniform(mcap * 0.1, mcap * 0.5) if rng.random() < 0.10 else 0
            target              = 0

        else:  # rug
            inflow_usd          = rng.uniform(50_000, 300_000)
            inflow_wallet_count = rng.integers(1, 5)
            avg_wallet_pnl_pct  = rng.uniform(200, 1000)
            social_score        = rng.uniform(40, 80)
            social_tweet_count  = rng.integers(20, 100)
            mcap                = rng.uniform(500_000, 5_000_000)
            liq                 = rng.uniform(50_000, 200_000)
            vol1h               = rng.uniform(100_000, 800_000)
            bsr                 = rng.uniform(2.0, 6.0)
            change_1h           = rng.uniform(20, 100)
            tvl                 = 0  # rug non ha TVL reale
            target              = 0

        row = {
            "inflow_usd":              inflow_usd,
            "inflow_wallet_count":     float(inflow_wallet_count),
            "avg_wallet_pnl_pct":      avg_wallet_pnl_pct,
            "inflow_to_mcap_ratio":    inflow_usd / max(mcap, 1),
            "social_score":            social_score,
            "social_tweet_count":      float(social_tweet_count),
            "tvl_usd":                 tvl,
            "tvl_to_mcap_ratio":       tvl / max(mcap, 1),
            "market_cap_usd_log":      np.log10(max(mcap, 1)),
            "liquidity_usd_log":       np.log10(max(liq, 1)),
            "volume_1h_usd":           vol1h,
            "volume_5m_usd":           vol1h / 12 * rng.uniform(0.5, 3),
            "buy_sell_ratio_1h":       bsr,
            "change_5m_pct":           change_1h / 12 * rng.uniform(0.3, 2),
            "change_1h_pct":           change_1h,
            "change_6h_pct":           change_1h * rng.uniform(0.5, 2),
            "pair_age_hours":          rng.uniform(0.5, 72),
            "liquidity_to_mcap_ratio": liq / max(mcap, 1),
            "volume_to_liquidity_ratio": vol1h / max(liq, 1),
            "txns_1h_buys":            int(vol1h / 500 * bsr),
            "txns_1h_sells":           int(vol1h / 500 / max(bsr, 0.1)),
            "target":                  target,
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    log.info(f"[training] Dataset pronto: {len(df)} righe, "
             f"{df['target'].sum()} gem ({df['target'].mean()*100:.1f}%)")
    return df

# ==============================================================================
# SEZIONE 12 – TRAINING & MODELLO ML
# ==============================================================================

def train_model(df: pd.DataFrame) -> tuple:
    """Addestra XGBoost o RandomForest.

    Strategia dati:
      1. Auto-etichetta CSV reali (gems_log + followup + vecchio bot)
      2. Oversampling dati reali (×2) + mix con simulati
      3. Se < 10 campioni reali → solo simulati
    """
    real_df = auto_label_from_csvs()
    if len(real_df) >= 10:
        real_2x = pd.concat([real_df] * 2, ignore_index=True)
        df = pd.concat([real_2x, df], ignore_index=True)
        log.info(f"[training] Mix: {len(real_df)} reali ×2 + "
                 f"{len(df) - len(real_2x)} simulati = {len(df)} tot")
    else:
        log.info(f"[training] Solo simulati (reali: {len(real_df)})")

    df = engineer_features(df)
    df = df.dropna(subset=["target"])

    if len(df) < ML_CONFIG["MIN_TRAINING_ROWS"]:
        raise ValueError(f"Dataset troppo piccolo: {len(df)} righe "
                         f"(min {ML_CONFIG['MIN_TRAINING_ROWS']})")

    X = df[FEATURE_COLUMNS].values
    y = df["target"].astype(int).values

    scaler = RobustScaler()
    X_scaled = scaler.fit_transform(X)

    X_train, X_test, y_train, y_test = train_test_split(
        X_scaled, y, test_size=ML_CONFIG["TEST_SIZE"],
        random_state=ML_CONFIG["RANDOM_STATE"], stratify=y
    )

    if XGB_AVAILABLE:
        log.info("[training] Addestramento XGBoost...")
        model = xgb.XGBClassifier(
            n_estimators=ML_CONFIG["N_ESTIMATORS"],
            max_depth=ML_CONFIG["MAX_DEPTH"],
            learning_rate=ML_CONFIG["LEARNING_RATE"],
            scale_pos_weight=(y == 0).sum() / max((y == 1).sum(), 1),
            random_state=ML_CONFIG["RANDOM_STATE"],
            eval_metric="logloss",
            verbosity=0,
        )
        # Fallback progressivo per compatibilità xgb 1.x / 2.x / 3.x
        fitted = False
        for fit_kwargs in [
            {"eval_set": [(X_test, y_test)],
             "callbacks": [xgb.callback.EarlyStopping(rounds=30, save_best=True)]},
            {"eval_set": [(X_test, y_test)], "early_stopping_rounds": 30},
            {},
        ]:
            try:
                model.fit(X_train, y_train, **fit_kwargs)
                fitted = True
                break
            except (TypeError, AttributeError):
                continue
        if not fitted:
            model.fit(X_train, y_train)
    else:
        log.info("[training] Addestramento RandomForest (XGBoost non disponibile)...")
        model = RandomForestClassifier(
            n_estimators=ML_CONFIG["N_ESTIMATORS"],
            max_depth=ML_CONFIG["MAX_DEPTH"],
            class_weight="balanced",
            random_state=ML_CONFIG["RANDOM_STATE"],
            n_jobs=-1,
        )
        model.fit(X_train, y_train)

    # Metriche
    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]
    auc    = roc_auc_score(y_test, y_prob)

    cv = StratifiedKFold(n_splits=ML_CONFIG["CV_FOLDS"], shuffle=True,
                         random_state=ML_CONFIG["RANDOM_STATE"])
    cv_scores = cross_val_score(model, X_scaled, y, cv=cv, scoring="roc_auc")

    log.info(f"[training] AUC test: {auc:.4f} | CV: {cv_scores.mean():.4f} ±{cv_scores.std():.4f}")
    log.info(f"[training]\n{classification_report(y_test, y_pred, target_names=['no_gem','gem'])}")

    # Feature importance
    if hasattr(model, "feature_importances_"):
        imp = pd.Series(model.feature_importances_, index=FEATURE_COLUMNS)
        top = imp.nlargest(5)
        log.info("[training] Top 5 feature:\n" +
                 "\n".join(f"  {f}: {v:.4f}" for f, v in top.items()))

    # Salvataggio
    joblib.dump(model,  ML_CONFIG["MODEL_PATH"])
    joblib.dump(scaler, ML_CONFIG["SCALER_PATH"])
    log.info(f"[training] ✅ Modello salvato → {ML_CONFIG['MODEL_PATH']}")

    return model, scaler

def load_model() -> tuple:
    """Carica modello e scaler da disco se esistono."""
    mp = Path(ML_CONFIG["MODEL_PATH"])
    sp = Path(ML_CONFIG["SCALER_PATH"])
    if mp.exists() and sp.exists():
        model  = joblib.load(mp)
        scaler = joblib.load(sp)
        log.info("[load] ✅ Modello e scaler caricati da disco.")
        return model, scaler
    return None, None

# ==============================================================================
# SEZIONE 13 – PREDIZIONE & SCORING
# ==============================================================================

def predict_and_score(profiles: list[dict], model, scaler) -> list[dict]:
    """
    Applica il modello ML ai profili delle gemme.
    Aggiunge gem_probability e gem_label a ogni profilo.
    """
    if not profiles:
        return []
    if model is None or scaler is None:
        # Senza modello: score basato su regole semplici
        for p in profiles:
            social  = p.get("social_score", 25)
            inflow  = p.get("inflow_usd", 0)
            bsr     = p.get("buy_sell_ratio_1h", 1)
            score   = (social / 100 * 0.3 + min(inflow / 100_000, 1) * 0.4 +
                       min((bsr - 1) / 3, 1) * 0.3)
            p["gem_probability"] = round(score, 4)
            p["gem_label"]       = int(score >= ML_CONFIG["SIGNAL_THRESHOLD"])
        return profiles

    df = pd.DataFrame(profiles)
    df = engineer_features(df)
    X  = df[FEATURE_COLUMNS].values
    try:
        X_scaled = scaler.transform(X)
        probs    = model.predict_proba(X_scaled)[:, 1]
    except Exception as e:
        log.warning(f"[predict] Errore ML: {e} — uso score rule-based")
        for p in profiles:
            p["gem_probability"] = 0.5
            p["gem_label"]       = 0
        return profiles

    for i, p in enumerate(profiles):
        p["gem_probability"] = round(float(probs[i]), 4)
        p["gem_label"]       = int(probs[i] >= ML_CONFIG["SIGNAL_THRESHOLD"])

    if hasattr(model, "feature_importances_"):
        imp  = pd.Series(model.feature_importances_, index=FEATURE_COLUMNS)
        top3 = imp.nlargest(3).index.tolist()
        for i, p in enumerate(profiles):
            p["top_features"] = " | ".join(
                f"{f}={df.iloc[i][f]:.3f}" for f in top3
            )

    return profiles


# ==============================================================================
# SEZIONE 14 – GENERAZIONE SEGNALI & OUTPUT
# ==============================================================================

def generate_gem_signals(profiles: list[dict], model, scaler) -> list[dict]:
    """
    Filtra e scoreizza i profili, ritorna solo le gemme segnalate.

    Filtri post-ML (calibrati su dati storici reali):
      1. pair_age_hours <= 720  : token non più vecchi di 1 mese
                                  (i token giovani hanno 21% hit rate vs 13% baseline)
      2. market_cap_usd < 3M    : market cap piccolo = più upside residuo
                                  (BUONI mediana $570k, MALE mediana $1M)
      3. buy_sell_ratio >= 1.3  : più compratori che venditori al momento del segnale
                                  (solo quando disponibile — se NaN/0 il filtro è saltato)
    """
    if not profiles:
        return []

    scored  = predict_and_score(profiles, model, scaler)
    segnali = [p for p in scored if p.get("gem_label") == 1]
    log.info(f"[segnali] {len(segnali)}/{len(scored)} gemme superano la soglia "
             f"P≥{ML_CONFIG['SIGNAL_THRESHOLD']:.0%}")

    # ── Filtri post-ML basati sull'analisi dei segnali storici ───────────────
    MAX_AGE_H  = FILTER_CONFIG.get("MAX_PAIR_AGE_HOURS", 720)   # 1 mese
    MAX_MCAP   = 3_000_000      # $3M — sopra questa soglia il token è già maturo
    MIN_BSR    = 1.3            # buy/sell ratio minimo (saltato se dato non disponibile)

    filtrati = []
    for p in segnali:
        sym  = p.get("token_symbol", "?")

        # 1. Età token
        age = p.get("pair_age_hours", 0) or 0
        if age > MAX_AGE_H:
            log.debug(f"[segnali] {sym} scartato: età {age:.0f}h > {MAX_AGE_H}h (token maturo)")
            continue

        # 2. Market cap
        mcap = p.get("market_cap_usd", 0) or 0
        if mcap > MAX_MCAP:
            log.debug(f"[segnali] {sym} scartato: mcap ${mcap:,.0f} > $3M (già scoperto)")
            continue

        # 3. Buy/sell ratio (solo quando disponibile e > 0)
        bsr = p.get("buy_sell_ratio_1h", 0) or 0
        if bsr > 0 and bsr < MIN_BSR:
            log.debug(f"[segnali] {sym} scartato: BSR={bsr:.2f} < {MIN_BSR} (sell pressure)")
            continue

        filtrati.append(p)

    n_scartati = len(segnali) - len(filtrati)
    if n_scartati:
        log.info(f"[segnali] Filtri post-ML: {n_scartati} scartati → {len(filtrati)} gemme finali")

    return filtrati


# Cooldown email in-memory: sym|chain → datetime ultimo invio
_V2_EMAIL_SENT: dict = {}
_V2_EMAIL_COOLDOWN_H = 6   # non rimandare la stessa gemma per 6 ore

def _v2_email_sent_recently(sym: str, chain: str) -> bool:
    key = f"{sym.upper()}|{chain.upper()}"
    ts = _V2_EMAIL_SENT.get(key)
    if not ts:
        return False
    return (datetime.now() - ts).total_seconds() < _V2_EMAIL_COOLDOWN_H * 3600

def _v2_mark_email_sent(sym: str, chain: str):
    _V2_EMAIL_SENT[f"{sym.upper()}|{chain.upper()}"] = datetime.now()


def send_gem_email(gem: dict, force: bool = False) -> bool:
    """
    Invia una email con le caratteristiche della gemma trovata.
    Ritorna True se l'invio è andato a buon fine, False altrimenti.

    force=True  → invia anche se gem_probability è sotto MIN_PROBABILITY.
                  Usato per notificare tutti i token che passano i filtri
                  qualità, indipendentemente dalla soglia ML (Fix 2).
    force=False → comportamento originale: invia solo se P >= MIN_PROBABILITY.
    """
    cfg = EMAIL_CONFIG
    if not cfg["ENABLED"]:
        return False
    if not cfg["SMTP_USER"] or not cfg["SMTP_PASSWORD"]:
        log.warning("[email] SMTP_USER o SMTP_PASSWORD non configurati. "
                    "Imposta le variabili d'ambiente SMTP_USER e SMTP_PASSWORD.")
        return False
    if not cfg["TO_ADDR"]:
        log.warning("[email] EMAIL_TO non configurato.")
        return False

    prob  = gem.get("gem_probability", 0)
    sym   = gem.get("token_symbol", "?")
    chain = gem.get("chain", "?").upper()

    # Dedup: non rimandare la stessa gemma per 6 ore
    if _v2_email_sent_recently(sym, chain):
        log.info(f"[email] ⏭️  {sym} ({chain}) già notificato nelle ultime {_V2_EMAIL_COOLDOWN_H}h — skip")
        return False

    if not force and prob < cfg["MIN_PROBABILITY"]:
        log.debug(f"[email] P={prob:.1%} < soglia {cfg['MIN_PROBABILITY']:.1%} — email non inviata.")
        return False
    addr      = gem.get("token_address", "")
    pair      = gem.get("pair_address", "")
    price     = gem.get("price_usd", 0)
    mcap      = gem.get("market_cap_usd", 0)
    liq       = gem.get("liquidity_usd", 0)
    vol1h     = gem.get("volume_1h_usd", 0)
    inflow    = gem.get("inflow_usd", 0)
    wallets   = gem.get("inflow_wallet_count", 0)
    pnl_w     = gem.get("avg_wallet_pnl_pct", 0)
    social    = gem.get("social_score", 0)
    tweets    = gem.get("social_tweet_count", 0)
    tvl       = gem.get("tvl_usd", 0)
    bsr       = gem.get("buy_sell_ratio_1h", 0)
    ch1h      = gem.get("change_1h_pct", 0)
    age       = gem.get("pair_age_hours", 0)
    top_f     = gem.get("top_features", "")
    gem_class = gem.get("gem_class", "NEUTRAL")   # ← fix: era undefined nel template HTML
    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Link DexScreener
    dex_chain = chain.lower()
    dexscreener_url = (
        f"https://dexscreener.com/{dex_chain}/{pair}"
        if pair else f"https://dexscreener.com/{dex_chain}/{addr}"
    )

    above_threshold = prob >= cfg["MIN_PROBABILITY"]
    subject = (
        f"💎 GEM trovata: {sym} [{chain}] — P={prob:.1%} — ${price:.8f}"
        if above_threshold else
        f"[FILTRO] {sym} [{chain}] — P={prob:.1%} (sotto soglia ML) — ${price:.8f}"
    )

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  body {{ font-family: Arial, sans-serif; background: #020617; color: #e2e8f0;
         margin: 0; padding: 20px; }}
  .card {{ background: #0f172a; border: 1px solid #1e293b; border-radius: 12px;
           padding: 24px; max-width: 600px; margin: 0 auto; }}
  .header {{ font-size: 22px; font-weight: 800; color: #e2e8f0; margin-bottom: 4px; }}
  .sub {{ color: #64748b; font-size: 13px; margin-bottom: 20px; }}
  .badge {{ display: inline-block; border-radius: 4px; padding: 2px 10px;
            font-size: 12px; font-weight: 600; margin-right: 6px; }}
  .chain-badge {{ background: #9945FF22; color: #9945FF; border: 1px solid #9945FF44; }}
  .prob-badge  {{ background: #4ade8022; color: #4ade80; border: 1px solid #4ade8044; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 16px 0; }}
  .metric {{ background: #1e293b; border-radius: 8px; padding: 10px 14px; }}
  .metric-label {{ color: #64748b; font-size: 11px; text-transform: uppercase;
                   letter-spacing: .5px; margin-bottom: 4px; }}
  .metric-value {{ color: #e2e8f0; font-weight: 700; font-size: 15px;
                   font-family: monospace; }}
  .metric-sub {{ color: #94a3b8; font-size: 11px; margin-top: 2px; }}
  .addr-box {{ background: #1e293b; border-radius: 6px; padding: 8px 12px;
               font-family: monospace; font-size: 12px; color: #94a3b8;
               word-break: break-all; margin: 10px 0; }}
  .cta {{ display: inline-block; background: #7c3aed; color: #fff;
          padding: 10px 22px; border-radius: 8px; text-decoration: none;
          font-weight: 700; font-size: 14px; margin: 12px 0; }}
  .disclaimer {{ color: #a16207; background: #1c1a00; border: 1px solid #6e5908;
                 border-radius: 8px; padding: 10px 14px; font-size: 12px;
                 margin-top: 20px; }}
  .positive {{ color: #4ade80; }}
  .negative {{ color: #f87171; }}
  .purple  {{ color: #a78bfa; }}
  .yellow  {{ color: #facc15; }}
</style>
</head>
<body>
<div class="card">
  <div class="header">💎 GEM SEGNALATA: {sym}</div>
  <div class="sub">{now_str}</div>

  <div>
    <span class="badge chain-badge">{chain}</span>
    <span class="badge prob-badge">P(gem) = {prob:.1%}</span>
  </div>

  <div class="addr-box">
    <b>Token:</b> {addr}<br>
    <b>Pair: </b> {pair}
  </div>

  <div class="grid">
    <div class="metric">
      <div class="metric-label">💲 Prezzo Entry</div>
      <div class="metric-value">${price:.8f}</div>
    </div>
    <div class="metric">
      <div class="metric-label">📊 Market Cap</div>
      <div class="metric-value">${mcap:,.0f}</div>
    </div>
    <div class="metric">
      <div class="metric-label">🏷️ Categoria</div>
      <div class="metric-value" style="color:#a78bfa">{gem_class}</div>
    </div>
    <div class="metric">
      <div class="metric-label">💧 Liquidità</div>
      <div class="metric-value">${liq:,.0f}</div>
    </div>
    <div class="metric">
      <div class="metric-label">📈 Volume 1h</div>
      <div class="metric-value">${vol1h:,.0f}</div>
    </div>
    <div class="metric">
      <div class="metric-label">↕️ BSR / Δ1h</div>
      <div class="metric-value">{bsr:.2f}
        <span class="{'positive' if ch1h >= 0 else 'negative'}">
          {'▲' if ch1h >= 0 else '▼'}{abs(ch1h):.1f}%
        </span>
      </div>
      <div class="metric-sub">Età pair: {age:.1f}h</div>
    </div>
    <div class="metric">
      <div class="metric-label">🐋 Smart Money</div>
      <div class="metric-value purple">${inflow:,.0f}</div>
      <div class="metric-sub">{wallets} wallet | avg PnL {pnl_w:+.0f}%</div>
    </div>
    <div class="metric">
      <div class="metric-label">💬 Social Score</div>
      <div class="metric-value yellow">{social:.0f}<span style="font-size:12px;color:#64748b">/100</span></div>
      <div class="metric-sub">{tweets} tweet analizzati</div>
    </div>
    {"<div class='metric'><div class='metric-label'>📊 TVL (DefiLlama)</div><div class='metric-value positive'>$" + f"{tvl:,.0f}" + "</div></div>" if tvl > 0 else ""}
  </div>

  {"<div style='color:#94a3b8;font-size:12px;margin:8px 0'>🔑 Top features: " + top_f + "</div>" if top_f else ""}

  <a class="cta" href="{dexscreener_url}" target="_blank">
    🔍 Apri su DexScreener →
  </a>

  <div class="disclaimer">
    ⚠️ <b>AVVISO:</b> Solo a scopo educativo. NON costituisce consiglio finanziario.
    Il trading di criptovalute comporta rischi molto elevati di perdita del capitale.
  </div>
</div>
</body>
</html>
"""

    # Testo plain come fallback
    plain_body = (
        f"💎 GEM TROVATA: {sym} [{chain}]\n"
        f"Timestamp: {now_str}\n"
        f"Probabilità gem: {prob:.1%}\n\n"
        f"Token address : {addr}\n"
        f"Pair address  : {pair}\n\n"
        f"Prezzo entry  : ${price:.8f}\n"
        f"Market Cap    : ${mcap:,.0f}\n"
        f"Liquidità     : ${liq:,.0f}\n"
        f"Volume 1h     : ${vol1h:,.0f}\n"
        f"BSR           : {bsr:.2f} | Δ1h: {ch1h:+.1f}% | Età: {age:.1f}h\n"
        f"Smart Money   : ${inflow:,.0f} da {wallets} wallet (avg PnL {pnl_w:+.0f}%)\n"
        f"Social Score  : {social:.0f}/100 ({tweets} tweet)\n"
        + (f"TVL           : ${tvl:,.0f}\n" if tvl > 0 else "")
        + (f"Top features  : {top_f}\n" if top_f else "")
        + f"\nDexScreener: {dexscreener_url}\n\n"
        "⚠️ Solo a scopo educativo. NON è un consiglio finanziario."
    )

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = cfg["FROM_ADDR"] or cfg["SMTP_USER"]
        msg["To"]      = cfg["TO_ADDR"]
        msg.attach(MIMEText(plain_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body,  "html",  "utf-8"))

        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            server.sendmail(msg["From"], [cfg["TO_ADDR"]], msg.as_string())

        _v2_mark_email_sent(sym, chain)   # segna cooldown DOPO invio riuscito
        log.info(f"[email] ✅ Email inviata per {sym} a {cfg['TO_ADDR']}")
        return True

    except smtplib.SMTPAuthenticationError:
        log.error("[email] ❌ Autenticazione SMTP fallita. "
                  "Verifica SMTP_USER e SMTP_PASSWORD (Gmail: usa App Password).")
    except smtplib.SMTPException as e:
        log.error(f"[email] ❌ Errore SMTP: {e}")
    except Exception as e:
        log.error(f"[email] ❌ Errore imprevisto: {e}")
    return False


def stampa_gemma(gem: dict) -> None:
    """Stampa un segnale gemma in modo leggibile e lo registra nel GemTracker."""
    if not rugcheck_safe(gem.get("token_address", ""), "gemme",
                         chain=gem.get("chain", "solana")):
        return

    sym     = gem.get("token_symbol", "?")
    chain   = gem.get("chain", "?").upper()
    addr    = gem.get("token_address", "")
    price   = gem.get("price_usd", 0)
    mcap    = gem.get("market_cap_usd", 0)
    liq     = gem.get("liquidity_usd", 0)
    inflow  = gem.get("inflow_usd", 0)
    wallets = gem.get("inflow_wallet_count", 0)
    social  = gem.get("social_score", 0)
    prob    = gem.get("gem_probability", 0)
    tvl     = gem.get("tvl_usd", 0)
    gem_class = gem.get("gem_class", "NEUTRAL")
    
    bar = "═" * 65
    log.info(f"\n{bar}")
    log.info(f"  💎 GEM SEGNALATA: {sym} [{chain}]")
    log.info(f"  CATEGORIA: {gem_class}")
    log.info(f"  Probabilità gem: {prob:.1%}  |  P(≥{ML_CONFIG['PUMP_THRESHOLD_PCT']:.0f}% in 4h)")
    log.info(f"  Indirizzo: {addr}")
    log.info(f"  Prezzo:   ${price:.8f}")
    log.info(f"  Market Cap:  ${mcap:>12,.0f}  |  Liquidità: ${liq:>10,.0f}")
    log.info(f"  Volume 1h:   ${gem.get('volume_1h_usd', 0):>10,.0f}")
    log.info(f"  BSR:    {gem.get('buy_sell_ratio_1h', 0):.2f}  |  "
             f"Δ1h: {gem.get('change_1h_pct', 0):+.1f}%  |  "
             f"Età: {gem.get('pair_age_hours', 0):.1f}h")
    log.info(f"  🐋 Smart Money: ${inflow:,.0f} da {wallets} wallet  |  "
             f"Avg PnL wallet: {gem.get('avg_wallet_pnl_pct', 0):+.0f}%")
    log.info(f"  💬 Social Score: {social:.0f}/100  "
             f"({gem.get('social_tweet_count', 0)} tweet)")
    if tvl > 0:
        log.info(f"  📊 TVL (DefiLlama): ${tvl:,.0f}")
    if gem.get("top_features"):
        log.info(f"  🔑 Top features: {gem['top_features']}")
    log.info(bar)

    # Email — usa force=True perché la soglia ML (0.05) è quasi aperta;
    # il cooldown 6h in send_gem_email impedisce lo spam.
    try:
        send_gem_email(gem, force=True)
    except Exception as e:
        log.warning(f"[email] Errore invio: {e}")

    # Registra nel GemTracker
    if GEM_TRACKER_AVAILABLE:
        try:
            gt = get_gem_tracker()
            gt.registra_gemma(gem)
        except Exception as e:
            log.warning(f"[tracker] Errore registrazione gemma: {e}")

    # Imposta cooldown per questo token (evita segnali ripetuti sullo stesso token in dump)
    pair_key = gem.get("pair_address", "") or gem.get("token_address", "")
    if pair_key:
        _set_gem_cooldown(pair_key)

    # ── Bridge → defi_optimized ────────────────────────────────────────────
    # Scrive la gemma nella watchlist condivisa (gem_watchlist.json).
    # defi_optimized la legge ogni N cicli e prioritizza il token nel loop intraday:
    # stesso token, due timeframe diversi — gemmeV2 trova la gemma,
    # defi_optimized trova il momento di entrata ottimale.
    if GEM_WATCHLIST_AVAILABLE:
        try:
            write_gem_to_watchlist(gem)
        except Exception as e:
            log.warning(f"[watchlist] Errore scrittura watchlist per {sym}: {e}")


# ==============================================================================
# SEZIONE 15 – LOOP PRINCIPALE
# ==============================================================================

def main_loop(model, scaler) -> None:
    """
    Loop principale del bot:
      1. Per ogni chain → Dune: token con smart money inflow
      2. DexScreener: dati mercato per ogni token
      3. Social score + TVL
      4. Filtri qualità
      5. ML scoring
      6. Output segnali → GemTracker
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    dune       = DuneDataFetcher()
    social     = SocialAnalyzer()
    defillama  = DefiLlamaFetcher()
    gem_filter = GemFilter()

    if GEM_TRACKER_AVAILABLE:
        gt = get_gem_tracker()
        log.info("[loop] 📁 GemTracker attivo → report in 'reports/'")
        try:
            gt.genera_report_html()
        except Exception:
            pass

    ciclo = 0
    # Deduplicazione cross-ciclo: pair_address → first_seen datetime (TTL 24h)
    seen_pairs: dict[str, datetime] = {}

    log.info("[loop] 🚀 Avvio loop principale. Ctrl+C per interrompere.")
    # Carica blacklist dinamica dai followup precedenti all'avvio
    _check_gem_followup_blacklist()

    try:
        while True:
            ciclo += 1
            now = datetime.now()
            log.info(f"\n{'─'*65}")
            log.info(f"[loop] Ciclo #{ciclo} — {now.strftime('%Y-%m-%d %H:%M:%S')}")

            # Aggiorna blacklist ogni 10 cicli (~50 min)
            if ciclo % 10 == 0:
                _check_gem_followup_blacklist()

            # Scade le entry più vecchie di 24h
            seen_pairs = {k: v for k, v in seen_pairs.items()
                          if now - v < timedelta(hours=24)}

            all_profiles = []

            for chain in CHAINS:
                log.info(f"[loop] ── Chain: {chain.upper()} ──")

                # Step 1: Dune → token con smart money
                dune_tokens = dune.get_smart_money_tokens(chain)

                # ── Piano B: DexScreener Boosted/Trending se Dune porta pochi token ──
                _no_inflow = {"mock_dune", "dexscreener_boosted", "dexscreener_trending"}
                n_real = len([t for t in dune_tokens if t.get("source") not in _no_inflow])
                if n_real < 3:
                    log.info(f"[loop] Dune porta {n_real} token reali su {chain} "
                             f"→ integro con DexScreener Boosted/Trending.")
                    boosted  = fetch_dexscreener_boosted(chain, limit=30)
                    trending = fetch_dexscreener_trending(chain, limit=20)
                    existing = {t.get("token_address", "") for t in dune_tokens}
                    extra    = [t for t in boosted + trending
                                if t.get("token_address", "") not in existing]
                    dune_tokens = dune_tokens + extra
                    log.info(f"[loop] Totale token dopo integrazione: "
                             f"{len(dune_tokens)} su {chain}.")

                if not dune_tokens:
                    log.info(f"[loop] Nessun token disponibile per {chain}.")
                    continue

                log.info(f"[loop] {len(dune_tokens)} token totali per {chain}.")

                # ── Dedup upstream: rimuove duplicati per token_address da Dune ──
                # Stesso token da più wallet smart-money → entry duplicate in Dune.
                # Teniamo quella con inflow_usd maggiore (la più significativa).
                seen_dune_addr: dict[str, dict] = {}
                for dt in dune_tokens:
                    addr = dt.get("token_address", "")
                    if not addr:
                        continue
                    if addr not in seen_dune_addr or                        dt.get("inflow_usd", 0) > seen_dune_addr[addr].get("inflow_usd", 0):
                        seen_dune_addr[addr] = dt
                dune_tokens_dedup = list(seen_dune_addr.values())
                if len(dune_tokens_dedup) < len(dune_tokens):
                    log.info(f"[loop] Dedup Dune: {len(dune_tokens)} → "
                             f"{len(dune_tokens_dedup)} token unici su {chain}.")
                dune_tokens = dune_tokens_dedup

                # Pre-filtro rapido su dati Dune (evita chiamate API inutili)
                # Token boosted/trending non hanno inflow reale → pass-through diretto
                _SKIP_SRC = {"mock_dune", "dexscreener_boosted", "dexscreener_trending"}
                has_inflow_data = any(
                    dt.get("inflow_usd", 0) > 0
                    for dt in dune_tokens
                    if dt.get("source") not in _SKIP_SRC
                )
                if has_inflow_data:
                    dune_tokens_filtered = [
                        dt for dt in dune_tokens
                        if dt.get("source") in _SKIP_SRC          # boosted/trending: sempre passa
                        or (dt.get("inflow_usd", 0) >= FILTER_CONFIG["MIN_INFLOW_USD"]
                            and dt.get("inflow_wallet_count", 0) >= FILTER_CONFIG["MIN_SMART_WALLETS"])
                    ]
                    n_dune_ok = sum(1 for dt in dune_tokens_filtered if dt.get("source") == "dune")
                    n_extra   = sum(1 for dt in dune_tokens_filtered if dt.get("source") in _SKIP_SRC)
                    log.info(f"[loop] Pre-filtro {chain}: "
                             f"{n_dune_ok} Dune reali + {n_extra} DexScreener = "
                             f"{len(dune_tokens_filtered)}/{len(dune_tokens)} token.")
                else:
                    dune_tokens_filtered = dune_tokens
                    log.info("[loop] Pre-filtro smart money saltato "
                             "(dati inflow non presenti nella query Dune).")

                # Step 2-4: Aggrega profilo per ogni token in parallelo
                chain_profiles = []
                filter_stats: dict = {}    # motivo → conteggio scartati (per debug)
                MAX_WORKERS = 2   # Ridotto da 4: il rate limiter DexScreener serializza già le call

                # Dedup intra-ciclo per token_address E symbol|chain PRIMA di fetchare DexScreener.
                # R.S C0IN e simili arrivano da Dune con N indirizzi diversi (pair address per
                # wallet diversi) → il dedup per sola token_address non basta. Usiamo anche
                # symbol|chain come chiave secondaria per eliminare i duplicati per nome.
                seen_ta_intra:  dict[str, dict] = {}   # token_address → best entry
                seen_sym_intra: dict[str, dict] = {}   # SYMBOL|chain  → best entry
                for dt in dune_tokens_filtered:
                    ta  = dt.get("token_address", "")
                    sym = dt.get("token_symbol", "").upper()
                    sk  = f"{sym}|{chain}" if sym else ""
                    inflow = dt.get("inflow_usd", 0)

                    # Controlla se già visto per symbol|chain
                    if sk and sk in seen_sym_intra:
                        if inflow > seen_sym_intra[sk].get("inflow_usd", 0):
                            # Rimuovi il vecchio dalla mappa token_address
                            old_ta = seen_sym_intra[sk].get("token_address", "")
                            if old_ta and old_ta in seen_ta_intra:
                                del seen_ta_intra[old_ta]
                            seen_sym_intra[sk] = dt
                            if ta:
                                seen_ta_intra[ta] = dt
                        # else: skip questo duplicato con inflow inferiore
                        continue

                    # Controlla se già visto per token_address
                    if ta and ta in seen_ta_intra:
                        if inflow > seen_ta_intra[ta].get("inflow_usd", 0):
                            seen_ta_intra[ta] = dt
                            if sk:
                                seen_sym_intra[sk] = dt
                        continue

                    # Prima volta: registra in entrambe le mappe
                    if ta:
                        seen_ta_intra[ta] = dt
                    if sk:
                        seen_sym_intra[sk] = dt

                dune_tokens_filtered = list(seen_sym_intra.values()) if seen_sym_intra else list(seen_ta_intra.values())
                log.info(f"[loop] {len(dune_tokens_filtered)} token unici dopo dedup intra-ciclo (addr+sym)")

                # ── Esegui profili in parallelo con shutdown non-bloccante ─────────
                # IMPORTANTE: non usare `with ThreadPoolExecutor` — il __exit__ chiama
                # shutdown(wait=True) che blocca indefinitamente se un worker è appeso
                # (es. social_analyzer bloccato su ntscraper/twscrape senza timeout hard).
                # Usiamo shutdown(wait=False, cancel_futures=True) per liberare subito.
                _executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
                futures = {
                    _executor.submit(build_gem_profile, dt, social, defillama): dt
                    for dt in dune_tokens_filtered
                }
                _rejected_this_cycle: set = set()   # sym|chain già scartati → no log duplicato
                try:
                    for future in as_completed(futures, timeout=300):
                        dt = futures[future]
                        try:
                            profile = future.result()
                            if profile is None:
                                continue

                            # Dedup cross-ciclo: token_address è chiave primaria,
                            # ma PPEG e simili possono arrivare da Dune con token_address
                            # diversi puntando allo stesso pair → usiamo ANCHE pair_address
                            # e symbol+chain come chiavi secondarie.
                            pair_addr  = profile.get("pair_address", "")
                            tok_addr   = profile.get("token_address", "")
                            sym_chain  = f"{profile.get('token_symbol','').upper()}|{chain}"
                            pair_key   = tok_addr or pair_addr

                            # Skip se già scartato in questo ciclo (stesso sym|chain)
                            if sym_chain in _rejected_this_cycle:
                                log.debug(f"[dedup] {dt.get('token_symbol','?')} già scartato questo ciclo — skip")
                                continue

                            already_seen = (
                                (pair_key  and pair_key  in seen_pairs) or
                                (pair_addr and pair_addr in seen_pairs) or
                                (sym_chain and sym_chain in seen_pairs)
                            )
                            if already_seen:
                                log.debug(f"[dedup] {dt.get('token_symbol','?')} già visto — skip")
                                continue

                            passed, reason = gem_filter.check(profile)
                            if not passed:
                                log.info(f"[filtro] ❌ {dt.get('token_symbol','?')}: {reason}")
                                rk = " ".join(reason.split()[:3])
                                filter_stats[rk] = filter_stats.get(rk, 0) + 1
                                _rejected_this_cycle.add(sym_chain)
                                continue

                            # TVL solo sui token che passano i filtri
                            if BOT_CONFIG.get("ENABLE_DEFILLAMA", True):
                                sym  = profile.get("token_symbol", "")
                                addr = profile.get("token_address", "")
                                mcap = profile.get("market_cap_usd", 0)
                                tvl  = defillama.get_tvl(sym, addr)
                                profile["tvl_usd"] = tvl
                                profile["tvl_to_mcap_ratio"] = tvl / mcap if mcap > 0 else 0
                                log.info(
                                    f"[filtro] ✅ {sym} ({chain}) — "
                                    f"mcap=${mcap:,.0f} | "
                                    f"liq=${profile.get('liquidity_usd', 0):,.0f} | "
                                    f"social={profile.get('social_score', 0):.0f} | "
                                    f"inflow=${profile.get('inflow_usd', 0):,.0f}"
                                    + (f" | tvl=${tvl:,.0f}" if tvl > 0 else "")
                                )
                            else:
                                log.info(
                                    f"[filtro] ✅ {dt.get('token_symbol','?')} ({chain}) — "
                                    f"mcap=${profile.get('market_cap_usd', 0):,.0f} | "
                                    f"liq=${profile.get('liquidity_usd', 0):,.0f} | "
                                    f"social={profile.get('social_score', 0):.0f} | "
                                    f"inflow=${profile.get('inflow_usd', 0):,.0f}"
                                )
                        except Exception as e:
                            sym_e = dt.get("token_symbol", "?") if isinstance(dt, dict) else "?"
                            log.warning(f"[loop] ⚠️  Errore profilo {sym_e}: {e}")
                            continue
                        all_profiles.append(profile)

                except TimeoutError:
                    log.warning("[loop] ⏱️  Timeout futures — alcuni token saltati.")
                except Exception as e_fut:
                    log.error(f"[loop] Errore ThreadPoolExecutor: {e_fut}")
                except TimeoutError:
                    log.warning("[loop] ⏱️  Timeout futures — alcuni token saltati.")
                except Exception as e_fut:
                    log.error(f"[loop] Errore ThreadPoolExecutor: {e_fut}")
                finally:
                    # Non bloccare: abbandona i worker appesi (es. social hung)
                    _executor.shutdown(wait=False, cancel_futures=True)

                if filter_stats:
                    top_reasons = sorted(filter_stats.items(), key=lambda x: -x[1])[:5]
                    log.info(f"[filtro] Top motivi scarto {chain}: {top_reasons}")

                all_profiles.extend(chain_profiles)

            # ── Scoring ML + segnali ──────────────────────────────────────────
            if all_profiles:
                gem_signals = generate_gem_signals(all_profiles, model, scaler)
                n_gems = len(gem_signals)
                if gem_signals:
                    log.info(f"\n[loop] {n_gems} GEMME trovate in questo ciclo:")
                    for gem in gem_signals:
                        sym_g   = gem.get("token_symbol", "?").upper()
                        chain_g = gem.get("chain", "?")
                        sc_key  = f"{sym_g}|{chain_g}"
                        if sc_key not in seen_pairs:
                            stampa_gemma(gem)
                            tok_k  = gem.get("token_address", "")
                            pair_k = gem.get("pair_address", "")
                            if tok_k:
                                seen_pairs[tok_k] = now
                            if pair_k:
                                seen_pairs[pair_k] = now
                            seen_pairs[sc_key] = now
                else:
                    log.info("[loop] Nessuna gemma supera la soglia ML in questo ciclo.")
                    n_gems = 0
                log.info(
                    f"[loop] Ciclo #{ciclo}: {n_gems} gemme segnalate "
                    f"su {len(all_profiles)} profili analizzati."
                )
            else:
                log.info(f"[loop] Ciclo #{ciclo}: nessun profilo raccolto.")

            # ── Report HTML ───────────────────────────────────────────────────
            if GEM_TRACKER_AVAILABLE:
                try:
                    get_gem_tracker().genera_report_html()
                except Exception as e:
                    log.warning(f"[tracker] Errore report HTML: {e}")

            interval = BOT_CONFIG.get("LOOP_INTERVAL_SEC", 300)
            log.info(f"[loop] Attesa {interval}s prima del ciclo #{ciclo + 1}...")
            time.sleep(interval)

    except KeyboardInterrupt:
        log.info("[loop] Interruzione manuale. Uscita.")
        if GEM_TRACKER_AVAILABLE:
            try:
                get_gem_tracker().stop()
            except Exception:
                pass
    except Exception:
        log.exception("[loop] Errore critico nel main loop.")
        raise


# ==============================================================================
# SEZIONE 17 – ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    log.info("=" * 65)
    log.info("  QUALITY GEM HUNTER v2 -- Smart Money + Social + Fondamentali")
    log.info(f"  Chain: {', '.join(CHAINS.keys()).upper()}")
    log.info("  AVVISO: Solo a scopo educativo. Non garantisce profitti.")
    log.info("=" * 65)

    model, scaler = load_model()
    if model is None:
        log.info("[main] Nessun modello trovato. Addestramento...")
        try:
            df_train = build_training_dataset(n_samples=800)
            model, scaler = train_model(df_train)
            log.info("[main] Modello addestrato.")
        except Exception as e:
            log.error(f"[main] Training fallito: {e}")
            log.warning("[main] Avvio senza modello (regole semplici).")
    else:
        log.info("[main] Modello caricato da disco.")

    try:
        main_loop(model, scaler)
    except Exception as e:
        log.exception(f"[main] ERRORE FATALE: {e}")
