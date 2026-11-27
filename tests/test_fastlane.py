"""Fast lane: simple questions answered from the systems with no model call, and the
model-outage fallback."""
import pytest

from ledgerflow import config
from ledgerflow.platform import fastlane
from ledgerflow.platform.gemini_text import TextAgent
from ledgerflow.platform.grounding import Grounder

from .conftest import ANANYA, MEERA, PRIYA, RAHUL

LOAN_TOOLS = ["get_loan_summary", "explain_emi_change", "get_payments", "get_last_payment", "check_foreclosure",
              "get_servicing_options"]


class NoModel:
    """Fails the test if the model is called."""
    calls = 0

    async def generate(self, *a):
        NoModel.calls += 1
        raise AssertionError("the fast lane must not call the model")


class Down:
    async def generate(self, *a):
        raise RuntimeError("model outage")


async def _noop(e):
    return None


@pytest.mark.req("REQ-AGT-005.1")
@pytest.mark.parametrize("q,intent", [
    ("What's my next EMI?", "next_emi"), ("When is my EMI due?", "next_emi"), ("मेरी अगली EMI कब है?", "next_emi"),
    ("Meri agli EMI kab hai?", "next_emi"), ("Did my last payment go through?", "last_payment"),
    ("Any failed payments?", "failed_payments"), ("पिछले छह महीनों में कौन से पेमेंट फेल हुए?", "failed_payments"),
    ("Am I eligible for foreclosure without a penalty?", "foreclosure"), ("Why did my EMI go up?", "emi_change"),
    ("What are my options?", "options"), ("What is my outstanding balance?", "outstanding")])
def test_router_recognises_simple_questions(q, intent):
    assert fastlane.route(q, LOAN_TOOLS) == [intent]


@pytest.mark.req("REQ-AGT-005.2")
@pytest.mark.parametrize("q", [
    "My EMI increased this month. Why? And if I prepay ₹50,000 today, how much interest would I save?",
    "Show me the payments that failed in the last six months and explain what happened.",
    "Dispute the September one", "Yes, go ahead.", "482913", "Would paying 20,000 extra reduce my tenure?",
    "I want to talk to a person", "अगर मैं पचास हज़ार प्रीपे करूँ तो?", "hello"])
def test_router_leaves_anything_else_to_the_model(q):
    assert fastlane.route(q, LOAN_TOOLS) == []


@pytest.mark.req("REQ-AGT-005.1", "REQ-AGT-005.3")
@pytest.mark.parametrize("who,q,expect", [
    (PRIYA, "What's my next EMI?", ["₹17,323.49", "5 December 2026"]),
    (PRIYA, "Why did my EMI go up?", ["₹17,112.63", "₹17,323.49", "PL-FL-3.4"]),
    (PRIYA, "Any failed payments?", ["**3**", "Insufficient balance"]),
    (PRIYA, "Am I eligible for foreclosure without a penalty?", ["without a penalty", "PL-FL-5.1"]),
    (RAHUL, "Am I eligible for foreclosure without a penalty?", ["4% charge", "PL-FX-6.2"]),
    (MEERA, "Can I close my loan?", ["Not yet", "PL-FX-6.1"])])
async def test_fast_lane_answers_without_the_model_and_is_grounded(env, monkeypatch, who, q, expect):
    monkeypatch.setattr(config, "FAST_LANE", True)
    s, _ = await env["login"](*who)
    events = []

    async def emit(e):
        events.append(e)
    reply = await TextAgent(NoModel(), env["wfs"], env["con"], env["actions"]).turn(s, q, emit)
    for needle in expect:
        assert needle in reply, (needle, reply)
    assert Grounder().check(reply, s.memory["tool_results"], [q]) == []
    steps = next(e for e in events if e["type"] == "assistant")["steps"]
    assert steps[0]["text"].startswith("Recognised a direct question")
    assert any(e["type"] == "tool_finished" and e["card"] for e in events)       # the same cards as the model path


@pytest.mark.req("REQ-AGT-005.3")
async def test_fast_lane_answers_in_hindi(env, monkeypatch):
    monkeypatch.setattr(config, "FAST_LANE", True)
    s, _ = await env["login"](*PRIYA)
    reply = await TextAgent(NoModel(), env["wfs"], env["con"], env["actions"]).turn(s, "मेरी अगली EMI कब है?", _noop)
    assert "आपकी अगली EMI **₹17,323.49**" in reply and "दिसंबर" in reply


@pytest.mark.req("REQ-AGT-005.4")
async def test_model_outage_still_answers_what_the_systems_can(env):
    # the fast lane is OFF for normal turns here; the outage fallback must still work
    s, _ = await env["login"](*PRIYA)
    q = "Show me the payments that failed in the last six months and explain what happened."
    reply = await TextAgent(Down(), env["wfs"], env["con"], env["actions"]).turn(s, q, _noop)
    assert reply.startswith("My assistant service is unavailable right now")
    assert "**3**" in reply and "Insufficient balance" in reply


@pytest.mark.req("REQ-AGT-005.4")
async def test_model_outage_with_nothing_recognisable_says_so(env):
    s, _ = await env["login"](*PRIYA)
    reply = await TextAgent(Down(), env["wfs"], env["con"], env["actions"]).turn(s, "Tell me a joke", _noop)
    assert "trouble reaching my language service" in reply


@pytest.mark.req("REQ-AGT-005.5")
async def test_fast_lane_steps_aside_while_an_action_awaits_confirmation(env, monkeypatch):
    monkeypatch.setattr(config, "FAST_LANE", True)
    s, ctx = await env["login"](*PRIYA)
    await env["run"](ctx, "propose_foreclosure_statement")
    from .test_text_agent import ScriptedLLM, text
    llm = ScriptedLLM([text("Shall I still send the foreclosure statement first?")])
    await TextAgent(llm, env["wfs"], env["con"], env["actions"]).turn(s, "What's my next EMI?", _noop)
    assert len(llm.calls) == 1                                # the model keeps the pending request in view


@pytest.mark.req("REQ-AGT-005.1")
async def test_card_questions_take_the_fast_lane_in_the_card_workflow(env, monkeypatch):
    monkeypatch.setattr(config, "FAST_LANE", True)
    s, _ = await env["login"](*ANANYA, workflow="card_servicing")
    reply = await TextAgent(NoModel(), env["wfs"], env["con"], env["actions"]).turn(s, "What's my statement?", _noop)
    assert "statement balance" in reply and "minimum due" in reply


@pytest.mark.req("REQ-AGT-005.3")
async def test_options_answer_states_the_loan_position(env, monkeypatch):
    monkeypatch.setattr(config, "FAST_LANE", True)
    s, _ = await env["login"](*PRIYA)
    reply = await TextAgent(NoModel(), env["wfs"], env["con"], env["actions"]).turn(s, "What are my options?", _noop)
    assert "₹5,77,913.11" in reply and "₹17,323.49" in reply and "PL-FL-5.2" in reply
