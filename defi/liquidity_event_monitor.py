"""
liquidity_event_monitor.py — Monitor nuovi pool liquidità su Solana e Base.

Due path paralleli:
  1. GeckoTerminal polling ogni 30s (Solana + Base fallback)
  2. WebSocket on-chain su Base: PairCreated event dal factory Uniswap V2
     → latenza ~2s (1 blocco), zero polling API

Azioni comuni:
  - Alert Telegram immediato (liq>$10k)
  - Segnale in pump_grad_signals.csv (liq>$25k)
  - Shadow queue (liq $10k-$25k)
  - liq_event_signals.csv (log storico)
"""
import asyncio
import csv
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

_HERE = Path(__file__).parent
_ROOT = _HERE.parent
_EXEC = _ROOT / "executor"

for _p in [str(_HERE), str(_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=_EXEC / ".env", override=False)
except ImportError:
    pass

log = logging.getLogger("liq_monitor")

POLL_SEC          = 30
MAX_POOL_AGE_MIN  = 5
MIN_LIQ_ALERT     = 10_000   # soglia Telegram alert
MIN_LIQ_SIGNAL    = 25_000   # soglia per aprire trade via pump_grad (backtest: <25k=69% rug)
SEEN_TTL_SEC      = 3600

_REPORTS          = _HERE / "reports"
_CSV_OUT          = _REPORTS / "liq_event_signals.csv"
_PUMP_GRAD_CSV    = _REPORTS / "pump_grad_signals.csv"
_SHADOW_QUEUE_CSV = _REPORTS / "liq_shadow_queue.csv"
_PUMP_GRAD_COLS  = [
    "signal_id", "timestamp_entry", "token_symbol", "token_name",
    "token_address", "chain", "pair_address", "price_entry_usd",
    "volume_1h_usd", "liquidity_usd", "buy_sell_ratio_1h", "change_1h_pct",
    "pump_probability", "buy_tax", "sell_tax", "lp_locked", "is_honeypot",
    "top_features",
]
_CHAINS  = ["solana", "base"]
_GT_BASE = "https://api.geckoterminal.com/api/v2"
_HEADERS = {"Accept": "application/json;version=20230302"}

_seen: dict[str, float] = {}


def _purge_seen():
    cutoff = time.time() - SEEN_TTL_SEC
    for k in list(_seen.keys()):
        if _seen[k] < cutoff:
            del _seen[k]


def _fetch_new_pools(chain: str) -> list[dict]:
    url = f"{_GT_BASE}/networks/{chain}/new_pools?page=1"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=10)
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        log.debug(f"[liq] fetch {chain}: {e}")
        return []


def _pool_age_min(created_at_str: str) -> float:
    try:
        dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60
    except Exception:
        return 999


def _extract_pool_data(pool: dict, chain: str) -> dict:
    """Estrae campi utili dal response GeckoTerminal new_pools."""
    attrs = pool.get("attributes", {})
    pool_id = pool.get("id", "")
    addr = pool_id.split("_")[-1]

    # token_address: relationships.base_token.data.id = "solana_ADDRESS"
    rel_id = (pool.get("relationships", {})
                  .get("base_token", {})
                  .get("data", {})
                  .get("id", ""))
    token_address = rel_id.split("_", 1)[-1] if "_" in rel_id else ""

    name_parts = attrs.get("name", "? / ?").split(" / ")
    token_symbol = name_parts[0].strip() if name_parts else "?"

    liq      = float(attrs.get("reserve_in_usd", 0) or 0)
    price    = float(attrs.get("base_token_price_usd", 0) or 0)
    vol_h1   = float((attrs.get("volume_usd") or {}).get("h1", 0) or 0)
    chg_1h   = float((attrs.get("price_change_percentage") or {}).get("h1", 0) or 0)
    age_min  = _pool_age_min(attrs.get("pool_created_at", ""))

    return {
        "addr":          addr,
        "token_address": token_address,
        "token_symbol":  token_symbol,
        "liq":           liq,
        "price":         price,
        "vol_h1":        vol_h1,
        "chg_1h":        chg_1h,
        "age_min":       age_min,
        "created_at":    attrs.get("pool_created_at", ""),
    }


