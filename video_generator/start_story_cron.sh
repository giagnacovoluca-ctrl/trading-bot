#!/bin/bash
set -euo pipefail
exec 9>/tmp/video_generator_pipeline.lock
flock -n 9 || { echo "Pipeline già in esecuzione: salto questo run"; exit 0; }
export DISPLAY=:0
export PATH=$PATH:/home/ubuntu/.local/bin
cd /home/ubuntu/GIT/video_generator
PYTHON=/home/ubuntu/GIT/video_generator/venv_video/bin/python

echo "Avvio generazione e pubblicazione Storia IG - $(date)" >> cron_story.log
"$PYTHON" agente_story.py >> cron_story.log 2>&1
echo "Fine - $(date)" >> cron_story.log
