"""
Startup safety checks.

Every item here is something that is harmless locally but damaging in
production, and silent in both - the kind of thing you discover from a customer
complaint rather than a stack trace. In production the app refuses to start
rather than run misconfigured; in development it prints warnings and continues.

Set SANCHARAI_ENV=production on your live server to turn these into hard stops.
"""
from __future__ import annotations

import os
from pathlib import Path


class UnsafeConfig(Exception):
    """Raised when production config would lose data or leak access."""


def is_production() -> bool:
    return os.getenv("SANCHARAI_ENV", "").strip().lower() in ("production", "prod", "live")


def _check_encryption_keys() -> list[str]:
    """The most damaging misconfiguration there is.

    Without FERNET_KEY, security.py generates a fresh secret.key at startup.
    On a host with an ephemeral filesystem that happens on EVERY deploy, and a
    new key cannot decrypt credentials encrypted with the old one - so every
    customer's stored Twilio and AI keys silently become unreadable, and calls
    stop working with no obvious cause.
    """
    problems = []
    if not os.getenv("FERNET_KEY", "").strip():
        problems.append(
            "FERNET_KEY is not set. A new encryption key will be generated on every "
            "deploy, permanently orphaning every stored Twilio and AI credential. "
            "Generate one with:\n"
            "    python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    if not os.getenv("SESSION_SECRET", "").strip():
        problems.append(
            "SESSION_SECRET is not set. Every deploy will invalidate all logins and "
            "all pending verification/reset links. Generate one with:\n"
            "    python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    return problems


def _check_billing() -> list[str]:
    """Demo upgrades hand out paid plans for free."""
    if os.getenv("SANCHARAI_ALLOW_DEMO_UPGRADES", "").strip() == "1":
        return [
            "SANCHARAI_ALLOW_DEMO_UPGRADES=1 grants any paid plan with NO payment. "
            "Anyone who signs up can take the most expensive tier for free. "
            "Set it to 0."
        ]
    return []


def _check_webhook_security() -> list[str]:
    if os.getenv("SANCHARAI_SKIP_TWILIO_VALIDATION", "").strip() == "1":
        return [
            "SANCHARAI_SKIP_TWILIO_VALIDATION=1 disables Twilio signature checking. "
            "Anyone who learns a webhook URL can drive your agents and run up your "
            "call bill. Set it to 0."
        ]
    return []


def _check_cookies() -> list[str]:
    if os.getenv("SANCHARAI_SECURE_COOKIES", "").strip() == "0":
        return [
            "SANCHARAI_SECURE_COOKIES=0 sends session cookies over plain HTTP, where "
            "they can be intercepted. Remove it so cookies are marked Secure."
        ]
    return []


def _check_public_url() -> list[str]:
    """Twilio has to be able to reach us to run a conversation."""
    base = os.getenv("PUBLIC_BASE_URL", "").strip()
    if not base:
        return [
            "PUBLIC_BASE_URL is not set. Twilio will have no address to fetch call "
            "instructions from, and every call will end in 'an application error "
            "has occurred'."
        ]
    host = base.split("//")[-1].split(":")[0].split("/")[0]
    if host in ("127.0.0.1", "localhost", "0.0.0.0") or host.startswith(
        ("192.168.", "10.", "172.16.", "172.17.", "172.18.")
    ):
        return [f"PUBLIC_BASE_URL is {base}, which only exists on this machine."]
    if not base.startswith("https://"):
        return [f"PUBLIC_BASE_URL is {base}. Twilio requires https for webhooks."]
    return []


def _check_admins() -> list[str]:
    """/admin exposes every tenant's data across the whole platform."""
    if not os.getenv("PLATFORM_ADMINS", "").strip():
        return [
            "PLATFORM_ADMINS is not set, so it falls back to the developer's own "
            "email address. Set it to the addresses that should see /admin, which "
            "shows every customer on the platform."
        ]
    return []


def _check_scheduler() -> list[str]:
    """Two schedulers dial every contact twice."""
    warnings = []
    if os.getenv("WEB_CONCURRENCY", "").strip() not in ("", "1"):
        warnings.append(
            f"WEB_CONCURRENCY={os.getenv('WEB_CONCURRENCY')} runs multiple workers. "
            "Set SANCHARAI_SCHEDULER=0 on all but one, or every contact is called "
            "once per worker."
        )
    return warnings


def _check_email() -> list[str]:
    if not os.getenv("BREVO_API_KEY", "").strip():
        return [
            "BREVO_API_KEY is not set. Verification, password-reset and team-invite "
            "emails will not send - links are only printed to the server log, which "
            "customers cannot see, so nobody will be able to verify or reset."
        ]
    return []


def _check_payments() -> list[str]:
    if not (os.getenv("RAZORPAY_KEY_ID", "").strip()
            and os.getenv("RAZORPAY_KEY_SECRET", "").strip()):
        return [
            "Razorpay is not configured, so plan upgrades will be refused. Customers "
            "can sign up on the free plan but cannot pay you."
        ]
    return []


# Blocking problems break the product or lose data. Advisories are things the
# operator may legitimately be doing on purpose (e.g. running free-plan-only).
BLOCKING = (
    _check_encryption_keys, _check_billing, _check_webhook_security,
    _check_cookies, _check_public_url, _check_admins,
)
ADVISORY = (_check_scheduler, _check_email, _check_payments)


def run(strict: bool | None = None) -> list[str]:
    """Check config. Returns the problems found.

    Raises UnsafeConfig in production (or when strict=True) so a misconfigured
    deploy fails loudly at boot instead of quietly corrupting data later.
    """
    strict = is_production() if strict is None else strict

    blocking = [p for check in BLOCKING for p in check()]
    advisory = [p for check in ADVISORY for p in check()]

    if not strict:
        for p in blocking + advisory:
            print(f"[config] {p.splitlines()[0]}", flush=True)
        return blocking + advisory

    for p in advisory:
        print(f"[config] NOTE: {p.splitlines()[0]}", flush=True)

    if blocking:
        lines = "\n\n".join(f"  {i}. {p}" for i, p in enumerate(blocking, 1))
        raise UnsafeConfig(
            f"\n\nRefusing to start: {len(blocking)} unsafe setting(s) for production."
            f"\n\n{lines}\n\n"
            "Fix these, or unset SANCHARAI_ENV to run in development mode.\n"
        )

    print("[config] production preflight passed", flush=True)
    return []
