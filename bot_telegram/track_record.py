"""
track_record.py — aggrega live_trades.csv in metriche di performance.
Genera state/stats.json (per landing page) e un recap testuale da postare sul
canale FREE (credibilità = motore di acquisizione).

Schema live_trades.csv:
  ts,signal_id,system,token_symbol,chain,pair_address,action,price,change_pct,
  vol_h1,bsr,remaining,pnl_eur,exit_reason,note
"""
from __future__ import annotations

import csv
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

import config
import store
import telegram_api as tg

log = logging.getLogger("track_record")

_STATS_FILE = "stats.json"


def _parse_ts(s: str) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    # epoch?
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
        try:
            # naive ts in live_trades.csv = datetime.now() locale (come scritto
            # da trade_simulator e letto via datetime.fromisoformat(...).timestamp());
            # interpretarlo come UTC sfasava la finestra 24h di +2h (CEST),
            # gonfiando pnl_24h con trade extra di 24-26h fa.
            return datetime.strptime(s.split("+")[0].strip(), fmt).timestamp()
        except ValueError:
            continue
    return None


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def compute() -> dict:
    path = config.TRADES_CSV
    if not path.exists():
        return {"available": False}

    pnl_by_signal: dict[str, float] = defaultdict(float)
    sys_by_signal: dict[str, str] = {}
    sym_by_signal: dict[str, str] = {}
    shadow_signals: set[str] = set()
    last_ts_by_signal: dict[str, float] = {}
    last_24h_by_signal: dict[str, float] = {}   # pnl ultima riga in finestra 24h
    base_24h_by_signal: dict[str, float] = {}   # pnl ultima riga pre-finestra 24h
    total_pnl = 0.0
    pnl_24h = 0.0
    now = time.time()

    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            pnl = _to_float(row.get("pnl_eur"))
            if pnl is None:
                continue
            sid = row.get("signal_id") or row.get("token_symbol") or "?"
            # pnl_eur è CUMULATIVO per segnale: l'esito è l'ULTIMA riga, non la
            # somma (la somma double-contava le uscite parziali: +1236€ pubblicati
            # vs +291€ reali al 10/06). Le righe sono in ordine cronologico.
            pnl_by_signal[sid] = pnl
            sys_by_signal[sid] = row.get("system", "")
            # alcuni scanner salvano il ticker già con "$" → evita "$$SCUP" nei recap
            sym_by_signal[sid] = (row.get("token_symbol") or sid).lstrip("$")
            # pre_grad shadow (12/06): size reale=0, non deve impattare il
            # track record pubblico (stessa esclusione del dashboard interno)
            note = row.get("note", "") or ""
            if "shadow=true" in note or "shadow_pnl=" in note:
                shadow_signals.add(sid)
            ts = _parse_ts(row.get("ts", ""))
            if ts:
                last_ts_by_signal[sid] = max(last_ts_by_signal.get(sid, 0), ts)
                if now - ts > 86400:
                    base_24h_by_signal[sid] = pnl
                else:
                    last_24h_by_signal[sid] = pnl

    # Esclude i segnali mai tradati (pnl=0: purged_stale, skip, duplicati…):
    # contarli come "loss" deprimeva il win-rate (30.4% vs 49.9% reale al 10/06).
    # Esclude anche "mirror": sistema in paper con WR~14%, escluderlo evita che
    # inquini il track record pubblico finché non è validato.
    mirror_signals: set[str] = {s for s, _ in pnl_by_signal.items()
                                if sys_by_signal.get(s) == "mirror"}
    _excluded = shadow_signals | mirror_signals
    closed = [(s, p) for s, p in pnl_by_signal.items()
              if p != 0.0 and s not in _excluded]
    total_pnl = sum(p for _, p in closed)
    # pnl_eur è CUMULATIVO per segnale: il 24h corretto è il delta tra l'ultima
    # riga in finestra e l'ultima riga PRE-finestra (stesso fix di
    # trade_simulator._compute_daily_pnl del 12/06, non sommare i cumulativi).
    pnl_24h = sum(p - base_24h_by_signal.get(s, 0.0)
                  for s, p in last_24h_by_signal.items()
                  if s not in _excluded)
    wins = [s for s, p in closed if p > 0]
    losses = [s for s, p in closed if p <= 0]
    n = len(closed)
    win_rate = (len(wins) / n * 100) if n else 0.0

    best = max(closed, key=lambda kv: kv[1], default=(None, 0.0))
    worst = min(closed, key=lambda kv: kv[1], default=(None, 0.0))

    # breakdown per sistema
    by_system: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "n": 0, "wins": 0})
    for sid, p in closed:
        s = sys_by_signal.get(sid, "")
        by_system[s]["pnl"] += p
        by_system[s]["n"] += 1
        by_system[s]["wins"] += 1 if p > 0 else 0

    # ── finestra 7 giorni (recap settimanale FREE) ──────────────────────────
    WEEK = 7 * 86400
    w_closed = [(s, p) for s, p in closed
                if now - last_ts_by_signal.get(s, 0) <= WEEK]
    w_wins = [(s, p) for s, p in w_closed if p > 0]
    w_best = max(w_closed, key=lambda kv: kv[1], default=(None, 0.0))
    w_by_sys: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "n": 0, "wins": 0})
    for sid, p in w_closed:
        s = sys_by_signal.get(sid, "")
        w_by_sys[s]["pnl"] += p
        w_by_sys[s]["n"] += 1
        w_by_sys[s]["wins"] += 1 if p > 0 else 0
    weekly = {
        "trades": len(w_closed),
        "wins": len(w_wins),
        "losses": len(w_closed) - len(w_wins),
        "win_rate": round(len(w_wins) / len(w_closed) * 100, 1) if w_closed else 0.0,
        "pnl_eur": round(sum(p for _, p in w_closed), 2),
        "best": {"symbol": sym_by_signal.get(w_best[0], "—"),
                 "pnl_eur": round(w_best[1], 2)},
        "by_system": {k: {"pnl_eur": round(v["pnl"], 2), "n": v["n"],
                          "win_rate": round(v["wins"] / v["n"] * 100, 1) if v["n"] else 0.0}
                      for k, v in w_by_sys.items()},
    }

    stats = {
        "available": True,
        "weekly": weekly,
        "updated_at": now,
        "updated_iso": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "trades_closed": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "total_pnl_eur": round(total_pnl, 2),
        "pnl_24h_eur": round(pnl_24h, 2),
        "avg_pnl_eur": round(total_pnl / n, 2) if n else 0.0,
        "best": {"symbol": sym_by_signal.get(best[0], "—"), "pnl_eur": round(best[1], 2)},
        "worst": {"symbol": sym_by_signal.get(worst[0], "—"), "pnl_eur": round(worst[1], 2)},
        "by_system": {k: {"pnl_eur": round(v["pnl"], 2), "n": v["n"],
                          "win_rate": round(v["wins"] / v["n"] * 100, 1) if v["n"] else 0.0}
                      for k, v in by_system.items()},
    }
    store.save(_STATS_FILE, stats)
    return stats


