"""
Transactional email via Brevo.

This is a PLATFORM-level integration, not per-company: one Brevo account (yours)
sends verification, reset, and team-invite mail on behalf of SancharAI.
Customers never supply an email key - only their own Twilio and Groq credentials.

Configure with environment variables:
    BREVO_API_KEY   - from https://app.brevo.com (Settings -> SMTP & API -> API Keys)
    MAIL_FROM       - the sender address, e.g. no-reply@sancharai.in
    MAIL_FROM_NAME  - display name, defaults to "SancharAI"
"""
from __future__ import annotations

import os

import httpx

BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"


def api_key() -> str:
    return os.getenv("BREVO_API_KEY", "").strip()


def is_configured() -> bool:
    """False until a key is set - callers degrade gracefully instead of crashing."""
    return bool(api_key())


def _sender() -> dict:
    return {
        "email": os.getenv("MAIL_FROM", "no-reply@sancharai.in").strip(),
        "name": os.getenv("MAIL_FROM_NAME", "SancharAI").strip(),
    }


def send(to_email: str, subject: str, html: str, text: str = "") -> tuple[bool, str]:
    """Send one email. Returns (ok, detail) rather than raising, so a mail
    outage can never break signup or take the whole site down."""
    if not is_configured():
        return False, "Email is not configured (BREVO_API_KEY is not set)."

    payload = {
        "sender": _sender(),
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html,
    }
    if text:
        payload["textContent"] = text

    try:
        resp = httpx.post(
            BREVO_ENDPOINT,
            headers={
                "api-key": api_key(),
                "content-type": "application/json",
                "accept": "application/json",
            },
            json=payload,
            timeout=15,
        )
    except httpx.HTTPError as exc:
        return False, f"Could not reach Brevo: {exc}"

    if resp.status_code in (200, 201, 202):
        return True, "sent"
    if resp.status_code == 401:
        return False, "Brevo rejected the API key."
    return False, f"Brevo error {resp.status_code}: {resp.text[:200]}"


# ------------------------------------------------------------------ templates
_WRAP = """\
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#05050a;
            padding:32px;color:#ffffff">
  <div style="max-width:520px;margin:0 auto;background:#12121a;border:1px solid #ffffff14;
              border-radius:14px;padding:32px">
    <div style="font-size:20px;font-weight:800;margin-bottom:20px">
      Sanchar<span style="color:#00e5ff">AI</span></div>
    {body}
    <div style="margin-top:28px;padding-top:18px;border-top:1px solid #ffffff12;
                font-size:12px;color:#8b8b9e">
      If you didn't request this, you can safely ignore this email.
    </div>
  </div>
</div>"""

_BUTTON = """\
<a href="{url}" style="display:inline-block;background:#00e5ff;color:#000000;
   text-decoration:none;padding:12px 26px;border-radius:9px;font-weight:600;
   margin:18px 0">{label}</a>
<div style="font-size:12px;color:#8b8b9e;word-break:break-all">
  Or paste this link into your browser:<br/>{url}
</div>"""


def send_verification(to_email: str, link: str) -> tuple[bool, str]:
    body = (
        "<div style='font-size:15px;line-height:1.6'>"
        "Welcome! Please confirm this email address to activate calling on your account."
        "</div>"
        + _BUTTON.format(url=link, label="Verify my email")
        + "<div style='font-size:12px;color:#8b8b9e'>This link expires in 24 hours.</div>"
    )
    return send(
        to_email,
        "Verify your SancharAI email",
        _WRAP.format(body=body),
        f"Verify your SancharAI email: {link}\n\nThis link expires in 24 hours.",
    )


def send_signup_code(to_email: str, code: str) -> tuple[bool, str]:
    body = (
        "<div style='font-size:15px;line-height:1.6'>"
        "Use this code to verify your email and create your SancharAI account:"
        "</div>"
        "<div style='font-size:34px;font-weight:800;letter-spacing:10px;margin:18px 0;"
        f"color:#00e5ff'>{code}</div>"
        "<div style='font-size:12px;color:#8b8b9e'>"
        "This code expires in 10 minutes. Never share it with anyone.</div>"
    )
    return send(
        to_email,
        f"{code} is your SancharAI verification code",
        _WRAP.format(body=body),
        f"Your SancharAI verification code is {code}. It expires in 10 minutes.",
    )


def send_signup_confirmation(to_email: str, link: str) -> tuple[bool, str]:
    body = (
        "<div style='font-size:15px;line-height:1.6'>"
        "Confirm this email address to finish creating your SancharAI account. "
        "Your account is only created once you click the button below."
        "</div>"
        + _BUTTON.format(url=link, label="Confirm email &amp; create account")
        + "<div style='font-size:12px;color:#8b8b9e'>This link expires in 24 hours.</div>"
    )
    return send(
        to_email,
        "Confirm your email to create your SancharAI account",
        _WRAP.format(body=body),
        f"Confirm your email to create your SancharAI account: {link}\n\n"
        "This link expires in 24 hours.",
    )


def send_password_reset(to_email: str, link: str) -> tuple[bool, str]:
    body = (
        "<div style='font-size:15px;line-height:1.6'>"
        "We received a request to reset your SancharAI password."
        "</div>"
        + _BUTTON.format(url=link, label="Reset my password")
        + "<div style='font-size:12px;color:#8b8b9e'>This link expires in 1 hour.</div>"
    )
    return send(
        to_email,
        "Reset your SancharAI password",
        _WRAP.format(body=body),
        f"Reset your SancharAI password: {link}\n\nThis link expires in 1 hour.",
    )


def send_invite(to_email: str, company_name: str, invited_by: str, link: str) -> tuple[bool, str]:
    body = (
        f"<div style='font-size:15px;line-height:1.6'>"
        f"<b>{invited_by}</b> invited you to join the <b>{company_name}</b> workspace "
        f"on SancharAI.</div>"
        + _BUTTON.format(url=link, label="Accept invitation")
        + "<div style='font-size:12px;color:#8b8b9e'>This invitation expires in 7 days.</div>"
    )
    return send(
        to_email,
        f"You've been invited to {company_name} on SancharAI",
        _WRAP.format(body=body),
        f"{invited_by} invited you to join {company_name} on SancharAI: {link}",
    )
