"""
The whole product, start to finish, in one run.

Walks a new customer through every tab the way a real person would: sign up
with an email code, fill in Settings, make an agent, add knowledge, upload
contacts, run a campaign, place a call, hold a conversation, book an
appointment, see it in history, pay for a plan, invite a teammate, change
password, log out, log back in. Then checks a second company cannot see any
of it.

Twilio, Brevo and the AI are stubbed - this proves OUR code works end to end,
not that those services are up. Signature checking is skipped here because
test_webhook_signature.py covers it properly.
"""
import csv
import io
import json
import os
import re
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

os.environ["SANCHARAI_SCHEDULER"] = "0"
os.environ["SANCHARAI_SECURE_COOKIES"] = "0"
os.environ["SANCHARAI_SKIP_TWILIO_VALIDATION"] = "1"
os.environ["SANCHARAI_ALLOW_DEMO_UPGRADES"] = "1"
os.environ["SANCHARAI_SIGNUP_LIMIT"] = "50"
os.environ["PLATFORM_ADMINS"] = "owner@shop.test"
os.environ["BREVO_API_KEY"] = "test-key-not-real"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("RAZORPAY_KEY_ID", None)
os.environ.pop("RAZORPAY_KEY_SECRET", None)
_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_end_to_end.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["SANCHARAI_DB"] = _DB
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, select  # noqa: E402
import app as application  # noqa: E402
import engine as call_engine  # noqa: E402
import telephony  # noqa: E402
from models import (Agent, Appointment, Call, Campaign, Company,  # noqa: E402
                    Contact, Faq, Invoice, User, engine as db_eng, init_db)

# Importing app runs load_dotenv(), which puts the real .env back - including
# live Razorpay keys. With those set, an upgrade returns "ok, now go pay"
# instead of applying the plan, so clear them AFTER the import, not before.
os.environ["RAZORPAY_KEY_ID"] = ""
os.environ["RAZORPAY_KEY_SECRET"] = ""

init_db()
fails = []
sent_codes = []
placed_calls = []
step = [""]


def section(name):
    step[0] = name
    print(f"\n--- {name} ---")


