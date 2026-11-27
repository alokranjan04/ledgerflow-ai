"""Durable records: requests and the audit trail survive a redeploy (Firestore over REST)."""
import json
import sqlite3

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from ledgerflow.mocks.server import MockSystems, build_mock_app
from ledgerflow.platform.actions import AuditLog
from ledgerflow.platform.durable import FirestoreStore, NullStore, build_store
from ledgerflow.platform.sessions import Session
from ledgerflow.platform.telemetry import METRICS

from .conftest import AS_OF


class FakeToken:
    async def header(self):
        return {"Authorization": "Bearer test"}


def fake_firestore(fail: bool = False):
    """The three Firestore REST calls the store makes: PATCH (upsert), POST ?documentId= (create-only)
    and GET (list, paged). Documents are kept per collection."""
    docs: dict[str, dict[str, dict]] = {}
    seen_auth: list[str] = []

    async def upsert(req):
        seen_auth.append(req.headers.get("Authorization", ""))
        if fail:
            return web.json_response({"error": "unavailable"}, status=503)
        docs.setdefault(req.match_info["coll"], {})[req.match_info["doc"]] = (await req.json())["fields"]
        return web.json_response({})

    async def create(req):
        if fail:
            return web.json_response({"error": "unavailable"}, status=503)
        coll, doc = docs.setdefault(req.match_info["coll"], {}), req.query["documentId"]
        if doc in coll:
            return web.json_response({"error": "exists"}, status=409)
        coll[doc] = (await req.json())["fields"]
        return web.json_response({})

    async def listing(req):
        items = sorted(docs.get(req.match_info["coll"], {}).items())
        start = int(req.query.get("pageToken") or 0)
        page = items[start:start + 2]                 # tiny pages, so paging is exercised
        body = {"documents": [{"name": k, "fields": v} for k, v in page]}
        if start + 2 < len(items):
            body["nextPageToken"] = str(start + 2)
        return web.json_response(body)

    app = web.Application()
    base = "/v1/projects/p/databases/(default)/documents/{coll}"
    app.router.add_patch(base + "/{doc}", upsert)
    app.router.add_post(base, create)
    app.router.add_get(base, listing)
    return app, docs, seen_auth


@pytest.fixture
async def firestore():
    servers = []

    async def start(fail=False):
        app, docs, auth = fake_firestore(fail)
        srv = TestServer(app)
        await srv.start_server()
        servers.append(srv)
        store = FirestoreStore("p", base_url=str(srv.make_url("/v1")), token=FakeToken(), prefix="lf")
        return store, docs, auth
    yield start
    for s in servers:
        await s.close()


async def _systems(tmp_path, name, durable):
    """A fresh 'container': new SQLite database, restored from the durable store, served over HTTP."""
    ms = MockSystems(str(tmp_path / f"{name}.db"), AS_OF)
    for p in ms.profiles.values():
        p.update(p50=1, p95=2, error_rate=0.0, down=False)
    await ms.restore(durable)
    root = web.Application()
    root.add_subapp("/mock", build_mock_app(ms))
    c = TestClient(TestServer(root))
    await c.start_server()
    return ms, c


REQUEST = {"customer_id": "C1001", "type": "foreclosure_statement", "summary": "Foreclosure letter", "payload": {"x": 1}}


@pytest.mark.req("REQ-DUR-001.1", "REQ-DUR-001.2")
async def test_requests_survive_a_redeploy_and_keys_still_replay(tmp_path, firestore):
    store, docs, auth = await firestore()
    _, before = await _systems(tmp_path, "before", store)
    r = await before.post("/mock/ops/requests", json=REQUEST, headers={"Idempotency-Key": "k-1"})
    created = await r.json()
    assert r.status == 201 and created["id"] in docs["lf_tickets"]          # stored before answering
    assert (await before.post(f"/mock/ops/requests/{created['id']}/escalate")).status == 200
    await before.close()

    ms, after = await _systems(tmp_path, "after", store)                    # new container, empty database
    listed = await (await after.get("/mock/ops/requests", params={"customer_id": "C1001"})).json()
    assert [x["id"] for x in listed] == [created["id"]] and listed[0]["status"] == "escalated"
    replay = await (await after.post("/mock/ops/requests", json=REQUEST, headers={"Idempotency-Key": "k-1"})).json()
    assert replay["id"] == created["id"] and replay.get("replayed")
    assert ms.q("SELECT COUNT(*) n FROM tickets")[0]["n"] == 1
    assert set(auth) == {"Bearer test"}
    await after.close()
    await store.close()


