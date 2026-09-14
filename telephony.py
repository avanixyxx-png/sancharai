"""
Telephony abstraction: Twilio or Plivo, chosen per company.

Twilio is a premium international carrier - about Rs 4.30/min to an Indian
mobile. Plivo, an Indian-market carrier with a Twilio-compatible model, runs
roughly Rs 0.60/min on the same route. For a company doing any real call
volume, that difference dwarfs SancharAI's own subscription fee - so this is
a per-company choice, not a platform-wide one, in Settings under "Telephony".

The one design decision worth flagging: the provider a call was PLACED with is
stamped onto its CallSession at creation. If a company switches provider in
Settings while a campaign is mid-call, in-flight calls keep speaking the
dialect they were started with - reading company_settings.telephony_provider
again on every webhook would silently reinterpret a live call's XML in the
wrong dialect.

CallResponse is the one place that knows the shape of a phone-call reply
(a greeting, then listen; or transfer to a human; or hang up). Everywhere
else in the app builds a CallResponse and calls .render(provider) - nothing
outside this file needs to know TwiML and Plivo XML are different languages.
"""
from __future__ import annotations

import xml.sax.saxutils as xml_escape

import plivo
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Dial as TwilioDial
from twilio.twiml.voice_response import Gather as TwilioGather
from twilio.twiml.voice_response import Start as TwilioStart
from twilio.twiml.voice_response import VoiceResponse as TwilioVoiceResponse

from security import decrypt

PROVIDERS = ("twilio", "plivo")


def normalize_webhook(provider: str, form: dict) -> dict:
    """Map either carrier's POST body to one shape the rest of the app reads:
    {call_id, speech, status, duration, dial_status, to, answered_by}.

    CONFIDENCE VARIES BY FIELD:
      call_id, status, duration, to  - Plivo's CallUUID/CallStatus/Duration/To
        are its long-standing documented webhook fields. High confidence.
      speech (the transcribed text)  - Plivo's exact field name for GetInput's
        speech-recognition result was NOT verified against live Plivo docs or
        a real account from here. Several plausible names are checked and the
        first present one wins; if NONE match, the raw form is logged so this
        is diagnosable rather than silently "hearing" nothing forever. Confirm
        the real field name against a Plivo trial call before relying on this
        in production, and fix the candidate list below if it differs.
      answered_by (voicemail)        - Plivo's machine-detection result field
        is likewise unverified; same defensive lookup.
    """
    if provider != "plivo":
        return {
            "call_id": form.get("CallSid", ""),
            "speech": form.get("SpeechResult", ""),
            "status": form.get("CallStatus", ""),
            "duration": form.get("CallDuration", "0"),
            "dial_status": form.get("DialCallStatus", ""),
            "to": form.get("To", ""),
            "answered_by": form.get("AnsweredBy", ""),
        }

    def first(*keys, default=""):
        for k in keys:
            if form.get(k):
                return form[k]
        return default

    speech = first("Speech", "SpeechResult", "SpeechResultText", "Result")
    return {
        "call_id": form.get("CallUUID", ""),
        "speech": speech,
        "status": first("CallStatus", "Status"),
        "duration": first("Duration", "CallDuration", default="0"),
        "dial_status": first("DialStatus", "DialCallStatus"),
        "to": form.get("To", ""),
        "answered_by": first("MachineDetection", "AnsweredBy"),
    }


class TelephonyError(Exception):
    """A problem placing a call or testing a connection - safe to show the user."""


# ------------------------------------------------------------- credentials
def _twilio_client(settings) -> TwilioClient:
    sid = settings.twilio_account_sid
    token = decrypt(settings.twilio_auth_token_enc)
    if not sid or not token:
        raise TelephonyError("Twilio is not connected. Add your credentials in Settings.")
    return TwilioClient(sid, token)


def _plivo_client(settings) -> plivo.RestClient:
    auth_id = settings.plivo_auth_id
    auth_token = decrypt(settings.plivo_auth_token_enc)
    if not auth_id or not auth_token:
        raise TelephonyError("Plivo is not connected. Add your credentials in Settings.")
    return plivo.RestClient(auth_id, auth_token)


def provider_of(settings) -> str:
    p = (getattr(settings, "telephony_provider", "") or "twilio").lower()
    return p if p in PROVIDERS else "twilio"


