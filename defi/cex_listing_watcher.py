"""
cex_listing_watcher.py — Monitora nuovi listing CEX (Binance, Coinbase).
Ogni 2min: se nuova coppia trovata e tradabile on-chain → email queue + CSV segnale.
Avviato da run.py (--no-cex per skippare).
"""
import csv
import logging
import re
import sys
import threading
import time
from datetime import datetime
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

log = logging.getLogger("cex_watcher")

POLL_SEC     = 120
SEEN_TTL_SEC = 86400   # listing rimane "noto" per 24h
MIN_LIQ_DEX  = 5_000

# Ticker CEX "di base" da ignorare — Coinbase li ha tutti ma non sono nuovi listing
_COINBASE_BASELINE = {
    "BTC","ETH","USDC","USDT","SOL","XRP","ADA","MATIC","LINK","DOT","DOGE","AVAX",
    "LTC","BCH","UNI","ATOM","ALGO","XLM","SHIB","TRX","ETC","NEAR","APT","ARB","OP",
    "PEPE","WIF","BONK","SUI","SEI","INJ","TIA","FTM","AAVE","CRV","COMP","MKR","SNX"
}

_REPORTS = _HERE / "reports"
_CSV_OUT  = _REPORTS / "cex_listing_signals.csv"
_seen: dict[str, float] = {}


def _purge_seen():
    cutoff = time.time() - SEEN_TTL_SEC
    for k in list(_seen.keys()):
        if _seen[k] < cutoff:
            del _seen[k]


