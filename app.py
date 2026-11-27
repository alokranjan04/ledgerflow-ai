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

from ledgerflow import appkeys as K
from ledgerflow import config
from ledgerflow.domains import cards as cards_domain
from ledgerflow.domains import loans as loans_domain
from ledgerflow.mocks.personas import demo_customers, load_personas
from ledgerflow.mocks.seed import CUSTOMERS_FILE
from ledgerflow.mocks.server import DEFAULT_PROFILES, MockSystems, build_mock_app
from ledgerflow.platform import common_tools, logs
from ledgerflow.platform.actions import ActionService, AuditLog
from ledgerflow.platform.connectors import DEFAULT_POLICIES, ConnectorError, ConnectorRegistry
from ledgerflow.platform.durable import build_store
from ledgerflow.platform.gcp_auth import IdTokenProvider
from ledgerflow.platform.gemini_text import GeminiREST, TextAgent
from ledgerflow.platform.language import LANGUAGES
from ledgerflow.platform.state import build_state
from ledgerflow.platform.stepup import DemoSms, StepUp
from ledgerflow.platform.telemetry import METRICS, Trace, kpis
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
# Sign-in personas come from data/customers.yaml (the same file seeds the mock systems).
DEMO_CUSTOMERS = demo_customers(load_personas(CUSTOMERS_FILE))
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self' ws: wss:; media-src 'self' blob:; frame-ancestors 'self'; base-uri 'none'; form-action 'self'")



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
    auth = IdTokenProvider(config.SYSTEMS_AUDIENCE) if config.SYSTEMS_AUDIENCE else None
    app[K.CONNECTORS] = ConnectorRegistry(config.MOCK_BASE_URL, DEFAULT_POLICIES,
                                          headers={"X-Internal-Token": app[K.INTERNAL_TOKEN]}, auth=auth)
    state = build_state(app.get(K.REDIS_CLIENT))
    app[K.STATE] = state
    app[K.SESSIONS] = state.sessions
    app[K.CHAT_LIMIT] = state.chat_limit
    app[K.VOICE_LIMIT] = state.voice_limit
    app[K.LOGIN_GUARD] = state.login_guard
    step_up = StepUp(_sms_sender(), config.STEP_UP_VALID_SECS) if config.STEP_UP_OTP else None
    audit = AuditLog(config.AUDIT_DB_PATH, durable=app[K.DURABLE])
    await _restore(app, audit)
    actions = ActionService(audit, step_up=step_up)
    common_tools.register_common_actions(actions)
    loans_domain.register_actions(actions)
    cards_domain.register_actions(actions)
    app[K.ACTIONS] = actions
    app[K.WORKFLOWS] = load_workflows()
    app[K.LLM] = GeminiREST(config.GEMINI_API_KEY, config.GEMINI_TEXT_MODEL, [config.GEMINI_FALLBACK_MODEL])
    app[K.AGENT] = TextAgent(app[K.LLM], app[K.WORKFLOWS], app[K.CONNECTORS], actions)
    app[K.LOCKS] = defaultdict(asyncio.Lock)
    logs.event(log, "ready", role=config.SERVICE_ROLE, state_backend=state.backend, durable=app[K.DURABLE].backend, port=config.PORT, as_of=config.AS_OF.isoformat(), workflows=list(app[K.WORKFLOWS]),
               text_model=config.GEMINI_TEXT_MODEL, fallback_model=config.GEMINI_FALLBACK_MODEL,
               gemini_key_set=bool(config.GEMINI_API_KEY), admin_token_set=bool(config.ADMIN_TOKEN))


async def _restore(app: web.Application, audit: AuditLog | None) -> None:
    """Bring back service requests and the audit trail from the durable store (Firestore)
    before serving. A failure is logged loudly but does not stop the service: nothing in
    Firestore is lost, and new records are still mirrored."""
    durable = app[K.DURABLE]
    if not durable.enabled:
        return
    try:
        tickets = await app[K.MOCKS].restore(durable) if K.MOCKS in app else 0
        trail = await audit.restore() if audit is not None else 0
        logs.event(log, "durable records restored", backend=durable.backend, requests=tickets, audit_events=trail)
    except Exception as e:  # noqa: BLE001
        logs.event(log, "durable restore failed", level=40, backend=durable.backend, error=str(e)[:300])


