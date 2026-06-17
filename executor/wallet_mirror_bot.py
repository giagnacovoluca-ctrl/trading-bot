"""
wallet_mirror_bot.py
====================
Monitora i wallet alpha in real-time e scrive segnali in mirror_signals.csv
quando comprano token.

Flusso:
  alpha_wallets.json (da wallet_alpha_finder.py)
      ↓
  logsSubscribe (RPC Solana standard, una sub per wallet con filtro `mentions`)
      ↓  signature della tx che tocca il wallet
  Helius Enhanced API /v0/transactions → parse swap (feePayer, token in, USD out)
      ↓
  DexScreener: valida liquidità (1 call/token, cache 5min)
      ↓
  mirror_signals.csv → LiveEngine lo legge → executor lo esegue

NOTA: la versione precedente usava `transactionSubscribe` su atlas-mainnet
(Helius Enhanced WS) che richiede piano developer+ → 403 sul piano attuale
(stesso problema risolto nel _RugWatcher di trade_simulator l'08/06).
Questa versione usa logsSubscribe standard + fetch Enhanced API on-trigger.

Avvio:
  python executor/wallet_mirror_bot.py          # standalone
  (oppure avviato da defi/run.py come thread)

.env (in executor/.env):
  HELIUS_API_KEY=...           (già presente)
  MIRROR_DRY_RUN=true          (false = scrive segnali reali)
  MIRROR_MAX_WALLETS=12        (top N da alpha_wallets.json)
  MIRROR_MIN_USD=5             (ignora buy < $5 equiv.)
  MIRROR_MIN_LIQ=15000         (ignora token con liq < $15k)
  MIRROR_MAX_ENHANCED_CALLS_DAY=100  (cap chiamate Enhanced API/giorno, 100 credit l'una)

NOTA COSTI (10/06): la `mentions` filter di logsSubscribe matcha QUALSIASI tx
che cita il wallet, non solo le sue. Prima di spendere una Enhanced API call
(100 credit, piano free=1M/mese → si esaurisce in giorni con 20-30 wallet) si
fa un getTransaction standard (1 credit) per verificare che il wallet sia
davvero il feePayer. Inoltre un contatore giornaliero limita le Enhanced call.
"""

import csv
import json
import logging
import os
import threading
import time
from datetime import datetime
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
MAX_WALLETS     = int(os.getenv("MIRROR_MAX_WALLETS", "12"))
MIN_USD_BUY     = float(os.getenv("MIRROR_MIN_USD", "5"))
MIN_LIQ_USD     = float(os.getenv("MIRROR_MIN_LIQ", "25000"))
MAX_ENHANCED_CALLS_DAY = int(os.getenv("MIRROR_MAX_ENHANCED_CALLS_DAY", "100"))

ROOT            = Path(__file__).parent.parent
ALPHA_FILE      = Path(__file__).parent / "alpha_wallets.json"
MIRROR_CSV      = ROOT / "defi" / "reports" / "mirror_signals.csv"
EVENTS_CSV      = ROOT / "defi" / "reports" / "wallet_events.csv"
STATE_FILE      = Path(__file__).parent / "mirror_state.json"

# Confluenza: più wallet alpha sullo stesso token entro la finestra = conviction
CONFLUENCE_WINDOW_S = 6 * 3600
# Risveglio: wallet senza attività da almeno N giorni che torna a comprare
INACTIVITY_WAKE_D   = 30

HELIUS_WS_URL    = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_PARSE_URL = f"https://api.helius.xyz/v0/transactions/?api-key={HELIUS_API_KEY}"
SOLANA_RPC_URL   = os.getenv("SOLANA_RPC_URL") or f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# Hold-time minimo: se il wallet rivende il token prima di MIN_HOLD_SECONDS
# dall'acquisto, il segnale viene cancellato (pattern bot-spray).
# Default 90s: LION/REBOUND/XP venduti in 12s → tutti loss.
MIN_HOLD_SECONDS = int(os.getenv("MIRROR_MIN_HOLD_S", "90"))

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL_MINT = "So11111111111111111111111111111111111111112"

