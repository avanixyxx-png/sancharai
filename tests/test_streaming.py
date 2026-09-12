"""
Streaming STT end to end, with Twilio and Deepgram both stubbed.

This is everything the proof-of-concept call will exercise, minus the two
things only a real call can prove (that Twilio actually dials the WebSocket,
and that a <Start><Stream> fork survives being redirected). What IS proved
here: the greeting TwiML forks audio instead of gathering, real Twilio media
frames are decoded and relayed to Deepgram byte-for-byte, a finished utterance
triggers exactly one live-call redirect carrying the AI's reply, the fork is
re-armed on the new document, and none of the failure modes - no key, dead
Deepgram, dead AI, dead redirect - drop the call.
"""
import asyncio
import base64
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import websockets

os.environ["SANCHARAI_SCHEDULER"] = "0"
os.environ["SANCHARAI_SECURE_COOKIES"] = "0"
os.environ["SANCHARAI_SKIP_TWILIO_VALIDATION"] = "1"
_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_streaming.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["SANCHARAI_DB"] = _DB
os.environ["PUBLIC_BASE_URL"] = "https://example.ngrok-free.app"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402
import app as application  # noqa: E402
import engine  # noqa: E402
import stt  # noqa: E402
import streaming  # noqa: E402
import telephony  # noqa: E402
from models import Company, CompanySettings, engine as db_eng, init_db  # noqa: E402

init_db()
fails = []


def check(label, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(label)


# --------------------------------------------------- unit: carrier frame shapes
twilio_media = {"event": "media", "media": {"track": "inbound", "chunk": "1",
                                            "payload": base64.b64encode(b"\x01\x02\x03").decode()}}
check("decodes a real Twilio media frame",
      streaming._frame_audio(twilio_media) == b"\x01\x02\x03",
      repr(streaming._frame_audio(twilio_media)))
check("a Twilio start frame carries no audio",
      streaming._frame_audio({"event": "start", "start": {"callSid": "CA1"}}) == b"")
check("corrupt base64 is dropped, not fatal",
      streaming._frame_audio({"media": {"payload": "!!!not base64!!!"}}) == b"")
check("finds the call id in a Twilio start frame",
      streaming._frame_call_id({"event": "start", "start": {"callSid": "CAabc"}}) == "CAabc")
check("finds a Plivo-shaped call id too",
      streaming._frame_call_id({"event": "start", "start": {"callUUID": "uuid-9"}}) == "uuid-9")
check("no id present is empty, not a crash",
      streaming._frame_call_id({"event": "media"}) == "")


# ------------------------------------------------------------- stub Deepgram
dg_state = {"audio": bytearray(), "connections": 0}


async def fake_deepgram(ws):
    dg_state["connections"] += 1

    async def collect():
        async for m in ws:
            if isinstance(m, bytes):
                dg_state["audio"].extend(m)

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.3)  # let the carrier's frames arrive
    # A finished utterance: this is the cue that must trigger exactly one reply.
    await ws.send(json.dumps({
        "type": "Results", "is_final": True, "speech_final": True,
        "channel": {"alternatives": [{"transcript": "mane kal appointment joiye"}],
                    "detected_language": "gu"},
    }))
    await asyncio.sleep(0.4)
    task.cancel()