def _durable():
    return build_store(config.DURABLE_STORE, config.GCP_PROJECT, config.FIRESTORE_DATABASE, config.FIRESTORE_PREFIX)


def _sms_sender():
    if config.SMS_PROVIDER == "demo":
        return DemoSms()
    raise SystemExit(f"SMS_PROVIDER={config.SMS_PROVIDER!r} has no adapter yet; use 'demo' or add one in stepup.py")


async def on_cleanup(app: web.Application) -> None:
    await app[K.CONNECTORS].close()
    await app[K.LLM].close()
    await app[K.STATE].close()
    await app[K.DURABLE].close()


async def _session(request: web.Request):
    """Session from the HttpOnly cookie (browser) or X-Session header (API clients).
    Never from the query string: URLs end up in proxy and access logs."""
    sid = request.cookies.get(COOKIE) or request.headers.get("X-Session")
    s = await request.app[K.SESSIONS].get(sid)
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
    wfs = request.app[K.WORKFLOWS]
    return web.json_response({
        "as_of": config.AS_OF.isoformat(),
        "workflows": [{"id": w.id, "name": w.name, "description": w.description, "panels": w.panels,
                       "suggestions": w.suggestions, "suggestions_hi": w.suggestions_hi}
                      for w in sorted(wfs.values(), key=lambda w: w.id != config.DEFAULT_WORKFLOW)],
        "default_workflow": config.DEFAULT_WORKFLOW,
        "demo_customers": DEMO_CUSTOMERS,
        "voice_enabled": bool(config.GEMINI_API_KEY),
        "languages": [{"code": "auto", "name": "Auto"}] + [{"code": k, "name": v} for k, v in LANGUAGES.items()],
    })


async def api_login(request):
    body = await request.json()
    phone = common_tools.normalise_phone(str(body.get("phone", "")))
    guard = request.app[K.LOGIN_GUARD]
    if await guard.locked(phone):
        logs.event(log, "login locked", level=30, phone=mask_phone(phone))
        return web.json_response({"ok": False, "error": "Too many attempts. Try again in 15 minutes."}, status=429)
    wf = body.get("workflow") or config.DEFAULT_WORKFLOW
    if wf not in request.app[K.WORKFLOWS]:
        return web.json_response({"ok": False, "error": "unknown workflow"}, status=400)
    s = await request.app[K.SESSIONS].create(wf, "web")
    res = await common_tools.verify(s, request.app[K.CONNECTORS], phone, str(body.get("dob", "")))
    if not res.get("ok"):
        await guard.record_failure(phone)
        await request.app[K.SESSIONS].drop(s.id)
        METRICS.incr("auth.login_failed")
        logs.event(log, "login failed", level=30, phone=mask_phone(phone))
        return web.json_response({"ok": False, "error": res.get("error", "Verification failed")}, status=401)
    await request.app[K.SESSIONS].save(s)
    METRICS.incr("auth.login_ok")
    logs.event(log, "login verified", session=logs.session_hash(s.id), customer=s.customer_id)
    if body.get("language") in LANGUAGES:
        s.language = body["language"]
        await request.app[K.SESSIONS].save(s)
    resp = web.json_response({"ok": True, "customer_name": s.customer_name, "workflow": s.workflow_id,
                              "language": s.language})
    resp.set_cookie(COOKIE, s.id, httponly=True, secure=_is_https(request), samesite="Strict", path="/",
                    max_age=config.SESSION_TTL_SECS)
    return resp


async def api_logout(request):
    sid = request.cookies.get(COOKIE) or request.headers.get("X-Session")
    if sid:
        await request.app[K.SESSIONS].drop(sid)
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


async def api_workflow(request):
    s = await _session(request)
    body = await request.json()
    wf = body.get("workflow")
    if wf not in request.app[K.WORKFLOWS]:
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
        await request.app[K.SESSIONS].save(s)
    return web.json_response({"ok": True, "workflow": s.workflow_id})


