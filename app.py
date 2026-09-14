"""
SancharAI - multi-tenant AI voice calling platform.

Three kinds of routes live here:
  1. Logged-in pages    - identified by a signed session cookie
  2. JSON/form actions  - same cookie, plus a CSRF token
  3. Twilio webhooks    - anonymous, identified by a secret per-company token
                          in the URL path (/hook/<token>/voice), and verified
                          against Twilio's request signature
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import os
import secrets
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv

for _s in (sys.stdout, sys.stderr):  # Windows console can't print Indic scripts
    with contextlib.suppress(Exception):
        _s.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI, File, Form, Request, UploadFile, WebSocket  # noqa: E402
from fastapi.responses import (  # noqa: E402
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402
from sqlmodel import Session, func, select  # noqa: E402

import booking  # noqa: E402
import engine  # noqa: E402
import gcal  # noqa: E402
import mailer  # noqa: E402
import preflight  # noqa: E402
import plans  # noqa: E402
import scheduler  # noqa: E402
import streaming  # noqa: E402
import telephony  # noqa: E402
from auth import (  # noqa: E402
    AuthError,
    accept_invite,
    create_account,
    get_current_user,
    login,
    prepare_signup,
)
from models import (  # noqa: E402
    Agent,
    Appointment,
    Call,
    CallSession,
    Campaign,
    Company,
    CompanySettings,
    Contact,
    Faq,
    Invite,
    Invoice,
    User,
    engine as db_engine,
    init_db,
    to_local,
    utcnow,
)
from security import (  # noqa: E402
    CSRF_FIELD,
    SESSION_COOKIE,
    check_csrf,
    csrf_token,
    decrypt,
    encrypt,
    hash_password,
    make_invite_token,
    check_signup_code,
    make_email_verified_token,
    make_reset_token,
    make_signup_code,
    make_verify_token,
    mask,
    read_email_verified_token,
    read_invite_token,
    read_reset_token,
    read_signup_token,
    read_verify_token,
    set_session_cookie,
    verify_password,
)

BASE_DIR = Path(__file__).parent
CSV_TEMPLATES_DIR = BASE_DIR / "csv_templates"

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    # Refuses to boot on unsafe production config - see preflight.py. Better a
    # loud failure at deploy time than silently orphaned credentials later.
    preflight.run()
    init_db()
    task = asyncio.create_task(scheduler.loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        scheduler.release_lock()


app = FastAPI(title="SancharAI", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ============================== helpers ==============================
def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _err(detail: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "detail": detail}, status_code=status)


_ngrok_cache: dict = {"url": "", "at": 0.0}


def _live_ngrok_url() -> str:
    """The tunnel ngrok is serving RIGHT NOW, or "" if it isn't running.

    A free ngrok URL changes every restart, so PUBLIC_BASE_URL in .env goes
    stale constantly during development. When that happens Twilio dials out
    fine, then calls back to a dead address and the customer hears "an
    application error has occurred" - with nothing in our logs, because the
    request never arrived.

    Asking ngrok's own local API removes the guesswork. Cached for 30s so this
    never adds latency to a live webhook, and silent when ngrok isn't running,
    which is the normal case in production.
    """
    now = time.time()
    if now - _ngrok_cache["at"] < 30:
        return _ngrok_cache["url"]
    url = ""
    try:
        resp = httpx.get("http://127.0.0.1:4040/api/tunnels", timeout=1.0)
        tunnels = resp.json().get("tunnels", [])
        url = next((t["public_url"] for t in tunnels
                    if t.get("public_url", "").startswith("https")), "")
    except Exception:
        url = ""
    _ngrok_cache.update(url=url, at=now)
    return url


def _public_base(request: Request | None = None) -> str:
    """The externally reachable origin.

    A live ngrok tunnel wins over PUBLIC_BASE_URL, because during development
    the .env value goes stale every time ngrok restarts and pointing Twilio at
    a dead tunnel is the single most common cause of "an application error has
    occurred". In production there is no ngrok on :4040, so this falls straight
    through to the configured value.

    PUBLIC_BASE_URL then wins over request headers, because the background
    scheduler has no request to read headers from and both must agree on the
    webhook URL.
    """
    configured = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

    # Only consult ngrok when the configured value is itself a tunnel (or
    # absent). A real domain must always win: otherwise an unrelated ngrok
    # session running on the same machine would silently hijack the webhook
    # address, and tests that set an explicit URL would be overridden.
    if not configured or "ngrok" in configured:
        live = _live_ngrok_url()
        if live:
            if configured and configured != live:
                print(f"[ngrok] .env is stale ({configured}) - using live tunnel {live}",
                      flush=True)
            return live

    if configured:
        return configured
    # A WebSocket handler has no Request to fall back on; it passes None and
    # relies on the configured value or a live tunnel, both handled above.
    if request is None:
        return ""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return f"{proto}://{host}".rstrip("/")


def _is_local(base: str) -> bool:
    """True if Twilio's servers could never reach this address."""
    host = base.split("//")[-1].split(":")[0].split("/")[0]
    return (
        not host
        or host in ("127.0.0.1", "localhost", "0.0.0.0", "::1")
        or host.startswith(("192.168.", "10.", "172.16.", "172.17.", "172.18."))
    )


async def _csrf_ok(request: Request) -> bool:
    """Verify the CSRF token from either a form field or the X-CSRF-Token header.

    v1 had no CSRF protection: every mutating route was a cookie-authenticated
    POST, so any other site could submit a form that deleted a whole account.
    """
    cookie = request.cookies.get(SESSION_COOKIE)
    header = request.headers.get("x-csrf-token")
    if header and check_csrf(cookie, header):
        return True
    with contextlib.suppress(Exception):
        form = await request.form()
        if check_csrf(cookie, form.get(CSRF_FIELD)):
            return True
    return False


def _load(user: User, db: Session) -> tuple[Company, CompanySettings]:
    company = db.get(Company, user.company_id)
    settings = db.exec(
        select(CompanySettings).where(CompanySettings.company_id == user.company_id)
    ).first()
    if not settings:  # safety net for older rows
        settings = CompanySettings(company_id=user.company_id, company_display_name=company.name)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    company = plans.ensure_period(company, db)
    return company, settings


_GOOD = {"CONFIRMED", "PAYMENT_PROMISED", "INTERESTED"}
_MISSED = {"NO_ANSWER", "FAILED", "BUSY", "CANCELED", "NO-ANSWER"}


def _stats(company_id: int, db: Session, tz_name: str) -> dict:
    """Aggregate in SQL, not in Python.

    v1 loaded every Call row for the company into memory on every dashboard
    render just to count them - fine at 100 calls, unusable at 100,000.
    """
    total, total_secs = db.exec(
        select(func.count(Call.id), func.coalesce(func.sum(Call.duration_sec), 0)).where(
            Call.company_id == company_id
        )
    ).one()

    breakdown = dict(
        db.exec(
            select(Call.outcome, func.count(Call.id))
            .where(Call.company_id == company_id)
            .group_by(Call.outcome)
        ).all()
    )
    breakdown = {(k or "UNCLEAR"): v for k, v in breakdown.items()}

    connected = sum(v for k, v in breakdown.items() if k not in _MISSED)
    converted = sum(v for k, v in breakdown.items() if k in _GOOD)

    answered_secs, answered_n = db.exec(
        select(func.coalesce(func.sum(Call.duration_sec), 0), func.count(Call.id)).where(
            Call.company_id == company_id, Call.duration_sec > 0
        )
    ).one()
    avg = int(answered_secs // answered_n) if answered_n else 0

    # 14-day trend, bucketed by the company's local date.
    since = utcnow() - timedelta(days=14)
    rows = db.exec(
        select(Call.created_at, Call.duration_sec).where(
            Call.company_id == company_id, Call.created_at >= since
        )
    ).all()
    by_day: dict[str, int] = {}
    for created_at, _ in rows:
        key = to_local(created_at, tz_name).strftime("%Y-%m-%d")
        by_day[key] = by_day.get(key, 0) + 1
    trend = [{"date": d, "calls": c} for d, c in sorted(by_day.items())]

    # Calls per hour of day, ported from VoiceIQ. Bucketed in the company's own
    # timezone, so "best hour to call" means their customers' clock, not UTC.
    all_rows = db.exec(
        select(Call.created_at).where(Call.company_id == company_id)
    ).all()
    hourly = [0] * 24
    for created_at in all_rows:
        hourly[to_local(created_at, tz_name).hour] += 1

    # Average lead score across scored calls.
    avg_score = (
        round(sum(engine.score_of(k) * v for k, v in breakdown.items()) / total)
        if total
        else 0
    )

    return {
        "total": total or 0,
        "connected": connected,
        "converted": converted,
        "connect_rate": round(connected / total * 100) if total else 0,
        "convert_rate": round(converted / total * 100) if total else 0,
        "avg_duration": f"{avg // 60}:{avg % 60:02d}",
        "total_minutes": round((total_secs or 0) / 60, 1),
        "breakdown": breakdown,
        "trend": trend,
        "hourly": hourly,
        "avg_score": avg_score,
    }


def _ctx(request: Request, user: User, db: Session, page: str, **extra) -> dict:
    """Context every dashboard page needs: identity, plan usage, nav counts."""
    company, settings = _load(user, db)
    counts = {
        "campaigns": db.exec(
            select(func.count()).select_from(Campaign).where(Campaign.company_id == company.id)
        ).one(),
        "agents": db.exec(
            select(func.count()).select_from(Agent).where(Agent.company_id == company.id)
        ).one(),
        "contacts": db.exec(
            select(func.count()).select_from(Contact).where(Contact.company_id == company.id)
        ).one(),
        "calls": db.exec(
            select(func.count()).select_from(Call).where(Call.company_id == company.id)
        ).one(),
        "faqs": db.exec(
            select(func.count()).select_from(Faq).where(Faq.company_id == company.id)
        ).one(),
    }
    ctx = {
        "request": request,
        "user": user,
        "company": company,
        "settings": settings,
        "page": page,
        "counts": counts,
        "usage": plans.usage(company),
        "csrf": csrf_token(request.cookies.get(SESSION_COOKIE)),
        "twilio_connected": telephony.is_connected(settings),
        "groq_connected": engine.ai_configured(settings),
        "ai_provider": getattr(settings, "ai_provider", "groq") or "groq",
        "webhook_base": f"{_public_base(request)}/hook/{company.webhook_token}",
        "public_reachable": not _is_local(_public_base(request)),
        "public_base": _public_base(request),
        "is_admin": user.email in PLATFORM_ADMINS,
    }
    ctx.update(extra)
    return ctx


# ============================== AUTH ==============================
@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    if get_current_user(request):
        return _redirect("/")
    return templates.TemplateResponse("signup.html", {"request": request})


# Confirmation emails sent per client IP in the last hour. Every signup attempt
# sends a real email, so without a cap one script could burn the whole Brevo
# daily quota and get the sending domain flagged as spam. In memory is enough:
# the app runs as a single instance, and a restart only resets the window.
_SIGNUP_ATTEMPTS: dict[str, list[float]] = {}


def _signup_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _signup_capped(ip: str) -> bool:
    limit = int(os.getenv("SANCHARAI_SIGNUP_LIMIT", "5") or 5)
    cutoff = time.time() - 3600
    recent = [t for t in _SIGNUP_ATTEMPTS.get(ip, []) if t > cutoff]
    _SIGNUP_ATTEMPTS[ip] = recent
    return len(recent) >= limit


# Wrong code guesses per ticket. Five tries on a 6-digit code is a 1-in-200,000
# chance of guessing it before the ticket locks.
_CODE_FAILURES: dict[str, int] = {}
_MAX_CODE_TRIES = 5


@app.post("/api/signup/send-code")
async def signup_send_code(request: Request, email: str = Form(default="")):
    """Step 1 of signup: email a 6-digit code. Creates nothing."""
    clean = email.strip().lower()
    domain = clean.rsplit("@", 1)[-1] if "@" in clean else ""
    if not clean or "." not in domain or clean.startswith("@"):
        return _err("Enter a valid email address.")
    with Session(db_engine) as db:
        if db.exec(select(User).where(User.email == clean)).first():
            return _err("An account with that email already exists. Sign in instead.", 409)

    ip = _signup_ip(request)
    if _signup_capped(ip):
        return _err("Too many codes requested from your network. Please try again in an hour.", 429)

    code, ticket = make_signup_code(clean)
    if mailer.is_configured():
        ok, detail = mailer.send_signup_code(clean, code)
        if not ok:
            print(f"[EMAIL FAILED] signup code for {clean}: {detail}", flush=True)
            return _err("We couldn't send the code. Check the address and try again.", 502)
        print(f"[EMAIL SENT] signup code to {clean} via Brevo", flush=True)
    elif os.getenv("SANCHARAI_ENV", "").strip().lower() == "production":
        # Never tell a real visitor "we sent a code" when nothing was sent.
        print(f"[EMAIL NOT CONFIGURED] signup code requested for {clean} - BREVO_API_KEY missing", flush=True)
        return _err("Email sending isn't set up on this server yet. Please try again later.", 503)
    else:
        # Local development without Brevo: the code goes to the server log.
        print(f"[EMAIL NOT CONFIGURED] signup code for {clean}: {code}", flush=True)
        _SIGNUP_ATTEMPTS.setdefault(ip, []).append(time.time())
        return {
            "ok": True,
            "ticket": ticket,
            "detail": "Email isn't configured on this server - the code is printed in the server log.",
        }

    # Only sent emails count toward the cap, so fixing a typo isn't punished.
    _SIGNUP_ATTEMPTS.setdefault(ip, []).append(time.time())
    return {
        "ok": True,
        "ticket": ticket,
        "detail": f"We sent a 6-digit code to {clean}. It expires in 10 minutes.",
    }


@app.post("/api/signup/verify-code")
async def signup_verify_code(
    email: str = Form(default=""), code: str = Form(default=""), ticket: str = Form(default="")
):
    """Step 2: check the code and hand back proof the email is verified."""
    clean = email.strip().lower()
    status, nonce = check_signup_code(ticket, clean, code)
    if status == "expired":
        return _err("That code has expired. Send a new one.")
    if status == "invalid":
        return _err("Send a code to this email first.")
    if _CODE_FAILURES.get(nonce, 0) >= _MAX_CODE_TRIES:
        return _err("Too many wrong attempts. Send a new code.", 429)
    if status == "wrong":
        _CODE_FAILURES[nonce] = _CODE_FAILURES.get(nonce, 0) + 1
        left = _MAX_CODE_TRIES - _CODE_FAILURES[nonce]
        if not left:
            return _err("Too many wrong attempts. Send a new code.", 429)
        return _err(f"Wrong code. {left} attempt{'s' if left != 1 else ''} left.")
    _CODE_FAILURES.pop(nonce, None)
    return {"ok": True, "verified_token": make_email_verified_token(clean), "detail": "Email verified"}


@app.post("/signup", response_class=HTMLResponse)
async def signup_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    company_name: str = Form(default=""),
    account_type: str = Form(default="enterprise"),
    verified_token: str = Form(default=""),
):
    """Step 3: create the account - only for an email proven by the code step.

    It is created already verified, so a mistyped or someone else's address
    can never produce an account.
    """
    clean = email.strip().lower()
    proven = read_email_verified_token(verified_token) == clean

    def form_error(message: str):
        return templates.TemplateResponse(
            "signup.html",
            {
                "request": request,
                "error": message,
                "email": email,
                "company_name": company_name,
                "account_type": account_type,
                # Keep a still-valid verification, so fixing e.g. a short
                # password doesn't mean waiting for another code.
                "verified_token": verified_token if proven else "",
            },
        )

    if not proven:
        return form_error("Verify your email with the 6-digit code before creating your account.")
    try:
        clean_email, clean_company, clean_type = prepare_signup(
            email, password, company_name, account_type
        )
        _, cookie = create_account(
            clean_email, hash_password(password), clean_company, clean_type, verified=True
        )
    except AuthError as exc:
        return form_error(str(exc))
    resp = _redirect("/settings")
    set_session_cookie(resp, cookie)
    return resp


