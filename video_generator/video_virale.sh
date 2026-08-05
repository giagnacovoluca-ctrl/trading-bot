#!/bin/bash
export DISPLAY=:0
export XAUTHORITY=/home/magic/.Xauthority
export PATH=$PATH:/home/magic/.local/bin
cd /home/magic/Scrivania/code/GIT/video_generator
source venv_video/bin/activate

HISTORY=$(tail -n 20 used_news_history.txt 2>/dev/null || echo "Nessuna notizia usata finora")

/home/magic/.local/bin/agy --add-dir /home/magic/Scrivania/code/GIT/video_generator --dangerously-skip-permissions --print "@tiktok_video_generator Sei il Direttore Creativo TikTok. 

TEMA OBBLIGATORIO: Mente, psicologia, salute, neuroscienze, crescita personale o nutrizione.
NOTIZIE GIA' TRATTATE RECENTEMENTE (IGNORA ASSOLUTAMENTE QUESTE E TROVA ALTRO):
$HISTORY

STEP 1: Usa SUBITO il tool search_web per cercare su internet una notizia VERA, recentissima (ultime 24 ore) e altamente virale sul tema obbligatorio. Non ripetere le notizie già trattate.

STEP 2: Scrivi uno script su questa notizia.
REGOLE:
- RIGA 1: scrivi esattamente 'TITOLO: ' seguito da un titolo/hook provocatorio (max 5 parole).
- RIGA 2 in poi: lo script di massimo 130 parole, focalizzato su ORIGINALITA', LOGICA (SENSATO) E VIRALITA' (STORYTELLING). Trova l'angolo più inaspettato della notizia, spiega perché cambia la vita di chi ascolta. Evita riassunti banali.
- Usa un hook ipnotico e chiudi chiedendo un parere nei commenti. (Nessun accenno ad Amazon).
- REGOLE PER LA VOCE: Scrivi TUTTI i numeri in lettere (es. \"mille\" e non \"1000\"). Non usare MAI simboli speciali, parentesi o virgolette. Usa frasi brevi e incisive. MANTIENI la punteggiatura forte (punti, virgole).

STEP 3: DEVI OBBLIGATORIAMENTE usare il tool 'write_to_file' per salvare questo script in scripts/script_generato.txt, altrimenti il sistema si rompe!
STEP 4: Usa il tool per salvare una descrizione TikTok con hashtag virali in scripts/tiktok_caption.txt. 
STEP 5: Genera 5 sfondi verticali fotorealistici pertinenti alla notizia e salvali in assets/backgrounds/." > cron_agy.log 2>&1

HOOK_TITLE=$(grep -E "^TITOLO:" scripts/script_generato.txt | head -n 1 | sed -e 's/^TITOLO: *//' -e 's/ *$//')
if [ -z "$HOOK_TITLE" ]; then HOOK_TITLE="SCOPERTA ASSURDA"; fi
echo "$HOOK_TITLE" >> used_news_history.txt

# Rimuovi la riga del titolo per non farla leggere alla voce
python -c "lines=[l for l in open('scripts/script_generato.txt').readlines() if not l.startswith('TITOLO:')]; open('scripts/script_generato.txt','w').writelines(lines)"

set -e
python step1_voce.py --script scripts/script_generato.txt --voice assets/voices/mia_voce.wav >> cron_agy.log 2>&1

# Trova le immagini appena generate (ultimi 10 min) per passarle allo step 2
IMAGES=$(find assets/backgrounds -type f -mmin -10 | tr '\n' ' ')
if [ -n "$IMAGES" ]; then
    python step2_sfondo.py --audio temp/voiceover.mp3 --topic "notizia curiosità scienza" --images $IMAGES >> cron_agy.log 2>&1
else
    python step2_sfondo.py --audio temp/voiceover.mp3 --topic "notizia curiosità scienza" >> cron_agy.log 2>&1
fi


python step3_sottotitoli.py --video temp/video_base.mp4 --audio temp/voiceover.mp3 --hook_title "$HOOK_TITLE" >> cron_agy.log 2>&1
python step4_pubblica.py --mode virale --script scripts/script_generato.txt >> cron_agy.log 2>&1
