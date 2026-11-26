"""Data, mock systems, connectors, actions and workflow definitions."""
import hashlib
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ledgerflow.mocks.seed import seed
from ledgerflow.mocks.server import MockSystems
from ledgerflow.platform.connectors import Connector, NotFound, Policy, Timeout, Unavailable
from ledgerflow.platform.telemetry import METRICS
from ledgerflow.platform.tools import REGISTRY
from ledgerflow.platform.workflow import load_workflows

from .conftest import PRIYA

ROOT = Path(__file__).resolve().parent.parent


def _dump_hash(as_of: date) -> str:
    conn = sqlite3.connect(":memory:")
    seed(conn, as_of)
    return hashlib.sha256("\n".join(conn.iterdump()).encode()).hexdigest()


# ----------------------------------------------------------------- data
@pytest.mark.req("REQ-DAT-001.1")
def test_seed_is_deterministic():
    assert _dump_hash(date(2026, 11, 26)) == _dump_hash(date(2026, 11, 26))


@pytest.mark.req("REQ-DAT-001.2", "REQ-DAT-001.3")
@pytest.mark.parametrize("as_of", [date(2026, 11, 26), date(2027, 3, 12), date(2027, 8, 2)])
def test_scenarios_hold_on_any_date(tmp_path, as_of):
    ms = MockSystems(str(tmp_path / f"{as_of}.db"), as_of)
    paid = [r for r in ms.loan_detail("L-10001")["schedule"] if r["due_date"] <= as_of.isoformat()]
    assert Decimal(paid[-1]["emi"]) > Decimal(paid[-2]["emi"])                     # Priya: EMI just went up
    fails = ms.q("SELECT due_date FROM payments WHERE loan_id='L-10001' AND status='failed'")
    assert len(fails) == 3
    rahul = [r for r in ms.loan_detail("L-10002")["schedule"] if r["due_date"] <= as_of.isoformat()]
    assert 12 <= len(rahul) < 24                                                  # past lock-in, < 24 months
    meera = [r for r in ms.loan_detail("L-10003")["schedule"] if r["due_date"] <= as_of.isoformat()]
    assert len(meera) < 12                                                        # inside lock-in
    dups = ms.q("SELECT COUNT(*) n FROM card_txns WHERE card_id='CARD-5002' AND merchant='Croma Electronics'")
    assert dups[0]["n"] == 2


# ----------------------------------------------------------------- mock systems
@pytest.mark.req("REQ-SYS-001.2")
def test_mock_latency_matches_profile(tmp_path):
    ms = MockSystems(str(tmp_path / "t.db"), date(2026, 11, 26))
    xs = sorted(ms._delay("loans") * 1000 for _ in range(4000))
    p50, p95 = xs[2000], xs[3800]
    assert 108 <= p50 <= 132          # profile p50 120 ms ±10%
    assert 297 <= p95 <= 403          # profile p95 350 ms ±15%


@pytest.mark.req("REQ-SYS-001.5")
async def test_service_request_escalation(env):
    ops = env["con"]["ops"]
    t = await ops.post("/requests", {"customer_id": "C1001", "type": "complaint", "summary": "x"}, idempotency_key="k1")
    again = await ops.post("/requests", {"customer_id": "C1001", "type": "complaint", "summary": "x"}, idempotency_key="k1")
    assert again["id"] == t["id"] and again["replayed"]
    esc = await ops.post(f"/requests/{t['id']}/escalate", {})
    assert esc["status"] == "escalated"


# ----------------------------------------------------------------- connectors
@pytest.mark.req("REQ-CON-001.1")
async def test_deadline_raises_typed_timeout(env):
    env["ms"].profiles["loans"].update(p50=400, p95=500)
    c = Connector("loans", env["con"]["loans"].base_url, Policy(timeout_s=0.1, retries=0))
    with pytest.raises(Timeout):
        await c.get("/customers/C1001/loans")
    await c.close()


@pytest.mark.req("REQ-CON-001.2")
async def test_non_idempotent_write_is_not_retried(env):
    env["ms"].profiles["crm"]["error_rate"] = 1.0
    before = METRICS.counters.get("connector.crm.unavailable", 0)
    with pytest.raises(Unavailable):
        await env["con"]["crm"].post("/verify", {"phone": PRIYA[0], "dob": PRIYA[1]})
    assert METRICS.counters["connector.crm.unavailable"] == before + 1


@pytest.mark.req("REQ-CON-001.4")
async def test_client_errors_do_not_open_the_breaker(env):
    for _ in range(10):
        with pytest.raises(NotFound):
            await env["con"]["loans"].get("/loans/L-NOPE")
    assert env["con"]["loans"].breaker.state == "closed"


