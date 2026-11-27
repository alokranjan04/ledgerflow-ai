"""Scale-out: Redis-shared state across instances, and the gateway / systems split."""
import io
import json
import logging
import socket

import fakeredis
import pytest
from aiohttp.test_utils import TestClient, TestServer

import app as lfapp
from ledgerflow import appkeys as K
from ledgerflow import config
from ledgerflow.platform import logs
from ledgerflow.platform.gcp_auth import IdTokenProvider
from ledgerflow.platform.ratelimit import RedisLoginGuard, RedisRateLimiter
from ledgerflow.platform.sessions import RedisSessionStore
from ledgerflow.platform.telemetry import Trace
from ledgerflow.platform.tools import ToolContext

from .conftest import AS_OF, PRIYA
from .test_text_agent import ScriptedLLM, fc, text


@pytest.fixture
def redis_server():
    return fakeredis.FakeServer()


def _client(server):
    return fakeredis.FakeAsyncRedis(server=server)


# ----------------------------------------------------------------- shared sessions
@pytest.mark.req("REQ-SCL-001.1", "REQ-SCL-001.6")
async def test_session_written_by_one_instance_is_served_by_another(redis_server, env):
    a = RedisSessionStore(_client(redis_server))
    b = RedisSessionStore(_client(redis_server))          # a second instance: empty cache
    s = await a.create("loan_servicing")
    s.verified, s.customer_id, s.customer_name = True, "C1001", "Priya Sharma"
    s.add_transcript("user", "my EMI went up", "text")
    await a.save(s)
    got = await b.get(s.id)
    assert got is not None and got is not s
    assert (got.customer_id, got.verified, got.transcript[0]["text"]) == ("C1001", True, "my EMI went up")
    assert await b.count() == 1
    await b.drop(s.id)                          # logout handled by instance B
    assert await a.get(s.id) is None            # instance A's cached copy is revoked immediately


@pytest.mark.req("REQ-SCL-001.2")
async def test_pending_action_proposed_on_one_instance_confirms_on_another(redis_server, env):
    _, ctx_seed = await env["login"](*PRIYA)
    a = RedisSessionStore(_client(redis_server))
    b = RedisSessionStore(_client(redis_server))
    s = await a.create("loan_servicing")
    s.verified, s.customer_id = True, "C1001"
    ctx_a = ToolContext(s, env["con"], env["actions"], Trace(), AS_OF)
    tok = (await env["run"](ctx_a, "propose_foreclosure_statement")).data["confirmation_id"]
    await a.save(s)
    s_b = await b.get(s.id)                     # reloaded: pending action is a plain dict now
    s_b.user_turn_seq += 1                      # customer said yes
    ctx_b = ToolContext(s_b, env["con"], env["actions"], Trace(), AS_OF)
    done = (await env["run"](ctx_b, "confirm_action", confirmation_id=tok)).data
    assert done["ok"] and done["request_id"].startswith("SR-")


@pytest.mark.req("REQ-SCL-001.3")
async def test_active_session_object_is_authoritative_on_its_instance(redis_server):
    """A live voice call holds unsaved changes; a chat request on the same instance
    must see them rather than a stale Redis copy."""
    store = RedisSessionStore(_client(redis_server))
    s = await store.create("loan_servicing")
    s.add_transcript("user", "said by voice, not saved yet", "voice")
    again = await store.get(s.id)
    assert again is s and again.transcript[-1]["text"] == "said by voice, not saved yet"


@pytest.mark.req("REQ-SCL-001.4")
async def test_rate_limit_and_login_lockout_are_shared_across_instances(redis_server):
    r1, r2 = _client(redis_server), _client(redis_server)
    lim1 = RedisRateLimiter(r1, "chat", per_minute=3, clock=lambda: 120.0)
    lim2 = RedisRateLimiter(r2, "chat", per_minute=3, clock=lambda: 120.0)
    results = [await lim1.hit("s1"), await lim2.hit("s1"), await lim1.hit("s1"), await lim2.hit("s1")]
    assert results == [True, True, True, False]
    assert await lim1.hit("s2")                                    # independent keys
    g1, g2 = RedisLoginGuard(r1, max_failures=2), RedisLoginGuard(r2, max_failures=2)
    await g1.record_failure("9800000001")
    assert not await g2.locked("9800000001")
    await g2.record_failure("9800000001")
    assert await g1.locked("9800000001")
    assert await r1.ttl("lf:loginfail:9800000001") > 0             # lockout expires


# ----------------------------------------------------------------- full app on Redis
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _isolate(monkeypatch, tmp_path, **over):
    base = dict(DATA_DIR=tmp_path, DB_PATH=tmp_path / "lf.db", AUDIT_DB_PATH=tmp_path / "audit.db",
                GEMINI_API_KEY="", ADMIN_TOKEN="", PUBLIC_URL="", STREAM_TOKEN="", REDIS_URL="",
                SERVICE_ROLE="all", INTERNAL_TOKEN="", SYSTEMS_AUDIENCE="")
    base.update(over)
    for k, v in base.items():
        monkeypatch.setattr(config, k, v)


