"""
binance_futures_scanner.py
==========================
Scansiona Binance Futures ogni 10 minuti.
Token con volume/variazione anomali ma SENZA pair DEX su DexScreener
vengono scritti in reports/binance_futures_signals.csv e poi rilevati
automaticamente da trade_simulator.py (sistema "bnf").

Uso:
    py binance_futures_scanner.py

Avvialo in un terminale separato mentre run.py è in esecuzione.
I segnali vengono aperti nel ciclo successivo del bot (max 60s di ritardo).
"""

import csv
import json
import logging
import os
import time
from datetime import datetime, date
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bf_scan")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE      = os.path.dirname(os.path.abspath(__file__))
OUT_CSV   = os.path.join(BASE, "reports", "binance_futures_signals.csv")
SENT_FILE = os.path.join(BASE, "reports", "bf_sent.json")   # dedup giornaliero

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BINANCE_BASE  = "https://fapi.binance.com"
DEXSCREENER   = "https://api.dexscreener.com"
SCAN_INTERVAL = 600   # secondi (10 minuti)

MIN_VOL_24H_USD = 10_000_000   # vol 24h minimo su Binance Futures (USD)
MIN_CHG_24H_PCT = 5.0          # variazione 24h minima positiva (solo long)
MAX_CHG_24H_PCT = 30.0         # cap: se già pompato > 30% in 24h → skip (mossa esaurita)
MAX_CANDIDATES  = 15           # massimo top-N da analizzare

# Token esclusi: stablecoin e mega-cap poco significativi come "gem"
EXCLUDE = {
    "USDTUSDT", "USDCUSDT", "BUSDUSDT", "TUSDUSDT", "DAIUSDT", "FRAXUSDT",
    "BTCUSDT",  "ETHUSDT",  "BNBUSDT",
}

FIELDNAMES = [
    "signal_id", "timestamp_entry", "token_symbol", "chain",
    "pair_address", "price_entry_usd", "volume_1h_usd", "binance_chg1h",
]


# ---------------------------------------------------------------------------
# Persistenza dedup
# ---------------------------------------------------------------------------
def _load_sent() -> dict:
    """Carica {token: date_string} per evitare segnali duplicati nello stesso giorno."""
    if os.path.exists(SENT_FILE):
        try:
            return json.load(open(SENT_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_sent(sent: dict):
    try:
        with open(SENT_FILE, "w", encoding="utf-8") as f:
            json.dump(sent, f)
    except Exception:
        pass


def _purge_old_sent(sent: dict) -> dict:
    """Rimuove entry più vecchie di oggi (reset giornaliero automatico)."""
    today = str(date.today())
    return {k: v for k, v in sent.items() if v == today}


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def _ensure_csv():
    if not os.path.exists(OUT_CSV):
        os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
        with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()
        log.info(f"[bf] Creato {OUT_CSV}")


def _append_signals(signals: list):
    with open(OUT_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        for s in signals:
            w.writerow(s)


# ---------------------------------------------------------------------------
# Logica di scan
# ---------------------------------------------------------------------------
def _has_dex_pair(token_sym: str) -> bool:
    """
    Verifica se il token ha un pair su DexScreener.
    Se sì → è già gestito da gemmeV3 → skip.
    """
    try:
        r = requests.get(
            f"{DEXSCREENER}/latest/dex/search",
            params={"q": token_sym},
            timeout=10,
        )
        if r.status_code != 200:
            return False
        pairs = r.json().get("pairs") or []
        return any(
            p.get("baseToken", {}).get("symbol", "").upper() == token_sym.upper()
            for p in pairs
        )
    except Exception:
        return False


def scan_once(sent: dict) -> list:
    """
    Scansiona Binance Futures e ritorna lista di nuovi segnali da registrare.
    """
    today = str(date.today())

    # 1. Fetch ticker Binance Futures
    try:
        resp = requests.get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=15)
        resp.raise_for_status()
        tickers = resp.json()
    except Exception as e:
        log.warning(f"[bf] Fetch Binance fallito: {e}")
        return []

    # 2. Filtra candidati per volume e variazione
    candidates = []
    for t in tickers:
        sym = t.get("symbol", "")
        if sym in EXCLUDE or not sym.endswith("USDT"):
            continue
        try:
            vol24  = float(t.get("quoteVolume", 0) or 0)
            chg24h = float(t.get("priceChangePercent", 0) or 0)  # 24h change, solo positivo
            price  = float(t.get("lastPrice", 0) or 0)
        except Exception:
            continue
        # Solo long: variazione positiva tra MIN e MAX (non inseguire pump già esauriti)
        if vol24 < MIN_VOL_24H_USD or price <= 0:
            continue
        if chg24h < MIN_CHG_24H_PCT or chg24h > MAX_CHG_24H_PCT:
            continue
        tok = sym.replace("USDT", "").replace("PERP", "")
        candidates.append({
            "sym": sym, "tok": tok,
            "vol24": vol24, "chg24h": chg24h, "price": price,
        })

    # Ordina per momentum (volume × variazione)
    candidates.sort(key=lambda x: x["vol24"] * x["chg24h"], reverse=True)
    top = candidates[:MAX_CANDIDATES]

    new_signals = []
    for c in top:
        tok = c["tok"]
        sym = c["sym"]

        # Dedup giornaliero
        if sent.get(tok) == today:
            log.debug(f"[bf] {tok}: già segnalato oggi — skip")
            continue

        # Skip se esiste un pair DEX (gemmeV3 lo gestisce già)
        if _has_dex_pair(tok):
            log.debug(f"[bf] {tok}: ha pair DEX — gemmeV3 lo gestisce")
            continue

        # Costruisci segnale
        sid = f"BF_{tok}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        vol1h_est = c["vol24"] / 24.0   # stima vol 1h da vol 24h
        signal = {
            "signal_id":       sid,
            "timestamp_entry": datetime.now().isoformat(),
            "token_symbol":    tok,
            "chain":           "binance_futures",
            "pair_address":    sym,          # es. "PEPEUSDT" — usato per fetch prezzo
            "price_entry_usd": c["price"],
            "volume_1h_usd":   round(vol1h_est, 2),
            "binance_chg1h":   round(c["chg24h"], 2),
        }
        new_signals.append(signal)
        sent[tok] = today
        log.info(
            f"[bf] 📡 Nuovo segnale BNF: {tok} "
            f"(${c['price']:.6f}, vol24h=${c['vol24']:,.0f}, Δ24h={c['chg24h']:.1f}%)"
        )

    return new_signals


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    _ensure_csv()
    sent = _load_sent()
    sent = _purge_old_sent(sent)   # rimuovi entry di ieri
    log.info(f"[bf] Avviato. Scan ogni {SCAN_INTERVAL}s. CSV → {OUT_CSV}")

    while True:
        try:
            new = scan_once(sent)
            if new:
                _append_signals(new)
                _save_sent(sent)
                log.info(f"[bf] {len(new)} nuovi segnali scritti.")
            else:
                log.info("[bf] Scan completato — nessun nuovo segnale.")
        except Exception as e:
            log.error(f"[bf] Errore ciclo: {e}")

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
