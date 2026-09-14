"""
Carrier webhook signatures, checked FOR REAL.

Reported: a live test call rang and was cut immediately. Every other test sets
SANCHARAI_SKIP_TWILIO_VALIDATION=1, so none of them ever ran signature checking
- and it was broken: the signature header was looked up as
"X-Twilio-Signature" while Starlette delivers header names lowercased, so every
genuine Twilio request looked forged and got <Hangup/>. This test leaves
validation ON and signs requests exactly the way Twilio does.
"""
import os
import sys
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

os.environ["SANCHARAI_SCHEDULER"] = "0"
os.environ["SANCHARAI_SECURE_COOKIES"] = "0"
os.environ["SANCHARAI_SKIP_TWILIO_VALIDATION"] = "0"  # the whole point: validation ON
os.environ.pop("DATABASE_URL", None)
_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_webhook_signature.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["SANCHARAI_DB"] = _DB
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402
from twilio.request_validator import RequestValidator  # noqa: E402
import app as application  # noqa: E402
import auth  # noqa: E402
import engine  # noqa: E402
import telephony  # noqa: E402
from models import CallSession, Company, CompanySettings, engine as db_eng, init_db  # noqa: E402
from security import encrypt  # noqa: E402

init_db()
fails = []
TOKEN = "twilio-auth-token-for-test"


def check(label, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(label)


user, _ = auth.signup("sig@test.com", "supersecret123", "Sig Co")
with Session(db_eng) as db:
    s = db.exec(select(CompanySettings).where(CompanySettings.company_id == user.company_id)).first()
    s.twilio_account_sid = "AC" + "0" * 32
    s.twilio_auth_token_enc = encrypt(TOKEN)
    s.twilio_phone_number = "+17406968280"
    db.add(s)
    db.commit()
    hook = db.get(Company, user.company_id).webhook_token

signer = RequestValidator(TOKEN)


def signed_post(c, path, params, signature=None):
    url = f"https://example.test/hook/{hook}/{path}"
    sig = signer.compute_signature(url, params) if signature is None else signature
    headers = {"X-Twilio-Signature": sig} if sig else {}
    return c.post(f"/hook/{hook}/{path}", data=params, headers=headers)


with TestClient(application.app) as c:
    engine.start_session(Session(db_eng), "CAsig1", user.company_id, {"phone": "+911"}, provider="twilio")
    params = {"CallSid": "CAsig1", "To": "+911", "AnsweredBy": "human"}

    r = signed_post(c, "voice", params)
    check("THE FIX: a genuinely Twilio-signed call is answered, not hung up",
          "<Gather" in r.text, r.text[:250])
    check("...with the greeting", "<Say" in r.text, r.text[:250])

    r = signed_post(c, "voice", params, signature="not-the-real-signature")
    check("a wrong signature is still hung up on",
          "<Hangup" in r.text and "<Gather" not in r.text, r.text[:250])
    r = signed_post(c, "voice", params, signature="")
    check("a missing signature is still hung up on",
          "<Hangup" in r.text and "<Gather" not in r.text, r.text[:250])

    tampered = dict(params, To="+919999999999")
    sig_for_original = signer.compute_signature(f"https://example.test/hook/{hook}/voice", params)
    r = signed_post(c, "voice", tampered, signature=sig_for_original)
    check("a signed request with altered fields is rejected", "<Gather" not in r.text, r.text[:250])

    # ---- the end-of-call status update, which saves the call record
    status = {"CallSid": "CAsig1", "CallStatus": "completed", "CallDuration": "12"}
    r = signed_post(c, "status", {"CallSid": "CAsig1", "CallStatus": "completed", "CallDuration": "12"},
                    signature="forged")
    check("a forged status callback is refused (403)", r.status_code == 403, str(r.status_code))
    with patch.object(engine, "analyze_call",
                      return_value={"outcome": "COMPLETED", "summary": "", "action_needed": "", "transcript": ""}):
        r = signed_post(c, "status", status)
    check("a genuinely signed status callback is accepted", r.status_code == 200 and r.json().get("ok"), r.text)
    with Session(db_eng) as db:
        left = db.exec(select(CallSession).where(CallSession.call_sid == "CAsig1")).first()
    check("...and closes out the live call session", left is None)

# ---- header case must not matter, for either carrier
st = CompanySettings(company_id=1, twilio_auth_token_enc=encrypt(TOKEN))
url = "https://example.test/hook/x/voice"
form = {"CallSid": "CA1"}
good = signer.compute_signature(url, form)
for name in ("X-Twilio-Signature", "x-twilio-signature", "X-TWILIO-SIGNATURE"):
    check(f"twilio signature found under header '{name}'",
          telephony.validate_signature(st, "twilio", url, form, {name: good}) is True)

pl = CompanySettings(company_id=1, plivo_auth_token_enc=encrypt("plivo-tok"))
with patch("telephony.plivo.utils.validate_v3_signature", return_value=True) as mk:
    ok = telephony.validate_signature(pl, "plivo", url, {}, {"x-plivo-signature-v3": "s",
                                                             "x-plivo-signature-v3-nonce": "n"})
check("plivo signature found under lowercase headers", ok is True and mk.call_count == 1)

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
try:
    os.remove(_DB)
except OSError:
    pass
