"""
Database models for SancharAI.

Design rules carried over from v1 and tightened:

  1. Every table holding customer data carries a company_id, and no query is
     ever made without scoping to the logged-in user's company. That isolation
     is what makes it safe for two businesses to share one server.

  2. All timestamps are stored as NAIVE UTC via utcnow(). v1 mixed
     datetime.utcnow() (storage) with datetime.now() (call-window checks),
     which silently shifted the daily call cap by the UTC offset. Anything
     user-facing converts to the company's own timezone at the edges only.

  3. Live call state lives in CallSession (a real table), not in module-level
     dicts. v1 kept conversations in RAM, which meant a restart mid-call lost
     the customer's name and history, and more than one worker was impossible.
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlmodel import Field, Session, SQLModel, create_engine

# Overridable so tests (and a second local instance) never share the running
# server's database file. On Windows a live server holds sancharai.db open, so
# a test that tries to reset it silently keeps the old rows instead.
DB_PATH = os.getenv("SANCHARAI_DB", "sancharai.db")
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)


def utcnow() -> datetime:
    """Naive UTC. One clock for the whole app - see rule 2 above."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(dt: datetime, tz_name: str) -> datetime:
    """Render a stored UTC timestamp in a company's own timezone."""
    if dt is None:
        return None
    try:
        tz = ZoneInfo(tz_name or "Asia/Kolkata")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    return dt.replace(tzinfo=timezone.utc).astimezone(tz)


def _new_token() -> str:
    return secrets.token_urlsafe(24)


