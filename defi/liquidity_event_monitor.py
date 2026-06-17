"""
liquidity_event_monitor.py — Monitor nuovi pool liquidità su Solana e Base.
Polling GeckoTerminal ogni 30s: pool nuovi (<5min) →
  - Alert Telegram immediato (tutti, liq>$10k)
  - Segnale diretto in pump_grad_signals.csv (liq>$25k, bypass lag Dune)
  - liq_event_signals.csv (log storico)
Avviato da run.py (--no-liq per skippare).
"""
import csv
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
_EXEC = _ROOT / "executor"

for _p in [str(_HERE), str(_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=_EXEC / ".env", override=False)
except ImportError:
    pass

log = logging.getLogger("liq_monitor")

POLL_SEC          = 30
MAX_POOL_AGE_MIN  = 5
MIN_LIQ_ALERT     = 10_000   # soglia Telegram alert
MIN_LIQ_SIGNAL    = 25_000   # soglia per aprire trade via pump_grad (backtest: <25k=69% rug)
SEEN_TTL_SEC      = 3600

_REPORTS          = _HERE / "reports"
_CSV_OUT          = _REPORTS / "liq_event_signals.csv"
_PUMP_GRAD_CSV    = _REPORTS / "pump_grad_signals.csv"
_SHADOW_QUEUE_CSV = _REPORTS / "liq_shadow_queue.csv"
_PUMP_GRAD_COLS  = [
    "signal_id", "timestamp_entry", "token_symbol", "token_name",
    "token_address", "chain", "pair_address", "price_entry_usd",
    "volume_1h_usd", "liquidity_usd", "buy_sell_ratio_1h", "change_1h_pct",
    "pump_probability", "buy_tax", "sell_tax", "lp_locked", "is_honeypot",
    "top_features",
]
_CHAINS  = ["solana", "base"]
_GT_BASE = "https://api.geckoterminal.com/api/v2"
_HEADERS = {"Accept": "application/json;version=20230302"}

_seen: dict[str, float] = {}


def _purge_seen():
    cutoff = time.time() - SEEN_TTL_SEC
    for k in list(_seen.keys()):
        if _seen[k] < cutoff:
            del _seen[k]


def _fetch_new_pools(chain: str) -> list[dict]:
    url = f"{_GT_BASE}/networks/{chain}/new_pools?page=1"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=10)
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        log.debug(f"[liq] fetch {chain}: {e}")
        return []


def _pool_age_min(created_at_str: str) -> float:
    try:
        dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except Exception:
        return 999


def _extract_pool_data(pool: dict, chain: str) -> dict:
    """Estrae campi utili dal response GeckoTerminal new_pools."""
    attrs = pool.get("attributes", {})
    pool_id = pool.get("id", "")
    addr = pool_id.split("_")[-1]

    # token_address: relationships.base_token.data.id = "solana_ADDRESS"
    rel_id = (pool.get("relationships", {})
                  .get("base_token", {})
                  .get("data", {})
                  .get("id", ""))
    token_address = rel_id.split("_", 1)[-1] if "_" in rel_id else ""

    name_parts = attrs.get("name", "? / ?").split(" / ")
    token_symbol = name_parts[0].strip() if name_parts else "?"

    liq      = float(attrs.get("reserve_in_usd", 0) or 0)
    price    = float(attrs.get("base_token_price_usd", 0) or 0)
    vol_h1   = float((attrs.get("volume_usd") or {}).get("h1", 0) or 0)
    chg_1h   = float((attrs.get("price_change_percentage") or {}).get("h1", 0) or 0)
    age_min  = _pool_age_min(attrs.get("pool_created_at", ""))

    return {
        "addr":          addr,
        "token_address": token_address,
        "token_symbol":  token_symbol,
        "liq":           liq,
        "price":         price,
        "vol_h1":        vol_h1,
        "chg_1h":        chg_1h,
        "age_min":       age_min,
        "created_at":    attrs.get("pool_created_at", ""),
    }


def _notify(d: dict, chain: str):
    # Stessi filtri del simulator (publisher.py linee 163-169):
    # evita alert per pool che verranno comunque scartate → era 87% spam
    if d["chg_1h"] > 20:
        log.debug(f"[liq] notify skip {d['token_symbol']}: chg1h={d['chg_1h']:+.0f}% > 20%")
        return
    if 0 < d["vol_h1"] < 5_000:
        log.debug(f"[liq] notify skip {d['token_symbol']}: vol_h1=${d['vol_h1']:,.0f} < $5k")
        return
    try:
        import tg_alert
        chain_emoji = {"solana": "🟣", "base": "🔵"}.get(chain, "🔹")
        dex_url = f"https://dexscreener.com/{chain}/{d['addr']}"
        text = (
            f"💧 <b>Nuova pool</b> · {chain_emoji} {chain.upper()} 🚀\n"
            f"<b>${d['token_symbol']}</b> · età {d['age_min']:.1f} min\n"
            f"Liq: <b>${d['liq']:,.0f}</b> · Vol1h: ${d['vol_h1']:,.0f}\n"
            f"<a href='{dex_url}'>DexScreener</a>"
        )
        tg_alert.send(text)
    except Exception as e:
        log.debug(f"[liq] notify: {e}")


