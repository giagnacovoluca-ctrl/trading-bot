"""
pump_graduation_scanner.py
===========================
Scanner real-time per token appena graduati da pump.fun a Raydium.

Architettura:
  1. WebSocket PumpPortal → evento graduation in tempo reale (< 1s dal contratto)
  2. Attesa 90s → Raydium pool indicizzata su DexScreener
  3. DexScreener → valida BSR, liquidità, volume
  4. Filtri qualità → segnale scritto in signals_log.csv

API gratuita:
  wss://pumpportal.fun/api/data  — nessuna auth, rate limit generoso

Perché WebSocket > polling:
  - Evento graduation entro 1s (non 30-45s di polling)
  - Zero Cloudflare, nessun 530
  - Nessun limite chiamate
"""

from __future__ import annotations
import csv
import json
import logging
import os
import queue
import threading
import time
from collections import deque

try:
    from rugcheck import is_safe as rugcheck_safe
except ImportError:
    def rugcheck_safe(mint, scanner, chain="solana"): return True
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

try:
    import websocket
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False

# ── Email (stessa config di defi_optimized) ───────────────────────────────────
_EMAIL_CFG = {
    "SMTP_HOST":     os.environ.get("SMTP_HOST",     "smtp.gmail.com"),
    "SMTP_PORT":     int(os.environ.get("SMTP_PORT", "587")),
    "SMTP_USER":     os.environ.get("SMTP_USER",     "giagnacovo.luca@gmail.com"),
    "SMTP_PASSWORD": os.environ.get("SMTP_PASSWORD", ""),
    "FROM_ADDR":     os.environ.get("SMTP_FROM",     "giagnacovo.luca@gmail.com"),
    "TO_ADDR":       os.environ.get("SMTP_TO",       "giagnacovo.luca@gmail.com"),
}


def _send_graduation_email(sig: dict, pair: dict) -> bool:
    try:
        cfg   = _EMAIL_CFG
        sym   = sig["token_symbol"]
        mcap  = float((pair.get("marketCap") or pair.get("fdv") or 0))
        liq   = float((pair.get("liquidity") or {}).get("usd") or 0)
        vol1h = float((pair.get("volume") or {}).get("h1") or 0)
        bsr   = float(sig["buy_sell_ratio_1h"])
        chg   = float(sig["change_1h_pct"])
        price = sig["price_entry_usd"]
        age_m = float(sig["top_features"].split("pair_age_min=")[1].split(" |")[0]) \
                if "pair_age_min=" in sig["top_features"] else 0

        subject = f"🚀 [PUMP GRAD] {sym} | mcap=${mcap:,.0f} | bsr={bsr:.2f} | {chg:+.1f}%"
        body = f"""
<html><body style="font-family:monospace;background:#0d1117;color:#e6edf3;padding:20px">
<h2 style="color:#f0883e">🚀 Pump.fun Graduation: {sym}</h2>
<table style="border-collapse:collapse">
  <tr><td style="color:#8b949e;padding:4px 12px">Token</td><td><b>{sym}</b> — {sig['token_name']}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Mint</td><td style="font-size:.85rem">{sig['token_address']}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Età su Raydium</td><td>{age_m:.0f} min</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Prezzo</td><td>${price}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Market Cap</td><td>${mcap:,.0f}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Liquidità</td><td>${liq:,.0f}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Volume 1h</td><td>${vol1h:,.0f}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">BSR 1h</td><td style="color:{'#3fb950' if bsr>=1.2 else '#e6edf3'}">{bsr:.3f}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Change 1h</td><td style="color:{'#3fb950' if chg>0 else '#f85149'}">{chg:+.1f}%</td></tr>
</table>
<p style="margin-top:16px;color:#8b949e;font-size:.8rem">
  TP1 +30% · Trail attivo a +15% · Hard SL -12%<br>
  Signal ID: {sig['signal_id']}
</p>
</body></html>"""

        try:
            import email_digest
            email_digest.queue_email("pump_grad", subject, body)
            log.info(f"[pump_grad] 📥 Segnale accodato al digest email: {sym}")
            return True
        except ImportError:
            pass   # standalone: invio diretto

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = cfg["FROM_ADDR"]
        msg["To"]      = cfg["TO_ADDR"]
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15) as srv:
            srv.starttls()
            srv.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            srv.sendmail(cfg["FROM_ADDR"], cfg["TO_ADDR"], msg.as_string())
        log.info(f"[pump_grad] 📧 Email inviata: {sym}")
        return True
    except Exception as e:
        log.warning(f"[pump_grad] Email error: {e}")
        return False

