"""
bsc_executor.py
===============
Execution layer per trade reali su BNB Smart Chain (BSC).

Legge le decisioni di trade_simulator.py da live_trades.csv
ed esegue swap via PancakeSwap v2.

Architettura:
  trade_simulator.py  →  live_trades.csv  →  bsc_executor.py
                                                    ↓
                                          PancakeSwap v2 Router
                                                    ↓
                                          BSC RPC (broadcast tx)
                                                    ↓
                                    bsc_real_state.json + bsc_executions.csv

Setup:
  1. Aggiungi a .env:
       BSC_PRIVATE_KEY=<chiave_privata_hex>
       BSC_RPC_URL=https://bsc-dataseed.binance.org/
       BSC_DRY_RUN=true
       BSC_TRADE_SIZE_USDT=10.0

Ctrl+C:
  - Primo  → countdown 5s, poi chiude posizioni aperte
  - Secondo entro 5s → stop immediato senza chiudere
"""

import csv
import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# web3 + eth_account
try:
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
    from eth_account import Account
    WEB3_AVAILABLE = True
except ImportError:
    WEB3_AVAILABLE = False

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bsc_exec")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROD_DIR  = os.path.dirname(os.path.abspath(__file__))
DEFI_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BSC_STATE_FILE = os.path.join(PROD_DIR, "bsc_real_state.json")
BSC_EXEC_CSV   = os.path.join(PROD_DIR, "bsc_executions.csv")
LIVE_CSV       = os.path.join(DEFI_ROOT, "defi", "reports", "live_trades.csv")
DEFI_SIGNALS   = os.path.join(DEFI_ROOT, "defi", "reports", "signals_log.csv")
V3_SIGNALS     = os.path.join(DEFI_ROOT, "gemme", "reports", "gems_log_v3.csv")

# ---------------------------------------------------------------------------
# Config da .env
# ---------------------------------------------------------------------------
def _env(k, default=None):
    return os.environ.get(k, default)

DRY_RUN        = _env("BSC_DRY_RUN", "true").lower() != "false"
TRADE_SIZE_USDT = float(_env("BSC_TRADE_SIZE_USDT", "10.0"))
BSC_RPC_URL    = _env("BSC_RPC_URL", "https://bsc-dataseed.binance.org/")
MAX_IMPACT     = float(_env("BSC_MAX_IMPACT_PCT", "3.0"))
MAX_POS        = int(_env("BSC_MAX_OPEN_POSITIONS", "5"))
SLIPPAGE_BPS   = int(_env("BSC_SLIPPAGE_BPS", "200"))   # 2% default
GAS_PRICE_GWEI = float(_env("BSC_GAS_PRICE_GWEI", "5.0"))

SUPPORTED_CHAINS = {"bsc"}

# ---------------------------------------------------------------------------
# Indirizzi BSC
# ---------------------------------------------------------------------------
PANCAKE_ROUTER_V2   = Web3.to_checksum_address("0x10ED43C718714eb63d5aA57B78B54704E256024E") if WEB3_AVAILABLE else ""
# Fallback DEX: BiSwap (spesso ha pool che PancakeSwap v2 non ha)
BISWAP_ROUTER      = Web3.to_checksum_address("0x3a6d8cA21D1CF76F653A67577FA0D27453350dD8") if WEB3_AVAILABLE else ""
USDT_BSC          = Web3.to_checksum_address("0x55d398326f99059fF775485246999027B3197955") if WEB3_AVAILABLE else ""
BUSD_BSC          = Web3.to_checksum_address("0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56") if WEB3_AVAILABLE else ""
WBNB_BSC          = Web3.to_checksum_address("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c") if WEB3_AVAILABLE else ""

ROUTER_ABI = [
    {"name": "getAmountsOut", "type": "function",
     "inputs":  [{"name": "amountIn", "type": "uint256"}, {"name": "path", "type": "address[]"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}],
     "stateMutability": "view"},
    {"name": "swapExactTokensForTokens", "type": "function",
     "inputs":  [{"name": "amountIn",    "type": "uint256"},
                 {"name": "amountOutMin","type": "uint256"},
                 {"name": "path",        "type": "address[]"},
                 {"name": "to",          "type": "address"},
                 {"name": "deadline",    "type": "uint256"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}],
     "stateMutability": "nonpayable"},
]

