# 🤖 Antigravity Video Orchestrator Guide

Questo documento contiene le direttive architetturali per l'automazione della generazione video su TikTok tramite l'agente (Antigravity).

## Architettura a 3 Step
Per evitare crash di memoria (Out of Memory - 137) dovuti al conflitto tra **XTTS v2** e **Whisper** all'interno dello stesso processo Python, il workflow è stato tassativamente diviso in 3 script separati. Come orchestratore, dovrai eseguire questi comandi in sequenza, assicurandoti che ogni step sia completato con successo prima di passare al successivo.

Tutti i comandi vanno lanciati dal virtual environment:
`source venv_video/bin/activate`

### Step 1: Generazione Voce Clonata (XTTS)
*   **Comando:** `python step1_voce.py --script <FILE_SCRIPT> --output temp/voice.mp3 --provider xtts`
*   **Descrizione:** Genera la voce in locale scaricando l'audio in `temp/voice.mp3`. Svuota la RAM alla fine.

### Step 2: Generazione Sfondo Dinamico (Pexels)
*   **Comando:** `python step2_sfondo.py --audio temp/voice.mp3 --output temp/video_base.mp4`
*   **Descrizione:** Scarica i video da Pexels (in formato 9:16), li taglia, e crea un file mp4 contenente video + audio, MA SENZA SOTTOTITOLI.

### Step 3: Sottotitoli Dinamici e CTA (Whisper)
*   **Comando:** `python step3_sottotitoli.py --video temp/video_base.mp4 --audio temp/voice.mp3 --output output/video_finale.mp4 --cta`
*   **Descrizione:** Esegue l'estrazione dei timestamp con Whisper, "stampa" i testi enormi in formato TikTok sul video e aggiunge la Call-To-Action finale.

## Ruolo dell'Agente
Come agente, il mio compito è orchestrare l'esecuzione manuale o automatizzata di questi step su indicazione dell'utente, gestire eventuali errori (come Pexels down o file mancanti) e passare al RAG per la redazione dei nuovi script quando richiesto.

## Pubblicazione Automatica (Playwright)
La pubblicazione automatica (`step4_pubblica.py`) non usa più i cookie testuali (`cookies.txt`), ma si affida a una **sessione browser persistente** sempre attiva salvata nella cartella `chrome_profile`. 
*   **Caroselli:** `python crea_carosello.py` genera testi con Gemini, 5 slide e crea un MP4. `step4_pubblica.py` carica il carosello/video usando Playwright bypassando i limiti API.
*   **Gestione Sessione:** `agente_tiktok.py` rileva la cartella `chrome_profile` per lanciare la pubblicazione. Se la sessione scade, basta rieseguire `setup_tiktok_login.sh` per aggiornarla.

## Automazione (Cronjob)
Il file `new_crontab.txt` contiene i timing esatti per la pubblicazione autonoma, divisa in script specifici che fungono da ponte verso l'orchestratore centrale `agente_tiktok.py` e il creatore di caroselli `crea_carosello.py`:
- **Video Standard**:
  - `video_virale.sh` (08:30, 19:00)
  - `video_bastian.sh` (15:00)
  - `video_promo.sh` (21:00) -> Questo script (in modalità promo) si occupa anche di trasferire il video nel progetto Conscia-Mente, generare l'articolo per il blog e fare push su Vercel.
- **Caroselli**:
  - `carousel_virale.sh` (08:00, 17:00)
  - `carousel_promo.sh` (13:00, 22:00)
  - `carousel_bastian.sh` (20:00)
Tutti questi script bash richiamano internamente Antigravity CLI utilizzando la massima qualità disponibile (`--model pro --effort high`), senza mai chiamare le vecchie API a pagamento. 
**Gestione Metadati (Novità)**: La descrizione (caption) dei video viene generata dinamicamente e in tempo reale all'interno di `step4_pubblica.py` tramite CLI se manca, eliminando completamente i bug di disallineamento (caption miste tra caroselli e video).