@app.get("/signup/confirm/{token}", response_class=HTMLResponse)
async def signup_confirm(request: Request, token: str):
    """Clicking the emailed link is what creates the account - already
    verified, since reaching the inbox proves the address is theirs."""
    pending = read_signup_token(token)
    if not pending:
        return templates.TemplateResponse(
            "message.html",
            {
                "request": request,
                "title": "Link expired",
                "body": "This signup link is invalid or has expired. Please sign up again.",
                "link": "/signup",
                "link_text": "Sign up again",
            },
        )
    try:
        _, cookie = create_account(
            pending["email"], pending["pw"], pending["company"], pending["type"], verified=True
        )
    except AuthError:
        # The link was already used, or the address was registered in between.
        return templates.TemplateResponse(
            "message.html",
            {
                "request": request,
                "title": "Account already exists",
                "body": "An account for this email has already been created. Sign in to continue.",
                "link": "/login",
                "link_text": "Sign in",
            },
        )
    resp = _redirect("/settings")
    set_session_cookie(resp, cookie)
    return resp


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if get_current_user(request):
        return _redirect("/")
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    try:
        _, cookie = login(email, password)
    except AuthError as exc:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": str(exc), "email": email}
        )
    resp = _redirect("/")
    set_session_cookie(resp, cookie)
    return resp


@app.get("/logout")
async def logout():
    resp = _redirect("/login")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ---- email verification ----
def _send_verification_for(user: User, request: Request) -> tuple[bool, str]:
    link = f"{_public_base(request)}/verify/{make_verify_token(user.id)}"
    if not mailer.is_configured():
        # Without a key we still let people in; the link is logged so the
        # platform owner can hand it over manually during early testing.
        print(f"[EMAIL NOT CONFIGURED] verification link for {user.email}: {link}", flush=True)
        return False, "Email is not configured yet."
    return mailer.send_verification(user.email, link)


@app.get("/verify/{token}", response_class=HTMLResponse)
async def verify_email(request: Request, token: str):
    uid = read_verify_token(token)
    if not uid:
        return templates.TemplateResponse(
            "message.html",
            {
                "request": request,
                "title": "Link expired",
                "body": "This verification link is invalid or has expired. "
                "Sign in and request a new one.",
                "link": "/login",
                "link_text": "Go to sign in",
            },
        )
    with Session(db_engine) as db:
        user = db.get(User, uid)
        if not user:
            return templates.TemplateResponse(
                "message.html",
                {
                    "request": request,
                    "title": "Account not found",
                    "body": "That account no longer exists.",
                    "link": "/signup",
                    "link_text": "Create an account",
                },
            )
        user.is_verified = True
        db.add(user)
        db.commit()
    return templates.TemplateResponse(
        "message.html",
        {
            "request": request,
            "title": "Email verified",
            "body": "Your email is confirmed. You can now place calls.",
            "link": "/",
            "link_text": "Open dashboard",
        },
    )


@app.post("/api/resend-verification")
async def resend_verification(request: Request):
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    if user.is_verified:
        return {"ok": True, "detail": "Your email is already verified."}
    ok, detail = _send_verification_for(user, request)
    return {"ok": ok, "detail": "Verification email sent." if ok else detail}


# ---- forgotten password ----
@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse("forgot_password.html", {"request": request})


@app.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password_submit(request: Request, email: str = Form(...)):
    email = email.strip().lower()
    with Session(db_engine) as db:
        user = db.exec(select(User).where(User.email == email)).first()
        if user:
            link = (
                f"{_public_base(request)}/reset-password/"
                f"{make_reset_token(user.id, user.password_hash)}"
            )
            if mailer.is_configured():
                mailer.send_password_reset(user.email, link)
            else:
                print(f"[EMAIL NOT CONFIGURED] reset link for {email}: {link}", flush=True)
    # Always show the same confirmation, whether or not the address exists -
    # otherwise this form becomes a way to discover who has an account.
    return templates.TemplateResponse(
        "message.html",
        {
            "request": request,
            "title": "Check your email",
            "body": "If an account exists for that address, we've sent a password "
            "reset link. It expires in one hour.",
            "link": "/login",
            "link_text": "Back to sign in",
        },
    )


@app.get("/reset-password/{token}", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str):
    if not read_reset_token(token):
        return templates.TemplateResponse(
            "message.html",
            {
                "request": request,
                "title": "Link expired",
                "body": "This reset link is invalid, already used, or expired.",
                "link": "/forgot-password",
                "link_text": "Request a new link",
            },
        )
    return templates.TemplateResponse("reset_password.html", {"request": request, "token": token})


@app.post("/reset-password/{token}", response_class=HTMLResponse)
async def reset_password_submit(request: Request, token: str, new_password: str = Form(...)):
    data = read_reset_token(token)
    if not data:
        return templates.TemplateResponse(
            "message.html",
            {
                "request": request,
                "title": "Link expired",
                "body": "This reset link is invalid, already used, or expired.",
                "link": "/forgot-password",
                "link_text": "Request a new link",
            },
        )
    if len(new_password) < 8:
        return templates.TemplateResponse(
            "reset_password.html",
            {
                "request": request,
                "token": token,
                "error": "Password must be at least 8 characters.",
            },
        )

    with Session(db_engine) as db:
        user = db.get(User, data["uid"])
        # The hash fragment in the token must still match, so a link that was
        # already used (password since changed) cannot be replayed.
        if not user or user.password_hash[-16:] != data.get("pw"):
            return templates.TemplateResponse(
                "message.html",
                {
                    "request": request,
                    "title": "Link already used",
                    "body": "This reset link has already been used.",
                    "link": "/forgot-password",
                    "link_text": "Request a new link",
                },
            )
        user.password_hash = hash_password(new_password)
        user.is_verified = True  # reaching the inbox proves the address works
        db.add(user)
        db.commit()
    return templates.TemplateResponse(
        "message.html",
        {
            "request": request,
            "title": "Password updated",
            "body": "Your password has been changed. You can sign in now.",
            "link": "/login",
            "link_text": "Sign in",
        },
    )


# ---- team invitations ----
@app.get("/invite/{token}", response_class=HTMLResponse)
async def invite_page(request: Request, token: str):
    invite_id = read_invite_token(token)
    with Session(db_engine) as db:
        invite = db.get(Invite, invite_id) if invite_id else None
        if not invite or invite.accepted:
            return templates.TemplateResponse(
                "message.html",
                {
                    "request": request,
                    "title": "Invitation not valid",
                    "body": "This invitation is invalid, expired, or already used.",
                    "link": "/login",
                    "link_text": "Go to sign in",
                },
            )
        company = db.get(Company, invite.company_id)
        return templates.TemplateResponse(
            "accept_invite.html",
            {
                "request": request,
                "token": token,
                "email": invite.email,
                "company_name": company.name if company else "the workspace",
            },
        )


@app.post("/invite/{token}", response_class=HTMLResponse)
async def invite_submit(request: Request, token: str, password: str = Form(...)):
    invite_id = read_invite_token(token)
    if not invite_id:
        return _redirect("/login")
    try:
        _, cookie = accept_invite(invite_id, "", password)
    except AuthError as exc:
        with Session(db_engine) as db:
            invite = db.get(Invite, invite_id)
            company = db.get(Company, invite.company_id) if invite else None
        return templates.TemplateResponse(
            "accept_invite.html",
            {
                "request": request,
                "token": token,
                "error": str(exc),
                "email": invite.email if invite else "",
                "company_name": company.name if company else "",
            },
        )
    resp = _redirect("/")
    set_session_cookie(resp, cookie)
    return resp