class Company(SQLModel, table=True):
    """A tenant. One customer = one row - a registered business ("enterprise")
    or a solo user ("individual")."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    account_type: str = Field(default="enterprise")  # individual | enterprise

    # Twilio's webhooks arrive with no login, so the company is identified by
    # this unguessable token in the callback URL. A plain company id here would
    # let anyone probe another tenant's greeting.
    webhook_token: str = Field(default_factory=_new_token, index=True, unique=True)

    # Billing period. Minutes reset when the period rolls over.
    plan: str = Field(default="free")
    minutes_used: float = Field(default=0.0)
    period_start: datetime = Field(default_factory=utcnow)
    period_end: Optional[datetime] = Field(default=None)

    # Every call window and every "today" boundary is evaluated in this zone.
    timezone: str = Field(default="Asia/Kolkata")

    created_at: datetime = Field(default_factory=utcnow)


class User(SQLModel, table=True):
    """A login. Belongs to exactly one Company."""

    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    email: str = Field(index=True, unique=True)
    password_hash: str
    role: str = Field(default="owner")  # owner | member

    # Unverified users can sign in and configure everything, but cannot place
    # calls - a softer gate than blocking login, which strands people whose
    # verification mail was delayed or filtered.
    is_verified: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow)


class Invite(SQLModel, table=True):
    """A pending team invitation. Ported from v2."""

    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    email: str = Field(index=True)
    role: str = Field(default="member")
    token: str = Field(default_factory=_new_token, index=True, unique=True)
    accepted: bool = Field(default=False)
    invited_by: str = Field(default="")
    created_at: datetime = Field(default_factory=utcnow)


class CompanySettings(SQLModel, table=True):
    """Per-company config.

    Credentials are stored ENCRYPTED (Fernet) and never sent back to the
    browser - the UI only ever shows a mask, never the value. v2 stored the
    Twilio auth token in plaintext; that regression is not carried over.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id", index=True, unique=True)

    # Each company connects their OWN telephony account and pays their own
    # costs. Twilio is a premium international carrier (~Rs 4.3/min to an
    # Indian mobile); Plivo is an India-market carrier on the same route for
    # roughly Rs 0.60/min. The choice is per company, not platform-wide,
    # because the gap dwarfs SancharAI's own fee at any real call volume.
    telephony_provider: str = Field(default="twilio")  # twilio | plivo

    twilio_account_sid: str = Field(default="")
    twilio_auth_token_enc: str = Field(default="")
    twilio_phone_number: str = Field(default="")

    plivo_auth_id: str = Field(default="")
    plivo_auth_token_enc: str = Field(default="")
    plivo_phone_number: str = Field(default="")

    # AI - per-company key, encrypted at rest.
    groq_api_key_enc: str = Field(default="")
    groq_model: str = Field(default="llama-3.1-8b-instant")

    # Provider choice, ported from VoiceIQ. "ollama" points at a host the
    # COMPANY runs, not ours - that keeps our compute cost at zero and lets
    # privacy-sensitive customers keep call audio inside their own network.
    ai_provider: str = Field(default="groq")  # groq | ollama | openai | nvidia
    ollama_host: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="llama3.2")
    openai_api_key_enc: str = Field(default="")
    openai_model: str = Field(default="gpt-4o-mini")
    nvidia_api_key_enc: str = Field(default="")
    nvidia_model: str = Field(default="nvidia/nvidia-nemotron-nano-9b-v2")

    # Speech-to-text source. "carrier" is Twilio's <Gather>/Plivo's <GetInput>:
    # zero setup, but billed per turn (~Rs 1.75 each), which at three turns a
    # minute costs more than the call itself. "deepgram" streams the raw call
    # audio to Deepgram instead at ~Rs 0.51/min flat - roughly a 63% cut in the
    # customer's total call bill, and one less thing for them to configure.
    # Per-company, so a customer can stay on the carrier if they'd rather.
    stt_provider: str = Field(default="carrier")  # carrier | deepgram
    deepgram_api_key_enc: str = Field(default="")

    # Default agent persona (a Campaign or Agent can override any of these).
    company_display_name: str = Field(default="")
    agent_name: str = Field(default="Aria")
    business_desc: str = Field(default="")
    voice: str = Field(default="Polly.Aditi")
    speech_language: str = Field(default="en-IN")

    # Cost controls, ported from VoiceIQ. It defined these but never wired them
    # to its settings form; v1 hardcoded them. A support line and a survey line
    # genuinely need different caps, so they belong per company.
    detect_voicemail: bool = Field(default=True)
    max_call_seconds: int = Field(default=180)

    # How long Twilio waits after the customer stops speaking before sending us
    # the transcript. "auto" uses Twilio's end-of-speech detection and replies
    # in well under a second; a number is a fixed wait in seconds, which is
    # slower but more forgiving of someone who pauses mid-sentence.
    #
    # This dominates perceived latency: at the old fixed "3", three of the
    # roughly four seconds before the agent spoke were just Twilio waiting.
    speech_timeout: str = Field(default="auto")

    # Default for calls with no campaign (Test Call). Off by default: forcing
    # the model to reason about language on every turn is a real accuracy
    # tradeoff, since Twilio's speech recognizer works best when its language
    # hint matches what is actually spoken - see engine.py.
    auto_language_switch: bool = Field(default=False)

    # Open the call by ASKING which language the customer wants, instead of
    # assuming speech_language. Removes the guess: without it, the first
    # sentence is transcribed with the wrong language hint and usually comes
    # back garbled.
    ask_language: bool = Field(default=False)

    # Appointment booking. When on, the agent can book a slot during the call
    # and is told about clashes before it confirms anything.
    booking_enabled: bool = Field(default=False)
    booking_slot_minutes: int = Field(default=30)
    booking_open: str = Field(default="10:00")   # earliest bookable time, company tz
    booking_close: str = Field(default="18:00")  # latest START time
    booking_days_ahead: int = Field(default=30)  # how far out a booking may be made

    # Google Calendar. The refresh token is long-lived - leaking one is as
    # serious as leaking a password - so it is encrypted like every other
    # credential and never returned to the browser.
    google_refresh_token_enc: str = Field(default="")
    google_calendar_id: str = Field(default="primary")

    # Outbound webhook: POST each call's outcome to the customer's own CRM /
    # Zapier / Make endpoint. The secret signs each payload (HMAC-SHA256) so
    # their receiver can verify it really came from us - shown in the UI in
    # full, like a Stripe webhook signing secret, since the account owner
    # needs to copy it into their own system rather than us acting on it.
    webhook_out_url: str = Field(default="")
    webhook_out_secret: str = Field(default="")

    # Let the agent transfer a live call to a real person - see engine.py's
    # handoff marker and app.py's hook_respond. Off by default: a phone
    # number must actually be staffed for this to make sense.
    handoff_enabled: bool = Field(default=False)
    handoff_number: str = Field(default="")


