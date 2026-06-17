"""
liquidity_event_monitor.py — Monitor nuovi pool liquidità su Solana e Base.
Polling GeckoTerminal ogni 30s: pool nuovi (<5min, liq>$10k) → email queue + CSV.
Avviato da run.py (--no-liq per skippare).
"""
import csv
import logging
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

POLL_SEC         = 30
MAX_POOL_AGE_MIN = 5
MIN_LIQ_USD      = 10_000
SEEN_TTL_SEC     = 3600   # ignora pool già visti per 1h

_REPORTS = _HERE / "reports"
_CSV_OUT = _REPORTS / "liq_event_signals.csv"
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


def _queue_email(attrs: dict, chain: str, addr: str, liq: float, age_min: float):
    try:
        import email_digest
        token_name = attrs.get("name", "?").split(" / ")[0]
        subj = f"[LIQ ALERT] Nuova pool {token_name} su {chain.upper()} — ${liq:,.0f} in {age_min:.1f}min"
        body = (
            f"<b>Nuova pool liquidità rilevata</b><br>"
            f"Chain: {chain}<br>"
            f"Token: {token_name}<br>"
            f"Liquidità: ${liq:,.0f}<br>"
            f"Età pool: {age_min:.1f} min<br>"
            f"Pool: <code>{addr}</code><br>"
            f"<a href='https://dexscreener.com/{chain}/{addr}'>DexScreener</a>"
        )
        email_digest.queue_email("liq_monitor", subj, body)
        log.info(f"[liq] ▶ {token_name} {chain} ${liq:,.0f} età {age_min:.1f}min → email queued")
    except Exception as e:
        log.warning(f"[liq] queue_email: {e}")


def _append_csv(attrs: dict, chain: str, addr: str, liq: float, age_min: float):
    row = {
        "ts":            datetime.now().isoformat(),
        "chain":         chain,
        "pool_address":  addr,
        "token_name":    attrs.get("name", "?").split(" / ")[0],
        "liquidity_usd": f"{liq:.0f}",
        "age_min":       f"{age_min:.1f}",
        "created_at":    attrs.get("pool_created_at", ""),
    }
    _REPORTS.mkdir(parents=True, exist_ok=True)
    new_file = not _CSV_OUT.exists()
    with open(_CSV_OUT, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new_file:
            w.writeheader()
        w.writerow(row)


def _tick():
    _purge_seen()
    now = time.time()
    for chain in _CHAINS:
        for pool in _fetch_new_pools(chain):
            pool_id = pool.get("id", "")
            if not pool_id or pool_id in _seen:
                continue
            attrs = pool.get("attributes", {})
            liq   = float(attrs.get("reserve_in_usd", 0) or 0)
            addr  = pool_id.split("_")[-1]
            _seen[pool_id] = now
            if liq < MIN_LIQ_USD:
                continue
            age_min = _pool_age_min(attrs.get("pool_created_at", ""))
            if age_min > MAX_POOL_AGE_MIN:
                continue
            _append_csv(attrs, chain, addr, liq, age_min)
            _queue_email(attrs, chain, addr, liq, age_min)


def main(stop_event: threading.Event | None = None):
    log.info(f"[liq] ▶ avviato (poll {POLL_SEC}s, liq>${MIN_LIQ_USD:,}, età<{MAX_POOL_AGE_MIN}min)")
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