# ============================== PAGES ==============================
@app.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    user = get_current_user(request)
    if not user:
        # Logged out, "/" is the marketing page rather than a bare redirect to
        # the login form - a visitor who has never heard of the product needs
        # to be told what it does before being asked for a password.
        return templates.TemplateResponse(
            "landing.html",
            {
                "request": request,
                "plans": plans.PLANS,
                "plan_order": plans.PLAN_ORDER,
            },
        )
    with Session(db_engine) as db:
        company, _ = _load(user, db)
        stats = _stats(company.id, db, company.timezone)
        recent = db.exec(
            select(Call)
            .where(Call.company_id == company.id)
            .order_by(Call.created_at.desc())
            .limit(8)
        ).all()
        campaigns = db.exec(select(Campaign).where(Campaign.company_id == company.id)).all()
        names = {c.id: c.name for c in campaigns}
        rows = [
            {
                "name": c.customer_name or "(unknown)",
                "phone": c.phone,
                "campaign": names.get(c.campaign_id, "—"),
                "status": c.outcome or "UNCLEAR",
                "dur": f"{c.duration_sec // 60}:{c.duration_sec % 60:02d}" if c.duration_sec else "—",
            }
            for c in recent
        ]
        return templates.TemplateResponse(
            "overview.html",
            _ctx(
                request,
                user,
                db,
                "overview",
                stats=stats,
                stats_json=json.dumps(stats),
                recent_json=json.dumps(rows),
                running=sum(1 for c in campaigns if c.status == "running"),
            ),
        )


@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    user = get_current_user(request)
    if not user:
        return _redirect("/login")
    with Session(db_engine) as db:
        agents = db.exec(
            select(Agent).where(Agent.company_id == user.company_id).order_by(Agent.created_at)
        ).all()
        return templates.TemplateResponse(
            "agents.html", _ctx(request, user, db, "agents", agents=agents)
        )


@app.get("/campaigns", response_class=HTMLResponse)
async def campaigns_page(request: Request):
    user = get_current_user(request)
    if not user:
        return _redirect("/login")
    with Session(db_engine) as db:
        company, _ = _load(user, db)
        campaigns = db.exec(
            select(Campaign)
            .where(Campaign.company_id == company.id)
            .order_by(Campaign.created_at.desc())
        ).all()
        agents = db.exec(select(Agent).where(Agent.company_id == company.id)).all()
        campaigns_json = json.dumps([
            {
                "id": c.id, "name": c.name, "campaign_type": c.campaign_type,
                "script": c.script, "voice": c.voice,
                "speech_language": c.speech_language,
                "call_window_start": c.call_window_start,
                "call_window_end": c.call_window_end,
                "max_calls_per_day": c.max_calls_per_day,
                "retry_policy": c.retry_policy,
                "auto_language_switch": bool(c.auto_language_switch),
                "ask_language": bool(c.ask_language),
                "agent_id": c.agent_id,
            }
            for c in campaigns
        ])
        pending = {
            c.id: db.exec(
                select(func.count())
                .select_from(Contact)
                .where(Contact.campaign_id == c.id, Contact.status == "pending")
            ).one()
            for c in campaigns
        }
        return templates.TemplateResponse(
            "campaigns.html",
            _ctx(
                request,
                user,
                db,
                "campaigns",
                campaigns=campaigns,
                campaigns_json=campaigns_json,
                agents=agents,
                pending=pending,
            ),
        )


@app.get("/contacts", response_class=HTMLResponse)
async def contacts_page(request: Request):
    user = get_current_user(request)
    if not user:
        return _redirect("/login")
    with Session(db_engine) as db:
        company, _ = _load(user, db)
        contacts = db.exec(
            select(Contact)
            .where(Contact.company_id == company.id)
            .order_by(Contact.created_at.desc())
            .limit(500)
        ).all()
        campaigns = db.exec(select(Campaign).where(Campaign.company_id == company.id)).all()
        return templates.TemplateResponse(
            "contacts.html",
            _ctx(
                request,
                user,
                db,
                "contacts",
                contacts=contacts,
                campaigns=campaigns,
                templates_list=_list_templates(),
            ),
        )


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    user = get_current_user(request)
    if not user:
        return _redirect("/login")
    with Session(db_engine) as db:
        company, _ = _load(user, db)
        calls = db.exec(
            select(Call)
            .where(Call.company_id == company.id)
            .order_by(Call.created_at.desc())
            .limit(300)
        ).all()
        campaigns = {
            c.id: c.name
            for c in db.exec(select(Campaign).where(Campaign.company_id == company.id)).all()
        }
        rows = [
            {
                "name": c.customer_name or "(unknown)",
                "phone": c.phone,
                "campaign": campaigns.get(c.campaign_id, "—"),
                "status": c.outcome or "UNCLEAR",
                "score": engine.score_of(c.outcome),
                "dur": f"{c.duration_sec // 60}:{c.duration_sec % 60:02d}" if c.duration_sec else "—",
                "when": to_local(c.created_at, company.timezone).strftime("%d %b, %H:%M"),
                "summary": c.summary,
                "action": c.action_needed,
                "transcript": c.transcript,
            }
            for c in calls
        ]
        return templates.TemplateResponse(
            "history.html",
            _ctx(request, user, db, "history", calls_json=json.dumps(rows)),
        )


@app.get("/appointments", response_class=HTMLResponse)
async def appointments_page(request: Request):
    user = get_current_user(request)
    if not user:
        return _redirect("/login")
    with Session(db_engine) as db:
        company, s = _load(user, db)
        rows = db.exec(
            select(Appointment)
            .where(Appointment.company_id == company.id)
            .order_by(Appointment.starts_at)
        ).all()
        now = utcnow()
        appts = [
            {
                "id": a.id,
                "when": booking.to_local(a.starts_at, company).strftime("%a %d %b, %I:%M %p"),
                "day": booking.to_local(a.starts_at, company).strftime("%Y-%m-%d"),
                "name": a.customer_name or "(unknown)",
                "phone": a.phone,
                "status": a.status,
                "past": a.ends_at < now,
                "notes": a.notes,
            }
            for a in rows
        ]
        return templates.TemplateResponse(
            "appointments.html",
            _ctx(request, user, db, "appointments", appts=appts,
                 upcoming=sum(1 for a in appts if not a["past"] and a["status"] == "booked")),
        )


@app.post("/api/appointment/{appt_id}/{action}")
async def appointment_action(request: Request, appt_id: int, action: str):
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    if action not in {"cancel", "complete", "no_show", "delete"}:
        return _err("Unknown action", 404)

    with Session(db_engine) as db:
        appt = db.get(Appointment, appt_id)
        if not appt or appt.company_id != user.company_id:
            return _err("Not found", 404)
        _, s = _load(user, db)
        # Removing the Google copy too, so a cancelled slot doesn't keep
        # blocking the diary the customer's staff actually look at.
        if action in ("cancel", "delete") and appt.gcal_event_id:
            await asyncio.to_thread(gcal.delete_event, s, appt.gcal_event_id)

        if action == "delete":
            db.delete(appt)
        else:
            # Cancelling frees the slot: clashes() only considers "booked".
            appt.status = {"cancel": "cancelled", "complete": "completed",
                           "no_show": "no_show"}[action]
            db.add(appt)
        db.commit()
    return {"ok": True}


@app.get("/settings/google/connect")
async def google_connect(request: Request):
    """Send the owner to Google to grant calendar access."""
    user = get_current_user(request)
    if not user:
        return _redirect("/login")
    if not gcal.configured():
        return _redirect("/settings?google=unconfigured")

    # The signed session cookie doubles as CSRF state: Google echoes it back,
    # and a callback whose state doesn't match this browser is rejected.
    state = csrf_token(request.cookies.get(SESSION_COOKIE))
    redirect_uri = f"{_public_base(request)}/settings/google/callback"
    return _redirect(gcal.consent_url(redirect_uri, state))


@app.get("/settings/google/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Where Google returns after consent."""
    user = get_current_user(request)
    if not user:
        return _redirect("/login")
    if error or not code:
        return _redirect("/settings?google=denied")
    if not check_csrf(request.cookies.get(SESSION_COOKIE), state):
        # A mismatched state means this callback was not started by this
        # browser - the classic OAuth CSRF, where an attacker links THEIR
        # calendar to YOUR account.
        return _redirect("/settings?google=badstate")

    redirect_uri = f"{_public_base(request)}/settings/google/callback"
    try:
        refresh = await asyncio.to_thread(gcal.exchange_code, code, redirect_uri)
    except gcal.CalendarError as exc:
        print(f"[gcal] connect failed: {exc}", flush=True)
        return _redirect("/settings?google=failed")

    with Session(db_engine) as db:
        _, s = _load(user, db)
        s.google_refresh_token_enc = encrypt(refresh)
        db.add(s)
        db.commit()
    return _redirect("/settings?google=connected")


@app.post("/api/google/disconnect")
async def google_disconnect(request: Request):
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    with Session(db_engine) as db:
        _, s = _load(user, db)
        s.google_refresh_token_enc = ""
        db.add(s)
        db.commit()
    return {"ok": True, "detail": "Google Calendar disconnected."}


@app.post("/api/google/test")
async def google_test(request: Request):
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    with Session(db_engine) as db:
        _, s = _load(user, db)
    ok, detail = await asyncio.to_thread(gcal.test_connection, s)
    return {"ok": ok, "detail": detail}


@app.get("/history/download")
async def download_history(request: Request):
    """Export this company's calls as CSV.

    Ported from VoiceIQ, which streamed the raw call_history.csv straight off
    disk. That is single-tenant thinking - here the rows are filtered by
    company_id and generated in memory, so one customer can never download
    another's call records.
    """
    user = get_current_user(request)
    if not user:
        return _redirect("/login")

    with Session(db_engine) as db:
        company, _ = _load(user, db)
        calls = db.exec(
            select(Call)
            .where(Call.company_id == company.id)
            .order_by(Call.created_at.desc())
        ).all()
        names = {
            c.id: c.name
            for c in db.exec(select(Campaign).where(Campaign.company_id == company.id)).all()
        }

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "timestamp", "customer_name", "phone", "campaign", "outcome", "score",
        "duration_sec", "summary", "action_needed", "transcript",
    ])
    for c in calls:
        writer.writerow([
            to_local(c.created_at, company.timezone).strftime("%Y-%m-%d %H:%M:%S"),
            c.customer_name,
            c.phone,
            names.get(c.campaign_id, ""),
            c.outcome,
            engine.score_of(c.outcome),
            c.duration_sec,
            # Newlines inside a CSV cell break naive spreadsheet importers.
            (c.summary or "").replace("\n", " "),
            (c.action_needed or "").replace("\n", " "),
            (c.transcript or "").replace("\n", " "),
        ])

    stamp = to_local(utcnow(), company.timezone).strftime("%Y%m%d")
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="sancharai-calls-{stamp}.csv"'
        },
    )


@app.get("/faqs", response_class=HTMLResponse)
async def faqs_page(request: Request):
    user = get_current_user(request)
    if not user:
        return _redirect("/login")
    with Session(db_engine) as db:
        faqs = db.exec(
            select(Faq).where(Faq.company_id == user.company_id).order_by(Faq.created_at)
        ).all()
        return templates.TemplateResponse("faqs.html", _ctx(request, user, db, "faqs", faqs=faqs))


@app.get("/call", response_class=HTMLResponse)
async def live_call_page(request: Request):
    user = get_current_user(request)
    if not user:
        return _redirect("/login")
    with Session(db_engine) as db:
        campaigns = db.exec(
            select(Campaign).where(Campaign.company_id == user.company_id)
        ).all()
        agents = db.exec(
            select(Agent).where(Agent.company_id == user.company_id, Agent.active == True)  # noqa: E712
        ).all()
        return templates.TemplateResponse(
            "call.html",
            _ctx(request, user, db, "call", campaigns=campaigns, agents=agents),
        )


@app.get("/billing", response_class=HTMLResponse)
async def billing_page(request: Request):
    user = get_current_user(request)
    if not user:
        return _redirect("/login")
    with Session(db_engine) as db:
        company, _ = _load(user, db)
        invoices = db.exec(
            select(Invoice)
            .where(Invoice.company_id == company.id)
            .order_by(Invoice.created_at.desc())
            .limit(50)
        ).all()
        return templates.TemplateResponse(
            "billing.html",
            _ctx(
                request,
                user,
                db,
                "billing",
                plans=plans.PLANS,
                plan_order=plans.PLAN_ORDER,
                invoices=invoices,
                payments_live=plans.razorpay_configured(),
                demo_allowed=plans.demo_upgrades_allowed(),
            ),
        )