ERC20_ABI = [
    {"name": "balanceOf", "type": "function",
     "inputs":  [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
    {"name": "decimals", "type": "function",
     "inputs":  [], "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view"},
    {"name": "approve", "type": "function",
     "inputs":  [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable"},
    {"name": "allowance", "type": "function",
     "inputs":  [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view"},
]

# ---------------------------------------------------------------------------
# Web3 setup
# ---------------------------------------------------------------------------
_w3: Optional["Web3"] = None
_account = None
_decimals_cache: dict = {}

def _get_w3() -> Optional["Web3"]:
    global _w3
    if _w3 and _w3.is_connected():
        return _w3
    if not WEB3_AVAILABLE:
        return None
    try:
        w3 = Web3(Web3.HTTPProvider(BSC_RPC_URL, request_kwargs={"timeout": 15}))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        if w3.is_connected():
            _w3 = w3
            return _w3
    except Exception as e:
        log.warning(f"[Web3] Connessione BSC fallita: {e}")
    return None

def _load_account():
    global _account
    if _account:
        return _account
    key = _env("BSC_PRIVATE_KEY", "")
    if not key or key.startswith("INSERISCI"):
        return None
    try:
        if not key.startswith("0x"):
            key = "0x" + key
        _account = Account.from_key(key)
        return _account
    except Exception as e:
        log.error(f"[Wallet] Errore caricamento chiave BSC: {e}")
        return None

def _get_token_decimals(token_addr: str) -> int:
    if token_addr in _decimals_cache:
        return _decimals_cache[token_addr]
    w3 = _get_w3()
    if not w3:
        return 18
    try:
        tok = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
        dec = tok.functions.decimals().call()
        _decimals_cache[token_addr] = dec
        return dec
    except Exception:
        return 18

# ---------------------------------------------------------------------------
# Quote PancakeSwap v2
# ---------------------------------------------------------------------------
def pancake_quote(token_in: str, token_out: str, amount_in_wei: int) -> Optional[dict]:
    """
    Quota token_in → token_out cercando su più DEX BSC in cascata:
    1. PancakeSwap v2 (path diretto + via WBNB)
    2. BiSwap (fallback per token non su PancakeSwap v2)
    """
    w3 = _get_w3()
    if not w3:
        log.warning("[BSC] Web3 non disponibile")
        return None

    t_in  = Web3.to_checksum_address(token_in)
    t_out = Web3.to_checksum_address(token_out)
    paths = [[t_in, t_out], [t_in, WBNB_BSC, t_out], [t_in, BUSD_BSC, t_out]]

    for router_addr, dex_name in [(PANCAKE_ROUTER_V2, "PancakeSwap"), (BISWAP_ROUTER, "BiSwap")]:
        router = w3.eth.contract(address=router_addr, abi=ROUTER_ABI)
        for path in paths:
            try:
                amounts = router.functions.getAmountsOut(amount_in_wei, path).call()
                out     = amounts[-1]
                if out > 0:
                    if dex_name != "PancakeSwap":
                        log.info(f"[{dex_name}] Route trovata per {token_in[:8]}… ({len(path)} hop)")
                    return {"outAmount": out, "path": path, "priceImpactPct": 0.0, "dex": dex_name}
            except Exception:
                continue

    log.warning(f"[BSC] Nessuna route su PancakeSwap/BiSwap per {token_in[:8]}…→{token_out[:8]}…")
    return None

# ---------------------------------------------------------------------------
# Approve ERC-20
# ---------------------------------------------------------------------------
def _ensure_approval(token_addr: str, amount_wei: int, owner: str) -> bool:
    """Approva il router PancakeSwap se l'allowance è insufficiente."""
    w3 = _get_w3()
    if not w3 or not _account:
        return DRY_RUN
    try:
        tok       = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI)
        allowance = tok.functions.allowance(owner, PANCAKE_ROUTER_V2).call()
        if allowance >= amount_wei:
            return True
        nonce    = w3.eth.get_transaction_count(owner)
        gas_p    = w3.to_wei(GAS_PRICE_GWEI, "gwei")
        tx       = tok.functions.approve(PANCAKE_ROUTER_V2, 2**256 - 1).build_transaction({
            "from": owner, "nonce": nonce, "gasPrice": gas_p, "gas": 60_000,
        })
        signed   = w3.eth.account.sign_transaction(tx, _account.key)
        tx_hash  = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        log.info(f"[Approve] {token_addr[:8]}… approvato: {tx_hash.hex()[:16]}…")
        return True
    except Exception as e:
        log.error(f"[Approve] Errore: {e}")
        return False

# ---------------------------------------------------------------------------
# Stato reale
# ---------------------------------------------------------------------------
def load_bsc_state() -> dict:
    if not os.path.exists(BSC_STATE_FILE):
        return {}
    try:
        with open(BSC_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_bsc_state(state: dict):
    try:
        tmp = BSC_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, BSC_STATE_FILE)
    except Exception as e:
        log.warning(f"[State] Errore salvataggio: {e}")

def _ensure_exec_csv():
    if not os.path.exists(BSC_EXEC_CSV):
        with open(BSC_EXEC_CSV, "w", newline="") as f:
            csv.writer(f).writerow(["ts","signal_id","token_symbol","action",
                                    "token_address","tokens_amount","usdt_amount",
                                    "tx_hash","status","note"])

def log_execution(row: dict):
    _ensure_exec_csv()
    with open(BSC_EXEC_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts","signal_id","token_symbol","action",
                                           "token_address","tokens_amount","usdt_amount",
                                           "tx_hash","status","note"])
        w.writerow(row)

# ---------------------------------------------------------------------------
# Token lookup
# ---------------------------------------------------------------------------
def build_token_lookup() -> dict:
    lookup = {}
    for path, sid_col in [(DEFI_SIGNALS, "signal_id"), (V3_SIGNALS, "gem_id")]:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    sid   = r.get(sid_col, "") or r.get("signal_id", "")
                    taddr = r.get("token_address", "")
                    chain = r.get("chain", "")
                    if sid and taddr and chain == "bsc" and sid not in lookup:
                        lookup[sid] = taddr
        except Exception:
            pass
    return lookup

# ---------------------------------------------------------------------------
# Execute BUY
# ---------------------------------------------------------------------------
def execute_buy(signal_id: str, token_symbol: str, token_address: str,
                system: str, real_state: dict) -> bool:
    if not token_address:
        log.warning(f"[BUY] {signal_id}: nessun token_address → skip")
        return False

    decimals    = _get_token_decimals(token_address)
    usdt_wei    = int(TRADE_SIZE_USDT * 10**18)   # USDT ha 18 dec su BSC
    slippage    = SLIPPAGE_BPS / 10_000

    log.info(f"[BUY] {signal_id} | {token_symbol} | {TRADE_SIZE_USDT} USDT | slippage={SLIPPAGE_BPS}bps")

    quote = pancake_quote(USDT_BSC, token_address, usdt_wei)
    if not quote:
        log.error(f"[BUY] Nessuna route per {token_symbol}")
        log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                       "token_symbol": token_symbol, "action": "buy_failed",
                       "token_address": token_address, "tokens_amount": "0",
                       "usdt_amount": TRADE_SIZE_USDT, "tx_hash": "", "status": "error",
                       "note": "no_route"})
        return False

    tokens_out    = quote["outAmount"] / (10 ** decimals)
    tokens_min    = int(quote["outAmount"] * (1 - slippage))
    tx_hash       = "DRY_RUN"
    status        = "dry_run"

    if not DRY_RUN:
        acc = _load_account()
        w3  = _get_w3()
        if not acc or not w3:
            log.error("[BUY] Wallet o Web3 non disponibili")
            return False
        if not _ensure_approval(USDT_BSC, usdt_wei, acc.address):
            log.error("[BUY] Approve USDT fallito")
            return False
        try:
            router   = w3.eth.contract(address=PANCAKE_ROUTER_V2, abi=ROUTER_ABI)
            deadline = int(time.time()) + 300
            nonce    = w3.eth.get_transaction_count(acc.address)
            gas_p    = w3.to_wei(GAS_PRICE_GWEI, "gwei")
            tx       = router.functions.swapExactTokensForTokens(
                usdt_wei, tokens_min, quote["path"], acc.address, deadline
            ).build_transaction({"from": acc.address, "nonce": nonce,
                                  "gasPrice": gas_p, "gas": 300_000})
            signed   = w3.eth.account.sign_transaction(tx, acc.key)
            tx_hash  = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            status   = "sent"
            log.info(f"[BUY] ✅ TX inviata: {tx_hash}")
        except Exception as e:
            log.error(f"[BUY] TX fallita: {e}")
            return False
    else:
        log.info(f"[DRY BUY] ~{tokens_out:.4f} {token_symbol} per {TRADE_SIZE_USDT} USDT")

    real_state[signal_id] = {
        "signal_id":     signal_id,
        "token_symbol":  token_symbol,
        "token_address": token_address,
        "chain":         "bsc",
        "system":        system,
        "decimals":      decimals,
        "tokens_held":   tokens_out,
        "tokens_bought": tokens_out,
        "usdt_spent":    TRADE_SIZE_USDT,
        "usdt_received": 0.0,
        "status":        "open",
        "entry_ts":      datetime.now().isoformat(),
        "entry_tx":      tx_hash,
        "exit_txs":      [],
        "real_pnl_usdt": -TRADE_SIZE_USDT,
        "sell_fail_count": 0,
    }
    log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                   "token_symbol": token_symbol, "action": "buy",
                   "token_address": token_address, "tokens_amount": f"{tokens_out:.6f}",
                   "usdt_amount": TRADE_SIZE_USDT, "tx_hash": tx_hash,
                   "status": status, "note": f"path={len(quote['path'])}hops"})
    return True