class Agent(SQLModel, table=True):
    """A reusable AI persona. v1 baked the persona into each campaign, so two
    campaigns could not share one agent; v2 split it out and that is the better
    model. A Campaign may point at an Agent, or carry its own inline persona."""

    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id", index=True)

    name: str
    greeting: str = Field(default="Hi {name}, how can I help you today?")
    system_prompt: str = Field(default="")
    voice: str = Field(default="Polly.Aditi")
    speech_language: str = Field(default="en-IN")
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utcnow)


class Faq(SQLModel, table=True):
    """A company's own Q&A, injected into every call's system prompt so the
    agent answers consistently without repeating it in each campaign script."""

    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    question: str
    answer: str
    created_at: datetime = Field(default_factory=utcnow)


class Campaign(SQLModel, table=True):
    """A calling campaign. Contacts are queued against it and dialed by the
    background scheduler - v1 dialed inline in the request handler, which froze
    the whole server for the length of the list."""

    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    agent_id: Optional[int] = Field(default=None, foreign_key="agent.id", index=True)

    name: str
    campaign_type: str = Field(default="Sales & Lead Gen")
    greeting: str = Field(default="")
    knowledge: str = Field(default="")
    # The goal as the user typed it. greeting/knowledge are GENERATED from this,
    # so keeping the original is what lets Edit show back what was written
    # rather than the assembled prompt.
    script: str = Field(default="")
    voice: str = Field(default="Polly.Aditi")
    agent_name: str = Field(default="Aria")
    speech_language: str = Field(default="en-IN")

    # draft -> running -> paused | completed
    status: str = Field(default="draft", index=True)
    pause_reason: str = Field(default="")

    call_window_start: str = Field(default="09:00")
    call_window_end: str = Field(default="20:00")
    max_calls_per_day: int = Field(default=500)

    # "No retry" | "After 2 hours" | "After 4 hours" | "Next day"
    # v1 stored this field and never read it. The scheduler now honours it.
    retry_policy: str = Field(default="No retry")
    max_retries: int = Field(default=3)

    # Let the agent follow the customer to a different language mid-call
    # instead of staying fixed on speech_language. Opt-in per campaign - see
    # CompanySettings.auto_language_switch for why this defaults off.
    auto_language_switch: bool = Field(default=False)

    # Ask the customer which language they want before starting. See
    # CompanySettings.ask_language.
    ask_language: bool = Field(default=False)

    # Transfer to a real person mid-call when the agent can't handle
    # something. Falls back to CompanySettings' values when unset, same
    # pattern as speech_language falling back for Test Call.
    handoff_enabled: bool = Field(default=False)
    handoff_number: str = Field(default="")

    last_run_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)


class Contact(SQLModel, table=True):
    """One person to call. Ported from v2, which had a proper contact queue;
    v1 only ever had an in-request list that vanished after the response."""

    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    campaign_id: Optional[int] = Field(default=None, foreign_key="campaign.id", index=True)

    name: str = Field(default="")
    phone: str = Field(default="", index=True)
    extra_json: str = Field(default="{}")  # any other CSV column -> {placeholder}

    # pending -> dialed -> completed | failed | dnc
    status: str = Field(default="pending", index=True)
    retry_count: int = Field(default=0)
    next_attempt_at: Optional[datetime] = Field(default=None, index=True)
    last_error: str = Field(default="")
    call_sid: str = Field(default="", index=True)
    dialed_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def extra(self) -> dict:
        try:
            return json.loads(self.extra_json or "{}")
        except (ValueError, TypeError):
            return {}

    def as_customer(self) -> dict:
        """The dict the engine uses for {placeholder} substitution."""
        data = {"phone": self.phone}
        if self.name:
            data["name"] = self.name
        data.update(self.extra)
        return data


