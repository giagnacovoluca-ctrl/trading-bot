"""
subscriptions.py — store abbonati con tier e scadenza.
Persistenza JSON atomica (store.py). Struttura:
  { "<chat_id>": {tier, expires_at(ts), referral_code, referred_by, username} }
"""
from __future__ import annotations

import secrets
import time

import config
import store

_FILE = "subscribers.json"


def _all() -> dict:
    return store.load(_FILE, {})


def _save(data: dict) -> None:
    store.save(_FILE, data)


def get(chat_id) -> dict | None:
    return _all().get(str(chat_id))


def is_active(chat_id) -> bool:
    rec = get(chat_id)
    return bool(rec and rec.get("expires_at", 0) > time.time())


def tier_of(chat_id) -> str:
    rec = get(chat_id)
    if rec and rec.get("expires_at", 0) > time.time():
        return rec.get("tier", config.TIER_FREE)
    return config.TIER_FREE


def _gen_referral() -> str:
    return "REF-" + secrets.token_hex(3).upper()


def ensure_user(chat_id, username: str = "") -> dict:
    data = _all()
    cid = str(chat_id)
    if cid not in data:
        data[cid] = {
            "tier": config.TIER_FREE,
            "expires_at": 0,
            "referral_code": _gen_referral(),
            "referred_by": None,
            "username": username,
        }
        _save(data)
    elif username and data[cid].get("username") != username:
        data[cid]["username"] = username
        _save(data)
    return data[cid]


def grant(chat_id, tier: str, days: int | None = None, username: str = "") -> dict:
    """Concede/estende un abbonamento. Estende dalla scadenza residua se attiva."""
    days = days or config.SUB_DAYS
    data = _all()
    cid = str(chat_id)
    rec = data.get(cid) or ensure_user(chat_id, username)
    data = _all()  # ricarica dopo ensure_user
    rec = data[cid]
    base = max(rec.get("expires_at", 0), time.time())
    rec["expires_at"] = base + days * 86400
    rec["tier"] = tier
    if username:
        rec["username"] = username
    _save(data)
    return rec


def set_referred_by(chat_id, code: str) -> bool:
    """Collega un nuovo utente a chi l'ha invitato (una sola volta)."""
    data = _all()
    cid = str(chat_id)
    rec = data.get(cid)
    if not rec or rec.get("referred_by"):
        return False
    referrer = next((k for k, v in data.items() if v.get("referral_code") == code), None)
    if not referrer or referrer == cid:
        return False
    rec["referred_by"] = referrer
    _save(data)
    return True


def active_subscribers() -> list[tuple[str, dict]]:
    now = time.time()
    return [(k, v) for k, v in _all().items() if v.get("expires_at", 0) > now]


def expired_since(grace_sec: int = 0) -> list[tuple[str, dict]]:
    """Abbonati scaduti (oltre l'eventuale grace) ancora marcati con tier != free."""
    now = time.time()
    out = []
    for k, v in _all().items():
        exp = v.get("expires_at", 0)
        if 0 < exp <= now - grace_sec and v.get("tier") != config.TIER_FREE:
            out.append((k, v))
    return out


def downgrade_to_free(chat_id) -> None:
    data = _all()
    cid = str(chat_id)
    if cid in data:
        data[cid]["tier"] = config.TIER_FREE
        _save(data)
