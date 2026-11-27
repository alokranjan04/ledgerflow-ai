"""Latency evidence.

    python scripts/bench.py platform --url http://localhost:8080 --steps 25,50,100,200
        Concurrent sessions, no LLM: login + account (Loans+Cards+Policy) + activity (Ops).
        Measures the platform: gateway, connectors, retries, mock-system latency.

    python scripts/bench.py journeys --url http://localhost:8080 --runs 3
        The brief's customer journeys over the real chat WebSocket with the real LLM:
        time to first progress event, and end-to-end turn time.

Writes reports/latency-<mode>-<timestamp>.md (+ .json) and prints the table.
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
CUSTOMERS = [("9800000001", "1990-04-12"), ("9800000002", "1986-11-03"), ("9800000003", "1994-07-21")]
JOURNEYS = {
    "J1 EMI increase + ₹50k prepayment": ("9800000001", "1990-04-12", [
        "My EMI increased this month. Why? And if I make a ₹50,000 prepayment today, how much interest would I save?"]),
    "J2 intent change (next EMI → last payment → ₹20k tenure)": ("9800000001", "1990-04-12", [
        "What's my next EMI?", "Actually, check my last payment first.",
        "Okay, now tell me whether paying ₹20,000 extra would reduce my tenure."]),
    "J3 failed payments → dispute": ("9800000001", "1990-04-12", [
        "Show me the payments that failed in the last six months and explain what happened.",
        "Please raise a dispute for the September one, I had money in my account.", "Yes, go ahead."]),
    "J4 foreclosure eligibility": ("9800000002", "1986-11-03", ["Am I eligible for foreclosure without a penalty?"]),
    "J5 options": ("9800000001", "1990-04-12", ["Based on my current loan and applicable terms, what are my options?"]),
}


def pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))]


async def login(h, url, phone, dob):
    async with h.post(f"{url}/api/login", json={"phone": phone, "dob": dob}) as r:
        await r.read()
        return r.cookies["lf_sid"].value      # HttpOnly cookie; scripts send it back as X-Session


async def platform(url: str, steps: list[int]) -> tuple[str, dict]:
    rows, raw = [], {}
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0), cookie_jar=aiohttp.DummyCookieJar()) as h:
        await h.post(f"{url}/api/metrics/reset")
        for n in steps:
            lat: dict[str, list[float]] = {"login": [], "account": [], "activity": []}
            errors = 0

            async def one(i, lat=lat):
                nonlocal errors
                phone, dob = CUSTOMERS[i % len(CUSTOMERS)]
                try:
                    t = time.perf_counter()
                    sid = await login(h, url, phone, dob)
                    lat["login"].append((time.perf_counter() - t) * 1000)
                    for name, path in (("account", "/api/account"), ("activity", "/api/activity")):
                        t = time.perf_counter()
                        async with h.get(f"{url}{path}", headers={"X-Session": sid}) as r:
                            await r.read()
                            if r.status != 200:
                                errors += 1
                        lat[name].append((time.perf_counter() - t) * 1000)
                except Exception:
                    errors += 1
            t0 = time.perf_counter()
            await asyncio.gather(*[one(i) for i in range(n)])
            wall = time.perf_counter() - t0
            for k, v in lat.items():
                rows.append((n, k, len(v), pct(v, 50), pct(v, 95), pct(v, 99)))
            rows.append((n, "errors", errors, 0, 0, 0))
            raw[n] = {"wall_s": wall, **{k: v for k, v in lat.items()}, "errors": errors}
            print(f"  {n} concurrent sessions done in {wall:.1f}s, errors={errors}")
        from ledgerflow import config  # metrics are admin-only on a deployed service
        admin = {"X-Admin-Token": config.ADMIN_TOKEN} if config.ADMIN_TOKEN else {}
        async with h.get(f"{url}/api/metrics", headers=admin) as r:
            server = await r.json()
    md = ["| Concurrent sessions | Call | n | p50 ms | p95 ms | p99 ms |", "|---|---|---|---|---|---|"]
    md += [f"| {n} | {k} | {c} | {a:.0f} | {b:.0f} | {d:.0f} |" for n, k, c, a, b, d in rows]
    md += ["", "Server-side connector latency (includes simulated system latency, retries):", "",
           "| Metric | n | p50 | p95 | p99 |", "|---|---|---|---|---|"]
    md += [f"| {k} | {v['n']} | {v['p50']} | {v['p95']} | {v['p99']} |"
           for k, v in server["latency_ms"].items() if k.startswith(("connector.", "tool."))]
    return "\n".join(md), {"client": raw, "server": server}


async def journeys(url: str, runs: int) -> tuple[str, dict]:
    results: dict[str, list[dict]] = {}
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as h:
        for name, (phone, dob, turns) in JOURNEYS.items():
            for _ in range(runs):
                sid = await login(h, url, phone, dob)
                async with h.ws_connect(f"{url.replace('http', 'ws', 1)}/ws/chat", headers={"X-Session": sid, "Origin": url}) as ws:
                    for text in turns:
                        t0 = time.perf_counter()
                        first_progress = None
                        tools = []
                        await ws.send_json({"type": "user", "text": text})
                        while True:
                            ev = await ws.receive_json(timeout=120)
                            if ev["type"] in ("thinking", "tool_started") and first_progress is None and ev["type"] == "tool_started":
                                first_progress = (time.perf_counter() - t0) * 1000
                            if ev["type"] == "tool_started":
                                tools.append(ev["name"])
                            if ev["type"] == "assistant":
                                total = (time.perf_counter() - t0) * 1000
                                reply = ev["text"]
                            if ev["type"] == "trace":
                                break
                        results.setdefault(f"{name} :: {text[:48]}", []).append(
                            {"total_ms": total, "first_tool_ms": first_progress, "tools": tools, "reply": reply[:300]})
                        print(f"  {name[:28]:28} {total:7.0f} ms  tools={tools}")
        from ledgerflow import config  # metrics are admin-only on a deployed service
        admin = {"X-Admin-Token": config.ADMIN_TOKEN} if config.ADMIN_TOKEN else {}
        async with h.get(f"{url}/api/metrics", headers=admin) as r:
            server = await r.json()
    md = ["| Journey turn | runs | first tool started p50 ms | turn total p50 ms | turn total max ms | tools |",
          "|---|---|---|---|---|---|"]
    for k, v in results.items():
        ft = [x["first_tool_ms"] for x in v if x["first_tool_ms"]]
        tt = [x["total_ms"] for x in v]
        md.append(f"| {k} | {len(v)} | {statistics.median(ft):.0f} | {statistics.median(tt):.0f} | {max(tt):.0f} | "
                  f"{', '.join(dict.fromkeys(t for x in v for t in x['tools']))} |" if ft else
                  f"| {k} | {len(v)} | – | {statistics.median(tt):.0f} | {max(tt):.0f} | – |")
    md += ["", "Server-side:", "", "| Metric | n | p50 | p95 |", "|---|---|---|---|"]
    md += [f"| {k} | {v['n']} | {v['p50']} | {v['p95']} |" for k, v in server["latency_ms"].items()
           if k.startswith(("turn.", "llm.", "tool.", "voice."))]
    md += ["", f"Grounding counters: {json.dumps({k: v for k, v in server['counters'].items() if 'grounding' in k})}"]
    return "\n".join(md), {"results": results, "server": server}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["platform", "journeys"])
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--steps", default="25,50,100,200")
    ap.add_argument("--runs", type=int, default=3)
    a = ap.parse_args()
    url = a.url.rstrip("/")
    if a.mode == "platform":
        md, raw = asyncio.run(platform(url, [int(x) for x in a.steps.split(",")]))
    else:
        md, raw = asyncio.run(journeys(url, a.runs))
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    header = f"# Latency — {a.mode} — {time.strftime('%Y-%m-%d %H:%M')} — target {url}\n\n"
    (out / f"latency-{a.mode}-{stamp}.md").write_text(header + md + "\n", encoding="utf-8")
    (out / f"latency-{a.mode}-{stamp}.json").write_text(json.dumps(raw, indent=1, default=str), encoding="utf-8")
    print("\n" + md)
    print(f"\nwritten reports/latency-{a.mode}-{stamp}.md")


if __name__ == "__main__":
    main()
