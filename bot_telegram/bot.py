"""
bot.py — bot comandi Telegram (long-polling via requests).
Comandi utente: /start /plans /subscribe <tier> /status /referral [code]
Comandi admin (solo ADMIN_CHAT_ID): /grant <chat_id> <tier> <days> · /stats · /broadcast
Gating: alla concessione di un abbonamento invia invite-link monouso al canale.
"""
from __future__ import annotations

import logging
import time

import config
import store
import subscriptions as subs
import telegram_api as tg

log = logging.getLogger("bot")

_UPDATE_OFFSET_FILE = "update_offset.json"


def _is_admin(chat_id) -> bool:
    return config.ADMIN_CHAT_ID and str(chat_id) == str(config.ADMIN_CHAT_ID)


def _welcome_text() -> str:
    return (
        "🤖 <b>Welcome to the signal bot</b>\n\n"
        "Automated multi-chain scanner (Solana · Base) running 24/7.\n"
        "Free channel: closed winning trades. Premium: every signal in real time "
        "with entry, TP and SL.\n\n"
        "Use the menu below 👇"
    )


def _main_keyboard() -> dict:
    return {"inline_keyboard": [
        [{"text": f"💎 Premium — ${config.PRICE_PREMIUM_USD:.0f}/mo", "callback_data": "menu:plans"}],
        [{"text": "📊 Track record", "callback_data": "menu:stats"},
         {"text": "👤 My status",    "callback_data": "menu:status"}],
    ]}


def _back_keyboard(target: str = "menu:home") -> dict:
    return {"inline_keyboard": [
        [{"text": f"💎 Premium — ${config.PRICE_PREMIUM_USD:.0f}/mo", "callback_data": "menu:plans"}],
        [{"text": "⬅️ Back", "callback_data": target}],
    ]}


def _plans_text() -> str:
    return (
        "<b>📈 Access Plans</b>\n\n"
        "🔓 <b>Free</b> — closed winning trades published publicly (results, % and profit)\n"
        f"💎 <b>Premium</b> — ${config.PRICE_PREMIUM_USD:.0f}/mo — real-time entry signals with "
        "price, liquidity, BSR and charts — private channel\n\n"
        "Pay in USDC — access is activated <b>automatically</b> upon on-chain confirmation.\n"
        "Choose your network 👇"
    )


def _plans_keyboard() -> dict:
    rows = []
    if config.PAY_WALLET_SOL:
        rows.append([{"text": "💳 Pay USDC · Solana", "callback_data": "pay:premium:solana"}])
    if config.PAY_WALLET_EVM:
        rows.append([{"text": "💳 Pay USDC · Base", "callback_data": "pay:premium:base"}])
    if not rows:
        rows.append([{"text": "✉️ Contact admin", "url": f"https://t.me/{config.BOT_USERNAME}"}])
    rows.append([{"text": "📊 Track record", "callback_data": "menu:stats"},
                 {"text": "⬅️ Back", "callback_data": "menu:home"}])
    return {"inline_keyboard": rows}


def _invoice_view(chat_id, tier: str, chain: str) -> tuple[str, dict] | None:
    """Crea l'invoice a importo univoco e il messaggio di pagamento."""
    import payments
    inv = payments.create_invoice(chat_id, tier, chain)
    if not inv:
        return None
    wallet = config.PAY_WALLET_SOL if chain == "solana" else config.PAY_WALLET_EVM
    net = "Solana" if chain == "solana" else "Base"
    text = (
        f"<b>💳 {tier.upper()} — payment via USDC on {net}</b>\n\n"
        f"Send <b>exactly</b> this amount:\n"
        f"💵 <code>{inv['amount']:.2f}</code> USDC\n\n"
        f"to this address:\n"
        f"📬 <code>{wallet}</code>\n\n"
        "⚠️ The unique cents identify YOUR payment — no memo needed.\n"
        "✅ Activation is automatic within ~2 minutes of on-chain confirmation: "
        "you'll receive the private channel invite here."
    )
    kb = {"inline_keyboard": [
        [{"text": "✅ I've paid — check status", "callback_data": f"chk:{inv['ref']}"}],
        [{"text": "⬅️ Back", "callback_data": "menu:plans"}],
    ]}
    return text, kb


