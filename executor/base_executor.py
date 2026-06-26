"""
base_executor.py
================
Executor Base chain + oracle on-chain diretto.

Oracle:
  - Uniswap V3 pool slot0() → sqrtPriceX96 → USD (no DexScreener lag)
  - Uniswap V2 / Aerodrome getReserves() → ratio → USD
  - WETH/USD: Chainlink on-chain (aggiornato ogni blocco ~2s)

Execution:
  - Buy: ETH→WETH→token via Uniswap V3 SwapRouter / Aerodrome fallback
  - Sell: token→WETH→ETH

.env keys:
  BASE_PRIVATE_KEY=<hex>
  BASE_RPC_URL=https://mainnet.base.org
  BASE_DRY_RUN=true
  BASE_TRADE_SIZE_ETH=0.003
  BASE_SLIPPAGE_BPS=300
  BASE_MAX_OPEN_POSITIONS=4
"""

import csv
import json
import logging
import os
import signal
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

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
log = logging.getLogger("base_exec")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent
_ROOT = _HERE.parent

LIVE_CSV           = str(_ROOT / "defi"  / "reports" / "live_trades.csv")
DEFI_SIGNALS       = str(_ROOT / "defi"  / "reports" / "signals_log.csv")
V3_SIGNALS         = str(_ROOT / "gemme" / "reports" / "gems_log_v3.csv")
_HONEYPOT_BL_FILE  = _HERE / "base_honeypot_symbols.json"
PUMP_GRAD_SIGNALS  = str(_ROOT / "defi"  / "reports" / "pump_grad_signals.csv")
BASE_STATE_FILE    = str(_HERE / "base_real_state.json")
BASE_EXEC_CSV      = str(_HERE / "base_executions.csv")

# ---------------------------------------------------------------------------
# Honeypot symbol blacklist (persistente su disco)
# ---------------------------------------------------------------------------
_honeypot_sym_bl: set = set()

def _load_honeypot_bl() -> None:
    try:
        data = json.loads(_HONEYPOT_BL_FILE.read_text())
        _honeypot_sym_bl.update(data)
        log.info(f"[HONEYPOT_BL] Caricati {len(_honeypot_sym_bl)} simboli blacklistati")
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning(f"[HONEYPOT_BL] Errore caricamento: {e}")

def _add_honeypot_sym(symbol: str) -> None:
    sym = symbol.strip().lower()
    if sym in _honeypot_sym_bl:
        return
    _honeypot_sym_bl.add(sym)
    try:
        _HONEYPOT_BL_FILE.write_text(json.dumps(sorted(_honeypot_sym_bl)))
    except Exception as e:
        log.warning(f"[HONEYPOT_BL] Errore salvataggio: {e}")
    log.info(f"[HONEYPOT_BL] Aggiunto '{symbol}' (tot={len(_honeypot_sym_bl)})")

# ---------------------------------------------------------------------------
# Config da .env
# ---------------------------------------------------------------------------
def _env(k, default=None):
    return os.environ.get(k, default)

DRY_RUN        = _env("BASE_DRY_RUN", "true").lower() != "false"
_LIQ_LIVE      = _env("BASE_LIQ_LIVE", "false").lower() == "true"
TRADE_SIZE_ETH = float(_env("BASE_TRADE_SIZE_ETH", "0.003"))
BASE_RPC_URL   = _env("BASE_RPC_URL", "https://mainnet.base.org")
MAX_POS        = int(_env("BASE_MAX_OPEN_POSITIONS", "4"))


def _is_dry(signal_id: str = "") -> bool:
    """DRY_RUN per-segnale: LIQ_* vanno live se BASE_LIQ_LIVE=true."""
    if _LIQ_LIVE and signal_id.startswith("LIQ_"):
        return False
    return DRY_RUN
SLIPPAGE_BPS   = int(_env("BASE_SLIPPAGE_BPS", "300"))

SUPPORTED_CHAINS = {"base"}

# ---------------------------------------------------------------------------
# Indirizzi Base chain
# ---------------------------------------------------------------------------
if WEB3_AVAILABLE:
    WETH_BASE         = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")
    USDC_BASE         = Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
    CHAINLINK_ETH_USD = Web3.to_checksum_address("0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70")
    UNIV3_ROUTER      = Web3.to_checksum_address("0x2626664c2603336E57B271c5C0b26F421741e481")
    UNIV3_FACTORY     = Web3.to_checksum_address("0x33128a8fC17869897dcE68Ed026d694621f6FDfD")
    AERO_ROUTER       = Web3.to_checksum_address("0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43")
    AERO_FACTORY      = Web3.to_checksum_address("0x420DD381b31aEf6683db6B902084cB0FFECe40Da")
    UNIV2_ROUTER      = Web3.to_checksum_address("0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24")
    UNIV2_FACTORY     = Web3.to_checksum_address("0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6")
else:
    WETH_BASE = USDC_BASE = CHAINLINK_ETH_USD = UNIV3_ROUTER = UNIV3_FACTORY = \
        AERO_ROUTER = AERO_FACTORY = UNIV2_ROUTER = UNIV2_FACTORY = ""

BASE_CHAIN_ID = 8453

# ---------------------------------------------------------------------------
# ABIs
# ---------------------------------------------------------------------------
_ABI_V3_POOL = [
    {"name": "slot0", "type": "function", "inputs": [], "stateMutability": "view",
     "outputs": [{"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"},
                 {"name": "observationIndex", "type": "uint16"}, {"name": "observationCardinality", "type": "uint16"},
                 {"name": "observationCardinalityNext", "type": "uint16"}, {"name": "feeProtocol", "type": "uint8"},
                 {"name": "unlocked", "type": "bool"}]},
    {"name": "token0", "type": "function", "inputs": [], "stateMutability": "view",
     "outputs": [{"type": "address"}]},
    {"name": "token1", "type": "function", "inputs": [], "stateMutability": "view",
     "outputs": [{"type": "address"}]},
    {"name": "fee", "type": "function", "inputs": [], "stateMutability": "view",
     "outputs": [{"type": "uint24"}]},
]

_ABI_V2_PAIR = [
    {"name": "getReserves", "type": "function", "inputs": [], "stateMutability": "view",
     "outputs": [{"name": "_reserve0", "type": "uint112"}, {"name": "_reserve1", "type": "uint112"},
                 {"name": "_blockTimestampLast", "type": "uint32"}]},
    {"name": "token0", "type": "function", "inputs": [], "stateMutability": "view",
     "outputs": [{"type": "address"}]},
    {"name": "token1", "type": "function", "inputs": [], "stateMutability": "view",
     "outputs": [{"type": "address"}]},
]

_ABI_CHAINLINK = [
    {"name": "latestRoundData", "type": "function", "inputs": [], "stateMutability": "view",
     "outputs": [{"name": "roundId", "type": "uint80"}, {"name": "answer", "type": "int256"},
                 {"name": "startedAt", "type": "uint256"}, {"name": "updatedAt", "type": "uint256"},
                 {"name": "answeredInRound", "type": "uint80"}]},
]

