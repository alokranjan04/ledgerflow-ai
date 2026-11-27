"""Admin panel API (products and customers) and customer sign-up, through the real gateway."""
import copy
import socket

import pytest
from aiohttp.test_utils import TestClient, TestServer

import app as lfapp
from ledgerflow import appkeys as K
from ledgerflow import config
from ledgerflow.mocks.server import MockSystems

from .conftest import AS_OF, PRIYA
from .test_durable import FakeToken, fake_firestore


def _free_port():
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


@pytest.fixture
async def client(tmp_path, monkeypatch):
    port = _free_port()
    for k, v in dict(DATA_DIR=tmp_path, DB_PATH=tmp_path / "lf.db", AUDIT_DB_PATH=tmp_path / "a.db", GEMINI_API_KEY="",
                     ADMIN_TOKEN="", PUBLIC_URL="", REDIS_URL="", SERVICE_ROLE="all", INTERNAL_TOKEN="",
                     LEDGERFLOW_AS_OF=AS_OF, MOCK_BASE_URL=f"http://127.0.0.1:{port}/mock").items():
        monkeypatch.setattr(config, k, v, raising=False)
    monkeypatch.setattr(config, "AS_OF", AS_OF)
    application = lfapp.make_app()
    for p in application[K.MOCKS].profiles.values():
        p.update(p50=1, p95=2, error_rate=0.0, down=False)
    c = TestClient(TestServer(application, port=port))
    await c.start_server()
    yield c
    await c.close()


async def spec_of(c, code):
    return (await (await c.get(f"/api/admin/products/{code}")).json())["spec"]


# ----------------------------------------------------------------- products
@pytest.mark.req("REQ-ADM-001.1", "REQ-ADM-001.2")
async def test_edit_a_policy_value_is_validated_versioned_and_used_at_once(client):
    c = client
    listed = (await (await c.get("/api/admin/products")).json())["products"]
    assert {p["code"] for p in listed} >= {"PL-FLOAT", "PL-FIX", "CC-PLAT"}
    assert next(p for p in listed if p["code"] == "PL-FLOAT")["source"] == "file"
    spec = await spec_of(c, "PL-FLOAT")
    spec["policy"]["rules"]["part_prepayment"]["min_amount"] = "15000"
    r = await c.post("/api/admin/products/PL-FLOAT", json={"spec": spec})
    assert r.status == 422 and "give the policy a new version" in str(await r.json())
    spec["policy"]["version"] = "2026.11"
    r = await c.post("/api/admin/products/PL-FLOAT", json={"spec": spec}, headers={"X-Admin-User": "asha"})
    body = await r.json()
    assert r.status == 200 and "policy.rules.part_prepayment.min_amount: \"10000\" -> \"15000\"" in body["changes"]
    # the assistant's tools see the new terms immediately (policy cache cleared)
    await c.post("/api/login", json={"phone": PRIYA[0], "dob": PRIYA[1]})
    from ledgerflow.platform.telemetry import Trace
    from ledgerflow.platform.tools import REGISTRY, ToolContext
    sess = next(iter(c.app[K.SESSIONS].sessions.values()))
    ctx = ToolContext(sess, c.app[K.CONNECTORS], c.app[K.ACTIONS], Trace(), AS_OF)
    res = await REGISTRY.run("simulate_prepayment", {"amount": 12000}, ctx, ["simulate_prepayment"])
    assert not res.data["eligible"] and "₹15,000" in " ".join(res.data["ineligible_reasons"])
    log = (await (await c.get("/api/admin/audit")).json())["entries"]
    assert log[0]["action"] == "product.updated" and log[0]["actor"] == "asha" and log[0]["target"] == "PL-FLOAT"
    # back to the file version
    r = await c.post("/api/admin/products/PL-FLOAT/revert", json={})
    assert r.status == 200 and (await spec_of(c, "PL-FLOAT"))["policy"]["version"] == "2026.10"


@pytest.mark.req("REQ-ADM-001.2")
async def test_bad_product_edits_are_refused_with_field_errors(client):
    c = client
    spec = await spec_of(c, "PL-FIX")
    bad = copy.deepcopy(spec)
    bad["policy"]["rules"]["bounce_charge"]["amount"] = "-5"
    bad["policy"]["rules"]["late_fee"] = {"amount": 100, "clause": "PL-FX-7.1"}
    bad["policy"]["version"] = "2026.05"
    errors = (await (await c.post("/api/admin/products/PL-FIX", json={"spec": bad})).json())["errors"]
    assert any("bounce_charge: amount must be 0 or more" in e for e in errors)
    assert any("late_fee: no calculation uses this rule" in e for e in errors)
    held = copy.deepcopy(spec)
    held["rate_type"] = "floating"
    held["policy"]["rules"]["rate_reset"] = {"benchmark": "repo", "frequency": "quarterly", "clause": "PL-FX-4.3"}
    held["policy"]["version"] = "2026.05"
    errors = (await (await c.post("/api/admin/products/PL-FIX", json={"spec": held})).json())["errors"]
    assert any("floating/fixed cannot change" in e for e in errors)
    assert (await c.post("/api/admin/products/PL-FIX/delete", json={})).status == 422     # from the file


