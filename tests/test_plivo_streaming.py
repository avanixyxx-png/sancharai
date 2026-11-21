"""
Streaming speech-to-text on Plivo, not just Twilio.

Deepgram streaming was Twilio-only: telephony.redirect_call refused Plivo
outright, because Plivo's Transfer API accepts a URL it will fetch and never
an inline document. The reply is now parked on the call's own row and served
by /hook/{token}/say/{call_id}.

Checked against Plivo's own docs (21 Nov 2026):
  - Transfer: POST /v1/Account/{auth_id}/Call/{call_uuid}/ with legs=aleg
    and aleg_url; SDK client.calls.transfer(...)
  - <Stream> carries the socket URL as element text, and its contentType
    DEFAULTS to linear PCM, so mu-law must be stated explicitly
  - the start frame names the call in start.callId, not start.callSid

Plivo and Deepgram are both stubbed; no account or network is used.
"""
import base64
import json
import os
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

os.environ["SANCHARAI_SCHEDULER"] = "0"
os.environ["SANCHARAI_SECURE_COOKIES"] = "0"
os.environ["SANCHARAI_SKIP_TWILIO_VALIDATION"] = "1"
os.environ.pop("DATABASE_URL", None)
_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_plivo_streaming.db")
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
import streaming  # noqa: E402
import telephony  # noqa: E402
from models import CallSession, Company, CompanySettings, engine as db_eng, init_db  # noqa: E402
from security import encrypt  # noqa: E402

init_db()
fails = []


def check(label, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(label)


user, _ = auth.signup("plivo@test.com", "supersecret123", "Plivo Co")
with Session(db_eng) as db:
    s = db.exec(select(CompanySettings).where(
        CompanySettings.company_id == user.company_id)).first()
    s.telephony_provider = "plivo"
    s.plivo_auth_id = "MAxxxxxxxxxxxxxxxxxx"
    s.plivo_auth_token_enc = encrypt("plivo-token")
    s.plivo_phone_number = "+917406968280"
    s.stt_provider = "deepgram"
    s.deepgram_api_key_enc = encrypt("dg-key")
    s.voice = "Polly.Aditi"
    db.add(s)
    db.commit()
    db.refresh(s)
    settings = s
    company = db.get(Company, user.company_id)
    token = company.webhook_token

# ------------------------------------------------- Plivo's own frame shapes
start_frame = {"event": "start", "sequenceNumber": 1,
               "start": {"callId": "plivo-uuid-77", "streamId": "st_1",
                         "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}}}
check("THE FIX: Plivo's start.callId is recognised",
      streaming._frame_call_id(start_frame) == "plivo-uuid-77",
      streaming._frame_call_id(start_frame))
media = {"event": "media", "streamId": "st_1",
         "media": {"track": "inbound", "payload": base64.b64encode(b"\x11\x22").decode()}}
check("Plivo media frames decode", streaming._frame_audio(media) == b"\x11\x22")

# -------------------------------------------------------------- the XML
body, ctype = telephony.CallResponse().say("Hello", "Polly.Aditi") \
    .stream("wss://example.test/hook/t/stream").pause(30).render("plivo")
check("stream URL is the element's text, as Plivo documents",
      ">wss://example.test/hook/t/stream</Stream>" in body, body)
check("mu-law stated explicitly (Plivo defaults to PCM otherwise)",
      'contentType="audio/x-mulaw;rate=8000"' in body, body)
check("listens to the caller's track", 'audioTrack="inbound"' in body, body)
check("one-way, so the rest of the document still runs",
      'bidirectional="false"' in body, body)

# --------------------------------------------------- transfer, not inline XML
fake = MagicMock()
with patch("telephony.plivo.RestClient", return_value=fake):
    telephony.redirect_call(settings, "plivo", "plivo-uuid-77", "<Response/>",
                            fetch_url="https://example.test/hook/t/say/plivo-uuid-77")
check("Plivo is transferred, not handed inline XML", fake.calls.transfer.call_count == 1,
      str(fake.calls.transfer.call_args))
kw = fake.calls.transfer.call_args.kwargs if fake.calls.transfer.call_args else {}
check("...on the caller's own leg", kw.get("legs") == "aleg", str(kw))
check("...pointed at our say URL",
      kw.get("aleg_url", "").endswith("/say/plivo-uuid-77"), str(kw))
check("...for the right call", kw.get("call_uuid") == "plivo-uuid-77", str(kw))

with patch("telephony.plivo.RestClient", return_value=MagicMock()):
    try:
        telephony.redirect_call(settings, "plivo", "plivo-uuid-77", "<Response/>")
        raised = False
    except telephony.TelephonyError:
        raised = True
check("a Plivo redirect without a URL is refused, not silently wrong", raised)

# ------------------------------------------------ parking and serving a reply
with TestClient(application.app) as c:
    engine.start_session(Session(db_eng), "plivo-uuid-77", company.id, {"name": "Ramesh"},
                         provider="plivo")

    r = c.post(f"/hook/{token}/say/plivo-uuid-77")
    check("nothing parked yet: Plivo gets a harmless wait, never an error",
          "<Wait" in r.text and "<Speak" not in r.text, r.text[:160])

    parked = ('<Response><Speak voice="Polly.Aditi">Chokkas, kal malish.</Speak>'
              '<Stream>wss://example.test/hook/t/stream</Stream><Wait length="30"/></Response>')
    check("reply parks onto the call row", streaming._park_reply("plivo-uuid-77", parked) is True)
    check("parking an unknown call fails loudly", streaming._park_reply("no-such-call", parked) is False)

    r = c.post(f"/hook/{token}/say/plivo-uuid-77")
    check("Plivo collects exactly what the AI said", "Chokkas, kal malish." in r.text, r.text[:200])
    check("...as XML", r.headers.get("content-type", "").startswith("application/xml"))
    check("...with the stream re-armed for the next turn", "<Stream" in r.text, r.text[:200])

    r = c.post(f"/hook/{token}/say/plivo-uuid-77")
    check("a repeated fetch does not say it twice",
          "Chokkas" not in r.text and "<Wait" in r.text, r.text[:160])

    # GET as well as POST, since Plivo's fetch method is configurable
    streaming._park_reply("plivo-uuid-77", parked)
    check("GET works too", "Chokkas" in c.get(f"/hook/{token}/say/plivo-uuid-77").text)

    # ------------------------------------------------- another company's call
    other, _ = auth.signup("rival@test.com", "supersecret123", "Rival Co")
    with Session(db_eng) as db:
        rival_token = db.get(Company, other.company_id).webhook_token
    streaming._park_reply("plivo-uuid-77", parked)
    r = c.post(f"/hook/{rival_token}/say/plivo-uuid-77")
    check("another workspace cannot collect our call's words",
          "Chokkas" not in r.text, r.text[:160])
    with Session(db_eng) as db:
        still = db.exec(select(CallSession).where(
            CallSession.call_sid == "plivo-uuid-77")).first()
    check("...and ours is still waiting for us", still and "Chokkas" in still.pending_reply)

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
try:
    os.remove(_DB)
except OSError:
    pass
