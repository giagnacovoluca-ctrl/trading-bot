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
*   **Caroselli:** `python crea_carosello.py` genera i testi tramite AGY CLI, crea 5 slide e produce un MP4. `step4_pubblica.py` carica il carosello/video usando Playwright.
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
Il vecchio e instabile `viral_news.py` è stato **deprecato**. Adesso il sistema si affida interamente ad AGY CLI locale e alle sue capacità di ricerca per individuare le notizie. Non è richiesta una chiave API LLM.

### Direttive Fondamentali Aggiunte:
1. **Focus di Nicchia Espanso:** Oltre a mente e salute, i prompt ora abbracciano tematiche curiose e affascinanti (scienza, spazio, storia, tecnologia, paradossi) per stimolare maggiormente la curiosità.
2. **Memoria Anti-Duplicazione:** È stato introdotto il file di log `used_news_history.txt`. Prima di ogni generazione, il sistema inietta le ultime 20 notizie usate nel prompt dell'agente ordinandogli di ignorarle, impedendo che i task automatizzati peschino sempre le stesse news di tendenza.
3. **Modalità "Bastian Contrario":** È stata introdotta una logica contrariana (`video_bastian.sh` e `carousel_bastian.sh`). Usata con moderazione serve a creare hook polarizzanti, smontando i luoghi comuni.
4. **Formattazione Text-To-Speech (TTS):** I prompt obbligano l'AI a non usare virgolette, parentesi o simboli strani. Le fonti devono essere citate in modo discorsivo (es. "secondo la rivista Nature") per evitare che la voce sintetica legga a voce alta i segni di punteggiatura.
5. **Divieto di Clickbait Obsoleto:** È severamente vietato l'uso di formule ripetitive come "Scoperta assurda" o citazioni di mesi specifici (es. "scoperta di agosto") per mantenere il contenuto sempreverde e naturale.

## 6. AGGIORNAMENTO 2026-08-22: Rollback a Linux Crontab
L'esperimento di migrazione dell'orchestrazione verso i task asincroni nativi di Antigravity (tramite il tool `schedule` e `agente_autonomo.py`) si è rivelato inaffidabile o non ottimale. Di conseguenza, il sistema è stato **ripristinato al crontab nativo di Linux**.

Attualmente, la pianificazione è governata nuovamente dagli script bash (es. `video_virale.sh`, `carousel_promo.sh`) che vengono richiamati direttamente dal cron di sistema agli orari stabiliti. Questi script, al loro interno, utilizzano l'interfaccia CLI di Antigravity per l'intelligenza artificiale, ma l'avvio temporizzato è demandato interamente al sistema operativo (Linux Cron) per garantire massima stabilità.

*Il tool interno `schedule` dell'agente non è più in uso per questo workflow.*

Se in futuro si vorranno sperimentare nuovamente orchestrazioni agentiche avanzate in background, andranno valutate architetture alternative o l'avvio stesso dell'agente autonomo dovrà essere triggerato dal crontab.

---

## 7. AGGIORNAMENTO 2026-08-23: Migliorie Qualità Contenuti & Anti-Ban TikTok

### 🚨 Problema Risolto: Ban TikTok per Disinformazione
Un video è stato rimosso da TikTok per disinformazione. L'analisi ha rivelato che il sistema generava titoli sensazionalistici e scientificamente inesatti (es. "il tuo sangue hackerava il cervello"). Cause identificate:
- Il prompt non aveva guardrail anti-disinformazione
- Il titolo hook di default era "SCOPERTA SHOCK"
- La history era contaminata da entry "SCOPERTA ASSURDA" ed "Errore di Generazione" che venivano reinieriate nei prompt

### Modifiche Implementate (23/08/2026):

#### `rag_generator.py` — Riscritto completamente
- **Anti-disinformazione**: Regole iniettate in tutti i prompt: vietato esagerare studi, vietato trasformare correlazioni in causalità, obbligo di citare fonte credibile
- **40+ topic** divisi in 12 categorie (neuroscienze, fisica/spazio, biologia, storia, tecnologia, matematica, filosofia, economia comportamentale, ambiente, sociologia)
- **Topic picker intelligente**: ruota le categorie evitando quelle usate nelle ultime 3 sessioni
- **Pulizia history**: filtra automaticamente le entry corrotte ("SCOPERTA ASSURDA", "Errore di Generazione") prima di iniettarle nel prompt
- **Ricerca virale migliorata**: notizie degli ultimi 7 giorni (non 24h) da fonti verificabili (Nature, Science, NASA, WHO, università)
- **Salvataggio history robusto**: salva `FONTE_NOTIZIA` non il titolo hook; in caso di errore salva `SKIP`