STABLECOIN_MINTS = {
    USDC_MINT,
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    WSOL_MINT,                                          # wSOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
}

# Cache DexScreener per evitare chiamate duplicate sullo stesso token
_dex_cache: dict = {}       # mint → {data: {...}, ts: float}
_dex_lock         = threading.Lock()
DEX_CACHE_TTL     = 300     # 5 minuti

# Mint già segnalati di recente: evita duplicati da più wallet.
# TTL: dopo 6h lo stesso token può essere ri-segnalato (la vecchia versione
# usava un set senza scadenza → token bloccati fino al restart).
_signaled_mints: dict = {}   # mint → ts segnale
_signaled_lock    = threading.Lock()
SIGNAL_DEDUP_TTL  = 6 * 3600

# Firme già processate (una tx menziona il wallet anche per transfer/approve)
_seen_sigs: dict = {}        # signature → ts
_seen_lock       = threading.Lock()
SEEN_SIG_TTL     = 1800

# Budget giornaliero Enhanced API (100 credit/call sul piano Helius)
_enhanced_calls  = {"day": "", "count": 0}
_enhanced_lock   = threading.Lock()

# Wallet monitorati (caricati da alpha_wallets.json)
_watched_wallets: set = set()
_wallet_meta: dict    = {}   # wallet → {score, tokens_early_count}

# Prezzo SOL cached (per stimare il valore dei buy pagati in SOL)
_sol_price_cache = {"price": 0.0, "ts": 0.0}
SOL_PRICE_TTL    = 600

# Buy recenti per confluenza cross-wallet: mint → {wallet: ts}
_recent_buys: dict = {}
_recent_lock       = threading.Lock()

# Stato persistente: wallet → last_seen_ts (per rilevare risvegli post-inattività)
_wallet_last_seen: dict = {}
_state_lock             = threading.Lock()

# Pending signals: mint → {fee_payer, dex, confluence, woke_days, ts, timer}
# Il segnale viene scritto solo dopo MIN_HOLD_SECONDS se il wallet non ha
# già rivenduto il token (bot-spray detection).
_pending_signals: dict = {}
_pending_lock           = threading.Lock()

# Buy recenti per hold-time check: (wallet, mint) → buy_ts
_wallet_buy_ts: dict = {}
_buy_ts_lock           = threading.Lock()
_BUY_TS_TTL            = 300  # pulizia entry più vecchie di 5 min

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
# Prezzo SOL (per buy pagati in SOL nativo / wSOL)
# ---------------------------------------------------------------------------

def _sol_price() -> float:
    now = time.time()
    if now - _sol_price_cache["ts"] < SOL_PRICE_TTL and _sol_price_cache["price"] > 0:
        return _sol_price_cache["price"]
    try:
        r = requests.get(f"https://api.dexscreener.com/tokens/v1/solana/{WSOL_MINT}", timeout=8)
        r.raise_for_status()
        pairs = r.json()
        pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)
        price = float(pairs[0].get("priceUsd") or 0)
        if price > 0:
            _sol_price_cache.update(price=price, ts=now)
            return price
    except Exception as e:
        log.debug(f"SOL price fetch: {e}")
    return _sol_price_cache["price"] or 150.0

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
# Stato persistente wallet (risvegli post-inattività)
# ---------------------------------------------------------------------------

def _load_state():
    global _wallet_last_seen
    try:
        if STATE_FILE.exists():
            _wallet_last_seen = json.loads(STATE_FILE.read_text()).get("last_seen", {})
            log.info(f"[MIRROR] Stato caricato: last_seen per {len(_wallet_last_seen)} wallet")
    except Exception as e:
        log.warning(f"[MIRROR] mirror_state.json non leggibile: {e}")
        _wallet_last_seen = {}


