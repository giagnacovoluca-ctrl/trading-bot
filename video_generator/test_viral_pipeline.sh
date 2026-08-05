#!/bin/bash
set -e
export PATH=$PATH:/home/ubuntu/.local/bin

cd /home/ubuntu/GIT/video_generator
source venv_video/bin/activate

echo "Inizio step 1"
python step1_voce.py --script scripts/script_generato.txt --voice assets/voices/mia_voce.wav

echo "Inizio step 2"
python step2_sfondo.py --audio temp/voiceover.mp3

echo "Inizio step 3"
python step3_sottotitoli.py --video temp/video_base.mp4 --audio temp/voiceover.mp3

echo "Inizio step 4"
python step4_pubblica.py --mode virale --script scripts/script_generato.txt

echo "Finito!"
