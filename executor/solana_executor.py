"""
solana_executor.py
==================
Execution layer per trade reali su Solana.

Legge le decisioni di trade_simulator.py da live_trades.csv
ed esegue swap reali via Jupiter V6 API.

Architettura:
  trade_simulator.py  →  live_trades.csv  →  solana_executor.py
                                                     ↓
                                           Jupiter V6 API (swap)
                                                     ↓
                                           Solana RPC (broadcast tx)
                                                     ↓
                                           real_executions.csv + real_state.json

Setup:
  1. cp .env.example .env  →  inserisci la chiave privata
  2. pip install solders base58 python-dotenv requests
  3. py solana_executor.py          (DRY_RUN=true in .env → test sicuro)
  4. py solana_executor.py          (DRY_RUN=false → trade reali)

IMPORTANTE:
  - Tieni sempre DRY_RUN=true finché non hai verificato che tutto funzioni
  - Il bot gestisce solo chain=solana; BSC/ETH verranno aggiunti dopo
  - Non committare mai .env su git (già nel .gitignore)
"""

import base64
import csv
import json
import logging
import os
import signal
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Dipendenze opzionali (installate solo se DRY_RUN=false)
# ---------------------------------------------------------------------------
try:
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    import base58
    SOLDERS_OK = True
except ImportError:
    SOLDERS_OK = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # .env letto manualmente sotto

import sys as _sys, site as _site
_sys.path.insert(0, str(Path(__file__).parent.parent / "defi"))
_sys.path.insert(0, _site.getusersitepackages())  # pumpswap-sdk installato in user site-packages
try:
    from rugcheck import is_safe as rugcheck_safe
except ImportError:
    def rugcheck_safe(mint, scanner, chain="solana"): return True

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sol_exec")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# PROD_DIR  = questa cartella (Produzione\)
# DEFI_ROOT = cartella defi\ (un livello sopra) — dove vivono i dati del bot
PROD_DIR         = os.path.dirname(os.path.abspath(__file__))
DEFI_ROOT        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Output → dentro Produzione\ (qui)
REAL_STATE_FILE  = os.path.join(PROD_DIR, "real_state.json")
REAL_EXEC_CSV    = os.path.join(PROD_DIR, "real_executions.csv")

# Input → cartella defi\ principale (un livello sopra)
LIVE_CSV            = os.path.join(DEFI_ROOT, "defi","reports",         "live_trades.csv")
DEFI_SIGNALS_CSV    = os.path.join(DEFI_ROOT, "defi", "reports",         "signals_log.csv")
PUMP_GRAD_SIGNALS_CSV = os.path.join(DEFI_ROOT, "defi", "reports",       "pump_grad_signals.csv")
MIRROR_SIGNALS_CSV    = os.path.join(DEFI_ROOT, "defi", "reports",       "mirror_signals.csv")
PRE_GRAD_SIGNALS_CSV  = os.path.join(DEFI_ROOT, "defi", "reports",       "pre_grad_signals.csv")
V2_SIGNALS_CSV      = os.path.join(DEFI_ROOT, "gemme", "reports",        "gems_log.csv")
V3_SIGNALS_CSV      = os.path.join(DEFI_ROOT, "gemme", "reports",        "gems_log_v3.csv")

# ---------------------------------------------------------------------------
# Costanti Solana
# ---------------------------------------------------------------------------
USDC_MINT     = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_DECIMALS = 6

# rugcheck_safe importato da defi/rugcheck.py (include LP lock check + pump_grad top holder check)

# Jupiter API endpoints in ordine di preferenza.
# lite.jup.ag non risolve su alcune reti (DNS issue) → commentato.
# api.jup.ag/v1 è l'endpoint attivo confermato.
JUPITER_ENDPOINTS = [
    {
        "quote": "https://api.jup.ag/swap/v1/quote",
        "swap":  "https://api.jup.ag/swap/v1/swap",
        "label": "api.jup.ag/v1",
    },
    # Nota: quote-api.jup.ag/v6 è deprecato (DNS non risolve).
    # api.jup.ag/v6 non esiste (404). Un solo endpoint attivo.
]

# Raydium Trade API — fallback quando Jupiter è down.
_RAYDIUM_SWAP_HOST = "https://transaction-v1.raydium.io"
# Indice dell'endpoint attivo
_jup_endpoint_idx = 0

# Cooldown per token illiquidi: nessuna route Jupiter (400) → salta per 2h.
# { token_address: {"fails": N, "skip_until": datetime} }
_quote_fail_cache: dict = {}

# True = Jupiter API non raggiungibile (429/timeout/5xx) — problema temporaneo.
# False = successo o 400 no-route (problema del token, non dell'API).
_jup_api_down: bool = False

# ---------------------------------------------------------------------------
# Carica configurazione da .env
# ---------------------------------------------------------------------------
def _env(key: str, default=None):
    return os.environ.get(key, default)

DRY_RUN           = _env("DRY_RUN", "true").lower() != "false"

# DRY_RUN per sistema: sovrascrive il flag globale per sistemi specifici.
# Es: DRY_RUN=false + DRY_RUN_SYSTEMS=defi,v3 → pump_grad reale, defi/v3 simulati.
# Se vuoto, tutti i sistemi usano il flag globale DRY_RUN.
_dry_run_systems_raw = _env("DRY_RUN_SYSTEMS", "")
DRY_RUN_SYSTEMS = {s.strip().lower() for s in _dry_run_systems_raw.split(",") if s.strip()}

def _is_dry_run(system: str) -> bool:
    """Ritorna True se questo sistema deve girare in dry_run."""
    if system in DRY_RUN_SYSTEMS:
        return True
    return DRY_RUN
TRADE_SIZE_USDC        = float(_env("TRADE_SIZE_USDC", "100.0"))
TRADE_SIZE_SOL_PUMP    = float(_env("TRADE_SIZE_SOL_PUMP", "0.065"))
TRADE_SIZE_SOL_PRE_GRAD = float(_env("TRADE_SIZE_SOL_PRE_GRAD", "0.030"))
MAX_PRICE_IMPACT    = float(_env("MAX_PRICE_IMPACT_PCT", "3.0"))
MAX_OPEN_POS        = int(_env("MAX_OPEN_POSITIONS", "5"))
_helius_key = _env("HELIUS_API_KEY", "")
RPC_URL = (
    _env("SOLANA_RPC_URL")
    or (f"https://mainnet.helius-rpc.com/?api-key={_helius_key}" if _helius_key else None)
    or "https://api.mainnet-beta.solana.com"
)
PRIVATE_KEY_RAW     = _env("SOLANA_PRIVATE_KEY", "")

# Slippage per sistema (basis points)
SLIPPAGE_BPS = {
    "defi":      int(_env("SLIPPAGE_DEFI_BPS",      "200")),
    "v2":        int(_env("SLIPPAGE_V2_BPS",         "150")),
    "v3":        int(_env("SLIPPAGE_V3_BPS",         "100")),
    "v3_large":  int(_env("SLIPPAGE_V3_LARGE_BPS",   "50")),
    "pump_grad": int(_env("SLIPPAGE_PUMP_GRAD_BPS",  "800")),  # token illiquidi appena graduati
    "mirror":    int(_env("SLIPPAGE_MIRROR_BPS",     "800")),  # stesso di pump_grad: token volatili post-graduation
    "pre_grad":  int(_env("SLIPPAGE_PRE_GRAD_BPS",  "1000")), # bonding curve: più volatile, slippage più alto
}

# Frazione di posizione venduta a TP1 (deve corrispondere a trade_simulator.py CONFIGS)
TP1_FRACTION = {
    "defi":      0.50,
    "v2":        0.50,
    "v3":        0.50,
    "v3_large":  0.40,
    "pump_grad": 1.00,   # vende tutto al tp1: pool ancora liquida, evita tp1_trail su pool morte
    "pre_grad":  1.00,   # vende tutto al tp1 (come pump_grad)
}

