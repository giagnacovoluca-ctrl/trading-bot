"""
==============================================================================
crypto_signal_bot.py
Versione migliorata — changelog:
  • Fix: API key Moralis letta da env var (non hardcoded)
  • Fix: doppia chiamata fetch_dexscreener_boosts/pairs rimossa
  • Fix: salvataggio debug_filters.csv ora sempre eseguito (return mal posizionato)
  • Fix: debug CSV usa modalità append consistente (non sovrascrive tra chain)
  • Fix: import csv spostato in cima al file (non dentro un if-else)
  • Fix: XGBoost early_stopping_rounds spostato a fit() (compatibilità xgb>=2.x)
Sistema di rilevamento segnali on-chain per token in trend rialzista.
Catene supportate: Ethereum mainnet, BNB Smart Chain (BSC).

⚠️  AVVISO IMPORTANTE ⚠️
Questo sistema NON garantisce profitti.
È esclusivamente uno strumento di analisi quantitativa e generazione segnali.
Il trading di token crypto e asset on-chain è altamente rischioso.
Usalo solo a scopo educativo o di ricerca. L'autore non è responsabile
di perdite finanziarie derivanti dall'utilizzo di questo software.
==============================================================================
"""

# ── Librerie standard ──────────────────────────────────────────────────────
import os
import csv
import time
import threading
import logging
import warnings
import random
from collections import deque
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
# ── Librerie di terze parti ────────────────────────────────────────────────
import numpy as np
import pandas as pd
import ssl
import requests
from requests.adapters import HTTPAdapter

# Scikit-learn
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.model_selection import (
    train_test_split,
    StratifiedKFold,
    TimeSeriesSplit,
    cross_val_score,
)
from sklearn.preprocessing import RobustScaler
from sklearn.metrics import (
    classification_report,
    roc_auc_score,
    precision_recall_curve,
    average_precision_score,
)
from sklearn.utils.class_weight import compute_class_weight

import joblib  # salvataggio/caricamento modello

# Directory reports ancorata al modulo: i path relativi ("reports/...") dipendono
# dalla CWD del processo (run.py gira con CWD esterna alla repo) e finivano in
# /home/magic/Scrivania/code/reports — vedi bug cycle_stats.csv/blacklist 10/06
_REPORTS_DIR = Path(__file__).resolve().parent / "reports"

try:
    from rugcheck import is_safe as rugcheck_safe
except ImportError:
    def rugcheck_safe(mint, scanner, chain="solana"): return True

# ── Modulo tracking segnali ────────────────────────────────────────────────
# Importa il tracker per salvare i segnali con monitoraggio prezzi 4h
try:
    from signal_tracker import get_tracker, TRACKER_CONFIG
    TRACKER_AVAILABLE = True
except ImportError:
    TRACKER_AVAILABLE = False
    warnings.warn("signal_tracker.py non trovato. Il tracking prezzi sarà disabilitato.")

# ── Bridge con gemmeV3 (gem_watchlist) ─────────────────────────────────────
# Legge la watchlist prodotta da gemmeV2: token con smart money inflow
# che devono essere prioritizzati nel loop intraday pre-pump.
try:
    from gem_watchlist import (
        get_watchlist_addresses,
        get_watchlist_pair_addresses,
        load_watchlist,
        watchlist_summary,
        WATCHLIST_PRIORITY_BOOST,
    )
    GEM_WATCHLIST_AVAILABLE = True
except ImportError:
    GEM_WATCHLIST_AVAILABLE = False
    def get_watchlist_addresses(chain=None): return {}        # noqa
    def get_watchlist_pair_addresses(chain=None): return {}   # noqa
    def load_watchlist(chain=None): return []                 # noqa
    def watchlist_summary(): return "gem_watchlist non disponibile"  # noqa
    WATCHLIST_PRIORITY_BOOST = 0.0
    warnings.warn(
        "gem_watchlist.py non trovato — il bridge con gemmeV2 è disabilitato. "
        "Il bot funziona normalmente senza prioritizzazione watchlist."
    )

# XGBoost (opzionale – commentato se non installato)
try:
    import xgboost as xgb
    XGBOOST_AVAILABLE = True
except ImportError:
    XGBOOST_AVAILABLE = False
    warnings.warn("XGBoost non installato. Verrà usato solo RandomForest.")

# Nasconde avvisi non critici
warnings.filterwarnings("ignore", category=UserWarning)

# ── Configurazione logging ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ==============================================================================
# SEZIONE 1 – CONFIGURAZIONE E COSTANTI
# ==============================================================================

# ── Chiavi API (inserisci le tue chiavi qui oppure usa variabili d'ambiente) ─
MORALIS_API_KEY = os.environ.get("MORALIS_API_KEY", "")  # ⚠️ NON hardcodare la chiave qui — usa variabile d'ambiente
GOPLUS_API_KEY  = os.environ.get("GOPLUS_KEY", "")  # opzionale sul piano free

# Avviso chiavi mancanti
if not MORALIS_API_KEY:
    warnings.warn("MORALIS_API_KEY non impostata. Moralis sarà disabilitato.")

# ── URL base delle API ─────────────────────────────────────────────────────
DEXSCREENER_BASE = "https://api.dexscreener.com/latest"
MORALIS_BASE     = "https://deep-index.moralis.io/api/v2.2"
GOPLUS_BASE      = "https://api.gopluslabs.io/api/v1"

# ── Catene supportate e Chain ID GoPlus ───────────────────────────────────
# Chain attive nel loop principale (bot le itera in ordine)
CHAINS = {
    "solana": {"moralis_chain": None,  "goplus_chain_id": "900",
               "dexscreener_id": "solana", "is_evm": False},
    # "bsc":  {"moralis_chain": "bsc", "goplus_chain_id": "56",
    #           "dexscreener_id": "bsc",    "is_evm": True},   # disabilitato: 16% WR
    "base": {"moralis_chain": "base", "goplus_chain_id": "8453",
             "dexscreener_id": "base",    "is_evm": True},
}

# Mappa estesa per lookup GoPlus/Moralis su chain non nel loop
# (evita il vecchio bug: chain sconosciuta → fallback a BSC chain_id="56")
_CHAIN_META = {
    **CHAINS,
    "ethereum": {"moralis_chain": "eth", "goplus_chain_id": "1",
                 "dexscreener_id": "ethereum", "is_evm": True},
    "base":     {"moralis_chain": "base", "goplus_chain_id": "8453",
                 "dexscreener_id": "base",    "is_evm": True},
    "arbitrum": {"moralis_chain": "arbitrum", "goplus_chain_id": "42161",
                 "dexscreener_id": "arbitrum", "is_evm": True},
}

# ── Parametri di configurazione del bot ───────────────────────────────────
CONFIG = {
    # Target: il token viene classificato come "pump" se sale almeno X%
    # entro la finestra temporale LOOKAHEAD_MINUTES
    "PUMP_THRESHOLD_PCT":  20.0,       # +20%
    "LOOKAHEAD_MINUTES":   60,         # finestra lookahead in minuti

    # Soglia di probabilità per generare un segnale di entrata.
    # Il modello è addestrato su dati SINTETICI → probabilità out-of-distribution
    # su dati reali sono sistematicamente basse (max ~0.20).
    # La soglia ML è quindi quasi aperta; il filtro primario è prepump_composite_score.
    # Alzare SIGNAL_THRESHOLD quando sarà disponibile training su dati reali (LABELED_CSV).
    "SIGNAL_THRESHOLD":    0.15,       # P(pump) > 15% — soglia discovery (modello sintetico)

    # ── Filtri di sicurezza hard (applicati prima del modello ML) ──────────────
    # Calibrati su 256 segnali reali (apr-mag 2026): 40% PUMP / 55% DUMP.
    "MAX_BUY_TAX_PCT":     10.0,    # era 15 — tax alta = rug risk
    "MAX_SELL_TAX_PCT":    10.0,    # era 15
    "MIN_LIQUIDITY_USD":   20_000.0, # era 10k — liq <20k → 61% dump
    "MAX_LIQUIDITY_USD":  150_000.0, # NUOVO — liq >150k = token maturo (ARK $72M, CARDS $2.5M mai pumpano)
    "MIN_VOLUME_1H_USD":   15_000.0, # era 10k
    "MAX_VOLUME_1H_USD":   80_000.0, # NUOVO — vol >100k → 68% dump (pump già avvenuto)
    "MAX_CHANGE_1H_PCT":   12.0,    # era 25 — +12 a +25% = già post-pump → 49% dump
    "MIN_CHANGE_1H_PCT":   -8.0,    # ok — zona -10/0% è la migliore (61% pump)
    "MAX_BSR_1H":          5.0,     # era 1.8 — formula buys/(sells+1) gonfia il valore: 164/82=2.0 è bullish, non wash. 5.0 blocca solo casi estremi (buys=500, sells=1)
    "MIN_BSR_1H":          0.55,    # NUOVO — BSR<0.55 → sells >> buys o zero attività → skip
    "MIN_VOL_1H_GECKO":    10_000.0, # vol_1h minimo per pool da GeckoTerminal (micro-token → troppo rumore)
    # ── GeckoTerminal Phase 1: filtri su dati unici wallet ───────────────────
    "MIN_BUYERS_1H_WALLETS": 3,      # almeno 3 wallet distinti acquirenti nell'ultima ora
    "MIN_MCAP_USD":        50_000.0,  # market cap minimo (token fantasma/rug sotto 50k)
    "MAX_MCAP_USD":    50_000_000.0,  # market cap massimo (token già maturo/mainstream)
    # ── GeckoTerminal Phase 2: filtro social ──────────────────────────────────
    "GECKO_REQUIRE_SOCIALS": False,   # True = scarta token senza alcun social (sito/TG/TW)
    "GECKO_MAX_INFO_CALLS":  8,       # max chiamate token-info per chain/ciclo (budget rate limit)
    # ── GeckoTerminal Phase 3: OHLCV trend filter ─────────────────────────────
    "GECKO_OHLCV_ENABLED": True,     # True = chiama endpoint OHLCV 15m per filtro trend
    "GECKO_MAX_OHLCV_CALLS": 6,      # max chiamate OHLCV per chain/ciclo
    # Cooldown: no segnale sullo stesso token per N minuti (evita ripetizioni su dump)
    "TOKEN_COOLDOWN_MIN":  90,      # era nessuno — BLANKY/HANK/NORMIE: 8-11 segnali tutti dump
    # Blacklist dinamica: se un token ha fatto -X% in snapshot precedenti, skippa
    "BLACKLIST_DROP_PCT":  -70.0,   # se price_followup mostra -70%, token blacklistato 6h
    "BLACKLIST_DURATION_H": 6.0,
    # Parametri del modello ML
    "N_ESTIMATORS":        300,
    "MAX_DEPTH":           6,
    "TEST_SIZE":           0.2,
    "CV_FOLDS":            5,
    "RANDOM_STATE":        42,

    # Numero di token da processare per ciclo del loop principale
    "BATCH_SIZE":          50,

    # Percorso dove salvare il modello addestrato
    "MODEL_PATH":          "crypto_signal_model.joblib",
    "SCALER_PATH":         "crypto_signal_scaler.joblib",

    # Frequenza del loop principale (secondi tra un ciclo e l'altro)
    "LOOP_INTERVAL_SEC":   180,        # ogni 3 minuti

    # Modalità mock: True = usa dati simulati, False = chiama API reali
    "USE_MOCK":            False,

    # ── Training ──
    # Se il file esiste viene usato per il training invece di build_dataset()
    "TRAINING_CSV":        "training_dataset.csv",

    # ── Moralis ──
    # False = salta Moralis (quota esaurita / non configurato)
    "MORALIS_ENABLED":     False,

    # ── Costi reali per filtro segnale netto ──
    "SLIPPAGE_ESTIMATE_PCT": 3.0,

    # ── DataCollector — raccolta dati reali per training ──
    # Ogni ciclo del loop salva uno snapshot di tutti i token osservati.
    # Dopo LOOKAHEAD_MINUTES, il collector torna a prendere il prezzo
    # e calcola il target reale (pump >= PUMP_THRESHOLD_PCT%).
    "COLLECTOR_ENABLED":       True,
    "COLLECTOR_DIR":           "collector_data",
    "SNAPSHOTS_CSV":           "collector_data/snapshots_unlabeled.csv",
    "LABELED_CSV":             "collector_data/dataset_labeled.csv",
    # Quante ore prima di tentare di etichettare uno snapshot
    # (deve essere >= LOOKAHEAD_MINUTES/60 + margine per latenza API)
    "LABEL_AFTER_HOURS":       1.25,
    # Quante ore massimo aspettare prima di scartare uno snapshot senza label
    "LABEL_EXPIRE_HOURS":      6.0,
    # Numero minimo di righe labeled per triggherare un auto-retrain
    "AUTO_RETRAIN_MIN_ROWS":   500,
    # Ogni quante ore tentare un auto-retrain (se ci sono abbastanza dati)
    "AUTO_RETRAIN_INTERVAL_H": 24,

    # ── Ricerca token avanzata ──
    # Usa endpoint aggiuntivi Dexscreener per trovare più token
    "USE_TOKEN_PROFILES":  True,    # /token-profiles/latest/v1
    "USE_NEW_PAIRS":       True,    # /latest/dex/pairs/<chain>/<addr>
    "MAX_PAIR_AGE_HOURS":  72,      # ignora pair più vecchi di N ore
    "MIN_PAIR_AGE_HOURS":  0.25,    # ignora pair creati da meno di 15min

    # ── GeckoTerminal (discovery complementare a Dexscreener) ──
    # Aggiunge pool trending + nuovi da GeckoTerminal prima dello scoring.
    # Free tier: 30 req/min, no API key. Utile soprattutto per Solana.
    "GECKOTERMINAL_ENABLED":    True,

    # ── Bridge gemmeV3 → defi_optimized ──
    # Ogni quanti cicli del loop principale ricaricare la watchlist da gem_watchlist.json.
    # Ciclo = 3 min → 10 cicli = 30 min. La watchlist si aggiorna quando gemmeV2 trova gemme.
    "WATCHLIST_RELOAD_EVERY_N_CYCLES": 10,
    # Max token da watchlist da aggiungere per ciclo (evita di saturare il batch)
    "WATCHLIST_MAX_INJECT":            5,
}

# ── Cooldown per token (evita segnali ripetuti sullo stesso token in dump) ────
# Struttura: { token_address: datetime_ultimo_segnale }
_token_last_signal: dict = {}
_token_cooldown_lock = __import__("threading").Lock()

# ── Blacklist dinamica (token che hanno mostrato dump >70% nei followup) ──────
# Struttura: { token_address: datetime_blacklist_scadenza }
_token_blacklist: dict = {}

def _is_token_cooldown(token_address: str) -> bool:
    """True se il token è ancora in cooldown (segnalato di recente)."""
    cooldown_min = CONFIG.get("TOKEN_COOLDOWN_MIN", 90)
    with _token_cooldown_lock:
        last = _token_last_signal.get(token_address)
        if last is None:
            return False
        elapsed = (datetime.now() - last).total_seconds() / 60
        return elapsed < cooldown_min

def _set_token_cooldown(token_address: str) -> None:
    """Registra l'istante del segnale per il token."""
    with _token_cooldown_lock:
        _token_last_signal[token_address] = datetime.now()

def _is_token_blacklisted(token_address: str) -> bool:
    """True se il token è in blacklist (ha mostrato dump massiccio)."""
    with _token_cooldown_lock:
        expiry = _token_blacklist.get(token_address)
        if expiry is None:
            return False
        if datetime.now() < expiry:
            return True
        del _token_blacklist[token_address]
        return False

def _blacklist_token(token_address: str, symbol: str = "") -> None:
    """Aggiunge token alla blacklist per BLACKLIST_DURATION_H ore."""
    hours = CONFIG.get("BLACKLIST_DURATION_H", 6.0)
    expiry = datetime.now() + __import__("datetime").timedelta(hours=hours)
    with _token_cooldown_lock:
        _token_blacklist[token_address] = expiry
    log.info(f"[blacklist] 🚫 {symbol or token_address[:8]} blacklistato per {hours}h (dump massiccio rilevato)")

# ── Storico BSR per token (per calcolare un trend "leading", non solo il valore puntuale) ──
# Struttura: { pair_address: deque[(timestamp_unix, buy_sell_ratio_1h)] }
# Permette di rilevare un BSR in calo nei cicli precedenti anche quando al momento
# del segnale è ancora >= soglia (1.0) — l'ipotesi è che questo possa anticipare i dump
# che il dump_risk_score puntuale non riesce a vedere (vedi memoria project_dumprisk_no_predictive_power).
_BSR_HISTORY_MAXLEN = 12  # ~36 minuti a LOOP_INTERVAL_SEC=180s
_token_bsr_history: dict = {}
_BSR_HISTORY_FILE = Path("data/bsr_history.json")
# Solo letture recenti hanno valore predittivo: scartiamo quelle >2h al caricamento
_BSR_HISTORY_MAX_AGE_SEC = 7200

def _save_bsr_history() -> None:
    """Persiste _token_bsr_history su disco (JSON). Chiamata dopo ogni _update_bsr_history."""
    try:
        import json
        _BSR_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            addr: list(buf)
            for addr, buf in _token_bsr_history.items()
            if buf
        }
        _BSR_HISTORY_FILE.write_text(json.dumps(payload))
    except Exception as e:
        log.debug(f"[bsr_history] save error: {e}")

def _load_bsr_history() -> None:
    """Carica _token_bsr_history dal file (se esiste). Chiamata all'avvio prima del loop."""
    import json
    if not _BSR_HISTORY_FILE.exists():
        return
    try:
        now = time.time()
        payload = json.loads(_BSR_HISTORY_FILE.read_text())
        loaded = 0
        for addr, entries in payload.items():
            # Scarta letture troppo vecchie (>2h) — non hanno più valore predittivo
            fresh = [(ts, bsr) for ts, bsr in entries if now - ts <= _BSR_HISTORY_MAX_AGE_SEC]
            if fresh:
                buf = deque(fresh, maxlen=_BSR_HISTORY_MAXLEN)
                _token_bsr_history[addr] = buf
                loaded += 1
        log.info(f"[bsr_history] Caricati {loaded} token da {_BSR_HISTORY_FILE} "
                 f"(scartati {len(payload)-loaded} per età >2h).")
    except Exception as e:
        log.warning(f"[bsr_history] Errore caricamento: {e}")

def _update_bsr_history(df: pd.DataFrame) -> None:
    """Accumula una lettura BSR per ogni pair scansionato in questo ciclo (per il trend del prossimo)."""
    if "pair_address" not in df.columns or "buy_sell_ratio_1h" not in df.columns:
        return
    now = time.time()
    for addr, bsr in zip(df["pair_address"], df["buy_sell_ratio_1h"]):
        addr = str(addr or "")
        if not addr or addr == "nan":
            continue
        try:
            bsr_f = float(bsr)
        except (TypeError, ValueError):
            continue
        buf = _token_bsr_history.setdefault(addr, deque(maxlen=_BSR_HISTORY_MAXLEN))
        buf.append((now, bsr_f))
    _save_bsr_history()

def _bsr_trend(pair_address: str) -> tuple:
    """
    Trend del BSR (variazione/minuto) calcolato SOLO sulle letture dei cicli
    precedenti — non include la lettura corrente, per restare una feature "leading".
    Ritorna (trend_per_minuto, numero_campioni_storici_disponibili).
    """
    buf = _token_bsr_history.get(str(pair_address or ""))
    if not buf or len(buf) < 2:
        return 0.0, (len(buf) if buf else 0)
    t0, b0 = buf[0]
    t1, b1 = buf[-1]
    minutes = (t1 - t0) / 60.0
    if minutes <= 0:
        return 0.0, len(buf)
    return (b1 - b0) / minutes, len(buf)

def _check_followup_blacklist() -> None:
    """
    Legge price_followup.csv e blacklista token che hanno mostrato
    drop > BLACKLIST_DROP_PCT in qualsiasi snapshot.
    Chiamata all'avvio e ogni N cicli.
    """
    drop_thresh = CONFIG.get("BLACKLIST_DROP_PCT", -70.0)
    followup_path = str(_REPORTS_DIR / "price_followup.csv")
    if not __import__("os").path.exists(followup_path):
        return
    try:
        import csv as _csv
        token_min_change: dict = {}  # pair_address → min change_pct visto
        token_symbol: dict = {}
        with open(followup_path, "r", encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                pair = row.get("pair_address", "")
                sym  = row.get("token_symbol", "")
                chg  = row.get("change_pct", "")
                if not pair or not chg:
                    continue
                try:
                    chg_f = float(chg)
                    prev  = token_min_change.get(pair, 0.0)
                    token_min_change[pair] = min(prev, chg_f)
                    token_symbol[pair] = sym
                except ValueError:
                    pass
        for pair, min_chg in token_min_change.items():
            if min_chg <= drop_thresh and not _is_token_blacklisted(pair):
                _blacklist_token(pair, token_symbol.get(pair, ""))
    except Exception as e:
        log.warning(f"[blacklist] Errore lettura followup: {e}")

# ── Email config (stesse credenziali di gemmeV2) ───────────────────────────
EMAIL_CONFIG = {
    "SMTP_HOST":     os.environ.get("SMTP_HOST",     "smtp.gmail.com"),
    "SMTP_PORT":     int(os.environ.get("SMTP_PORT", "587")),
    "SMTP_USER":     os.environ.get("SMTP_USER",     "giagnacovo.luca@gmail.com"),
    "SMTP_PASSWORD": os.environ.get("SMTP_PASSWORD", ""),
    "FROM_ADDR":     os.environ.get("SMTP_FROM",     "giagnacovo.luca@gmail.com"),
    "TO_ADDR":       os.environ.get("SMTP_TO",       "giagnacovo.luca@gmail.com"),
    # Soglia minima pump_probability per inviare la mail
    # 0.0 = manda sempre (i filtri hard a monte già garantiscono qualità)
    "MIN_PROBABILITY": float(os.environ.get("DEFI_EMAIL_MIN_PROB", "0.0")),
}

# ==============================================================================
# SEZIONE 2 – FUNZIONI DI FETCH DATI (CON FALLBACK MOCK)
# ==============================================================================

class _TLS12Adapter(HTTPAdapter):
    """Forza TLS 1.2 — fallback per SSLEOFError su CDN che chiudono TLS 1.3."""
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)

_http_session      = requests.Session()
_http_session_tls12 = requests.Session()
_http_session_tls12.mount("https://", _TLS12Adapter())


def _get_headers_moralis() -> dict:
    """Restituisce gli header HTTP per Moralis, inclusa la API key."""
    return {
        "X-API-Key": MORALIS_API_KEY,
        "Accept":    "application/json",
    }


# Set di label che hanno già loggato errori 4xx → no flood nel loop
_logged_4xx: set = set()

def _safe_get(url: str, params: dict = None, headers: dict = None,
              timeout: int = 12, label: str = "") -> Optional[dict]:
    """
    Esegue una GET HTTP con gestione degli errori comuni:
    - 429 → attende e riprova (backoff esponenziale)
    - 401/403/404 → logga solo la prima volta per label (no flood)
    - Timeout/ConnectionError → backoff 3/6s + switch a sessione TLS 1.2 dal 2° tentativo
    """
    for tentativo in range(3):
        # Dal secondo tentativo usa TLS 1.2 (fix SSLEOFError su alcuni CDN)
        session = _http_session_tls12 if tentativo > 0 else _http_session
        try:
            resp = session.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                _logged_4xx.discard(label)  # reset se ora funziona
                return resp.json()
            elif resp.status_code == 429:
                attesa = 2 ** tentativo * 10
                log.warning(f"[{label}] Rate limit (429). Attendo {attesa}s...")
                time.sleep(attesa)
            elif resp.status_code in (401, 403):
                if label not in _logged_4xx:
                    log.error(f"[{label}] Auth fallita ({resp.status_code}). "
                              "Verifica la API key. (errore silenziato nei cicli futuri)")
                    _logged_4xx.add(label)
                return None
            elif resp.status_code == 404:
                if label not in _logged_4xx:
                    log.warning(f"[{label}] Endpoint non trovato (404). "
                                "Chain non supportata? (silenziato nei cicli futuri)")
                    _logged_4xx.add(label)
                return None
            else:
                log.warning(f"[{label}] Status inatteso: {resp.status_code}")
                return None
        except requests.exceptions.Timeout:
            log.warning(f"[{label}] Timeout al tentativo {tentativo + 1}.")
            if tentativo < 2:
                time.sleep(2 ** tentativo * 3)
        except requests.exceptions.ConnectionError as e:
            log.warning(f"[{label}] Errore di connessione (tentativo {tentativo + 1}/3): {e}")
            if tentativo < 2:
                time.sleep(2 ** tentativo * 3)
    return None

