#!/bin/bash
# Script automatico per avviare la generazione e caricamento del video tramite Antigravity in background

export DISPLAY=:0
export XAUTHORITY=/home/ubuntu/.Xauthority
export PATH=$PATH:/home/ubuntu/.local/bin

cd /home/ubuntu/GIT/video_generator
source venv_video/bin/activate

# Invoca Antigravity (me) dall'esterno specificando il workspace.
/home/ubuntu/.local/bin/agy --add-dir /home/ubuntu/GIT/video_generator --dangerously-skip-permissions --print "Sei il Direttore Creativo TikTok. Scrivi un copione persuasivo (max 130 parole) per uno dei miei Ebook su Amazon: 1) 'Come usare il cibo per correggere energia, umore e digestione' 2) 'Meditazione per chiunque' 3) 'Tra Scienza e Intuizione'. 
REGOLE:
- RIGA 1: scrivi esattamente 'TITOLO: ' seguito da un titolo/hook fortissimo di massimo 5 parole.
- RIGA 2 in poi: lo script vero e proprio. Fai un hook verbale, dai valore e finisci con la call to action 'Trovi il libro su Amazon al link in bio!'
Salva il testo in scripts/script_generato.txt. 
Scrivi anche una descrizione per TikTok in scripts/tiktok_caption.txt. 
Genera 5 sfondi verticali fotorealistici pertinenti all'argomento scelto e salvali in assets/backgrounds/." > cron_agy.log 2>&1

set -e
python step1_voce.py --script scripts/script_generato.txt --voice assets/voices/mia_voce.wav >> cron_agy.log 2>&1
python step2_sfondo.py --audio temp/voiceover.mp3 >> cron_agy.log 2>&1

# Estrai il titolo dal file
HOOK_TITLE=$(grep -E "^TITOLO:" scripts/script_generato.txt | head -n 1 | sed -e 's/^TITOLO: *//' -e 's/ *$//')
if [ -z "$HOOK_TITLE" ]; then HOOK_TITLE="IL MIO EBOOK"; fi

python step3_sottotitoli.py --video temp/video_base.mp4 --audio temp/voiceover.mp3 --hook_title "$HOOK_TITLE" --cta >> cron_agy.log 2>&1
python step4_pubblica.py --mode promo --script scripts/script_generato.txt >> cron_agy.log 2>&1