# Sistemi abilitati: "" = tutti, altrimenti lista separata da virgola (es. "pump_grad")
# Utile per testare un solo sistema con wallet reale senza eseguire gli altri.
_systems_raw     = _env("SYSTEMS_ENABLED", "")
SYSTEMS_ENABLED  = {s.strip().lower() for s in _systems_raw.split(",") if s.strip()} if _systems_raw else set()

# Azioni che chiudono l'intera posizione residua
EXIT_ACTIONS = {
    "exit_bsr_collapse", "exit_vol_crash", "exit_low_liq",
    "exit_adaptive", "exit_price_timeout", "sl_adaptive",
    "trail_exit", "tp2", "manual_pause", "manual_close",
    "purged_stale",
}

# Chain gestite da questo executor
SUPPORTED_CHAINS = {"solana"}

# ---------------------------------------------------------------------------
# Wallet
# ---------------------------------------------------------------------------

def _load_keypair() -> Optional[object]:
    """Carica Keypair da chiave privata (base58 o array JSON)."""
    if not SOLDERS_OK:
        log.error("Installa solders e base58: pip install solders base58")
        return None
    if not PRIVATE_KEY_RAW:
        log.error("SOLANA_PRIVATE_KEY non impostata nel .env")
        return None
    try:
        # Prova formato JSON array: [12,34,56,...]
        if PRIVATE_KEY_RAW.strip().startswith("["):
            secret = bytes(json.loads(PRIVATE_KEY_RAW.strip()))
            return Keypair.from_bytes(secret)
        else:
            # Formato base58
            secret = base58.b58decode(PRIVATE_KEY_RAW.strip())
            return Keypair.from_bytes(secret)
    except Exception as e:
        log.error(f"Errore caricamento keypair: {e}")
        return None


# ---------------------------------------------------------------------------
# Jupiter API — con auto-fallback tra endpoint
# ---------------------------------------------------------------------------

def _jup_ep() -> dict:
    """Ritorna l'endpoint Jupiter attivo."""
    return JUPITER_ENDPOINTS[_jup_endpoint_idx]


def jupiter_quote(input_mint: str, output_mint: str, amount_lamports: int,
                  slippage_bps: int = 100) -> Optional[dict]:
    """Richiede un preventivo di swap, prova tutti gli endpoint in ordine.

    Se un token fallisce 3 volte consecutive (es. nessuna route su Jupiter),
    viene messo in cooldown 30 min per non spammare l'API ogni ciclo.
    """
    global _jup_endpoint_idx, _jup_api_down

    # Cooldown: token illiquidi che non hanno route su Jupiter
    cache = _quote_fail_cache.get(input_mint)
    if cache and cache.get("skip_until") and datetime.now() < cache["skip_until"]:
        mins_left = int((cache["skip_until"] - datetime.now()).total_seconds() / 60)
        log.debug(f"[Jupiter] {input_mint[:12]}… in cooldown ({mins_left}min rimanenti) — skip quote.")
        return None

    params = {
        "inputMint":        input_mint,
        "outputMint":       output_mint,
        "amount":           str(amount_lamports),
        "slippageBps":      str(slippage_bps),
        "onlyDirectRoutes": "false",
        "maxAccounts":      "64",   # limita complessità tx: previene "Transaction too large: 1852 bytes"
    }

    for i, ep in enumerate(JUPITER_ENDPOINTS):
        try:
            resp = requests.get(ep["quote"], params=params, timeout=15)
            if resp.status_code == 429:
                log.warning(f"[Jupiter] {ep['label']} rate limit (429) — attendo 2s...")
                time.sleep(2)
                resp = requests.get(ep["quote"], params=params, timeout=15)
            # 400 = nessuna route disponibile per questo token → cooldown immediato 2h
            if resp.status_code == 400:
                entry = _quote_fail_cache.setdefault(input_mint, {"fails": 0, "skip_until": None})
                entry["fails"] += 1
                entry["skip_until"] = datetime.now() + timedelta(hours=2)
                log.warning(
                    f"[Jupiter] {input_mint[:16]}… 400 No Route → cooldown 2h "
                    f"(token illiquido su Jupiter, impossibile vendere via DEX)."
                )
                _jup_api_down = False
                return None
            resp.raise_for_status()
            if i != _jup_endpoint_idx:
                log.info(f"[Jupiter] Endpoint attivo ora: {ep['label']}")
                _jup_endpoint_idx = i
            _quote_fail_cache.pop(input_mint, None)
            _jup_api_down = False
            return resp.json()
        except Exception as e:
            log.warning(f"[Jupiter] {ep['label']} fallito: {e}")

    # Fallimento generico (429/timeout/5xx): API temporaneamente down, non token morto.
    # Non mettere in cooldown per-token: quando l'API torna su, il token potrebbe essere vendibile.
    _jup_api_down = True
    log.error("[Jupiter] Tutti gli endpoint quote hanno fallito (API down).")
    return None


def jupiter_swap_tx(quote: dict, user_pubkey: str) -> Optional[str]:
    """Ottiene la transazione serializzata, usa l'endpoint attivo."""
    ep = _jup_ep()
    try:
        body = {
            "quoteResponse":             quote,
            "userPublicKey":             user_pubkey,
            "wrapAndUnwrapSol":          True,
            "dynamicComputeUnitLimit":   True,
            "prioritizationFeeLamports": "auto",
        }
        resp = requests.post(ep["swap"], json=body, timeout=20)
        resp.raise_for_status()
        return resp.json().get("swapTransaction")
    except Exception as e:
        log.warning(f"[Jupiter] swap tx fallita ({ep['label']}): {e}")
        return None


# ---------------------------------------------------------------------------
# Raydium API — fallback swap quando Jupiter è down
# ---------------------------------------------------------------------------

