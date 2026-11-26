"""The real HTTP/WebSocket app (make_app) on a local port, with a scripted LLM."""
import socket

import pytest
from aiohttp.test_utils import TestClient, TestServer

import app as lfapp
from ledgerflow import config

from .test_text_agent import ScriptedLLM, fc, text


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def client(tmp_path, monkeypatch):
    port = _free_port()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "lf.db")
    monkeypatch.setattr(config, "AUDIT_DB_PATH", tmp_path / "audit.db")
    monkeypatch.setattr(config, "MOCK_BASE_URL", f"http://127.0.0.1:{port}/mock")
    # Isolate from the developer's .env (deploy.sh writes ADMIN_TOKEN there).
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    monkeypatch.setattr(config, "PUBLIC_URL", "")
    monkeypatch.setattr(config, "STREAM_TOKEN", "")
    lfapp.LOGIN_FAILURES.clear()
    application = lfapp.make_app()
    for p in application["mocks"].profiles.values():
        p.update(p50=1, p95=2, error_rate=0.0, down=False)
    c = TestClient(TestServer(application, port=port))
    await c.start_server()
    yield c
    await c.close()


async def _login(c, phone="9800000001", dob="1990-04-12"):
    r = await c.post("/api/login", json={"phone": phone, "dob": dob})
    return r.status, await r.json()


@pytest.mark.req("REQ-SEC-001.1", "REQ-UI-001.1")
async def test_login_account_and_unknown_session(client):
    status, body = await _login(client)
    assert status == 200 and body["customer_name"] == "Priya Sharma"
    acct = await (await client.get("/api/account")).json()
    assert acct["loan"]["loan_id"] == "L-10001" and acct["card"]["card"].endswith("4417")
    assert (await client.get("/api/account", headers={"X-Session": "nope"}, cookies={"lf_sid": "nope"})).status == 401


@pytest.mark.req("REQ-SEC-001.3")
async def test_login_rate_limited_per_phone_across_sessions(client):
    for _ in range(5):
        assert (await _login(client, dob="2000-01-01"))[0] == 401
    status, body = await _login(client)          # correct DOB, but locked
    assert status == 429


@pytest.mark.req("REQ-UI-001.2", "REQ-AGT-002.2", "REQ-AGT-002.5")
async def test_chat_websocket_event_protocol(client):
    _, body = await _login(client)
    client.app["agent"].llm = ScriptedLLM([fc("get_loan_summary"), text("Your next EMI is ₹17,323.49.")])
    async with client.ws_connect("/ws/chat") as ws:
        await ws.send_json({"type": "user", "text": "What's my next EMI?"})
        kinds = []
        while True:
            ev = await ws.receive_json(timeout=10)
            kinds.append(ev["type"])
            if ev["type"] == "trace":
                break
    assert kinds == ["thinking", "tool_started", "tool_finished", "assistant", "trace"]


@pytest.mark.req("REQ-AGT-001.3")
async def test_workflow_switch_keeps_identity_and_context(client):
    await _login(client)
    sess = next(iter(client.app["sessions"].sessions.values()))
    sess.add_transcript("user", "my EMI went up", "text")
    r = await (await client.post("/api/workflow", json={"workflow": "card_servicing"})).json()
    assert r["workflow"] == "card_servicing" and sess.verified
    assert "my EMI went up" in sess.history[0]["parts"][0]["text"]


@pytest.mark.req("REQ-VOX-001.6")
async def test_phone_answer_webhook(client):
    r = await client.post("/answer", data={"From": "+919800000001"})
    xml = await r.text()
    assert r.status == 200 and "/stream?caller_id=919800000001" in xml


@pytest.mark.req("REQ-SYS-001.3")
async def test_fault_panel_api(client):
    r = await (await client.post("/api/faults", json={"payments": {"down": True}})).json()
    assert r["profiles"]["payments"]["down"] is True
    r = await (await client.post("/api/faults", json={"reset": True})).json()
    assert r["profiles"]["payments"]["down"] is False


@pytest.mark.req("REQ-SEC-001.7")
def test_phone_numbers_are_masked_in_logs():
    assert lfapp.mask_phone("9800000001") == "******0001"


@pytest.mark.req("REQ-SEC-001.9")
async def test_security_headers(client):
    r = await client.get("/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["X-Frame-Options"] == "SAMEORIGIN"


@pytest.mark.req("REQ-SEC-002.1")
async def test_session_is_an_httponly_cookie_never_in_body_or_url(client):
    r = await client.post("/api/login", json={"phone": "9800000001", "dob": "1990-04-12"})
    body = await r.json()
    assert "session_id" not in body
    cookie = r.headers["Set-Cookie"]
    assert "lf_sid=" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    sid = r.cookies["lf_sid"].value
    # the query string is no longer accepted as a session carrier
    client.session.cookie_jar.clear()
    assert (await client.get(f"/api/account?session={sid}")).status == 401


@pytest.mark.req("REQ-SEC-002.2")
async def test_mock_systems_are_not_reachable_from_outside(client):
    r = await client.get("/mock/crm/customers/C1001")
    assert r.status == 403
    r = await client.post("/mock/admin/faults", json={"payments": {"down": True}})
    assert r.status == 403


@pytest.mark.req("REQ-SEC-002.3")
async def test_admin_calls_refused_when_deployed_without_token(client, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "https://example.run.app")
    assert (await client.post("/api/faults", json={"reset": True})).status == 403
    monkeypatch.setattr(config, "ADMIN_TOKEN", "s3cret")
    assert (await client.post("/api/faults", json={"reset": True}, headers={"X-Admin-Token": "nope"})).status == 403
    assert (await client.post("/api/faults", json={"reset": True}, headers={"X-Admin-Token": "s3cret"})).status == 200


@pytest.mark.req("REQ-SEC-002.4")
async def test_csp_and_transport_headers(client):
    r = await client.get("/", headers={"X-Forwarded-Proto": "https"})
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp and "frame-ancestors 'self'" in csp
    assert "max-age" in r.headers["Strict-Transport-Security"]
    assert (await client.get("/api/config")).headers["Cache-Control"] == "no-store"


@pytest.mark.req("REQ-SEC-002.5")
async def test_cross_origin_websocket_is_refused(client):
    await _login(client)
    import aiohttp
    with pytest.raises(aiohttp.WSServerHandshakeError):
        await client.ws_connect("/ws/chat", headers={"Origin": "https://evil.example"})


@pytest.mark.req("REQ-OPS-001.3")
async def test_chat_is_rate_limited_per_session(client, monkeypatch):
    await _login(client)
    from ledgerflow.platform.ratelimit import RateLimiter
    client.app["chat_limit"] = RateLimiter(per_minute=2)
    client.app["agent"].llm = ScriptedLLM([text("one"), text("two")])
    async with client.ws_connect("/ws/chat") as ws:
        got = []
        for i in range(3):
            await ws.send_json({"type": "user", "text": f"hi {i}"})
        while len(got) < 3:
            ev = await ws.receive_json(timeout=10)
            if ev["type"] in ("assistant", "error"):
                got.append(ev["type"])
    assert got.count("assistant") == 2 and got.count("error") == 1


@pytest.mark.req("REQ-OPS-001.2")
async def test_liveness_and_readiness(client):
    assert (await client.get("/healthz")).status == 200
    r = await client.get("/readyz")
    body = await r.json()
    assert r.status == 200 and body["checks"]["systems"] == "ok" and body["checks"]["llm_key"] == "missing"
    client.app["mocks"].profiles["crm"]["down"] = True
    for _ in range(5):
        await client.get("/readyz")
    assert (await client.get("/readyz")).status == 503
