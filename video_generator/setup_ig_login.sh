#!/bin/bash
# ============================================================
# setup_ig_login.sh
# Avvia VNC + Chrome per fare login Instagram una volta sola.
# Poi il profilo viene salvato e usato automaticamente.
# ============================================================

set -e

PROFILE_DIR="$(pwd)/chrome_profile_ig"
DISPLAY_NUM=":98"
VNC_PORT=5998
NOVNC_PORT=6081

echo "============================================"
echo "  Instagram Login Setup — Profilo Persistente"
echo "============================================"
echo ""

# Crea cartella profilo se non esiste
mkdir -p "$PROFILE_DIR"

# Ferma eventuali processi precedenti
echo "[1/4] Pulizia processi precedenti..."
pkill -f "Xvfb $DISPLAY_NUM" 2>/dev/null || true
pkill -f "x11vnc.*$VNC_PORT" 2>/dev/null || true
pkill -f "websockify.*$NOVNC_PORT" 2>/dev/null || true
sleep 1

# Avvia display virtuale
echo "[2/4] Avvio display virtuale ($DISPLAY_NUM)..."
Xvfb $DISPLAY_NUM -screen 0 1280x900x24 &
XVFB_PID=$!
sleep 2

# Avvia VNC server
echo "[3/4] Avvio VNC server (porta $VNC_PORT)..."
x11vnc -display $DISPLAY_NUM -nopw -listen localhost -rfbport $VNC_PORT -xkb -forever -shared -bg -quiet
sleep 1

# Avvia noVNC (browser web per connettersi al VNC)
echo "[4/4] Avvio noVNC (porta $NOVNC_PORT)..."
websockify --web=/usr/share/novnc/ 127.0.0.1:$NOVNC_PORT localhost:$VNC_PORT &
NOVNC_PID=$!
sleep 1

echo ""
echo "============================================"
echo "  ✅ VNC PRONTO!"
echo "============================================"
echo ""
echo "  Collegati esclusivamente tramite port-forwarding SSH:"
echo "     ssh -L $NOVNC_PORT:localhost:$NOVNC_PORT ubuntu@$(hostname -I | awk '{print $1}')"
echo "     poi: http://localhost:$NOVNC_PORT/vnc.html"
echo ""
echo "  Apertura Chrome su Instagram..."
echo "  Fai login (spunta 'Salva le informazioni di accesso'), poi chiudi Chrome quando hai finito."
echo ""

# Apri Chrome con profilo persistente direttamente su Instagram
DISPLAY=$DISPLAY_NUM google-chrome \
    --user-data-dir="$PROFILE_DIR" \
    --no-first-run \
    --no-default-browser-check \
    --window-size=1280,900 \
    --disable-blink-features=AutomationControlled \
    --exclude-switches=enable-automation \
    --no-sandbox \
    --disable-gpu \
    --disable-dev-shm-usage \
    https://www.instagram.com/ &
CHROME_PID=$!

echo "  Chrome avviato (PID: $CHROME_PID)"
echo "  Connettiti al VNC e fai login su Instagram."
echo ""
echo "  Premi INVIO quando hai finito il login per salvare e chiudere..."
read -r

# Chiudi tutto
echo ""
echo "Salvataggio profilo e chiusura..."
kill $CHROME_PID 2>/dev/null || true
sleep 2
kill $NOVNC_PID 2>/dev/null || true
pkill -f "x11vnc.*$VNC_PORT" 2>/dev/null || true
kill $XVFB_PID 2>/dev/null || true

echo ""
echo "✅ Profilo salvato in: $PROFILE_DIR"
echo "   Da ora gli upload Instagram useranno questo profilo automaticamente!"
echo ""