def is_connected(settings, provider: str | None = None) -> bool:
    provider = provider or provider_of(settings)
    if provider == "plivo":
        return bool(settings.plivo_auth_id and settings.plivo_auth_token_enc
                   and settings.plivo_phone_number)
    return bool(settings.twilio_account_sid and settings.twilio_auth_token_enc
               and settings.twilio_phone_number)


def from_number(settings, provider: str | None = None) -> str:
    provider = provider or provider_of(settings)
    return settings.plivo_phone_number if provider == "plivo" else settings.twilio_phone_number


# ------------------------------------------------------------------ testing
def test_twilio(sid: str, token: str) -> tuple[bool, str]:
    if not sid or not token:
        return False, "Missing Account SID or Auth Token."
    try:
        client = TwilioClient(sid, token)
        account = client.api.accounts(sid).fetch()
        bal = client.balance.fetch()
        return True, f"Connected as '{account.friendly_name}' · Balance {bal.balance} {bal.currency}"
    except Exception as exc:
        msg = str(exc)
        if "401" in msg or "Authenticate" in msg:
            return False, "Invalid Account SID or Auth Token."
        return False, f"Twilio error: {msg}"


def test_plivo(auth_id: str, auth_token: str) -> tuple[bool, str]:
    if not auth_id or not auth_token:
        return False, "Missing Auth ID or Auth Token."
    try:
        client = plivo.RestClient(auth_id, auth_token)
        account = client.account.get()
        return True, (
            f"Connected as '{getattr(account, 'name', auth_id)}' · "
            f"Balance {getattr(account, 'cash_credits', '?')} "
            f"{getattr(account, 'currency', '')}".strip()
        )
    except Exception as exc:
        msg = str(exc)
        if "401" in msg or "Unauthorized" in msg or "authenticate" in msg.lower():
            return False, "Invalid Auth ID or Auth Token."
        return False, f"Plivo error: {msg}"


# ------------------------------------------------------------- placing calls
def place_call(
    settings,
    webhook_base: str,
    to_number: str,
    max_seconds: int | None = None,
    detect_voicemail: bool | None = None,
    twiml_path: str = "",
    provider: str | None = None,
) -> tuple[str, str]:
    """Dial out. Returns (call_sid, provider_used).

    provider_used is what gets stamped onto the CallSession - see the module
    docstring for why that matters.
    """
    if not webhook_base:
        raise TelephonyError("Public URL is not set. Add it in Settings.")
    provider = provider or provider_of(settings)
    if not is_connected(settings, provider):
        raise TelephonyError(f"{provider.title()} is not connected. Add your credentials in Settings.")

    if max_seconds is None:
        max_seconds = getattr(settings, "max_call_seconds", None) or 180
    if detect_voicemail is None:
        detect_voicemail = getattr(settings, "detect_voicemail", True)

    answer_path = twiml_path or "/voice"

    if provider == "plivo":
        client = _plivo_client(settings)
        kwargs = dict(
            from_=settings.plivo_phone_number,
            to_=to_number,
            answer_url=f"{webhook_base}{answer_path}",
            answer_method="POST",
            hangup_url=f"{webhook_base}/status",
            hangup_method="POST",
            time_limit=int(max_seconds),
        )
        # Plivo's own AMD costs extra and adds a similar few seconds of delay
        # to Twilio's - same cost/latency tradeoff, so it stays opt-in.
        if detect_voicemail:
            kwargs["machine_detection"] = "true"
            kwargs["machine_detection_time"] = 5000
        resp = client.calls.create(**kwargs)
        return resp.request_uuid, "plivo"

    kwargs = dict(
        to=to_number,
        from_=settings.twilio_phone_number,
        url=f"{webhook_base}{answer_path}",
        status_callback=f"{webhook_base}/status",
        status_callback_event=["initiated", "answered", "completed"],
        status_callback_method="POST",
        time_limit=int(max_seconds),
    )
    if detect_voicemail:
        kwargs["machine_detection"] = "Enable"
    sid = _twilio_client(settings).calls.create(**kwargs).sid
    return sid, "twilio"


