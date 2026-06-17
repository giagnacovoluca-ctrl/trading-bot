"""
composite_monitor_report.py — report sulla distribuzione di prepump_composite_score
e delle nuove metriche (wallet_confluence_score, bsr_recent_shift,
momentum_score_continuous), basato su reports/composite_monitor.csv
(loggato da _log_composite_monitor in defi_optimized.py, un cicli lento ogni ~180s).

Uso: python3 composite_monitor_report.py [path_csv]

Monitoraggio temporaneo richiesto il 14/06 — rimuovere/disattivare quando
l'osservazione è conclusa.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_DEFAULT_CSV = Path(__file__).resolve().parent / "reports" / "composite_monitor.csv"


def main(csv_path: Path) -> None:
    if not csv_path.exists():
        print(f"File non trovato: {csv_path} — il monitor non ha ancora scritto dati "
              f"(serve restart run.py + qualche ciclo lento).")
        return

    df = pd.read_csv(csv_path)
    if df.empty:
        print("composite_monitor.csv è vuoto.")
        return

    n_rows = len(df)
    n_unique_tokens = df["token_address"].nunique()
    ts_min, ts_max = df["timestamp"].min(), df["timestamp"].max()
    print(f"Righe totali: {n_rows} | token distinti: {n_unique_tokens}")
    print(f"Periodo osservato: {ts_min} -> {ts_max}")

    comp = df["prepump_composite_score"]

    # ── 1. Distribuzione score ──────────────────────────────────────────
    print("\n" + "=" * 60)
    print("1. DISTRIBUZIONE prepump_composite_score")
    print("=" * 60)
    buckets = [
        (">0.55",      comp > 0.55),
        ("0.50-0.55",  (comp >= 0.50) & (comp <= 0.55)),
        ("0.45-0.50",  (comp >= 0.45) & (comp < 0.50)),
        ("0.40-0.45",  (comp >= 0.40) & (comp < 0.45)),
        ("<0.40",      comp < 0.40),
    ]
    for label, mask in buckets:
        n = mask.sum()
        print(f"  {label:10s}: {n:6d}  ({n / n_rows * 100:5.2f}%)")

    # ── 2. Top 20 per composite (ultima osservazione per token) ────────
    print("\n" + "=" * 60)
    print("2. TOP 20 TOKEN PER COMPOSITE (ultima osservazione per token)")
    print("=" * 60)
    last_per_token = (
        df.sort_values("timestamp")
          .groupby("token_address", as_index=False)
          .last()
    )
    top20 = last_per_token.nlargest(20, "prepump_composite_score")
    cols = ["timestamp", "chain", "token_symbol", "prepump_composite_score",
            "score_top_component", "wallet_confluence_score",
            "bsr_recent_shift", "momentum_score_continuous", "passes_c9_comp"]
    print(top20[cols].to_string(index=False))

    # ── 3. Segnali effettivi (composite >= 0.55) ────────────────────────
    print("\n" + "=" * 60)
    print("3. SEGNALI (prepump_composite_score >= 0.55)")
    print("=" * 60)
    segnali = df[df["prepump_composite_score"] >= 0.55]
    if segnali.empty:
        print("  Nessun segnale con composite >= 0.55 nel periodo osservato.")
    else:
        print(segnali[["timestamp", "chain", "token_symbol",
                        "prepump_composite_score", "score_top_component",
                        "wallet_confluence_score",
                        "momentum_score_continuous"]].to_string(index=False))

    # ── 4. Candidati appena sotto soglia (0.50-0.55) ────────────────────
    print("\n" + "=" * 60)
    print("4. CANDIDATI APPENA SOTTO SOGLIA (0.50 <= composite < 0.55)")
    print("=" * 60)
    near = last_per_token[
        (last_per_token["prepump_composite_score"] >= 0.50)
        & (last_per_token["prepump_composite_score"] < 0.55)
    ].sort_values("prepump_composite_score", ascending=False)
    n_near = len(near)
    n_unique_near = near["token_address"].nunique()
    print(f"  Token distinti con ultima osservazione in 0.50-0.55: {n_unique_near}")
    if not near.empty:
        print(near[["timestamp", "chain", "token_symbol",
                     "prepump_composite_score", "score_top_component",
                     "wallet_confluence_score",
                     "momentum_score_continuous"]].to_string(index=False))

    # bonus: quante osservazioni (non solo ultima) hanno toccato 0.50-0.55
    touched = df[(df["prepump_composite_score"] >= 0.50) & (df["prepump_composite_score"] < 0.55)]
    print(f"\n  Osservazioni totali (tutti i cicli) in 0.50-0.55: {len(touched)} "
          f"su {n_rows} ({len(touched)/n_rows*100:.2f}%)")


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_CSV
    main(path)