#### `agente_tiktok.py` — Aggiornato
- **Hook title di fallback**: più generico e neutro (non più "SCOPERTA SHOCK")
- **Cleanup immagini background**: elimina immagini generate dinamicamente dai run precedenti (prefissi `fallback_bg_`, `pexels_`) prima di ogni run, evitando il riuso di sfondi vecchi e ripetitivi
- **Notifiche Telegram**: funzione `notify_telegram()` invia alert su errori critici del cron (richiede `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` nel `.env`)
- **Log upload JSON**: ogni pubblicazione viene loggata in `output/upload_log.json` con timestamp, filename, mode, fonte_notizia, esito

#### `genera_immagini_carosello.py` — Aggiornato
- **Retry con backoff esponenziale** su Pollinations (2s, 4s, 6s prima di arrendersi)
- **Fallback AGY sincrono**: attendere il completamento del subprocess e verificare che il file esista
- **Fallback PIL garantito**: se tutto fallisce, genera uno sfondo gradient colorato con PIL (basato sul tema e sul numero slide) — ogni slide ha SEMPRE uno sfondo

#### `crea_carosello.py` — Aggiornato
- **Rilevamento automatico modalità**: controlla il `tiktok_caption.txt` per parole chiave ("link in bio", "Amazon") e imposta `--mode promo` o `virale` di conseguenza

#### Script Bash (`carousel_virale.sh`, `carousel_bastian.sh`, `carousel_promo.sh`) — Aggiornati
- **11 categorie tematiche** selezionate casualmente con `$RANDOM` ad ogni esecuzione
- **Anti-disinformazione** esplicita nel prompt: vietato clickbait, esagerazioni, titoli tipo "SCOPERTA ASSURDA"
- **Ricerca 7 giorni** invece di "ultime 24h" (che portava a invenzioni)
- **Carosello promo**: sceglie random tra i 3 ebook e fa divulgazione su un concetto specifico del libro selezionato

### Note per l'Agente:
- **NON usare mai** il titolo hook del video come `FONTE_NOTIZIA` — sono due cose diverse
- **SEMPRE verificare** che la fonte sia un ente accreditato prima di affermare qualcosa come fatto
- **Se una notizia non è verificabile**, sceglierne un'altra invece di inventare
- **I numeri vanno sempre in lettere** nel copione TTS (es. "tremila" non "3000")
- **Il tone of voice** deve essere "insider affascinato che condivide una scoperta reale", NON "guru che svela segreti nascosti"

---

## 8. AGGIORNAMENTO 2026-08-23: Salto di Qualità — M1-M10

### 🎯 Obiettivo: Miglioramento profondo di contenuto, estetica e sicurezza

#### M1 — Script a 5 atti (`rag_generator.py`)
Tutti i prompt (promo/virale/bastian) ora generano copioni con struttura narrativa obbligatoria:
- **ATTO 1 — HOOK** (15-20 parole): fatto contro-intuitivo che rompe aspettative
- **ATTO 2 — CONTESTO** (25-30 parole): rilevanza per la vita quotidiana
- **ATTO 3 — RIVELAZIONE** (40-50 parole): dato/studio con fonte verificata
- **ATTO 4 — COLPO DI SCENA** (15-20 parole): implicazione inaspettata
- **ATTO 5 — CTA** (10-15 parole): azione specifica concreta

#### M2 — Immagini contestualizzate per scena (`rag_generator.py`)
Le 7 immagini sfondo vengono generate DOPO il copione con prompt specifici per ogni atto:
- Immagine 1 → Atto 1 (shock visivo coerente con hook)
- Immagine 2 → Atto 2 (scena di vita quotidiana)
- Immagine 3 → Atto 3 (visualizzazione scientifica del dato)
- Immagine 4 → Atto 4 (immagine spiazzante/paradossale)
- Immagine 5 → Atto 5 (motivazionale/di azione)
- Immagine 6 → panoramica tematica
- Immagine 7 → chiusura brandizzata (sfondo scuro)
Tutti i prompt immagini sono in inglese, fotorealistici, ultra HD, cinematic lighting.

#### M4 — Template dark premium (`templates/carousel_dark.html`)
- Sfondo: `#0a0a0a → #1a1a2e`, font Inter, glassmorphism, glow ambientale radiale
- Usato automaticamente per `mode=dark` o `mode=bastian`