def _fetch_binance_listings() -> list[tuple[str, str]]:
    """Restituisce [(ticker, titolo)] da annunci Binance delle ultime 48h."""
    try:
        r = requests.post(
            "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query",
            json={"type": 1, "pageNo": 1, "pageSize": 20},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        r.raise_for_status()
        articles = r.json().get("data", {}).get("articles", [])
        results = []
        for art in articles:
            title  = art.get("title", "")
            ts_ms  = int(art.get("releaseDate", 0) or 0)
            if ts_ms and (time.time() - ts_ms / 1000) > 48 * 3600:
                continue
            if "New Listing" not in title and "Will List" not in title:
                continue
            for m in re.findall(r'\(([A-Z]{2,12})\)', title):
                results.append((m, title))
            for m in re.findall(r'Will List ([A-Z]{2,12})', title):
                results.append((m, title))
        return results
    except Exception as e:
        log.debug(f"[cex] binance: {e}")
        return []


def _fetch_coinbase_new() -> list[tuple[str, str]]:
    """Tickers Coinbase non in baseline → potenzialmente nuovi listing."""
    try:
        r = requests.get("https://api.exchange.coinbase.com/products", timeout=10)
        r.raise_for_status()
        out = []
        for p in r.json():
            if p.get("status") != "online":
                continue
            base = p.get("base_currency", "")
            if base and base not in _COINBASE_BASELINE:
                out.append((base, f"Coinbase product {base}"))
        return out
    except Exception as e:
        log.debug(f"[cex] coinbase: {e}")
        return []


def _search_dexscreener(ticker: str) -> list[dict]:
    """Cerca ticker su DexScreener, ritorna pair solana/base con liq>MIN."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/search/?q={ticker}",
            timeout=10,
        )
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
        results = []
        for p in pairs:
            if p.get("chainId") not in ("solana", "base"):
                continue
            liq = float((p.get("liquidity") or {}).get("usd", 0) or 0)
            if liq < MIN_LIQ_DEX:
                continue
            if p.get("baseToken", {}).get("symbol", "").upper() != ticker.upper():
                continue
            results.append(p)
        return sorted(results, key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)
    except Exception as e:
        log.debug(f"[cex] dexscreener {ticker}: {e}")
        return []


def _queue_email(ticker: str, source: str, title: str, pairs: list[dict]):
    try:
        import email_digest
        top   = pairs[0]
        liq   = float((top.get("liquidity") or {}).get("usd", 0) or 0)
        chain = top.get("chainId", "?")
        addr  = top.get("pairAddress", "")
        price = top.get("priceUsd", "?")
        subj  = f"[CEX LISTING] {ticker} su {source} — trovato {chain.upper()} ${liq:,.0f} liq"
        body  = (
            f"<b>Nuovo listing CEX rilevato!</b><br>"
            f"Ticker: <b>${ticker}</b><br>"
            f"Exchange: {source}<br>"
            f"Annuncio: {title[:120]}<br><br>"
            f"Trovato on-chain: {chain} — ${liq:,.0f} liq | Prezzo: ${price}<br>"
            f"<a href='https://dexscreener.com/{chain}/{addr}'>DexScreener</a><br>"
            f"<br><i>Entry midcap consigliata se liq &gt; $25k e non già pompato (&lt;+20% 1h).</i>"
        )
        email_digest.queue_email("cex_watcher", subj, body)
        log.info(f"[cex] ★ {ticker} ({source}) → {chain} ${liq:,.0f} → email queued")
    except Exception as e:
        log.warning(f"[cex] queue_email: {e}")


def _append_csv(ticker: str, source: str, pairs: list[dict]):
    top   = pairs[0]
    liq   = float((top.get("liquidity") or {}).get("usd", 0) or 0)
    chain = top.get("chainId", "?")
    row   = {
        "ts":            datetime.now().isoformat(),
        "ticker":        ticker,
        "cex_source":    source,
        "chain":         chain,
        "pair_address":  top.get("pairAddress", ""),
        "liquidity_usd": f"{liq:.0f}",
        "price_usd":     top.get("priceUsd", ""),
        "n_dex_pairs":   len(pairs),
    }
    _REPORTS.mkdir(parents=True, exist_ok=True)
    new_file = not _CSV_OUT.exists()
    with open(_CSV_OUT, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new_file:
            w.writeheader()
        w.writerow(row)


def _tick():
    _purge_seen()
    now = time.time()
    candidates: list[tuple[str, str, str]] = []  # (ticker, source, title)

    for ticker, title in _fetch_binance_listings():
        candidates.append((ticker, "Binance", title))
    for ticker, title in _fetch_coinbase_new():
        if ticker not in _seen:
            candidates.append((ticker, "Coinbase", title))

    for ticker, source, title in candidates:
        if ticker in _seen:
            continue
        _seen[ticker] = now
        pairs = _search_dexscreener(ticker)
        if not pairs:
            log.debug(f"[cex] {ticker} ({source}) non trovato on-chain")
            continue
        _append_csv(ticker, source, pairs)
        _queue_email(ticker, source, title, pairs)


def _bootstrap():
    """Primo avvio: popola _seen con tutti i prodotti Coinbase esistenti
    senza emettere segnali — evita lo spam del bootstrap iniziale."""
    now = time.time()
    try:
        r = requests.get("https://api.exchange.coinbase.com/products", timeout=10)
        r.raise_for_status()
        count = 0
        for p in r.json():
            base = p.get("base_currency", "")
            if base and base not in _seen:
                _seen[base] = now
                count += 1
        log.info(f"[cex] bootstrap: {count} ticker Coinbase esistenti marcati come visti (no email)")
    except Exception as e:
        log.warning(f"[cex] bootstrap error: {e}")


def main(stop_event: threading.Event | None = None):
    log.info(f"[cex] ▶ avviato (poll {POLL_SEC}s, liq>${MIN_LIQ_DEX:,})")
    _bootstrap()   # popola _seen senza segnali al primo avvio
    while True:
        if stop_event and stop_event.is_set():
            break
        try:
            _tick()
        except Exception as e:
            log.warning(f"[cex] tick error: {e}")
        if stop_event:
            stop_event.wait(POLL_SEC)
        else:
            time.sleep(POLL_SEC)
    log.info("[cex] ■ fermato")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    main()
