#!/bin/bash
cd /home/ubuntu/GIT/video_generator
source venv_video/bin/activate
echo "Step 1: Voce..."
python step1_voce.py --script scripts/script_ereader.txt --output temp/voice.mp3 --provider xtts
echo "Step 2: Sfondo..."
python step2_sfondo.py --audio temp/voice.mp3 --output temp/video_base.mp4
echo "Step 3: Sottotitoli..."
python step3_sottotitoli.py --video temp/video_base.mp4 --audio temp/voice.mp3 --output temp/video_con_sottotitoli.mp4

echo "Generazione CTA screenshot..."
python create_cta_video.py

echo "Concatenazione finale..."
# Use filter_complex to ensure proper concatenation regardless of slight differences
ffmpeg -y -i temp/video_con_sottotitoli.mp4 -i temp/cta_video.mp4 \
-filter_complex "[0:v]scale=1080:1920,setdar=9/16[v0]; [1:v]scale=1080:1920,setdar=9/16[v1]; [v0][0:a][v1][1:a]concat=n=2:v=1:a=1[v][a]" \
-map "[v]" -map "[a]" -c:v libx264 -c:a aac -vsync 2 output/video_finale.mp4

echo "Pubblicazione..."
python step4_pubblica.py --video output/video_finale.mp4 --script scripts/script_ereader.txt --mode promo
