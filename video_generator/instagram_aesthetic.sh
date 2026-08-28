#!/bin/bash
set -euo pipefail
exec 9>/tmp/video_generator_pipeline.lock
flock -n 9 || { echo "Pipeline già in esecuzione: salto questo run"; exit 0; }
export DISPLAY=:0
export PATH=$PATH:/home/ubuntu/.local/bin
cd /home/ubuntu/GIT/video_generator
PYTHON=/home/ubuntu/GIT/video_generator/venv_video/bin/python

# Array degli Ebook con le rispettive categorie
declare -A EBOOKS=(
    ["Cibo/Salute"]="cibo_salute"
    ["Integratori"]="cibo_salute"
    ["Meditazione"]="nervo_vago"
    ["Nervo Vago/Stress"]="nervo_vago"
    ["Acqua/Idratazione"]="acqua_idratazione"
    ["Epigenetica/DNA"]="epigenetica"
)

# Seleziona un Ebook a caso
keys=("${!EBOOKS[@]}")
RANDOM_INDEX=$((RANDOM % ${#keys[@]}))
CHOSEN_EBOOK="${keys[$RANDOM_INDEX]}"
CATEGORY="${EBOOKS[$CHOSEN_EBOOK]}"

echo "Avvio generazione Aesthetic Reel per Ebook: $CHOSEN_EBOOK (Categoria: $CATEGORY)" > cron_aesthetic.log

# Genera la frase a effetto e la caption tramite AGY
/usr/local/bin/agy --add-dir /home/ubuntu/GIT/video_generator --dangerously-skip-permissions --print "@tiktok_video_generator Sei un copywriter per Instagram di altissimo livello (stile Dark Academia / Quiet Luxury).
Devi promuovere il mio libro: '$CHOSEN_EBOOK'.

REGOLE:
1. Crea una SINGOLA FRASE ad altissimo impatto (max 15 parole) che faccia riflettere profondamente l'utente su questo tema. La frase sarà il testo a schermo nel video. Deve essere tagliente, cinica o rivelatoria.
2. Crea una DESCRIZIONE PER INSTAGRAM lunga e accattivante. Inizia spiegando il concetto della frase a schermo, distruggi un falso mito e finisci con una forte Call To Action: 'Vuoi capire come risolvere davvero? Scrivi la parola GUIDA nei commenti e ti manderò il manuale nei DM.' (Usa anche gli hashtag appropriati).

STEP OBBLIGATORIO: Usa il tool write_to_file per creare un file chiamato 'scripts/ig_aesthetic_data.json' contenente ESATTAMENTE questo JSON valido:
{
  \"testo_schermo\": \"<inserisci qui la frase breve>\",
  \"caption\": \"<inserisci qui tutta la descrizione lunga con gli hashtag>\"
}
Non usare virgolette interne non fuggite nel JSON." >> cron_aesthetic.log 2>&1

# Leggi i dati dal JSON (usiamo python per estrarre in modo sicuro)
"$PYTHON" -c "
import json
with open('scripts/ig_aesthetic_data.json', 'r', encoding='utf-8') as f:
    data = json.load(f)
with open('scripts/temp_aesthetic_text.txt', 'w', encoding='utf-8') as f:
    f.write(data['testo_schermo'])
with open('scripts/ig_caption.txt', 'w', encoding='utf-8') as f:
    f.write(data['caption'])
" >> cron_aesthetic.log 2>&1

TESTO=$(cat scripts/temp_aesthetic_text.txt)

# Genera il video
"$PYTHON" crea_ig_aesthetic.py --text "$TESTO" --category "$CATEGORY" --out "output/aesthetic_reel.mp4" >> cron_aesthetic.log 2>&1

# Pubblica su Instagram (legge in automatico scripts/ig_caption.txt)
"$PYTHON" step4_pubblica_ig_api.py --video output/aesthetic_reel.mp4 >> cron_aesthetic.log 2>&1

echo "Finito!" >> cron_aesthetic.log
