"""Hindi / Hinglish conversations and the KPI dashboard (in-app + Cloud Monitoring as code)."""
import asyncio
import io
import json
import logging
import re
import socket
from pathlib import Path

import pytest
import websockets
from aiohttp.test_utils import TestClient, TestServer

import app as lfapp
from ledgerflow import config
from ledgerflow.platform import language as lang
from ledgerflow.platform.gemini_text import TextAgent
from ledgerflow.platform.grounding import Grounder
from ledgerflow.voice.pipeline import VoiceConversation

from .conftest import PRIYA
from .test_text_agent import ScriptedLLM, fc, text
from .test_voice import FakeTransport

ROOT = Path(__file__).resolve().parent.parent
TOOLS = [{"new_emi": "₹17,323.49", "reduce_tenure": {"interest_saved": "₹21,003.30", "tenure_reduced_by_months": 5},
          "x": "₹87,000"}]
G = Grounder()


async def _noop(e):
    return None


# ----------------------------------------------------------------- detection and prompts
@pytest.mark.req("REQ-LNG-001.1")
def test_language_detection():
    assert lang.detect("मेरी EMI इस महीने क्यों बढ़ गई?") == "hi"
    assert lang.detect("Meri EMI kitni hai aur kab hai?") == "hi-Latn"
    assert lang.detect("What is my next EMI?") == "en"
    assert lang.detect("EMI kab?") == "en"                 # one Hinglish word is not enough


@pytest.mark.req("REQ-LNG-001.2", "REQ-LNG-001.4")
def test_prompt_language_block_keeps_figures_and_locks_voice():
    hi_text = lang.instruction("hi", "text")
    assert "Devanagari" in hi_text and "₹17,323.49" in hi_text and "clause IDs" in hi_text
    hi_voice = lang.instruction("hi", "voice")
    assert "BOTH directions" in hi_voice and "unclear audio" in hi_voice


# ----------------------------------------------------------------- grounding in Hindi
@pytest.mark.req("REQ-LNG-001.3")
def test_grounding_understands_hindi_and_hinglish_numbers():
    ok = ["आपकी नई EMI ₹१७,३२३.४९ है", "आप लगभग इक्कीस हज़ार रुपये बचाएंगे", "आप सत्तासी हज़ार रुपये बचाएंगे",
          "Aapki EMI 17,323 rupaye hai", "tenure 5 mahine kam hoga", "आपकी अवधि पांच महीने कम होगी"]
    bad = ["आप पचास हज़ार रुपये बचाएंगे", "tenure 9 mahine kam hoga", "50 hazaar bachenge", "आपकी अवधि नौ महीने कम होगी"]
    for t in ok:
        assert G.check(t, TOOLS, []) == [], t
    for t in bad:
        assert G.check(t, TOOLS, []), t


@pytest.mark.req("REQ-LNG-001.3")
def test_english_compound_number_words_regression():
    assert G.check("You save eighty seven thousand rupees", TOOLS, []) == []
    assert G.check("You save seventy eight thousand rupees", TOOLS, [])


# ----------------------------------------------------------------- text runtime
@pytest.mark.req("REQ-LNG-001.1", "REQ-LNG-001.2")
async def test_first_hindi_turn_locks_the_session_language(env):
    s, _ = await env["login"](*PRIYA)
    llm = ScriptedLLM([fc("get_loan_summary"), text("आपकी अगली EMI ₹17,323.49 है।"), text("ठीक है।")])
    agent = TextAgent(llm, env["wfs"], env["con"], env["actions"])
    await agent.turn(s, "मेरी अगली EMI कितनी है?", _noop)
    assert s.language == "hi" and "Devanagari" in llm.calls[0]["system"]
    await agent.turn(s, "ok thanks", _noop)                 # an English word later does not flip it
    assert s.language == "hi" and "Devanagari" in llm.calls[-1]["system"]


@pytest.mark.req("REQ-LNG-001.5")
async def test_system_messages_follow_the_language(env):
    s, _ = await env["login"](*PRIYA)
    s.language = "hi"

    class Down:
        async def generate(self, *a):
            raise RuntimeError("boom")
    reply = await TextAgent(Down(), env["wfs"], env["con"], env["actions"]).turn(s, "नमस्ते", _noop)
    assert "माफ़ कीजिए" in reply