@app.get("/team", response_class=HTMLResponse)
async def team_page(request: Request):
    user = get_current_user(request)
    if not user:
        return _redirect("/login")
    with Session(db_engine) as db:
        members = db.exec(select(User).where(User.company_id == user.company_id)).all()
        invites = db.exec(
            select(Invite).where(Invite.company_id == user.company_id, Invite.accepted == False)  # noqa: E712
        ).all()
        return templates.TemplateResponse(
            "team.html", _ctx(request, user, db, "team", members=members, invites=invites)
        )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = get_current_user(request)
    if not user:
        return _redirect("/login")
    with Session(db_engine) as db:
        _, s = _load(user, db)
        return templates.TemplateResponse(
            "settings.html",
            _ctx(
                request,
                user,
                db,
                "settings",
                masked_token=mask(decrypt(s.twilio_auth_token_enc)),
                masked_plivo_token=mask(decrypt(s.plivo_auth_token_enc)),
                masked_groq=mask(decrypt(s.groq_api_key_enc)),
                masked_openai=mask(decrypt(s.openai_api_key_enc)),
                masked_nvidia=mask(decrypt(s.nvidia_api_key_enc)),
                masked_deepgram=mask(decrypt(s.deepgram_api_key_enc)),
                google_configured=gcal.configured(),
                google_connected=gcal.connected(s),
            ),
        )


# ============================== SETTINGS ==============================
@app.post("/settings")
async def save_settings(
    request: Request,
    telephony_provider: str = Form(default="twilio"),
    twilio_account_sid: str = Form(default=""),
    twilio_auth_token: str = Form(default=""),
    twilio_phone_number: str = Form(default=""),
    plivo_auth_id: str = Form(default=""),
    plivo_auth_token: str = Form(default=""),
    plivo_phone_number: str = Form(default=""),
    groq_api_key: str = Form(default=""),
    groq_model: str = Form(default="llama-3.1-8b-instant"),
    ai_provider: str = Form(default="groq"),
    ollama_host: str = Form(default=""),
    ollama_model: str = Form(default="llama3.2"),
    openai_api_key: str = Form(default=""),
    openai_model: str = Form(default="gpt-4o-mini"),
    nvidia_api_key: str = Form(default=""),
    nvidia_model: str = Form(default="nvidia/nvidia-nemotron-nano-9b-v2"),
    stt_provider: str = Form(default="carrier"),
    deepgram_api_key: str = Form(default=""),
    company_display_name: str = Form(default=""),
    agent_name: str = Form(default=""),
    business_desc: str = Form(default=""),
    voice: str = Form(default="Polly.Aditi"),
    speech_language: str = Form(default="en-IN"),
    company_timezone: str = Form(default="Asia/Kolkata"),
    detect_voicemail: str = Form(default=""),
    max_call_seconds: int = Form(default=180),
    speech_timeout: str = Form(default="auto"),
    auto_language_switch: str = Form(default=""),
    ask_language: str = Form(default=""),
    booking_enabled: str = Form(default=""),
    booking_slot_minutes: int = Form(default=30),
    booking_open: str = Form(default="10:00"),
    booking_close: str = Form(default="18:00"),
    booking_days_ahead: int = Form(default=30),
    webhook_out_url: str = Form(default=""),
    handoff_enabled: str = Form(default=""),
    handoff_number: str = Form(default=""),
    csrf_token: str = Form(default="", alias=CSRF_FIELD),
):
    user = get_current_user(request)
    if not user:
        return _redirect("/login")
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)

    with Session(db_engine) as db:
        company, s = _load(user, db)
        s.telephony_provider = telephony_provider if telephony_provider in telephony.PROVIDERS else "twilio"
        s.twilio_account_sid = twilio_account_sid.strip()
        s.twilio_phone_number = twilio_phone_number.strip()
        s.plivo_auth_id = plivo_auth_id.strip()
        s.plivo_phone_number = plivo_phone_number.strip()
        s.company_display_name = company_display_name.strip()
        s.agent_name = agent_name.strip() or "Aria"
        s.business_desc = business_desc.strip()
        s.voice = voice.strip()
        s.speech_language = speech_language.strip()
        s.groq_model = groq_model.strip() or "llama-3.1-8b-instant"
        # An unchecked checkbox submits nothing, so presence means "on".
        s.detect_voicemail = bool(detect_voicemail)
        # Clamp: below 30s cuts off real conversations, above 30min is a runaway.
        s.max_call_seconds = max(30, min(1800, int(max_call_seconds or 180)))
        # "auto" or a small number of seconds; anything else would make Twilio
        # reject the Gather and drop the call.
        st = (speech_timeout or "auto").strip().lower()
        s.speech_timeout = st if st == "auto" or (st.isdigit() and 1 <= int(st) <= 10) else "auto"
        s.auto_language_switch = bool(auto_language_switch)
        s.ask_language = bool(ask_language)
        s.booking_enabled = bool(booking_enabled)
        s.booking_slot_minutes = max(5, min(240, int(booking_slot_minutes or 30)))
        s.booking_open = booking_open.strip() or "10:00"
        s.booking_close = booking_close.strip() or "18:00"
        s.booking_days_ahead = max(1, min(365, int(booking_days_ahead or 30)))

        # Only http(s) - a javascript: or file: URL here would be a footgun.
        url = webhook_out_url.strip()
        s.webhook_out_url = url if url.startswith(("http://", "https://")) else ""
        # Mint a signing secret the first time a URL is set, so the customer
        # always has something to verify against without an extra step.
        if s.webhook_out_url and not s.webhook_out_secret:
            s.webhook_out_secret = secrets.token_urlsafe(32)

        s.handoff_enabled = bool(handoff_enabled)
        s.handoff_number = engine.normalize_phone(handoff_number) if handoff_number.strip() else ""
        # Blank means "leave unchanged" - the form never receives the real
        # secret back, so an empty box must not wipe a stored credential.
        if twilio_auth_token.strip():
            s.twilio_auth_token_enc = encrypt(twilio_auth_token.strip())
        if plivo_auth_token.strip():
            s.plivo_auth_token_enc = encrypt(plivo_auth_token.strip())
        if groq_api_key.strip():
            s.groq_api_key_enc = encrypt(groq_api_key.strip())
        if openai_api_key.strip():
            s.openai_api_key_enc = encrypt(openai_api_key.strip())
        if nvidia_api_key.strip():
            s.nvidia_api_key_enc = encrypt(nvidia_api_key.strip())
        if deepgram_api_key.strip():
            s.deepgram_api_key_enc = encrypt(deepgram_api_key.strip())
        s.stt_provider = stt_provider if stt_provider in ("carrier", "deepgram") else "carrier"

        s.ai_provider = ai_provider if ai_provider in ("groq", "ollama", "openai", "nvidia") else "groq"
        s.ollama_host = ollama_host.strip().rstrip("/") or "http://localhost:11434"
        s.ollama_model = ollama_model.strip() or "llama3.2"
        s.openai_model = openai_model.strip() or "gpt-4o-mini"
        s.nvidia_model = nvidia_model.strip() or "nvidia/nvidia-nemotron-nano-9b-v2"
        db.add(s)

        company.timezone = company_timezone.strip() or "Asia/Kolkata"
        db.add(company)
        db.commit()
    return _redirect("/settings?saved=1")


@app.post("/api/test-twilio")
async def api_test_twilio(
    request: Request,
    twilio_account_sid: str = Form(default=""),
    twilio_auth_token: str = Form(default=""),
):
    """Test the credentials CURRENTLY IN THE FORM, falling back to stored ones.

    Testing only what is saved is a trap: you paste a token, hit Test, and get
    "Missing Auth Token" because the save hasn't happened yet. Whatever is on
    screen is what the user means to test.
    """
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)

    with Session(db_engine) as db:
        _, s = _load(user, db)
        sid = twilio_account_sid.strip() or s.twilio_account_sid
        # A blank token box means "keep the saved one", exactly as on save.
        tok = twilio_auth_token.strip() or decrypt(s.twilio_auth_token_enc)

    ok, detail = await asyncio.to_thread(engine.test_twilio, sid, tok)
    if ok and not (twilio_account_sid.strip() or twilio_auth_token.strip()):
        return {"ok": ok, "detail": detail}
    if ok:
        detail += "  (not saved yet — press Save settings to keep these)"
    return {"ok": ok, "detail": detail}


@app.post("/api/test-plivo")
async def api_test_plivo(
    request: Request,
    plivo_auth_id: str = Form(default=""),
    plivo_auth_token: str = Form(default=""),
):
    """Same as /api/test-twilio, but for Plivo — whatever is on screen wins."""
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)

    with Session(db_engine) as db:
        _, s = _load(user, db)
        auth_id = plivo_auth_id.strip() or s.plivo_auth_id
        auth_token = plivo_auth_token.strip() or decrypt(s.plivo_auth_token_enc)

    ok, detail = await asyncio.to_thread(telephony.test_plivo, auth_id, auth_token)
    if ok and not (plivo_auth_id.strip() or plivo_auth_token.strip()):
        return {"ok": ok, "detail": detail}
    if ok:
        detail += "  (not saved yet — press Save settings to keep these)"
    return {"ok": ok, "detail": detail}


@app.post("/api/test-ai")
async def api_test_ai(
    request: Request,
    ai_provider: str = Form(default=""),
    groq_api_key: str = Form(default=""),
    groq_model: str = Form(default=""),
    ollama_host: str = Form(default=""),
    ollama_model: str = Form(default=""),
    openai_api_key: str = Form(default=""),
    openai_model: str = Form(default=""),
    nvidia_api_key: str = Form(default=""),
    nvidia_model: str = Form(default=""),
):
    """Round-trip the AI provider currently selected IN THE FORM.

    Catches a wrong Ollama host or a dead key here, in Settings, rather than
    halfway through a live call. Values on screen win over stored ones so the
    button tests what the user is looking at.
    """
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)

    with Session(db_engine) as db:
        _, s = _load(user, db)
        # A detached copy: never persist anything from a test.
        probe = CompanySettings(
            company_id=s.company_id,
            ai_provider=(ai_provider or s.ai_provider or "groq"),
            groq_api_key_enc=encrypt(groq_api_key.strip()) if groq_api_key.strip()
            else s.groq_api_key_enc,
            groq_model=groq_model.strip() or s.groq_model,
            ollama_host=ollama_host.strip().rstrip("/") or s.ollama_host,
            ollama_model=ollama_model.strip() or s.ollama_model,
            openai_api_key_enc=encrypt(openai_api_key.strip()) if openai_api_key.strip()
            else s.openai_api_key_enc,
            openai_model=openai_model.strip() or s.openai_model,
            nvidia_api_key_enc=encrypt(nvidia_api_key.strip()) if nvidia_api_key.strip()
            else s.nvidia_api_key_enc,
            nvidia_model=nvidia_model.strip() or s.nvidia_model,
            speech_language=s.speech_language,
        )
        unsaved = bool(groq_api_key.strip() or openai_api_key.strip() or nvidia_api_key.strip()
                       or (ai_provider and ai_provider != s.ai_provider))

    ok, detail = await asyncio.to_thread(engine.test_ai, probe)
    if ok and unsaved:
        detail += "  (not saved yet — press Save settings to keep these)"
    return {"ok": ok, "detail": detail}


# ============================== AGENTS ==============================
@app.post("/api/agent")
async def create_agent(
    request: Request,
    name: str = Form(...),
    greeting: str = Form(default=""),
    system_prompt: str = Form(default=""),
    voice: str = Form(default="Polly.Aditi"),
    speech_language: str = Form(default="en-IN"),
):
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    with Session(db_engine) as db:
        company, _ = _load(user, db)
        try:
            plans.check_agent_limit(company, db)
        except plans.PlanLimitError as exc:
            return _err(str(exc), 402)
        agent = Agent(
            company_id=company.id,
            name=name.strip(),
            greeting=greeting.strip() or "Hi {name}, how can I help you today?",
            system_prompt=system_prompt.strip(),
            voice=voice.strip(),
            speech_language=speech_language.strip(),
        )
        db.add(agent)
        db.commit()
        db.refresh(agent)
    return {"ok": True, "id": agent.id}


