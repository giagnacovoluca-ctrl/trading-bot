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

## Workflow Caroselli (Novità)
Oltre ai video classici, il sistema gestisce la pubblicazione 100% automatica di caroselli motivazionali.
*   **Script Principale:** `python crea_carosello.py`
*   **Funzionamento:** Genera testi con Gemini, crea 5 slide 9:16 con testo gigante (120/160px) sovrapposto allo sfondo `assets/backgrounds/carousel_bg.jpg`, e applica una colonna sonora automatica presa a caso da `assets/ambient/`. Infine crea un MP4 da 15 secondi per TikTok.
*   **Pubblicazione Automatica:** `python step4_pubblica.py` carica il video/carosello tramite Playwright bypassando i limiti API.

## Automazione (Cronjob)
Il file `current_cron.txt` contiene i timing esatti per la pubblicazione autonoma:
- **Video Standard (`start_bot_cron.sh`)**: Ogni 4 ore (00, 04, 08, 12, 16, 20).
- **Caroselli (`start_carousel_cron.sh`)**: Sfalsati di 2 ore (02, 06, 10, 14, 18, 22). Il bash script aggiorna prima l'immagine di sfondo usando lo strumento Antigravity CLI e poi lancia lo script Python.

## Novità Architetturali Recenti (Ricerca Web & Tematiche)
Il vecchio e instabile `viral_news.py` è stato **deprecato**. Adesso il sistema si affida interamente alle capacità native dell'agente (Antigravity `search_web` API) per esplorare internet in tempo reale alla ricerca delle ultime notizie.

### Direttive Fondamentali Aggiunte:
1. **Focus di Nicchia Espanso:** Oltre a mente e salute, i prompt ora abbracciano tematiche curiose e affascinanti (scienza, spazio, storia, tecnologia, paradossi) per stimolare maggiormente la curiosità.
2. **Memoria Anti-Duplicazione:** È stato introdotto il file di log `used_news_history.txt`. Prima di ogni generazione, il sistema inietta le ultime 20 notizie usate nel prompt dell'agente ordinandogli di ignorarle, impedendo che i task automatizzati peschino sempre le stesse news di tendenza.
3. **Modalità "Bastian Contrario":** È stata introdotta una logica contrariana (`video_bastian.sh` e `carousel_bastian.sh`). Usata con moderazione serve a creare hook polarizzanti, smontando i luoghi comuni.
4. **Formattazione Text-To-Speech (TTS):** I prompt obbligano l'AI a non usare virgolette, parentesi o simboli strani. Le fonti devono essere citate in modo discorsivo (es. "secondo la rivista Nature") per evitare che la voce sintetica legga a voce alta i segni di punteggiatura.
5. **Divieto di Clickbait Obsoleto:** È severamente vietato l'uso di formule ripetitive come "Scoperta assurda" o citazioni di mesi specifici (es. "scoperta di agosto") per mantenere il contenuto sempreverde e naturale.