#### M5 — Template promo ebook (`templates/carousel_promo.html`)
- Slide hero: copertina ebook + badge BESTSELLER + CTA "Link in Bio"
- Slide benefit: checkmark oro, font Playfair Display, palette #D4A853
- Usato automaticamente per `mode=promo`

#### M7 — Ken Burns evoluto (`step2_sfondo.py`)
Ogni clip video ora applica una delle 4 varianti di movimento randomizzate:
- `zoom_in` (1.0 → 1.04), `zoom_out` (1.04 → 1.0), `pan_h` (pan orizzontale), `pan_v` (pan verticale)

#### M8 — Sottotitoli Hormozi evoluti (`modules/whisper_captions.py`)
- `font_size`: 60 → 78
- `stroke_width` parole non-correnti: 1 → 3
- Posizione: già nella metà inferiore (70% height) ✅
- Max parole per riga: già 3 ✅

#### M10 — Validatore qualità copione (`agente_tiktok.py`)
Prima di ogni pubblicazione, il copione viene valutato con score 0-10:
- Accuratezza scientifica: 40%
- Potenziale watch-time: 35%
- Originalità: 25%
- Se score < 7: rigenera automaticamente con `rag_generator.py --force-new` (max 1 retry)
- Il `quality_score` viene loggato in `output/upload_log.json` per ogni pubblicazione

---

## 9. AGGIORNAMENTO 2026-08-25: Self-Healing, Sottotitoli Ininterrotti e Validazione Titolo

### 🚨 Risoluzione Bug e Miglioramenti Resilienza
Il workflow andava in crash per errori imprevisti durante i passaggi chiave o produceva risultati esteticamente errati. Sono state implementate diverse patch correttive:

#### Sottotitoli Ininterrotti (`modules/whisper_captions.py`)
- **Fix "sottotitoli mancanti":** Precedentemente, l'ultima parola di una frase svaniva dopo 0.1 secondi, lasciando lo schermo vuoto durante le pause parlate. Ora la durata dell'ultima parola viene estesa fino all'inizio della parola successiva (con un cap di 1.5 secondi) per mantenere il testo sempre a schermo.

#### Estrazione Titolo Avanzata (`agente_tiktok.py` & `agente_autonomo.py`)
- **Fix regex Titolo:** L'AI a volte formattava il titolo con markdown (es. `**TITOLO:**` o `# TITOLO:`). Il vecchio `startswith("TITOLO:")` falliva e ripiegava erroneamente sulle prime 5 parole del video. Ora l'estrazione usa espressioni regolari (regex) robuste in grado di pulire e catturare sempre il vero titolo (Hook).

