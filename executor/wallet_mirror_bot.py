"""
wallet_mirror_bot.py
====================
Monitora i wallet alpha in real-time via Helius Enhanced WebSocket
e scrive segnali in mirror_signals.csv quando comprano token.

Flusso:
  alpha_wallets.json (da wallet_alpha_finder.py)
      ↓
  Helius WS → notifica tx in real-time (push, zero polling)
      ↓
  Parse: feePayer in watched_wallets + token ricevuto non-stable
      ↓
  DexScreener: valida liquidità (1 call/token, cache 5min)
      ↓
  mirror_signals.csv → LiveEngine lo legge → executor lo esegue

Avvio:
  python executor/wallet_mirror_bot.py

.env (in executor/.env):
  HELIUS_API_KEY=...           (già presente)
  MIRROR_DRY_RUN=true          (false = scrive segnali reali)
  MIRROR_MAX_WALLETS=20        (top N da alpha_wallets.json)
  MIRROR_MIN_USD=5             (ignora buy < $5 equiv.)
  MIRROR_MIN_LIQ=15000         (ignora token con liq < $15k)
"""

import csv
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

try:
    import websocket
    WS_OK = True
except ImportError:
    WS_OK = False
    print("WARN: websocket-client non installato — pip install websocket-client")

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mirror_bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HELIUS_API_KEY  = os.getenv("HELIUS_API_KEY", "")
DRY_RUN         = os.getenv("MIRROR_DRY_RUN", "true").lower() != "false"
MAX_WALLETS     = int(os.getenv("MIRROR_MAX_WALLETS", "20"))
MIN_USD_BUY     = float(os.getenv("MIRROR_MIN_USD", "5"))
MIN_LIQ_USD     = float(os.getenv("MIRROR_MIN_LIQ", "15000"))

ROOT            = Path(__file__).parent.parent
ALPHA_FILE      = Path(__file__).parent / "alpha_wallets.json"
MIRROR_CSV      = ROOT / "defi" / "reports" / "mirror_signals.csv"

HELIUS_WS_URL   = f"wss://atlas-mainnet.helius-rpc.com?api-key={HELIUS_API_KEY}"

STABLECOIN_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "So11111111111111111111111111111111111111112",     # wSOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
}

# Cache DexScreener per evitare chiamate duplicate sullo stesso token
_dex_cache: dict = {}       # mint → {data: {...}, ts: float}
_dex_lock         = threading.Lock()
DEX_CACHE_TTL     = 300     # 5 minuti

# Mint già segnalati in questa sessione: evita duplicati da più wallet
_signaled_mints: set = set()
_signaled_lock    = threading.Lock()

# Wallet monitorati (caricati da alpha_wallets.json)
_watched_wallets: set = set()
_wallet_meta: dict    = {}   # wallet → {score, tokens_early_count}

# WebSocket state
_ws: Optional[object]   = None
_sub_id: Optional[int]  = None
_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Carica wallet alpha
# ---------------------------------------------------------------------------

def load_alpha_wallets() -> bool:
    global _watched_wallets, _wallet_meta
    if not ALPHA_FILE.exists():
        log.error(f"alpha_wallets.json non trovato: {ALPHA_FILE}")
        log.error("Esegui prima: python executor/wallet_alpha_finder.py")
        return False

    try:
        data = json.loads(ALPHA_FILE.read_text())
    except Exception as e:
        log.error(f"Errore lettura alpha_wallets.json: {e}")
        return False

    top = data[:MAX_WALLETS]
    _watched_wallets = {entry["wallet"] for entry in top}
    _wallet_meta     = {entry["wallet"]: entry for entry in top}
    log.info(f"Wallet alpha caricati: {len(_watched_wallets)} (top {MAX_WALLETS})")
    for entry in top[:5]:
        log.info(f"  {entry['wallet'][:12]}… score={entry['score']} tokens={entry['tokens_early_count']}")
    return True

# ---------------------------------------------------------------------------
# DexScreener — validazione token (una chiamata per token, cache 5 min)
# ---------------------------------------------------------------------------

