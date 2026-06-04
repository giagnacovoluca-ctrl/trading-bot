"""
rugcheck.py — wrapper RugCheck.xyz per Solana.
Usato da defi_optimized, gemmeV3 e pump_graduation_scanner prima di emettere segnali.

Due tipi di check:
  - LP lock check (v3/v3_large/defi/gemme): blocca se lpLockedPct < soglia.
  - Top holder check (pump_grad): blocca se il top 1 holder > soglia.
    pump.fun blocca sempre l'LP alla graduation, quindi il rischio è il dump
    dei token accumulati da sniper/dev durante la bonding curve.
"""
import logging
import time

import requests

log = logging.getLogger("rugcheck")

_API_SUMMARY  = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"
_API_DETAILED = "https://api.rugcheck.xyz/v1/tokens/{}/report"
_cache: dict = {}   # { mint: (safe: bool, expires: float) }
_CACHE_TTL = 1800   # 30 min

# Soglia LP locked (v3/v3_large/defi/gemme): blocca se lpLockedPct < valore.
# pump_grad: LP è sempre locked su pump.fun → check LP disabilitato (None).
MIN_LP_LOCKED = {
    "defi":       20,
    "gemme":      20,
    "v3":         20,
    "v3_large":    0,   # large-cap ($10M+ mcap): LP lock meno critico con alto volume
    "v3_midcap":  20,
    "pump_grad":  None,
}

# Pericoli che bloccano SEMPRE indipendentemente dal LP lock
ALWAYS_BLOCK_DANGERS = {
    "freeze authority still enabled",  # creator può bloccare/confiscare token
}

# Soglia top holder per pump_grad: blocca se il wallet maggiore detiene > X% supply.
# Pattern rug pump.fun: sniper/dev accumulano >50% durante bonding curve poi dumpano.
MAX_TOP_HOLDER_PCT = 25.0


def is_safe(mint: str, scanner: str, chain: str = "solana") -> bool:
    """
    Ritorna True se il token supera i check RugCheck.
    Fail-open: in caso di errore API ritorna True (non blocca per problemi di rete).
    Solo Solana; altri chain ritornano True senza chiamata.
    """
    if chain.lower() not in ("solana", "sol"):
        return True

    cached = _cache.get(mint)
    if cached:
        safe, expires = cached
        if time.time() < expires:
            return safe

    if scanner == "pump_grad":
        return _check_pump_grad(mint)
    else:
        return _check_lp_lock(mint, scanner)


def _check_lp_lock(mint: str, scanner: str) -> bool:
    """Check LP locking per v3/v3_large/defi/gemme."""
    min_lp = MIN_LP_LOCKED.get(scanner)
    if min_lp is None:
        return True

    try:
        r = requests.get(_API_SUMMARY.format(mint), timeout=8,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            log.debug(f"[RugCheck] {mint[:12]}… HTTP {r.status_code} → skip")
            return True

        data      = r.json()
        lp_locked = float(data.get("lpLockedPct") or 0)
        score     = int(data.get("score_normalised") or 0)
        dangers   = [x["name"] for x in data.get("risks", []) if x.get("level") == "danger"]

        # Blocca se LP insufficiente
        lp_ok = lp_locked >= min_lp

        # Blocca se presenza di pericoli critici (Freeze Authority, ecc.)
        critical = [d for d in dangers if d.lower() in ALWAYS_BLOCK_DANGERS]

        safe = lp_ok and not critical
        _cache[mint] = (safe, time.time() + _CACHE_TTL)

        if not safe:
            reason = []
            if not lp_ok:
                reason.append(f"lpLocked={lp_locked:.0f}% < {min_lp}%")
            if critical:
                reason.append(f"danger_critico={critical}")
            log.warning(
                f"[RugCheck] {mint[:16]}… BLOCCATO | "
                f"{' | '.join(reason)} | score={score} | danger={dangers}"
            )
        else:
            log.debug(f"[RugCheck] {mint[:12]}… OK | lpLocked={lp_locked:.0f}% | score={score}")
        return safe

    except Exception as e:
        log.debug(f"[RugCheck] {mint[:12]}… errore rete: {e} → skip")
        return True


def _check_pump_grad(mint: str) -> bool:
    """
    Check per pump.fun graduated token.
    LP è sempre locked → non check LP.
    Blocca se top 1 holder > MAX_TOP_HOLDER_PCT (sniper/dev con troppa supply).

    Se topHolders è vuoto (token non ancora indicizzato), riprova fino a 3 volte
    con 5s di attesa. Se ancora vuoto dopo i retry → blocca (fail-closed):
    token con holder sconosciuti su pump.fun sono sospetti.
    """
    MAX_RETRIES = 3
    RETRY_DELAY = 5

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(_API_DETAILED.format(mint), timeout=10,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                log.debug(f"[RugCheck/pump] {mint[:12]}… HTTP {r.status_code} → skip (fail-open)")
                return True   # API irraggiungibile: fail-open

            data    = r.json()
            holders = data.get("topHolders") or []

            if not holders:
                if attempt < MAX_RETRIES:
                    log.debug(f"[RugCheck/pump] {mint[:12]}… topHolders vuoto, retry {attempt}/{MAX_RETRIES} tra {RETRY_DELAY}s...")
                    time.sleep(RETRY_DELAY)
                    continue
                # Dopo tutti i retry: token non indicizzato → blocca
                log.warning(f"[RugCheck/pump] {mint[:16]}… BLOCCATO | topHolders vuoto dopo {MAX_RETRIES} tentativi (token non indicizzato = sospetto)")
                _cache[mint] = (False, time.time() + 300)   # cache corta: riprova tra 5 min
                return False

            top_pct  = float(holders[0].get("pct") or holders[0].get("percentage") or 0)
            safe     = top_pct <= MAX_TOP_HOLDER_PCT
            _cache[mint] = (safe, time.time() + _CACHE_TTL)

            if not safe:
                top_addr = str(holders[0].get("address") or holders[0].get("owner") or "")[:16]
                log.warning(
                    f"[RugCheck/pump] {mint[:16]}… BLOCCATO | "
                    f"top holder {top_addr}… detiene {top_pct:.1f}% > {MAX_TOP_HOLDER_PCT}% "
                    f"(dump risk)"
                )
            else:
                log.debug(f"[RugCheck/pump] {mint[:12]}… OK | top holder={top_pct:.1f}%")
            return safe

        except Exception as e:
            log.debug(f"[RugCheck/pump] {mint[:12]}… errore rete tentativo {attempt}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    log.debug(f"[RugCheck/pump] {mint[:12]}… tutti i tentativi falliti → skip (fail-open)")
    return True   # errori di rete: fail-open
