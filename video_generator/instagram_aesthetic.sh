#!/bin/bash
set -euo pipefail
exec 9>/tmp/video_generator_pipeline.lock
flock -n 9 || { echo "Pipeline già in esecuzione: salto questo run"; /home/ubuntu/GIT/video_generator/venv_video/bin/python -c 'from modules.email_notifications import notify_content_status; notify_content_status("not_published", "Reel aesthetic", "Instagram", reason="pipeline già in esecuzione")' || true; exit 0; }
export DISPLAY=:0
export PATH=$PATH:/home/ubuntu/.local/bin
cd /home/ubuntu/GIT/video_generator

PYTHON=/home/ubuntu/GIT/video_generator/venv_video/bin/python
LOG_FILE=/home/ubuntu/GIT/video_generator/cron_aesthetic.log

# Un solo percorso editoriale: selezione, generazione, revisione, rendering,
# pubblicazione e tracciamento sono gestiti da agente_carosello.py.
echo "Avvio pipeline Instagram aesthetic - $(date)" > "$LOG_FILE"
"$PYTHON" agente_carosello.py --mode aesthetic >> "$LOG_FILE" 2>&1
echo "Pipeline Instagram aesthetic completata - $(date)" >> "$LOG_FILE"