def _stats_text() -> str:
    """Track record pubblico — gli stessi numeri della landing, dentro il bot."""
    try:
        import track_record
        return track_record.recap_text()
    except Exception as e:
        log.warning("[bot] stats non disponibili: %s", e)
        return "📊 Track record is being updated — try again in a minute."


def _status_text(chat_id) -> str:
    rec = subs.get(chat_id)
    if not rec or rec.get("expires_at", 0) <= time.time():
        return "Status: <b>Free</b>. Use /plans for real-time access."
    days = (rec["expires_at"] - time.time()) / 86400
    return (f"Status: <b>{rec['tier'].upper()}</b> · expires in {days:.1f} days\n"
            f"Referral code: <code>{rec.get('referral_code','—')}</code>")


def deliver_access(chat_id, tier: str):
    """Sends Premium channel invite link after activation."""
    chan = config.PREMIUM_CHANNEL_ID
    link = tg.create_invite_link(chan) if chan else None
    if link:
        tg.send_message(chat_id, f"✅ <b>{tier.upper()}</b> access activated!\n"
                                 f"Join here (one-time link): {link}")
    else:
        tg.send_message(chat_id, f"✅ <b>{tier.upper()}</b> access activated. "
                                 "The admin will add you to the channel shortly.")


def _edit_or_send(chat_id, message_id, text: str, kb: dict | None):
    """Naviga modificando il messaggio esistente; fallback a nuovo messaggio."""
    if not (message_id and tg.edit_message_text(chat_id, message_id, text, reply_markup=kb)):
        tg.send_message(chat_id, text, reply_markup=kb)


def _handle_callback(cq: dict):
    """Router dei bottoni inline: menu:* navigazione, pay:* invoice, chk:* stato."""
    chat_id = cq.get("from", {}).get("id")
    msg = cq.get("message") or {}
    mid = msg.get("message_id")
    data = cq.get("data") or ""
    username = cq.get("from", {}).get("username", "")
    subs.ensure_user(chat_id, username)

    if data == "menu:home":
        tg.answer_callback_query(cq.get("id"))
        _edit_or_send(chat_id, mid, _welcome_text(), _main_keyboard())
    elif data in ("menu:plans", "sub:premium"):   # sub:premium = bottoni legacy
        tg.answer_callback_query(cq.get("id"))
        _edit_or_send(chat_id, mid, _plans_text(), _plans_keyboard())
    elif data == "menu:stats":
        tg.answer_callback_query(cq.get("id"))
        _edit_or_send(chat_id, mid, _stats_text(), _back_keyboard())
    elif data == "menu:status":
        tg.answer_callback_query(cq.get("id"))
        _edit_or_send(chat_id, mid, _status_text(chat_id), _back_keyboard())
    elif data.startswith("pay:"):
        _, tier, chain = (data.split(":") + ["", ""])[:3]
        view = _invoice_view(chat_id, tier, chain) if chain in ("solana", "base") else None
        if view:
            tg.answer_callback_query(cq.get("id"))
            _edit_or_send(chat_id, mid, view[0], view[1])
        else:
            tg.answer_callback_query(cq.get("id"), "Payment unavailable — contact admin.")
    elif data.startswith("chk:"):
        import payments
        inv = payments.get_invoice(data[4:])
        if inv and inv.get("status") == "paid":
            tg.answer_callback_query(cq.get("id"), "✅ Payment confirmed — access active!")
        elif inv:
            tg.answer_callback_query(
                cq.get("id"),
                "⏳ Not seen on-chain yet. Confirmation can take a couple of minutes "
                "after you send — I'll message you automatically.")
        else:
            tg.answer_callback_query(cq.get("id"), "Invoice not found — use /plans to retry.")
    else:
        tg.answer_callback_query(cq.get("id"))