class CallSession(SQLModel, table=True):
    """Live state for a call that is currently in progress.

    This is the fix for v1's biggest structural flaw: conversation history,
    the customer record, and the campaign link all lived in module-level dicts
    keyed by CallSid. A restart mid-call lost them, and a second worker never
    saw them at all. Rows are deleted by end_call() once the call completes.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    call_sid: str = Field(index=True, unique=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    campaign_id: Optional[int] = Field(default=None)
    contact_id: Optional[int] = Field(default=None)
    # Which reusable Agent persona is speaking. Set when a test call picks one
    # directly; campaigns resolve theirs from campaign.agent_id instead.
    agent_id: Optional[int] = Field(default=None)

    # Which carrier this SPECIFIC call was placed through. Stamped once at
    # start_session() and never re-read from settings, so a company switching
    # provider mid-campaign does not reinterpret an in-flight call's webhook
    # replies in the wrong XML dialect.
    provider: str = Field(default="twilio")

    customer_json: str = Field(default="{}")
    history_json: str = Field(default="[]")  # [{"role": ..., "content": ...}]
    started_at: datetime = Field(default_factory=utcnow)

    # The language Twilio should listen for on the NEXT turn. Starts at the
    # campaign's configured language and, when auto_language_switch is on,
    # moves to whatever the AI last replied in - see engine.get_reply().
    language: str = Field(default="en-IN")

    # True while the opening language question is outstanding. The next thing
    # the customer says is treated as their answer, not as conversation.
    awaiting_language: bool = Field(default=False)
    # How many times we've asked. After two unclear answers we stop asking and
    # just proceed in the default language, rather than trapping the caller in
    # a menu they may not understand.
    language_asks: int = Field(default=0)

    @property
    def customer(self) -> dict:
        try:
            return json.loads(self.customer_json or "{}")
        except (ValueError, TypeError):
            return {}

    @property
    def history(self) -> list[dict]:
        try:
            return json.loads(self.history_json or "[]")
        except (ValueError, TypeError):
            return []


class Appointment(SQLModel, table=True):
    """A booking the agent made during a call.

    starts_at / ends_at are stored as naive UTC like every other timestamp, so
    conflict checks are a plain comparison. They are only converted to the
    company's timezone for display and when the agent speaks a time aloud -
    otherwise a 3pm booking in Mumbai would collide with a 3pm booking made
    from a server in another zone.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    campaign_id: Optional[int] = Field(default=None)
    contact_id: Optional[int] = Field(default=None)
    call_sid: str = Field(default="", index=True)

    customer_name: str = Field(default="")
    phone: str = Field(default="", index=True)

    starts_at: datetime = Field(index=True)
    ends_at: datetime = Field(index=True)

    # booked -> completed | cancelled | no_show
    status: str = Field(default="booked", index=True)
    notes: str = Field(default="")
    # Google Calendar event id, so a cancellation can remove the mirror copy.
    gcal_event_id: str = Field(default="")
    created_at: datetime = Field(default_factory=utcnow)


class Call(SQLModel, table=True):
    """One finished call."""

    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    campaign_id: Optional[int] = Field(default=None, foreign_key="campaign.id", index=True)

    call_sid: str = Field(default="", index=True)
    customer_name: str = Field(default="")
    phone: str = Field(default="")
    direction: str = Field(default="outbound")
    outcome: str = Field(default="", index=True)
    summary: str = Field(default="")
    action_needed: str = Field(default="")
    duration_sec: int = Field(default=0)
    transcript: str = Field(default="")
    created_at: datetime = Field(default_factory=utcnow, index=True)


