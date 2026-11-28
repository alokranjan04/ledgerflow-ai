"""Mock enterprise systems served over real HTTP under /mock/<system>/.

Each system has its own latency profile (log-normal, p50/p95) and fault model
(error rate, hard down, slow tail), switchable at runtime through
/mock/admin/faults — the demo's fault-injection panel drives this.
They share a process with the gateway to keep the PoC to one Cloud Run
service; connectors still call them over HTTP, so the network hop, timeouts,
retries and breakers are real. Splitting them out is a deployment change only.
"""
from __future__ import annotations

import asyncio
import copy
import hmac
import json
import math
import random
import sqlite3
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from aiohttp import web

from ledgerflow.finance.loans import RateChange, build_schedule
from ledgerflow.mocks import seed as seed_mod
from ledgerflow.mocks.admin import AdminService, schema
from ledgerflow.mocks.personas import load_personas
from ledgerflow.mocks.policies import load_policies
from ledgerflow.mocks.seed import seed
from ledgerflow.platform import logs
from ledgerflow.platform.durable import NullStore

# p50/p95 in milliseconds, error rate 0..1
DEFAULT_PROFILES = {
    "crm":      {"p50": 60,  "p95": 180,  "error_rate": 0.0, "down": False},
    "loans":    {"p50": 120, "p95": 350,  "error_rate": 0.0, "down": False},
    "payments": {"p50": 350, "p95": 1200, "error_rate": 0.03, "down": False},
    "policy":   {"p50": 90,  "p95": 250,  "error_rate": 0.0, "down": False},
    "ops":      {"p50": 250, "p95": 700,  "error_rate": 0.0, "down": False},
    "cards":    {"p50": 150, "p95": 450,  "error_rate": 0.0, "down": False},
}


