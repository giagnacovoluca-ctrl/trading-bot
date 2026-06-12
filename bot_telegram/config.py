"""
config.py — configurazione centralizzata bot_telegram.
Carica executor/.env (per riusare RPC Helius/Alchemy) e poi bot_telegram/.env
(che ha la precedenza). Nessun segreto hardcoded.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent          # bot_telegram/
ROOT_DIR = BASE_DIR.parent                           # GIT/
STATE_DIR = BASE_DIR / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

# --- carica env: prima executor (RPC condivisi), poi quello locale (override) ---
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT_DIR / "executor" / ".env", override=False)
    load_dotenv(BASE_DIR / ".env", override=True)
except ImportError:
    pass


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _envf(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN          = _env("TELEGRAM_BOT_TOKEN")
FREE_CHANNEL_ID    = _env("TELEGRAM_FREE_CHANNEL_ID")
PREMIUM_CHANNEL_ID = _env("TELEGRAM_PREMIUM_CHANNEL_ID")
ADMIN_CHAT_ID      = _env("TELEGRAM_ADMIN_CHAT_ID")
BOT_USERNAME              = _env("TELEGRAM_BOT_USERNAME")          # es. MySignalBot
FREE_CHANNEL_USERNAME     = _env("TELEGRAM_FREE_CHANNEL_USERNAME") # es. mysignals_free
PREMIUM_CHANNEL_USERNAME  = _env("TELEGRAM_PREMIUM_CHANNEL_USERNAME")

# ── Sorgenti dati (read-only) ───────────────────────────────────────────────────
SIGNALS_DIR = Path(_env("SIGNALS_DIR", str(ROOT_DIR / "defi" / "reports")))
TRADES_CSV  = Path(_env("TRADES_CSV", str(SIGNALS_DIR / "live_trades.csv")))

# Mappa file CSV → nome sistema (per routing tier e label messaggio)
SIGNAL_FILES = {
    "signals_log.csv":        "defi",
    "pump_grad_signals.csv":  "pump_grad",
    "pre_grad_signals.csv":   "pre_grad",
    "mirror_signals.csv":     "mirror",
    "midcap_signals.csv":     "midcap",
    "base_pump_signals.csv":  "base_pump",
}

# Sorgenti fuori da SIGNALS_DIR (path assoluti). gems_log_v3.csv è il CSV che
# gemmeV3/gem_tracker scrive per OGNI gemma segnalata: senza questo il canale
# Premium riceveva solo il lifecycle TP/SL ma mai le call v3.
GEMME_REPORTS_DIR = ROOT_DIR / "gemme" / "reports"
SIGNAL_SOURCES = [
    *((SIGNALS_DIR / fname, system) for fname, system in SIGNAL_FILES.items()),
    (GEMME_REPORTS_DIR / "gems_log_v3.csv", "v3"),
]

# Eventi wallet alpha (wallet_events.csv dal wallet_mirror_bot) → alert whale
# su PREMIUM. Pubblicati: buy ≥ WHALE_ALERT_MIN_USD, confluenza ≥2 wallet,
# risveglio post-inattività, sell su token segnalato di recente.
WALLET_EVENTS_CSV   = SIGNALS_DIR / "wallet_events.csv"
WHALE_ALERT_MIN_USD = _envf("WHALE_ALERT_MIN_USD", 500.0)

# ── Publisher ───────────────────────────────────────────────────────────────────
FREE_DELAY_MIN        = _envf("FREE_DELAY_MIN", 15.0)
FREE_MIN_PROBABILITY  = _envf("FREE_MIN_PROBABILITY", 0.65)
PREMIUM_MIN_PROBABILITY = _envf("PREMIUM_MIN_PROBABILITY", 0.0)
POLL_INTERVAL_SEC     = _envf("POLL_INTERVAL_SEC", 5.0)

# Teaser live su FREE: segnale appena inviato ai Premium, ticker censurato.
# Rate-limited per non trasformare il FREE in spam (e non svalutare il Premium).
FREE_TEASER_ENABLED          = _env("FREE_TEASER_ENABLED", "true").lower() != "false"
FREE_TEASER_MIN_INTERVAL_MIN = _envf("FREE_TEASER_MIN_INTERVAL_MIN", 45.0)
FREE_TEASER_MAX_PER_DAY      = int(_envf("FREE_TEASER_MAX_PER_DAY", 6))

# ── Promo X (Twitter) ────────────────────────────────────────────────────────────
# L'API X richiede un piano a pagamento per postare: niente posting automatico.
# Per ogni trade vincente sopra soglia, il bot manda all'admin (Telegram) una
# card immagine + testo pronto (con hashtag/$TICKER) da copiare e postare a mano.
X_PROMO_ENABLED        = _env("X_PROMO_ENABLED", "false").lower() == "true"
X_PROMO_MIN_PNL_EUR    = _envf("X_PROMO_MIN_PNL_EUR", 15.0)   # ignora micro-win
X_PROMO_MIN_PNL_PCT    = _envf("X_PROMO_MIN_PNL_PCT", 8.0)    # ignora % marginali
X_PROMO_MIN_INTERVAL_MIN = _envf("X_PROMO_MIN_INTERVAL_MIN", 120.0)
X_PROMO_MAX_PER_DAY    = int(_envf("X_PROMO_MAX_PER_DAY", 4))

# ── Monetizzazione ──────────────────────────────────────────────────────────────
PRICE_PREMIUM_USD = _envf("PRICE_PREMIUM_USD", 49.0)
SUB_DAYS          = int(_envf("SUB_DAYS", 30))
PAY_WALLET_SOL    = _env("PAY_WALLET_SOL")
PAY_WALLET_EVM    = _env("PAY_WALLET_EVM")

# RPC riusati da executor/.env
SOLANA_RPC_URL = _env("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
HELIUS_API_KEY = _env("HELIUS_API_KEY")
BASE_RPC_URL   = _env("BASE_RPC_URL", "https://mainnet.base.org")

TIER_PREMIUM = "premium"
TIER_FREE    = "free"

TIER_PRICES = {TIER_PREMIUM: PRICE_PREMIUM_USD}


def channel_for_system(system: str) -> str:
    """Canale di destinazione per un segnale 'full' (tutti i sistemi → Premium)."""
    return PREMIUM_CHANNEL_ID


def explorer_link(chain: str, token_address: str) -> str:
    chain = (chain or "").lower()
    if chain == "solana":
        return f"https://solscan.io/token/{token_address}"
    if chain == "base":
        return f"https://basescan.org/token/{token_address}"
    if chain in ("bsc", "binance-smart-chain"):
        return f"https://bscscan.com/token/{token_address}"
    if chain in ("eth", "ethereum"):
        return f"https://etherscan.io/token/{token_address}"
    return ""


def dexscreener_link(chain: str, pair_address: str, token_address: str) -> str:
    chain = (chain or "").lower()
    cmap = {"solana": "solana", "base": "base", "bsc": "bsc",
            "binance-smart-chain": "bsc", "eth": "ethereum", "ethereum": "ethereum"}
    c = cmap.get(chain, chain)
    ref = pair_address or token_address
    return f"https://dexscreener.com/{c}/{ref}" if ref else ""


def is_configured() -> bool:
    return bool(BOT_TOKEN)
