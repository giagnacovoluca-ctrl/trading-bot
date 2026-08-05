# FraDodo & KASH!DO Contest - Documentazione di Progetto

Questo documento serve come "memoria storica" e guida architetturale per facilitare lo sviluppo futuro, i riavvii o il debug da parte dell'IA o degli sviluppatori.

## Architettura del Sistema
L'intero ecosistema è diviso in **3 Componenti Principali**, tutti gestiti tramite sessioni `screen` su Linux per garantire operatività 24/7 in background.

### 1. Il Bot Discord (`main.py` & `cogs/`)
- **Linguaggio/Libreria:** Python 3 (discord.py)
- **Funzione:** Gestisce l'interfaccia utente su Discord tramite Slash Commands (`/`) e bottoni interattivi (Views).
- **Sessione Screen:** `fradodo_bot`
- **Comandi Principali:**
  - `!crea_canali`: (Solo Admin) Genera l'intera struttura dei canali e i messaggi con i bottoni interattivi (Menu, Classifica, Profilo).
  - `/invia_risultato`: Permette l'upload nativo da Discord del risultato (inclusa la foto dello screenshot).
- **Logica Bottoni & Menu:**
  - `ksd_contest.py` contiene i bottoni.
  - L'invio dei risultati può avvenire via Web App (con foto) o via Modal Discord (solo testo, limite imposto da Discord).

### 2. La Web App / Dashboard (`web/app.py`)
- **Linguaggio/Framework:** Python (FastAPI per il Backend) + HTML/CSS/JS (Jinja2 per il Frontend).
- **Funzione:** Offre una dashboard visivamente accattivante per gli utenti per inviare i risultati e controllare le classifiche. Offre un pannello `/admin` per lo staff per approvare/rifiutare le partite e gestire gli utenti.
- **Porta:** Gira in locale sulla porta `8000`.
- **Integrazione:** Legge e scrive direttamente sul database SQLite condiviso con il Bot (`../database.db`).

### 3. Il Watchdog & Tunneling (`watchdog.py`)
- **Linguaggio:** Python
- **Funzione:** Espone la Web App locale su Internet tramite `localtunnel` e garantisce che rimanga online.
- **Sessione Screen:** `fradodo_watchdog`
- **Meccanismo di Auto-Riparazione:** 
  1. Avvia `npx localtunnel --port 8000`.
  2. Cattura l'URL generato e lo salva nel file `current_url.txt`.
  3. Se il tunnel cade o si blocca, il Watchdog riavvia il tunnel silenziosamente e aggiorna il file.
  4. Il Bot Discord, tramite un `tasks.loop`, legge `current_url.txt` ogni 15 secondi. Se nota che l'URL è cambiato, **aggiorna automaticamente tutti i bottoni URL su Discord** in tempo reale, garantendo continuità assoluta per gli utenti.

## Database (`database.db`)
Database SQLite situato nella root del progetto.
Tabelle Principali:
- **players:** Registra gli utenti (discord_id, activision_id, punti totali, contest_points). *L'iscrizione avviene ora in modo automatico ed invisibile alla prima interazione.*
- **matches:** Salva le partite da approvare, includendo kills, placement, screenshot_url e l'id del giocatore.
- Altre tabelle legacy mantengono la storicità dei vecchi tornei.

## Avvio Rapido & Gestione
Per riavviare l'intero sistema in modo pulito e sicuro, utilizzare il file bash incluso:
```bash
bash start_fradodo.sh
```
Questo script ucciderà eventuali istanze vecchie, riattiverà l'ambiente virtuale (`venv`) e creerà le nuove sessioni `screen`.

### Comandi Utili (Debug)
- `screen -ls` -> Mostra i processi attivi.
- `screen -r fradodo_bot` -> Visualizza i log in diretta del Bot.
- `screen -r fradodo_watchdog` -> Visualizza i log in diretta del Tunnel e del Watchdog.
- Premi `CTRL+A` seguito da `D` per uscire da uno screen senza chiuderlo.

## Note per Sviluppi Futuri
- L'integrazione di OCR per la lettura automatica degli screenshot è presente ma attualmente limitata/da raffinare nel modulo di upload (`app.py`).
- Non utilizzare i Modal di Discord per richiedere immagini ai giocatori, poiché Discord non lo supporta. Affidarsi alla Web App o agli Slash Command (`/invia_risultato`).
