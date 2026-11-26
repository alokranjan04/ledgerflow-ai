"""Text-mode agent runtime: Gemini function calling over the shared tool registry.

Turn loop: model -> (parallel tool calls -> results)* -> answer -> grounding
check -> one regeneration with feedback if a number is unsupported -> templated
fallback if it still is. Emits progress events (tool started/finished, cards,
trace) so the UI stays responsive while slow systems answer.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable

import aiohttp

from ledgerflow import config
from ledgerflow.platform import language as lang
from ledgerflow.platform import logs
from ledgerflow.platform.connectors import Breaker
from ledgerflow.platform.grounding import Grounder
from ledgerflow.platform.telemetry import METRICS, Trace, mark_engaged
from ledgerflow.platform.tools import REGISTRY, ToolContext

Emit = Callable[[dict], Awaitable[None]]
MAX_TOOL_ROUNDS = 6
MAX_HISTORY = 40
GROUNDER = Grounder()
log = logs.get("ledgerflow.llm")


class LLMError(Exception):
    pass


class GeminiREST:
    """Gemini generateContent with a model fallback chain.

    Each model has its own circuit breaker: when the primary keeps failing
    (outage, quota), calls go straight to the fallback model until the
    primary's cooldown passes, instead of paying its timeout on every turn.
    """

    def __init__(self, api_key: str, model: str, fallback_models: list[str] | None = None,
                 call_timeout_s: float = 25.0):
        self.api_key = api_key
        self.models = [model] + [m for m in (fallback_models or []) if m and m != model]
        self.breakers = {m: Breaker(threshold=3, cooldown=30) for m in self.models}
        self.call_timeout_s = call_timeout_s
        self._session: aiohttp.ClientSession | None = None

    @property
    def model(self) -> str:
        return self.models[0]

    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def generate(self, system: str, contents: list[dict], tools: list[dict] | None) -> dict:
        last: Exception | None = None
        for i, model in enumerate(self.models):
            br = self.breakers[model]
            if not br.allow():
                continue
            try:
                content = await self._call(model, system, contents, tools)
                br.success()
                if i:
                    METRICS.incr("llm.text.fallback_used")
                    log.warning("LLM fallback model answered", extra={"fields": {"model": model}})
                return content
            except (TimeoutError, LLMError, aiohttp.ClientError) as e:
                br.failure()
                METRICS.incr(f"llm.text.error.{model}")
                log.warning("LLM call failed", extra={"fields": {"model": model, "error": str(e)[:200]}})
                last = e
        raise LLMError(f"all models failed or circuit-open: {last!r}")

    async def _call(self, model: str, system: str, contents: list[dict], tools: list[dict] | None) -> dict:
        gen = {"temperature": 0.2}
        if config.GEMINI_THINKING_LEVEL:
            gen["thinkingConfig"] = {"thinkingLevel": config.GEMINI_THINKING_LEVEL}
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents, "generationConfig": gen}
        if tools:
            body["tools"] = tools
        url = f"{config.GEMINI_REST}/models/{model}:generateContent"
        for attempt in range(2):
            async with self.session().post(url, json=body, headers={"x-goog-api-key": self.api_key},
                                           timeout=aiohttp.ClientTimeout(total=self.call_timeout_s)) as r:
                if r.status in (429, 500, 502, 503, 504) and attempt == 0:
                    await asyncio.sleep(0.4)
                    continue
                data = await r.json(content_type=None)
                if r.status != 200:
                    raise LLMError(f"Gemini HTTP {r.status}: {json.dumps(data)[:300]}")
                cands = data.get("candidates") or []
                if not cands or "content" not in cands[0]:
                    raise LLMError(f"Gemini returned no content: {json.dumps(data)[:300]}")
                return cands[0]["content"]
        raise LLMError(f"{model} unavailable")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


def _trim(history: list[dict]) -> list[dict]:
    if len(history) <= MAX_HISTORY:
        return history
    cut = len(history) - MAX_HISTORY
    while cut < len(history):
        h = history[cut]
        if h["role"] == "user" and any("text" in p for p in h["parts"]):
            break
        cut += 1
    return history[cut:]


MESSAGES = {
    "fallback_intro": {"en": "I want to be exact, so here are the figures straight from your account:",
                       "hi": "सही जानकारी के लिए, ये आँकड़े सीधे आपके खाते से हैं:",
                       "hi-Latn": "Bilkul sahi jaankari ke liye, ye figures seedhe aapke account se hain:"},
    "budget": {"en": "This is taking longer than it should, so I've stopped rather than keep you waiting. "
                     "Please ask again, or I can raise a service request for you.",
               "hi": "इसमें सामान्य से ज़्यादा समय लग रहा है, इसलिए मैंने रोक दिया है। कृपया दोबारा पूछें, "
                     "या मैं आपके लिए सर्विस रिक्वेस्ट बना सकती हूँ।",
               "hi-Latn": "Isme zyada time lag raha hai, isliye maine rok diya hai. Please dobara poochiye, "
                          "ya main aapke liye service request bana sakti hoon."},
    "llm_down": {"en": "Sorry, I'm having trouble reaching my language service right now. Your account data is "
                       "safe; please try again in a moment.",
                 "hi": "माफ़ कीजिए, अभी मेरी भाषा सेवा से संपर्क नहीं हो पा रहा है। आपका खाता सुरक्षित है; "
                       "कृपया थोड़ी देर में फिर कोशिश करें।",
                 "hi-Latn": "Maaf kijiye, abhi meri language service se connect nahi ho pa raha. Aapka account "
                            "surakshit hai; thodi der mein phir try kijiye."},
}


def msg(key: str, language: str) -> str:
    return MESSAGES[key].get(language, MESSAGES[key]["en"])


def _fallback(results: list[dict], language: str = "en") -> str:
    lines = [msg("fallback_intro", language)]
    for r in results[-2:]:
        for k, v in r.items():
            if isinstance(v, (str, int, float)) and k not in ("ok", "note", "next_step", "clause_text", "assumption"):
                lines.append(f"- {k.replace('_', ' ')}: {v}")
    return "\n".join(lines[:16])


class TextAgent:
    def __init__(self, llm: GeminiREST, workflows: dict, connectors, actions):
        self.llm, self.workflows, self.connectors, self.actions = llm, workflows, connectors, actions

    async def turn(self, session, text: str, emit: Emit) -> str:
        wf = self.workflows[session.workflow_id]
        trace = Trace()
        t0 = time.perf_counter()
        session.user_turn_seq += 1
        mark_engaged(session.memory)
        if session.language == "auto" and lang.detect(text) != "en":
            session.language = lang.detect(text)        # lock on the first non-English turn
        turn_token = logs.bind(turn=session.user_turn_seq, channel="text", workflow=wf.id)
        session.add_transcript("user", text, "text")
        session.history.append({"role": "user", "parts": [{"text": text}]})
        session.history = _trim(session.history)
        tool_results: list[dict] = session.memory.setdefault("tool_results", [])
        ctx = ToolContext(session, self.connectors, self.actions, trace, config.AS_OF, mode="text")
        contents = list(session.history)
        tools_called: list[str] = []
        grounding = "pass"

        async def run_loop() -> str:
            nonlocal grounding
            reply = ""
            for _ in range(MAX_TOOL_ROUNDS + 1):
                system = wf.system_prompt("text", session.customer_name, session.verified, as_of=config.AS_OF.isoformat(),
                                         language=session.language)
                with trace.span("llm", metric="llm.text.call"):
                    content = await self.llm.generate(system, contents, REGISTRY.declarations(wf.tools))
                calls = [p["functionCall"] for p in content.get("parts", []) if "functionCall" in p]
                if not calls:
                    reply = _text(content)
                    break
                contents.append(content)
                for c in calls:
                    tools_called.append(c["name"])
                    await emit({"type": "tool_started", "name": c["name"]})
                results = await asyncio.gather(*[
                    REGISTRY.run(c["name"], c.get("args") or {}, ctx, wf.tools) for c in calls])
                parts = []
                for c, res in zip(calls, results, strict=True):
                    await emit({"type": "tool_finished", "name": c["name"], "ok": bool(res.data.get("ok", True)),
                                "card": res.card})
                    tool_results.append(res.data)
                    fr = {"name": c["name"], "response": res.data}
                    if c.get("id"):
                        fr["id"] = c["id"]
                    parts.append({"functionResponse": fr})
                contents.append({"role": "user", "parts": parts})
            del tool_results[:-30]
            # Grounding gate: numbers must come from tools or the customer.
            user_texts = [t["text"] for t in session.transcript if t["role"] == "user"]
            with trace.span("grounding"):
                bad = GROUNDER.check(reply, tool_results, user_texts)
            if bad:
                grounding = "regenerated"
                METRICS.incr("grounding.violation_first_pass")
                feedback = (f"[GROUNDING CHECK] These figures are not supported by any tool result: {', '.join(bad)}. "
                            "Rewrite the answer using only values returned by tools; call a tool if you need a number.")
                retry = contents + [{"role": "model", "parts": [{"text": reply}]},
                                    {"role": "user", "parts": [{"text": feedback}]}]
                system = wf.system_prompt("text", session.customer_name, session.verified, as_of=config.AS_OF.isoformat(),
                                         language=session.language)
                with trace.span("llm regenerate", metric="llm.text.call"):
                    reply = _text(await self.llm.generate(system, retry, None))
                if GROUNDER.check(reply, tool_results, user_texts):
                    grounding = "fallback"
                    METRICS.incr("grounding.fallback")
                    reply = _fallback(tool_results, session.language)
            METRICS.incr("grounding.checked")
            return reply

        try:
            reply = await asyncio.wait_for(run_loop(), timeout=config.LLM_TURN_BUDGET_S)
            outcome = "ok"
        except TimeoutError:
            METRICS.incr("turn.text.budget_exceeded")
            outcome = "budget_exceeded"
            reply = msg("budget", session.language)
            trace.mark("turn budget exceeded", config.LLM_TURN_BUDGET_S * 1000, ok=False)
        except Exception as e:  # LLM outage: say so, keep the session usable
            METRICS.incr("llm.text.error")
            outcome = "llm_unavailable"
            reply = msg("llm_down", session.language)
            trace.mark("llm error", 0, ok=False, detail=str(e)[:120])
        total = (time.perf_counter() - t0) * 1000
        METRICS.observe("turn.text.total", total)
        log.info("text turn", extra={"fields": {"outcome": outcome, "ms": round(total), "tools": tools_called,
                                                "grounding": grounding}})
        logs.reset(turn_token)
        # Tool call/response pairs stay in history so follow-ups keep their context.
        session.history = contents + [{"role": "model", "parts": [{"text": reply or "…"}]}]
        session.add_transcript("assistant", reply, "text")
        await emit({"type": "assistant", "text": reply})
        await emit({"type": "trace", "mode": "text", "total_ms": round(total, 1), "spans": trace.to_event()})
        return reply


def _text(content: dict) -> str:
    return "".join(p.get("text", "") for p in content.get("parts", []) if not p.get("thought")).strip()
