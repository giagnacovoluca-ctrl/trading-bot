"""
Post-mortem automatico per ogni trade chiuso.

Genera una valutazione statistica (non narrativa) del trade:
  - R-multiple realizzato vs pianificato
  - efficienza dell'exit (MFE catturato)
  - contributo paritario dei segnali attivi
  - confronto con l'expectancy storica dei segnali coinvolti
"""

from __future__ import annotations

import time
from typing import Any

from analytics.performance import basic_stats, trade_signals


def build_postmortem(
    trade: dict[str, Any],
    signal_stats: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """
    Costruisce il dict pronto per Repository.save_postmortem().
    `trade` è il dict di Repository._trade_to_dict (o equivalente).
    `signal_stats` (opzionale): stats correnti dell'AdaptiveScorer per
    contestualizzare il risultato rispetto alla storia di ogni segnale.
    """
    pnl = trade["pnl_usdt"]
    entry = trade["entry_price"]
    sl = trade["stop_loss"]
    qty = trade["quantity"]

    initial_risk = abs(entry - sl) * qty
    r_multiple = pnl / initial_risk if initial_risk > 1e-10 else 0.0
    hold_hours = (
        (trade["exit_ts"] - trade["entry_ts"]) / 3600.0 if trade.get("exit_ts") else 0.0
    )

    sigs = trade_signals(trade)
    # Contributo paritario: il PnL è attribuito in parti uguali ai segnali attivi
    contributions = {s: round(pnl / len(sigs), 4) for s in sigs} if sigs else {}

    evaluation = _evaluate(trade, r_multiple, sigs, signal_stats or {})

    return {
        "trade_id": trade["id"],
        "ts": time.time(),
        "ticker": trade.get("ticker", ""),
        "direction": trade["direction"],
        "entry_reason": trade.get("reason", ""),
        "entry_confidence": trade.get("confidence", 0.0),
        "active_signals": trade.get("active_signals") or [],
        "signal_values": trade.get("signal_values") or {},
        "exit_reason": trade.get("exit_reason", ""),
        "pnl_usdt": round(pnl, 4),
        "r_multiple": round(r_multiple, 3),
        "hold_hours": round(hold_hours, 2),
        "mae_pct": round(trade.get("mae_pct", 0.0), 3),
        "mfe_pct": round(trade.get("mfe_pct", 0.0), 3),
        "signal_contributions": contributions,
        "evaluation": evaluation,
    }


def _evaluate(
    trade: dict,
    r_multiple: float,
    sigs: list[str],
    signal_stats: dict[str, dict],
) -> str:
    notes: list[str] = []
    pnl = trade["pnl_usdt"]
    mfe = trade.get("mfe_pct", 0.0)
    mae = trade.get("mae_pct", 0.0)

    notes.append(f"R={r_multiple:+.2f}")

    if pnl > 0:
        if mae > 0.5 * mfe and mfe > 0:
            notes.append(f"win ma con MAE elevato ({mae:.2f}% vs MFE {mfe:.2f}%) — entry timing migliorabile")
    else:
        if mfe > 1.0:
            notes.append(f"loss con MFE {mfe:.2f}% non catturato — exit/trailing da valutare")
        if trade.get("exit_reason") == "SL" and mae > 0 and mfe < 0.2:
            notes.append("SL diretto senza escursione favorevole — segnale d'ingresso debole in questo contesto")

    # Confronto col profilo storico dei segnali coinvolti
    for s in sigs:
        st = signal_stats.get(s)
        if not st or st.get("n", 0) < 5:
            continue
        exp = st.get("expectancy", 0.0)
        if pnl <= 0 and exp > 0:
            notes.append(f"{s}: loss contro expectancy storica positiva ({exp:+.2f}$) — entro varianza")
        elif pnl <= 0 and exp <= 0:
            notes.append(f"{s}: expectancy storica già negativa ({exp:+.2f}$, n={st['n']}) — pattern ricorrente")

    conf = trade.get("confidence", 0.0)
    if pnl <= 0 and conf >= 0.65:
        notes.append(f"score alto ({conf:.2f}) con esito negativo — rivedere i bonus di scoring se ricorrente")

    return "; ".join(notes)