@app.post("/api/agent/{agent_id}/delete")
async def delete_agent(request: Request, agent_id: int):
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    with Session(db_engine) as db:
        agent = db.get(Agent, agent_id)
        # Scope check: an id from another company must never be deletable here.
        if not agent or agent.company_id != user.company_id:
            return _err("Not found", 404)
        # Detach rather than orphan: campaigns keep their inline persona copy.
        for camp in db.exec(select(Campaign).where(Campaign.agent_id == agent_id)).all():
            camp.agent_id = None
            db.add(camp)
        db.delete(agent)
        db.commit()
    return {"ok": True}


@app.post("/api/agent/{agent_id}/toggle")
async def toggle_agent(request: Request, agent_id: int):
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    with Session(db_engine) as db:
        agent = db.get(Agent, agent_id)
        if not agent or agent.company_id != user.company_id:
            return _err("Not found", 404)
        agent.active = not agent.active
        db.add(agent)
        db.commit()
        return {"ok": True, "active": agent.active}


# ============================== FAQs ==============================
@app.post("/api/faq")
async def add_faq(request: Request, question: str = Form(...), answer: str = Form(...)):
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    question, answer = question.strip(), answer.strip()
    if not question or not answer:
        return _err("Both question and answer are required.")
    with Session(db_engine) as db:
        faq = Faq(company_id=user.company_id, question=question, answer=answer)
        db.add(faq)
        db.commit()
        db.refresh(faq)
    return {"ok": True, "id": faq.id, "question": faq.question, "answer": faq.answer}


@app.post("/api/faq/{faq_id}/delete")
async def delete_faq(request: Request, faq_id: int):
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    with Session(db_engine) as db:
        faq = db.get(Faq, faq_id)
        if not faq or faq.company_id != user.company_id:
            return _err("Not found", 404)
        db.delete(faq)
        db.commit()
    return {"ok": True}


# ============================== CAMPAIGNS ==============================
def _build_persona(campaign_type: str, name: str, script: str, agent_name: str, company: str):
    if "Support" in campaign_type:
        greeting = (
            f"Hi {{name}}, this is {agent_name} from {company} support regarding {name}. "
            "How can I help you today?"
        )
        knowledge = (
            f'This is a CUSTOMER SUPPORT call for "{name}". '
            f'Your goal: {script or "help the customer, be empathetic."}'
        )
    elif "Reminder" in campaign_type:
        greeting = f"Hi {{name}}, this is {agent_name} from {company} about {name}. Just a quick reminder call."
        knowledge = (
            f'This is a REMINDER call for "{name}". '
            f'Your goal: {script or "remind and confirm or reschedule."}'
        )
    else:
        greeting = f"Hi {{name}}, this is {agent_name} from {company} about {name}."
        knowledge = (
            f'This is a SALES call for "{name}". '
            f'Your goal: {script or "introduce the offer and gauge interest."}'
        )
    return greeting, knowledge


@app.post("/api/campaign")
async def save_campaign(
    request: Request,
    name: str = Form(...),
    campaign_type: str = Form(default="Sales & Lead Gen"),
    script: str = Form(default=""),
    voice: str = Form(default="Polly.Aditi|Aria"),
    speech_language: str = Form(default="en-IN"),
    call_window_start: str = Form(default="09:00"),
    call_window_end: str = Form(default="20:00"),
    max_calls_per_day: int = Form(default=500),
    retry_policy: str = Form(default="No retry"),
    auto_language_switch: str = Form(default=""),
    ask_language: str = Form(default=""),
    agent_id: int = Form(default=0),
):
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)

    voice_id, _, agent_name = voice.partition("|")
    agent_name = agent_name or "Aria"
    with Session(db_engine) as db:
        company, s = _load(user, db)
        try:
            plans.check_campaign_limit(company, db)
        except plans.PlanLimitError as exc:
            return _err(str(exc), 402)

        display = s.company_display_name or "our company"
        greeting, knowledge = _build_persona(campaign_type, name, script, agent_name, display)
        camp = Campaign(
            company_id=company.id,
            name=name.strip(),
            campaign_type=campaign_type,
            greeting=greeting,
            knowledge=knowledge,
            script=script.strip(),
            voice=voice_id,
            agent_name=agent_name,
            speech_language=speech_language,
            call_window_start=call_window_start,
            call_window_end=call_window_end,
            max_calls_per_day=max_calls_per_day,
            retry_policy=retry_policy,
            auto_language_switch=bool(auto_language_switch),
            ask_language=bool(ask_language),
            agent_id=agent_id or None,
        )
        db.add(camp)
        db.commit()
        db.refresh(camp)
        return {"ok": True, "campaign_id": camp.id, "greeting": greeting}


@app.post("/api/campaign/{cid}/edit")
async def edit_campaign(
    request: Request,
    cid: int,
    name: str = Form(...),
    campaign_type: str = Form(default="Sales & Lead Gen"),
    script: str = Form(default=""),
    voice: str = Form(default="Polly.Aditi|Aria"),
    speech_language: str = Form(default="en-IN"),
    call_window_start: str = Form(default="09:00"),
    call_window_end: str = Form(default="20:00"),
    max_calls_per_day: int = Form(default=500),
    retry_policy: str = Form(default="No retry"),
    auto_language_switch: str = Form(default=""),
    ask_language: str = Form(default=""),
    agent_id: int = Form(default=0),
):
    """Update an existing campaign.

    Previously a campaign could only be started, paused or deleted - a typo in
    the script meant deleting it and re-uploading every contact. Contacts and
    call history are attached to the campaign id, so editing preserves both.
    """
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)

    voice_id, _, agent_name = voice.partition("|")
    with Session(db_engine) as db:
        company, s = _load(user, db)
        camp = db.get(Campaign, cid)
        if not camp or camp.company_id != company.id:
            return _err("Campaign not found", 404)

        display = s.company_display_name or "our company"
        greeting, knowledge = _build_persona(
            campaign_type, name, script, agent_name or "Aria", display
        )
        camp.name = name.strip()
        camp.campaign_type = campaign_type
        camp.greeting = greeting
        camp.knowledge = knowledge
        camp.script = script.strip()
        camp.voice = voice_id
        camp.agent_name = agent_name or "Aria"
        camp.speech_language = speech_language
        camp.call_window_start = call_window_start
        camp.call_window_end = call_window_end
        camp.max_calls_per_day = max_calls_per_day
        camp.retry_policy = retry_policy
        camp.auto_language_switch = bool(auto_language_switch)
        camp.ask_language = bool(ask_language)
        camp.agent_id = agent_id or None
        db.add(camp)
        db.commit()
    return {"ok": True, "detail": "Campaign updated."}


@app.post("/api/campaign/{cid}/{action}")
async def campaign_action(request: Request, cid: int, action: str):
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    if action not in {"start", "pause", "delete"}:
        return _err("Unknown action", 404)

    with Session(db_engine) as db:
        company, s = _load(user, db)
        camp = db.get(Campaign, cid)
        # Scope check: a campaign id from another tenant must never be usable.
        if not camp or camp.company_id != company.id:
            return _err("Campaign not found", 404)

        if action == "delete":
            for contact in db.exec(select(Contact).where(Contact.campaign_id == cid)).all():
                db.delete(contact)
            db.delete(camp)
            db.commit()
            return {"ok": True}

        if action == "pause":
            camp.status = "paused"
            camp.pause_reason = "Paused by you"
            db.add(camp)
            db.commit()
            return {"ok": True, "status": camp.status}

        # start
        if not user.is_verified:
            return _err("Verify your email before placing calls.", 403)
        if not telephony.is_connected(s):
            return _err(f"{telephony.provider_of(s).title()} is not connected. Add your credentials in Settings.", 400)
        if not engine.ai_configured(s):
            return _err("Configure an AI provider in Settings so the agent can speak.", 400)
        if not os.getenv("PUBLIC_BASE_URL", "").strip():
            return _err(
                "PUBLIC_BASE_URL is not set. The scheduler needs a public URL for "
                "Twilio to call back to.",
                400,
            )
        try:
            plans.check_minutes(company)
        except plans.PlanLimitError as exc:
            return _err(str(exc), 402)

        waiting = db.exec(
            select(func.count())
            .select_from(Contact)
            .where(Contact.campaign_id == cid, Contact.status == "pending")
        ).one()
        if not waiting:
            return _err("No pending contacts. Upload a CSV first.", 400)

        camp.status = "running"
        camp.pause_reason = ""
        camp.last_run_at = utcnow()
        db.add(camp)
        db.commit()
        return {
            "ok": True,
            "status": "running",
            "queued": waiting,
            "detail": f"{waiting} contact(s) queued. The scheduler will dial them within "
            f"{camp.call_window_start}-{camp.call_window_end} ({company.timezone}), "
            f"max {camp.max_calls_per_day}/day.",
        }


# ============================== CONTACTS / CSV ==============================
def _list_templates() -> list[dict]:
    if not CSV_TEMPLATES_DIR.exists():
        return []
    items = []
    for f in sorted(CSV_TEMPLATES_DIR.glob("*.csv")):
        stem = f.stem
        parts = stem.split("_", 1)
        label = parts[1].replace("_", " ").title() if len(parts) > 1 else stem.title()
        items.append({"filename": f.name, "name": label})
    return items


@app.get("/templates/{filename}")
async def download_template(request: Request, filename: str):
    if not get_current_user(request):
        return _redirect("/login")
    # Resolve and confirm the result is still inside the templates directory.
    # A name check alone misses Windows separators and symlinks.
    candidate = (CSV_TEMPLATES_DIR / filename).resolve()
    if candidate.parent != CSV_TEMPLATES_DIR.resolve() or not candidate.is_file():
        return _err("Template not found", 404)
    return FileResponse(candidate, media_type="text/csv", filename=candidate.name)


@app.post("/api/contacts/upload")
async def upload_contacts(
    request: Request,
    campaign_id: int = Form(...),
    numbers: str = Form(default=""),
    file: UploadFile | None = File(default=None),
):
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)

    text = numbers or ""
    if file and file.filename:
        text += "\n" + (await file.read()).decode("utf-8", errors="ignore")

    with Session(db_engine) as db:
        company, _ = _load(user, db)
        camp = db.get(Campaign, campaign_id)
        if not camp or camp.company_id != company.id:
            return _err("Campaign not found", 404)

        customers = engine.parse_csv(text)
        if not customers:
            return _err("No valid phone numbers found.")

        try:
            plans.check_contact_limit(company, db, len(customers))
        except plans.PlanLimitError as exc:
            return _err(str(exc), 402)

        existing = {
            c.phone
            for c in db.exec(
                select(Contact).where(Contact.campaign_id == campaign_id)
            ).all()
        }
        added = 0
        for cust in customers:
            if cust["phone"] in existing:
                continue
            db.add(
                Contact(
                    company_id=company.id,
                    campaign_id=campaign_id,
                    name=cust.get("name", ""),
                    phone=cust["phone"],
                    extra_json=json.dumps(
                        {k: v for k, v in cust.items() if k not in ("phone", "name")}
                    ),
                )
            )
            existing.add(cust["phone"])
            added += 1
        db.commit()
    return {
        "ok": True,
        "imported": added,
        "skipped": len(customers) - added,
        "detail": f"{added} contact(s) queued"
        + (f", {len(customers) - added} duplicate(s) skipped" if len(customers) - added else ""),
    }


@app.post("/api/contacts/{contact_id}/delete")
async def delete_contact(request: Request, contact_id: int):
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    with Session(db_engine) as db:
        contact = db.get(Contact, contact_id)
        if not contact or contact.company_id != user.company_id:
            return _err("Not found", 404)
        db.delete(contact)
        db.commit()
    return {"ok": True}


