"""Voice latency on a real Gemini Live session through the service.

    python scripts/voice_bench.py --url https://<service> [--rounds 2]

Signs in as a demo customer, opens /ws/voice, waits for the greeting, then sends each question as a
text turn over the same live session and measures:
  - question sent -> first agent audio frame   (what the customer waits before hearing anything:
                                                the "one moment" clip when a tool runs)
  - question sent -> first answer audio        (the model's own speech, after the tool result)
  - question sent -> first tool step           (the agent starting to work)
Text injection skips speech recognition and end-of-speech detection, so add the model's VAD
end-of-turn delay (typically a few hundred ms) for a spoken turn. Writes reports/voice-latency-<ts>.md.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

import aiohttp

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ["What's my next EMI?", "Actually, check my last payment first.",
             "Would paying twenty thousand rupees extra reduce my tenure?", "Am I eligible for foreclosure without a penalty?"]


async def until_turn_complete(ws, seen: dict, cap_s: float = 30) -> None:
    """Read until the model completes its turn (the service sends a `trace` event on turnComplete).
    A turn with a tool call completes only after the tool result has been spoken."""
    end = time.perf_counter() + cap_s
    while time.perf_counter() < end:
        try:
            msg = await ws.receive(timeout=max(0.1, end - time.perf_counter()))
        except TimeoutError:
            return
        if msg.type == aiohttp.WSMsgType.BINARY:
            seen.setdefault("audio_at", time.perf_counter())
            if seen.get("tool_done") or not (seen["tools"] or seen.get("filler")):
                seen.setdefault("answer_at", time.perf_counter())
        elif msg.type == aiohttp.WSMsgType.TEXT:
            ev = json.loads(msg.data)
            if ev.get("type") == "tool_started":
                seen["tools"].append(ev["name"])
            elif ev.get("type") == "tool_finished":
                seen["tool_done"] = True
            elif ev.get("type") == "filler":
                seen["filler"] = True
            elif ev.get("type") == "agent_step" and "step_at" not in seen:
                seen["step_at"] = time.perf_counter()
            elif ev.get("type") == "transcript" and ev.get("role") == "assistant" and ev.get("final"):
                seen["said"].append(ev["text"])
            elif ev.get("type") == "error":
                raise RuntimeError(ev.get("text"))
            elif ev.get("type") == "trace" and seen.get("audio_at"):
                return
        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            return


async def one_turn(ws, q: str) -> dict:
    t0 = time.perf_counter()
    await ws.send_str(json.dumps({"type": "text", "text": q}))
    seen = {"tools": [], "said": []}
    await until_turn_complete(ws, seen)
    ms = lambda k: round((seen[k] - t0) * 1000) if k in seen else None  # noqa: E731
    return {"q": q, "first_audio_ms": ms("audio_at"), "answer_audio_ms": ms("answer_at"), "filler": bool(seen.get("filler")),
            "first_step_ms": ms("step_at"), "tools": seen["tools"],
            "said": " ".join(seen["said"])}


async def main(url: str, rounds: int) -> None:
    results = []
    async with aiohttp.ClientSession() as h:
        for _ in range(rounds):
            async with h.post(f"{url}/api/login", json={"phone": "9800000001", "dob": "1990-04-12"}) as r:
                await r.read()
            async with h.ws_connect(url.replace("http", "ws", 1) + "/ws/voice", headers={"Origin": url},
                                    max_msg_size=0) as ws:
                t = time.perf_counter()
                await until_turn_complete(ws, {"tools": [], "said": []})   # setup + greeting
                print(f"  session ready + greeting in {(time.perf_counter() - t):.1f}s")
                for q in QUESTIONS:
                    res = await one_turn(ws, q)
                    results.append(res)
                    print(f"  {res['first_audio_ms'] or -1:7.0f} ms to first audio · {res['answer_audio_ms'] or -1:7.0f} ms to "
                          f"answer · clip={res['filler']} · tools={res['tools']} · {q}")
                    print(f"           said: {res['said'][:160]}")
                await ws.send_str(json.dumps({"type": "stop"}))
    def pct(key):
        xs = sorted(r[key] for r in results if r[key] is not None)
        if not xs:
            return 0, 0, 0
        return statistics.median(xs), xs[max(0, int(len(xs) * 0.95) - 1)], len(xs)
    p50, p95, n = pct("first_audio_ms")
    a50, a95, _ = pct("answer_audio_ms")
    md = [f"# Voice latency — {time.strftime('%Y-%m-%d %H:%M')} — {url}", "",
          "Real Gemini Live session through the service; questions injected as text turns (excludes speech "
          "recognition and end-of-speech detection).", "",
          "| Question | First tool step (ms) | First audio (ms) | Answer audio (ms) | One-moment clip | Tools |",
          "|---|---|---|---|---|---|"]
    md += [f"| {r['q']} | {r['first_step_ms'] and round(r['first_step_ms'])} | {r['first_audio_ms'] and round(r['first_audio_ms'])} | "
           f"{r['answer_audio_ms'] and round(r['answer_audio_ms'])} | {'yes' if r['filler'] else '—'} | "
           f"{', '.join(r['tools']) or '—'} |" for r in results]
    md += ["", f"Answer audio (the model's own speech after the tool result): p50 {a50:.0f} ms, p95 {a95:.0f} ms.",
           "", f"**First agent audio: p50 {p50:.0f} ms, p95 {p95:.0f} ms over {n} turns** "
               f"(SLO: p50 < 1000 ms, p95 < 2000 ms, measured from end of speech)."]
    out = ROOT / "reports" / f"voice-latency-{time.strftime('%Y%m%d-%H%M%S')}.md"
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n" + md[-1] + f"\nwritten {out.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--rounds", type=int, default=2)
    a = ap.parse_args()
    asyncio.run(main(a.url.rstrip("/"), a.rounds))
