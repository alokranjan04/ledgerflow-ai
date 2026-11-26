"""LedgerFlow AI — one aiohttp service.

Web UI      GET  /                      static single-page app
Session     POST /api/login             phone + DOB -> verified session (HttpOnly cookie)
            POST /api/logout            end the session
            POST /api/workflow          switch workflow (loans <-> cards), identity kept
            GET  /api/account           account panel data
            GET  /api/activity          service requests + audit trail
Text        GET  /ws/chat               chat WebSocket (events: tool progress, cards, trace)
Voice       GET  /ws/voice              browser mic <-> Gemini Live
Phone       POST /answer, GET /stream   Vobiz / Twilio media stream <-> Gemini Live
Ops         GET  /api/metrics, /api/faults, /healthz, /readyz, /health
Mocks       /mock/<system>/...          simulated enterprise systems; internal callers only
"""
from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import sys
import time
import uuid
from collections import defaultdict
from urllib.parse import urlparse

from aiohttp import web

from ledgerflow import config
from ledgerflow.domains import cards as cards_domain
from ledgerflow.domains import loans as loans_domain
from ledgerflow.mocks.server import DEFAULT_PROFILES, MockSystems, build_mock_app
from ledgerflow.platform import common_tools, logs
from ledgerflow.platform.actions import ActionService, AuditLog
from ledgerflow.platform.connectors import DEFAULT_POLICIES, ConnectorError, ConnectorRegistry
from ledgerflow.platform.gemini_text import GeminiREST, TextAgent
from ledgerflow.platform.ratelimit import RateLimiter
from ledgerflow.platform.sessions import SessionStore
from ledgerflow.platform.telemetry import METRICS, Trace
from ledgerflow.platform.tools import REGISTRY, ToolContext
from ledgerflow.platform.workflow import load_workflows
from ledgerflow.voice import telephony
from ledgerflow.voice.pipeline import BrowserTransport, PhoneTransport, VoiceConversation
from ledgerflow.voice.recorder import CallRecorder

for stream_ in (sys.stdout, sys.stderr):
    if hasattr(stream_, "reconfigure"):
        stream_.reconfigure(encoding="utf-8", errors="replace")

log = logs.get("ledgerflow.app")
STATIC = config.ROOT / "frontend"
COOKIE = "lf_sid"
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
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self' ws: wss:; media-src 'self' blob:; frame-ancestors 'self'; base-uri 'none'; form-action 'self'")

# Brute-force protection across sessions: phone -> failure timestamps.
LOGIN_FAILURES: dict[str, list[float]] = defaultdict(list)
LOGIN_WINDOW_S, LOGIN_MAX_FAILURES = 900, 5

APP_KEYS = ("connectors", "actions", "workflows", "sessions", "llm", "agent", "locks", "chat_limit", "voice_limit")


def mask_phone(p: str) -> str:
    return logs.mask_phone(p)


def admin_ok(request: web.Request) -> bool:
    """Admin calls need the token when one is set; a deployed service without a
    token refuses them outright (open only for local development)."""
    if config.ADMIN_TOKEN:
        return hmac.compare_digest(request.headers.get("X-Admin-Token", ""), config.ADMIN_TOKEN)
    return not config.deployed()


def _is_https(request: web.Request) -> bool:
    return request.headers.get("X-Forwarded-Proto", "https" if request.secure else "http") == "https"


async def on_startup(app: web.Application) -> None:
    app["connectors"] = ConnectorRegistry(config.MOCK_BASE_URL, DEFAULT_POLICIES,
                                          headers={"X-Internal-Token": app["internal_token"]})
    actions = ActionService(AuditLog(config.AUDIT_DB_PATH))
    common_tools.register_common_actions(actions)
    loans_domain.register_actions(actions)
    cards_domain.register_actions(actions)
    app["actions"] = actions
    app["workflows"] = load_workflows()
    app["sessions"] = SessionStore(config.SESSION_TTL_SECS)
    app["llm"] = GeminiREST(config.GEMINI_API_KEY, config.GEMINI_TEXT_MODEL, [config.GEMINI_FALLBACK_MODEL])
    app["agent"] = TextAgent(app["llm"], app["workflows"], app["connectors"], actions)
    app["locks"] = defaultdict(asyncio.Lock)
    app["chat_limit"] = RateLimiter(config.CHAT_TURNS_PER_MIN)
    app["voice_limit"] = RateLimiter(config.VOICE_STARTS_PER_MIN)
    logs.event(log, "ready", port=config.PORT, as_of=config.AS_OF.isoformat(), workflows=list(app["workflows"]),
               text_model=config.GEMINI_TEXT_MODEL, fallback_model=config.GEMINI_FALLBACK_MODEL,
               gemini_key_set=bool(config.GEMINI_API_KEY), admin_token_set=bool(config.ADMIN_TOKEN))