def check(label, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(f"[{step[0]}] {label}")


# ----------------------------------------------------------------- the stubs
def fake_send_code(to_email, code):
    sent_codes.append({"to": to_email, "code": code})
    return True, "sent"


def fake_place_call(settings, webhook_base, to_number, max_seconds=None,
                    detect_voicemail=None, twiml_path="", provider=None):
    placed_calls.append({"to": to_number, "base": webhook_base, "path": twiml_path})
    return f"CAe2e{len(placed_calls):028d}", "twilio"


class AI(BaseHTTPRequestHandler):
    """Stands in for the customer's own AI provider."""
    reply = "Sure, I can help with that."

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        system = next((m["content"] for m in body.get("messages", []) if m.get("role") == "system"), "")
        if "Translate" in system:
            content = "નમસ્તે, હું અરિયા બોલું છું."
        elif "JSON" in system and "outcome" in system.lower():
            content = json.dumps({"outcome": "INTERESTED", "summary": "Wants a haircut booking",
                                  "action_needed": "Confirm slot"})
        else:
            content = AI.reply
        p = json.dumps({"message": {"content": content}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(p)))
        self.end_headers()
        self.wfile.write(p)

    def log_message(self, *a):
        pass


ai = HTTPServer(("127.0.0.1", 0), AI)
AI_URL = f"http://127.0.0.1:{ai.server_address[1]}"
threading.Thread(target=ai.serve_forever, daemon=True).start()


def settings_form(csrf, **over):
    """Settings posts EVERY field at once - omitting one blanks it."""
    base = {
        "telephony_provider": "twilio",
        "twilio_account_sid": "AC" + "0" * 32, "twilio_auth_token": "tok",
        "twilio_phone_number": "+17406968280",
        "stt_provider": "carrier",
        "ai_provider": "ollama", "ollama_host": AI_URL, "ollama_model": "llama3.2",
        "company_display_name": "Glow Salon", "agent_name": "Aria",
        "business_desc": "A hair salon in Rajkot.",
        "voice": "Aoede", "speech_language": "en-IN", "company_timezone": "Asia/Kolkata",
        "max_call_seconds": "180", "speech_timeout": "auto",
        "booking_enabled": "1", "booking_slot_minutes": "30",
        "booking_open": "10:00", "booking_close": "18:00", "booking_days_ahead": "30",
        "handoff_enabled": "1", "handoff_number": "+919876500000",
        "csrf_token": csrf,
    }
    base.update(over)
    return base


with patch.object(application.mailer, "send_signup_code", side_effect=fake_send_code), \
     patch.object(telephony, "place_call", fake_place_call):

    c = TestClient(application.app)
    with c:
        # ============================================================ SIGN UP
        section("1. Sign up with an email code")
        r = c.post("/api/signup/send-code", data={"email": "owner@shop.test"})
        check("code requested", r.json().get("ok") is True, r.text)
        ticket, code = r.json()["ticket"], sent_codes[-1]["code"]
        check("no account exists yet", not [u for u in Session(db_eng).exec(select(User)).all()])

        r = c.post("/api/signup/verify-code",
                   data={"email": "owner@shop.test", "code": code, "ticket": ticket})
        token = r.json().get("verified_token", "")
        check("code accepted", bool(token), r.text)

        r = c.post("/signup", data={"email": "owner@shop.test", "password": "supersecret123",
                                    "company_name": "Glow Salon", "account_type": "enterprise",
                                    "verified_token": token}, follow_redirects=False)
        check("account created and logged in", r.status_code in (302, 303), str(r.status_code))
        with Session(db_eng) as db:
            owner = db.exec(select(User).where(User.email == "owner@shop.test")).first()
            company_id = owner.company_id
            webhook_token = db.get(Company, company_id).webhook_token
        check("email already verified, so calls are allowed", owner.is_verified is True)
        check("a starter agent was created for them",
              len(Session(db_eng).exec(select(Agent).where(Agent.company_id == company_id)).all()) == 1)

        csrf = re.search(r'name="csrf" content="([^"]+)"', c.get("/").text).group(1)

        # ======================================================== EVERY TAB
        section("2. Every tab loads")
        for path, must_contain in [
            ("/", "Dashboard"), ("/agents", "Agent"), ("/campaigns", "Campaign"),
            ("/contacts", "Contact"), ("/history", "Call"), ("/appointments", "Appointment"),
            ("/faqs", "Knowledge"), ("/call", "Test Call"), ("/billing", "Plan"),
            ("/team", "Team"), ("/settings", "Settings"), ("/admin", "Platform Admin"),
        ]:
            r = c.get(path)
            check(f"{path} loads", r.status_code == 200 and must_contain.lower() in r.text.lower(),
                  f"{r.status_code}")
        for page in ("terms", "privacy", "refund"):
            check(f"/legal/{page} loads", c.get(f"/legal/{page}").status_code == 200)

        # ========================================================== SETTINGS
        section("3. Settings")
        r = c.post("/settings", data=settings_form(csrf), follow_redirects=False)
        check("settings saved", r.status_code in (302, 303), str(r.status_code))
        page = c.get("/settings").text
        check("Twilio auth token never shown back", "tok" not in page.split("masked")[0][-200:])
        check("booking switched on", 'name="booking_enabled"' in page and "checked" in page)

        with patch.object(telephony, "test_twilio", return_value=(True, "Connected")):
            r = c.post("/api/test-twilio", data={"csrf_token": csrf})
        check("Twilio test button works", r.json().get("ok") is True, r.text)
        r = c.post("/api/test-ai", data={"csrf_token": csrf, "ai_provider": "ollama",
                                         "ollama_host": AI_URL})
        check("AI test button works", r.json().get("ok") is True, r.text)

        # ============================================================ AGENTS
        section("4. Agents")
        # Free Trial allows only 1 agent (and 1 campaign), and signup already
        # created the starter agent - so the plan limit fires before anything
        # else can be tested. A real customer hits this the same way.
        r = c.post("/api/agent", data={"name": "Priya", "greeting": "Hi", "csrf_token": csrf})
        check("Free Trial refuses a 2nd agent", r.status_code == 402 and "limit" in r.text.lower(), r.text[:120])
        r = c.post("/api/billing/upgrade", data={"plan": "starter", "csrf_token": csrf})
        check("upgrading lifts the limit", r.json().get("ok") is True, r.text)
        r = c.post("/api/agent", data={"name": "Priya", "greeting": "Hi {name}, Priya here.",
                                       "system_prompt": "Be brief.", "voice": "Aoede",
                                       "speech_language": "gu-IN", "csrf_token": csrf})
        agent_id = r.json().get("id")
        check("agent created", bool(agent_id), r.text)
        check("agent appears on the page", "Priya" in c.get("/agents").text)
        check("agent can be paused",
              c.post(f"/api/agent/{agent_id}/toggle", data={"csrf_token": csrf}).json().get("ok"))
        check("agent can be resumed",
              c.post(f"/api/agent/{agent_id}/toggle", data={"csrf_token": csrf}).json().get("ok"))

        # ====================================================== KNOWLEDGE
        section("5. Knowledge base")
        r = c.post("/api/faq", data={"question": "What are your hours?",
                                     "answer": "10am to 8pm daily.", "csrf_token": csrf})
        check("FAQ saved", r.json().get("ok") is True, r.text)
        check("FAQ shows on the page", "What are your hours?" in c.get("/faqs").text)

        # ======================================================= CAMPAIGN
        section("6. Campaign and contacts")
        r = c.post("/api/campaign", data={
            "name": "Diwali Offers", "campaign_type": "Sales & Lead Gen",
            "script": "Offer 20% off haircuts.", "voice": "Aoede|Priya",
            "speech_language": "en-IN", "agent_id": str(agent_id), "csrf_token": csrf})
        camp_id = r.json().get("campaign_id")
        check("campaign created", bool(camp_id), r.text)

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["name", "phone"])
        w.writerow(["Ramesh", "+919876543210"])
        w.writerow(["Sita", "+919876543211"])
        r = c.post("/api/contacts/upload",
                   data={"campaign_id": str(camp_id), "csrf_token": csrf},
                   files={"file": ("contacts.csv", buf.getvalue(), "text/csv")})
        check("CSV uploaded", r.json().get("ok") is True, r.text)
        with Session(db_eng) as db:
            contacts = db.exec(select(Contact).where(Contact.campaign_id == camp_id)).all()
        check("both contacts imported", len(contacts) == 2, str(len(contacts)))
        check("contacts show on the page", "Ramesh" in c.get("/contacts").text)

        r = c.post(f"/api/campaign/{camp_id}/edit", data={
            "name": "Diwali Offers 2026", "script": "Offer 25% off.", "voice": "Aoede|Priya",
            "speech_language": "en-IN", "csrf_token": csrf})
        check("campaign edited", r.json().get("ok") is True, r.text)
        with Session(db_eng) as db:
            still = db.exec(select(Contact).where(Contact.campaign_id == camp_id)).all()
        check("editing keeps the uploaded contacts", len(still) == 2, str(len(still)))

        r = c.post(f"/api/campaign/{camp_id}/start", data={"csrf_token": csrf})
        check("campaign starts", r.json().get("ok") is True, r.text)
        r = c.post(f"/api/campaign/{camp_id}/pause", data={"csrf_token": csrf})
        check("campaign pauses", r.json().get("ok") is True, r.text)

        # ======================================================= TEST CALL
        section("7. Placing a call")
        r = c.post("/api/call/test", data={"phone": "+91 98765 43210",
                                           "agent_id": str(agent_id), "csrf_token": csrf})
        check("test call placed", r.json().get("ok") is True, r.text)
        check("number was cleaned up before dialling",
              placed_calls[-1]["to"] == "+919876543210", placed_calls[-1]["to"])
        call_sid = r.json().get("call_sid", "")

        # ================================================ THE CALL ITSELF
        section("8. The conversation")
        hook = f"/hook/{webhook_token}"
        r = c.post(f"{hook}/voice", data={"CallSid": call_sid, "To": "+919876543210",
                                          "AnsweredBy": "human"})
        check("agent answers with the greeting", "<Say" in r.text and "Priya" in r.text, r.text[:200])
        check("...and listens for a reply", "<Gather" in r.text, r.text[:200])
        check("...in the agent's own language", 'language="gu-IN"' in r.text, r.text[:250])

        AI.reply = "We are open 10am to 8pm daily."
        r = c.post(f"{hook}/respond", data={"CallSid": call_sid,
                                            "SpeechResult": "What time do you open?"})
        check("agent answers the question", "10am to 8pm" in r.text, r.text[:250])
        check("...and keeps listening", "<Gather" in r.text, r.text[:250])

        when = (datetime.now() + timedelta(days=2)).replace(hour=11, minute=0, second=0, microsecond=0)
        AI.reply = json.dumps({"language": "en-IN", "book": when.strftime("%Y-%m-%dT%H:%M"),
                               "reply": "Booked, see you then."})
        r = c.post(f"{hook}/respond", data={"CallSid": call_sid,
                                            "SpeechResult": "Book me for day after at 11"})
        with Session(db_eng) as db:
            appts = db.exec(select(Appointment).where(Appointment.company_id == company_id)).all()
        check("appointment booked during the call", len(appts) == 1, str(len(appts)))
        check("...and confirmed out loud", "<Say" in r.text or "Booked" in r.text, r.text[:250])

        AI.reply = json.dumps({"language": "en-IN", "book": when.strftime("%Y-%m-%dT%H:%M"),
                               "reply": "Booked."})
        c.post(f"{hook}/respond", data={"CallSid": call_sid, "SpeechResult": "Same slot again"})
        with Session(db_eng) as db:
            again = db.exec(select(Appointment).where(Appointment.company_id == company_id)).all()
        check("a clashing slot is refused, not double-booked", len(again) == 1, str(len(again)))

        AI.reply = "Goodbye, have a great day."
        r = c.post(f"{hook}/respond", data={"CallSid": call_sid, "SpeechResult": "That's all, bye"})
        check("agent says goodbye and hangs up", "<Hangup" in r.text, r.text[:250])

        r = c.post(f"{hook}/status", data={"CallSid": call_sid, "CallStatus": "completed",
                                           "CallDuration": "95"})
        check("call outcome recorded", r.json().get("ok") is True, r.text)

        # ========================================================== HISTORY
        section("9. History and appointments")
        with Session(db_eng) as db:
            saved = db.exec(select(Call).where(Call.company_id == company_id)).first()
        check("call saved to history", saved is not None)
        check("...with its length", saved and saved.duration_sec == 95, str(saved and saved.duration_sec))
        check("history page shows it", "95" in c.get("/history").text or saved.phone in c.get("/history").text)
        r = c.get("/history/download")
        check("history downloads as CSV", r.status_code == 200 and "text/csv" in r.headers.get("content-type", ""))
        check("appointment shows on its page",
              str(when.year) in c.get("/appointments").text or "11:00" in c.get("/appointments").text)
        r = c.post(f"/api/appointment/{appts[0].id}/cancel", data={"csrf_token": csrf})
        check("appointment can be cancelled", r.json().get("ok") is True, r.text)

        # ========================================================== BILLING
        section("10. Billing")
        page = c.get("/billing").text
        check("current plan shown", "Starter" in page)
        check("minutes used are counted", "1.6" in page or "minutes" in page.lower())
        r = c.post("/api/billing/upgrade", data={"plan": "starter", "csrf_token": csrf})
        check("upgrading to the plan you're already on is refused",
              r.json().get("ok") is not True, r.text)
        r = c.post("/api/billing/upgrade", data={"plan": "pro", "csrf_token": csrf})
        check("upgrading to a higher plan works", r.json().get("ok") is True, r.text)
        with Session(db_eng) as db:
            check("company moved to Pro",
                  db.get(Company, company_id).plan == "pro", db.get(Company, company_id).plan)
            check("invoices written for each upgrade",
                  len(db.exec(select(Invoice).where(Invoice.company_id == company_id)).all()) >= 2)

        # ============================================================= TEAM
        section("11. Team and account")
        with patch.object(application.mailer, "send_invite", return_value=(True, "sent")):
            r = c.post("/api/team/invite", data={"email": "staff@shop.test", "role": "member",
                                                 "csrf_token": csrf})
        check("teammate invited", r.json().get("ok") is True, r.text)
        check("invite shows on the team page", "staff@shop.test" in c.get("/team").text)

        r = c.post("/api/change-password", data={"current_password": "supersecret123",
                                                 "new_password": "newsecret45678",
                                                 "csrf_token": csrf})
        check("password changed", r.json().get("ok") is True, r.text)

        c.get("/logout")
        check("logged out", c.get("/settings", follow_redirects=False).status_code in (302, 303))
        r = c.post("/login", data={"email": "owner@shop.test", "password": "supersecret123"},
                   follow_redirects=False)
        check("old password no longer works", r.status_code == 200 and "Incorrect" in r.text)
        r = c.post("/login", data={"email": "owner@shop.test", "password": "newsecret45678"},
                   follow_redirects=False)
        check("new password works", r.status_code in (302, 303), str(r.status_code))

    # =================================================== ANOTHER CUSTOMER
    section("12. A second company is kept separate")
    other = TestClient(application.app)
    with other:
        r = other.post("/api/signup/send-code", data={"email": "rival@other.test"})
        tkt, cd = r.json()["ticket"], sent_codes[-1]["code"]
        vt = other.post("/api/signup/verify-code",
                        data={"email": "rival@other.test", "code": cd, "ticket": tkt}
                        ).json()["verified_token"]
        other.post("/signup", data={"email": "rival@other.test", "password": "supersecret123",
                                    "company_name": "Rival Salon", "account_type": "enterprise",
                                    "verified_token": vt})
        ocsrf = re.search(r'name="csrf" content="([^"]+)"', other.get("/").text).group(1)

        check("rival sees no contacts of ours", "Ramesh" not in other.get("/contacts").text)
        check("rival sees none of our campaigns", "Diwali" not in other.get("/campaigns").text)
        check("rival sees none of our knowledge base",
              "What are your hours?" not in other.get("/faqs").text)
        check("rival sees no calls of ours", "+919876543210" not in other.get("/history").text)
        with Session(db_eng) as db:
            rival_id = db.exec(select(User).where(User.email == "rival@other.test")).first().company_id
            mine = db.exec(select(Agent).where(Agent.company_id == company_id)).all()
            theirs = db.exec(select(Agent).where(Agent.company_id == rival_id)).all()
        check("our agents and theirs are separate",
              len(mine) == 2 and len(theirs) == 1, f"ours={len(mine)} theirs={len(theirs)}")
        r = other.post(f"/api/campaign/{camp_id}/delete", data={"csrf_token": ocsrf})
        check("rival cannot delete our campaign", r.status_code in (403, 404), str(r.status_code))
        with Session(db_eng) as db:
            check("...our campaign still exists", db.get(Campaign, camp_id) is not None)
        r = other.post(f"/api/agent/{agent_id}/delete", data={"csrf_token": ocsrf})
        check("rival cannot delete our agent", r.status_code in (403, 404), str(r.status_code))
        r = other.get("/admin")
        # 404, not 403: a non-admin isn't told the page exists at all.
        check("rival is refused the platform admin page", r.status_code == 404, str(r.status_code))
        check("...and sees none of its data", "Platform Admin" not in r.text)

ai.shutdown()
print("\n" + "=" * 60)
print("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
try:
    os.remove(_DB)
except OSError:
    pass
