"""
A silent line must hang up, not re-ask forever.

Every re-prompt is a fresh <Gather>, about Rs 1.75 on Twilio, plus the call
minutes ticking by. Before this fix nothing counted them: voicemail the
machine-detector missed, a phone in a pocket or a caller who walked away
would be re-asked until max_call_seconds, costing tens of rupees per dead
call. The agent now gives up after MAX_SILENT_TURNS and says goodbye.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

os.environ["SANCHARAI_SCHEDULER"] = "0"
os.environ["SANCHARAI_SECURE_COOKIES"] = "0"
os.environ["SANCHARAI_SKIP_TWILIO_VALIDATION"] = "1"
os.environ.pop("DATABASE_URL", None)
_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_silent_call.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["SANCHARAI_DB"] = _DB
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402
import app as application  # noqa: E402
import auth  # noqa: E402
import engine  # noqa: E402
from models import CallSession, Company, engine as db_eng, init_db  # noqa: E402

init_db()
fails = []


def check(label, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(label)


user, _ = auth.signup("quiet@test.com", "supersecret123", "Quiet Co")
with Session(db_eng) as db:
    hook = f"/hook/{db.get(Company, user.company_id).webhook_token}"


def silence(c, sid):
    return c.post(f"{hook}/respond", data={"CallSid": sid, "SpeechResult": ""})


with TestClient(application.app) as c:
    engine.start_session(Session(db_eng), "CAquiet", user.company_id, {"name": "X"},
                         provider="twilio")

    r = silence(c, "CAquiet")
    check("1st silence: asks them to repeat", "<Gather" in r.text and "catch that" in r.text, r.text[:160])
    r = silence(c, "CAquiet")
    check("2nd silence: asks once more", "<Gather" in r.text, r.text[:160])

    r = silence(c, "CAquiet")
    check("THE FIX: 3rd silence hangs up instead of asking again",
          "<Hangup" in r.text and "<Gather" not in r.text, r.text[:200])
    check("...with a polite goodbye, not dead air", "Goodbye" in r.text, r.text[:200])

    with Session(db_eng) as db:
        sess = db.exec(select(CallSession).where(CallSession.call_sid == "CAquiet")).first()
    check("silent turns were counted on the call", sess and sess.silent_turns == 3,
          str(sess and sess.silent_turns))

    # A caller who pauses, then speaks, must not be penalised later.
    engine.end_call(Session(db_eng), "CAquiet")
    engine.start_session(Session(db_eng), "CApause", user.company_id, {"name": "Y"},
                         provider="twilio")
    silence(c, "CApause")
    silence(c, "CApause")
    r = c.post(f"{hook}/respond", data={"CallSid": "CApause", "SpeechResult": "Sorry, I'm here"})
    check("speaking again keeps the call alive", "<Hangup" not in r.text, r.text[:160])
    with Session(db_eng) as db:
        sess = db.exec(select(CallSession).where(CallSession.call_sid == "CApause")).first()
    check("...and resets the silence count", sess and sess.silent_turns == 0,
          str(sess and sess.silent_turns))
    for _ in range(2):
        silence(c, "CApause")
    r = silence(c, "CApause")
    check("the limit still applies after the reset", "<Hangup" in r.text, r.text[:160])

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
try:
    os.remove(_DB)
except OSError:
    pass
