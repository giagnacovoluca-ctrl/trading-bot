#!/bin/bash
export DISPLAY=:0
export XAUTHORITY=/home/ubuntu/.Xauthority
export PATH=$PATH:/home/ubuntu/.local/bin
cd /home/ubuntu/GIT/video_generator
source venv_video/bin/activate

HISTORY=$(tail -n 20 used_news_history.txt 2>/dev/null || echo "Nessuna notizia usata finora")

/usr/local/bin/agy --add-dir /home/ubuntu/GIT/video_generator --dangerously-skip-permissions --print "@tiktok_video_generator Sei un maestro dello storytelling visivo. Genera un carosello TikTok ipnotico (5 o 6 slide).

TEMA OBBLIGATORIO: Mente, psicologia, salute, neuroscienze, crescita personale o nutrizione.
NOTIZIE GIA' TRATTATE RECENTEMENTE (IGNORA ASSOLUTAMENTE QUESTE E TROVA ALTRO):
$HISTORY

STEP 1: Usa il tool search_web per cercare su internet una notizia VERA, recentissima (ultime 24 ore) e molto discussa o controversa sul tema obbligatorio. Non ripetere le notizie già trattate.
STEP 2: Genera il carosello. Deve essere ESTREMAMENTE ORIGINALE E POLARIZZANTE (tecnica del Bastian Contrario). Analizza la notizia e proponi in modo logico una visione totalmente opposta alla massa, impopolare ma inattaccabile. Distruggi i luoghi comuni e stravolgi la prospettiva. Usa un hook visivo. Nell'ultima slide fai una Call To Action forte per chiedere commenti. (Nessun accenno ad Amazon). 
STEP 3: DEVI usare il tool 'write_to_file' per salvare esattamente un array JSON in scripts/slides_carosello.json. 
STEP 4: Usa 'write_to_file' per scrivere una description con hashtag in scripts/tiktok_caption.txt.
STEP 5: Usa run_command o write_to_file per aggiungere in append il titolo o l'argomento della notizia a 'used_news_history.txt'." > cron_carousel.log 2>&1

set -e
python crea_carosello.py >> cron_carousel.log 2>&1
