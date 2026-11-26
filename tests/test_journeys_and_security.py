"""Customer journeys against the mock systems, plus security, actions and resilience."""
from datetime import date

from ledgerflow.platform.common_tools import verify
from ledgerflow.platform.connectors import Breaker
from ledgerflow.platform.tools import FORBIDDEN_ARGS, REGISTRY

from .conftest import ANANYA, MEERA, PRIYA, RAHUL


# ----------------------------------------------------------------- journeys
async def test_journey1_emi_increase_explained_and_prepayment_savings(env):
    s, ctx = await env["login"](*PRIYA)
    r = (await env["run"](ctx, "explain_emi_change")).data
    assert r["emi_changed"] and r["direction"] == "increased"
    assert (r["previous_emi"], r["new_emi"]) == ("₹17,112.63", "₹17,323.49")
    assert (r["previous_rate_pct"], r["new_rate_pct"]) == ("10.25", "11.00")
    assert r["clause"] == "PL-FL-3.4"
    p = (await env["run"](ctx, "simulate_prepayment", amount=50000)).data
    assert p["eligible"] and p["prepayment_charge"] == "₹0"
    assert p["emis_remaining_now"] == 40
    assert p["reduce_tenure"]["interest_saved"] == "₹21,003.30"


async def test_journey2_next_emi_last_payment_and_20k_tenure(env):
    s, ctx = await env["login"](*PRIYA)
    summ = (await env["run"](ctx, "get_loan_summary")).data
    assert summ["next_due_date"] == "2026-12-05" and summ["next_due_amount"] == "₹17,323.49"
    last = (await env["run"](ctx, "get_last_payment")).data
    assert last["status"] == "success" and last["paid_on"] == "2026-11-05"
    p = (await env["run"](ctx, "simulate_prepayment", amount=20000)).data
    assert p["reduce_tenure"]["tenure_reduced_by_months"] >= 1


async def test_journey3_failed_payments_then_dispute_september(env):
    s, ctx = await env["login"](*PRIYA)
    r = (await env["run"](ctx, "get_payments", status="failed", months=6)).data
    assert r["failed_count"] == 3
    sept = [p for p in r["payments"] if p["due_date"].startswith("2026-09")]
    assert sept and sept[0]["reason_code"] == "INSUFFICIENT_FUNDS"
    prop = (await env["run"](ctx, "propose_payment_dispute", payment_id=sept[0]["payment_id"], reason="had funds")).data
    assert prop["status"] == "awaiting_customer_confirmation"
    s.user_turn_seq += 1                                       # customer says "yes"
    done = (await env["run"](ctx, "confirm_action", confirmation_id=prop["confirmation_id"])).data
    assert done["ok"] and done["request_id"].startswith("SR-")
    audit = env["actions"].audit.for_customer("C1001")
    assert [a["event"] for a in audit][:2] == ["executed", "proposed"]


async def test_journey4_foreclosure_three_customers(env):
    _, ctx = await env["login"](*PRIYA)
    f = (await env["run"](ctx, "check_foreclosure")).data
    assert f["eligible"] and f["penalty_free"] and f["clause"] == "PL-FL-5.1"
    _, ctx = await env["login"](*RAHUL)
    f = (await env["run"](ctx, "check_foreclosure")).data
    assert f["eligible"] and not f["penalty_free"] and f["foreclosure_charge_pct"] == "4" and f["clause"] == "PL-FX-6.2"
    _, ctx = await env["login"](*MEERA)
    f = (await env["run"](ctx, "check_foreclosure")).data
    assert not f["eligible"] and f["clause"] == "PL-FX-6.1" and f["total_payoff_amount"] is None


async def test_journey5_card_workflow_duplicate_dispute(env):
    s, ctx = await env["login"](*ANANYA, workflow="card_servicing")
    t = (await env["run"](ctx, "get_card_transactions", merchant="croma")).data
    dup = [x for x in t["transactions"] if x.get("possible_duplicate_of")]
    assert len(dup) == 1
    prop = (await env["run"](ctx, "propose_card_dispute", transaction_id=dup[0]["transaction_id"], reason="charged twice")).data
    s.user_turn_seq += 1
    done = (await env["run"](ctx, "confirm_action", confirmation_id=prop["confirmation_id"])).data
    assert done["ok"] and done["type"] == "card_dispute"


# ----------------------------------------------------------------- security
async def test_tools_never_take_customer_id_from_the_model():
    for spec in REGISTRY.specs.values():
        assert not (set(spec.params) & FORBIDDEN_ARGS), spec.name


async def test_unverified_session_cannot_read_data(env):
    s = env["store"].create("loan_servicing")
    from ledgerflow.platform.telemetry import Trace
    from ledgerflow.platform.tools import ToolContext
    ctx = ToolContext(s, env["con"], env["actions"], Trace(), date(2026, 11, 26))
    r = await env["run"](ctx, "get_loan_summary")
    assert not r.data["ok"] and "Verify" in r.data["error"]


async def test_cannot_dispute_another_customers_payment(env):
    _, ctx = await env["login"](*PRIYA)
    rahul_failed = env["ms"].q("SELECT id FROM payments WHERE loan_id='L-10002' AND status='failed'")[0]["id"]
    r = (await env["run"](ctx, "propose_payment_dispute", payment_id=rahul_failed, reason="x")).data
    assert not r["ok"]