def _touch_wallet(wallet: str) -> float:
    """Aggiorna last_seen del wallet e ritorna i giorni di inattività precedenti
    (0 se mai visto prima o attività recente)."""
    now = time.time()
    with _state_lock:
        last = _wallet_last_seen.get(wallet, 0)
        _wallet_last_seen[wallet] = now
        try:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last_seen": _wallet_last_seen}))
            tmp.replace(STATE_FILE)
        except Exception as e:
            log.debug(f"[MIRROR] salvataggio stato: {e}")
    return (now - last) / 86400 if last > 0 else 0.0

# ---------------------------------------------------------------------------
# Wallet events CSV — storico completo (anche eventi scartati): è la base
# dati per analisi pattern/feature su finestre temporali
# ---------------------------------------------------------------------------

EVENTS_CSV_HEADER = ["ts", "wallet", "side", "mint", "usd", "confluence", "wake_days", "note"]
_events_lock = threading.Lock()


def _log_event(wallet: str, side: str, mint: str, usd: float,
               confluence: int = 1, wake_days: float = 0.0, note: str = ""):
    try:
        with _events_lock:
            new = not EVENTS_CSV.exists()
            EVENTS_CSV.parent.mkdir(parents=True, exist_ok=True)
            with open(EVENTS_CSV, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(EVENTS_CSV_HEADER)
                w.writerow([datetime.now().isoformat(), wallet, side, mint,
                            f"{usd:.2f}", confluence, f"{wake_days:.1f}", note])
    except Exception as e:
        log.debug(f"[MIRROR] wallet_events.csv: {e}")

# ---------------------------------------------------------------------------
# Confluenza cross-wallet
# ---------------------------------------------------------------------------

def _register_buy(mint: str, wallet: str) -> int:
    """Registra il buy e ritorna quanti wallet alpha distinti hanno comprato
    questo mint nella finestra di confluenza."""
    now = time.time()
    with _recent_lock:
        buys = _recent_buys.setdefault(mint, {})
        buys[wallet] = now
        # purge finestra + mint senza buy recenti
        for w in [w for w, t in buys.items() if now - t > CONFLUENCE_WINDOW_S]:
            buys.pop(w, None)
        for m in [m for m, b in _recent_buys.items() if not b]:
            _recent_buys.pop(m, None)
        return len(buys)

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


def _write_signal(mint: str, dex: dict, copier_wallet: str,
                  confluence: int = 1, woke_days: float = 0.0):
    # Conviction: base 0.80, +0.05 per ogni wallet alpha oltre il primo (cap 0.95)
    prob = min(0.80 + 0.05 * (confluence - 1), 0.95)
    extra = ""
    if confluence >= 2:
        extra += f" | confluence={confluence}"
    if woke_days > 0:
        extra += f" | wake={woke_days:.0f}d"
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
        "pump_probability":  f"{prob:.2f}",
        "buy_tax":           "0.0",
        "sell_tax":          "0.0",
        "lp_locked":         "0",
        "is_honeypot":       "0",
        "top_features":      f"mirror_from={copier_wallet[:8]} | liq=${dex['liquidity_usd']:.0f} | bsr={dex['bsr']:.2f}{extra}",
    }
    with open(MIRROR_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MIRROR_CSV_HEADER)
        w.writerow(row)
    return sig_id

# ---------------------------------------------------------------------------
# Parse transazione (formato Helius Enhanced API /v0/transactions)
# ---------------------------------------------------------------------------

