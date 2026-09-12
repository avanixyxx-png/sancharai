"""
Streaming speech-to-text via Deepgram.

Why this exists: the carriers will transcribe for us perfectly well - Twilio's
<Gather> and Plivo's <GetInput> both do - but they bill PER TURN, about Rs 1.75
and Rs 1.70 respectively. At three turns a minute that is over Rs 5/min for
transcription alone. Deepgram's streaming API does the same job at about
Rs 0.51/min with no per-turn charge. Combined with moving TTS off the carrier
too, it takes a call from roughly Rs 15/min to Rs 5.5/min - a 3x cut.

What it does NOT fix: the call leg itself. Dialling an Indian mobile from a US
Twilio number is Rs 4.35/min, which after this change is ~80% of the remaining
cost and is a carrier termination rate, not something code can optimise. Only
an Indian number (which needs GST/DOT compliance) moves that number.

The trade is that we now own the listening half of the call: the carrier forks
raw call audio to us over a WebSocket, we relay it to Deepgram, and WE decide
when the caller has stopped talking - a job the carrier used to do. Nothing in
this file talks to a carrier or to the AI; it is purely "audio in, transcripts
out". app.py's stream endpoint is what joins the two halves.

Phone audio is 8kHz mu-law, which is what both carriers send and what Deepgram
accepts natively, so no transcoding is needed anywhere in the path - one less
thing to add latency.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from urllib.parse import urlencode

import websockets

DEEPGRAM_WS = "wss://api.deepgram.com/v1/listen"

# Deepgram's multilingual code. Nova-3 with "multi" detects and transcribes
# across languages within one stream, which is what lets a single agent follow
# a caller who switches from English to Gujarati mid-sentence - the same
# behaviour engine.auto_language_switch provides on the reply side.
MULTILINGUAL = "multi"


class SttError(Exception):
    """A problem opening or running the transcription stream."""


@dataclass
class Transcript:
    """One event from Deepgram.

    `text` is cumulative for the current utterance, not a delta - Deepgram
    resends the whole phrase as it refines it, so callers should replace
    rather than append.
    """

    text: str
    is_final: bool = False       # this chunk won't be revised further
    speech_final: bool = False   # Deepgram heard a natural end to the speech
    utterance_end: bool = False  # silence timer expired - caller has stopped
    language: str = ""           # detected language, when multilingual is on

    @property
    def done_speaking(self) -> bool:
        """Whether this is the cue to stop listening and reply.

        Either signal alone is unreliable: speech_final can fire mid-thought
        when someone pauses for breath, and utterance_end alone would make us
        wait out the full silence timer even when the caller clearly finished.
        Treating both as "your turn" keeps replies prompt without cutting
        people off - dead air and interruptions being the two ways a voice
        agent feels broken.
        """
        return self.speech_final or self.utterance_end


class DeepgramStream:
    """A live transcription session.

    Used as an async context manager so the socket is always closed, including
    when the caller hangs up mid-sentence and the surrounding task is
    cancelled:

        async with DeepgramStream(key) as stream:
            asyncio.create_task(pump_audio_from_carrier(stream))
            async for t in stream.events():
                if t.done_speaking:
                    ...
    """

    def __init__(
        self,
        api_key: str,
        *,
        language: str = MULTILINGUAL,
        model: str = "nova-3",
        encoding: str = "mulaw",
        sample_rate: int = 8000,
        endpointing_ms: int = 300,
        utterance_end_ms: int = 1000,
    ):
        if not api_key:
            raise SttError("No Deepgram API key configured.")
        self._api_key = api_key
        self._params = {
            "model": model,
            "language": language,
            "encoding": encoding,
            "sample_rate": str(sample_rate),
            "channels": "1",
            "punctuate": "true",
            # Interim results cost nothing extra and let a future barge-in
            # feature know someone started talking without waiting for the
            # final transcript.
            "interim_results": "true",
            # Silence (ms) before Deepgram calls a phrase finished. 300ms is
            # short enough to feel responsive; much lower starts chopping
            # people off mid-sentence when they pause to think.
            "endpointing": str(endpointing_ms),
            "utterance_end_ms": str(utterance_end_ms),
            "vad_events": "true",
        }
        self._ws: websockets.WebSocketClientProtocol | None = None

    # ------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> "DeepgramStream":
        url = f"{DEEPGRAM_WS}?{urlencode(self._params)}"
        try:
            self._ws = await websockets.connect(
                url,
                additional_headers={"Authorization": f"Token {self._api_key}"},
                # A caller is on the line; failing fast and falling back beats
                # holding an open call in silence waiting for a dead socket.
                open_timeout=5,
                ping_interval=5,
                ping_timeout=10,
            )
        except Exception as exc:  # noqa: BLE001 - surface anything as SttError
            raise SttError(f"Could not connect to Deepgram: {exc}") from exc
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def close(self) -> None:
        if not self._ws:
            return
        ws, self._ws = self._ws, None
        try:
            # Tells Deepgram to flush any buffered audio and close cleanly
            # rather than dropping the last partial word.
            await ws.send(json.dumps({"type": "CloseStream"}))
        except Exception:  # noqa: BLE001 - already closing; nothing to salvage
            pass
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------------------- audio
    async def send(self, audio: bytes) -> None:
        """Forward one chunk of raw mu-law audio from the carrier.

        Silently ignores a closed socket: the caller hanging up races with
        in-flight audio frames, and that is a normal end to a call, not an
        error worth propagating into the call handler.
        """
        if not self._ws:
            return
        try:
            await self._ws.send(audio)
        except websockets.exceptions.ConnectionClosed:
            self._ws = None

    async def keepalive(self) -> None:
        """Hold the socket open through a long silence.

        Deepgram closes an idle connection, and a caller thinking quietly for
        ten seconds is not a reason to lose transcription for the rest of the
        call.
        """
        if not self._ws:
            return
        try:
            await self._ws.send(json.dumps({"type": "KeepAlive"}))
        except websockets.exceptions.ConnectionClosed:
            self._ws = None

    # ----------------------------------------------------------------- events
    async def events(self):
        """Yield Transcript events until the socket closes.

        Deepgram sends several message types; anything unrecognised is skipped
        rather than raising, so a new event type added on their side can never
        take down a live call.
        """
        if not self._ws:
            return
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue

                kind = msg.get("type", "")

                if kind == "UtteranceEnd":
                    # Carries no transcript of its own - it is purely the
                    # "they've stopped talking" timer firing.
                    yield Transcript(text="", utterance_end=True)
                    continue

                if kind != "Results":
                    continue  # Metadata, SpeechStarted, etc.

                alt = (msg.get("channel", {}).get("alternatives") or [{}])[0]
                text = (alt.get("transcript") or "").strip()
                if not text:
                    # Deepgram emits empty finals during silence; forwarding
                    # them would look like the caller said nothing at all.
                    continue

                yield Transcript(
                    text=text,
                    is_final=bool(msg.get("is_final")),
                    speech_final=bool(msg.get("speech_final")),
                    language=str(msg.get("channel", {}).get("detected_language", "")),
                )
        except websockets.exceptions.ConnectionClosed:
            return
        # Deliberately does NOT clear self._ws. The call loop's normal shape is
        # to `break` out of this generator the moment the caller stops talking,
        # which runs the generator's cleanup - and if that dropped the socket
        # reference, close() would become a no-op and leak one open Deepgram
        # connection per conversational turn. close() owns the socket's life.


async def transcribe_once(
    api_key: str, audio_chunks, *, language: str = MULTILINGUAL, timeout: float = 15.0
) -> str:
    """Feed a finite series of audio chunks and return the final transcript.

    Not used on live calls - this exists so the Deepgram credentials can be
    round-tripped from Settings the same way every other provider's can,
    surfacing a bad key there rather than mid-call.
    """
    said: list[str] = []

    async with DeepgramStream(api_key, language=language) as stream:

        async def pump():
            for chunk in audio_chunks:
                await stream.send(chunk)
                await asyncio.sleep(0.01)
            await stream.close()

        task = asyncio.create_task(pump())
        try:
            async with asyncio.timeout(timeout):
                async for t in stream.events():
                    if t.text and t.is_final:
                        said.append(t.text)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        finally:
            task.cancel()

    return " ".join(said).strip()