log = logging.getLogger("pump_grad")

# ── Percorsi ─────────────────────────────────────────────────────────────────
_HERE        = Path(__file__).parent
SIGNALS_CSV  = _HERE / "reports" / "pump_grad_signals.csv"
STATE_FILE   = _HERE / "reports" / "pump_grad_state.json"
SIGNALS_CSV.parent.mkdir(parents=True, exist_ok=True)

PUMPPORTAL_WS  = "wss://pumpportal.fun/api/data"
DEXSCREENER    = "https://api.dexscreener.com"

# ── Configurazione ────────────────────────────────────────────────────────────
CONFIG = {
    # Quanto aspettare dopo l'evento graduation prima di chiamare DexScreener
    # (la pool Raydium impiega 60-120s a essere indicizzata)
    "DEX_WAIT_SEC":       15,   # ridotto 90→15: Jupiter indicizza la pool subito, non aspettiamo DexScreener

    # Tentativi DexScreener se la pool non è ancora indicizzata
    "DEX_MAX_RETRIES":     4,
    "DEX_RETRY_INTERVAL": 30,

    # Filtri qualità (post-DexScreener)
    "MIN_LIQ_USD":      15_000,
    "MIN_VOL_1H_USD":    2_000,   # volume 1h minimo
    "MIN_BSR":             1.0,   # buyers > sellers
    "MAX_CHANGE_1H_PCT":   60.0,  # già pompato troppo: graduation mcap ~$40-45K, a +60% = $65-72K → near peak
    "MIN_CHANGE_1H_PCT":  -20.0,  # in dump dopo graduation
    "MIN_TXNS_1H":          15,   # attività reale
    "MAX_PAIR_AGE_MIN":  1_440,   # pair >24h → non è una graduation fresca

    # Dedup
    "SEEN_TTL_HOURS":      24,

    # Reconnect WebSocket dopo X secondi di silenzio (keepalive)
    "WS_KEEPALIVE_SEC":   120,
}

# Mint di token noti (stablecoin, wrapped SOL, ecc.) che non possono essere
# token pump.fun — blocco hard indipendentemente dall'evento WebSocket.
_KNOWN_MINTS_BLOCKLIST = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
    "So11111111111111111111111111111111111111112",      # Wrapped SOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",   # ETH (Wormhole)
    "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E",   # BTC (Wormhole)
}

# ── CSV ───────────────────────────────────────────────────────────────────────
_CSV_FIELDS = [
    "signal_id", "timestamp_entry", "token_symbol", "token_name",
    "token_address", "chain", "pair_address", "price_entry_usd",
    "volume_1h_usd", "liquidity_usd", "buy_sell_ratio_1h", "change_1h_pct",
    "pump_probability", "buy_tax", "sell_tax", "lp_locked", "is_honeypot",
    "top_features",
]
_csv_lock = threading.Lock()


def _ensure_csv():
    if not SIGNALS_CSV.exists() or SIGNALS_CSV.stat().st_size == 0:
        with open(SIGNALS_CSV, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_CSV_FIELDS).writeheader()


def _write_signal(row: dict):
    _ensure_csv()
    with _csv_lock:
        with open(SIGNALS_CSV, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore").writerow(row)


# ── Seen / dedup ──────────────────────────────────────────────────────────────
_seen: dict[str, datetime] = {}
_seen_lock = threading.Lock()


def _load_seen():
    global _seen
    try:
        if STATE_FILE.exists():
            raw = json.loads(STATE_FILE.read_text())
            cutoff = datetime.now() - timedelta(hours=CONFIG["SEEN_TTL_HOURS"])
            _seen = {k: datetime.fromisoformat(v) for k, v in raw.items()
                     if datetime.fromisoformat(v) > cutoff}
    except Exception:
        _seen = {}


def _save_seen():
    try:
        STATE_FILE.write_text(
            json.dumps({k: v.isoformat() for k, v in _seen.items()}, indent=2)
        )
    except Exception:
        pass


def _is_seen(mint: str) -> bool:
    with _seen_lock:
        ts = _seen.get(mint)
        if ts and datetime.now() - ts < timedelta(hours=CONFIG["SEEN_TTL_HOURS"]):
            return True
        if ts:
            del _seen[mint]
        return False


def _mark_seen(mint: str):
    with _seen_lock:
        _seen[mint] = datetime.now()


# ── DexScreener ───────────────────────────────────────────────────────────────
_dex_last = 0.0


def _dex_get(url: str, params: dict = None) -> Optional[dict]:
    global _dex_last
    wait = 1.2 - (time.time() - _dex_last)
    if wait > 0:
        time.sleep(wait)
    try:
        r = requests.get(url, params=params, timeout=12,
                         headers={"User-Agent": "Mozilla/5.0"})
        _dex_last = time.time()
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.debug(f"[pump_grad] dex fetch: {e}")
    return None


def _fetch_dex_pair(mint: str) -> Optional[dict]:
    """
    Cerca la Raydium pair del token su DexScreener.
    Ritorna la pair con maggiore liquidità su Raydium o PumpSwap.
    Pump.fun gradua su PumpSwap (dexId="pumpswap") dal 2025, non più su Raydium v4.
    """
    data = _dex_get(f"{DEXSCREENER}/tokens/v1/solana/{mint}")
    if not data:
        return None
    pairs = data if isinstance(data, list) else data.get("pairs") or []
    # Accetta Raydium e PumpSwap; esclude la bonding curve (dexId="pump.fun")
    amm = [p for p in pairs
           if ("raydium" in (p.get("dexId") or "").lower()
               or "pumpswap" in (p.get("dexId") or "").lower())
           and float((p.get("liquidity") or {}).get("usd") or 0) > 0]
    if not amm:
        return None
    return max(amm, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))


