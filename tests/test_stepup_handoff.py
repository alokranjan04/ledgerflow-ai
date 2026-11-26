"""Step-up OTP before write actions, human hand-off with context, KPI counters."""
import io
import logging
import re
import socket

import pytest
from aiohttp.test_utils import TestClient, TestServer

import app as lfapp
from ledgerflow import config
from ledgerflow.platform.actions import ActionService, AuditLog
from ledgerflow.platform.stepup import DemoSms, StepUp
from ledgerflow.platform.telemetry import METRICS, Trace, kpis
from ledgerflow.platform.tools import REGISTRY, ToolContext

from .conftest import AS_OF, PRIYA


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def stepped(env, tmp_path):
    """Same systems, an ActionService that requires step-up, and a controllable clock."""
    from ledgerflow.domains import cards, loans
    from ledgerflow.platform import common_tools
    clock = Clock()
    actions = ActionService(AuditLog(tmp_path / "audit2.db"), step_up=StepUp(DemoSms(), valid_secs=600, clock=clock))
    common_tools.register_common_actions(actions)
    loans.register_actions(actions)
    cards.register_actions(actions)

    async def login():
        s, _ = await env["login"](*PRIYA)
        s.phone = PRIYA[0]
        return s, ToolContext(s, env["con"], actions, Trace(), AS_OF)

    async def run(ctx, name, **args):
        return await REGISTRY.run(name, args, ctx, env["wfs"]["loan_servicing"].tools)
    return {"login": login, "run": run, "clock": clock, "actions": actions, "ms": env["ms"]}


async def _propose_and_say_yes(st, ctx):
    tok = (await st["run"](ctx, "propose_foreclosure_statement")).data["confirmation_id"]
    ctx.session.user_turn_seq += 1
    return tok


# ----------------------------------------------------------------- step-up
@pytest.mark.req("REQ-SEC-003.1", "REQ-SEC-003.2")
async def test_write_needs_a_code_and_the_model_never_sees_it(stepped):
    s, ctx = await stepped["login"]()
    tok = await _propose_and_say_yes(stepped, ctx)
    res = await stepped["run"](ctx, "confirm_action", confirmation_id=tok)
    assert res.data["step_up_required"] and res.data["sent_to"] == "******0001"
    code = res.card["data"]["demo_code"]
    assert re.fullmatch(r"\d{6}", code)
    assert code not in str(res.data)                              # model-visible result carries no code
    assert stepped["ms"].q("SELECT COUNT(*) n FROM tickets")[0]["n"] == 0   # nothing executed yet
    ok = await stepped["run"](ctx, "verify_otp", code=f"{code[:3]} {code[3:]}")   # spoken with a pause
    assert ok.data["ok"]
    done = await stepped["run"](ctx, "confirm_action", confirmation_id=tok)
    assert done.data["ok"] and done.data["request_id"].startswith("SR-")
    events = [a["event"] for a in stepped["actions"].audit.for_customer("C1001")]
    assert "step_up_challenged" in events and "verified" in events and "executed" in events


@pytest.mark.req("REQ-SEC-003.3")
async def test_wrong_codes_are_limited_and_codes_expire(stepped):
    s, ctx = await stepped["login"]()
    tok = await _propose_and_say_yes(stepped, ctx)
    await stepped["run"](ctx, "confirm_action", confirmation_id=tok)
    r1 = await stepped["run"](ctx, "verify_otp", code="000000")
    r2 = await stepped["run"](ctx, "verify_otp", code="000001")
    r3 = await stepped["run"](ctx, "verify_otp", code="000002")
    assert r1.data["attempts_left"] == 2 and r2.data["attempts_left"] == 1 and r3.data["locked"]
    assert (await stepped["run"](ctx, "verify_otp", code="123456")).data["error"].startswith("No code is pending")
    # after a lockout, confirming again sends a fresh code; that code expires after 5 minutes
    res = await stepped["run"](ctx, "confirm_action", confirmation_id=tok)
    assert res.data["step_up_required"] and not res.data.get("ok")
    code = res.card["data"]["demo_code"]
    stepped["clock"].t += 301
    late = await stepped["run"](ctx, "verify_otp", code=code)
    assert not late.data["ok"] and "expired" in late.data["error"]


@pytest.mark.req("REQ-SEC-003.4")
async def test_code_sends_are_rate_limited(stepped):
    s, ctx = await stepped["login"]()
    tok = await _propose_and_say_yes(stepped, ctx)
    sent = [await stepped["run"](ctx, "confirm_action", confirmation_id=tok) for _ in range(4)]
    assert [bool(r.card["data"].get("demo_code")) for r in sent] == [True, True, True, False]
    assert "Too many codes" in sent[-1].data["error"]


@pytest.mark.req("REQ-SEC-003.5")
async def test_step_up_is_valid_for_a_window_then_required_again(stepped):
    s, ctx = await stepped["login"]()
    tok = await _propose_and_say_yes(stepped, ctx)
    code = (await stepped["run"](ctx, "confirm_action", confirmation_id=tok)).card["data"]["demo_code"]
    await stepped["run"](ctx, "verify_otp", code=code)
    assert (await stepped["run"](ctx, "confirm_action", confirmation_id=tok)).data["ok"]
    tok2 = await _propose_and_say_yes(stepped, ctx)
    assert (await stepped["run"](ctx, "confirm_action", confirmation_id=tok2)).data["ok"]      # within 10 min
    stepped["clock"].t += 601
    tok3 = await _propose_and_say_yes(stepped, ctx)
    assert (await stepped["run"](ctx, "confirm_action", confirmation_id=tok3)).data["step_up_required"]