def _fetch_dex(mint: str) -> Optional[dict]:
    """Fetcha dati DexScreener per un token Solana. Cached 5 min."""
    now = time.time()
    with _dex_lock:
        cached = _dex_cache.get(mint)
        if cached and now - cached["ts"] < DEX_CACHE_TTL:
            return cached["data"]

    try:
        url = f"https://api.dexscreener.com/tokens/v1/solana/{mint}"
        r   = requests.get(url, timeout=8)
        r.raise_for_status()
        pairs = r.json()
        if not isinstance(pairs, list) or not pairs:
            return None
        # Prendi la pair con più liquidità
        pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
        best = pairs[0]
        data = {
            "pair_address":    best.get("pairAddress", ""),
            "token_symbol":    (best.get("baseToken") or {}).get("symbol", "?"),
            "token_name":      (best.get("baseToken") or {}).get("name", ""),
            "price_usd":       float((best.get("priceUsd") or 0)),
            "liquidity_usd":   float((best.get("liquidity") or {}).get("usd", 0) or 0),
            "volume_1h_usd":   float((best.get("volume") or {}).get("h1", 0) or 0),
            "bsr":             _calc_bsr(best),
            "chain":           "solana",
        }
        with _dex_lock:
            _dex_cache[mint] = {"data": data, "ts": now}
        return data
    except Exception as e:
        log.debug(f"DexScreener {mint[:8]}: {e}")
        return None


def _calc_bsr(pair: dict) -> float:
    txns = pair.get("txns", {}).get("h1", {})
    buys  = float(txns.get("buys", 0) or 0)
    sells = float(txns.get("sells", 0) or 0)
    total = buys + sells
    return round(buys / total, 3) if total > 0 else 0.0

# ---------------------------------------------------------------------------
# Mirror signals CSV
# ---------------------------------------------------------------------------

MIRROR_CSV_HEADER = [
    "signal_id", "timestamp_entry", "token_symbol", "token_name",
    "token_address", "chain", "pair_address", "price_entry_usd",
    "volume_1h_usd", "liquidity_usd", "buy_sell_ratio_1h",
    "change_1h_pct", "pump_probability", "buy_tax", "sell_tax",
    "lp_locked", "is_honeypot", "top_features",
]


def _ensure_csv():
    MIRROR_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not MIRROR_CSV.exists():
        with open(MIRROR_CSV, "w", newline="") as f:
            csv.writer(f).writerow(MIRROR_CSV_HEADER)


def _write_signal(mint: str, dex: dict, copier_wallet: str):
    sig_id = f"mirror_{dex['token_symbol']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    row = {
        "signal_id":         sig_id,
        "timestamp_entry":   datetime.now().isoformat(),
        "token_symbol":      dex["token_symbol"],
        "token_name":        dex["token_name"],
        "token_address":     mint,
        "chain":             "solana",
        "pair_address":      dex["pair_address"],
        "price_entry_usd":   f"{dex['price_usd']:.10g}",
        "volume_1h_usd":     f"{dex['volume_1h_usd']:.2f}",
        "liquidity_usd":     f"{dex['liquidity_usd']:.2f}",
        "buy_sell_ratio_1h": f"{dex['bsr']:.3f}",
        "change_1h_pct":     "0.00",
        "pump_probability":  "0.80",
        "buy_tax":           "0.0",
        "sell_tax":          "0.0",
        "lp_locked":         "0",
        "is_honeypot":       "0",
        "top_features":      f"mirror_from={copier_wallet[:8]} | liq=${dex['liquidity_usd']:.0f} | bsr={dex['bsr']:.2f}",
    }
    with open(MIRROR_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MIRROR_CSV_HEADER)
        w.writerow(row)
    return sig_id

# ---------------------------------------------------------------------------
# Parse transazione Helius Enhanced WS
# ---------------------------------------------------------------------------