def _bsr(txns: dict) -> float:
    try:
        h1 = txns.get("h1", {})
        b = int(h1.get("buys", 0) or 0)
        s = int(h1.get("sells", 0) or 0)
        return b / (b + s) * 2 if (b + s) >= 5 else 1.0
    except Exception:
        return 1.0


# ── Validazione e segnale ─────────────────────────────────────────────────────

def _validate_and_signal(event: dict) -> bool:
    """
    Ricevuto l'evento graduation: aspetta, poi valida con DexScreener.
    Ritorna True se il segnale è stato emesso.
    """
    mint   = event.get("mint", "")
    symbol = event.get("symbol", "?").upper()
    name   = event.get("name", symbol)

    if not mint or _is_seen(mint):
        return False
    if mint in _KNOWN_MINTS_BLOCKLIST:
        log.warning(f"[pump_grad] _validate: mint bloccato {mint[:16]}… — skip")
        return False

    _mark_seen(mint)

    log.info(f"[pump_grad] 🎓 Graduation: {symbol} ({mint[:16]}…) — verifica Jupiter poi DexScreener")

    # 1. Attendi che Jupiter abbia la route (pool on-chain già esiste)
    # Jupiter è molto più veloce di DexScreener: retry ogni 3s, max 30s
    _JUP_QUOTE = "https://api.jup.ag/swap/v1/quote"
    _USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    jup_ready = False
    for _jup_try in range(10):
        try:
            jr = requests.get(_JUP_QUOTE, params={
                "inputMint": mint, "outputMint": _USDC_MINT,
                "amount": "1000000", "slippageBps": "1000",
            }, timeout=5)
            if jr.status_code == 200 and "outAmount" in jr.json():
                jup_ready = True
                log.info(f"[pump_grad] {symbol}: Jupiter pool pronta dopo {_jup_try*3}s")
                break
        except Exception:
            pass
        time.sleep(3)

    if not jup_ready:
        log.info(f"[pump_grad] {symbol}: nessuna route Jupiter dopo 30s — aspetto DexScreener")

    # 2. Attesa minima per DexScreener (ridotta da 90s a 15s se Jupiter già ok)
    _wait = CONFIG["DEX_WAIT_SEC"] if not jup_ready else max(5, CONFIG["DEX_WAIT_SEC"] - 10)
    time.sleep(_wait)

    pair = None
    for attempt in range(1, CONFIG["DEX_MAX_RETRIES"] + 1):
        pair = _fetch_dex_pair(mint)
        if pair:
            break
        log.debug(f"[pump_grad] {symbol}: DexScreener tentativo {attempt}/{CONFIG['DEX_MAX_RETRIES']} — pool non ancora indicizzata")
        if attempt < CONFIG["DEX_MAX_RETRIES"]:
            time.sleep(CONFIG["DEX_RETRY_INTERVAL"])

    if not pair:
        log.info(f"[pump_grad] {symbol or mint[:8]}: nessuna pool AMM trovata dopo {CONFIG['DEX_MAX_RETRIES']} tentativi — skip")
        return False

    # Recupera simbolo e nome da DexScreener se PumpPortal non li ha inviati
    base = pair.get("baseToken") or {}
    if not symbol or symbol == "?":
        symbol = base.get("symbol", mint[:8])
    if not name or name == symbol:
        name = base.get("name", symbol)
    dex_id = pair.get("dexId", "?")

    # ── Filtri qualità ───────────────────────────────────────────────────────
    cfg   = CONFIG
    liq   = float((pair.get("liquidity") or {}).get("usd") or 0)
    vol1h = float((pair.get("volume") or {}).get("h1") or 0)
    bsr   = _bsr(pair.get("txns") or {})
    chg   = float((pair.get("priceChange") or {}).get("h1") or 0)
    price = float(pair.get("priceUsd") or 0)
    mcap  = float(pair.get("marketCap") or pair.get("fdv") or 0)
    h1_t  = pair.get("txns", {}).get("h1", {})
    txns  = int(h1_t.get("buys", 0) or 0) + int(h1_t.get("sells", 0) or 0)
    age_min = (datetime.now().timestamp() * 1000 - (pair.get("pairCreatedAt") or 0)) / 60_000

    # ── Stability check: ri-fetcha dopo 8s (ridotto da 20s) ──
    # Pattern rug: liq drain rapido nei primi secondi dopo graduation.
    time.sleep(8)
    _pair2 = _fetch_dex_pair(mint)
    if _pair2:
        _liq2 = float((_pair2.get("liquidity") or {}).get("usd") or 0)
        if liq > 0 and _liq2 < liq * 0.80:
            log.info(f"[pump_grad] {symbol}: liq drain ${liq:,.0f}→${_liq2:,.0f} "
                     f"({_liq2/liq*100:.0f}% in 20s) — rug in corso? skip")
            return False
        # Aggiorna con dati freschi
        liq   = _liq2
        vol1h = float((_pair2.get("volume") or {}).get("h1") or 0)
        bsr   = _bsr(_pair2.get("txns") or {})
        chg   = float((_pair2.get("priceChange") or {}).get("h1") or 0)
        price = float(_pair2.get("priceUsd") or 0)
        mcap  = float(_pair2.get("marketCap") or _pair2.get("fdv") or 0)
        h1_t  = _pair2.get("txns", {}).get("h1", {})
        txns  = int(h1_t.get("buys", 0) or 0) + int(h1_t.get("sells", 0) or 0)
        age_min = (datetime.now().timestamp() * 1000 - (_pair2.get("pairCreatedAt") or 0)) / 60_000
        pair = _pair2

    rejects = []
    if liq   < cfg["MIN_LIQ_USD"]:           rejects.append(f"liq=${liq:,.0f}")
    if vol1h < cfg["MIN_VOL_1H_USD"]:        rejects.append(f"vol1h=${vol1h:,.0f}")
    if bsr   < cfg["MIN_BSR"]:               rejects.append(f"bsr={bsr:.2f}")
    if chg   > cfg["MAX_CHANGE_1H_PCT"]:     rejects.append(f"chg={chg:+.0f}% già pompato")
    if chg   < cfg["MIN_CHANGE_1H_PCT"]:     rejects.append(f"chg={chg:+.0f}% dump")
    if txns  < cfg["MIN_TXNS_1H"]:           rejects.append(f"txns={txns}")
    if age_min > cfg["MAX_PAIR_AGE_MIN"]:    rejects.append(f"age={age_min:.0f}min>{cfg['MAX_PAIR_AGE_MIN']}min (non graduation)")

    if rejects:
        log.info(f"[pump_grad] {symbol} [{dex_id}]: filtro → {', '.join(rejects)}")
        return False

    # ── Costruzione segnale ──────────────────────────────────────────────────
    now       = datetime.now()
    sid       = f"{symbol}_{now.strftime('%Y%m%d_%H%M%S')}"
    pair_addr = pair.get("pairAddress", "")
    vol_5m    = float((pair.get("volume") or {}).get("m5") or 0)
    vol_accel = (vol_5m * 12) / vol1h if vol1h > 0 else 1.0
    pump_prob = min(0.95, (bsr - 1.0) * 0.3 + min(vol_accel, 3.0) * 0.15 + 0.30)

    top_features = (
        f"pump_graduation=true | "
        f"pair_age_min={age_min:.0f} | "
        f"bsr_1h={bsr:.2f} | "
        f"vol_accel={vol_accel:.2f} | "
        f"mcap_usd={mcap:,.0f}"
    )

    sig = {
        "signal_id":         sid,
        "timestamp_entry":   now.isoformat(),
        "token_symbol":      symbol,
        "token_name":        name,
        "token_address":     mint,
        "chain":             "solana",
        "pair_address":      pair_addr,
        "price_entry_usd":   f"{price:.10g}",
        "volume_1h_usd":     f"{vol1h:.2f}",
        "liquidity_usd":     f"{liq:.2f}",
        "buy_sell_ratio_1h": f"{bsr:.3f}",
        "change_1h_pct":     f"{chg:.2f}",
        "pump_probability":  f"{pump_prob:.4f}",
        "buy_tax":           "0.0",
        "sell_tax":          "0.0",
        "lp_locked":         "0",
        "is_honeypot":       "0",
        "top_features":      top_features,
    }
    if not rugcheck_safe(mint, "pump_grad", chain="solana"):
        log.info(f"[pump_grad] {symbol}: rugcheck fallito → skip segnale")
        return False

    _write_signal(sig)
    _send_graduation_email(sig, pair)

    log.info(
        f"[pump_grad] ✅ SEGNALE {symbol} [{dex_id}] | "
        f"age={age_min:.0f}min | mcap=${mcap:,.0f} | liq=${liq:,.0f} | "
        f"bsr={bsr:.2f} | vol1h=${vol1h:,.0f} | chg={chg:+.1f}%"
    )
    return True


