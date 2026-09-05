"""Storyboard condiviso tra immagini, voce e montaggio TikTok."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable


INTERROGATIVE_OPENINGS = (
    "chi ", "cosa ", "come ", "quando ", "dove ", "perché ", "perche ",
    "quanto ", "quale ", "davvero ", "hai mai ", "ti sei mai ",
)


def split_spoken_segments(text: str, max_segments: int = 12, max_words: int = 16) -> list[str]:
    """Divide il parlato in beat visivi brevi, senza perdere l'ordine narrativo.

    Un singolo atto poteva restare a schermo per quindici-venti secondi: anche
    con una buona immagine il risultato appariva fermo e spesso incoerente con
    la parte della frase pronunciata. Ogni beat resta invece entro circa sei
    secondi di parlato.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    source_units = lines or [text.strip()]
    beats: list[str] = []

    for unit in source_units:
        if not unit:
            continue
        # La virgola è utile per cambiare soggetto/azione, ma il testo di ogni
        # beat rimane sempre una porzione letterale del copione.
        clauses = [
            clause.strip()
            for clause in re.split(r"(?<=[,;:.!?])\s+", unit)
            if clause.strip()
        ]
        current: list[str] = []
        current_words = 0
        for clause in clauses:
            clause_words = clause.split()
            # Se una frase lunga non contiene pause, la dividiamo comunque in
            # blocchi leggibili: meglio cambiare visual nel punto giusto che
            # mostrare una sola immagine generica per tutto l'atto.
            while clause_words:
                room = max_words - current_words
                if room <= 0:
                    beats.append(" ".join(current))
                    current, current_words, room = [], 0, max_words
                take = min(room, len(clause_words))
                current.extend(clause_words[:take])
                current_words += take
                clause_words = clause_words[take:]
                if current_words >= max_words:
                    beats.append(" ".join(current))
                    current, current_words = [], 0
        if current:
            beats.append(" ".join(current))

    if len(beats) <= max_segments:
        return beats

    # Limite di costo/tempo per la generazione immagini: conserva i beat
    # iniziali e ricompone soltanto la coda, senza scartare parti del copione.
    head = beats[:max_segments - 1]
    return head + [" ".join(beats[max_segments - 1:])]


def infer_intent(text: str, index: int, total: int) -> str:
    normalized = text.strip().lower()
    if "?" in text or normalized.startswith(INTERROGATIVE_OPENINGS):
        return "domanda_curiosa"
    if index == total - 1:
        return "cta_chiara"
    if any(token in normalized for token in ("ma ", "invece", "eppure", "sorprende", "significa")):
        return "rivelazione"
    if index == 0:
        return "hook_diretto"
    return "spiegazione_calma"


def _fallback_visual(segment: str, title: str, index: int) -> dict:
    words = re.findall(r"[A-Za-zÀ-ÿ0-9]+", segment)
    keywords = " ".join(words[:10]) or title
    return {
        "visual_prompt": (
            f"Vertical cinematic editorial photograph illustrating this exact idea: {keywords}. "
            "One clear subject, realistic environment, natural light, no text, no letters, no logo, 9:16"
        ),
        "pexels_query": keywords,
        "provider": "generated",
    }


def _extract_json_array(raw: str) -> list[dict]:
    blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, re.IGNORECASE)
    for source in [*blocks, raw]:
        start, end = source.find("["), source.rfind("]")
        if start < 0 or end <= start:
            continue
        try:
            value = json.loads(source[start:end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def create_scene_plan(
    title: str,
    topic: str,
    spoken_text: str,
    call_agy: Callable[[str], str] | None = None,
) -> list[dict]:
    segments = split_spoken_segments(spoken_text)
    if not segments:
        raise ValueError("Copione senza segmenti visivi")

    generated: list[dict] = []
    if call_agy:
        numbered = "\n".join(f"{index + 1}. {text}" for index, text in enumerate(segments))
        prompt = f"""Sei un visual director per video TikTok verticali.
Titolo: {title}
Tema: {topic}

Per ciascuno dei {len(segments)} segmenti crea UNA scena letterale e riconoscibile, coerente con ciò che viene pronunciato in quel momento.
Mantieni la stessa estetica editoriale cinematografica e, quando compare una persona, la stessa descrizione del soggetto fra scene consecutive.
Ogni scena deve raffigurare almeno un soggetto, un'azione e un luogo/materiale specifici nominati o implicati dal segmento. Se il segmento cita una fonte, mostra il fenomeno o l'esperimento, mai il logo della fonte o una persona generica che legge.
Evita sfondi astratti generici, persone che fissano la camera senza fare nulla, testo dentro l'immagine, collage, infografiche e metafore non collegate alla frase.
La prima scena deve rappresentare precisamente titolo e hook.
Usa provider "stock" soltanto per azioni, persone o luoghi realistici facilmente reperibili; usa "generated" per concetti scientifici, storici o difficili da trovare.

SEGMENTI:
{numbered}

Restituisci SOLO un array JSON con esattamente {len(segments)} oggetti, nello stesso ordine:
[
  {{"visual_prompt":"prompt inglese concreto e dettagliato, vertical 9:16, no text", "pexels_query":"query inglese di 2-6 parole", "provider":"stock oppure generated"}}
]
"""
        try:
            generated = _extract_json_array(call_agy(prompt))
        except Exception:
            generated = []

    plan = []
    total = len(segments)
    for index, segment in enumerate(segments):
        visual = generated[index] if index < len(generated) else {}
        fallback = _fallback_visual(segment, title, index)
        provider = str(visual.get("provider", fallback["provider"])).lower()
        if provider not in {"stock", "generated"}:
            provider = "generated"
        plan.append({
            "scene_number": index + 1,
            "spoken_text": segment,
            "intent": infer_intent(segment, index, total),
            "visual_prompt": str(visual.get("visual_prompt") or fallback["visual_prompt"]).strip(),
            "pexels_query": str(visual.get("pexels_query") or fallback["pexels_query"]).strip(),
            "provider": provider,
            "duration_weight": max(1, len(segment.split())),
        })
    return plan


def save_scene_plan(plan: list[dict], path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def scaled_scene_durations(plan: list[dict], total_duration: float) -> list[float]:
    """Distribuisce la durata audio sulle scene usando il numero di parole."""
    has_audio_timings = bool(plan) and all(float(scene.get("audio_duration", 0)) > 0 for scene in plan)
    weights = [
        max(0.01, float(scene["audio_duration"]))
        if has_audio_timings
        else max(1.0, float(scene.get("duration_weight", 1)))
        for scene in plan
    ]
    total_weight = sum(weights)
    durations = [total_duration * weight / total_weight for weight in weights]
    if durations:
        durations[-1] += total_duration - sum(durations)
    return durations