async def on_cleanup(app: web.Application) -> None:
    await app["connectors"].close()
    await app["llm"].close()


def _session(request: web.Request):
    """Session from the HttpOnly cookie (browser) or X-Session header (API clients).
    Never from the query string: URLs end up in proxy and access logs."""
    sid = request.cookies.get(COOKIE) or request.headers.get("X-Session")
    s = request.app["sessions"].get(sid)
    if s is None:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "session expired"}), content_type="application/json")
    logs.bind(session=logs.session_hash(s.id), customer=s.customer_id)
    return s


def _check_origin(request: web.Request) -> None:
    """Refuse cross-site WebSocket handshakes (CSWSH)."""
    origin = request.headers.get("Origin")
    if origin:
        host = request.headers.get("X-Forwarded-Host") or request.host
        if urlparse(origin).netloc != host:
            raise web.HTTPForbidden(text="cross-origin WebSocket refused")


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
    })


async def api_login(request):
    body = await request.json()
    phone = common_tools.normalise_phone(str(body.get("phone", "")))
    now = time.time()
    LOGIN_FAILURES[phone] = [t for t in LOGIN_FAILURES[phone] if now - t < LOGIN_WINDOW_S]
    if len(LOGIN_FAILURES[phone]) >= LOGIN_MAX_FAILURES:
        logs.event(log, "login locked", level=30, phone=mask_phone(phone))
        return web.json_response({"ok": False, "error": "Too many attempts. Try again in 15 minutes."}, status=429)
    wf = body.get("workflow") or config.DEFAULT_WORKFLOW
    if wf not in request.app["workflows"]:
        return web.json_response({"ok": False, "error": "unknown workflow"}, status=400)
    s = request.app["sessions"].create(wf, "web")
    res = await common_tools.verify(s, request.app["connectors"], phone, str(body.get("dob", "")))
    if not res.get("ok"):
        LOGIN_FAILURES[phone].append(now)
        request.app["sessions"].drop(s.id)
        METRICS.incr("auth.login_failed")
        logs.event(log, "login failed", level=30, phone=mask_phone(phone))
        return web.json_response({"ok": False, "error": res.get("error", "Verification failed")}, status=401)
    METRICS.incr("auth.login_ok")
    logs.event(log, "login verified", session=logs.session_hash(s.id), customer=s.customer_id)
    resp = web.json_response({"ok": True, "customer_name": s.customer_name, "workflow": s.workflow_id})
    resp.set_cookie(COOKIE, s.id, httponly=True, secure=_is_https(request), samesite="Strict", path="/",
                    max_age=config.SESSION_TTL_SECS)
    return resp


async def api_logout(request):
    sid = request.cookies.get(COOKIE) or request.headers.get("X-Session")
    if sid:
        request.app["sessions"].drop(sid)
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


async def api_workflow(request):
    s = _session(request)
    body = await request.json()
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
    _check_origin(request)
    s = _session(request)
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=64 * 1024)
    await ws.prepare(request)

    async def emit(ev: dict):
        if not ws.closed:
            await ws.send_str(json.dumps(ev, default=str))

    async for msg in ws:
        if msg.type != web.WSMsgType.TEXT:
            continue
        try:
            data = json.loads(msg.data)
        except ValueError:
            continue
        if data.get("type") == "user" and str(data.get("text", "")).strip():
            if not request.app["chat_limit"].allow(s.id):
                METRICS.incr("ratelimit.chat")
                await emit({"type": "error", "text": "You're sending messages very quickly — please wait a moment."})
                continue
            text = str(data["text"]).strip()[:2000]
            async with request.app["locks"][s.id]:
                await emit({"type": "thinking"})
                await request.app["agent"].turn(s, text, emit)
    return ws


# ------------------------------------------------------------------ voice (browser)
async def ws_voice(request):
    _check_origin(request)
    s = _session(request)
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1024 * 1024)
    await ws.prepare(request)
    if not config.GEMINI_API_KEY:
        await ws.send_str(json.dumps({"type": "error", "text": "Voice needs GEMINI_API_KEY on the server."}))
        await ws.close()
        return ws
    if not request.app["voice_limit"].allow(s.id):
        METRICS.incr("ratelimit.voice")
        await ws.send_str(json.dumps({"type": "error", "text": "Too many voice sessions — please wait a minute."}))
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
    logs.event(log, "phone answer", provider=phone.name, caller=mask_phone(caller))
    return web.Response(text=phone.answer_xml(_ws_base(request), caller or "unknown"), content_type="text/xml")