# ============================== VOICE PREVIEW ==============================
@app.post("/api/call/preview-voices")
async def preview_voices(
    request: Request,
    phone: str = Form(...),
    mode: str = Form(default="all"),   # "all" = every identity | "languages" = one identity, every language
    voice: str = Form(default=""),
):
    """Ring the user and play voice samples, so they can choose by ear.

    There is no way to preview these voices without a real call: Twilio has no
    standalone text-to-speech endpoint, and Polly/Chirp3 voices exist only
    inside a call. A browser preview would use the listener's OS voice, which
    is not what the customer hears - worse than useless for choosing.
    """
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    if not user.is_verified:
        return _err("Verify your email before placing calls.", 403)

    with Session(db_engine) as db:
        company, s = _load(user, db)
        try:
            plans.check_minutes(company)
        except plans.PlanLimitError as exc:
            return _err(str(exc), 402)

        number = engine.normalize_phone(phone)
        if not number:
            return _err("Enter a valid phone number.")

        base = _public_base(request)
        if _is_local(base):
            return _err(
                f"Twilio cannot call back to {base} — that address only exists on "
                "this machine. Run 'ngrok http 8010', put the https URL in "
                "PUBLIC_BASE_URL in your .env, and restart.",
                400,
            )

        # The preview is pure speech - no AI involved - so it deliberately does
        # NOT require an AI provider to be configured. Choosing a voice is
        # something you do before finishing setup.
        mode = "languages" if mode == "languages" else "all"
        chosen = voice.strip() or s.voice
        query = f"?mode={mode}&voice={chosen}"
        webhook_base = f"{base}/hook/{company.webhook_token}"
        try:
            sid, provider = await asyncio.to_thread(
                telephony.place_call, s, webhook_base, number,
                None, False, f"/preview{query}",
            )
        except telephony.TelephonyError as exc:
            return _err(str(exc), 400)
        except Exception as exc:
            msg = str(exc)
            if "20003" in msg or "Authenticate" in msg or "401" in msg:
                msg = f"{telephony.provider_of(s).title()} authentication failed. Check your credentials in Settings."
            elif "21219" in msg or "unverified" in msg.lower():
                msg = "Number not verified on your trial account."
            return _err(msg, 400)
        engine.start_session(db, call_sid=sid, company_id=company.id, customer={},
                             provider=provider)

    detail = (
        f"Calling {number} — you'll hear all 8 voices."
        if mode == "all"
        else f"Calling {number} — you'll hear {chosen} in all 7 languages."
    )
    return {"ok": True, "detail": detail, "call_sid": sid}


@app.post("/hook/{token}/preview")
async def hook_preview(request: Request, token: str, mode: str = "all", voice: str = ""):
    """Reply for the preview call. No AI, no Gather - just speech then hangup."""
    with Session(db_engine) as db:
        company, s = _tenant(token, db)
        form = dict((await request.form()))
        CallSid = form.get("CallSid") or form.get("CallUUID") or ""
        # The call was placed with whichever provider was active AT THAT TIME
        # - read from the stamped session, never re-derived from settings,
        # so a mid-flight provider change in Settings can't scramble the
        # dialect this reply is rendered in.
        session = engine.get_session_for(db, CallSid) if CallSid else None
        provider = session.provider if session else telephony.provider_of(s) if s else "twilio"

        if not company:
            return _twiml(telephony.CallResponse().hangup(), provider)
        if not await _verify_webhook(request, s, provider):
            return _twiml(telephony.CallResponse().hangup(), provider)

        language = s.speech_language if s.speech_language in engine.SUPPORTED_LANGUAGES else "en-IN"
        resp = telephony.CallResponse()

        if mode == "languages":
            # One identity, every language - demonstrates that it stays the
            # same person while the language changes.
            name = voice or s.voice
            resp.say(f"Here is {name} speaking each of the seven languages.",
                     voice=engine.voice_for(name, "en-IN"))
            for lang, line in engine.PREVIEW_LINES.items():
                resp.pause(1)
                resp.say(line, voice=engine.voice_for(name, lang))
        else:
            # Every identity, in the company's own language - the "which voice
            # do I want" comparison.
            resp.say("Here are the eight voices. Each one can speak all seven languages.",
                     voice=engine.voice_for("Aoede", "en-IN"))
            line = engine.PREVIEW_LINES.get(language, engine.PREVIEW_LINES["en-IN"])
            for name, gender in engine.CHIRP_VOICES.items():
                resp.pause(1)
                resp.say(f"{name}, {gender}.", voice=engine.voice_for(name, "en-IN"))
                resp.say(line, voice=engine.voice_for(name, language))

        resp.pause(1)
        resp.say("That's all of them. Goodbye.", voice=engine.voice_for("Aoede", "en-IN"))
        resp.hangup()
        if session:
            engine.end_call(db, CallSid)
        return _twiml(resp, provider)


# ============================== TEST CALL ==============================
@app.post("/api/call/test")
async def test_call(
    request: Request,
    phone: str = Form(...),
    campaign_id: int = Form(default=0),
    agent_id: int = Form(default=0),
):
    """Dial a single number right now, bypassing the scheduler queue.

    One call is safe to place inline, but the Twilio SDK is blocking, so it
    still goes to a worker thread rather than stalling the event loop.
    """
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    if not user.is_verified:
        return _err("Verify your email before placing calls.", 403)

    with Session(db_engine) as db:
        company, s = _load(user, db)
        try:
            plans.check_minutes(company)
        except plans.PlanLimitError as exc:
            return _err(str(exc), 402)
        if not engine.ai_configured(s):
            return _err("Configure an AI provider in Settings first.", 400)

        camp = db.get(Campaign, campaign_id) if campaign_id else None
        if camp and camp.company_id != company.id:
            return _err("Campaign not found", 404)

        chosen_agent = db.get(Agent, agent_id) if agent_id else None
        if chosen_agent and chosen_agent.company_id != company.id:
            return _err("Agent not found", 404)

        number = engine.normalize_phone(phone)
        if not number:
            return _err("Enter a valid phone number.")

        # Twilio dials out fine, then calls BACK to fetch what the agent should
        # say. If that address is loopback or a private LAN address, Twilio
        # cannot reach it and the customer hears "an application error has
        # occurred" - with nothing in our logs, because the request never
        # arrived. Fail here, where the reason can actually be explained.
        base = _public_base(request)
        if _is_local(base):
            return _err(
                f"Twilio cannot call back to {base} — that address only exists on "
                "this machine. Run 'ngrok http 8010', put the https URL in "
                "PUBLIC_BASE_URL in your .env, and restart.",
                400,
            )

        webhook_base = f"{base}/hook/{company.webhook_token}"
        try:
            sid, provider = await asyncio.to_thread(telephony.place_call, s, webhook_base, number)
        except Exception as exc:
            msg = str(exc)
            if "20003" in msg or "Authenticate" in msg:
                msg = f"{telephony.provider_of(s).title()} authentication failed. Check your credentials in Settings."
            elif "21219" in msg or "unverified" in msg.lower():
                msg = "Number not verified on your Twilio trial account."
            return _err(msg, 400)

        engine.start_session(
            db,
            call_sid=sid,
            company_id=company.id,
            customer={"phone": number, "name": "Test call"},
            campaign_id=camp.id if camp else None,
            agent_id=chosen_agent.id if chosen_agent else None,
            language=(
                chosen_agent.speech_language if chosen_agent
                else camp.speech_language if camp
                else s.speech_language
            ),
            provider=provider,
        )
    return {"ok": True, "detail": f"Calling {number} — SID {sid[:14]}…", "call_sid": sid}


# ============================== BILLING ==============================
@app.post("/api/billing/upgrade")
async def billing_upgrade(request: Request, plan: str = Form(...)):
    """Start an upgrade. This does NOT grant the plan - see plans.confirm_upgrade.

    v2's equivalent endpoint set company.plan directly with no payment check at
    all, so any logged-in user could take the Rs 19,999 tier for free.
    """
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    if user.role != "owner":
        return _err("Only the workspace owner can change the plan.", 403)

    with Session(db_engine) as db:
        company, _ = _load(user, db)
        try:
            invoice = plans.start_upgrade(company, plan, db)
        except plans.PlanLimitError as exc:
            return _err(str(exc))

        if plans.razorpay_configured():
            try:
                order = plans.create_razorpay_order(invoice)
            except plans.PlanLimitError as exc:
                invoice.status = "failed"
                db.add(invoice)
                db.commit()
                return _err(str(exc), 502)
            db.add(invoice)
            db.commit()
            return {
                "ok": True,
                "invoice_id": invoice.id,
                "amount_inr": invoice.amount_inr,
                "next": "razorpay",
                "order_id": order["id"],
                "key_id": os.getenv("RAZORPAY_KEY_ID", "").strip(),
                "company_name": company.name,
                "detail": "Complete payment to activate the plan.",
            }
        if plans.demo_upgrades_allowed():
            try:
                plans.confirm_upgrade(company, invoice, db)
            except plans.PlanLimitError as exc:
                return _err(str(exc))
            return {"ok": True, "next": "done", "detail": f"Demo grant: now on {plan}."}
        return _err(
            "Payments are not configured. Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET, "
            "or set SANCHARAI_ALLOW_DEMO_UPGRADES=1 for local testing.",
            400,
        )


@app.post("/api/billing/confirm")
async def billing_confirm(
    request: Request,
    invoice_id: int = Form(...),
    razorpay_order_id: str = Form(default=""),
    razorpay_payment_id: str = Form(default=""),
    razorpay_signature: str = Form(default=""),
):
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    if user.role != "owner":
        return _err("Only the workspace owner can change the plan.", 403)

    with Session(db_engine) as db:
        company, _ = _load(user, db)
        invoice = db.get(Invoice, invoice_id)
        if not invoice or invoice.company_id != company.id:
            return _err("Invoice not found", 404)
        try:
            plans.confirm_upgrade(
                company,
                invoice,
                db,
                order_id=razorpay_order_id,
                payment_id=razorpay_payment_id,
                signature=razorpay_signature,
            )
        except plans.PlanLimitError as exc:
            return _err(str(exc), 402)
        # Read while the session is still open - after the `with` block ends,
        # `invoice` is detached and touching any of its columns raises.
        upgraded_plan = invoice.plan
    return {"ok": True, "detail": f"Upgraded to {upgraded_plan}."}


# ============================== TEAM ==============================
@app.post("/api/team/invite")
async def invite_member(request: Request, email: str = Form(...), role: str = Form(default="member")):
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    if user.role != "owner":
        return _err("Only the workspace owner can invite people.", 403)

    email = email.strip().lower()
    if not email or "@" not in email:
        return _err("Enter a valid email address.")

    with Session(db_engine) as db:
        company, _ = _load(user, db)
        if db.exec(select(User).where(User.email == email)).first():
            return _err("That email already has an account.")
        invite = Invite(
            company_id=company.id,
            email=email,
            role="member" if role != "owner" else "owner",
            invited_by=user.email,
        )
        db.add(invite)
        db.commit()
        db.refresh(invite)
        link = f"{_public_base(request)}/invite/{make_invite_token(invite.id)}"
        if mailer.is_configured():
            mailer.send_invite(email, company.name, user.email, link)
            detail = f"Invitation sent to {email}."
        else:
            print(f"[EMAIL NOT CONFIGURED] invite link for {email}: {link}", flush=True)
            detail = "Email is not configured — copy the link below and send it yourself."
    return {"ok": True, "detail": detail, "link": link, "id": invite.id}


@app.post("/api/team/invite/{invite_id}/cancel")
async def cancel_invite(request: Request, invite_id: int):
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    if user.role != "owner":
        return _err("Only the workspace owner can do that.", 403)
    with Session(db_engine) as db:
        invite = db.get(Invite, invite_id)
        if not invite or invite.company_id != user.company_id:
            return _err("Not found", 404)
        db.delete(invite)
        db.commit()
    return {"ok": True}


