"""
wallet_intel.py — wallet_confluence_score: aggrega le informazioni già raccolte
da wallet_mirror_bot (wallet_events.csv) e wallet_alpha_finder (alpha_wallets.json)
in un'unica metrica 0-1 da usare come BOOST informativo (non filtro hard) nello
scoring di defi_optimized (prepump_composite_score) e gemmeV3 (score_gem).

Finora queste informazioni erano completamente separate dal processo decisionale
principale: wallet_mirror_bot apriva posizioni proprie ma defi_optimized/gemmeV3
non ne leggevano l'output (a parte _smart_money_count, usato SOLO come annotazione
nel note dell'entry, mai nello score).
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parent          # defi/
_WALLET_EVENTS_CSV  = _BASE / "reports" / "wallet_events.csv"
_ALPHA_WALLETS_JSON = _BASE.parent / "executor" / "alpha_wallets.json"

# Finestra di osservazione eventi: coerente con _SM_WINDOW_H di trade_simulator
# (confluenza/smart-money ha senso solo su attività recente)
_WINDOW_SEC = 6 * 3600

# Cap usati per normalizzare gli score storici di alpha_wallets.json (score
# osservati 0-30 circa nel dataset attuale; 50 lascia margine senza saturare)
_ALPHA_SCORE_CAP = 50.0

# cache alpha wallet scores: wallet -> score normalizzato 0-1 (invalidata su mtime)
_alpha_cache = {"mtime": 0.0, "scores": {}}

# cache eventi: mint -> lista eventi nella finestra (ricostruita ogni 60s)
_events_cache = {"ts": 0.0, "by_mint": {}}


def _load_alpha_scores() -> dict:
    """wallet -> score storico normalizzato [0,1] da alpha_wallets.json."""
    try:
        mtime = _ALPHA_WALLETS_JSON.stat().st_mtime
    except OSError:
        return {}
    if mtime == _alpha_cache["mtime"]:
        return _alpha_cache["scores"]
    scores = {}
    try:
        data = json.loads(_ALPHA_WALLETS_JSON.read_text())
        for w in data:
            wallet = w.get("wallet")
            if not wallet:
                continue
            raw = float(w.get("score", 0) or 0)
            scores[wallet] = max(0.0, min(1.0, raw / _ALPHA_SCORE_CAP))
    except Exception:
        scores = {}
    _alpha_cache["mtime"] = mtime
    _alpha_cache["scores"] = scores
    return scores


def _load_events_by_mint(now_ts: float) -> dict:
    """mint -> lista di eventi {ts, wallet, side, usd, confluence, wake_days} nella finestra."""
    if now_ts - _events_cache["ts"] < 60:
        return _events_cache["by_mint"]
    by_mint: dict = {}
    try:
        if _WALLET_EVENTS_CSV.exists():
            size = _WALLET_EVENTS_CSV.stat().st_size
            with open(_WALLET_EVENTS_CSV, "r", encoding="utf-8", errors="replace") as f:
                if size > 500_000:
                    f.seek(size - 500_000)
                    f.readline()  # scarta riga troncata
                cutoff = now_ts - _WINDOW_SEC
                for line in f:
                    # colonne: ts,wallet,side,mint,usd,confluence,wake_days,note
                    parts = line.rstrip("\n").split(",")
                    if len(parts) < 7:
                        continue
                    ts_s, wallet, side, mint, usd_s, conf_s, wake_s = parts[:7]
                    try:
                        ev_ts = datetime.fromisoformat(ts_s).timestamp()
                    except Exception:
                        continue
                    if ev_ts < cutoff:
                        continue
                    try:
                        usd  = float(usd_s or 0)
                        conf = int(float(conf_s or 0))
                        wake = float(wake_s or 0)
                    except ValueError:
                        usd, conf, wake = 0.0, 0, 0.0
                    by_mint.setdefault(mint, []).append({
                        "ts": ev_ts, "wallet": wallet, "side": side,
                        "usd": usd, "confluence": conf, "wake_days": wake,
                    })
    except Exception:
        by_mint = {}
    _events_cache["ts"] = now_ts
    _events_cache["by_mint"] = by_mint
    return by_mint


def wallet_confluence_score(token_address: str, now_ts: float | None = None) -> float:
    """
    Punteggio 0-1 di "confluenza wallet alpha" su questo token, derivato da
    wallet_events.csv (wallet_mirror_bot) + alpha_wallets.json (score storico
    di wallet_alpha_finder). Ritorna 0.0 se non c'è nessuna attività alpha nota
    sul token (caso più comune): nessun rischio di penalizzare segnali "normali".

    Premia:
      - più wallet alpha distinti comprano lo stesso token (confluenza)
      - acquisti ravvicinati nel tempo (entro 30min = conferma forte)
      - wallet storicamente profittevoli (score alpha_wallets.json)
      - whale awakening: wallet dormiente che torna a comprare (wake_days alto)

    Penalizza (moltiplicativo):
      - presenza di sell alpha sullo stesso token nella finestra (segnale
        contraddittorio: smart money in uscita mentre il resto entra)
      - acquisto isolato (1 solo wallet, nessuna conferma) — comunque > 0,
        ma pesato in modo conservativo

    NON è un filtro hard: usato come boost additivo in prepump_composite_score
    (defi_optimized) e score_gem (gemmeV3).
    """
    if not token_address:
        return 0.0
    now_ts = now_ts if now_ts is not None else time.time()
    events = _load_events_by_mint(now_ts).get(token_address, [])
    if not events:
        return 0.0

    buys  = [e for e in events if e["side"] == "buy"]
    sells = [e for e in events if e["side"] == "sell"]
    if not buys:
        return 0.0

    alpha_scores = _load_alpha_scores()

    # 1. Confluenza: numero di wallet alpha distinti che comprano (cap 4 → 1.0)
    n_wallets = len({e["wallet"] for e in buys})
    confluence_term = min(1.0, n_wallets / 4.0)

    # 2. Vicinanza temporale dei buy: entro 30min = conferma forte.
    #    Con un solo buy non c'è conferma temporale → termine neutro-basso.
    ts_list = sorted(e["ts"] for e in buys)
    if len(ts_list) >= 2:
        span_min = (ts_list[-1] - ts_list[0]) / 60.0
        recency_term = max(0.0, 1.0 - span_min / 30.0)
    else:
        recency_term = 0.3

    # 3. Qualità storica dei wallet coinvolti (media degli score alpha normalizzati)
    quality_vals = [alpha_scores.get(e["wallet"], 0.0) for e in buys]
    quality_term = sum(quality_vals) / len(quality_vals) if quality_vals else 0.0

    # 4. Whale awakening: wallet dormiente (wake_days popolato solo se >= soglia
    #    inattività nel mirror bot) che ricompra = forte segnale di convinzione.
    wake_term = min(1.0, max((e["wake_days"] for e in buys), default=0.0) / 30.0)

    score = (
        confluence_term * 0.40
        + recency_term   * 0.20
        + quality_term   * 0.20
        + wake_term      * 0.20
    )

    # Penalità: smart money in uscita sullo stesso token → segnale contraddittorio
    if sells:
        score *= 0.5

    # Penalità: acquisto isolato, nessuna conferma da altri wallet
    if n_wallets == 1 and len(buys) == 1:
        score *= 0.6

    return round(max(0.0, min(1.0, score)), 4)
