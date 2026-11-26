"""Text agent loop with a scripted LLM (deterministic, offline)."""
import pytest

from ledgerflow.platform.gemini_text import TextAgent
from ledgerflow.platform.telemetry import METRICS

from .conftest import PRIYA


class ScriptedLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def generate(self, system, contents, tools):
        self.calls.append({"system": system, "contents": list(contents), "tools": tools})
        return self.script.pop(0)


def fc(name, **args):
    return {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}}]}


def text(t):
    return {"role": "model", "parts": [{"text": t}]}


async def _agent(env, script):
    llm = ScriptedLLM(script)
    agent = TextAgent(llm, env["wfs"], env["con"], env["actions"])
    return agent, llm


@pytest.mark.req("REQ-AGT-002.1", "REQ-AGT-002.2", "REQ-SEC-001.4")
async def test_parallel_tools_and_grounded_answer(env):
    s, _ = await env["login"](*PRIYA)
    both = {"role": "model", "parts": [{"functionCall": {"name": "explain_emi_change", "args": {}}},
                                       {"functionCall": {"name": "simulate_prepayment", "args": {"amount": 50000}}}]}
    agent, llm = await _agent(env, [both, text(
        "Your EMI went up from **₹17,112.63** to **₹17,323.49** after the rate moved from 10.25% to 11%. "
        "Prepaying ₹50,000 and keeping the EMI saves **₹21,003.30** in interest.")])
    events = []

    async def emit(e):
        events.append(e)
    reply = await agent.turn(s, "Why did my EMI go up and what if I prepay 50,000?", emit)
    assert "₹21,003.30" in reply
    kinds = [e["type"] for e in events]
    assert kinds.count("tool_started") == 2 and kinds.count("tool_finished") == 2
    assert {e["card"]["type"] for e in events if e["type"] == "tool_finished"} == {"emi_change", "prepayment"}
    # tool declarations sent to the model never include customer_id
    assert "customer_id" not in str(llm.calls[0]["tools"])


@pytest.mark.req("REQ-AGT-004.4")
async def test_hallucinated_number_is_regenerated(env):
    s, _ = await env["login"](*PRIYA)
    agent, llm = await _agent(env, [fc("get_loan_summary"), text("Your next EMI is ₹18,000."),
                                    text("Your next EMI is ₹17,323.49, due 5 December.")])
    reply = await agent.turn(s, "What's my next EMI?", lambda e: _noop(e))
    assert reply == "Your next EMI is ₹17,323.49, due 5 December."
    assert len(llm.calls) == 3 and "[GROUNDING CHECK]" in str(llm.calls[2]["contents"][-1])


@pytest.mark.req("REQ-AGT-004.4")
async def test_persistent_hallucination_falls_back_to_tool_values(env):
    s, _ = await env["login"](*PRIYA)
    before = METRICS.counters.get("grounding.fallback", 0)
    agent, _ = await _agent(env, [fc("get_loan_summary"), text("It is ₹99,999."), text("It is ₹88,888.")])
    reply = await agent.turn(s, "What's my next EMI?", lambda e: _noop(e))
    assert "₹17,323.49" in reply and "99,999" not in reply
    assert METRICS.counters["grounding.fallback"] == before + 1


@pytest.mark.req("REQ-AGT-002.3")
async def test_context_carries_over_turns(env):
    s, _ = await env["login"](*PRIYA)
    agent, llm = await _agent(env, [fc("get_payments", status="failed"), text("You had 3 failed payments."),
                                    text("Shall I raise a dispute for the September one?")])
    await agent.turn(s, "Show failed payments", lambda e: _noop(e))
    await agent.turn(s, "dispute the September one", lambda e: _noop(e))
    second = llm.calls[-1]["contents"]
    assert any("functionResponse" in p for c in second for p in c["parts"])   # earlier tool results still in context


@pytest.mark.req("REQ-AGT-002.4")
async def test_llm_outage_is_reported_not_raised(env):
    s, _ = await env["login"](*PRIYA)

    class Down:
        async def generate(self, *a):
            raise RuntimeError("boom")
    agent = TextAgent(Down(), env["wfs"], env["con"], env["actions"])
    reply = await agent.turn(s, "hi", lambda e: _noop(e))
    assert "trouble" in reply


async def _noop(e):
    return None
