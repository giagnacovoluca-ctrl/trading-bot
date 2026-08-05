#!/bin/bash
export DISPLAY=:0
export XAUTHORITY=/home/magic/.Xauthority
export PATH=$PATH:/home/magic/.local/bin
cd /home/magic/Scrivania/code/GIT/video_generator
source venv_video/bin/activate

/home/magic/.local/bin/agy --add-dir /home/magic/Scrivania/code/GIT/video_generator --dangerously-skip-permissions --print "@tiktok_video_generator Sei un social media manager di altissimo livello. Genera un carosello TikTok virale e intellettualmente stimolante (5 o 6 slide) per promuovere uno dei miei Ebook su Amazon ('Come usare il cibo...', 'Meditazione per chiunque', o 'Tra Scienza e Intuizione'). Usa un hook visivo fortissimo, fai divulgazione svelando un segreto profondo e controintuitivo ma super logico, e nell'ultima slide chiedi di scaricare il libro (link in bio). Salva esattamente un array JSON in scripts/slides_carosello.json. Scrivi anche una description persuasiva con hashtag in scripts/tiktok_caption.txt." > cron_carousel.log 2>&1

set -e
python crea_carosello.py >> cron_carousel.log 2>&1
