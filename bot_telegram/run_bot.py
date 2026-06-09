"""
run_bot.py — orchestratore del modulo Telegram (ISOLATO dal core di trading).
Avvia in thread daemon:
  1. publisher       — feed segnali su Premium + teaser FREE ritardati
  2. bot             — comandi utente/admin (long-polling)
  3. payments        — verifica pagamenti on-chain (opzionale)
  4. track_record    — recap performance giornaliero sul canale FREE
  5. gating          — declassa/espelle abbonati scaduti

Avvio:  python bot_telegram/run_bot.py
Flag:   --no-payments  --no-publisher  --no-bot  --no-track
"""
from __future__ import annotations

import argparse
import logging
import threading
import time

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("run_bot")

_STOP = threading.Event()


def _start(name: str, target, *args):
    def _wrap():
        while not _STOP.is_set():
            try:
                target(*args)
                return
            except Exception as e:
                log.exception("[%s] crash, restart tra 10s: %s", name, e)
                _STOP.wait(10)
    t = threading.Thread(target=_wrap, name=name, daemon=True)
    t.start()
    log.info("[run] componente avviato: %s", name)
    return t


def _gating_loop(stop_event):
    """Declassa gli scaduti e prova a rimuoverli dai canali a pagamento."""
    import subscriptions as subs
    import telegram_api as tg
    while not stop_event.is_set():
        try:
            for chat_id, rec in subs.expired_since(grace_sec=3600):
                chan = config.PREMIUM_CHANNEL_ID
                if chan:
                    tg.ban_member(chan, chat_id)
                    tg.unban_member(chan, chat_id)   # ban+unban = kick (può rientrare)
                subs.downgrade_to_free(chat_id)
                tg.send_message(chat_id, "⏳ Il tuo abbonamento è scaduto. /plans per rinnovare.")
                log.info("[gating] scaduto declassato: %s", chat_id)
        except Exception as e:
            log.exception("[gating] errore: %s", e)
        stop_event.wait(3600)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-publisher", action="store_true")
    ap.add_argument("--no-bot", action="store_true")
    ap.add_argument("--no-payments", action="store_true")
    ap.add_argument("--no-track", action="store_true")
    args = ap.parse_args()

    if not config.is_configured():
        log.error("TELEGRAM_BOT_TOKEN mancante in bot_telegram/.env — esco.")
        return

    if not args.no_publisher:
        import publisher
        _start("publisher", publisher.Publisher().run, _STOP)
    if not args.no_bot:
        import bot
        _start("bot", bot.run, _STOP)
        _start("gating", _gating_loop, _STOP)
    if not args.no_payments and (config.PAY_WALLET_EVM or config.PAY_WALLET_SOL):
        import payments
        _start("payments", payments.run, _STOP)
    if not args.no_track:
        import track_record
        _start("track_record", track_record.run_daily, _STOP)

    log.info("[run] bot_telegram operativo. Ctrl+C per fermare.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("[run] arresto…")
        _STOP.set()
        time.sleep(2)


if __name__ == "__main__":
    main()
