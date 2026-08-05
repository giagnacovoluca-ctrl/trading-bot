"""
Ingest endpoint — receives LLM call events from the SDK.
High-throughput: validated quickly, written async, worker aggregates.
"""
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field
from typing import Optional
import uuid
from datetime import datetime, timezone

from database import get_db
from models import LLMEvent, Project, Organization, Plan
from pricing import compute_cost


router = APIRouter()


class EventPayload(BaseModel):
    provider: str
    model: str
    endpoint: str = "chat"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: Optional[int] = None
    error: Optional[str] = None
    cached: bool = False
    user_id: Optional[str] = None
    feature: Optional[str] = None
    prompt_name: Optional[str] = None
    prompt_version: Optional[str] = None
    metadata: Optional[dict] = None
    timestamp: Optional[str] = None


class BatchPayload(BaseModel):
    events: list[EventPayload] = Field(..., max_length=100)


async def _get_project(api_key: str, db: AsyncSession) -> tuple[Project, Organization]:
    result = await db.execute(
        select(Project, Organization)
        .join(Organization, Project.org_id == Organization.id)
        .where(Project.api_key == api_key)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return row.Project, row.Organization


@router.post("/events")
async def ingest_event(
    payload: EventPayload,
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    project, org = await _get_project(x_api_key, db)
    event = _build_event(payload, project, org)
    db.add(event)
    return {"id": event.id, "cost_usd": event.cost_usd}


@router.post("/events/batch")
async def ingest_batch(
    payload: BatchPayload,
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    project, org = await _get_project(x_api_key, db)
    events = [_build_event(e, project, org) for e in payload.events]
    db.add_all(events)
    total_cost = sum(e.cost_usd for e in events)
    return {"count": len(events), "total_cost_usd": total_cost}


def _build_event(payload: EventPayload, project: Project, org: Organization) -> LLMEvent:
    total_tokens = payload.prompt_tokens + payload.completion_tokens
    cost = compute_cost(payload.provider, payload.model, payload.prompt_tokens, payload.completion_tokens)

    # Strip metadata for non-Growth plans
    meta = None
    if org.plan in (Plan.GROWTH, Plan.SCALE):
        meta = payload.metadata

    return LLMEvent(
        id=str(uuid.uuid4()),
        project_id=project.id,
        org_id=org.id,
        provider=payload.provider,
        model=payload.model,
        endpoint=payload.endpoint,
        prompt_tokens=payload.prompt_tokens,
        completion_tokens=payload.completion_tokens,
        total_tokens=total_tokens,
        cost_usd=cost,
        user_id=payload.user_id,
        feature=payload.feature,
        prompt_name=payload.prompt_name,
        prompt_version=payload.prompt_version,
        latency_ms=payload.latency_ms,
        error=payload.error,
        cached=payload.cached,
        metadata=meta,
    )