def _append_log_csv(d: dict, chain: str):
    row = {
        "ts":            datetime.now().isoformat(),
        "chain":         chain,
        "pool_address":  d["addr"],
        "token_symbol":  d["token_symbol"],
        "liquidity_usd": f"{d['liq']:.0f}",
        "vol_h1":        f"{d['vol_h1']:.0f}",
        "age_min":       f"{d['age_min']:.1f}",
        "signal_sent":   "1" if d["liq"] >= MIN_LIQ_SIGNAL else "0",
    }
    _REPORTS.mkdir(parents=True, exist_ok=True)
    new_file = not _CSV_OUT.exists()
    with open(_CSV_OUT, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new_file:
            w.writeheader()
        w.writerow(row)


def _build_signal_row(d: dict, chain: str) -> tuple[str, dict]:
    """Costruisce sid e riga CSV comune per segnali e shadow."""
    ts  = datetime.now().isoformat()
    sid = f"LIQ_{d['token_symbol']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    top_feat = (
        f"liq_monitor=true | pair_age_min={d['age_min']:.1f} | "
        f"vol_h1={d['vol_h1']:.0f} | chg_1h={d['chg_1h']:+.1f}%"
    )
    row = {
        "signal_id":        sid,
        "timestamp_entry":  ts,
        "token_symbol":     d["token_symbol"],
        "token_name":       d["token_symbol"],
        "token_address":    d["token_address"],
        "chain":            chain,
        "pair_address":     d["addr"],
        "price_entry_usd":  f"{d['price']:.8g}",
        "volume_1h_usd":    f"{d['vol_h1']:.2f}",
        "liquidity_usd":    f"{d['liq']:.2f}",
        "buy_sell_ratio_1h": "1.0",
        "change_1h_pct":    f"{d['chg_1h']:.2f}",
        "pump_probability": "0.75",
        "buy_tax":          "0.0",
        "sell_tax":         "0.0",
        "lp_locked":        "0",
        "is_honeypot":      "0",
        "top_features":     top_feat,
    }
    return sid, row


def _write_pump_grad_signal(d: dict, chain: str):
    """Scrive segnale reale in pump_grad_signals.csv (liq>=$25k)."""
    sid, row = _build_signal_row(d, chain)
    new_file = not _PUMP_GRAD_CSV.exists()
    with open(_PUMP_GRAD_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_PUMP_GRAD_COLS)
        if new_file:
            w.writeheader()
        w.writerow(row)
    log.info(
        f"[liq] ✅ segnale: {d['token_symbol']} {chain} "
        f"liq=${d['liq']:,.0f} vol1h=${d['vol_h1']:,.0f} → {sid}"
    )


def _write_shadow_queue(d: dict, chain: str):
    """Scrive pool liq $10k-$25k in liq_shadow_queue.csv.
    Il simulator lo legge, chiama _shadow_register, poi tronca il file."""
    sid, row = _build_signal_row(d, chain)
    new_file = not _SHADOW_QUEUE_CSV.exists()
    with open(_SHADOW_QUEUE_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_PUMP_GRAD_COLS)
        if new_file:
            w.writeheader()
        w.writerow(row)
    log.debug(
        f"[liq] 👻 shadow_queue: {d['token_symbol']} {chain} "
        f"liq=${d['liq']:,.0f} → {sid}"
    )


def _tick():
    _purge_seen()
    now = time.time()
    for chain in _CHAINS:
        for pool in _fetch_new_pools(chain):
            pool_id = pool.get("id", "")
            if not pool_id or pool_id in _seen:
                continue
            _seen[pool_id] = now
            d = _extract_pool_data(pool, chain)
            if d["liq"] < MIN_LIQ_ALERT or d["age_min"] > MAX_POOL_AGE_MIN:
                continue
            _append_log_csv(d, chain)
            if d["liq"] >= MIN_LIQ_SIGNAL:
                _notify(d, chain)
                try:
                    _write_pump_grad_signal(d, chain)
                except Exception as e:
                    log.warning(f"[liq] write_pump_grad_signal: {e}")
            else:
                # liq $10k-$25k: shadow queue separata, pump_grad_signals.csv rimane pulito.
                try:
                    _write_shadow_queue(d, chain)
                except Exception as e:
                    log.warning(f"[liq] write_shadow_queue: {e}")


def main(stop_event: threading.Event | None = None):
    log.info(
        f"[liq] ▶ avviato (poll {POLL_SEC}s, alert>${MIN_LIQ_ALERT:,}, "
        f"segnale>${MIN_LIQ_SIGNAL:,}, età<{MAX_POOL_AGE_MIN}min)"
    )
    while True:
        if stop_event and stop_event.is_set():
            break
        try:
            _tick()
        except Exception as e:
            log.warning(f"[liq] tick error: {e}")
        if stop_event:
            stop_event.wait(POLL_SEC)
        else:
            time.sleep(POLL_SEC)
    log.info("[liq] ■ fermato")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    main()
