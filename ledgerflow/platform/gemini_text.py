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
from ledgerflow.platform.grounding import Grounder
from ledgerflow.platform.telemetry import METRICS, Trace
from ledgerflow.platform.tools import REGISTRY, ToolContext

Emit = Callable[[dict], Awaitable[None]]
MAX_TOOL_ROUNDS = 6
MAX_HISTORY = 40
GROUNDER = Grounder()


class LLMError(Exception):
    pass


class GeminiREST:
    def __init__(self, api_key: str, model: str):
        self.api_key, self.model = api_key, model
        self._session: aiohttp.ClientSession | None = None

    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def generate(self, system: str, contents: list[dict], tools: list[dict] | None) -> dict:
        body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents,
                "generationConfig": {"temperature": 0.2}}
        if tools:
            body["tools"] = tools
        url = f"{config.GEMINI_REST}/models/{self.model}:generateContent"
        for attempt in range(3):
            async with self.session().post(url, json=body, headers={"x-goog-api-key": self.api_key},
                                           timeout=aiohttp.ClientTimeout(total=45)) as r:
                if r.status in (429, 500, 502, 503, 504) and attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                data = await r.json(content_type=None)
                if r.status != 200:
                    raise LLMError(f"Gemini HTTP {r.status}: {json.dumps(data)[:300]}")
                cands = data.get("candidates") or []
                if not cands or "content" not in cands[0]:
                    raise LLMError(f"Gemini returned no content: {json.dumps(data)[:300]}")
                return cands[0]["content"]
        raise LLMError("Gemini unavailable")

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


def _fallback(results: list[dict]) -> str:
    lines = ["I want to be exact, so here are the figures straight from your account:"]
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
        session.add_transcript("user", text, "text")
        session.history.append({"role": "user", "parts": [{"text": text}]})
        session.history = _trim(session.history)
        tool_results: list[dict] = session.memory.setdefault("tool_results", [])
        ctx = ToolContext(session, self.connectors, self.actions, trace, config.AS_OF, mode="text")
        contents = list(session.history)
        reply = ""
        try:
            for _ in range(MAX_TOOL_ROUNDS + 1):
                system = wf.system_prompt("text", session.customer_name, session.verified, as_of=config.AS_OF.isoformat())
                tools = REGISTRY.declarations(wf.tools)
                with trace.span("llm", metric="llm.text.call"):
                    content = await self.llm.generate(system, contents, tools)
                calls = [p["functionCall"] for p in content.get("parts", []) if "functionCall" in p]
                if not calls:
                    reply = "".join(p.get("text", "") for p in content.get("parts", []) if not p.get("thought")).strip()
                    break
                contents.append(content)
                for c in calls:
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
            # Grounding: numbers must come from tools or the customer.
            user_texts = [t["text"] for t in session.transcript if t["role"] == "user"]
            with trace.span("grounding"):
                bad = GROUNDER.check(reply, tool_results, user_texts)
            if bad:
                METRICS.incr("grounding.violation_first_pass")
                feedback = (f"[GROUNDING CHECK] These figures are not supported by any tool result: {', '.join(bad)}. "
                            "Rewrite the answer using only values returned by tools; call a tool if you need a number.")
                retry = contents + [{"role": "model", "parts": [{"text": reply}]},
                                    {"role": "user", "parts": [{"text": feedback}]}]
                system = wf.system_prompt("text", session.customer_name, session.verified, as_of=config.AS_OF.isoformat())
                with trace.span("llm regenerate", metric="llm.text.call"):
                    content = await self.llm.generate(system, retry, None)
                reply = "".join(p.get("text", "") for p in content.get("parts", []) if not p.get("thought")).strip()
                bad = GROUNDER.check(reply, tool_results, user_texts)
                if bad:
                    METRICS.incr("grounding.fallback")
                    reply = _fallback(tool_results)
            METRICS.incr("grounding.checked")
        except Exception as e:  # LLM outage: say so, keep the session usable
            METRICS.incr("llm.text.error")
            reply = ("Sorry, I'm having trouble reaching my language service right now. Your account data is safe; "
                     "please try again in a moment.")
            trace.mark("llm error", 0, ok=False, detail=str(e)[:120])
        total = (time.perf_counter() - t0) * 1000
        METRICS.observe("turn.text.total", total)
        # Persist only the clean exchange; tool call/response pairs stay in history for context.
        session.history = contents + [{"role": "model", "parts": [{"text": reply or "…"}]}]
        session.add_transcript("assistant", reply, "text")
        await emit({"type": "assistant", "text": reply})
        await emit({"type": "trace", "mode": "text", "total_ms": round(total, 1), "spans": trace.to_event()})
        return reply