_ABI_ERC20 = [
    {"name": "decimals", "type": "function", "inputs": [], "stateMutability": "view",
     "outputs": [{"type": "uint8"}]},
    {"name": "balanceOf", "type": "function",
     "inputs": [{"name": "account", "type": "address"}], "stateMutability": "view",
     "outputs": [{"type": "uint256"}]},
    {"name": "approve", "type": "function",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "stateMutability": "nonpayable", "outputs": [{"type": "bool"}]},
    {"name": "allowance", "type": "function",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "stateMutability": "view", "outputs": [{"type": "uint256"}]},
]

_ABI_WETH_EXTRA = [
    {"name": "deposit", "type": "function", "inputs": [], "stateMutability": "payable",
     "outputs": []},
    {"name": "withdraw", "type": "function",
     "inputs": [{"name": "wad", "type": "uint256"}], "stateMutability": "nonpayable",
     "outputs": []},
]

_ABI_V3_ROUTER = [
    {"name": "exactInputSingle", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "tokenIn",          "type": "address"},
         {"name": "tokenOut",         "type": "address"},
         {"name": "fee",              "type": "uint24"},
         {"name": "recipient",        "type": "address"},
         {"name": "amountIn",         "type": "uint256"},
         {"name": "amountOutMinimum", "type": "uint256"},
         {"name": "sqrtPriceLimitX96","type": "uint160"},
     ]}],
     "outputs": [{"name": "amountOut", "type": "uint256"}]},
]

_ABI_AERO_ROUTER = [
    {"name": "getAmountsOut", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "amountIn", "type": "uint256"},
                {"name": "routes", "type": "tuple[]", "components": [
                    {"name": "from",    "type": "address"},
                    {"name": "to",      "type": "address"},
                    {"name": "stable",  "type": "bool"},
                    {"name": "factory", "type": "address"},
                ]}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "swapExactTokensForTokens", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "amountIn",    "type": "uint256"},
                {"name": "amountOutMin","type": "uint256"},
                {"name": "routes", "type": "tuple[]", "components": [
                    {"name": "from",    "type": "address"},
                    {"name": "to",      "type": "address"},
                    {"name": "stable",  "type": "bool"},
                    {"name": "factory", "type": "address"},
                ]},
                {"name": "to",       "type": "address"},
                {"name": "deadline", "type": "uint256"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
]

# ---------------------------------------------------------------------------
# Web3 setup
# ---------------------------------------------------------------------------
_w3: Optional["Web3"] = None
_account = None
_decimals_cache: dict = {}
_pool_type_cache: dict = {}
_miss_counts: dict = {}   # signal_id → n. tentativi falliti per token_address mancante

# Chainlink cache
_weth_usd_cache: dict = {"price": 3500.0, "ts": 0.0}
_WETH_USD_TTL = 30.0


def _get_w3() -> Optional["Web3"]:
    global _w3
    if _w3 and _w3.is_connected():
        return _w3
    if not WEB3_AVAILABLE:
        return None
    try:
        w3 = Web3(Web3.HTTPProvider(BASE_RPC_URL, request_kwargs={"timeout": 15}))
        if w3.is_connected():
            _w3 = w3
            return _w3
    except Exception as e:
        log.warning(f"[Web3] Connessione Base fallita: {e}")
    return None


def _load_account() -> Optional["Account"]:
    global _account
    if _account:
        return _account
    key = _env("BASE_PRIVATE_KEY", "")
    if not key or key.startswith("INSERISCI"):
        return None
    try:
        if not key.startswith("0x"):
            key = "0x" + key
        _account = Account.from_key(key)
        return _account
    except Exception as e:
        log.error(f"[Wallet] Errore caricamento chiave Base: {e}")
        return None


def _get_decimals(token_addr: str) -> int:
    if token_addr in _decimals_cache:
        return _decimals_cache[token_addr]
    w3 = _get_w3()
    if not w3:
        return 18
    try:
        tok = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=_ABI_ERC20)
        dec = tok.functions.decimals().call()
        _decimals_cache[token_addr] = dec
        return dec
    except Exception:
        return 18


_ABI_V3_FACTORY = [
    {"name": "getPool", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenA", "type": "address"},
                {"name": "tokenB", "type": "address"},
                {"name": "fee",    "type": "uint24"}],
     "outputs": [{"name": "pool", "type": "address"}]},
]
_ABI_AERO_FACTORY = [
    {"name": "getPool", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenA", "type": "address"},
                {"name": "tokenB", "type": "address"},
                {"name": "stable", "type": "bool"}],
     "outputs": [{"name": "", "type": "address"}]},
]
_ABI_UNIV2_ROUTER = [
    {"name": "swapExactTokensForTokens", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "amountIn",     "type": "uint256"},
                {"name": "amountOutMin", "type": "uint256"},
                {"name": "path",         "type": "address[]"},
                {"name": "to",           "type": "address"},
                {"name": "deadline",     "type": "uint256"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
]
_ABI_UNIV2_FACTORY = [
    {"name": "getPair", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenA", "type": "address"},
                {"name": "tokenB", "type": "address"}],
     "outputs": [{"name": "pair", "type": "address"}]},
]
_NULL_ADDR = "0x0000000000000000000000000000000000000000"

# Cache token_address → (pool_address, pool_type)
_token_pool_cache: dict = {}


def _find_pool(token_address: str) -> Optional[tuple]:
    """
    Trova il pool migliore per un token su Base.
    Cerca via Uniswap V3 Factory (fee tier 100/500/3000/10000) poi Aerodrome.
    Ritorna (pool_address, pool_type) o None.
    Cache per token_address.
    """
    if token_address in _token_pool_cache:
        return _token_pool_cache[token_address]

    w3 = _get_w3()
    if not w3 or not WEB3_AVAILABLE:
        return None

    t = Web3.to_checksum_address(token_address)
    # Solo WETH: il wallet usa sempre WETH come quote token (ETH wrappato).
    # Pool USDC non sono tradabili con swap diretto WETH→token.
    quotes = [WETH_BASE]
    v3_fees = [100, 500, 3000, 10000]

    # Uniswap V3
    try:
        factory = w3.eth.contract(address=UNIV3_FACTORY, abi=_ABI_V3_FACTORY)
        for quote in quotes:
            for fee in v3_fees:
                try:
                    pool_addr = factory.functions.getPool(t, quote, fee).call()
                    if pool_addr and pool_addr != _NULL_ADDR:
                        result = (pool_addr, "v3")
                        _token_pool_cache[token_address] = result
                        log.debug(f"[Oracle] {token_address[:8]}…: V3 pool fee={fee} vs {quote[:6]}…")
                        return result
                except Exception:
                    continue
    except Exception:
        pass

    # Aerodrome V2 (volatile + stable)
    try:
        factory = w3.eth.contract(address=AERO_FACTORY, abi=_ABI_AERO_FACTORY)
        for quote in quotes:
            for stable in (False, True):
                try:
                    pool_addr = factory.functions.getPool(t, quote, stable).call()
                    if pool_addr and pool_addr != _NULL_ADDR:
                        result = (pool_addr, "v2")
                        _token_pool_cache[token_address] = result
                        log.debug(f"[Oracle] {token_address[:8]}…: Aerodrome pool stable={stable} vs {quote[:6]}…")
                        return result
                except Exception:
                    continue
    except Exception:
        pass

    # Uniswap V2 (Base deployment)
    try:
        factory = w3.eth.contract(address=UNIV2_FACTORY, abi=_ABI_UNIV2_FACTORY)
        for quote in quotes:
            try:
                pair_addr = factory.functions.getPair(t, Web3.to_checksum_address(quote)).call()
                if pair_addr and pair_addr != _NULL_ADDR:
                    result = (pair_addr, "univ2")
                    _token_pool_cache[token_address] = result
                    log.debug(f"[Oracle] {token_address[:8]}…: Uniswap V2 pair vs {quote[:6]}…")
                    return result
            except Exception:
                continue
    except Exception:
        pass

    _token_pool_cache[token_address] = None
    return None


