#!/bin/bash
export DISPLAY=:0
export XAUTHORITY=/home/ubuntu/.Xauthority
export PATH=$PATH:/home/ubuntu/.local/bin
cd /home/ubuntu/GIT/video_generator
source venv_video/bin/activate

/usr/local/bin/agy --add-dir /home/ubuntu/GIT/video_generator --dangerously-skip-permissions --print "@tiktok_video_generator Sei un genio del copywriting e sceneggiatore per TikTok. Scrivi un copione persuasivo (max 130 parole) per promuovere uno dei miei Ebook su Amazon: 1) 'Come usare il cibo per correggere energia, umore e digestione' 2) 'Meditazione per chiunque' 3) 'Tra Scienza e Intuizione'. 
REGOLE:
- RIGA 1: scrivi esattamente 'TITOLO: ' seguito da un titolo/hook shock (max 5 parole).
- RIGA 2 in poi: lo script di massimo 130 parole, che deve essere ESTREMAMENTE ORIGINALE E SENSATO (nessuna banalità, rivela un segreto nascosto nel libro o una verità controintuitiva ma logica).
- Usa un hook viscerale, dai un valore incredibile e finisci con una Call To Action forte: link in bio per scaricare l'ebook.
- REGOLE PER LA VOCE: Scrivi TUTTI i numeri in lettere (es. \"cento\" e non \"100\"). Non usare MAI simboli speciali, parentesi o virgolette. Usa frasi brevi. MANTIENI la punteggiatura forte (punti, virgole) per dare ritmo.
Salva il testo in scripts/script_generato.txt. 
Scrivi anche una descrizione per TikTok (inclusa l'invito a cliccare sul link in bio e hashtag corretti) in scripts/tiktok_caption.txt. 
Genera 3 sfondi verticali fotorealistici pertinenti all'argomento scelto e salvali in assets/backgrounds/." > cron_agy.log 2>&1

HOOK_TITLE=$(grep -E "^TITOLO:" scripts/script_generato.txt | head -n 1 | sed -e 's/^TITOLO: *//' -e 's/ *$//')
if [ -z "$HOOK_TITLE" ]; then HOOK_TITLE="IL MIO EBOOK"; fi

# Rimuovi la riga del titolo per non farla leggere alla voce
python -c "lines=[l for l in open('scripts/script_generato.txt').readlines() if not l.startswith('TITOLO:')]; open('scripts/script_generato.txt','w').writelines(lines)"

set -e
python step1_voce.py --script scripts/script_generato.txt --voice assets/voices/mia_voce.wav --provider xtts >> cron_agy.log 2>&1

# Trova le immagini appena generate (ultimi 10 min) per passarle allo step 2
IMAGES=$(find assets/backgrounds -type f -mmin -10 | tr '\n' ' ')
if [ -n "$IMAGES" ]; then
    python step2_sfondo.py --audio temp/voiceover.mp3 --topic "ebook energia meditazione" --images $IMAGES >> cron_agy.log 2>&1
else
    python step2_sfondo.py --audio temp/voiceover.mp3 --topic "ebook energia meditazione" >> cron_agy.log 2>&1
fi

python step3_sottotitoli.py --video temp/video_base.mp4 --audio temp/voiceover.mp3 --hook_title "$HOOK_TITLE" --cta >> cron_agy.log 2>&1
python step4_pubblica.py --mode promo --script scripts/script_generato.txt >> cron_agy.log 2>&1
