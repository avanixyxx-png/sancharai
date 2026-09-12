"""
The listening half of a call, when transcription is ours rather than the
carrier's.

The carrier opens a WebSocket to us and pushes the caller's audio down it as
base64 mu-law frames. We relay those to Deepgram, watch for the caller to stop
talking, ask the AI for a reply, and then REDIRECT the live call to fresh XML
that speaks it. The carrier still does the speaking - see telephony.stream()
for why only the listening half moves.

The redirect is the load-bearing assumption here and it is UNPROVEN: it
requires that a <Start><Stream> fork survives its call being redirected to new
TwiML. Twilio's docs do not state either way. If the stream dies on redirect,
every turn after the first goes deaf and this design needs rethinking - which
is exactly what the proof-of-concept call is meant to find out, cheaply,
before the rest is built on top of it. Every step logs, so a failed call can
be read off the server output rather than guessed at.
"""
from __future__ import annotations

import asyncio
import base64
import json

import engine
import stt
import telephony

# Twilio calls the field "payload" inside a "media" object; Plivo's shape was
# not confirmed against a live account, so both known spellings are accepted.
_AUDIO_KEYS = ("payload", "data")


def _frame_audio(msg: dict) -> bytes:
    """Pull raw mu-law bytes out of one carrier frame, or b'' if it isn't audio.

    Handles Twilio's {"event":"media","media":{"payload": ...}} and the flatter
    shape Plivo is documented to send, without needing to know which carrier
    sent it - the frame either carries audio or it doesn't.
    """
    holder = msg.get("media") if isinstance(msg.get("media"), dict) else msg
    for key in _AUDIO_KEYS:
        b64 = holder.get(key)
        if isinstance(b64, str) and b64:
            try:
                return base64.b64decode(b64)
            except (ValueError, TypeError):
                return b""
    return b""


def _frame_call_id(msg: dict) -> str:
    """The carrier's id for this call, wherever it hides in the start frame."""
    start = msg.get("start") if isinstance(msg.get("start"), dict) else {}
    for src in (start, msg):
        for key in ("callSid", "call_sid", "callUUID", "call_uuid", "CallUUID"):
            val = src.get(key)
            if isinstance(val, str) and val:
                return val
    return ""


async def _pump_audio(ws, stream: "stt.DeepgramStream", state: dict) -> None:
    """Carrier -> Deepgram, until the call ends.

    Runs as its own task so audio keeps flowing while the reply is being
    thought about; if this awaited the AI, we would go deaf for the second or
    two the model takes and clip the start of whatever the caller said next.
    """
    while True:
        try:
            raw = await ws.receive_text()
        except Exception:  # noqa: BLE001 - any socket end means the call ended
            break

        try:
            msg = json.loads(raw)
        except ValueError:
            continue

        event = msg.get("event", "")

        if event in ("start", "connected"):
            found = _frame_call_id(msg)
            if found:
                state["call_id"] = found
                print(f"[stream] carrier connected, call {found[:12]}", flush=True)
            continue

        if event == "stop":
            print("[stream] carrier sent stop", flush=True)
            break

        audio = _frame_audio(msg)
        if audio:
            state["frames"] += 1
            await stream.send(audio)

    await stream.close()


async def run_call(ws, company, settings, token: str, public_base: str) -> None:
    """Own one call's listening loop from connect to hangup."""
    key = engine.decrypt(settings.deepgram_api_key_enc)
    state = {"call_id": "", "frames": 0, "turns": 0}

    try:
        async with stt.DeepgramStream(key) as stream:
            pump = asyncio.create_task(_pump_audio(ws, stream, state))
            said: list[str] = []

            async for t in stream.events():
                if t.text:
                    said = [t.text]  # cumulative, not a delta

                if not t.done_speaking:
                    continue

                heard = " ".join(said).strip()
                said = []
                if not heard:
                    continue

                state["turns"] += 1
                print(f"[stream] heard ({t.language or '?'}): {heard!r}", flush=True)

                await _reply(company, settings, token, state["call_id"], heard, public_base)

            await pump
    except stt.SttError as exc:
        # Never let a transcription problem kill the call silently. The caller
        # is still connected and the carrier is still speaking; the call
        # simply stops understanding replies, which is visible in the log.
        print(f"[stream] ERROR {exc}", flush=True)
    finally:
        print(f"[stream] call {state['call_id'][:12] or '?'} ended - "
              f"{state['frames']} audio frames, {state['turns']} turns", flush=True)


async def _reply(company, settings, token, call_id, heard, public_base) -> None:
    """Think of a reply and push it into the live call."""
    if not call_id:
        print("[stream] no call id yet - cannot redirect", flush=True)
        return

    provider = telephony.provider_of(settings)
    resp = telephony.CallResponse()

    try:
        reply = await asyncio.to_thread(engine.quick_reply, settings, heard)
    except Exception as exc:  # noqa: BLE001 - a dead AI must not drop the call
        print(f"[stream] AI failed: {exc}", flush=True)
        reply = "Sorry, could you say that again?"

    print(f"[stream] replying: {reply!r}", flush=True)
    resp.say(reply, engine.voice_for(settings.voice, settings.speech_language))
    # Re-arm the fork on the new document. If a redirect DOES tear down the
    # original stream, this is what brings it back for the next turn - and if
    # it does not, the carrier simply ignores a duplicate.
    resp.stream(f"{public_base}/hook/{token}/stream".replace("https://", "wss://")
                .replace("http://", "ws://"))
    resp.pause(30)

    body, _ = resp.render(provider)
    try:
        await asyncio.to_thread(
            telephony.redirect_call, settings, provider, call_id, body
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[stream] redirect FAILED: {exc}", flush=True)
