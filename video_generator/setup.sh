#!/usr/bin/env bash
# Setup completo per video_generator
# Uso: bash setup.sh

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Video Generator Setup ==="

# 1. FFmpeg (richiesto da moviepy)
if ! command -v ffmpeg &>/dev/null; then
    echo "→ Installo FFmpeg..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y ffmpeg
    elif command -v brew &>/dev/null; then
        brew install ffmpeg
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y ffmpeg
    else
        echo "⚠ Installa FFmpeg manualmente: https://ffmpeg.org/download.html"
    fi
else
    echo "✓ FFmpeg già disponibile: $(ffmpeg -version 2>&1 | head -1)"
fi

# 2. ImageMagick (richiesto da moviepy TextClip per le caption)
if ! command -v convert &>/dev/null; then
    echo "→ Installo ImageMagick..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y imagemagick
    elif command -v brew &>/dev/null; then
        brew install imagemagick
    else
        echo "⚠ Caption disabilitate senza ImageMagick"
    fi
else
    echo "✓ ImageMagick disponibile"
fi

# 3. Virtualenv Python
PYTHON="${PYTHON:-python3.12}"
if [ ! -d "venv_video" ]; then
    echo "→ Creo virtualenv con $PYTHON..."
    $PYTHON -m venv venv_video
fi
source venv_video/bin/activate
echo "✓ Virtualenv attivato: $(python --version)"

# 4. Pip upgrade + dipendenze
pip install --upgrade pip -q
pip install -r requirements.txt

# 5. Env file
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "→ Creato .env (compila le chiavi API se necessario)"
fi

# 6. Font Montserrat (caption bold)
FONT_DIR="assets/fonts"
FONT_FILE="$FONT_DIR/Montserrat-Bold.ttf"
if [ ! -f "$FONT_FILE" ]; then
    echo "→ Scarico Montserrat-Bold..."
    FONT_URL="https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-Bold.ttf"
    curl -sL "$FONT_URL" -o "$FONT_FILE" && echo "✓ Font scaricato" || echo "⚠ Download font fallito, userà font di sistema"
fi

echo ""
echo "=== Setup completato ==="
echo ""
echo "Comandi disponibili (attiva prima: source venv_video/bin/activate):"
echo ""
echo "  # Test TTS (verifica che la voce funzioni)"
echo "  python main.py test-tts 'Ciao, questo è un test.'"
echo ""
echo "  # Genera video base (16:9 YouTube)"
echo "  python main.py generate --script scripts/esempio_base.txt --output output/test.mp4 --ratio 169"
echo ""
echo "  # Genera video TikTok (9:16)"
echo "  python main.py generate --script scripts/esempio_base.txt --output output/tiktok.mp4 --ratio 916"
echo ""
echo "  # Dry run (mostra parsing senza generare)"
echo "  python main.py generate --script scripts/esempio_base.txt --dry-run"
echo ""
echo "  # Lista voci italiane edge-tts"
echo "  python main.py list-voices"
