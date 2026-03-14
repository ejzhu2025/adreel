"""billing/router.py — /api/billing/* endpoints: plans, checkout, webhook."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from web.auth.deps import current_user
from web.auth.models import User
from web.billing.credits import fulfill_session, get_credits
from web.billing.stripe_client import PLANS, create_checkout_session, fetch_checkout_session, verify_webhook

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/billing", tags=["billing"])


# ── Plans ─────────────────────────────────────────────────────────────────────

@router.get("/plans")
def list_plans():
    """Return available credit packages (public — no auth needed)."""
    return [
        {k: v for k, v in p.items() if k != "stripe_price_id"}
        for p in PLANS
    ]


# ── Checkout ──────────────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    plan_id: str


@router.post("/checkout")
def create_checkout(body: CheckoutRequest, user: User = Depends(current_user)):
    """Create a Stripe Checkout Session and return the redirect URL."""
    try:
        url = create_checkout_session(body.plan_id, user.id, user.email)
        return {"url": url}
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.error("Stripe checkout error: %s", exc)
        raise HTTPException(502, "Payment service unavailable")


# ── Fulfill (no-webhook fallback) ─────────────────────────────────────────────

class FulfillRequest(BaseModel):
    session_id: str


@router.post("/fulfill")
def fulfill(body: FulfillRequest, user: User = Depends(current_user)):
    """Called by frontend after Stripe redirects back with ?session_id=...
    Verifies the session is paid and idempotently adds credits."""
    try:
        session = fetch_checkout_session(body.session_id)
    except Exception as exc:
        logger.error("Stripe session fetch error: %s", exc)
        raise HTTPException(502, "Could not verify payment")

    if session["payment_status"] != "paid":
        raise HTTPException(402, f"Payment not completed (status: {session['payment_status']})")

    # Security: verify this session belongs to the requesting user
    if session["user_id"] != user.id:
        raise HTTPException(403, "Session does not belong to this user")

    was_new, new_balance = fulfill_session(
        session_id=body.session_id,
        user_id=user.id,
        credits=session["credits"],
    )
    logger.info(
        "Fulfill session=%s user=%s credits=%d was_new=%s new_balance=%d",
        body.session_id, user.id, session["credits"], was_new, new_balance,
    )
    # Always return the plan's credit amount so the UI shows the correct figure
    # (webhook may have already fulfilled it, making was_new=False)
    return {"credits_added": session["credits"], "balance": new_balance}


# ── Balance ───────────────────────────────────────────────────────────────────

@router.get("/balance")
def balance(user: User = Depends(current_user)):
    return {"credits": get_credits(user.id)}


# ── Webhook ───────────────────────────────────────────────────────────────────

@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(default="", alias="stripe-signature"),
):
    """Stripe calls this after a successful payment. Adds credits to user."""
    payload = await request.body()
    try:
        event = verify_webhook(payload, stripe_signature)
    except Exception as exc:
        logger.warning("Webhook signature verification failed: %s", exc)
        raise HTTPException(400, "Invalid webhook signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        meta = session.get("metadata", {})
        user_id = meta.get("user_id")
        credits = int(meta.get("credits", 0))

        if user_id and credits > 0:
            was_new, new_balance = fulfill_session(
                session_id=session["id"],
                user_id=user_id,
                credits=credits,
            )
            if was_new:
                logger.info("Webhook: Credits added: user=%s credits=%d new_balance=%d",
                            user_id, credits, new_balance)
            else:
                logger.info("Webhook: Session already fulfilled (skipped): user=%s", user_id)

    # Always return 200 so Stripe doesn't retry
    return {"received": True}