def _get_weth_usd() -> float:
    """WETH/USD da Chainlink on-chain, cache 30s. Fallback: 3500.0."""
    now = time.time()
    if now - _weth_usd_cache["ts"] < _WETH_USD_TTL:
        return _weth_usd_cache["price"]
    w3 = _get_w3()
    if not w3:
        return _weth_usd_cache["price"]
    try:
        feed = w3.eth.contract(
            address=Web3.to_checksum_address(CHAINLINK_ETH_USD), abi=_ABI_CHAINLINK
        )
        data = feed.functions.latestRoundData().call()
        price = data[1] / 1e8   # answer / 10^8 decimals
        if price > 0:
            _weth_usd_cache["price"] = price
            _weth_usd_cache["ts"] = now
            return price
    except Exception as e:
        log.debug(f"[Chainlink] latestRoundData fallito: {e}")
    return _weth_usd_cache["price"]


def quote_onchain(token_address: str, _pair_address: str = "") -> Optional[float]:
    """
    Legge il prezzo USD del token direttamente on-chain (no DexScreener lag).

    Trova il pool via Uniswap V3 Factory o Aerodrome Factory,
    poi calcola il prezzo da slot0 (V3) o getReserves (V2/Aerodrome).
    Il parametro _pair_address è ignorato (DexScreener usa ID 64-char, non contract).

    Ritorna il prezzo in USD o None se il pool non è trovato.
    """
    if not token_address or not WEB3_AVAILABLE:
        return None
    w3 = _get_w3()
    if not w3:
        return None

    pool_info = _find_pool(token_address)
    if not pool_info:
        return None

    pool_addr, pool_type = pool_info
    t_addr = Web3.to_checksum_address(token_address)

    try:
        if pool_type == "v3":
            pool   = w3.eth.contract(address=pool_addr, abi=_ABI_V3_POOL)
            sqrt   = pool.functions.slot0().call()[0]   # sqrtPriceX96
            token0 = Web3.to_checksum_address(pool.functions.token0().call())
            token1 = Web3.to_checksum_address(pool.functions.token1().call())
            dec0, dec1 = _get_decimals(token0), _get_decimals(token1)

            price_raw = (sqrt / 2**96) ** 2
            price_adj = price_raw * (10**dec0) / (10**dec1)

            if t_addr.lower() == token0.lower():
                price_in_quote, quote_token = price_adj, token1
            else:
                price_in_quote = 1.0 / price_adj if price_adj != 0 else None
                quote_token = token0

        else:  # v2 / Aerodrome
            pool   = w3.eth.contract(address=pool_addr, abi=_ABI_V2_PAIR)
            res    = pool.functions.getReserves().call()
            token0 = Web3.to_checksum_address(pool.functions.token0().call())
            token1 = Web3.to_checksum_address(pool.functions.token1().call())
            dec0, dec1 = _get_decimals(token0), _get_decimals(token1)

            r0 = res[0] / (10**dec0)
            r1 = res[1] / (10**dec1)
            if r0 == 0 or r1 == 0:
                return None

            if t_addr.lower() == token0.lower():
                price_in_quote, quote_token = r1 / r0, token1
            else:
                price_in_quote, quote_token = r0 / r1, token0

        if not price_in_quote:
            return None

        if quote_token.lower() == WETH_BASE.lower():
            return price_in_quote * _get_weth_usd()
        elif quote_token.lower() == USDC_BASE.lower():
            return price_in_quote
        return None

    except Exception as e:
        log.debug(f"[Oracle] quote_onchain({token_address[:8]}…): {e}")
        _token_pool_cache.pop(token_address, None)   # reset cache su errore
        return None


# ---------------------------------------------------------------------------
# Ensure approval
# ---------------------------------------------------------------------------
def _ensure_approval(token_addr: str, spender: str, amount_wei: int, owner: str) -> bool:
    """Approva spender se l'allowance è insufficiente.
    Retry automatico su 429 (RPC rate limit) con backoff 5s, max 3 tentativi."""
    w3 = _get_w3()
    if not w3 or not _account:
        return False
    for attempt in range(1, 4):
        try:
            tok       = w3.eth.contract(address=Web3.to_checksum_address(token_addr), abi=_ABI_ERC20)
            allowance = tok.functions.allowance(owner, spender).call()
            if allowance >= amount_wei:
                return True
            nonce  = w3.eth.get_transaction_count(owner)
            tx     = tok.functions.approve(spender, 2**256 - 1).build_transaction({
                "from": owner, "nonce": nonce, "chainId": BASE_CHAIN_ID, "gas": 80_000,
            })
            signed   = w3.eth.account.sign_transaction(tx, _account.key)
            tx_hash  = w3.eth.send_raw_transaction(signed.raw_transaction)
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            log.info(f"[Approve] {token_addr[:8]}… → {spender[:8]}…: {tx_hash.hex()[:16]}…")
            return True
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "Too Many Requests" in err_str or "rate limit" in err_str.lower():
                wait = attempt * 5
                log.warning(f"[Approve] RPC 429 (tentativo {attempt}/3) — retry tra {wait}s")
                time.sleep(wait)
                continue
            log.error(f"[Approve] Errore: {e}")
            return False
    log.error("[Approve] Fallito dopo 3 tentativi (RPC rate limit persistente)")
    return False


# ---------------------------------------------------------------------------
# Swap V3 (Uniswap SwapRouter02)
# ---------------------------------------------------------------------------
def _swap_v3(token_in: str, token_out: str, amount_in_wei: int,
             min_out_wei: int, fee: int, acc, w3) -> Optional[str]:
    """Esegue exactInputSingle su Uniswap V3. Ritorna tx_hash o None."""
    try:
        router   = w3.eth.contract(address=UNIV3_ROUTER, abi=_ABI_V3_ROUTER)
        deadline = int(time.time()) + 300
        nonce    = w3.eth.get_transaction_count(acc.address)
        params   = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            fee,
            acc.address,
            amount_in_wei,
            min_out_wei,
            0,   # sqrtPriceLimitX96
        )
        tx = router.functions.exactInputSingle(params).build_transaction({
            "from": acc.address, "nonce": nonce, "chainId": BASE_CHAIN_ID,
            "gas": 300_000, "value": 0,
        })
        signed   = w3.eth.account.sign_transaction(tx, acc.key)
        tx_hash  = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt  = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.get("status") == 0:
            log.warning(f"[V3Swap] TX revertita on-chain: {tx_hash.hex()[:20]}…")
            return None
        return tx_hash.hex()
    except Exception as e:
        log.warning(f"[V3Swap] Fallito: {e}")
        return None


