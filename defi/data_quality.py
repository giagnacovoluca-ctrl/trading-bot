"""
Soglie e validazioni condivise per la qualità dei dati del sistema "defi".

Single source of truth per evitare disallineamenti tra scanner
(defi_optimized.py) e simulator (trade_simulator.py).
"""

# Soglia minima volume_1h_usd per considerare un segnale defi tracciabile.
# Usata sia dal filtro candidati (defi_optimized.CONFIG["MIN_VOLUME_1H_USD"])
# sia dal guard di entry in trade_simulator (era 10_000, ora allineata).
MIN_VOLUME_1H_USD_DEFI = 15_000.0

# Drawdown immediato (change_pct) sotto il quale, in assenza di una riga
# action="entry" per quel signal_id, il trade non è un esito di strategia
# ma una corruzione/anomalia di stato (es. rehydration da live_state.json
# obsoleto senza fill reale — vedi WINNING/GOBLIEN/SERENA/PXC 23-24/05).
MAX_INITIAL_DRAWDOWN_PCT = -80.0

# Salto massimo plausibile di change_pct tra due tick consecutivi senza
# entry valida (snapshot di prezzo non confermato da fonte multipla).
MAX_SINGLE_TICK_JUMP_PCT = 70.0


def is_valid_trade_event(row: dict, has_entry: bool, prev_change_pct: float = None) -> tuple:
    """
    Valida una riga di live_trades.csv prima che contribuisca alle metriche
    (PF/WR/EV). Ritorna (True, None) se è un risultato di strategia valido,
    (False, motivo) se è un "data_fault" (corruzione/anomalia dati).

    NON applica soglie di strategia/tuning: classifica solo eventi che non
    rappresentano un trade realmente eseguito.
    """
    action = row.get("action", "")
    note   = row.get("note", "") or ""

    try:
        chg = float(str(row.get("change_pct", "0")).replace("+", "") or 0)
    except (ValueError, TypeError):
        chg = 0.0

    # Nessuna entry registrata per questo signal_id + drawdown immediato
    # estremo → posizione mai realmente apribile. Esclude skip/skip_stale:
    # quelli sono rifiuti pre-entry by-design (mai diventati posizioni),
    # già autodescritti dall'action stessa, non corruzione di stato.
    if not has_entry and action not in ("skip", "skip_stale") and chg <= MAX_INITIAL_DRAWDOWN_PCT:
        return False, "missing_entry_event"

    # Entry con vol_h1 non attendibile (esclude il caso intenzionale "vol_na"
    # per segnali v3 stale, che è un comportamento by-design).
    if action == "entry" and "vol_na" not in note:
        try:
            vol_h1 = float(row.get("vol_h1", 0) or 0)
        except (ValueError, TypeError):
            vol_h1 = 0.0
        if vol_h1 <= 0:
            return False, "invalid_vol_h1_at_entry"

    # Salto di prezzo non plausibile in singolo tick, senza entry valida.
    if not has_entry and prev_change_pct is not None:
        if abs(chg - prev_change_pct) > MAX_SINGLE_TICK_JUMP_PCT:
            return False, "extreme_single_tick_jump"

    # hard_sl con pnl_eur=0 ma drawdown reale registrato (>1%): il simulator
    # non ha potuto stimare un prezzo di uscita realistico per liquidità
    # troppo bassa (pump_grad backtest 15/06: 10/80 trade, tutti hard_sl con
    # chg<=-12% ma pnl azzerato) — non è un pareggio di strategia.
    if action == "hard_sl":
        try:
            pnl = float(str(row.get("pnl_eur", "0")).replace("+", "") or 0)
        except (ValueError, TypeError):
            pnl = 0.0
        if pnl == 0.0 and abs(chg) > 1.0:
            return False, "zeroed_low_liquidity"

    return True, None
