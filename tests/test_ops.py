"""Operability: LLM fallback and budgets, rate limits, structured logs."""
import asyncio
import io
import json
import logging

import pytest

from ledgerflow import config
from ledgerflow.platform import logs
from ledgerflow.platform.gemini_text import GeminiREST, LLMError, TextAgent
from ledgerflow.platform.ratelimit import RateLimiter
from ledgerflow.platform.telemetry import METRICS

from .conftest import PRIYA


class FlakyGemini(GeminiREST):
    """GeminiREST with the network call replaced: listed models fail."""

    def __init__(self, failing: set[str]):
        super().__init__("k", "primary", ["fallback"])
        self.failing, self.calls = failing, []

    async def _call(self, model, system, contents, tools):
        self.calls.append(model)
        if model in self.failing:
            raise LLMError(f"{model} down")
        return {"role": "model", "parts": [{"text": f"answer from {model}"}]}


@pytest.mark.req("REQ-OPS-001.1")
async def test_llm_falls_back_to_second_model_and_breaker_skips_dead_primary():
    llm = FlakyGemini({"primary"})
    before = METRICS.counters.get("llm.text.fallback_used", 0)
    for _ in range(4):
        out = await llm.generate("s", [], None)
        assert out["parts"][0]["text"] == "answer from fallback"
    # after 3 failures the primary's breaker is open: the 4th turn goes straight to the fallback
    assert llm.calls == ["primary", "fallback"] * 3 + ["fallback"]
    assert METRICS.counters["llm.text.fallback_used"] == before + 4


@pytest.mark.req("REQ-OPS-001.1")
async def test_all_models_down_raises():
    with pytest.raises(LLMError):
        await FlakyGemini({"primary", "fallback"}).generate("s", [], None)


@pytest.mark.req("REQ-OPS-001.4")
async def test_turn_budget_stops_a_hung_turn(env, monkeypatch):
    s, _ = await env["login"](*PRIYA)
    monkeypatch.setattr(config, "LLM_TURN_BUDGET_S", 0.2)

    class Hung:
        async def generate(self, *a):
            await asyncio.sleep(5)
    events = []

    async def emit(e):
        events.append(e)
    reply = await TextAgent(Hung(), env["wfs"], env["con"], env["actions"]).turn(s, "hi", emit)
    assert "taking longer" in reply and events[-1]["type"] == "trace"


@pytest.mark.req("REQ-OPS-001.3")
def test_token_bucket_refills():
    t = [0.0]
    rl = RateLimiter(per_minute=60, burst=2, clock=lambda: t[0])
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a")
    assert rl.allow("b")                      # keys are independent
    t[0] = 1.0                                # one token per second
    assert rl.allow("a") and not rl.allow("a")


@pytest.mark.req("REQ-OPS-001.5")
def test_json_logs_carry_correlation_and_mask_pii():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logs.JsonFormatter())
    lg = logging.getLogger("test.json")
    lg.handlers[:] = [handler]
    lg.propagate = False
    token = logs.bind(request_id="r1", session=logs.session_hash("raw-session-id"))
    try:
        logs.event(lg, "login failed", level=logging.WARNING, phone=logs.mask_phone("9800000001"))
    finally:
        logs.reset(token)
    entry = json.loads(buf.getvalue())
    assert entry["severity"] == "WARNING" and entry["request_id"] == "r1"
    assert entry["phone"] == "******0001"
    assert "raw-session-id" not in buf.getvalue() and len(entry["session"]) == 12


@pytest.mark.req("REQ-OPS-001.6")
def test_golden_set_references_real_tools_and_value_matching_works():
    import importlib.util
    from pathlib import Path

    import yaml

    from ledgerflow.platform.tools import REGISTRY
    from ledgerflow.platform.workflow import load_workflows
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("lf_eval", root / "scripts" / "eval.py")
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    cases = yaml.safe_load((root / "evals" / "golden.yaml").read_text(encoding="utf-8"))["cases"]
    wfs = load_workflows()
    assert len(cases) >= 10
    for case in cases:
        wf = wfs[case.get("workflow", "loan_servicing")]
        assert case["customer"] in ev.CUSTOMERS
        for t in case["turns"]:
            for path in t.get("expect_tools", []) + [p.split(".")[0] for p in t.get("expect_values", []) + t.get("expect_clauses", [])]:
                tool = path.split(".")[0]
                assert tool in REGISTRY.specs and tool in wf.tools, (case["id"], tool)
    assert ev.value_stated("You save ₹21,003 in interest", "₹21,003.30")
    assert ev.value_stated("about twenty one thousand rupees", "₹21,003.30")
    assert not ev.value_stated("You save ₹25,000", "₹21,003.30")
    assert ev.dig({"a": {"b": "x"}}, "a.b") == "x" and ev.dig({}, "a.b") is None
