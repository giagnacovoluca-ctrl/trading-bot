"""
telegram_api.py — wrapper minimale Bot API via requests.
Gestisce retry su 429 (rispetta retry_after) e errori di rete.
Nessuna dipendenza extra oltre `requests` (già nel venv).
"""
from __future__ import annotations

import logging
import threading
import time

import requests

import config

log = logging.getLogger("telegram_api")

_BASE = "https://api.telegram.org/bot{token}/{method}"
_SESSION = requests.Session()

# Throttle client-side: Telegram limita ~20 msg/min per canale. Senza questo,
# un burst di chiusure + i retry sui timeout generava tempeste di 429 da 10+
# minuti (visto 11/06 12:09-12:21).
_MIN_SEND_INTERVAL = 3.0
_send_lock = threading.Lock()
_last_send_per_chat: dict = {}


def _throttle(chat_id: str):
    with _send_lock:
        last = _last_send_per_chat.get(chat_id, 0.0)
        wait = _MIN_SEND_INTERVAL - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        _last_send_per_chat[chat_id] = time.time()


def _call(method: str, params: dict, timeout: int = 30, retries: int = 3,
          retry_read_timeout: bool = True):
    if not config.BOT_TOKEN:
        log.warning("[tg] BOT_TOKEN mancante — chiamata %s ignorata", method)
        return None
    url = _BASE.format(token=config.BOT_TOKEN, method=method)
    for attempt in range(retries + 1):
        try:
            r = _SESSION.post(url, json=params, timeout=timeout)
            if r.status_code == 429:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                log.warning("[tg] 429 su %s — attendo %ss", method, wait)
                time.sleep(wait + 1)
                continue
            data = r.json()
            if not data.get("ok"):
                log.warning("[tg] %s fallito: %s", method, data.get("description"))
                time.sleep(2 * (attempt + 1))
                continue
            return data["result"]
        except requests.exceptions.ReadTimeout as e:
            # La richiesta è arrivata a Telegram (è la risposta che si è persa):
            # per sendMessage il retry produce un DUPLICATO sul canale e consuma
            # budget rate-limit. Meglio un raro messaggio perso.
            if not retry_read_timeout:
                log.warning("[tg] read-timeout su %s — probabile consegna avvenuta, "
                            "niente retry (anti-duplicato)", method)
                return None
            log.warning("[tg] errore %s (tentativo %d): %s", method, attempt + 1, e)
            time.sleep(2 * (attempt + 1))
        except (requests.RequestException, ValueError) as e:
            log.warning("[tg] errore %s (tentativo %d): %s", method, attempt + 1, e)
            time.sleep(2 * (attempt + 1))
    return None


def send_message(chat_id: str, text: str, parse_mode: str = "HTML",
                 disable_preview: bool = True, reply_markup: dict | None = None):
    if not chat_id:
        return None
    params = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }
    if reply_markup:
        params["reply_markup"] = reply_markup
    _throttle(str(chat_id))
    return _call("sendMessage", params, retry_read_timeout=False)


def get_updates(offset: int | None = None, timeout: int = 25):
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    return _call("getUpdates", params, timeout=timeout + 10) or []


def answer_callback_query(callback_query_id: str, text: str | None = None):
    params = {"callback_query_id": callback_query_id}
    if text:
        params["text"] = text
    return _call("answerCallbackQuery", params)


def create_invite_link(chat_id: str, member_limit: int = 1, expire_seconds: int = 86400):
    params = {"chat_id": chat_id, "member_limit": member_limit,
              "expire_date": int(time.time()) + expire_seconds}
    res = _call("createChatInviteLink", params)
    return res.get("invite_link") if res else None


def ban_member(chat_id: str, user_id: str):
    return _call("banChatMember", {"chat_id": chat_id, "user_id": user_id})


def unban_member(chat_id: str, user_id: str):
    return _call("unbanChatMember", {"chat_id": chat_id, "user_id": user_id,
                                     "only_if_banned": True})