async def api_language(request):
    """Set the conversation language explicitly (the UI toggle); 'auto' re-enables detection."""
    s = await _session(request)
    body = await request.json()
    code = str(body.get("language", ""))
    if code not in LANGUAGES and code != "auto":
        return web.json_response({"ok": False, "error": "unsupported language"}, status=400)
    s.language = code
    await request.app[K.SESSIONS].save(s)
    return web.json_response({"ok": True, "language": s.language})


async def _direct_tool(request, s, name: str):
    ctx = ToolContext(s, request.app[K.CONNECTORS], request.app[K.ACTIONS], Trace(), config.AS_OF)
    return await REGISTRY.run(name, {}, ctx, [name])


async def api_account(request):
    s = await _session(request)
    loan, card = await asyncio.gather(_direct_tool(request, s, "get_loan_summary"),
                                      _direct_tool(request, s, "get_card_summary"))
    return web.json_response({"customer_name": s.customer_name, "workflow": s.workflow_id,
                              "loan": loan.data if loan.data.get("ok") else None,
                              "card": card.data if card.data.get("ok") else None})


async def api_activity(request):
    s = await _session(request)
    reqs = await _direct_tool(request, s, "list_service_requests")
    audit = request.app[K.ACTIONS].audit.for_customer(s.customer_id, 30)
    return web.json_response({"requests": reqs.data.get("requests", []), "audit": audit})


# ------------------------------------------------------------------ text chat
async def ws_chat(request):
    _check_origin(request)
    s = await _session(request)
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
            if not await request.app[K.CHAT_LIMIT].hit(s.id):
                METRICS.incr("ratelimit.chat")
                await emit({"type": "error", "text": "You're sending messages very quickly — please wait a moment."})
                continue
            text = str(data["text"]).strip()[:2000]
            async with request.app[K.LOCKS][s.id]:
                await emit({"type": "thinking"})
                await request.app[K.AGENT].turn(s, text, emit)
                await request.app[K.SESSIONS].save(s)      # durable after every turn
    return ws


# ------------------------------------------------------------------ voice (browser)
async def ws_voice(request):
    _check_origin(request)
    s = await _session(request)
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1024 * 1024)
    await ws.prepare(request)
    if not config.GEMINI_API_KEY:
        await ws.send_str(json.dumps({"type": "error", "text": "Voice needs GEMINI_API_KEY on the server."}))
        await ws.close()
        return ws
    if not await request.app[K.VOICE_LIMIT].hit(s.id):
        METRICS.incr("ratelimit.voice")
        await ws.send_str(json.dumps({"type": "error", "text": "Too many voice sessions — please wait a minute."}))
        await ws.close()
        return ws
    conv = VoiceConversation(s, request.app[K.WORKFLOWS][s.workflow_id], BrowserTransport(ws),
                             request.app[K.CONNECTORS], request.app[K.ACTIONS])
    try:
        await conv.run()
    finally:
        await request.app[K.SESSIONS].save(s)          # voice transcript + actions become durable
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
    s = await request.app[K.SESSIONS].create(config.DEFAULT_WORKFLOW, "phone")
    s.phone = common_tools.normalise_phone(caller)
    logs.bind(session=logs.session_hash(s.id), channel="phone")
    conv = VoiceConversation(s, request.app[K.WORKFLOWS][s.workflow_id], transport,
                             request.app[K.CONNECTORS], request.app[K.ACTIONS])
    await conv.run()
    if not ws.closed:
        await ws.close()
    if transport.recorder:
        await asyncio.to_thread(_save_recording, transport.recorder, s)
    await request.app[K.SESSIONS].drop(s.id)
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
    if not admin_ok(request):      # business volumes and KPIs are not public on a deployed service
        raise web.HTTPForbidden(text=json.dumps({"error": "admin token required"}), content_type="application/json")
    out = METRICS.summary()
    out["breakers"] = request.app[K.CONNECTORS].health()
    out["llm_breakers"] = {m: b.state for m, b in request.app[K.LLM].breakers.items()}
    out["active_sessions"] = await request.app[K.SESSIONS].count()
    out["state_backend"] = request.app[K.STATE].backend
    out["kpis"] = kpis()
    out["role"] = config.SERVICE_ROLE
    return web.json_response(out)


