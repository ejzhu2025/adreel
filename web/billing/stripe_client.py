"""billing/stripe_client.py — Stripe Checkout Session creation and webhook handling."""
from __future__ import annotations

import os

import stripe

# ── Credentials (set via env vars) ───────────────────────────────────────────
# 1. Go to https://dashboard.stripe.com/apikeys
# 2. Copy "Secret key" → STRIPE_SECRET_KEY
# 3. Go to Webhooks → Add endpoint → copy "Signing secret" → STRIPE_WEBHOOK_SECRET
STRIPE_SECRET_KEY      = os.getenv("STRIPE_SECRET_KEY",      "sk_test_PLACEHOLDER")
STRIPE_WEBHOOK_SECRET  = os.getenv("STRIPE_WEBHOOK_SECRET",  "whsec_PLACEHOLDER")
STRIPE_SUCCESS_URL     = os.getenv("STRIPE_SUCCESS_URL",     "http://localhost:8000/?payment=success&session_id={CHECKOUT_SESSION_ID}")
STRIPE_CANCEL_URL      = os.getenv("STRIPE_CANCEL_URL",      "http://localhost:8000/?payment=cancel")

stripe.api_key = STRIPE_SECRET_KEY

# ── Credit plans ──────────────────────────────────────────────────────────────
# price_id comes from Stripe Dashboard → Products → Prices
# Placeholder IDs — replace with real ones after creating products in Stripe

PLANS: list[dict] = [
    {
        "id":          "starter",
        "name":        "Starter",
        "credits":     50,
        "price_usd":   5_00,           # cents
        "stripe_price_id": os.getenv("STRIPE_PRICE_STARTER", "price_PLACEHOLDER_starter"),
        "description": "~5 videos",
    },
    {
        "id":          "pro",
        "name":        "Pro",
        "credits":     200,
        "price_usd":   15_00,
        "stripe_price_id": os.getenv("STRIPE_PRICE_PRO", "price_PLACEHOLDER_pro"),
        "description": "~20 videos",
        "popular":     True,
    },
    {
        "id":          "studio",
        "name":        "Studio",
        "credits":     500,
        "price_usd":   30_00,
        "stripe_price_id": os.getenv("STRIPE_PRICE_STUDIO", "price_PLACEHOLDER_studio"),
        "description": "~50 videos",
    },
]

_plans_by_id = {p["id"]: p for p in PLANS}


def create_checkout_session(plan_id: str, user_id: str, user_email: str) -> str:
    """Create a Stripe Checkout Session and return the hosted URL."""
    plan = _plans_by_id.get(plan_id)
    if not plan:
        raise ValueError(f"Unknown plan: {plan_id}")

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": plan["stripe_price_id"], "quantity": 1}],
        mode="payment",
        customer_email=user_email,
        success_url=STRIPE_SUCCESS_URL,
        cancel_url=STRIPE_CANCEL_URL,
        metadata={"user_id": user_id, "plan_id": plan_id, "credits": plan["credits"]},
    )
    return session.url  # type: ignore[return-value]


def fetch_checkout_session(session_id: str) -> dict:
    """Fetch a Stripe Checkout Session and return relevant fields."""
    session = stripe.checkout.Session.retrieve(session_id)
    meta = session.get("metadata") or {}
    return {
        "id": session.id,
        "payment_status": session.payment_status,  # "paid" | "unpaid" | "no_payment_required"
        "status": session.status,                  # "open" | "complete" | "expired"
        "user_id": meta.get("user_id", ""),
        "plan_id": meta.get("plan_id", ""),
        "credits": int(meta.get("credits", 0)),
    }


def verify_webhook(payload: bytes, sig_header: str) -> stripe.Event:
    """Verify Stripe webhook signature. Raises on tampered/invalid payload."""
    return stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
