"""Catalogo canonico dei sei ebook ConsciaMente.

Il file sorgente vive nel progetto del sito, che lo usa anche durante la build.
Il generatore video legge lo stesso JSON sulla VPS per mantenere titoli, CTA,
destinazioni e tipologie di consegna perfettamente allineati.
"""

from __future__ import annotations

import json
from pathlib import Path

CATALOG_PATH = Path("/home/ubuntu/conscia-mente/src/data/ebooks.json")


def load_ebook_catalog() -> list[dict]:
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Catalogo ebook non disponibile: {CATALOG_PATH}") from exc
    if not isinstance(data, list) or len(data) != 6:
        raise ValueError("Il catalogo canonico deve contenere esattamente sei ebook")
    required = {
        "id", "title", "sourceFile", "deliveryType", "landingPath", "asin",
        "category", "socialWeight", "ctaInstagram", "ctaTikTok", "storyCta",
        "keywords", "promoTopics",
    }
    ids = set()
    for item in data:
        missing = required.difference(item)
        if missing:
            raise ValueError(f"Ebook senza campi obbligatori: {sorted(missing)}")
        if item["id"] in ids:
            raise ValueError(f"ID ebook duplicato: {item['id']}")
        if item["deliveryType"] not in {"pdf_email", "preview_online"}:
            raise ValueError(f"Tipologia di consegna non valida: {item['deliveryType']}")
        ids.add(item["id"])
    return data


def get_ebook(ebook_id: str) -> dict:
    for item in load_ebook_catalog():
        if item["id"] == ebook_id:
            return item
    raise KeyError(f"Ebook sconosciuto: {ebook_id}")


def get_ebook_by_title(title: str) -> dict:
    for item in load_ebook_catalog():
        if item["title"] == title:
            return item
    raise KeyError(f"Titolo ebook sconosciuto: {title}")


def ebook_to_rag(item: dict) -> dict:
    return {
        "id": item["id"],
        "titolo": item["title"],
        "file": Path("/home/ubuntu/ebooks") / item["sourceFile"],
        "argomenti": item["keywords"],
        "pitch": item["pitch"],
        "delivery_type": item["deliveryType"],
        "landing_path": item["landingPath"],
        "cta_instagram": item["ctaInstagram"],
        "cta_tiktok": item["ctaTikTok"],
        "promo_topics": item["promoTopics"],
        "social_weight": float(item["socialWeight"]),
        "category": item["category"],
    }
