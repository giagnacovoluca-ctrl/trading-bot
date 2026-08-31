"""ID e link brevi per collegare pubblicazioni social, visite e lead."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "output"
REGISTRY_PATH = OUTPUT_DIR / "campaign_log.jsonl"

FOCUS_KEYWORDS = {
    "stress": ("stress", "nervo vago", "ansia", "respiro", "meditazione", "calma", "sonno"),
    "energia": ("acqua", "idrata", "energia", "stanco", "stanchezza", "cibo", "aliment", "integrator"),
    "identita": ("numerolog", "identità", "identita", "personalità", "personalita", "coppia", "destino"),
}
PLATFORM_CODES = {"instagram": "ig", "ig": "ig", "tiktok": "tt"}


def choose_focus(text: str) -> str:
    normalized = (text or "").lower()
    scores = {focus: sum(normalized.count(word) for word in words) for focus, words in FOCUS_KEYWORDS.items()}
    winner = max(scores, key=scores.get)
    return winner if scores[winner] else "risorse"


def create_campaign(platform: str, text: str, mode: str = "", now: dt.datetime | None = None) -> dict:
    code = PLATFORM_CODES.get(platform.lower())
    if not code:
        raise ValueError(f"Piattaforma non supportata: {platform}")
    timestamp = now or dt.datetime.now(dt.timezone.utc)
    focus = choose_focus(text)
    digest = hashlib.sha256(f"{platform}|{mode}|{text}|{timestamp.isoformat()}".encode()).hexdigest()[:8]
    campaign_id = f"{focus}-{code}-{digest}"
    return {
        "campaign_id": campaign_id,
        "platform": "instagram" if code == "ig" else "tiktok",
        "focus": focus,
        "mode": mode,
        "tracking_url": f"https://conscia-mente.vercel.app/c/{campaign_id}",
        "created_at": timestamp.isoformat(),
        "status": "prepared",
    }


def append_tracking(caption: str, campaign: dict) -> str:
    """Mantiene la caption pulita: gli URL nelle caption social non sono cliccabili.

    La campagna resta nel registro interno; il traffico pubblico usa il link in
    bio o, quando disponibile, uno sticker cliccabile creato dalla piattaforma.
    """
    del campaign
    return (caption or "").strip()


def save_campaign(campaign: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with REGISTRY_PATH.open("a", encoding="utf-8") as handle:
        json.dump(campaign, handle, ensure_ascii=False)
        handle.write("\n")
    current = OUTPUT_DIR / f"current_campaign_{campaign['platform']}.json"
    current.write_text(json.dumps(campaign, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare_tracked_caption(caption: str, platform: str, source_text: str, mode: str = "") -> tuple[str, dict]:
    campaign = create_campaign(platform, source_text, mode)
    save_campaign(campaign)
    return append_tracking(caption, campaign), campaign


def current_campaign(platform: str) -> dict | None:
    path = OUTPUT_DIR / f"current_campaign_{platform}.json"
    if not path.exists():
        return None
    try:
        campaign = json.loads(path.read_text(encoding="utf-8"))
        created = dt.datetime.fromisoformat(campaign["created_at"])
        if dt.datetime.now(dt.timezone.utc) - created > dt.timedelta(hours=4):
            return None
        return campaign
    except (ValueError, KeyError, json.JSONDecodeError):
        return None