# ---------------------------------------------------------------------------
# Swap Aerodrome (V2-style)
# ---------------------------------------------------------------------------
def _swap_aero(token_in: str, token_out: str, amount_in_wei: int,
               min_out_wei: int, acc, w3) -> Optional[str]:
    """Esegue swap su Aerodrome (volatile pool). Ritorna tx_hash o None."""
    try:
        router  = w3.eth.contract(address=AERO_ROUTER, abi=_ABI_AERO_ROUTER)
        routes  = [(
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            False,           # stable=False (volatile)
            AERO_FACTORY,
        )]
        # Verifica che esista una route con liquidità
        amounts = router.functions.getAmountsOut(amount_in_wei, routes).call()
        if not amounts or amounts[-1] == 0:
            log.debug("[Aero] getAmountsOut = 0 — route inesistente")
            return None
        deadline = int(time.time()) + 300
        nonce    = w3.eth.get_transaction_count(acc.address)
        tx = router.functions.swapExactTokensForTokens(
            amount_in_wei, min_out_wei, routes, acc.address, deadline
        ).build_transaction({
            "from": acc.address, "nonce": nonce, "chainId": BASE_CHAIN_ID, "gas": 300_000,
        })
        signed   = w3.eth.account.sign_transaction(tx, acc.key)
        tx_hash  = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt  = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.get("status") == 0:
            log.warning(f"[Aero] TX revertita on-chain: {tx_hash.hex()[:20]}…")
            return None
        return tx_hash.hex()
    except Exception as e:
        log.warning(f"[Aero] Fallito: {e}")
        return None


def _swap_v2(token_in: str, token_out: str, amount_in_wei: int,
             min_out_wei: int, acc, w3) -> Optional[str]:
    """Esegue swap su Uniswap V2 (Base deployment). Ritorna tx_hash o None."""
    try:
        router   = w3.eth.contract(address=UNIV2_ROUTER, abi=_ABI_UNIV2_ROUTER)
        path     = [Web3.to_checksum_address(token_in), Web3.to_checksum_address(token_out)]
        deadline = int(time.time()) + 300
        nonce    = w3.eth.get_transaction_count(acc.address)
        tx = router.functions.swapExactTokensForTokens(
            amount_in_wei, min_out_wei, path, acc.address, deadline
        ).build_transaction({
            "from": acc.address, "nonce": nonce, "chainId": BASE_CHAIN_ID, "gas": 250_000,
        })
        signed   = w3.eth.account.sign_transaction(tx, acc.key)
        tx_hash  = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt  = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.get("status") == 0:
            log.warning(f"[UniV2] TX revertita on-chain: {tx_hash.hex()[:20]}…")
            return None
        return tx_hash.hex()
    except Exception as e:
        log.warning(f"[UniV2] Fallito: {e}")
        return None


