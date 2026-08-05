import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional

from database import get_db
from models import AlertRule, User
from routers.auth import get_current_user


router = APIRouter()


class AlertRuleRequest(BaseModel):
    name: str
    project_id: Optional[str] = None
    metric: str           # cost_usd, tokens, error_rate, latency_ms
    operator: str         # gt, gte, lt, lte
    threshold: float
    window: str = "24h"   # 1h, 24h, 7d, 30d
    notify_email: Optional[str] = None
    notify_webhook: Optional[str] = None


@router.get("/")
async def list_alerts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AlertRule).where(AlertRule.org_id == current_user.org_id)
    )
    rules = result.scalars().all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "metric": r.metric,
            "operator": r.operator,
            "threshold": r.threshold,
            "window": r.window,
            "notify_email": r.notify_email,
            "is_active": r.is_active,
            "last_triggered_at": r.last_triggered_at.isoformat() if r.last_triggered_at else None,
        }
        for r in rules
    ]


@router.post("/")
async def create_alert(
    req: AlertRuleRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    allowed_metrics = {"cost_usd", "tokens", "error_rate", "latency_ms"}
    allowed_operators = {"gt", "gte", "lt", "lte"}
    if req.metric not in allowed_metrics:
        raise HTTPException(status_code=400, detail=f"metric must be one of {allowed_metrics}")
    if req.operator not in allowed_operators:
        raise HTTPException(status_code=400, detail=f"operator must be one of {allowed_operators}")

    rule = AlertRule(
        id=str(uuid.uuid4()),
        org_id=current_user.org_id,
        project_id=req.project_id,
        name=req.name,
        metric=req.metric,
        operator=req.operator,
        threshold=req.threshold,
        window=req.window,
        notify_email=req.notify_email,
        notify_webhook=req.notify_webhook,
    )
    db.add(rule)
    await db.flush()
    return {"id": rule.id, "name": rule.name}


@router.patch("/{rule_id}/toggle")
async def toggle_alert(
    rule_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AlertRule).where(AlertRule.id == rule_id, AlertRule.org_id == current_user.org_id)
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    rule.is_active = not rule.is_active
    return {"is_active": rule.is_active}


@router.delete("/{rule_id}")
async def delete_alert(
    rule_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AlertRule).where(AlertRule.id == rule_id, AlertRule.org_id == current_user.org_id)
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Alert rule not found")
    await db.delete(rule)
    return {"ok": True}