# ----------------------------------------------------------------- voice
@pytest.mark.req("REQ-LNG-001.4")
async def test_phone_greets_bilingually_and_voice_uses_session_language(env, monkeypatch):
    s = await env["store"].create("loan_servicing", "phone")
    seen = {}

    async def handler(ws):
        seen["setup"] = json.loads(await ws.recv())["setup"]
        await ws.send(json.dumps({"setupComplete": {}}))
        await ws.recv()
        await ws.close()
    server = await websockets.serve(handler, "127.0.0.1", 0)
    monkeypatch.setattr(config, "GEMINI_WS_URL", f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
    try:
        await asyncio.wait_for(VoiceConversation(s, env["wfs"]["loan_servicing"], FakeTransport("phone"),
                                                 env["con"], env["actions"]).run(), 10)
    finally:
        server.close()
    system = seen["setup"]["systemInstruction"]["parts"][0]["text"]
    assert "नमस्ते" in system and "English or Hindi" in system and "BOTH directions" in system


# ----------------------------------------------------------------- app: language API, dashboard, metrics lock
def _free_port():
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


@pytest.fixture
async def client(tmp_path, monkeypatch):
    port = _free_port()
    for k, v in dict(DATA_DIR=tmp_path, DB_PATH=tmp_path / "lf.db", AUDIT_DB_PATH=tmp_path / "a.db", GEMINI_API_KEY="",
                     ADMIN_TOKEN="", PUBLIC_URL="", REDIS_URL="", SERVICE_ROLE="all", INTERNAL_TOKEN="",
                     MOCK_BASE_URL=f"http://127.0.0.1:{port}/mock").items():
        monkeypatch.setattr(config, k, v)
    application = lfapp.make_app()
    c = TestClient(TestServer(application, port=port))
    await c.start_server()
    yield c
    await c.close()


@pytest.mark.req("REQ-LNG-001.6")
async def test_language_api_and_hindi_suggestions(client):
    cfg = await (await client.get("/api/config")).json()
    assert {lg["code"] for lg in cfg["languages"]} == {"auto", "en", "hi", "hi-Latn"}
    assert any("EMI" in x for x in cfg["workflows"][0]["suggestions_hi"])
    r = await client.post("/api/login", json={"phone": PRIYA[0], "dob": PRIYA[1], "language": "hi"})
    assert (await r.json())["language"] == "hi"
    assert (await (await client.post("/api/language", json={"language": "fr"})).json())["ok"] is False
    assert (await (await client.post("/api/language", json={"language": "hi-Latn"})).json())["language"] == "hi-Latn"


@pytest.mark.req("REQ-KPI-001.1", "REQ-KPI-001.3")
async def test_dashboard_page_and_metrics_are_admin_only_when_deployed(client, monkeypatch):
    page = await (await client.get("/dashboard")).text()
    assert "KPI dashboard" in page
    m = await (await client.get("/api/metrics")).json()
    assert {"containment_rate", "sessions_engaged", "handoffs", "actions_executed"} <= set(m["kpis"])
    monkeypatch.setattr(config, "PUBLIC_URL", "https://x.run.app")
    assert (await client.get("/api/metrics")).status == 403
    monkeypatch.setattr(config, "ADMIN_TOKEN", "t0k")
    assert (await client.get("/api/metrics", headers={"X-Admin-Token": "t0k"})).status == 200


@pytest.mark.req("REQ-KPI-001.2")
async def test_engagement_is_logged_once_per_conversation(env):
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    lg = logging.getLogger("ledgerflow.kpi")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        s, _ = await env["login"](*PRIYA)
        agent = TextAgent(ScriptedLLM([text("Hi"), text("Again")]), env["wfs"], env["con"], env["actions"])
        await agent.turn(s, "hi", _noop)
        await agent.turn(s, "hello", _noop)
    finally:
        lg.removeHandler(h)
    assert buf.getvalue().count("conversation engaged") == 1


# ----------------------------------------------------------------- monitoring as code
@pytest.mark.req("REQ-KPI-001.4")
def test_log_based_metrics_match_what_the_code_logs():
    code = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "ledgerflow").rglob("*.py")) + \
        (ROOT / "app.py").read_text(encoding="utf-8")
    names = set()
    for f in (ROOT / "monitoring" / "metrics").glob("*.json"):
        m = json.loads(f.read_text(encoding="utf-8"))
        names.add(m["name"])
        assert m["name"] == f.stem and 'service_name="ledgerflow-ai"' in m["filter"]
        for msg in re.findall(r'jsonPayload\.message="([^"]+)"', m["filter"]):
            assert f'"{msg}"' in code, f"{f.name}: no log line '{msg}' in the code"
        for field in re.findall(r"jsonPayload\.(\w+)", json.dumps(m.get("labelExtractors", {})) + m.get("valueExtractor", "")):
            assert re.search(rf"\b{field}=", code) or f'"{field}"' in code, f"{f.name}: field {field} never logged"
    dash = json.loads((ROOT / "monitoring" / "dashboard.json").read_text(encoding="utf-8"))
    used = set(re.findall(r"user[/_](lf_\w+)", json.dumps(dash)))
    assert used and used <= names, used - names
