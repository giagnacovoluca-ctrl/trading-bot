"""
publisher.py — daemon read-only che pubblica i segnali su Telegram.
- Tail dei *_signals.csv esistenti (offset persistente, anti-repost).
- PREMIUM/VIP: invio immediato e completo.
- FREE: teaser ritardato di FREE_DELAY_MIN, senza entry price, solo prob >= soglia.

Processo ISOLATO dal core di trading: un crash qui non tocca scanner/executor.
Avvio standalone:  python bot_telegram/publisher.py
"""
from __future__ import annotations

import logging
import time

import config
import formatter
import store
import telegram_api as tg
from csv_tail import CsvTailer

log = logging.getLogger("publisher")

_QUEUE_FILE = "free_queue.json"   # teaser FREE schedulati (persistenti)


class Publisher:
    def __init__(self):
        self.tailers = [
            CsvTailer(config.SIGNALS_DIR / fname, key=fname, skip_backlog=True)
            for fname in config.SIGNAL_FILES
        ]
        self.file_system = dict(config.SIGNAL_FILES)

    # ── coda teaser FREE (persistente su disco) ────────────────────────────────
    def _load_queue(self) -> list:
        return store.load(_QUEUE_FILE, [])

    def _save_queue(self, q: list) -> None:
        store.save(_QUEUE_FILE, q)

    def _schedule_free(self, row: dict, system: str):
        if not config.FREE_CHANNEL_ID:
            return
        prob = _to_float(row.get("pump_probability"))
        if prob is not None and prob < config.FREE_MIN_PROBABILITY:
            return
        q = self._load_queue()
        q.append({
            "send_at": time.time() + config.FREE_DELAY_MIN * 60,
            "text": formatter.format_teaser(row, system),
        })
        self._save_queue(q)

    def _flush_free_due(self):
        q = self._load_queue()
        if not q:
            return
        now = time.time()
        due = [item for item in q if item.get("send_at", 0) <= now]
        if not due:
            return
        remaining = [item for item in q if item.get("send_at", 0) > now]
        for item in due:
            tg.send_message(config.FREE_CHANNEL_ID, item["text"])
        self._save_queue(remaining)

    # ── pubblicazione segnale full su PREMIUM/VIP ──────────────────────────────
    def _publish_full(self, row: dict, system: str):
        prob = _to_float(row.get("pump_probability"))
        if prob is not None and prob < config.PREMIUM_MIN_PROBABILITY:
            return
        chan = config.channel_for_system(system)
        if not chan:
            log.warning("[pub] nessun canale Premium/VIP configurato — segnale non inviato")
            return
        tg.send_message(chan, formatter.format_full(row, system))

    # ── loop principale ────────────────────────────────────────────────────────
    def run(self, stop_event=None):
        log.info("[pub] publisher avviato — sorgenti: %s", config.SIGNALS_DIR)
        if not config.is_configured():
            log.error("[pub] TELEGRAM_BOT_TOKEN mancante: configura bot_telegram/.env")
        while stop_event is None or not stop_event.is_set():
            try:
                for tailer in self.tailers:
                    system = self.file_system.get(tailer.key, tailer.key)
                    for row in tailer.new_rows():
                        self._publish_full(row, system)
                        self._schedule_free(row, system)
                self._flush_free_due()
            except Exception as e:  # daemon resiliente
                log.exception("[pub] errore nel loop: %s", e)
            _sleep(config.POLL_INTERVAL_SEC, stop_event)


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _sleep(seconds: float, stop_event):
    if stop_event is None:
        time.sleep(seconds)
    else:
        stop_event.wait(seconds)


def main(stop_event=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    Publisher().run(stop_event)


if __name__ == "__main__":
    main()
