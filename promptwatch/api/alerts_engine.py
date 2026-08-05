"""
Alert evaluation engine.
Queries aggregated metrics and fires notifications when thresholds are breached.
"""
import logging
import httpx
import aiosmtplib
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from models import AlertRule, LLMEvent
from config import settings

log = logging.getLogger(__name__)

WINDOW_DELTA = {
    "1h":  timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d":  timedelta(days=7),
    "30d": timedelta(days=30),
}

OPERATORS = {
    "gt":  lambda v, t: v > t,
    "gte": lambda v, t: v >= t,
    "lt":  lambda v, t: v < t,
    "lte": lambda v, t: v <= t,
}


async def _get_metric_value(rule: AlertRule, db: AsyncSession) -> float:
    since = datetime.now(timezone.utc) - WINDOW_DELTA.get(rule.window, timedelta(hours=24))
    filters = [LLMEvent.org_id == rule.org_id, LLMEvent.created_at >= since]
    if rule.project_id:
        filters.append(LLMEvent.project_id == rule.project_id)

    if rule.metric == "cost_usd":
        result = await db.execute(
            select(func.sum(LLMEvent.cost_usd)).where(and_(*filters))
        )
        return float(result.scalar() or 0)

    elif rule.metric == "tokens":
        result = await db.execute(
            select(func.sum(LLMEvent.total_tokens)).where(and_(*filters))
        )
        return float(result.scalar() or 0)

    elif rule.metric == "error_rate":
        result = await db.execute(
            select(
                func.count().label("total"),
                func.sum(func.case((LLMEvent.error.isnot(None), 1), else_=0)).label("errors"),
            ).where(and_(*filters))
        )
        row = result.one()
        if not row.total:
            return 0.0
        return float(row.errors or 0) / float(row.total) * 100

    elif rule.metric == "latency_ms":
        result = await db.execute(
            select(func.avg(LLMEvent.latency_ms)).where(and_(*filters))
        )
        return float(result.scalar() or 0)

    return 0.0


async def evaluate_and_fire(rule: AlertRule, db: AsyncSession):
    value = await _get_metric_value(rule, db)
    op = OPERATORS.get(rule.operator)
    if not op:
        return

    if not op(value, rule.threshold):
        return

    # Cooldown: don't re-fire within same window
    if rule.last_triggered_at:
        cooldown = WINDOW_DELTA.get(rule.window, timedelta(hours=24))
        if datetime.now(timezone.utc) - rule.last_triggered_at < cooldown:
            return

    rule.last_triggered_at = datetime.now(timezone.utc)

    subject = f"[PromptWatch] Alert: {rule.name}"
    body = (
        f"Alert triggered: {rule.name}\n"
        f"Metric: {rule.metric} = {value:.4f} (threshold: {rule.operator} {rule.threshold})\n"
        f"Window: {rule.window}\n"
        f"Time: {rule.last_triggered_at.isoformat()}"
    )
    log.info(f"Firing alert {rule.id}: {body}")

    if rule.notify_email and settings.SMTP_USER:
        await _send_email(rule.notify_email, subject, body)

    if rule.notify_webhook:
        await _send_webhook(rule.notify_webhook, rule, value)


async def _send_email(to: str, subject: str, body: str):
    try:
        msg = EmailMessage()
        msg["From"] = settings.FROM_EMAIL
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            start_tls=True,
        )
    except Exception as e:
        log.error(f"Email send failed to {to}: {e}")


async def _send_webhook(url: str, rule: AlertRule, value: float):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={
                "alert_id": rule.id,
                "alert_name": rule.name,
                "metric": rule.metric,
                "value": value,
                "threshold": rule.threshold,
                "operator": rule.operator,
                "window": rule.window,
                "triggered_at": rule.last_triggered_at.isoformat(),
            })
    except Exception as e:
        log.error(f"Webhook failed to {url}: {e}")
