"""
tg_alert.py — invio alert Telegram diretto (bypass email digest).
Usato da liq_monitor e cex_watcher per notifiche time-sensitive.
Carica TELEGRAM_BOT_TOKEN e TELEGRAM_ADMIN_CHAT_ID da bot_telegram/.env.
"""
import logging
import os
from pathlib import Path

import requests

log = logging.getLogger("tg_alert")

_ROOT    = Path(__file__).parent.parent
_BOT_ENV = _ROOT / "bot_telegram" / ".env"

_token:   str = ""
_chat_id: str = ""
_loaded   = False


def _load():
    global _token, _chat_id, _loaded
    if _loaded:
        return
    _loaded = True
    # Prima prova variabili d'ambiente già impostate
    _token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    _chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
    # Fallback: legge bot_telegram/.env manualmente (run.py carica solo executor/.env)
    if (not _token or not _chat_id) and _BOT_ENV.exists():
        for line in _BOT_ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN=") and not _token:
                _token = line.split("=", 1)[1].strip()
            elif line.startswith("TELEGRAM_ADMIN_CHAT_ID=") and not _chat_id:
                _chat_id = line.split("=", 1)[1].strip()


def send(text: str, parse_mode: str = "HTML") -> bool:
    """Invia messaggio all'admin. Ritorna True se ok."""
    _load()
    if not _token or not _chat_id:
        log.debug("[tg_alert] token/chat_id mancanti — skip")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{_token}/sendMessage",
            json={"chat_id": _chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        if r.status_code == 429:
            log.warning("[tg_alert] rate limit 429")
            return False
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning(f"[tg_alert] send error: {e}")
        return False
