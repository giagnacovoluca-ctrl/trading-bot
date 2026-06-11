"""
Audit completo dei trade — eseguibile offline:

    python -m analytics.audit

1. Backfill: collega i trade pre-migrazione ai segnali (tabella signals)
   via market_id + direzione + prossimità temporale (±120s).
2. Genera i post-mortem mancanti.
3. Stampa il report: pattern win/loss, ranking segnali, combinazioni,
   mercati, analisi temporale, coerenza scoring.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings
from database.repository import Repository
from analytics import performance as perf
from analytics.adaptive_scorer import AdaptiveScorer
from analytics.postmortem import build_postmortem


async def backfill_trade_signals(repo: Repository, trades: list[dict]) -> int:
    """Collega trade senza contesto segnali alla riga signals più vicina."""
    fixed = 0
    for t in trades:
        if t.get("active_signals"):
            continue
        candidates = await repo.get_signals_window(t["entry_ts"] - 120, t["entry_ts"] + 120)
        match = None
        best_dt = 1e9
        for s in candidates:
            if s["market_id"] != t["market_id"]:
                continue
            if s["decision"] not in (t["direction"],):
                continue
            dt = abs(s["ts"] - t["entry_ts"])
            if dt < best_dt:
                best_dt = dt
                match = s
        if match:
            await repo.update_trade_signals(t["id"], match["signals"], match["values"])
            t["active_signals"] = match["signals"]
            t["signal_values"] = match["values"]
            fixed += 1
    return fixed


def _print_table(title: str, rows: list[dict], cols: list[str]) -> None:
    print(f"\n— {title} " + "—" * max(0, 60 - len(title)))
    if not rows:
        print("  (nessun dato)")
        return
    header = " | ".join(f"{c:>14}" for c in cols)
    print("  " + header)
    for r in rows:
        print("  " + " | ".join(f"{str(r.get(c, '')):>14}" for c in cols))


async def run_audit() -> None:
    cfg = get_settings()
    repo = Repository(cfg.db_url)
    await repo.init()

    trades = await repo.get_closed_trades()
    print(f"AUDIT — {len(trades)} trade chiusi nel database")
    if not trades:
        return

    fixed = await backfill_trade_signals(repo, trades)
    if fixed:
        print(f"Backfill: contesto segnali ricostruito per {fixed} trade")

    # Post-mortem mancanti
    scorer = AdaptiveScorer()
    scorer.update(trades)
    existing_pm = {pm["trade_id"] for pm in await repo.get_postmortems(limit=10000)}
    new_pm = 0
    for t in trades:
        if t["id"] not in existing_pm:
            await repo.save_postmortem(build_postmortem(t, scorer.signal_stats))
            new_pm += 1
    if new_pm:
        print(f"Post-mortem generati: {new_pm}")

    # ── Report ──
    stats = perf.basic_stats([t["pnl_usdt"] for t in trades])
    print(f"\nGLOBALE: n={stats['n']} WR={stats['win_rate']}% PF={stats['profit_factor']} "
          f"exp={stats['expectancy']}$ pnl={stats['net_pnl']}$ maxDD={stats['max_drawdown']}$")

    cols = ["n", "win_rate", "profit_factor", "expectancy", "net_pnl", "max_drawdown"]
    _print_table("RANKING SEGNALI", perf.signal_ranking(trades), ["signal"] + cols)
    _print_table("RANKING COMBINAZIONI", perf.combo_ranking(trades), ["combo"] + cols)
    _print_table("RANKING MERCATI", perf.market_ranking(trades), ["ticker"] + cols + ["sharpe"])
    _print_table("DIREZIONE", perf.direction_ranking(trades), ["direction"] + cols)
    _print_table("ORA DEL GIORNO (UTC)", perf.hourly_analysis(trades), ["hour"] + cols)
    _print_table("GIORNO SETTIMANA", perf.weekday_analysis(trades), ["weekday"] + cols)
    _print_table("REGIME VOLATILITÀ", perf.vol_regime_analysis(trades), ["regime"] + cols)
    _print_table("COERENZA SCORING (bucket confidence)", perf.score_bucket_analysis(trades), ["score_bucket"] + cols)
    _print_table("EXIT REASON", perf.exit_reason_analysis(trades), ["exit_reason"] + cols)

    print("\n— PATTERN WIN vs LOSS " + "—" * 40)
    print(json.dumps(perf.win_loss_patterns(trades), indent=2))

    print("\n— PESI ADATTIVI CORRENTI " + "—" * 38)
    for sig, w in sorted(scorer.weights.items(), key=lambda x: -x[1]):
        st = scorer.signal_stats[sig]
        print(f"  {sig:<18} w={w:.3f}  (n={st['total_activations']}, "
              f"postWR={st['posterior_winrate']:.2f}, ewma={st['ewma_norm_expectancy']:+.2f})")

    # Snapshot baseline dei pesi
    await repo.save_weight_snapshot(len(trades), scorer.weights, scorer.signal_stats)
    print("\nSnapshot pesi salvato.")


if __name__ == "__main__":
    asyncio.run(run_audit())
