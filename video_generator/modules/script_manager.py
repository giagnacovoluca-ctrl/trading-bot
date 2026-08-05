"""
Module A — Script Ingestion & Parsing.

Legge uno script testuale (txt/md), lo divide in segmenti sincronizzati
con i visuals e calcola la durata attesa di ogni segmento basandosi sul
conteggio parole quando non sono presenti timestamp espliciti.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path

from config import WORDS_PER_MINUTE, SLIDE_MARKERS


@dataclass
class Segment:
    """Un blocco di testo con durata stimata e indice di sequenza."""
    index: int
    text: str
    estimated_duration: float   # secondi
    start_time: float = 0.0
    end_time: float = 0.0
    words: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.words = self.text.split()

    @property
    def word_count(self) -> int:
        return len(self.words)


@dataclass
class ParsedScript:
    segments: list[Segment]
    full_text: str
    total_estimated_duration: float

    @property
    def word_count(self) -> int:
        return sum(s.word_count for s in self.segments)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean_text(raw: str) -> str:
    """Rimuove commenti Markdown, link, tag HTML e spazi multipli."""
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)
    raw = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = re.sub(r"#+\s*", "", raw)         # rimuove heading ##
    raw = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", raw)   # grassetto/corsivo
    raw = re.sub(r"[ \t]+", " ", raw)
    return raw.strip()


def _estimate_duration(text: str) -> float:
    """Stima la durata in secondi basandosi sul WPM configurato."""
    word_count = len(text.split())
    seconds = (word_count / WORDS_PER_MINUTE) * 60
    return round(max(seconds, 1.0), 2)


def _split_by_markers(text: str) -> list[str]:
    """Divide il testo usando i marcatori di slide/pausa configurati."""
    pattern = "|".join(re.escape(m) for m in SLIDE_MARKERS)
    chunks = re.split(pattern, text)
    return [c.strip() for c in chunks if c.strip()]


def _split_by_timestamp(text: str) -> list[tuple[float, str]]:
    """
    Cerca timestamp nel formato [MM:SS] o [SS] all'inizio di ogni riga.
    Restituisce lista di (start_sec, text).
    """
    results = []
    ts_pattern = re.compile(r"^\[(\d{1,2}):(\d{2})\]\s*(.*)", re.MULTILINE)
    ts_simple  = re.compile(r"^\[(\d+)\]\s*(.*)", re.MULTILINE)

    matches = list(ts_pattern.finditer(text))
    if matches:
        for m in matches:
            start = int(m.group(1)) * 60 + int(m.group(2))
            results.append((float(start), m.group(3).strip()))
        return results

    matches = list(ts_simple.finditer(text))
    if matches:
        for m in matches:
            results.append((float(m.group(1)), m.group(2).strip()))
        return results

    return []


# ── Public API ────────────────────────────────────────────────────────────────

def load_script(path: str | Path) -> ParsedScript:
    """
    Carica e parsa uno script da file.

    Priorità parsing:
    1. Timestamp espliciti [MM:SS] → usa quelli
    2. Marcatori slide (---, ===, [SLIDE]) → divide in blocchi
    3. Nessun marcatore → tratta tutto come un unico blocco
    """
    raw = Path(path).read_text(encoding="utf-8")
    return parse_script(raw)


def parse_script(raw_text: str) -> ParsedScript:
    """Parsa il testo grezzo e restituisce un ParsedScript strutturato."""
    cleaned = _clean_text(raw_text)
    full_text = cleaned

    # Caso 1: timestamp espliciti
    ts_segments = _split_by_timestamp(cleaned)
    if ts_segments:
        segments = []
        for i, (start, text) in enumerate(ts_segments):
            end = ts_segments[i + 1][0] if i + 1 < len(ts_segments) else start + _estimate_duration(text)
            seg = Segment(
                index=i,
                text=text,
                estimated_duration=round(end - start, 2),
                start_time=start,
                end_time=end,
            )
            segments.append(seg)
        total = segments[-1].end_time if segments else 0.0
        return ParsedScript(segments=segments, full_text=full_text, total_estimated_duration=total)

    # Caso 2: marcatori slide
    chunks = _split_by_markers(cleaned)
    if len(chunks) > 1:
        segments = []
        cursor = 0.0
        for i, chunk in enumerate(chunks):
            dur = _estimate_duration(chunk)
            seg = Segment(
                index=i,
                text=chunk,
                estimated_duration=dur,
                start_time=cursor,
                end_time=cursor + dur,
            )
            segments.append(seg)
            cursor += dur
        total = cursor
        spoken_full_text = ". ".join(s.text.rstrip(". ") for s in segments) + "."
        return ParsedScript(segments=segments, full_text=spoken_full_text, total_estimated_duration=total)

    # Caso 3: testo monolitico → un solo segmento
    dur = _estimate_duration(cleaned)
    seg = Segment(index=0, text=cleaned, estimated_duration=dur, start_time=0.0, end_time=dur)
    return ParsedScript(segments=[seg], full_text=cleaned, total_estimated_duration=dur)


def adjust_timing_to_audio(script: ParsedScript, audio_duration: float) -> ParsedScript:
    """
    Riscala i timing dei segmenti per adattarsi alla durata audio reale
    (che può differire dall'stima basata su WPM).
    """
    estimated = script.total_estimated_duration
    if estimated <= 0:
        return script

    ratio = audio_duration / estimated
    cursor = 0.0
    for seg in script.segments:
        seg.start_time = round(cursor, 3)
        seg.estimated_duration = round(seg.estimated_duration * ratio, 3)
        seg.end_time = round(cursor + seg.estimated_duration, 3)
        cursor = seg.end_time

    script.total_estimated_duration = audio_duration
    return script


def split_into_caption_chunks(text: str, max_chars: int = 45) -> list[str]:
    """
    Divide il testo in chunk da mostrare come caption (max N caratteri),
    cercando di spezzare ai confini di parola.
    """
    words = text.split()
    chunks: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = word
    if current:
        chunks.append(current)
    return chunks
