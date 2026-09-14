"""
Passwords, credential encryption, sessions, and CSRF.

Three different problems, three different tools:
  - Passwords are HASHED (one-way). Even we can never read them back.
  - Twilio/Groq keys are ENCRYPTED (two-way), because the real value has to be
    handed to Twilio at call time. They are never returned to the browser.
  - Sessions and one-time email links are SIGNED, so they can be handed to the
    client without letting the client forge one.

Key handling changed from v1. There, keys lived in secret.key next to the
database, and that file shipped inside the project folder - anyone with a copy
could decrypt every stored Twilio token and forge a login cookie for any user,
which undid the encryption entirely. Keys are now read from the environment
first, and the file fallback exists only so local development still works.
"""
from __future__ import annotations

import hmac
import os
import secrets
from hashlib import sha256
from pathlib import Path

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import (
    BadSignature,
    SignatureExpired,
    URLSafeSerializer,
    URLSafeTimedSerializer,
)

_KEY_FILE = Path(__file__).parent / "secret.key"

SESSION_COOKIE = "sancharai_session"
CSRF_FIELD = "csrf_token"

# Set SANCHARAI_SECURE_COOKIES=0 only for plain-HTTP local testing.
SECURE_COOKIES = os.getenv("SANCHARAI_SECURE_COOKIES", "1").strip() != "0"


def _load_or_create_keys() -> tuple[bytes, str]:
    """Environment first, file second.

    In production set FERNET_KEY and SESSION_SECRET as environment variables and
    never create secret.key at all. The file path stays for local runs, and is
    listed in .gitignore so it cannot be committed alongside the database.
    """
    env_fernet = os.getenv("FERNET_KEY", "").strip()
    env_session = os.getenv("SESSION_SECRET", "").strip()
    if env_fernet and env_session:
        return env_fernet.encode(), env_session

    if _KEY_FILE.exists():
        data = _KEY_FILE.read_text().strip().splitlines()
        if len(data) >= 2:
            print(
                "[security] using secret.key from disk. For production, move these "
                "into FERNET_KEY and SESSION_SECRET environment variables.",
                flush=True,
            )
            return data[0].encode(), data[1]

    fernet_key = Fernet.generate_key()
    session_secret = secrets.token_urlsafe(32)
    _KEY_FILE.write_text(f"{fernet_key.decode()}\n{session_secret}\n")
    try:  # best-effort lockdown on POSIX; no-op on Windows
        os.chmod(_KEY_FILE, 0o600)
    except OSError:
        pass
    print(
        "[security] generated a new secret.key. Keep it out of version control - "
        "losing it makes every stored credential undecryptable.",
        flush=True,
    )
    return fernet_key, session_secret


_FERNET_KEY, _SESSION_SECRET = _load_or_create_keys()
_fernet = Fernet(_FERNET_KEY)
_serializer = URLSafeSerializer(_SESSION_SECRET, salt="sancharai-session")


# ------------------------------------------------------------------ passwords
def hash_password(plain: str) -> str:
    # bcrypt refuses secrets over 72 bytes; reject clearly rather than truncate,
    # which would silently make a long password weaker than the user expects.
    if len(plain.encode()) > 72:
        raise ValueError("Password must be 72 bytes or fewer.")
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


# --------------------------------------------- reversible secrets (API keys)
def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode() if value else ""


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet.decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


def mask(value: str) -> str:
    """What the UI shows instead of a real secret."""
    if not value:
        return ""
    return f"{'•' * 8}{value[-4:]}" if len(value) > 4 else "•" * 8


# ------------------------------------------------------------------ sessions
def make_session(user_id: int, company_id: int) -> str:
    return _serializer.dumps({"uid": user_id, "cid": company_id})


def read_session(cookie: str | None) -> dict | None:
    if not cookie:
        return None
    try:
        return _serializer.loads(cookie)
    except BadSignature:
        return None


def set_session_cookie(response, cookie_value: str) -> None:
    """One place that decides cookie flags, so no route can forget them.

    v1 set neither `secure` nor an explicit lifetime, so the session cookie
    would travel over plain HTTP.
    """
    response.set_cookie(
        SESSION_COOKIE,
        cookie_value,
        httponly=True,
        samesite="lax",
        secure=SECURE_COOKIES,
        max_age=60 * 60 * 24 * 14,
        path="/",
    )


# ---------------------------------------------------------------------- CSRF
def csrf_token(session_cookie: str | None) -> str:
    """A token bound to the current session, verifiable without server state.

    v1 had no CSRF protection at all: every mutating route was a cookie-authed
    POST, so any other site could submit a form that deleted an account.
    """
    if not session_cookie:
        return ""
    return hmac.new(
        _SESSION_SECRET.encode(), f"csrf:{session_cookie}".encode(), sha256
    ).hexdigest()


def check_csrf(session_cookie: str | None, submitted: str | None) -> bool:
    expected = csrf_token(session_cookie)
    if not expected or not submitted:
        return False
    return hmac.compare_digest(expected, submitted.strip())


