#!/bin/bash
exec 9>/tmp/video_generator_pipeline.lock
flock -n 9 || { echo "Pipeline già in esecuzione: salto questo run"; exit 0; }
export DISPLAY=:0
export XAUTHORITY=/home/ubuntu/.Xauthority
export PATH=$PATH:/home/ubuntu/.local/bin
cd /home/ubuntu/GIT/video_generator
source venv_video/bin/activate

# Cancella i vecchi file per non riciclare caroselli passati in caso di errore
rm -f scripts/slides_carosello.json scripts/tiktok_caption.txt

EBOOKS=(
    "Cibo/Salute"
    "Meditazione"
    "Integratori"
    "Acqua/Idratazione"
    "Epigenetica/DNA"
    "Nervo Vago/Stress"
)

RANDOM_INDEX=$((RANDOM % ${#EBOOKS[@]}))
CHOSEN_EBOOK="${EBOOKS[$RANDOM_INDEX]}"

/usr/local/bin/agy --add-dir /home/ubuntu/GIT/video_generator --dangerously-skip-permissions --print "@tiktok_video_generator Sei un social media manager di altissimo livello. Genera un carosello TikTok (5 o 6 slide) per promuovere uno dei miei Ebook su Amazon. Il libro scelto per questo video è: '${CHOSEN_EBOOK}'.

REGOLE CONTENUTO: Fai divulgazione concreta dal libro. Non limitarti a promettere un segreto in modo generico, ma cita e spiega un CONCETTO SPECIFICO tratto dal libro scelto in modo intellettualmente stimolante. Usa un hook visivo fortissimo, rendi il concetto chiaro e logico.
ATTENZIONE FONDAMENTALE SULLA SCRITTURA: I caroselli non hanno voce narrante, l'utente leggerà SOLO la chiave 'overlay_text'. L'overlay_text deve formare un discorso logico, coerente e fluido tra le slide, MA DEVE ESSERE BREVE, CONCISO E DI FORTE IMPATTO. Usa un tono molto SERIO, PROFESSIONALE E AUTOREVOLE. Evita "muri di testo" (massimo 15-20 parole per slide). Niente battutine o toni infantili. Spiega concetti complessi in frasi brevi e taglienti.
Nell'ultima slide chiedi di scaricare il libro (link in bio).

STEP 1: OBBLIGATORIO: Usa il tool 'write_to_file' per salvare un array JSON in scripts/slides_carosello.json.
STEP 2: OBBLIGATORIO: Usa il tool 'write_to_file' per salvare una description persuasiva con hashtag in scripts/tiktok_caption.txt." > cron_carousel.log 2>&1

set -e
python crea_carosello.py >> cron_carousel.log 2>&1