def recap_text(stats: dict | None = None) -> str:
    stats = stats or compute()
    if not stats.get("available") or stats.get("trades_closed", 0) == 0:
        return "📊 Track record: not enough data yet."
    s = stats
    sign = "🟢" if s["total_pnl_eur"] >= 0 else "🔴"
    sign24 = "🟢" if s["pnl_24h_eur"] >= 0 else "🔴"
    lines = [
        "📊 <b>TRACK RECORD</b>",
        "━━━━━━━━━━━━━━━",
        f"{sign} Total P&L      <b>{s['total_pnl_eur']:+.2f}€</b>",
        f"{sign24} Last 24h       <b>{s['pnl_24h_eur']:+.2f}€</b>",
        f"🎯 Win-rate       <b>{s['win_rate']}%</b>",
        f"📈 Trades closed  <b>{s['trades_closed']}</b> · avg {s['avg_pnl_eur']:+.2f}€",
    ]
    # Top strategie attive (per pnl, min 10 trade) — il dato che vende davvero
    top = sorted(((k, v) for k, v in s.get("by_system", {}).items()
                  if v.get("n", 0) >= 10 and v.get("pnl_eur", 0) > 0),
                 key=lambda kv: kv[1]["pnl_eur"], reverse=True)[:3]
    if top:
        lines += ["", "🔥 <b>Top strategies</b>"]
        lines += [f"  • {_e(k)}  <b>{v['pnl_eur']:+.0f}€</b>  (WR {v['win_rate']:.0f}%)"
                  for k, v in top]
    lines += [
        "━━━━━━━━━━━━━━━",
        "💎 Real-time signals 👇",
        f"<i>Updated {s['updated_iso']} · €100/signal, all trades counted</i>",
    ]
    return "\n".join(lines)


