"""Voice pipeline against a fake Gemini Live server (offline, deterministic)."""
import asyncio
import base64
import json

import pytest
import websockets

from ledgerflow import config
from ledgerflow.platform.telemetry import METRICS
from ledgerflow.voice import telephony
from ledgerflow.voice.pipeline import PhoneTransport, VoiceConversation

from .conftest import PRIYA

PCM24_20MS = b"\x10\x00" * 480


class FakeTransport:
    def __init__(self, mode="voice"):
        self.mode = mode
        self.guard_greeting = False
        self.guard_tools = False
        self.played, self.events, self.clears = [], [], 0

    async def frames(self):
        yield "audio", b"\x00\x00" * 320
        await asyncio.sleep(30)

    async def play(self, pcm):
        self.played.append(pcm)

    async def clear(self):
        self.clears += 1

    async def event(self, ev):
        self.events.append(ev)


async def fake_gemini(script):
    """Start a fake Live server. `script(ws, setup)` drives the conversation."""
    seen = {}

    async def handler(ws):
        seen["setup"] = json.loads(await ws.recv())["setup"]
        await ws.send(json.dumps({"setupComplete": {}}))
        seen["kick"] = json.loads(await ws.recv())
        await script(ws, seen)

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}", seen


async def recv_tool_response(ws):
    while True:
        msg = json.loads(await ws.recv())
        if "toolResponse" in msg:
            return msg["toolResponse"]


def audio(pcm=PCM24_20MS):
    return {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000",
                                                                      "data": base64.b64encode(pcm).decode()}}]}}}


@pytest.mark.req("REQ-VOX-001.1", "REQ-VOX-001.2", "REQ-VOX-001.3", "REQ-VOX-001.4", "REQ-VOX-001.5",
                 "REQ-VOX-001.9")
async def test_browser_voice_turn_tools_bargein_and_context(env, monkeypatch):
    s, _ = await env["login"](*PRIYA)
    s.add_transcript("user", "earlier I asked about my loan by text", "text")
    got = {}

    async def script(ws, seen):
        await ws.send(json.dumps({"serverContent": {"inputTranscription": {"text": "what's my next EMI"}}}))
        await ws.send(json.dumps({"toolCall": {"functionCalls": [{"id": "c1", "name": "get_loan_summary", "args": {}}]}}))
        got["tool"] = await recv_tool_response(ws)
        await ws.send(json.dumps({"serverContent": {"outputTranscription": {"text": "Your next EMI is ₹17,323.49."}}}))
        await ws.send(json.dumps(audio()))
        await ws.send(json.dumps({"serverContent": {"interrupted": True}}))
        await ws.send(json.dumps({"serverContent": {"turnComplete": True}}))
        await asyncio.sleep(0.1)

    server, url, seen = await fake_gemini(script)
    monkeypatch.setattr(config, "GEMINI_WS_URL", url)
    tr = FakeTransport()
    seq0 = s.user_turn_seq
    try:
        await asyncio.wait_for(VoiceConversation(s, env["wfs"]["loan_servicing"], tr, env["con"], env["actions"]).run(), 10)
    finally:
        server.close()
    # same tools as text, executed through the registry with the session's identity
    assert got["tool"]["functionResponses"][0]["response"]["loan_id"] == "L-10001"
    assert "get_loan_summary" in json.dumps(seen["setup"]["tools"])
    # context from the text conversation is seeded into the live session
    assert "earlier I asked about my loan by text" in seen["setup"]["systemInstruction"]["parts"][0]["text"]
    # barge-in clears queued audio
    assert tr.played and tr.clears == 1
    # the customer's speech counts as a new turn (gates confirm_action)
    assert s.user_turn_seq == seq0 + 1
    # voice turns carried into text history
    assert any("[said by voice]" in p.get("text", "") for h in s.history for p in h["parts"])
    assert any(e["type"] == "tool_finished" and e["card"]["type"] == "loan_summary" for e in tr.events)
    assert METRICS.summary()["latency_ms"]["voice.voice.user_to_first_audio"]["n"] >= 1


@pytest.mark.req("REQ-AGT-004.5")
async def test_voice_grounding_monitor_flags_unsupported_figure(env, monkeypatch):
    s, _ = await env["login"](*PRIYA)

    async def script(ws, seen):
        await ws.send(json.dumps({"serverContent": {"outputTranscription": {"text": "You will save ₹99,999."}}}))
        await ws.send(json.dumps({"serverContent": {"turnComplete": True}}))
        await asyncio.sleep(0.1)

    server, url, _ = await fake_gemini(script)
    monkeypatch.setattr(config, "GEMINI_WS_URL", url)
    tr = FakeTransport()
    before = METRICS.counters.get("grounding.voice_unsupported", 0)
    try:
        await asyncio.wait_for(VoiceConversation(s, env["wfs"]["loan_servicing"], tr, env["con"], env["actions"]).run(), 10)
    finally:
        server.close()
    await asyncio.sleep(0)
    assert METRICS.counters["grounding.voice_unsupported"] == before + 1
    assert any(e["type"] == "grounding_warning" for e in tr.events)