# ── Mappa chain_name → network slug GeckoTerminal ─────────────────────────
_GECKO_NETWORK = {
    "solana": "solana",
    "bsc":    "bsc",
    "base":   "base",
    "ethereum": "eth",
    "arbitrum": "arbitrum",
}
GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"

# Cache token-info GeckoTerminal (per sessione, azzerata al restart)
# Chiave: "{network}:{token_address_lower}" → dict con website/socials
_gecko_token_info_cache: dict[str, dict] = {}

# Throttle globale GeckoTerminal: max 1 req ogni 5s (12/min, free tier = 30 req/min).
# Più conservativo: 3.5s causava 429 su restart/cycle overlap (sliding window API).
_gecko_last_req_time: float = 0.0


def _gecko_throttle() -> None:
    """Rispetta il rate limit GeckoTerminal free tier (30 req/min = ~2s/req)."""
    global _gecko_last_req_time
    elapsed = time.time() - _gecko_last_req_time
    if elapsed < 5.0:
        time.sleep(5.0 - elapsed)
    _gecko_last_req_time = time.time()


def _gecko_after_req(success: bool) -> None:
    """Aggiorna _gecko_last_req_time dopo ogni chiamata gecko.
    Se la chiamata è fallita (429/None), impone una penalità di 60s per proteggere
    il budget residuo. Evita il pattern 'retry burst' dopo un rate-limit."""
    global _gecko_last_req_time
    if not success:
        # Penalità: prossima chiamata non prima di 60s
        _gecko_last_req_time = time.time() + 55.0
        log.info("[GeckoTerminal] 🛑 Rate limit rilevato → pausa 60s prima della prossima chiamata.")


def _fetch_gecko_token_info(network: str, token_address: str) -> dict:
    """
    Fase 2 — Token info endpoint GeckoTerminal (cached per sessione).
    Restituisce dict con: has_website, has_telegram, has_twitter, has_socials (bool).
    1 chiamata per token mai visto; successivamente dal cache → zero costo.
    """
    cache_key = f"{network}:{token_address.lower()}"
    if cache_key in _gecko_token_info_cache:
        return _gecko_token_info_cache[cache_key]

    url = f"{GECKOTERMINAL_BASE}/networks/{network}/tokens/{token_address}/info"
    headers = {"Accept": "application/json;version=20230302"}
    _gecko_throttle()
    data = _safe_get(url, headers=headers, label=f"GeckoTerminal-token-info-{token_address[:8]}")
    _gecko_after_req(success=bool(data))   # impone penalità 60s se 429/None
    result: dict = {"has_website": False, "has_telegram": False, "has_twitter": False, "has_socials": False}

    if data:
        attr = (data.get("data") or {}).get("attributes") or {}
        websites = attr.get("websites") or []
        tg = attr.get("telegram_handle") or ""
        tw = attr.get("twitter_handle") or ""
        disc = attr.get("discord_url") or ""
        result["has_website"]  = bool(websites and any(w for w in websites))
        result["has_telegram"] = bool(tg)
        result["has_twitter"]  = bool(tw)
        result["has_socials"]  = result["has_website"] or result["has_telegram"] or result["has_twitter"] or bool(disc)

    _gecko_token_info_cache[cache_key] = result
    return result