class MockSystems:
    def __init__(self, db_path: str, as_of: date, rng_seed: int = 7):
        self.as_of = as_of
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        exists = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='customers'").fetchone()
        # Products as shipped (data/policies.yaml) and as live (after admin changes).
        self.file_products = load_policies()
        if not exists:
            seed(self.conn, as_of, products=self.file_products,
                 personas=load_personas(seed_mod.CUSTOMERS_FILE, self.file_products))
        self.products = copy.deepcopy(self.file_products) if not exists else {
                         r["code"]: {"name": r["name"], "kind": r["kind"], "rate_type": r["rate_type"],
                                     **({"short_name": r["short_name"]} if r["kind"] == "card" else {}),
                                     "policy": json.loads(r["policy"])}
                         for r in self.conn.execute("SELECT * FROM products").fetchall()}
        self.profiles = json.loads(json.dumps(DEFAULT_PROFILES))
        self.rng = random.Random(rng_seed)
        self.durable = NullStore()
        self.admin = AdminService(self)

    # ---- durability (service requests survive redeploys) ---------------------
    async def restore(self, durable) -> int:
        """Load service requests from the durable store into a freshly seeded database;
        then mirror every later change to it."""
        self.durable = durable
        counts = await self.admin.restore(durable)          # products and customers changed in the admin panel
        if any(counts.values()):
            logs.event(logs.get("ledgerflow.admin"), "admin changes restored", **counts)
        rows = await durable.all("tickets")
        cols = ("id", "customer_id", "type", "status", "summary", "payload", "created", "updated", "idem_key")
        self.conn.executemany(f"INSERT OR REPLACE INTO tickets({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                              [tuple(r.get(c) for c in cols) for r in rows])
        self.conn.commit()
        return len(rows)

    async def mirror(self, tid: str) -> None:
        rows = self.q("SELECT * FROM tickets WHERE id=?", tid)
        if rows:
            await self.durable.put("tickets", tid, rows[0])

    # ---- latency / faults --------------------------------------------------
    def _delay(self, system: str) -> float:
        p = self.profiles[system]
        mu = math.log(max(p["p50"], 1))
        sigma = max(0.01, (math.log(max(p["p95"], p["p50"] + 1)) - mu) / 1.645)
        return self.rng.lognormvariate(mu, sigma) / 1000.0

    async def simulate(self, system: str) -> None:
        p = self.profiles[system]
        await asyncio.sleep(min(self._delay(system), 15))
        if p.get("down"):
            raise web.HTTPServiceUnavailable(text=json.dumps({"error": f"{system} unavailable"}),
                                             content_type="application/json")
        if self.rng.random() < p.get("error_rate", 0):
            raise web.HTTPInternalServerError(text=json.dumps({"error": f"{system} internal error"}),
                                              content_type="application/json")

    def q(self, sql: str, *args) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # ---- domain reads ------------------------------------------------------
    def loan_detail(self, loan_id: str) -> dict | None:
        rows = self.q("SELECT * FROM loans WHERE id=?", loan_id)
        if not rows:
            return None
        loan = rows[0]
        hist = self.q("SELECT effective, rate, reason FROM rate_history WHERE loan_id=? ORDER BY effective", loan_id)
        sched = build_schedule(Decimal(loan["principal"]), loan["tenure"], date.fromisoformat(loan["first_due"]),
                               [RateChange(date.fromisoformat(h["effective"]), Decimal(h["rate"]), h["reason"]) for h in hist])
        loan["rate_history"] = hist
        prod = self.q("SELECT name, rate_type FROM products WHERE code=?", loan["product_code"])
        loan["rate_type"] = prod[0]["rate_type"] if prod else "fixed"
        loan["product_name"] = prod[0]["name"] if prod else loan["product_code"]
        short = self.q("SELECT short_name FROM products WHERE code=?", loan["product_code"])
        loan["product_short_name"] = (short[0]["short_name"] if short else "") or loan["product_name"]
        loan["schedule"] = [r.as_dict() for r in sched.rows]
        loan["as_of"] = self.as_of.isoformat()
        return loan


def build_mock_app(ms: MockSystems, internal_token: str | None = None) -> web.Application:
    """`internal_token`: when set, every call must carry it in X-Internal-Token, so the
    simulated systems are reachable only by the service's own connectors."""

    @web.middleware
    async def internal_only(request, handler):
        if internal_token and not hmac.compare_digest(request.headers.get("X-Internal-Token", ""), internal_token):
            raise web.HTTPForbidden(text=json.dumps({"error": "internal endpoint"}), content_type="application/json")
        return await handler(request)

    app = web.Application(middlewares=[internal_only])
    r = app.router

    def sys_route(system: str):
        def deco(fn):
            async def handler(request: web.Request):
                await ms.simulate(system)
                return await fn(request)
            return handler
        return deco

    def not_found(what: str):
        return web.json_response({"error": f"{what} not found"}, status=404)

    # CRM ---------------------------------------------------------------------
    @sys_route("crm")
    async def crm_customer(req):
        rows = ms.q("SELECT * FROM customers WHERE id=?", req.match_info["cid"])
        return web.json_response(rows[0]) if rows else not_found("customer")

    @sys_route("crm")
    async def crm_lookup(req):
        rows = ms.q("SELECT id, name FROM customers WHERE phone=? AND status='active'", req.query.get("phone", "")[-10:])
        return web.json_response(rows[0]) if rows else not_found("customer")

    @sys_route("crm")
    async def crm_verify(req):
        body = await req.json()
        rows = ms.q("SELECT id, name FROM customers WHERE phone=? AND dob=? AND status='active'",
                    str(body.get("phone", ""))[-10:], str(body.get("dob", "")))
        if not rows:
            return web.json_response({"verified": False}, status=401)
        return web.json_response({"verified": True, **rows[0]})

    @sys_route("crm")
    async def crm_interactions(req):
        return web.json_response(ms.q("SELECT ts, channel, summary FROM interactions WHERE customer_id=? ORDER BY ts DESC",
                                      req.match_info["cid"]))

    # Loan management -----------------------------------------------------------
    @sys_route("loans")
    async def loans_for_customer(req):
        return web.json_response(ms.q(
            "SELECT l.id, l.product_code, l.principal, l.disbursal_date, l.tenure, l.status, "
            "COALESCE(p.short_name, p.name, l.product_code) AS product_short_name, p.name AS product_name "
            "FROM loans l LEFT JOIN products p ON p.code = l.product_code WHERE l.customer_id=? ORDER BY l.id",
            req.match_info["cid"]))

    @sys_route("loans")
    async def loan_detail(req):
        loan = ms.loan_detail(req.match_info["lid"])
        return web.json_response(loan) if loan else not_found("loan")

    # Payments --------------------------------------------------------------------
    @sys_route("payments")
    async def payments(req):
        since = req.query.get("since", "0000-00-00")
        return web.json_response(ms.q("SELECT * FROM payments WHERE loan_id=? AND attempt_date>=? "
                                      "ORDER BY attempt_date DESC, id DESC", req.match_info["lid"], since))

    # Policy knowledge ------------------------------------------------------------
    @sys_route("policy")
    async def product(req):
        rows = ms.q("SELECT * FROM products WHERE code=?", req.match_info["code"])
        if not rows:
            return not_found("product")
        row = rows[0]
        row["policy"] = json.loads(row["policy"])
        return web.json_response(row)

    @sys_route("policy")
    async def policy_search(req):
        terms = [t for t in req.query.get("q", "").lower().split() if len(t) > 2]
        code = req.query.get("product")
        hits = []
        for p in ms.q("SELECT code, name, policy FROM products" + (" WHERE code=?" if code else ""), *([code] if code else [])):
            for cid, text in json.loads(p["policy"])["clauses"].items():
                score = sum(text.lower().count(t) for t in terms)
                if score:
                    hits.append({"product": p["code"], "clause": cid, "text": text, "score": score})
        hits.sort(key=lambda h: -h["score"])
        return web.json_response(hits[:5])

    # Service operations ------------------------------------------------------------
    @sys_route("ops")
    async def create_request(req):
        key = req.headers.get("Idempotency-Key")
        if not key:
            return web.json_response({"error": "Idempotency-Key header required"}, status=400)
        existing = ms.q("SELECT * FROM tickets WHERE idem_key=?", key)
        if existing:
            return web.json_response({**existing[0], "replayed": True})
        body = await req.json()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        tid = "SR-" + uuid.uuid4().hex[:8].upper()
        ms.conn.execute("INSERT INTO tickets VALUES(?,?,?,?,?,?,?,?,?)",
                        (tid, body["customer_id"], body["type"], "open", body.get("summary", ""),
                         json.dumps(body.get("payload", {})), now, now, key))
        ms.conn.commit()
        await ms.mirror(tid)
        return web.json_response(ms.q("SELECT * FROM tickets WHERE id=?", tid)[0], status=201)

    @sys_route("ops")
    async def list_requests(req):
        return web.json_response(ms.q("SELECT id, type, status, summary, created, updated FROM tickets "
                                      "WHERE customer_id=? ORDER BY created DESC", req.query.get("customer_id", "")))

    @sys_route("ops")
    async def get_request(req):
        rows = ms.q("SELECT * FROM tickets WHERE id=?", req.match_info["tid"])
        return web.json_response(rows[0]) if rows else not_found("request")

    @sys_route("ops")
    async def escalate(req):
        tid = req.match_info["tid"]
        now = datetime.now(UTC).isoformat(timespec="seconds")
        cur = ms.conn.execute("UPDATE tickets SET status='escalated', updated=? WHERE id=? AND status!='resolved'", (now, tid))
        ms.conn.commit()
        if not cur.rowcount:
            return not_found("open request")
        await ms.mirror(tid)
        return web.json_response(ms.q("SELECT * FROM tickets WHERE id=?", tid)[0])

    @sys_route("ops")
    async def queue(req):
        """Agent desk: open requests of one type, oldest first, with their payload."""
        kind = req.query.get("type", "agent_handoff")
        rows = ms.q("SELECT id, customer_id, type, status, summary, payload, created, updated FROM tickets "
                    "WHERE type=? AND status IN ('open','in_progress') ORDER BY created", kind)
        for r in rows:
            r["payload"] = json.loads(r["payload"] or "{}")
        return web.json_response({"open": sum(1 for r in rows if r["status"] == "open"), "items": rows})

    @sys_route("ops")
    async def accept(req):
        tid = req.match_info["tid"]
        body = await req.json() if req.can_read_body else {}
        now = datetime.now(UTC).isoformat(timespec="seconds")
        cur = ms.conn.execute("UPDATE tickets SET status='in_progress', updated=? WHERE id=? AND status='open'", (now, tid))
        ms.conn.commit()
        if not cur.rowcount:
            return not_found("open request")
        await ms.mirror(tid)
        row = ms.q("SELECT * FROM tickets WHERE id=?", tid)[0]
        row["accepted_by"] = str(body.get("agent", ""))[:60]
        return web.json_response(row)

    # Cards (workflow 2) -------------------------------------------------------------
    @sys_route("cards")
    async def cards_for_customer(req):
        return web.json_response(ms.q(
            "SELECT k.*, COALESCE(p.short_name, p.name, k.product) AS product_short_name FROM cards k "
            "LEFT JOIN products p ON p.code = k.product WHERE k.customer_id=? ORDER BY k.id", req.match_info["cid"]))

    @sys_route("cards")
    async def card_txns(req):
        return web.json_response(ms.q("SELECT * FROM card_txns WHERE card_id=? ORDER BY ts DESC", req.match_info["card"]))

    # Admin: fault injection ------------------------------------------------------------
    async def get_faults(req):
        return web.json_response(ms.profiles)

    async def set_faults(req):
        body = await req.json()
        for system, prof in body.items():
            if system in ms.profiles:
                for k in ("p50", "p95", "error_rate", "down"):
                    if k in prof:
                        ms.profiles[system][k] = prof[k]
        return web.json_response(ms.profiles)

    async def reset_faults(req):
        ms.profiles = json.loads(json.dumps(DEFAULT_PROFILES))
        return web.json_response(ms.profiles)

    r.add_get("/crm/customers/{cid}", crm_customer)
    r.add_get("/crm/lookup", crm_lookup)
    r.add_post("/crm/verify", crm_verify)
    r.add_get("/crm/customers/{cid}/interactions", crm_interactions)
    r.add_get("/loans/customers/{cid}/loans", loans_for_customer)
    r.add_get("/loans/loans/{lid}", loan_detail)
    r.add_get("/payments/loans/{lid}/payments", payments)
    r.add_get("/policy/products/{code}", product)
    r.add_get("/policy/search", policy_search)
    r.add_post("/ops/requests", create_request)
    r.add_get("/ops/requests", list_requests)
    r.add_get("/ops/requests/{tid}", get_request)
    r.add_post("/ops/requests/{tid}/escalate", escalate)
    r.add_get("/ops/queue", queue)
    r.add_post("/ops/requests/{tid}/accept", accept)
    r.add_get("/cards/customers/{cid}/cards", cards_for_customer)
    r.add_get("/cards/cards/{card}/transactions", card_txns)
    add_admin_routes(r, ms)
    r.add_get("/admin/faults", get_faults)
    r.add_post("/admin/faults", set_faults)
    r.add_post("/admin/faults/reset", reset_faults)
    return app


def add_admin_routes(r, ms: MockSystems) -> None:
    """Back-office API (products and customers). Internal callers only, like everything under /mock."""
    adm = ms.admin

    def reply(res: dict, created: bool = False):
        if res.get("ok"):
            return web.json_response(res, status=201 if created else 200)
        return web.json_response(res, status=404 if res.get("not_found") else 422)

    async def body(req) -> dict:
        try:
            b = await req.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text=json.dumps({"ok": False, "errors": ["body must be JSON"]}),
                                     content_type="application/json") from None
        if not isinstance(b, dict):
            raise web.HTTPBadRequest(text=json.dumps({"ok": False, "errors": ["body must be an object"]}),
                                     content_type="application/json")
        return b

    async def get_schema(req):
        return web.json_response(schema())

    async def list_products(req):
        return web.json_response({"products": adm.list_products()})

    async def get_product(req):
        p = adm.get_product(req.match_info["code"].upper())
        return web.json_response(p) if p else web.json_response({"ok": False, "errors": ["no such product"]}, status=404)

    async def save_product(req):
        b = await body(req)
        code = req.match_info["code"].upper()
        existed = code in ms.products
        return reply(await adm.save_product(code, b.get("spec"), str(b.get("actor", "admin"))[:60]), created=not existed)

    async def revert_product(req):
        b = await body(req)
        return reply(await adm.revert_product(req.match_info["code"].upper(), str(b.get("actor", "admin"))[:60]))

    async def delete_product(req):
        b = await body(req)
        return reply(await adm.delete_product(req.match_info["code"].upper(), str(b.get("actor", "admin"))[:60]))

    async def list_customers(req):
        return web.json_response({"customers": adm.list_customers(req.query.get("q", ""), req.query.get("scope", "named"),
                                                                  req.query.get("demo") == "1",
                                                                  int(req.query.get("limit", "50") or 50))})

    async def get_customer(req):
        c = adm.get_customer(req.match_info["cid"].upper())
        return web.json_response(c) if c else web.json_response({"ok": False, "errors": ["no such customer"]}, status=404)

    async def create_customer(req):
        b = await body(req)
        source = b.get("source") if b.get("source") in ("admin", "signup") else "admin"
        res = await adm.create_customer(b.get("persona") or {}, str(b.get("actor", "admin"))[:60], source,
                                        dry_run=bool(b.get("dry_run")), check_phone=b.get("check_phone", True) is not False)
        return reply(res, created=not res.get("dry_run"))

    async def update_customer(req):
        b = await body(req)
        return reply(await adm.update_customer(req.match_info["cid"].upper(), b.get("profile") or {},
                                               str(b.get("actor", "admin"))[:60]))

    async def set_status(req):
        b = await body(req)
        return reply(await adm.set_status(req.match_info["cid"].upper(), str(b.get("status", "")),
                                          str(b.get("reason", "")), str(b.get("actor", "admin"))[:60]))

    async def audit(req):
        return web.json_response({"entries": adm.audit_log(int(req.query.get("limit", "100") or 100))})

    r.add_get("/admin/schema", get_schema)
    r.add_get("/admin/products", list_products)
    r.add_get("/admin/products/{code}", get_product)
    r.add_post("/admin/products/{code}", save_product)
    r.add_post("/admin/products/{code}/revert", revert_product)
    r.add_post("/admin/products/{code}/delete", delete_product)
    r.add_get("/admin/customers", list_customers)
    r.add_post("/admin/customers", create_customer)
    r.add_get("/admin/customers/{cid}", get_customer)
    r.add_post("/admin/customers/{cid}", update_customer)
    r.add_post("/admin/customers/{cid}/status", set_status)
    r.add_get("/admin/audit", audit)
