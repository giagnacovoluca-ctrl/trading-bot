"""
Configurazione centralizzata: costanti, path, parametri voce e video.
Ogni modulo importa solo da qui — nessun magic string sparso nel codice.
"""

from __future__ import annotations
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── FFmpeg: usa il binario bundled con imageio_ffmpeg (nessuna install sistema) ──
import imageio_ffmpeg as _iio_ffmpeg
import moviepy.config as _mp_config
_FFMPEG_BIN = _iio_ffmpeg.get_ffmpeg_exe()
_mp_config.change_settings({"FFMPEG_BINARY": _FFMPEG_BIN})
os.environ.setdefault("FFMPEG_BINARY", _FFMPEG_BIN)

# ── Directory Layout ──────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
ASSETS_DIR    = BASE_DIR / os.getenv("ASSETS_DIR", "assets")
BG_DIR        = ASSETS_DIR / "backgrounds"
FONTS_DIR     = ASSETS_DIR / "fonts"
AMBIENT_DIR   = ASSETS_DIR / "ambient"
VOICES_DIR    = ASSETS_DIR / "voices"
OUTPUT_DIR    = BASE_DIR / os.getenv("OUTPUT_DIR", "output")
TEMP_DIR      = BASE_DIR / os.getenv("TEMP_DIR", "temp")
SCRIPTS_DIR   = BASE_DIR / "scripts"

XTTS_DEFAULT_SPEAKER = VOICES_DIR / "mia_voce.wav"

for _d in (BG_DIR, FONTS_DIR, AMBIENT_DIR, VOICES_DIR, OUTPUT_DIR, TEMP_DIR, SCRIPTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── API Keys ──────────────────────────────────────────────────────────────────
PEXELS_API_KEY      = os.getenv("PEXELS_API_KEY", "")
ELEVENLABS_API_KEY  = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")

# ── TTS: Edge-TTS (Microsoft Neural, gratuito) ───────────────────────────────
EDGE_TTS_VOICE   = "it-IT-ElsaNeural"       # prima scelta: voce femminile morbida
EDGE_TTS_VOICE_M = "it-IT-DiegoNeural"      # alternativa: voce maschile profonda
EDGE_TTS_RATE    = "+0%"
EDGE_TTS_PITCH   = "+0Hz"
EDGE_TTS_VOLUME  = "+0%"

# ── ElevenLabs fine-tuning (opzionale) ───────────────────────────────────────
EL_STABILITY        = 0.55   # 0–1: bassa = più espressiva
EL_SIMILARITY_BOOST = 0.82   # 0–1: alta = più fedele alla voce
EL_STYLE            = 0.35   # 0–1: stile/esagerazione
EL_USE_SPEAKER_BOOST = True
EL_MODEL            = "eleven_multilingual_v2"

# ── Audio Mix ─────────────────────────────────────────────────────────────────
AMBIENT_VOLUME_DB   = -22    # dB relativi rispetto al silenzio (voce sarà ~-3dB)
VOICEOVER_VOLUME_DB = -1     # dB voiceover nel mix finale
AUDIO_SAMPLE_RATE   = 44100
AUDIO_BITRATE       = "192k"

# ── Video ─────────────────────────────────────────────────────────────────────
SUPPORTED_RATIOS: dict[str, tuple[int, int]] = {
    "169":  (1920, 1080),   # YouTube / Orizzontale
    "916":  (1080, 1920),   # TikTok / Reels / Shorts
    "11":   (1080, 1080),   # Instagram Square
    "43":   (1440, 1080),   # Classico
}
DEFAULT_RATIO = "169"
VIDEO_FPS     = 30
VIDEO_CODEC   = "libx264"
VIDEO_BITRATE = "4000k"
AUDIO_CODEC   = "aac"

# ── Caption Style ─────────────────────────────────────────────────────────────
CAPTION_FONT        = str(FONTS_DIR / "Montserrat-Bold.ttf")
CAPTION_FALLBACK    = "Arial"          # usato se il font custom non esiste
CAPTION_FONTSIZE    = 72               # px su 1080p (ridotto proporzionalmente)
CAPTION_COLOR       = "white"
CAPTION_STROKE_COLOR = "black"
CAPTION_STROKE_WIDTH = 3
CAPTION_POSITION    = ("center", 0.75) # (x, y_fraction) – basso/centro
CAPTION_MAX_CHARS   = 45              # caratteri per riga prima di andare a capo
CAPTION_FADE_DUR    = 0.15            # secondi di fade-in/out di ogni caption
HOOK_DURATION       = 8.0             # secondi di durata visualizzazione titolo

# ── Smart BGM Mood Mapping ────────────────────────────────────────────────────
BGM_MOOD_MAP = {
    "zen":      ["meditazione", "zen", "calma", "respiro", "mente", "stress", "ansia", "sonno", "relax"],
    "scienza":  ["scienza", "fisica", "universo", "spazio", "quantistica", "dna", "neuroni", "cervello", "ricerca", "studio", "biologia"],
    "energica": ["energia", "fuoco", "motivazione", "successo", "crescita", "forza", "potere"],
}

# ── Pexels (fallback se non ci sono bg locali) ────────────────────────────────
PEXELS_SEARCH_QUERIES = [
    "hypnotic abstract",
    "relaxing nature loop",
    "satisfying geometry",
    "cinematic dark ambient",
    "flowing particles",
]
PEXELS_PER_PAGE = 5
PEXELS_MIN_DURATION = 10   # scarica solo clip ≥10s

# ── Script Parsing ────────────────────────────────────────────────────────────
WORDS_PER_MINUTE = 145      # media parlato italiano lento/persuasivo
SLIDE_MARKERS    = ("---", "===", "[SLIDE]", "[PAUSA]")
