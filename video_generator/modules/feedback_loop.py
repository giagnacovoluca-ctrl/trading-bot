"""
modules/feedback_loop.py

Feedback Loop per il sistema TikTok video generator.
Legge upload_log.json e usa le performance passate per:
1. Pesare la selezione dei topic futuri (categorie che performano meglio hanno più probabilità)
2. Garantire varietà (boost alle categorie meno usate di recente)
3. Loggare statistiche aggregate per analisi manuale
"""

from __future__ import annotations
import json
import datetime
from collections import defaultdict
from pathlib import Path

LOG_PATH = Path(__file__).parent.parent / "output" / "upload_log.json"
LEADS_PATH = Path("/home/ubuntu/vps_services/conscia_leads/leads.jsonl")


def _load_upload_log() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    entries = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def get_recent_published(limit: int = 30) -> list[dict]:
    """Restituisce solo pubblicazioni riuscite, dalla più vecchia alla più recente."""
    successful = [entry for entry in _load_upload_log() if entry.get("success") is True]
    return successful[-limit:]


def _lead_counts_by_campaign() -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    if not LEADS_PATH.exists():
        return counts
    for line in LEADS_PATH.read_text(encoding="utf-8").splitlines():
        try:
            lead = json.loads(line)
            campaign_id = lead.get("attribution", {}).get("utm_content")
            if campaign_id:
                counts[campaign_id] += 1
        except (json.JSONDecodeError, AttributeError):
            continue
    return counts


def get_topic_weights(all_categories: list[str]) -> dict[str, float]:
    """
    Calcola un peso per ogni categoria basandosi su:
    1. Views/likes medi (se disponibili nel log)
    2. Frequenza d'uso recente (boost alle categorie meno usate)
    Ritorna un dict {categoria: peso} normalizzato (somma = 1.0).
    """
    entries = get_recent_published(20)
    recent_entries = entries[-20:] if len(entries) > 20 else entries
    category_count: dict[str, int] = defaultdict(int)
    category_views: dict[str, list[int]] = defaultdict(list)
    category_leads: dict[str, int] = defaultdict(int)
    campaign_leads = _lead_counts_by_campaign()

    for entry in recent_entries:
        cat = entry.get("category", "")
        if cat:
            category_count[cat] += 1
            views = entry.get("views", 0)
            if views:
                category_views[cat].append(views)
            category_leads[cat] += campaign_leads.get(entry.get("campaign_id", ""), 0)

    weights: dict[str, float] = {}
    for cat in all_categories:
        base_weight = 1.0
        # Penalità per overuse (-15% per ogni uso recente, min 20%)
        uses = category_count.get(cat, 0)
        recency_penalty = max(0.2, 1.0 - (uses * 0.15))
        # Bonus performance se ci sono views reali
        views_list = category_views.get(cat, [])
        if views_list:
            avg_views = sum(views_list) / len(views_list)
            perf_bonus = min(2.0, avg_views / 10000)  # baseline 10k views
            base_weight *= (1.0 + perf_bonus)
        # Un lead vale più di una view: bonus limitato per evitare che un solo
        # contenuto monopolizzi la rotazione editoriale.
        base_weight *= 1.0 + min(1.5, category_leads.get(cat, 0) * 0.5)
        weights[cat] = base_weight * recency_penalty

    total = sum(weights.values())
    if total > 0:
        return {k: v / total for k, v in weights.items()}
    return {k: 1.0 / len(all_categories) for k in all_categories}


def log_upload(
    video_file: str,
    hook_title: str,
    category: str,
    mode: str,
    quality_score: int,
    fonte: str,
    success: bool,
    topic: str = "",
    views: int = 0,
    likes: int = 0,
    comments: int = 0,
    shares: int = 0,
    platform: str = "tiktok",
    resource_id: str = "",
    delivery_type: str = "",
) -> None:
    """Logga un upload con metadati estesi per il feedback loop."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "video_file": video_file,
        "hook_title": hook_title,
        "topic": topic,
        "category": category,
        "mode": mode,
        "quality_score": quality_score,
        "fonte_notizia": fonte,
        "success": success,
        "platform": platform,
        "views": views,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "resource_id": resource_id,
        "delivery_type": delivery_type,
    }
    try:
        from modules.content_tracking import current_campaign
        campaign = current_campaign(platform)
        if campaign:
            entry["campaign_id"] = campaign["campaign_id"]
            entry["tracking_url"] = campaign["tracking_url"]
            entry["landing_focus"] = campaign["focus"]
    except (ImportError, KeyError, ValueError):
        pass
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False)
        f.write("\n")


def update_recent_tiktok_views(latest_views: list[int]) -> int:
    """Associa le view del profilo (più recenti prima) ai nuovi upload TikTok.

    Sono aggiornate solo le entry con ``platform=tiktok``: questo evita di
    attribuire per errore un Reel Instagram ai video del profilo TikTok.
    """
    entries = _load_upload_log()
    candidates = [
        index for index, entry in enumerate(entries)
        if entry.get("success") is True and entry.get("platform") == "tiktok"
    ]
    updated = 0
    for view_count, entry_index in zip(latest_views, reversed(candidates)):
        if isinstance(view_count, int) and view_count >= 0:
            entries[entry_index]["views"] = view_count
            entries[entry_index]["metrics_updated_at"] = datetime.datetime.now().isoformat()
            updated += 1
    if updated:
        temp_path = LOG_PATH.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as output:
            for entry in entries:
                json.dump(entry, output, ensure_ascii=False)
                output.write("\n")
        temp_path.replace(LOG_PATH)
    return updated


def print_performance_report() -> None:
    """Stampa un report delle performance per categoria."""
    entries = _load_upload_log()
    if not entries:
        print("Nessun dato nel log.")
        return
    stats: dict[str, dict] = defaultdict(lambda: {"count": 0, "total_views": 0, "total_likes": 0, "scores": []})
    for e in entries:
        cat = e.get("category", "Sconosciuta")
        stats[cat]["count"] += 1
        stats[cat]["total_views"] += e.get("views", 0)
        stats[cat]["total_likes"] += e.get("likes", 0)
        if e.get("quality_score"):
            stats[cat]["scores"].append(e["quality_score"])
    print("\n📊 Performance per Categoria:")
    print("-" * 65)
    for cat, s in sorted(stats.items(), key=lambda x: x[1]["total_views"], reverse=True):
        avg_score = sum(s["scores"]) / len(s["scores"]) if s["scores"] else 0
        avg_views = s["total_views"] / s["count"] if s["count"] else 0
        print(f"  {cat[:35]:<35} | {s['count']:>3} video | avg {avg_views:>7,.0f} views | score {avg_score:.1f}/10")
    print("-" * 65)


if __name__ == "__main__":
    print_performance_report()
