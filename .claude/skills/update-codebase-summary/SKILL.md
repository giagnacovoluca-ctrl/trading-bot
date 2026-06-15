---
name: update-codebase-summary
description: Aggiorna codebase_summary.md riflettendo le modifiche recenti (file tree, firme funzioni/classi, stato sistemi), senza rileggere l'intero file.
disable-model-invocation: true
---

# Update Codebase Summary

`codebase_summary.md` è la mappa di riferimento del progetto (consultata SEMPRE prima di grep/read, vedi CLAUDE.md). Va tenuta sincronizzata con il codice senza richiedere una lettura completa ogni volta.

## Procedura

1. **Identifica i file modificati** dall'ultimo aggiornamento:
   ```bash
   git log --since="<data ultimo aggiornamento in codebase_summary.md>" --name-only --pretty=format: | sort -u | grep -v '^$'
   ```
   Oppure, se non committato: `git status --porcelain`.

2. **Per ogni file modificato**, leggi solo le parti cambiate (`git diff <file>`) e individua:
   - nuove funzioni/classi o firme cambiate
   - nuovi parametri/costanti chiave (soglie, env var)
   - cambi di stato (es. "ora attivo", "thread aggiunto a run.py", "deprecato")

3. **Aggiorna `codebase_summary.md`**:
   - Aggiungi una nuova entry in cima alla sezione "Aggiornato: <data>" (usa la data odierna) con un riassunto in 2-4 righe delle modifiche, stile delle entry esistenti (telegrafico, con riferimenti a file/funzioni).
   - Aggiorna l'albero dei file (sezione 1) se sono stati aggiunti/rimossi file: aggiungi/marca con `← NUOVO <data>` o `← DEPRECATO`.
   - Aggiorna le sezioni con firme di funzioni/classi/parametri se la logica interna di un componente è cambiata in modo rilevante per chi legge solo il summary (nuovi parametri chiave, nuove costanti soglia, nuovi CSV/output).

4. **Non riscrivere sezioni intere** se non necessario: usa Edit puntuali, preservando lo stile telegrafico esistente (niente prosa, niente markdown decorativo extra).

5. Se il file supera dimensioni eccessive (>80KB), valuta di comprimere le entry "Aggiornato" più vecchie di 2+ settimane in una riga riassuntiva, ma SOLO se richiesto esplicitamente.

## Quando usarla

Invocare `/update-codebase-summary` a fine sessione dopo modifiche a scanner/simulator/executor, o quando l'utente segnala che il summary è desincronizzato dal codice.