# ============================== TWILIO WEBHOOKS ==============================
def _persona(
    db: Session, s: CompanySettings, campaign: Campaign | None, agent_id: int | None = None
) -> tuple[str, str, str, str]:
    """Who the agent IS on this call: (greeting, voice, language, agent_name).

    Resolution order, most specific first:
      1. An Agent record - chosen directly for a test call, or linked to the
         campaign. This is the reusable persona the Agents page creates.
      2. The campaign's own inline persona, for campaigns built before agents
         existed or that deliberately override one.
      3. Company Settings, for a bare test call with neither.

    Agents were previously stored and displayed but never read by any call
    path, so creating one appeared to do nothing.
    """
    agent = None
    if agent_id:
        agent = db.get(Agent, agent_id)
    elif campaign and campaign.agent_id:
        agent = db.get(Agent, campaign.agent_id)

    # An agent belonging to another company must never be usable here, and a
    # paused one should not answer the phone.
    if agent and (agent.company_id != s.company_id or not agent.active):
        agent = None

    if agent:
        return (
            agent.greeting or f"Hello, this is {agent.name}.",
            agent.voice or s.voice,
            agent.speech_language or s.speech_language,
            agent.name,
        )
    if campaign:
        return campaign.greeting, campaign.voice, campaign.speech_language, campaign.agent_name
    return (
        f"Hello, this is {s.agent_name}.",
        s.voice,
        s.speech_language,
        s.agent_name,
    )


def _tenant(token: str, db: Session) -> tuple[Company | None, CompanySettings | None]:
    company = db.exec(select(Company).where(Company.webhook_token == token)).first()
    if not company:
        return None, None
    s = db.exec(select(CompanySettings).where(CompanySettings.company_id == company.id)).first()
    return company, s


def _twiml(resp: telephony.CallResponse, provider: str) -> Response:
    """Render a CallResponse in whichever dialect the CALL was placed with.

    Kept under its old name and single-argument call sites, updated to also
    take provider, so the diff at every call site is mechanical rather than
    a rethink of each handler.
    """
    body, content_type = resp.render(provider)
    return Response(content=body, media_type=content_type)


async def _verify_webhook(request: Request, settings: CompanySettings, provider: str) -> bool:
    """Confirm the POST really came from the claimed carrier before acting on it."""
    if os.getenv("SANCHARAI_SKIP_TWILIO_VALIDATION", "").strip() == "1":
        return True
    form = await request.form()
    url = f"{_public_base(request)}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"
    return telephony.validate_signature(
        settings, provider, url,
        {k: str(v) for k, v in form.items()},
        dict(request.headers),
    )


@app.websocket("/hook/{token}/stream")
async def hook_stream(ws: WebSocket, token: str):
    """Where the carrier delivers the caller's audio when STT is ours.

    Note there is no signature to check here: neither carrier signs a
    WebSocket upgrade the way it signs a webhook POST. The tenant's random
    webhook token in the path is the only credential, exactly as it is for the
    rest of /hook/* - which is why Settings tells customers to treat it like a
    password.
    """
    with Session(db_engine) as db:
        company, s = _tenant(token, db)
        if not company or not s:
            await ws.close(code=1008)
            return
        # Copy what the stream needs before the session closes; the handler
        # outlives this block by the whole length of the call.
        settings, company_row = s, company

    await ws.accept()
    print(f"[stream] {company_row.name}: websocket open", flush=True)
    try:
        await streaming.run_call(ws, company_row, settings, token, _public_base())
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001 - already gone is fine
            pass


@app.post("/hook/{token}/voice")
async def hook_voice(request: Request, token: str):
    with Session(db_engine) as db:
        company, s = _tenant(token, db)
        form = dict((await request.form()))
        # The call was placed by whichever provider was active when it was
        # dialled - start_session() already stamped that onto the session, so
        # this is read from there rather than re-derived from current
        # settings, which could have changed since.
        raw_call_id = form.get("CallSid") or form.get("CallUUID") or ""
        session = engine.get_session_for(db, raw_call_id) if raw_call_id else None
        provider = session.provider if session else telephony.provider_of(s) if s else "twilio"
        wf = telephony.normalize_webhook(provider, form)
        CallSid = wf["call_id"]

        if not company:
            return _twiml(telephony.CallResponse().say("This number is not configured.", "").hangup(), provider)
        if not await _verify_webhook(request, s, provider):
            return _twiml(telephony.CallResponse().hangup(), provider)

        # Voicemail: hang up rather than talk to an answering machine and bill
        # the whole message. v2 had no answering-machine handling at all.
        if wf["answered_by"].startswith("machine") or wf["answered_by"] == "fax":
            print(f"[{company.name}] voicemail on {CallSid[:10]} — hanging up", flush=True)
            return _twiml(telephony.CallResponse().hangup(), provider)

        resp = telephony.CallResponse()
        campaign = db.get(Campaign, session.campaign_id) if session and session.campaign_id else None
        raw_greeting, voice_name, language, agent_name = _persona(
            db, s, campaign, session.agent_id if session else None
        )
        customer = dict(session.customer) if session else {}
        customer.setdefault("agent_name", agent_name)

        greeting = engine.fill_template(raw_greeting, customer)
        # A Chirp3 identity resolves against the language being spoken; a fixed
        # Polly/Google voice passes through unchanged.
        voice = engine.voice_for(voice_name, language)

        ask_language = bool(campaign.ask_language if campaign else s.ask_language)

        # Seed the session's in-call language now, so the first customer reply
        # is transcribed with the same hint the greeting was spoken in.
        if session and session.language != language:
            session.language = language
            db.add(session)
            db.commit()

        if ask_language and session:
            # Open by ASKING rather than assuming. The menu is spoken in
            # English because we don't yet know what they speak, and the
            # language names are the recognisable part either way.
            session.awaiting_language = True
            session.language_asks = 1
            db.add(session)
            db.commit()
            resp.gather(
                action=f"/hook/{token}/respond", language="en-IN",
                speech_timeout=s.speech_timeout,
                say_text=engine.language_menu(agent_name=agent_name, company=s.company_display_name),
                voice=engine.voice_for(voice_name, "en-IN"),
            )
            return _twiml(resp, provider)

        # Streaming STT: speak the greeting, then fork the caller's audio to
        # our own WebSocket instead of paying the carrier per <Gather>. The
        # <Pause> is what keeps the call alive and listening - without it the
        # document would end and the carrier would hang up on a caller who is
        # about to speak. streaming.py redirects the call when it has a reply.
        if telephony.provider_of(s) == "twilio" and getattr(s, "stt_provider", "") == "deepgram" \
                and s.deepgram_api_key_enc:
            base = _public_base(request)
            ws_url = f"{base}/hook/{token}/stream".replace("https://", "wss://") \
                                                  .replace("http://", "ws://")
            resp.say(greeting, voice)
            resp.stream(ws_url)
            resp.pause(30)
            print(f"[{company.name}] streaming STT on {CallSid[:10]} -> {ws_url}", flush=True)
            return _twiml(resp, provider)

        resp.gather(
            action=f"/hook/{token}/respond", language=language,
            speech_timeout=s.speech_timeout, say_text=greeting, voice=voice,
        )
        return _twiml(resp, provider)


@app.post("/hook/{token}/respond")
async def hook_respond(request: Request, token: str):
    with Session(db_engine) as db:
        company, s = _tenant(token, db)
        form = dict((await request.form()))
        raw_call_id = form.get("CallSid") or form.get("CallUUID") or ""
        session = engine.get_session_for(db, raw_call_id) if raw_call_id else None
        provider = session.provider if session else telephony.provider_of(s) if s else "twilio"
        wf = telephony.normalize_webhook(provider, form)
        CallSid = wf["call_id"]
        SpeechResult = wf["speech"]

        resp = telephony.CallResponse()
        if not company:
            resp.hangup()
            return _twiml(resp, provider)
        if not await _verify_webhook(request, s, provider):
            resp.hangup()
            return _twiml(resp, provider)
        campaign = db.get(Campaign, session.campaign_id) if session and session.campaign_id else None
        raw_greeting, voice_name, persona_language, agent_name = _persona(
            db, s, campaign, session.agent_id if session else None
        )
        # The language to LISTEN in right now is whatever the session was left
        # on - the fixed campaign language normally, or wherever the last turn
        # switched to when auto_language_switch is enabled.
        language = (session.language if session else None) or persona_language
        auto_switch = bool(
            campaign.auto_language_switch if campaign else s.auto_language_switch
        )
        # Campaign overrides company default, same fallback shape as language.
        handoff_number = (
            (campaign.handoff_number if campaign and campaign.handoff_enabled else "")
            or (s.handoff_number if s.handoff_enabled else "")
        ).strip()

        try:
            # The opening language question is answered here, before the AI is
            # involved at all - the reply is a menu choice, not conversation.
            if session and session.awaiting_language:
                chosen = engine.detect_language_choice(SpeechResult)

                if not chosen and session.language_asks < 2:
                    # One re-ask, then give up rather than trapping someone in
                    # a menu they may not have understood.
                    session.language_asks += 1
                    db.add(session)
                    db.commit()
                    # No intro on the re-ask - they already know who is calling.
                    resp.gather(
                        action=f"/hook/{token}/respond", language="en-IN",
                        speech_timeout=s.speech_timeout,
                        say_text="Sorry, I didn't catch that. " + engine.language_menu(intro=False),
                        voice=engine.voice_for(voice_name, "en-IN"),
                    )
                    return _twiml(resp, provider)

                # Either they chose, or we've asked twice and fall back to the
                # configured default so the call can actually get started.
                picked = chosen or language
                session.awaiting_language = False
                session.language = picked
                db.add(session)
                db.commit()
                print(f"[{company.name}] language chosen: {picked}"
                      f"{'' if chosen else ' (defaulted after 2 tries)'}", flush=True)

                # Now deliver the REAL greeting, in the language they picked.
                customer = dict(session.customer)
                customer.setdefault("agent_name", agent_name)
                greeting = engine.fill_template(raw_greeting, customer)
                # Greetings are authored in English. Switching the voice does not
                # translate them, so the TEXT has to be translated too - otherwise
                # the caller hears English words in a Gujarati accent.
                greeting = await asyncio.to_thread(engine.greeting_in, s, greeting, picked)
                resp.gather(
                    action=f"/hook/{token}/respond", language=picked,
                    speech_timeout=s.speech_timeout,
                    say_text=greeting, voice=engine.voice_for(voice_name, picked),
                )
                return _twiml(resp, provider)

            if not SpeechResult.strip():
                resp.gather(
                    action=f"/hook/{token}/respond", language=language,
                    speech_timeout=s.speech_timeout,
                    say_text="Sorry, I didn't catch that. Could you repeat?",
                    voice=engine.voice_for(voice_name, language),
                )
                return _twiml(resp, provider)

            print(f"[{company.name}] heard: {SpeechResult!r}", flush=True)
            faqs = db.exec(select(Faq).where(Faq.company_id == company.id)).all()
            # The model needs today's date in the COMPANY's timezone to turn
            # "tomorrow at 11" into a real timestamp.
            today = to_local(utcnow(), company.timezone).strftime("%A %d %B %Y")
            reply, language, wants_handoff, book_when = await asyncio.to_thread(
                engine.get_reply, db, s, campaign, CallSid, SpeechResult, faqs,
                auto_switch, bool(handoff_number), bool(s.booking_enabled), today,
            )

            # Booking is decided AFTER the model has spoken, because the model
            # cannot know what is already in the diary. If the slot is taken,
            # its confirmation is replaced with a real alternative rather than
            # promising the customer a slot that doesn't exist.
            if book_when and s.booking_enabled:
                cust = session.customer if session else {}
                appt, problem = booking.try_book(
                    db, company, s, book_when,
                    phone=cust.get("phone", ""),
                    customer_name=cust.get("name", ""),
                    call_sid=CallSid,
                    campaign_id=session.campaign_id if session else None,
                    contact_id=session.contact_id if session else None,
                )
                if problem:
                    print(f"[{company.name}] booking refused: {problem}", flush=True)
                    reply = problem
                elif appt:
                    local = booking.to_local(appt.starts_at, company)
                    print(f"[{company.name}] booked {local:%Y-%m-%d %H:%M} "
                          f"for {appt.phone}", flush=True)
            # Resolve the voice AFTER the reply, against the language the agent
            # actually replied in. A Chirp3 identity therefore follows the
            # switch - same person, now speaking Tamil - while a fixed Polly
            # voice stays exactly as it was.
            voice = engine.voice_for(voice_name, language)
            print(f"[{company.name}] replied ({language}, {voice}): {reply!r}", flush=True)

            if wants_handoff and handoff_number:
                # Transfer the LIVE call to a real person rather than promising
                # a callback. caller_id must be the company's own Twilio number:
                # Twilio rejects a From it doesn't own, and the staff member
                # seeing their own business number is the sane caller ID anyway.
                print(f"[{company.name}] handing off {CallSid[:10]} -> {handoff_number}", flush=True)
                resp.say(reply, voice=voice)
                # If nobody picks up within 25s, fall through to the <Say>
                # after this Dial rather than dropping the customer silently.
                resp.dial(
                    handoff_number,
                    action=f"/hook/{token}/handoff-done",
                    caller_id=telephony.from_number(s, provider) or "",
                    timeout=25,
                )
                return _twiml(resp, provider)

            if any(p in reply.lower() for p in ("goodbye", "bye for now", "have a great day")):
                resp.say(reply, voice=voice)
                resp.hangup()
            else:
                # Next Gather listens in whatever language the AI just used -
                # that IS the "follow the customer" behaviour.
                resp.gather(
                    action=f"/hook/{token}/respond", language=language,
                    speech_timeout=s.speech_timeout, say_text=reply, voice=voice,
                )
            return _twiml(resp, provider)
        except Exception as exc:
            print(f"[ERROR respond] {exc}", flush=True)
            # Resolve from voice_name, not `voice` - if the failure happened
            # before the reply, `voice` was never assigned and referencing it
            # here would raise a second time, leaving the caller in silence.
            resp.say("Sorry, something went wrong. Goodbye.",
                     voice=engine.voice_for(voice_name, language))
            resp.hangup()
            return _twiml(resp, provider)


