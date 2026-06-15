---
name: validate-filter
description: Valida un filtro candidato (su segnali defi/pre_grad/injective) contro dati storici, calcolando precisione (win bloccati vs loss evitate) prima di attivarlo.
---

# Validate Filter

Implementa la regola memorizzata: prima di attivare un filtro su un sistema live, controllare i suoi effetti su dati storici. Soglia di accettazione: **precisione > 60%** (loss evitate / (loss evitate + win bloccati)).

## Input richiesto dall'utente

- La condizione candidata (es. `vol_h1 < 5000`, `change_7d > 150 and ADX > 55`, `drift < -8%`)
- Il dataset storico da usare. Mappa sistema → file:
  - defi/pre_grad/v3 → `defi/reports/signals_log.csv`, `defi/reports/debug_candidates.csv`, `defi/reports/token_outcomes.csv`
  - pump_grad → `defi/reports/pump_grad_signals.csv`
  - midcap → `defi/reports/midcap_signals.csv`
  - injective → `injective_autopilot/` (chiedi il path esatto dei trade storici/paper)
- L'esito da usare per classificare win/loss (es. `pnl_eur`, `ret_1h_pct`, `exit_type`)

## Procedura

1. Carica il CSV con pandas, applica la condizione candidata come maschera.
2. Tra i segnali che la condizione AVREBBE scartato:
   - conta quanti erano **win** (esito positivo secondo la colonna scelta) → "win bloccati"
   - conta quanti erano **loss** (esito negativo) → "loss evitate"
3. Calcola `precisione = loss_evitate / (loss_evitate + win_bloccati)`
4. Riporta:
   - n totale segnali coperti dal filtro
   - win bloccati / loss evitate (numeri assoluti)
   - precisione %
   - PF (profit factor) o pnl totale prima/dopo il filtro, se la colonna pnl è disponibile

## Output

Tabella sintetica + verdetto:
- precisione > 60% → filtro consigliato per attivazione ALLA RADICE (scanner/emettitore, non nel simulator — vedi regola "filtri alla radice")
- precisione ≤ 60% → filtro da scartare o da raffinare (proponi varianti della soglia)

## Esempio invocazione

```
/validate-filter vol_h1 < 5000 su defi/reports/signals_log.csv, esito = pnl_eur
```