## Novità Architetturali Recenti (Ricerca Web & Tematiche)
Il vecchio e instabile `viral_news.py` è stato **deprecato**. Adesso il sistema si affida interamente alle capacità native dell'agente (Antigravity `search_web` API) per esplorare internet in tempo reale alla ricerca delle ultime notizie.

### Direttive Fondamentali Aggiunte:
1. **Focus di Nicchia Espanso:** Oltre a mente e salute, i prompt ora abbracciano tematiche curiose e affascinanti (scienza, spazio, storia, tecnologia, paradossi) per stimolare maggiormente la curiosità.
2. **Memoria Anti-Duplicazione:** È stato introdotto il file di log `used_news_history.txt`. Prima di ogni generazione, il sistema inietta le ultime 20 notizie usate nel prompt dell'agente ordinandogli di ignorarle, impedendo che i task automatizzati peschino sempre le stesse news di tendenza.
3. **Modalità "Bastian Contrario":** È stata introdotta una logica contrariana (`video_bastian.sh` e `carousel_bastian.sh`). Usata con moderazione serve a creare hook polarizzanti, smontando i luoghi comuni.
4. **Formattazione Text-To-Speech (TTS):** I prompt obbligano l'AI a non usare virgolette, parentesi o simboli strani. Le fonti devono essere citate in modo discorsivo (es. "secondo la rivista Nature") per evitare che la voce sintetica legga a voce alta i segni di punteggiatura.
5. **Divieto di Clickbait Obsoleto:** È severamente vietato l'uso di formule ripetitive come "Scoperta assurda" o citazioni di mesi specifici (es. "scoperta di agosto") per mantenere il contenuto sempreverde e naturale.

## 6. AGGIORNAMENTO 2026-08-20: Transizione all'Agente Autonomo (Subagents & Scheduling)
L'orchestrazione è stata migrata verso un workflow agentico basato su `agente_autonomo.py` gestito nativamente dai task asincroni di Antigravity, limitando la dipendenza da script bash statici.
- **Subagents Integrati**:
  1. **`trend_hunter`**: Subagent esplorativo che seleziona dinamicamente le nicchie (Spazio, Fisica, Biologia, ecc.) e legge autonomamente `used_news_history.txt` per non creare contenuti doppi.
  2. **`revisore_editoriale`**: Subagent severo che esegue un controllo qualità (voto 1-10) del testo prima della generazione audio. Rifiuta testi sotto voto 8.
  3. **Self-Healing**: Il codice gestisce attivamente i crash di rete/API (es. Pexels down) in tempo reale fornendo argomenti e asset di fallback sicuri senza interruzioni.
- **Disabilitazione Linux Crontab**: Per delegare la responsabilità all'Agente, i vecchi lavori crontab di Linux sono stati commentati con `#` nel terminale. Lo schedule è stato replicato tramite il tool `schedule` interno dell'agente.

> **NOTA SULL'ARCHITETTURA IBRIDA (Salvaguardia Prompt):** Tutti i prompt lunghi e testati per le modalità (virale, promo, bastian) sono stati volutamente mantenuti dentro `rag_generator.py` e nei file `carousel_*.sh`. Il nuovo `agente_autonomo.py` funziona da orchestratore intelligente, leggendo il `--mode` e innescando gli script storici, assicurando così che nessuna istruzione di base vada mai persa.

**⚠️ ISTRUZIONI PER IL ROLLBACK ⚠️**
Se vuoi ritornare al vecchio sistema passivo senza IA orchestrante:
1. Esegui il comando `crontab -e` e togli il simbolo `# ` dalle righe per riattivare i cron di sistema.
2. Ripristina i file della cartella di backup: `/home/ubuntu/GIT/video_generator/backup_vecchio_sistema/`.
3. Chiedi a me (Antigravity) di aprire i Task in background attivi (o usare /manage_task) e fermare i miei Cron interni.