def weekly_recap_text(stats: dict | None = None) -> str | None:
    """Recap settimanale FREE con 'delta Premium': cosa hanno visto gli abbonati.
    Numeri reali dal CSV (verificabili) — niente % sparate, è il differenziatore
    rispetto ai canali scam. None se la settimana non ha trade."""
    stats = stats or compute()
    w = stats.get("weekly") or {}
    if not stats.get("available") or not w.get("trades"):
        return None
    sign = "🟢" if w["pnl_eur"] >= 0 else "🔴"
    lines = [
        "📊 <b>Weekly recap — what Premium members saw</b>",
        f"Signals traded: <b>{w['trades']}</b> · "
        f"Wins: <b>{w['wins']}</b> · Stop loss: {w['losses']} (WR {w['win_rate']}%)",
        f"{sign} Net P&L (7d): <b>{w['pnl_eur']:+.2f}€</b>",
    ]
    if w["best"]["pnl_eur"] > 0:
        lines.append(f"🏆 Best trade: ${_e(w['best']['symbol'])} <b>{w['best']['pnl_eur']:+.2f}€</b>")
    top = sorted(((k, v) for k, v in w.get("by_system", {}).items()
                  if v.get("pnl_eur", 0) > 0),
                 key=lambda kv: kv[1]["pnl_eur"], reverse=True)[:2]
    if top:
        lines.append("🔥 Top systems: " + " · ".join(
            f"{_e(k)} <b>{v['pnl_eur']:+.0f}€</b> (WR {v['win_rate']:.0f}%)" for k, v in top))
    lines += [
        "",
        "Every one of these hit Premium <b>in real time</b>, with entry, TP and SL.",
        "Here on Free you only see them after they close.",
        "💎 Get the next one live 👇",
    ]
    return "\n".join(lines)


_WEEKLY_STATE = "weekly_recap.json"


def post_weekly_recap(stats: dict | None = None) -> bool:
    """Posta il recap settimanale sul FREE se sono passati ≥6.5 giorni dall'ultimo."""
    state = store.load(_WEEKLY_STATE, {})
    if time.time() - state.get("last_ts", 0) < 6.5 * 86400:
        return False
    text = weekly_recap_text(stats)
    if not text or not config.FREE_CHANNEL_ID:
        return False
    import formatter
    tg.send_message(config.FREE_CHANNEL_ID, text,
                    reply_markup=formatter.premium_keyboard())
    store.save(_WEEKLY_STATE, {"last_ts": time.time()})
    return True


def _e(x) -> str:
    import html
    return html.escape(str(x))


def post_recap():
    """Calcola, posta il recap sul canale FREE e rigenera la landing page.
    Una volta a settimana posta anche il recap 'delta Premium'."""
    stats = compute()
    text = recap_text(stats)
    if config.FREE_CHANNEL_ID:
        import formatter
        tg.send_message(config.FREE_CHANNEL_ID, text,
                        reply_markup=formatter.premium_keyboard())
    try:
        post_weekly_recap(stats)
    except Exception as e:
        log.warning("[track] recap settimanale non postato: %s", e)
    try:
        import landing
        landing.generate(stats)
    except Exception as e:
        log.warning("[track] landing non generata: %s", e)
    return text


def run_daily(stop_event=None, interval_hours: float = 24.0):
    """Loop che posta il recap ogni interval_hours."""
    while stop_event is None or not stop_event.is_set():
        try:
            post_recap()
        except Exception as e:
            log.exception("[track] errore recap: %s", e)
        wait = interval_hours * 3600
        if stop_event:
            stop_event.wait(wait)
        else:
            time.sleep(wait)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")
    print(recap_text())