@pytest.mark.req("REQ-CON-001.5")
async def test_reference_data_is_cached_balances_are_not(env):
    before = METRICS.counters.get("connector.policy.cache_hit", 0)
    await env["con"]["policy"].get("/products/PL-FLOAT")
    await env["con"]["policy"].get("/products/PL-FLOAT")
    assert METRICS.counters["connector.policy.cache_hit"] == before + 1
    assert env["con"]["loans"].policy.cache_ttl_s == 0 and env["con"]["payments"].policy.cache_ttl_s == 0


# ----------------------------------------------------------------- actions
@pytest.mark.req("REQ-ACT-001.1")
async def test_propose_has_no_side_effect(env):
    _, ctx = await env["login"](*PRIYA)
    r = (await env["run"](ctx, "propose_foreclosure_statement")).data
    assert r["status"] == "awaiting_customer_confirmation" and r["confirmation_id"]
    assert env["ms"].q("SELECT COUNT(*) n FROM tickets")[0]["n"] == 0


@pytest.mark.req("REQ-ACT-001.4")
async def test_expired_confirmation_is_rejected(env):
    s, ctx = await env["login"](*PRIYA)
    tok = (await env["run"](ctx, "propose_foreclosure_statement")).data["confirmation_id"]
    s.pending_actions[tok].expires = 0
    s.user_turn_seq += 1
    r = (await env["run"](ctx, "confirm_action", confirmation_id=tok)).data
    assert not r["ok"] and "expired" in r["error"]


@pytest.mark.req("REQ-ACT-001.8")
async def test_failed_execution_is_reported_and_can_be_retried(env):
    s, ctx = await env["login"](*PRIYA)
    tok = (await env["run"](ctx, "propose_foreclosure_statement")).data["confirmation_id"]
    s.user_turn_seq += 1
    env["ms"].profiles["ops"]["down"] = True
    r = (await env["run"](ctx, "confirm_action", confirmation_id=tok)).data
    assert not r["ok"] and "safe to try again" in r["error"]
    env["ms"].profiles["ops"]["down"] = False
    import asyncio
    await asyncio.sleep(0.25)          # breaker cooldown in tests
    r = (await env["run"](ctx, "confirm_action", confirmation_id=tok)).data
    assert r["ok"]


# ----------------------------------------------------------------- workflows
@pytest.mark.req("REQ-AGT-001.1", "REQ-AGT-001.2")
def test_workflows_load_and_reference_only_registered_tools(env):
    wfs = load_workflows()
    assert set(wfs) == {"loan_servicing", "card_servicing"}
    for wf in wfs.values():
        assert wf.prompt.strip() and wf.tools
        assert set(wf.tools) <= set(REGISTRY.specs), wf.id
        assert set(wf.actions) <= set(env["actions"].executors), wf.id
    # The card workflow reuses platform tools; its own tools are only the card domain module's.
    card_only = set(wfs["card_servicing"].tools) - set(wfs["loan_servicing"].tools)
    assert {REGISTRY.specs[t].fn.__module__ for t in card_only} == {"ledgerflow.domains.cards"}


@pytest.mark.req("REQ-SEC-001.8")
def test_prompts_treat_data_as_reference_not_instructions():
    for wf in load_workflows().values():
        assert "not instructions" in wf.prompt


@pytest.mark.req("REQ-PLT-001.1")
def test_secrets_are_excluded_from_image_and_repo():
    for f in (".dockerignore", ".gitignore"):
        lines = (ROOT / f).read_text().splitlines()
        assert ".env" in lines, f
    assert "!.env.example" in (ROOT / ".gitignore").read_text()


# ----------------------------------------------------------------- card workflow
@pytest.mark.req("REQ-CRD-001.1")
async def test_card_summary_with_minimum_due_rule(env):
    from .conftest import ANANYA
    _, ctx = await env["login"](*ANANYA, workflow="card_servicing")
    d = (await env["run"](ctx, "get_card_summary")).data
    assert d["ok"] and d["min_due_clause"] == "CC-PL-4.1"
    card = env["ms"].q("SELECT statement_balance, min_due FROM cards WHERE id='CARD-5002'")[0]
    expected = max(Decimal(card["statement_balance"]) * Decimal("0.05"), Decimal(200)).quantize(Decimal("0.01"))
    assert Decimal(card["min_due"]) == expected


@pytest.mark.req("REQ-CRD-001.4")
async def test_card_dispute_outside_window_is_refused(env):
    from .conftest import ANANYA
    _, ctx = await env["login"](*ANANYA, workflow="card_servicing")
    ctx.as_of = date(2027, 6, 1)
    r = (await env["run"](ctx, "propose_card_dispute", transaction_id="T-5002-015", reason="charged twice")).data
    assert not r["ok"] and r["clause"] == "CC-PL-9.2"
