"""
Signup with a 6-digit email code: no account without proving the inbox.

Reported: signing up with an address that isn't yours (tt@gmail.com) still
created an account. Now the form has a "Send code" step beside the email;
the account can only be created with the verified token that step returns,
and it is created already verified. Brevo is stubbed.
"""
import base64
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
import security  # noqa: E402
from models import Company, User, engine as db_eng, init_db  # noqa: E402

init_db()
fails = []
sent = []


def check(label, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(label)


def fake_send(to_email, code):
    sent.append({"to": to_email, "code": code})
    return True, "sent"


def users(email=None):
    with Session(db_eng) as db:
        q = select(User)
        if email:
            q = q.where(User.email == email)
        return db.exec(q).all()


def reset_limits():
    application._SIGNUP_ATTEMPTS.clear()
    application._CODE_FAILURES.clear()


def get_code(c, email):
    r = c.post("/api/signup/send-code", data={"email": email})
    return r, (sent[-1]["code"] if r.json().get("ok") else None), r.json().get("ticket", "")


def verify(c, email, code, ticket):
    return c.post("/api/signup/verify-code", data={"email": email, "code": code, "ticket": ticket})


def form(email, token, password="supersecret123", company="Acme", kind="enterprise"):
    return {"email": email, "password": password, "company_name": company,
            "account_type": kind, "verified_token": token}


with patch.object(application.mailer, "send_signup_code", side_effect=fake_send):
    with TestClient(application.app) as c:
        reset_limits()
        page = c.get("/signup").text
        check("signup page has a Send code button beside the email", 'id="send-code"' in page)
        check("...a hidden verified-token field", 'name="verified_token"' in page)
        check("...and Create account starts disabled", 'id="create-btn" disabled' in page)

        # --------------------------------------------------------- send code
        r = c.post("/api/signup/send-code", data={"email": "not-an-email"})
        check("invalid email is refused", r.status_code == 400 and not r.json()["ok"], r.text)
        check("...without sending anything", sent == [])

        r, code, ticket = get_code(c, "tt@gmail.com")
        check("send-code succeeds", r.json().get("ok") is True, r.text)
        check("exactly one code email sent, to that address",
              len(sent) == 1 and sent[0]["to"] == "tt@gmail.com", str(sent))
        check("code is 6 digits", bool(code) and re.fullmatch(r"\d{6}", code), str(code))
        check("THE FIX: sending a code creates no account", users("tt@gmail.com") == [])
        payload = ticket.split(".")[0]
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode(errors="ignore")
        check("the code itself is not inside the ticket the browser holds", code not in decoded, decoded)

        # ---------------------------------------------- submit without proof
        r = c.post("/signup", data=form("tt@gmail.com", ""))
        check("creating an account without the code is refused",
              "6-digit code" in r.text and users("tt@gmail.com") == [], r.text[:200])
        r = c.post("/signup", data=form("tt@gmail.com", "forged.token.value"))
        check("a forged verified token is refused", users("tt@gmail.com") == [])

        # ------------------------------------------------------- wrong codes
        wrong = "000000" if code != "000000" else "111111"
        r = verify(c, "tt@gmail.com", wrong, ticket)
        check("a wrong code is rejected", not r.json()["ok"] and "Wrong code" in r.json()["detail"], r.text)
        check("...and says how many tries are left", "4 attempts left" in r.json()["detail"], r.text)
        r = verify(c, "other@gmail.com", code, ticket)
        check("a code can't be used for a different email", not r.json()["ok"], r.text)
        r = verify(c, "tt@gmail.com", code, "")
        check("verifying without a ticket is refused", not r.json()["ok"], r.text)
        check("an expired ticket is detected",
              security.check_signup_code(ticket, "tt@gmail.com", code, max_age=-1)[0] == "expired")

        # --------------------------------------------------------- right code
        r = verify(c, "tt@gmail.com", code, ticket)
        d = r.json()
        check("the right code verifies", d.get("ok") is True and d.get("verified_token"), r.text)
        token = d.get("verified_token", "")
        check("still no account after verifying (created only on submit)", users("tt@gmail.com") == [])

        r = c.post("/signup", data=form("someone@else.com", token))
        check("a verified token for one email can't create another", users("someone@else.com") == [])

        r = c.post("/signup", data=form("tt@gmail.com", token, password="123"))
        check("a short password is still refused", "at least 8" in r.text, r.text[:200])
        check("...keeping the verification so no new code is needed", f'value="{token}"' in r.text)
        check("...and creating nothing", users("tt@gmail.com") == [])

        r = c.post("/signup", data=form("TT@gmail.com ", token), follow_redirects=False)
        check("verified email creates the account (case/space-insensitive)",
              r.status_code in (302, 303), str(r.status_code))
        check("...redirecting into settings", r.headers.get("location", "").endswith("/settings"))
        created = users("tt@gmail.com")
        check("account exists", len(created) == 1)
        check("...ALREADY VERIFIED", created and created[0].is_verified is True)
        check("...as the owner", created and created[0].role == "owner")
        with Session(db_eng) as db:
            company = db.get(Company, created[0].company_id) if created else None
        check("...with the company name", company and company.name == "Acme")
        check("...and logged in", c.get("/settings", follow_redirects=False).status_code == 200)

    with TestClient(application.app) as c:
        r = c.post("/signup", data=form("tt@gmail.com", token), follow_redirects=False)
        check("reusing the verified token can't create a duplicate", len(users("tt@gmail.com")) == 1)
        check("...it says the account exists", "already exists" in r.text, r.text[:200])

        before = len(sent)
        r = c.post("/api/signup/send-code", data={"email": "tt@gmail.com"})
        check("send-code refuses an email that already has an account",
              r.status_code == 409 and len(sent) == before, r.text)

        # --------------------------------------------------- brute-force lock
        reset_limits()
        r, code2, ticket2 = get_code(c, "lock@test.com")
        bad = "000000" if code2 != "000000" else "111111"
        for _ in range(5):
            verify(c, "lock@test.com", bad, ticket2)
        r = verify(c, "lock@test.com", code2, ticket2)
        check("after 5 wrong tries even the right code is locked out",
              not r.json()["ok"] and "Too many" in r.json()["detail"], r.text)

        # ------------------------------------------------------- individuals
        reset_limits()
        _, code3, ticket3 = get_code(c, "solo@test.com")
        t3 = verify(c, "solo@test.com", code3, ticket3).json()["verified_token"]
        c.post("/signup", data=form("solo@test.com", t3, company="", kind="individual"))
        solo = users("solo@test.com")
        with Session(db_eng) as db:
            sc = db.get(Company, solo[0].company_id) if solo else None
        check("individuals need no company name", sc and sc.name == "Solo", str(sc and sc.name))

    # ------------------------------------------------------- Brevo failure
    with TestClient(application.app) as c:
        reset_limits()
        with patch.object(application.mailer, "send_signup_code",
                          return_value=(False, "Brevo error 400: invalid email")):
            r = c.post("/api/signup/send-code", data={"email": "bounce@test.com"})
        check("a failed send reports an error", r.status_code == 502 and not r.json()["ok"], r.text)
        check("...and isn't counted toward the hourly cap", application._SIGNUP_ATTEMPTS.get("testclient", []) == [])

    # ------------------------------------------------------------- rate limit
    with TestClient(application.app) as c:
        reset_limits()
        for i in range(3):
            c.post("/api/signup/send-code", data={"email": f"flood{i}@test.com"})
        r = c.post("/api/signup/send-code", data={"email": "flood9@test.com"})
        check("codes are capped per network per hour", r.status_code == 429, r.text)

# ------------------------------------------------- Brevo not configured (dev)
os.environ.pop("BREVO_API_KEY", None)
with TestClient(application.app) as c:
    reset_limits()
    r = c.post("/api/signup/send-code", data={"email": "local@test.com"})
    check("without Brevo, send-code still works (code goes to the server log)", r.json().get("ok") is True)

# ------------------------------------------ old emailed links keep working
old = security.make_signup_token("oldlink@test.com", security.hash_password("supersecret123"), "Old Co", "enterprise")
with TestClient(application.app) as c:
    r = c.get(f"/signup/confirm/{old}", follow_redirects=False)
    check("confirmation links already emailed before this change still work",
          r.status_code in (302, 303) and len(users("oldlink@test.com")) == 1, str(r.status_code))

# ------------------------------------------------- direct helper for scripts
u, _ = auth.signup("script@test.com", "supersecret123", "Script Co")
check("auth.signup() still creates an account directly (tests/scripts)", u.id is not None)

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
try:
    os.remove(_DB)
except OSError:
    pass