@pytest.mark.req("REQ-ADM-001.1", "REQ-ADM-001.2")
async def test_create_and_delete_a_new_product(client):
    c = client
    spec = await spec_of(c, "CC-PLAT")
    spec["name"], spec["short_name"] = "LedgerFlow Gold Card", "Gold card"
    spec["policy"]["rules"]["dispute"]["window_days"] = 30
    r = await c.post("/api/admin/products/CC-GOLD", json={"spec": spec})
    assert r.status == 200 and (await r.json())["product"]["source"] == "admin"
    opts = await (await c.get("/api/signup/options")).json()
    assert "CC-GOLD" in {p["code"] for p in opts["products"]}
    r = await c.post("/api/admin/products/CC-GOLD/delete", json={})
    assert r.status == 200 and (await c.get("/api/admin/products/CC-GOLD")).status == 404


# ----------------------------------------------------------------- customers
NEW = {"name": "Kiran Desai", "phone": "9123456780", "dob": "1992-07-19", "city": "Surat", "segment": "salaried",
       "loan": {"product": "PL-FLOAT", "principal": 300000, "tenure_months": 24, "emis_paid": 5, "rate": 10.75}}


@pytest.mark.req("REQ-ADM-001.3", "REQ-ADM-001.4")
async def test_create_update_disable_and_enable_a_customer(client):
    c = client
    r = await c.post("/api/admin/customers", json={"persona": NEW})
    created = (await r.json())["customer"]
    assert r.status == 200 and created["id"].startswith("C1") and created["loan_product"] == "PL-FLOAT"
    dup = await c.post("/api/admin/customers", json={"persona": {**NEW, "name": "Someone Else"}})
    assert dup.status == 422 and "already registered" in str(await dup.json())
    cid = created["id"]
    assert (await c.post("/api/login", json={"phone": NEW["phone"], "dob": NEW["dob"]})).status == 200
    r = await c.post(f"/api/admin/customers/{cid}", json={"profile": {"city": "Vadodara", "demo": True,
                                                                     "story": "Admin-created test customer"}})
    assert r.status == 200 and (await r.json())["customer"]["city"] == "Vadodara"
    cfg = await (await c.get("/api/config")).json()
    assert "Kiran Desai" in {d["name"] for d in cfg["demo_customers"]}          # now on the sign-in list
    bad = await c.post(f"/api/admin/customers/{cid}", json={"profile": {"dob": "2015-01-01", "phone": "12345"}})
    errs = str(await bad.json())
    assert bad.status == 422 and "18 to 100" in errs and "10-digit" in errs
    assert (await c.post(f"/api/admin/customers/{cid}/status", json={"status": "disabled", "reason": "fraud check"})).status == 200
    assert (await c.post("/api/login", json={"phone": NEW["phone"], "dob": NEW["dob"]})).status == 401
    assert "Kiran Desai" not in {d["name"] for d in (await (await c.get("/api/config")).json())["demo_customers"]}
    await c.post(f"/api/admin/customers/{cid}/status", json={"status": "active"})
    assert (await c.post("/api/login", json={"phone": NEW["phone"], "dob": NEW["dob"]})).status == 200
    found = (await (await c.get("/api/admin/customers", params={"q": "Kiran"})).json())["customers"]
    assert [x["id"] for x in found] == [cid]
    actions = [e["action"] for e in (await (await c.get("/api/admin/audit")).json())["entries"]]
    assert actions[:4] == ["customer.enabled", "customer.disabled", "customer.updated", "customer.created (admin)"]


@pytest.mark.req("REQ-ADM-001.5")
async def test_admin_api_needs_the_token_when_deployed(client, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "https://x.run.app")
    monkeypatch.setattr(config, "ADMIN_TOKEN", "s3cret")
    for path in ("/api/admin/products", "/api/admin/customers", "/api/admin/audit", "/api/admin/schema"):
        assert (await client.get(path)).status == 403
    assert (await client.post("/api/admin/products/PL-FLOAT", json={"spec": {}})).status == 403
    assert (await client.get("/api/admin/products", headers={"X-Admin-Token": "s3cret"})).status == 200
    assert (await client.get("/admin")).status == 200          # the page itself is static; its data needs the token


