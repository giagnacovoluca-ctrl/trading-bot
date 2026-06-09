"""
telegram_api.py — wrapper minimale Bot API via requests.
Gestisce retry su 429 (rispetta retry_after) e errori di rete.
Nessuna dipendenza extra oltre `requests` (già nel venv).
"""
from __future__ import annotations

import logging
import time

import requests

import config

log = logging.getLogger("telegram_api")

_BASE = "https://api.telegram.org/bot{token}/{method}"
_SESSION = requests.Session()


def _call(method: str, params: dict, timeout: int = 30, retries: int = 3):
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
                return None
            return data["result"]
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
    return _call("sendMessage", params)


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
