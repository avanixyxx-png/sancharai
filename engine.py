"""
The calling engine.

Every function takes the company's own settings, so two businesses can run
campaigns at the same time with different Twilio accounts, voices, languages
and Groq keys.

Changed from v1: conversation state used to live in module-level dicts keyed by
CallSid (_conversations, _call_customer, _call_meta). That meant a restart
mid-call lost the customer's name and the whole history, and running more than
one worker was impossible because a webhook could land on a process that had
never seen the call. All of it now goes through the CallSession table.
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import re

import httpx
from sqlmodel import Session, select

import telephony

from models import CallSession, CompanySettings, utcnow
from security import decrypt

VALID_OUTCOMES = [
    "CONFIRMED",
    "CANCELLED",
    "RESCHEDULED",
    "INTERESTED",
    "NOT_INTERESTED",
    "PAYMENT_PROMISED",
    "PAYMENT_REFUSED",
    "CALLBACK",
    "DNC",
    "UNCLEAR",
    "NO_ANSWER",
]

# Outcomes that mean "do not dial this person again", honoured by the scheduler.
TERMINAL_OUTCOMES = {"DNC", "NOT_INTERESTED", "CONFIRMED", "PAYMENT_PROMISED"}

# Lead quality 0-100, ported from VoiceIQ. A bare label tells you what happened;
# a score lets you sort a thousand calls by "who is worth ringing back".
OUTCOME_SCORE = {
    "CONFIRMED": 90,
    "PAYMENT_PROMISED": 85,
    "INTERESTED": 75,
    "CALLBACK": 65,
    "RESCHEDULED": 60,
    "UNCLEAR": 40,
    "NOT_INTERESTED": 15,
    "CANCELLED": 10,
    "PAYMENT_REFUSED": 10,
    "DNC": 0,
    "NO_ANSWER": 0,
    "FAILED": 0,
    "BUSY": 0,
    "CANCELED": 0,
}


def score_of(outcome: str) -> int:
    return OUTCOME_SCORE.get((outcome or "").upper(), 30)


# ------------------------------------------------------------- customer CSV
def normalize_phone(num: str) -> str:
    digits = re.sub(r"[^\d]", "", (num or "").strip())
    return ("+" + digits) if digits else ""


def _looks_like_phone(token: str) -> bool:
    """A bare phone number: 7-15 digits once punctuation is stripped."""
    return 7 <= len(re.sub(r"[^\d]", "", token or "")) <= 15


def _bare_number_list(text: str) -> list[dict] | None:
    """Handle a plain list of numbers with no other fields, e.g.
        +919876543210, +919876543211, +919876543212

    Without this, csv.reader sees ONE row of three columns and produces a single
    contact whose 'name' is the second number and 'info' is the third - silently
    losing two customers. VoiceIQ had the same bug (its comma-splitting fallback
    was unreachable because parse_csv always 'succeeded' first).

    Returns None when the text is anything richer than a bare number list, so
    real CSVs still go down the normal path.
    """
    tokens = [t.strip() for t in re.split(r"[,\n\r]+", text) if t.strip()]
    if len(tokens) < 2 or not all(_looks_like_phone(t) for t in tokens):
        return None

    out, seen = [], set()
    for token in tokens:
        phone = normalize_phone(token)
        if phone and phone not in seen:
            seen.add(phone)
            out.append({"phone": phone})
    return out or None


def parse_csv(text: str) -> list[dict]:
    """Turn pasted text or an uploaded CSV into customer dicts.
    Every column becomes a {placeholder} usable in the greeting."""
    text = (text or "").strip()
    if not text:
        return []

    bare = _bare_number_list(text)
    if bare is not None:
        return bare

    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []

    first = text.splitlines()[0].lower()
    has_header = any(k in first for k in ("phone", "number", "mobile", "name"))
    if has_header:
        headers = [h.strip().lower() for h in rows[0]]
        data_rows = rows[1:]
    else:
        ncols = max(len(r) for r in rows)
        headers = (
            ["phone", "name", "info"][:ncols]
            if ncols <= 3
            else ["phone", "name"] + [f"info_{i}" for i in range(ncols - 2)]
        )
        data_rows = rows

    phone_idx = next(
        (
            i
            for i, h in enumerate(headers)
            if h in ("phone", "number", "mobile", "phone_number", "contact")
        ),
        0,
    )

    customers, seen = [], set()
    for row in data_rows:
        if not row or not any(c.strip() for c in row):
            continue
        row = row + [""] * (len(headers) - len(row))
        phone = normalize_phone(row[phone_idx])
        if not phone or phone in seen:
            continue
        seen.add(phone)
        cust = {"phone": phone}
        for i, h in enumerate(headers):
            if i != phone_idx and row[i].strip():
                cust[h] = row[i].strip()
        customers.append(cust)
    return customers


def fill_template(template: str, data: dict) -> str:
    """Replace {name}, {product} etc. Unfilled placeholders are stripped so the
    agent never reads '{product}' aloud to a customer."""
    if not template:
        return template
    out = template
    for key, val in (data or {}).items():
        out = out.replace("{" + key + "}", str(val))
    out = re.sub(r"\{[a-z_ ]+\}", "", out)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,.!?])", r"\1", out)
    return out.strip()


def customer_context(customer: dict) -> str:
    if not customer or len(customer) <= 1:
        return ""
    parts = [f"  - {k}: {v}" for k, v in customer.items() if k != "phone"]
    return "Customer you are calling:\n" + "\n".join(parts)


# ------------------------------------------------------------- telephony
# Thin delegates to telephony.py, kept under their original names so every
# existing caller (app.py, scheduler.py) needed no changes beyond what
# provider-choice itself required. telephony.py is the module that actually
# knows about Twilio vs Plivo; nothing here does.
def test_twilio(sid: str, token: str) -> tuple[bool, str]:
    return telephony.test_twilio(sid, token)


def validate_twilio_signature(
    settings: CompanySettings, url: str, form: dict, signature: str
) -> bool:
    return telephony.validate_signature(
        settings, "twilio", url, form, {"X-Twilio-Signature": signature}
    )


def place_call(
    settings: CompanySettings,
    webhook_base: str,
    to_number: str,
    max_seconds: int | None = None,
    detect_voicemail: bool | None = None,
    twiml_path: str = "",
) -> str:
    """Dial through whichever provider this company has chosen in Settings.

    Returns just the call SID/UUID, matching every existing caller's
    expectations. Which provider was actually used is a pure function of
    `settings` at call time - see telephony.provider_of() - so a caller that
    needs to stamp it onto a CallSession calls that directly rather than
    needing it back from here.
    """
    sid, _provider = telephony.place_call(
        settings, webhook_base, to_number, max_seconds, detect_voicemail, twiml_path
    )
    return sid


# ----------------------------------------------------------- call session IO
def start_session(
    db: Session,
    call_sid: str,
    company_id: int,
    customer: dict,
    campaign_id: int | None = None,
    contact_id: int | None = None,
    language: str = "en-IN",
    agent_id: int | None = None,
    provider: str = "twilio",
) -> CallSession:
    """Record who we are calling, before the carrier's first webhook arrives."""
    existing = db.exec(select(CallSession).where(CallSession.call_sid == call_sid)).first()
    if existing:
        return existing
    session = CallSession(
        call_sid=call_sid,
        company_id=company_id,
        campaign_id=campaign_id,
        contact_id=contact_id,
        agent_id=agent_id,
        customer_json=json.dumps(customer or {}),
        history_json="[]",
        language=language or "en-IN",
        provider=provider if provider in telephony.PROVIDERS else "twilio",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def get_session_for(db: Session, call_sid: str) -> CallSession | None:
    return db.exec(select(CallSession).where(CallSession.call_sid == call_sid)).first()


def end_call(db: Session, call_sid: str) -> None:
    session = get_session_for(db, call_sid)
    if session:
        db.delete(session)
        db.commit()


# ----------------------------------------------------------------------- AI
# The 7 languages exposed everywhere in the UI (campaign/agent/settings forms).
# Used to validate whatever language code the model claims to be replying in -
# a hallucinated code would otherwise reach Twilio's Gather and silently break
# speech recognition for the rest of the call.
SUPPORTED_LANGUAGES = {
    "en-IN": "English",
    "hi-IN": "Hindi",
    "gu-IN": "Gujarati",
    "mr-IN": "Marathi",
    "ta-IN": "Tamil",
    "te-IN": "Telugu",
    "bn-IN": "Bengali",
}

# Google Chirp3-HD voice identities. The SAME name exists in every language, so
# "Aoede" speaking Tamil is recognisably the same person as "Aoede" speaking
# Hindi. That is what makes one agent able to switch language mid-call without
# sounding like the customer was transferred to somebody else.
#
# Stored as just the name ("Aoede"); the full Twilio voice string is assembled
# per utterance from the CURRENT language - see voice_for().
CHIRP_VOICES = {
    "Aoede": "female",
    "Kore": "female",
    "Leda": "female",
    "Zephyr": "female",
    "Charon": "male",
    "Fenrir": "male",
    "Orus": "male",
    "Puck": "male",
}


# One short line per language, used by the voice-preview call. Written so a
# listener can judge pronunciation and warmth, not just hear a beep - each is a
# sentence an agent might realistically open a call with.
PREVIEW_LINES = {
    "en-IN": "Hello, this is your AI agent. I can speak with your customers in seven languages.",
    "hi-IN": "Namaste, main aapka AI agent hoon. Main aapke customers se baat kar sakta hoon.",
    "gu-IN": "Namaste, hu tamaro AI agent chhu. Hu tamara customers sathe vaat kari shaku chhu.",
    "mr-IN": "Namaskar, mi tumcha AI agent aahe. Mi tumchya customers shi bolu shakto.",
    "ta-IN": "Vanakkam, naan ungal AI agent. Naan ungal customers-udan pesa mudiyum.",
    "te-IN": "Namaskaram, nenu mee AI agent ni. Nenu mee customers tho matladagalanu.",
    "bn-IN": "Nomoskar, ami apnar AI agent. Ami apnar customer der sathe kotha bolte pari.",
}


# How a customer might name each language out loud. Twilio transcribes the
# menu answer using the campaign's language hint, so the same word can come
# back romanised or in native script depending on that hint - both are matched.
# Common misspellings are included because phone transcription is imperfect and
# a near-miss should still pick the right language rather than fall through.
_LANGUAGE_WORDS = {
    "en-IN": ["english", "angrezi", "angreji", "इंग्लिश", "इंग्रजी", "अंग्रेजी"],
    "hi-IN": ["hindi", "हिंदी", "हिन्दी", "हिंदि"],
    "gu-IN": ["gujarati", "gujrati", "gujarathi", "ગુજરાતી", "गुजराती"],
    "mr-IN": ["marathi", "marati", "मराठी"],
    "ta-IN": ["tamil", "thamil", "தமிழ்", "தமிழ", "तमिल"],
    "te-IN": ["telugu", "telegu", "తెలుగు", "तेलुगु"],
    "bn-IN": ["bengali", "bangla", "bangali", "বাংলা", "बंगाली"],
}


def detect_language_choice(text: str) -> str | None:
    """Which language did the customer just ask for? None if unclear.

    Deliberately a plain word match rather than an AI call: this runs on the
    very first turn of every call, and a round-trip to the model here would add
    a second of dead air before the conversation has even started.
    """
    said = (text or "").lower().strip()
    if not said:
        return None
    for code, words in _LANGUAGE_WORDS.items():
        if any(w in said for w in words):
            return code
    return None


def language_menu(
    agent_name: str = "",
    company: str = "",
    languages: list[str] | None = None,
    intro: bool = True,
) -> str:
    """The spoken prompt asking which language the customer prefers.

    Leads with who is calling. Opening a cold call with a bare "Which language
    would you prefer?" is disorienting - the person has no idea who they are
    talking to yet, and the natural reaction is "who is this?" rather than an
    answer to the question.

    Offered in English because the whole point is that we do not yet know what
    they speak, and the language NAMES are the recognisable part regardless.

    intro=False is used when re-asking, so a caller who simply mishears the
    options is not made to sit through the introduction a second time.
    """
    codes = languages or list(SUPPORTED_LANGUAGES)
    names = [SUPPORTED_LANGUAGES[c] for c in codes if c in SUPPORTED_LANGUAGES]
    if len(names) > 1:
        spoken = ", ".join(names[:-1]) + ", or " + names[-1]
    else:
        spoken = names[0] if names else "English"

    question = f"Which language would you prefer? {spoken}."
    if not intro:
        return question

    agent = (agent_name or "").strip()
    org = (company or "").strip()
    if agent and org:
        hello = f"Hi, this is {agent} from {org}."
    elif agent:
        hello = f"Hi, this is {agent}."
    elif org:
        hello = f"Hi, this is {org}."
    else:
        return question
    return f"{hello} {question}"


def voice_for(voice: str, language: str) -> str:
    """The Twilio `voice=` string to speak `language` in.

    A stored value that is a bare Chirp3 name ("Aoede") is multilingual: it gets
    combined with whatever language the agent is currently speaking, so the
    voice follows a mid-call language switch while staying the same person.

    Anything else - "Polly.Aditi", "Google.gu-IN-Standard-A" - is a fixed
    single-language voice and is passed through untouched. That keeps every
    existing campaign working exactly as it did.
    """
    name = (voice or "").strip()
    if name in CHIRP_VOICES:
        lang = language if language in SUPPORTED_LANGUAGES else "en-IN"
        return f"Google.{lang}-Chirp3-HD-{name}"
    return name or "Polly.Aditi"


def _faq_block(faqs: list) -> str:
    """A company's saved FAQs as reference knowledge - answers it should give
    consistently on every call, regardless of which campaign is running."""
    if not faqs:
        return ""
    lines = "\n".join(f"Q: {f.question}\nA: {f.answer}" for f in faqs)
    return (
        "\n\nFrequently asked questions for this business - use these exact "
        "answers if the customer asks something matching one of them:\n" + lines
    )


# The agent emits this marker when a call needs a real person. A sentinel
# rather than an intent-classification pass: it costs zero extra latency and
# zero extra tokens, and the model decides in the same breath as its reply.
# Stripped from the text before anything is spoken - see strip_handoff().
HANDOFF_MARKER = "[[HANDOFF]]"


def strip_handoff(text: str) -> tuple[str, bool]:
    """Split the marker off a reply. Returns (spoken_text, wants_handoff)."""
    if HANDOFF_MARKER not in text:
        return text, False
    return text.replace(HANDOFF_MARKER, "").strip(), True


def _system_prompt(
    settings: CompanySettings,
    campaign,
    customer: dict,
    faqs: list | None = None,
    auto_switch: bool = False,
    handoff: bool = False,
    booking: bool = False,
    today: str = "",
) -> str:
    agent = (campaign.agent_name if campaign else "") or settings.agent_name
    company = settings.company_display_name or "our company"
    knowledge = f"\n\nWhat you know:\n{campaign.knowledge}" if campaign and campaign.knowledge else ""
    faq_text = _faq_block(faqs or [])

    # Both language-switching and booking need structured output, so the JSON
    # shape is assembled from whichever are enabled rather than duplicated.
    fields, rules = [], []
    if auto_switch:
        codes = ", ".join(f'"{c}" ({n})' for c, n in SUPPORTED_LANGUAGES.items())
        rules.append(
            "The customer may switch language mid-call (e.g. Hindi to English). "
            f"Follow them: reply in whichever of these the customer JUST used - {codes}."
        )
        fields.append('"language": "<one of the codes above>"')
    if booking:
        rules.append(
            f"Today is {today}. You can book appointments. When the customer agrees to "
            "a specific date and time, include it as an exact timestamp - never a phrase "
            'like "next Tuesday". Work out the real date yourself from that. '
            "Omit the field entirely if no time has been agreed yet."
        )
        fields.append('"book": "YYYY-MM-DDTHH:MM"  (omit unless a time was just agreed)')
    fields.append('"reply": "<your one-sentence reply>"')

    if fields[:-1]:
        language_rule = (
            " ".join(rules)
            + " Respond with ONLY a JSON object, no markdown, no code fences: {"
            + ", ".join(fields) + "}"
        )
    else:
        language_rule = "ALWAYS reply in the SAME language the customer used. Do not switch on your own."

    handoff_rule = ""
    if handoff:
        handoff_rule = (
            " If the customer asks to speak to a human, is angry or distressed, or asks "
            "something you genuinely cannot answer from what you know, say one short "
            f"sentence telling them you're connecting them now, then end with {HANDOFF_MARKER}. "
            f"Only use {HANDOFF_MARKER} when a real person is actually needed - not for "
            "ordinary questions you can answer."
        )

    base = (
        f"You are {agent}, a friendly voice agent for {company}. "
        f"About the business: {settings.business_desc or 'We help customers.'} "
        "Keep replies to ONE short sentence - calls are billed per minute. "
        f"{language_rule}{handoff_rule} "
        "Be warm but get to the point. Do not repeat yourself. "
        "As soon as your goal is achieved or the customer is not interested, "
        "say goodbye immediately to end the call."
        f"{knowledge}{faq_text}"
    )
    extra = customer_context(customer)
    if extra:
        base += "\n\n" + extra + "\nGreet them by name and use their details naturally."
    return fill_template(base, customer)


def _reply_budget(language: str = "") -> int:
    """Ceiling on reply length, in tokens.

    Deliberately generous and NOT per-language. Two reasons:

    1. max_tokens is a ceiling, not a charge. The model stops when its sentence
       ends, and you pay only for what it actually generates - so a tight cap
       saves nothing and only risks cutting a reply mid-word.

    2. Sizing it per language was actively broken. The budget was derived from
       the SESSION language, but the model replies in whatever language the
       customer just used, which is often different. An en-IN session (55
       tokens) answering in Gujarati (~1.5 tokens/char) truncated at about 36
       characters - mid-word, every time.

    Indic scripts are the expensive case at roughly 1.5 tokens per character,
    so this allows a comfortable spoken sentence in the worst case. Runaway
    replies are prevented by the prompt ("ONE short sentence") and by the
    call's own time_limit, not by starving the token budget.
    """
    return 400


_SENTENCE_END = ".!?।"


def _trim_to_last_sentence(text: str) -> str:
    """Cut back to the last complete sentence if the model was truncated.

    If nothing usable remains the fragment is returned anyway - a short odd
    reply still beats silence on a live call - but it is logged, because a
    fragment reaching here means the token budget was too tight and the caller
    heard half a word.
    """
    text = (text or "").strip()
    if not text or text[-1] in _SENTENCE_END:
        return text
    cut = max(text.rfind(c) for c in _SENTENCE_END)
    if cut > 20:
        return text[: cut + 1].strip()
    print(f"[TRUNCATED] reply had no complete sentence, spoken as-is: {text!r}", flush=True)
    return text


def _parse_reply(
    raw: str, fallback_language: str, auto_switch: bool, booking: bool = False
) -> tuple[str, str, str]:
    """Turn the model's raw output into (reply_text, language_code, book_when).

    JSON is only requested when auto_switch or booking is on. Otherwise this is
    a no-op returning the plain text, exactly as before either feature existed.

    A live call cannot wait on a retry or throw a 500, so if the JSON is
    missing, truncated, or carries a language we don't support, the whole
    response is treated as plain text and the caller stays on the language they
    were already using. The call keeps going either way.
    """
    if not (auto_switch or booking):
        return _trim_to_last_sentence(raw), fallback_language, ""

    try:
        match = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(match.group(0)) if match else {}
        reply = str(data.get("reply", "")).strip()
        language = str(data.get("language", "")).strip()
        book_when = str(data.get("book", "")).strip()
        if reply:
            # Language only moves when switching is enabled AND the model named
            # one we actually support.
            if auto_switch and language in SUPPORTED_LANGUAGES:
                fallback_language = language
            return _trim_to_last_sentence(reply), fallback_language, book_when
    except (ValueError, AttributeError):
        pass

    print(f"[REPLY] could not parse JSON, speaking as plain text: {raw!r}", flush=True)
    return _trim_to_last_sentence(raw), fallback_language, ""


def _llm_groq(settings: CompanySettings, messages: list[dict], max_tokens: int) -> str:
    from openai import OpenAI

    key = decrypt(settings.groq_api_key_enc)
    if not key:
        raise RuntimeError("No Groq API key configured.")
    # Groq speaks the OpenAI dialect, so the same client works.
    client = OpenAI(api_key=key, base_url="https://api.groq.com/openai/v1", timeout=20.0)
    resp = client.chat.completions.create(
        model=settings.groq_model or "llama-3.1-8b-instant",
        messages=messages,
        temperature=0.7,
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


def _llm_ollama(settings: CompanySettings, messages: list[dict], max_tokens: int) -> str:
    """Talk to an Ollama server the CUSTOMER runs.

    Ported from VoiceIQ. Deliberately not a shared platform service: pointing at
    the company's own host keeps our compute cost at zero and means call audio
    and transcripts never leave their network - the reason clinics and finance
    customers ask for it.

    Uses the plain HTTP API rather than the `ollama` package so there is no
    extra dependency for the majority who never enable this.
    """
    host = (settings.ollama_host or "http://localhost:11434").rstrip("/")
    try:
        resp = httpx.post(
            f"{host}/api/chat",
            json={
                "model": settings.ollama_model or "llama3.2",
                "messages": messages,
                "stream": False,
                "options": {"temperature": 0.7, "num_predict": max_tokens},
            },
            # Local models are slower than Groq; a caller is waiting on the line,
            # so this stays tight rather than letting dead air run.
            timeout=25.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Could not reach Ollama at {host}: {exc}") from exc
    return (resp.json().get("message", {}).get("content") or "").strip()


def _llm_openai(settings: CompanySettings, messages: list[dict], max_tokens: int) -> str:
    from openai import OpenAI

    key = decrypt(settings.openai_api_key_enc)
    if not key:
        raise RuntimeError("No OpenAI API key configured.")
    client = OpenAI(api_key=key, timeout=20.0)
    resp = client.chat.completions.create(
        model=settings.openai_model or "gpt-4o-mini",
        messages=messages,
        temperature=0.7,
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


def _llm_nvidia(settings: CompanySettings, messages: list[dict], max_tokens: int) -> str:
    """NVIDIA NIM - OpenAI-compatible hosted inference, free tier via build.nvidia.com.

    Defaults to nemotron-nano-9b-v2: small enough to stay fast on a live call,
    instruction-tuned enough to hold the JSON reply format the booking/handoff/
    language-switch features depend on. chat_template_kwargs disables the
    model's own extended "thinking" step - a reasoning preamble would blow past
    _reply_budget and either get truncated or add dead air, neither acceptable
    with a caller on the line.
    """
    from openai import OpenAI

    key = decrypt(settings.nvidia_api_key_enc)
    if not key:
        raise RuntimeError("No NVIDIA API key configured.")
    client = OpenAI(api_key=key, base_url="https://integrate.api.nvidia.com/v1", timeout=20.0)
    resp = client.chat.completions.create(
        model=settings.nvidia_model or "nvidia/nvidia-nemotron-nano-9b-v2",
        messages=messages,
        temperature=0.7,
        max_tokens=max_tokens,
        extra_body={"chat_template_kwargs": {"thinking": False}},
    )
    return (resp.choices[0].message.content or "").strip()


def _llm(settings: CompanySettings, messages: list[dict], max_tokens: int) -> str:
    """Route to whichever provider this company chose."""
    provider = (getattr(settings, "ai_provider", "") or "groq").lower()
    if provider == "ollama":
        return _llm_ollama(settings, messages, max_tokens)
    if provider == "openai":
        return _llm_openai(settings, messages, max_tokens)
    if provider == "nvidia":
        return _llm_nvidia(settings, messages, max_tokens)
    return _llm_groq(settings, messages, max_tokens)


def quick_reply(settings: CompanySettings, heard: str) -> str:
    """One conversational turn, with no database and no history.

    Used by the streaming path, where a turn arrives from the WebSocket rather
    than from a webhook and there is no CallSession to hang a transcript off
    yet. Deliberately minimal: the point of the streaming proof-of-concept is
    to prove audio and redirects work, not to re-implement get_reply(). Once
    the transport is proven, this should give way to the real get_reply() so
    streaming calls get the same FAQs, booking and handoff behaviour that
    <Gather> calls already have.
    """
    persona = (settings.business_desc or "").strip()
    system = (
        f"You are {settings.agent_name or 'Aria'}, a phone agent for "
        f"{settings.company_display_name or 'our company'}. "
        f"{persona} "
        "Reply in ONE short sentence - you are speaking on a phone call, and "
        "anything longer gets cut off. Reply in the same language the caller "
        "used."
    )
    return _llm(
        settings,
        [{"role": "system", "content": system}, {"role": "user", "content": heard}],
        _reply_budget(settings.speech_language),
    )


def ai_configured(settings: CompanySettings) -> bool:
    """Whether this company can actually place an AI call yet."""
    provider = (getattr(settings, "ai_provider", "") or "groq").lower()
    if provider == "ollama":
        return bool(settings.ollama_host)
    if provider == "openai":
        return bool(settings.openai_api_key_enc)
    if provider == "nvidia":
        return bool(settings.nvidia_api_key_enc)
    return bool(settings.groq_api_key_enc)


def test_ai(settings: CompanySettings) -> tuple[bool, str]:
    """Round-trip the configured provider so problems surface in Settings
    rather than mid-call."""
    provider = (getattr(settings, "ai_provider", "") or "groq").lower()
    try:
        reply = _llm(settings, [{"role": "user", "content": "Say OK."}], 12)
    except Exception as exc:
        return False, str(exc)
    return True, f"{provider} replied: {reply[:60] or '(empty response)'}"


def get_reply(
    db: Session,
    settings: CompanySettings,
    campaign,
    call_sid: str,
    user_text: str,
    faqs: list | None = None,
    auto_switch: bool = False,
    handoff: bool = False,
    booking: bool = False,
    today: str = "",
) -> tuple[str, str, bool, str]:
    """One conversational turn, with the full history loaded from the database.

    v2 rebuilt its LLM client on every webhook and sent only the newest sentence,
    so the agent had no memory of anything said earlier in the same call.

    Returns (reply_text, language_code, wants_handoff, book_when). When auto_switch is off,
    language_code is just whatever the session already had - unchanged, exactly
    as before that feature existed. When it's on, the model reports its own reply
    language each turn (see _system_prompt), and that becomes what Twilio listens
    for next - see _gather() in app.py. wants_handoff is True when the agent
    signalled it needs a real person.
    """
    session = get_session_for(db, call_sid)
    customer = session.customer if session else {}
    history = session.history if session else []
    current_language = (session.language if session else None) or "en-IN"

    if not history:
        history = [
            {
                "role": "system",
                "content": _system_prompt(
                    settings, campaign, customer, faqs, auto_switch, handoff,
                    booking, today,
                ),
            }
        ]
    history.append({"role": "user", "content": user_text})

    wants_handoff = False
    try:
        raw = _llm(settings, history, _reply_budget(current_language))
        # Strip the marker BEFORE parsing. _trim_to_last_sentence() cuts back to
        # the last '.', which would silently swallow a trailing marker - the
        # agent would say the right words and the transfer would never fire.
        raw, wants_handoff = strip_handoff(raw)
        reply, language, book_when = _parse_reply(
            raw, current_language, auto_switch, booking
        )
    except Exception as exc:
        print(f"[AI ERROR] {exc}", flush=True)
        reply, language, book_when = (
            "Sorry, I'm having trouble hearing you. Could you repeat that?",
            current_language, "",
        )

    if wants_handoff and not handoff:
        # Model emitted it despite handoff being disabled: the text is already
        # clean, so just don't act on it.
        wants_handoff = False

    # The history stores the plain reply an agent would actually say, never the
    # JSON wrapper - otherwise every later turn would see JSON in its own
    # transcript and start imitating it.
    history.append({"role": "assistant", "content": reply})

    if session:
        session.history_json = json.dumps(history)
        session.language = language
        db.add(session)
        db.commit()
    return reply, language, wants_handoff, book_when


def send_outcome_webhook(settings: CompanySettings, payload: dict) -> tuple[bool, str]:
    """POST a finished call's outcome to the customer's own endpoint.

    This is the cheap 80% of "integrations": rather than building a connector
    per CRM, hand them a signed POST they can point at Zapier, Make, n8n, or
    their own backend.

    The body is signed HMAC-SHA256 with their webhook secret and sent as
    X-SancharAI-Signature, so their receiver can prove the request came from us
    and not from anyone who guessed the URL - the same scheme Stripe and Twilio
    use. Never raises: a customer's broken endpoint must not take down our
    status handler, which still has minutes to bill and a contact to settle.
    """
    url = (settings.webhook_out_url or "").strip()
    if not url:
        return False, "not configured"

    body = json.dumps(payload, separators=(",", ":"), default=str)
    headers = {"Content-Type": "application/json", "User-Agent": "SancharAI-Webhook/1"}
    secret = (settings.webhook_out_secret or "").strip()
    if secret:
        headers["X-SancharAI-Signature"] = hmac.new(
            secret.encode(), body.encode(), hashlib.sha256
        ).hexdigest()

    try:
        # Short timeout: this runs while the call's status webhook is being
        # handled, and Twilio will retry us if we stall.
        resp = httpx.post(url, content=body, headers=headers, timeout=8.0)
    except httpx.HTTPError as exc:
        print(f"[webhook-out] {url} unreachable: {exc}", flush=True)
        return False, str(exc)

    if 200 <= resp.status_code < 300:
        return True, f"{resp.status_code}"
    print(f"[webhook-out] {url} returned {resp.status_code}", flush=True)
    return False, f"HTTP {resp.status_code}"


def greeting_in(settings: CompanySettings, greeting: str, language: str) -> str:
    """Render a greeting in `language`.

    Greetings are authored once, in English, in the campaign builder. Switching
    the VOICE to a Gujarati one does not translate them - a Gujarati voice
    reading English text still says English. So when the customer picks a
    different language, the text itself has to be translated before it is
    spoken, or they hear "Hello, this is Aria" in a Gujarati accent.

    Costs one model call (~0.5s) and only happens on the single turn where the
    language is chosen. Falls back to the original text on any failure - an
    English greeting is far better than dead air.
    """
    text = (greeting or "").strip()
    if not text or language.startswith("en"):
        return text

    target = SUPPORTED_LANGUAGES.get(language, "")
    if not target:
        return text

    try:
        out = _llm(
            settings,
            [
                {
                    "role": "system",
                    "content": (
                        f"Translate the user's message into {target}, as it would be "
                        "SPOKEN on a phone call. Keep people's names, company names and "
                        "numbers exactly as they are. Reply with ONLY the translation - "
                        "no quotes, no explanation, no transliteration in brackets."
                    ),
                },
                {"role": "user", "content": text},
            ],
            _reply_budget(),
        )
        out = _trim_to_last_sentence(out)
        return out or text
    except Exception as exc:
        print(f"[GREETING] could not translate to {language}, using original: {exc}", flush=True)
        return text


def transcript_of(session: CallSession | None) -> str:
    if not session:
        return ""
    return " | ".join(
        f"{'CUSTOMER' if m['role'] == 'user' else 'AI'}: {m['content']}"
        for m in session.history
        if m.get("role") != "system"
    )


# Ported from VoiceIQ, which spelled out a rule per outcome instead of listing
# bare labels. Explicit definitions measurably reduce everything collapsing into
# UNCLEAR, especially on short calls.
_OUTCOME_INSTRUCTIONS = """\
You are analyzing a customer service phone call to extract its outcome.

Read the conversation between the AI agent and the customer, then output ONLY
a JSON object (no markdown, no prose, no code fences) with these fields:

{
  "outcome": "ONE OF: CONFIRMED, CANCELLED, RESCHEDULED, INTERESTED, NOT_INTERESTED, PAYMENT_PROMISED, PAYMENT_REFUSED, CALLBACK, DNC, UNCLEAR, NO_ANSWER",
  "summary": "1 short sentence describing what the customer decided",
  "action_needed": "1 short sentence on what the business should do next"
}

Rules:
- CONFIRMED        -> customer agreed (yes / book it / confirm / okay do it)
- CANCELLED        -> customer refused or cancelled an existing commitment
- RESCHEDULED      -> customer wants a different date/time/details
- INTERESTED       -> customer wants more info but did not commit
- NOT_INTERESTED   -> customer said no thanks (sales / inquiry context)
- PAYMENT_PROMISED -> customer said they will pay
- PAYMENT_REFUSED  -> customer refused to pay
- CALLBACK         -> customer asked to be called at another time
- DNC              -> customer asked never to be called again
- UNCLEAR          -> conversation too short or ambiguous
- NO_ANSWER        -> call connected but customer barely spoke

Respond with the JSON ONLY.
"""


def analyze_call(settings: CompanySettings, session: CallSession | None) -> dict:
    """Ask the LLM to classify how the call went."""
    text = transcript_of(session)
    if not text:
        return {
            "outcome": "NO_ANSWER",
            "summary": "Call ended with no conversation.",
            "action_needed": "Try calling again later.",
            "transcript": "",
        }
    try:
        raw = _llm(
            settings,
            [
                {"role": "system", "content": _OUTCOME_INSTRUCTIONS},
                {"role": "user", "content": "Conversation transcript:\n\n" + text[:4000]},
            ],
            200,
        )
        # Strip markdown fences before hunting for the JSON - some models add
        # them even when told not to.
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        outcome = str(data.get("outcome", "UNCLEAR")).upper()
        return {
            "outcome": outcome if outcome in VALID_OUTCOMES else "UNCLEAR",
            "summary": str(data.get("summary", ""))[:400],
            "action_needed": str(data.get("action_needed", ""))[:400],
            "transcript": text,
        }
    except Exception as exc:
        print(f"[ANALYZE ERROR] {exc}", flush=True)
        return {
            "outcome": "UNCLEAR",
            "summary": "Could not analyze the call.",
            "action_needed": "Review the transcript.",
            "transcript": text,
        }
