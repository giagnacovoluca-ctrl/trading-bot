"""Importa gli insight ufficiali dei Reel Instagram nello storico editoriale."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from modules.feedback_loop import _load_upload_log, update_instagram_metrics
from modules.meta_config import graph_url

load_dotenv(Path(__file__).resolve().parent / '.env')

METRICS = "views,reach,likes,comments,shares,saved"


def _metric_values(media_id: str, token: str) -> dict[str, int]:
    response = requests.get(
        graph_url(f"{media_id}/insights"),
        params={"metric": METRICS, "access_token": token},
        timeout=30,
    )
    response.raise_for_status()
    result: dict[str, int] = {}
    for item in response.json().get("data", []):
        values = item.get("values", [])
        if values and isinstance(values[0].get("value"), int):
            result[item["name"]] = values[0]["value"]
    return result


def _recent_media(user_id: str, token: str) -> list[dict]:
    response = requests.get(
        graph_url(f"{user_id}/media"),
        params={
            "fields": "id,timestamp,media_product_type",
            "limit": 100,
            "access_token": token,
        },
        timeout=30,
    )
    response.raise_for_status()
    return [
        media for media in response.json().get("data", [])
        if media.get("media_product_type") == "REELS"
    ]


def _backfill_media_ids(media: list[dict]) -> int:
    """Collega gli upload storici, creati prima dell'introduzione del receipt.

    Il confronto usa il timestamp, non testo/caption, e accetta soltanto un
    candidato entro dieci minuti: evita attribuzioni arbitrarie.
    """
    entries = _load_upload_log()
    candidates = [entry for entry in entries if entry.get("platform") == "instagram" and not entry.get("media_id")]
    available = {item["id"]: item for item in media}
    linked = 0
    for entry in candidates:
        try:
            local_time = dt.datetime.fromisoformat(entry["timestamp"]).replace(tzinfo=ZoneInfo("Europe/Rome"))
        except (KeyError, ValueError):
            continue
        nearest_id = ""
        nearest_delta = dt.timedelta.max
        for media_id, item in available.items():
            try:
                published_at = dt.datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            delta = abs(published_at - local_time)
            if delta < nearest_delta:
                nearest_delta = delta
                nearest_id = media_id
        if nearest_id and nearest_delta <= dt.timedelta(minutes=10):
            entry["media_id"] = nearest_id
            available.pop(nearest_id)
            linked += 1
    if linked:
        from modules.feedback_loop import LOG_PATH
        temp_path = LOG_PATH.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as output:
            import json
            for entry in entries:
                json.dump(entry, output, ensure_ascii=False)
                output.write("\n")
        temp_path.replace(LOG_PATH)
    return linked


def sync_insights() -> tuple[int, int]:
    user_id = os.getenv("IG_USER_ID")
    token = os.getenv("IG_ACCESS_TOKEN")
    if not user_id or not token:
        raise RuntimeError("IG_USER_ID o IG_ACCESS_TOKEN mancanti")
    media = _recent_media(user_id, token)
    backfilled = _backfill_media_ids(media)
    tracked_ids = {
        str(entry.get("media_id"))
        for entry in _load_upload_log()
        if entry.get("platform") == "instagram" and entry.get("media_id")
    }
    metrics = {media_id: _metric_values(media_id, token) for media_id in tracked_ids}
    return backfilled, update_instagram_metrics(metrics)


if __name__ == "__main__":
    linked, updated = sync_insights()
    print(f"Instagram: {linked} ID storici collegati, {updated} Reel aggiornati con insight Meta.")