def _notify(d: dict, chain: str):
    # Stessi filtri del simulator (publisher.py linee 163-169):
    # evita alert per pool che verranno comunque scartate → era 87% spam
    if d["chg_1h"] > 80:
        log.debug(f"[liq] notify skip {d['token_symbol']}: chg1h={d['chg_1h']:+.0f}% > 80%")
        return
    if 0 < d["vol_h1"] < 5_000:
        log.debug(f"[liq] notify skip {d['token_symbol']}: vol_h1=${d['vol_h1']:,.0f} < $5k")
        return
    try:
        import tg_alert
        chain_emoji = {"solana": "🟣", "base": "🔵"}.get(chain, "🔹")
        dex_url = f"https://dexscreener.com/{chain}/{d['addr']}"
        text = (
            f"💧 <b>Nuova pool</b> · {chain_emoji} {chain.upper()} 🚀\n"
            f"<b>${d['token_symbol']}</b> · età {d['age_min']:.1f} min\n"
            f"Liq: <b>${d['liq']:,.0f}</b> · Vol1h: ${d['vol_h1']:,.0f}\n"
            f"<a href='{dex_url}'>DexScreener</a>"
        )
        tg_alert.send(text)
    except Exception as e:
        log.debug(f"[liq] notify: {e}")


def _append_log_csv(d: dict, chain: str):
    row = {
        "ts":            datetime.now().isoformat(),
        "chain":         chain,
        "pool_address":  d["addr"],
        "token_symbol":  d["token_symbol"],
        "liquidity_usd": f"{d['liq']:.0f}",
        "vol_h1":        f"{d['vol_h1']:.0f}",
        "age_min":       f"{d['age_min']:.1f}",
        "signal_sent":   "1" if d["liq"] >= MIN_LIQ_SIGNAL else "0",
    }
    _REPORTS.mkdir(parents=True, exist_ok=True)
    new_file = not _CSV_OUT.exists()
    with open(_CSV_OUT, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new_file:
            w.writeheader()
        w.writerow(row)


def _build_signal_row(d: dict, chain: str) -> tuple[str, dict]:
    """Costruisce sid e riga CSV comune per segnali e shadow."""
    ts  = datetime.now().isoformat()
    sid = f"LIQ_{d['token_symbol']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    top_feat = (
        f"liq_monitor=true | pair_age_min={d['age_min']:.1f} | "
        f"vol_h1={d['vol_h1']:.0f} | chg_1h={d['chg_1h']:+.1f}%"
    )
    row = {
        "signal_id":        sid,
        "timestamp_entry":  ts,
        "token_symbol":     d["token_symbol"],
        "token_name":       d["token_symbol"],
        "token_address":    d["token_address"],
        "chain":            chain,
        "pair_address":     d["addr"],
        "price_entry_usd":  f"{d['price']:.8g}",
        "volume_1h_usd":    f"{d['vol_h1']:.2f}",
        "liquidity_usd":    f"{d['liq']:.2f}",
        "buy_sell_ratio_1h": "1.0",
        "change_1h_pct":    f"{d['chg_1h']:.2f}",
        "pump_probability": "0.75",
        "buy_tax":          "0.0",
        "sell_tax":         "0.0",
        "lp_locked":        "0",
        "is_honeypot":      "0",
        "top_features":     top_feat,
    }
    return sid, row


def _write_pump_grad_signal(d: dict, chain: str):
    """Scrive segnale reale in pump_grad_signals.csv (liq>=$25k)."""
    sid, row = _build_signal_row(d, chain)
    new_file = not _PUMP_GRAD_CSV.exists()
    with open(_PUMP_GRAD_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_PUMP_GRAD_COLS)
        if new_file:
            w.writeheader()
        w.writerow(row)
    log.info(
        f"[liq] ✅ segnale: {d['token_symbol']} {chain} "
        f"liq=${d['liq']:,.0f} vol1h=${d['vol_h1']:,.0f} → {sid}"
    )