# ── WebSocket listener ────────────────────────────────────────────────────────

class PumpGraduationScanner:
    """
    Scanner WebSocket per graduation events pump.fun.
    Gira in background come thread daemon.
    """

    def __init__(self):
        _load_seen()
        _ensure_csv()
        self._stop       = threading.Event()
        self._ws_thread  = None
        self._val_queue  = queue.Queue()    # graduation events da validare
        self._last_msg   = time.time()      # keepalive tracker

    def start(self):
        if not WS_AVAILABLE:
            log.warning("[pump_grad] websocket-client non installato — scanner disabilitato. "
                        "Esegui: pip install websocket-client")
            return
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True, name="pump_ws")
        self._ws_thread.start()
        val_thread = threading.Thread(target=self._validation_loop, daemon=True, name="pump_val")
        val_thread.start()
        log.info("[pump_grad] Scanner avviato — in ascolto su graduation events pump.fun")

    def stop(self):
        self._stop.set()
        _save_seen()

    # ── WebSocket ─────────────────────────────────────────────────────────────

    def _ws_loop(self):
        """Connette al WebSocket con auto-reconnect."""
        while not self._stop.is_set():
            try:
                ws = websocket.WebSocketApp(
                    PUMPPORTAL_WS,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                log.warning(f"[pump_grad] WebSocket error: {e}")
            if not self._stop.is_set():
                log.info("[pump_grad] Reconnessione tra 10s...")
                time.sleep(10)

    def _on_open(self, ws):
        self._last_msg = time.time()
        ws.send(json.dumps({"method": "subscribeMigration"}))
        log.info("[pump_grad] WebSocket connesso — iscritto a subscribeMigration")

    def _on_message(self, ws, raw: str):
        self._last_msg = time.time()
        try:
            msg = json.loads(raw)
        except Exception:
            return

        # Messaggio di sistema (es. "Subscribed to...")
        if "message" in msg and "mint" not in msg:
            log.debug(f"[pump_grad] WS: {msg.get('message')}")
            return

        # Evento graduation reale: ha 'mint' + dati bonding curve
        mint   = msg.get("mint", "")
        symbol = msg.get("symbol", "")
        if not mint:
            return
        if mint in _KNOWN_MINTS_BLOCKLIST:
            log.warning(f"[pump_grad] Mint bloccato (token noto): {symbol or '?'} {mint[:16]}…")
            return
        log.info(f"[pump_grad] 🎓 Evento graduation: {symbol or '?'} ({mint[:16]}…)")
        self._val_queue.put(msg)

    def _on_error(self, ws, err):
        log.warning(f"[pump_grad] WS error: {err}")

    def _on_close(self, ws, code, msg):
        log.info(f"[pump_grad] WS chiuso (code={code})")

    # ── Validation loop ───────────────────────────────────────────────────────

    def _validation_loop(self):
        """
        Consuma gli eventi dalla queue e li valida con DexScreener.
        Gira in thread separato per non bloccare il WebSocket.
        """
        while not self._stop.is_set():
            try:
                event = self._val_queue.get(timeout=5)
                _validate_and_signal(event)
                self._val_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                log.error(f"[pump_grad] validation error: {e}")

    @property
    def queue_size(self) -> int:
        return self._val_queue.qsize()


# ── Standalone ────────────────────────────────────────────────────────────────

def main_loop():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not WS_AVAILABLE:
        print("Installa websocket-client: pip install websocket-client")
        return

    scanner = PumpGraduationScanner()
    scanner.start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("[pump_grad] Stop.")
        scanner.stop()


if __name__ == "__main__":
    main_loop()
