#!/bin/bash
export DISPLAY=:0
export XAUTHORITY=/home/ubuntu/.Xauthority
export PATH=$PATH:/home/ubuntu/.local/bin
cd /home/ubuntu/GIT/video_generator
source venv_video/bin/activate

HISTORY=$(tail -n 20 used_news_history.txt 2>/dev/null || echo "Nessuna notizia usata finora")

CATEGORIES=(
    "mente/psicologia"
    "neuroscienze"
    "fisica/spazio/astronomia"
    "biologia/evoluzione"
    "storia/archeologia"
    "tecnologia/IA/futuro"
    "matematica/paradossi"
    "filosofia"
    "economia comportamentale"
    "ambiente/natura"
    "sociologia/antropologia"
)

RANDOM_INDEX=$((RANDOM % ${#CATEGORIES[@]}))
CHOSEN_CATEGORY="${CATEGORIES[$RANDOM_INDEX]}"

/usr/local/bin/agy --add-dir /home/ubuntu/GIT/video_generator --dangerously-skip-permissions --print "@tiktok_video_generator Sei un maestro dello storytelling visivo. Genera un carosello TikTok ipnotico (5 o 6 slide).

TEMA OBBLIGATORIO: $CHOSEN_CATEGORY
NOTIZIE GIA' TRATTATE RECENTEMENTE (IGNORA ASSOLUTAMENTE QUESTE E TROVA ALTRO):
$HISTORY

REGOLE CONTENUTO: Il contenuto deve essere DIVULGATIVO e basato su FATTI VERIFICABILI con fonte citata.
ANTI-DISINFORMAZIONE: Verifica che la notizia provenga da una fonte credibile (Nature, Science, università, WHO, ESA, NASA, ecc.).
DIVIETO ESPLICITO: Vietati titoli clickbait falsi, esagerazioni di studi scientifici, trasformare correlazioni in causalità. NON usare 'SCOPERTA ASSURDA', 'SEGRETO NASCOSTO' o altri trigger di ban TikTok.

STEP 1: Usa il tool search_web per cercare su internet una notizia VERA, recente (degli ultimi 7 giorni) e molto discussa o controversa sul tema obbligatorio. Non ripetere le notizie già trattate.
STEP 2: Genera il carosello. Deve essere ESTREMAMENTE ORIGINALE E POLARIZZANTE (tecnica del Bastian Contrario). Analizza la notizia e proponi in modo logico una visione totalmente opposta alla massa, impopolare ma inattaccabile, SUPPORTATA DA DATI REALI (non speculativa). Distruggi i luoghi comuni e stravolgi la prospettiva. Usa un hook visivo. Nell'ultima slide fai una Call To Action forte per chiedere commenti. (Nessun accenno ad Amazon). 
STEP 3: DEVI usare il tool 'write_to_file' per salvare esattamente un array JSON in scripts/slides_carosello.json. 
STEP 4: Usa 'write_to_file' per scrivere una description con hashtag in scripts/tiktok_caption.txt.
STEP 5: Usa run_command o write_to_file per aggiungere in append il titolo o l'argomento della notizia a 'used_news_history.txt'." > cron_carousel.log 2>&1

set -e
python crea_carosello.py >> cron_carousel.log 2>&1