def raydium_quote(input_mint: str, output_mint: str,
                  amount_lamports: int, slippage_bps: int = 100) -> Optional[dict]:
    """Quote via Raydium Trade API. Ritorna l'intero response (serve per la tx)."""
    try:
        r = requests.get(
            f"{_RAYDIUM_SWAP_HOST}/compute/swap-base-in",
            params={
                "inputMint":  input_mint,
                "outputMint": output_mint,
                "amount":     str(amount_lamports),
                "slippageBps": str(slippage_bps),
                "txVersion":  "V0",
            },
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("success"):
                return data
        log.debug(f"[Raydium] quote {r.status_code}: {r.text[:120]}")
    except Exception as e:
        log.debug(f"[Raydium] quote error: {e}")
    return None


def _get_token_ata(wallet_pubkey: str, mint: str) -> Optional[str]:
    """Ritorna l'indirizzo del primo ATA per il token nel wallet (via RPC)."""
    try:
        result = rpc_call("getTokenAccountsByOwner", [
            wallet_pubkey,
            {"mint": mint},
            {"encoding": "jsonParsed"},
        ])
        accounts = result.get("result", {}).get("value", [])
        if accounts:
            return accounts[0]["pubkey"]
    except Exception as e:
        log.debug(f"[Raydium] get_ata {mint[:8]}: {e}")
    return None


def raydium_swap_tx(quote_response: dict, wallet_pubkey: str,
                    input_ata: Optional[str], output_ata: Optional[str]) -> Optional[str]:
    """Ottiene tx serializzata (base64) da Raydium per il quote dato."""
    try:
        body = {
            "computeUnitPriceMicroLamports": "100000",
            "swapResponse": quote_response,
            "txVersion": "V0",
            "wallet": wallet_pubkey,
            "wrapSol": False,
            "unwrapSol": False,
            "inputAccount": input_ata,
            "outputAccount": output_ata,
        }
        r = requests.post(
            f"{_RAYDIUM_SWAP_HOST}/transaction/swap-base-in",
            json=body,
            timeout=20,
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("success") and data.get("data"):
                return data["data"][0]["transaction"]
            log.debug(f"[Raydium] swap_tx failure: {data.get('msg')}")
    except Exception as e:
        log.debug(f"[Raydium] swap_tx error: {e}")
    return None


# ---------------------------------------------------------------------------
# PumpSwap SDK — swap diretto on-chain per token pump_grad (no rate limit)
# ---------------------------------------------------------------------------

_sol_price_cache: dict = {"price": 0.0, "ts": 0.0}


def _get_sol_price_usd() -> float:
    """SOL/USD corrente da DexScreener, cache 5 min."""
    if time.time() - _sol_price_cache["ts"] < 300 and _sol_price_cache["price"] > 0:
        return _sol_price_cache["price"]
    try:
        r = requests.get(
            "https://api.dexscreener.com/tokens/v1/solana/So11111111111111111111111111111111112",
            timeout=5, headers={"User-Agent": "Mozilla/5.0"}
        )
        if r.status_code == 200:
            data = r.json()
            pairs = data if isinstance(data, list) else data.get("pairs", [])
            for p in pairs:
                if "usdc" in (p.get("quoteToken", {}).get("symbol") or "").lower():
                    price = float(p.get("priceUsd") or 0)
                    if price > 0:
                        _sol_price_cache["price"] = price
                        _sol_price_cache["ts"] = time.time()
                        return price
    except Exception:
        pass
    return _sol_price_cache["price"] or 220.0


def _pumpswap_buy(mint: str, sol_amount: float, slippage_pct: float = 8.0) -> Optional[dict]:
    """
    Acquista token PumpSwap direttamente on-chain (no API esterne, no rate limit).
    Ritorna {"tx_id": str, "tokens_out": float, "sol_spent": float} oppure None.
    """
    try:
        import asyncio
        os.environ["HTTPS_RPC_ENDPOINT"] = RPC_URL
        os.environ["BUY_SLIPPAGE"]       = str(slippage_pct)
        os.environ["SWAP_PRIORITY_FEE"]  = "100000"
        from pumpswap_sdk import PumpSwapSDK
        result = asyncio.run(PumpSwapSDK().buy(mint, sol_amount, PRIVATE_KEY_RAW))
        if result.get("status") and result.get("data"):
            return {
                "tx_id":      str(result["data"]["tx_id"]),
                "tokens_out": float(result["data"]["token_amount"]),
                "sol_spent":  sol_amount,
            }
        log.error(f"[PumpSwap] buy failed: {result.get('message')}")
    except Exception as e:
        log.error(f"[PumpSwap] buy exception: {e}")
    return None


def _pumpswap_sell(mint: str, token_amount: float, slippage_pct: float = 8.0) -> Optional[dict]:
    """
    Vende token PumpSwap direttamente on-chain.
    Ritorna {"tx_id": str, "sol_received": float} oppure None.
    """
    try:
        import asyncio
        os.environ["HTTPS_RPC_ENDPOINT"] = RPC_URL
        os.environ["SELL_SLIPPAGE"]      = str(slippage_pct)
        os.environ["SWAP_PRIORITY_FEE"]  = "100000"
        from pumpswap_sdk import PumpSwapSDK
        result = asyncio.run(PumpSwapSDK().sell(mint, token_amount, PRIVATE_KEY_RAW))
        if result.get("status") and result.get("data"):
            return {
                "tx_id":        str(result["data"]["tx_id"]),
                "sol_received": float(result["data"].get("sol_amount", 0)),
            }
        log.error(f"[PumpSwap] sell failed: {result.get('message')}")
    except Exception as e:
        log.error(f"[PumpSwap] sell exception: {e}")
    return None


# ---------------------------------------------------------------------------
# Solana RPC
# ---------------------------------------------------------------------------

def rpc_call(method: str, params: list) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    resp = requests.post(RPC_URL, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def send_transaction(tx_b64: str) -> Optional[str]:
    """Firma e invia una transazione serializzata. Ritorna il tx hash."""
    keypair = _load_keypair()
    if not keypair:
        return None
    try:
        tx_bytes = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(tx_bytes)
        # VersionedTransaction (V0) non ha .sign() — va ricostruita con il keypair
        tx = VersionedTransaction(tx.message, [keypair])
        signed_b64 = base64.b64encode(bytes(tx)).decode()
        result = rpc_call("sendTransaction", [
            signed_b64,
            {"encoding": "base64", "skipPreflight": False,
             "preflightCommitment": "confirmed"}
        ])
        if "error" in result:
            log.error(f"RPC errore invio tx: {result['error']}")
            return None
        return result.get("result")
    except Exception as e:
        log.error(f"Errore firma/invio tx: {e}")
        return None


def get_usdc_balance(pubkey: str) -> float:
    """Ritorna il saldo USDC del wallet (in USDC, non lamports)."""
    try:
        result = rpc_call("getTokenAccountsByOwner", [
            pubkey,
            {"mint": USDC_MINT},
            {"encoding": "jsonParsed"}
        ])
        accounts = result.get("result", {}).get("value", [])
        total = 0.0
        for acc in accounts:
            amt = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += float(amt.get("uiAmount", 0) or 0)
        return total
    except Exception as e:
        log.warning(f"Errore lettura saldo USDC: {e}")
        return 0.0


def get_token_balance(pubkey: str, token_mint: str) -> float:
    """Ritorna il saldo di un token SPL (in unità leggibili)."""
    try:
        result = rpc_call("getTokenAccountsByOwner", [
            pubkey,
            {"mint": token_mint},
            {"encoding": "jsonParsed"}
        ])
        accounts = result.get("result", {}).get("value", [])
        total = 0.0
        for acc in accounts:
            amt = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += float(amt.get("uiAmount", 0) or 0)
        return total
    except Exception as e:
        log.warning(f"Errore lettura saldo token {token_mint[:8]}: {e}")
        return 0.0


def get_token_decimals(token_mint: str) -> int:
    """Ritorna i decimali di un token SPL (default 6)."""
    try:
        result = rpc_call("getAccountInfo", [
            token_mint,
            {"encoding": "jsonParsed"}
        ])
        info = result.get("result", {}).get("value", {})
        if info:
            data = info.get("data", {})
            if isinstance(data, dict):
                return int(data.get("parsed", {}).get("info", {}).get("decimals", 6))
    except Exception:
        pass
    return 6  # default per la maggior parte dei token Solana


# ---------------------------------------------------------------------------
# Token address lookup (signal_id → token_address)
# ---------------------------------------------------------------------------

def build_token_lookup() -> dict:
    """
    Costruisce la mappa signal_id → token_address leggendo tutti i CSV segnali
    e, come fallback, tracker_state.json (cattura segnali non ancora nel CSV).
    Aggiornato ad ogni ciclo per catturare nuovi segnali.
    """
    lookup = {}
    sources = [
        (DEFI_SIGNALS_CSV,       "signal_id"),
        (PUMP_GRAD_SIGNALS_CSV,  "signal_id"),
        (MIRROR_SIGNALS_CSV,     "signal_id"),
        (PRE_GRAD_SIGNALS_CSV,   "signal_id"),
        (V2_SIGNALS_CSV,         "gem_id"),
        (V3_SIGNALS_CSV,         "gem_id"),
    ]
    for path, id_col in sources:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    sid   = row.get(id_col, "").strip()
                    taddr = row.get("token_address", "").strip()
                    chain = row.get("chain", "").strip().lower()
                    # Salta gli address che sono ID CoinGecko (es. "cg_zest")
                    # invece di veri mint Solana (32-44 char base58)
                    if taddr.startswith("cg_") or len(taddr) < 30:
                        continue
                    if sid and taddr and chain == "solana":
                        lookup[sid] = taddr
        except Exception as e:
            log.debug(f"Lookup {path}: {e}")

    # Fallback: tracker_state.json — cattura segnali registrati in memoria
    # ma non ancora scritti in signals_log.csv (es. riavvio bot, race condition)
    tracker_state_path = os.path.join(DEFI_ROOT, "reports", "tracker_state.json")
    if os.path.exists(tracker_state_path):
        try:
            with open(tracker_state_path, encoding="utf-8") as f:
                state = json.load(f)
            added = 0
            for sid, meta in state.items():
                if sid in lookup:
                    continue   # già trovato nei CSV, non sovrascrivere
                taddr = str(meta.get("token_address", "") or "").strip()
                chain = str(meta.get("chain", "") or "").strip().lower()
                if taddr.startswith("cg_") or len(taddr) < 30:
                    continue
                if sid and taddr and chain == "solana":
                    lookup[sid] = taddr
                    added += 1
            if added:
                log.debug(f"Token lookup: +{added} da tracker_state.json")
        except Exception as e:
            log.debug(f"Lookup tracker_state.json: {e}")

    log.debug(f"Token lookup: {len(lookup)} segnali indicizzati")
    return lookup


# ---------------------------------------------------------------------------
# Stato reale (real_state.json)
# ---------------------------------------------------------------------------

def load_real_state() -> dict:
    if os.path.exists(REAL_STATE_FILE):
        try:
            return json.load(open(REAL_STATE_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_real_state(state: dict):
    try:
        with open(REAL_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.warning(f"Errore salvataggio real_state: {e}")
    # Rigenera il dashboard HTML dopo ogni salvataggio
    try:
        from Produzione.executor_report import build_executor_report
        build_executor_report()
    except Exception as _e:
        log.debug(f"[report] Dashboard non aggiornato: {_e}")


# ---------------------------------------------------------------------------
# Log esecuzioni reali (real_executions.csv)
# ---------------------------------------------------------------------------

EXEC_FIELDNAMES = [
    "ts", "signal_id", "token_symbol", "chain", "action",
    "token_address", "tokens_amount", "usdc_amount",
    "price_actual", "slippage_pct", "price_impact_pct",
    "tx_hash", "status", "note",
]


def _ensure_exec_csv():
    if not os.path.exists(REAL_EXEC_CSV):
        with open(REAL_EXEC_CSV, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=EXEC_FIELDNAMES).writeheader()


def log_execution(row: dict):
    _ensure_exec_csv()
    with open(REAL_EXEC_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EXEC_FIELDNAMES)
        w.writerow({k: row.get(k, "") for k in EXEC_FIELDNAMES})


# ---------------------------------------------------------------------------
# Core: esecuzione swap
# ---------------------------------------------------------------------------

def execute_buy(signal_id: str, token_symbol: str, token_address: str,
                system: str, real_state: dict, keypair_pubkey: str,
                entry_price: float = 0.0) -> bool:
    """
    Acquista `TRADE_SIZE_USDC` di token con USDC via Jupiter.
    Retry progressivo: se Jupiter non trova route al primo tentativo,
    riprova con slippage crescente (token neolistati o liquidità frammentata).
    """
    base_slippage = SLIPPAGE_BPS.get(system, 100)
    dry = _is_dry_run(system)

    # DRY: notional fisso $100 (allineato a CAPITAL_EUR del simulator) invece dei
    # size reali da .env — il dry valida i segnali, non la gestione del capitale
    _DRY_NOTIONAL_USD = 100.0
    _size_usdc = TRADE_SIZE_USDC
    _size_sol  = TRADE_SIZE_SOL_PRE_GRAD
    if dry:
        _size_usdc = _DRY_NOTIONAL_USD
        _sol_px = _get_sol_price_usd()
        if _sol_px and _sol_px > 0:
            _size_sol = round(_DRY_NOTIONAL_USD / _sol_px, 6)

    # pre_grad: compra sulla bonding curve con SOL (non USDC)
    _is_pre_grad  = (system == "pre_grad")
    WSOL_MINT     = "So11111111111111111111111111111111111111112"
    if _is_pre_grad:
        _sol_lamports = int(_size_sol * 1e9)
        input_mint    = WSOL_MINT
        input_amount  = _sol_lamports
    else:
        input_mint   = USDC_MINT
        input_amount = int(_size_usdc * (10 ** USDC_DECIMALS))

    usdc_lamports = input_amount  # variabile usata anche più avanti nei retry
    # Tentativi: (slippage_bps, delay_sec_prima_del_tentativo)
    # Slippage max cap 600bps (6%) per evitare fill catastrofici
    SLIPPAGE_MAX_BPS = 600
    _retries = [
        (base_slippage,                          0),
        (min(int(base_slippage * 1.5), SLIPPAGE_MAX_BPS),  5),
        (min(int(base_slippage * 2.5), SLIPPAGE_MAX_BPS), 15),
    ]

    _size_label = (f"{_size_sol} SOL" if _is_pre_grad
                   else f"{_size_usdc} USDC")
    log.info(f"[BUY] {signal_id} | {token_symbol} | {_size_label} | base_slippage={base_slippage}bps")

    if not dry:
        if _is_pre_grad:
            sol_bal = rpc_call("getBalance", [keypair_pubkey, {"commitment": "confirmed"}])
            sol_available = (sol_bal or 0) / 1e9
            if sol_available < _size_sol + 0.01:  # +0.01 SOL per fee
                log.error(f"[BUY] Saldo SOL insufficiente: {sol_available:.4f} < {_size_sol}")
                return False
        else:
            balance = get_usdc_balance(keypair_pubkey)
            if balance < _size_usdc:
                log.error(f"[BUY] Saldo USDC insufficiente: {balance:.2f} < {_size_usdc}")
                return False

    # RugCheck: blocca token con LP non bloccato (rug risk)
    # pre_grad: skip (token non ancora su DEX, LP non esiste)
    # LIQ_*: skip top_holder (liq_monitor filtra già liq>$25k; LP locked da pump.fun)
    # mirror: dopo il fix effective_system, viene passato "mirror" → _check_lp_lock → None → True
    _skip_rugcheck = _is_pre_grad or signal_id.startswith("LIQ_")
    if not _skip_rugcheck and not rugcheck_safe(token_address, system, chain="solana"):
        log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                       "token_symbol": token_symbol, "action": "buy_skipped",
                       "token_address": token_address, "status": "skipped",
                       "note": "rugcheck_failed"})
        return False

    # ── v3/defi: Raydium primario, Jupiter fallback ───────────────────────────
    # pump_grad/pre_grad: Jupiter diretto con input_mint appropriato
    _raydium_resp: Optional[dict] = None
    quote = None
    used_slippage = base_slippage

    if system not in ("pump_grad", "pre_grad"):
        _raydium_resp = raydium_quote(USDC_MINT, token_address, usdc_lamports, base_slippage)
        if _raydium_resp:
            log.info(f"[BUY] Route su Raydium ✓ (slippage={base_slippage}bps)")

    if not _raydium_resp:
        if system not in ("pump_grad", "pre_grad"):
            log.info(f"[BUY] Raydium non disponibile — provo Jupiter...")
        for attempt, (slippage, delay) in enumerate(_retries, 1):
            if delay > 0:
                log.info(f"[BUY] Jupiter tentativo {attempt}/{len(_retries)} slippage={slippage}bps (attendo {delay}s...)")
                time.sleep(delay)
            jup_q = jupiter_quote(input_mint, token_address, input_amount, slippage)
            if jup_q:
                quote = jup_q
                used_slippage = slippage
                if attempt > 1:
                    log.info(f"[BUY] Route Jupiter al tentativo {attempt} con slippage={slippage}bps")
                break
            log.warning(f"[BUY] Jupiter tentativo {attempt}: nessun quote (slippage={slippage}bps)")

    if not _raydium_resp and not quote:
        log.error(f"[BUY] Nessuna route su Raydium né Jupiter per {token_symbol} ({token_address[:12]}...)")
        log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                       "token_symbol": token_symbol, "action": "buy_failed",
                       "token_address": token_address, "status": "error",
                       "note": "no_route_raydium_jupiter"})
        return False

    # Estrai price_impact e tokens_out dal DEX che ha risposto
    if _raydium_resp:
        _rd = _raydium_resp.get("data", {})
        price_impact        = float(_rd.get("priceImpactPct", 0) or 0)
        tokens_out_lamports = int(_rd.get("outputAmount", 0))
    else:
        price_impact        = float(quote.get("priceImpactPct", 0) or 0)
        tokens_out_lamports = int(quote.get("outAmount", 0))

    # Verifica price impact
    if price_impact > MAX_PRICE_IMPACT:
        log.warning(f"[BUY] Price impact troppo alto: {price_impact:.2f}% > {MAX_PRICE_IMPACT}% → skip")
        log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                       "token_symbol": token_symbol, "action": "buy_skipped",
                       "token_address": token_address, "price_impact_pct": f"{price_impact:.2f}",
                       "status": "skipped", "note": f"price_impact>{MAX_PRICE_IMPACT}%"})
        return False

    # Calcola decimali: sempre via RPC (anche in DRY_RUN) per evitare il bug
    # 6-vs-9 decimali che gonfia/sgonfia tokens_out di 1000x
    decimals   = get_token_decimals(token_address)
    tokens_out = tokens_out_lamports / (10 ** decimals)
    price_actual  = _size_usdc / tokens_out if tokens_out > 0 else 0

    # Filtro prezzo stantio: segnale già dumpato prima dell'acquisto → skip.
    # La soglia è slippage_pct + 12% per escludere la differenza fisiologica
    # tra il mid price del segnale (DexScreener) e il prezzo eseguito (quote DEX).
    # Es. pump_grad 800bps: 8% slippage + 12% = soglia 20%.
    _slippage_pct = used_slippage / 100
    MAX_ENTRY_DROP_MARGIN = {"pump_grad": 12.0}
    _margin = MAX_ENTRY_DROP_MARGIN.get(system)
    if _margin and entry_price > 0 and price_actual > 0:
        _max_drop = _slippage_pct + _margin
        _drop_pct = (entry_price - price_actual) / entry_price * 100
        if _drop_pct > _max_drop:
            log.warning(
                f"[BUY] {token_symbol}: prezzo calato {_drop_pct:.1f}% dal segnale "
                f"({entry_price:.6g} → {price_actual:.6g}) > -{_max_drop:.1f}% "
                f"(slippage={_slippage_pct:.0f}%+margin={_margin:.0f}%) → skip"
            )
            log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                           "token_symbol": token_symbol, "action": "buy_skipped",
                           "token_address": token_address, "status": "skipped",
                           "note": f"entry_drop={_drop_pct:.1f}%>{_max_drop:.1f}%"})
            return False

    tx_hash = "DRY_RUN"
    status  = "dry_run"

    if not dry:
        if _raydium_resp:
            input_ata  = _get_token_ata(keypair_pubkey, USDC_MINT)
            output_ata = _get_token_ata(keypair_pubkey, token_address)  # None se token nuovo → Raydium crea l'ATA
            swap_tx    = raydium_swap_tx(_raydium_resp, keypair_pubkey, input_ata, output_ata)
            provider   = "Raydium"
        else:
            swap_tx  = jupiter_swap_tx(quote, keypair_pubkey)
            provider = "Jupiter"
            # Retry con quote fresco se la swap TX build fallisce (race condition pool nuova)
            if not swap_tx:
                log.warning(f"[BUY] Jupiter swap_tx fallita — retry con quote fresco in 3s...")
                time.sleep(3)
                _fresh_quote = jupiter_quote(USDC_MINT, token_address, usdc_lamports, used_slippage)
                if _fresh_quote:
                    swap_tx = jupiter_swap_tx(_fresh_quote, keypair_pubkey)
                    if swap_tx:
                        quote = _fresh_quote
        if not swap_tx:
            log.error(f"[BUY] Transazione {provider} non ottenuta per {signal_id}")
            return False
        tx_hash = send_transaction(swap_tx)
        if not tx_hash:
            log.error(f"[BUY] Invio tx fallito per {signal_id}")
            return False

        # Verifica conferma on-chain: controlla il saldo token reale dopo 2s.
        # Evita posizioni fantasma quando la tx viene rigettata dal programma
        # (es. slippage exceeded 6001, insufficient funds, ecc.) pur avendo
        # restituito un hash valido.
        time.sleep(2)
        actual_balance = get_token_balance(keypair_pubkey, token_address)
        if actual_balance <= 0:
            log.error(
                f"[BUY] ⚠ TX inviata ma saldo token = 0 dopo 2s → tx fallita on-chain "
                f"(probabile slippage exceeded o program error) | tx={tx_hash[:20]}…"
            )
            log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                           "token_symbol": token_symbol, "action": "buy_failed",
                           "token_address": token_address, "tx_hash": tx_hash,
                           "status": "failed_onchain", "note": "balance=0_post_buy"})
            return False

        tokens_out = actual_balance  # usa saldo reale invece dello stimato dal quote
        price_actual = _size_usdc / tokens_out if tokens_out > 0 else price_actual
        status = "confirmed"
        log.info(f"[BUY] ✅ TX confermata via {provider}: {tx_hash} | saldo reale={tokens_out:.4f} {token_symbol}")
    else:
        log.info(f"[DRY BUY] {_size_usdc} USDC → ~{tokens_out:.2f} {token_symbol} "
                 f"| price≈${price_actual:.8g} | impact={price_impact:.2f}%")

    # Salva stato
    real_state[signal_id] = {
        "signal_id":     signal_id,
        "token_symbol":  token_symbol,
        "token_address": token_address,
        "system":        system,
        "tokens_held":   tokens_out,
        "tokens_bought": tokens_out,
        "usdc_spent":    _size_usdc,
        "usdc_received": 0.0,
        "status":        "open",
        "entry_ts":      datetime.now().isoformat(),
        "entry_tx":      tx_hash,
        "exit_txs":      [],
        "decimals":      decimals,
    }

    log_execution({
        "ts":               datetime.now().isoformat(),
        "signal_id":        signal_id,
        "token_symbol":     token_symbol,
        "chain":            "solana",
        "action":           "buy",
        "token_address":    token_address,
        "tokens_amount":    f"{tokens_out:.6f}",
        "usdc_amount":      f"{_size_usdc:.2f}",
        "price_actual":     f"{price_actual:.8g}",
        "slippage_pct":     f"{used_slippage/100:.2f}",
        "price_impact_pct": f"{price_impact:.2f}",
        "tx_hash":          tx_hash,
        "status":           status,
        "note":             "",
    })
    return True


