"""Controlli locali e deterministici sulla qualità dei copioni."""

from __future__ import annotations

import re
from dataclasses import dataclass


FORBIDDEN_CLAIMS = re.compile(
    r"\b(cura|curare|guarisce|elimina|garantisce|disintossica|ripara il dna|"
    r"abbassa sicuramente|sconfigge l'ansia|dimagrire senza sforzo)\b",
    re.IGNORECASE,
)
MISINFO_RISK_TERMS = re.compile(
    r"\b(prova definitiva|prova inconfutabile|dimostra definitivamente|"
    r"che non ti dicono|verit[aà] nascosta|nessuno te lo dice|"
    r"sempre vero|senza alcun dubbio)\b",
    re.IGNORECASE,
)
UNSOURCED_ANALOGIES = re.compile(
    r"\b(?:pari a|equivale(?:nte)? a|come)\s+(?:\w+\s+){0,5}"
    r"(?:bombe?|esplosioni? nucleari|hiroshima)\b|"
    r"\b(?:bombe?|esplosioni? nucleari|hiroshima)\s+(?:\w+\s+){0,5}"
    r"(?:al secondo|al minuto|ogni secondo|ogni minuto)\b",
    re.IGNORECASE,
)
AGGRESSIVE_CONTRARIAN_TERMS = re.compile(
    r"\b(ti hanno mentito|ti stanno mentendo|tutti sbagliano|"
    r"(?:una |un')?enorme bugia|la (?:scienza|matematica) [eè] rotta|"
    r"peggiore (?:scelta )?della tua vita|guerra chimica spietata|"
    r"annientare tutto|ignoranti)\b",
    re.IGNORECASE,
)
GENERIC_SOURCES = re.compile(
    r"^(?:letteratura scientifica(?: (?:consolidata|su))?|"
    r"fatto scientifico consolidato|fonti scientifiche|studi scientifici)\b",
    re.IGNORECASE,
)
CTA_TERMS = re.compile(
    r"\b(commenta|scrivi|salva|prova|scopri|leggi|condividi|sperimenta|usa|cerca|pensa|chiedi|guarda|trova|inizia|segui|seguimi|seguici)\b",
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
    issues.extend(validate_publication_text(script_text))
    if not CTA_TERMS.search(script_text):
        issues.append("CTA pratica assente")
    if not metadata.get("FONTE_NOTIZIA") or metadata["FONTE_NOTIZIA"].lower() in {"nessuna", "skip"}:
        issues.append("fonte assente")
    elif GENERIC_SOURCES.search(metadata["FONTE_NOTIZIA"].strip()):
        issues.append("fonte troppo generica: indicare ente, documento o studio verificabile")
    for field in META_FIELDS:
        value = metadata.get(field, "").strip()
        if not value or value.startswith("<") or value.lower() in {"n/a", "da compilare", "tbd"}:
            issues.append(f"metadato {field} assente")

    score = 10 - min(10, len(issues) * 2)
    return QualityReport(score=score, issues=tuple(issues))


def validate_publication_text(text: str) -> tuple[str, ...]:
    """Controlla il testo che gli utenti vedranno, inclusa la caption.

    È volutamente deterministico: se il validatore remoto non nota un claim
    rischioso, la pubblicazione viene comunque fermata.
    """
    issues: list[str] = []
    if re.search(r"(?:\b(?:cura|curare|guarisce|guarire)\s+(?:l['’]|la |il |un['’]?)?(?:ansia|depressione|insonnia|cancro)|auto[- ]?guarigione|riprogramma(?:re)?\s+(?:il |tuo |il tuo )?DNA|confini biologici della tua meditazione|cortisolo.{0,30}(?:azzera|elimina)|(?:azzera|elimina).{0,30}cortisolo)", text, re.I):
        issues.append("promessa fisiologica o terapeutica non sostenibile")
    if MISINFO_RISK_TERMS.search(text):
        issues.append("affermazione assoluta o retorica da disinformazione")
    if UNSOURCED_ANALOGIES.search(text):
        issues.append("analogia quantitativa sensibile senza fonte esplicita")
    if AGGRESSIVE_CONTRARIAN_TERMS.search(text):
        issues.append("retorica contrariana aggressiva o denigratoria")
    return tuple(issues)
