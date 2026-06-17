"""
token_outcome_logger.py — dataset multi-timeframe per ML/backtest.

Per OGNI segnale generato dai vari scanner (anche quelli scartati/non tradati:
skip_stale, skip_routing, rugcheck-block...) registra il prezzo a T0 e poi a
+15m/+1h/+4h/+24h/+72h via DexScreener, scrivendo una riga finale in
reports/token_outcomes.csv con i return percentuali e il max return osservato.

Oggi questo dato esiste SOLO per i segnali che il simulator apre e chiude
(live_trades.csv, exit-driven), quindi è cieco sui token scartati e su
timeframe non comparabili tra sistemi. Questo logger chiude quel gap: è il
dataset di base per validare filtri (es. skip_stale di oggi) e per un futuro
modello ML (vedi idea "Token Early Accumulation Scanner").

Sorgenti (stesso schema signal_id/.../pair_address/price_entry_usd/.../top_features):
  - reports/signals_log.csv      (defi_optimized)
  - reports/pre_grad_signals.csv (pre_grad_monitor)
  - reports/mirror_signals.csv   (wallet_mirror)
  - reports/pump_grad_signals.csv / base_pump_signals.csv
  - ../gemme/reports/gems_log.csv (schema simile, id_field=gem_id)

Avvio standalone: python token_outcome_logger.py
Integrazione:     from token_outcome_logger import outcome_loop  (thread in run.py)
"""
from __future__ import annotations

import csv
import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                     datefmt="%H:%M:%S")
log = logging.getLogger("token_outcome")

_BASE = Path(__file__).resolve().parent          # defi/
_REPORTS = _BASE / "reports"
_REPORTS.mkdir(exist_ok=True)

STATE_FILE = _REPORTS / "token_outcome_state.json"
OUT_CSV = _REPORTS / "token_outcomes.csv"

DEXSCREENER_BASE = "https://api.dexscreener.com/latest"

# (nome sorgente, path csv, nome colonna id)
SOURCES = [
    ("defi",      _REPORTS / "signals_log.csv",      "signal_id"),
    ("pre_grad",  _REPORTS / "pre_grad_signals.csv",  "signal_id"),
    ("mirror",    _REPORTS / "mirror_signals.csv",    "signal_id"),
    ("pump_grad", _REPORTS / "pump_grad_signals.csv", "signal_id"),
    ("base_pump", _REPORTS / "base_pump_signals.csv", "signal_id"),
    ("gemme",     _BASE.parent / "gemme" / "reports" / "gems_log.csv", "gem_id"),
]

# (label, minuti dopo T0)
CHECKPOINTS = [("15m", 15), ("1h", 60), ("4h", 240), ("24h", 1440), ("72h", 4320)]

OUT_FIELDS = (
    ["signal_id", "source", "token_symbol", "chain", "token_address", "pair_address",
     "t0_ts", "t0_price", "top_features"]
    + [f"ret_{lbl}_pct" for lbl, _ in CHECKPOINTS]
    + ["max_ret_pct", "status"]
)

# se una sorgente ha colonne diverse (gemme) qui i mapping verso lo schema comune
_COL_ALIASES = {
    "gemme": {
        "token_symbol": "token_symbol", "chain": "chain", "token_address": "token_address",
        "pair_address": "pair_address", "price_entry_usd": "price_entry_usd",
        "timestamp_entry": "timestamp_entry", "top_features": "top_features",
    },
}

_POLL_INTERVAL_SEC = 5 * 60       # ciclo del loop
_MAX_MISSES = 8                   # fetch falliti consecutivi prima di abbandonare il pending
_STALE_AFTER_MIN = 72 * 60 + 60   # margine oltre il 72h checkpoint per chiudere comunque


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            log.warning("state corrotto, riparto da zero")
    return {"seen": {}, "pending": {}}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE_FILE)


def _safe_get(url: str, params: dict = None, label: str = "") -> Optional[dict]:
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=12)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            return None
        except Exception as e:
            if attempt == 2:
                log.debug(f"_safe_get {label} fallito: {e}")
            time.sleep(1 + attempt)
    return None


def _fetch_price_usd(chain: str, token_address: str, pair_address: str) -> Optional[float]:
    """Prezzo corrente USD via DexScreener: prova pair_address, altrimenti
    /dex/tokens/<address> scegliendo il pair con più liquidità sulla chain giusta."""
    pa = (pair_address or "").strip().lower()
    if pa and pa not in ("nan", "none", ""):
        data = _safe_get(f"{DEXSCREENER_BASE}/dex/pairs/{chain}/{pair_address}", label="outcome-pair")
        pairs = (data or {}).get("pairs") or []
        if pairs:
            try:
                return float(pairs[0]["priceUsd"])
            except (KeyError, TypeError, ValueError):
                pass

    data = _safe_get(f"{DEXSCREENER_BASE}/dex/tokens/{token_address}", label="outcome-token")
    pairs = (data or {}).get("pairs") or []
    pairs = [p for p in pairs if p.get("chainId") == chain]
    if not pairs:
        return None
    best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
    try:
        return float(best["priceUsd"])
    except (KeyError, TypeError, ValueError):
        return None


