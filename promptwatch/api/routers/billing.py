"""Stripe billing integration."""
import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import get_db
from models import Organization, User, Plan
from routers.auth import get_current_user
from config import settings


router = APIRouter()

PRICE_TO_PLAN = {
    settings.STRIPE_STARTER_PRICE_ID: Plan.STARTER,
    settings.STRIPE_GROWTH_PRICE_ID: Plan.GROWTH,
    settings.STRIPE_SCALE_PRICE_ID: Plan.SCALE,
}

PLAN_PRICES = {
    Plan.STARTER: settings.STRIPE_STARTER_PRICE_ID,
    Plan.GROWTH:  settings.STRIPE_GROWTH_PRICE_ID,
    Plan.SCALE:   settings.STRIPE_SCALE_PRICE_ID,
}


@router.post("/checkout/{plan}")
async def create_checkout(
    plan: Plan,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if plan == Plan.FREE:
        raise HTTPException(status_code=400, detail="Cannot checkout FREE plan")
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Billing not configured")

    stripe.api_key = settings.STRIPE_SECRET_KEY
    price_id = PLAN_PRICES.get(plan)
    if not price_id:
        raise HTTPException(status_code=400, detail="Invalid plan")

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        metadata={"org_id": current_user.org_id},
        success_url=f"{settings.BASE_URL}/dashboard?billing=success",
        cancel_url=f"{settings.BASE_URL}/dashboard?billing=cancel",
    )
    return {"url": session.url}


@router.post("/portal")
async def billing_portal(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stripe.api_key = settings.STRIPE_SECRET_KEY
    result = await db.execute(select(Organization).where(Organization.id == current_user.org_id))
    org = result.scalar_one()
    if not org.stripe_customer_id:
        raise HTTPException(status_code=400, detail="No active subscription")

    session = stripe.billing_portal.Session.create(
        customer=org.stripe_customer_id,
        return_url=f"{settings.BASE_URL}/dashboard",
    )
    return {"url": session.url}


@router.post("/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(..., alias="stripe-signature")):
    if not settings.STRIPE_WEBHOOK_SECRET:
        return {"ok": True}

    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, settings.STRIPE_WEBHOOK_SECRET)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Import here to avoid circular dependency
    from database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            org_id = session["metadata"].get("org_id")
            if org_id:
                result = await db.execute(select(Organization).where(Organization.id == org_id))
                org = result.scalar_one_or_none()
                if org:
                    org.stripe_customer_id = session.get("customer")
                    org.stripe_subscription_id = session.get("subscription")
                    price_id = session["line_items"]["data"][0]["price"]["id"] if "line_items" in session else None
                    if price_id:
                        org.plan = PRICE_TO_PLAN.get(price_id, Plan.STARTER)
                    await db.commit()

        elif event["type"] == "customer.subscription.deleted":
            sub = event["data"]["object"]
            result = await db.execute(
                select(Organization).where(Organization.stripe_subscription_id == sub["id"])
            )
            org = result.scalar_one_or_none()
            if org:
                org.plan = Plan.FREE
                await db.commit()

    return {"ok": True}
