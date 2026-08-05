"""
Background worker: alert evaluation, daily aggregations.
Runs as a separate process: python -m worker
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, and_
from database import AsyncSessionLocal
from models import AlertRule, LLMEvent, Organization
from alerts_engine import evaluate_and_fire

logging.basicConfig(level=logging.INFO, format="%(asctime)s [worker] %(message)s")
log = logging.getLogger(__name__)

ALERT_CHECK_INTERVAL = 300   # every 5 minutes
AGGREGATE_INTERVAL   = 3600  # every hour


async def check_alerts():
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AlertRule).where(AlertRule.is_active == True)
        )
        rules = result.scalars().all()
        for rule in rules:
            try:
                await evaluate_and_fire(rule, db)
            except Exception as e:
                log.error(f"Alert rule {rule.id} evaluation failed: {e}")


async def alert_loop():
    while True:
        try:
            await check_alerts()
        except Exception as e:
            log.error(f"Alert loop error: {e}")
        await asyncio.sleep(ALERT_CHECK_INTERVAL)


async def main():
    log.info("Worker started")
    await asyncio.gather(
        alert_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
