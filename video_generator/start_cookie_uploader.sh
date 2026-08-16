#!/bin/bash
# Avvia il Cookie Uploader Server in background
# Uso: ./start_cookie_uploader.sh [porta]

PORT=${1:-8888}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/venv_video/bin/python"
LOG="$SCRIPT_DIR/temp/cookie_uploader.log"

# Crea cartella log se non esiste
mkdir -p "$SCRIPT_DIR/temp"

# Termina eventuale istanza precedente
pkill -f "cookie_uploader.py" 2>/dev/null
sleep 1

# Avvia
echo "🍪 Avvio Cookie Uploader su porta $PORT..."
nohup "$VENV" "$SCRIPT_DIR/cookie_uploader.py" "$PORT" > "$LOG" 2>&1 &
echo "PID: $!"
sleep 2

# Verifica
if curl -s -o /dev/null -w "%{http_code}" http://localhost:$PORT/ | grep -q "200"; then
    echo "✅ Server attivo!"
    echo "📱 Accedi dal telefono: http://141.94.79.16:$PORT"
    echo "🔑 Password: ${COOKIE_UPLOAD_PASSWORD:-tiktok2024}"
    echo "📋 Log: $LOG"
else
    echo "❌ Il server non risponde. Controlla il log:"
    tail -20 "$LOG"
fi