async def stream(request):
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1024 * 1024)
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
    if config.STREAM_TOKEN and not hmac.compare_digest(token, config.STREAM_TOKEN):
        logs.event(log, "phone stream rejected: bad token", level=30)
        await ws.close()
        return ws
    s = request.app["sessions"].create(config.DEFAULT_WORKFLOW, "phone")
    s.phone = common_tools.normalise_phone(caller)
    logs.bind(session=logs.session_hash(s.id), channel="phone")
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
        logs.event(log, "recording saved", file=path.name)
    except Exception as e:
        logs.event(log, "recording failed", level=40, error=repr(e))


# ------------------------------------------------------------------ ops
async def api_metrics(request):
    out = METRICS.summary()
    out["breakers"] = request.app["connectors"].health()
    out["llm_breakers"] = {m: b.state for m, b in request.app["llm"].breakers.items()}
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
            raise web.HTTPForbidden(text=json.dumps({"error": "admin token required"}), content_type="application/json")
        body = await request.json()
        if body.get("reset"):
            ms.profiles = json.loads(json.dumps(DEFAULT_PROFILES))
        else:
            for system, prof in body.items():
                if system in ms.profiles and isinstance(prof, dict):
                    for k in ("p50", "p95", "error_rate", "down"):
                        if k in prof:
                            ms.profiles[system][k] = prof[k]
        logs.event(log, "fault profile changed", level=30, change=body)
    return web.json_response({"profiles": ms.profiles, "breakers": request.app["connectors"].health(),
                              "admin": admin_ok(request)})


async def healthz(request):
    return web.json_response({"status": "ok"})


async def readyz(request):
    """Ready when the systems answer and the audit store is writable."""
    checks = {}
    try:
        await request.app["connectors"]["crm"].get("/lookup", {"phone": "0000000000"})
        checks["systems"] = "ok"
    except ConnectorError as e:
        checks["systems"] = "ok" if e.kind == "not_found" else e.kind
    try:
        request.app["actions"].audit.conn.execute("SELECT 1")
        checks["audit_store"] = "ok"
    except Exception as e:  # pragma: no cover - defensive
        checks["audit_store"] = type(e).__name__
    checks["llm_key"] = "ok" if config.GEMINI_API_KEY else "missing"
    ready = checks["systems"] == "ok" and checks["audit_store"] == "ok"
    return web.json_response({"ready": ready, "checks": checks}, status=200 if ready else 503)


async def health(request):
    return web.json_response({"status": "ok", "as_of": config.AS_OF.isoformat(),
                              "gemini_key_set": bool(config.GEMINI_API_KEY), "text_model": config.GEMINI_TEXT_MODEL,
                              "fallback_model": config.GEMINI_FALLBACK_MODEL, "live_model": config.GEMINI_LIVE_MODEL,
                              "telephony": config.TELEPHONY_PROVIDER, "workflows": list(request.app["workflows"])})


# ------------------------------------------------------------------ middleware
@web.middleware
async def request_context(request, handler):
    """Correlation IDs on every log line, and one structured access line per request
    (path only — never the query string)."""
    rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:16]
    trace = (request.headers.get("X-Cloud-Trace-Context") or "").split("/")[0] or None
    token = logs.bind(request_id=rid, trace=trace)
    t0 = time.perf_counter()
    status = 500
    try:
        resp = await handler(request)
        status = getattr(resp, "status", 200)
        return resp
    except web.HTTPException as e:
        status = e.status
        raise
    finally:
        if not request.path.startswith(("/static", "/mock", "/healthz")):
            logs.event(log, "request", method=request.method, path=request.path, status=status,
                       ms=round((time.perf_counter() - t0) * 1000, 1))
        logs.reset(token)


@web.middleware
async def security_headers(request, handler):
    resp = await handler(request)
    if isinstance(resp, web.StreamResponse) and not isinstance(resp, web.WebSocketResponse):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("Permissions-Policy", "microphone=(self), camera=(), geolocation=()")
        if _is_https(request):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
    return resp


def make_app() -> web.Application:
    logs.setup(config.LOG_LEVEL, config.LOG_JSON, config.GCP_PROJECT)
    app = web.Application(middlewares=[request_context, security_headers], client_max_size=256 * 1024)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()   # synthetic data is re-seeded on every start (deterministic)
    # Only in-process connectors know this token, so the simulated systems are not
    # reachable from the internet even though they share the service's port.
    app["internal_token"] = secrets.token_urlsafe(24)
    app["mocks"] = MockSystems(str(config.DB_PATH), config.AS_OF)
    app.add_subapp("/mock", build_mock_app(app["mocks"], internal_token=app["internal_token"]))

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
    r.add_get("/healthz", healthz)
    r.add_get("/readyz", readyz)
    r.add_get("/health", health)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=config.PORT, print=None, access_log=None)