def _parse_enhanced_tx(tx: dict) -> Optional[tuple[str, str, str, float]]:
    """
    Estrae (side, feePayer, token_mint, usd_approx) da una tx Helius Enhanced.
    side = "buy" (riceve token non-stable, paga USDC/SOL) oppure
           "sell" (cede token non-stable, riceve USDC/SOL).
    Ritorna None se non è uno swap di un wallet monitorato.

    Valore = USDC + wSOL + SOL nativo mossi (× prezzo SOL cached).
    La vecchia versione contava solo USDC e solo i buy → perdeva i buy
    pagati in SOL (la maggioranza sui memecoin) e tutta la distribuzione.
    """
    try:
        if not tx or tx.get("transactionError"):
            return None

        fee_payer = tx.get("feePayer", "")
        if fee_payer not in _watched_wallets:
            return None

        usd_out = usd_in = 0.0
        sol_out = sol_in = 0.0
        token_in:  dict[str, float] = {}   # mint → amount ricevuto
        token_out: dict[str, float] = {}   # mint → amount ceduto

        for tr in tx.get("tokenTransfers", []) or []:
            mint     = tr.get("mint", "")
            from_acc = tr.get("fromUserAccount", "")
            to_acc   = tr.get("toUserAccount", "")
            amount   = float(tr.get("tokenAmount", 0) or 0)
            if from_acc == fee_payer:
                if mint == USDC_MINT:
                    usd_out += amount
                elif mint == WSOL_MINT:
                    sol_out += amount
                elif mint and mint not in STABLECOIN_MINTS and amount > 0:
                    token_out[mint] = token_out.get(mint, 0.0) + amount
            elif to_acc == fee_payer:
                if mint == USDC_MINT:
                    usd_in += amount
                elif mint == WSOL_MINT:
                    sol_in += amount
                elif mint and mint not in STABLECOIN_MINTS and amount > 0:
                    token_in[mint] = token_in.get(mint, 0.0) + amount

        # SOL nativo (lamports)
        for nt in tx.get("nativeTransfers", []) or []:
            amt = float(nt.get("amount", 0) or 0) / 1e9
            if nt.get("fromUserAccount") == fee_payer:
                sol_out += amt
            elif nt.get("toUserAccount") == fee_payer:
                sol_in += amt

        sol_px = _sol_price()
        spent    = usd_out + (sol_out * sol_px if sol_out > 0.001 else 0.0)
        received = usd_in  + (sol_in  * sol_px if sol_in  > 0.001 else 0.0)

        if token_in and spent > 0:
            mint = max(token_in, key=token_in.get)
            return "buy", fee_payer, mint, spent
        if token_out and received > 0:
            mint = max(token_out, key=token_out.get)
            return "sell", fee_payer, mint, received
        return None

    except Exception as e:
        log.debug(f"Parse tx: {e}")
        return None


def _get_fee_payer(signature: str) -> Optional[str]:
    """getTransaction standard (1 credito) per leggere il feePayer (primo
    accountKey). La `mentions` filter del logsSubscribe matcha anche tx dove
    il wallet è solo citato (non firmatario): pre-filtra qui prima di
    spendere una Enhanced API call (100 credit)."""
    try:
        r = requests.post(SOLANA_RPC_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
            # commitment "confirmed": il default "finalized" ritorna result=None
            # per le tx appena notificate da logsSubscribe (non ancora finalized)
            # → ogni evento veniva scartato silenziosamente (0 alert dal 10/06)
            "params": [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0,
                                    "commitment": "confirmed"}],
        }, timeout=10)
        r.raise_for_status()
        result = (r.json() or {}).get("result")
        if not result:
            return None
        return result["transaction"]["message"]["accountKeys"][0]
    except Exception as e:
        log.debug(f"getTransaction {signature[:12]}…: {e}")
        return None


