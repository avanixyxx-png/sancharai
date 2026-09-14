"""
Email-first signup: no account exists until the emailed link is clicked.

Reported: signing up with an address that isn't yours (tt@gmail.com) still
created an account. Now the form only validates and emails a link; the link
carries the pending signup (encrypted) and creating the account is the click.
Brevo is stubbed - what matters here is what the app does with the link.
"""
import os
import re
import sys
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

os.environ["SANCHARAI_SCHEDULER"] = "0"
os.environ["SANCHARAI_SECURE_COOKIES"] = "0"
os.environ["SANCHARAI_SKIP_TWILIO_VALIDATION"] = "1"
os.environ["SANCHARAI_SIGNUP_LIMIT"] = "3"
os.environ["BREVO_API_KEY"] = "test-key-not-real"
os.environ.pop("DATABASE_URL", None)
_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_signup_verify.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["SANCHARAI_DB"] = _DB
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402
import app as application  # noqa: E402
import auth  # noqa: E402
from models import Company, User, engine as db_eng, init_db  # noqa: E402
from security import read_signup_token  # noqa: E402

init_db()
fails = []
sent = []


def check(label, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(label)


def fake_send(to_email, link):
    sent.append({"to": to_email, "link": link})
    return True, "sent"


def users(email=None):
    with Session(db_eng) as db:
        q = select(User)
        if email:
            q = q.where(User.email == email)
        return db.exec(q).all()


def form(email, password="supersecret123", company="Acme", kind="enterprise"):
    return {"email": email, "password": password, "company_name": company, "account_type": kind}


def token_of(link):
    return link.rsplit("/signup/confirm/", 1)[-1]


with patch.object(application.mailer, "send_signup_confirmation", side_effect=fake_send):
    # ---------------------------------------------------- submit creates nothing
    with TestClient(application.app) as c:
        application._SIGNUP_ATTEMPTS.clear()
        r = c.post("/signup", data=form("tt@gmail.com"))
        check("form shows 'check your email'", "Check your email" in r.text, r.text[:300])
        check("THE FIX: no account exists before the link is clicked", users("tt@gmail.com") == [])
        check("no company created either", len(users()) == 0)
        check("exactly one confirmation email sent", len(sent) == 1, str(sent))
        check("...to the address that was typed", sent and sent[0]["to"] == "tt@gmail.com")
        check("not logged in after submitting",
              c.get("/settings", follow_redirects=False).status_code in (302, 303))

    link = sent[0]["link"]
    token = token_of(link)
    check("link points at the public domain", link.startswith("https://example.test/signup/confirm/"), link)
    check("password is not readable in the link", "supersecret123" not in link)
    check("email is not readable in the link", "tt@gmail.com" not in link and "gmail" not in link)
    pending = read_signup_token(token)
    check("link decrypts to the pending signup",
          pending and pending["email"] == "tt@gmail.com" and pending["company"] == "Acme", str(pending))
    check("only a password HASH travels, never the password",
          pending and pending["pw"].startswith("$2") and pending["pw"] != "supersecret123")
    check("link expires (24h max age honoured)", read_signup_token(token, max_age=-1) is None)

    # ------------------------------------------------------- forged / tampered
    with TestClient(application.app) as c:
        bad = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        r = c.get(f"/signup/confirm/{bad}")
        check("tampered link is rejected", "expired" in r.text.lower() or "invalid" in r.text.lower(), r.text[:300])
        r = c.get("/signup/confirm/not-a-real-token")
        check("garbage link is rejected", "invalid" in r.text.lower() or "expired" in r.text.lower())
        check("rejected links create no account", users("tt@gmail.com") == [])

    # --------------------------------------------------- clicking creates it
    with TestClient(application.app) as c:
        r = c.get(f"/signup/confirm/{token}", follow_redirects=False)
        check("confirming redirects into the app", r.status_code in (302, 303), str(r.status_code))
        check("...to settings", r.headers.get("location", "").endswith("/settings"), r.headers.get("location"))
        created = users("tt@gmail.com")
        check("clicking the link creates the account", len(created) == 1)
        check("...already verified (they proved the inbox)", created and created[0].is_verified is True)
        check("...as the workspace owner", created and created[0].role == "owner")
        with Session(db_eng) as db:
            company = db.get(Company, created[0].company_id) if created else None
        check("...with the company name from the form", company and company.name == "Acme")
        check("...and logs them straight in", c.get("/settings", follow_redirects=False).status_code == 200)
        check("the original password works for sign-in",
              auth.login("tt@gmail.com", "supersecret123")[0].email == "tt@gmail.com")

    with TestClient(application.app) as c:
        r = c.get(f"/signup/confirm/{token}", follow_redirects=False)
        check("clicking the link twice does not create a duplicate", len(users("tt@gmail.com")) == 1)
        check("...it says the account already exists", "already" in r.text.lower(), r.text[:300])
        check("...and does not log in from a reused link",
              c.get("/settings", follow_redirects=False).status_code in (302, 303))

    # ------------------------------------------------------ form-side refusals
    with TestClient(application.app) as c:
        application._SIGNUP_ATTEMPTS.clear()
        before = len(sent)
        r = c.post("/signup", data=form("tt@gmail.com"))
        check("an existing email is refused on the form", "already exists" in r.text, r.text[:300])
        check("...without sending another email", len(sent) == before)

        r = c.post("/signup", data=form("short@test.com", password="123"))
        check("a short password is refused on the form", "at least 8" in r.text, r.text[:300])
        check("...without sending an email", len(sent) == before)
        check("...and the typed email is kept in the form", 'value="short@test.com"' in r.text)

        r = c.post("/signup", data=form("solo@test.com", company="", kind="individual"))
        solo = read_signup_token(token_of(sent[-1]["link"]))
        check("individuals need no company - named after the mailbox",
              solo and solo["company"] == "Solo" and solo["type"] == "individual", str(solo))

    # -------------------------------------------------------- Brevo failure
    with TestClient(application.app) as c:
        application._SIGNUP_ATTEMPTS.clear()
        with patch.object(application.mailer, "send_signup_confirmation",
                          return_value=(False, "Brevo error 400: invalid email")):
            r = c.post("/signup", data=form("bounce@test.com"))
        # The template HTML-escapes the apostrophe in "couldn't", so match past it.
        check("a failed send shows an error instead of 'check your email'",
              "send the confirmation email" in r.text and "Check your email" not in r.text, r.text[:300])
        check("...and creates nothing", users("bounce@test.com") == [])

    # ------------------------------------------------------------- rate limit
    with TestClient(application.app) as c:
        application._SIGNUP_ATTEMPTS.clear()
        before = len(sent)
        for i in range(3):
            c.post("/signup", data=form(f"flood{i}@test.com"))
        r = c.post("/signup", data=form("flood9@test.com"))
        check("signups from one address are capped per hour", "Too many" in r.text, r.text[:300])
        check("...the capped attempt sends no email", len(sent) == before + 3, str(len(sent) - before))
        application._SIGNUP_ATTEMPTS.clear()
        r = c.post("/signup", data=form("wrong@test.com", password="123"))
        r = c.post("/signup", data=form("wrong@test.com", password="123"))
        r = c.post("/signup", data=form("wrong@test.com", password="123"))
        r = c.post("/signup", data=form("wrong@test.com", password="123"))
        check("typos don't count toward the cap (only sent emails do)", "Too many" not in r.text)

# ------------------------------------------------- Brevo not configured (dev)
os.environ.pop("BREVO_API_KEY", None)
with TestClient(application.app) as c:
    application._SIGNUP_ATTEMPTS.clear()
    r = c.post("/signup", data=form("local@test.com"))
    check("without Brevo the flow still works (link goes to the server log)", "Check your email" in r.text)
    check("...and still creates nothing up front", users("local@test.com") == [])

# ------------------------------------------------- direct helper for scripts
u, _ = auth.signup("script@test.com", "supersecret123", "Script Co")
check("auth.signup() still creates an account directly (tests/scripts)", u.id is not None)
check("...unverified, as before", u.is_verified is False)

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
try:
    os.remove(_DB)
except OSError:
    pass