# ---------------------------------------------------------------------------
# Execute BUY
# ---------------------------------------------------------------------------
def execute_buy(signal_id: str, token_symbol: str, token_address: str,
                pair_address: str, system: str, real_state: dict) -> bool:
    if not token_address:
        log.warning(f"[BUY] {signal_id}: nessun token_address → skip")
        return False

    # Honeypot blacklist: skip immediato senza RPC se il simbolo è noto honeypot
    if token_symbol.strip().lower() in _honeypot_sym_bl:
        log.debug(f"[BUY] {token_symbol}: in honeypot_sym_bl — skip")
        log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                       "token_symbol": token_symbol, "action": "buy_skipped",
                       "token_address": token_address, "status": "skipped",
                       "note": "honeypot_sym_blacklisted"})
        return False

    decimals   = _get_decimals(token_address)
    weth_usd   = _get_weth_usd()

    # DRY_RUN: notional fisso $100 (allineato a CAPITAL_EUR del simulator) invece
    # del size reale da .env — il dry serve a validare i segnali, non il capitale
    trade_size_eth = TRADE_SIZE_ETH
    if _is_dry(signal_id) and weth_usd > 0:
        trade_size_eth = round(100.0 / weth_usd, 6)

    eth_wei    = int(trade_size_eth * 10**18)
    slippage   = SLIPPAGE_BPS / 10_000

    log.info(f"[BUY] {signal_id} | {token_symbol} | {trade_size_eth} ETH | slippage={SLIPPAGE_BPS}bps")

    # Stima token out per calcolo min_out (usa prezzo oracle se disponibile)
    price_usd   = quote_onchain(token_address)
    tokens_est  = 0.0
    if price_usd and price_usd > 0:
        tokens_est = (trade_size_eth * weth_usd) / price_usd

    tx_hash = "DRY_RUN"
    status  = "dry_run"

    # ── Preflight checks (read-only, nessun gas) — girano anche in dry mode ──
    # Così il dashboard mostra honeypot/no_route/liq bassa anche senza TX reali.
    w3 = _get_w3()
    _pool_info = None
    pool_type  = "unknown"
    _reserves_min_out = 0
    if w3:
        _pool_info = _find_pool(token_address)
        pool_type  = _pool_info[1] if _pool_info else "unknown"
        if _pool_info is None:
            log.warning(f"[BUY] {token_symbol}: nessun pool trovato — skip")
            log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                           "token_symbol": token_symbol, "action": "buy_failed",
                           "token_address": token_address, "tokens_amount": "0",
                           "eth_amount": trade_size_eth, "tx_hash": "", "status": "error",
                           "note": "no_route"})
            return False

        # Reserves + honeypot check (view calls, zero gas, anche in dry mode)
        if pool_type in ("univ2", "v2") and _pool_info:
            try:
                _pair_abi = [
                    {"name":"getReserves","type":"function","stateMutability":"view",
                     "inputs":[],"outputs":[{"type":"uint112"},{"type":"uint112"},{"type":"uint32"}]},
                    {"name":"token0","type":"function","stateMutability":"view",
                     "inputs":[],"outputs":[{"type":"address"}]}]
                _pair_c  = w3.eth.contract(
                    address=Web3.to_checksum_address(_pool_info[0]), abi=_pair_abi)
                _r       = _pair_c.functions.getReserves().call()
                _tok0    = _pair_c.functions.token0().call().lower()
                if _tok0 == WETH_BASE.lower():
                    _weth_res, _tok_res = _r[0], _r[1]
                else:
                    _tok_res, _weth_res = _r[0], _r[1]
                _live_liq = _weth_res / 1e18 * _get_weth_usd() * 2
                if _live_liq < 10_000:
                    log.warning(f"[BUY] {token_symbol}: liq live ${_live_liq:.0f} < $10k — skip")
                    log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                                   "token_symbol": token_symbol, "action": "buy_skipped",
                                   "token_address": token_address, "status": "skipped",
                                   "note": f"live_liq=${_live_liq:.0f}<10k"})
                    return False
                if _tok_res > 0 and _weth_res > 0:
                    _expected = (_tok_res * eth_wei) // (_weth_res + eth_wei)
                    _reserves_min_out = int(_expected * 0.80)
                    # Simula sell via eth_call (honeypot tipo 2: buy ok, sell bloccato)
                    _sim_from = "0x0000000000000000000000000000000000000001"
                    try:
                        acc_tmp = _load_account()
                        if acc_tmp:
                            _sim_from = acc_tmp.address
                    except Exception:
                        pass
                    try:
                        _router_c = w3.eth.contract(
                            address=Web3.to_checksum_address(UNIV2_ROUTER),
                            abi=_ABI_UNIV2_ROUTER)
                        _sell_out = _router_c.functions.swapExactTokensForTokens(
                            _expected // 10, 0,
                            [Web3.to_checksum_address(token_address), WETH_BASE],
                            _sim_from, int(time.time()) + 300
                        ).call({"from": _sim_from})
                        if not _sell_out or _sell_out[-1] == 0:
                            log.warning(f"[BUY] {token_symbol}: sell simulato=0 (honeypot) — skip")
                            _add_honeypot_sym(token_symbol)
                            log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                                           "token_symbol": token_symbol, "action": "buy_skipped",
                                           "token_address": token_address, "status": "skipped",
                                           "note": "honeypot_sell_lock"})
                            return False
                    except Exception:
                        log.warning(f"[BUY] {token_symbol}: sell simulato revertito (honeypot) — skip")
                        _add_honeypot_sym(token_symbol)
                        log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                                       "token_symbol": token_symbol, "action": "buy_skipped",
                                       "token_address": token_address, "status": "skipped",
                                       "note": "honeypot_sell_lock"})
                        return False
            except Exception as e:
                log.warning(f"[BUY] {token_symbol}: reserves check fallito ({e}) — skip")
                log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                               "token_symbol": token_symbol, "action": "buy_skipped",
                               "token_address": token_address, "status": "skipped",
                               "note": "reserves_check_failed"})
                return False

    if not _is_dry(signal_id):
        acc = _load_account()
        if not acc or not w3:
            log.error("[BUY] Wallet o Web3 non disponibili")
            return False

        # Controllo saldo: gas reserve sempre in ETH nativo
        GAS_RESERVE = int(0.001 * 10**18)
        weth_abi    = _ABI_ERC20 + _ABI_WETH_EXTRA
        weth_c      = w3.eth.contract(address=WETH_BASE, abi=weth_abi)
        eth_balance  = w3.eth.get_balance(acc.address)
        weth_balance = weth_c.functions.balanceOf(acc.address).call()

        if eth_balance < GAS_RESERVE:
            log.error(
                f"[BUY] ETH insufficiente per gas: {eth_balance/1e18:.5f} ETH < {GAS_RESERVE/1e18:.4f} (reserve)"
            )
            return False

        # 1. Wrap ETH → WETH solo per la quota mancante
        weth_needed = max(0, eth_wei - weth_balance)
        if weth_needed > 0:
            if eth_balance < weth_needed + GAS_RESERVE:
                log.error(
                    f"[BUY] ETH+WETH insufficienti: ETH={eth_balance/1e18:.5f} "
                    f"WETH={weth_balance/1e18:.5f} serve {trade_size_eth:.4f} ETH equiv. + gas"
                )
                return False
            try:
                nonce  = w3.eth.get_transaction_count(acc.address)
                tx_dep = weth_c.functions.deposit().build_transaction({
                    "from": acc.address, "nonce": nonce, "value": weth_needed,
                    "chainId": BASE_CHAIN_ID, "gas": 50_000,
                })
                signed = w3.eth.account.sign_transaction(tx_dep, acc.key)
                wrap_h = w3.eth.send_raw_transaction(signed.raw_transaction)
                w3.eth.wait_for_transaction_receipt(wrap_h, timeout=60)
                log.info(f"[BUY] WETH wrap {weth_needed/1e18:.5f} ETH OK: {wrap_h.hex()[:16]}…")
            except Exception as e:
                log.error(f"[BUY] WETH wrap fallito: {e}")
                return False
        else:
            log.info(f"[BUY] WETH disponibile ({weth_balance/1e18:.5f}) — skip wrap")

        fee = 3000   # default 0.3%
        if pool_type == "v3":
            try:
                pool_c = w3.eth.contract(address=_pool_info[0], abi=_ABI_V3_POOL)
                fee    = pool_c.functions.fee().call()
            except Exception:
                pass

        # Stima min_out: reserves > oracle > 0
        min_out = _reserves_min_out
        if min_out == 0 and tokens_est > 0:
            min_out = int(tokens_est * (10**decimals) * (1 - slippage))

        # 2. Approva solo il router necessario e prova lo swap
        tx_hash_res = None
        if pool_type == "v3":
            if not _ensure_approval(WETH_BASE, UNIV3_ROUTER, eth_wei, acc.address):
                log.error("[BUY] Approve WETH→V3Router fallito")
                return False
            tx_hash_res = _swap_v3(WETH_BASE, token_address, eth_wei, min_out, fee, acc, w3)

        # Uniswap V2 (se pool trovato su V2)
        if tx_hash_res is None and pool_type == "univ2":
            if not _ensure_approval(WETH_BASE, UNIV2_ROUTER, eth_wei, acc.address):
                log.error("[BUY] Approve WETH→UniV2Router fallito")
                return False
            tx_hash_res = _swap_v2(WETH_BASE, token_address, eth_wei, min_out, acc, w3)

        # Fallback Aerodrome (se V3 ha fallito e non è V2)
        if tx_hash_res is None and pool_type != "univ2":
            if not _ensure_approval(WETH_BASE, AERO_ROUTER, eth_wei, acc.address):
                log.error("[BUY] Approve WETH→AeroRouter fallito")
                return False
            tx_hash_res = _swap_aero(WETH_BASE, token_address, eth_wei, min_out, acc, w3)

        if tx_hash_res is None:
            log.error(f"[BUY] Tutti i router falliti per {token_symbol}")
            log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                           "token_symbol": token_symbol, "action": "buy_failed",
                           "token_address": token_address, "tokens_amount": "0",
                           "eth_amount": trade_size_eth, "tx_hash": "", "status": "error",
                           "note": "no_route"})
            return False

        tx_hash = tx_hash_res
        status  = "sent"
        log.info(f"[BUY] TX inviata: {tx_hash}")

        # Leggi balance reale post-swap — se 0 la TX è revertita on-chain
        try:
            tok_c      = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=_ABI_ERC20)
            raw_bal    = tok_c.functions.balanceOf(acc.address).call()
            tokens_est = raw_bal / (10**decimals)
        except Exception:
            pass
        if tokens_est <= 0:
            log.error(
                f"[BUY] ⚠ TX inviata ma saldo token = 0 → swap revertito on-chain "
                f"(honeypot / liquidità esaurita / slippage) | tx={tx_hash[:20]}…"
            )
            log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                           "token_symbol": token_symbol, "action": "buy_failed",
                           "token_address": token_address, "tokens_amount": "0",
                           "eth_amount": trade_size_eth, "tx_hash": tx_hash,
                           "status": "failed_onchain", "note": "balance=0_post_swap"})
            return False
    else:
        log.info(f"[DRY BUY] ~{tokens_est:.4f} {token_symbol} per {trade_size_eth} ETH (notional $100)")

    _eth_usd  = _get_weth_usd()
    _usd_spent = round(trade_size_eth * _eth_usd, 4)
    real_state[signal_id] = {
        "signal_id":      signal_id,
        "token_symbol":   token_symbol,
        "token_address":  token_address,
        "pair_address":   pair_address,
        "chain":          "base",
        "system":         system,
        "decimals":       decimals,
        "tokens_held":    tokens_est,
        "tokens_bought":  tokens_est,
        "eth_spent":      trade_size_eth,
        "eth_received":   0.0,
        "usdc_spent":     _usd_spent,     # USD equiv. per executor_report
        "usdc_received":  0.0,
        "real_pnl_eth":   -trade_size_eth,
        "real_pnl_usdc":  -_usd_spent,
        "status":         "open",
        "entry_ts":       datetime.now().isoformat(),
        "entry_tx":       tx_hash,
        "exit_txs":       [],
        "sell_fail_count": 0,
    }
    log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                   "token_symbol": token_symbol, "action": "buy",
                   "token_address": token_address, "tokens_amount": f"{tokens_est:.6f}",
                   "eth_amount": trade_size_eth, "tx_hash": tx_hash,
                   "status": status, "note": f"system={system}"})
    return True