#### Auto-Riparazione Live / Self-Healing (`agente_tiktok.py` & `agente_autonomo.py`)
Invece di usare `sys.exit()` in caso di fallimenti nei sub-processi, i passi di rendering ora catturano l'errore e tentano fallback automatici:
- **Step 1 (Voce):** Se `xtts` fallisce o il file audio generato è corrotto (< 1000 bytes), scatta in automatico il fallback su `edge-tts`.
- **Step 2 (Sfondo):** Se la composizione fallisce, viene ripetuta la generazione senza immagini custom (solo sfondi locali sicuri).
- **Step 3 (Sottotitoli):** Se `whisper_timestamped` crasha o il file finale è troppo piccolo, il sistema riprova in fallback base.
- **Step 0.5 (Pollinations):** Se Pexels restituisce meno di 3 immagini o va in timeout, il sistema genera gli sfondi mancanti utilizzando **Pollinations AI** (modello `flux` verticale e ottimizzato per l'estetica TikTok).

#### Validazione Qualità Estesa al Titolo (`agente_tiktok.py`)
- **`valida_qualita_copione` aggiornato:** Prima il validatore Gemini leggeva *solo* il copione, ignorando l'hook a schermo (Titolo). Ora riceve nel prompt anche l'`hook_title` e il peso del prompt è stato ridistribuito (30% assegnato specificamente al potenziale "ipnotico" del Titolo). Se il titolo non è d'impatto, viene bocciato e rigenerato.

## 10. AGGIORNAMENTO 2026-08-25: Instagram API, Orchestratore Caroselli e Reel Estetici

### 📸 Pubblicazione Diretta su Instagram (API Ufficiali)
Il caricamento su Instagram ora è completamente slegato dall'automazione fragile del browser (Playwright) e avviene nativamente tramite le **API Graph di Meta** (`step4_pubblica_ig_api.py`).
- **Nessun limite di sicurezza:** Meta accetta nativamente i file serviti tramite un mini web-server Python temporaneo (su porta dinamica locale).
- **Integrazione Caroselli & Reels:** Tutti i video generati dal bot (inclusi i caroselli `crea_carosello.py`) vengono ora caricati automaticamente sia su TikTok (via Playwright) sia su Instagram (via API).

### 🤖 Agente Direttore per Caroselli (`agente_carosello.py`)
I vecchi script bash "ciechi" (`carousel_promo.sh`, `carousel_virale.sh`, ecc.) sono stati sostituiti dal nuovo **Agente Direttore Python** (`agente_carosello.py`).
- **Live Review (Revisore JSON):** Prima di renderizzare le immagini, l'agente esamina il testo prodotto da AGY CLI per assicurarsi che:
  1. Il file JSON sia formattato correttamente.
  2. Le slide non abbiano "muri di testo" ma frasi brevi (max 15 parole).
  3. Il testo sia logicamente collegato dalla slide 1 alla 6, garantendo un'esperienza di lettura fluida senza audio.
- **Gestione Errori:** Se l'AI hallucina, l'agente cancella il JSON rotto e lo fa rigenerare fino a 3 volte.

### 🎭 Nuovo Format: Reel Estetici (Quiet Luxury)
Aggiunta la modalità `--mode aesthetic` per Instagram.
- **Magia CSS:** Invece di usare API per generare sfondi o sprecare limiti di scaricamento, il sistema usa Playwright per renderizzare un **Template HTML/CSS premium** (`ig_aesthetic.html`) con gradienti scuri in movimento e tipografia elegante.
- **Ken Burns Cinematico:** L'immagine generata viene animata tramite `FFMPEG` con uno zoom lentissimo (dal 100% al 110% in 6 secondi) combinato a musica lo-fi o drammatica.
- **Palettes Dinamiche:** Il codice CSS si adatta in base all'argomento del Reel (verde per cibo, blu profondo per mente, ecc.).
- **Strategia Anti-Spam:** Programmati nel Crontab per le 11:00, 16:00 e 22:30, per un totale di 6 contenuti giornalieri ottimizzati per l'algoritmo Reels di Instagram.

## 11. AGGIORNAMENTO 2026-08-27: Ecosistema Instagram Nativi, Storie e Conscia-Mente

### 🚀 Nuovo Ecosistema Instagram Nativi
- **Caroselli Immagini (Nativi):** Creato `step4_pubblica_ig_carousel_api.py`. Invece di limitarci ai Reel MP4, il sistema carica automaticamente le grafiche generate come Caroselli Fotografici Nativi tramite le API Graph di Meta, aumentando le probabilità di Salvataggio.
- **Storie Giornaliere Automatizzate:** Creato `agente_story.py`. Genera sfondi dark aesthetic verticali, estrae da AGY una mini-frase ad altissimo impatto (max 6 parole) e stampa un finto "bottone CTA". Utilizza Pillow con ancoraggio centrale perfetto (`anchor="mm"`) e calcolo delle zone sicure (safe zones) per evitare l'intersezione con la UI di Instagram.

### 🤖 CTA Automatizzate per ManyChat
- Modificati i prompt core in `agente_carosello.py` e negli script aesthetic. Eliminata la dicitura "Link in bio" (per IG) in favore di CTA mirate ai DM: *"Commenta GUIDA e te lo mando nei DM"*. Questo favorisce l'intervento di bot esterni (es. ManyChat) che moltiplicano l'engagement.

### 🔮 Funnel Conscia-Mente (Oracolo & Numerologia)
- **Agente Dedicato:** Creato `agente_cosciamente.py`. Genera contenuti esoterici per i due pillar del sito Conscia-Mente.
- **Cross-Platform Ibrido:** Se indirizzato a TikTok, pubblica un Video MP4 con musica. Se indirizzato a Instagram, sfrutta le nuove API pubblicando un Carosello Fotografico Swipeabile nativo.
- **Crontab Intelligente:** `cron_cosciamente.sh` controlla il giorno dell'anno. Nei giorni Pari: TikTok->Oracolo, IG->Numerologia. Nei giorni Dispari l'inverso.

### ⚖️ Spaziatura Crontab Definitiva
- **Risoluzione Sovraccarico VPS:** I rendering video TTS + Playwright consumano enormi risorse. Il crontab è stato completamente riscritto garantendo una finestra *minima* di 45-60 minuti tra l'avvio di qualsiasi processo pesante e l'altro, prevenendo crash `OOM` (Out Of Memory) ed evitando i filtri anti-spam dei social.
