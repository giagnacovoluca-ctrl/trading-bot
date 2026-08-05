"""
Dashboard API — aggregated stats for the frontend.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from datetime import datetime, timedelta, timezone, date

from database import get_db
from models import LLMEvent, User
from routers.auth import get_current_user


router = APIRouter()


def _parse_window(window: str) -> datetime:
    now = datetime.now(timezone.utc)
    mapping = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
    delta = mapping.get(window, timedelta(days=7))
    return now - delta


@router.get("/summary")
async def get_summary(
    project_id: str | None = Query(None),
    window: str = Query("30d", regex="^(1h|24h|7d|30d)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    since = _parse_window(window)
    filters = [LLMEvent.org_id == current_user.org_id, LLMEvent.created_at >= since]
    if project_id:
        filters.append(LLMEvent.project_id == project_id)

    result = await db.execute(
        select(
            func.count().label("total_requests"),
            func.sum(LLMEvent.total_tokens).label("total_tokens"),
            func.sum(LLMEvent.cost_usd).label("total_cost_usd"),
            func.avg(LLMEvent.latency_ms).label("avg_latency_ms"),
            func.sum(func.case((LLMEvent.error.isnot(None), 1), else_=0)).label("error_count"),
        ).where(and_(*filters))
    )
    row = result.one()
    return {
        "total_requests": row.total_requests or 0,
        "total_tokens": int(row.total_tokens or 0),
        "total_cost_usd": round(float(row.total_cost_usd or 0), 4),
        "avg_latency_ms": round(float(row.avg_latency_ms or 0), 1),
        "error_rate": round((row.error_count or 0) / max(row.total_requests or 1, 1) * 100, 2),
        "window": window,
    }


@router.get("/cost-by-model")
async def cost_by_model(
    project_id: str | None = Query(None),
    window: str = Query("30d"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    since = _parse_window(window)
    filters = [LLMEvent.org_id == current_user.org_id, LLMEvent.created_at >= since]
    if project_id:
        filters.append(LLMEvent.project_id == project_id)

    result = await db.execute(
        select(
            LLMEvent.provider,
            LLMEvent.model,
            func.count().label("requests"),
            func.sum(LLMEvent.cost_usd).label("cost_usd"),
            func.sum(LLMEvent.total_tokens).label("tokens"),
        )
        .where(and_(*filters))
        .group_by(LLMEvent.provider, LLMEvent.model)
        .order_by(func.sum(LLMEvent.cost_usd).desc())
    )
    return [
        {
            "provider": r.provider,
            "model": r.model,
            "requests": r.requests,
            "cost_usd": round(float(r.cost_usd or 0), 4),
            "tokens": int(r.tokens or 0),
        }
        for r in result.all()
    ]


@router.get("/cost-by-feature")
async def cost_by_feature(
    project_id: str | None = Query(None),
    window: str = Query("30d"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    since = _parse_window(window)
    filters = [LLMEvent.org_id == current_user.org_id, LLMEvent.created_at >= since]
    if project_id:
        filters.append(LLMEvent.project_id == project_id)

    result = await db.execute(
        select(
            LLMEvent.feature,
            func.count().label("requests"),
            func.sum(LLMEvent.cost_usd).label("cost_usd"),
        )
        .where(and_(*filters))
        .group_by(LLMEvent.feature)
        .order_by(func.sum(LLMEvent.cost_usd).desc())
        .limit(20)
    )
    return [
        {"feature": r.feature or "untagged", "requests": r.requests, "cost_usd": round(float(r.cost_usd or 0), 4)}
        for r in result.all()
    ]


@router.get("/daily-cost")
async def daily_cost(
    project_id: str | None = Query(None),
    days: int = Query(30, ge=1, le=90),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    filters = [LLMEvent.org_id == current_user.org_id, LLMEvent.created_at >= since]
    if project_id:
        filters.append(LLMEvent.project_id == project_id)

    result = await db.execute(
        select(
            func.date_trunc("day", LLMEvent.created_at).label("day"),
            func.sum(LLMEvent.cost_usd).label("cost_usd"),
            func.count().label("requests"),
        )
        .where(and_(*filters))
        .group_by(func.date_trunc("day", LLMEvent.created_at))
        .order_by(func.date_trunc("day", LLMEvent.created_at))
    )
    return [
        {
            "date": r.day.strftime("%Y-%m-%d"),
            "cost_usd": round(float(r.cost_usd or 0), 4),
            "requests": r.requests,
        }
        for r in result.all()
    ]


@router.get("/recent-events")
async def recent_events(
    project_id: str | None = Query(None),
    limit: int = Query(50, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    filters = [LLMEvent.org_id == current_user.org_id]
    if project_id:
        filters.append(LLMEvent.project_id == project_id)

    result = await db.execute(
        select(LLMEvent)
        .where(and_(*filters))
        .order_by(LLMEvent.created_at.desc())
        .limit(limit)
    )
    events = result.scalars().all()
    return [
        {
            "id": e.id,
            "provider": e.provider,
            "model": e.model,
            "feature": e.feature,
            "user_id": e.user_id,
            "prompt_tokens": e.prompt_tokens,
            "completion_tokens": e.completion_tokens,
            "cost_usd": e.cost_usd,
            "latency_ms": e.latency_ms,
            "error": e.error,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]