# ---------------------------------------------------------------------------
# Helper: legge pnl_eur finale del simulatore per un signal_id
# ---------------------------------------------------------------------------
def _read_sim_pnl(signal_id: str) -> float | None:
    """Ritorna pnl_eur dell'ultima riga chiusa (remaining=0) del simulatore, o None."""
    try:
        import csv as _csv
        best = None
        with open(LIVE_CSV, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                if row.get("signal_id") != signal_id:
                    continue
                try:
                    rem = float(row.get("remaining", "1") or 1)
                    pnl = float((row.get("pnl_eur") or "0").replace("+", ""))
                    if rem <= 0.001:
                        best = pnl
                except Exception:
                    pass
        return best
    except Exception:
        return None


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
    pair_address  = pos.get("pair_address", "")
    decimals      = pos.get("decimals", 18)
    tokens_held   = pos.get("tokens_held", 0.0)
    tokens_sell   = tokens_held * sell_fraction
    if tokens_sell <= 0:
        # DRY_RUN: nessun token reale — stima PnL dal simulatore (live_trades.csv)
        sim_pnl_eur = _read_sim_pnl(signal_id)
        eth_spent   = pos.get("eth_spent", TRADE_SIZE_ETH) or TRADE_SIZE_ETH
        weth_usd_now = _get_weth_usd()
        if sim_pnl_eur is not None and weth_usd_now > 0:
            # Converti pnl_eur (simulato su 100€ fissi) in ETH alla size reale
            sim_pct         = sim_pnl_eur / 100.0
            eth_received    = eth_spent * (1.0 + sim_pct) * sell_fraction
            pnl_eth         = eth_received - eth_spent * sell_fraction
            pnl_usd         = pnl_eth * weth_usd_now
        else:
            # Fallback: chiudi a pareggio se non abbiamo dati
            eth_received = eth_spent * sell_fraction
            pnl_eth      = 0.0
            pnl_usd      = 0.0

        pos["eth_received"]   = pos.get("eth_received", 0.0) + eth_received
        pos["usdc_received"]  = pos.get("usdc_received", 0.0) + eth_received * weth_usd_now
        pos["real_pnl_eth"]   = pos.get("eth_received", 0.0) - pos.get("eth_spent", eth_spent)
        pos["real_pnl_usdc"]  = pos.get("usdc_received", 0.0) - pos.get("usdc_spent", eth_spent * weth_usd_now)
        if sell_fraction >= 1.0:
            pos["status"]   = "closed"
            pos["close_ts"] = datetime.now().isoformat()
        log.info(f"[DRY SELL] {signal_id} {action_label} | sim_pnl={sim_pnl_eur:+.2f}€ → {pnl_usd:+.2f}$")
        log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                       "token_symbol": token_symbol, "action": action_label,
                       "token_address": token_address, "tokens_amount": "0",
                       "eth_amount": f"{eth_received:.6f}", "tx_hash": "DRY_RUN",
                       "status": "dry_run", "note": f"sim_pnl={sim_pnl_eur:+.2f}€"})
        return True

    sell_wei = int(tokens_sell * (10**decimals))
    slippage = SLIPPAGE_BPS / 10_000

    log.info(f"[SELL] {signal_id} | {action_label} | {tokens_sell:.4f} {token_symbol} ({sell_fraction*100:.0f}%)")

    # Stima min_out in WETH
    price_usd = quote_onchain(token_address)
    weth_usd  = _get_weth_usd()
    eth_est   = 0.0
    min_out   = 0

    if action_label in _ILLIQUID_EXITS:
        # Pool potenzialmente prosciugato: min_out=0 — accetta qualsiasi output
        # piuttosto che fallire e restare bloccati su un token senza mercato
        min_out = 0
        log.info(f"[SELL] {action_label}: min_out=0 (pool potenzialmente illiquido)")
    elif price_usd and weth_usd > 0:
        eth_est = (tokens_sell * price_usd) / weth_usd
        min_out = int(eth_est * (10**18) * (1 - slippage))
    else:
        # Oracle non disponibile: floor al 20% del capitale investito
        eth_floor = pos.get("eth_spent", TRADE_SIZE_ETH) * sell_fraction * 0.20
        min_out   = int(eth_floor * 10**18)
        log.warning(f"[SELL] Oracle non disponibile — min_out floor: {eth_floor:.6f} ETH")

    tx_hash = "DRY_RUN"
    status  = "dry_run"

    if not _is_dry(signal_id):
        acc = _load_account()
        w3  = _get_w3()
        if not acc or not w3:
            log.error("[SELL] Wallet o Web3 non disponibili")
            return False

        _pool_info = _find_pool(token_address)
        pool_type  = _pool_info[1] if _pool_info else "unknown"
        fee        = 3000
        if pool_type == "v3" and _pool_info:
            try:
                pool_c = w3.eth.contract(address=_pool_info[0], abi=_ABI_V3_POOL)
                fee    = pool_c.functions.fee().call()
            except Exception:
                pass

        # Approva token per V3 Router
        if not _ensure_approval(token_address, UNIV3_ROUTER, sell_wei, acc.address):
            log.error("[SELL] Approve token→V3Router fallito")
            return False

        tx_hash_res = None
        if pool_type == "v3":
            tx_hash_res = _swap_v3(token_address, WETH_BASE, sell_wei, min_out, fee, acc, w3)

        if tx_hash_res is None and pool_type == "univ2":
            if not _ensure_approval(token_address, UNIV2_ROUTER, sell_wei, acc.address):
                log.error("[SELL] Approve token→UniV2Router fallito")
                return False
            tx_hash_res = _swap_v2(token_address, WETH_BASE, sell_wei, min_out, acc, w3)

        if tx_hash_res is None and pool_type != "univ2":
            if not _ensure_approval(token_address, AERO_ROUTER, sell_wei, acc.address):
                log.error("[SELL] Approve token→AeroRouter fallito")
                return False
            tx_hash_res = _swap_aero(token_address, WETH_BASE, sell_wei, min_out, acc, w3)

        if tx_hash_res is None:
            prev_fail = pos.get("sell_fail_count", 0)
            pos["sell_fail_count"] = prev_fail + 1
            STUCK_THRESHOLD = 3
            if pos["sell_fail_count"] >= STUCK_THRESHOLD and pos.get("status") != "stuck":
                pos["status"] = "stuck"
                log.error(f"[SELL] POSIZIONE BLOCCATA: {signal_id} — {pos['sell_fail_count']} tentativi. "
                          f"Vendere manualmente su Uniswap/Aerodrome.")
                log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                               "token_symbol": token_symbol, "action": f"{action_label}_stuck",
                               "token_address": token_address, "tokens_amount": f"{tokens_sell:.6f}",
                               "eth_amount": "0", "tx_hash": "", "status": "stuck",
                               "note": f"no_route x{pos['sell_fail_count']}"})
            else:
                log.error(f"[SELL] Tutti i router falliti per {token_symbol} "
                          f"(tentativo {pos['sell_fail_count']}/{STUCK_THRESHOLD})")
                log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                               "token_symbol": token_symbol, "action": f"{action_label}_failed",
                               "token_address": token_address, "tokens_amount": f"{tokens_sell:.6f}",
                               "eth_amount": "0", "tx_hash": "", "status": "error", "note": "no_route"})
            return False

        tx_hash = tx_hash_res
        status  = "sent"
        log.info(f"[SELL] TX inviata: {tx_hash}")

        # Leggi WETH ricevuto dallo swap (balance totale post-swap)
        try:
            weth_abi = _ABI_ERC20 + _ABI_WETH_EXTRA
            weth_c   = w3.eth.contract(address=WETH_BASE, abi=weth_abi)
            weth_bal = weth_c.functions.balanceOf(acc.address).call()
            eth_est  = weth_bal / 1e18
            log.info(f"[SELL] WETH balance post-sell: {eth_est:.6f} ETH (rimane in WETH per prossimo buy)")
        except Exception as e:
            log.warning(f"[SELL] Lettura WETH fallita: {e}")
    else:
        log.info(f"[DRY SELL] {tokens_sell:.4f} {token_symbol} → ~{eth_est:.6f} WETH")

    _eth_usd = _get_weth_usd()
    pos["tokens_held"]    -= tokens_sell
    pos["eth_received"]   += eth_est
    pos["exit_txs"].append(tx_hash)
    pos["real_pnl_eth"]    = pos["eth_received"] - pos["eth_spent"]
    pos["usdc_received"]   = pos["eth_received"] * _eth_usd
    pos["real_pnl_usdc"]   = pos["usdc_received"] - pos.get("usdc_spent", 0)
    pos["sell_fail_count"] = 0

    if sell_fraction >= 1.0 or pos["tokens_held"] <= 0.0001:
        pos["status"]   = "closed"
        pos["close_ts"] = datetime.now().isoformat()
        log.info(f"[SELL] {signal_id} chiusa | P&L reale: {pos['real_pnl_eth']:+.6f} ETH ({pos['real_pnl_usdc']:+.2f} USD)")

    log_execution({"ts": datetime.now().isoformat(), "signal_id": signal_id,
                   "token_symbol": token_symbol, "action": action_label,
                   "token_address": token_address, "tokens_amount": f"{tokens_sell:.6f}",
                   "eth_amount": f"{eth_est:.6f}", "tx_hash": tx_hash,
                   "status": status, "note": f"pnl={pos['real_pnl_eth']:+.6f}"})
    return True