def redirect_call(settings, provider: str, call_id: str, xml: str) -> None:
    """Point a call that is already in progress at new instructions.

    This is how the streaming path speaks: the WebSocket only carries audio TO
    us, so saying something back means handing the carrier fresh XML for the
    live call. Raises on failure rather than returning a flag - the caller
    logs it, and a silent no-op here would look exactly like the AI having
    nothing to say.
    """
    if not call_id:
        raise TelephonyError("No call id to redirect.")

    if provider == "plivo":
        # UNVERIFIED shape - Plivo's live-call update is documented as taking
        # a URL to fetch rather than inline XML, so this may need a hosted
        # endpoint instead. Twilio is the tested path for streaming.
        raise TelephonyError(
            "Live-call redirect is not implemented for Plivo yet - "
            "streaming STT is Twilio-only while the design is being proven."
        )

    _twilio_client(settings).calls(call_id).update(twiml=xml)


# --------------------------------------------------------- webhook signatures
def validate_signature(settings, provider: str, url: str, form: dict, headers: dict) -> bool:
    """Confirm a webhook really came from the provider it claims to.

    Fails open only when no credential is stored yet, since there is nothing
    to validate against during first-time setup - matching the existing
    Twilio behaviour this replaces.
    """
    # Header names are case-insensitive in HTTP, and Starlette hands them over
    # lowercased - so dict(request.headers) has "x-twilio-signature", never
    # "X-Twilio-Signature". Looking up the capitalised name found nothing,
    # every genuine call failed verification, and the app hung up on it.
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}

    if provider == "plivo":
        auth_token = decrypt(settings.plivo_auth_token_enc) if settings else ""
        if not auth_token:
            return True
        signature = headers.get("x-plivo-signature-v3", "")
        nonce = headers.get("x-plivo-signature-v3-nonce", "")
        if not (signature and nonce):
            return False
        try:
            return plivo.utils.validate_v3_signature(url, nonce, signature, auth_token)
        except Exception:
            return False

    auth_token = decrypt(settings.twilio_auth_token_enc) if settings else ""
    if not auth_token:
        return True
    signature = headers.get("x-twilio-signature", "")
    if not signature:
        return False
    try:
        return RequestValidator(auth_token).validate(url, form, signature)
    except Exception:
        return False


