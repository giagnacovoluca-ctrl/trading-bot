"""
base_pump_scanner.py
====================
Scanner on-chain per token appena listati su Base chain.

Architettura:
  1. Web3.py polling blocchi Base (ogni 8s)
  2. PoolCreated events da Uniswap V3 Factory + Aerodrome Factory
  3. Filtra: token freschi (< 24h) con WETH o USDC come quote
  4. DexScreener: valida BSR, volume, liquidità, age
  5. Stability check (8s delay + secondo fetch)
  6. Scrive su base_pump_signals.csv
  7. Invia email

Equivalente di pump_graduation_scanner.py per Base chain.
"""

from __future__ import annotations
import csv
import json
import logging
import os
import smtplib
import threading
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests

try:
    from web3 import Web3
    from web3.exceptions import BlockNotFound
    WEB3_AVAILABLE = True
except ImportError:
    WEB3_AVAILABLE = False

log = logging.getLogger("base_pump")

# ── Percorsi ──────────────────────────────────────────────────────────────────
_HERE       = Path(__file__).parent
SIGNALS_CSV = _HERE / "reports" / "base_pump_signals.csv"
STATE_FILE  = _HERE / "reports" / "base_pump_state.json"
SIGNALS_CSV.parent.mkdir(parents=True, exist_ok=True)

# ── Costanti Base ─────────────────────────────────────────────────────────────
UNIV3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
AERO_FACTORY  = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
WETH_BASE     = "0x4200000000000000000000000000000000000006"
USDC_BASE     = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
DEXSCREENER   = "https://api.dexscreener.com"
BASE_RPC_URL  = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
CHAIN_ID      = 8453

# ── ABI ───────────────────────────────────────────────────────────────────────
_ABI_UNIV3_FACTORY = [
    {"type": "event", "name": "PoolCreated", "inputs": [
        {"name": "token0",      "type": "address", "indexed": True},
        {"name": "token1",      "type": "address", "indexed": True},
        {"name": "fee",         "type": "uint24",  "indexed": True},
        {"name": "tickSpacing", "type": "int24",   "indexed": False},
        {"name": "pool",        "type": "address", "indexed": False},
    ]},
]
_ABI_AERO_FACTORY = [
    {"type": "event", "name": "PoolCreated", "inputs": [
        {"name": "token0",      "type": "address", "indexed": True},
        {"name": "token1",      "type": "address", "indexed": True},
        {"name": "stable",      "type": "bool",    "indexed": True},
        {"name": "pool",        "type": "address", "indexed": False},
        {"name": "arg4",        "type": "uint256", "indexed": False},
    ]},
]
_ABI_ERC20_MINIMAL = [
    {"name": "symbol",   "type": "function", "inputs": [], "outputs": [{"type": "string"}], "stateMutability": "view"},
    {"name": "name",     "type": "function", "inputs": [], "outputs": [{"type": "string"}], "stateMutability": "view"},
    {"name": "decimals", "type": "function", "inputs": [], "outputs": [{"type": "uint8"}],  "stateMutability": "view"},
]

# ── Configurazione ────────────────────────────────────────────────────────────
CONFIG = {
    "POLL_INTERVAL_SEC":  8,
    "LOOKBACK_BLOCKS":    20,
    "DEX_WAIT_SEC":       10,
    "DEX_MAX_RETRIES":     4,
    "DEX_RETRY_INTERVAL": 30,
    "MIN_LIQ_USD":      15_000,
    "MIN_VOL_1H_USD":    2_000,
    "MIN_BSR":             1.0,
    "MAX_CHANGE_1H_PCT":  60.0,
    "MIN_CHANGE_1H_PCT": -20.0,
    "MIN_TXNS_1H":         10,
    "MAX_PAIR_AGE_MIN":  1_440,   # max 24h
    "TOKEN_AGE_MAX_H":      24,
    "SEEN_TTL_HOURS":       24,
}

