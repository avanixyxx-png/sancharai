"""
Subscription tiers and their enforcement.

Ported from v2, with its central flaw removed. In v2, POST /api/billing/upgrade
set the company's plan to anything the caller asked for and wrote an invoice
marked "paid_demo" - no payment, no verification. Any logged-in user could hand
themselves the Rs 19,999 Enterprise plan with one request.

Here an upgrade is a two-step flow:
    start_upgrade()    -> writes a PENDING invoice, returns the amount to charge
    confirm_upgrade()  -> applies the plan ONLY after a verified Razorpay
                          signature (HMAC-SHA256 over "order_id|payment_id")

Demo grants are still possible for local development, but only when
SANCHARAI_ALLOW_DEMO_UPGRADES=1 is set explicitly, and every one is logged.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta

from sqlmodel import Session, func, select

from models import Agent, Call, Campaign, Company, Invoice, utcnow

PLANS: dict[str, dict] = {
    "free": {
        "name": "Free Trial",
        "price_inr": 0,
        "monthly_minutes": 30,
        "max_agents": 1,
        "max_campaigns": 1,
        "max_contacts": 50,
        "features": ["30 minutes/month", "1 AI agent", "1 campaign", "Browser demo"],
    },
    "starter": {
        "name": "Starter",
        "price_inr": 999,
        "monthly_minutes": 500,
        "max_agents": 3,
        "max_campaigns": 5,
        "max_contacts": 2_000,
        "features": ["500 minutes/month", "3 AI agents", "5 campaigns", "Email support"],
    },
    "pro": {
        "name": "Pro",
        "price_inr": 2_999,
        "monthly_minutes": 2_500,
        "max_agents": 10,
        "max_campaigns": 25,
        "max_contacts": 25_000,
        "features": [
            "2,500 minutes/month",
            "10 AI agents",
            "Call transcripts + outcomes",
            "Priority support",
        ],
    },
    "business": {
        "name": "Business",
        "price_inr": 7_999,
        "monthly_minutes": 10_000,
        "max_agents": 50,
        "max_campaigns": 200,
        "max_contacts": 200_000,
        "features": [
            "10,000 minutes/month",
            "50 AI agents",
            "Advanced analytics",
            "Team seats",
        ],
    },
    "enterprise": {
        "name": "Enterprise",
        "price_inr": 19_999,
        "monthly_minutes": 50_000,
        "max_agents": 999,
        "max_campaigns": 9_999,
        "max_contacts": 2_000_000,
        "features": ["50,000+ minutes/month", "Unlimited agents", "SLA 99.9%", "White-label"],
    },
}

PLAN_ORDER = ["free", "starter", "pro", "business", "enterprise"]


class PlanLimitError(Exception):
    """Raised when an action would exceed the company's plan. The message is
    written to be shown to the user as-is."""


def plan_of(company: Company) -> dict:
    return PLANS.get(company.plan or "free", PLANS["free"])


# --------------------------------------------------------------- period reset
def ensure_period(company: Company, db: Session) -> Company:
    """Roll the billing period forward and zero the minute counter when the
    month is up. Called on every dashboard load, so it self-heals even if no
    scheduler is running."""
    if company.period_end is None:
        company.period_start = utcnow()
        company.period_end = utcnow() + timedelta(days=30)
        db.add(company)
        db.commit()
        db.refresh(company)
        return company

    if utcnow() >= company.period_end:
        company.period_start = utcnow()
        company.period_end = utcnow() + timedelta(days=30)
        company.minutes_used = 0.0
        db.add(company)
        db.commit()
        db.refresh(company)
        print(f"[billing] period rolled over for company {company.id}", flush=True)
    return company


# ------------------------------------------------------------------- guards
def check_minutes(company: Company) -> None:
    limit = plan_of(company)["monthly_minutes"]
    if float(company.minutes_used or 0) >= limit:
        raise PlanLimitError(
            f"Monthly limit of {limit:,} minutes reached on the "
            f"{plan_of(company)['name']} plan. Upgrade to keep calling."
        )


def minutes_remaining(company: Company) -> float:
    return max(0.0, plan_of(company)["monthly_minutes"] - float(company.minutes_used or 0))


def check_agent_limit(company: Company, db: Session) -> None:
    limit = plan_of(company)["max_agents"]
    count = db.exec(
        select(func.count()).select_from(Agent).where(Agent.company_id == company.id)
    ).one()
    if count >= limit:
        raise PlanLimitError(
            f"Agent limit reached ({limit}) on the {plan_of(company)['name']} plan. "
            "Upgrade to add more."
        )


def check_campaign_limit(company: Company, db: Session) -> None:
    limit = plan_of(company)["max_campaigns"]
    count = db.exec(
        select(func.count()).select_from(Campaign).where(Campaign.company_id == company.id)
    ).one()
    if count >= limit:
        raise PlanLimitError(
            f"Campaign limit reached ({limit}) on the {plan_of(company)['name']} plan. "
            "Upgrade to add more."
        )


def check_contact_limit(company: Company, db: Session, adding: int) -> None:
    from models import Contact

    limit = plan_of(company)["max_contacts"]
    count = db.exec(
        select(func.count()).select_from(Contact).where(Contact.company_id == company.id)
    ).one()
    if count + adding > limit:
        raise PlanLimitError(
            f"Contact limit reached ({limit:,}) on the {plan_of(company)['name']} plan. "
            f"You have {count:,} and tried to add {adding:,}."
        )


def record_minutes(company_id: int, seconds: int, db: Session) -> None:
    """Bill call time against the company's monthly allowance."""
    if seconds <= 0:
        return
    company = db.get(Company, company_id)
    if not company:
        return
    company.minutes_used = round(float(company.minutes_used or 0) + seconds / 60.0, 3)
    db.add(company)
    db.commit()