def _fetch_gecko_ohlcv(network: str, pool_address: str, candles: int = 6) -> list[list]:
    """
    Fase 3 — OHLCV 15-min GeckoTerminal per rilevare token già al picco.
    Ritorna lista di candle [[ts, open, high, low, close, vol], ...] (le più recenti ultime).
    Lista vuota se la chiamata fallisce.
    """
    url = (
        f"{GECKOTERMINAL_BASE}/networks/{network}/pools/{pool_address}/ohlcv/minute"
        f"?aggregate=15&limit={candles}&currency=usd"
    )
    headers = {"Accept": "application/json;version=20230302"}
    _gecko_throttle()
    data = _safe_get(url, headers=headers, label=f"GeckoTerminal-ohlcv-{pool_address[:8]}")
    _gecko_after_req(success=bool(data))   # penalità 60s su 429
    if not data:
        return []
    try:
        return (data.get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
    except Exception:
        return []


def _gecko_pool_to_dexscreener(pool: dict, chain: str) -> dict | None:
    """
    Normalizza un pool GeckoTerminal nel formato Dexscreener usato dal bot.
    Ritorna None se i dati minimi non sono presenti.
    """
    try:
        attr  = pool.get("attributes", {}) or {}
        rels  = pool.get("relationships", {}) or {}

        # ── Identificatori ──
        pair_addr = attr.get("address", "")
        if not pair_addr:
            return None

        base_token_rel = (rels.get("base_token", {}) or {}).get("data", {}) or {}
        base_addr = base_token_rel.get("id", "")         # es. "solana_<address>"
        if "_" in base_addr:
            base_addr = base_addr.split("_", 1)[1]       # rimuove prefisso network

        symbol = attr.get("base_token_symbol", attr.get("name", "?"))
        name   = attr.get("name", symbol)

        # ── Prezzi ──
        price_usd = float(attr.get("base_token_price_usd", 0) or 0)

        # ── Variazioni % ──
        chg = attr.get("price_change_percentage", {}) or {}
        m5  = float(chg.get("m5",  0) or 0)
        h1  = float(chg.get("h1",  0) or 0)
        h6  = float(chg.get("h6",  0) or 0)
        h24 = float(chg.get("h24", 0) or 0)

        # ── Volumi ──
        vol = attr.get("volume_usd", {}) or {}
        v_m5  = float(vol.get("m5",  0) or 0)
        v_h1  = float(vol.get("h1",  0) or 0)
        v_h24 = float(vol.get("h24", 0) or 0)

        # ── Liquidità ──
        liq_usd = float(attr.get("reserve_in_usd", 0) or 0)

        # ── Transazioni multi-timeframe (m5/h1/h6/h24) ──
        txns  = attr.get("transactions", {}) or {}
        txn5m = txns.get("m5", {}) or {}
        buys5m       = int(txn5m.get("buys",  0) or 0)
        sells5m      = int(txn5m.get("sells", 0) or 0)
        txn1h = txns.get("h1", {}) or {}
        buys1h       = int(txn1h.get("buys",   0) or 0)
        sells1h      = int(txn1h.get("sells",  0) or 0)
        buyers1h     = int(txn1h.get("buyers", 0) or 0)   # Phase 1: unique wallet buyers
        sellers1h    = int(txn1h.get("sellers",0) or 0)   # Phase 1: unique wallet sellers
        txn6h = txns.get("h6", {}) or {}
        buys6h       = int(txn6h.get("buys",  0) or 0)
        sells6h      = int(txn6h.get("sells", 0) or 0)
        txn24h  = txns.get("h24", {}) or {}
        buys24h  = int(txn24h.get("buys",  0) or 0)
        sells24h = int(txn24h.get("sells", 0) or 0)

        # ── Phase 1: campi aggiuntivi ──
        market_cap_usd = float(attr.get("market_cap_usd", 0) or 0)
        v_h6           = float(vol.get("h6", 0) or 0)

        # ── Età pair ──
        created_at_str = attr.get("pool_created_at", "")
        try:
            from datetime import timezone
            created_dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            created_ms = created_dt.timestamp() * 1000
        except Exception:
            created_ms = time.time() * 1000  # assume appena creato se mancante

        dex_id = CHAINS.get(chain, {}).get("dexscreener_id", chain)

        return {
            "chainId":       dex_id,
            "pairAddress":   pair_addr,
            "pairCreatedAt": created_ms,
            "baseToken": {
                "address": base_addr,
                "symbol":  symbol,
                "name":    name,
            },
            "quoteToken": {},
            "priceUsd":   str(price_usd),
            "priceChange": {"m5": m5, "m15": 0, "h1": h1, "h6": h6, "h24": h24},
            "volume":      {"m5": v_m5, "m1": 0, "h1": v_h1, "h6": v_h6, "h24": v_h24},
            "liquidity":   {"usd": liq_usd},
            "txns": {
                "m5":  {"buys": buys5m,  "sells": sells5m},
                "h1":  {"buys": buys1h,  "sells": sells1h,
                        "buyers": buyers1h, "sellers": sellers1h},   # unique wallets
                "h6":  {"buys": buys6h,  "sells": sells6h},
                "h24": {"buys": buys24h, "sells": sells24h},
            },
            "fdv":            float(attr.get("fdv_usd", 0) or 0),
            "_market_cap_usd": market_cap_usd,   # Phase 1: da GeckoTerminal
            "_source":        "geckoterminal",   # tag debug
        }
    except Exception as e:
        log.debug(f"[GeckoTerminal] Normalizzazione pool fallita: {e}")
        return None


def fetch_geckoterminal_pools(chain: str, limit: int = 20) -> list[dict]:
    """
    Recupera pool trending + nuovi da GeckoTerminal per la chain indicata.
    Ritorna una lista di pair nel formato Dexscreener (normalizzati).
    Free tier: 30 req/min, no API key.
    """
    network = _GECKO_NETWORK.get(chain)
    if not network:
        log.debug(f"[GeckoTerminal] Chain '{chain}' non supportata, skip.")
        return []

    base = GECKOTERMINAL_BASE
    headers = {"Accept": "application/json;version=20230302"}
    results: list[dict] = []
    seen_pairs: set = set()

    # Parametri comuni: include token base per symbol/address, ordina per attività tx
    _COMMON_PARAMS = "include=base_token,quote_token&sort=h24_tx_count_desc"

    def _fetch_endpoint(url: str, label: str) -> None:
        _gecko_throttle()
        data = _safe_get(f"{url}&{_COMMON_PARAMS}", headers=headers, label=label)
        if not data:
            return
        pools = (data.get("data") or [])
        for pool in pools:
            norm = _gecko_pool_to_dexscreener(pool, chain)
            if norm and norm["pairAddress"] not in seen_pairs:
                seen_pairs.add(norm["pairAddress"])
                results.append(norm)

    # Trending pools (token più scambiati)
    _fetch_endpoint(
        f"{base}/networks/{network}/trending_pools?page=1",
        f"GeckoTerminal-trending-{network}"
    )
    # Nuovi pool (token appena listati, max 24h) — throttle gestito in _fetch_endpoint
    _fetch_endpoint(
        f"{base}/networks/{network}/new_pools?page=1",
        f"GeckoTerminal-new-{network}"
    )

    log.info(f"[GeckoTerminal] {len(results)} pool grezzi trovati per '{network}' (trending+nuovi).")

    # ── Phase 1: filtri su dati unici wallet e market cap ──────────────────
    min_buyers = int(CONFIG.get("MIN_BUYERS_1H_WALLETS", 3))
    min_mcap   = float(CONFIG.get("MIN_MCAP_USD", 50_000))
    max_mcap   = float(CONFIG.get("MAX_MCAP_USD", 50_000_000))
    phase1_filtered = []
    p1_skip_wallet = 0
    p1_skip_mcap   = 0
    for p in results:
        txn1h   = (p.get("txns", {}) or {}).get("h1", {}) or {}
        buyers  = int(txn1h.get("buyers", 0) or 0)
        mcap    = float(p.get("_market_cap_usd", 0) or 0)
        sym     = (p.get("baseToken", {}) or {}).get("symbol", "?")
        # Filtro wallet unici (evita bot che generano mille tx con 1 wallet)
        if min_buyers > 0 and buyers > 0 and buyers < min_buyers:
            log.debug(f"[GeckoTerminal] ❌ {sym}: buyers_1h={buyers} < {min_buyers} wallets → skip")
            p1_skip_wallet += 1
            continue
        # Filtro market cap (presuppone che market_cap_usd sia valorizzato)
        if mcap > 0:
            if mcap < min_mcap:
                log.debug(f"[GeckoTerminal] ❌ {sym}: mcap=${mcap:,.0f} < ${min_mcap:,.0f} → skip")
                p1_skip_mcap += 1
                continue
            if mcap > max_mcap:
                log.debug(f"[GeckoTerminal] ❌ {sym}: mcap=${mcap:,.0f} > ${max_mcap:,.0f} → skip (maturo)")
                p1_skip_mcap += 1
                continue
        phase1_filtered.append(p)
    if p1_skip_wallet or p1_skip_mcap:
        log.info(f"[GeckoTerminal] Phase1: {p1_skip_wallet} skip wallet-unici, "
                 f"{p1_skip_mcap} skip mcap → {len(phase1_filtered)} rimasti.")

    # ── Phase 2: token info (socials) — max GECKO_MAX_INFO_CALLS per ciclo ──
    # Ordina per vol_1h desc per chiamare prima i token più rilevanti.
    # Token già in cache non contano verso il budget.
    require_socials  = CONFIG.get("GECKO_REQUIRE_SOCIALS", False)
    max_info_calls   = int(CONFIG.get("GECKO_MAX_INFO_CALLS", 8))
    phase2_filtered  = []
    info_calls_made  = 0
    # Ordina: priorità ai pool con più volume 1h (più interessanti)
    phase1_sorted = sorted(
        phase1_filtered,
        key=lambda p: float((p.get("volume", {}) or {}).get("h1", 0) or 0),
        reverse=True,
    )
    for p in phase1_sorted:
        token_addr = (p.get("baseToken", {}) or {}).get("address", "")
        sym        = (p.get("baseToken", {}) or {}).get("symbol", "?")
        if token_addr:
            cache_key = f"{network}:{token_addr.lower()}"
            already_cached = cache_key in _gecko_token_info_cache
            if not already_cached and info_calls_made >= max_info_calls:
                # Budget esaurito: passa senza info (neutro)
                log.debug(f"[GeckoTerminal] ℹ️  {sym}: info budget esaurito ({max_info_calls}), skip socials check")
                phase2_filtered.append(p)
                continue
            info = _fetch_gecko_token_info(network, token_addr)
            if not already_cached:
                if not any(info.values()):
                    # 429: attendi e riprova una volta prima di abortire
                    log.debug(f"[GeckoTerminal] Phase2 rate limit su {sym} — attendo 12s e riprovo")
                    import time as _t; _t.sleep(12)
                    info = _fetch_gecko_token_info(network, token_addr)
                    if not any(info.values()):
                        # Secondo fallimento: skip socials per tutti i restanti ma continua
                        log.info(f"[GeckoTerminal] ⚠️  Phase2 rate limit persistente su {sym} "
                                 f"— continuo senza socials check ({len(phase2_filtered)} già processati)")
                        phase2_filtered.append(p)
                        remaining_idx = phase1_sorted.index(p) + 1
                        phase2_filtered.extend(phase1_sorted[remaining_idx:])
                        break
                info_calls_made += 1
            p["_gecko_has_socials"]  = info["has_socials"]
            p["_gecko_has_twitter"]  = info["has_twitter"]
            p["_gecko_has_telegram"] = info["has_telegram"]
            p["_gecko_has_website"]  = info["has_website"]
            if require_socials and not info["has_socials"]:
                log.debug(f"[GeckoTerminal] ❌ {sym}: nessun social → skip (GECKO_REQUIRE_SOCIALS=True)")
                continue
            social_str = "/".join(filter(None, [
                "TW" if info["has_twitter"]  else "",
                "TG" if info["has_telegram"] else "",
                "WEB" if info["has_website"] else "",
            ])) or "NESSUNO"
            log.debug(f"[GeckoTerminal] ℹ️  {sym}: socials=[{social_str}]")
        phase2_filtered.append(p)
    if info_calls_made:
        log.debug(f"[GeckoTerminal] Phase2: {info_calls_made} chiamate token-info (cache={len(_gecko_token_info_cache)})")

    # ── Phase 3: OHLCV trend filter — max GECKO_MAX_OHLCV_CALLS per ciclo ──
    if not CONFIG.get("GECKO_OHLCV_ENABLED", True):
        final = phase2_filtered
    else:
        final        = []
        p3_skip      = 0
        ohlcv_calls  = 0
        max_ohlcv    = int(CONFIG.get("GECKO_MAX_OHLCV_CALLS", 6))
        for p in phase2_filtered:
            pair_addr = p.get("pairAddress", "")
            sym       = (p.get("baseToken", {}) or {}).get("symbol", "?")
            if not pair_addr or ohlcv_calls >= max_ohlcv:
                # Budget OHLCV esaurito → beneficio del dubbio
                p["_gecko_ohlcv_trend"] = "skip/budget"
                final.append(p)
                continue
            candles = _fetch_gecko_ohlcv(network, pair_addr, candles=6)
            ohlcv_calls += 1
            if not candles or len(candles) < 4:
                p["_gecko_ohlcv_trend"] = "unknown"
                final.append(p)
                continue
            # Analisi ultime 4 candele: close < open = candela rossa
            last4     = candles[-4:]
            red_count = sum(1 for c in last4 if len(c) >= 5 and c[4] < c[1])
            if red_count >= 3:
                log.debug(f"[GeckoTerminal] ❌ {sym}: {red_count}/4 candele rosse 15m → al picco, skip")
                p3_skip += 1
                continue
            last_c    = candles[-1]
            last_pct  = ((last_c[4] - last_c[1]) / (last_c[1] + 1e-12)) * 100 if len(last_c) >= 5 else 0
            p["_gecko_ohlcv_trend"] = f"red={red_count}/4 last={last_pct:+.1f}%"
            final.append(p)
        if p3_skip or ohlcv_calls:
            log.debug(f"[GeckoTerminal] Phase3: {ohlcv_calls} OHLCV calls, {p3_skip} al picco scartati → {len(final)} rimasti.")

    log.info(f"[GeckoTerminal] {len(final)} pool qualificati dopo Phase1+2+3 per '{network}'.")
    return final[:limit]


def fetch_dexscreener_boosts(chain: str = "ethereum") -> set:
    dex_id = CHAINS.get(chain, {}).get("dexscreener_id", chain)
    url_boosts = "https://api.dexscreener.com/token-boosts/latest/v1"
    data_boosts = _safe_get(url_boosts, label=f"Dexscreener-boosts-{dex_id}")
    if not data_boosts or not isinstance(data_boosts, list):
        return set()
    return {
        b.get("tokenAddress", "").lower()
        for b in data_boosts
        if b.get("chainId", "") == dex_id and b.get("tokenAddress")
    }


def fetch_dexscreener_pairs(chain: str = "ethereum", limit: int = 50) -> list[dict]:
    """
    Recupera i pair attivi da Dexscreener per la chain specificata.
    Ritorna una lista di dizionari con dati di mercato grezzi.
    In modalità mock genera dati simulati.
    """
    if CONFIG["USE_MOCK"]:
        return _mock_dexscreener_pairs(chain, n=limit)

    dex_id = CHAINS.get(chain, {}).get("dexscreener_id", chain)

    # ── Endpoint 1: token-boosts (token con hype/advertising recente) ──
    boost_addrs: set = set()
    profile_addrs: set = set()
    url_boosts = "https://api.dexscreener.com/token-boosts/latest/v1"
    data_boosts = _safe_get(url_boosts, label="Dexscreener-boosts")
    if data_boosts and isinstance(data_boosts, list):
        boost_addrs = {
            b.get("tokenAddress", "").lower()
            for b in data_boosts if b.get("chainId", "") == dex_id
        }
        log.info(f"[Dexscreener] {len(boost_addrs)} token boosted su '{dex_id}'.")

    # ── Endpoint 2: token-profiles (token con profilo verificato) ──
    if CONFIG.get("USE_TOKEN_PROFILES", True):
        url_profiles = "https://api.dexscreener.com/token-profiles/latest/v1"
        data_profiles = _safe_get(url_profiles, label="Dexscreener-profiles")
        if data_profiles and isinstance(data_profiles, list):
            profile_addrs = {
                p.get("tokenAddress", "").lower()
                for p in data_profiles if p.get("chainId", "") == dex_id
            }
            log.info(f"[Dexscreener] {len(profile_addrs)} token con profilo su '{dex_id}'.")

    # ── Endpoint 3: discovery multi-strategia (rimpiazza il singolo /dex/search sbagliato) ──
    # Il vecchio endpoint "q=solana token" cercava il testo nei simboli dei pair,
    # non tutti i pair della chain → restituiva 0-2 risultati.
    # Strategia corretta:
    #   3a. /dex/search?q=<dex_id>  — matcha il campo chainId/dexId, più risultati
    #   3b. /dex/tokens/<address>   — fetch diretto per ogni token boosted/profilato
    # I risultati vengono deduplicati per pairAddress.

    pairs_raw: list[dict] = []
    seen_pair_addrs: set = set()

    def _add_pairs(new_pairs: list[dict]) -> None:
        for p in new_pairs:
            pa = p.get("pairAddress", "")
            if pa and pa not in seen_pair_addrs:
                seen_pair_addrs.add(pa)
                pairs_raw.append(p)

    # 3a. Ricerca per chain id — più selettiva di "q=solana token"
    for query in [dex_id]:
        url_search = f"{DEXSCREENER_BASE}/dex/search"
        data = _safe_get(url_search, params={"q": query}, label=f"Dexscreener-search-{query}")
        # GUARD: data["pairs"] può essere None anche se la chiave esiste ({"pairs": null})
        raw_pairs = (data or {}).get("pairs") or []
        if raw_pairs:
            chain_pairs = [p for p in raw_pairs if p.get("chainId", "") == dex_id]
            _add_pairs(chain_pairs)
            log.debug(f"[Dexscreener] search '{query}' → {len(chain_pairs)} pair chain-match")

    # 3b. Fetch diretto dei pair per gli indirizzi boosted (max 10 per rate limit)
    for addr in list(boost_addrs)[:10]:
        url_tok = f"{DEXSCREENER_BASE}/dex/tokens/{addr}"
        data_tok = _safe_get(url_tok, label=f"Dexscreener-token-{addr[:8]}")
        raw_pairs = (data_tok or {}).get("pairs") or []  # GUARD: pairs può essere null
        if raw_pairs:
            chain_pairs = [p for p in raw_pairs if p.get("chainId", "") == dex_id]
            _add_pairs(chain_pairs)

    # 3c. Fetch per token profilati non già boosted
    for addr in list(profile_addrs - boost_addrs)[:8]:
        url_tok = f"{DEXSCREENER_BASE}/dex/tokens/{addr}"
        data_tok = _safe_get(url_tok, label=f"Dexscreener-profile-{addr[:8]}")
        raw_pairs = (data_tok or {}).get("pairs") or []  # GUARD: pairs può essere null
        if raw_pairs:
            chain_pairs = [p for p in raw_pairs if p.get("chainId", "") == dex_id]
            _add_pairs(chain_pairs)

    log.info(f"[Dexscreener] {len(pairs_raw)} pair grezzi raccolti per '{dex_id}' "
             f"prima dei filtri (boost={len(boost_addrs)}, profili={len(profile_addrs)}).")

    if not pairs_raw:
        log.warning(f"[Dexscreener] Nessun pair trovato per '{dex_id}' — chain skippata.")
        return []

    # ── Filtro età pair ──
    now_ms = time.time() * 1000
    min_age_ms = CONFIG.get("MIN_PAIR_AGE_HOURS", 0.25) * 3_600_000
    max_age_ms = CONFIG.get("MAX_PAIR_AGE_HOURS", 72)   * 3_600_000

    pairs_before_age = len(pairs_raw)
    pairs_raw = [
        p for p in pairs_raw
        if min_age_ms
           <= (now_ms - float(p.get("pairCreatedAt", now_ms) or now_ms))
           <= max_age_ms
    ]
    log.debug(f"[Dexscreener] Filtro età [{CONFIG['MIN_PAIR_AGE_HOURS']}h–{CONFIG['MAX_PAIR_AGE_HOURS']}h]: "
              f"{pairs_before_age} → {len(pairs_raw)} pair")

    pairs = pairs_raw

    # ── Carica watchlist condivisa per questa chain ────────────────────────
    # I token trovati da gemmeV3 (smart money + social) ricevono un boost
    # massimo nello score così finiscono sempre in cima al batch.
    watchlist_addrs: dict = {}
    if GEM_WATCHLIST_AVAILABLE:
        try:
            watchlist_addrs = get_watchlist_addresses(chain)
            if watchlist_addrs:
                log.info(
                    f"[Dexscreener] 💎 Watchlist gemmeV3: "
                    f"{len(watchlist_addrs)} token prioritizzati su '{dex_id}'."
                )
        except Exception as _e:
            log.debug(f"[Dexscreener] Errore caricamento watchlist: {_e}")

    # ── Fetch diretto per token watchlist non già nel batch ────────────────
    # Se gemmeV2 ha trovato una gemma che non compare nei boosted/trending,
    # la recuperiamo esplicitamente via /dex/tokens/{address}.
    if watchlist_addrs:
        existing_addrs = {
            (p.get("baseToken", {}).get("address", "") or "").lower()
            for p in pairs
        }
        missing_wl = [
            addr for addr in watchlist_addrs
            if addr not in existing_addrs
        ][:CONFIG.get("WATCHLIST_MAX_INJECT", 5)]

        for wl_addr in missing_wl:
            url_wl = f"{DEXSCREENER_BASE}/dex/tokens/{wl_addr}"
            data_wl = _safe_get(url_wl, label=f"Dexscreener-watchlist-{wl_addr[:8]}")
            raw_wl  = (data_wl or {}).get("pairs") or []
            if raw_wl:
                chain_wl = [p for p in raw_wl if p.get("chainId", "") == dex_id]
                _add_pairs(chain_wl)
                wl_entry = watchlist_addrs[wl_addr]
                log.info(
                    f"[watchlist] ✅ Iniettato {wl_entry.get('token_symbol','?')} "
                    f"({wl_addr[:8]}…) da gemmeV3 — "
                    f"P(gem)={wl_entry.get('gem_probability',0):.1%} | "
                    f"inflow=${wl_entry.get('inflow_usd',0):,.0f}"
                )

    # ── Score di priorità multi-segnale (early trend detection) ──
    # Obiettivo: identificare token che stanno INIZIANDO a muoversi,
    # non quelli che hanno già pompato (e rischiano lo scarico).
    #
    # Logica di scoring:
    #   0. Watchlist gemmeV2             → boost assoluto (+300): sempre in cima
    #   1. Boost/profilo Dexscreener     → segnale social/marketing attivo
    #   2. Volume spike 5m vs 1h         → attività recente in accelerazione
    #   3. Change 5m / Change 1h ratio   → momentum concentrato nelle ultime candele
    #   4. Penalità se change_1h > soglia→ probabile già partito / zona scarico
    #   5. Buy/sell ratio favorevole     → pressione acquisti in corso
    #   6. Penalità liquidità bassissima → rug risk / illiquidità pericolosa
    #   7. Penalità pair vecchio (> 48h) → opportunità già metabolizzata dal mercato
    def _priority(p):
        addr = (p.get("baseToken", {}).get("address", "") or "").lower()
        score = 0.0

        # ── 0. Watchlist gemmeV2 (boost assoluto) ──
        # I token validati da gemmeV2 (smart money + social + ML medio-termine)
        # vanno sempre analizzati per primi nel loop intraday.
        if addr in watchlist_addrs:
            wl = watchlist_addrs[addr]
            score += WATCHLIST_PRIORITY_BOOST
            # Bonus proporzionale alla qualità del segnale gemmeV2
            score += wl.get("gem_probability", 0) * 50      # max +50
            score += min(wl.get("inflow_usd", 0) / 10_000, 30)  # max +30 ogni $10k inflow
            log.debug(
                f"[watchlist] 💎 {p.get('baseToken',{}).get('symbol','?')} "
                f"in watchlist — score boost +{WATCHLIST_PRIORITY_BOOST:.0f}"
            )

        # ── 1. Segnali social/marketing ──
        if addr in boost_addrs:   score += 150
        if addr in profile_addrs: score += 80

        priceChange = p.get("priceChange", {}) or {}
        volume      = p.get("volume", {})      or {}
        txns        = p.get("txns", {})        or {}
        liquidity   = p.get("liquidity", {})   or {}

        ch5m  = float(priceChange.get("m5",  0) or 0)
        ch1h  = float(priceChange.get("h1",  0) or 0)
        ch6h  = float(priceChange.get("h6",  0) or 0)
        vol5m = float(volume.get("m5", 0)  or 0)
        vol1h = float(volume.get("h1", 0)  or 0)
        liq   = float(liquidity.get("usd", 0) or 0)
        txn1h = txns.get("h1", {}) or {}
        buys1h  = float(txn1h.get("buys",  0) or 0)
        sells1h = float(txn1h.get("sells", 0) or 0)

        age_hours = (now_ms - float(p.get("pairCreatedAt", now_ms) or now_ms)) / 3_600_000

        # ── 2. Volume spike 5m vs media oraria ──
        # Se vol5m > media minuto dell'1h → attività in accelerazione
        avg_vol_per_5m = vol1h / 12.0 if vol1h > 0 else 1.0
        vol_spike_ratio = vol5m / (avg_vol_per_5m + 1.0)
        score += min(vol_spike_ratio * 30, 90)  # cap a 90 punti

        # ── 3. Momentum concentrato nelle ultime candele ──
        # Vogliamo ch5m positivo ma ch1h ancora contenuto (trend che inizia)
        if ch5m > 0 and ch1h > 0:
            early_ratio = ch5m / (abs(ch1h) + 0.01)
            score += min(early_ratio * 15, 60)

        # ── 4. Penalità: post-pump E pre-dump (entrambi nemici del pre-pump) ──
        # Post-pump: change_1h > 25% → distribuzione imminente
        if ch1h > 25:
            score -= (ch1h - 25) * 3.0   # soglia abbassata + penalità maggiore
        if ch6h > 60:
            score -= (ch6h - 60) * 2.0

        # Pre-dump: change negativo → token sta già scendendo
        if ch1h < -5:
            score -= abs(ch1h + 5) * 4.0  # penalità forte per token in discesa
        if ch5m < -3:
            score -= abs(ch5m + 3) * 6.0  # spike negativo recente = vendita aggressiva

        # ── 5. Buy/sell ratio: pressione acquisti ──
        if sells1h > 0:
            bsr_val = buys1h / sells1h
            if bsr_val > 1.2:
                score += min((bsr_val - 1.0) * 20, 40)
            elif bsr_val < 0.8:
                score -= 50  # sell pressure dominante = segnale dump
        elif buys1h > 5:
            score += 20  # solo acquisti, nessuna vendita

        # ── 5b. Bonus accumulo silenzioso (pattern pre-pump classico) ──
        # Prezzo fermo + volume che esplode = qualcuno compra senza far salire il prezzo
        avg_vol_5m_base = vol1h / 12.0 if vol1h > 0 else 1.0
        vol_spike_val = vol5m / (avg_vol_5m_base + 1.0)
        if vol_spike_val > 1.5 and abs(ch1h) < 10 and abs(ch5m) < 5:
            score += min(vol_spike_val * 15, 50)

        # ── 6. Penalità liquidità bassa (rug risk) ──
        min_liq = CONFIG.get("MIN_LIQUIDITY_USD", 5_000)
        if liq < min_liq:
            score -= 80
        elif liq < min_liq * 2:
            score -= 30

        # ── 7. Preferenza pair recenti ma non appena creati ──
        # Finestra ideale: 30 minuti – 12 ore (abbastanza giovane, già liquido)
        if 0.5 <= age_hours <= 12:
            score += 25
        elif age_hours > 48:
            score -= 15

        return -score  # negativo per sort ascendente (migliore = più basso)

    pairs.sort(key=_priority)

    # ── Log diagnostico top-5 ──
    for p in pairs[:5]:
        sym  = p.get("baseToken", {}).get("symbol", "?")
        ch5m = (p.get("priceChange", {}) or {}).get("m5", 0)
        ch1h = (p.get("priceChange", {}) or {}).get("h1", 0)
        v5m  = (p.get("volume", {}) or {}).get("m5", 0)
        liq  = (p.get("liquidity", {}) or {}).get("usd", 0)
        age  = round((now_ms - float(p.get("pairCreatedAt", now_ms) or now_ms)) / 3_600_000, 1)
        log.debug(
            f"[trend] {sym:10s} | ch5m={ch5m:+.1f}% ch1h={ch1h:+.1f}% "
            f"| vol5m=${v5m:,.0f} | liq=${liq:,.0f} | età={age}h"
        )

    log.info(f"[Dexscreener] {len(pairs)} pair trovati per '{dex_id}' "
             f"(boost={len(boost_addrs)}, profili={len(profile_addrs)}).")
    return pairs[:limit]


def fetch_moralis_token_data(token_address: str, chain: str = "ethereum") -> dict:
    """
    Recupera dati on-chain da Moralis con supporto duale EVM/Solana.
    """
    if CONFIG["USE_MOCK"]:
        return _mock_moralis_data(token_address)

    _empty = {"holders_total": 0, "top_10_holder_pct": 0.0,
              "transfers_1h": 0, "holders_change_1h": 0}

    if not CONFIG.get("MORALIS_ENABLED", True):
        return _empty

    if not _CHAIN_META.get(chain, {}).get("is_evm", True):
        log.debug(f"[Moralis] Chain '{chain}' non EVM, skip.")
        return _empty

    # Inizializzazione risultato
    risultato = {
        "holders_total": 0,
        "top_10_holder_pct": 0.0,
        "transfers_1h": 0,
        "holders_change_1h": 0,
    }

    headers = _get_headers_moralis()
    moralis_chain = _CHAIN_META.get(chain, {}).get("moralis_chain", "eth")
    is_evm = _CHAIN_META.get(chain, {}).get("is_evm", True)

    try:
        if is_evm:
            # --- LOGICA EVM (BSC, Ethereum, etc.) ---
            base_url = "https://deep-index.moralis.io/api/v2.2"
            
            # 1. Owners/Holders
            url_owners = f"{base_url}/erc20/{token_address}/owners"
            data_owners = _safe_get(url_owners, params={"chain": moralis_chain, "limit": 10}, headers=headers)
            if data_owners:
                risultato["holders_total"] = int(data_owners.get("total", 0) or 0)
                top_holders = data_owners.get("result", [])
                if top_holders:
                    # Nota: calcola la % dei top-10 sulla supply restituita (non supply totale)
                    total_sum = sum(float(h.get("balance", 0) or 0) for h in top_holders)
                    top_10_sum = sum(float(h.get("balance", 0) or 0) for h in top_holders[:10])
                    if total_sum > 0:
                        risultato["top_10_holder_pct"] = round((top_10_sum / total_sum) * 100, 2)

            # 2. Transfers
            url_transfers = f"{base_url}/erc20/{token_address}/transfers"
            data_transfers = _safe_get(url_transfers, params={"chain": moralis_chain, "limit": 100}, headers=headers)
            if data_transfers:
                un_ora_fa = datetime.now() - timedelta(hours=1)
                transfers = data_transfers.get("result", [])
                recenti = [t for t in transfers if _parse_iso(t.get("block_timestamp", "")) >= un_ora_fa]
                risultato["transfers_1h"] = len(recenti)

        else:
            # --- LOGICA SOLANA ---
            # Nota: Solana usa un base URL differente
            sol_base_url = "https://solana-gateway.moralis.io"
            
            # 1. Token Stats (Holders su Solana)
            url_stats = f"{sol_base_url}/token/{moralis_chain}/{token_address}/stats"
            data_stats = _safe_get(url_stats, headers=headers)
            if data_stats:
                # Moralis Solana API fornisce spesso i dati già aggregati
                risultato["holders_total"] = int(data_stats.get("holders", 0))

    except Exception as e:
        log.error(f"[Moralis] Errore critico durante fetch per {token_address}: {e}")

    return risultato

def fetch_goplus_security(token_address: str, chain: str = "ethereum") -> dict:
    return {}  # GoPlus disabilitato
    if CONFIG["USE_MOCK"]:
        return _mock_goplus_data(token_address)

    # GoPlus supporta solo chain EVM; Solana → skip
    if not CHAINS.get(chain, {}).get("is_evm", True):
        return {"is_honeypot": False, "buy_tax": 0.0, "sell_tax": 0.0,
                "lp_locked": None, "is_open_source": None, "is_mintable": None}

    # Usa _CHAIN_META (superset) per il lookup: copre chain non nel loop attivo
    chain_cfg = _CHAIN_META.get(chain)
    if chain_cfg is None:
        log.warning(f"[GoPlus] Chain '{chain}' non in _CHAIN_META — skip sicurezza.")
        return {"is_honeypot": None, "buy_tax": 0.0, "sell_tax": 0.0,
                "lp_locked": None, "is_open_source": None, "is_mintable": None}
    chain_id = chain_cfg["goplus_chain_id"]
    url = f"{GOPLUS_BASE}/token_security/{chain_id}"
    data = _safe_get(url, params={"contract_addresses": token_address},
                     label=f"GoPlus-{chain}")

    # Valore di default (sicuro per non bloccare l'analisi)
    default = {
        "is_honeypot":  None,
        "buy_tax":      0.0,
        "sell_tax":     0.0,
        "lp_locked":    None,
        "is_open_source": None,
        "is_mintable":  None,
    }

    if not data or data.get("code") != 1:
        return default

    result = data.get("result", {})
    # GoPlus restituisce l'indirizzo come chiave (può essere uppercase o lowercase)
    token_data = (
        result.get(token_address.lower())
        or result.get(token_address)
        or {}
    )

    def _safe_float(v, scale: float = 1.0) -> float:
        """Converte un valore GoPlus (stringa o numero) in float."""
        try:
            f = float(v or 0)
            # GoPlus a volte restituisce valori 0–1, a volte 0–100
            return f * scale if f <= 1.0 else f
        except (TypeError, ValueError):
            return 0.0

    return {
        "is_honeypot":    str(token_data.get("is_honeypot", "0")) == "1",
        "buy_tax":        _safe_float(token_data.get("buy_tax"), scale=100),
        "sell_tax":       _safe_float(token_data.get("sell_tax"), scale=100),
        "lp_locked":      str(token_data.get("lp_holder_analysis", [{}])[0].get("is_locked", "0")) == "1"
                          if token_data.get("lp_holder_analysis") else None,
        "is_open_source": str(token_data.get("is_open_source", "0")) == "1",
        "is_mintable":    str(token_data.get("is_mintable", "0")) == "1",
    }


# ==============================================================================
# SEZIONE 3 – GENERATORI DI DATI MOCK (sviluppo senza API reali)
# ==============================================================================

def _mock_dexscreener_pairs(chain: str, n: int = 30) -> list[dict]:
    """
    Genera n pair simulati con dati realistici per sviluppo e test.
    La struttura è identica a quella reale di Dexscreener.
    """
    # Seed variabile per garantire diversità tra snapshot → clustering significativo
    rng = np.random.default_rng(seed=(int(time.time() * 1000) + hash(chain)) % 2**31)
    pairs = []
    simboli = [
        "ALPHA", "BETA", "GAMMA", "DELTA", "ZETA", "ETA", "THETA",
        "IOTA", "KAPPA", "LAMBDA", "MU", "NU", "XI", "OMICRON",
        "PI", "RHO", "SIGMA", "TAU", "UPSILON", "PHI", "CHI",
        "PSI", "OMEGA", "AURA", "NOVA", "FLUX", "EDGE", "CORE",
        "PEAK", "VOLT"
    ]
    quote = "WETH" if chain in ("ethereum", "base") else "WBNB"

    for i in range(n):
        simbolo = simboli[i % len(simboli)]

        # Genera dati di mercato con distribuzioni realistiche
        prezzo      = float(rng.lognormal(mean=-3.0, sigma=2.5))
        liq_usd     = float(rng.lognormal(mean=11.0, sigma=1.5))
        vol_1h      = float(rng.lognormal(mean=9.0,  sigma=1.8))
        vol_24h     = vol_1h * float(rng.uniform(12, 30))
        buys_1h     = int(rng.integers(5, 400))
        sells_1h    = int(rng.integers(3, 350))
        change_1h   = float(rng.normal(0.5, 8.0))   # % cambio prezzo 1h
        change_5m   = float(rng.normal(0.1, 3.0))
        change_15m  = float(rng.normal(0.2, 5.0))
        change_24h  = float(rng.normal(1.0, 20.0))
        created_ago = int(rng.integers(3600, 30 * 24 * 3600))  # secondi fa

        pairs.append({
            "chainId":      chain,
            "dexId":        "uniswap" if chain == "ethereum" else "pancakeswap",
            "pairAddress":  f"0x{rng.integers(0, 2**32):032x}{i:08x}",
            "baseToken": {
                "address": f"0x{rng.integers(0, 2**32):032x}{i+1:08x}",
                "name":    f"{simbolo} Token",
                "symbol":  simbolo,
            },
            "quoteToken": {"symbol": quote},
            "priceUsd":   str(prezzo),
            "volume":     {"m1": vol_1h/60, "m5": vol_1h/12,
                           "h1": vol_1h, "h24": vol_24h},
            "liquidity":  {"usd": liq_usd},
            "priceChange":{"m5": change_5m, "m15": change_15m,
                           "h1": change_1h, "h24": change_24h},
            "txns": {
                "h1":  {"buys": buys_1h,  "sells": sells_1h},
                "h24": {"buys": buys_1h*20, "sells": sells_1h*18},
            },
            "fdv": prezzo * float(rng.lognormal(18, 2)),
            "pairCreatedAt": int(time.time() * 1000) - created_ago * 1000,
        })
    return pairs


def _mock_moralis_data(token_address: str) -> dict:
    """
    Genera dati on-chain simulati (Moralis) per un token.
    La distribuzione è realistica: la maggior parte dei token
    ha pochi holder e alta concentrazione.
    """
    rng = np.random.default_rng(seed=hash(token_address) % 2**31)
    holders = int(rng.integers(50, 15000))
    top_pct = float(rng.uniform(15, 85))  # % supply nei top 10 wallet
    transfers = int(rng.integers(0, 200))
    change_h  = int(rng.integers(-20, 80))
    return {
        "holders_total":     holders,
        "top_10_holder_pct": top_pct,
        "transfers_1h":      transfers,
        "holders_change_1h": change_h,
    }


def _mock_goplus_data(token_address: str) -> dict:
    """
    Genera flag di sicurezza simulati (GoPlus) per un token.
    ~5% dei token simulati è un honeypot, con tax elevate.
    """
    rng = np.random.default_rng(seed=hash(token_address + "gp") % 2**31)
    honeypot = bool(rng.random() < 0.05)  # 5% honeypot
    buy_tax  = float(rng.choice([0, 0, 0, 1, 2, 5, 10, 15, 25, 99],
                                p=[.45,.15,.15,.1,.05,.04,.03,.01,.01,.01]))
    sell_tax = buy_tax + float(rng.uniform(0, 3)) if not honeypot else 99.0
    return {
        "is_honeypot":    honeypot,
        "buy_tax":        buy_tax,
        "sell_tax":       min(sell_tax, 99.0),
        "lp_locked":      bool(rng.random() > 0.4),
        "is_open_source": bool(rng.random() > 0.3),
        "is_mintable":    bool(rng.random() < 0.2),
    }


def _parse_iso(s: str) -> datetime:
    """Converte una stringa ISO 8601 in datetime UTC (con fallback)."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return datetime(2000, 1, 1)

# ==============================================================================
# SEZIONE 4 – RACCOLTA DATI E COSTRUZIONE DATASET
# ==============================================================================

def fetch_onchain_and_market_data(chain: str = "ethereum") -> pd.DataFrame:
    """
    Pipeline principale di raccolta dati.
    Per ogni token nella batch:
      1. Recupera pair da Dexscreener
      2. Arricchisce con dati on-chain da Moralis
      3. Aggiunge flag di sicurezza da GoPlus
    Ritorna un DataFrame con tutti i dati grezzi.
    """
    log.info(f"[fetch] Recupero pair su {chain}...")
    # Debug lists inizializzate una sola volta per ciclo
    debug_filter_records: list[dict] = []
    debug_candidates: list[dict] = []
    # FIX: fetch_dexscreener_pairs() chiama già internamente boosts+profiles.
    # La chiamata separata a fetch_dexscreener_boosts() era duplicata → rimossa.
    boost_addrs: set = set()  # placeholder per il log sotto (non serve più ricalcolarlo)
    pairs = fetch_dexscreener_pairs(chain, limit=CONFIG["BATCH_SIZE"])
    log.info(f"[fetch] Trovati {len(pairs)} pair dopo scoring/filtri Dexscreener.")

    # ── Arricchimento con pool GeckoTerminal (discovery complementare) ──────
    # GeckoTerminal usa sorgenti diverse da Dexscreener: trova token in trend
    # che non appaiono nei boost/profili Dexscreener (utile per Solana).
    if CONFIG.get("GECKOTERMINAL_ENABLED", True):
        gecko_pools = fetch_geckoterminal_pools(chain, limit=20)
        if gecko_pools:
            # Deduplica per pairAddress (evita doppioni con Dexscreener)
            existing_pairs = {p.get("pairAddress", "").lower() for p in pairs}
            existing_bases = {
                (p.get("baseToken", {}).get("address", "") or "").lower()
                for p in pairs
            }
            # Filtro volume minimo per pool GeckoTerminal:
            # i micro-token (vol_1h < MIN_VOL_1H_GECKO) entrano col BSR=1.0 hardcoded
            # e escono quasi subito via bsr_collapse → alto rumore, basso segnale.
            min_gecko_vol = CONFIG.get("MIN_VOL_1H_GECKO", 10_000)
            gecko_filtered = []
            gecko_skipped = 0
            for p in gecko_pools:
                vol = float((p.get("volume", {}) or {}).get("h1", 0) or 0)
                if vol < min_gecko_vol:
                    gecko_skipped += 1
                    continue
                gecko_filtered.append(p)
            if gecko_skipped:
                log.debug(
                    f"[GeckoTerminal] {gecko_skipped} pool scartati per vol_1h < ${min_gecko_vol:,.0f}."
                )
            new_pools = [
                p for p in gecko_filtered
                if p.get("pairAddress", "").lower() not in existing_pairs
                and (p.get("baseToken", {}).get("address", "") or "").lower() not in existing_bases
            ]
            pairs.extend(new_pools)
            log.info(
                f"[GeckoTerminal] +{len(new_pools)} nuovi pool aggiunti "
                f"({gecko_skipped} vol<{min_gecko_vol/1000:.0f}k scartati, "
                f"{len(gecko_pools) - len(new_pools) - gecko_skipped} già presenti da Dexscreener)."
            )
    log.info(f"[fetch] Totale {len(pairs)} pair candidati (Dexscreener + GeckoTerminal).")

    # ── Log riepilogativo di tutti i pair candidati ──
    # Permette di vedere COSA sta guardando il bot ad ogni ciclo
    # senza dover mettere breakpoint o ispezionare l'API a mano.
    for _i, _p in enumerate(pairs):
        _bt   = _p.get("baseToken", {}) or {}
        _ch   = _p.get("priceChange", {}) or {}
        _vol  = _p.get("volume", {}) or {}
        _liq  = _p.get("liquidity", {}) or {}
        _txn  = (_p.get("txns", {}) or {}).get("h1", {}) or {}
        _age  = round((time.time()*1000 - float(_p.get("pairCreatedAt", time.time()*1000) or time.time()*1000)) / 3_600_000, 1)
        _bsr  = float(_txn.get("buys", 0) or 0) / (float(_txn.get("sells", 0) or 0) + 1)
        _boost = ("[BOOST]" if (_bt.get("address","") or "").lower() in boost_addrs else "")
        log.debug(
            f"[fetch]  #{_i+1:02d} {_bt.get('symbol','?'):>10s} | "
            f"{_bt.get('name','?')[:20]:<20s} | "
            f"addr={(_bt.get('address','') or '')[:8]}… | "
            f"5m={_ch.get('m5',0):+6.1f}% 1h={_ch.get('h1',0):+7.1f}% | "
            f"vol5m=${float(_vol.get('m5',0) or 0):>8,.0f} "
            f"vol1h=${float(_vol.get('h1',0) or 0):>9,.0f} | "
            f"liq=${float(_liq.get('usd',0) or 0):>9,.0f} | "
            f"bsr={_bsr:.2f} | "
            f"età={_age}h {_boost}"
        )

    righe = []
    for pair in pairs:
        indirizzo = pair.get("baseToken", {}).get("address", "")
        if not indirizzo:
            continue

        # --- Dati Dexscreener (mercato) ---
        volume    = pair.get("volume", {})
        liquidity = pair.get("liquidity", {})
        change    = pair.get("priceChange", {})
        txns_5m   = pair.get("txns", {}).get("m5", {})
        txns_1h   = pair.get("txns", {}).get("h1", {})
        txns_6h   = pair.get("txns", {}).get("h6", {})
        txns_24h  = pair.get("txns", {}).get("h24", {})

        riga = {
            "timestamp":      datetime.now().isoformat(),
            "token_address":  indirizzo,
            "token_symbol":   pair.get("baseToken", {}).get("symbol", ""),
            "token_name":     pair.get("baseToken", {}).get("name", ""),
            "chain":          chain,
            "pair_address":   pair.get("pairAddress", ""),
            # Prezzi e variazioni
            "price_usd":      float(pair.get("priceUsd", 0) or 0),
            "fdv":            float(pair.get("fdv", 0) or 0),  # Fully Diluted Valuation
            "change_5m_pct":  float(change.get("m5",  0) or 0),
            "change_15m_pct": float(change.get("m15", 0) or 0),
            "change_1h_pct":  float(change.get("h1",  0) or 0),
            "change_24h_pct": float(change.get("h24", 0) or 0),
            # Volumi
            "volume_1m_usd":  float(volume.get("m1",  0) or 0),
            "volume_5m_usd":  float(volume.get("m5",  0) or 0),
            "volume_1h_usd":  float(volume.get("h1",  0) or 0),
            "volume_24h_usd": float(volume.get("h24", 0) or 0),
            # Liquidità
            "liquidity_usd":  float(liquidity.get("usd", 0) or 0),
            # Transazioni (tx count)
            "buys_1h":        int(txns_1h.get("buys",  0) or 0),
            "sells_1h":       int(txns_1h.get("sells", 0) or 0),
            "buys_5m":        int(txns_5m.get("buys",  0) or 0),
            "sells_5m":       int(txns_5m.get("sells", 0) or 0),
            "buys_6h":        int(txns_6h.get("buys",  0) or 0),
            "sells_6h":       int(txns_6h.get("sells", 0) or 0),
            "buys_24h":       int(txns_24h.get("buys",  0) or 0),
            "sells_24h":      int(txns_24h.get("sells", 0) or 0),
            # Wallet unici (solo GeckoTerminal, 0 per Dexscreener)
            "buyers_1h_wallets":  int(txns_1h.get("buyers",  0) or 0),
            "sellers_1h_wallets": int(txns_1h.get("sellers", 0) or 0),
            # Market cap (GeckoTerminal) e volume 6h
            "market_cap_usd": float(pair.get("_market_cap_usd", 0) or 0),
            "volume_6h_usd":  float((pair.get("volume", {}) or {}).get("h6", 0) or 0),
            # Segnali qualitativi Phase 2 (socials)
            "gecko_has_socials":  int(pair.get("_gecko_has_socials",  False) or False),
            "gecko_has_twitter":  int(pair.get("_gecko_has_twitter",  False) or False),
            "gecko_has_telegram": int(pair.get("_gecko_has_telegram", False) or False),
            # Età del pair (ore dalla creazione)
            "pair_age_hours": (
                (time.time() * 1000 - float(pair.get("pairCreatedAt", time.time()*1000)))
                / 3_600_000
            ),
        }

        # --- Dati on-chain Moralis ---
        moralis = fetch_moralis_token_data(indirizzo, chain)
        riga.update(moralis)

        # --- Flag di sicurezza GoPlus ---
        security = fetch_goplus_security(indirizzo, chain)
        riga.update({
            "is_honeypot":    int(security.get("is_honeypot", False) or False),
            "buy_tax":        float(security.get("buy_tax", 0) or 0),
            "sell_tax":       float(security.get("sell_tax", 0) or 0),
            "lp_locked":      int(security.get("lp_locked", False) or False),
            "is_open_source": int(security.get("is_open_source", False) or False),
            "is_mintable":    int(security.get("is_mintable", False) or False),
        })
        # --- DEBUG: reasons e salvataggio su CSV (inserire subito prima di righe.append(riga)) ---
        # --- DEBUG: reasons e accumulo record (prima di righe.append(riga)) ---
        reasons = []
        if riga["liquidity_usd"] < CONFIG["MIN_LIQUIDITY_USD"]:
            reasons.append(f"liq=${riga['liquidity_usd']:.0f}<min")
        if riga["volume_1h_usd"] < CONFIG["MIN_VOLUME_1H_USD"]:
            reasons.append(f"vol1h=${riga['volume_1h_usd']:.0f}<min")
        if riga.get("is_honeypot"):
            reasons.append("honeypot")
        if riga.get("buy_tax", 0) > CONFIG["MAX_BUY_TAX_PCT"]:
            reasons.append(f"buy_tax={riga['buy_tax']:.1f}%>max")
        if riga.get("sell_tax", 0) > CONFIG["MAX_SELL_TAX_PCT"]:
            reasons.append(f"sell_tax={riga['sell_tax']:.1f}%>max")
        if riga.get("change_1h_pct", 0) > CONFIG["MAX_CHANGE_1H_PCT"]:
            reasons.append(f"change_1h={riga['change_1h_pct']:.1f}%>max (postpump)")
        if riga.get("change_1h_pct", 0) < CONFIG["MIN_CHANGE_1H_PCT"]:
            reasons.append(f"change_1h={riga['change_1h_pct']:.1f}%<min (dump)")
            
        passed_filters = len(reasons) == 0

        if passed_filters:
            log.info(f"[filtri] ✅ PASSATO  {chain} ({riga['token_address'][:8]}…) — liq=${riga['liquidity_usd']:.0f}, vol1h=${riga['volume_1h_usd']:.0f}")
        else:
            log.debug(f"[filtri] ❌ SCARTATO {chain} ({riga['token_address'][:8]}…) — " + ", ".join(reasons))

        debug_filter_records.append({
            "timestamp": datetime.now().isoformat(),
            "chain": chain,
            "token_address": riga["token_address"],
            "symbol": riga.get("token_symbol",""),
            "liquidity_usd": riga["liquidity_usd"],
            "volume_1h_usd": riga["volume_1h_usd"],
            "passed": int(passed_filters),
            "reasons": ";".join(reasons)
        })

        if passed_filters:
            debug_candidates.append({
                "timestamp": datetime.now().isoformat(),
                "chain": chain,
                "token_address": riga["token_address"],
                "symbol": riga.get("token_symbol",""),
                "price_usd": riga.get("price_usd", 0),
                "liquidity_usd": riga["liquidity_usd"],
                "volume_1h_usd": riga["volume_1h_usd"],
                "buys_1h": riga.get("buys_1h", 0),
                "sells_1h": riga.get("sells_1h", 0),
                # Feature 5m per backtest offline condizioni pre-pump (c2/c6/c11)
                # su Base: il flusso nativo emette 0 segnali ma il pump-rate Base
                # è pari a Solana — servono questi dati per ricalibrare le soglie.
                "volume_5m_usd": riga.get("volume_5m_usd", 0),
                "change_5m_pct": riga.get("change_5m_pct", 0),
                "change_1h_pct": riga.get("change_1h_pct", 0),
                "buys_5m": riga.get("buys_5m", 0),
                "sells_5m": riga.get("sells_5m", 0),
            })
        # --- fine blocco debug ---

        righe.append(riga)

    df = pd.DataFrame(righe)

    # ── Salvataggio debug su disco (sempre, indipendentemente dal contenuto del df) ──
    out_dir = Path(__file__).resolve().parent / "debug"   # ancorato al modulo (no CWD)
    out_dir.mkdir(exist_ok=True)

    if debug_filter_records:
        keys = ["timestamp", "chain", "token_address", "symbol",
                "liquidity_usd", "volume_1h_usd", "passed", "reasons"]
        filters_file = out_dir / "debug_filters.csv"
        write_header = not filters_file.exists()
        with filters_file.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=keys)
            if write_header:
                writer.writeheader()
            writer.writerows(debug_filter_records)
        log.info(f"[filtri] Filter log appended to {filters_file}")

    if debug_candidates:
        keys2 = ["timestamp", "chain", "token_address", "symbol",
                 "price_usd", "liquidity_usd", "volume_1h_usd", "buys_1h", "sells_1h",
                 "volume_5m_usd", "change_5m_pct", "change_1h_pct", "buys_5m", "sells_5m"]
        candidates_file = out_dir / "debug_candidates.csv"
        # Schema cambiato (colonne 5m aggiunte): ruota il file vecchio per non
        # disallineare l'header esistente con le nuove righe.
        if candidates_file.exists():
            with candidates_file.open(encoding="utf-8") as fh:
                old_header = fh.readline().strip()
            if old_header != ",".join(keys2):
                candidates_file.rename(
                    out_dir / f"debug_candidates_old_{datetime.now():%Y%m%d_%H%M%S}.csv")
        write_header2 = not candidates_file.exists()
        with candidates_file.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=keys2)
            if write_header2:
                writer.writeheader()
            writer.writerows(debug_candidates)
        log.info(f"[filtri] Candidates appended to {candidates_file}")

    if df.empty:
        log.warning("[fetch] DataFrame vuoto — nessun pair superato i controlli.")
    else:
        log.info(
            f"[fetch] ✓ Dataset grezzo: {df.shape[0]} token, {df.shape[1]} colonne | "
            f"liq media ${df['liquidity_usd'].mean():,.0f} | "
            f"vol1h media ${df['volume_1h_usd'].mean():,.0f}"
        )

    return df


def build_dataset(n_snapshots: int = 500) -> pd.DataFrame:
    """
    Costruisce un dataset storico simulato per il training con target FORWARD-LOOKING.

    Struttura corretta (nessun data leakage):
    ─────────────────────────────────────────
    Ogni riga rappresenta lo stato di un token a t₀.
    Il target viene calcolato simulando il prezzo a t₀ + LOOKAHEAD_MINUTES,
    usando SOLO variabili causalmente precedenti al target stesso.

    La simulazione modella REGIMI DI MERCATO distinti (non un'unica distribuzione):
      • Regime PUMP   – token con struttura pre-pump reale (early momentum, bsr > 1,
                        volume spike concentrato nelle ultime candele, holder in crescita,
                        pair giovane, liquidità media)
      • Regime DUMP   – token già pompati o manipolati (volume spike ma già al picco,
                        bsr < 1, pair aging, top holder concentrati)
      • Regime FLAT   – token normali senza segnali particolari (la maggioranza)
      • Regime RUG    – token con caratteristiche di rug pull (liquidity bassa,
                        high sell tax, is_mintable, top holder > 70%)

    La probabilità di pump FUTURA dipende dal regime e da variabili
    strutturali del token al t₀ — NON da change_1h_pct o altri indicatori
    che già incorporano il movimento attuale del prezzo.

    In produzione questa funzione viene sostituita da un caricamento
    da database time-series (TimescaleDB, InfluxDB, Parquet su S3).
    """
    log.info(f"[build_dataset] Generazione dataset con {n_snapshots} snapshot "
             f"(target forward-looking, LOOKAHEAD={CONFIG['LOOKAHEAD_MINUTES']}min)...")

    rng = np.random.default_rng(CONFIG["RANDOM_STATE"])

    # ── Parametri di regime ──────────────────────────────────────────────────
    # Distribuzione realistica: la maggioranza dei token è flat/irrilevante
    REGIMI = ["flat", "pre_pump", "post_pump", "rug"]
    PESI   = [0.55,   0.25,       0.12,        0.08]   # pre_pump: 18%→25%, flat: 62%→55%

    n_chains = len(CHAINS)
    rows_per_chain = n_snapshots // n_chains

    df_list = []

    for chain in list(CHAINS.keys()):
        for i in range(rows_per_chain):

            regime = rng.choice(REGIMI, p=PESI)

            # ── t₀: timestamp casuale nelle ultime 90 giorni ──
            ago_minutes = int(rng.integers(60, 90 * 24 * 60))
            t0 = datetime.now() - timedelta(minutes=ago_minutes)

            # ── Genera token identity ──
            addr_bytes = rng.bytes(20)
            token_address = "0x" + addr_bytes.hex()
            token_symbol  = "".join(
                rng.choice(list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"), size=rng.integers(3, 6))
            )

            # ── Genera caratteristiche strutturali (NON usate come feature di prezzo) ──
            pair_age_hours   = float(rng.uniform(0.3, 96))
            holders_total    = int(rng.integers(30, 8000))
            top_10_holder_pct = float(rng.uniform(10, 90))
            lp_locked        = int(rng.random() > 0.4)
            is_open_source   = int(rng.random() > 0.3)
            is_mintable      = int(rng.random() < 0.15)

            # ── Genera metriche di mercato condizionate al regime ──
            # Ogni regime ha distribuzioni plausibili ma DISTINTE
            # Le feature di prezzo (change_Xm_pct) rappresentano il recente passato
            # PRIMA del pump futuro — non coincidono con il target

            if regime == "pre_pump":
                # Segnali early: piccolo momentum, volume in leggera crescita,
                # buy pressure positiva, holder in aumento — ma NON ancora esploso
                liquidity_usd      = float(rng.lognormal(9.5, 0.8))   # ~14k median
                volume_24h_usd     = float(rng.lognormal(9.0, 1.0))
                vol_spike_factor   = float(rng.uniform(1.3, 3.5))      # volume 1h > media
                volume_1h_usd      = volume_24h_usd / 24 * vol_spike_factor
                volume_5m_usd      = volume_1h_usd / 12 * rng.uniform(1.2, 2.5)
                change_5m_pct      = float(rng.uniform(0.5, 8.0))
                change_15m_pct     = float(rng.uniform(1.0, 12.0))
                change_1h_pct      = float(rng.uniform(2.0, 18.0))    # ancora contenuto
                change_24h_pct     = float(rng.normal(5.0, 15.0))
                buys_1h            = int(rng.integers(30, 300))
                sells_1h           = int(rng.integers(10, int(buys_1h * 0.75) + 1))
                holders_change_1h  = int(rng.integers(-5, 80))
                buy_tax            = float(rng.choice([0, 0, 1, 3], p=[.5, .3, .15, .05]))
                sell_tax           = float(rng.choice([0, 0, 1, 3], p=[.45, .3, .15, .1]))
                pair_age_hours     = float(rng.uniform(0.3, 12.0))    # pair giovane
                top_10_holder_pct  = float(rng.uniform(15, 55))       # non troppo concentrato

            elif regime == "post_pump":
                # Già pompato: change_1h alto ma volume inizia a calare,
                # sellers > buyers, holder smettono di crescere
                liquidity_usd      = float(rng.lognormal(9.0, 1.0))
                volume_24h_usd     = float(rng.lognormal(10.0, 0.8))
                volume_1h_usd      = volume_24h_usd / 24 * rng.uniform(0.6, 1.4)
                volume_5m_usd      = volume_1h_usd / 12 * rng.uniform(0.5, 1.0)  # in calo
                change_5m_pct      = float(rng.uniform(-8.0, 5.0))
                change_15m_pct     = float(rng.uniform(-5.0, 15.0))
                change_1h_pct      = float(rng.uniform(20.0, 80.0))   # già salito molto
                change_24h_pct     = float(rng.uniform(30.0, 200.0))
                buys_1h            = int(rng.integers(10, 150))
                sells_1h           = int(rng.integers(buys_1h, buys_1h * 3 + 1))
                holders_change_1h  = int(rng.integers(-30, 10))
                buy_tax            = float(rng.choice([0, 1, 3, 5], p=[.3, .3, .25, .15]))
                sell_tax           = float(rng.choice([0, 1, 3, 5], p=[.25, .3, .25, .2]))
                pair_age_hours     = float(rng.uniform(6.0, 72.0))

            elif regime == "rug":
                # Token a rischio rug: liquidità bassa, tasse alte, mintable,
                # concentration alta, volume gonfiato artificialmente
                liquidity_usd      = float(rng.lognormal(7.5, 0.8))   # ~1.8k median
                volume_24h_usd     = float(rng.lognormal(8.5, 1.2))
                volume_1h_usd      = volume_24h_usd / 24 * rng.uniform(0.5, 4.0)
                volume_5m_usd      = volume_1h_usd / 12 * rng.uniform(0.5, 2.0)
                change_5m_pct      = float(rng.uniform(-5.0, 15.0))
                change_15m_pct     = float(rng.uniform(-5.0, 20.0))
                change_1h_pct      = float(rng.uniform(-10.0, 30.0))
                change_24h_pct     = float(rng.normal(0.0, 40.0))
                buys_1h            = int(rng.integers(5, 80))
                sells_1h           = int(rng.integers(5, 80))
                holders_change_1h  = int(rng.integers(-10, 20))
                buy_tax            = float(rng.choice([0, 3, 5, 10], p=[.2, .3, .3, .2]))
                sell_tax           = float(rng.choice([0, 5, 10, 15], p=[.15, .25, .35, .25]))
                is_mintable        = 1
                top_10_holder_pct  = float(rng.uniform(55, 92))
                lp_locked          = int(rng.random() > 0.75)  # spesso non locked

            else:  # flat
                # Token normali: nessun segnale particolare
                liquidity_usd      = float(rng.lognormal(10.0, 1.2))
                volume_24h_usd     = float(rng.lognormal(9.5, 1.3))
                volume_1h_usd      = volume_24h_usd / 24 * rng.uniform(0.5, 1.5)
                volume_5m_usd      = volume_1h_usd / 12 * rng.uniform(0.7, 1.3)
                change_5m_pct      = float(rng.normal(0.0, 3.0))
                change_15m_pct     = float(rng.normal(0.0, 5.0))
                change_1h_pct      = float(rng.normal(0.0, 8.0))
                change_24h_pct     = float(rng.normal(0.0, 15.0))
                buys_1h            = int(rng.integers(5, 200))
                sells_1h           = int(rng.integers(5, 200))
                holders_change_1h  = int(rng.integers(-10, 20))
                buy_tax            = float(rng.choice([0, 0, 1, 3], p=[.55, .25, .12, .08]))
                sell_tax           = float(rng.choice([0, 0, 1, 3], p=[.5,  .25, .15, .1]))

            transfers_1h = int(rng.integers(0, buys_1h + sells_1h + 10))

            riga = {
                "snapshot_ts":      t0.isoformat(),
                "token_address":    token_address,
                "token_symbol":     token_symbol,
                "chain":            chain,
                "regime":           regime,           # solo per debug/analisi, rimosso prima del training
                "price_usd":        float(rng.lognormal(-4.0, 2.5)),
                "fdv":              float(rng.lognormal(-4.0, 2.5)) * float(rng.lognormal(18, 2)),
                "change_5m_pct":    round(float(np.clip(change_5m_pct,  -50,  50)), 4),
                "change_15m_pct":   round(float(np.clip(change_15m_pct, -70,  70)), 4),
                "change_1h_pct":    round(float(np.clip(change_1h_pct,  -90, 200)), 4),
                "change_24h_pct":   round(float(np.clip(change_24h_pct, -95, 500)), 4),
                "volume_5m_usd":    round(max(0.0, volume_5m_usd), 2),
                "volume_1h_usd":    round(max(0.0, volume_1h_usd), 2),
                "volume_24h_usd":   round(max(0.0, volume_24h_usd), 2),
                "liquidity_usd":    round(max(100.0, liquidity_usd), 2),
                "buys_1h":          max(0, buys_1h),
                "sells_1h":         max(0, sells_1h),
                "pair_age_hours":   round(max(0.1, pair_age_hours), 2),
                "holders_total":    max(1, holders_total),
                "top_10_holder_pct": round(float(np.clip(top_10_holder_pct, 5, 99)), 2),
                "transfers_1h":     max(0, transfers_1h),
                "holders_change_1h": int(holders_change_1h),
                "is_honeypot":      0,
                "buy_tax":          round(float(np.clip(buy_tax,  0, 25)), 2),
                "sell_tax":         round(float(np.clip(sell_tax, 0, 25)), 2),
                "lp_locked":        int(lp_locked),
                "is_open_source":   int(is_open_source),
                "is_mintable":      int(is_mintable),
            }
            df_list.append(riga)

    df = pd.DataFrame(df_list)

    # ── Target forward-looking (causalmente corretto) ──────────────────────
    df = _simulate_target_forward(df, rng)

    # Rimuovi colonna regime (era solo per diagnostica interna)
    df = df.drop(columns=["regime"], errors="ignore")

    log.info(f"[build_dataset] Dataset finale: {df.shape}. "
             f"Pump rate: {df['target'].mean():.1%}")
    return df


def _simulate_target_forward(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """
    Simula il target binario (pump o no) in modo CAUSALMENTE CORRETTO.

    PROBLEMA DEL VECCHIO APPROCCIO — target circolare:
    ───────────────────────────────────────────────────
    La versione precedente calcolava la probabilità di pump usando le stesse
    variabili (vol_accel, bsr, pair_age_hours, change_5m, ecc.) che diventano
    feature dopo engineer_features(). Il modello risolve banalmente la formula
    usata per generare i label: ROC-AUC 0.95 non misura nulla di reale.

    SOLUZIONE — separazione causale netta:
    ──────────────────────────────────────
    • La base probability dipende SOLO dal regime (variabile latente, non osservata
      direttamente dal modello in produzione).
    • Le correzioni usano SOLO variabili strutturali che NON sono feature dirette:
        - lp_locked, is_mintable, is_open_source  → flag binari grezzi
        - top_10_holder_pct, buy_tax/sell_tax     → strutturali, non computati
    • Volume, price changes, BSR — NON vengono usati qui. Sono feature del modello.
      Se li usassimo, il modello troverebbe la formula e non imparebbe niente.
    • Rumore strutturale realistico: σ aggiustato per regime. Il test set avrà
      inevitabilmente performance peggiori del train → segnale di generalizzazione reale.

    In produzione: questo blocco va sostituito con label storici reali
    (TimescaleDB/Parquet con prezzi a t₀ + LOOKAHEAD_MINUTES).
    """
    n = len(df)
    THRESHOLD = CONFIG["PUMP_THRESHOLD_PCT"]

    # ── Probabilità base per regime (variabile latente) ──────────────────────
    # Nota: queste probabilità riflettono la frequenza attesa DI MERCATO, non
    # la "decodificabilità" del segnale. pre_pump ha p alta perché per definizione
    # stiamo campionando token che avranno un pump — ma il modello deve SCOPRIRLO
    # dalle feature, non dalla label diretta.
    regime_base_prob = {
        "pre_pump":  0.48,   # alto ma non triviale: non tutti i pre-pump pompano
        "post_pump": 0.05,   # raro: già esploso, il pump futuro è un secondo leg
        "rug":       0.02,   # quasi mai: rug = perdita, non pump
        "flat":      0.06,   # raro: movimento casuale
    }
    prob = df["regime"].map(regime_base_prob).fillna(0.06).values.astype(float)

    # ── Correzioni da variabili STRUTTURALI (NON feature engineered) ──────────
    # Usiamo solo variabili che esistono prima di engineer_features() e che
    # NON compaiono direttamente in FEATURE_COLUMNS in forma calcolata.

    # Sicurezza contratto: LP non locked e mintable riducono fortemente le chances
    prob -= (1 - df["lp_locked"].fillna(0))  * 0.08   # LP non locked → rischio rug
    prob -= df["is_mintable"].fillna(0)       * 0.12   # mintable → inflate & dump
    # Open source: contratto verificato aumenta leggermente la credibilità
    prob += df["is_open_source"].fillna(0)    * 0.03

    # Concentrazione holder: top10 > 70% → whale può dumpare in qualsiasi momento
    top10 = df["top_10_holder_pct"].fillna(50.0)
    prob -= np.clip((top10 - 70.0) / 20.0, 0.0, 0.12)   # penalità crescente sopra 70%

    # Tasse: ogni punto % di tassa round-trip riduce il profitto netto reale
    # del trader → meno incentivo al pump, meno buyer marginali
    tax_rt = (df["buy_tax"].fillna(0) + df["sell_tax"].fillna(0))
    prob -= np.clip(tax_rt / 50.0, 0.0, 0.10)

    # ── Rumore stocastico per regime (realismo) ───────────────────────────────
    # Il rumore NON è uniforme: pre_pump ha sigma minore (segnali più coerenti),
    # flat e rug hanno sigma maggiore (comportamento imprevedibile).
    sigma_by_regime = df["regime"].map({
        "pre_pump":  0.10,
        "post_pump": 0.14,
        "rug":       0.16,
        "flat":      0.13,
    }).fillna(0.13).values
    noise = rng.normal(0.0, sigma_by_regime)
    prob += noise

    # ── Clamp finale ─────────────────────────────────────────────────────────
    # Max = 0.65: nessun token simulato ha certezza di pompare.
    # Min = 0.01: nessun token è impossibile da pompare (coda lunga crypto).
    prob = np.clip(prob, 0.01, 0.65)

    df["target"] = (rng.random(n) < prob).astype(int)

    log.info(
        f"[build_dataset] Pump rate simulato: {df['target'].mean():.1%} "
        f"(threshold: +{THRESHOLD}% in {CONFIG['LOOKAHEAD_MINUTES']}min) "
        f"| pre_pump nel dataset: {(df['regime']=='pre_pump').mean():.0%} "
        f"| pump in pre_pump: {df.loc[df['regime']=='pre_pump', 'target'].mean():.0%}"
    )
    return df

# ==============================================================================
# SEZIONE 5 – FEATURE ENGINEERING (Pre-Pump Hunter v2)
# ==============================================================================
#
# LOGICA DI SELEZIONE FEATURE — correlazione empirica col pre-pump
# ───────────────────────────────────────────────────────────────────
# Le feature sono organizzate in 8 gruppi tematici.
# Ogni gruppo cattura un segnale distinto che precede statisticamente un pump:
#
#  1. MOMENTUM PREZZO       → accelerazione su finestre brevi (5m/15m) mentre 1h è ancora basso
#  2. VOLUME EXPLOSION      → volume 5m che esplode vs media 1h = accumulo silenzioso che inizia
#  3. PRESSIONE ACQUISTI    → BSR > 1 = più buyer che seller = domanda in eccesso
#  4. MICRO-TRANSAZIONI     → tanti piccoli acquisti vs pochi grandi = accumulo organico (non whale dump)
#  5. LIQUIDITÀ & SALUTE    → liq/vol stabile, liq non troppo bassa (rug) né troppo alta (maturo)
#  6. ON-CHAIN HOLDER       → holder in crescita = nuovi ingressi, distribuzione non concentrata
#  7. STRUTTURA TOKEN       → pair giovane (sweet spot 30min-12h), tax basse, LP locked, open source
#  8. SEGNALI COMPOSITI     → combinazioni non lineari ad alta correlazione empirica

FEATURE_COLUMNS = [
    # ── 1. Momentum prezzo ───────────────────────────────────────────────────
    "change_5m_pct",            # % cambio prezzo ultimi 5 min
    "change_15m_pct",           # % cambio prezzo ultimi 15 min
    "change_1h_pct",            # % cambio prezzo ultima ora (NON deve essere già alto)
    "change_24h_pct",           # % cambio prezzo ultime 24h
    "price_momentum_accel",     # (change_1h - change_15m): accelerazione tra finestre
    "price_momentum_early",     # change_5m / (|change_1h| + 0.1): segnale precoce vs totale
    "momentum_consistency",     # tutti e 3 i timeframe (5m/15m/1h) concordi e positivi
    # ── 2. Volume explosion ──────────────────────────────────────────────────
    "volume_1h_usd",            # volume $USD ultima ora
    "volume_24h_usd",           # volume $USD ultime 24h
    "volume_ratio_1h_24h",      # vol_1h / (vol_24h/24): spike vs media oraria
    "vol_accel_5m_vs_1h",       # vol_5m / (vol_1h/12): accelerazione intra-ora (SEGNALE FORTE)
    "volume_acceleration",      # prima derivata volume (cambio relativo)
    "vol_per_trade",            # vol_1h / total_trades: dimensione media trade
    # ── 3. Pressione acquisti ────────────────────────────────────────────────
    "buy_sell_ratio_1h",        # buys / sells ultima ora (> 1.5 = pressione rialzista)
    "total_trades_1h",          # numero totale transazioni ultima ora
    "trade_intensity",          # trades / (liquidità/1000): attività normalizzata
    "buy_pressure_score",       # BSR ponderato per vol_accel (amplifica quando entrambi alti)
    # ── 4. Micro-transazioni (accumulo organico) ─────────────────────────────
    "avg_trade_size_usd",       # vol_1h / trades: piccolo = accumulo retail, non whale
    "micro_buy_dominance",      # proxy: buys_1h / total_trades quando vol_per_trade < mediana
    # ── 5. Liquidità e salute mercato ────────────────────────────────────────
    "liquidity_usd",            # liquidità totale $USD
    "liq_to_vol_ratio",         # liq / vol_1h: >1 = liquidità sana vs attività
    "liq_depth_score",          # funzione a campana: penalizza liq troppo bassa O troppo alta
    "fdv_to_liq_ratio",         # FDV / liq: alto = token gonfiato = rug risk
    # ── 6. On-chain holder ───────────────────────────────────────────────────
    "holders_total",            # numero holder totali
    "top_10_holder_pct",        # % supply in top 10 wallet (alta concentrazione = rischio)
    "transfers_1h",             # trasferimenti on-chain ultima ora
    "holders_change_1h",        # variazione holder assoluta ultima ora
    "holder_growth_rate",       # holders_change_1h / holders_total * 100
    "holder_quality_score",     # combinazione: crescita holder + bassa concentrazione
    # ── 7. Struttura token ───────────────────────────────────────────────────
    "buy_tax",                  # tassa acquisto (%)
    "sell_tax",                 # tassa vendita (%)
    "total_tax_cost",           # buy_tax + sell_tax: costo round-trip
    "lp_locked",                # 1 = LP locked (meno rug risk)
    "is_open_source",           # 1 = contratto verificato
    "is_mintable",              # 1 = supply inflazionabile (rischio)
    "pair_age_hours",           # età pair in ore
    "pair_age_score",           # curva a campana: sweet spot 0.5-12h
    # ── 8. Segnali compositi (alta correlazione empirica) ────────────────────
    "rsi_proxy",                # proxy RSI su snapshot (50 + clip(change_1h, -50,50))
    "volatility_proxy",         # |change_1h| / (|change_24h| + 1)
    "price_vol_divergence",     # |change_5m| basso MA vol_accel alto = accumulo silenzioso
    "prepump_composite_score",  # score aggregato pesato di tutti i segnali pre-pump
    # ── 9. Anti-dump features ─────────────────────────────────────────────────
    "dump_risk_score",           # alto = distribuzione in corso (pattern opposto al pre-pump)
    "sell_pressure_momentum",    # BSR invertito × momentum negativo
    # ── 10. Bollinger Bands & volatility compression ──────────────────────────
    "bb_width_proxy",            # range prezzi 5m/15m/1h/24h / media: basso = squeeze
    "bb_squeeze_score",          # 1 / bb_width: alto = prezzo compresso = energia accumulata
    "squeeze_momentum",          # bb_squeeze × vol_accel: IL segnale pre-breakout
]


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Costruisce le feature engineered dal dataset grezzo.
    Pipeline completa: momentum → volume → pressione → liquidità → on-chain → compositi.
    Compatibile con dati live (DexScreener), mock e CSV storico.
    """
    df = df.copy()

    def _norm01(s: pd.Series, cap: float = None) -> pd.Series:
        """Normalizza una serie in [0, 1] con clip opzionale."""
        if cap is not None:
            s = s.clip(0, cap)
        vmax = s.max()
        vmin = s.min()
        if vmax == vmin:
            return pd.Series(0.5, index=s.index)
        return (s - vmin) / (vmax - vmin + 1e-9)

    # ═══════════════════════════════════════════════════════════════
    # GRUPPO 1 — MOMENTUM PREZZO
    # ═══════════════════════════════════════════════════════════════

    ch5m  = df["change_5m_pct"].fillna(0)
    ch15m = df["change_15m_pct"].fillna(0)
    ch1h  = df["change_1h_pct"].fillna(0)
    ch24h = df["change_24h_pct"].fillna(0)

    # Accelerazione: 1h cresce più veloce di 15m = trend che si amplifica
    df["price_momentum_accel"] = ch1h - ch15m

    # Segnale precoce: quanto del movimento 1h è concentrato negli ultimi 5m
    # Alto = il movimento è appena partito (pre-pump) vs già distribuito nel tempo
    df["price_momentum_early"] = ch5m / (ch1h.abs() + 0.1)

    # Consistenza momentum: tutti i timeframe positivi e concordi
    # 1 = tutti e 3 positivi (trend solido), 0 = segnali contrastanti
    df["momentum_consistency"] = (
        (ch5m > 0).astype(float) *
        (ch15m > 0).astype(float) *
        (ch1h > 0).astype(float)
    )

    # ═══════════════════════════════════════════════════════════════
    # GRUPPO 2 — VOLUME EXPLOSION
    # ═══════════════════════════════════════════════════════════════

    vol1h  = df["volume_1h_usd"].fillna(0)
    vol24h = df["volume_24h_usd"].fillna(0)

    # Volume_5m: usa colonna reale se disponibile, altrimenti stima 1/12 del 1h.
    # FIX: il vecchio fallback scattava solo se la colonna mancava del tutto.
    # DexScreener spesso restituisce volume.m5 = 0 (o null) per pair illiquidi/nuovi
    # → in quel caso vol5m era 0, vol_accel era 0, e la condizione >= 1.3 non passava mai.
    # Ora: se la colonna esiste ma è tutta a zero, si usa comunque la stima 1/12 del 1h.
    if "volume_5m_usd" in df.columns:
        vol5m = df["volume_5m_usd"].fillna(0)
        # Fallback per righe con vol5m zero (API non ha dati granulari 5m)
        vol5m = vol5m.where(vol5m > 0, vol1h / 12.0)
    else:
        vol5m = vol1h / 12.0

    # Volume ratio 1h vs media oraria del 24h (>1.5 = spike anomalo)
    media_oraria_24h = vol24h / 24.0
    df["volume_ratio_1h_24h"] = vol1h / (media_oraria_24h + 1.0)

    # SEGNALE CHIAVE: accelerazione intra-ora
    # vol_5m vs media attesa di un segmento 5m dentro l'ora
    # >2.0 = il volume degli ultimi 5 minuti è 2x la media → accumulo che parte ORA
    avg_vol_5m_atteso = vol1h / 12.0
    df["vol_accel_5m_vs_1h"] = vol5m / (avg_vol_5m_atteso + 1.0)

    # Prima derivata volume (cambio relativo vs stima precedente)
    df["volume_acceleration"] = (vol5m / (avg_vol_5m_atteso + 1.0)) - 1.0

    # Volume per trade (trade size media)
    if "buys_1h" in df.columns and "sells_1h" in df.columns:
        _buys  = df["buys_1h"].fillna(0)
        _sells = df["sells_1h"].fillna(0)
        _total_trades = _buys + _sells
        df["buy_sell_ratio_1h"] = _buys / (_sells + 1.0)
        df["total_trades_1h"]   = _total_trades
    else:
        if "buy_sell_ratio_1h" not in df.columns:
            df["buy_sell_ratio_1h"] = 1.0
        if "total_trades_1h" not in df.columns:
            df["total_trades_1h"] = 0.0
        _buys         = df["buy_sell_ratio_1h"] * 50  # stima
        _sells        = pd.Series(50.0, index=df.index)
        _total_trades = df["total_trades_1h"]

    df["vol_per_trade"] = vol1h / (_total_trades + 1.0)

    # BSR SHIFT ISTANTANEO — confronta il BSR delle ultime 5 minuti con quello dell'ultima ora.
    # Disponibile SUBITO nello stesso snapshot (txns.m5 vs txns.h1 di Dexscreener/GeckoTerminal),
    # niente da raccogliere nel tempo: cattura un cambio di pressione compratori/venditori
    # iniziato negli ultimi minuti, prima che si rifletta nella media cumulata oraria.
    if "buys_5m" in df.columns and "sells_5m" in df.columns:
        _b5m = df["buys_5m"].fillna(0)
        _s5m = df["sells_5m"].fillna(0)
        df["bsr_5m"] = _b5m / (_s5m + 1.0)
    else:
        df["bsr_5m"] = df["buy_sell_ratio_1h"]
    df["bsr_recent_shift"] = df["bsr_5m"] - df["buy_sell_ratio_1h"]

    # BSR TREND — variazione del buy/sell ratio nei cicli precedenti (feature "leading",
    # cattura un BSR in calo anche mentre è ancora >= soglia d'ingresso)
    if "pair_address" in df.columns:
        _trend = df["pair_address"].apply(_bsr_trend)
        df["bsr_trend_per_min"] = _trend.apply(lambda t: t[0])
        df["bsr_trend_samples"] = _trend.apply(lambda t: t[1])
    else:
        df["bsr_trend_per_min"] = 0.0
        df["bsr_trend_samples"] = 0

    # ═══════════════════════════════════════════════════════════════
    # GRUPPO 3 — PRESSIONE ACQUISTI
    # ═══════════════════════════════════════════════════════════════

    bsr = df["buy_sell_ratio_1h"]

    # Intensità trading normalizzata per liquidità
    liq = df["liquidity_usd"].fillna(0)
    df["trade_intensity"] = _total_trades / (liq / 1000.0 + 1.0)

    # Buy pressure score: amplifica quando BSR alto E volume accelera simultaneamente
    # Questa combinazione è il segnale più forte di accumulo pre-pump
    df["buy_pressure_score"] = (
        bsr.clip(0, 5) *                        # BSR (cappato a 5 per non dominare)
        df["vol_accel_5m_vs_1h"].clip(0, 10)    # accel volume (cappato a 10)
    )

    # ═══════════════════════════════════════════════════════════════
    # GRUPPO 4 — MICRO-TRANSAZIONI (accumulo organico)
    # ═══════════════════════════════════════════════════════════════

    # Dimensione media trade in USD
    df["avg_trade_size_usd"] = vol1h / (_total_trades + 1.0)

    # Dominanza micro-acquisti:
    # Molti trades piccoli = retail che accumula organicamente (NON whale manipulation)
    # Proxy: alto numero buys + bassa size media = pattern da accumulo pre-pump genuino
    median_trade_size = df["avg_trade_size_usd"].median() if len(df) > 1 else 500.0
    df["micro_buy_dominance"] = (
        (_buys / (_total_trades + 1.0)) *                           # % buys sul totale
        (1.0 / (df["avg_trade_size_usd"] / (median_trade_size + 1.0) + 1.0))  # inv. size
    )

    # ═══════════════════════════════════════════════════════════════
    # GRUPPO 5 — LIQUIDITÀ E SALUTE MERCATO
    # ═══════════════════════════════════════════════════════════════

    # Rapporto liquidità / volume: >1 = mercato sano, non manipolato
    df["liq_to_vol_ratio"] = liq / (vol1h + 1.0)

    # Liquidity depth score: funzione a campana
    # Penalizza liquidità <5k (rug risk) e >500k (token già maturo, meno upside)
    # Sweet spot: 10k-100k USD di liquidità
    liq_norm = liq / 20_000.0  # normalizza su 20k come punto ottimale
    df["liq_depth_score"] = np.exp(-0.5 * (np.log1p(liq_norm) - np.log1p(1.0)) ** 2)

    # FDV / Liquidità: alto = supply gonfiata vs liquidità reale → segnale rug/dump
    # Usa fdv se disponibile, altrimenti stima da price * supply approssimata
    if "fdv" in df.columns:
        fdv = df["fdv"].fillna(0)
    else:
        # Stima conservativa: price * 1B token (tipico per meme/defi token)
        fdv = df.get("price_usd", pd.Series(0.0, index=df.index)).fillna(0) * 1e9
    df["fdv_to_liq_ratio"] = fdv / (liq + 1.0)

    # ═══════════════════════════════════════════════════════════════
    # GRUPPO 6 — ON-CHAIN HOLDER
    # ═══════════════════════════════════════════════════════════════

    holders_total    = df["holders_total"].fillna(0)
    holders_change   = df["holders_change_1h"].fillna(0)
    top10_pct        = df["top_10_holder_pct"].fillna(50)

    # Tasso di crescita holder (% su base esistente)
    df["holder_growth_rate"] = holders_change / (holders_total + 1.0) * 100.0

    # Holder quality score: cresce con holder in aumento E bassa concentrazione
    # Alta concentrazione (top10 > 60%) = whale può scaricare = penalità
    concentration_penalty = np.clip((top10_pct - 40.0) / 40.0, 0.0, 1.0)
    growth_bonus          = np.clip(df["holder_growth_rate"] / 5.0, 0.0, 1.0)
    df["holder_quality_score"] = growth_bonus * (1.0 - concentration_penalty)

    # ═══════════════════════════════════════════════════════════════
    # GRUPPO 7 — STRUTTURA TOKEN
    # ═══════════════════════════════════════════════════════════════

    # Costo totale round-trip (buy_tax + sell_tax)
    buy_tax  = df["buy_tax"].fillna(0)
    sell_tax = df["sell_tax"].fillna(0)
    df["total_tax_cost"] = buy_tax + sell_tax

    # Pair age score: curva a campana centrata su 2h
    # < 15min = troppo nuovo (liquidità non stabilizzata)
    # 15min-12h = sweet spot pre-pump (nuovo abbastanza da avere upside)
    # > 48h = opportunità già metabolizzata dal mercato
    age = df["pair_age_hours"].fillna(24.0)
    df["pair_age_score"] = np.where(
        age < 0.25,  0.0,                                  # troppo nuovo
        np.where(age <= 2.0,  1.0,                         # sweet spot peak
        np.where(age <= 12.0, 1.0 - (age - 2.0) / 20.0,  # decadimento lento
        np.where(age <= 48.0, 0.5 - (age - 12.0) / 72.0, # maturazione
                 0.1)))                                     # già vecchio
    )

    # ═══════════════════════════════════════════════════════════════
    # GRUPPO 8 — SEGNALI COMPOSITI
    # ═══════════════════════════════════════════════════════════════

    # Proxy RSI: mappa change_1h in [0, 100]
    # <30 = oversold (possibile rimbalzo), 50-70 = momentum sano, >80 = overbought
    df["rsi_proxy"] = 50.0 + ch1h.clip(-50, 50)

    # Proxy volatilità: quanto del movimento 24h è concentrato nell'ultima ora
    df["volatility_proxy"] = ch1h.abs() / (ch24h.abs() + 1.0)

    # PRICE-VOLUME DIVERGENCE (segnale di accumulo silenzioso):
    # Prezzo che non si muove ancora MA volume che esplode = qualcuno sta comprando
    # senza far salire il prezzo → classico segnale pre-pump da accumulo istituzionale
    # Formula: alto quando vol_accel è alto E change_5m è basso (non ancora scoppiato)
    price_stillness = 1.0 / (ch5m.abs() + 1.0)  # inversamente prop. al movimento di prezzo
    df["price_vol_divergence"] = (
        df["vol_accel_5m_vs_1h"].clip(0, 20) *
        price_stillness.clip(0, 2)
    )

    # ═══════════════════════════════════════════════════════════════
    # GRUPPO 10 — BOLLINGER BANDS & VOLATILITY COMPRESSION
    # ═══════════════════════════════════════════════════════════════
    # Ricostruisce prezzi relativi dai 4 timeframe di change disponibili.
    # BB width basso = prezzo compresso = energia accumulata → breakout imminente.

    _p_now  = pd.Series(1.0, index=df.index)
    _p_5m   = (1.0 / (1.0 + ch5m  / 100.0)).clip(0.01, 100.0)
    _p_15m  = (1.0 / (1.0 + ch15m / 100.0)).clip(0.01, 100.0)
    _p_1h   = (1.0 / (1.0 + ch1h  / 100.0)).clip(0.01, 100.0)
    _p_24h  = (1.0 / (1.0 + ch24h / 100.0)).clip(0.01, 100.0)

    _prices = pd.DataFrame({
        "p_now": _p_now, "p_5m": _p_5m, "p_15m": _p_15m,
        "p_1h":  _p_1h,  "p_24h": _p_24h,
    })
    _p_high = _prices.max(axis=1)
    _p_low  = _prices.min(axis=1)
    _p_mean = _prices.mean(axis=1)

    df["bb_width_proxy"]  = (_p_high - _p_low) / (_p_mean + 1e-9)
    df["bb_squeeze_score"] = (1.0 / (df["bb_width_proxy"] + 0.02)).clip(0, 50)

    # Segnale chiave: squeeze × vol_accel → prezzo fermo + volume esplode
    df["squeeze_momentum"] = (
        _norm01(df["bb_squeeze_score"],   cap=30.0) *
        _norm01(df["vol_accel_5m_vs_1h"], cap=10.0)
    )

    # PREPUMP COMPOSITE SCORE — score aggregato pesato
    # Pesi calibrati empiricamente sulla correlazione con il target pre-pump:
    #   vol_accel_5m_vs_1h    → peso 0.25 (segnale più forte)
    #   buy_pressure_score    → peso 0.20
    #   momentum_consistency  → peso 0.15
    #   holder_quality_score  → peso 0.15
    #   pair_age_score        → peso 0.10
    #   liq_depth_score       → peso 0.10
    #   price_vol_divergence  → peso 0.05 (utile ma secondario)
    # Ogni componente è normalizzata in [0, 1] prima della somma pesata (vedi _norm01 in cima)
    df["prepump_composite_score"] = (
        _norm01(df["vol_accel_5m_vs_1h"],   cap=15.0) * 0.20 +  # -0.05 (ceduto a squeeze)
        _norm01(df["buy_pressure_score"],    cap=25.0) * 0.20 +
        df["momentum_consistency"]                     * 0.15 +
        _norm01(df["holder_quality_score"],  cap=1.0)  * 0.10 +  # -0.05 (ceduto a squeeze)
        _norm01(df["pair_age_score"],        cap=1.0)  * 0.10 +
        _norm01(df["liq_depth_score"],       cap=1.0)  * 0.10 +
        _norm01(df["price_vol_divergence"],  cap=5.0)  * 0.05 +
        _norm01(df["squeeze_momentum"],      cap=1.0)  * 0.10   # NUOVO: BB squeeze × vol_accel
    )

    # ═══════════════════════════════════════════════════════════════
    # GRUPPO 9 — ANTI-DUMP FEATURES (NUOVO)
    # ═══════════════════════════════════════════════════════════════

    # DUMP RISK SCORE — cattura il pattern pre-dump (opposto al pre-pump)
    # Alto quando: prezzi negativi su più TF + volume in crescita = distribuzione
    # Il modello ML impara: dump_risk_score alto → target=0
    neg_momentum_5m = (-ch5m).clip(0, 20)
    neg_momentum_1h = (-ch1h).clip(0, 50)
    vol_accel_for_dump = df["vol_accel_5m_vs_1h"].clip(0, 10)
    df["dump_risk_score"] = (
        (neg_momentum_5m / 20.0) * 0.40 +
        (neg_momentum_1h / 50.0) * 0.40 +
        (vol_accel_for_dump / 10.0) * 0.20
    ).clip(0, 1)

    # SELL PRESSURE MOMENTUM — BSR invertito pesato per momentum negativo
    # Alto quando c'è più sell pressure E il prezzo già scende
    sell_dom = (1.0 / (bsr.clip(0.1, 10))).clip(0, 5)
    neg_5m_norm = neg_momentum_5m / (20.0 + 1e-9)
    df["sell_pressure_momentum"] = (sell_dom * neg_5m_norm).clip(0, 5)

    # ── Pulizia finale ──────────────────────────────────────────────────────
    df = df.replace([np.inf, -np.inf], np.nan)
    # Assicura che tutte le FEATURE_COLUMNS siano presenti (fallback a 0)
    for col in FEATURE_COLUMNS:
        if col not in df.columns:
            df[col] = 0.0
    df[FEATURE_COLUMNS] = df[FEATURE_COLUMNS].fillna(0)

    return df

# ==============================================================================
# SEZIONE 6 – ANALISI CORRELAZIONI E PATTERN
# ==============================================================================

def analyze_correlations_and_patterns(df: pd.DataFrame) -> None:
    """
    Analizza le correlazioni tra feature e target, e raggruppa i token
    in cluster comportamentali (regime di mercato).
    Stampa i risultati nel log.
    """
    if "target" not in df.columns:
        log.warning("[analisi] Colonna 'target' mancante. Salto l'analisi.")
        return

    df_feat = engineer_features(df)

    # ── 1. Correlazione di Pearson tra feature e target ──
    corr = df_feat[FEATURE_COLUMNS + ["target"]].corr()["target"] \
               .drop("target").sort_values(ascending=False)

    log.info("\n── Correlazioni feature → target (pump) ──")
    log.info(corr.to_string())

    # ── 2. Statistiche medie per classe (pump vs non-pump) ──
    log.info("\n── Medie feature: pump (1) vs non-pump (0) ──")
    stat = df_feat.groupby("target")[FEATURE_COLUMNS].mean()
    log.info(stat.T.to_string())

    # ── 3. Clustering comportamentale K-Means (3 cluster) ──
    try:
        from sklearn.cluster import KMeans
        from sklearn.preprocessing import StandardScaler

        # Usa un sottoinsieme di feature interpretabili per il clustering
        cluster_features = [
            "volume_ratio_1h_24h", "buy_sell_ratio_1h",
            "liq_to_vol_ratio", "holder_growth_rate",
            "change_1h_pct", "rsi_proxy",
        ]
        X_cluster = df_feat[cluster_features].fillna(0)
        scaler_c  = StandardScaler()
        X_scaled  = scaler_c.fit_transform(X_cluster)

        kmeans = KMeans(n_clusters=3, random_state=CONFIG["RANDOM_STATE"], n_init=10)
        df_feat["cluster"] = kmeans.fit_predict(X_scaled)

        log.info("\n── Cluster comportamentali (K-Means, k=3) ──")
        descrizioni = {
            # I nomi vengono assegnati in base alle medie del cluster
        }
        centri = pd.DataFrame(
            scaler_c.inverse_transform(kmeans.cluster_centers_),
            columns=cluster_features
        )
        for c in range(3):
            centro = centri.iloc[c]
            mask_c = df_feat["cluster"] == c
            count_c = mask_c.sum()
            pump_rate = df_feat.loc[mask_c, "target"].mean() if count_c > 0 else float("nan")
            pump_str  = f"{pump_rate:.1%}" if count_c > 0 else "n/a"

            # Assegna un nome descrittivo basato sulle caratteristiche del centro
            if centro["volume_ratio_1h_24h"] > 1.5 and centro["buy_sell_ratio_1h"] > 1.2:
                nome = "Accumulo con volume crescente"
            elif centro["liq_to_vol_ratio"] < 0.5 and centro["volume_ratio_1h_24h"] > 2:
                nome = "Spike illiquido improvviso"
            else:
                nome = "Trend stabile con liquidità sana"

            log.info(
                f"  Cluster {c} — '{nome}': "
                f"{count_c} token, "
                f"pump rate={pump_str}, "
                f"vol_ratio={centro['volume_ratio_1h_24h']:.2f}, "
                f"bsr={centro['buy_sell_ratio_1h']:.2f}"
            )
    except Exception as e:
        log.warning(f"[analisi] Clustering fallito: {e}")

# ==============================================================================
# SEZIONE 7 – TRAINING DEL MODELLO ML
# ==============================================================================

def train_model(df: pd.DataFrame) -> tuple:
    """
    Addestra un modello di classificazione binaria per predire pump.
    Gestisce lo sbilanciamento di classe con class_weight='balanced'.
    Esegue cross-validation e stampa le metriche principali.

    Ritorna: (modello addestrato, scaler RobustScaler)
    """
    log.info("[training] Avvio feature engineering sul dataset...")
    df_feat = engineer_features(df)

    # Rimuovi righe con target mancante
    df_feat = df_feat.dropna(subset=["target"])
    if len(df_feat) < 50:
        raise ValueError("Dataset troppo piccolo per il training (< 50 righe).")

    X = df_feat[FEATURE_COLUMNS].values
    y = df_feat["target"].values.astype(int)

    log.info(f"[training] Dataset: {X.shape[0]} campioni, "
             f"{X.shape[1]} feature. Pump rate: {y.mean():.1%}")

    # ── Ordina per timestamp se disponibile (evita data leakage) ──
    ts_col = next((c for c in ("snapshot_ts","timestamp") if c in df_feat.columns), None)
    if ts_col:
        sort_idx = pd.to_datetime(df_feat[ts_col], errors="coerce").argsort()
        X = X[sort_idx]; y = y[sort_idx]
        log.info(f"[training] Ordinato per {ts_col} — no data leakage temporale.")

    # ── Split temporale PRIMA dello scaling (evita leakage test→scaler) ──
    # Il test set non deve MAI contribuire al fit dello scaler.
    split_idx = int(len(X) * (1 - CONFIG["TEST_SIZE"]))
    X_train_raw, X_test_raw = X[:split_idx], X[split_idx:]
    y_train,     y_test     = y[:split_idx], y[split_idx:]

    # ── Normalizzazione: fit SOLO su X_train_raw ──
    scaler  = RobustScaler()
    X_train = scaler.fit_transform(X_train_raw)   # fit+transform solo su train
    X_test  = scaler.transform(X_test_raw)         # solo transform su test (no fit)

    # Alias per la CV (dati raw, lo scaler verrà rincorporato nella Pipeline per fold)
    X_raw_all = X

    # ── Pesi di classe per gestire lo sbilanciamento ──
    classi = np.unique(y_train)
    pesi   = compute_class_weight("balanced", classes=classi, y=y_train)
    pesi_dict = dict(zip(classi, pesi))
    log.info(f"[training] Class weights: {pesi_dict}")

    # ── Selezione modello ──
    if XGBOOST_AVAILABLE:
        log.info("[training] Uso XGBoost.")
        scale_pos = float((y_train == 0).sum() / (y_train == 1).sum())
        # Rileva versione xgboost: early stopping API cambia tra versioni
        _xgb_ver = tuple(int(x) for x in xgb.__version__.split(".")[:2])
        # < 1.3  → early_stopping_rounds nel costruttore
        # >= 1.3 → callbacks=[EarlyStopping(...)] in fit()
        _xgb_constructor_kwargs = dict(
            n_estimators=CONFIG["N_ESTIMATORS"],
            max_depth=CONFIG["MAX_DEPTH"],
            learning_rate=0.05,
            scale_pos_weight=scale_pos,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=CONFIG["RANDOM_STATE"],
            eval_metric="aucpr",
            verbosity=0,
        )
        if _xgb_ver < (1, 3):
            # API vecchia: early_stopping va nel costruttore
            _xgb_constructor_kwargs["early_stopping_rounds"] = 30
        modello = xgb.XGBClassifier(**_xgb_constructor_kwargs)
        log.info(f"[training] XGBoost {xgb.__version__} — "
                 f"early stopping: {'costruttore' if _xgb_ver < (1,3) else 'callback'}")
    else:
        log.info("[training] Uso RandomForestClassifier.")
        modello = RandomForestClassifier(
            n_estimators=CONFIG["N_ESTIMATORS"],
            max_depth=CONFIG["MAX_DEPTH"],
            class_weight="balanced",
            random_state=CONFIG["RANDOM_STATE"],
            n_jobs=-1,
        )

    # ── Cross-validation temporale senza leakage ──
    # Usiamo una Pipeline(scaler + modello) su dati RAW: ogni fold scala
    # autonomamente il proprio split, eliminando il leakage globale dello scaler.
    from sklearn.pipeline import Pipeline as SkPipeline
    cv = TimeSeriesSplit(n_splits=CONFIG["CV_FOLDS"])

    if XGBOOST_AVAILABLE:
        clf_for_cv = xgb.XGBClassifier(
            n_estimators=CONFIG["N_ESTIMATORS"],
            max_depth=CONFIG["MAX_DEPTH"],
            learning_rate=0.05,
            scale_pos_weight=scale_pos,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=CONFIG["RANDOM_STATE"],
            verbosity=0,
        )
    else:
        clf_for_cv = RandomForestClassifier(
            n_estimators=CONFIG["N_ESTIMATORS"],
            max_depth=CONFIG["MAX_DEPTH"],
            class_weight="balanced",
            random_state=CONFIG["RANDOM_STATE"],
            n_jobs=-1,
        )
    pipeline_cv = SkPipeline([("scaler", RobustScaler()), ("clf", clf_for_cv)])

    cv_auc = cross_val_score(pipeline_cv, X_raw_all, y, cv=cv,
                              scoring="roc_auc", n_jobs=1)
    cv_ap  = cross_val_score(pipeline_cv, X_raw_all, y, cv=cv,
                              scoring="average_precision", n_jobs=1)
    log.info(f"[training] CV ROC-AUC:  {cv_auc.mean():.4f} ± {cv_auc.std():.4f}")
    log.info(f"[training] CV Avg-Prec: {cv_ap.mean():.4f} ± {cv_ap.std():.4f}")

    # ── Fit finale ──
    # FIX: early stopping usava X_test/y_test come eval_set → leakage sul test.
    # Riserviamo un validation set interno (ultimi 15% di X_train) separato dal test.
    val_split = int(len(X_train) * 0.85)
    X_tr, X_val = X_train[:val_split], X_train[val_split:]
    y_tr, y_val = y_train[:val_split], y_train[val_split:]

    if XGBOOST_AVAILABLE and isinstance(modello, xgb.XGBClassifier):
        # Fallback progressivo: prova le API di early stopping in ordine di preferenza.
        # XGBoost ha cambiato l'API in ogni major version, questo gestisce tutte.
        _base_kwargs = dict(eval_set=[(X_val, y_val)], verbose=False)
        _fit_attempts = [
            # 1. xgb >= 1.3 e < 3.0: callbacks in fit()
            {**_base_kwargs, "callbacks": [xgb.callback.EarlyStopping(rounds=30, save_best=True)]},
            # 2. xgb 1.x stile vecchio: early_stopping_rounds in fit()
            {**_base_kwargs, "early_stopping_rounds": 30},
            # 3. Fallback universale: niente early stopping (usa n_estimators fisso)
            _base_kwargs,
        ]
        for _attempt in _fit_attempts:
            try:
                modello.fit(X_tr, y_tr, **_attempt)
                _used = list(_attempt.keys() - _base_kwargs.keys()) or ["nessuno"]
                log.info(f"[training] fit() riuscito con parametri extra: {_used}")
                break
            except TypeError:
                continue
    else:
        modello.fit(X_tr, y_tr)

    # ── Valutazione sul test set ──
    y_pred_proba = modello.predict_proba(X_test)[:, 1]
    y_pred       = (y_pred_proba >= 0.5).astype(int)

    auc   = roc_auc_score(y_test, y_pred_proba)
    ap    = average_precision_score(y_test, y_pred_proba)
    log.info(f"\n[training] Test set — ROC-AUC: {auc:.4f}, Avg Precision: {ap:.4f}")
    log.info(f"\n{classification_report(y_test, y_pred, target_names=['No pump','Pump'])}")

    # ── Feature importance ──
    if hasattr(modello, "feature_importances_"):
        imp = pd.Series(modello.feature_importances_, index=FEATURE_COLUMNS)
        imp = imp.sort_values(ascending=False)
        log.info("\n── Top 10 feature per importanza ──")
        log.info(imp.head(10).to_string())

    # ── Salvataggio modello e scaler ──
    joblib.dump(modello, CONFIG["MODEL_PATH"])
    joblib.dump(scaler,  CONFIG["SCALER_PATH"])
    log.info(f"[training] Modello salvato in: {CONFIG['MODEL_PATH']}")
    log.info(f"[training] Scaler salvato in: {CONFIG['SCALER_PATH']}")

    return modello, scaler




# ==============================================================================
# SEZIONE 7b – WALK-FORWARD BACKTEST CON METRICHE DA TRADING
# ==============================================================================

def walk_forward_backtest(df: pd.DataFrame, n_splits: int = 5) -> dict:
    """
    Walk-forward backtest su dati storici simulati o reali.

    Metriche calcolate per ogni fold e aggregate:
      • ROC-AUC / Average Precision     → qualità del ranking probabilistico
      • Precision@K (K = soglia segnale) → % segnali che avrebbero pompato
      • Hit rate                         → precision@soglia CONFIG["SIGNAL_THRESHOLD"]
      • Net profit proxy                 → P(pump | signal) - slippage - tax stimati
      • Signal rate                      → frequenza segnali generati (non troppo alta)
      • Calibration slope               → 1.0 = prob calibrate, > 1 = sottostima, < 1 = sovrastima

    La funzione è pensata per essere chiamata dopo build_dataset() ma prima
    di avviare il loop principale, come sanity check del modello.

    Ritorna: dict con metriche aggregate (mean ± std per fold)
    """
    from sklearn.calibration import calibration_curve

    log.info("[backtest] Avvio walk-forward backtest...")
    df_feat = engineer_features(df.copy())
    df_feat = df_feat.dropna(subset=["target"])

    # Ordina per timestamp (walk-forward richiede ordine temporale)
    ts_col = next((c for c in ("snapshot_ts", "timestamp") if c in df_feat.columns), None)
    if ts_col:
        df_feat = df_feat.sort_values(ts_col).reset_index(drop=True)

    X = df_feat[FEATURE_COLUMNS].values
    y = df_feat["target"].values.astype(int)

    tscv    = TimeSeriesSplit(n_splits=n_splits)
    results = []

    for fold_i, (train_idx, test_idx) in enumerate(tscv.split(X)):
        if len(np.unique(y[test_idx])) < 2:
            log.warning(f"[backtest] Fold {fold_i+1}: test privo di una classe — salto.")
            continue

        X_tr_raw, X_te_raw = X[train_idx], X[test_idx]
        y_tr,     y_te     = y[train_idx], y[test_idx]

        # Scala per fold (nessun leakage)
        sc = RobustScaler()
        X_tr = sc.fit_transform(X_tr_raw)
        X_te = sc.transform(X_te_raw)

        # Modello leggero per backtest (evita overfitting su fold piccoli)
        if XGBOOST_AVAILABLE:
            clf = xgb.XGBClassifier(
                n_estimators=150,
                max_depth=4,
                learning_rate=0.05,
                scale_pos_weight=float((y_tr == 0).sum() / max((y_tr == 1).sum(), 1)),
                subsample=0.8,
                colsample_bytree=0.8,
                random_state=CONFIG["RANDOM_STATE"],
                verbosity=0,
            )
        else:
            clf = RandomForestClassifier(
                n_estimators=150,
                max_depth=4,
                class_weight="balanced",
                random_state=CONFIG["RANDOM_STATE"],
            )

        clf.fit(X_tr, y_tr)
        prob_te = clf.predict_proba(X_te)[:, 1]

        # ── Metriche standard ──
        auc  = roc_auc_score(y_te, prob_te)
        ap   = average_precision_score(y_te, prob_te)

        # ── Hit rate (precision alla soglia di segnale) ──
        threshold  = CONFIG["SIGNAL_THRESHOLD"]
        signal_mask = prob_te >= threshold
        n_signals   = signal_mask.sum()
        hit_rate    = y_te[signal_mask].mean() if n_signals > 0 else 0.0
        signal_rate = n_signals / len(y_te)

        # ── Net profit proxy ──
        # Stima: se hit_rate% dei segnali pompano +PUMP_THRESHOLD_PCT%
        # e il resto perde -SLIPPAGE_ESTIMATE_PCT%, qual è il profitto medio per trade?
        pump_gain   = CONFIG["PUMP_THRESHOLD_PCT"] / 100.0
        slip_cost   = CONFIG["SLIPPAGE_ESTIMATE_PCT"] / 100.0
        avg_tax     = 0.03   # stima media tasse round-trip (3%)
        net_profit  = hit_rate * pump_gain - (1 - hit_rate) * (slip_cost + avg_tax)

        # ── Calibrazione slope (regressione prob_te → y_te) ──
        # slope ≈ 1 = ben calibrato; slope > 1 = modello troppo conservativo
        try:
            frac_pos, mean_pred = calibration_curve(y_te, prob_te, n_bins=5)
            if len(mean_pred) > 1:
                cal_slope = float(np.polyfit(mean_pred, frac_pos, 1)[0])
            else:
                cal_slope = float("nan")
        except Exception:
            cal_slope = float("nan")

        fold_result = {
            "fold":        fold_i + 1,
            "n_train":     len(y_tr),
            "n_test":      len(y_te),
            "roc_auc":     auc,
            "avg_prec":    ap,
            "hit_rate":    hit_rate,
            "signal_rate": signal_rate,
            "n_signals":   int(n_signals),
            "net_profit":  net_profit,
            "cal_slope":   cal_slope,
        }
        results.append(fold_result)

        log.info(
            f"[backtest] Fold {fold_i+1}/{n_splits} | "
            f"AUC={auc:.3f} AP={ap:.3f} | "
            f"Hit={hit_rate:.0%} ({n_signals} segnali) | "
            f"NetProfit={net_profit:+.1%} | "
            f"CalSlope={cal_slope:.2f}"
        )

    if not results:
        log.warning("[backtest] Nessun fold valido completato.")
        return {}

    # ── Aggregazione metriche ──
    df_res = pd.DataFrame(results)
    summary = {}
    for col in ["roc_auc", "avg_prec", "hit_rate", "signal_rate", "net_profit", "cal_slope"]:
        summary[f"{col}_mean"] = float(df_res[col].mean())
        summary[f"{col}_std"]  = float(df_res[col].std())

    log.info("\n[backtest] ── Riepilogo walk-forward ─────────────────────────────")
    log.info(f"  ROC-AUC        : {summary['roc_auc_mean']:.3f} ± {summary['roc_auc_std']:.3f}")
    log.info(f"  Avg Precision  : {summary['avg_prec_mean']:.3f} ± {summary['avg_prec_std']:.3f}")
    log.info(f"  Hit Rate       : {summary['hit_rate_mean']:.1%} ± {summary['hit_rate_std']:.1%}  ← % segnali che pompano")
    log.info(f"  Signal Rate    : {summary['signal_rate_mean']:.1%} ± {summary['signal_rate_std']:.1%}  ← frequenza segnali")
    log.info(f"  Net Profit/trade: {summary['net_profit_mean']:+.1%} ± {summary['net_profit_std']:.1%}  ← dopo slippage+tax")
    log.info(f"  Calibration ρ  : {summary['cal_slope_mean']:.2f} ± {summary['cal_slope_std']:.2f}  ← 1.0=perfetto")
    log.info(
        f"  ⚠️  NOTA: questi numeri sono su DATI SIMULATI e non garantiscono "
        f"le stesse performance su dati reali."
    )

    # Warning se le metriche indicano overfitting al generatore
    if summary["roc_auc_mean"] > 0.85:
        log.warning(
            "[backtest] ⚠️  ROC-AUC > 0.85 su dati simulati: possibile overfitting "
            "alla funzione generatrice. Verifica con dati storici reali prima del deploy."
        )
    if summary["hit_rate_mean"] > 0.70:
        log.warning(
            "[backtest] ⚠️  Hit rate > 70%: soglia di segnale troppo facile su simulazione. "
            "In produzione aspettati hit rate 20-40% su token reali."
        )

    return summary

def load_model() -> tuple:
    """
    Carica modello e scaler da disco se esistono.
    Valida che il numero di feature sia compatibile con FEATURE_COLUMNS.
    Se c'è mismatch elimina i file e forza il retrain.
    Ritorna (modello, scaler) o (None, None) se non trovati/incompatibili.
    """
    if os.path.exists(CONFIG["MODEL_PATH"]) and \
       os.path.exists(CONFIG["SCALER_PATH"]):
        try:
            modello = joblib.load(CONFIG["MODEL_PATH"])
            scaler  = joblib.load(CONFIG["SCALER_PATH"])

            # ── Controllo compatibilità feature ──────────────────────────────
            n_expected = len(FEATURE_COLUMNS)
            n_scaler   = getattr(scaler, "n_features_in_", None)
            n_model    = getattr(modello, "n_features_in_", None)

            mismatch = (
                (n_scaler is not None and n_scaler != n_expected) or
                (n_model  is not None and n_model  != n_expected)
            )
            if mismatch:
                log.warning(
                    f"[load] ⚠️  Mismatch feature: modello={n_model}, "
                    f"scaler={n_scaler}, attese={n_expected}. "
                    "Elimino i file e forzo il retrain."
                )
                for path in [CONFIG["MODEL_PATH"], CONFIG["SCALER_PATH"]]:
                    try:
                        os.remove(path)
                        log.info(f"[load] Eliminato: {path}")
                    except Exception as e:
                        log.warning(f"[load] Impossibile eliminare {path}: {e}. "
                                    "Eliminalo manualmente e riavvia.")
                return None, None

            log.info(f"[load] Modello e scaler caricati da disco ({n_expected} feature). ✅")
            return modello, scaler

        except Exception as e:
            log.error(f"[load] Errore caricamento modello: {e}. Forzo retrain.")
            return None, None

    return None, None

# ==============================================================================
# SEZIONE 8 – PREDIZIONE E SCORING
# ==============================================================================

def predict_and_score(df: pd.DataFrame, modello, scaler) -> pd.DataFrame:
    """
    Applica feature engineering e modello a un DataFrame di token.
    Aggiunge le colonne:
      - pump_probability: probabilità predetta P(pump)
      - pump_label: classificazione binaria (0/1)
      - top_features: stringa con le feature più importanti
    """
    # Salta re-engineering se le feature composte sono già presenti (evita doppio calcolo)
    if FEATURE_COLUMNS[0] in df.columns:
        df_feat = df.copy()
    else:
        df_feat = engineer_features(df)

    # Modello ML disabilitato (addestrato su dati sintetici, non usato nel filtro)
    if modello is None or scaler is None:
        df_feat = df_feat.copy()
        df_feat["pump_probability"] = 0.0
        df_feat["pump_label"]       = 0
        df_feat["top_features"]     = ""
        return df_feat

    # Prepara la matrice di feature
    X = df_feat[FEATURE_COLUMNS].values
    X_scaled = scaler.transform(X)

    # Predici probabilità e label
    prob   = modello.predict_proba(X_scaled)[:, 1]
    label  = (prob >= CONFIG["SIGNAL_THRESHOLD"]).astype(int)

    df_feat = df_feat.copy()
    df_feat["pump_probability"] = prob
    df_feat["pump_label"]       = label

    # Aggiunge una descrizione delle feature più rilevanti per ogni riga
    if hasattr(modello, "feature_importances_"):
        importanze = pd.Series(modello.feature_importances_, index=FEATURE_COLUMNS)
        top3 = importanze.nlargest(3).index.tolist()
        df_feat["top_features"] = df_feat.apply(
            lambda r: " | ".join(
                f"{f}={r[f]:.2f}" for f in top3
            ), axis=1
        )
    else:
        df_feat["top_features"] = "N/A"

    return df_feat

# ==============================================================================
# SEZIONE 9 – FILTRI DI SICUREZZA (pre-segnale)
# ==============================================================================

def apply_hard_filters(df: pd.DataFrame, quiet: bool = False) -> pd.DataFrame:
    """
    Applica filtri hard prima della generazione segnali.
    Scarta token che non superano i requisiti minimi di sicurezza.
    Questi filtri sono OBBLIGATORI indipendentemente dalla probabilità del modello.
    quiet=True (tick fast-poll): riepilogo a DEBUG per non inflazionare i log.
    """
    if df.empty:
        return df

    def _col(name, default=0):
        return df[name] if name in df.columns else pd.Series(default, index=df.index)

    n_before = len(df)
    max_vol        = CONFIG.get("MAX_VOLUME_1H_USD", 80_000.0)
    max_bsr        = CONFIG.get("MAX_BSR_1H", 1.8)
    min_bsr        = CONFIG.get("MIN_BSR_1H", 0.55)
    min_buyers_wl  = int(CONFIG.get("MIN_BUYERS_1H_WALLETS", 3))
    min_mcap       = float(CONFIG.get("MIN_MCAP_USD", 0))       # 0 = disabilitato se non configurato
    max_mcap       = float(CONFIG.get("MAX_MCAP_USD", 0))       # 0 = disabilitato se non configurato

    # Filtro wallet unici (solo se disponibile, ovvero da GeckoTerminal):
    # buyers_1h_wallets=0 per Dexscreener → non applichiamo per non scartare token Dex legittimi
    buyers_wl_col = _col("buyers_1h_wallets", 0)
    wallet_mask   = (buyers_wl_col == 0) | (buyers_wl_col >= min_buyers_wl)

    # Filtro market cap (solo se > 0, cioè se disponibile)
    mcap_col      = _col("market_cap_usd", 0)
    mcap_mask     = pd.Series(True, index=df.index)
    if min_mcap > 0:
        mcap_mask = mcap_mask & ((mcap_col == 0) | (mcap_col >= min_mcap))
    if max_mcap > 0:
        mcap_mask = mcap_mask & ((mcap_col == 0) | (mcap_col <= max_mcap))

    max_liq = CONFIG.get("MAX_LIQUIDITY_USD", 0)  # 0 = nessun cap

    mask = (
        (_col("is_honeypot")  == 0)
        & (_col("buy_tax")       <= CONFIG["MAX_BUY_TAX_PCT"])
        & (_col("sell_tax")      <= CONFIG["MAX_SELL_TAX_PCT"])
        & (_col("liquidity_usd") >= CONFIG["MIN_LIQUIDITY_USD"])
        & ((max_liq <= 0) | (_col("liquidity_usd") <= max_liq))  # token maturo (es. ARK $72M) → skip
        & (_col("volume_1h_usd") >= CONFIG["MIN_VOLUME_1H_USD"])
        & (_col("volume_1h_usd") <= max_vol)           # vol>80k = pump già avvenuto
        & (_col("buy_sell_ratio_1h") >= min_bsr)       # BSR<0.55 = sells dominanti o zero attività
        & (_col("buy_sell_ratio_1h") <= max_bsr)       # BSR>1.8 = wash trading sospetto
        & (_col("change_1h_pct") >= CONFIG["MIN_CHANGE_1H_PCT"])
        & wallet_mask                                  # Phase 1: wallet unici GeckoTerminal
        & mcap_mask                                    # Phase 1: market cap range
    )
    # Soglia change_1h differenziata per età:
    # - Token freschi (<48h): MAX_CHANGE_1H_PCT=12% (memecoins pump-and-dump veloci)
    # - Token maturi (≥48h, liq>$50k): soglia alzata al 20% (breakout legittimo post-consolidazione)
    # Dati storici: win rate cala da 36% (5-8%) a 20% (8-12%) → >12% su freschi è postpump
    age_col = _col("pair_age_hours", 0)
    liq_col = _col("liquidity_usd", 0)
    max_chg_default = CONFIG["MAX_CHANGE_1H_PCT"]        # 12%
    max_chg_mature  = 20.0                                # token >48h con liq>50k
    mature_mask     = (age_col >= 48) & (liq_col >= 50_000)
    change_mask     = _col("change_1h_pct") <= max_chg_default
    change_mask     = change_mask | (mature_mask & (_col("change_1h_pct") <= max_chg_mature))
    mask = mask & change_mask

    df_filtrato = df[mask].copy()
    n_scartati = n_before - len(df_filtrato)

    # Applica cooldown e blacklist dinamica per token
    if not df_filtrato.empty:
        keep = []
        for _, r in df_filtrato.iterrows():
            pair = str(r.get("pair_address", "") or r.get("token_address", ""))
            sym  = r.get("token_symbol", "?")
            if _is_token_blacklisted(pair):
                log.info(f"[filtri] 🚫 {sym} BLACKLISTATO (dump massiccio precedente)")
                continue
            if _is_token_cooldown(pair):
                log.debug(f"[filtri] ⏳ {sym} in cooldown ({CONFIG.get('TOKEN_COOLDOWN_MIN',90)}min)")
                continue
            keep.append(r.name)
        n_bl = len(df_filtrato) - len(keep)
        if n_bl > 0:
            log.info(f"[filtri] 🧹 {n_bl} token rimossi da cooldown/blacklist.")
        df_filtrato = df_filtrato.loc[keep]

    # Log dettagliato: mostra ogni token scartato con il motivo
    if n_scartati > 0:
        scartati = df[~mask].copy()
        for _, r in scartati.iterrows():
            motivi = []
            if _col("is_honeypot")[r.name] != 0:                            motivi.append("honeypot")
            if _col("buy_tax")[r.name]  > CONFIG["MAX_BUY_TAX_PCT"]:       motivi.append(f"buy_tax={r.get('buy_tax',0):.0f}%")
            if _col("sell_tax")[r.name] > CONFIG["MAX_SELL_TAX_PCT"]:      motivi.append(f"sell_tax={r.get('sell_tax',0):.0f}%")
            liq_v = _col("liquidity_usd")[r.name]
            if liq_v < CONFIG["MIN_LIQUIDITY_USD"]: motivi.append(f"liq=${liq_v:,.0f}<min")
            if max_liq > 0 and liq_v > max_liq:    motivi.append(f"liq=${liq_v:,.0f}>max (token maturo)")
            vol = _col("volume_1h_usd")[r.name]
            if vol < CONFIG["MIN_VOLUME_1H_USD"]: motivi.append(f"vol1h=${vol:,.0f}<min")
            if vol > max_vol:                      motivi.append(f"vol1h=${vol:,.0f}>max (post-pump)")
            bsr = _col("buy_sell_ratio_1h")[r.name]
            if bsr < min_bsr: motivi.append(f"BSR={bsr:.2f}<min (sells dominanti o zero attività)")
            if bsr > max_bsr: motivi.append(f"BSR={bsr:.2f}>max (wash?)")
            ch   = r.get("change_1h_pct", 0) or 0
            r_age = float(r.get("pair_age_hours", 0) or 0)
            r_liq = float(r.get("liquidity_usd", 0) or 0)
            r_max_chg = 20.0 if (r_age >= 48 and r_liq >= 50_000) else CONFIG["MAX_CHANGE_1H_PCT"]
            if ch > r_max_chg: motivi.append(f"change_1h={ch:+.1f}%>max ({r_max_chg:.0f}%) (post-pump)")
            if ch < CONFIG["MIN_CHANGE_1H_PCT"]: motivi.append(f"change_1h={ch:+.1f}%<min (dump)")
            if ch < -2.0 and bsr < 0.50: motivi.append(f"anti-dump: chg={ch:+.1f}% bsr={bsr:.2f}")
            buyers_w = r.get("buyers_1h_wallets", 0) or 0
            if buyers_w > 0 and buyers_w < min_buyers_wl:
                motivi.append(f"buyers_wallets={buyers_w}<{min_buyers_wl} (bot?)")
            mcap_v = r.get("market_cap_usd", 0) or 0
            if mcap_v > 0 and min_mcap > 0 and mcap_v < min_mcap:
                motivi.append(f"mcap=${mcap_v:,.0f}<min")
            if mcap_v > 0 and max_mcap > 0 and mcap_v > max_mcap:
                motivi.append(f"mcap=${mcap_v:,.0f}>max (maturo)")
            sym  = r.get("token_symbol", "?") or "?"
            addr = str(r.get("token_address", "") or "")[:8]
            log.debug(f"[filtri] ❌ SCARTATO {sym:>10s} ({addr}…) — {', '.join(motivi)}")
        # "filtri ML" era una dicitura fossile: l'ML è disabilitato (modello sintetico),
        # i token passano alle condizioni pre-pump rule-based
        (log.debug if quiet else log.info)(
            f"[filtri] {n_scartati}/{n_before} token scartati, "
            f"{len(df_filtrato)} passano alle condizioni pre-pump.")
    else:
        log.info(f"[filtri] Tutti i {n_before} token superano i filtri di sicurezza.")
    return df_filtrato

# ==============================================================================
# SEZIONE 10 – GENERAZIONE SEGNALI
# ==============================================================================

# ==============================================================================
# FAST-POLL WATCHLIST (2-stage entry)
# ==============================================================================
# Stage 1: il ciclo lento (~6min) individua i "near-miss" — token che passano
#   TUTTE le condizioni pre-pump tranne comp>=0.55, con comp in [0.45, 0.55).
# Stage 2: un thread li ripolla ogni 30s (1 chiamata batch DexScreener per chain,
#   max 30 pair/call) e riesegue l'INTERA pipeline (engineer + hard filters +
#   condizioni via generate_signals): al 2° tick consecutivo che genera il
#   segnale → emissione. Il doppio tick sostituisce la conferma implicita che
#   prima dava la latenza dei 6 minuti (anti rumore attorno alla soglia).
# Note:
#   - vol_accel/PVD ecc. sono ratio di finestre API (m5 vs h1) → timebase-safe;
#     _update_bsr_history NON viene chiamato dal fast-poll (bsr_trend resta
#     calcolato sui soli cicli lenti).
#   - Nessuna persistenza su disco: lo stage 1 ripopola la watchlist a ogni
#     ciclo lento, dopo un restart si perde al più ~6 min di candidati.
_FASTPOLL_INTERVAL_SEC  = 30
_FASTPOLL_TTL_MIN       = 45     # un near-miss che non converge in 45min è morto
_FASTPOLL_COMP_MIN      = 0.45   # sotto questa soglia il token è troppo lontano
_FASTPOLL_MAX_WATCH     = 30     # = 1 chiamata batch DexScreener per chain
_FASTPOLL_CONFIRM_TICKS = 2      # tick consecutivi sopra soglia per emettere

_fastpoll_lock:  threading.Lock = threading.Lock()
_fastpoll_watch: dict = {}   # pair_address → {"row": dict, "added": datetime, "streak": int}

# Prezzo all'ultimo ciclo LENTO per pair (per condizione c10_notfall in generate_signals).
# Aggiornato solo con collect_nearmiss=True: i tick fast-poll a 30s NON lo toccano,
# altrimenti il confronto "vs ciclo precedente" degenererebbe a "vs 30s fa" (sempre ~0).
_prev_cycle_px: dict = {}


def _fastpoll_add_candidates(df_nearmiss: pd.DataFrame) -> None:
    """Registra i near-miss del ciclo lento nella watchlist fast-poll."""
    if df_nearmiss.empty:
        return
    now = datetime.now()
    added = []
    with _fastpoll_lock:
        for _, riga in df_nearmiss.iterrows():
            pair = str(riga.get("pair_address", "") or "")
            if not pair or pair in _fastpoll_watch:
                continue
            if len(_fastpoll_watch) >= _FASTPOLL_MAX_WATCH:
                # evict del più vecchio (FIFO): meglio perdere un candidato
                # stantio che saltare uno fresco
                oldest = min(_fastpoll_watch, key=lambda k: _fastpoll_watch[k]["added"])
                _fastpoll_watch.pop(oldest, None)
            row = {k: (v.item() if hasattr(v, "item") else v) for k, v in riga.items()}
            _fastpoll_watch[pair] = {"row": row, "added": now, "streak": 0}
            added.append(f"{row.get('token_symbol','?')}(comp={row.get('prepump_composite_score',0):.2f})")
    if added:
        log.info(f"[fastpoll] +{len(added)} near-miss in watchlist ({len(_fastpoll_watch)} totali): "
                 + ", ".join(added))


def _fastpoll_refresh_rows(entries: dict) -> list[dict]:
    """Batch-fetch DexScreener e aggiorna i campi dinamici delle righe in watch.
    Ritorna le righe aggiornate (quelle senza risposta API restano fuori dal tick)."""
    by_chain: dict = {}
    for pair, ent in entries.items():
        by_chain.setdefault(ent["row"].get("chain", "solana"), []).append(pair)
    refreshed = []
    for chain, addrs in by_chain.items():
        # _safe_get ritorna già il dict JSON deserializzato (non la Response)
        data = _safe_get(f"{DEXSCREENER_BASE}/dex/pairs/{chain}/{','.join(addrs)}",
                         label="fastpoll-pairs")
        if not data:
            continue
        pairs = data.get("pairs") or []
        for p in pairs:
            pair_addr = p.get("pairAddress", "")
            ent = entries.get(pair_addr)
            if not ent:
                continue
            volume, change = p.get("volume", {}), p.get("priceChange", {})
            txns = p.get("txns", {})
            row = dict(ent["row"])
            row.update({
                "price_usd":      float(p.get("priceUsd", 0) or 0),
                "fdv":            float(p.get("fdv", 0) or row.get("fdv", 0) or 0),
                "change_5m_pct":  float(change.get("m5",  0) or 0),
                "change_15m_pct": float(change.get("m15", 0) or 0),
                "change_1h_pct":  float(change.get("h1",  0) or 0),
                "change_24h_pct": float(change.get("h24", 0) or 0),
                "volume_5m_usd":  float(volume.get("m5",  0) or 0),
                "volume_1h_usd":  float(volume.get("h1",  0) or 0),
                "volume_24h_usd": float(volume.get("h24", 0) or 0),
                "liquidity_usd":  float((p.get("liquidity", {}) or {}).get("usd", 0) or 0),
                "buys_5m":        int((txns.get("m5",  {}) or {}).get("buys",  0) or 0),
                "sells_5m":       int((txns.get("m5",  {}) or {}).get("sells", 0) or 0),
                "buys_1h":        int((txns.get("h1",  {}) or {}).get("buys",  0) or 0),
                "sells_1h":       int((txns.get("h1",  {}) or {}).get("sells", 0) or 0),
                "buys_24h":       int((txns.get("h24", {}) or {}).get("buys",  0) or 0),
                "sells_24h":      int((txns.get("h24", {}) or {}).get("sells", 0) or 0),
                "timestamp":      datetime.now().isoformat(),
            })
            refreshed.append(row)
    return refreshed


def fastpoll_loop(stop_event=None) -> None:
    """Thread worker: ripolla la watchlist near-miss ogni 30s ed emette al cross confermato."""
    log.info(f"[fastpoll] ▶ attivo — tick {_FASTPOLL_INTERVAL_SEC}s, conferma {_FASTPOLL_CONFIRM_TICKS} tick, "
             f"TTL {_FASTPOLL_TTL_MIN}min, max {_FASTPOLL_MAX_WATCH} token")
    while not (stop_event and stop_event.is_set()):
        time.sleep(_FASTPOLL_INTERVAL_SEC)
        try:
            now = datetime.now()
            with _fastpoll_lock:
                # Scadenza TTL + token entrati in cooldown (già segnalati altrove)
                for pair in list(_fastpoll_watch):
                    ent = _fastpoll_watch[pair]
                    age_min = (now - ent["added"]).total_seconds() / 60
                    if age_min > _FASTPOLL_TTL_MIN or _is_token_cooldown(pair):
                        _fastpoll_watch.pop(pair, None)
                snapshot = {p: {"row": dict(e["row"]), "added": e["added"]}
                            for p, e in _fastpoll_watch.items()}
            if not snapshot:
                continue

            rows = _fastpoll_refresh_rows(snapshot)
            if not rows:
                continue
            # Pipeline identica al ciclo lento (engineering, hard filters, condizioni).
            # collect_nearmiss=False: il fast-poll non ri-alimenta sé stesso.
            segnali = generate_signals(pd.DataFrame(rows), None, None,
                                       collect_nearmiss=False, quiet=True)
            passed = {s.get("pair_address", ""): s for s in segnali}

            to_emit = []
            with _fastpoll_lock:
                for pair in list(_fastpoll_watch):
                    if pair in passed:
                        _fastpoll_watch[pair]["streak"] += 1
                        sym = passed[pair].get("token_symbol", "?")
                        if _fastpoll_watch[pair]["streak"] >= _FASTPOLL_CONFIRM_TICKS:
                            to_emit.append(passed[pair])
                            _fastpoll_watch.pop(pair, None)
                        else:
                            log.info(f"[fastpoll] {sym}: cross comp>=0.55 al tick "
                                     f"{_fastpoll_watch[pair]['streak']}/{_FASTPOLL_CONFIRM_TICKS} — attendo conferma")
                    elif pair in _fastpoll_watch and any(r.get("pair_address") == pair for r in rows):
                        _fastpoll_watch[pair]["streak"] = 0   # sotto soglia → riparte la conferma

            for sig in to_emit:
                # Marker per backtest: distingue le entry fast-poll da quelle del ciclo lento
                sig["top_features"] = (sig.get("top_features", "") + " | fastpoll=true").strip(" |")
                log.info(f"[fastpoll] 🚀 {sig.get('token_symbol','?')}: cross confermato su "
                         f"{_FASTPOLL_CONFIRM_TICKS} tick → segnale anticipato")
                stampa_segnale(sig)
        except Exception as e:
            log.warning(f"[fastpoll] errore tick: {e}")


def generate_signals(
    df: pd.DataFrame,
    modello,
    scaler,
    threshold: float = None,
    collect_nearmiss: bool = True,
    quiet: bool = False,
) -> list[dict]:
    """
    Genera segnali di entrata per i token che soddisfano TUTTE le condizioni:

    Condizioni modello:
      1. pump_probability > threshold (soglia configurabile)

    Condizioni tecniche (multi-condizione, non solo il modello):
      2. Volume in crescita progressiva (non un singolo spike isolato)
      3. Liquidità stabile o in crescita
      4. Buy/sell ratio > 1 nell'ultima ora
      5. Nessun flag honeypot o tax eccessiva (GoPlus)
      6. LP ragionevolmente sicuro (non ovviamente rug-prone)

    ⚠️  I segnali indicano POSSIBILI opportunità, non certezze di profitto.
        Il trading crypto è altamente rischioso.

    Parametri:
        df       : DataFrame con dati correnti (NON ancora con feature eng.)
        modello  : modello addestrato
        scaler   : scaler addestrato
        threshold: soglia probabilità (default: CONFIG["SIGNAL_THRESHOLD"])

    Ritorna: lista di dizionari, uno per ogni segnale generato.
    """
    if threshold is None:
        threshold = CONFIG["SIGNAL_THRESHOLD"]
    # quiet=True (tick fast-poll ogni 30s): diagnostica a livello DEBUG per non
    # triplicare il volume di log del ciclo lento
    _log_info = log.debug if quiet else log.info

    if df.empty:
        log.warning("[segnali] DataFrame vuoto ricevuto.")
        return []

    # ── Step 1: feature engineering (assicura buy_sell_ratio_1h e feature composte) ──
    df_eng = engineer_features(df)

    # ── Step 2: filtri hard di sicurezza (honeypot, tax, liq, vol, change, BSR) ──
    df_filtrato = apply_hard_filters(df_eng, quiet=quiet)
    if df_filtrato.empty:
        _log_info("[segnali] Nessun token supera i filtri di sicurezza.")
        return []

    # ── Step 3: predizione del modello ──
    df_scored = predict_and_score(df_filtrato, modello, scaler)
    # ── Step 4: filtri multi-condizione pre-pump ───────────────────────────
    # FIX: soglie abbassate e diagnostico per-condizione aggiunto.
    # Le soglie originali erano troppo stringenti: 9 AND simultanei → probabilità
    # combinata di passare su dati reali quasi zero.
    slippage = CONFIG.get("SLIPPAGE_ESTIMATE_PCT", 3.0)

    # Condizioni booleane nominali (per diagnostico)
    # c1_prob: soglia ML bassa (0.15) — il modello è addestrato su dati sintetici,
    #          su dati reali le probabilità sono sistematicamente basse (~0.10-0.22).
    #          Il filtro principale è il prepump_composite_score (c9_comp).
    c1_prob  = df_scored["pump_probability"] >= threshold
    # Soglie rilassate SOLO su Base (BASE_DRY_RUN=true, flusso simulato): il nativo
    # emetteva 0 segnali in 600 cicli ma il pump-rate Base ≥20%/60min è pari a
    # Solana (1.48% vs 1.28%, backtest debug_candidates 12/06). Soglie calibrate
    # su Solana strozzavano tutto; validazione live in corso, giudizio a WR>50%
    # dopo ~1 settimana sui trade defi nativo chain=base in live_trades.csv.
    _is_base   = df_scored.get("chain", pd.Series("", index=df_scored.index)).astype(str).str.lower().eq("base")
    _accel_min = pd.Series(0.7,  index=df_scored.index).mask(_is_base, 0.5)
    _pvd_min   = pd.Series(0.08, index=df_scored.index).mask(_is_base, 0.04)
    _comp_min  = pd.Series(0.55, index=df_scored.index).mask(_is_base, 0.45)
    # c2_accel: abbassato a 0.7x (era 1.0). Su Solana vol5m è spesso basso
    #           → accel raramente supera 1.0 su token legittimi non in pump.
    c2_accel = df_scored.get("vol_accel_5m_vs_1h",  pd.Series(0.5, index=df_scored.index)) >= _accel_min
    c3_bsr   = df_scored.get("buy_sell_ratio_1h",   pd.Series(1.0, index=df_scored.index)) >= 1.0
    c4_max   = df_scored["change_1h_pct"] <= CONFIG["MAX_CHANGE_1H_PCT"]
    c5_min   = df_scored["change_1h_pct"] >= CONFIG["MIN_CHANGE_1H_PCT"]
    c6_pvd   = df_scored.get("price_vol_divergence", pd.Series(0.0, index=df_scored.index)) >= _pvd_min
    c7_age   = df_scored["pair_age_hours"] >= 0.25
    c8_tax   = (df_scored["buy_tax"].fillna(0) + df_scored["sell_tax"].fillna(0) + slippage) < CONFIG["PUMP_THRESHOLD_PCT"]
    # c9_comp: gate principale basato su composite score (NON dipende dall'ML sintetico).
    #          Aggrega: vol_accel, BSR, PVD, age score, holder quality, squeeze_momentum.
    #          Soglia alzata 0.38 → 0.42 → 0.55 (analisi 7g: score<0.55 = 12% WR, -30€).
    c9_comp  = df_scored.get("prepump_composite_score", pd.Series(0.0, index=df_scored.index)) >= _comp_min
    # c10_notfall: prezzo NON in caduta >3% rispetto al ciclo lento precedente.
    #   Le metriche 1h sono lagging: il segnale spesso scatta nel retrace post-pump
    #   (STEPHEN/KINS/YETZY: prezzo in calo da 2-3 cicli all'allineamento).
    #   Backtest 10/06 su n=46 con storia: blocca 13, precisione 77%, pnl bloccato -35€,
    #   sottoinsieme tenuto +17€→+52€. I token alla 1ª apparizione passano (nessuna storia).
    _prev_px = df_scored["pair_address"].map(_prev_cycle_px) if "pair_address" in df_scored.columns else pd.Series(dtype=float)
    c10_notfall = pd.Series(True, index=df_scored.index)
    if len(_prev_px):
        _has_prev = _prev_px.notna() & (_prev_px > 0)
        c10_notfall = ~_has_prev | (df_scored["price_usd"] >= _prev_px * 0.97)
    # c11_bsrshift: venditori non in surge ADESSO (bsr_5m vs bsr_1h).
    #   PROVVISORIA: backtest n=8 (strumentata 08/06), 4 bloccate tutte loss (-44€),
    #   0 win perse — ricontrollare quando n>=20.
    c11_bsrshift = df_scored.get("bsr_recent_shift", pd.Series(0.0, index=df_scored.index)) >= -0.15

    # Log diagnostico: quanti token falliscono per ogni condizione
    # NOTA: c1_prob (pump_probability ML) RIMOSSO — modello addestrato su dati sintetici
    # era inversamente correlato (prob alta = più perdite). Gate ora puramente rule-based.
    _log_info(
        f"[segnali] Diagnosi condizioni su {len(df_scored)} token (ML disabilitato, solo rule-based): "
        f"accel≥0.7: {c2_accel.sum()} | "
        f"BSR≥1.0: {c3_bsr.sum()} | "
        f"ch1h in range: {(c4_max & c5_min).sum()} | "
        f"PVD≥0.08: {c6_pvd.sum()} | "
        f"age≥15m: {c7_age.sum()} | "
        f"tax ok: {c8_tax.sum()} | "
        f"comp≥0.55: {c9_comp.sum()} | "
        f"not-falling: {c10_notfall.sum()} | "
        f"bsr_shift ok: {c11_bsrshift.sum()}"
    )

    base_cond = c2_accel & c3_bsr & c4_max & c5_min & c6_pvd & c7_age & c8_tax & c10_notfall & c11_bsrshift
    candidati = df_scored[base_cond & c9_comp].copy()

    # Aggiorna lo storico prezzi per c10_notfall DOPO la valutazione (il confronto
    # di questo ciclo usa solo il ciclo precedente). Solo cicli lenti.
    if collect_nearmiss and "pair_address" in df_scored.columns:
        for _pa, _px in zip(df_scored["pair_address"], df_scored["price_usd"]):
            if _pa and _px and _px > 0:
                _prev_cycle_px[str(_pa)] = float(_px)
        if len(_prev_cycle_px) > 2000:   # bound memoria: tiene gli ultimi inseriti
            for _k in list(_prev_cycle_px)[:-1500]:
                _prev_cycle_px.pop(_k, None)

    # Stage 1 fast-poll: near-miss = tutte le condizioni OK tranne comp>=0.55,
    # con comp >= 0.45 → watchlist a 30s per catturare il cross senza aspettare
    # il prossimo ciclo lento (vedi blocco FAST-POLL WATCHLIST sopra)
    if collect_nearmiss:
        _comp_series = df_scored.get("prepump_composite_score", pd.Series(0.0, index=df_scored.index))
        nearmiss = df_scored[base_cond & ~c9_comp & (_comp_series >= _FASTPOLL_COMP_MIN)]
        if not nearmiss.empty:
            _fastpoll_add_candidates(nearmiss)

    if candidati.empty:
        _log_info("[segnali] Nessun token supera tutte le condizioni pre-pump "
                  "(accel≥0.7, BSR≥1.0, PVD≥0.08, comp≥0.55, change in range).")
        return []

    # ── Step 4: costruzione dei segnali ──
    segnali = []
    for _, riga in candidati.iterrows():
        vol_accel  = float(riga.get("vol_accel_5m_vs_1h", 0) or 0)
        pv_div     = float(riga.get("price_vol_divergence", 0) or 0)
        bps        = float(riga.get("buy_pressure_score", 0) or 0)
        mom_cons   = int(riga.get("momentum_consistency", 0) or 0)
        composite  = float(riga.get("prepump_composite_score", 0) or 0)
        age_score  = float(riga.get("pair_age_score", 0) or 0)
        hq_score   = float(riga.get("holder_quality_score", 0) or 0)
        fdv_liq    = float(riga.get("fdv_to_liq_ratio", 0) or 0)
        tax_total  = float(riga.get("total_tax_cost", 0) or 0)
        dump_risk  = float(riga.get("dump_risk_score", 0) or 0)
        sell_press = float(riga.get("sell_pressure_momentum", 0) or 0)
        bsr_trend     = float(riga.get("bsr_trend_per_min", 0) or 0)
        bsr_trend_n   = int(riga.get("bsr_trend_samples", 0) or 0)
        bsr_5m        = float(riga.get("bsr_5m", 0) or 0)
        bsr_shift     = float(riga.get("bsr_recent_shift", 0) or 0)
        # Persistito in top_features (stringa libera già presente nello schema signals_log.csv)
        # per poter fare backtest futuri senza migrare lo schema CSV.
        top_features_str = (
            f"bsr_trend_per_min={bsr_trend:+.4f} | bsr_trend_samples={bsr_trend_n} | "
            f"bsr_5m={bsr_5m:.3f} | bsr_recent_shift={bsr_shift:+.4f}"
        )

        segnale = {
            # Identificatori
            "timestamp":               datetime.now().isoformat(),
            "token_symbol":            riga.get("token_symbol", ""),
            "token_name":              riga.get("token_name", ""),
            "token_address":           riga.get("token_address", ""),
            "chain":                   riga.get("chain", ""),
            "pair_address":            riga.get("pair_address", ""),
            # Metriche mercato
            "price_usd":               round(float(riga.get("price_usd", 0) or 0), 8),
            "volume_1h_usd":           round(float(riga.get("volume_1h_usd", 0) or 0), 2),
            "liquidity_usd":           round(float(riga.get("liquidity_usd", 0) or 0), 2),
            "holders_total":           int(riga.get("holders_total", 0) or 0),
            "buy_sell_ratio_1h":       round(float(riga.get("buy_sell_ratio_1h", 0) or 0), 3),
            "change_1h_pct":           round(float(riga.get("change_1h_pct", 0) or 0), 2),
            # Pre-pump signals
            "vol_accel_5m_vs_1h":      round(vol_accel, 3),
            "price_vol_divergence":    round(pv_div, 3),
            "buy_pressure_score":      round(bps, 3),
            "momentum_consistency":    mom_cons,
            "prepump_composite_score": round(composite, 4),
            "pair_age_score":          round(age_score, 3),
            "holder_quality_score":    round(hq_score, 3),
            "fdv_to_liq_ratio":        round(fdv_liq, 1),
            "total_tax_cost":          round(tax_total, 2),
            # Anti-dump signals (NUOVO)
            "dump_risk_score":         round(dump_risk, 3),
            "sell_pressure_momentum":  round(sell_press, 3),
            "bsr_trend_per_min":       round(bsr_trend, 4),
            "bsr_trend_samples":       bsr_trend_n,
            "bsr_5m":                  round(bsr_5m, 3),
            "bsr_recent_shift":        round(bsr_shift, 4),
            # Output modello
            "pump_probability":        round(float(riga.get("pump_probability", 0) or 0), 4),
            "top_features":            top_features_str,
            # Flag sicurezza
            "buy_tax":                 float(riga.get("buy_tax", 0) or 0),
            "sell_tax":                float(riga.get("sell_tax", 0) or 0),
            "lp_locked":               bool(riga.get("lp_locked", False)),
            "is_honeypot":             bool(riga.get("is_honeypot", False)),
            "disclaimer": "⚠️ NON è un consiglio finanziario. Il trading crypto è ad alto rischio.",
        }
        segnali.append(segnale)

    # Ordina per composite score decrescente (ML disabilitato, priorità rule-based)
    segnali.sort(key=lambda s: s["prepump_composite_score"], reverse=True)
    log.info(f"[segnali] ✅ Generati {len(segnali)} segnali pre-pump "
             f"(da {len(df_filtrato)} candidati dopo filtri hard).")
    return segnali


def invia_segnale_email(segnale: dict) -> bool:
    """Invia una email HTML con i dettagli del segnale pre-pump."""
    cfg  = EMAIL_CONFIG
    prob = float(segnale.get("pump_probability", 0) or 0)

    if not cfg["SMTP_USER"] or not cfg["SMTP_PASSWORD"]:
        log.warning("[email] Credenziali SMTP non configurate — email non inviata.")
        return False
    if prob < cfg["MIN_PROBABILITY"]:
        log.debug(f"[email] prob={prob:.1%} < soglia {cfg['MIN_PROBABILITY']:.1%} — skip.")
        return False

    # ── Estrai tutti i valori prima del template ──────────────────────────────
    sym      = str(segnale.get("token_symbol", "?"))
    chain    = str(segnale.get("chain", "?")).upper()
    addr     = str(segnale.get("token_address", ""))
    pair_addr= str(segnale.get("pair_address", ""))
    price    = float(segnale.get("price_usd", 0) or 0)
    vol1h    = float(segnale.get("volume_1h_usd", 0) or 0)
    liq      = float(segnale.get("liquidity_usd", 0) or 0)
    bsr      = float(segnale.get("buy_sell_ratio_1h", 0) or 0)
    ch1h     = float(segnale.get("change_1h_pct", 0) or 0)
    accel    = float(segnale.get("vol_accel_5m_vs_1h", 0) or 0)
    pvd      = float(segnale.get("price_vol_divergence", 0) or 0)
    bps      = float(segnale.get("buy_pressure_score", 0) or 0)
    mom      = int(segnale.get("momentum_consistency", 0) or 0)
    comp     = float(segnale.get("prepump_composite_score", 0) or 0)
    dump     = float(segnale.get("dump_risk_score", 0) or 0)
    buy_tax  = float(segnale.get("buy_tax", 0) or 0)
    sell_tax = float(segnale.get("sell_tax", 0) or 0)
    lp_lock  = bool(segnale.get("lp_locked", False))
    top_f    = str(segnale.get("top_features", ""))
    ts       = str(segnale.get("timestamp", datetime.now().isoformat()))[:16].replace("T", " ")

    # ── Colori e valori derivati ──────────────────────────────────────────────
    chain_colors = {"SOLANA": "#9945FF", "BSC": "#F0B90B", "BASE": "#0052FF", "ETHEREUM": "#627EEA"}
    cc           = chain_colors.get(chain, "#64748b")
    bar_pct      = min(int(comp * 100), 100)
    bar_w        = min(int(comp * 200), 200)   # px su 200px totali
    ch1h_col     = "#4ade80" if ch1h >= 0 else "#f87171"
    ch1h_str     = f"{ch1h:+.2f}%"
    accel_col    = "#4ade80" if accel >= 1.3 else "#facc15" if accel >= 0.7 else "#f87171"
    dump_col     = "#4ade80" if dump < 0.3 else ("#facc15" if dump < 0.6 else "#f87171")
    dump_lbl     = "✅ Basso" if dump < 0.3 else ("⚠️ Medio" if dump < 0.6 else "🔴 ALTO")
    mom_str      = "✅ TF concordi" if mom else "⚠️ TF discordanti"
    mom_col      = "#4ade80" if mom else "#facc15"
    lp_str       = "🔒 Sì" if lp_lock else "❌ No"
    lp_col       = "#4ade80" if lp_lock else "#f87171"
    tax_col      = "#4ade80" if (buy_tax + sell_tax) == 0 else ("#facc15" if (buy_tax + sell_tax) <= 5 else "#f87171")
    top_f_html   = ("🔑 " + top_f) if top_f else ""
    dex_chain_map = {"SOLANA": "solana", "BSC": "bsc", "BASE": "base", "ETHEREUM": "ethereum"}
    dex_chain    = dex_chain_map.get(chain, chain.lower())
    dex_url      = (f"https://dexscreener.com/{dex_chain}/{pair_addr}"
                    if pair_addr else f"https://dexscreener.com/{dex_chain}/{addr}")

    # ── Template HTML (solo f-string, nessun .format()) ──────────────────────
    html_body = f"""<!DOCTYPE html>
<html lang="it">
<head><meta charset="UTF-8">
<meta name="color-scheme" content="dark">
</head>
<body style="margin:0;padding:0;background-color:#020617;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#020617;">
<tr><td align="center" style="padding:24px 12px;">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

  <!-- HEADER -->
  <tr><td style="background-color:#020617;border-bottom:2px solid #22c55e;padding:0 0 14px 0;">
    <p style="margin:0;font-size:22px;font-weight:800;color:#22c55e;">🟢 SEGNALE PRE-PUMP</p>
    <p style="margin:4px 0 0;font-size:13px;color:#64748b;">{ts} — defi_optimized</p>
  </td></tr>

  <!-- TOKEN + PROB -->
  <tr><td style="background-color:#020617;padding:16px 0 12px;">
    <table cellpadding="0" cellspacing="0">
      <tr>
        <td style="padding-right:10px;font-size:30px;font-weight:900;color:#e2e8f0;">{sym}</td>
        <td style="padding-right:8px;">
          <span style="background-color:{cc};color:#ffffff;border-radius:4px;
                       padding:3px 10px;font-size:12px;font-weight:700;">{chain}</span>
        </td>
        <td>
          <span style="background-color:#166534;color:#4ade80;border-radius:4px;
                       padding:4px 12px;font-size:13px;font-weight:800;border:1px solid #22c55e;">
            P(pump) = {prob:.1%}
          </span>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- METRICHE MERCATO -->
  <tr><td style="padding-bottom:12px;">
    <table width="100%" cellpadding="6" cellspacing="4">
      <tr>
        <td width="50%" style="background-color:#0f172a;border:1px solid #1e293b;border-radius:6px;padding:10px;">
          <p style="margin:0;font-size:11px;color:#64748b;">Prezzo Entry</p>
          <p style="margin:4px 0 0;font-weight:700;font-family:monospace;color:#e2e8f0;">${price:.8f}</p>
        </td>
        <td width="50%" style="background-color:#0f172a;border:1px solid #1e293b;border-radius:6px;padding:10px;">
          <p style="margin:0;font-size:11px;color:#64748b;">Variazione 1h</p>
          <p style="margin:4px 0 0;font-weight:700;color:{ch1h_col};">{ch1h_str}</p>
        </td>
      </tr>
      <tr>
        <td style="background-color:#0f172a;border:1px solid #1e293b;border-radius:6px;padding:10px;">
          <p style="margin:0;font-size:11px;color:#64748b;">Volume 1h</p>
          <p style="margin:4px 0 0;font-weight:700;color:#e2e8f0;">${vol1h:,.0f}</p>
        </td>
        <td style="background-color:#0f172a;border:1px solid #1e293b;border-radius:6px;padding:10px;">
          <p style="margin:0;font-size:11px;color:#64748b;">Liquidità</p>
          <p style="margin:4px 0 0;font-weight:700;color:#e2e8f0;">${liq:,.0f}</p>
        </td>
      </tr>
      <tr>
        <td style="background-color:#0f172a;border:1px solid #1e293b;border-radius:6px;padding:10px;">
          <p style="margin:0;font-size:11px;color:#64748b;">Buy/Sell Ratio</p>
          <p style="margin:4px 0 0;font-weight:700;color:#a78bfa;">{bsr:.2f}x</p>
        </td>
        <td style="background-color:#0f172a;border:1px solid #1e293b;border-radius:6px;padding:10px;">
          <p style="margin:0;font-size:11px;color:#64748b;">Tax (Buy / Sell)</p>
          <p style="margin:4px 0 0;font-weight:700;color:{tax_col};">{buy_tax:.1f}% / {sell_tax:.1f}%</p>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- PRE-PUMP SIGNALS -->
  <tr><td style="background-color:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:14px;margin-bottom:12px;">
    <p style="margin:0 0 12px;font-size:12px;font-weight:700;color:#94a3b8;letter-spacing:.5px;">
      ── PRE-PUMP SIGNALS ──────────────────────────────
    </p>
    <table width="100%" cellpadding="4" cellspacing="0">
      <tr>
        <td style="font-size:12px;color:#94a3b8;width:130px;">VolAccel 5m</td>
        <td style="font-weight:700;color:{accel_col};">{accel:.2f}x
          <span style="font-size:11px;color:#64748b;">&nbsp;(&gt;1.3 = accumulo)</span>
        </td>
      </tr>
      <tr>
        <td style="font-size:12px;color:#94a3b8;">PV Divergence</td>
        <td style="font-weight:700;color:#e2e8f0;">{pvd:.3f}
          <span style="font-size:11px;color:#64748b;">&nbsp;(alto = prezzo fermo + vol esplode)</span>
        </td>
      </tr>
      <tr>
        <td style="font-size:12px;color:#94a3b8;">Buy Pressure</td>
        <td style="font-weight:700;color:#a78bfa;">{bps:.2f}</td>
      </tr>
      <tr>
        <td style="font-size:12px;color:#94a3b8;">Momentum</td>
        <td style="font-weight:700;color:{mom_col};">{mom_str}</td>
      </tr>
      <tr>
        <td style="font-size:12px;color:#94a3b8;">Dump Risk</td>
        <td style="font-weight:700;color:{dump_col};">{dump:.3f} — {dump_lbl}</td>
      </tr>
      <tr>
        <td style="font-size:12px;color:#94a3b8;">LP Locked</td>
        <td style="font-weight:700;color:{lp_col};">{lp_str}</td>
      </tr>
    </table>

    <!-- COMPOSITE BAR -->
    <p style="margin:12px 0 4px;font-size:12px;color:#94a3b8;">
      Composite Score: <strong style="color:#e2e8f0;">{comp:.3f} / 1.000</strong>
    </p>
    <table width="200" cellpadding="0" cellspacing="0">
      <tr>
        <td style="background-color:#1e293b;border-radius:4px;height:10px;width:200px;">
          <div style="width:{bar_w}px;height:10px;background-color:#22c55e;border-radius:4px;"></div>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- TOP FEATURES -->
  <tr><td style="padding:10px 0;">
    <p style="margin:0;font-size:11px;color:#64748b;word-break:break-word;">{top_f_html}</p>
  </td></tr>

  <!-- INDIRIZZO -->
  <tr><td style="background-color:#0f172a;border:1px solid #1e293b;border-radius:6px;
                 padding:10px;font-family:monospace;font-size:11px;
                 color:#94a3b8;word-break:break-all;">
    {addr}
  </td></tr>

  <!-- BUTTON -->
  <tr><td style="text-align:center;padding:16px 0;">
    <a href="{dex_url}"
       style="background-color:#22c55e;color:#020617;padding:11px 28px;
              border-radius:6px;text-decoration:none;font-weight:800;font-size:14px;
              display:inline-block;">
      📊 Apri su DexScreener
    </a>
  </td></tr>

  <!-- DISCLAIMER -->
  <tr><td style="background-color:#1c1a00;border:1px solid #6e5908;border-radius:6px;
                 padding:10px;font-size:12px;color:#a16207;">
    ⚠️ Solo a scopo educativo. NON è un consiglio finanziario.
    Il trading di criptovalute comporta rischi molto elevati di perdita del capitale.
  </td></tr>

</table>
</td></tr></table>
</body>
</html>"""

    subject = f"🟢 [{chain}] Pre-Pump: {sym} | P={prob:.0%} | {ch1h_str}"
    try:
        import email_digest
        email_digest.queue_email("defi", subject, html_body)
        log.info(f"[email] 📥 {sym} ({chain}) accodata al digest")
        return True
    except ImportError:
        pass   # standalone: invio diretto
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = cfg["FROM_ADDR"] or cfg["SMTP_USER"]
        msg["To"]      = cfg["TO_ADDR"]
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            server.sendmail(cfg["SMTP_USER"], cfg["TO_ADDR"], msg.as_string())

        log.info(f"[email] ✅ Inviata: {sym} ({chain}) P={prob:.1%} → {cfg['TO_ADDR']}")
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("[email] ❌ Auth fallita — controlla App Password Gmail.")
    except Exception as e:
        log.warning(f"[email] ⚠️  Invio fallito: {e}")
    return False


def stampa_segnale(segnale: dict) -> None:
    """Stampa un segnale in formato leggibile nel log e invia email."""
    if not rugcheck_safe(segnale.get("token_address", ""), "defi",
                         chain=segnale.get("chain", "solana")):
        return

    sep = "=" * 65
    sym   = segnale["token_symbol"]
    chain = segnale["chain"].upper()
    log.info("\n" + sep)
    log.info(f"  🟢 SEGNALE PRE-PUMP — {sym} ({chain})")
    log.info(f"  Indirizzo  : {segnale['token_address']}")
    log.info(f"  Prezzo     : ${segnale['price_usd']:.8f}")
    log.info(f"  Volume 1h  : ${segnale['volume_1h_usd']:,.0f}")
    log.info(f"  Liquidità  : ${segnale['liquidity_usd']:,.0f}")
    log.info(f"  Buy/Sell   : {segnale['buy_sell_ratio_1h']:.2f}")
    log.info(f"  Var. 1h    : {segnale['change_1h_pct']:+.2f}%")
    log.info(f"  P(pump)    : {segnale['pump_probability']:.1%}")
    log.info(f"  ── Pre-Pump Signals ─────────────────────────────────")
    log.info(f"  VolAccel5m : {segnale.get('vol_accel_5m_vs_1h', 0):.2f}x (>1.3 = accumulo attivo)")
    log.info(f"  PV Diverg. : {segnale.get('price_vol_divergence', 0):.3f} (alto = prezzo fermo + vol esplode)")
    log.info(f"  BuyPressure: {segnale.get('buy_pressure_score', 0):.2f}")
    log.info(f"  Momentum✓  : {'✅ TF concordi' if segnale.get('momentum_consistency') else '⚠️  TF discordanti'}")
    composite = segnale.get('prepump_composite_score', 0)
    bar_len = int(composite * 20)
    bar = "█" * bar_len + "░" * (20 - bar_len)
    log.info(f"  COMPOSITE  : [{bar}] {composite:.3f}/1.000")
    dump_risk = segnale.get('dump_risk_score', 0)
    dump_icon = "✅ Basso" if dump_risk < 0.3 else ("⚠️  Medio" if dump_risk < 0.6 else "🔴 ALTO")
    log.info(f"  DumpRisk   : {dump_risk:.3f} — {dump_icon}")
    bsr_trend   = segnale.get('bsr_trend_per_min', 0)
    bsr_trend_n = segnale.get('bsr_trend_samples', 0)
    bsr_trend_icon = "📉 in calo" if bsr_trend < -0.01 else ("📈 in salita" if bsr_trend > 0.01 else "➡️  stabile")
    log.info(f"  BSR trend  : {bsr_trend:+.4f}/min su {bsr_trend_n} letture — {bsr_trend_icon} (sperimentale, in raccolta dati)")
    bsr_shift = segnale.get('bsr_recent_shift', 0)
    shift_icon = "📉 venditori in aumento ORA" if bsr_shift < -0.15 else ("📈 compratori in aumento ORA" if bsr_shift > 0.15 else "➡️  coerente con la media oraria")
    log.info(f"  BSR shift  : 5m={segnale.get('bsr_5m', 0):.2f} vs 1h={segnale.get('buy_sell_ratio_1h', 0):.2f} ({bsr_shift:+.3f}) — {shift_icon} (sperimentale)")
    log.info(f"  ── Sicurezza ────────────────────────────────────────")
    log.info(f"  Tax (B/S)  : {segnale['buy_tax']:.1f}% / {segnale['sell_tax']:.1f}%")
    log.info(f"  LP locked  : {'🔒 Sì' if segnale['lp_locked'] else '❌ No'}")
    log.info(f"  Top feat.  : {segnale['top_features']}")
    log.info(f"  ⚠️  {segnale['disclaimer']}")
    log.info(sep)

    # ── Registra nel tracker (aggiorna CSV + HTML + avvia monitoraggio prezzi 4h) ──
    if TRACKER_AVAILABLE:
        try:
            tracker = get_tracker()
            riga_tracker = {
                "token_symbol":       segnale.get("token_symbol", ""),
                "token_name":         segnale.get("token_name", ""),
                "token_address":      segnale.get("token_address", ""),
                "chain":              segnale.get("chain", ""),
                "pair_address":       segnale.get("pair_address", ""),
                "price_usd":          segnale.get("price_usd", 0),
                "volume_1h_usd":      segnale.get("volume_1h_usd", 0),
                "liquidity_usd":      segnale.get("liquidity_usd", 0),
                "buy_sell_ratio_1h":  segnale.get("buy_sell_ratio_1h", 0),
                "change_1h_pct":      segnale.get("change_1h_pct", 0),
                "pump_probability":   segnale.get("pump_probability", 0),
                "buy_tax":            segnale.get("buy_tax", 0),
                "sell_tax":           segnale.get("sell_tax", 0),
                "lp_locked":          1 if segnale.get("lp_locked") else 0,
                "is_honeypot":        1 if segnale.get("is_honeypot") else 0,
                "top_features":       segnale.get("top_features", ""),
            }
            tracker.registra_segnale(riga_tracker)
            log.info(f"[tracker] ✅ Segnale {segnale.get('token_symbol')} registrato nel tracker.")
        except Exception as e:
            log.warning(f"[tracker] ⚠️  Impossibile registrare il segnale: {e}")

    # Imposta cooldown per questo token (evita segnali ripetuti in dump)
    pair_addr = segnale.get("pair_address", "") or segnale.get("token_address", "")
    if pair_addr:
        _set_token_cooldown(pair_addr)

    # Invia email
    invia_segnale_email(segnale)


# ==============================================================================
# MAIN LOOP
# ==============================================================================

def main():
    log.info("=" * 65)
    log.info("  crypto_signal_bot — Pre-Pump Hunter (ottimizzato anti-dump)")
    log.info("=" * 65)

    if GEM_WATCHLIST_AVAILABLE:
        log.info("[main] " + watchlist_summary())
    else:
        log.info("[main] gem_watchlist non disponibile — solo DexScreener.")

    # Carica blacklist dinamica dai followup precedenti
    _check_followup_blacklist()

    # Carica storico BSR persistito (sopravvive ai restart)
    _load_bsr_history()

    # Thread fast-poll: ripolla i near-miss ogni 30s (vedi FAST-POLL WATCHLIST)
    threading.Thread(target=fastpoll_loop, name="defi_fastpoll", daemon=True).start()

    # ML rimosso: pump_probability non era usato nel filtro (modello su dati sintetici)
    model, scaler = None, None

    chains   = list(CHAINS.keys())
    interval = CONFIG.get("LOOP_INTERVAL_SEC", 180)
    _ciclo   = 0

    # Stats log per diagnosticare il calo segnali (funnel API→filtri→segnali per ciclo)
    _cycle_stats_file = _REPORTS_DIR / "cycle_stats.csv"
    _cycle_stats_header = not _cycle_stats_file.exists()

    while True:
        _ciclo += 1
        log.info("=" * 60)
        log.info(f"  CICLO #{_ciclo} — {datetime.now().strftime('%H:%M:%S')}")
        log.info("=" * 60)

        if _ciclo % CONFIG.get("WATCHLIST_RELOAD_EVERY_N_CYCLES", 10) == 1:
            if GEM_WATCHLIST_AVAILABLE:
                log.info("[main] Watchlist: " + watchlist_summary())
            # Aggiorna blacklist dinamica dai followup (token ruggati)
            _check_followup_blacklist()

        for chain in chains:
            try:
                df_raw = fetch_onchain_and_market_data(chain)

                n_raw = len(df_raw) if not df_raw.empty else 0
                if df_raw.empty:
                    log.warning(f"[main] Nessun pair trovato per {chain}.")
                    # Scrivi comunque riga zero per vedere i buchi
                    with _cycle_stats_file.open("a", newline="", encoding="utf-8") as fh:
                        w = csv.DictWriter(fh, fieldnames=["ts","chain","n_raw","n_hard_pass","n_signals","bsr_med","vol_med","chg_med"])
                        if _cycle_stats_header:
                            w.writeheader(); _cycle_stats_header = False
                        w.writerow({"ts": datetime.now().isoformat(), "chain": chain,
                                    "n_raw": 0, "n_hard_pass": 0, "n_signals": 0,
                                    "bsr_med": 0, "vol_med": 0, "chg_med": 0})
                    continue

                signals = generate_signals(df_raw, model, scaler)
                for sig in signals:
                    stampa_segnale(sig)

                # n_hard_pass: applica i filtri hard allo stesso df per contare quanti passano
                # (df_eng ha buy_sell_ratio_1h, df_raw NO — serve anche sotto per bsr_history/bsr_med)
                try:
                    _df_eng = engineer_features(df_raw.copy())
                    _df_pass = apply_hard_filters(_df_eng)
                    n_hard_pass = len(_df_pass)
                except Exception:
                    _df_eng = None
                    n_hard_pass = -1

                # Aggiorna lo storico BSR DOPO aver generato i segnali (il trend usato
                # nei segnali di questo ciclo riflette solo i cicli precedenti — leading).
                # 12/06 fix: df_raw non ha mai buy_sell_ratio_1h (calcolato solo in
                # engineer_features su copia) → _update_bsr_history era no-op da sempre.
                if _df_eng is not None:
                    _update_bsr_history(_df_eng)

                # Calcola metriche di funnel per il cycle_stats log
                bsr_med = float(_df_eng["buy_sell_ratio_1h"].median()) if _df_eng is not None and "buy_sell_ratio_1h" in _df_eng.columns else 0
                vol_med = float(df_raw["volume_1h_usd"].median())     if "volume_1h_usd" in df_raw.columns else 0
                chg_med = float(df_raw["change_1h_pct"].median())     if "change_1h_pct" in df_raw.columns else 0

                with _cycle_stats_file.open("a", newline="", encoding="utf-8") as fh:
                    w = csv.DictWriter(fh, fieldnames=["ts","chain","n_raw","n_hard_pass","n_signals","bsr_med","vol_med","chg_med"])
                    if _cycle_stats_header:
                        w.writeheader(); _cycle_stats_header = False
                    w.writerow({
                        "ts": datetime.now().isoformat(), "chain": chain,
                        "n_raw": n_raw, "n_hard_pass": n_hard_pass, "n_signals": len(signals),
                        "bsr_med": round(bsr_med, 3), "vol_med": round(vol_med, 0), "chg_med": round(chg_med, 2),
                    })
                log.info(f"[stats] {chain}: raw={n_raw} → hard_pass={n_hard_pass} → segnali={len(signals)} | bsr_med={bsr_med:.2f} vol_med={vol_med:.0f} chg_med={chg_med:+.1f}%")

            except Exception as e:
                import traceback
                log.error(f"[main] Errore su {chain}: {e}")
                traceback.print_exc()

        log.info(f"[main] Attesa {interval}s prima del prossimo ciclo...")
        time.sleep(interval)


if __name__ == "__main__":
    main()
