#!/bin/bash
export DISPLAY=:0
export XAUTHORITY=/home/ubuntu/.Xauthority
export PATH=$PATH:/home/ubuntu/.local/bin
cd /home/ubuntu/GIT/video_generator
source venv_video/bin/activate

EBOOKS=(
    "Cibo/Salute"
    "Meditazione"
    "Integratori"
)

RANDOM_INDEX=$((RANDOM % ${#EBOOKS[@]}))
CHOSEN_EBOOK="${EBOOKS[$RANDOM_INDEX]}"

/usr/local/bin/agy --add-dir /home/ubuntu/GIT/video_generator --dangerously-skip-permissions --print "@tiktok_video_generator Sei un social media manager di altissimo livello. Genera un carosello TikTok (5 o 6 slide) per promuovere uno dei miei Ebook su Amazon. Il libro scelto per questo video è: '${CHOSEN_EBOOK}'.

REGOLE CONTENUTO: Fai divulgazione concreta dal libro. Non limitarti a promettere un segreto in modo generico, ma cita e spiega un CONCETTO SPECIFICO tratto dal libro scelto in modo intellettualmente stimolante. Usa un hook visivo fortissimo, rendi il concetto chiaro e logico.
Nell'ultima slide chiedi di scaricare il libro (link in bio).

STEP 1: Salva esattamente un array JSON in scripts/slides_carosello.json. 
STEP 2: Scrivi anche una description persuasiva con hashtag in scripts/tiktok_caption.txt." > cron_carousel.log 2>&1

set -e
python crea_carosello.py >> cron_carousel.log 2>&1