@pytest.mark.req("REQ-SCL-001.5")
async def test_two_app_instances_share_one_customer_session(redis_server, tmp_path, monkeypatch):
    """Login on instance A, chat on instance B: B serves the session and the turn is visible to A."""
    port = _free_port()
    _isolate(monkeypatch, tmp_path, MOCK_BASE_URL=f"http://127.0.0.1:{port}/mock")
    app_a = lfapp.make_app()
    app_a[K.REDIS_CLIENT] = _client(redis_server)
    for p in app_a[K.MOCKS].profiles.values():
        p.update(p50=1, p95=2, error_rate=0.0, down=False)
    ca = TestClient(TestServer(app_a, port=port))
    await ca.start_server()
    # Instance B: a gateway that uses A's systems (same internal token) and the same Redis.
    monkeypatch.setattr(config, "SERVICE_ROLE", "gateway")
    monkeypatch.setattr(config, "INTERNAL_TOKEN", app_a[K.INTERNAL_TOKEN])
    app_b = lfapp.make_app()
    app_b[K.REDIS_CLIENT] = _client(redis_server)
    cb = TestClient(TestServer(app_b))
    await cb.start_server()
    try:
        r = await ca.post("/api/login", json={"phone": PRIYA[0], "dob": PRIYA[1]})
        sid = r.cookies["lf_sid"].value
        assert (await (await ca.get("/api/metrics")).json())["state_backend"] == "redis"
        cb.app[K.AGENT].llm = ScriptedLLM([fc("get_loan_summary"), text("Your next EMI is ₹17,323.49.")])
        async with cb.ws_connect("/ws/chat", headers={"X-Session": sid}) as ws:
            await ws.send_json({"type": "user", "text": "next EMI?"})
            while (await ws.receive_json(timeout=10))["type"] != "trace":
                pass
        fresh = await RedisSessionStore(_client(redis_server)).get(sid)
        assert fresh.customer_id == "C1001" and fresh.transcript[-1]["text"] == "Your next EMI is ₹17,323.49."
    finally:
        await cb.close()
        await ca.close()


@pytest.mark.req("REQ-SCL-002.1", "REQ-SCL-002.2")
async def test_gateway_and_systems_split(tmp_path, monkeypatch):
    sys_port = _free_port()
    _isolate(monkeypatch, tmp_path, SERVICE_ROLE="systems", INTERNAL_TOKEN="shared-secret")
    systems = TestClient(TestServer(lfapp.make_app(), port=sys_port))
    await systems.start_server()
    for p in systems.app[K.MOCKS].profiles.values():
        p.update(p50=1, p95=2, error_rate=0.0, down=False)
    _isolate(monkeypatch, tmp_path / "gw", SERVICE_ROLE="gateway", INTERNAL_TOKEN="shared-secret",
             MOCK_BASE_URL=f"http://127.0.0.1:{sys_port}/mock")
    (tmp_path / "gw").mkdir()
    gw = TestClient(TestServer(lfapp.make_app()))
    await gw.start_server()
    try:
        # the systems service serves only internal callers and has no customer UI
        assert (await systems.get("/mock/crm/customers/C1001")).status == 403
        assert (await systems.get("/")).status == 404
        # the gateway has no co-located systems but works end to end through them
        assert (await gw.get("/mock/crm/customers/C1001")).status == 404
        r = await gw.post("/api/login", json={"phone": PRIYA[0], "dob": PRIYA[1]})
        assert r.status == 200
        acct = await (await gw.get("/api/account")).json()
        assert acct["loan"]["loan_id"] == "L-10001"
        assert (await (await gw.get("/readyz")).json())["ready"] is True
        # fault injection is proxied to the systems service
        r = await (await gw.post("/api/faults", json={"payments": {"down": True}})).json()
        assert r["profiles"]["payments"]["down"] is True
        assert systems.app[K.MOCKS].profiles["payments"]["down"] is True
    finally:
        await gw.close()
        await systems.close()


@pytest.mark.req("REQ-SCL-002.3")
def test_split_roles_refuse_to_start_without_a_shared_secret(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path, SERVICE_ROLE="gateway")
    with pytest.raises(SystemExit):
        lfapp.make_app()
    _isolate(monkeypatch, tmp_path, SERVICE_ROLE="systems")
    with pytest.raises(SystemExit):
        lfapp.make_app()


@pytest.mark.req("REQ-SCL-002.4")
async def test_id_token_is_fetched_once_and_cached():
    calls = []

    def fetch(aud):
        calls.append(aud)
        return f"tok-{len(calls)}"
    p = IdTokenProvider("https://systems.example.run.app", fetch=fetch)
    assert await p.headers() == {"Authorization": "Bearer tok-1"}
    assert await p.headers() == {"Authorization": "Bearer tok-1"}
    p._expires = 0
    assert (await p.headers())["Authorization"] == "Bearer tok-2"
    assert calls == ["https://systems.example.run.app"] * 2


@pytest.mark.req("REQ-SCL-002.5")
async def test_audit_events_also_go_to_the_durable_log(env):
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logs.JsonFormatter())
    lg = logging.getLogger("ledgerflow.audit")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        s, ctx = await env["login"](*PRIYA)
        tok = (await env["run"](ctx, "propose_foreclosure_statement")).data["confirmation_id"]
        s.user_turn_seq += 1
        await env["run"](ctx, "confirm_action", confirmation_id=tok)
    finally:
        lg.removeHandler(h)
    events = [json.loads(line) for line in buf.getvalue().splitlines()]
    kinds = [e["audit_event"] for e in events if e["message"] == "audit"]
    assert kinds == ["proposed", "executed"]
    assert all(e["customer"] == "C1001" and len(e["session"]) == 12 for e in events)
    assert events[-1]["request_id"].startswith("SR-")
