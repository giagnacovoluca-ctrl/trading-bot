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
}

# ── Publisher ───────────────────────────────────────────────────────────────────
FREE_DELAY_MIN        = _envf("FREE_DELAY_MIN", 15.0)
FREE_MIN_PROBABILITY  = _envf("FREE_MIN_PROBABILITY", 0.65)
PREMIUM_MIN_PROBABILITY = _envf("PREMIUM_MIN_PROBABILITY", 0.0)
POLL_INTERVAL_SEC     = _envf("POLL_INTERVAL_SEC", 5.0)

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
