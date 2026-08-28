#!/bin/bash
set -euo pipefail
exec 9>/tmp/video_generator_pipeline.lock
flock -n 9 || { echo "Pipeline già in esecuzione: salto questo run"; exit 0; }
# Script automatico per avviare la generazione e caricamento del video tramite Antigravity in background

export DISPLAY=:0
export XAUTHORITY=/home/ubuntu/.Xauthority
export PATH=$PATH:/home/ubuntu/.local/bin:/usr/local/bin

cd /home/ubuntu/GIT/video_generator

# Scegli la modalità in base all'ora corrente per mantenere la programmazione specifica
CURRENT_HOUR=$(date +%H)
SELECTED_MODE="virale" # fallback

if [ "$CURRENT_HOUR" == "00" ] || [ "$CURRENT_HOUR" == "08" ] || [ "$CURRENT_HOUR" == "16" ]; then
    SELECTED_MODE="promo"
elif [ "$CURRENT_HOUR" == "12" ]; then
    SELECTED_MODE="bastian"
elif [ "$CURRENT_HOUR" == "04" ] || [ "$CURRENT_HOUR" == "20" ]; then
    SELECTED_MODE="virale"
fi

echo "---" >> cron_agy.log
echo "Avvio CRON: agente_tiktok.py in modalità: $SELECTED_MODE" >> cron_agy.log

venv_video/bin/python agente_tiktok.py --mode "$SELECTED_MODE" >> cron_agy.log 2>&1
