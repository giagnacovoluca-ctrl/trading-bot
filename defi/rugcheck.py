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

# Soglia top holder: blocca se il wallet maggiore detiene > X% supply.
# Pattern rug pump.fun: sniper/dev accumulano >50% durante bonding curve poi dumpano.
# pre_grad 12/06: a 25% il 100% dei token HOT veniva bloccato (0 segnali in 11h,
# min osservato 26%, mediana 38%) → alzata a 55% (~75-80% pass rate) per
# raccogliere dati. I segnali con top_holder in (25%, 55%] sono "shadow"
# (size=0 nel simulator, vedi SHADOW_TOP_HOLDER_PCT).
MAX_TOP_HOLDER_PCT = {
    "pump_grad": 25.0,
    "pre_grad":  55.0,
}
# Soglia "sicura" originale: oltre questa, il segnale pre_grad passa ma come shadow.
SHADOW_TOP_HOLDER_PCT = 25.0


def is_safe(mint: str, scanner: str, chain: str = "solana") -> bool:
    """
    Ritorna True se il token supera i check RugCheck.
    Fail-open: in caso di errore API ritorna True (non blocca per problemi di rete).
    Solo Solana; altri chain ritornano True senza chiamata.
    """
    return is_safe_detailed(mint, scanner, chain)[0]


def is_safe_detailed(mint: str, scanner: str, chain: str = "solana") -> tuple[bool, float | None]:
    """
    Come is_safe, ma ritorna anche il top_holder_pct (None se non applicabile/non
    determinato) per permettere ai chiamanti pre_grad di distinguere i segnali
    "shadow" (passano la soglia rilassata ma non quella originale 25%).
    """
    if chain.lower() not in ("solana", "sol"):
        return True, None

    cached = _cache.get(mint)
    if cached:
        safe, expires, top_pct = cached
        if time.time() < expires:
            return safe, top_pct

    if scanner in ("pump_grad", "pre_grad"):
        # pre-graduation: niente LP da controllare (bonding curve), il rischio
        # vero è la concentrazione holder (sniper/dev che dumpano alla graduation)
        return _check_pump_grad(mint, scanner)
    else:
        return _check_lp_lock(mint, scanner), None


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
        _cache[mint] = (safe, time.time() + _CACHE_TTL, None)

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


def _check_pump_grad(mint: str, scanner: str) -> tuple[bool, float | None]:
    """
    Check per pump.fun graduated token.
    LP è sempre locked → non check LP.
    Blocca se top 1 holder > MAX_TOP_HOLDER_PCT[scanner] (sniper/dev con troppa supply).
    Ritorna anche il top_holder_pct (None se non determinato/fail-open).

    Se topHolders è vuoto (token non ancora indicizzato), riprova fino a 3 volte
    con 5s di attesa. Se ancora vuoto dopo i retry → blocca (fail-closed):
    token con holder sconosciuti su pump.fun sono sospetti.
    """
    MAX_RETRIES = 3
    RETRY_DELAY = 5
    max_pct = MAX_TOP_HOLDER_PCT.get(scanner, 25.0)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(_API_DETAILED.format(mint), timeout=10,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                log.debug(f"[RugCheck/pump] {mint[:12]}… HTTP {r.status_code} → skip (fail-open)")
                return True, None   # API irraggiungibile: fail-open

            data    = r.json()
            holders = data.get("topHolders") or []

            if not holders:
                if attempt < MAX_RETRIES:
                    log.debug(f"[RugCheck/pump] {mint[:12]}… topHolders vuoto, retry {attempt}/{MAX_RETRIES} tra {RETRY_DELAY}s...")
                    time.sleep(RETRY_DELAY)
                    continue
                # Dopo tutti i retry: token non indicizzato → blocca
                log.warning(f"[RugCheck/pump] {mint[:16]}… BLOCCATO | topHolders vuoto dopo {MAX_RETRIES} tentativi (token non indicizzato = sospetto)")
                _cache[mint] = (False, time.time() + 300, None)   # cache corta: riprova tra 5 min
                return False, None

            top_pct  = float(holders[0].get("pct") or holders[0].get("percentage") or 0)
            safe     = top_pct <= max_pct
            _cache[mint] = (safe, time.time() + _CACHE_TTL, top_pct)

            if not safe:
                top_addr = str(holders[0].get("address") or holders[0].get("owner") or "")[:16]
                log.warning(
                    f"[RugCheck/pump] {mint[:16]}… BLOCCATO | "
                    f"top holder {top_addr}… detiene {top_pct:.1f}% > {max_pct}% "
                    f"(dump risk)"
                )
            else:
                log.debug(f"[RugCheck/pump] {mint[:12]}… OK | top holder={top_pct:.1f}%")
            return safe, top_pct

        except Exception as e:
            log.debug(f"[RugCheck/pump] {mint[:12]}… errore rete tentativo {attempt}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    log.debug(f"[RugCheck/pump] {mint[:12]}… tutti i tentativi falliti → skip (fail-open)")
    return True, None   # errori di rete: fail-open
