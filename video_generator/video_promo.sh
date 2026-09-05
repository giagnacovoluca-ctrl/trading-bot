#!/bin/bash
set -euo pipefail
exec 9>/tmp/video_generator_pipeline.lock
flock -n 9 || { echo "Pipeline già in esecuzione: salto questo run"; /home/ubuntu/GIT/video_generator/venv_video/bin/python -c 'from modules.email_notifications import notify_content_status; notify_content_status("not_published", "video promo", "TikTok", reason="pipeline già in esecuzione")' || true; exit 0; }
export DISPLAY=:0
export XAUTHORITY=/home/ubuntu/.Xauthority
export PATH=$PATH:/home/ubuntu/.local/bin:/usr/local/bin

cd /home/ubuntu/GIT/video_generator

echo "---" >> cron_agy.log
echo "Avvio CRON: agente_tiktok.py in modalità: promo" >> cron_agy.log
./run_agent_until_publish.sh promo >> cron_agy.log 2>&1
