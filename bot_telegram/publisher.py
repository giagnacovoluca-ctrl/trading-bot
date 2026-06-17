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

        # pnl_eur in live_trades.csv è CUMULATIVO per segnale (ogni riga di uscita
        # porta il totale corrente): si tiene l'ultimo valore, NON si somma.
        # Il += gonfiava le chiusure postate sul FREE (tp1 +12 → trail +18
        # veniva pubblicato come +30).
        state["pnl"] = pnl

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
            CsvTailer(path, key=path.name, skip_backlog=True)
            for path, _system in config.SIGNAL_SOURCES
        ]
        self.file_system = {path.name: system for path, system in config.SIGNAL_SOURCES}
        self._trades_tailer = CsvTailer(config.TRADES_CSV, key="_live_trades", skip_backlog=True)
        self._events_tailer = CsvTailer(config.WALLET_EVENTS_CSV, key="_wallet_events", skip_backlog=True)
        self._closure_tracker = _TradeClosureTracker()
        # Anti-flood alert whale (10/06: confl>=2 da solo = 3610 alert/gg, sell
        # micro ripetuti = 2202/gg → storm di 429 Telegram alle 07:13)
        self._wallet_alert_last: dict = {}      # (mint, side) → epoch ultimo alert
        self._wallet_alert_window: list = []    # epoch degli alert nell'ultima ora
        self._wallet_alert_dropped = 0
        self._teaser_times: list = []           # epoch teaser live FREE (ultime 24h)
        self._pump_grad_notified: dict = {}     # token_symbol → epoch ultimo entry notify (dedup pool multiple)

    # ── alert whale (wallet_events.csv) su PREMIUM ─────────────────────────────
    def _publish_wallet_event(self, row: dict):
        if not config.PREMIUM_CHANNEL_ID:
            return
        side  = (row.get("side") or "").lower()
        usd   = _to_float(row.get("usd")) or 0.0
        confl = _to_float(row.get("confluence")) or 1.0
        wake  = _to_float(row.get("wake_days")) or 0.0
        note  = row.get("note", "") or ""

        # Criteri stretti (10/06): l'OR originale faceva passare ~5800 alert/gg
        # (confl>=2 è quasi sempre vero col bot-spray; i sell sono micro-dump
        # ripetuti dello stesso wallet a distanza di secondi) → 429 a raffica.
        if side == "sell":
            # sell su token segnalati di recente, ma solo di taglia significativa
            if "sell_after_signal" not in note or usd < config.WHALE_ALERT_MIN_USD / 2:
                return
        elif side == "buy":
            # buy: taglia minima SEMPRE; confluenza/risveglio da soli non bastano.
            # Eccezione: buy molto grossi (4x soglia) passano comunque.
            big = usd >= config.WHALE_ALERT_MIN_USD * 4
            qualified = usd >= config.WHALE_ALERT_MIN_USD and (confl >= 2 or wake >= 1)
            if not (big or qualified):
                return
        else:
            return

        # Dedup per (mint, side): max 1 alert ogni 30 min sullo stesso token
        now = time.time()
        key = (row.get("mint", ""), side)
        if now - self._wallet_alert_last.get(key, 0) < 1800:
            return
        # Tetto globale: max 20 alert/h (limite Telegram canale ≈ 20 msg/min,
        # ma qui il collo è l'utilità del canale, non l'API)
        self._wallet_alert_window = [t for t in self._wallet_alert_window if now - t < 3600]
        if len(self._wallet_alert_window) >= 20:
            self._wallet_alert_dropped += 1
            if self._wallet_alert_dropped % 50 == 1:
                log.warning("[pub] tetto 20 alert whale/h raggiunto — %d scartati",
                            self._wallet_alert_dropped)
            return

        self._wallet_alert_last[key] = now
        self._wallet_alert_window.append(now)
        tg.send_message(config.PREMIUM_CHANNEL_ID, formatter.format_wallet_event(row))

    # ── pubblicazione segnale full su PREMIUM ──────────────────────────────────
    def _publish_full(self, row: dict, system: str):
        prob = _to_float(row.get("pump_probability"))
        if prob is not None and prob < config.PREMIUM_MIN_PROBABILITY:
            return
        # pre_grad shadow (12/06): segnali a size=0 (rugcheck rilassato 25-55%),
        # tracciati solo nel simulator per stimare l'edge — non vanno su Telegram.
        if "shadow=true" in (row.get("top_features", "") or ""):
            return
        # mirror: sistema in paper, WR~14%, escluso dal track record pubblico
        # finché non validato — non pubblicare né entry né exit sui canali.
        if system == "mirror":
            return
        # pump_grad: applica gli stessi filtri del simulator per evitare di
        # pubblicare segnali che non verranno mai aperti (87% del totale era spam)
        if system == "pump_grad":
            liq = _to_float(row.get("liquidity_usd")) or 0.0
            chg = _to_float(row.get("change_1h_pct")) or 0.0
            vol = _to_float(row.get("volume_1h_usd")) or 0.0
            if 0 < liq < 25_000:   return
            if chg > 20:           return
            if 0 < vol < 5_000:    return
            # Dedup pool multiple per stesso token: max 1 entry notify ogni 30 min
            sym = str(row.get("token_symbol", "") or "").upper()
            now_e = time.time()
            if sym and now_e - self._pump_grad_notified.get(sym, 0) < 1800:
                return
            if sym:
                self._pump_grad_notified[sym] = now_e
        chan = config.channel_for_system(system)
        if not chan:
            log.warning("[pub] nessun canale Premium configurato — segnale non inviato")
            return
        tg.send_message(chan, formatter.format_full(row, system))
        self._maybe_publish_teaser(row, system)

    # ── teaser live censurato su FREE ──────────────────────────────────────────
    def _maybe_publish_teaser(self, row: dict, system: str):
        """Dopo ogni full su Premium, valuta un teaser censurato sul FREE.
        Rate limit: min FREE_TEASER_MIN_INTERVAL_MIN tra teaser e max
        FREE_TEASER_MAX_PER_DAY/24h — il FREE deve incuriosire, non spammare."""
        if not (config.FREE_TEASER_ENABLED and config.FREE_CHANNEL_ID):
            return
        prob = _to_float(row.get("pump_probability"))
        if prob is not None and prob < config.FREE_MIN_PROBABILITY:
            return
        now = time.time()
        self._teaser_times = [t for t in self._teaser_times if now - t < 86400]
        if len(self._teaser_times) >= config.FREE_TEASER_MAX_PER_DAY:
            return
        if self._teaser_times and now - self._teaser_times[-1] < config.FREE_TEASER_MIN_INTERVAL_MIN * 60:
            return
        self._teaser_times.append(now)
        tg.send_message(config.FREE_CHANNEL_ID, formatter.format_teaser_live(row, system),
                        reply_markup=formatter.premium_keyboard())

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
                    # pre_grad shadow: trade a size=0, mai pubblicati su Telegram
                    if "shadow=true" in (row.get("note", "") or ""):
                        continue
                    # mirror: in paper, WR~14%, escluso dal track record pubblico
                    if row.get("system") == "mirror":
                        continue
                    # PREMIUM: ciclo di vita completo del trade (TP/trailing/SL
                    # gestiti dal simulator) per i segnali già pubblicati
                    action = (row.get("action") or "").strip()
                    # liq_collapse = pool svuotata in secondi, non informativo su Premium
                    if action in formatter._EXIT_LABEL and action != "liq_collapse" and config.PREMIUM_CHANNEL_ID:
                        tg.send_message(config.PREMIUM_CHANNEL_ID,
                                        formatter.format_exit_premium(row))
                    closure = self._closure_tracker.feed(row)
                    if closure and config.FREE_CHANNEL_ID:
                        tg.send_message(config.FREE_CHANNEL_ID,
                                        formatter.format_closure_free(closure),
                                        reply_markup=formatter.premium_keyboard())
                    if closure:
                        try:
                            import x_poster
                            x_poster.maybe_send_x_draft(closure)
                        except Exception as e:
                            log.warning("[pub] x_poster: %s", e)
                for row in self._events_tailer.new_rows():
                    self._publish_wallet_event(row)
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
