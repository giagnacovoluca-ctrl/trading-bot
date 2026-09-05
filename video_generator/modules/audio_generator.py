"""
Module B — Audio Generation.

Genera il voiceover italiano via edge-tts (gratuito) o ElevenLabs (premium),
poi mixa opzionalmente una traccia ambient sotto la voce.

Uso:
    audio_path, duration = generate_italian_voiceover(
        text="Benvenuto...",
        output_path="temp/voiceover.mp3",
        provider="edge",   # oppure "elevenlabs"
    )
"""

from __future__ import annotations
import asyncio
import math
import random
import os
import re
import shutil
import tempfile
from pathlib import Path

from rich.console import Console
from pydub import AudioSegment

import config

console = Console()


def sanitize_tts_segment(text: str) -> str:
    """Rimuove i simboli pronunciabili dopo averne già ricavato l'intenzione."""
    cleaned = text.replace('"', '').replace('*', '').replace('(', '').replace(')', '')
    cleaned = re.sub(r"[.!?]+", " ", cleaned)
    cleaned = re.sub(r"[,;:]+", "\n", cleaned)
    cleaned = re.sub(r"[—–-]+", " ", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    return cleaned.strip()


def _apply_question_contour(wav_path: Path) -> None:
    """Alza lievemente la coda vocale senza inserire '?' nel testo letto."""
    try:
        import librosa
        import numpy as np
        import soundfile as sf

        audio, sample_rate = sf.read(str(wav_path), always_2d=False)
        if getattr(audio, "ndim", 1) > 1:
            audio = audio.mean(axis=1)
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
        voiced = np.flatnonzero(np.abs(audio) > peak * 0.025) if peak else np.array([])
        if not len(voiced):
            return
        end = int(voiced[-1]) + 1
        start = max(0, end - int(sample_rate * 0.7))
        original = audio[start:end].copy()
        shifted = librosa.effects.pitch_shift(original, sr=sample_rate, n_steps=0.9)
        blend_samples = min(len(original), int(sample_rate * 0.09))
        blend = np.ones(len(original), dtype=float)
        if blend_samples:
            blend[:blend_samples] = np.linspace(0.0, 1.0, blend_samples)
        audio[start:end] = original * (1.0 - blend) + shifted[:len(original)] * blend
        sf.write(str(wav_path), audio, sample_rate)
    except Exception as exc:
        console.print(f"[dim yellow]Contorno interrogativo non applicato: {exc}[/]")


# ── Edge-TTS ──────────────────────────────────────────────────────────────────

async def _edge_tts_async(text: str, output_path: Path, voice: str, rate: str, pitch: str, volume: str) -> None:
    """Wrapper async per edge-tts."""
    import edge_tts  # lazy import: non necessario se si usa ElevenLabs
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
    await communicate.save(str(output_path))


def _generate_edge(text: str, output_path: Path, voice: str | None = None) -> Path:
    voice   = voice or config.EDGE_TTS_VOICE
    rate    = config.EDGE_TTS_RATE
    pitch   = config.EDGE_TTS_PITCH
    volume  = config.EDGE_TTS_VOLUME

    console.print(f"[cyan]edge-tts[/] → voce: [bold]{voice}[/], rate={rate}, pitch={pitch}")
    asyncio.run(_edge_tts_async(text, output_path, voice, rate, pitch, volume))
    console.print(f"[green]✓[/] voiceover salvato: {output_path}")
    return output_path


# ── ElevenLabs ────────────────────────────────────────────────────────────────

def _generate_elevenlabs(text: str, output_path: Path) -> Path:
    """Genera audio via ElevenLabs API (multilingual v2)."""
    try:
        from elevenlabs.client import ElevenLabs
        from elevenlabs import VoiceSettings
    except ImportError as e:
        raise RuntimeError("elevenlabs non installato: pip install elevenlabs") from e

    if not config.ELEVENLABS_API_KEY:
        raise ValueError("ELEVENLABS_API_KEY mancante nel file .env")

    client = ElevenLabs(api_key=config.ELEVENLABS_API_KEY)
    console.print(f"[magenta]ElevenLabs[/] → voice_id: [bold]{config.ELEVENLABS_VOICE_ID}[/]")

    audio_generator = client.text_to_speech.convert(
        voice_id=config.ELEVENLABS_VOICE_ID,
        text=text,
        model_id=config.EL_MODEL,
        voice_settings=VoiceSettings(
            stability=config.EL_STABILITY,
            similarity_boost=config.EL_SIMILARITY_BOOST,
            style=config.EL_STYLE,
            use_speaker_boost=config.EL_USE_SPEAKER_BOOST,
        ),
    )
    with open(output_path, "wb") as f:
        for chunk in audio_generator:
            f.write(chunk)

    console.print(f"[green]✓[/] ElevenLabs salvato: {output_path}")
    return output_path


# ── Coqui XTTS v2 (Voice Cloning) ────────────────────────────────────────────

def _generate_xtts(
    text: str,
    output_path: Path,
    speaker_wav: str | Path | None = None,
    segments: list[dict] | None = None,
) -> Path:
    """Genera audio via XTTS v2 Coqui TTS (clonazione vocale zero-shot)."""
    import os
    os.environ["COQUI_TOS_AGREED"] = "1"
    try:
        import torch
        import soundfile as sf
        import torchaudio
        import transformers.utils.import_utils as tu
        import transformers.pytorch_utils as pu
        from transformers.utils import logging as transformers_logging

        # Gli avvisi sui token GPT non riguardano XTTS e riempivano inutilmente i log.
        transformers_logging.set_verbosity_error()

        if not hasattr(pu, 'isin_mps_friendly'):
            pu.isin_mps_friendly = lambda elements, test_elements: torch.isin(elements, test_elements)
        tu.is_torchcodec_available = lambda: True

        def _torchaudio_load_sf(filepath, **kwargs):
            data, samplerate = sf.read(filepath)
            tensor = torch.from_numpy(data).float()
            if tensor.ndim == 1:
                tensor = tensor.unsqueeze(0)
            elif tensor.ndim == 2:
                tensor = tensor.t()
            return tensor, samplerate

        torchaudio.load = _torchaudio_load_sf

        from TTS.api import TTS
    except ImportError as e:
        raise RuntimeError(
            "Coqui TTS non installato nel virtualenv.\n"
            "Installa con: source venv_video/bin/activate && pip install coqui-tts torch torchaudio soundfile"
        ) from e

    speaker_sample = Path(speaker_wav) if speaker_wav else config.XTTS_DEFAULT_SPEAKER
    if not speaker_sample.exists():
        raise FileNotFoundError(
            f"Campione vocale per XTTS non trovato: {speaker_sample}\n"
            f"Inserisci un file .wav della tua voce in assets/voices/mia_voce.wav oppure specifica --voice percorso/audio.wav"
        )

    console.print(f"[bold cyan]XTTS v2 (Clonazione Vocale)[/] → speaker: [bold]{speaker_sample.name}[/]")
    import torch
    use_gpu = torch.cuda.is_available()
    if not use_gpu:
        torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            pass

    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2", gpu=use_gpu)

    # L'intenzione è già contenuta nello storyboard. Al modello non passiamo
    # simboli che in alcuni campioni vocali vengono letti come parole.
    clean_tts_text = sanitize_tts_segment(text)

    if output_path.suffix.lower() == ".mp3":
        from pydub import AudioSegment
        AudioSegment.converter = config._FFMPEG_BIN
        ffmpeg_dir = str(Path(config._FFMPEG_BIN).parent)
        if ffmpeg_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

        if segments:
            combined = AudioSegment.empty()
            with tempfile.TemporaryDirectory(prefix="conscia_voice_") as temp_dir:
                for index, segment in enumerate(segments):
                    segment_text = sanitize_tts_segment(str(segment.get("spoken_text", "")))
                    if not segment_text:
                        continue
                    wav_segment = Path(temp_dir) / f"segment_{index:02d}.wav"
                    with torch.inference_mode():
                        tts.tts_to_file(
                            text=segment_text,
                            speaker_wav=str(speaker_sample),
                            language="it",
                            file_path=str(wav_segment),
                        )
                    intent = str(segment.get("intent", "spiegazione_calma"))
                    if intent == "domanda_curiosa":
                        _apply_question_contour(wav_segment)
                    spoken = AudioSegment.from_wav(str(wav_segment))
                    pause_ms = {
                        "domanda_curiosa": 330,
                        "hook_diretto": 230,
                        "rivelazione": 260,
                        "cta_chiara": 300,
                    }.get(intent, 190)
                    combined += spoken + AudioSegment.silent(duration=pause_ms)
                    segment["audio_duration"] = round((len(spoken) + pause_ms) / 1000, 3)
            combined.export(str(output_path), format="mp3", bitrate=config.AUDIO_BITRATE)
        else:
            wav_temp = output_path.with_suffix(".temp.wav")
            with torch.inference_mode():
                tts.tts_to_file(
                    text=clean_tts_text,
                    speaker_wav=str(speaker_sample),
                    language="it",
                    file_path=str(wav_temp),
                )
            sound = AudioSegment.from_wav(str(wav_temp))
            sound.export(str(output_path), format="mp3", bitrate=config.AUDIO_BITRATE)
            if wav_temp.exists():
                wav_temp.unlink()
    else:
        with torch.inference_mode():
            tts.tts_to_file(
                text=clean_tts_text,
                speaker_wav=str(speaker_sample),
                language="it",
                file_path=str(output_path),
            )

    # Free memory to prevent OOM during video generation
    del tts
    import gc
    gc.collect()
    if use_gpu:
        torch.cuda.empty_cache()

    console.print(f"[green]✓[/] Voce clonata XTTS salvata: {output_path}")
    return output_path


# ── Audio Duration ────────────────────────────────────────────────────────────

def get_audio_duration(path: Path) -> float:
    """Restituisce la durata in secondi usando mutagen (pure Python, senza ffprobe)."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".mp3":
            from mutagen.mp3 import MP3
            return float(MP3(str(path)).info.length)
        elif suffix in (".wav", ".wave"):
            from mutagen.wave import WAVE
            return float(WAVE(str(path)).info.length)
        elif suffix in (".ogg", ".oga"):
            from mutagen.oggvorbis import OggVorbis
            return float(OggVorbis(str(path)).info.length)
        elif suffix in (".flac",):
            from mutagen.flac import FLAC
            return float(FLAC(str(path)).info.length)
        else:
            from mutagen import File as MutagenFile
            audio = MutagenFile(str(path))
            if audio and audio.info:
                return float(audio.info.length)
    except Exception as e:
        console.print(f"[yellow]mutagen fallback ({e})[/] — stima da file size")

    # Stima fallback: ~128kbps MP3 ≈ 16000 byte/s
    size = path.stat().st_size
    return max(1.0, size / 16_000)


# ── Ambient Mix ───────────────────────────────────────────────────────────────

def _find_ambient_track(script_text: str = "") -> Path | None:
    """Cerca un file audio nella cartella ambient (mp3/wav/ogg/flac).
    Sceglie una sottocartella in base alle parole chiave del testo usando BGM_MOOD_MAP da config."""
    exts = ("*.mp3", "*.wav", "*.ogg", "*.flac", "*.m4a")

    text_lower = script_text.lower()
    mood = ""

    # Usa BGM_MOOD_MAP da config se disponibile, altrimenti fallback hardcoded
    mood_map = getattr(config, "BGM_MOOD_MAP", {
        "zen":      ["meditazione", "zen", "calma", "respiro", "mente", "stress", "ansia", "sonno"],
        "scienza":  ["scienza", "fisica", "universo", "spazio", "quantistica", "dna", "neuroni", "cervello"],
        "energica": ["energia", "fuoco", "motivazione", "successo", "crescita", "forza"],
    })

    best_mood = ""
    best_score = 0
    for m, keywords in mood_map.items():
        score = sum(1 for k in keywords if k in text_lower)
        if score > best_score:
            best_score = score
            best_mood = m

    if best_mood and best_score > 0:
        mood = best_mood
        console.print(f"[cyan]Smart BGM:[/] Rilevato mood '[bold]{mood}[/]' (score={best_score}).")

    search_dirs = []
    if mood:
        mood_dir = config.AMBIENT_DIR / mood
        if mood_dir.exists():
            search_dirs.append(mood_dir)

    # Fallback alla directory ambient generale
    search_dirs.append(config.AMBIENT_DIR)

    candidates: list[Path] = []
    for s_dir in search_dirs:
        for pattern in exts:
            candidates.extend(p for p in s_dir.glob(pattern) if p.is_file())
        if candidates:
            break

    if not candidates:
        return None
    return random.choice(candidates)


def _mix_with_ambient(voiceover_path: Path, output_path: Path, ambient_path: Path) -> Path:
    """
    Mixa il voiceover con la traccia ambient.
    - La traccia ambient viene loopata se più corta del voiceover.
    - Il volume ambient è abbassato a AMBIENT_VOLUME_DB.
    - Il voiceover è a VOICEOVER_VOLUME_DB.
    """
    from pydub import AudioSegment
    AudioSegment.converter = config._FFMPEG_BIN
    ffmpeg_dir = str(Path(config._FFMPEG_BIN).parent)
    if ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

    voice = AudioSegment.from_file(str(voiceover_path))
    ambient = AudioSegment.from_file(str(ambient_path))

    # Loop ambient se necessario
    voice_ms = len(voice)
    if len(ambient) < voice_ms:
        loops = math.ceil(voice_ms / len(ambient))
        ambient = ambient * loops
    ambient = ambient[:voice_ms]

    # Aggiusta volumi
    voice   = voice   + config.VOICEOVER_VOLUME_DB
    ambient = ambient + config.AMBIENT_VOLUME_DB

    mixed = voice.overlay(ambient)
    mixed.export(str(output_path), format="mp3", bitrate=config.AUDIO_BITRATE)
    console.print(f"[green]✓[/] mix voiceover+ambient: {output_path}")
    return output_path


# ── Public API ────────────────────────────────────────────────────────────────

def generate_italian_voiceover(
    text: str,
    output_path: str | Path,
    provider: str = "edge",
    voice: str | None = None,
    mix_ambient: bool = True,
    segments: list[dict] | None = None,
) -> tuple[Path, float]:
    """
    Genera il voiceover italiano e opzionalmente lo mixa con un ambient track.

    Args:
        text: testo da sintetizzare
        output_path: percorso del file audio finale (.mp3)
        provider: "edge" (gratuito) | "elevenlabs" (premium)
        voice: override della voce (solo per edge-tts)
        mix_ambient: se True e c'è un file in assets/ambient/, viene mixato

    Returns:
        (path_audio_finale, durata_in_secondi)
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    raw_voice_path = output_path.parent / f"_raw_{output_path.name}"

    # Generazione voiceover
    if provider == "elevenlabs":
        _generate_elevenlabs(text, raw_voice_path)
    elif provider == "xtts":
        _generate_xtts(text, raw_voice_path, speaker_wav=voice, segments=segments)
    else:
        _generate_edge(text, raw_voice_path, voice=voice)

    # Mix ambient opzionale
    if mix_ambient:
        ambient = _find_ambient_track(text)
        if ambient:
            console.print(f"[yellow]ambient[/] → mixando con: {ambient.name}")
            _mix_with_ambient(raw_voice_path, output_path, ambient)
        else:
            console.print("[dim]Nessuna traccia ambient trovata in assets/ambient/, uso solo voiceover[/]")
            raw_voice_path.rename(output_path)
    else:
        raw_voice_path.rename(output_path)

    duration = get_audio_duration(output_path)
    console.print(f"[bold green]Audio finale:[/] {output_path} ({duration:.1f}s)")
    return output_path, duration