def execute_sell(signal_id: str, sell_fraction: float, action_label: str,
                 real_state: dict, keypair_pubkey: str) -> bool:
    """
    Vende `sell_fraction` dei token detenuti per USDC via Jupiter.
    sell_fraction=1.0 → vende tutto il residuo
    """
    pos = real_state.get(signal_id)
    if not pos:
        log.warning(f"[SELL] {signal_id}: nessuna posizione reale trovata")
        return False
    if pos.get("status") == "closed":
        log.debug(f"[SELL] {signal_id}: già chiusa")
        return True

    token_address = pos["token_address"]
    token_symbol  = pos["token_symbol"]
    decimals      = pos.get("decimals", 6)
    tokens_held   = pos.get("tokens_held", 0.0)

    tokens_to_sell = tokens_held * sell_fraction
    if tokens_to_sell <= 0:
        log.warning(f"[SELL] {signal_id}: nessun token da vendere (held={tokens_held})")
        return False

    slippage = SLIPPAGE_BPS.get(pos.get("system", "v3"), 100)
    tokens_lamports = int(tokens_to_sell * (10 ** decimals))

    log.info(f"[SELL] {signal_id} | {action_label} | "
             f"{tokens_to_sell:.4f} {token_symbol} ({sell_fraction*100:.0f}%) | slippage={slippage}bps")

    dry = _is_dry_run(pos.get("system", ""))

    if not dry:
        # Verifica saldo on-chain
        on_chain_balance = get_token_balance(keypair_pubkey, token_address)
        if on_chain_balance < tokens_to_sell * 0.95:  # tolleranza 5%
            log.warning(f"[SELL] Saldo on-chain ({on_chain_balance:.4f}) < atteso ({tokens_to_sell:.4f})")
            tokens_to_sell  = on_chain_balance
            tokens_lamports = int(on_chain_balance * (10 ** decimals))

    # Rispetta backoff se entrambi i DEX erano down al tentativo precedente
    retry_after = pos.get("sell_retry_after")
    if retry_after and datetime.fromisoformat(retry_after) > datetime.now():
        log.debug(f"[SELL] {signal_id}: DEX backoff attivo fino a {retry_after[:16]} — skip")
        return False

    # Raydium primario: nessun rate limit, risponde in < 1s
    _raydium_resp: Optional[dict] = None
    quote = None
    used_slippage = slippage

    _raydium_resp = raydium_quote(token_address, USDC_MINT, tokens_lamports, slippage)
    if _raydium_resp:
        quote = _raydium_resp
        log.info(f"[SELL] {token_symbol}: route su Raydium ✓")
    else:
        # Raydium non disponibile → Jupiter fallback con slippage crescente
        log.info(f"[SELL] {token_symbol}: Raydium non disponibile — provo Jupiter...")
        for _sl in [slippage, 2000, 5000]:
            jup_q = jupiter_quote(token_address, USDC_MINT, tokens_lamports, _sl)
            if jup_q:
                quote = jup_q
                used_slippage = _sl
                if _sl > slippage:
                    log.warning(f"[SELL] Route Jupiter con slippage={_sl}bps (base={slippage}bps)")
                break
            if _sl < 5000:
                time.sleep(2)
        if quote:
            log.info(f"[SELL] {token_symbol}: route su Jupiter ✓ (slippage={used_slippage}bps)")

    if not quote:
        # Entrambi i DEX irraggiungibili → backoff se API-down, sell_fail_count se no-route
        if _jup_api_down:
            retry_ts = (datetime.now() + timedelta(minutes=5)).isoformat()
            pos["sell_retry_after"] = retry_ts
            log.warning(
                f"[SELL] {token_symbol}: Raydium e Jupiter entrambi down — "
                f"sell rinviato a {retry_ts[:16]} (sell_fail_count invariato)"
            )
            save_real_state(real_state)
            return False

    if not quote:
        # No route (400) o token morto: conta come fallimento reale
        prev_fail = pos.get("sell_fail_count", 0)
        pos["sell_fail_count"] = prev_fail + 1
        pos["sell_fail_last"]  = datetime.now().isoformat()
        STUCK_THRESHOLD = 3
        if pos["sell_fail_count"] >= STUCK_THRESHOLD and pos.get("status") != "stuck":
            pos["status"] = "stuck"
            log.error(
                f"[SELL] ⚠ POSIZIONE BLOCCATA: {signal_id} ({token_symbol}) — "
                f"{pos['sell_fail_count']} tentativi falliti anche con slippage 50%. "
                f"Pool probabilmente morto. Tokens in wallet: {tokens_held:.4f} | "
                f"Contratto: {token_address}"
            )
            log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                           "token_symbol": token_symbol, "action": f"{action_label}_stuck",
                           "token_address": token_address, "status": "stuck",
                           "note": f"no_quote x{pos['sell_fail_count']} (max slippage 50%)"})
        elif prev_fail == 0:
            log.error(f"[SELL] Nessun quote per {token_symbol} con slippage fino a 50% (tentativo 1/{STUCK_THRESHOLD})")
            log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                           "token_symbol": token_symbol, "action": f"{action_label}_failed",
                           "token_address": token_address, "status": "error", "note": "no_quote_50pct"})
        else:
            log.warning(f"[SELL] Nessun quote per {token_symbol} "
                        f"(tentativo {pos['sell_fail_count']}/{STUCK_THRESHOLD})")
        save_real_state(real_state)
        return False
    slippage = used_slippage

    # Estrai price_impact e usdc_received: Jupiter e Raydium hanno strutture diverse
    if _raydium_resp:
        _rd = _raydium_resp.get("data", {})
        price_impact  = float(_rd.get("priceImpactPct", 0) or 0)
        usdc_out_lam  = int(_rd.get("outputAmount", 0))
    else:
        price_impact  = float(quote.get("priceImpactPct", 0) or 0)
        usdc_out_lam  = int(quote.get("outAmount", 0))
    usdc_received = usdc_out_lam / (10 ** USDC_DECIMALS)
    price_actual  = usdc_received / tokens_to_sell if tokens_to_sell > 0 else 0

    tx_hash = "DRY_RUN"
    status  = "dry_run"

    if not dry:
        if _raydium_resp:
            input_ata  = _get_token_ata(keypair_pubkey, token_address)
            output_ata = _get_token_ata(keypair_pubkey, USDC_MINT)
            swap_tx = raydium_swap_tx(_raydium_resp, keypair_pubkey, input_ata, output_ata)
            provider = "Raydium"
        else:
            swap_tx  = jupiter_swap_tx(quote, keypair_pubkey)
            provider = "Jupiter"
        if not swap_tx:
            # Retry immediato con slippage massimo prima di arrendersi
            if not _raydium_resp and used_slippage < SLIPPAGE_MAX_BPS:
                log.warning(f"[SELL] {provider} swap tx None — retry slippage={SLIPPAGE_MAX_BPS}bps")
                _jq2 = jupiter_quote(token_address, USDC_MINT, tokens_lamports, SLIPPAGE_MAX_BPS)
                if _jq2:
                    swap_tx = jupiter_swap_tx(_jq2, keypair_pubkey)
                    if swap_tx:
                        quote = _jq2
                        used_slippage = SLIPPAGE_MAX_BPS
                        usdc_out_lam  = int(_jq2.get("outAmount", 0))
                        usdc_received = usdc_out_lam / (10 ** USDC_DECIMALS)
            if not swap_tx:
                pos["sell_fail_count"] = pos.get("sell_fail_count", 0) + 1
                pos["sell_fail_last"]  = datetime.now().isoformat()
                pos["sell_retry_after"] = (datetime.now() + timedelta(seconds=60)).isoformat()
                _stuck_tx_threshold = 5
                if pos["sell_fail_count"] >= _stuck_tx_threshold and pos.get("status") != "stuck":
                    pos["status"] = "stuck"
                    log.error(f"[SELL] ⚠ POSIZIONE BLOCCATA: {signal_id} ({token_symbol}) — "
                              f"{pos['sell_fail_count']} tx None di fila. Pool morto o token non routabile.")
                    log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                                   "token_symbol": token_symbol, "action": f"{action_label}_stuck",
                                   "token_address": token_address, "status": "stuck",
                                   "note": f"tx_none x{pos['sell_fail_count']}"})
                else:
                    log.error(f"[SELL] Transazione {provider} non ottenuta per {signal_id} "
                              f"(fail #{pos['sell_fail_count']}/{_stuck_tx_threshold}) — retry in 60s")
                save_real_state(real_state)
                return False
        tx_hash = send_transaction(swap_tx)
        if not tx_hash:
            pos["sell_fail_count"] = pos.get("sell_fail_count", 0) + 1
            pos["sell_fail_last"]  = datetime.now().isoformat()
            pos["sell_retry_after"] = (datetime.now() + timedelta(seconds=60)).isoformat()
            _stuck_tx_threshold = 5
            if pos["sell_fail_count"] >= _stuck_tx_threshold and pos.get("status") != "stuck":
                pos["status"] = "stuck"
                log.error(f"[SELL] ⚠ POSIZIONE BLOCCATA: {signal_id} ({token_symbol}) — "
                          f"{pos['sell_fail_count']} tx fallite di fila (error 0x1788 o simile). "
                          f"Pool non routabile. Tokens in wallet: {tokens_held:.4f}")
                log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                               "token_symbol": token_symbol, "action": f"{action_label}_stuck",
                               "token_address": token_address, "status": "stuck",
                               "note": f"tx_failed x{pos['sell_fail_count']}"})
            else:
                log.error(f"[SELL] Invio tx fallito per {signal_id} "
                          f"(fail #{pos['sell_fail_count']}/{_stuck_tx_threshold}) — retry in 60s")
            save_real_state(real_state)
            return False
        status = "sent"
        log.info(f"[SELL] ✅ TX inviata via {provider}: {tx_hash}")
    else:
        log.info(f"[DRY SELL] {tokens_to_sell:.4f} {token_symbol} → ~{usdc_received:.2f} USDC "
                 f"| impact={price_impact:.2f}%")

    # Aggiorna stato
    pos.pop("sell_retry_after", None)   # API tornata su: rimuovi backoff
    pos["tokens_held"]   -= tokens_to_sell
    pos["usdc_received"] += usdc_received
    pos["exit_txs"].append(tx_hash)

    real_pnl = pos["usdc_received"] - pos["usdc_spent"]
    pos["real_pnl_usdc"] = real_pnl

    if sell_fraction >= 1.0 or pos["tokens_held"] <= 0.0001:
        pos["status"] = "closed"
        pos["close_ts"] = datetime.now().isoformat()
        log.info(f"[SELL] 🏁 {signal_id} chiusa | P&L reale: {real_pnl:+.2f} USDC")

    log_execution({
        "ts":               datetime.now().isoformat(),
        "signal_id":        signal_id,
        "token_symbol":     token_symbol,
        "chain":            "solana",
        "action":           action_label,
        "token_address":    token_address,
        "tokens_amount":    f"{tokens_to_sell:.6f}",
        "usdc_amount":      f"{usdc_received:.2f}",
        "price_actual":     f"{price_actual:.8g}",
        "slippage_pct":     f"{slippage/100:.2f}",
        "price_impact_pct": f"{price_impact:.2f}",
        "tx_hash":          tx_hash,
        "status":           status,
        "note":             f"pnl={real_pnl:+.2f}USDC" if pos["status"] == "closed" else "",
    })
    return True


