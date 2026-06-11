"""
wallet_alpha_finder.py
======================
Identifica i wallet "alpha" su Solana che comprano token early nei pump.

Logica (approccio backward):
  1. Parte dai token che SAI hanno pompato (pump_grad_signals.csv + real_executions.csv)
  2. Per ogni token: recupera le prime N transazioni sul pool Raydium/PumpSwap
  3. Estrae i wallet che hanno comprato nelle prime 10 minuti
  4. Score: wallet che appaiono su più token vincenti = alpha wallet
  5. Verifica ogni wallet: analizza la sua storia swap con Helius Enhanced API
  6. Output: alpha_wallets.json con ranking e statistiche

API usate:
  - Helius Enhanced Transactions (gratis, 1M req/mese) → parse swap tx
  - Solana RPC standard → getSignaturesForAddress per trovare tx del pool
  - DexScreener → verifica che il token abbia effettivamente pompato

Setup:
  HELIUS_API_KEY=xxx  in executor/.env
  (opzionale) SOLANA_RPC=https://...   default: mainnet Helius

Avvio:
  python wallet_alpha_finder.py
  python wallet_alpha_finder.py --min-tokens 2 --top 30
"""

import argparse
import csv
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

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
log = logging.getLogger("alpha_finder")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HELIUS_API_KEY  = os.getenv("HELIUS_API_KEY", "")
SOLANA_RPC      = os.getenv("SOLANA_RPC", f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}")

ROOT            = Path(__file__).parent.parent
PUMP_GRAD_CSV   = ROOT / "defi" / "reports" / "pump_grad_signals.csv"
LIVE_TRADES_CSV = ROOT / "defi" / "reports" / "live_trades.csv"
REAL_EXEC_CSV   = Path(__file__).parent / "real_executions.csv"
OUTPUT_FILE     = Path(__file__).parent / "alpha_wallets.json"

# Finestra early: wallet che comprano entro X minuti dal segnale = alpha
EARLY_WINDOW_MIN   = 10
# Paginazione getSignaturesForAddress: firme per pagina e max pagine.
# Le firme arrivano newest-first: per pool ancora attivi la finestra early
# è molto indietro nella history → si pagina con `before` fino a signal_ts.
SIGS_PAGE_LIMIT    = 1000
MAX_SIG_PAGES      = 15
# Batch size per Helius parse API
HELIUS_BATCH_SIZE  = 100
# Quante tx totali analizzare per la history di ogni wallet
WALLET_HISTORY_LIMIT = 200

# Token noti non-alpha da escludere (SOL, USDC, USDT, WSOL, etc.)
STABLECOIN_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "So11111111111111111111111111111111111111112",     # wSOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # ETH (wormhole)
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rpc(method: str, params: list, retries: int = 3) -> Optional[dict]:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(retries):
        try:
            r = requests.post(SOLANA_RPC, json=payload, timeout=15)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                log.debug(f"RPC error {method}: {data['error']}")
                return None
            return data.get("result")
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                log.debug(f"RPC {method} fallito: {e}")
    return None


def _helius_parse(signatures: list[str]) -> list[dict]:
    """Usa Helius Enhanced Transactions API per parsare tx raw in eventi leggibili."""
    if not HELIUS_API_KEY or not signatures:
        return []
    url = f"https://api.helius.xyz/v0/transactions/?api-key={HELIUS_API_KEY}"
    results = []
    for i in range(0, len(signatures), HELIUS_BATCH_SIZE):
        batch = signatures[i:i + HELIUS_BATCH_SIZE]
        try:
            r = requests.post(url, json={"transactions": batch}, timeout=20)
            r.raise_for_status()
            results.extend(r.json())
            time.sleep(0.3)
        except Exception as e:
            log.debug(f"Helius parse batch {i}: {e}")
    return results


def _helius_wallet_history(wallet: str) -> list[dict]:
    """Recupera la history swap di un wallet via Helius."""
    if not HELIUS_API_KEY:
        return []
    url = f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
    params = {"api-key": HELIUS_API_KEY, "type": "SWAP", "limit": str(WALLET_HISTORY_LIMIT)}
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.debug(f"Helius wallet history {wallet[:8]}: {e}")
        return []

