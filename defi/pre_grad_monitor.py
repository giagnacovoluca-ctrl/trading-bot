"""
pre_grad_monitor.py
===================
Monitor pre-graduation pump.fun — intercetta token PRIMA del pump post-graduation.

Problema del scanner standard:
  graduation event → attesa 90s (DexScreener) → compra al +30-80%

Questa soluzione:
  1. subscribeNewToken    → scopre token con initial buy serio (>= INITIAL_BUY_SOL)
  2. subscribeTokenTrade  → traccia vSolInBondingCurve in real-time
  3. subscribeMigration   → se token è in watchlist → segnale in 0-5s (no 90s)

Risultato: entry al prezzo di graduation (+0-10%) invece che al pump (+30-80%).
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
from datetime import datetime
from pathlib import Path
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

_EMAIL_CFG = {
    "SMTP_HOST":     os.environ.get("SMTP_HOST",     "smtp.gmail.com"),
    "SMTP_PORT":     int(os.environ.get("SMTP_PORT", "587")),
    "SMTP_USER":     os.environ.get("SMTP_USER",     "giagnacovo.luca@gmail.com"),
    "SMTP_PASSWORD": os.environ.get("SMTP_PASSWORD", ""),
    "FROM_ADDR":     os.environ.get("SMTP_FROM",     "giagnacovo.luca@gmail.com"),
    "TO_ADDR":       os.environ.get("SMTP_TO",       "giagnacovo.luca@gmail.com"),
}


def _send_pre_grad_email(sig: dict) -> bool:
    try:
        cfg    = _EMAIL_CFG
        sym    = sig["token_symbol"]
        feats  = sig.get("top_features", "")
        v_sol  = feats.split("vSol=")[1].split(" |")[0]  if "vSol="  in feats else "?"
        vel    = feats.split("velocity=")[1].split(" |")[0] if "velocity=" in feats else "?"
        mcap   = feats.split("mcap=$")[1].split(" ")[0]  if "mcap=$"  in feats else "0"
        liq    = float(sig.get("liquidity_usd", 0) or 0)
        price  = sig.get("price_entry_usd", "?")

        subject = f"⚡ [PRE-GRAD] {sym} | vSol={v_sol} | vel={vel} | mcap=${mcap}"
        body = f"""
<html><body style="font-family:monospace;background:#0d1117;color:#e6edf3;padding:20px">
<h2 style="color:#58a6ff">⚡ Pre-Graduation Signal: {sym}</h2>
<p style="color:#8b949e;font-size:.85rem">Token ancora sulla bonding curve — graduation imminente</p>
<table style="border-collapse:collapse">
  <tr><td style="color:#8b949e;padding:4px 12px">Token</td><td><b>{sym}</b></td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Mint</td><td style="font-size:.85rem">{sig['token_address']}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">vSol Bonding Curve</td><td style="color:#3fb950">{v_sol}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Velocity</td><td style="color:#3fb950">{vel}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Prezzo BC</td><td>${price}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Liquidità (stima)</td><td>${liq:,.0f}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Market Cap</td><td>${mcap}</td></tr>
</table>
<p style="margin-top:16px;color:#8b949e;font-size:.8rem">
  TP1 +40% · SL -12% · Exit se no graduation in 20 min<br>
  Signal ID: {sig['signal_id']}