def _enhanced_budget_ok() -> bool:
    """True se restano Enhanced API call nel budget odierno (reset a UTC midnight)."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with _enhanced_lock:
        if _enhanced_calls["day"] != today:
            _enhanced_calls["day"] = today
            _enhanced_calls["count"] = 0
        if _enhanced_calls["count"] >= MAX_ENHANCED_CALLS_DAY:
            return False
        _enhanced_calls["count"] += 1
        return True


def _fetch_and_process_signature(signature: str):
    """Pre-filtra il feePayer (1 credito), poi fetcha la tx via Enhanced API
    (100 credit) e processa buy/sell rilevati."""
    fee_payer = _get_fee_payer(signature)
    if fee_payer not in _watched_wallets:
        return  # mention indiretta (non è una tx del wallet) → skip

    if not _enhanced_budget_ok():
        log.debug(f"[MIRROR] budget Enhanced API esaurito ({MAX_ENHANCED_CALLS_DAY}/giorno) → skip {signature[:12]}…")
        return

    try:
        r = requests.post(HELIUS_PARSE_URL, json={"transactions": [signature]}, timeout=15)
        r.raise_for_status()
        parsed = r.json()
    except Exception as e:
        log.debug(f"Enhanced fetch {signature[:12]}…: {e}")
        return

    for tx in parsed or []:
        res = _parse_enhanced_tx(tx)
        if not res:
            continue
        side, fee_payer, mint, usd = res
        if side == "buy":
            _on_buy_detected(fee_payer, mint, usd)
        else:
            _on_sell_detected(fee_payer, mint, usd)

# ---------------------------------------------------------------------------
# Gestione segnale rilevato
# ---------------------------------------------------------------------------

def _on_buy_detected(fee_payer: str, mint: str, usd_approx: float):
    """Chiamato quando un alpha wallet compra un token. Valida e scrivi segnale."""
    wake_days  = _touch_wallet(fee_payer)
    confluence = _register_buy(mint, fee_payer)
    woke       = wake_days >= INACTIVITY_WAKE_D
    if woke:
        log.info(f"[MIRROR] ⏰ Wallet {fee_payer[:12]}… torna attivo dopo {wake_days:.0f}gg di inattività")

    now = time.time()
    with _signaled_lock:
        last = _signaled_mints.get(mint, 0)
        if now - last < SIGNAL_DEDUP_TTL:
            # già segnalato: traccia comunque la confluenza (altro wallet sullo stesso token)
            if confluence >= 2:
                log.info(f"[MIRROR] 🔥 CONFLUENZA: {confluence} wallet alpha su {mint[:12]}… in {CONFLUENCE_WINDOW_S//3600}h")
            _log_event(fee_payer, "buy", mint, usd_approx, confluence,
                       wake_days if woke else 0.0, "dedup_ttl")
            return
        _signaled_mints[mint] = now  # prenota subito, prima delle chiamate HTTP
        # purge entries scadute
        for m in [m for m, t in _signaled_mints.items() if now - t > SIGNAL_DEDUP_TTL]:
            _signaled_mints.pop(m, None)

    meta_w = _wallet_meta.get(fee_payer, {})
    log.info(f"[MIRROR] Wallet {fee_payer[:12]}… (score={meta_w.get('score',0)}) compra {mint[:12]}… ~${usd_approx:.1f}")

    def _release(reason: str):
        with _signaled_lock:
            _signaled_mints.pop(mint, None)
        _log_event(fee_payer, "buy", mint, usd_approx, confluence,
                   wake_days if woke else 0.0, reason)

    if usd_approx < MIN_USD_BUY:
        log.info(f"[MIRROR] {mint[:12]}: buy ~${usd_approx:.1f} < {MIN_USD_BUY} → skip (dust)")
        _release("skip_dust")
        return

    # DexScreener: una sola call, già cached se il token è noto
    dex = _fetch_dex(mint)
    if not dex:
        log.info(f"[MIRROR] {mint[:12]}: DexScreener non trova dati → skip")
        _release("skip_no_dex")
        return

    liq = dex["liquidity_usd"]
    sym = dex["token_symbol"]

    if liq < MIN_LIQ_USD:
        log.info(f"[MIRROR] {sym}: liq=${liq:.0f} < {MIN_LIQ_USD:.0f} → skip")
        _release("skip_low_liq")
        return

    _log_event(fee_payer, "buy", mint, usd_approx, confluence,
               wake_days if woke else 0.0, f"signal sym={sym}" + (" DRY" if DRY_RUN else ""))

    if DRY_RUN:
        log.info(f"[DRY MIRROR] Segnale: {sym} | liq=${liq:.0f} | bsr={dex['bsr']:.2f} | "
                 f"confluence={confluence} | pair={dex['pair_address'][:12]}…")
        return

    # Registra buy_ts per hold-time check e schedula segnale differito
    now_ts = time.time()
    with _buy_ts_lock:
        _wallet_buy_ts[(fee_payer, mint)] = now_ts
        # purge entry scadute
        stale = [k for k, t in _wallet_buy_ts.items() if now_ts - t > _BUY_TS_TTL]
        for k in stale:
            _wallet_buy_ts.pop(k, None)

    def _emit():
        with _pending_lock:
            if mint not in _pending_signals:
                return  # già cancellato da sell rapido
            _pending_signals.pop(mint, None)
        sig_id = _write_signal(mint, dex, fee_payer, confluence=confluence,
                               woke_days=wake_days if woke else 0.0)
        log.info(f"[MIRROR] ✅ Segnale scritto: {sig_id} | {sym} | liq=${liq:.0f} | "
                 f"bsr={dex['bsr']:.2f} | confluence={confluence} "
                 f"(hold≥{MIN_HOLD_SECONDS}s confermato)")

    timer = threading.Timer(MIN_HOLD_SECONDS, _emit)
    with _pending_lock:
        _pending_signals[mint] = {
            "fee_payer": fee_payer, "sym": sym, "ts": now_ts, "timer": timer,
        }
    timer.start()
    log.info(f"[MIRROR] ⏳ {sym}: segnale in attesa {MIN_HOLD_SECONDS}s hold-time check")


def _on_sell_detected(fee_payer: str, mint: str, usd_approx: float):
    """Alpha wallet in uscita da un token: log + warning se il token era stato
    segnalato di recente (smart money distribuisce mentre noi siamo dentro).
    Se il sell arriva entro MIN_HOLD_SECONDS dal buy, cancella il segnale pendente."""
    wake_days = _touch_wallet(fee_payer)
    now_ts = time.time()

    with _signaled_lock:
        recently_signaled = (now_ts - _signaled_mints.get(mint, 0)) < SIGNAL_DEDUP_TTL

    # Hold-time check: cancella segnale se questo wallet ha venduto troppo presto
    with _buy_ts_lock:
        buy_ts = _wallet_buy_ts.get((fee_payer, mint), 0)
    hold_s = now_ts - buy_ts if buy_ts else None

    cancelled = False
    if hold_s is not None and hold_s < MIN_HOLD_SECONDS:
        with _pending_lock:
            pending = _pending_signals.pop(mint, None)
        if pending and pending.get("fee_payer") == fee_payer:
            pending["timer"].cancel()
            cancelled = True
            sym = pending.get("sym", mint[:8])
            log.warning(
                f"[MIRROR] 🚫 {sym}: segnale CANCELLATO — wallet {fee_payer[:12]}… "
                f"ha venduto in {hold_s:.0f}s < {MIN_HOLD_SECONDS}s (bot-spray)"
            )
            # Rimuovi anche dalla dedup per non bloccare un segnale legittimo
            with _signaled_lock:
                _signaled_mints.pop(mint, None)

    note = "sell_after_signal" if recently_signaled else ("quick_flip_cancelled" if cancelled else "")
    _log_event(fee_payer, "sell", mint, usd_approx,
               wake_days=wake_days if wake_days >= INACTIVITY_WAKE_D else 0.0, note=note)

    if cancelled:
        pass  # già loggato sopra
    elif recently_signaled:
        log.warning(f"[MIRROR] ⚠️ SMART MONEY IN USCITA: wallet {fee_payer[:12]}… vende "
                    f"{mint[:12]}… (~${usd_approx:.0f}) su token segnalato di recente")
    else:
        log.info(f"[MIRROR] Wallet {fee_payer[:12]}… vende {mint[:12]}… ~${usd_approx:.0f}")

# ---------------------------------------------------------------------------
# WebSocket: logsSubscribe per wallet (stesso pattern del _RugWatcher)
# ---------------------------------------------------------------------------

class _WalletWatcher:
    """Una subscription `logsSubscribe` con filtro `mentions` per ogni wallet
    alpha. Ogni notifica porta la signature della tx → fetch Enhanced API.
    Se l'RPC nega le subscription (-32403) si disabilita con errore esplicito."""

    _RECONNECT_S    = 10
    _MAX_SUB_ERRORS = 3

    def __init__(self, stop_event: threading.Event):
        self._stop          = stop_event
        self._ws            = None
        self._lock          = threading.Lock()
        self._sub_by_wallet: dict = {}   # wallet → subscription_id
        self._wallet_by_sub: dict = {}   # subscription_id → wallet
        self._pending: dict       = {}   # request_id → wallet
        self._sub_errors    = 0
        self._req_counter   = 0
        self.disabled       = False

    # -- lifecycle -----------------------------------------------------------
    def run(self):
        while not self._stop.is_set() and not self.disabled:
            try:
                ws = websocket.WebSocketApp(
                    HELIUS_WS_URL,
                    on_open=self._on_open, on_message=self._on_message,
                    on_error=self._on_error, on_close=self._on_close,
                )
                ws.run_forever(reconnect=self._RECONNECT_S, ping_interval=30, ping_timeout=10)
            except Exception as e:
                log.warning(f"[WS] crash: {e}")
            if not self._stop.is_set() and not self.disabled:
                time.sleep(self._RECONNECT_S)

    def close(self):
        if self._ws:
            try: self._ws.close()
            except Exception: pass

    # -- subscription management ----------------------------------------------
    def _next_req_id(self) -> int:
        self._req_counter += 1
        return self._req_counter

    def _subscribe_wallet(self, wallet: str):
        if not self._ws:
            return
        with self._lock:
            req_id = self._next_req_id()
            self._pending[req_id] = wallet
        try:
            self._ws.send(json.dumps({
                "jsonrpc": "2.0", "id": req_id, "method": "logsSubscribe",
                "params": [{"mentions": [wallet]}, {"commitment": "confirmed"}],
            }))
        except Exception as e:
            log.debug(f"[WS] subscribe {wallet[:8]}…: {e}")

    def _unsubscribe_wallet(self, wallet: str):
        if not self._ws:
            return
        with self._lock:
            sub_id = self._sub_by_wallet.pop(wallet, None)
            if sub_id is not None:
                self._wallet_by_sub.pop(sub_id, None)
        if sub_id is None:
            return
        try:
            self._ws.send(json.dumps({
                "jsonrpc": "2.0", "id": self._next_req_id(),
                "method": "logsUnsubscribe", "params": [sub_id],
            }))
        except Exception as e:
            log.debug(f"[WS] unsubscribe {wallet[:8]}…: {e}")

    def sync_wallets(self):
        """Allinea le subscription all'insieme corrente di _watched_wallets."""
        with self._lock:
            current = set(self._sub_by_wallet) | set(self._pending.values())
        for w in _watched_wallets - current:
            self._subscribe_wallet(w)
        for w in current - _watched_wallets:
            self._unsubscribe_wallet(w)

    # -- callbacks -------------------------------------------------------------
    def _on_open(self, ws):
        self._ws = ws
        with self._lock:
            self._sub_by_wallet.clear()
            self._wallet_by_sub.clear()
            self._pending.clear()
        for w in list(_watched_wallets):
            self._subscribe_wallet(w)
        log.info(f"[WS] Connesso — subscribe a {len(_watched_wallets)} wallet alpha (logsSubscribe)")

    def _on_message(self, ws, raw: str):
        try:
            msg = json.loads(raw)
        except Exception:
            return

        # Conferma/errore subscribe
        if "id" in msg:
            req_id = msg.get("id")
            with self._lock:
                wallet = self._pending.pop(req_id, None)
            if wallet is None:
                return
            err = msg.get("error")
            if err is not None:
                with self._lock:
                    self._sub_errors += 1
                    n_err = self._sub_errors
                code = err.get("code") if isinstance(err, dict) else err
                log.warning(f"[WS] subscribe rifiutata per {wallet[:8]}… ({code})")
                if code == -32403 or n_err >= self._MAX_SUB_ERRORS:
                    self.disabled = True
                    log.error("[WS] logsSubscribe non disponibile su questo piano → mirror bot disattivato")
                    self.close()
                return
            with self._lock:
                self._sub_errors = 0
                sub_id = msg.get("result")
                if isinstance(sub_id, int):
                    self._sub_by_wallet[wallet] = sub_id
                    self._wallet_by_sub[sub_id] = wallet
            return

        # logsNotification: {"params": {"subscription": id, "result": {"value": {"signature": ..., "err": ...}}}}
        try:
            params = msg.get("params") or {}
            sub_id = params.get("subscription")
            value  = ((params.get("result") or {}).get("value")) or {}
            if value.get("err"):
                return
            sig = value.get("signature", "")
            with self._lock:
                wallet = self._wallet_by_sub.get(sub_id)
            if not wallet or not sig:
                return
        except Exception:
            return

        now = time.time()
        with _seen_lock:
            if sig in _seen_sigs:
                return
            _seen_sigs[sig] = now
            for s in [s for s, t in _seen_sigs.items() if now - t > SEEN_SIG_TTL]:
                _seen_sigs.pop(s, None)

        # Fetch + parse in thread separato per non bloccare il WS loop
        threading.Thread(
            target=_fetch_and_process_signature, args=(sig,), daemon=True,
        ).start()

    def _on_error(self, ws, err):
        log.debug(f"[WS] errore: {err}")

    def _on_close(self, ws, code, msg):
        log.warning(f"[WS] connessione chiusa (code={code}) — reconnect automatico")

