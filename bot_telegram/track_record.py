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
            return datetime.strptime(s.split("+")[0].strip(), fmt).replace(
                tzinfo=timezone.utc).timestamp()
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
    last_ts_by_signal: dict[str, float] = {}
    total_pnl = 0.0
    pnl_24h = 0.0
    now = time.time()

    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            pnl = _to_float(row.get("pnl_eur"))
            if pnl is None:
                continue
            sid = row.get("signal_id") or row.get("token_symbol") or "?"
            pnl_by_signal[sid] += pnl
            total_pnl += pnl
            sys_by_signal[sid] = row.get("system", "")
            sym_by_signal[sid] = row.get("token_symbol", sid)
            ts = _parse_ts(row.get("ts", ""))
            if ts:
                last_ts_by_signal[sid] = max(last_ts_by_signal.get(sid, 0), ts)
                if now - ts <= 86400:
                    pnl_24h += pnl

    closed = list(pnl_by_signal.items())
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

    stats = {
        "available": True,
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
    lines = [
        "📊 <b>Track record</b> (auto)",
        f"Closed trades: <b>{s['trades_closed']}</b> · Win-rate: <b>{s['win_rate']}%</b>",
        f"{sign} Total P&L: <b>{s['total_pnl_eur']:+.2f}€</b> · last 24h: {s['pnl_24h_eur']:+.2f}€",
        f"Avg/trade: {s['avg_pnl_eur']:+.2f}€",
        f"🏆 Best: ${_e(s['best']['symbol'])} {s['best']['pnl_eur']:+.2f}€  ·  "
        f"Worst: ${_e(s['worst']['symbol'])} {s['worst']['pnl_eur']:+.2f}€",
        "",
        "💎 Real-time signals: /plans",
        f"<i>Updated {s['updated_iso']}</i>",
    ]
    return "\n".join(lines)


def _e(x) -> str:
    import html
    return html.escape(str(x))


def post_recap():
    """Calcola, posta il recap sul canale FREE e rigenera la landing page."""
    stats = compute()
    text = recap_text(stats)
    if config.FREE_CHANNEL_ID:
        tg.send_message(config.FREE_CHANNEL_ID, text)
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