</p>
</body></html>"""

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = cfg["FROM_ADDR"]
        msg["To"]      = cfg["TO_ADDR"]
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15) as srv:
            srv.starttls()
            srv.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
            srv.sendmail(cfg["FROM_ADDR"], cfg["TO_ADDR"], msg.as_string())
        log.info(f"[pre_grad] 📧 Email inviata: {sym}")
        return True
    except Exception as e:
        log.warning(f"[pre_grad] Email error: {e}")
        return False

try:
    from rugcheck import is_safe as rugcheck_safe
except ImportError:
    def rugcheck_safe(mint, scanner, chain="solana"): return True

log = logging.getLogger("pre_grad")

_PUMPPORTAL_APIKEY = os.environ.get("PUMPPORTAL_APIKEY", "")
PUMPPORTAL_WS  = (
    f"wss://pumpportal.fun/api/data?api-key={_PUMPPORTAL_APIKEY}"
    if _PUMPPORTAL_APIKEY else "wss://pumpportal.fun/api/data"
)
DEXSCREENER    = "https://api.dexscreener.com"

# ── Configurazione ────────────────────────────────────────────────────────────
CFG = {
    # Filtra new token: solo se l'initial buy >= questa soglia (SOL)
    # 0.01 SOL: soglia minima anti-spam puro, cattura token con buy piccolo che poi pompano
    "INITIAL_BUY_SOL":    0.01,

    # Inizia a tracciare il token quando vSolInBondingCurve supera questa soglia
    "TRACK_WARN_SOL":    55.0,

    # Soglia "prossimo alla graduation" → rugcheck preventivo
    "TRACK_HOT_SOL":     60.0,  # era 72 — più margine prima di graduation (88 SOL)

    # Graduation avviene intorno a questa soglia (varia 79-85 SOL)
    "GRAD_SOL":          80.0,

    # Max token tracciati contemporaneamente (WebSocket overhead)
    # Alzato a 150: con INITIAL_BUY_SOL=0.1 arrivano più token
    "MAX_TRACKED":       150,

    # Dopo quanto tempo rimuovere token dalla watchlist senza graduation
    "WATCHLIST_TTL_SEC": 5400,   # 90 minuti — bonding curve può richiedere ore

    # Retry Jupiter per verificare che la pool esista (ogni N sec, max tentativi)
    "JUPITER_RETRY_SEC":  4,
    "JUPITER_MAX_RETRY": 10,     # 40s massimo

    # Filtri segnale (allineati al graduation scanner esistente)
    "MIN_LIQ_USD":      12_000,
    "MIN_VOL_1H_USD":    1_500,
    "MAX_CHANGE_1H_PCT": 80.0,   # più stretto: già pompato troppo → skip
}

# Signals CSV
_BASE             = os.path.dirname(os.path.abspath(__file__))
_SIGNALS_CSV      = os.path.join(_BASE, "reports", "pump_grad_signals.csv")   # post-graduation
_PRE_GRAD_CSV     = os.path.join(_BASE, "reports", "pre_grad_signals.csv")    # pre-graduation
_SEEN_FILE        = os.path.join(_BASE, "reports", "pre_grad_seen.json")
_WATCHLIST_FILE   = os.path.join(_BASE, "reports", "pre_grad_watchlist.json") # persistenza tra restart

PRE_GRAD_ENABLED  = os.environ.get("PRE_GRAD_ENABLED", "true").lower() != "false"
_WATCHLIST_TTL_SEC = CFG["WATCHLIST_TTL_SEC"]   # token scaduti non vengono ricaricati

_PRE_GRAD_CSV_HEADER = [
    "signal_id", "timestamp_entry", "token_symbol", "token_name",
    "token_address", "chain", "pair_address", "price_entry_usd",
    "volume_1h_usd", "liquidity_usd", "buy_sell_ratio_1h",
    "change_1h_pct", "pump_probability", "buy_tax", "sell_tax",
    "lp_locked", "is_honeypot", "top_features",
]

# ── Stato condiviso ───────────────────────────────────────────────────────────
_lock       = threading.Lock()
_watchlist: dict[str, dict] = {}   # mint → {symbol, v_sol, ts_first, ts_hot, rugcheck_ok, v_sol_history}
_seen_mints: set[str] = set()      # già segnalati post-graduation (dedup)
_pre_grad_signaled: set[str] = set()  # già segnalati pre-graduation (dedup)
_evicted_vsol: dict[str, float] = {}  # mint → ultimo v_sol noto prima dell'eviction (max 500 entry)


def _load_seen():
    global _seen_mints
    try:
        data = json.loads(Path(_SEEN_FILE).read_text())
        _seen_mints = set(data.get("mints", []))
    except Exception:
        _seen_mints = set()


def _save_seen():
    try:
        Path(_SEEN_FILE).write_text(json.dumps({"mints": list(_seen_mints)}, indent=2))
    except Exception:
        pass


def _save_watchlist():
    """Persiste la watchlist su disco per sopravvivere ai restart di run.py."""
    try:
        now = time.time()
        serializable = {}
        for mint, entry in _watchlist.items():
            if now - entry.get("ts_first", now) > _WATCHLIST_TTL_SEC:
                continue   # non salvare token scaduti
            serializable[mint] = {
                "symbol":      entry.get("symbol", "?"),
                "v_sol":       entry.get("v_sol", 0),
                "ts_first":    entry.get("ts_first", now),
                "ts_hot":      entry.get("ts_hot"),
                "rugcheck_ok": entry.get("rugcheck_ok"),
                # v_sol_history: non serializzabile (deque con tuple) — si ricostruisce online
            }
        Path(_WATCHLIST_FILE).write_text(json.dumps(serializable, indent=2))
    except Exception:
        pass


def _load_watchlist():
    """Ricarica la watchlist dal disco al riavvio. Esclude token scaduti."""
    global _watchlist
    try:
        data = json.loads(Path(_WATCHLIST_FILE).read_text())
        now = time.time()
        loaded = 0
        for mint, entry in data.items():
            ts_first = float(entry.get("ts_first", 0))
            if now - ts_first > _WATCHLIST_TTL_SEC:
                continue   # scaduto
            _watchlist[mint] = {
                "symbol":       entry.get("symbol", "?"),
                "v_sol":        float(entry.get("v_sol", 0)),
                "ts_first":     ts_first,
                "ts_hot":       entry.get("ts_hot"),
                "rugcheck_ok":  entry.get("rugcheck_ok"),
                "v_sol_history": deque(maxlen=30),
            }
            loaded += 1
        if loaded:
            log.info(f"[pre_grad] 📂 Watchlist ricaricata: {loaded} token dal disco")
    except Exception:
        pass


def _ensure_csv():
    path = Path(_SIGNALS_CSV)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                "signal_id", "timestamp_entry", "token_symbol", "mint_type",
                "token_address", "chain", "pair_address",
                "price_entry_usd", "volume_1h_usd", "liquidity_usd",
                "bsr_1h", "change_1h_pct", "mcap_usd", "pair_age_min",
                "source", "signals",
            ])


# ── Pre-grad signals CSV ──────────────────────────────────────────────────────

def _ensure_pre_grad_csv():
    path = Path(_PRE_GRAD_CSV)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(_PRE_GRAD_CSV_HEADER)


def _fetch_pumpfun_price(mint: str) -> Optional[dict]:
    """
    Legge prezzo e stato della bonding curve pump.fun.
    Ritorna {price_usd, liq_usd, graduated, raydium_pool} oppure None.
    """
    try:
        r = requests.get(
            f"https://frontend-api.pump.fun/coins/{mint}",
            timeout=6,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code != 200:
            return None
        d = r.json()
        v_sol  = float(d.get("virtual_sol_reserves", 0) or 0) / 1e9
        v_tok  = float(d.get("virtual_token_reserves", 0) or 0) / 1e6
        mcap   = float(d.get("usd_market_cap", 0) or 0)
        graduated = bool(d.get("complete", False))
        pool   = d.get("raydium_pool") or ""
        if v_tok <= 0:
            return None
        sol_price = _get_sol_price_usd()  # usa cache da solana_executor
        price_usd = (v_sol / v_tok) * sol_price if sol_price > 0 else 0.0
        liq_usd   = v_sol * sol_price * 2 if sol_price > 0 else 0.0
        return {
            "price_usd":   price_usd,
            "liq_usd":     liq_usd,
            "mcap_usd":    mcap,
            "v_sol":       v_sol,
            "graduated":   graduated,
            "raydium_pool": pool,
        }
    except Exception as e:
        log.debug(f"[pre_grad] pump.fun API {mint[:8]}: {e}")
        return None


_pf_sol_price_cache: dict = {"price": 0.0, "ts": 0.0}


def _get_sol_price_usd() -> float:
    """SOL price USD, cache 5 min."""
    if time.time() - _pf_sol_price_cache["ts"] < 300 and _pf_sol_price_cache["price"] > 0:
        return _pf_sol_price_cache["price"]
    try:
        r = requests.get(
            "https://api.dexscreener.com/tokens/v1/solana/So11111111111111111111111111111111111111112",
            timeout=5,
        )
        if r.status_code == 200:
            pairs = r.json() if isinstance(r.json(), list) else []
            for p in pairs:
                if "usdc" in (p.get("quoteToken", {}).get("symbol") or "").lower():
                    price = float(p.get("priceUsd") or 0)
                    if price > 0:
                        _pf_sol_price_cache["price"] = price
                        _pf_sol_price_cache["ts"] = time.time()
                        return price
    except Exception:
        pass
    return _pf_sol_price_cache["price"] or 180.0


def _emit_pre_grad_signal(mint: str, symbol: str, v_sol: float, velocity_sol_min: float,
                          dex_pair: Optional[dict] = None):
    """Scrive segnale pre-graduation su pre_grad_signals.csv.

    `dex_pair`: oggetto pair DexScreener già disponibile (path "scoperto da poll
    via pair su DEX") — usato come fonte prezzo/liq/pair_address al posto della
    pump.fun API (morta, sempre 404 → price_usd=0 → simulator scarta il segnale
    come non tracciabile, vedi caso BILLY 07/06).
    """
    if not PRE_GRAD_ENABLED:
        return
    with _lock:
        if mint in _pre_grad_signaled:
            return
        _pre_grad_signaled.add(mint)

    pair_address = ""
    if dex_pair:
        price_usd    = float(dex_pair.get("priceUsd") or 0)
        liq_usd      = float((dex_pair.get("liquidity") or {}).get("usd", 0) or 0)
        mcap_usd     = float(dex_pair.get("marketCap") or dex_pair.get("fdv") or 0)
        pair_address = str(dex_pair.get("pairAddress") or "")
    else:
        pf = _fetch_pumpfun_price(mint)
        price_usd = pf["price_usd"] if pf else 0.0
        liq_usd   = pf["liq_usd"]   if pf else v_sol * _get_sol_price_usd() * 2
        mcap_usd  = pf["mcap_usd"]  if pf else 0.0

    sid = f"PG_{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    row = {
        "signal_id":         sid,
        "timestamp_entry":   datetime.now().isoformat(),
        "token_symbol":      symbol,
        "token_name":        symbol,
        "token_address":     mint,
        "chain":             "solana",
        "pair_address":      pair_address,  # popolato solo se scoperto già su DEX
        "price_entry_usd":   f"{price_usd:.10g}" if price_usd else "0",
        "volume_1h_usd":     "0",
        "liquidity_usd":     f"{liq_usd:.2f}",
        "buy_sell_ratio_1h": "1.000",  # solo buy sulla bonding curve in questo momento
        "change_1h_pct":     "0.00",
        "pump_probability":  "0.88",   # a 72 SOL ~85-90% graduation probability
        "buy_tax":           "0.0",
        "sell_tax":          "0.0",
        "lp_locked":         "0",
        "is_honeypot":       "0",
        "top_features":      (
            f"pre_grad=true | vSol={v_sol:.1f} | entry_vsol={v_sol:.2f} | "
            f"velocity=+{velocity_sol_min:.2f}SOL/min | "
            f"mcap=${mcap_usd:,.0f}"
        ),
    }
    with open(_PRE_GRAD_CSV, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=_PRE_GRAD_CSV_HEADER).writerow(row)
    log.info(
        f"[pre_grad] 🚀 PRE-GRAD SIGNAL {symbol} | "
        f"vSol={v_sol:.1f} | vel=+{velocity_sol_min:.2f}SOL/min | "
        f"price=${price_usd:.8g} | liq=${liq_usd:,.0f}"
    )
    _send_pre_grad_email(row)


# ── DexScreener ───────────────────────────────────────────────────────────────

def _dex_get(url: str, params: dict = None) -> Optional[dict]:
    try:
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _fetch_pair(mint: str) -> Optional[dict]:
    """Fetch del pair Raydium/PumpSwap (esclude bonding curve pump.fun)."""
    data = _dex_get(f"{DEXSCREENER}/tokens/v1/solana/{mint}")
    if not data:
        return None
    pairs = data if isinstance(data, list) else data.get("pairs", [])
    valid = [
        p for p in (pairs or [])
        if p.get("dexId") in ("raydium", "pumpswap", "meteora")
        and float((p.get("liquidity") or {}).get("usd", 0) or 0) > 5_000
    ]
    if not valid:
        return None
    return max(valid, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))


def _bsr(pair: dict) -> float:
    txns = pair.get("txns", {}).get("h1", {})
    b, s = int(txns.get("buys", 0)), int(txns.get("sells", 0))
    return b / (b + s) if b + s > 0 else 1.0


# ── Jupiter check ─────────────────────────────────────────────────────────────

_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_JUP  = "https://api.jup.ag/swap/v1/quote"


def _jupiter_pool_exists(mint: str) -> bool:
    """Verifica che Jupiter abbia una route per il token → pool indicizzata."""
    try:
        r = requests.get(_JUP, params={
            "inputMint": mint, "outputMint": _USDC,
            "amount": "1000000", "slippageBps": "1000",
        }, timeout=6)
        return r.status_code == 200 and "outAmount" in r.json()
    except Exception:
        return False


# ── Segnale immediato ─────────────────────────────────────────────────────────

def _write_signal(row: dict):
    with open(_SIGNALS_CSV, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=[
            "signal_id", "timestamp_entry", "token_symbol", "mint_type",
            "token_address", "chain", "pair_address",
            "price_entry_usd", "volume_1h_usd", "liquidity_usd",
            "bsr_1h", "change_1h_pct", "mcap_usd", "pair_age_min",
            "source", "signals",
        ]).writerow(row)


def _signal_immediate(mint: str, symbol: str, v_sol: float):
    """
    Graduation di un token in watchlist:
    attende che Jupiter abbia la route (max JUPITER_MAX_RETRY * JUPITER_RETRY_SEC)
    poi valida con DexScreener e scrive il segnale.
    """
    if mint in _seen_mints:
        return
    _seen_mints.add(mint)

    log.info(f"[pre_grad] ⚡ {symbol} in watchlist ({v_sol:.1f} SOL) — "
             f"attendo Jupiter (max {CFG['JUPITER_MAX_RETRY']*CFG['JUPITER_RETRY_SEC']}s)")

    # Retry Jupiter finché la pool non esiste
    pool_ready = False
    for attempt in range(1, CFG["JUPITER_MAX_RETRY"] + 1):
        if _jupiter_pool_exists(mint):
            pool_ready = True
            log.info(f"[pre_grad] {symbol}: pool Jupiter pronta al tentativo {attempt} "
                     f"({attempt*CFG['JUPITER_RETRY_SEC']}s dopo graduation)")
            break
        time.sleep(CFG["JUPITER_RETRY_SEC"])

    if not pool_ready:
        log.warning(f"[pre_grad] {symbol}: pool non trovata dopo "
                    f"{CFG['JUPITER_MAX_RETRY']*CFG['JUPITER_RETRY_SEC']}s → skip")
        return

    # Valida con DexScreener
    pair = _fetch_pair(mint)
    if not pair:
        log.info(f"[pre_grad] {symbol}: nessun pair DexScreener → skip")
        return

    liq  = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
    vol1 = float((pair.get("volume") or {}).get("h1", 0) or 0)
    chg1 = float((pair.get("priceChange") or {}).get("h1", 0) or 0)
    price = float(pair.get("priceUsd") or 0)
    mcap  = float((pair.get("fdv") or pair.get("marketCap") or 0))
    pair_addr = pair.get("pairAddress", "")
    age_min = 0.0
    try:
        created = pair.get("pairCreatedAt", 0)
        if created:
            age_min = (time.time() * 1000 - created) / 60000
    except Exception:
        pass

    bsr_val = _bsr(pair)

    if liq < CFG["MIN_LIQ_USD"]:
        log.info(f"[pre_grad] {symbol}: liq=${liq:,.0f} < min → skip")
        return
    if chg1 > CFG["MAX_CHANGE_1H_PCT"]:
        log.info(f"[pre_grad] {symbol}: chg={chg1:+.0f}% già troppo pompato → skip")
        return

    sid = f"{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    row = {
        "signal_id":       sid,
        "timestamp_entry": datetime.now().isoformat(),
        "token_symbol":    symbol,
        "mint_type":       "pre_grad",
        "token_address":   mint,
        "chain":           "solana",
        "pair_address":    pair_addr,
        "price_entry_usd": f"{price:.10g}",
        "volume_1h_usd":   f"{vol1:.2f}",
        "liquidity_usd":   f"{liq:.2f}",
        "bsr_1h":          f"{bsr_val:.3f}",
        "change_1h_pct":   f"{chg1:.2f}",
        "mcap_usd":        f"{mcap:.0f}",
        "pair_age_min":    f"{age_min:.1f}",
        "source":          "pre_grad",
        "signals":         f"pre_grad | vSol={v_sol:.1f} | entry@graduation+{attempt*CFG['JUPITER_RETRY_SEC']}s",
    }
    _write_signal(row)
    log.info(
        f"[pre_grad] ✅ SEGNALE {symbol} | liq=${liq:,.0f} | bsr={bsr_val:.2f} "
        f"| chg={chg1:+.1f}% | entry@{attempt*CFG['JUPITER_RETRY_SEC']}s"
    )


# ── WebSocket handler ─────────────────────────────────────────────────────────

class PreGradMonitor:
    """
    Monitor a 3 livelli su PumpPortal WebSocket:
      1. subscribeNewToken    → filtra token con initial buy >= soglia
      2. subscribeTokenTrade  → traccia vSolInBondingCurve
      3. subscribeMigration   → graduation → segnale immediato se in watchlist
    """

    def __init__(self):
        _load_seen()
        _ensure_csv()
        _ensure_pre_grad_csv()
        self._stop        = threading.Event()
        self._sig_queue   = queue.Queue()
        self._subscribed  : set[str] = set()   # mint già iscritti a tokenTrade
        self._ws          = None
        self._last_ws_msg = time.time()        # watchdog: ultima volta che il WS ha parlato

    def start(self):
        if not WS_AVAILABLE:
            log.warning("[pre_grad] websocket-client non installato → disabilitato")
            return
        _load_watchlist()   # ripristina token tracciati prima del restart
        threading.Thread(target=self._ws_loop,           daemon=True, name="pregrd_ws").start()
        threading.Thread(target=self._sig_loop,          daemon=True, name="pregrd_sig").start()
        threading.Thread(target=self._persist_loop,      daemon=True, name="pregrd_persist").start()
        threading.Thread(target=self._poll_vsol_loop,    daemon=True, name="pregrd_poll").start()
        log.info("[pre_grad] Monitor pre-graduation avviato")

    def stop(self):
        self._stop.set()
        _save_seen()
        _save_watchlist()

    # ── WebSocket ─────────────────────────────────────────────────────────────

    def _ws_loop(self):
        WS_SILENCE_MAX = 90   # secondi: se nessun messaggio → forza riconnessione

        while not self._stop.is_set():
            self._last_ws_msg = time.time()
            try:
                ws = websocket.WebSocketApp(
                    PUMPPORTAL_WS,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=lambda ws, e: log.warning(f"[pre_grad] WS error: {e}"),
                    on_close=lambda ws, c, m: log.info(f"[pre_grad] WS chiuso (code={c})"),
                )
                self._ws = ws

                # Watchdog in thread separato: chiude il WS se silenzio > WS_SILENCE_MAX
                def _watchdog(ws_ref):
                    while not self._stop.is_set():
                        time.sleep(15)
                        silence = time.time() - self._last_ws_msg
                        if silence > WS_SILENCE_MAX:
                            log.warning(f"[pre_grad] WS silenzio da {silence:.0f}s → forzo riconnessione")
                            try:
                                ws_ref.close()
                            except Exception:
                                pass
                            return

                threading.Thread(target=_watchdog, args=(ws,), daemon=True).start()
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                log.warning(f"[pre_grad] WS crash: {e}")
            if not self._stop.is_set():
                time.sleep(10)

    def _on_open(self, ws):
        # Livello 1: nuovi token (per scoprire candidati con initial buy serio)
        ws.send(json.dumps({"method": "subscribeNewToken"}))
        # Livello 3: graduation events
        ws.send(json.dumps({"method": "subscribeMigration"}))
        # Ri-sottoscrivi i token già in watchlist (persi su ogni reconnect)
        with _lock:
            tracked = list(_watchlist.keys())
        self._subscribed.clear()
        for mint in tracked:
            try:
                ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
                self._subscribed.add(mint)
            except Exception:
                pass
        log.info(f"[pre_grad] WS connesso — subscribeNewToken + subscribeMigration + {len(tracked)} token ri-sottoscritti")

    def _on_message(self, ws, raw: str):
        self._last_ws_msg = time.time()
        try:
            msg = json.loads(raw)
        except Exception:
            return

        if "message" in msg and "mint" not in msg:
            return

        mint   = msg.get("mint", "")
        symbol = (msg.get("symbol") or msg.get("name") or mint[:8]).upper()
        if not mint:
            return

        # Normalizza txType: None → "", "create" → "create"
        tx_type = str(msg.get("txType") or "").lower()

        # ── Nuovo token (subscribeNewToken) ───────────────────────────────────
        # PumpPortal invia txType="create" per nuovi token.
        # txType="" o assente → migration (graduation) event.
        if tx_type == "create" or (tx_type == "" and msg.get("initialBuy") is not None):
            self._handle_new_token(ws, mint, symbol, msg)
            return

        # ── Graduation event (subscribeMigration) ────────────────────────────
        if tx_type == "" and msg.get("initialBuy") is None:
            self._handle_graduation(mint, symbol, msg)
            return

        # ── Trade event (subscribeTokenTrade) ────────────────────────────────
        if tx_type in ("buy", "sell"):
            self._handle_trade(ws, mint, symbol, msg)
        else:
            log.debug(f"[pre_grad] txType sconosciuto: {tx_type!r} keys={list(msg.keys())[:6]}")

    def _handle_new_token(self, ws, mint: str, symbol: str, msg: dict):
        """Filtra nuovi token: traccia solo quelli con initial buy serio.
        PumpPortal invia initialBuy in LAMPORTS (1 SOL = 1,000,000,000 lamports).
        """
        _raw = float(msg.get("initialBuy", 0) or 0)
        # Converti lamports → SOL se il valore è >> 1 (lamports tipici: milioni)
        initial_buy = _raw / 1_000_000_000 if _raw > 1_000 else _raw
        if initial_buy < CFG["INITIAL_BUY_SOL"]:
            log.debug(f"[pre_grad] skip {symbol}: initial_buy={initial_buy:.4f} SOL < soglia {CFG['INITIAL_BUY_SOL']}")
            return
        # Valori >300 SOL = vSolInBondingCurve inviato per errore da PumpPortal
        # (token già quasi-graduati o campo sbagliato) → skip, non tracciare
        if initial_buy > 300:
            log.debug(f"[pre_grad] {symbol}: skip initial={initial_buy:.0f} SOL (anomalo/già-graduato)")
            return

        with _lock:
            if len(_watchlist) >= CFG["MAX_TRACKED"]:
                # Evict il token con vSol più BASSA (più lontano dalla graduation),
                # NON il più vecchio — preserva token che stanno accumulando vSol.
                # Tie-break: tra token con stessa vSol, rimuovi il più vecchio.
                evict = min(_watchlist,
                            key=lambda m: (_watchlist[m].get("v_sol", 0),
                                           -_watchlist[m].get("ts_first", 0)))
                evict_sym = _watchlist[evict].get("symbol", "?")
                evict_vsol = _watchlist[evict].get("v_sol", 0)
                log.debug(f"[pre_grad] Evict {evict_sym} (vSol={evict_vsol:.1f}) per fare spazio")
                # Ricorda il vSol prima dell'eviction per restaurarlo se ri-aggiunto
                _evicted_vsol[evict] = evict_vsol
                if len(_evicted_vsol) > 500:
                    oldest = next(iter(_evicted_vsol))
                    del _evicted_vsol[oldest]
                del _watchlist[evict]
                if evict in self._subscribed:
                    self._subscribed.discard(evict)

            if mint in _watchlist:
                return  # già tracciato, ignora duplicate WS events per lo stesso token

            if mint not in _watchlist:
                _raw_vsol = float(msg.get("vSolInBondingCurve", 0) or 0)
                _init_vsol = _raw_vsol / 1_000_000_000 if _raw_vsol > 1_000 else _raw_vsol
                # Se questo mint era già stato evicted, riparti dall'ultimo vSol noto
                _init_vsol = max(_init_vsol, _evicted_vsol.pop(mint, 0))
                _watchlist[mint] = {
                    "symbol":        symbol,
                    "v_sol":         _init_vsol,
                    "ts_first":      time.time(),
                    "last_trade_ts": 0,   # 0 → verrà pollato subito dal _poll_vsol_loop
                    "ts_hot":        None,
                    "rugcheck_ok":   None,
                    "v_sol_history": deque(maxlen=30),
                }
                log.info(f"[pre_grad] 📥 {symbol} — initial={initial_buy:.3f}SOL vSol={_init_vsol:.1f} → watchlist ({len(_watchlist)})")

                # Se ri-aggiunto con vSol già >= HOT (da evicted_vsol), avvia subito rugcheck
                if _init_vsol >= CFG["TRACK_HOT_SOL"]:
                    _watchlist[mint]["ts_hot"] = time.time()
                    log.info(f"[pre_grad] 🔥 {symbol} ri-aggiunto già HOT — vSol={_init_vsol:.1f} → rugcheck preventivo")
                    threading.Thread(
                        target=self._precheck_rug, args=(mint, symbol), daemon=True
                    ).start()

        # Sottoscrivi ai trade di questo token (livello 2)
        if mint not in self._subscribed and ws:
            try:
                ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": [mint]}))
                self._subscribed.add(mint)
            except Exception:
                pass

    def _handle_trade(self, ws, mint: str, symbol: str, msg: dict):
        """Traccia vSolInBondingCurve, segna token caldi e genera segnale pre-graduation."""

        v_sol_raw = float(msg.get("vSolInBondingCurve", 0) or 0)
        if v_sol_raw <= 0:
            # Prova campo alternativo (pump.fun potrebbe usare nome diverso)
            for alt in ("solAmount", "virtualSolReserves", "sol_reserves", "sol"):
                v_alt = float(msg.get(alt, 0) or 0)
                if v_alt > 0:
                    v_sol_raw = v_alt
                    log.info(f"[pre_grad] campo alternativo '{alt}'={v_alt}")
                    break
            if v_sol_raw <= 0:
                return
        # Converti lamports→SOL se necessario (valore > 1000 implica lamports)
        v_sol = v_sol_raw / 1_000_000_000 if v_sol_raw > 1_000 else v_sol_raw

        now_t = time.time()
        with _lock:
            if mint not in _watchlist:
                # Token che non abbiamo tracciato dall'inizio ma ha v_sol alto
                if v_sol >= CFG["TRACK_WARN_SOL"]:
                    _watchlist[mint] = {
                        "symbol":       symbol,
                        "v_sol":        v_sol,
                        "ts_first":     now_t,
                        "ts_hot":       None,
                        "rugcheck_ok":  None,
                        "v_sol_history": deque(maxlen=30),
                    }
            else:
                _watchlist[mint]["v_sol"]          = v_sol
                _watchlist[mint]["symbol"]         = symbol
                _watchlist[mint]["last_trade_ts"]  = now_t

            entry = _watchlist.get(mint)
            if not entry:
                return

            # Aggiorna history velocity (timestamp, v_sol)
            if "v_sol_history" not in entry:
                entry["v_sol_history"] = deque(maxlen=30)
            entry["v_sol_history"].append((now_t, v_sol))

            # Calcola velocity SOL/min sull'ultimo minuto
            history = entry["v_sol_history"]
            recent  = [(t, v) for t, v in history if now_t - t <= 60]
            velocity_sol_min = 0.0
            if len(recent) >= 2:
                dt = recent[-1][0] - recent[0][0]
                if dt > 0:
                    velocity_sol_min = (recent[-1][1] - recent[0][1]) / dt * 60

            # Supera soglia HOT → avvia rugcheck preventivo in background
            if v_sol >= CFG["TRACK_HOT_SOL"] and entry.get("ts_hot") is None:
                entry["ts_hot"] = now_t
                log.info(f"[pre_grad] 🔥 {symbol} HOT — vSol={v_sol:.1f} → rugcheck preventivo")
                threading.Thread(
                    target=self._precheck_rug, args=(mint, symbol), daemon=True
                ).start()

            # Segnale pre-graduation: vSol >= soglia + velocity positiva
            if (v_sol >= CFG["TRACK_HOT_SOL"]
                    and velocity_sol_min > 0
                    and mint not in _pre_grad_signaled
                    and entry.get("rugcheck_ok") is not False):
                _v = v_sol
                _vel = velocity_sol_min
                threading.Thread(
                    target=_emit_pre_grad_signal,
                    args=(mint, symbol, _v, _vel),
                    daemon=True,
                ).start()

    def _precheck_rug(self, mint: str, symbol: str):
        """Rugcheck in anticipo mentre il token si avvicina alla graduation."""
        ok = rugcheck_safe(mint, "pre_grad", chain="solana")
        with _lock:
            if mint in _watchlist:
                _watchlist[mint]["rugcheck_ok"] = ok
        if not ok:
            log.info(f"[pre_grad] {symbol}: BLOCCATO da rugcheck preventivo")

    def _poll_vsol_loop(self):
        """
        Fallback polling: subscribeTokenTrade spesso non consegna eventi vSol.
        Ogni 30s interroga pump.fun API per i token in watchlist senza aggiornamenti recenti.
        Se vSol >= HOT threshold → triggera segnale pre_grad.
        """
        while not self._stop.is_set():
            self._stop.wait(30)
            if self._stop.is_set():
                break

            now_t = time.time()
            with _lock:
                # Poll solo token senza aggiornamenti vSol negli ultimi 60s
                to_poll = [
                    (mint, entry["symbol"], entry.get("v_sol", 0))
                    for mint, entry in _watchlist.items()
                    if now_t - entry.get("last_trade_ts", 0) > 60
                ]

            polled = 0
            for mint, symbol, old_vsol in to_poll[:25]:   # max 25/ciclo → ~750 req/ora
                if self._stop.is_set():
                    break
                try:
                    # Prova pump.fun API, fallback DexScreener se 5xx
                    d = None
                    for api_url in [
                        f"https://frontend-api.pump.fun/coins/{mint}",
                        f"https://api.dexscreener.com/tokens/v1/solana/{mint}",
                    ]:
                        try:
                            r = requests.get(api_url, timeout=5)
                            if r.status_code == 200:
                                raw_d = r.json()
                                # DexScreener /tokens/v1 restituisce una lista di pair, non un dict
                                d = {"pairs": raw_d} if isinstance(raw_d, list) else raw_d
                                break
                            log.debug(f"[pre_grad] poll {symbol}: {api_url.split('/')[2]} → {r.status_code}")
                        except Exception as e:
                            log.debug(f"[pre_grad] poll {symbol}: errore API {e}")
                    if d is None:
                        continue

                    # Già graduato (pump.fun campo "complete") → rimuovi senza segnale.
                    # Niente "segnale tardivo": a questo punto il token è già su DEX,
                    # entrare ora significa comprare nel graduation dump (vedi pump_grad
                    # MIN_AGE_MIN=4min). pump_graduation_scanner lo intercetta in autonomia
                    # via il proprio evento WS con il filtro anti-dump già applicato.
                    if d.get("complete", False):
                        with _lock:
                            entry_snap = _watchlist.pop(mint, None)
                        if entry_snap:
                            v_snap = entry_snap.get("v_sol", 0)
                            log.info(f"[pre_grad] {symbol}: già graduato (vSol={v_snap:.1f}) → rimosso, demandato a pump_graduation_scanner")
                        continue

                    # pump.fun: virtual_sol_reserves in lamports
                    # DexScreener: nested pairs → usa liquidità come proxy
                    raw = float(d.get("virtual_sol_reserves", 0) or 0)
                    if raw <= 0:
                        # Fallback DexScreener: se esiste un pair con liq → già graduato su DEX.
                        # Stesso discorso: niente segnale tardivo, demandato a pump_grad.
                        pairs = d.get("pairs") or []
                        if pairs:
                            liq = float((pairs[0].get("liquidity") or {}).get("usd", 0) or 0)
                            if liq >= CFG["MIN_LIQ_USD"]:
                                with _lock:
                                    entry_snap = _watchlist.pop(mint, None)
                                if entry_snap:
                                    log.info(f"[pre_grad] {symbol}: pair su DEX (liq=${liq:,.0f}) → già graduato, rimosso, demandato a pump_graduation_scanner")
                        continue
                    v_sol = raw / 1_000_000_000 if raw > 1_000 else raw

                    with _lock:
                        if mint not in _watchlist:
                            continue
                        entry = _watchlist[mint]
                        entry["v_sol"]          = v_sol
                        entry["last_trade_ts"]  = now_t
                        if "v_sol_history" not in entry:
                            entry["v_sol_history"] = deque(maxlen=30)
                        entry["v_sol_history"].append((now_t, v_sol))

                    polled += 1
                    if v_sol > old_vsol + 1.0:
                        log.info(f"[pre_grad] poll {symbol}: vSol {old_vsol:.1f}→{v_sol:.1f} (+{v_sol-old_vsol:.1f})")

                    # Soglia HOT → avvia rugcheck se non già fatto
                    if v_sol >= CFG["TRACK_HOT_SOL"]:
                        with _lock:
                            entry = _watchlist.get(mint, {})
                            is_hot = entry.get("ts_hot") is None
                        if is_hot:
                            with _lock:
                                if mint in _watchlist:
                                    _watchlist[mint]["ts_hot"] = now_t
                            log.info(f"[pre_grad] 🔥 {symbol} HOT via poll — vSol={v_sol:.1f}")
                            threading.Thread(
                                target=self._precheck_rug, args=(mint, symbol), daemon=True
                            ).start()

                        # Calcola velocity per segnale
                        with _lock:
                            history = list(_watchlist.get(mint, {}).get("v_sol_history", []))
                        recent = [(t, v) for t, v in history if now_t - t <= 120]
                        velocity = 0.0
                        if len(recent) >= 2:
                            dt = recent[-1][0] - recent[0][0]
                            if dt > 0:
                                velocity = (recent[-1][1] - recent[0][1]) / dt * 60

                        with _lock:
                            already = mint in _pre_grad_signaled
                            rug_ok  = _watchlist.get(mint, {}).get("rugcheck_ok")
                        # Emetti segnale anche se velocity==0 ma vSol è cresciuto dall'ultimo poll
                        # (token che pompano velocemente hanno un solo punto di storia)
                        vel_use = velocity if velocity > 0 else max(0.05, v_sol - old_vsol)
                        if not already and rug_ok is not False and (velocity > 0 or v_sol > old_vsol):
                            threading.Thread(
                                target=_emit_pre_grad_signal,
                                args=(mint, symbol, v_sol, vel_use),
                                daemon=True,
                            ).start()

                except Exception as e:
                    log.warning(f"[pre_grad] poll {symbol}: errore — {e}")

            if polled > 0:
                log.debug(f"[pre_grad] poll loop: {polled}/{len(to_poll)} token aggiornati")

    def _handle_graduation(self, mint: str, symbol: str, msg: dict):
        """Graduation event — se il token era in watchlist: segnale immediato."""
        with _lock:
            entry = _watchlist.get(mint)

        if entry:
            rug_ok = entry.get("rugcheck_ok")
            v_sol  = entry.get("v_sol", 0)

            if rug_ok is False:
                log.info(f"[pre_grad] {symbol}: graduation ma rugcheck fallito → skip")
                with _lock:
                    _watchlist.pop(mint, None)
                return

            log.info(f"[pre_grad] 🎓 {symbol} in watchlist ({v_sol:.1f}SOL) → segnale IMMEDIATO")
            self._sig_queue.put((mint, symbol, v_sol))

            with _lock:
                _watchlist.pop(mint, None)
        else:
            # Token sconosciuto → lascia gestire al graduation scanner standard (90s)
            log.debug(f"[pre_grad] {symbol}: graduation ma non in watchlist → scanner standard")

    # ── Signal loop ───────────────────────────────────────────────────────────

    def _sig_loop(self):
        """Elabora i segnali immediati in thread separato (non blocca il WS)."""
        while not self._stop.is_set():
            try:
                mint, symbol, v_sol = self._sig_queue.get(timeout=5)
                _signal_immediate(mint, symbol, v_sol)
                self._sig_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                log.error(f"[pre_grad] signal error: {e}")

    # ── Persist watchlist ogni 60s ────────────────────────────────────────────

    def _persist_loop(self):
        """Salva la watchlist su disco ogni 60s per sopravvivere ai restart."""
        while not self._stop.is_set():
            time.sleep(60)
            with _lock:
                _save_watchlist()

    # ── Pulizia watchlist ─────────────────────────────────────────────────────

    def _cleanup_loop(self):
        """Rimuove token stantii dalla watchlist ogni 60s."""
        while not self._stop.is_set():
            now = time.time()
            with _lock:
                stale = [
                    m for m, e in _watchlist.items()
                    if now - e.get("ts_first", now) > CFG["WATCHLIST_TTL_SEC"]
                ]
                for m in stale:
                    del _watchlist[m]
            if stale:
                log.debug(f"[pre_grad] Cleanup: {len(stale)} token stantii rimossi")
            time.sleep(60)


# ── Integrazione run.py ───────────────────────────────────────────────────────

def main_loop():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    monitor = PreGradMonitor()
    monitor.start()
    try:
        while True:
            time.sleep(60)
            with _lock:
                hot = [(e["symbol"], e["v_sol"]) for e in _watchlist.values()
                       if e["v_sol"] >= CFG["TRACK_HOT_SOL"]]
            if hot:
                log.info(f"[pre_grad] Watchlist HOT: {hot}")
    except KeyboardInterrupt:
        monitor.stop()
