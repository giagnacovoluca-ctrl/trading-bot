#!/bin/bash
# Script automatico per avviare la generazione e caricamento del carosello
# in background

export DISPLAY=:0
export XAUTHORITY=/home/ubuntu/.Xauthority
export PATH=$PATH:/home/ubuntu/.local/bin

cd /home/ubuntu/GIT/video_generator

# Attiva il virtual environment se esiste (opzionale ma consigliato se si usano pacchetti pip)
if [ -d "venv_video" ]; then
    source venv_video/bin/activate
fi

source venv_video/bin/activate

# Fai generare e salvare i JSON da Antigravity nel workspace
/usr/local/bin/agy --add-dir /home/ubuntu/GIT/video_generator --dangerously-skip-permissions --print "Sei un social media manager. Genera le 6 slide per un carosello TikTok virale su una curiosità a tua scelta (non usare argomenti vecchi). Salvale esattamente in scripts/slides_carosello.json." > cron_carousel.log 2>&1

set -e
# Avvia la creazione
python crea_carosello.py >> cron_carousel.log 2>&1