# ── DEX accettati su DexScreener/Base ─────────────────────────────────────────
_ACCEPTED_DEX = {"uniswap", "aerodrome", "pumpswap", "baseswap"}

# ── Email ─────────────────────────────────────────────────────────────────────
_EMAIL_CFG = {
    "SMTP_HOST":     os.environ.get("SMTP_HOST",     "smtp.gmail.com"),
    "SMTP_PORT":     int(os.environ.get("SMTP_PORT", "587")),
    "SMTP_USER":     os.environ.get("SMTP_USER",     "giagnacovo.luca@gmail.com"),
    "SMTP_PASSWORD": os.environ.get("SMTP_PASSWORD", ""),
    "FROM_ADDR":     os.environ.get("SMTP_FROM",     "giagnacovo.luca@gmail.com"),
    "TO_ADDR":       os.environ.get("SMTP_TO",       "giagnacovo.luca@gmail.com"),
}

# ── CSV fields (compatibili con pump_grad_signals.csv → trade_simulator) ──────
_CSV_FIELDS = [
    "signal_id", "timestamp_entry", "token_symbol", "token_name",
    "token_address", "chain", "pair_address", "price_entry_usd",
    "volume_1h_usd", "liquidity_usd", "buy_sell_ratio_1h", "change_1h_pct",
    "pump_probability", "buy_tax", "sell_tax", "lp_locked", "is_honeypot",
    "top_features",
]
_csv_lock = threading.Lock()

# ── Web3 cache ────────────────────────────────────────────────────────────────
_w3_instance: Optional["Web3"] = None
_w3_lock = threading.Lock()

# ── DexScreener rate limit ────────────────────────────────────────────────────
_dex_last   = 0.0
_dex_lock   = threading.Lock()

# ── Seen / dedup ──────────────────────────────────────────────────────────────
_seen: dict[str, datetime] = {}
_seen_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Web3 helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_w3() -> Optional["Web3"]:
    """Connessione Web3 con cache e retry."""
    global _w3_instance
    if not WEB3_AVAILABLE:
        return None
    with _w3_lock:
        if _w3_instance is not None and _w3_instance.is_connected():
            return _w3_instance
        try:
            w3 = Web3(Web3.HTTPProvider(BASE_RPC_URL, request_kwargs={"timeout": 15}))
            if w3.is_connected():
                _w3_instance = w3
                log.info(f"[base_pump] Web3 connesso a Base RPC (chain_id={w3.eth.chain_id})")
                return _w3_instance
        except Exception as e:
            log.warning(f"[base_pump] Web3 connessione fallita: {e}")
        return None


def _get_token_info(token_addr: str) -> tuple[str, str]:
    """Legge symbol e name on-chain. Fallback: addr[:8]."""
    fallback = (token_addr[:8], token_addr[:8])
    w3 = _get_w3()
    if not w3:
        return fallback
    try:
        checksum = Web3.to_checksum_address(token_addr)
        contract = w3.eth.contract(address=checksum, abi=_ABI_ERC20_MINIMAL)
        symbol = contract.functions.symbol().call()
        name   = contract.functions.name().call()
        return (symbol or token_addr[:8], name or symbol or token_addr[:8])
    except Exception as e:
        log.debug(f"[base_pump] _get_token_info {token_addr[:10]}: {e}")
        return fallback


# ─────────────────────────────────────────────────────────────────────────────
# DexScreener helpers
# ─────────────────────────────────────────────────────────────────────────────

def _dex_get(url: str, params: dict = None) -> Optional[dict]:
    global _dex_last
    with _dex_lock:
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
            log.debug(f"[base_pump] dex fetch {url}: {e}")
        return None