@pytest.mark.req("REQ-DUR-001.3")
async def test_audit_trail_is_restored_in_order_and_stays_append_only(tmp_path, firestore):
    store, docs, _ = await firestore()
    s = Session(id="sid-1", workflow_id="loan_servicing", customer_id="C1001")
    a1 = AuditLog(tmp_path / "a1.db", durable=store)
    for ev in ("proposed", "step_up_challenged", "verified", "executed"):
        a1.write(s, "foreclosure_statement", ev, token="t1")
    await store.drain()
    assert len(docs["lf_audit"]) == 4
    # a retried create-only write succeeds without overwriting
    first = sorted(docs["lf_audit"])[0]
    assert await store.put("audit", first, {"event": "tampered"}, create_only=True)
    assert json.loads(docs["lf_audit"][first]["json"]["stringValue"])["event"] == "proposed"

    a2 = AuditLog(tmp_path / "a2.db", durable=store)
    assert await a2.restore() == 4
    assert [e["event"] for e in a2.for_customer("C1001")] == ["executed", "verified", "step_up_challenged", "proposed"]
    assert await a2.restore() == 0                                        # only into an empty table
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        a2.conn.execute("DELETE FROM audit")
    await store.close()


@pytest.mark.req("REQ-DUR-001.4")
async def test_store_outage_never_breaks_the_customer(tmp_path, firestore):
    store, _, _ = await firestore(fail=True)
    before = METRICS.counters.get("durable.write_failed", 0)
    ms = MockSystems(str(tmp_path / "down.db"), AS_OF)
    ms.durable = store
    for p in ms.profiles.values():
        p.update(p50=1, p95=2, error_rate=0.0, down=False)
    root = web.Application()
    root.add_subapp("/mock", build_mock_app(ms))
    c = TestClient(TestServer(root))
    await c.start_server()
    try:
        r = await c.post("/mock/ops/requests", json=REQUEST, headers={"Idempotency-Key": "k-2"})
        assert r.status == 201
        assert METRICS.counters["durable.write_failed"] == before + 1
    finally:
        await c.close()
        await store.close()


@pytest.mark.req("REQ-DUR-001.4")
async def test_a_failed_restore_does_not_stop_start_up(tmp_path, monkeypatch):
    import app as lfapp
    from ledgerflow import appkeys as K
    from ledgerflow import config
    for k, v in dict(DATA_DIR=tmp_path, DB_PATH=tmp_path / "lf.db", AUDIT_DB_PATH=tmp_path / "a.db", GEMINI_API_KEY="",
                     ADMIN_TOKEN="", PUBLIC_URL="", REDIS_URL="", SERVICE_ROLE="all", INTERNAL_TOKEN="").items():
        monkeypatch.setattr(config, k, v)

    class Broken(NullStore):
        enabled, backend = True, "broken"

        async def all(self, collection):
            raise RuntimeError("firestore down")
    monkeypatch.setattr(lfapp, "_durable", lambda: Broken())
    c = TestClient(TestServer(lfapp.make_app()))
    await c.start_server()
    try:
        assert c.app[K.DURABLE].backend == "broken"
        assert (await c.get("/healthz")).status == 200
    finally:
        await c.close()


@pytest.mark.req("REQ-DUR-001.5", "REQ-DUR-001.6")
def test_durability_is_opt_in_rest_based_and_needs_a_project():
    assert not build_store("none", "p", "(default)", "lf").enabled
    fs = build_store("firestore", "p", "(default)", "lf")
    assert fs.enabled and fs.root == "https://firestore.googleapis.com/v1/projects/p/databases/(default)/documents"
    assert fs._coll("audit") == "lf_audit"
    with pytest.raises(SystemExit):
        build_store("firestore", "", "(default)", "lf")
    with pytest.raises(SystemExit):
        build_store("dynamo", "p", "(default)", "lf")
