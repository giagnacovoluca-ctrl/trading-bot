"""Controlli locali e deterministici sulla qualità dei copioni."""

from __future__ import annotations

import re
from dataclasses import dataclass


FORBIDDEN_CLAIMS = re.compile(
    r"\b(cura|curare|guarisce|elimina|garantisce|disintossica|ripara il dna|"
    r"abbassa sicuramente|sconfigge l'ansia|dimagrire senza sforzo)\b",
    re.IGNORECASE,
)
CTA_TERMS = re.compile(
    r"\b(commenta|scrivi|salva|prova|scopri|leggi|condividi|sperimenta)\b",
    re.IGNORECASE,
)
META_FIELDS = ("FATTO_CENTRALE", "TIPO_EVIDENZA", "LIMITE_EVIDENZA", "ANGOLO_NARRATIVO")


@dataclass(frozen=True)
class QualityReport:
    score: int
    issues: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.score >= 7 and not self.issues


def extract_metadata(script: str) -> dict[str, str]:
    metadata = {}
    for field in ("FONTE_NOTIZIA", *META_FIELDS):
        match = re.search(rf"^{field}:\s*(.+)$", script, re.IGNORECASE | re.MULTILINE)
        if match:
            metadata[field] = match.group(1).strip()
    return metadata


def validate_script(hook_title: str, script_text: str, metadata: dict[str, str] | None = None) -> QualityReport:
    metadata = metadata or {}
    words = script_text.split()
    sentences = [part for part in re.split(r"[.!?]+", script_text) if part.strip()]
    issues: list[str] = []

    if not 70 <= len(words) <= 220:
        issues.append(f"lunghezza fuori soglia ({len(words)} parole)")
    if sentences and max(len(sentence.split()) for sentence in sentences) > 28:
        issues.append("almeno una frase supera 28 parole")
    if not hook_title.strip() or len(hook_title.split()) > 8:
        issues.append("titolo assente o troppo lungo")
    if FORBIDDEN_CLAIMS.search(script_text):
        issues.append("promessa sanitaria assoluta o non verificabile")
    if not CTA_TERMS.search(script_text):
        issues.append("CTA pratica assente")
    if not metadata.get("FONTE_NOTIZIA") or metadata["FONTE_NOTIZIA"].lower() in {"nessuna", "skip"}:
        issues.append("fonte assente")
    for field in META_FIELDS:
        value = metadata.get(field, "").strip()
        if not value or value.startswith("<") or value.lower() in {"n/a", "da compilare", "tbd"}:
            issues.append(f"metadato {field} assente")

    score = 10 - min(10, len(issues) * 2)
    return QualityReport(score=score, issues=tuple(issues))