def _write_shadow_queue(d: dict, chain: str):
    """Scrive pool liq $10k-$25k in liq_shadow_queue.csv.
    Il simulator lo legge, chiama _shadow_register, poi tronca il file."""
    sid, row = _build_signal_row(d, chain)
    new_file = not _SHADOW_QUEUE_CSV.exists()
    with open(_SHADOW_QUEUE_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_PUMP_GRAD_COLS)
        if new_file:
            w.writeheader()
        w.writerow(row)
    log.debug(
        f"[liq] 👻 shadow_queue: {d['token_symbol']} {chain} "
        f"liq=${d['liq']:,.0f} → {sid}"
    )


def _tick():
    _purge_seen()
    now = time.time()
    for chain in _CHAINS:
        for pool in _fetch_new_pools(chain):
            pool_id = pool.get("id", "")
            if not pool_id or pool_id in _seen:
                continue
            _seen[pool_id] = now
            d = _extract_pool_data(pool, chain)
            if d["liq"] < MIN_LIQ_ALERT or d["age_min"] > MAX_POOL_AGE_MIN:
                continue
            _append_log_csv(d, chain)
            if d["liq"] >= MIN_LIQ_SIGNAL:
                _notify(d, chain)
                try:
                    _write_pump_grad_signal(d, chain)
                except Exception as e:
                    log.warning(f"[liq] write_pump_grad_signal: {e}")
            else:
                # liq $10k-$25k: shadow queue separata, pump_grad_signals.csv rimane pulito.
                try:
                    _write_shadow_queue(d, chain)
                except Exception as e:
                    log.warning(f"[liq] write_shadow_queue: {e}")


# ---------------------------------------------------------------------------
# WebSocket Base V2 factory — PairCreated on-chain (~2s latenza, 0 API poll)
# ---------------------------------------------------------------------------
_BASE_RPC_WSS     = os.environ.get("BASE_RPC_URL", "").replace("https://", "wss://")
_WETH_BASE        = "0x4200000000000000000000000000000000000006"
_UNIV2_FACTORY_B  = "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"
# keccak256("PairCreated(address,address,address,uint256)")
_PAIR_CREATED_SIG = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"

