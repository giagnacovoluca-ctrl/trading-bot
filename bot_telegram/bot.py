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


def _plans_text() -> str:
    return (
        "<b>📈 Access Plans</b>\n\n"
        f"🔓 <b>Free</b> — closed winning trades published publicly (results, % and profit)\n"
        f"💎 <b>Premium</b> — ${config.PRICE_PREMIUM_USD:.0f}/mo — real-time entry signals with "
        "price, liquidity, BSR and charts — private channel\n\n"
        "Tap the button below to subscribe 👇"
    )


def _plans_keyboard() -> dict:
    return {"inline_keyboard": [
        [{"text": f"💎 Premium — ${config.PRICE_PREMIUM_USD:.0f}/mo", "callback_data": "sub:premium"}],
    ]}


def _subscribe_text(tier: str) -> str:
    price = config.TIER_PRICES.get(tier)
    if price is None:
        return "Invalid tier. Use: /subscribe premium"
    lines = [
        f"<b>{tier.upper()} Subscription — ${price:.0f}/mo</b>",
        "",
        "Pay in crypto to one of these addresses and include your Telegram ID in the memo/note:",
    ]
    if config.PAY_WALLET_SOL:
        lines.append(f"• <b>SOL/USDC (Solana)</b>: <code>{config.PAY_WALLET_SOL}</code>")
    if config.PAY_WALLET_EVM:
        lines.append(f"• <b>ETH/USDC (Base)</b>: <code>{config.PAY_WALLET_EVM}</code>")
    if not (config.PAY_WALLET_SOL or config.PAY_WALLET_EVM):
        lines.append("<i>(payment wallets not yet configured — contact admin)</i>")
    lines += [
        "",
        "Your access is activated automatically upon on-chain confirmation.",
        "Alternatively, send a payment screenshot to the admin.",
    ]
    return "\n".join(lines)


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


def _handle_callback(cq: dict):
    """Tap su un bottone inline (es. /plans → 'Premium')."""
    chat_id = cq.get("from", {}).get("id")
    data = cq.get("data") or ""
    tg.answer_callback_query(cq.get("id"))
    if data.startswith("sub:"):
        tier = data.split(":", 1)[1]
        username = cq.get("from", {}).get("username", "")
        subs.ensure_user(chat_id, username)
        tg.send_message(chat_id, _subscribe_text(tier))


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
            tg.send_message(chat_id, "👋 Welcome to the signal bot.\n\n" + _subscribe_text(payload))
        elif payload.startswith("ref_") or payload.startswith("ref-"):
            ok = subs.set_referred_by(chat_id, payload[4:].upper())
            tg.send_message(chat_id, "👋 Welcome to the signal bot.\n"
                            + ("✅ Referral registered.\n" if ok else "")
                            + _plans_text(), reply_markup=_plans_keyboard())
        else:
            tg.send_message(chat_id, "👋 Welcome to the signal bot.\n" + _plans_text(),
                            reply_markup=_plans_keyboard())
    elif cmd == "/plans":
        tg.send_message(chat_id, _plans_text(), reply_markup=_plans_keyboard())
    elif cmd == "/subscribe":
        tier = args[0].lower() if args else ""
        if tier in config.TIER_PRICES:
            tg.send_message(chat_id, _subscribe_text(tier))
        else:
            tg.send_message(chat_id, "Choose a plan:", reply_markup=_plans_keyboard())
    elif cmd == "/status":
        tg.send_message(chat_id, _status_text(chat_id))
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
