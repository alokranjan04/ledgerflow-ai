"""Browser end-to-end tests: the real app in a real browser (Playwright), plus an axe-core
accessibility scan. The server runs with no model key, so answers come from the fast lane and
the outage fallback: deterministic, offline, and a test of both.

Run:  pytest tests/e2e -q          (needs Microsoft Edge locally, or `playwright install chromium`;
                                    set E2E_BROWSER_CHANNEL="" to use the bundled Chromium)
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pw = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = _free_port()
    env = {**os.environ, "PORT": str(port), "GEMINI_API_KEY": "", "PUBLIC_URL": "", "ADMIN_TOKEN": "",
           "LOG_JSON": "0", "FAST_LANE": "1", "REDIS_URL": "", "SERVICE_ROLE": "all",
           "DATA_DIR": str(tmp_path_factory.mktemp("e2e-data"))}
    # Server output goes to a file: an unread pipe fills up and blocks the server mid-request.
    log = open(tmp_path_factory.mktemp("e2e-log") / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            import urllib.request
            if urllib.request.urlopen(url + "/readyz", timeout=1).status == 200:
                break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail("server did not become ready")
    yield url
    proc.terminate()
    proc.wait(10)
    log.close()


@pytest.fixture(scope="module")
def browser():
    channel = os.environ.get("E2E_BROWSER_CHANNEL", "msedge") or None
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel=channel, headless=True)
        except Exception as e:  # no Edge and no bundled Chromium
            pytest.skip(f"no browser available: {e}")
        yield b
        b.close()


@pytest.fixture
def page(browser, server):
    # bypass_csp lets Playwright's own wait helpers run; the app's CSP is tested in test_app.py
    ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, bypass_csp=True)
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(server + "/")
    yield pg
    ctx.close()
    assert not errors, f"JavaScript errors: {errors}"


def sign_in(pg, label: str):
    pg.click(f"text={label}")
    pg.wait_for_selector("#customer-name")
    pg.wait_for_function("document.querySelector('#account-panel').textContent.length > 20")


def ask(pg, text: str):
    before = pg.locator(".msg.assistant").count()
    pg.fill("#input", text)
    pg.click("#composer button[type=submit]")
    pg.wait_for_function(f"document.querySelectorAll('.msg.assistant').length > {before}", timeout=30000)
    return pg.locator(".msg.assistant").last.inner_text()


@pytest.mark.req("REQ-UI-001.6")
def test_sign_in_account_panel_and_fast_lane_answer_with_card_and_steps(page):
    sign_in(page, "Priya Sharma · 9800000001")
    assert "₹5,77,913.11" in page.inner_text("#account-panel")
    reply = ask(page, "What's my next EMI?")
    assert "₹17,323.49" in reply and "5 December 2026" in reply
    assert page.locator(".card").count() >= 1                                   # loan summary card
    page.click(".agent-steps summary")
    steps = page.inner_text(".agent-steps")
    assert "no AI model call" in steps and "Loan Management" in steps


@pytest.mark.req("REQ-UI-001.6")
def test_model_outage_fallback_is_visible_and_useful(page):
    sign_in(page, "Priya Sharma · 9800000001")
    reply = ask(page, "Show me the payments that failed in the last six months and explain what happened.")
    assert "assistant service is unavailable" in reply and "Insufficient balance" in reply
    assert page.locator(".card table").count() >= 1                            # payments table card


@pytest.mark.req("REQ-UI-001.6")
def test_hindi_language_switch_and_workflow_switch(page):
    sign_in(page, "Priya Sharma · 9800000001")
    page.select_option("#lang", "hi")
    page.wait_for_function("document.querySelector('#suggestions').textContent.includes('EMI')")
    reply = ask(page, "मेरी अगली EMI कब है?")
    assert "₹17,323.49" in reply and "दिसंबर" in reply
    page.click("#workflow-switch >> text=Credit card")
    page.wait_for_function("document.querySelector('#account-panel').textContent.includes('Statement balance')")
    reply = ask(page, "What's my statement?")
    assert "₹" in reply and ("statement" in reply.lower() or "स्टेटमेंट" in reply)


@pytest.mark.req("REQ-UI-001.6")
def test_wrong_details_are_refused(page):
    page.fill("#login-phone", "9800000001")
    page.fill("#login-dob", "2000-01-01")
    page.click("#login-form button[type=submit]")
    page.wait_for_function("document.querySelector('#login-error').textContent.length > 0")
    assert "did not match" in page.inner_text("#login-error")


@pytest.mark.req("REQ-UI-001.6")
def test_ops_dashboard_and_agent_desk_pages_load(browser, server):
    ctx = browser.new_context(bypass_csp=True)
    pg = ctx.new_page()
    pg.goto(server + "/dashboard")
    pg.wait_for_selector(".tile")
    assert "containment" in pg.inner_text("#tiles").lower()
    pg.goto(server + "/agent")
    pg.wait_for_function("document.querySelector('#queue').textContent.length > 0")
    ctx.close()


@pytest.mark.req("REQ-UI-001.4")
def test_accessibility_no_serious_violations(page):
    from axe_playwright_python.sync_playwright import Axe
    axe = Axe()
    results = {"sign-in": axe.run(page)}
    sign_in(page, "Priya Sharma · 9800000001")
    ask(page, "What's my next EMI?")
    results["chat"] = axe.run(page)
    problems = []
    for screen, r in results.items():
        for v in r.response["violations"]:
            if v["impact"] in ("serious", "critical"):
                problems.append(f"{screen}: {v['id']} ({v['impact']}) x{len(v['nodes'])} — {v['help']}")
    assert not problems, "\n".join(problems)


@pytest.mark.req("REQ-UI-001.6")
def test_no_console_errors_on_load(page):
    assert re.search(r"LedgerFlow", page.title())