# ---------------------------------------------------------------------------
# Phase 1 — carica token seed da CSV esistenti
# ---------------------------------------------------------------------------

def load_winning_signal_ids() -> Optional[set]:
    """
    Da live_trades.csv: signal_id con esito vincente.
    Win = almeno una riga tp1/tp2, oppure exit con change_pct > 0.
    Ritorna None se il CSV manca (→ nessun filtro applicabile).
    """
    if not LIVE_TRADES_CSV.exists():
        return None
    wins: set = set()
    try:
        with open(LIVE_TRADES_CSV) as f:
            for row in csv.DictReader(f):
                sid    = (row.get("signal_id") or "").strip()
                action = (row.get("action") or "").strip()
                if not sid:
                    continue
                if action in ("tp1", "tp2"):
                    wins.add(sid)
                    continue
                if action == "entry":
                    continue
                try:
                    chg = float((row.get("change_pct") or "0").replace("+", ""))
                except Exception:
                    chg = 0.0
                if chg > 0:
                    wins.add(sid)
    except Exception as e:
        log.warning(f"live_trades.csv non leggibile ({e}) — filtro win disattivato")
        return None
    return wins


def _mint_from_pair(pair_address: str) -> str:
    """Risolve il mint (baseToken) di una pair Solana via DexScreener."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}",
            timeout=8)
        r.raise_for_status()
        data  = r.json()
        pairs = data.get("pairs") or ([data.get("pair")] if data.get("pair") else [])
        if pairs:
            return ((pairs[0].get("baseToken") or {}).get("address") or "").strip()
    except Exception as e:
        log.debug(f"mint da pair {pair_address[:8]}…: {e}")
    return ""


def load_seed_tokens(winners_only: bool = True) -> list[dict]:
    """
    Carica i token seed da:
      1. pump_grad_signals.csv (ha già mint + pair, ma viene ruotato spesso)
      2. live_trades.csv — righe entry pump_grad/mirror con pair_address;
         il mint viene risolto via DexScreener (1 call/seed)
    Se winners_only: tiene solo i token il cui trade ha avuto esito vincente
    (live_trades.csv) — altrimenti un wallet prende score "alpha" anche
    comprando early token poi ruggati.
    """
    win_ids = load_winning_signal_ids() if winners_only else None
    if winners_only and win_ids is None:
        log.warning("live_trades.csv assente — uso TUTTI i seed (anche loss)")

    tokens = []
    seen_mints = set()
    seen_pairs = set()
    skipped_loss = 0

    if PUMP_GRAD_CSV.exists():
        with open(PUMP_GRAD_CSV) as f:
            for row in csv.DictReader(f):
                mint  = row.get("token_address", "").strip()
                pair  = row.get("pair_address", "").strip()
                ts_str = row.get("timestamp_entry", "")
                sym   = row.get("token_symbol", "?")
                sid   = (row.get("signal_id") or "").strip()
                if not mint or not pair or mint in seen_mints:
                    continue
                if mint in STABLECOIN_MINTS:
                    continue
                if win_ids is not None and sid not in win_ids:
                    skipped_loss += 1
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str).timestamp()
                except Exception:
                    ts = 0.0
                tokens.append({"mint": mint, "pair": pair, "ts": ts, "symbol": sym, "source": "pump_grad"})
                seen_mints.add(mint)
                seen_pairs.add(pair)

    # Seed aggiuntivi da live_trades.csv (storico più lungo del CSV segnali)
    pending = []   # entry win senza mint, da risolvere via DexScreener
    if LIVE_TRADES_CSV.exists():
        with open(LIVE_TRADES_CSV) as f:
            for row in csv.DictReader(f):
                if (row.get("system") or "") not in ("pump_grad", "mirror"):
                    continue
                if (row.get("action") or "") != "entry":
                    continue
                if (row.get("chain") or "") != "solana":
                    continue
                pair = (row.get("pair_address") or "").strip()
                sid  = (row.get("signal_id") or "").strip()
                if not pair or pair in seen_pairs:
                    continue
                if win_ids is not None and sid not in win_ids:
                    skipped_loss += 1
                    continue
                try:
                    ts = datetime.fromisoformat((row.get("ts") or "")).timestamp()
                except Exception:
                    ts = 0.0
                pending.append({"pair": pair, "ts": ts, "symbol": row.get("token_symbol", "?")})
                seen_pairs.add(pair)

    for p in pending:
        mint = _mint_from_pair(p["pair"])
        time.sleep(0.3)  # rate limit DexScreener
        if not mint or mint in seen_mints or mint in STABLECOIN_MINTS:
            continue
        tokens.append({"mint": mint, "pair": p["pair"], "ts": p["ts"],
                       "symbol": p["symbol"], "source": "live_trades"})
        seen_mints.add(mint)

    log.info(f"Seed token caricati: {len(tokens)} "
             f"(pump_grad_csv={sum(1 for t in tokens if t['source']=='pump_grad')}, "
             f"live_trades={sum(1 for t in tokens if t['source']=='live_trades')}, "
             f"scartati {skipped_loss} non-win)")
    return tokens

# ---------------------------------------------------------------------------
# Phase 2 — trova early buyers di ogni token
# ---------------------------------------------------------------------------

def get_pool_signatures(pair_address: str, signal_ts: float) -> list[str]:
    """
    Recupera le firme delle transazioni del pool nella finestra early
    [signal_ts, signal_ts + EARLY_WINDOW_MIN].

    getSignaturesForAddress restituisce newest-first: per pool ancora attivi
    la finestra early è indietro nella history → si pagina con `before`
    finché non si raggiunge signal_ts (max MAX_SIG_PAGES pagine).
    """
    cutoff = signal_ts + EARLY_WINDOW_MIN * 60
    sigs: list[str] = []
    before: Optional[str] = None

    for _page in range(MAX_SIG_PAGES):
        params: dict = {"limit": SIGS_PAGE_LIMIT, "commitment": "confirmed"}
        if before:
            params["before"] = before
        result = _rpc("getSignaturesForAddress", [pair_address, params])
        if not result:
            break

        oldest_bt = None
        for item in result:
            block_time = item.get("blockTime", 0) or 0
            if block_time:
                oldest_bt = block_time
            if block_time == 0:
                continue  # senza timestamp non possiamo collocarla nella finestra
            if signal_ts <= block_time <= cutoff:
                sigs.append(item["signature"])

        if len(result) < SIGS_PAGE_LIMIT:
            break  # history esaurita
        if oldest_bt is not None and oldest_bt < signal_ts:
            break  # superata (all'indietro) la finestra early
        before = result[-1]["signature"]
        time.sleep(0.2)

    return sigs


_SOL_PRICE_CACHE = {"price": 0.0, "ts": 0.0}


def _sol_price() -> float:
    """Prezzo SOL via DexScreener (cache 10 min) per valorizzare buy in SOL."""
    now = time.time()
    if now - _SOL_PRICE_CACHE["ts"] < 600 and _SOL_PRICE_CACHE["price"] > 0:
        return _SOL_PRICE_CACHE["price"]
    try:
        r = requests.get(
            "https://api.dexscreener.com/tokens/v1/solana/So11111111111111111111111111111111111111112",
            timeout=8)
        r.raise_for_status()
        pairs = r.json()
        pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)
        price = float(pairs[0].get("priceUsd") or 0)
        if price > 0:
            _SOL_PRICE_CACHE.update(price=price, ts=now)
            return price
    except Exception:
        pass
    return _SOL_PRICE_CACHE["price"] or 150.0


def extract_buyers_from_parsed(parsed_txns: list[dict], pool_address: str) -> list[dict]:
    """
    Da transazioni Helius parsed, estrae wallet che hanno fatto BUY sul pool.
    Restituisce [{wallet, amount_usd, block_time}]

    Valore speso = USDC out + wSOL out + SOL nativo out (× prezzo SOL).
    La versione precedente contava solo USDC → perdeva i buy in SOL,
    cioè la maggioranza sui memecoin Solana.
    """
    wsol = "So11111111111111111111111111111111111111112"
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    sol_px = _sol_price()

    buyers = []
    for tx in parsed_txns:
        if not tx or tx.get("transactionError"):
            continue

        tx_type = tx.get("type", "")
        if tx_type not in ("SWAP", ""):
            continue

        block_time = tx.get("timestamp", 0)
        fee_payer  = tx.get("feePayer", "")
        if not fee_payer:
            continue

        usd_out  = 0.0
        sol_out  = 0.0
        non_stable_in = False

        for transfer in tx.get("tokenTransfers", []) or []:
            mint = transfer.get("mint", "")
            from_acc = transfer.get("fromUserAccount", "")
            to_acc   = transfer.get("toUserAccount", "")
            amount   = float(transfer.get("tokenAmount", 0) or 0)

            if from_acc == fee_payer:
                if mint == usdc:
                    usd_out += amount
                elif mint == wsol:
                    sol_out += amount
            elif to_acc == fee_payer and mint and mint not in STABLECOIN_MINTS:
                non_stable_in = True

        # SOL nativo in uscita (lamports) — i buy pump.fun/Raydium passano da qui
        for nt in tx.get("nativeTransfers", []) or []:
            if nt.get("fromUserAccount") == fee_payer:
                sol_out += float(nt.get("amount", 0) or 0) / 1e9

        if sol_out > 0.001:   # sopra le fee/rent tipiche
            usd_out += sol_out * sol_px

        if usd_out > 0 and non_stable_in:
            buyers.append({
                "wallet":     fee_payer,
                "amount_usd": usd_out,
                "block_time": block_time,
            })

    return buyers


def find_early_buyers(token: dict) -> list[dict]:
    """Wrapper: trova early buyers per un singolo token."""
    pair = token["pair"]
    ts   = token["ts"]
    sym  = token["symbol"]

    if not pair:
        return []

    sigs = get_pool_signatures(pair, ts)
    if not sigs:
        log.debug(f"  {sym}: nessuna firma trovata sul pool")
        return []

    parsed = _helius_parse(sigs)
    buyers = extract_buyers_from_parsed(parsed, pair)

    log.debug(f"  {sym}: {len(sigs)} sigs → {len(parsed)} parsed → {len(buyers)} buyer")
    return buyers

# ---------------------------------------------------------------------------
# Phase 3 — aggrega e score wallet
# ---------------------------------------------------------------------------

def aggregate_wallet_stats(token_buyers: dict[str, list[dict]]) -> dict[str, dict]:
    """
    Aggrega le apparizioni di ogni wallet su più token.
    token_buyers: {token_mint: [{wallet, amount_usd, block_time}]}
    """
    wallet_stats: dict[str, dict] = defaultdict(lambda: {
        "tokens_early":    [],     # lista di {mint, symbol, amount_usd, rank}
        "total_usd_deployed": 0.0,
        "avg_rank":        0.0,    # posizione media nella coda acquisti (1=primo)
    })

    for mint, buyers in token_buyers.items():
        # Dedup per wallet: tieni il primo buy, somma le size dei successivi
        # (prima ogni buy contava come "token early" separato → score gonfiato
        # per i bot che spammano acquisti nella finestra)
        first_buy: dict[str, dict] = {}
        for b in sorted(buyers, key=lambda x: x["block_time"]):
            w = b["wallet"]
            if w in first_buy:
                first_buy[w]["amount_usd"] += b["amount_usd"]
            else:
                first_buy[w] = dict(b)

        ordered = sorted(first_buy.values(), key=lambda x: x["block_time"])
        for rank, buyer in enumerate(ordered, start=1):
            w = buyer["wallet"]
            wallet_stats[w]["tokens_early"].append({
                "mint":       mint,
                "amount_usd": buyer["amount_usd"],
                "rank":       rank,
                "block_time": buyer["block_time"],
            })
            wallet_stats[w]["total_usd_deployed"] += buyer["amount_usd"]

    # Calcola avg_rank per ogni wallet
    for w, stats in wallet_stats.items():
        ranks = [t["rank"] for t in stats["tokens_early"]]
        stats["avg_rank"] = sum(ranks) / len(ranks) if ranks else 999

    return dict(wallet_stats)


def enrich_with_history(wallet: str, stats: dict) -> dict:
    """
    Aggiunge dati dalla history swap del wallet via Helius.
    Stima win rate basandosi sui token comprati di recente.
    """
    history = _helius_wallet_history(wallet)
    if not history:
        return stats

    swaps_analyzed = 0
    unique_tokens_bought = set()

    for tx in history:
        if not tx or tx.get("transactionError"):
            continue
        for transfer in tx.get("tokenTransfers", []):
            mint = transfer.get("mint", "")
            to   = transfer.get("toUserAccount", "")
            if to == wallet and mint not in STABLECOIN_MINTS and mint:
                unique_tokens_bought.add(mint)
        swaps_analyzed += 1

    stats["total_swaps_analyzed"] = swaps_analyzed
    stats["unique_tokens_traded"]  = len(unique_tokens_bought)

    # Ultimo trade per capire se il wallet è ancora attivo
    last_tx = history[0] if history else {}
    stats["last_active_ts"] = last_tx.get("timestamp", 0)
    if stats["last_active_ts"]:
        days_ago = (time.time() - stats["last_active_ts"]) / 86400
        stats["days_since_last_trade"] = round(days_ago, 1)
    else:
        stats["days_since_last_trade"] = 999

    return stats


def compute_score(stats: dict) -> float:
    """
    Score composito per ranking finale.

    Fattori:
      - n_tokens_early:  quanti token pompati ha preso early (peso massimo)
      - avg_rank:        posizione media nella coda (1 = meglio)
      - recency:         wallet attivo di recente
      - size filter:     escludi wallet con < $1 o > $50k per trade (bot/whale non copiabili)
    """
    n_tokens = len(stats.get("tokens_early", []))
    avg_rank = stats.get("avg_rank", 999)
    days_ago = stats.get("days_since_last_trade", 999)
    avg_usd  = stats.get("total_usd_deployed", 0) / max(n_tokens, 1)

    # Filtri hard
    if n_tokens < 1:
        return 0.0
    if avg_usd < 1 or avg_usd > 50_000:
        return 0.0

    # Score base: numero token early (più è alto meglio)
    score = n_tokens * 10.0

    # Bonus earliness: rank 1 = +5, rank 5 = +1
    rank_bonus = max(0, 6 - avg_rank)
    score += rank_bonus * 2

    # Penalità "non davvero early": rank medio >300 nella finestra di 10 min
    # = partecipante generico/bot spray, non smart money selettiva
    if avg_rank > 300:
        score *= 0.5
    elif avg_rank > 100:
        score *= 0.8

    # Penalità inattività (ramo >60 PRIMA di >30, altrimenti irraggiungibile)
    if days_ago < 7:
        score *= 1.2
    elif days_ago > 60:
        score *= 0.3
    elif days_ago > 30:
        score *= 0.6

    return round(score, 2)

# ---------------------------------------------------------------------------
# Phase 4 — output
# ---------------------------------------------------------------------------

def build_report(ranked: list[dict]) -> None:
    """Stampa un report leggibile e salva alpha_wallets.json."""
    print("\n" + "=" * 70)
    print(f"  ALPHA WALLETS — top {len(ranked)}")
    print("=" * 70)
    print(f"{'#':<4} {'Wallet':<46} {'Score':>6} {'Tokens':>7} {'AvgRank':>8} {'Last':>6}")
    print("-" * 70)

    for i, entry in enumerate(ranked[:50], start=1):
        w      = entry["wallet"]
        score  = entry["score"]
        n      = len(entry["tokens_early"])
        rank   = entry.get("avg_rank", 0)
        days   = entry.get("days_since_last_trade", "?")
        days_s = f"{days}d" if isinstance(days, (int, float)) else "?"
        print(f"{i:<4} {w:<46} {score:>6.1f} {n:>7} {rank:>8.1f} {days_s:>6}")

    # Salva JSON completo
    output = []
    for entry in ranked:
        output.append({
            "wallet":               entry["wallet"],
            "score":                entry["score"],
            "tokens_early_count":   len(entry["tokens_early"]),
            "avg_rank":             round(entry.get("avg_rank", 0), 1),
            "total_usd_deployed":   round(entry.get("total_usd_deployed", 0), 2),
            "days_since_last_trade": entry.get("days_since_last_trade", 999),
            "unique_tokens_traded": entry.get("unique_tokens_traded", 0),
            "tokens_detail":        entry["tokens_early"][:10],  # max 10 per brevità
        })

    OUTPUT_FILE.write_text(json.dumps(output, indent=2))
    print(f"\nSalvato: {OUTPUT_FILE}  ({len(output)} wallet)")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(min_tokens: int = 2, top: int = 50, enrich: bool = True, winners_only: bool = True):
    if not HELIUS_API_KEY:
        log.error("HELIUS_API_KEY mancante in .env — necessario per parsare le tx")
        log.error("Ottieni una chiave gratis su https://www.helius.dev (free: 1M req/mese)")
        return

    log.info("=== WALLET ALPHA FINDER ===")

    # 1. Carica token seed
    tokens = load_seed_tokens(winners_only=winners_only)
    if not tokens:
        log.error("Nessun token seed trovato. Verifica i path dei CSV.")
        return

    # 2. Per ogni token trova early buyers
    token_buyers: dict[str, list[dict]] = {}
    for i, token in enumerate(tokens, 1):
        sym  = token["symbol"]
        mint = token["mint"]
        log.info(f"[{i}/{len(tokens)}] Analizzo {sym} ({mint[:8]}...)")

        if not token["pair"]:
            log.debug(f"  {sym}: pair_address mancante, skip")
            continue

        buyers = find_early_buyers(token)
        if buyers:
            token_buyers[mint] = buyers
            log.info(f"  → {len(buyers)} early buyer trovati")
        else:
            log.info(f"  → nessun buyer trovato (Helius non disponibile o pool vuoto)")

        time.sleep(0.5)  # rate limit

    if not token_buyers:
        log.warning("Nessun buyer trovato. Verifica HELIUS_API_KEY e la connessione.")
        return

    # 3. Aggrega stats per wallet
    log.info("Aggregazione wallet stats...")
    wallet_stats = aggregate_wallet_stats(token_buyers)

    # 4. Filtra: solo wallet presenti su >= min_tokens
    candidates = {
        w: s for w, s in wallet_stats.items()
        if len(s["tokens_early"]) >= min_tokens
    }
    log.info(f"Wallet con >= {min_tokens} token early: {len(candidates)}")

    # 5. Enrich con history Helius (opzionale, costoso in req)
    # Ordina per score preliminare PRIMA di arricchire: l'ordine di inserzione
    # del dict era arbitrario → i top reali restavano senza history/recency
    if enrich and candidates:
        prelim = sorted(candidates.items(),
                        key=lambda kv: len(kv[1]["tokens_early"]), reverse=True)
        log.info(f"Arricchimento history per {min(len(prelim), top*2)} wallet...")
        for j, (w, stats) in enumerate(prelim[:top * 2], 1):
            log.info(f"  [{j}] Wallet {w[:8]}...")
            candidates[w] = enrich_with_history(w, stats)
            time.sleep(0.3)

    # 6. Calcola score e ordina
    scored = []
    for w, stats in candidates.items():
        score = compute_score(stats)
        if score > 0:
            scored.append({"wallet": w, "score": score, **stats})

    ranked = sorted(scored, key=lambda x: x["score"], reverse=True)

    # 7. Report
    if ranked:
        build_report(ranked[:top])
    else:
        log.warning("Nessun wallet qualificato trovato. Prova con --min-tokens 1")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Trova wallet alpha su Solana")
    parser.add_argument("--min-tokens", type=int, default=2,
                        help="Minimo token early per qualificarsi (default: 2)")
    parser.add_argument("--top",        type=int, default=50,
                        help="Quanti wallet analizzare in depth (default: 50)")
    parser.add_argument("--no-enrich",  action="store_true",
                        help="Salta l'enrichment history (più veloce ma meno dati)")
    parser.add_argument("--all-seeds",  action="store_true",
                        help="Usa tutti i token seed, anche quelli con esito loss")
    args = parser.parse_args()

    main(
        min_tokens=args.min_tokens,
        top=args.top,
        enrich=not args.no_enrich,
        winners_only=not args.all_seeds,
    )
