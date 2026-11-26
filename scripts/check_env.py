"""Verify configuration for real before running or deploying.

    python scripts/check_env.py

- Gemini text: one generateContent call with a tool declaration (catches a bad key or model name).
- Gemini Live: opens a real native-audio session (catches an invalid key AND depleted prepaid
  credits, which a model-list call does not — see the voice-agent skill's troubleshooting).
Prints only lengths/prefixes, never secrets.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import aiohttp  # noqa: E402
import websockets  # noqa: E402

from ledgerflow import config  # noqa: E402

PLACEHOLDER_HINTS = ("your_", "xxx", "changeme", "<", "placeholder", "example")
ok = True


def report(name: str, passed: bool, detail: str = "") -> None:
    global ok
    ok &= passed
    print(f"{'PASS' if passed else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")


def check_placeholders() -> None:
    for name in ("GEMINI_API_KEY", "PUBLIC_URL", "STREAM_TOKEN", "ADMIN_TOKEN"):
        v = config.env(name)
        if v and any(h in v.lower() for h in PLACEHOLDER_HINTS):
            report(name, False, f"looks like a placeholder (len={len(v)})")
    extra = [p.name for p in config.ROOT.glob(".env.*") if p.name != ".env.example"]
    if extra:
        print(f"WARN  other env files present {extra} — the app ignores them; do not deploy from them")


async def check_text() -> None:
    if not config.GEMINI_API_KEY:
        return report("Gemini key", False, "GEMINI_API_KEY not set")
    body = {"contents": [{"role": "user", "parts": [{"text": "What is 2+2? Use the tool."}]}],
            "tools": [{"functionDeclarations": [{"name": "add", "description": "Add two numbers",
                       "parameters": {"type": "OBJECT", "properties": {"a": {"type": "NUMBER"}, "b": {"type": "NUMBER"}},
                                      "required": ["a", "b"]}}]}]}
    url = f"{config.GEMINI_REST}/models/{config.GEMINI_TEXT_MODEL}:generateContent"
    async with aiohttp.ClientSession() as h, h.post(url, json=body, headers={"x-goog-api-key": config.GEMINI_API_KEY}) as r:
        data = await r.json(content_type=None)
        if r.status != 200:
            return report(f"Gemini text ({config.GEMINI_TEXT_MODEL})", False, json.dumps(data)[:250])
        parts = data["candidates"][0]["content"]["parts"]
        report(f"Gemini text ({config.GEMINI_TEXT_MODEL})", any("functionCall" in p for p in parts),
               "function calling works" if any("functionCall" in p for p in parts) else str(parts)[:200])


async def check_live() -> None:
    if not config.GEMINI_API_KEY:
        return
    try:
        async with websockets.connect(config.GEMINI_WS_URL, open_timeout=15) as ws:
            await ws.send(json.dumps({"setup": {"model": config.GEMINI_LIVE_MODEL,
                                                "generationConfig": {"responseModalities": ["AUDIO"]}}}))
            first = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if "setupComplete" not in first:
                return report("Gemini Live", False, f"setup rejected: {str(first)[:200]}")
            await ws.send(json.dumps({"realtimeInput": {"text": "Say hello."}}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 15))
            report(f"Gemini Live ({config.GEMINI_LIVE_MODEL})", "serverContent" in reply,
                   "session answered" if "serverContent" in reply else str(reply)[:200])
    except Exception as e:  # 1007 invalid key / 1011 credits depleted arrive here
        report("Gemini Live", False, str(e)[:300])


if __name__ == "__main__":
    check_placeholders()
    asyncio.run(check_text())
    asyncio.run(check_live())
    sys.exit(0 if ok else 1)
