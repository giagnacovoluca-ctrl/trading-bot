Struttura progetto creata

video_generator/
├── config.py                  # Costanti centrali (voci, ratio, ffmpeg, caption style)
├── main.py                    # CLI orchestratore (argparse)
├── requirements.txt
├── .env.example               # Template API keys
├── setup.sh                   # Setup automatico (ffmpeg, venv, font)
├── modules/
│   ├── script_manager.py      # Parsing script (timestamp / slide markers / testo libero)
│   ├── audio_generator.py     # TTS edge-tts / ElevenLabs + mix ambient
│   └── video_composer.py      # Background + loop + crop + caption + render
├── assets/
│   ├── backgrounds/           # ← metti qui i tuoi video .mp4
│   ├── ambient/               # ← musica di sottofondo .mp3
│   └── fonts/                 # Montserrat-Bold.ttf (auto-scaricato da setup.sh)
├── scripts/
│   ├── esempio_base.txt       # Script diviso da ---
│   └── esempio_timestamp.txt  # Script con [MM:SS] espliciti
└── output/                    # Video generati

Setup (una volta sola)

cd video_generator
bash setup.sh   # installa dipendenze, crea venv_video, scarica font
source venv_video/bin/activate

▎ FFmpeg non serve installarlo: imageio_ffmpeg lo porta bundled nel venv.

Comandi principali

# Test voce (verifica connessione TTS)
python main.py test-tts "Ciao, sono Elsa, la voce del tuo video."

# Voci italiane disponibili
python main.py list-voices

# Genera video YouTube 16:9 (prima metti un .mp4 in assets/backgrounds/)
python main.py generate --script scripts/esempio_base.txt --output output/video.mp4 --ratio 169

# TikTok 9:16 con voce maschile
python main.py generate --script scripts/esempio_base.txt --ratio 916 --voice diego

# Preview del parsing senza generare nulla
python main.py generate --script scripts/esempio_base.txt --dry-run

# Con ElevenLabs (premium)
python main.py generate --script scripts/mio_script.txt --provider elevenlabs

Cosa aggiungere prima della prima generazione video

1. Background video: copia almeno un .mp4 in assets/backgrounds/ (oppure configura PEXELS_API_KEY nel .env per scaricarlo automaticamente)
2. Ambient track (opzionale): un .mp3 in assets/ambient/ per musica di sottofondo
3. ImageMagick (per le caption): sudo apt install imagemagick — senza, il video viene generato senza sottotitoli ma funziona comunque