def _handle(update: dict):
    cq = update.get("callback_query")
    if cq:
        _handle_callback(cq)
        return
    msg = update.get("message") or update.get("channel_post")
    if not msg:
        return
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    username = msg.get("from", {}).get("username", "")
    if not text.startswith("/"):
        return
    subs.ensure_user(chat_id, username)
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]

    if cmd == "/start":
        payload = args[0].lower() if args else ""
        if payload in config.TIER_PRICES:
            tg.send_message(chat_id, _plans_text(), reply_markup=_plans_keyboard())
        elif payload.startswith("ref_") or payload.startswith("ref-"):
            ok = subs.set_referred_by(chat_id, payload[4:].upper())
            tg.send_message(chat_id, ("✅ Referral registered.\n\n" if ok else "")
                            + _welcome_text(), reply_markup=_main_keyboard())
        else:
            tg.send_message(chat_id, _welcome_text(), reply_markup=_main_keyboard())
    elif cmd == "/plans":
        tg.send_message(chat_id, _plans_text(), reply_markup=_plans_keyboard())
    elif cmd == "/subscribe":
        tg.send_message(chat_id, _plans_text(), reply_markup=_plans_keyboard())
    elif cmd == "/status":
        tg.send_message(chat_id, _status_text(chat_id), reply_markup=_back_keyboard())
    elif cmd == "/referral":
        if args:
            ok = subs.set_referred_by(chat_id, args[0].upper())
            tg.send_message(chat_id, "✅ Referral registered." if ok
                            else "❌ Invalid code or already set.")
        else:
            rec = subs.get(chat_id)
            tg.send_message(chat_id, f"Your referral code: <code>{rec.get('referral_code')}</code>\n"
                                     "Share it — anyone who subscribes extends your access.")
    # ── admin ──
    elif cmd == "/grant" and _is_admin(chat_id):
        if len(args) >= 2:
            target, tier = args[0], args[1].lower()
            days = int(args[2]) if len(args) > 2 else config.SUB_DAYS
            subs.grant(target, tier, days)
            deliver_access(target, tier)
            tg.send_message(chat_id, f"✅ {tier} granted to {target} for {days}d.")
        else:
            tg.send_message(chat_id, "Usage: /grant <chat_id> <premium> [days]")
    elif cmd == "/stats" and _is_admin(chat_id):
        active = subs.active_subscribers()
        tg.send_message(chat_id, f"Active subscribers: <b>{len(active)}</b>")
    elif cmd == "/broadcast" and _is_admin(chat_id):
        body = text[len("/broadcast"):].strip()
        n = 0
        for cid, _ in subs.active_subscribers():
            if tg.send_message(cid, body):
                n += 1
        tg.send_message(chat_id, f"Sent to {n} subscribers.")


def run(stop_event=None):
    log.info("[bot] command bot avviato (long-polling)")
    offset = store.load(_UPDATE_OFFSET_FILE, {}).get("offset")
    while stop_event is None or not stop_event.is_set():
        try:
            updates = tg.get_updates(offset=offset, timeout=25)
            for up in updates:
                offset = up["update_id"] + 1
                try:
                    _handle(up)
                except Exception as e:
                    log.exception("[bot] errore handling update: %s", e)
            if updates:
                store.save(_UPDATE_OFFSET_FILE, {"offset": offset})
        except Exception as e:
            log.exception("[bot] errore polling: %s", e)
            if stop_event:
                stop_event.wait(5)
            else:
                time.sleep(5)


def main(stop_event=None):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                        datefmt="%H:%M:%S")
    run(stop_event)


if __name__ == "__main__":
    main()