def _ingest_new_signals(state: dict) -> int:
    """Legge le righe nuove da tutte le sorgenti e le aggiunge a pending."""
    added = 0
    for source, path, id_field in SOURCES:
        if not path.exists():
            continue
        seen = state["seen"].setdefault(source, [])
        seen_set = set(seen)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for row in csv.DictReader(f):
                    sig_id = row.get(id_field, "")
                    if not sig_id or sig_id in seen_set:
                        continue
                    seen.append(sig_id)
                    seen_set.add(sig_id)

                    chain = (row.get("chain") or "").strip()
                    token_address = (row.get("token_address") or "").strip()
                    pair_address = (row.get("pair_address") or "").strip()
                    ts = (row.get("timestamp_entry") or "").strip()
                    try:
                        price_csv = float(row.get("price_entry_usd") or 0)
                    except ValueError:
                        price_csv = 0.0

                    if not chain or not token_address or not ts:
                        continue
                    if chain not in ("solana", "base"):
                        continue  # BSC/ETH disattivati

                    # T0 price: meglio un fetch live (poco dopo la creazione del
                    # segnale) che il price_entry_usd del CSV, per coerenza di
                    # unità con i fetch successivi (entrambi DexScreener priceUsd)
                    t0_price = _fetch_price_usd(chain, token_address, pair_address)
                    if t0_price is None or t0_price <= 0:
                        t0_price = price_csv if price_csv > 0 else None
                    if t0_price is None:
                        continue  # niente di utile da tracciare

                    state["pending"][f"{source}:{sig_id}"] = {
                        "signal_id": sig_id,
                        "source": source,
                        "token_symbol": row.get("token_symbol", ""),
                        "chain": chain,
                        "token_address": token_address,
                        "pair_address": pair_address,
                        "top_features": row.get("top_features", ""),
                        "t0_ts": ts,
                        "t0_epoch": time.time(),
                        "t0_price": t0_price,
                        "returns": {},
                        "misses": 0,
                    }
                    added += 1
        except Exception as e:
            log.warning(f"errore lettura {path.name}: {e}")
    return added


def _finalize_row(entry: dict) -> dict:
    rets = entry["returns"]
    row = {
        "signal_id": entry["signal_id"], "source": entry["source"],
        "token_symbol": entry["token_symbol"], "chain": entry["chain"],
        "token_address": entry["token_address"], "pair_address": entry["pair_address"],
        "t0_ts": entry["t0_ts"], "t0_price": entry["t0_price"],
        "top_features": entry["top_features"],
    }
    vals = []
    for lbl, _ in CHECKPOINTS:
        v = rets.get(lbl)
        row[f"ret_{lbl}_pct"] = "" if v is None else f"{v:.2f}"
        if v is not None:
            vals.append(v)
    row["max_ret_pct"] = f"{max(vals):.2f}" if vals else ""
    row["status"] = "complete" if len(rets) == len(CHECKPOINTS) else "partial"
    return row


def _append_csv(row: dict) -> None:
    header_needed = not OUT_CSV.exists()
    with open(OUT_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        if header_needed:
            w.writeheader()
        w.writerow(row)


def _process_pending(state: dict) -> tuple[int, int]:
    """Aggiorna i checkpoint scaduti. Ritorna (n_aggiornati, n_finalizzati)."""
    now = time.time()
    updated = 0
    finalized = []

    for key, entry in state["pending"].items():
        age_min = (now - entry["t0_epoch"]) / 60.0
        due = [lbl for lbl, mins in CHECKPOINTS if age_min >= mins and lbl not in entry["returns"]]
        if not due:
            if age_min > _STALE_AFTER_MIN:
                finalized.append(key)
            continue

        price = _fetch_price_usd(entry["chain"], entry["token_address"], entry["pair_address"])
        if price is None:
            entry["misses"] += 1
            if entry["misses"] >= _MAX_MISSES:
                finalized.append(key)
            continue

        entry["misses"] = 0
        ret_pct = (price - entry["t0_price"]) / entry["t0_price"] * 100.0
        for lbl in due:
            entry["returns"][lbl] = ret_pct
            updated += 1

        if len(entry["returns"]) == len(CHECKPOINTS) or age_min > _STALE_AFTER_MIN:
            finalized.append(key)

    for key in finalized:
        _append_csv(_finalize_row(state["pending"].pop(key)))

    return updated, len(finalized)


def _bootstrap(state: dict) -> None:
    """Primo avvio: segna come 'visti' tutti i segnali storici senza tracciarli
    (il prezzo live non sarebbe il T0 storico). Si traccia solo da qui in poi."""
    for source, path, id_field in SOURCES:
        if not path.exists():
            continue
        seen = state["seen"].setdefault(source, [])
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for row in csv.DictReader(f):
                    sig_id = row.get(id_field, "")
                    if sig_id:
                        seen.append(sig_id)
        except Exception as e:
            log.warning(f"bootstrap {path.name}: {e}")
    log.info("bootstrap completato: solo segnali futuri verranno tracciati")


def tick() -> None:
    state = _load_state()
    if not STATE_FILE.exists() and not state["seen"]:
        _bootstrap(state)
        _save_state(state)
        return
    added = _ingest_new_signals(state)
    updated, finalized = _process_pending(state)
    _save_state(state)
    if added or updated or finalized:
        log.info(f"+{added} nuovi | {updated} checkpoint aggiornati | "
                 f"{finalized} finalizzati | pending={len(state['pending'])}")


def outcome_loop(stop_event: threading.Event | None = None) -> None:
    log.info("token_outcome_logger avviato")
    while True:
        try:
            tick()
        except Exception as e:
            log.error(f"tick fallito: {e}")
        if stop_event is not None:
            if stop_event.wait(_POLL_INTERVAL_SEC):
                break
        else:
            time.sleep(_POLL_INTERVAL_SEC)


if __name__ == "__main__":
    outcome_loop()