# ---------------------------------------------------------------------------
# Stato reale
# ---------------------------------------------------------------------------
def load_base_state() -> dict:
    if not os.path.exists(BASE_STATE_FILE):
        return {}
    try:
        with open(BASE_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_base_state(state: dict):
    try:
        tmp = BASE_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, BASE_STATE_FILE)
    except Exception as e:
        log.warning(f"[State] Errore salvataggio: {e}")


def _ensure_exec_csv():
    if not os.path.exists(BASE_EXEC_CSV):
        with open(BASE_EXEC_CSV, "w", newline="") as f:
            csv.writer(f).writerow(["ts", "signal_id", "token_symbol", "action",
                                    "token_address", "tokens_amount", "eth_amount",
                                    "tx_hash", "status", "note"])


def log_execution(row: dict):
    _ensure_exec_csv()
    with open(BASE_EXEC_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts", "signal_id", "token_symbol", "action",
                                           "token_address", "tokens_amount", "eth_amount",
                                           "tx_hash", "status", "note"])
        w.writerow(row)


# ---------------------------------------------------------------------------
# Token lookup
# ---------------------------------------------------------------------------
def build_token_lookup() -> dict:
    """Costruisce {signal_id → {token_address, pair_address}} per Base chain.
    Legge da signals_log.csv (defi_optimized) e gems_log_v3.csv (gemmeV3,
    che include i segnali micro-cap Base routati a sistema 'defi')."""
    lookup = {}
    for path in [DEFI_SIGNALS, V3_SIGNALS, PUMP_GRAD_SIGNALS]:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    sid   = str(r.get("signal_id", "") or r.get("gem_id", "")).strip()
                    taddr = str(r.get("token_address", "") or "").strip()
                    paddr = str(r.get("pair_address",  "") or "").strip()
                    chain = str(r.get("chain", "")         or "").strip().lower()
                    if sid and taddr and chain == "base" and sid not in lookup:
                        lookup[sid] = {"token_address": taddr, "pair_address": paddr}
        except Exception:
            pass
    return lookup


# ---------------------------------------------------------------------------
# Process row from live_trades.csv
# ---------------------------------------------------------------------------
EXIT_ACTIONS = {
    "trail_exit", "tp1", "tp2", "hard_sl", "exit_bsr_collapse",
    "exit_vol_crash", "liq_collapse", "sl_adaptive", "exit_time_limit",
    # presenti in trade_simulator ma mancavano:
    "exit_adaptive",      # snap1: crash improvviso nelle prime barre
    "exit_momentum",      # momentum perso
    "exit_max_age",       # hold time massimo superato
    "exit_price_timeout", # timeout fetch prezzo
    "manual_close",       # chiusura manuale dal dashboard
}

