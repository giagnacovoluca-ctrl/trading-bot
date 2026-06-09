"""
publisher.py — daemon read-only che pubblica i segnali su Telegram.
- Tail dei *_signals.csv esistenti (offset persistente, anti-repost).
- PREMIUM: invio immediato e completo.
- FREE: solo chiusure positive (importo + % + guadagno), da live_trades.csv.

Processo ISOLATO dal core di trading: un crash qui non tocca scanner/executor.
Avvio standalone:  python bot_telegram/publisher.py
"""
from __future__ import annotations

import logging
import time

import config
import formatter
import telegram_api as tg
from csv_tail import CsvTailer

log = logging.getLogger("publisher")

class _TradeClosureTracker:
    """Accumula pnl per signal_id; quando remaining→0 restituisce info chiusura."""

    def __init__(self):
        self._open: dict = {}

    def feed(self, row: dict) -> dict | None:
        sid = row.get("signal_id") or row.get("token_symbol") or "?"
        action = (row.get("action") or "").strip()
        pnl = _to_float(row.get("pnl_eur"))
        remaining = _to_float(row.get("remaining"))
        change_pct = _to_float(row.get("change_pct"))

        if action == "entry":
            self._open[sid] = {
                "pnl": 0.0, "invested_eur": None,
                "symbol": row.get("token_symbol", "?"),
                "system": row.get("system", ""),
                "chain": row.get("chain", ""),
                "prev_remaining": 1.0,
            }
            return None

        if pnl is None:
            return None

        state = self._open.setdefault(sid, {
            "pnl": 0.0, "invested_eur": None,
            "symbol": row.get("token_symbol", "?"),
            "system": row.get("system", ""),
            "chain": row.get("chain", ""),
            "prev_remaining": 1.0,
        })

        state["pnl"] += pnl

        # Deriva capitale investito dalla prima uscita con pnl != 0
        if state["invested_eur"] is None and pnl and change_pct:
            fraction = state["prev_remaining"] - (remaining or 0.0)
            if fraction > 0.01:
                derived = pnl / ((change_pct / 100.0) * fraction)
                if 20.0 <= derived <= 2000.0:
                    state["invested_eur"] = derived

        if remaining is not None:
            state["prev_remaining"] = remaining

        if remaining is not None and remaining <= 0.001:
            total_pnl = state["pnl"]
            invested = state["invested_eur"] or 100.0
            closure = {
                "symbol": state["symbol"], "system": state["system"],
                "chain": state["chain"],
                "pnl_eur": total_pnl,
                "invested_eur": invested,
                "pct": total_pnl / invested * 100.0 if invested > 0 else None,
            }
            self._open.pop(sid, None)
            return closure if total_pnl > 0 else None

        return None


class Publisher:
    def __init__(self):
        self.tailers = [
            CsvTailer(config.SIGNALS_DIR / fname, key=fname, skip_backlog=True)
            for fname in config.SIGNAL_FILES
        ]
        self.file_system = dict(config.SIGNAL_FILES)
        self._trades_tailer = CsvTailer(config.TRADES_CSV, key="_live_trades", skip_backlog=True)
        self._closure_tracker = _TradeClosureTracker()

    # ── pubblicazione segnale full su PREMIUM ──────────────────────────────────
    def _publish_full(self, row: dict, system: str):
        prob = _to_float(row.get("pump_probability"))
        if prob is not None and prob < config.PREMIUM_MIN_PROBABILITY:
            return
        chan = config.channel_for_system(system)
        if not chan:
            log.warning("[pub] nessun canale Premium configurato — segnale non inviato")
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
                for row in self._trades_tailer.new_rows():
                    closure = self._closure_tracker.feed(row)
                    if closure and config.FREE_CHANNEL_ID:
                        tg.send_message(config.FREE_CHANNEL_ID,
                                        formatter.format_closure_free(closure))
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