# ------------------------------------------------------------ call response
class CallResponse:
    """A phone-call reply, built once and rendered in whichever dialect the
    call was actually placed with.

    Only the handful of shapes this app ever needs: speak (optionally then
    listen), transfer to a human, hang up, pause. Nothing more generic - a
    full TwiML/Plivo-XML feature-parity layer is not the job here.
    """

    def __init__(self):
        self._ops: list[tuple] = []

    def say(self, text: str, voice: str) -> "CallResponse":
        self._ops.append(("say", text, voice))
        return self

    def gather(self, action: str, language: str, speech_timeout: str,
              say_text: str, voice: str) -> "CallResponse":
        """Speak, then listen for the customer's reply. This app always
        pairs a gather with exactly one thing to say while listening, which
        is why the two are one call instead of separate primitives."""
        self._ops.append(("gather", action, language, speech_timeout, say_text, voice))
        return self

    def dial(self, number: str, *, action: str, caller_id: str = "", timeout: int = 25) -> "CallResponse":
        self._ops.append(("dial", number, action, caller_id, timeout))
        return self

    def pause(self, seconds: int = 1) -> "CallResponse":
        self._ops.append(("pause", seconds))
        return self

    def stream(self, ws_url: str) -> "CallResponse":
        """Fork the caller's audio to our own WebSocket for transcription.

        This is the alternative to letting the carrier transcribe: instead of
        <Gather>/<GetInput> billing per turn, the raw audio comes to us and we
        pay a flat streaming rate. See stt.py for why that matters.

        Deliberately the one-way fork (<Start><Stream> on Twilio), NOT the
        two-way <Connect><Stream>: one-way leaves the carrier in charge of
        speaking, so the existing <Say>/<Speak> path and every voice already
        configured keep working untouched. Taking over both directions would
        mean owning TTS too, which is a much larger change than the listening
        half needs.
        """
        self._ops.append(("stream", ws_url))
        return self

    def hangup(self) -> "CallResponse":
        self._ops.append(("hangup",))
        return self

    # ------------------------------------------------------------- render
    def render(self, provider: str) -> tuple[str, str]:
        """(body, content_type) in the given dialect."""
        if provider == "plivo":
            return self._render_plivo(), "application/xml"
        return self._render_twilio(), "application/xml"

    def _render_twilio(self) -> str:
        resp = TwilioVoiceResponse()
        for op in self._ops:
            kind = op[0]
            if kind == "say":
                resp.say(op[1], voice=op[2])
            elif kind == "gather":
                _, action, language, speech_timeout, say_text, voice = op
                g = TwilioGather(
                    input="speech", action=action, method="POST",
                    action_on_empty_result=True, speech_timeout=speech_timeout or "auto",
                    language=language or "en-IN", speech_model="phone_call",
                )
                g.say(say_text, voice=voice)
                resp.append(g)
            elif kind == "dial":
                _, number, action, caller_id, timeout = op
                d = TwilioDial(caller_id=caller_id or None, timeout=timeout,
                               action=action, method="POST")
                d.number(number)
                resp.append(d)
            elif kind == "pause":
                resp.pause(length=op[1])
            elif kind == "stream":
                # <Start> makes this a one-way fork that returns immediately,
                # so whatever follows in this document still runs. <Connect>
                # would block here and hand us both directions of audio.
                start = TwilioStart()
                start.stream(url=op[1], track="inbound_track")
                resp.append(start)
            elif kind == "hangup":
                resp.hangup()
        return str(resp)

    def _render_plivo(self) -> str:
        # Plivo XML is close enough to TwiML in shape (Speak/GetInput/Dial/
        # Hangup/Wait) that hand-building it is clearer than pulling in a
        # second templating layer for four tags.
        parts = ["<Response>"]
        for op in self._ops:
            kind = op[0]
            if kind == "say":
                parts.append(self._speak(op[1], op[2]))
            elif kind == "gather":
                _, action, language, speech_timeout, say_text, voice = op
                # Plivo's speech GetInput uses speechEndTimeout in whole
                # seconds; "auto" has no Plivo equivalent, so it maps to a
                # short fixed wait rather than silently behaving differently.
                timeout = "1" if speech_timeout in ("auto", "", None) else str(speech_timeout)
                parts.append(
                    f'<GetInput inputType="speech" action="{xml_escape.quoteattr(action)[1:-1]}" '
                    f'method="POST" language="{xml_escape.quoteattr(language or "en-IN")[1:-1]}" '
                    f'speechModel="phone_call" speechEndTimeout="{timeout}" redirect="true">'
                    + self._speak(say_text, voice) + "</GetInput>"
                )
            elif kind == "dial":
                _, number, action, caller_id, timeout = op
                cid = f' callerId="{xml_escape.quoteattr(caller_id)[1:-1]}"' if caller_id else ""
                parts.append(
                    f'<Dial action="{xml_escape.quoteattr(action)[1:-1]}" method="POST" '
                    f'timeLimit="{timeout}"{cid}>'
                    f'<Number>{xml_escape.escape(number)}</Number></Dial>'
                )
            elif kind == "pause":
                parts.append(f'<Wait length="{op[1]}"/>')
            elif kind == "stream":
                # UNVERIFIED against live Plivo docs, same caveat as <Speak>
                # below: Plivo's audio-stream element is documented as
                # <Stream> with bidirectional="false" for a listen-only fork,
                # but this was not confirmed against a real Plivo account.
                # Twilio is the tested path; confirm this before relying on
                # streaming STT on Plivo.
                parts.append(
                    f'<Stream bidirectional="false" audioTrack="inbound" '
                    f'contentType="audio/x-mulaw;rate=8000">'
                    f'{xml_escape.escape(op[1])}</Stream>'
                )
            elif kind == "hangup":
                parts.append("<Hangup/>")
        parts.append("</Response>")
        return "".join(parts)

    @staticmethod
    def _speak(text: str, voice: str) -> str:
        # NOT verified against live Plivo docs: Plivo's <Speak> takes its own
        # voice catalogue (Polly-based, but not confirmed identical to
        # Twilio's naming). The Chirp3-HD multilingual identities (Aoede,
        # Kore, ...) are Google/Twilio-specific and almost certainly do NOT
        # exist on Plivo at all - see the compatibility warning surfaced in
        # Settings when a company on Plivo has one selected.
        return f'<Speak voice="{xml_escape.quoteattr(voice)[1:-1]}">{xml_escape.escape(text)}</Speak>'