# Per questi exit il pool potrebbe essere prosciugato → min_out=0
_ILLIQUID_EXITS = {"liq_collapse", "exit_vol_crash", "exit_adaptive"}


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
        # Conta solo posizioni LIVE (entry_tx != DRY_RUN) verso il limite MAX_POS.
        # Le posizioni dry_run non impegnano capitale reale e non devono bloccare trade live.
        open_pos = sum(
            1 for p in real_state.values()
            if p.get("status") == "open" and p.get("entry_tx", "DRY_RUN") != "DRY_RUN"
        )
        if not _is_dry(sid) and open_pos >= MAX_POS:
            log.info(f"[BUY] Limite {MAX_POS} posizioni aperte raggiunto — skip {sid}")
            processed.add(key)
            return False
        info = token_lookup.get(sid)
        if not info:
            _miss_counts[sid] = _miss_counts.get(sid, 0) + 1
            if _miss_counts[sid] == 1:
                # Primo miss: il segnale potrebbe essere appena stato scritto nel CSV.
                # Forza rebuild immediato prima di contare il miss.
                token_lookup.clear()
                token_lookup.update(build_token_lookup())
                info = token_lookup.get(sid)
            if not info:
                if _miss_counts[sid] >= 6:
                    log.warning(f"[BUY] {sid}: token_address non trovato dopo 6 cicli — skip definitivo")
                    processed.add(key)
                else:
                    log.debug(f"[BUY] {sid}: token_address non trovato (tentativo {_miss_counts[sid]}/6)")
                return False
        token_address = info.get("token_address", "")
        pair_address  = info.get("pair_address", "")
        if not token_address:
            log.warning(f"[BUY] {sid}: token_address vuoto — skip")
            processed.add(key)
            return False
        sym = row.get("token_symbol", "?")
        ok  = execute_buy(sid, sym, token_address, pair_address, system, real_state)
        processed.add(key)
        return ok

    elif action in EXIT_ACTIONS:
        if sid not in real_state or real_state[sid].get("status") != "open":
            return False
        # remaining nel CSV dice quanta posizione rimane dopo questo exit:
        # pump_grad tp1_fraction=1.0 → remaining=0 → vende tutto
        # altri sistemi tp1_fraction=0.5 → remaining=0.5 → vende metà
        if action == "tp1":
            try:
                rem = float(row.get("remaining", "0.5") or 0.5)
                frac = 1.0 if rem <= 0 else 0.5
            except (ValueError, TypeError):
                frac = 0.5
        else:
            frac = 1.0
        return execute_sell(sid, frac, action, real_state)

    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(stop_event=None):
    _load_honeypot_bl()
    _liq_mode = "🟢 LIVE" if _LIQ_LIVE else "🔵 dry_run"
    _other_mode = "🔵 dry_run" if DRY_RUN else "🟢 LIVE"
    log.info("=" * 60)
    log.info("  Base Executor avviato")
    log.info(f"  LIQ_*      = {_liq_mode}  |  altri = {_other_mode}")
    log.info(f"  TRADE_SIZE = {TRADE_SIZE_ETH} ETH/trade")
    log.info(f"  MAX_POS    = {MAX_POS}")
    log.info(f"  SLIPPAGE   = {SLIPPAGE_BPS}bps")
    log.info(f"  RPC        = {BASE_RPC_URL}")
    log.info(f"  HONEYPOT_BL= {len(_honeypot_sym_bl)} simboli")
    log.info("=" * 60)

    if not WEB3_AVAILABLE:
        log.error("web3 non installato — pip install web3")
        return

    w3 = _get_w3()
    if w3:
        block = w3.eth.block_number
        log.info(f"  Base connessa — blocco #{block:,}")
        weth_price = _get_weth_usd()
        log.info(f"  WETH/USD (Chainlink) = ${weth_price:,.2f}")
    else:
        log.warning("  Base RPC non raggiungibile — continuo in DRY_RUN")

    if DRY_RUN:
        log.info("  [DRY RUN] Nessuna tx reale")
    else:
        acc = _load_account()
        if acc:
            log.info(f"  Wallet: {acc.address}")
        else:
            log.error("  BASE_PRIVATE_KEY mancante in .env — uscita")
            return

    real_state   = load_base_state()
    token_lookup = build_token_lookup()
    processed    = set()

    # Bootstrap: marca righe storiche come già viste
    if os.path.exists(LIVE_CSV):
        open_sids   = {sid for sid, p in real_state.items() if p.get("status") == "open"}
        bought_sids = set(real_state.keys())
        now_ts      = datetime.now()
        RETRY_WINDOW_H = 24
        snap = 0
        try:
            # Pre-scan: segnali Base che hanno già un exit (già chiusi nel simulator)
            # → non tentare buy su trade già conclusi, anche se < 24h
            all_base = []
            with open(LIVE_CSV, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("chain","").lower() == "base":
                        all_base.append(row)
            already_closed = {
                r["signal_id"] for r in all_base
                if r.get("exit_reason","open") not in ("open","")
                and r.get("action","") not in ("entry","tp1")
            }

            for row in all_base:
                sid_r  = row.get("signal_id", "").strip()
                act_r  = row.get("action",    "").strip()
                key    = f"{sid_r}|{act_r}"
                if sid_r in open_sids and act_r != "entry":
                    continue
                if act_r == "entry" and sid_r not in bought_sids:
                    # Skip il retry solo se il segnale NON è già chiuso nel simulator
                    if sid_r not in already_closed:
                        try:
                            age_h = (now_ts - datetime.fromisoformat(
                                row.get("ts", ""))).total_seconds() / 3600
                            if age_h < RETRY_WINDOW_H:
                                continue
                        except Exception:
                            pass
                processed.add(key)
                snap += 1
        except Exception as e:
            log.warning(f"Bootstrap fallito: {e}")
        if snap:
            log.info(f"  [BOOTSTRAP] {snap} righe Base storiche marcate")

    open_pos = sum(1 for p in real_state.values() if p.get("status") == "open")
    stuck    = [(s, p) for s, p in real_state.items() if p.get("status") == "stuck"]
    log.info(f"  Posizioni Base caricate: {len(real_state)} | Aperte: {open_pos}")
    if stuck:
        for sid_s, p_s in stuck:
            log.error(f"  STUCK: {sid_s} ({p_s.get('token_symbol','?')}) — vendere su Uniswap/Aerodrome")
    log.info(f"  Token in lookup: {len(token_lookup)}")
    log.info("  In ascolto su live_trades.csv (Base)...")
    log.info("-" * 60)

    # Ctrl+C handler (solo se lanciato direttamente)
    if stop_event is None:
        _last_ctrl_c = [0.0]
        _abort       = [False]

        def _on_sigint(sig, frame):
            now = time.time()
            if now - _last_ctrl_c[0] < 6.0:
                log.info("[base] Stop immediato — posizioni lasciate aperte.")
                _abort[0] = True
                raise SystemExit(0)
            _last_ctrl_c[0] = now
            log.info("[base] Ctrl+C — Ctrl+C di nuovo entro 5s per uscire SENZA chiudere.")
            log.info("[base]    Altrimenti chiusura automatica tra 5 secondi...")

            def _countdown():
                for i in range(5, 0, -1):
                    if _abort[0]:
                        return
                    log.info(f"[base]    Chiusura in {i}s...")
                    time.sleep(1)
                if _abort[0]:
                    return
                for sid_c, pos_c in list(real_state.items()):
                    if pos_c.get("status") == "open":
                        execute_sell(sid_c, 1.0, "manual_close", real_state)
                save_base_state(real_state)
                raise SystemExit(0)

            threading.Thread(target=_countdown, daemon=True).start()

        signal.signal(signal.SIGINT, _on_sigint)
    else:
        _abort = [False]

    cycle = 0
    while not _abort[0]:
        if stop_event is not None and stop_event.is_set():
            log.info("[base] stop_event ricevuto — uscita.")
            break
        try:
            cycle += 1
            if cycle % 12 == 0:
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
                    save_base_state(real_state)

            if actions:
                op = sum(1 for p in real_state.values() if p.get("status") == "open")
                log.info(f"[ciclo {cycle}] {actions} azioni Base | aperte: {op}")

        except SystemExit:
            save_base_state(real_state)
            raise
        except Exception as e:
            log.error(f"Errore ciclo: {e}")

        time.sleep(5)

    save_base_state(real_state)


if __name__ == "__main__":
    main()