_ABI_UNIV2_PAIR = [
    {"name": "getReserves", "type": "function", "stateMutability": "view",
     "inputs": [],
     "outputs": [{"name": "reserve0", "type": "uint112"},
                 {"name": "reserve1", "type": "uint112"},
                 {"name": "blockTimestampLast", "type": "uint32"}]},
    {"name": "token0", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "string"}]},
]
_ABI_ERC20_SYM = [
    {"name": "symbol",   "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "string"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
]

_weth_usd_cache: dict = {"price": 0.0, "ts": 0.0}


def _get_weth_usd_cached() -> float:
    """WETH/USD da DexScreener, cache 2 min."""
    now = time.time()
    if now - _weth_usd_cache["ts"] < 120 and _weth_usd_cache["price"] > 0:
        return _weth_usd_cache["price"]
    try:
        r = requests.get(
            "https://api.dexscreener.com/latest/dex/pairs/base/"
            "0x4200000000000000000000000000000000000006",
            timeout=5)
        if r.status_code == 200:
            pairs = r.json().get("pairs") or []
            for p in pairs:
                px = float(p.get("priceUsd") or 0)
                if px > 0:
                    _weth_usd_cache.update({"price": px, "ts": now})
                    return px
    except Exception:
        pass
    # Fallback: usa Chainlink via base_executor se disponibile
    try:
        from base_executor import _get_weth_usd
        px = _get_weth_usd()
        if px > 0:
            _weth_usd_cache.update({"price": px, "ts": now})
            return px
    except Exception:
        pass
    return _weth_usd_cache["price"] or 1800.0


def _handle_pair_created(log_entry: dict) -> None:
    """
    Chiamata quando arriva un evento PairCreated dal factory V2 su Base.
    Calcola liquidità da reserves on-chain, emette segnale se > soglia.
    """
    try:
        topics = log_entry.get("topics", [])
        if len(topics) < 3:
            return
        # topics[1] = token0 (indexed, bytes32 padded), topics[2] = token1
        token0 = "0x" + topics[1][-40:]
        token1 = "0x" + topics[2][-40:]
        # data: abi-encoded (pair_address, uint)
        raw_data = log_entry.get("data", "0x")
        pair_addr = "0x" + raw_data[26:66]  # bytes 12-31 of first 32-byte word

        pair_id = f"base_{pair_addr.lower()}"
        if pair_id in _seen:
            return
        _seen[pair_id] = time.time()

        # Determina quale token è WETH
        weth_lc = _WETH_BASE.lower()
        if token0.lower() == weth_lc:
            token_addr = token1
            weth_is_t0 = True
        elif token1.lower() == weth_lc:
            token_addr = token0
            weth_is_t0 = False
        else:
            log.debug(f"[liq/ws] pair {pair_addr[:12]}: nessun WETH — skip")
            return

        # Retry reserves fino a che la liquidità viene aggiunta (max 30s, poll 2s)
        # PairCreated scatta al deploy del pair (reserves=0); la liquidità arriva
        # nella tx successiva, tipicamente nello stesso blocco o 1-2 blocchi dopo.
        try:
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider(
                os.environ.get("BASE_RPC_URL", "https://mainnet.base.org"),
                request_kwargs={"timeout": 8}
            ))
            pair_c  = w3.eth.contract(
                address=Web3.to_checksum_address(pair_addr), abi=_ABI_UNIV2_PAIR)
            tok_c   = w3.eth.contract(
                address=Web3.to_checksum_address(token_addr), abi=_ABI_ERC20_SYM)

            weth_raw = 0
            tok_raw  = 0
            for attempt in range(15):          # max 15 × 2s = 30s
                time.sleep(2)
                reserves = pair_c.functions.getReserves().call()
                r0, r1   = reserves[0], reserves[1]
                weth_raw = r0 if weth_is_t0 else r1
                tok_raw  = r1 if weth_is_t0 else r0
                if weth_raw > 0:
                    log.debug(f"[liq/ws] {pair_addr[:12]} liquidità trovata dopo {(attempt+1)*2}s")
                    break
            if weth_raw == 0:
                log.debug(f"[liq/ws] {pair_addr[:12]}: reserves=0 dopo 30s — skip")
                return

            tok_dec     = tok_c.functions.decimals().call()
            tok_sym     = tok_c.functions.symbol().call()

            weth_amt    = weth_raw / 1e18
            weth_usd    = _get_weth_usd_cached()
            liq_usd     = weth_amt * weth_usd * 2   # liq totale = 2× lato WETH

            tok_amt     = tok_raw / (10 ** tok_dec)
            price_usd   = (weth_amt * weth_usd / tok_amt) if tok_amt > 0 else 0.0

        except Exception as e:
            log.debug(f"[liq/ws] reserves {pair_addr[:12]}: {e}")
            return

        if liq_usd < MIN_LIQ_ALERT:
            log.debug(f"[liq/ws] {tok_sym} liq=${liq_usd:.0f} < ${MIN_LIQ_ALERT:,} — skip")
            return

        # Honeypot check (view call, zero gas):
        # Simula sell 1% dei token → WETH con formula V2 constant product.
        # Se tok_raw=0 o l'output simulato è <0.1% del lato WETH → pool non vendibile.
        # Non cattura tutte le transfer fee, ma elimina i casi ovvi (reserve=0 o pool drenata).
        if tok_raw == 0:
            log.info(f"[liq/ws] {tok_sym} honeypot: tok_reserve=0 — skip")
            return
        _test_sell    = tok_raw // 100          # 1% delle riserve token
        _sim_weth_out = (weth_raw * _test_sell) // (tok_raw + _test_sell)
        _min_expected = weth_raw // 1000        # almeno 0.1% del lato WETH
        if _sim_weth_out < _min_expected:
            log.info(f"[liq/ws] {tok_sym} honeypot: sell simulato=${_sim_weth_out/1e18*_get_weth_usd_cached():.2f} troppo basso — skip")
            return

        age_min = 0.1  # appena creata
        d = {
            "addr":         pair_addr,
            "token_address": token_addr,
            "token_symbol":  tok_sym,
            "liq":           liq_usd,
            "price":         price_usd,
            "vol_h1":        0.0,
            "chg_1h":        0.0,
            "age_min":       age_min,
        }
        log.info(f"[liq/ws] ⚡ Base PairCreated: {tok_sym} liq=${liq_usd:,.0f}")
        _append_log_csv(d, "base")

        if liq_usd >= MIN_LIQ_SIGNAL:
            _notify(d, "base")
            try:
                _write_pump_grad_signal(d, "base")
            except Exception as e:
                log.warning(f"[liq/ws] write signal: {e}")
        else:
            try:
                _write_shadow_queue(d, "base")
            except Exception as e:
                log.warning(f"[liq/ws] write shadow: {e}")

    except Exception as e:
        log.warning(f"[liq/ws] handle_pair_created: {e}")