# ---------------------------------------------------------------------------
# Refresh periodico wallet (ogni 6h)
# ---------------------------------------------------------------------------

def _wallet_refresh_loop(watcher: "_WalletWatcher", stop_event: threading.Event):
    while not stop_event.wait(6 * 3600):
        log.info("[MIRROR] Refresh wallet alpha da alpha_wallets.json...")
        old = set(_watched_wallets)
        if load_alpha_wallets() and _watched_wallets != old:
            log.info(f"[MIRROR] Watchlist cambiata ({len(old)}→{len(_watched_wallets)}), sync subscription...")
            watcher.sync_wallets()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(stop_event: Optional[threading.Event] = None):
    if not HELIUS_API_KEY:
        log.error("HELIUS_API_KEY mancante nel .env")
        raise SystemExit(1)

    if not WS_OK:
        log.error("Installa websocket-client: pip install websocket-client")
        raise SystemExit(1)

    _ensure_csv()
    _load_state()

    if not load_alpha_wallets():
        raise SystemExit(1)

    stop_event = stop_event or threading.Event()

    log.info(f"[MIRROR] DRY_RUN={DRY_RUN} | min_liq=${MIN_LIQ_USD:.0f} | min_buy=${MIN_USD_BUY}")
    log.info(f"[MIRROR] Segnali → {MIRROR_CSV}")

    watcher = _WalletWatcher(stop_event)
    threading.Thread(target=_wallet_refresh_loop, args=(watcher, stop_event), daemon=True).start()

    try:
        watcher.run()   # blocca fino a stop_event/disabled
    except KeyboardInterrupt:
        log.info("[MIRROR] Stop manuale")
    finally:
        watcher.close()

    if watcher.disabled:
        raise SystemExit(1)   # evita restart-loop in run.py: il piano non supporta le sub


if __name__ == "__main__":
    main()
