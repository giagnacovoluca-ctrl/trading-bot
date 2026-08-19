#!/bin/bash
export DISPLAY=:0
export XAUTHORITY=/home/ubuntu/.Xauthority
export PATH=$PATH:/home/ubuntu/.local/bin
cd /home/ubuntu/GIT/video_generator
source venv_video/bin/activate

HOOK_TITLE=$(grep -E "^TITOLO:" scripts/script_generato.txt | head -n 1 | sed -e 's/^TITOLO: *//' -e 's/ *$//')
if [ -z "$HOOK_TITLE" ]; then HOOK_TITLE="LA VERITÀ NASCOSTA"; fi

set -e
python step1_voce.py --script scripts/script_generato.txt --voice assets/voices/mia_voce.wav --provider xtts >> cron_agy.log 2>&1

find assets/backgrounds -type f -size 0 -delete
IMAGES=$(find assets/backgrounds -type f -mmin -30 | tr '\n' ' ')
if [ -n "$IMAGES" ]; then
    python step2_sfondo.py --audio temp/voiceover.mp3 --topic "notizia curiosità scienza" --images $IMAGES >> cron_agy.log 2>&1
else
    python step2_sfondo.py --audio temp/voiceover.mp3 --topic "notizia curiosità scienza" >> cron_agy.log 2>&1
fi

python step3_sottotitoli.py --video temp/video_base.mp4 --audio temp/voiceover.mp3 --hook_title "$HOOK_TITLE" >> cron_agy.log 2>&1
python step4_pubblica.py --mode virale --script scripts/script_generato.txt >> cron_agy.log 2>&1