def usage(company: Company) -> dict:
    plan = plan_of(company)
    used = float(company.minutes_used or 0)
    limit = plan["monthly_minutes"]
    return {
        "plan_id": company.plan or "free",
        "plan": plan,
        "minutes_used": round(used, 1),
        "minutes_limit": limit,
        "minutes_remaining": round(max(0.0, limit - used), 1),
        "usage_pct": round(min(100.0, used / limit * 100), 1) if limit else 0.0,
        "period_end": company.period_end,
    }


# ------------------------------------------------------------------ payments
def razorpay_configured() -> bool:
    return bool(os.getenv("RAZORPAY_KEY_ID") and os.getenv("RAZORPAY_KEY_SECRET"))


def demo_upgrades_allowed() -> bool:
    return os.getenv("SANCHARAI_ALLOW_DEMO_UPGRADES", "").strip() == "1"


def start_upgrade(company: Company, target: str, db: Session) -> Invoice:
    """Record the intent to upgrade. Grants nothing on its own."""
    if target not in PLANS:
        raise PlanLimitError("Unknown plan.")
    if target == (company.plan or "free"):
        raise PlanLimitError(f"You are already on the {PLANS[target]['name']} plan.")

    invoice = Invoice(
        company_id=company.id,
        plan=target,
        amount_inr=PLANS[target]["price_inr"],
        status="pending",
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return invoice


def verify_razorpay_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """Razorpay signs "<order_id>|<payment_id>" with your key secret.

    hmac.compare_digest, not ==, so the check cannot be timing-attacked.
    """
    secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
    if not (secret and order_id and payment_id and signature):
        return False
    expected = hmac.new(
        secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def apply_plan(company: Company, target: str, db: Session, *, reason: str) -> None:
    """The ONLY place a plan is granted. Every caller must have proven payment
    (or be an explicit, logged demo grant)."""
    company.plan = target
    company.period_start = utcnow()
    company.period_end = utcnow() + timedelta(days=30)
    company.minutes_used = 0.0
    db.add(company)
    db.commit()
    print(
        f"[billing] company {company.id} -> {target} ({reason})",
        flush=True,
    )


def confirm_upgrade(
    company: Company,
    invoice: Invoice,
    db: Session,
    *,
    order_id: str = "",
    payment_id: str = "",
    signature: str = "",
) -> None:
    """Apply a pending upgrade after verifying payment."""
    if invoice.company_id != company.id:
        raise PlanLimitError("Invoice not found.")
    if invoice.status == "paid":
        raise PlanLimitError("This invoice is already paid.")

    if razorpay_configured():
        if not verify_razorpay_signature(order_id, payment_id, signature):
            invoice.status = "failed"
            db.add(invoice)
            db.commit()
            raise PlanLimitError("Payment could not be verified. Nothing was charged.")
        invoice.provider_ref = payment_id
        reason = f"razorpay {payment_id}"
    elif demo_upgrades_allowed():
        # Local development only. Loud on purpose so this can never be mistaken
        # for a real payment path in a log.
        print(
            f"[billing] *** DEMO GRANT *** company {company.id} -> {invoice.plan} "
            "with NO payment (SANCHARAI_ALLOW_DEMO_UPGRADES=1)",
            flush=True,
        )
        reason = "demo grant"
    else:
        raise PlanLimitError(
            "Payments are not configured yet. Set RAZORPAY_KEY_ID and "
            "RAZORPAY_KEY_SECRET to accept upgrades."
        )

    invoice.status = "paid"
    db.add(invoice)
    db.commit()
    apply_plan(company, invoice.plan, db, reason=reason)
