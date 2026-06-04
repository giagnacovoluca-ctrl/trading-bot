"""
==============================================================================
gem_watchlist.py — Bridge condiviso tra gemmeV2 e defi_optimized
==============================================================================

Scopo:
  gemmeV2 trova una gemma (medio termine) → la scrive qui
  defi_optimized la legge → la prioritizza nel loop intraday

Flusso:
  [gemmeV2]  stampa_gemma()  ──→  write_gem_to_watchlist()  ──→  watchlist.json
  [defi_opt] ogni N cicli    ←──  load_watchlist()          ←──  watchlist.json
  [defi_opt] fetch_dexscreener_pairs() → boost score per token in watchlist

Design:
  • File JSON atomico (scrittura su .tmp → rename): nessun dato corrotto
  • TTL configurabile (default 12h): le gemme scadono automaticamente
  • Thread-safe: filelock su piattaforme Unix/Win; fallback a lock Python
  • Zero dipendenze circolari: questo modulo non importa né gemmeV2 né defi
==============================================================================
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Cross-process file lock (filelock) ──────────────────────────────────────
# threading.Lock() protegge thread nello stesso processo.
# FileLock protegge processi distinti (gemmeV2 + defi_optimized) che accedono
# allo stesso file JSON. I due lock si annidano: threading.Lock() esterno,
# FileLock interno — così più thread non si accumulano sul lock OS.
try:
    from filelock import FileLock as _FileLock

    def _process_lock() -> contextlib.AbstractContextManager:
        """Lock OS-level per accesso cross-processo al file JSON."""
        lock_path = str(WATCHLIST_PATH.with_suffix(".json.lock"))
        return _FileLock(lock_path, timeout=15)

    log.debug("[watchlist] filelock disponibile — lock inter-processo attivo.")
    _FILELOCK_AVAILABLE = True
except ImportError:
    def _process_lock() -> contextlib.AbstractContextManager:
        """Fallback no-op quando filelock non è installato."""
        return contextlib.nullcontext()

    _FILELOCK_AVAILABLE = False
    log.warning(
        "[watchlist] ⚠️  filelock non installato — lock inter-processo disabilitato. "
        "Installa con: pip install filelock"
    )

# ==============================================================================
# CONFIGURAZIONE
# ==============================================================================

# Path del file watchlist — ASSOLUTO rispetto alla posizione di questo modulo.
# Così gemmeV2/V3 (in gemme/) e defi_optimized (in defi/) leggono/scrivono
# sempre lo STESSO file, indipendentemente dalla CWD di lancio.
# Sovrascrivibile via variabile d'ambiente GEM_WATCHLIST_PATH.
WATCHLIST_PATH = Path(
    os.environ.get("GEM_WATCHLIST_PATH", "")
) if os.environ.get("GEM_WATCHLIST_PATH") else Path(__file__).parent / "gem_watchlist.json"

# Quante ore una gemma rimane in watchlist dopo la scoperta.
# 12h = abbastanza da coprire diversi cicli intraday, ma non troppo da inquinare
# il segnale con gemme già "metabolizzate" dal mercato.
WATCHLIST_TTL_HOURS = float(os.environ.get("WATCHLIST_TTL_HOURS", "12"))

# Boost di priorità assegnato ai token in watchlist nello score DexScreener.
# 300 supera qualsiasi altro segnale (boost/profilo) per mettere il token in cima.
WATCHLIST_PRIORITY_BOOST = 300.0

# ==============================================================================
# LOCK THREAD-SAFE
# ==============================================================================

_file_lock = threading.Lock()   # protegge letture/scritture concorrenti in-process


# ==============================================================================
# API PUBBLICA
# ==============================================================================

def write_gem_to_watchlist(gem: dict, ttl_hours: float = WATCHLIST_TTL_HOURS) -> bool:
    """
    Aggiunge (o aggiorna) una gemma alla watchlist condivisa.

    Chiamato da gemmeV2 ogni volta che viene trovata una gemma.
    Se il token è già in watchlist (stesso pair_address), aggiorna i metadati
    e prolunga il TTL — non crea duplicati.

    Parametri:
        gem       : dizionario profilo gemma prodotto da gemmeV2
        ttl_hours : durata in ore prima che la entry scada (default: 12h)

    Ritorna True se la scrittura è riuscita, False in caso di errore.
    """
    token_address = gem.get("token_address", "").strip().lower()
    pair_address  = gem.get("pair_address",  "").strip().lower()
    chain         = gem.get("chain", "").strip().lower()

    if not token_address or not chain:
        log.warning("[watchlist] write: token_address o chain mancante — skip.")
        return False

    entry = {
        "token_address":     token_address,
        "token_symbol":      gem.get("token_symbol", ""),
        "token_name":        gem.get("token_name", ""),
        "chain":             chain,
        "pair_address":      pair_address,
        # Metriche originali gemmeV2 (utili per il log in defi_optimized)
        "gem_probability":   round(float(gem.get("gem_probability", 0)), 4),
        "inflow_usd":        float(gem.get("inflow_usd", 0)),
        "inflow_wallet_count": int(gem.get("inflow_wallet_count", 0)),
        "avg_wallet_pnl_pct":  float(gem.get("avg_wallet_pnl_pct", 0)),
        "social_score":      float(gem.get("social_score", 0)),
        "market_cap_usd":    float(gem.get("market_cap_usd", 0)),
        "gem_class":         gem.get("gem_class", "NEUTRAL"),
        # Timestamps
        "added_at":          datetime.now().isoformat(),
        "expires_at":        (datetime.now() + timedelta(hours=ttl_hours)).isoformat(),
    }

    with _file_lock:
        with _process_lock():
            data = _read_raw()
            gems = data.get("gems", [])

        # Aggiorna entry esistente se stessa pair_address (o token_address se pair mancante)
        key = pair_address or token_address
        updated = False
        for i, g in enumerate(gems):
            existing_key = g.get("pair_address") or g.get("token_address", "")
            if existing_key.lower() == key:
                gems[i] = entry
                updated = True
                break

        if not updated:
            gems.append(entry)

        data["gems"] = gems
        data["last_updated"] = datetime.now().isoformat()
        ok = _write_raw(data)

    sym = gem.get("token_symbol", token_address[:8])
    if ok:
        verb = "aggiornata" if updated else "aggiunta"
        log.info(
            f"[watchlist] ✅ {sym} ({chain.upper()}) {verb} — "
            f"P={entry['gem_probability']:.1%} | "
            f"scade: {entry['expires_at'][:16]}"
        )
    else:
        log.warning(f"[watchlist] ⚠️  Scrittura fallita per {sym}.")
    return ok


def load_watchlist(chain: Optional[str] = None) -> list[dict]:
    """
    Carica la watchlist corrente, rimuovendo le entry scadute.

    Chiamato da defi_optimized ogni N cicli.

    Parametri:
        chain : se specificata, ritorna solo le entry per quella chain.
                None = tutte le chain.

    Ritorna lista di dict, ognuno con almeno:
        token_address, chain, pair_address, gem_probability
    """
    with _file_lock:
        with _process_lock():
            data  = _read_raw()
            now   = datetime.now()
            gems  = data.get("gems", [])

            # Rimuovi entry scadute
            valid = []
            expired_count = 0
            for g in gems:
                try:
                    exp = datetime.fromisoformat(g["expires_at"])
                    if exp > now:
                        valid.append(g)
                    else:
                        expired_count += 1
                except (KeyError, ValueError):
                    valid.append(g)   # entry senza expires_at → lascia passare

        if expired_count:
            data["gems"] = valid
            _write_raw(data)
            log.debug(f"[watchlist] Rimosse {expired_count} entry scadute.")

    if chain:
        valid = [g for g in valid if g.get("chain", "").lower() == chain.lower()]

    return valid


def get_watchlist_addresses(chain: Optional[str] = None) -> dict[str, dict]:
    """
    Ritorna un dict {token_address_lower → entry} per lookup O(1) in defi_optimized.

    Usato in fetch_dexscreener_pairs() per verificare rapidamente se un pair
    è in watchlist senza iterare la lista ogni volta.
    """
    return {
        g["token_address"].lower(): g
        for g in load_watchlist(chain)
        if g.get("token_address")
    }


def get_watchlist_pair_addresses(chain: Optional[str] = None) -> dict[str, dict]:
    """
    Come get_watchlist_addresses() ma indicizzato per pair_address.
    Utile quando si ha il pair_address ma non il token_address.
    """
    return {
        g["pair_address"].lower(): g
        for g in load_watchlist(chain)
        if g.get("pair_address")
    }


def watchlist_summary() -> str:
    """Ritorna una stringa di riepilogo per il log."""
    gems = load_watchlist()
    if not gems:
        return "Watchlist vuota."
    lines = []
    for g in gems:
        exp = g.get("expires_at", "?")[:16]
        lines.append(
            f"  • {g.get('token_symbol','?'):>10s} [{g.get('chain','?').upper()}] "
            f"P={g.get('gem_probability',0):.1%} | "
            f"inflow=${g.get('inflow_usd',0):,.0f} | "
            f"scade {exp}"
        )
    return f"{len(gems)} gemme in watchlist:\n" + "\n".join(lines)


# ==============================================================================
# HELPERS INTERNI
# ==============================================================================

def _read_raw() -> dict:
    """
    Legge il file JSON, ritorna dict vuoto se non esiste o corrotto.
    Usa JSONDecoder.raw_decode per tollerare null bytes / trailing garbage
    che Windows può lasciare dopo scritture parziali o crash.
    """
    try:
        if WATCHLIST_PATH.exists():
            raw = WATCHLIST_PATH.read_text(encoding="utf-8", errors="replace")
            # strip: null bytes e whitespace che rompono json.loads()
            raw = raw.strip().rstrip("\x00")
            if raw:
                data, _ = json.JSONDecoder().raw_decode(raw)
                if isinstance(data, dict):
                    return data
    except (json.JSONDecodeError, ValueError, OSError) as e:
        log.warning(f"[watchlist] Errore lettura {WATCHLIST_PATH}: {e} — reset.")
    return {"gems": [], "last_updated": datetime.now().isoformat()}


def _write_raw(data: dict) -> bool:
    """
    Scrittura atomica su file JSON.
    Scrive su .tmp e poi rinomina → nessun file parzialmente scritto in caso di crash.
    """
    tmp = WATCHLIST_PATH.with_suffix(".json.tmp")
    try:
        WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data, indent=2, ensure_ascii=False)
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()   # forza flush OS buffer prima del rename
        tmp.replace(WATCHLIST_PATH)   # atomico su tutti i SO supportati da Python
        return True
    except OSError as e:
        log.error(f"[watchlist] Errore scrittura {WATCHLIST_PATH}: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
