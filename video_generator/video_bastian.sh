#!/bin/bash
set -euo pipefail
exec 9>/tmp/video_generator_pipeline.lock
flock -n 9 || { echo "Pipeline già in esecuzione: salto questo run"; exit 0; }
export DISPLAY=:0
export XAUTHORITY=/home/ubuntu/.Xauthority
export PATH=$PATH:/home/ubuntu/.local/bin:/usr/local/bin

cd /home/ubuntu/GIT/video_generator

echo "---" >> cron_agy.log
echo "Avvio CRON: agente_tiktok.py in modalità: bastian" >> cron_agy.log
venv_video/bin/python agente_tiktok.py --mode bastian >> cron_agy.log 2>&1