# ---------------------------------------------------------------------------
# Execute SELL
# ---------------------------------------------------------------------------
def execute_sell(signal_id: str, sell_fraction: float, action_label: str,
                 real_state: dict) -> bool:
    pos = real_state.get(signal_id)
    if not pos:
        log.warning(f"[SELL] {signal_id}: posizione non trovata")
        return False
    if pos.get("status") == "closed":
        log.debug(f"[SELL] {signal_id}: già chiusa")
        return True
    if pos.get("status") == "stuck":
        log.warning(f"[SELL] {signal_id}: posizione STUCK, skip")
        return False

    token_address = pos["token_address"]
    token_symbol  = pos["token_symbol"]
    decimals      = pos.get("decimals", 18)
    tokens_held   = pos.get("tokens_held", 0.0)
    tokens_sell   = tokens_held * sell_fraction
    if tokens_sell <= 0:
        log.warning(f"[SELL] {signal_id}: nessun token da vendere")
        return False

    sell_wei = int(tokens_sell * (10 ** decimals))
    slippage = SLIPPAGE_BPS / 10_000

    log.info(f"[SELL] {signal_id} | {action_label} | {tokens_sell:.4f} {token_symbol} ({sell_fraction*100:.0f}%)")

    quote = pancake_quote(token_address, USDT_BSC, sell_wei)
    if not quote:
        prev_fail = pos.get("sell_fail_count", 0)
        pos["sell_fail_count"] = prev_fail + 1
        STUCK_THRESHOLD = 3
        if pos["sell_fail_count"] >= STUCK_THRESHOLD and pos.get("status") != "stuck":
            pos["status"] = "stuck"
            log.error(f"[SELL] ⚠ POSIZIONE BLOCCATA: {signal_id} — {pos['sell_fail_count']} tentativi, "
                      f"nessuna route PancakeSwap. Vendere manualmente su PancakeSwap.")
            log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                           "token_symbol": token_symbol, "action": f"{action_label}_stuck",
                           "token_address": token_address, "tokens_amount": f"{tokens_sell:.6f}",
                           "usdt_amount": "0", "tx_hash": "", "status": "stuck",
                           "note": f"no_route x{pos['sell_fail_count']}"})
        elif prev_fail == 0:
            # Log solo al primo fallimento, poi silenzioso fino a stuck
            log.error(f"[SELL] Nessuna route per {token_symbol} (tentativo 1/{STUCK_THRESHOLD})")
            log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                           "token_symbol": token_symbol, "action": f"{action_label}_failed",
                           "token_address": token_address, "tokens_amount": f"{tokens_sell:.6f}",
                           "usdt_amount": "0", "tx_hash": "", "status": "error", "note": "no_route"})
        else:
            log.warning(f"[SELL] Nessuna route per {token_symbol} (tentativo {pos['sell_fail_count']}/{STUCK_THRESHOLD})")
        return False

    usdt_out  = quote["outAmount"] / 10**18
    usdt_min  = int(quote["outAmount"] * (1 - slippage))
    tx_hash   = "DRY_RUN"
    status    = "dry_run"

    if not DRY_RUN:
        acc = _load_account()
        w3  = _get_w3()
        if not acc or not w3:
            log.error("[SELL] Wallet o Web3 non disponibili")
            return False
        if not _ensure_approval(token_address, sell_wei, acc.address):
            log.error("[SELL] Approve token fallito")
            return False
        try:
            router   = w3.eth.contract(address=PANCAKE_ROUTER_V2, abi=ROUTER_ABI)
            deadline = int(time.time()) + 300
            nonce    = w3.eth.get_transaction_count(acc.address)
            gas_p    = w3.to_wei(GAS_PRICE_GWEI, "gwei")
            tx       = router.functions.swapExactTokensForTokens(
                sell_wei, usdt_min, quote["path"], acc.address, deadline
            ).build_transaction({"from": acc.address, "nonce": nonce,
                                  "gasPrice": gas_p, "gas": 300_000})
            signed   = w3.eth.account.sign_transaction(tx, acc.key)
            tx_hash  = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            status   = "sent"
            log.info(f"[SELL] ✅ TX inviata: {tx_hash}")
        except Exception as e:
            log.error(f"[SELL] TX fallita: {e}")
            return False
    else:
        log.info(f"[DRY SELL] {tokens_sell:.4f} {token_symbol} → ~{usdt_out:.2f} USDT")

    pos["tokens_held"]   -= tokens_sell
    pos["usdt_received"] += usdt_out
    pos["exit_txs"].append(tx_hash)
    pos["real_pnl_usdt"]  = pos["usdt_received"] - pos["usdt_spent"]
    pos["sell_fail_count"] = 0

    if sell_fraction >= 1.0 or pos["tokens_held"] <= 0.0001:
        pos["status"]    = "closed"
        pos["close_ts"]  = datetime.now().isoformat()
        log.info(f"[SELL] 🏁 {signal_id} chiusa | P&L reale: {pos['real_pnl_usdt']:+.2f} USDT")

    log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                   "token_symbol": token_symbol, "action": action_label,
                   "token_address": token_address, "tokens_amount": f"{tokens_sell:.6f}",
                   "usdt_amount": f"{usdt_out:.2f}", "tx_hash": tx_hash,
                   "status": status, "note": f"pnl={pos['real_pnl_usdt']:+.2f}"})
    return True

