"""
payments.py — verifica pagamenti crypto on-chain e attivazione automatica.
Riusa gli RPC già configurati in executor/.env (Helius per Solana, Alchemy per Base).

Modello a INVOICE con importo univoco:
  - l'utente riceve un importo da pagare = prezzo_tier + centesimi-nonce (es. 49.07 USDC)
  - i centesimi univoci identificano l'invoice senza bisogno di memo
  - il watcher rileva i Transfer USDC in entrata al wallet e fa match per importo esatto

Pagamenti supportati (stablecoin, 1:1 con USD):
  - Base (EVM):  USDC via eth_getLogs (event Transfer → PAY_WALLET_EVM)
  - Solana:      USDC via getSignaturesForAddress + parsing token balance delta

NB: è il pezzo a priorità più bassa. Finché non è validato, usa /grant manuale (bot.py).
"""
from __future__ import annotations

import logging
import secrets
import time

import requests

import config
import store
import subscriptions as subs
import telegram_api as tg

log = logging.getLogger("payments")

_INVOICES_FILE = "invoices.json"
_PAYWATCH_FILE = "paywatch.json"      # stato scansione (ultimo blocco/firma)

# USDC contracts (override via env se necessario)
USDC_BASE = config._env("USDC_BASE_CONTRACT", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
USDC_SOL  = config._env("USDC_SOL_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
USDC_DECIMALS = 6
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


# ── INVOICE ─────────────────────────────────────────────────────────────────────
def _invoices() -> dict:
    return store.load(_INVOICES_FILE, {})


def _save_invoices(d: dict) -> None:
    store.save(_INVOICES_FILE, d)


def create_invoice(chat_id, tier: str, chain: str) -> dict | None:
    """Crea un invoice con importo univoco. chain: 'base' | 'solana'."""
    price = config.TIER_PRICES.get(tier)
    if price is None:
        return None
    inv = _invoices()
    # centesimi-nonce univoci tra gli invoice aperti
    used = {round(float(v["amount"]) % 1, 2) for v in inv.values()
            if v["chain"] == chain and v["status"] == "pending"}
    cents = None
    for _ in range(200):
        c = round(secrets.randbelow(99) / 100 + 0.01, 2)
        if c not in used:
            cents = c
            break
    if cents is None:
        return None
    amount = round(price + cents, 2)
    ref = secrets.token_hex(4)
    inv[ref] = {
        "chat_id": str(chat_id), "tier": tier, "chain": chain,
        "amount": amount, "status": "pending", "created_at": time.time(),
    }
    _save_invoices(inv)
    return inv[ref]


def _settle(ref: str):
    inv = _invoices()
    rec = inv.get(ref)
    if not rec or rec["status"] != "pending":
        return
    rec["status"] = "paid"
    rec["paid_at"] = time.time()
    _save_invoices(inv)
    subs.grant(rec["chat_id"], rec["tier"], config.SUB_DAYS)
    # referral bonus: estende chi ha invitato
    referrer = (subs.get(rec["chat_id"]) or {}).get("referred_by")
    if referrer:
        subs.grant(referrer, subs.tier_of(referrer) if subs.is_active(referrer)
                   else config.TIER_PREMIUM, days=7)
    try:
        import bot
        bot.deliver_access(rec["chat_id"], rec["tier"])
    except Exception:
        tg.send_message(rec["chat_id"], f"✅ Pagamento ricevuto — {rec['tier'].upper()} attivo.")
    log.info("[pay] invoice %s saldato: %s %s", ref, rec["amount"], rec["chain"])


def _match_amount(chain: str, amount: float) -> str | None:
    """Trova un invoice pending con quell'importo esatto (tolleranza 1 cent)."""
    for ref, rec in _invoices().items():
        if (rec["status"] == "pending" and rec["chain"] == chain
                and abs(float(rec["amount"]) - amount) < 0.005):
            return ref
    return None


# ── BASE (EVM) USDC ──────────────────────────────────────────────────────────────
def _rpc(url: str, method: str, params: list):
    try:
        r = requests.post(url, json={"jsonrpc": "2.0", "id": 1,
                                     "method": method, "params": params}, timeout=20)
        return r.json().get("result")
    except (requests.RequestException, ValueError) as e:
        log.warning("[pay] RPC %s err: %s", method, e)
        return None


def _scan_base(state: dict):
    if not (config.PAY_WALLET_EVM and config.BASE_RPC_URL):
        return
    latest_hex = _rpc(config.BASE_RPC_URL, "eth_blockNumber", [])
    if not latest_hex:
        return
    latest = int(latest_hex, 16)
    from_block = state.get("base_block", latest - 100)
    if from_block > latest:
        from_block = latest - 100
    pad = config.PAY_WALLET_EVM.lower().replace("0x", "").rjust(64, "0")
    logs = _rpc(config.BASE_RPC_URL, "eth_getLogs", [{
        "fromBlock": hex(max(from_block, 0)), "toBlock": hex(latest),
        "address": USDC_BASE, "topics": [_TRANSFER_TOPIC, None, "0x" + pad],
    }]) or []
    for lg in logs:
        try:
            raw = int(lg["data"], 16)
            amount = raw / (10 ** USDC_DECIMALS)
        except (KeyError, ValueError):
            continue
        ref = _match_amount("base", round(amount, 2))
        if ref:
            _settle(ref)
    state["base_block"] = latest + 1


# ── SOLANA USDC ──────────────────────────────────────────────────────────────────
def _scan_solana(state: dict):
    if not (config.PAY_WALLET_SOL and config.SOLANA_RPC_URL):
        return
    last_sig = state.get("sol_last_sig")
    params = [config.PAY_WALLET_SOL, {"limit": 25}]
    if last_sig:
        params[1]["until"] = last_sig
    sigs = _rpc(config.SOLANA_RPC_URL, "getSignaturesForAddress", params) or []
    if not sigs:
        return
    newest = sigs[0]["signature"]
    for s in reversed(sigs):                      # dal più vecchio
        tx = _rpc(config.SOLANA_RPC_URL, "getTransaction",
                  [s["signature"], {"encoding": "jsonParsed",
                                    "maxSupportedTransactionVersion": 0}])
        if not tx:
            continue
        amount = _sol_usdc_credit(tx)
        if amount is not None:
            ref = _match_amount("solana", round(amount, 2))
            if ref:
                _settle(ref)
    state["sol_last_sig"] = newest


def _sol_usdc_credit(tx: dict) -> float | None:
    """Variazione netta del saldo USDC dell'owner PAY_WALLET_SOL nella tx."""
    try:
        meta = tx["meta"]
        pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances", [])
               if b.get("mint") == USDC_SOL and b.get("owner") == config.PAY_WALLET_SOL}
        post = {b["accountIndex"]: b for b in meta.get("postTokenBalances", [])
                if b.get("mint") == USDC_SOL and b.get("owner") == config.PAY_WALLET_SOL}
        delta = 0.0
        for idx, b in post.items():
            after = float(b["uiTokenAmount"]["uiAmount"] or 0)
            before = float(pre.get(idx, {}).get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
            delta += after - before
        return delta if delta > 0 else None
    except (KeyError, TypeError, ValueError):
        return None


# ── LOOP ──────────────────────────────────────────────────────────────────────────
def run(stop_event=None, interval_sec: float = 60.0):
    log.info("[pay] payment watcher avviato")
    while stop_event is None or not stop_event.is_set():
        state = store.load(_PAYWATCH_FILE, {})
        try:
            _scan_base(state)
            _scan_solana(state)
        except Exception as e:
            log.exception("[pay] errore scan: %s", e)
        store.save(_PAYWATCH_FILE, state)
        if stop_event:
            stop_event.wait(interval_sec)
        else:
            time.sleep(interval_sec)


def main(stop_event=None):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(name)s | %(message)s")
    run(stop_event)


if __name__ == "__main__":
    main()
