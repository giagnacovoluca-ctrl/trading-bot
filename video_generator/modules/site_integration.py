"""Validazione del contratto con il repository Conscia-Mente."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath


MANIFEST_PREFIX = "GENERATED_FILES_JSON:"
ALLOWED_GENERATED_ROOTS = ("src/content/articles", "public/images")


def parse_generated_manifest(stdout: str, repository: Path) -> list[str]:
    """Estrae e valida i soli file che il generatore dichiara di aver creato."""
    line = next(
        (item for item in stdout.splitlines() if item.startswith(MANIFEST_PREFIX)),
        None,
    )
    if line is None:
        raise ValueError("Il generatore non ha restituito il manifest dei file creati")

    try:
        manifest = json.loads(line.removeprefix(MANIFEST_PREFIX))
    except json.JSONDecodeError as exc:
        raise ValueError("Manifest JSON non valido") from exc

    if not isinstance(manifest, list) or not manifest:
        raise ValueError("Il manifest deve contenere almeno un file")

    repository = repository.resolve(strict=True)
    validated: list[str] = []
    for item in manifest:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"Percorso non valido nel manifest: {item!r}")

        relative = PurePosixPath(item)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Percorso non sicuro nel manifest: {item}")
        if not any(relative.is_relative_to(root) for root in ALLOWED_GENERATED_ROOTS):
            raise ValueError(f"Percorso fuori dalle directory consentite: {item}")

        target = (repository / Path(*relative.parts)).resolve(strict=True)
        if not target.is_relative_to(repository):
            raise ValueError(f"Percorso fuori dal repository: {item}")
        validated.append(relative.as_posix())

    if len(validated) != len(set(validated)):
        raise ValueError("Il manifest contiene percorsi duplicati")
    return validated
