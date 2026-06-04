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
        "<b>📈 Piani di accesso</b>\n\n"
        f"🔓 <b>Free</b> — segnali in ritardo (15m), senza entry price\n"
        f"💎 <b>Premium</b> — ${config.PRICE_PREMIUM_USD:.0f}/mese — tutti i segnali real-time, "
        "entry/liq/BSR, canale privato\n"
        f"👑 <b>VIP</b> — ${config.PRICE_VIP_USD:.0f}/mese — Premium + pre-graduation & wallet-mirror alpha\n\n"
        "Per abbonarti: /subscribe premium  oppure  /subscribe vip"
    )


def _subscribe_text(tier: str) -> str:
    price = config.TIER_PRICES.get(tier)
    if price is None:
        return "Tier non valido. Usa: /subscribe premium  o  /subscribe vip"
    lines = [
        f"<b>Abbonamento {tier.upper()} — ${price:.0f}/mese</b>",
        "",
        "Paga in crypto a uno di questi indirizzi e includi il tuo ID nel memo/nota:",
    ]
    if config.PAY_WALLET_SOL:
        lines.append(f"• <b>SOL/USDC (Solana)</b>: <code>{config.PAY_WALLET_SOL}</code>")
    if config.PAY_WALLET_EVM:
        lines.append(f"• <b>ETH/USDC (Base)</b>: <code>{config.PAY_WALLET_EVM}</code>")
    if not (config.PAY_WALLET_SOL or config.PAY_WALLET_EVM):
        lines.append("<i>(wallet di pagamento non ancora configurati — contatta l'admin)</i>")
    lines += [
        "",
        "Il tuo accesso viene attivato automaticamente alla conferma on-chain.",
        "In alternativa, dopo il pagamento invia lo screenshot all'admin.",
    ]
    return "\n".join(lines)


def _status_text(chat_id) -> str:
    rec = subs.get(chat_id)
    if not rec or rec.get("expires_at", 0) <= time.time():
        return "Stato: <b>Free</b>. Usa /plans per l'accesso real-time."
    days = (rec["expires_at"] - time.time()) / 86400
    return (f"Stato: <b>{rec['tier'].upper()}</b> · scade tra {days:.1f} giorni\n"
            f"Codice referral: <code>{rec.get('referral_code','—')}</code>")


def deliver_access(chat_id, tier: str):
    """Invia l'invite-link del canale corretto dopo l'attivazione."""
    chan = config.VIP_CHANNEL_ID if (tier == config.TIER_VIP and config.VIP_CHANNEL_ID) \
        else config.PREMIUM_CHANNEL_ID
    link = tg.create_invite_link(chan) if chan else None
    if link:
        tg.send_message(chat_id, f"✅ Accesso <b>{tier.upper()}</b> attivato!\n"
                                 f"Entra qui (link monouso): {link}")
    else:
        tg.send_message(chat_id, f"✅ Accesso <b>{tier.upper()}</b> attivato. "
                                 "L'admin ti aggiungerà al canale a breve.")


def _handle(update: dict):
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
        tg.send_message(chat_id, "👋 Benvenuto nel signal bot.\n" + _plans_text())
    elif cmd == "/plans":
        tg.send_message(chat_id, _plans_text())
    elif cmd == "/subscribe":
        tier = args[0].lower() if args else ""
        tg.send_message(chat_id, _subscribe_text(tier))
    elif cmd == "/status":
        tg.send_message(chat_id, _status_text(chat_id))
    elif cmd == "/referral":
        if args:
            ok = subs.set_referred_by(chat_id, args[0].upper())
            tg.send_message(chat_id, "✅ Referral registrato." if ok
                            else "❌ Codice non valido o già impostato.")
        else:
            rec = subs.get(chat_id)
            tg.send_message(chat_id, f"Il tuo codice referral: <code>{rec.get('referral_code')}</code>\n"
                                     "Condividilo: chi si abbona ti dà un'estensione.")
    # ── admin ──
    elif cmd == "/grant" and _is_admin(chat_id):
        if len(args) >= 2:
            target, tier = args[0], args[1].lower()
            days = int(args[2]) if len(args) > 2 else config.SUB_DAYS
            subs.grant(target, tier, days)
            deliver_access(target, tier)
            tg.send_message(chat_id, f"✅ {tier} concesso a {target} per {days}g.")
        else:
            tg.send_message(chat_id, "Uso: /grant <chat_id> <premium|vip> [giorni]")
    elif cmd == "/stats" and _is_admin(chat_id):
        active = subs.active_subscribers()
        tg.send_message(chat_id, f"Abbonati attivi: <b>{len(active)}</b>")
    elif cmd == "/broadcast" and _is_admin(chat_id):
        body = text[len("/broadcast"):].strip()
        n = 0
        for cid, _ in subs.active_subscribers():
            if tg.send_message(cid, body):
                n += 1
        tg.send_message(chat_id, f"Inviato a {n} abbonati.")


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