async def _ws_base_factory(stop_event: threading.Event) -> None:
    """Loop WebSocket asincrono: subscribe a PairCreated sul factory V2 Base."""
    import websockets

    if not _BASE_RPC_WSS or _BASE_RPC_WSS.startswith("wss://https"):
        log.warning("[liq/ws] BASE_RPC_URL non configurato per WSS — skip")
        return

    while not stop_event.is_set():
        try:
            async with websockets.connect(
                _BASE_RPC_WSS, ping_interval=20, ping_timeout=30
            ) as ws:
                sub = json.dumps({
                    "jsonrpc": "2.0", "id": 1,
                    "method": "eth_subscribe",
                    "params": ["logs", {
                        "address": _UNIV2_FACTORY_B,
                        "topics":  [_PAIR_CREATED_SIG]
                    }]
                })
                await ws.send(sub)
                resp = json.loads(await ws.recv())
                sub_id = resp.get("result")
                log.info(f"[liq/ws] ✅ subscribed PairCreated Base V2 (sub={sub_id})")

                while not stop_event.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=60)
                        data = json.loads(msg)
                        entry = data.get("params", {}).get("result")
                        if entry:
                            # esegui in thread separato per non bloccare il loop WS
                            threading.Thread(
                                target=_handle_pair_created,
                                args=(entry,), daemon=True
                            ).start()
                    except asyncio.TimeoutError:
                        pass  # keepalive, riprova
        except Exception as e:
            log.warning(f"[liq/ws] Base factory WS disconnesso: {e} — reconnect 10s")
            await asyncio.sleep(10)


def _start_base_ws_thread(stop_event: threading.Event) -> None:
    """Avvia il loop WebSocket in un thread daemon con il proprio event loop."""
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_ws_base_factory(stop_event))
        finally:
            loop.close()
    t = threading.Thread(target=_run, name="liq_ws_base", daemon=True)
    t.start()
    return t


def main(stop_event: threading.Event | None = None):
    log.info(
        f"[liq] ▶ avviato (poll {POLL_SEC}s, alert>${MIN_LIQ_ALERT:,}, "
        f"segnale>${MIN_LIQ_SIGNAL:,}, età<{MAX_POOL_AGE_MIN}min)"
    )
    # Avvia WebSocket Base V2 factory in parallelo (latenza ~2s, no API poll)
    _ws_stop = stop_event or threading.Event()
    _start_base_ws_thread(_ws_stop)
    while True:
        if stop_event and stop_event.is_set():
            break
        try:
            _tick()
        except Exception as e:
            log.warning(f"[liq] tick error: {e}")
        if stop_event:
            stop_event.wait(POLL_SEC)
        else:
            time.sleep(POLL_SEC)
    log.info("[liq] ■ fermato")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    main()