def _extract_buyer_and_token(tx_data: dict) -> Optional[tuple[str, str, float]]:
    """
    Estrae (feePayer, token_mint, usd_approx) da una tx Helius Enhanced.
    Ritorna None se non è un buy di token non-stable da un wallet monitorato.

    Usa preTokenBalances/postTokenBalances per rilevare:
      - USDC in uscita dal feePayer  → valore speso
      - token non-stable in entrata → token comprato
    """
    try:
        meta = tx_data.get("meta", {}) or {}
        if meta.get("err"):
            return None

        tx       = tx_data.get("transaction", {}) or {}
        msg      = tx.get("message", {}) or {}
        keys     = msg.get("accountKeys", []) or []

        # feePayer = primo account che firma
        fee_payer = None
        for k in keys:
            if isinstance(k, dict):
                pk = k.get("pubkey", "")
                if k.get("signer"):
                    fee_payer = pk
                    break
            elif isinstance(k, str) and not fee_payer:
                fee_payer = k

        if not fee_payer or fee_payer not in _watched_wallets:
            return None

        pre_balances  = {(b["owner"], b["mint"]): float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                         for b in (meta.get("preTokenBalances") or []) if b.get("owner")}
        post_balances = {(b["owner"], b["mint"]): float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                         for b in (meta.get("postTokenBalances") or []) if b.get("owner")}

        # USDC speso → valore approssimativo del buy
        usdc_pre  = pre_balances.get((fee_payer, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"), 0)
        usdc_post = post_balances.get((fee_payer, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"), usdc_pre)
        usd_spent = max(0.0, usdc_pre - usdc_post)

        # Token non-stable apparso in post che non era in pre → il token comprato
        new_token = None
        for (owner, mint), post_amt in post_balances.items():
            if owner != fee_payer:
                continue
            if mint in STABLECOIN_MINTS:
                continue
            pre_amt = pre_balances.get((fee_payer, mint), 0)
            if post_amt > pre_amt and post_amt > 0:
                new_token = mint
                break

        if not new_token:
            return None

        # Se USDC speso è 0 (pagato in SOL), stima dal saldo SOL
        if usd_spent < MIN_USD_BUY:
            sol_pre  = (meta.get("preBalances")  or [0])[0] / 1e9
            sol_post = (meta.get("postBalances") or [0])[0] / 1e9
            sol_spent = max(0.0, sol_pre - sol_post)
            usd_spent = sol_spent * 180.0  # stima SOL price: aggiustata nel validate

        return fee_payer, new_token, usd_spent

    except Exception as e:
        log.debug(f"Parse tx: {e}")
        return None

# ---------------------------------------------------------------------------
# Gestione segnale rilevato
# ---------------------------------------------------------------------------

def _on_buy_detected(fee_payer: str, mint: str, usd_approx: float):
    """Chiamato quando un alpha wallet compra un token. Valida e scrivi segnale."""
    with _signaled_lock:
        if mint in _signaled_mints:
            return  # già segnalato da altro wallet
        _signaled_mints.add(mint)  # prenota subito, prima delle chiamate async

    meta_w = _wallet_meta.get(fee_payer, {})
    log.info(f"[MIRROR] Wallet {fee_payer[:12]}… (score={meta_w.get('score',0)}) compra {mint[:12]}… ~${usd_approx:.1f}")

    # DexScreener: una sola call, già cached se il token è noto
    dex = _fetch_dex(mint)
    if not dex:
        log.info(f"[MIRROR] {mint[:12]}: DexScreener non trova dati → skip")
        with _signaled_lock:
            _signaled_mints.discard(mint)
        return

    liq = dex["liquidity_usd"]
    sym = dex["token_symbol"]

    if liq < MIN_LIQ_USD:
        log.info(f"[MIRROR] {sym}: liq=${liq:.0f} < {MIN_LIQ_USD:.0f} → skip")
        with _signaled_lock:
            _signaled_mints.discard(mint)
        return

    if usd_approx < MIN_USD_BUY:
        log.info(f"[MIRROR] {sym}: buy ~${usd_approx:.1f} < {MIN_USD_BUY} → skip (dust)")
        with _signaled_lock:
            _signaled_mints.discard(mint)
        return

    if DRY_RUN:
        log.info(f"[DRY MIRROR] Segnale: {sym} | liq=${liq:.0f} | bsr={dex['bsr']:.2f} | pair={dex['pair_address'][:12]}…")
        return

    sig_id = _write_signal(mint, dex, fee_payer)
    log.info(f"[MIRROR] ✅ Segnale scritto: {sig_id} | {sym} | liq=${liq:.0f} | bsr={dex['bsr']:.2f}")

# ---------------------------------------------------------------------------
# Helius Enhanced WebSocket
# ---------------------------------------------------------------------------

def _on_open(ws):
    global _sub_id
    if not _watched_wallets:
        log.error("Nessun wallet da monitorare")
        return

    _sub_id = 420
    sub_msg = json.dumps({
        "jsonrpc": "2.0",
        "id":      _sub_id,
        "method":  "transactionSubscribe",
        "params": [
            {"accountInclude": list(_watched_wallets)},
            {
                "commitment":                  "confirmed",
                "encoding":                    "jsonParsed",
                "transactionDetails":          "full",
                "showRewards":                 False,
                "maxSupportedTransactionVersion": 0,
            },
        ],
    })
    ws.send(sub_msg)
    log.info(f"[WS] Subscribed a {len(_watched_wallets)} wallet alpha")


def _on_message(ws, raw: str):
    try:
        msg = json.loads(raw)
    except Exception:
        return

    # Conferma subscription
    if "result" in msg and isinstance(msg["result"], int):
        log.info(f"[WS] Subscription confermata (id={msg['result']})")
        return

    params = msg.get("params", {}) or {}
    result = params.get("result", {}) or {}
    tx_data = result.get("transaction", {}) or {}

    parsed = _extract_buyer_and_token(tx_data)
    if not parsed:
        return

    fee_payer, mint, usd_approx = parsed
    # Processa in thread separato per non bloccare il WS loop
    threading.Thread(
        target=_on_buy_detected,
        args=(fee_payer, mint, usd_approx),
        daemon=True,
    ).start()


def _on_error(ws, err):
    log.warning(f"[WS] Errore: {err}")


def _on_close(ws, code, msg):
    log.warning(f"[WS] Connessione chiusa (code={code}) — reconnect automatico")

# ---------------------------------------------------------------------------
# Refresh periodico wallet (ogni 6h)
# ---------------------------------------------------------------------------

def _wallet_refresh_loop():
    while True:
        time.sleep(6 * 3600)
        log.info("[MIRROR] Refresh wallet alpha da alpha_wallets.json...")
        old_count = len(_watched_wallets)
        if load_alpha_wallets():
            if len(_watched_wallets) != old_count:
                log.info("[MIRROR] Wallet aggiornati, riconnessione WS in 5s...")
                time.sleep(5)
                if _ws:
                    _ws.close()  # trigger reconnect automatico

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not HELIUS_API_KEY:
        log.error("HELIUS_API_KEY mancante nel .env")
        return

    if not WS_OK:
        log.error("Installa websocket-client: pip install websocket-client")
        return

    _ensure_csv()

    if not load_alpha_wallets():
        return

    log.info(f"[MIRROR] DRY_RUN={DRY_RUN} | min_liq=${MIN_LIQ_USD:.0f} | min_buy=${MIN_USD_BUY}")
    log.info(f"[MIRROR] Segnali → {MIRROR_CSV}")

    # Thread refresh wallet
    threading.Thread(target=_wallet_refresh_loop, daemon=True).start()

    # WebSocket con reconnect automatico (reconnect=5 = 5s tra tentativi)
    global _ws
    while True:
        try:
            _ws = websocket.WebSocketApp(
                HELIUS_WS_URL,
                on_open    = _on_open,
                on_message = _on_message,
                on_error   = _on_error,
                on_close   = _on_close,
            )
            _ws.run_forever(reconnect=5, ping_interval=30, ping_timeout=10)
        except KeyboardInterrupt:
            log.info("[MIRROR] Stop manuale")
            break
        except Exception as e:
            log.error(f"[WS] Crash: {e} — retry in 10s")
            time.sleep(10)


if __name__ == "__main__":
    main()
