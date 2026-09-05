#!/usr/bin/env bash
set -euo pipefail
exec 9>/tmp/video_generator_pipeline.lock
flock -n 9 || {
  echo "Pipeline già in esecuzione: salto carosello"
  /home/ubuntu/GIT/video_generator/venv_video/bin/python -c 'from modules.email_notifications import notify_content_status; notify_content_status("not_published", "carosello virale", "TikTok e Instagram", reason="pipeline già in esecuzione")' || true
  exit 0
}
cd /home/ubuntu/GIT/video_generator
exec /home/ubuntu/GIT/video_generator/venv_video/bin/python agente_carosello.py --mode virale
