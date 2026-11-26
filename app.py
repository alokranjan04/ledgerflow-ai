"""LedgerFlow AI — one aiohttp service.

Web UI      GET  /                      static single-page app
Session     POST /api/login             phone + DOB -> verified session
            POST /api/workflow          switch workflow (loans <-> cards), identity kept
            GET  /api/account           account panel data
            GET  /api/activity          service requests + audit trail
Text        GET  /ws/chat               chat WebSocket (events: tool progress, cards, trace)
Voice       GET  /ws/voice              browser mic <-> Gemini Live
Phone       POST /answer, GET /stream   Vobiz / Twilio media stream <-> Gemini Live
Ops         GET  /api/metrics, /api/faults, /health
Mocks       /mock/<system>/...          simulated enterprise systems (latency + faults)
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections import defaultdict

from aiohttp import web

from ledgerflow import config
from ledgerflow.domains import cards as cards_domain
from ledgerflow.domains import loans as loans_domain
from ledgerflow.mocks.server import MockSystems, build_mock_app
from ledgerflow.platform import common_tools
from ledgerflow.platform.actions import ActionService, AuditLog
from ledgerflow.platform.connectors import DEFAULT_POLICIES, ConnectorRegistry
from ledgerflow.platform.gemini_text import GeminiREST, TextAgent
from ledgerflow.platform.sessions import SessionStore
from ledgerflow.platform.telemetry import METRICS, Trace
from ledgerflow.platform.tools import REGISTRY, ToolContext
from ledgerflow.platform.workflow import load_workflows
from ledgerflow.voice import telephony
from ledgerflow.voice.pipeline import BrowserTransport, PhoneTransport, VoiceConversation
from ledgerflow.voice.recorder import CallRecorder

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

STATIC = config.ROOT / "frontend"
DEMO_CUSTOMERS = [
    {"name": "Priya Sharma", "phone": "9800000001", "dob": "1990-04-12",
     "story": "Floating-rate loan: EMI went up after a rate reset, 3 failed debits, penalty-free foreclosure, credit card"},
    {"name": "Rahul Verma", "phone": "9800000002", "dob": "1986-11-03",
     "story": "Fixed-rate loan past lock-in: foreclosure carries a 4% charge"},
    {"name": "Meera Nair", "phone": "9800000003", "dob": "1994-07-21",
     "story": "Fixed-rate loan inside the 12-EMI lock-in"},
    {"name": "Ananya Iyer", "phone": "9800000004", "dob": "1992-02-14",
     "story": "Credit card with a duplicate Croma charge to dispute"},
]

# Brute-force protection across sessions: phone -> failure timestamps.
LOGIN_FAILURES: dict[str, list[float]] = defaultdict(list)
LOGIN_WINDOW_S, LOGIN_MAX_FAILURES = 900, 5


def mask_phone(p: str) -> str:
    return ("*" * max(0, len(p) - 4)) + p[-4:] if p else ""


def admin_ok(request: web.Request) -> bool:
    return not config.ADMIN_TOKEN or request.headers.get("X-Admin-Token") == config.ADMIN_TOKEN


async def on_startup(app: web.Application) -> None:
    app["connectors"] = ConnectorRegistry(config.MOCK_BASE_URL, DEFAULT_POLICIES)
    actions = ActionService(AuditLog(config.AUDIT_DB_PATH))
    common_tools.register_common_actions(actions)
    loans_domain.register_actions(actions)
    cards_domain.register_actions(actions)
    app["actions"] = actions
    app["workflows"] = load_workflows()
    app["sessions"] = SessionStore(config.SESSION_TTL_SECS)
    app["llm"] = GeminiREST(config.GEMINI_API_KEY, config.GEMINI_TEXT_MODEL)
    app["agent"] = TextAgent(app["llm"], app["workflows"], app["connectors"], actions)
    app["locks"] = defaultdict(asyncio.Lock)
    print(f"LedgerFlow AI ready on :{config.PORT} as_of={config.AS_OF} workflows={list(app['workflows'])}", flush=True)


async def on_cleanup(app: web.Application) -> None:
    await app["connectors"].close()
    await app["llm"].close()


def _session(request: web.Request, sid: str | None = None):
    sid = sid or request.query.get("session") or request.headers.get("X-Session")
    s = request.app["sessions"].get(sid)
    if s is None:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "session expired"}), content_type="application/json")
    return s


# ------------------------------------------------------------------ pages / config
async def index(request):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def api_config(request):
    wfs = request.app["workflows"]
    return web.json_response({
        "as_of": config.AS_OF.isoformat(),
        "workflows": [{"id": w.id, "name": w.name, "description": w.description, "panels": w.panels,
                       "suggestions": w.suggestions}
                      for w in sorted(wfs.values(), key=lambda w: w.id != config.DEFAULT_WORKFLOW)],
        "default_workflow": config.DEFAULT_WORKFLOW,
        "demo_customers": DEMO_CUSTOMERS,
        "voice_enabled": bool(config.GEMINI_API_KEY),
        "phone_number_hint": "Call the LedgerFlow number configured in Vobiz/Twilio" if config.PUBLIC_URL else "",
    })


async def api_login(request):
    body = await request.json()
    phone = common_tools.normalise_phone(str(body.get("phone", "")))
    now = time.time()
    LOGIN_FAILURES[phone] = [t for t in LOGIN_FAILURES[phone] if now - t < LOGIN_WINDOW_S]
    if len(LOGIN_FAILURES[phone]) >= LOGIN_MAX_FAILURES:
        return web.json_response({"ok": False, "error": "Too many attempts. Try again in 15 minutes."}, status=429)
    wf = body.get("workflow") or config.DEFAULT_WORKFLOW
    if wf not in request.app["workflows"]:
        return web.json_response({"ok": False, "error": "unknown workflow"}, status=400)
    s = request.app["sessions"].create(wf, "web")
    res = await common_tools.verify(s, request.app["connectors"], phone, str(body.get("dob", "")))
    if not res.get("ok"):
        LOGIN_FAILURES[phone].append(now)
        request.app["sessions"].drop(s.id)
        print(f"[login] failed phone={mask_phone(phone)}", flush=True)
        return web.json_response({"ok": False, "error": res.get("error", "Verification failed")}, status=401)
    print(f"[login] verified {s.customer_id}", flush=True)
    return web.json_response({"ok": True, "session_id": s.id, "customer_name": s.customer_name, "workflow": s.workflow_id})


async def api_logout(request):
    body = await request.json()
    request.app["sessions"].drop(body.get("session_id", ""))
    return web.json_response({"ok": True})


async def api_workflow(request):
    body = await request.json()
    s = _session(request, body.get("session_id"))
    wf = body.get("workflow")
    if wf not in request.app["workflows"]:
        return web.json_response({"ok": False, "error": "unknown workflow"}, status=400)
    if wf != s.workflow_id:
        s.workflow_id = wf
        # Tool-call history of the old workflow references tools the new one doesn't declare;
        # keep the conversation as plain text so context carries over.
        s.history = []
        ctx = s.recent_transcript(12)
        if ctx:
            s.history = [{"role": "user", "parts": [{"text": "[Earlier in this session]\n" + ctx}]},
                         {"role": "model", "parts": [{"text": "Understood, I have the context."}]}]
    return web.json_response({"ok": True, "workflow": s.workflow_id})


async def _direct_tool(request, s, name: str):
    ctx = ToolContext(s, request.app["connectors"], request.app["actions"], Trace(), config.AS_OF)
    return await REGISTRY.run(name, {}, ctx, [name])


async def api_account(request):
    s = _session(request)
    loan, card = await asyncio.gather(_direct_tool(request, s, "get_loan_summary"),
                                      _direct_tool(request, s, "get_card_summary"))
    return web.json_response({"customer_name": s.customer_name, "workflow": s.workflow_id,
                              "loan": loan.data if loan.data.get("ok") else None,
                              "card": card.data if card.data.get("ok") else None})


async def api_activity(request):
    s = _session(request)
    reqs = await _direct_tool(request, s, "list_service_requests")
    audit = request.app["actions"].audit.for_customer(s.customer_id, 30)
    return web.json_response({"requests": reqs.data.get("requests", []), "audit": audit})


# ------------------------------------------------------------------ text chat
async def ws_chat(request):
    s = _session(request)
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)

    async def emit(ev: dict):
        if not ws.closed:
            await ws.send_str(json.dumps(ev, default=str))

    async for msg in ws:
        if msg.type != web.WSMsgType.TEXT:
            continue
        data = json.loads(msg.data)
        if data.get("type") == "user" and data.get("text", "").strip():
            text = data["text"].strip()[:2000]
            async with request.app["locks"][s.id]:
                await emit({"type": "thinking"})
                await request.app["agent"].turn(s, text, emit)
    return ws


# ------------------------------------------------------------------ voice (browser)
async def ws_voice(request):
    s = _session(request)
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=0)
    await ws.prepare(request)
    if not config.GEMINI_API_KEY:
        await ws.send_str(json.dumps({"type": "error", "text": "Voice needs GEMINI_API_KEY on the server."}))
        await ws.close()
        return ws
    conv = VoiceConversation(s, request.app["workflows"][s.workflow_id], BrowserTransport(ws),
                             request.app["connectors"], request.app["actions"])
    await conv.run()
    if not ws.closed:
        await ws.close()
    return ws


# ------------------------------------------------------------------ phone (Vobiz / Twilio)
def _ws_base(request: web.Request) -> str:
    if config.PUBLIC_URL:
        return re.sub(r"^http", "ws", config.PUBLIC_URL) + "/stream"
    proto = request.headers.get("X-Forwarded-Proto", "https" if request.secure else "http")
    host = request.headers.get("X-Forwarded-Host") or request.host
    return f"{'wss' if proto == 'https' else 'ws'}://{host}/stream"


async def answer(request):
    phone = telephony.get_provider()
    form = await request.post()
    caller = str(form.get("From") or form.get("CallerName") or "unknown")
    caller = re.sub(r"[^0-9A-Za-z_+\-]", "", caller.split("sip:")[-1].split("@")[0].replace("+", ""))
    print(f"[answer] provider={phone.name} caller={mask_phone(caller)}", flush=True)
    return web.Response(text=phone.answer_xml(_ws_base(request), caller or "unknown"), content_type="text/xml")


async def stream(request):
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=0)
    await ws.prepare(request)
    provider = telephony.get_provider()
    caller = re.sub(r"[^0-9]", "", request.query.get("caller_id", ""))
    token = request.query.get("token", "")
    transport = PhoneTransport(ws, provider, CallRecorder() if config.RECORD_CALLS else None)
    if provider.needs_start:
        start = await _wait_for_start(ws, provider)
        if start is None:
            await ws.close()
            return ws
        transport.stream_id = start.get("streamId", "")
        params = start.get("customParameters") or {}
        caller = re.sub(r"[^0-9]", "", params.get("caller_id", caller))
        token = params.get("token", token)
    if config.STREAM_TOKEN and token != config.STREAM_TOKEN:
        print("[stream] bad token — rejecting", flush=True)
        await ws.close()
        return ws
    s = request.app["sessions"].create(config.DEFAULT_WORKFLOW, "phone")
    s.phone = common_tools.normalise_phone(caller)
    conv = VoiceConversation(s, request.app["workflows"][s.workflow_id], transport,
                             request.app["connectors"], request.app["actions"])
    await conv.run()
    if not ws.closed:
        await ws.close()
    if transport.recorder:
        await asyncio.to_thread(_save_recording, transport.recorder, s)
    request.app["sessions"].drop(s.id)
    return ws


async def _wait_for_start(ws, provider, timeout: float = 10.0):
    try:
        async with asyncio.timeout(timeout):
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    kind, payload = provider.parse(json.loads(msg.data))
                    if kind == "start":
                        return payload
    except TimeoutError:
        pass
    return None


def _save_recording(recorder, s) -> None:
    try:
        config.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        path = config.RECORDINGS_DIR / f"call_{time.strftime('%Y%m%d_%H%M%S')}_{s.customer_id or 'unverified'}.wav"
        recorder.write_wav(path)
        if config.GCS_RECORDINGS_BUCKET:
            from google.cloud import storage
            storage.Client().bucket(config.GCS_RECORDINGS_BUCKET).blob(f"recordings/{path.name}").upload_from_filename(str(path))
            path.unlink(missing_ok=True)
        print(f"[recording] saved {path.name}", flush=True)
    except Exception as e:
        print(f"[recording] failed: {e!r}", flush=True)


# ------------------------------------------------------------------ ops
async def api_metrics(request):
    out = METRICS.summary()
    out["breakers"] = request.app["connectors"].health()
    out["active_sessions"] = len(request.app["sessions"])
    return web.json_response(out)


async def api_metrics_reset(request):
    if not admin_ok(request):
        raise web.HTTPForbidden()
    METRICS.reset()
    return web.json_response({"ok": True})


async def api_faults(request):
    ms: MockSystems = request.app["mocks"]
    if request.method == "POST":
        if not admin_ok(request):
            raise web.HTTPForbidden()
        body = await request.json()
        if body.get("reset"):
            from ledgerflow.mocks.server import DEFAULT_PROFILES
            ms.profiles = json.loads(json.dumps(DEFAULT_PROFILES))
        else:
            for system, prof in body.items():
                if system in ms.profiles and isinstance(prof, dict):
                    for k in ("p50", "p95", "error_rate", "down"):
                        if k in prof:
                            ms.profiles[system][k] = prof[k]
    return web.json_response({"profiles": ms.profiles, "breakers": request.app["connectors"].health()})


async def health(request):
    return web.json_response({"status": "ok", "as_of": config.AS_OF.isoformat(),
                              "gemini_key_set": bool(config.GEMINI_API_KEY), "text_model": config.GEMINI_TEXT_MODEL,
                              "live_model": config.GEMINI_LIVE_MODEL, "telephony": config.TELEPHONY_PROVIDER,
                              "public_url": config.PUBLIC_URL, "workflows": list(request.app["workflows"])})


@web.middleware
async def security_headers(request, handler):
    resp = await handler(request)
    if isinstance(resp, web.StreamResponse) and not isinstance(resp, web.WebSocketResponse):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    return resp


def make_app() -> web.Application:
    app = web.Application(middlewares=[security_headers])
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()   # synthetic data is re-seeded on every start (deterministic)
    app["mocks"] = MockSystems(str(config.DB_PATH), config.AS_OF)
    app.add_subapp("/mock", build_mock_app(app["mocks"]))

    r = app.router
    r.add_get("/", index)
    r.add_static("/static", STATIC)
    r.add_get("/api/config", api_config)
    r.add_post("/api/login", api_login)
    r.add_post("/api/logout", api_logout)
    r.add_post("/api/workflow", api_workflow)
    r.add_get("/api/account", api_account)
    r.add_get("/api/activity", api_activity)
    r.add_get("/api/metrics", api_metrics)
    r.add_post("/api/metrics/reset", api_metrics_reset)
    r.add_get("/api/faults", api_faults)
    r.add_post("/api/faults", api_faults)
    r.add_get("/ws/chat", ws_chat)
    r.add_get("/ws/voice", ws_voice)
    r.add_post("/answer", answer)
    r.add_get("/stream", stream)
    r.add_get("/health", health)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=config.PORT, print=None)