# ----------------------------------------------------------------- persistence
@pytest.mark.req("REQ-ADM-001.6")
async def test_admin_changes_survive_a_redeploy(tmp_path):
    from ledgerflow.platform.durable import FirestoreStore
    fapp, docs, _ = fake_firestore()
    srv = TestServer(fapp)
    await srv.start_server()
    store = FirestoreStore("p", base_url=str(srv.make_url("/v1")), token=FakeToken(), prefix="lf")
    try:
        a = MockSystems(str(tmp_path / "a.db"), AS_OF)
        await a.restore(store)
        spec = copy.deepcopy(a.products["PL-FIX"])
        spec["policy"]["rules"]["bounce_charge"]["amount"] = "650"
        spec["policy"]["version"] = "2026.09"
        assert (await a.admin.save_product("PL-FIX", spec))["ok"]
        cid = (await a.admin.create_customer(NEW, source="signup"))["customer"]["id"]
        await a.admin.update_customer("C1002", {"city": "Nagpur"})
        await a.admin.set_status("C1003", "disabled", "left the bank")
        await store.drain()

        b = MockSystems(str(tmp_path / "b.db"), AS_OF)               # new container
        await b.restore(store)
        assert b.products["PL-FIX"]["policy"]["rules"]["bounce_charge"]["amount"] == "650"
        assert b.q("SELECT source FROM customers WHERE id=?", cid)[0]["source"] == "signup"
        assert b.q("SELECT COUNT(*) n FROM loans WHERE customer_id=?", cid)[0]["n"] == 1
        assert b.q("SELECT city FROM customers WHERE id='C1002'")[0]["city"] == "Nagpur"
        assert b.q("SELECT status FROM customers WHERE id='C1003'")[0]["status"] == "disabled"
        assert [e["action"] for e in b.admin.audit_log()][0] == "customer.disabled"
    finally:
        await store.close()
        await srv.close()


# ----------------------------------------------------------------- sign-up
FORM = {"name": "Neha Kulkarni", "phone": "9988776655", "dob": "1995-03-08", "city": "Nashik", "consent": True,
        "loan": {"product": "PL-FLOAT", "principal": 250000, "tenure_months": 36, "emis_paid": 4},
        "card": {"product": "CC-PLAT", "limit": 100000}}


@pytest.mark.req("REQ-REG-001.1", "REQ-REG-001.2", "REQ-REG-001.3")
async def test_sign_up_with_a_code_creates_the_customer_and_signs_them_in(client):
    c = client
    opts = await (await c.get("/api/signup/options")).json()
    assert {"PL-FLOAT", "PL-FIX", "CC-PLAT"} <= {p["code"] for p in opts["products"]}
    r = await c.post("/api/signup/start", json=FORM)
    start = await r.json()
    assert r.status == 200 and start["sent_to"] == "******6655" and len(start["demo_code"]) == 6
    assert not (await (await c.get("/api/admin/customers", params={"q": "Neha"})).json())["customers"]  # not yet
    wrong = await c.post("/api/signup/verify", json={"signup_id": start["signup_id"], "code": "000000"})
    assert wrong.status == 400 and "2 attempt(s) left" in str(await wrong.json())
    ok = await c.post("/api/signup/verify", json={"signup_id": start["signup_id"], "code": start["demo_code"]})
    body = await ok.json()
    assert ok.status == 200 and body["customer_name"] == "Neha Kulkarni" and body["workflow"] == "loan_servicing"
    account = await (await c.get("/api/account")).json()                  # signed in by the cookie
    assert account["loan"]["product"] == "PL-FLOAT" and account["card"]["card"].startswith("Platinum card ending ")
    found = (await (await c.get("/api/admin/customers", params={"q": "Neha"})).json())["customers"][0]
    assert found["source"] == "signup" and found["card_product"] == "CC-PLAT" and not found["demo"]
    # the same number again: refused only after the code, so the form cannot reveal who banks here
    again = await (await c.post("/api/signup/start", json={**FORM, "name": "Other Person"})).json()
    assert again["ok"]
    dup = await c.post("/api/signup/verify", json={"signup_id": again["signup_id"], "code": again["demo_code"]})
    assert dup.status == 409 and (await dup.json())["registered"]


@pytest.mark.req("REQ-REG-001.4")
async def test_sign_up_form_errors_are_listed_before_any_code(client):
    bad = {**FORM, "dob": "2012-01-01", "consent": False, "phone": "12345",
           "loan": {**FORM["loan"], "principal": 1000, "tenure_months": 7}, "card": {"product": "PL-FIX", "limit": 5}}
    r = await client.post("/api/signup/start", json=bad)
    text = str(await r.json())
    assert r.status == 422
    for needle in ("consent", "loan amount", "loan tenure", "card product", "card limit"):
        assert needle in text, needle
    r = await client.post("/api/signup/start", json={**FORM, "dob": "2012-01-01"})
    assert r.status == 422 and "18 to 100" in str(await r.json())


@pytest.mark.req("REQ-REG-001.5")
async def test_sign_up_is_rate_limited(client):
    sent = [await client.post("/api/signup/start", json=FORM) for _ in range(4)]
    assert [s.status for s in sent] == [200, 200, 200, 429]                      # 3 codes per number
    more = [await client.post("/api/signup/start", json={**FORM, "phone": f"98700000{i:02d}"}) for i in range(3)]
    assert more[-1].status == 429                                                   # 5 applications per client
