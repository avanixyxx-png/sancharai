"""
Deepgram streaming transcription client.

No Deepgram account is used - a stub WebSocket server stands in for their API,
which lets us assert the things we actually control: that the connection is
opened with the right model/encoding for phone audio, that the API key is sent
as a Token header, that carrier audio is relayed through untouched, that
transcripts and utterance-end events are parsed into the shape the call loop
expects, and - most important for a live call - that a Deepgram outage or a
mid-sentence hangup closes cleanly instead of raising into the call handler.
"""
import asyncio
import json
import os
import sys
from urllib.parse import parse_qs, urlparse

import websockets

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import stt  # noqa: E402

fails = []


def check(label, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"   [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(label)


# What the stub server saw, so the client's outbound behaviour can be asserted.
seen = {"path": "", "auth": "", "audio": bytearray(), "control": []}


async def fake_deepgram(ws):
    seen["path"] = ws.request.path
    seen["auth"] = ws.request.headers.get("Authorization", "")

    async def collect():
        async for msg in ws:
            if isinstance(msg, bytes):
                seen["audio"].extend(msg)
            else:
                seen["control"].append(msg)

    collector = asyncio.create_task(collect())
    await asyncio.sleep(0.15)  # let the client's audio arrive first

    # An interim guess, then the refined final, then the silence timer.
    await ws.send(json.dumps({
        "type": "Results", "is_final": False, "speech_final": False,
        "channel": {"alternatives": [{"transcript": "kal subah"}]},
    }))
    await ws.send(json.dumps({
        "type": "Results", "is_final": True, "speech_final": True,
        "channel": {"alternatives": [{"transcript": "kal subah gyarah baje"}],
                    "detected_language": "hi"},
    }))
    # Empty finals arrive constantly during silence and must not surface.
    await ws.send(json.dumps({
        "type": "Results", "is_final": True, "speech_final": False,
        "channel": {"alternatives": [{"transcript": "   "}]},
    }))
    # An unknown type from a future Deepgram release must be ignored, not fatal.
    await ws.send(json.dumps({"type": "SomethingNewTheyAdded", "foo": 1}))
    await ws.send(json.dumps({"type": "UtteranceEnd", "last_word_end": 3.2}))
    # Stay open. The real call loop breaks out of events() here and closes from
    # ITS side - which is the path that has to send CloseStream, and the path
    # that leaks a socket per turn if close() has lost the reference.
    try:
        await asyncio.wait_for(collector, timeout=3)
    except asyncio.TimeoutError:
        collector.cancel()


async def main():
    server = await websockets.serve(fake_deepgram, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    stt.DEEPGRAM_WS = f"ws://127.0.0.1:{port}/v1/listen"

    got = []
    async with stt.DeepgramStream("dg-test-key") as stream:
        await stream.send(b"\xff\xfe\xfd\xfc")  # pretend mu-law frames
        await stream.send(b"\xfb\xfa")

        async def read():
            # Exactly how the live call loop consumes this: stop the moment the
            # caller has finished, then go think of a reply.
            async for t in stream.events():
                got.append(t)
                if t.done_speaking and t.utterance_end:
                    break

        try:
            await asyncio.wait_for(read(), timeout=5)
        except asyncio.TimeoutError:
            pass
    # context manager has now exited -> close() ran while the socket was live

    # ------------------------------------------------------ connection shape
    q = parse_qs(urlparse(seen["path"]).query)
    check("authenticates with a Token header",
          seen["auth"] == "Token dg-test-key", seen["auth"])
    check("requests nova-3", q.get("model") == ["nova-3"], str(q.get("model")))
    check("requests multilingual so one agent follows a language switch",
          q.get("language") == ["multi"], str(q.get("language")))
    check("declares mulaw, the encoding both carriers actually send",
          q.get("encoding") == ["mulaw"], str(q.get("encoding")))
    check("declares 8kHz phone audio", q.get("sample_rate") == ["8000"], str(q.get("sample_rate")))
    check("mono", q.get("channels") == ["1"], str(q.get("channels")))
    check("asks for utterance-end events (our 'caller stopped talking' cue)",
          q.get("utterance_end_ms") == ["1000"], str(q.get("utterance_end_ms")))
    check("endpointing tuned short enough to feel responsive",
          q.get("endpointing") == ["300"], str(q.get("endpointing")))

    # --------------------------------------------------------------- audio
    check("carrier audio is relayed byte-for-byte, no transcoding",
          bytes(seen["audio"]) == b"\xff\xfe\xfd\xfc\xfb\xfa", repr(bytes(seen["audio"])))

    # ----------------------------------------------------------- transcripts
    texts = [t.text for t in got]
    check("interim guess surfaces", "kal subah" in texts, str(texts))
    check("refined final surfaces", "kal subah gyarah baje" in texts, str(texts))
    check("empty/whitespace finals are dropped, not spoken as silence",
          "" not in [t.text for t in got if not t.utterance_end], str(texts))
    check("unknown message types are ignored rather than fatal", len(got) >= 3, str(texts))

    final = next((t for t in got if t.text == "kal subah gyarah baje"), None)
    check("final is flagged final", final and final.is_final, str(final))
    check("speech_final carried through", final and final.speech_final, str(final))
    check("detected language surfaced for the switch feature",
          final and final.language == "hi", str(final))
    check("a natural speech end counts as the caller's turn ending",
          final and final.done_speaking is True)

    interim = next((t for t in got if t.text == "kal subah"), None)
    check("an interim guess does NOT end the turn (would cut people off)",
          interim and interim.done_speaking is False, str(interim))

    ue = next((t for t in got if t.utterance_end), None)
    check("utterance-end event parsed", ue is not None)
    check("...and ends the turn even with no transcript of its own",
          ue and ue.done_speaking is True)

    check("CloseStream sent so the last word isn't dropped",
          any("CloseStream" in c for c in seen["control"]), str(seen["control"]))

    server.close()
    await server.wait_closed()

    # --------------------------------------------- failure modes on a live call
    try:
        stt.DeepgramStream("")
        check("a missing API key is refused up front", False, "no error raised")
    except stt.SttError:
        check("a missing API key is refused up front", True)

    stt.DEEPGRAM_WS = "ws://127.0.0.1:1/v1/listen"  # nothing listening
    try:
        async with stt.DeepgramStream("k"):
            pass
        check("an unreachable Deepgram raises SttError, not a raw socket error", False)
    except stt.SttError:
        check("an unreachable Deepgram raises SttError, not a raw socket error", True)
    except Exception as exc:  # noqa: BLE001
        check("an unreachable Deepgram raises SttError, not a raw socket error",
              False, type(exc).__name__)

    # Sending after the socket dies is normal (caller hung up); must not raise.
    dead = stt.DeepgramStream("k")
    try:
        await dead.send(b"\x00\x01")
        await dead.keepalive()
        await dead.close()
        check("audio arriving after hangup is dropped quietly, never raises", True)
    except Exception as exc:  # noqa: BLE001
        check("audio arriving after hangup is dropped quietly, never raises",
              False, type(exc).__name__)


asyncio.run(main())
print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
