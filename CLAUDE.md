# Regole di Sviluppo e Ottimizzazione Token

## Gestione del Contesto
- Prima di usare strumenti di ricerca liberi ('grep', 'find') o leggere interi file, consulta SEMPRE il file `@codebase_summary.md`.
- Troverai lì l'albero dei file, le firme di tutte le funzioni/classi, i parametri chiave e lo stato attuale di ogni sistema.
- Ispeziona o leggi un file sorgente specifico SOLO se ti viene chiesto esplicitamente o se devi modificarne la logica interna.

## Architettura in breve
Il sistema è un bot di trading crypto multi-chain (Solana + Base) composto da:
- **Scanner** (`defi_optimized.py`, `gemmeV3.py`) → trovano segnali, inviano email, scrivono CSV
- **LiveEngine** (`trade_simulator.py`) → traccia posizioni, gestisce exit automatici, genera HTML
- **Executor** (`solana_executor.py`) → esegue swap reali su Solana via Jupiter
- **Specializzati** (`pump_graduation_scanner.py`, `pre_grad_monitor.py`, `wallet_mirror_bot.py`)
- **Orchestratore** (`run.py`) → avvia tutto in thread daemon con auto-restart

## Chain attive
- **Solana**: scanner + executor reale (Jupiter)
- **Base**: scanner + simulator only (nessun base_executor.py)
- BSC/ETH: disabilitati (codice conservato commentato)

## Stile di Risposta
- Sii estremamente conciso, diretto e orientato al codice.
- Evita convenevoli, spiegazioni teoriche ridondanti o riassunti prolissi. Mostra solo i piani d'azione e i blocchi di codice modificati.