@pytest.mark.req("REQ-VOX-001.7")
async def test_phone_call_hangs_up_after_goodbye(env, monkeypatch):
    s = await env["store"].create("loan_servicing", "phone")

    async def script(ws, seen):
        await ws.send(json.dumps({"toolCall": {"functionCalls": [{"id": "e", "name": "end_call", "args": {}}]}}))
        await recv_tool_response(ws)
        await ws.send(json.dumps(audio()))
        await ws.send(json.dumps({"serverContent": {"turnComplete": True}}))
        await asyncio.sleep(30)        # the pipeline must hang up on its own

    server, url, seen = await fake_gemini(script)
    monkeypatch.setattr(config, "GEMINI_WS_URL", url)
    tr = FakeTransport(mode="phone")
    try:
        await asyncio.wait_for(VoiceConversation(s, env["wfs"]["loan_servicing"], tr, env["con"], env["actions"]).run(), 8)
    finally:
        server.close()
    assert seen["kick"]["realtimeInput"]["text"] == "[CALL_STARTED]"
    assert "verify" in seen["setup"]["systemInstruction"]["parts"][0]["text"].lower()


@pytest.mark.req("REQ-VOX-001.8")
async def test_gemini_rejecting_the_session_is_reported(env, monkeypatch):
    s, _ = await env["login"](*PRIYA)

    async def handler(ws):
        await ws.recv()
        await ws.close(code=1007, reason="API key not valid")

    server = await websockets.serve(handler, "127.0.0.1", 0)
    monkeypatch.setattr(config, "GEMINI_WS_URL", f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
    tr = FakeTransport()
    try:
        await asyncio.wait_for(VoiceConversation(s, env["wfs"]["loan_servicing"], tr, env["con"], env["actions"]).run(), 10)
    finally:
        server.close()
    assert any(e["type"] == "error" for e in tr.events)


class _SentWS:
    closed = False

    def __init__(self):
        self.sent = []

    async def send_str(self, s):
        self.sent.append(json.loads(s))


@pytest.mark.req("REQ-VOX-001.6")
async def test_phone_transport_converts_24k_pcm_to_8k_mulaw(monkeypatch):
    monkeypatch.setattr(config, "TELEPHONY_PROVIDER", "vobiz")
    ws = _SentWS()
    tr = PhoneTransport(ws, telephony.get_provider(), None)
    await tr.play(PCM24_20MS)
    frame = ws.sent[0]
    assert frame["event"] == "playAudio" and frame["media"]["sampleRate"] == 8000
    assert abs(len(base64.b64decode(frame["media"]["payload"])) - 160) <= 2     # 20 ms of 8 kHz mu-law
    await tr.clear()
    assert ws.sent[-1] == {"event": "clear"}


@pytest.mark.req("REQ-VOX-001.6")
def test_answer_xml_for_vobiz_and_twilio(monkeypatch):
    monkeypatch.setattr(config, "STREAM_TOKEN", "tok")
    v = telephony.Vobiz().answer_xml("wss://h/stream", "9800000001")
    assert "wss://h/stream?caller_id=9800000001&amp;token=tok" in v and 'bidirectional="true"' in v
    t = telephony.Twilio().answer_xml("wss://h/stream", "9800000001")
    assert '<Stream url="wss://h/stream">' in t and 'name="token" value="tok"' in t


@pytest.mark.req("REQ-VOX-001.11")
def test_voice_prompt_puts_tools_before_speech():
    """Measured on the live model: a 'say a filler, then call the tool' instruction made Gemini Live skip
    the tool and invent the EMI (three different made-up amounts in three calls). Tool-first fixed it 3/3."""
    from ledgerflow.platform.workflow import COMMON_VOICE
    assert "call the tool FIRST" in COMMON_VOICE
    assert "Never say an amount, date or count that no tool returned" in COMMON_VOICE
    assert "say a 2-4 word filler" not in COMMON_VOICE


@pytest.mark.req("REQ-UI-001.3")
async def test_final_transcript_arrives_before_the_turn_trace(env, monkeypatch):
    s, _ = await env["login"](*PRIYA)

    async def script(ws, seen):
        await ws.send(json.dumps({"serverContent": {"outputTranscription": {"text": "Hello Priya."}}}))
        await ws.send(json.dumps(audio()))
        await ws.send(json.dumps({"serverContent": {"turnComplete": True}}))
        await asyncio.sleep(0.2)

    server, url, _ = await fake_gemini(script)
    monkeypatch.setattr(config, "GEMINI_WS_URL", url)
    tr = FakeTransport()
    try:
        await asyncio.wait_for(VoiceConversation(s, env["wfs"]["loan_servicing"], tr, env["con"], env["actions"]).run(), 10)
    finally:
        server.close()
    kinds = [(e["type"], e.get("final")) for e in tr.events if e["type"] in ("transcript", "trace")]
    assert kinds.index(("transcript", True)) < kinds.index(("trace", None))


# ----------------------------------------------------------------- server-side "one moment" clip
def _tool_turn(heard: str, speak_first: bool = False, tool: str = "get_loan_summary"):
    async def script(ws, seen):
        await ws.send(json.dumps({"serverContent": {"inputTranscription": {"text": heard}}}))
        if speak_first:
            await ws.send(json.dumps(audio(b"\x22\x00" * 480)))
        await ws.send(json.dumps({"toolCall": {"functionCalls": [{"id": "c1", "name": tool, "args": {}}]}}))
        seen["tool"] = await recv_tool_response(ws)
        await ws.send(json.dumps(audio(b"\x33\x00" * 480)))
        await ws.send(json.dumps({"serverContent": {"turnComplete": True}}))
        await asyncio.sleep(0.1)
    return script


async def _run_turn(env, monkeypatch, script, language="auto"):
    s, _ = await env["login"](*PRIYA)
    s.language = language
    server, url, seen = await fake_gemini(script)
    monkeypatch.setattr(config, "GEMINI_WS_URL", url)
    tr = FakeTransport()
    try:
        await asyncio.wait_for(VoiceConversation(s, env["wfs"]["loan_servicing"], tr, env["con"], env["actions"]).run(), 10)
    finally:
        server.close()
    return tr, seen


@pytest.mark.req("REQ-VOX-001.12")
async def test_one_moment_clip_plays_as_the_tool_starts_then_the_answer(env, monkeypatch):
    from ledgerflow.voice import fillers
    METRICS.reset()
    tr, seen = await _run_turn(env, monkeypatch, _tool_turn("what's my next EMI"))
    clip = fillers.clip("en")
    assert clip and b"".join(tr.played).startswith(clip)                    # the clip first ...
    assert tr.played[-1] == b"\x33\x00" * 480                               # ... then the model's answer
    kinds = [e["type"] for e in tr.events]
    assert kinds.index("filler") < kinds.index("tool_started")
    assert seen["tool"]["functionResponses"][0]["response"]["loan_id"] == "L-10001"   # the tool is untouched
    lat = METRICS.summary()["latency_ms"]
    assert lat["voice.voice.user_to_first_audio"]["n"] == 1                 # the wait ends at the clip
    assert lat["voice.voice.tool_result_to_audio"]["n"] == 1
    assert METRICS.counters["voice.voice.filler"] == 1


@pytest.mark.req("REQ-VOX-001.12")
async def test_no_clip_when_the_agent_already_spoke_or_is_hanging_up_or_it_is_off(env, monkeypatch):
    from ledgerflow.voice import fillers
    clip = fillers.clip("en")
    tr, _ = await _run_turn(env, monkeypatch, _tool_turn("what's my next EMI", speak_first=True))
    assert not b"".join(tr.played).startswith(clip) and not any(e["type"] == "filler" for e in tr.events)
    tr, _ = await _run_turn(env, monkeypatch, _tool_turn("bye", tool="end_call"))
    assert not any(e["type"] == "filler" for e in tr.events)
    monkeypatch.setattr(config, "VOICE_FILLER", False)
    tr, _ = await _run_turn(env, monkeypatch, _tool_turn("what's my next EMI"))
    assert not any(e["type"] == "filler" for e in tr.events)


@pytest.mark.req("REQ-VOX-001.12")
async def test_clip_follows_the_customers_language(env, monkeypatch):
    from ledgerflow.voice import fillers
    tr, _ = await _run_turn(env, monkeypatch, _tool_turn("मेरी अगली EMI कितनी है"))
    assert b"".join(tr.played).startswith(fillers.clip("hi"))
    tr, _ = await _run_turn(env, monkeypatch, _tool_turn("next EMI"), language="hi-Latn")
    assert b"".join(tr.played).startswith(fillers.clip("hi"))


@pytest.mark.req("REQ-VOX-001.12")
def test_clips_are_short_and_carry_no_figures():
    import re

    from ledgerflow.voice import fillers
    for lang, text in fillers.PHRASES.items():
        assert not re.search(r"[\d०-९₹]", text)
        secs = len(fillers.clip(lang)) / 48000
        assert 0.5 < secs < 2.5, (lang, secs)