def _fetch_dex_pair(token_addr: str) -> Optional[dict]:
    """
    DexScreener /tokens/v1/base/{token_addr} → pair con più liquidità
    tra uniswap, aerodrome, pumpswap, baseswap.
    """
    data = _dex_get(f"{DEXSCREENER}/tokens/v1/base/{token_addr}")
    if not data:
        return None
    pairs = data if isinstance(data, list) else (data.get("pairs") or [])
    candidates = []
    for p in pairs:
        dex_id = (p.get("dexId") or "").lower()
        if any(accepted in dex_id for accepted in _ACCEPTED_DEX):
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if liq > 0:
                candidates.append(p)
    if not candidates:
        return None
    return max(candidates, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))


def _bsr(txns: dict) -> float:
    """BSR 1h da txns dict. Default 1.0 se < 5 transazioni."""
    try:
        h1 = txns.get("h1", {})
        b = int(h1.get("buys",  0) or 0)
        s = int(h1.get("sells", 0) or 0)
        return b / (b + s) * 2 if (b + s) >= 5 else 1.0
    except Exception:
        return 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Seen / dedup
# ─────────────────────────────────────────────────────────────────────────────

def _load_seen():
    global _seen
    try:
        if STATE_FILE.exists():
            raw    = json.loads(STATE_FILE.read_text())
            cutoff = datetime.now() - timedelta(hours=CONFIG["SEEN_TTL_HOURS"])
            _seen  = {k: datetime.fromisoformat(v) for k, v in raw.items()
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


def _is_seen(token_addr: str) -> bool:
    with _seen_lock:
        ts = _seen.get(token_addr.lower())
        if ts and datetime.now() - ts < timedelta(hours=CONFIG["SEEN_TTL_HOURS"]):
            return True
        if ts:
            del _seen[token_addr.lower()]
        return False


def _mark_seen(token_addr: str):
    with _seen_lock:
        _seen[token_addr.lower()] = datetime.now()


# ─────────────────────────────────────────────────────────────────────────────
# CSV
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_csv():
    if not SIGNALS_CSV.exists() or SIGNALS_CSV.stat().st_size == 0:
        with open(SIGNALS_CSV, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_CSV_FIELDS).writeheader()


def _write_signal(row: dict):
    _ensure_csv()
    with _csv_lock:
        with open(SIGNALS_CSV, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore").writerow(row)


# ─────────────────────────────────────────────────────────────────────────────
# Email
# ─────────────────────────────────────────────────────────────────────────────

def _send_base_pump_email(sig: dict, pair: dict) -> bool:
    try:
        cfg   = _EMAIL_CFG
        sym   = sig["token_symbol"]
        mcap  = float(pair.get("marketCap") or pair.get("fdv") or 0)
        liq   = float((pair.get("liquidity") or {}).get("usd") or 0)
        vol1h = float((pair.get("volume") or {}).get("h1") or 0)
        bsr   = float(sig["buy_sell_ratio_1h"])
        chg   = float(sig["change_1h_pct"])
        price = sig["price_entry_usd"]
        dex_name = ""
        if "dex_name=" in sig.get("top_features", ""):
            dex_name = sig["top_features"].split("dex_name=")[1].split(" |")[0]
        age_m = 0.0
        if "pair_age_min=" in sig.get("top_features", ""):
            age_m = float(sig["top_features"].split("pair_age_min=")[1].split(" |")[0])

        subject = (
            f"[BASE PUMP] {sym} | mcap=${mcap:,.0f} | "
            f"bsr={bsr:.2f} | {chg:+.1f}%"
        )
        body = f"""
<html><body style="font-family:monospace;background:#0d1117;color:#e6edf3;padding:20px">
<h2 style="color:#58a6ff">Base Chain New Pool: {sym}</h2>
<table style="border-collapse:collapse">
  <tr><td style="color:#8b949e;padding:4px 12px">Token</td><td><b>{sym}</b> — {sig['token_name']}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Address</td><td style="font-size:.85rem">{sig['token_address']}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">DEX</td><td>{dex_name}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Eta pool</td><td>{age_m:.0f} min</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Prezzo</td><td>${price}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Market Cap</td><td>${mcap:,.0f}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Liquidita</td><td>${liq:,.0f}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Volume 1h</td><td>${vol1h:,.0f}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">BSR 1h</td><td style="color:{'#3fb950' if bsr>=1.2 else '#e6edf3'}">{bsr:.3f}</td></tr>
  <tr><td style="color:#8b949e;padding:4px 12px">Change 1h</td><td style="color:{'#3fb950' if chg>0 else '#f85149'}">{chg:+.1f}%</td></tr>
</table>
<p style="margin-top:16px;color:#8b949e;font-size:.8rem">
  TP1 +25% · Trail attivo a +15% · Hard SL -12%<br>
  Sistema: base_pump | Chain: Base | Signal ID: {sig['signal_id']}
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
        log.info(f"[base_pump] Email inviata: {sym}")
        return True
    except Exception as e:
        log.warning(f"[base_pump] Email error: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Validazione e segnale
# ─────────────────────────────────────────────────────────────────────────────

def _validate_and_signal(token_addr: str, quote_addr: str, pool_addr: str, dex_name: str):
    """
    Valida un nuovo token appena listato su Base.
    Chiama DexScreener, applica filtri, emette segnale CSV + email.
    Gira in thread separato — non blocca il poll loop.
    """
    symbol, name = _get_token_info(token_addr)
    log.info(f"[base_pump] Nuova pool {dex_name}: {symbol} ({token_addr[:10]}…) — verifica DexScreener")

    time.sleep(CONFIG["DEX_WAIT_SEC"])

    pair = None
    for attempt in range(1, CONFIG["DEX_MAX_RETRIES"] + 1):
        pair = _fetch_dex_pair(token_addr)
        if pair:
            break
        log.debug(
            f"[base_pump] {symbol}: DexScreener tentativo {attempt}/{CONFIG['DEX_MAX_RETRIES']} — non ancora indicizzato"
        )
        if attempt < CONFIG["DEX_MAX_RETRIES"]:
            time.sleep(CONFIG["DEX_RETRY_INTERVAL"])

    if not pair:
        log.info(
            f"[base_pump] {symbol}: nessuna pair su DexScreener dopo "
            f"{CONFIG['DEX_MAX_RETRIES']} tentativi — skip"
        )
        return

    # Controlla età pair (ignora token già vecchi)
    pair_created_at = pair.get("pairCreatedAt") or 0
    age_min = (datetime.now().timestamp() * 1000 - pair_created_at) / 60_000
    if age_min > CONFIG["MAX_PAIR_AGE_MIN"]:
        log.info(f"[base_pump] {symbol}: pair troppo vecchia ({age_min:.0f}min > {CONFIG['MAX_PAIR_AGE_MIN']}min) — skip")
        return

    def _extract_metrics(p: dict) -> tuple:
        _liq   = float((p.get("liquidity") or {}).get("usd") or 0)
        _vol1h = float((p.get("volume") or {}).get("h1") or 0)
        _chg   = float((p.get("priceChange") or {}).get("h1") or 0)
        _price = float(p.get("priceUsd") or 0)
        _mcap  = float(p.get("marketCap") or p.get("fdv") or 0)
        _h1t   = (p.get("txns") or {}).get("h1", {})
        _txns  = int(_h1t.get("buys", 0) or 0) + int(_h1t.get("sells", 0) or 0)
        _bsr   = _bsr_fn(p.get("txns") or {})
        _age   = (datetime.now().timestamp() * 1000 - (p.get("pairCreatedAt") or 0)) / 60_000
        return _liq, _vol1h, _chg, _price, _mcap, _txns, _bsr, _age

    # Usa un alias locale per evitare shadowing con la funzione globale _bsr
    _bsr_fn = _bsr

    liq, vol1h, chg, price, mcap, txns, bsr_val, age_min = _extract_metrics(pair)

    # Stability check: ri-fetcha dopo 8s
    time.sleep(8)
    pair2 = _fetch_dex_pair(token_addr)
    if pair2:
        liq2 = float((pair2.get("liquidity") or {}).get("usd") or 0)
        if liq > 0 and liq2 < liq * 0.80:
            log.info(
                f"[base_pump] {symbol}: liq drain ${liq:,.0f}→${liq2:,.0f} "
                f"({liq2/liq*100:.0f}% in 8s) — possibile rug, skip"
            )
            return
        # Aggiorna con dati freschi
        liq, vol1h, chg, price, mcap, txns, bsr_val, age_min = _extract_metrics(pair2)
        pair = pair2

    dex_id = (pair.get("dexId") or dex_name).lower()

    # Filtri qualità
    rejects = []
    if liq    < CONFIG["MIN_LIQ_USD"]:          rejects.append(f"liq=${liq:,.0f}")
    if vol1h  < CONFIG["MIN_VOL_1H_USD"]:       rejects.append(f"vol1h=${vol1h:,.0f}")
    if bsr_val < CONFIG["MIN_BSR"]:             rejects.append(f"bsr={bsr_val:.2f}")
    if chg    > CONFIG["MAX_CHANGE_1H_PCT"]:    rejects.append(f"chg={chg:+.0f}%>60%")
    if chg    < CONFIG["MIN_CHANGE_1H_PCT"]:    rejects.append(f"chg={chg:+.0f}% dump")
    if txns   < CONFIG["MIN_TXNS_1H"]:          rejects.append(f"txns={txns}")
    if age_min > CONFIG["MAX_PAIR_AGE_MIN"]:    rejects.append(f"age={age_min:.0f}min>{CONFIG['MAX_PAIR_AGE_MIN']}min")

    if rejects:
        log.info(f"[base_pump] {symbol} [{dex_id}]: filtro → {', '.join(rejects)}")
        return

    # Costruzione segnale
    now      = datetime.now()
    sid      = f"BP_{symbol}_{now.strftime('%Y%m%d_%H%M%S')}"
    vol_5m   = float((pair.get("volume") or {}).get("m5") or 0)
    vol_accel = (vol_5m * 12) / vol1h if vol1h > 0 else 1.0
    pump_prob = min(0.95, (bsr_val - 1.0) * 0.3 + min(vol_accel, 3.0) * 0.15 + 0.30)

    # symbol e name da DexScreener se on-chain era fallback
    base_tok = pair.get("baseToken") or {}
    if not symbol or len(symbol) <= 8 and symbol == token_addr[:8]:
        symbol = base_tok.get("symbol", symbol)
    if not name or name == token_addr[:8]:
        name = base_tok.get("name", name)

    pair_addr = pair.get("pairAddress") or pool_addr

    top_features = (
        f"base_pump=true | "
        f"dex_name={dex_id} | "
        f"pair_age_min={age_min:.0f} | "
        f"bsr_1h={bsr_val:.2f} | "
        f"vol_accel={vol_accel:.2f} | "
        f"mcap_usd={mcap:,.0f}"
    )

    sig = {
        "signal_id":         sid,
        "timestamp_entry":   now.isoformat(),
        "token_symbol":      symbol,
        "token_name":        name,
        "token_address":     token_addr,
        "chain":             "base",
        "pair_address":      pair_addr,
        "price_entry_usd":   f"{price:.10g}",
        "volume_1h_usd":     f"{vol1h:.2f}",
        "liquidity_usd":     f"{liq:.2f}",
        "buy_sell_ratio_1h": f"{bsr_val:.3f}",
        "change_1h_pct":     f"{chg:.2f}",
        "pump_probability":  f"{pump_prob:.4f}",
        "buy_tax":           "0.0",
        "sell_tax":          "0.0",
        "lp_locked":         "0",
        "is_honeypot":       "0",
        "top_features":      top_features,
    }

    _write_signal(sig)
    _send_base_pump_email(sig, pair)

    log.info(
        f"[base_pump] SEGNALE {symbol} [{dex_id}] | "
        f"age={age_min:.0f}min | mcap=${mcap:,.0f} | liq=${liq:,.0f} | "
        f"bsr={bsr_val:.2f} | vol1h=${vol1h:,.0f} | chg={chg:+.1f}%"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scanner principale
# ─────────────────────────────────────────────────────────────────────────────

class BasePumpScanner:
    """
    Scanner on-chain per nuove pool Base (Uniswap V3 + Aerodrome).
    Gira in background come thread daemon.
    """

    def __init__(self):
        _load_seen()
        _ensure_csv()
        self._stop = threading.Event()

    def start(self):
        if not WEB3_AVAILABLE:
            log.warning("[base_pump] web3 non installato → scanner disabilitato. "
                        "Esegui: pip install web3")
            return
        w3 = _get_w3()
        if not w3:
            log.warning("[base_pump] RPC Base non raggiungibile → scanner disabilitato")
            return
        threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="base_pump",
        ).start()
        log.info("[base_pump] Scanner avviato — polling Uniswap V3 + Aerodrome su Base")

    def stop(self):
        self._stop.set()
        _save_seen()

    # ── Poll loop ─────────────────────────────────────────────────────────────

    def _poll_loop(self):
        w3 = _get_w3()
        if not w3:
            log.error("[base_pump] Web3 non disponibile nel poll loop — abort")
            return

        try:
            last_block = w3.eth.block_number - CONFIG["LOOKBACK_BLOCKS"]
        except Exception as e:
            log.error(f"[base_pump] Impossibile ottenere block_number: {e}")
            return

        log.info(f"[base_pump] Poll loop avviato dal blocco {last_block}")

        while not self._stop.is_set():
            try:
                current = w3.eth.block_number
            except Exception as e:
                log.warning(f"[base_pump] Errore block_number: {e}")
                time.sleep(CONFIG["POLL_INTERVAL_SEC"])
                continue

            if current <= last_block:
                time.sleep(CONFIG["POLL_INTERVAL_SEC"])
                continue

            for factory_addr, factory_abi, dex_name in [
                (UNIV3_FACTORY, _ABI_UNIV3_FACTORY, "uniswap_v3"),
                (AERO_FACTORY,  _ABI_AERO_FACTORY,  "aerodrome"),
            ]:
                try:
                    checksum_factory = Web3.to_checksum_address(factory_addr)
                    contract = w3.eth.contract(address=checksum_factory, abi=factory_abi)
                    events = contract.events.PoolCreated.get_logs(
                        fromBlock=last_block + 1,
                        toBlock=current,
                    )
                    for evt in events:
                        self._handle_pool_created(evt, dex_name)
                except Exception as e:
                    log.debug(f"[base_pump] Errore eventi {dex_name} "
                              f"[{last_block+1}-{current}]: {e}")

            last_block = current
            time.sleep(CONFIG["POLL_INTERVAL_SEC"])

    # ── Handler singolo evento ────────────────────────────────────────────────

    def _handle_pool_created(self, evt, dex_name: str):
        try:
            token0 = evt.args.token0
            token1 = evt.args.token1
            pool   = evt.args.pool

            quotes = {WETH_BASE.lower(), USDC_BASE.lower()}

            if token0.lower() in quotes:
                token_addr, quote_addr = token1, token0
            elif token1.lower() in quotes:
                token_addr, quote_addr = token0, token1
            else:
                return  # nessuna quote nota

            if _is_seen(token_addr):
                return
            _mark_seen(token_addr)

            threading.Thread(
                target=_validate_and_signal,
                args=(token_addr, quote_addr, pool, dex_name),
                daemon=True,
            ).start()
        except Exception as e:
            log.debug(f"[base_pump] _handle_pool_created error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Standalone
# ─────────────────────────────────────────────────────────────────────────────

def main_loop():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not WEB3_AVAILABLE:
        print("Installa web3: pip install web3")
        return

    scanner = BasePumpScanner()
    scanner.start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("[base_pump] Stop.")
        scanner.stop()


if __name__ == "__main__":
    main_loop()