# ---------------------------------------- one-time email links (verify/reset)
# Separate salts so a verification link can never be replayed as a reset link.
_verify_signer = URLSafeTimedSerializer(_SESSION_SECRET, salt="sancharai-verify")
_reset_signer = URLSafeTimedSerializer(_SESSION_SECRET, salt="sancharai-reset")
_invite_signer = URLSafeTimedSerializer(_SESSION_SECRET, salt="sancharai-invite")


def make_verify_token(user_id: int) -> str:
    return _verify_signer.dumps({"uid": user_id})


def read_verify_token(token: str, max_age: int = 86_400) -> int | None:
    """Returns the user id, or None if the link is forged or expired (24h)."""
    try:
        return _verify_signer.loads(token, max_age=max_age).get("uid")
    except (BadSignature, SignatureExpired):
        return None


def make_reset_token(user_id: int, password_hash: str) -> str:
    """The current password hash is baked in, so the link stops working the
    moment the password changes - a reset link can only ever be used once."""
    return _reset_signer.dumps({"uid": user_id, "pw": password_hash[-16:]})


def read_reset_token(token: str, max_age: int = 3_600) -> dict | None:
    """Returns {'uid', 'pw'} or None if forged/expired (1 hour)."""
    try:
        return _reset_signer.loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def make_invite_token(invite_id: int) -> str:
    return _invite_signer.dumps({"iid": invite_id})


def read_invite_token(token: str, max_age: int = 7 * 86_400) -> int | None:
    """Team invites expire after 7 days."""
    try:
        return _invite_signer.loads(token, max_age=max_age).get("iid")
    except (BadSignature, SignatureExpired):
        return None


# ------------------------------------------------ email-first signup links
# Signup creates nothing until the emailed link is clicked, so the pending
# account has to travel inside the link itself. It is ENCRYPTED, not just
# signed: a signed-only token is plain base64 anyone can read, and this one
# carries the email and the password hash.
_signup_signer = URLSafeTimedSerializer(_SESSION_SECRET, salt="sancharai-signup")


def make_signup_token(email: str, password_hash: str, company_name: str, account_type: str) -> str:
    import json

    payload = json.dumps(
        {"email": email, "pw": password_hash, "company": company_name, "type": account_type}
    )
    return _signup_signer.dumps({"d": encrypt(payload)})


def read_signup_token(token: str, max_age: int = 86_400) -> dict | None:
    """Returns the pending signup, or None if forged, tampered, or expired (24h)."""
    import json

    try:
        data = _signup_signer.loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    raw = decrypt(data.get("d", "")) if isinstance(data, dict) else ""
    if not raw:
        return None
    try:
        pending = json.loads(raw)
    except ValueError:
        return None
    if not all(pending.get(k) for k in ("email", "pw", "company", "type")):
        return None
    return pending


# -------------------------------------------- signup email codes (6-digit OTP)
# The browser holds a signed "ticket" for the code it asked for. The ticket
# carries only an HMAC of the code - keyed with the server secret - so the code
# can neither be read out of it nor brute-forced offline. Online guessing is
# capped per ticket in app.py. No database table is needed.
_code_signer = URLSafeTimedSerializer(_SESSION_SECRET, salt="sancharai-signup-code")
_verified_email_signer = URLSafeTimedSerializer(_SESSION_SECRET, salt="sancharai-email-verified")
SIGNUP_CODE_TTL = 600  # 10 minutes


def _code_mac(nonce: str, email: str, code: str) -> str:
    return hmac.new(
        _SESSION_SECRET.encode(), f"signup-code:{nonce}:{email}:{code}".encode(), sha256
    ).hexdigest()


def make_signup_code(email: str) -> tuple[str, str]:
    """Returns (code, ticket). The code goes in the email; the ticket to the browser."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    nonce = secrets.token_urlsafe(12)
    ticket = _code_signer.dumps({"e": email, "n": nonce, "m": _code_mac(nonce, email, code)})
    return code, ticket


def check_signup_code(
    ticket: str, email: str, code: str, max_age: int = SIGNUP_CODE_TTL
) -> tuple[str, str]:
    """Returns (status, nonce). status is "ok", "wrong", "expired" or "invalid".

    nonce identifies the ticket, so the caller can count wrong guesses per code.
    """
    try:
        data = _code_signer.loads(ticket or "", max_age=max_age)
    except SignatureExpired:
        return "expired", ""
    except BadSignature:
        return "invalid", ""
    if not isinstance(data, dict) or data.get("e") != email:
        return "invalid", ""
    nonce = str(data.get("n", ""))
    code = (code or "").strip()
    if not (len(code) == 6 and code.isdigit()):
        return "wrong", nonce
    if hmac.compare_digest(_code_mac(nonce, email, code), str(data.get("m", ""))):
        return "ok", nonce
    return "wrong", nonce


def make_email_verified_token(email: str) -> str:
    """Proof, handed to the signup form, that this email passed the code step."""
    return _verified_email_signer.dumps({"e": email})


def read_email_verified_token(token: str, max_age: int = 1800) -> str | None:
    """The verified email, or None if forged or older than 30 minutes."""
    try:
        data = _verified_email_signer.loads(token or "", max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("e") if isinstance(data, dict) else None
