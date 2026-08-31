#!/bin/bash
set -euo pipefail

exec 9>/tmp/video_generator_pipeline.lock
flock -n 9 || { echo "Pipeline già in esecuzione: salto questo run"; exit 0; }
export DISPLAY=:0
export PATH=$PATH:/home/ubuntu/.local/bin
cd /home/ubuntu/GIT/video_generator

PYTHON=/home/ubuntu/GIT/video_generator/venv_video/bin/python
LOG_FILE=/home/ubuntu/GIT/video_generator/cron_cosciamente.log
RUN_MODE=${1:-both}
if [ "$RUN_MODE" != "both" ] && [ "$RUN_MODE" != "--ig-only" ]; then
    echo "Uso: $0 [--ig-only]" >&2
    exit 2
fi
trap 'status=$?; echo "ERRORE job Conscia-Mente (exit $status) - $(date)" >> "$LOG_FILE"; exit $status' ERR

# Ottieni il giorno dell'anno (1-365)
DAY_OF_YEAR=$(date +%j)
IS_EVEN=$((10#$DAY_OF_YEAR % 2))

echo "Avvio job Conscia-Mente (Giorno dell'anno: $DAY_OF_YEAR) - $(date)" >> "$LOG_FILE"

if [ "$IS_EVEN" -eq 0 ]; then
    echo "Giorno PARI: TikTok -> Oracolo | IG -> Numerologia" >> "$LOG_FILE"

    if [ "$RUN_MODE" = "both" ]; then
        # 1. TikTok: Oracolo
        "$PYTHON" agente_cosciamente.py --prodotto oracolo --piattaforma tiktok >> "$LOG_FILE" 2>&1
        sleep 60
    fi

    # 2. Instagram: Numerologia
    "$PYTHON" agente_cosciamente.py --prodotto numerologia --piattaforma ig >> "$LOG_FILE" 2>&1

else
    echo "Giorno DISPARI: TikTok -> Numerologia | IG -> Oracolo" >> "$LOG_FILE"

    if [ "$RUN_MODE" = "both" ]; then
        # 1. TikTok: Numerologia
        "$PYTHON" agente_cosciamente.py --prodotto numerologia --piattaforma tiktok >> "$LOG_FILE" 2>&1
        sleep 60
    fi

    # 2. Instagram: Oracolo
    "$PYTHON" agente_cosciamente.py --prodotto oracolo --piattaforma ig >> "$LOG_FILE" 2>&1
fi

trap - ERR
echo "Job Conscia-Mente terminato! - $(date)" >> "$LOG_FILE"