async def test_injected_customer_id_argument_is_ignored(env):
    _, ctx = await env["login"](*PRIYA)
    r = await REGISTRY.run("get_loan_summary", {"customer_id": "C1002"}, ctx, ["get_loan_summary"])
    assert r.data["loan_id"] == "L-10001"


async def test_identity_lockout_after_three_failures(env):
    s = env["store"].create("loan_servicing")
    for _ in range(3):
        assert not (await verify(s, env["con"], PRIYA[0], "2000-01-01"))["ok"]
    res = await verify(s, env["con"], *PRIYA)
    assert res.get("locked") and not s.verified


async def test_workflow_isolation_cards_cannot_call_loan_tools(env):
    _, ctx = await env["login"](*PRIYA, workflow="card_servicing")
    r = await env["run"](ctx, "propose_prepayment_request", amount=20000, option="reduce_tenure")
    assert "not available" in r.data["error"]


# ----------------------------------------------------------------- actions
async def test_action_needs_new_customer_turn_and_is_idempotent(env):
    s, ctx = await env["login"](*PRIYA)
    prop = (await env["run"](ctx, "propose_foreclosure_statement")).data
    tok = prop["confirmation_id"]
    same_turn = (await env["run"](ctx, "confirm_action", confirmation_id=tok)).data
    assert not same_turn["ok"]
    s.user_turn_seq += 1
    first = (await env["run"](ctx, "confirm_action", confirmation_id=tok)).data
    again = (await env["run"](ctx, "confirm_action", confirmation_id=tok)).data
    assert first["ok"] and again["replayed"] and again["request_id"] == first["request_id"]
    assert len(env["ms"].q("SELECT id FROM tickets WHERE customer_id='C1001'")) == 1


async def test_token_from_another_session_is_rejected(env):
    s1, ctx1 = await env["login"](*PRIYA)
    s2, ctx2 = await env["login"](*PRIYA)
    tok = (await env["run"](ctx1, "propose_foreclosure_statement")).data["confirmation_id"]
    s2.user_turn_seq += 5
    assert not (await env["run"](ctx2, "confirm_action", confirmation_id=tok)).data["ok"]


async def test_cancel_action(env):
    s, ctx = await env["login"](*PRIYA)
    tok = (await env["run"](ctx, "propose_foreclosure_statement")).data["confirmation_id"]
    assert (await env["run"](ctx, "cancel_action", confirmation_id=tok)).data["ok"]
    s.user_turn_seq += 1
    assert not (await env["run"](ctx, "confirm_action", confirmation_id=tok)).data["ok"]


async def test_audit_log_is_append_only(env):
    import sqlite3

    import pytest
    s, ctx = await env["login"](*PRIYA)
    await env["run"](ctx, "propose_foreclosure_statement")
    with pytest.raises(sqlite3.IntegrityError):
        env["actions"].audit.conn.execute("DELETE FROM audit")
    with pytest.raises(sqlite3.IntegrityError):
        env["actions"].audit.conn.execute("UPDATE audit SET event='x'")


# ----------------------------------------------------------------- resilience
async def test_payments_down_gives_degraded_options_not_a_crash(env):
    _, ctx = await env["login"](*PRIYA)
    env["ms"].profiles["payments"]["down"] = True
    r = (await env["run"](ctx, "get_servicing_options")).data
    assert r["ok"] and r["unavailable_systems"] == ["payments"]
    r2 = (await env["run"](ctx, "get_payments")).data
    assert not r2["ok"] and r2["unavailable"] == "payments"


async def test_retries_absorb_intermittent_errors(env):
    _, ctx = await env["login"](*PRIYA)
    env["ms"].profiles["loans"]["error_rate"] = 0.3
    ok = 0
    for _ in range(100):
        if (await env["run"](ctx, "get_loan_summary")).data.get("ok"):
            ok += 1
    # Without retries 2 calls at 30% error succeed 49% of the time; with 3 attempts each, ~95%.
    assert ok >= 85


async def test_circuit_opens_then_recovers(env):
    _, ctx = await env["login"](*PRIYA)
    env["ms"].profiles["loans"]["down"] = True
    for _ in range(5):                      # 5 failed calls (each already retried) open the breaker
        await env["run"](ctx, "get_loan_summary")
    assert env["con"]["loans"].breaker.state == "open"
    r = (await env["run"](ctx, "get_loan_summary")).data
    assert r["error_kind"] == "circuit_open"
    env["ms"].profiles["loans"]["down"] = False
    import asyncio
    await asyncio.sleep(0.25)
    assert (await env["run"](ctx, "get_loan_summary")).data["ok"]
    assert env["con"]["loans"].breaker.state == "closed"


def test_breaker_state_machine_with_fake_clock():
    t = [0.0]
    b = Breaker(threshold=2, cooldown=10, clock=lambda: t[0])
    b.failure()
    assert b.state == "closed"
    b.failure()
    assert b.state == "open" and not b.allow()
    t[0] = 11
    assert b.state == "half_open" and b.allow() and not b.allow()   # one trial only
    b.failure()
    assert b.state == "open"
    t[0] = 22
    assert b.allow()
    b.success()
    assert b.state == "closed"


async def test_ops_requires_idempotency_key(env):
    import aiohttp
    async with aiohttp.ClientSession() as http:
        async with http.post(env["con"]["ops"].base_url + "/requests", json={"customer_id": "C1001", "type": "x"}) as r:
            assert r.status == 400