# ---------------------------------------------------------------------------
# Watcher: legge le nuove righe da live_trades.csv
# ---------------------------------------------------------------------------

def get_processed_rows() -> set:
    """Carica l'insieme delle righe già elaborate (ts+signal_id+action)."""
    processed = set()
    if os.path.exists(REAL_EXEC_CSV):
        try:
            with open(REAL_EXEC_CSV, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    # Usa signal_id come chiave — una entry è sufficiente per sapere se è stata processata
                    sid    = row.get("signal_id", "")
                    action = row.get("action", "")
                    if sid and action:
                        processed.add(f"{sid}|{action}")
        except Exception:
            pass
    return processed


def process_row(row: dict, processed: set, real_state: dict,
                token_lookup: dict, keypair_pubkey: str) -> bool:
    """
    Elabora una riga di live_trades.csv.
    Ritorna True se è stata eseguita un'azione (entry/exit).
    """
    sid     = row.get("signal_id", "").strip()
    action  = row.get("action", "").strip()
    chain   = row.get("chain", "").strip().lower()
    system  = row.get("system", "").strip().lower()
    sym     = row.get("token_symbol", "").strip()
    ts      = row.get("ts", "")

    # Salta chain non supportate
    if chain not in SUPPORTED_CHAINS:
        return False

    # Filtro sistemi abilitati (SYSTEMS_ENABLED="" = tutti)
    if SYSTEMS_ENABLED and system not in SYSTEMS_ENABLED:
        return False

    key = f"{sid}|{action}"

    # --- ENTRY ---
    if action == "entry":
        if key in processed:
            return False

        # Segnali shadow (rugcheck rilassato, size=0 nel simulator): solo
        # tracciamento dati, mai eseguire l'acquisto on-chain
        if "shadow=true" in (row.get("note") or ""):
            log.info(f"[ENTRY] {sid}: segnale shadow (size=0) → skip")
            processed.add(key)
            return False

        # Già in portafoglio reale (evita doppio acquisto)
        if sid in real_state and real_state[sid].get("status") == "open":
            processed.add(key)
            return False

        # Filtro età segnale: pump_grad validi solo per 1h dall'entry
        # Evita di comprare token già pompati ore fa quando il bot riparte
        MAX_ENTRY_AGE_H = {"pump_grad": 1.0, "pre_grad": 0.33, "defi": 3.0}
        _max_age = MAX_ENTRY_AGE_H.get(system)
        if _max_age:
            try:
                _entry_ts = datetime.fromisoformat(ts)
                _age_h = (datetime.now() - _entry_ts).total_seconds() / 3600
                if _age_h > _max_age:
                    log.info(f"[ENTRY] {sid}: segnale stantio ({_age_h:.1f}h > {_max_age}h) → skip")
                    processed.add(key)
                    return False
            except Exception:
                pass

        # Numero massimo posizioni aperte — conta solo sistemi live senza sell falliti.
        # Posizioni con sell_fail_count>=1 stanno già cercando di uscire: non bloccano nuovi ingressi.
        open_pos = sum(
            1 for p in real_state.values()
            if p.get("status") == "open"
            and not _is_dry_run(p.get("system", ""))
            and p.get("sell_fail_count", 0) == 0
        )
        # Il limite protegge solo il capitale reale: i segnali dry (sistema in
        # DRY_RUN_SYSTEMS o DRY_RUN globale) passano sempre, come nel simulator
        if open_pos >= MAX_OPEN_POS and not _is_dry_run(system):
            log.warning(f"[ENTRY] {sid}: max posizioni live raggiunto ({MAX_OPEN_POS}) → skip")
            processed.add(key)
            return False

        # Recupera token_address
        token_address = token_lookup.get(sid, "")
        if not token_address:
            # NON aggiungere a processed: il lookup potrebbe essere stantio (race condition
            # tra gemmeV3 che scrive il segnale e il ciclo di refresh del lookup ogni 10 cicli).
            # Il ciclo successivo riproverà con il lookup aggiornato.
            log.warning(f"[ENTRY] {sid}: token_address non trovato — riproverò al prossimo ciclo")
            return False

        _entry_price = float(row.get("price") or 0)
        ok = execute_buy(sid, sym, token_address, system, real_state, keypair_pubkey,
                         entry_price=_entry_price)
        processed.add(key)
        if not ok:
            # Marca l'entry come permanentemente processata in real_executions.csv
            # così al prossimo restart non viene ripetuta (evita replay di buy_skipped/failed)
            log_execution({"ts": datetime.now().isoformat(), "signal_id": sid,
                           "token_symbol": sym, "action": "entry",
                           "token_address": token_address, "status": "skipped",
                           "note": "processed_no_buy"})
        return ok

    # --- TP1 ---
    if action == "tp1":
        if key in processed:
            return False
        if sid not in real_state or real_state[sid].get("status") != "open":
            processed.add(key)
            return False
        frac = TP1_FRACTION.get(system, 0.50)
        ok = execute_sell(sid, frac, "tp1", real_state, keypair_pubkey)
        if ok:
            processed.add(key)  # solo se vendita riuscita → riprova al ciclo successivo se fallisce
        return ok

    # --- TP2 (vende tutto il residuo dopo TP1) ---
    if action == "tp2":
        if key in processed:
            return False
        if sid not in real_state or real_state[sid].get("status") != "open":
            processed.add(key)
            return False
        ok = execute_sell(sid, 1.0, "tp2", real_state, keypair_pubkey)
        if ok:
            processed.add(key)
        return ok

    # --- EXIT (qualsiasi altra uscita) ---
    exit_reason = row.get("exit_reason", "").strip()
    if exit_reason and exit_reason != "open" and action not in ("entry", "tp1", "tp2"):
        if key in processed:
            return False
        if sid not in real_state or real_state[sid].get("status") != "open":
            processed.add(key)
            return False
        ok = execute_sell(sid, 1.0, exit_reason, real_state, keypair_pubkey)
        if ok:
            processed.add(key)
        return ok

    return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main(stop_event=None):
    """
    stop_event: threading.Event opzionale — usato quando l'executor
    gira come thread in run.py invece che come processo standalone.
    """
    log.info("=" * 60)
    log.info(f"  Solana Executor avviato")
    log.info(f"  TRADE_SIZE    = {TRADE_SIZE_USDC} USDC/trade")
    log.info(f"  MAX_IMPACT    = {MAX_PRICE_IMPACT}%")
    log.info(f"  MAX_POS       = {MAX_OPEN_POS}")
    log.info(f"  RPC           = {RPC_URL}")
    # Mostra modalità per sistema
    all_systems = ["pump_grad", "defi", "v3", "v3_large", "v3_midcap", "v2", "bnf"]
    for _s in all_systems:
        _mode = "💰 REALE" if not _is_dry_run(_s) else "🔵 dry_run"
        log.info(f"  {_s:<12} = {_mode}")
    log.info("=" * 60)

    if not DRY_RUN and not SOLDERS_OK:
        log.error("ERRORE: solders non installato. Installa con: pip install solders base58")
        return

    # Carica keypair
    keypair_pubkey = "DRY_RUN_WALLET"
    if not DRY_RUN:
        kp = _load_keypair()
        if not kp:
            log.error("ERRORE: impossibile caricare il keypair. Controlla .env")
            return
        keypair_pubkey = str(kp.pubkey())
        log.info(f"  Wallet: {keypair_pubkey}")
        bal = get_usdc_balance(keypair_pubkey)
        log.info(f"  Saldo USDC: {bal:.2f}")
    else:
        log.info("  [DRY RUN] Nessun wallet reale caricato")

    _ensure_exec_csv()

    real_state   = load_real_state()
    processed    = get_processed_rows()
    token_lookup = build_token_lookup()

    # -----------------------------------------------------------------------
    # BOOTSTRAP STORICO (sempre, ad ogni avvio)
    # Marca tutte le righe già presenti in live_trades.csv come "già viste"
    # in modo da non rieseguirle al riavvio.
    # Eccezione: le exit/tp1/tp2 di posizioni ancora aperte in real_state
    # NON vengono marcate, così il bot può ancora eseguirle.
    # -----------------------------------------------------------------------
    open_sids  = {sid for sid, pos in real_state.items() if pos.get("status") == "open"}
    bought_sids = {sid for sid in real_state}   # qualsiasi stato: open/closed/stuck
    if os.path.exists(LIVE_CSV):
        snapshot_count = 0
        try:
            with open(LIVE_CSV, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    sid    = row.get("signal_id", "").strip()
                    action = row.get("action", "").strip()
                    chain  = row.get("chain", "").strip().lower()
                    if not sid or not action:
                        continue
                    key = f"{sid}|{action}"
                    if key in processed:
                        continue
                    # Lascia libere le exit di posizioni ancora aperte
                    if sid in open_sids and action != "entry":
                        continue
                    # ENTRY mai comprata: lascia libera solo se recente (<24h)
                    if action == "entry" and chain in SUPPORTED_CHAINS and sid not in bought_sids:
                        try:
                            age_h = (now - datetime.fromisoformat(
                                row.get("ts",""))).total_seconds() / 3600
                            if age_h < 24:
                                continue   # entry recente → retry al prossimo ciclo
                        except Exception:
                            pass  # non parsabile → marca come vista

                    processed.add(key)
                    snapshot_count += 1
        except Exception as e:
            log.warning(f"Bootstrap storico fallito: {e}")
        if snapshot_count:
            log.info(f"  [BOOTSTRAP] {snapshot_count} righe storiche marcate come già viste")

    log.info(f"  Posizioni reali caricate: {len(real_state)}")
    log.info(f"  Token in lookup:          {len(token_lookup)}")
    log.info(f"  Azioni già processate:    {len(processed)}")
    # Segnala posizioni bloccate all'avvio
    stuck = [(sid, p) for sid, p in real_state.items() if p.get("status") == "stuck"]
    if stuck:
        for sid, p in stuck:
            log.error(f"  ⚠ STUCK: {sid} ({p.get('token_symbol','?')}) — "
                      f"{p.get('sell_fail_count',0)} sell falliti, tokens={p.get('tokens_held',0):.4f}")
    log.info("  In ascolto su live_trades.csv... (Ctrl+C per fermare)")
    log.info("-" * 60)

    # ── Gestione Ctrl+C ──────────────────────────────────────────────────────
    # Primo Ctrl+C  → chiude tutte le posizioni aperte al prezzo attuale (via
    #                 Jupiter DRY_RUN o reale) e poi ferma l'executor.
    # Secondo Ctrl+C entro 5s → stop immediato senza chiudere posizioni.
    _last_ctrl_c   = [0.0]
    _shutdown_flag = [False]

    def _close_all_positions():
        """Tenta di vendere tutte le posizioni aperte prima dello shutdown."""
        open_items = [(sid, p) for sid, p in real_state.items()
                      if p.get("status") == "open"]
        if not open_items:
            log.info("[shutdown] Nessuna posizione aperta da chiudere.")
            return
        log.info(f"[shutdown] Chiusura pulita di {len(open_items)} posizione/i...")
        for sid, pos in open_items:
            try:
                execute_sell(sid, 1.0, "manual_close", real_state, keypair_pubkey)
            except Exception as e:
                log.warning(f"[shutdown] Errore chiusura {sid}: {e}")
        save_real_state(real_state)

    # signal.signal solo se siamo nel main thread (processo standalone)
    # Se stop_event è passato siamo in un thread di run.py → niente signal
    import threading as _threading
    if stop_event is None and _threading.current_thread() is _threading.main_thread():
        def _on_sigint(sig, frame):
            now = time.time()
            if now - _last_ctrl_c[0] < 5.0:
                log.info("[executor] Stop immediato — posizioni NON chiuse.")
                raise SystemExit(0)
            _last_ctrl_c[0] = now
            log.info("[executor] ⏸  Ctrl+C — chiusura posizioni in corso...")
            _close_all_positions()
            _shutdown_flag[0] = True
        signal.signal(signal.SIGINT, _on_sigint)

    # ── Loop principale ───────────────────────────────────────────────────────
    cycle = 0
    while not _shutdown_flag[0] and not (stop_event and stop_event.is_set()):
        try:
            cycle += 1

            # Aggiorna token lookup ogni ciclo: pump_grad richiede reattività immediata
            # (il segnale viene scritto sul CSV pochi secondi prima che l'executor lo cerchi)
            token_lookup = build_token_lookup()

            if not os.path.exists(LIVE_CSV):
                time.sleep(5)
                continue

            with open(LIVE_CSV, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))

            actions_done = 0
            for row in rows:
                if process_row(row, processed, real_state, token_lookup, keypair_pubkey):
                    actions_done += 1
                    save_real_state(real_state)

            if actions_done:
                open_pos = sum(1 for p in real_state.values() if p.get("status") == "open")
                log.info(f"[ciclo {cycle}] {actions_done} azioni eseguite | posizioni aperte: {open_pos}")

        except SystemExit:
            save_real_state(real_state)
            raise
        except Exception as e:
            log.error(f"Errore ciclo principale: {e}")

        time.sleep(10)


if __name__ == "__main__":
    main()