class Invoice(SQLModel, table=True):
    """A billing record. Written only after a payment is actually confirmed -
    see plans.py. v2's upgrade endpoint wrote 'paid_demo' invoices and granted
    the plan with no payment at all, which is not carried over."""

    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    plan: str
    amount_inr: int = Field(default=0)
    status: str = Field(default="pending")  # pending | paid | failed
    provider_ref: str = Field(default="")
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------- migrations
def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """create_all() creates missing TABLES but never missing COLUMNS, so a
    database made before a field existed would break on every query. Add them
    in place, preserving existing rows."""
    from sqlalchemy import inspect, text

    additions = {
        "user": {"is_verified": "BOOLEAN DEFAULT 0"},
        "company": {
            "account_type": "VARCHAR DEFAULT 'enterprise'",
            "plan": "VARCHAR DEFAULT 'free'",
            "minutes_used": "FLOAT DEFAULT 0",
            "timezone": "VARCHAR DEFAULT 'Asia/Kolkata'",
        },
        "campaign": {
            "status": "VARCHAR DEFAULT 'draft'",
            "pause_reason": "VARCHAR DEFAULT ''",
            "agent_id": "INTEGER",
            "max_retries": "INTEGER DEFAULT 3",
            "auto_language_switch": "BOOLEAN DEFAULT 0",
            "handoff_enabled": "BOOLEAN DEFAULT 0",
            "ask_language": "BOOLEAN DEFAULT 0",
            "script": "VARCHAR DEFAULT ''",
            "handoff_number": "VARCHAR DEFAULT ''",
        },
        "call": {"direction": "VARCHAR DEFAULT 'outbound'"},
        "appointment": {"gcal_event_id": "VARCHAR DEFAULT ''"},
        "callsession": {
            "agent_id": "INTEGER",
            "provider": "VARCHAR DEFAULT 'twilio'",
            "language": "VARCHAR DEFAULT 'en-IN'",
            "awaiting_language": "BOOLEAN DEFAULT 0",
            "language_asks": "INTEGER DEFAULT 0",
        },
        "companysettings": {
            "groq_model": "VARCHAR DEFAULT 'llama-3.1-8b-instant'",
            "detect_voicemail": "BOOLEAN DEFAULT 1",
            "max_call_seconds": "INTEGER DEFAULT 180",
            "ai_provider": "VARCHAR DEFAULT 'groq'",
            "ollama_host": "VARCHAR DEFAULT 'http://localhost:11434'",
            "ollama_model": "VARCHAR DEFAULT 'llama3.2'",
            "openai_api_key_enc": "VARCHAR DEFAULT ''",
            "openai_model": "VARCHAR DEFAULT 'gpt-4o-mini'",
            "nvidia_api_key_enc": "VARCHAR DEFAULT ''",
            "nvidia_model": "VARCHAR DEFAULT 'nvidia/nvidia-nemotron-nano-9b-v2'",
            "stt_provider": "VARCHAR DEFAULT 'carrier'",
            "deepgram_api_key_enc": "VARCHAR DEFAULT ''",
            "auto_language_switch": "BOOLEAN DEFAULT 0",
            "webhook_out_url": "VARCHAR DEFAULT ''",
            "webhook_out_secret": "VARCHAR DEFAULT ''",
            "handoff_enabled": "BOOLEAN DEFAULT 0",
            "handoff_number": "VARCHAR DEFAULT ''",
            "speech_timeout": "VARCHAR DEFAULT 'auto'",
            "ask_language": "BOOLEAN DEFAULT 0",
            "booking_enabled": "BOOLEAN DEFAULT 0",
            "booking_slot_minutes": "INTEGER DEFAULT 30",
            "booking_open": "VARCHAR DEFAULT '10:00'",
            "booking_close": "VARCHAR DEFAULT '18:00'",
            "booking_days_ahead": "INTEGER DEFAULT 30",
            "google_refresh_token_enc": "VARCHAR DEFAULT ''",
            "google_calendar_id": "VARCHAR DEFAULT 'primary'",
            "telephony_provider": "VARCHAR DEFAULT 'twilio'",
            "plivo_auth_id": "VARCHAR DEFAULT ''",
            "plivo_auth_token_enc": "VARCHAR DEFAULT ''",
            "plivo_phone_number": "VARCHAR DEFAULT ''",
        },
    }

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    added: set[str] = set()

    with engine.begin() as conn:
        for table, columns in additions.items():
            if table not in tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in present:
                    conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {name} {ddl}'))
                    added.add(f"{table}.{name}")
                    print(f"[db] added column {table}.{name}", flush=True)

        # Accounts created before verification existed never had a chance to
        # verify, so locking them out would be wrong. Runs on migration only,
        # never on a fresh database.
        if "user.is_verified" in added:
            conn.execute(text("UPDATE user SET is_verified = 1"))
            print("[db] existing accounts marked verified", flush=True)


def get_session() -> Session:
    return Session(engine)