async def api_metrics_reset(request):
    if not admin_ok(request):
        raise web.HTTPForbidden()
    METRICS.reset()
    return web.json_response({"ok": True})


async def api_faults(request):
    """Fault injection for the demo. Local systems in "all"; proxied to the systems
    service (same admin API, internal credentials) in "gateway"."""
    body = None
    if request.method == "POST":
        if not admin_ok(request):
            raise web.HTTPForbidden(text=json.dumps({"error": "admin token required"}), content_type="application/json")
        body = await request.json()
        logs.event(log, "fault profile changed", level=30, change=body)
    if K.MOCKS in request.app:
        ms: MockSystems = request.app[K.MOCKS]
        if body is not None:
            if body.get("reset"):
                ms.profiles = json.loads(json.dumps(DEFAULT_PROFILES))
            else:
                for system, prof in body.items():
                    if system in ms.profiles and isinstance(prof, dict):
                        for k in ("p50", "p95", "error_rate", "down"):
                            if k in prof:
                                ms.profiles[system][k] = prof[k]
        profiles = ms.profiles
    else:
        admin = request.app[K.CONNECTORS].admin
        try:
            if body is None:
                profiles = await admin.get("/faults")
            elif body.get("reset"):
                profiles = await admin.post("/faults/reset", {})
            else:
                profiles = await admin.post("/faults", body)
        except ConnectorError as e:
            return web.json_response({"error": f"systems service: {e.kind}"}, status=502)
    return web.json_response({"profiles": profiles, "breakers": request.app[K.CONNECTORS].health(),
                              "admin": admin_ok(request)})