@pytest.mark.req("REQ-SEC-003.6")
async def test_the_code_never_reaches_the_logs(stepped):
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(h)
    old = root.level
    root.setLevel(logging.DEBUG)
    try:
        s, ctx = await stepped["login"]()
        tok = await _propose_and_say_yes(stepped, ctx)
        code = (await stepped["run"](ctx, "confirm_action", confirmation_id=tok)).card["data"]["demo_code"]
    finally:
        root.removeHandler(h)
        root.setLevel(old)
    assert "sms (demo) delivered" in buf.getvalue() and code not in buf.getvalue()


# ----------------------------------------------------------------- hand-off
@pytest.mark.req("REQ-HND-001.1", "REQ-HND-001.2")
async def test_handoff_carries_identity_and_conversation_and_is_idempotent(env):
    s, ctx = await env["login"](*PRIYA)
    s.phone = PRIYA[0]
    s.add_transcript("user", "I was charged a bounce fee twice", "text")
    before = METRICS.counters.get("kpi.handoffs", 0)
    r1 = await env["run"](ctx, "request_human_agent", reason="Disputes a duplicate bounce fee", urgency="high")
    r2 = await env["run"](ctx, "request_human_agent", reason="Disputes a duplicate bounce fee")
    assert r1.data["ok"] and r1.data["request_id"] == r2.data["request_id"]          # one hand-off, not two
    assert METRICS.counters["kpi.handoffs"] == before + 1
    q = await env["con"]["ops"].get("/queue", {"type": "agent_handoff"})
    item = q["items"][0]
    assert item["payload"]["verified"] and item["payload"]["customer_name"] == "Priya Sharma"
    assert item["payload"]["urgency"] == "high" and item["payload"]["phone_masked"] == "******0001"
    assert item["payload"]["transcript"][0]["text"] == "I was charged a bounce fee twice"


@pytest.mark.req("REQ-HND-001.3")
async def test_unverified_caller_can_still_reach_a_human(env):
    s = await env["store"].create("loan_servicing", "phone")
    s.phone = "9811111111"
    ctx = ToolContext(s, env["con"], env["actions"], Trace(), AS_OF, mode="phone")
    r = await env["run"](ctx, "request_human_agent", reason="Cannot remember date of birth")
    assert r.data["ok"] and not r.data["verified"] and "call back" in r.data["next_step"]
    item = (await env["con"]["ops"].get("/queue", {"type": "agent_handoff"}))["items"][0]
    assert item["customer_id"] == "UNVERIFIED" and item["payload"]["customer_name"] == ""


@pytest.mark.req("REQ-HND-001.5")
async def test_containment_kpi_is_derived_from_engaged_sessions_and_handoffs(env):
    from ledgerflow.platform.gemini_text import TextAgent

    from .test_text_agent import ScriptedLLM, text
    METRICS.reset()
    for _ in range(4):
        s, _ = await env["login"](*PRIYA)
        await TextAgent(ScriptedLLM([text("Hello")]), env["wfs"], env["con"], env["actions"]).turn(s, "hi", _noop)
        await TextAgent(ScriptedLLM([text("Again")]), env["wfs"], env["con"], env["actions"]).turn(s, "hi", _noop)
    s, ctx = await env["login"](*PRIYA)
    await env["run"](ctx, "request_human_agent", reason="wants a person")
    k = kpis()
    assert k["sessions_engaged"] == 4 and k["handoffs"] == 1 and k["containment_rate"] == 0.75


async def _noop(e):
    return None


# ----------------------------------------------------------------- agent desk API
def _free_port():
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


@pytest.mark.req("REQ-HND-001.4")
async def test_agent_desk_lists_and_accepts_handoffs(tmp_path, monkeypatch):
    port = _free_port()
    for k, v in dict(DATA_DIR=tmp_path, DB_PATH=tmp_path / "lf.db", AUDIT_DB_PATH=tmp_path / "a.db", GEMINI_API_KEY="",
                     ADMIN_TOKEN="", PUBLIC_URL="", REDIS_URL="", SERVICE_ROLE="all", INTERNAL_TOKEN="",
                     MOCK_BASE_URL=f"http://127.0.0.1:{port}/mock").items():
        monkeypatch.setattr(config, k, v)
    application = lfapp.make_app()
    for p in application["mocks"].profiles.values():
        p.update(p50=1, p95=2, error_rate=0.0, down=False)
    c = TestClient(TestServer(application, port=port))
    await c.start_server()
    try:
        await c.post("/api/login", json={"phone": PRIYA[0], "dob": PRIYA[1]})
        sess = next(iter(c.app["sessions"].sessions.values()))
        ctx = ToolContext(sess, c.app["connectors"], c.app["actions"], Trace(), AS_OF)
        tid = (await REGISTRY.run("request_human_agent", {"reason": "wants a person"}, ctx, ["request_human_agent"])).data["request_id"]
        q = await (await c.get("/api/agent/queue")).json()
        assert q["open"] == 1 and q["items"][0]["id"] == tid
        acc = await c.post("/api/agent/accept", json={"id": tid, "agent": "asha"})
        assert acc.status == 200 and (await acc.json())["status"] == "in_progress"
        assert (await c.post("/api/agent/accept", json={"id": "nope"})).status == 400
        assert (await (await c.get("/agent")).text()).count("Agent desk") >= 1
        monkeypatch.setattr(config, "PUBLIC_URL", "https://x.run.app")          # deployed: admin token required
        assert (await c.get("/api/agent/queue")).status == 403
    finally:
        await c.close()