# ------------------------------------------------------------ stub AI (Ollama)
class AI(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        p = json.dumps({"message": {"content": "Chokkas, kal sagvad chhe."}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(p)))
        self.end_headers()
        self.wfile.write(p)

    def log_message(self, *a):
        pass


ai = HTTPServer(("127.0.0.1", 0), AI)
AI_URL = f"http://127.0.0.1:{ai.server_address[1]}"
threading.Thread(target=ai.serve_forever, daemon=True).start()


# -------------------------------------------------------- setup a real tenant
with TestClient(application.app) as c:
    c.post("/signup", data={"email": "st@test.com", "password": "supersecret123",
                            "company_name": "Stream Co", "account_type": "enterprise"})
    csrf = re.search(r'name="csrf" content="([^"]+)"', c.get("/").text).group(1)
    c.post("/settings", data={
        "telephony_provider": "twilio",
        "twilio_account_sid": "AC" + "0" * 32, "twilio_auth_token": "tok",
        "twilio_phone_number": "+17406968280",
        "stt_provider": "deepgram", "deepgram_api_key": "dg-secret-key",
        "ai_provider": "ollama", "ollama_host": AI_URL,
        "voice": "Polly.Aditi", "speech_language": "gu-IN",
        "agent_name": "Aria", "company_display_name": "Stream Co",
        "company_timezone": "Asia/Kolkata", "csrf_token": csrf})

    page = c.get("/settings").text
    check("settings offers the Deepgram option", 'value="deepgram"' in page)
    check("Deepgram key never echoed back to the browser", "dg-secret-key" not in page)

    with Session(db_eng) as db:
        company = db.exec(select(Company)).first()
        s = db.exec(select(CompanySettings)).first()
        token = company.webhook_token
        check("stt_provider saved", s.stt_provider == "deepgram", s.stt_provider)
        check("deepgram key stored encrypted",
              s.deepgram_api_key_enc and s.deepgram_api_key_enc != "dg-secret-key")
        check("...and decrypts back correctly",
              engine.decrypt(s.deepgram_api_key_enc) == "dg-secret-key")

    # ------------------------------------------- the greeting TwiML forks audio
    engine.start_session(Session(db_eng), "CAstream1", company.id, {"name": "Ramesh"},
                         language="gu-IN", provider="twilio")
    r = c.post(f"/hook/{token}/voice", data={"CallSid": "CAstream1", "To": "+911",
                                             "AnsweredBy": "human"})
    check("greeting still spoken", "<Say" in r.text, r.text[:200])
    check("audio is forked with <Stream>", "<Stream" in r.text, r.text[:300])
    check("...as a one-way <Start> fork, so the carrier keeps speaking",
          "<Start>" in r.text and "<Connect>" not in r.text, r.text[:300])
    check("...listening to the caller's track",
          'track="inbound_track"' in r.text, r.text[:300])
    check("...over wss, not ws (Twilio refuses plaintext)",
          "wss://" in r.text and "ws://example" not in r.text, r.text[:300])
    check("NO <Gather> - that's the per-turn charge we're removing",
          "<Gather" not in r.text, r.text[:300])
    check("call held open with a pause so it doesn't hang up on the caller",
          "<Pause" in r.text, r.text[:300])

    # a company still on the carrier is completely unaffected
    c.post("/settings", data={
        "telephony_provider": "twilio", "stt_provider": "carrier",
        "twilio_phone_number": "+17406968280",
        "ai_provider": "ollama", "ollama_host": AI_URL,
        "voice": "Polly.Aditi", "speech_language": "gu-IN",
        "agent_name": "Aria", "company_display_name": "Stream Co",
        "company_timezone": "Asia/Kolkata", "csrf_token": csrf})
    engine.start_session(Session(db_eng), "CAstream2", company.id, {}, provider="twilio")
    r = c.post(f"/hook/{token}/voice", data={"CallSid": "CAstream2", "To": "+911",
                                             "AnsweredBy": "human"})
    check("carrier mode still uses <Gather>, unchanged",
          "<Gather" in r.text and "<Stream" not in r.text, r.text[:300])


# ------------------------------------- the live loop: carrier -> Deepgram -> AI
class FakeWS:
    """Stands in for the carrier's WebSocket: replays real Twilio frames."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.closed = False

    async def receive_text(self):
        if not self._frames:
            await asyncio.sleep(0.5)
            raise RuntimeError("carrier hung up")
        await asyncio.sleep(0.02)
        return json.dumps(self._frames.pop(0))


redirects = []


def fake_redirect(settings, provider, call_id, xml):
    redirects.append({"call_id": call_id, "xml": xml, "provider": provider})


async def main():
    server = await websockets.serve(fake_deepgram, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    stt.DEEPGRAM_WS = f"ws://127.0.0.1:{port}/v1/listen"

    with Session(db_eng) as db:
        company = db.exec(select(Company)).first()
        s = db.exec(select(CompanySettings)).first()
        s.stt_provider = "deepgram"
        s.ai_provider = "ollama"
        s.ollama_host = AI_URL
        db.add(s); db.commit(); db.refresh(s)
        comp, setts, tok = company, s, company.webhook_token

    frames = [
        {"event": "connected"},
        {"event": "start", "start": {"callSid": "CAlive123",
                                     "mediaFormat": {"encoding": "audio/x-mulaw",
                                                     "sampleRate": 8000}}},
        {"event": "media", "media": {"payload": base64.b64encode(b"\xaa" * 160).decode()}},
        {"event": "media", "media": {"payload": base64.b64encode(b"\xbb" * 160).decode()}},
    ]

    with patch.object(telephony, "redirect_call", fake_redirect):
        await asyncio.wait_for(
            streaming.run_call(FakeWS(frames), comp, setts, tok,
                               "https://example.ngrok-free.app"),
            timeout=12,
        )

    check("opened exactly one Deepgram connection for the call",
          dg_state["connections"] == 1, str(dg_state["connections"]))
    check("carrier audio relayed to Deepgram byte-for-byte",
          bytes(dg_state["audio"]) == b"\xaa" * 160 + b"\xbb" * 160,
          f"{len(dg_state['audio'])} bytes")

    check("a finished utterance triggers exactly one redirect",
          len(redirects) == 1, str(len(redirects)))
    if redirects:
        rd = redirects[0]
        check("redirect targets the call id from the start frame",
              rd["call_id"] == "CAlive123", rd["call_id"])
        check("the AI's reply is what gets spoken",
              "Chokkas" in rd["xml"], rd["xml"][:200])
        check("reply spoken in the configured voice",
              'voice="Polly.Aditi"' in rd["xml"], rd["xml"][:200])
        check("the fork is re-armed so the NEXT turn is still heard",
              "<Stream" in rd["xml"], rd["xml"][:250])
        check("...and the call is held open again",
              "<Pause" in rd["xml"], rd["xml"][:250])

    server.close()
    await server.wait_closed()

    # ------------------------------------------------------- failure modes
    setts.deepgram_api_key_enc = ""
    try:
        await asyncio.wait_for(
            streaming.run_call(FakeWS([]), comp, setts, tok, "https://x.test"), timeout=8)
        check("a missing Deepgram key ends the call cleanly, never crashes", True)
    except Exception as exc:  # noqa: BLE001
        check("a missing Deepgram key ends the call cleanly, never crashes",
              False, type(exc).__name__)

    from security import encrypt
    setts.deepgram_api_key_enc = encrypt("k")
    stt.DEEPGRAM_WS = "ws://127.0.0.1:1/v1/listen"  # nothing listening
    try:
        await asyncio.wait_for(
            streaming.run_call(FakeWS([]), comp, setts, tok, "https://x.test"), timeout=8)
        check("a Deepgram outage ends the call cleanly, never crashes", True)
    except Exception as exc:  # noqa: BLE001
        check("a Deepgram outage ends the call cleanly, never crashes",
              False, type(exc).__name__)

    # a redirect that fails must be logged, not raised into the call
    def boom(*a, **k):
        raise RuntimeError("Twilio 404")

    with patch.object(telephony, "redirect_call", boom):
        try:
            await streaming._reply(comp, setts, tok, "CAx", "hello", "https://x.test")
            check("a failed redirect is logged, not raised into the call", True)
        except Exception as exc:  # noqa: BLE001
            check("a failed redirect is logged, not raised into the call",
                  False, type(exc).__name__)

    # a dead AI must still say something rather than leave silence
    with patch.object(telephony, "redirect_call", fake_redirect), \
         patch.object(engine, "quick_reply", boom):
        redirects.clear()
        await streaming._reply(comp, setts, tok, "CAy", "hello", "https://x.test")
        check("a dead AI still speaks a fallback instead of dead air",
              len(redirects) == 1 and "say that again" in redirects[0]["xml"].lower(),
              str(redirects))


asyncio.run(main())
ai.shutdown()

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
try:
    os.remove(_DB)
except OSError:
    pass
