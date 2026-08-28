#!/bin/bash
set -uo pipefail
exec 9>/tmp/video_generator_pipeline.lock
flock -n 9 || { echo "Pipeline già in esecuzione: salto questo run"; exit 0; }
export DISPLAY=:0
export XAUTHORITY=/home/ubuntu/.Xauthority
export PATH=$PATH:/home/ubuntu/.local/bin:/usr/local/bin

cd /home/ubuntu/GIT/video_generator

echo "---" >> cron_agy.log
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
echo "[$TIMESTAMP] Avvio CRON: agente_tiktok.py in modalità: virale" >> cron_agy.log
venv_video/bin/python agente_tiktok.py --mode virale >> cron_agy.log 2>&1
EXIT_CODE=$?
echo "[$TIMESTAMP] Esecuzione completata con codice di uscita: $EXIT_CODE" >> cron_agy.log
exit "$EXIT_CODE"