# ---------------------------------------------------------------------------
# Process row from live_trades.csv
# ---------------------------------------------------------------------------
EXIT_ACTIONS = {"trail_exit","tp2","manual_pause","manual_close","hard_sl",
                "exit_bsr_collapse","exit_vol_crash","liq_collapse","sl_adaptive"}

def process_row(row: dict, processed: set, real_state: dict, token_lookup: dict) -> bool:
    sid    = row.get("signal_id", "").strip()
    action = row.get("action",    "").strip()
    chain  = row.get("chain",     "").strip().lower()
    system = row.get("system",    "").strip()

    if not sid or not action:
        return False
    key = f"{sid}|{action}"
    if key in processed:
        return False

    if chain not in SUPPORTED_CHAINS:
        processed.add(key)
        return False

    if action == "entry":
        if sid in real_state and real_state[sid].get("status") == "open":
            processed.add(key)
            return False
        open_pos = sum(1 for p in real_state.values() if p.get("status") == "open")
        if open_pos >= MAX_POS:
            log.info(f"[BUY] Limite {MAX_POS} posizioni aperte raggiunto — skip {sid}")
            processed.add(key)
            return False
        token_address = token_lookup.get(sid, "")
        if not token_address:
            # NON aggiungere a processed: il lookup potrebbe essere stantio
            log.warning(f"[BUY] {sid}: token_address non trovato — riproverò al prossimo ciclo")
            return False
        sym = row.get("token_symbol", "?")
        ok = execute_buy(sid, sym, token_address, system, real_state)
        processed.add(key)
        return ok

    elif action in EXIT_ACTIONS:
        if sid not in real_state or real_state[sid].get("status") != "open":
            return False
        frac = 1.0
        if action == "tp1":
            frac = 0.5
        return execute_sell(sid, frac, action, real_state)

    elif action == "tp1":
        if sid not in real_state or real_state[sid].get("status") != "open":
            return False
        return execute_sell(sid, 0.5, "tp1", real_state)

    return False

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    log.info("=" * 60)
    log.info("  BSC Executor avviato")
    log.info(f"  DRY_RUN      = {DRY_RUN}")
    log.info(f"  TRADE_SIZE   = {TRADE_SIZE_USDT} USDT/trade")
    log.info(f"  MAX_IMPACT   = {MAX_IMPACT}%")
    log.info(f"  MAX_POS      = {MAX_POS}")
    log.info(f"  RPC          = {BSC_RPC_URL}")
    log.info("=" * 60)

    if not WEB3_AVAILABLE:
        log.error("web3 non installato — pip install web3")
        return

    # Verifica connessione BSC
    w3 = _get_w3()
    if w3:
        block = w3.eth.block_number
        log.info(f"  BSC connessa — blocco #{block:,}")
    else:
        log.warning("  BSC RPC non raggiungibile — continuo in DRY_RUN")

    if DRY_RUN:
        log.info("  [DRY RUN] Nessuna tx reale")
    else:
        acc = _load_account()
        if acc:
            log.info(f"  Wallet: {acc.address}")
        else:
            log.error("  BSC_PRIVATE_KEY mancante in .env — uscita")
            return

    real_state   = load_bsc_state()
    token_lookup = build_token_lookup()
    processed    = set()

    # Bootstrap: marca righe storiche come già viste
    if os.path.exists(LIVE_CSV):
        open_sids   = {sid for sid, p in real_state.items() if p.get("status") == "open"}
        bought_sids = set(real_state.keys())
        now_ts      = datetime.now()
        RETRY_WINDOW_H = 24   # riprova solo entry delle ultime 24h non ancora comprate
        snap = 0
        try:
            with open(LIVE_CSV, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    sid    = row.get("signal_id","").strip()
                    action = row.get("action","").strip()
                    chain  = row.get("chain","").strip().lower()
                    if chain != "bsc":
                        continue
                    key = f"{sid}|{action}"
                    if sid in open_sids and action != "entry":
                        continue
                    # ENTRY mai comprata: lascia libera solo se recente (<24h)
                    if action == "entry" and sid not in bought_sids:
                        try:
                            entry_age_h = (now_ts - datetime.fromisoformat(
                                row.get("ts",""))).total_seconds() / 3600
                            if entry_age_h < RETRY_WINDOW_H:
                                continue  # entry recente → lascia libera per retry
                        except Exception:
                            pass  # se non parsabile, marca come vista
                    processed.add(key)
                    snap += 1
        except Exception as e:
            log.warning(f"Bootstrap fallito: {e}")
        if snap:
            log.info(f"  [BOOTSTRAP] {snap} righe BSC storiche marcate")

    open_pos = sum(1 for p in real_state.values() if p.get("status") == "open")
    stuck    = [(s,p) for s,p in real_state.items() if p.get("status") == "stuck"]
    log.info(f"  Posizioni BSC caricate: {len(real_state)} | Aperte: {open_pos}")
    if stuck:
        for sid, p in stuck:
            log.error(f"  ⚠ STUCK: {sid} ({p.get('token_symbol','?')}) — vendere su PancakeSwap")
    log.info(f"  Token in lookup: {len(token_lookup)}")
    log.info("  In ascolto su live_trades.csv (BSC)...")
    log.info("-" * 60)

    # Ctrl+C: countdown 5s, poi chiude posizioni
    _last_ctrl_c = [0.0]
    _abort       = [False]

    def _on_sigint(sig, frame):
        now = time.time()
        if now - _last_ctrl_c[0] < 6.0:
            log.info("[bsc] Stop immediato — posizioni lasciate aperte.")
            _abort[0] = True
            raise SystemExit(0)
        _last_ctrl_c[0] = now
        log.info("[bsc] ⏸  Ctrl+C — Ctrl+C di nuovo entro 5s per uscire SENZA chiudere.")
        log.info("[bsc]    Altrimenti chiusura automatica tra 5 secondi...")
        def _countdown():
            for i in range(5, 0, -1):
                if _abort[0]: return
                log.info(f"[bsc]    Chiusura in {i}s...")
                time.sleep(1)
            if _abort[0]: return
            for sid, pos in list(real_state.items()):
                if pos.get("status") == "open":
                    execute_sell(sid, 1.0, "manual_close", real_state)
            save_bsc_state(real_state)
            raise SystemExit(0)
        threading.Thread(target=_countdown, daemon=True).start()

    signal.signal(signal.SIGINT, _on_sigint)

    cycle = 0
    while not _abort[0]:
        try:
            cycle += 1
            if cycle % 10 == 0:
                token_lookup = build_token_lookup()

            if not os.path.exists(LIVE_CSV):
                time.sleep(5)
                continue

            with open(LIVE_CSV, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

            actions = 0
            for row in rows:
                if process_row(row, processed, real_state, token_lookup):
                    actions += 1
                    save_bsc_state(real_state)

            if actions:
                op = sum(1 for p in real_state.values() if p.get("status") == "open")
                log.info(f"[ciclo {cycle}] {actions} azioni BSC | aperte: {op}")

        except SystemExit:
            save_bsc_state(real_state)
            raise
        except Exception as e:
            log.error(f"Errore ciclo: {e}")

        time.sleep(10)


if __name__ == "__main__":
    main()
