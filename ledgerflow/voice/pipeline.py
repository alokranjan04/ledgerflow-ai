"""Live voice: one Gemini Live (native audio) session per conversation, bridged
to either the browser mic or a phone call. Same session, tools, identity,
actions and grounding as text.

Adapted from the Mydoot telephony voice-agent bridge (greeting guard, tool
guard, barge-in clear, hang-up after the goodbye, stereo recording), with the
transport split out so the browser and the phone share one pipeline:

  browser  PCM16 16 kHz ─────────────────────────────▶ Gemini ─ PCM16 24 kHz ─▶ browser
  phone    mu-law 8 kHz ─▶ PCM16 16 kHz ─▶ Gemini ─ PCM16 24 kHz ─▶ 8 kHz mu-law ─▶ phone
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
import traceback
import warnings

import websockets
from aiohttp import WSMsgType, web

from ledgerflow import config
from ledgerflow.platform import agentlog, logs
from ledgerflow.platform.grounding import Grounder
from ledgerflow.platform.language import detect
from ledgerflow.platform.telemetry import METRICS, Trace, mark_engaged
from ledgerflow.platform.tools import REGISTRY, ToolContext
from ledgerflow.voice import fillers
from ledgerflow.voice.recorder import CallRecorder

with warnings.catch_warnings():   # stdlib audioop is deprecated in 3.12 and gone in 3.13,
    warnings.simplefilter("ignore", DeprecationWarning)   # where the audioop-lts package provides it
    import audioop  # noqa: E402

GREETING_GUARD_MAX_SECS = 20
POST_END_MAX_SECS = 30
HANGUP_PAD_SECS = 0.8
GROUNDER = Grounder()
_log = logs.get("ledgerflow.voice")


# ----------------------------------------------------------------- transports
class BrowserTransport:
    """Browser sends binary PCM16 16 kHz frames; receives binary PCM16 24 kHz
    frames plus JSON events (transcripts, tool progress, cards, traces)."""
    mode = "voice"
    guard_greeting = False
    guard_tools = False     # keep listening during tools so "actually, check X first" is heard

    def __init__(self, ws: web.WebSocketResponse):
        self.ws = ws

    async def frames(self):
        async for msg in self.ws:
            if msg.type == WSMsgType.BINARY:
                yield "audio", msg.data
            elif msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("type") == "stop":
                    return
                yield "control", data

    async def play(self, pcm24: bytes) -> None:
        if not self.ws.closed:
            await self.ws.send_bytes(pcm24)

    async def clear(self) -> None:
        await self.event({"type": "interrupted"})

    async def event(self, ev: dict) -> None:
        if not self.ws.closed:
            await self.ws.send_str(json.dumps(ev, default=str))


class PhoneTransport:
    """Telephony media stream (Vobiz / Twilio): mu-law 8 kHz JSON frames."""
    mode = "phone"
    guard_greeting = True   # line noise during the greeting makes the model interrupt itself
    guard_tools = True      # audio during a tool call caused spurious interrupts on phone lines

    def __init__(self, ws: web.WebSocketResponse, provider, recorder: CallRecorder | None):
        self.ws, self.provider, self.recorder = ws, provider, recorder
        self.stream_id = ""
        self.up_state = None
        self.down_state = None

    async def frames(self):
        async for msg in self.ws:
            if msg.type != WSMsgType.TEXT:
                continue
            kind, payload = self.provider.parse(json.loads(msg.data))
            if kind == "start":
                self.stream_id = payload.get("streamId", self.stream_id)
            elif kind == "stop":
                return
            elif kind == "media":
                pcm8 = audioop.ulaw2lin(payload, 2)
                if self.recorder is not None:
                    self.recorder.add_caller(pcm8)
                pcm16, self.up_state = audioop.ratecv(pcm8, 2, 1, 8000, 16000, self.up_state)
                yield "audio", pcm16

    async def play(self, pcm24: bytes) -> None:
        pcm8, self.down_state = audioop.ratecv(pcm24, 2, 1, 24000, 8000, self.down_state)
        if self.recorder is not None:
            self.recorder.add_agent(pcm8)
        if not self.ws.closed:
            await self.ws.send_str(json.dumps(self.provider.play(audioop.lin2ulaw(pcm8, 2), self.stream_id)))

    async def clear(self) -> None:
        if not self.ws.closed:
            await self.ws.send_str(json.dumps(self.provider.clear(self.stream_id)))
        if self.recorder is not None:
            self.recorder.clear_agent()

    async def event(self, ev: dict) -> None:
        return None


# ----------------------------------------------------------------- pipeline
class VoiceConversation:
    def __init__(self, session, workflow, transport, connectors, actions):
        self.session, self.wf, self.tr = session, workflow, transport
        self.connectors, self.actions = connectors, actions
        self.t0 = time.time()
        self.greeting_done = False
        self.tool_running = False
        self.closing = False
        self.end_after_turn = False
        self.end_requested_at = 0.0
        self.audio_since_end = 0
        self.agent_play_end = 0.0
        self.heard_buf = ""
        self.said_buf = ""
        self.user_turn_open = False
        self.last_user_speech = 0.0
        self.awaiting_first_audio = False
        self.awaiting_answer_audio = False
        self.tools_done_at = 0.0
        self.last_heard = ""
        self.turn_tool_results: list[dict] = []
        self.trace = Trace()
        self.transcript_start = len(session.transcript)

    def log(self, msg: str, level: int = 20) -> None:
        logs.event(_log, f"voice: {msg}", level=level, channel=self.tr.mode,
                   session=logs.session_hash(self.session.id), customer=self.session.customer_id or "unverified")

    def setup_message(self) -> dict:
        context = self.session.recent_transcript() if self.session.transcript else ""
        system = self.wf.system_prompt(self.tr.mode, self.session.customer_name, self.session.verified,
                                       context=context, as_of=config.AS_OF.isoformat(),
                                       language=self.session.language)
        if self.tr.mode == "phone":
            greet = self.wf.greeting + (" " + self.wf.greeting_hi if self.wf.greeting_hi else "")
            system += ("\n\nPHONE CALL: When you receive [CALL_STARTED], greet the caller in English and Hindi: " + greet +
                       " Ask whether they prefer English or Hindi, then continue only in that language."
                       " Then ask for their date of birth to verify identity. The caller's number is "
                       + ("known." if self.session.phone else "unknown, so also ask for the registered mobile number.")
                       + " When the caller is done, say goodbye and call end_call.")
        else:
            system += ("\n\nWhen you receive [SESSION_STARTED], greet the customer by first name in one short sentence "
                       "and ask how you can help — or, if there is conversation so far, say you're listening and "
                       "continue from it.")
        return {"setup": {
            "model": config.GEMINI_LIVE_MODEL,
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                                     "voiceName": self.wf.voice or config.GEMINI_VOICE}}}},
            "inputAudioTranscription": {}, "outputAudioTranscription": {},
            "systemInstruction": {"parts": [{"text": system}]},
            "tools": REGISTRY.declarations(self.wf.tools),
        }}

    async def run(self) -> None:
        self.log("voice session started")
        METRICS.incr(f"voice.{self.tr.mode}.sessions")
        try:
            async with websockets.connect(config.GEMINI_WS_URL, open_timeout=15, ping_interval=20,
                                          ping_timeout=20, max_size=None) as gem:
                t_setup = time.perf_counter()
                await gem.send(json.dumps(self.setup_message()))
                first = json.loads(await asyncio.wait_for(gem.recv(), timeout=10))
                if "setupComplete" not in first:
                    raise RuntimeError(f"Gemini setup rejected: {str(first)[:200]}")
                METRICS.observe("voice.setup", (time.perf_counter() - t_setup) * 1000)
                await self.tr.event({"type": "voice_ready"})
                kick = "[CALL_STARTED]" if self.tr.mode == "phone" else "[SESSION_STARTED]"
                await gem.send(json.dumps({"realtimeInput": {"text": kick}}))
                jobs = [asyncio.create_task(self.up(gem)), asyncio.create_task(self.down(gem)),
                        asyncio.create_task(self.watchdog())]
                done, pending = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                for j in pending:
                    j.cancel()
                for j in done:
                    if j.exception():
                        self.log(f"task error: {j.exception()!r}")
        except websockets.ConnectionClosed as e:
            # Invalid key (1007) and depleted credits (1011) arrive here, not at connect.
            close = e.rcvd
            self.log(f"Gemini closed the session: code={close.code if close else None} "
                     f"reason={close.reason if close else ''}")
            await self.tr.event({"type": "error", "text": "The voice service closed the session. Please try again."})
        except Exception:
            self.log("pipeline error:\n" + traceback.format_exc())
            await self.tr.event({"type": "error", "text": "Voice is unavailable right now. You can keep typing."})
        finally:
            self.closing = True
            self.flush()
            self.carry_into_text_history()
            self.log(f"voice session ended after {time.time() - self.t0:.0f}s")

    # caller/customer -> Gemini
    async def up(self, gem) -> None:
        async for kind, payload in self.tr.frames():
            if kind == "control":
                if payload.get("type") == "text" and payload.get("text"):
                    self.session.user_turn_seq += 1
                    self.session.add_transcript("user", payload["text"], "voice")
                    self.last_heard = payload["text"]
                    self.last_user_speech = time.perf_counter()
                    self.awaiting_first_audio = True
                    await gem.send(json.dumps({"realtimeInput": {"text": payload["text"]}}))
                continue
            if self.tr.guard_greeting and not self.greeting_done and time.time() - self.t0 > GREETING_GUARD_MAX_SECS:
                self.greeting_done = True
            if (self.tr.guard_greeting and not self.greeting_done) or (self.tr.guard_tools and self.tool_running) \
                    or self.closing:
                continue
            await gem.send(json.dumps({"realtimeInput": {"audio": {
                "data": base64.b64encode(payload).decode(), "mimeType": "audio/pcm;rate=16000"}}}))

    # Gemini -> caller/customer
    async def down(self, gem) -> None:
        async for raw in gem:
            data = json.loads(raw)
            sc = data.get("serverContent", {})

            heard = _transcription(data, sc, "input")
            if heard:
                if not self.user_turn_open:
                    self.user_turn_open = True
                    self.session.user_turn_seq += 1      # a new customer turn (gates confirm_action)
                    mark_engaged(self.session.memory, self.tr.mode)
                    self.trace = Trace()
                self.heard_buf += heard
                self.last_heard = self.heard_buf
                self.last_user_speech = time.perf_counter()
                self.awaiting_first_audio = True
                await self.tr.event({"type": "transcript", "role": "user", "text": self.heard_buf, "final": False})

            said = _transcription(data, sc, "output")
            if said:
                if self.heard_buf.strip():
                    self.flush_user()
                self.said_buf += said
                await self.tr.event({"type": "transcript", "role": "assistant", "text": self.said_buf, "final": False})

            for part in sc.get("modelTurn", {}).get("parts", []):
                inline = part.get("inlineData")
                if not inline or self.closing:
                    continue
                pcm24 = base64.b64decode(inline["data"])
                self.first_audio_heard("first audio after customer speech")
                if self.awaiting_answer_audio:
                    ms = (time.perf_counter() - self.tools_done_at) * 1000
                    METRICS.observe(f"voice.{self.tr.mode}.tool_result_to_audio", ms)
                    self.trace.mark("answer audio after tool result", ms)
                    self.awaiting_answer_audio = False
                self.user_turn_open = False
                self.agent_play_end = max(self.agent_play_end, time.time()) + len(pcm24) / 48000
                if self.end_after_turn:
                    self.audio_since_end += len(pcm24)
                await self.tr.play(pcm24)

            if sc.get("interrupted") and not self.end_after_turn:
                METRICS.incr(f"voice.{self.tr.mode}.barge_in")
                await self.tr.clear()
                self.agent_play_end = time.time()
                self.said_buf += " …"

            if sc.get("turnComplete"):
                self.flush()
                self.greeting_done = True
                await asyncio.sleep(0)   # let flush()'s final-transcript events go out before the turn's trace
                await self.tr.event({"type": "trace", "mode": self.tr.mode, "spans": self.trace.to_event(),
                                     "total_ms": None})
                if self.end_after_turn and self.audio_since_end > 0:
                    delay = max(0.0, self.agent_play_end - time.time()) + HANGUP_PAD_SECS
                    self.log(f"goodbye spoken — hanging up in {delay:.1f}s")
                    await asyncio.sleep(delay)
                    return

            if "toolCall" in data:
                await self.run_tools(gem, data["toolCall"])

            if "goAway" in data:
                self.log(f"Gemini goAway: {data['goAway']}")

    async def run_tools(self, gem, tool_call: dict) -> None:
        calls = tool_call.get("functionCalls", [])
        if not calls:
            return
        self.flush_user()
        ctx = ToolContext(self.session, self.connectors, self.actions, self.trace, config.AS_OF, mode=self.tr.mode)
        self.tool_running = True
        t0 = time.perf_counter()
        if self.awaiting_first_audio and not {c.get("name") for c in calls} <= fillers.SKIP_TOOLS:
            await self.play_filler()
        await self.tr.event({"type": "agent_step", "step": agentlog.planned([c.get("name", "") for c in calls])})
        for c in calls:
            await self.tr.event({"type": "tool_started", "name": c.get("name")})
        try:
            results = await asyncio.gather(*[REGISTRY.run(c.get("name", ""), c.get("args") or {}, ctx, self.wf.tools)
                                             for c in calls])
        finally:
            self.tool_running = False
        round_ms = (time.perf_counter() - t0) * 1000
        METRICS.observe(f"voice.{self.tr.mode}.tool_round", round_ms)
        responses = []
        for c, res in zip(calls, results, strict=True):
            self.log(f"tool {c.get('name')} ok={res.data.get('ok', True)}")
            self.turn_tool_results.append(res.data)
            self.session.memory.setdefault("tool_results", []).append(res.data)
            await self.tr.event({"type": "tool_finished", "name": c.get("name"), "ok": bool(res.data.get("ok", True)),
                                 "card": res.card})
            await self.tr.event({"type": "agent_step", "step": agentlog.tool_step(c.get("name", ""), res.data, round_ms)})
            if res.ends_call and not self.end_after_turn:
                self.end_after_turn = True
                self.end_requested_at = time.time()
            responses.append({"id": c.get("id"), "name": c.get("name"), "response": res.data})
        # Do not resume a dead socket after the phone hung up.
        if not self.closing:
            await gem.send(json.dumps({"toolResponse": {"functionResponses": responses}}))
        self.awaiting_answer_audio = True
        self.tools_done_at = time.perf_counter()

    def first_audio_heard(self, label: str) -> None:
        """The customer's wait ends at the first audio they hear, whether the clip or the answer."""
        if self.awaiting_first_audio and self.last_user_speech:
            ms = (time.perf_counter() - self.last_user_speech) * 1000
            METRICS.observe(f"voice.{self.tr.mode}.user_to_first_audio", ms)
            self.trace.mark(label, ms)
        self.awaiting_first_audio = False

    async def play_filler(self) -> None:
        """A pre-recorded "one moment" in the agent's voice, played as the tool call starts.
        It carries no figures and does not touch the model, so it cannot cause the invented
        numbers a model-spoken filler did (REQ-VOX-001.11, VOX-001.12)."""
        if not config.VOICE_FILLER or self.closing:
            return
        language = self.session.language if self.session.language != "auto" else detect(self.last_heard)
        pcm = fillers.clip(language)
        if not pcm:
            return
        METRICS.incr(f"voice.{self.tr.mode}.filler")
        self.first_audio_heard("first audio (one-moment clip)")
        await self.tr.event({"type": "filler", "language": language})
        self.agent_play_end = max(self.agent_play_end, time.time()) + len(pcm) / 48000
        for frame in fillers.frames(pcm):
            await self.tr.play(frame)

    async def watchdog(self) -> None:
        while True:
            await asyncio.sleep(1)
            if self.end_after_turn and time.time() - self.end_requested_at > POST_END_MAX_SECS:
                self.log("no goodbye after end_call — hanging up")
                return

    # transcripts -------------------------------------------------------------
    def flush_user(self) -> None:
        text = self.heard_buf.strip()
        if text:
            self.session.add_transcript("user", text, self.tr.mode)
            asyncio.ensure_future(self.tr.event({"type": "transcript", "role": "user", "text": text, "final": True}))
        self.heard_buf = ""

    def flush(self) -> None:
        self.flush_user()
        text = self.said_buf.strip()
        if text:
            self.session.add_transcript("assistant", text, self.tr.mode)
            asyncio.ensure_future(self.tr.event({"type": "transcript", "role": "assistant", "text": text, "final": True}))
            # Voice audio is already spoken, so grounding here is a monitor, not a gate.
            user_texts = [t["text"] for t in self.session.transcript if t["role"] == "user"]
            bad = GROUNDER.check(text, self.session.memory.get("tool_results", []), user_texts)
            METRICS.incr("grounding.voice_checked")
            if bad:
                METRICS.incr("grounding.voice_unsupported")
                self.log(f"grounding monitor: unsupported {bad}")
                asyncio.ensure_future(self.tr.event({"type": "grounding_warning", "figures": bad}))
        self.said_buf = ""

    def carry_into_text_history(self) -> None:
        """Voice turns become context for the text agent, so switching back to
        typing does not lose the thread."""
        for t in self.session.transcript[self.transcript_start:]:
            role = "user" if t["role"] == "user" else "model"
            self.session.history.append({"role": role, "parts": [{"text": f"[said by voice] {t['text']}"}]})
        # Gemini requires alternating roles to start with a user turn; merge consecutive same-role turns.
        merged: list[dict] = []
        for h in self.session.history:
            if merged and merged[-1]["role"] == h["role"] and all("text" in p for p in h["parts"]) \
                    and all("text" in p for p in merged[-1]["parts"]):
                merged[-1]["parts"].extend(h["parts"])
            else:
                merged.append(h)
        self.session.history = merged


def _transcription(data: dict, sc: dict, kind: str) -> str:
    for key in (f"{kind}Transcription", f"{kind}AudioTranscription"):
        t = sc.get(key) or data.get(key)
        if t and t.get("text"):
            return t["text"]
    return ""