@app.post("/hook/{token}/handoff-done")
async def hook_handoff_done(request: Request, token: str):
    """Where the call lands after a <Dial> transfer finishes or fails.

    The carrier hits this whether the human answered and hung up, or never
    picked up at all. Without it, a failed transfer would drop the customer
    into silence - the "we'll get back to you" failure mode this feature
    exists to avoid.
    """
    with Session(db_engine) as db:
        company, s = _tenant(token, db)
        form = dict((await request.form()))
        raw_call_id = form.get("CallSid") or form.get("CallUUID") or ""
        session = engine.get_session_for(db, raw_call_id) if raw_call_id else None
        provider = session.provider if session else telephony.provider_of(s) if s else "twilio"
        wf = telephony.normalize_webhook(provider, form)
        CallSid = wf["call_id"]
        DialCallStatus = wf["dial_status"]

        resp = telephony.CallResponse()
        if not company or not await _verify_webhook(request, s, provider):
            resp.hangup()
            return _twiml(resp, provider)

        voice = s.voice
        if DialCallStatus == "completed":
            # The human took it and the conversation ended there.
            resp.hangup()
            return _twiml(resp, provider)

        # busy / no-answer / failed - say so plainly rather than leaving dead air.
        print(f"[{company.name}] handoff failed on {CallSid[:10]}: {DialCallStatus}", flush=True)
        resp.say(
            "Sorry, nobody is available right now. We'll call you back shortly. Goodbye.",
            voice=voice,
        )
        resp.hangup()
        return _twiml(resp, provider)


@app.post("/hook/{token}/status")
async def hook_status(request: Request, token: str):
    with Session(db_engine) as db:
        company, s = _tenant(token, db)
        form = dict((await request.form()))
        raw_call_id = form.get("CallSid") or form.get("CallUUID") or ""
        session = engine.get_session_for(db, raw_call_id) if raw_call_id else None
        provider = session.provider if session else telephony.provider_of(s) if s else "twilio"
        wf = telephony.normalize_webhook(provider, form)
        CallSid = wf["call_id"]
        CallStatus = wf["status"]
        CallDuration = wf["duration"]

        if CallStatus not in {"completed", "failed", "busy", "no-answer", "canceled"}:
            return {"ok": True}

        if not company:
            return {"ok": False}
        if not await _verify_webhook(request, s, provider):
            return JSONResponse({"ok": False, "detail": "bad signature"}, status_code=403)

        try:
            session = engine.get_session_for(db, CallSid)
            customer = session.customer if session else {}
            # Twilio's own CallDuration is authoritative. v1 timed calls with a
            # local dict that a restart wiped, so durations silently read zero.
            duration = int(CallDuration or "0")

            if CallStatus == "completed":
                analysis = await asyncio.to_thread(engine.analyze_call, s, session)
                outcome = analysis["outcome"]
            else:
                analysis = {"summary": "", "action_needed": "", "transcript": ""}
                outcome = CallStatus.upper().replace("-", "_")

            db.add(
                Call(
                    company_id=company.id,
                    campaign_id=session.campaign_id if session else None,
                    call_sid=CallSid,
                    customer_name=customer.get("name", "(unknown)"),
                    phone=customer.get("phone", ""),
                    outcome=outcome,
                    summary=analysis["summary"],
                    action_needed=analysis["action_needed"],
                    duration_sec=duration,
                    transcript=analysis["transcript"],
                )
            )
            db.commit()

            plans.record_minutes(company.id, duration, db)
            _settle_contact(db, session, outcome)
            print(f"[{company.name}] saved {CallSid[:10]}: {outcome} ({duration}s)", flush=True)

            # Notify the customer's own system, if they configured one. Runs in
            # a thread so a slow endpoint of theirs doesn't hold up our reply
            # to Twilio - and never raises, so it can't lose the call record we
            # just committed above.
            if s and s.webhook_out_url:
                await asyncio.to_thread(
                    engine.send_outcome_webhook,
                    s,
                    {
                        "event": "call.completed",
                        "call_sid": CallSid,
                        "campaign": (
                            db.get(Campaign, session.campaign_id).name
                            if session and session.campaign_id
                            and db.get(Campaign, session.campaign_id) else None
                        ),
                        "customer_name": customer.get("name", ""),
                        "phone": customer.get("phone", ""),
                        "outcome": outcome,
                        "score": engine.score_of(outcome),
                        "duration_sec": duration,
                        "summary": analysis["summary"],
                        "action_needed": analysis["action_needed"],
                        "transcript": analysis["transcript"],
                        "occurred_at": utcnow().isoformat() + "Z",
                    },
                )
        except Exception as exc:
            print(f"[STATUS ERROR] {exc}", flush=True)
        finally:
            engine.end_call(db, CallSid)
    return {"ok": True}


def _settle_contact(db: Session, session: CallSession | None, outcome: str) -> None:
    """Close out the contact this call belonged to, or queue a retry."""
    if not session or not session.contact_id:
        return
    contact = db.get(Contact, session.contact_id)
    if not contact:
        return
    campaign = db.get(Campaign, session.campaign_id) if session.campaign_id else None

    if outcome in engine.TERMINAL_OUTCOMES:
        contact.status = "dnc" if outcome == "DNC" else "completed"
        contact.next_attempt_at = None
        db.add(contact)
        db.commit()
        return

    if outcome in {"NO_ANSWER", "FAILED", "BUSY", "CANCELED"} and campaign:
        scheduler._schedule_retry(contact, campaign, f"Call outcome: {outcome}", db)
        return

    contact.status = "completed"
    db.add(contact)
    db.commit()


# ============================== ACCOUNT ==============================
@app.post("/api/change-password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
):
    """Self-service password change for a user who is already logged in.
    Requires the current password, so a hijacked open tab can't lock the owner out."""
    user = get_current_user(request)
    if not user:
        return _err("Not logged in", 401)
    if not await _csrf_ok(request):
        return _err("Invalid CSRF token", 403)
    if len(new_password) < 8:
        return _err("New password must be at least 8 characters.")
    if len(new_password.encode()) > 72:
        return _err("New password is too long (max 72 bytes).")

    with Session(db_engine) as db:
        fresh = db.get(User, user.id)
        if not fresh or not verify_password(current_password, fresh.password_hash):
            return _err("Current password is incorrect.")
        fresh.password_hash = hash_password(new_password)
        db.add(fresh)
        db.commit()
    return {"ok": True, "detail": "Password changed."}


@app.post("/delete-account")
async def delete_account(request: Request, password: str = Form(...)):
    """Permanently delete the company and ALL its data. Requires the account
    password again, so a stolen open browser tab can't destroy everything."""
    user = get_current_user(request)
    if not user:
        return _redirect("/login")
    if not await _csrf_ok(request):
        return _redirect("/settings?delete_error=csrf")

    with Session(db_engine) as db:
        fresh = db.get(User, user.id)
        if not fresh or not verify_password(password, fresh.password_hash):
            return _redirect("/settings?delete_error=1")
        if fresh.role != "owner":
            return _redirect("/settings?delete_error=owner")

        cid = fresh.company_id
        # Every company-scoped table, not just some of them. v1 deleted
        # Call/Campaign/CompanySettings/User and left Faq rows orphaned in the
        # database after the company they belonged to was gone.
        for model in (Call, CallSession, Contact, Campaign, Agent, Faq, Invite, Invoice,
                      CompanySettings, User):
            for row in db.exec(select(model).where(model.company_id == cid)).all():
                db.delete(row)
        company = db.get(Company, cid)
        if company:
            db.delete(company)
        db.commit()
        print(f"[ACCOUNT DELETED] company_id={cid} by {fresh.email}", flush=True)

    resp = _redirect("/signup")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ============================== PLATFORM ADMIN ==============================
# Only these emails can see the cross-tenant overview. Everyone else, including
# logged-in customers, gets a 404 so the page's existence is hidden.
PLATFORM_ADMINS = {
    e.strip().lower()
    for e in os.getenv("PLATFORM_ADMINS", "avanixyxx@gmail.com").split(",")
    if e.strip()
}


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    user = get_current_user(request)
    if not user or user.email not in PLATFORM_ADMINS:
        return HTMLResponse("Not found", status_code=404)

    with Session(db_engine) as db:
        companies = db.exec(select(Company).order_by(Company.created_at.desc())).all()
        rows = []
        for c in companies:
            users = db.exec(select(User).where(User.company_id == c.id)).all()
            calls = db.exec(select(Call).where(Call.company_id == c.id)).all()
            camps = db.exec(select(Campaign).where(Campaign.company_id == c.id)).all()
            s = db.exec(
                select(CompanySettings).where(CompanySettings.company_id == c.id)
            ).first()
            rows.append(
                {
                    "id": c.id,
                    "name": c.name,
                    "type": c.account_type,
                    "plan": c.plan,
                    "created": c.created_at.strftime("%Y-%m-%d %H:%M"),
                    "emails": ", ".join(u.email for u in users),
                    "campaigns": len(camps),
                    "calls": len(calls),
                    "minutes": round(sum(x.duration_sec for x in calls) / 60, 1),
                    "twilio": bool(s and telephony.is_connected(s)),
                }
            )
        totals = {
            "companies": len(rows),
            "individual": sum(1 for r in rows if r["type"] == "individual"),
            "enterprise": sum(1 for r in rows if r["type"] == "enterprise"),
            "calls": sum(r["calls"] for r in rows),
            "minutes": round(sum(r["minutes"] for r in rows), 1),
            "mrr": sum(plans.PLANS.get(r["plan"], {}).get("price_inr", 0) for r in rows),
        }
        return templates.TemplateResponse(
            "admin.html",
            {
                "request": request,
                "rows": rows,
                "totals": totals,
                "user": user,
                "csrf": csrf_token(request.cookies.get(SESSION_COOKIE)),
            },
        )


@app.get("/legal/{page}", response_class=HTMLResponse)
async def legal(request: Request, page: str):
    """Terms, Privacy, and Refund pages. Public - no login required, since a
    prospective customer needs to be able to read them before signing up."""
    templates_by_page = {
        "terms": "legal_terms.html",
        "privacy": "legal_privacy.html",
        "refund": "legal_refund.html",
    }
    template = templates_by_page.get(page)
    if not template:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(
        template,
        {
            "request": request,
            "page": page,
            "today": to_local(utcnow(), "Asia/Kolkata").strftime("%d %B %Y"),
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