async def dashboard_page(request):
    return web.FileResponse(STATIC / "dashboard.html", headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------------ agent desk (hand-offs)
async def agent_page(request):
    return web.FileResponse(STATIC / "agent.html", headers={"Cache-Control": "no-cache"})


async def api_agent_queue(request):
    if not admin_ok(request):
        raise web.HTTPForbidden(text=json.dumps({"error": "admin token required"}), content_type="application/json")
    try:
        return web.json_response(await request.app[K.CONNECTORS]["ops"].get("/queue", {"type": "agent_handoff"}))
    except ConnectorError as e:
        return web.json_response({"error": f"service ops: {e.kind}"}, status=502)


async def api_agent_accept(request):
    if not admin_ok(request):
        raise web.HTTPForbidden(text=json.dumps({"error": "admin token required"}), content_type="application/json")
    body = await request.json()
    tid = str(body.get("id", ""))
    if not re.fullmatch(r"SR-[A-Z0-9]{8}", tid):
        return web.json_response({"error": "bad request id"}, status=400)
    try:
        row = await request.app[K.CONNECTORS]["ops"].post(f"/requests/{tid}/accept", {"agent": str(body.get("agent", ""))[:60]})
    except ConnectorError as e:
        return web.json_response({"error": f"service ops: {e.kind}"}, status=404 if e.kind == "not_found" else 502)
    logs.event(log, "hand-off accepted", request_id=tid)
    return web.json_response(row)


async def healthz(request):
    return web.json_response({"status": "ok"})


async def readyz(request):
    """Ready when the systems answer and the audit store is writable."""
    checks = {}
    try:
        await request.app[K.CONNECTORS]["crm"].get("/lookup", {"phone": "0000000000"})
        checks["systems"] = "ok"
    except ConnectorError as e:
        checks["systems"] = "ok" if e.kind == "not_found" else e.kind
    try:
        request.app[K.ACTIONS].audit.conn.execute("SELECT 1")
        checks["audit_store"] = "ok"
    except Exception as e:  # pragma: no cover - defensive
        checks["audit_store"] = type(e).__name__
    if request.app[K.STATE].backend == "redis":
        try:
            await request.app[K.STATE].sessions.r.ping()
            checks["state"] = "ok"
        except Exception as e:
            checks["state"] = type(e).__name__
    checks["llm_key"] = "ok" if config.GEMINI_API_KEY else "missing"
    checks["durable"] = request.app[K.DURABLE].backend          # informational: none | firestore
    ready = all(checks[k] == "ok" for k in ("systems", "audit_store")) and checks.get("state", "ok") == "ok"
    return web.json_response({"ready": ready, "checks": checks}, status=200 if ready else 503)


async def health(request):
    return web.json_response({"status": "ok", "as_of": config.AS_OF.isoformat(),
                              "gemini_key_set": bool(config.GEMINI_API_KEY), "text_model": config.GEMINI_TEXT_MODEL,
                              "fallback_model": config.GEMINI_FALLBACK_MODEL, "live_model": config.GEMINI_LIVE_MODEL,
                              "telephony": config.TELEPHONY_PROVIDER, "workflows": list(request.app[K.WORKFLOWS])})


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


def _systems_app() -> web.Application:
    """SERVICE_ROLE=systems: only the simulated enterprise systems, internal callers only."""
    if not config.INTERNAL_TOKEN:
        raise SystemExit("SERVICE_ROLE=systems needs INTERNAL_TOKEN (shared with the gateway)")
    app = web.Application(middlewares=[request_context, security_headers])
    ms = MockSystems(str(config.DB_PATH), config.AS_OF)
    app[K.MOCKS] = ms
    app[K.DURABLE] = _durable()

    async def restore(app):
        await _restore(app, None)

    async def close(app):
        await app[K.DURABLE].close()
    app.on_startup.append(restore)
    app.on_cleanup.append(close)
    app.add_subapp("/mock", build_mock_app(ms, internal_token=config.INTERNAL_TOKEN))

    async def ready(request):
        return web.json_response({"ready": True, "role": "systems"})
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", ready)
    logs.event(log, "ready", role="systems", port=config.PORT, as_of=config.AS_OF.isoformat())
    return app


def make_app() -> web.Application:
    logs.setup(config.LOG_LEVEL, config.LOG_JSON, config.GCP_PROJECT)
    if config.SERVICE_ROLE not in ("all", "gateway", "systems"):
        raise SystemExit(f"SERVICE_ROLE must be all | gateway | systems, not {config.SERVICE_ROLE!r}")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if config.SERVICE_ROLE in ("all", "systems") and config.DB_PATH.exists():
        config.DB_PATH.unlink()   # synthetic data is re-seeded on every start (deterministic)
    if config.SERVICE_ROLE == "systems":
        return _systems_app()

    app = web.Application(middlewares=[request_context, security_headers], client_max_size=256 * 1024)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    # The token on every connector call. In "all" it is random per process, so the
    # co-located systems answer only this process; in "gateway" it is the secret
    # shared with the systems service.
    app[K.INTERNAL_TOKEN] = config.INTERNAL_TOKEN or secrets.token_urlsafe(24)
    app[K.DURABLE] = _durable()
    if config.SERVICE_ROLE == "all":
        app[K.MOCKS] = MockSystems(str(config.DB_PATH), config.AS_OF)
        app.add_subapp("/mock", build_mock_app(app[K.MOCKS], internal_token=app[K.INTERNAL_TOKEN]))
    elif not config.INTERNAL_TOKEN:
        raise SystemExit("SERVICE_ROLE=gateway needs INTERNAL_TOKEN and MOCK_BASE_URL of the systems service")

    r = app.router
    r.add_get("/", index)
    r.add_static("/static", STATIC)
    r.add_get("/api/config", api_config)
    r.add_post("/api/login", api_login)
    r.add_post("/api/logout", api_logout)
    r.add_post("/api/workflow", api_workflow)
    r.add_post("/api/language", api_language)
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
    r.add_get("/agent", agent_page)
    r.add_get("/dashboard", dashboard_page)
    r.add_get("/api/agent/queue", api_agent_queue)
    r.add_post("/api/agent/accept", api_agent_accept)
    r.add_get("/healthz", healthz)
    r.add_get("/readyz", readyz)
    r.add_get("/health", health)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=config.PORT, print=None, access_log=None)
