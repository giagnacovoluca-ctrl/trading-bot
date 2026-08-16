#!/bin/bash
# ============================================================
# setup_tiktok_login.sh
# Avvia VNC + Chrome per fare login TikTok una volta sola.
# Poi il profilo viene salvato e usato automaticamente.
#
# USO:
#   ./setup_tiktok_login.sh
#
# Dal tuo PC/telefono: apri http://<IP-VPS>:6080 nel browser
# oppure su Termius:
#   ssh -L 6080:localhost:6080 ubuntu@<IP-VPS>
#   poi apri http://localhost:6080
# ============================================================

set -e

PROFILE_DIR="$(pwd)/chrome_profile"
DISPLAY_NUM=":99"
VNC_PORT=5999
NOVNC_PORT=6080

echo "============================================"
echo "  TikTok Login Setup — Profilo Persistente"
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
websockify --web=/usr/share/novnc/ $NOVNC_PORT localhost:$VNC_PORT &
NOVNC_PID=$!
sleep 1

echo ""
echo "============================================"
echo "  ✅ VNC PRONTO!"
echo "============================================"
echo ""
echo "  👉 Apri nel browser:"
echo "     http://$(hostname -I | awk '{print $1}'):$NOVNC_PORT/vnc.html"
echo ""
echo "  Oppure da Termius (port-forwarding):"
echo "     ssh -L $NOVNC_PORT:localhost:$NOVNC_PORT ubuntu@$(hostname -I | awk '{print $1}')"
echo "     poi: http://localhost:$NOVNC_PORT/vnc.html"
echo ""
echo "  Apertura Chrome su TikTok..."
echo "  Fai login, poi chiudi Chrome quando hai finito."
echo ""

# Apri Chrome con profilo persistente direttamente su TikTok
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
    https://www.tiktok.com/login &
CHROME_PID=$!

echo "  Chrome avviato (PID: $CHROME_PID)"
echo "  Connettiti al VNC e fai login su TikTok."
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
echo "   Da ora gli upload TikTok useranno questo profilo automaticamente!"
echo ""
