"""Back-office management of products (policies) and customers, inside the enterprise systems.

Used by the admin panel and by customer sign-up. Every change is validated with the same
rules as data/policies.yaml and data/customers.yaml, applied to the live systems at once,
written to the durable store (Firestore) so it survives redeploys, and recorded in an
append-only change log with what changed.

Durable records (collections under the store's prefix):
  products/<code>   {"code", "spec"}            spec None = back to the file version / removed
  customers/<id>    {"id", "persona", "profile", "status", "source"}
                    persona: the full persona of a customer created here (re-seeded on start);
                    profile: field overrides for any customer (file, generated or created)
  admin_audit/<t>   one change-log entry per change (create-only)
"""
from __future__ import annotations

import copy
import json
import random
import re
import time
from collections import deque
from datetime import date

from ledgerflow.mocks.personas import cards_of, loans_of
from ledgerflow.mocks.personas import validate as validate_personas
from ledgerflow.mocks.policies import FORECLOSURE, REQUIRED, RULES
from ledgerflow.mocks.policies import validate as validate_policies
from ledgerflow.platform import logs
from ledgerflow.platform.durable import time_key

_log = logs.get("ledgerflow.admin")
PROFILE_FIELDS = ("name", "phone", "dob", "city", "segment", "email", "demo", "story")
SEGMENTS = ("salaried", "self-employed", "student", "retired", "other")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{1,79}$")


def schema() -> dict:
    """What the admin panel's product form is built from."""
    return {"rules": RULES, "foreclosure": FORECLOSURE, "required": {k: sorted(v) for k, v in REQUIRED.items()},
            "floating_requires": ["rate_reset"], "segments": list(SEGMENTS)}


def _flat(d, prefix="") -> dict:
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_flat(v, f"{prefix}{k}."))
    else:
        out[prefix[:-1]] = d
    return out


def diff(before: dict | None, after: dict | None) -> list[str]:
    """Readable change list: 'policy.rules.bounce_charge.amount: 500 -> 750'."""
    a, b = _flat(before or {}), _flat(after or {})
    lines = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            old = "—" if k not in a else json.dumps(a[k], ensure_ascii=False)[:120]
            new = "—" if k not in b else json.dumps(b[k], ensure_ascii=False)[:120]
            lines.append(f"{k}: {old} -> {new}")
    return lines


def age_on(dob: date, today: date) -> int:
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


class AdminService:
    def __init__(self, ms):
        self.ms = ms
        self.records: dict[str, dict] = {}          # durable customer records by id
        self.log: deque[dict] = deque(maxlen=300)   # recent change log (newest last)
        self.rng = random.Random()

    # ------------------------------------------------------------ change log
    def _audit(self, actor: str, action: str, target: str, changes: list[str]) -> None:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "actor": actor or "admin", "action": action,
                 "target": target, "changes": changes[:40]}
        self.log.append(entry)
        self.ms.durable.put_soon("admin_audit", time_key(), entry, create_only=True)
        logs.event(_log, "admin change", action=action, target=target, actor=entry["actor"], changes=len(changes))

    def audit_log(self, limit: int = 100) -> list[dict]:
        return list(self.log)[-limit:][::-1]

    # ------------------------------------------------------------ products
    def holders(self, code: str) -> int:
        return (self.ms.q("SELECT COUNT(*) n FROM loans WHERE product_code=?", code)[0]["n"]
                + self.ms.q("SELECT COUNT(*) n FROM cards WHERE product=?", code)[0]["n"])

    def _source(self, code: str) -> str:
        f = self.ms.file_products.get(code)
        if f is None:
            return "admin"
        return "file" if f == self.ms.products.get(code) else "file, edited"

    def list_products(self) -> list[dict]:
        out = []
        for code, p in self.ms.products.items():
            pol = p["policy"]
            out.append({"code": code, "name": p["name"], "kind": p["kind"], "rate_type": p["rate_type"],
                        "short_name": p.get("short_name", ""), "version": pol["version"], "effective": pol["effective"],
                        "rules": len(pol["rules"]), "clauses": len(pol["clauses"]), "source": self._source(code),
                        "holders": self.holders(code)})
        return out

    def get_product(self, code: str) -> dict | None:
        p = self.ms.products.get(code)
        if p is None:
            return None
        return {"code": code, "spec": p, "source": self._source(code), "holders": self.holders(code),
                "file_spec": self.ms.file_products.get(code)}

    def _apply_product(self, code: str, spec: dict | None) -> None:
        if spec is None:
            self.ms.conn.execute("DELETE FROM products WHERE code=?", (code,))
            self.ms.products.pop(code, None)
        else:
            self.ms.conn.execute("INSERT OR REPLACE INTO products(code,name,kind,rate_type,policy,short_name) "
                                 "VALUES(?,?,?,?,?,?)",
                                 (code, spec["name"], spec["kind"], spec["rate_type"], json.dumps(spec["policy"]),
                                  spec.get("short_name") or spec["name"]))
            self.ms.products[code] = spec
        self.ms.conn.commit()

    def check_product(self, code: str, spec: dict) -> tuple[list[str], dict | None]:
        code = str(code).strip().upper()
        if not isinstance(spec, dict):
            return ["the product must be a block with name, kind and policy"], None
        candidate = {**{c: p for c, p in self.ms.products.items() if c != code}, code: spec}
        errors, norm = validate_policies(candidate)
        mine = [e for e in errors if e.startswith(f"product {code}") or not e.startswith("product ")]
        old = self.ms.products.get(code)
        if not mine and old is not None:
            new = norm[code]
            held = self.holders(code)
            if held and new["kind"] != old["kind"]:
                mine.append(f"product {code}: {held} customer account(s) hold it, so its kind cannot change")
            if held and new["rate_type"] != old["rate_type"]:
                mine.append(f"product {code}: {held} customer account(s) hold it, so floating/fixed cannot change")
            terms_changed = (new["policy"]["rules"] != old["policy"]["rules"]
                             or new["policy"]["clauses"] != old["policy"]["clauses"])
            if terms_changed and new["policy"]["version"] == old["policy"]["version"]:
                mine.append(f"product {code}: rules or clauses changed, so give the policy a new version "
                            f"(currently {old['policy']['version']})")
        return mine, (None if mine else norm[code])

    async def save_product(self, code: str, spec: dict, actor: str = "admin") -> dict:
        code = str(code).strip().upper()
        errors, norm = self.check_product(code, spec)
        if errors:
            return {"ok": False, "errors": errors}
        before = self.ms.products.get(code)
        changes = diff(before, norm)
        if before is not None and not changes:
            return {"ok": True, "unchanged": True, "product": self.get_product(code)}
        self._apply_product(code, norm)
        await self.ms.durable.put("products", code, {"code": code, "spec": norm})
        self._audit(actor, "product.created" if before is None else "product.updated", code, changes)
        return {"ok": True, "product": self.get_product(code), "changes": changes}

    async def revert_product(self, code: str, actor: str = "admin") -> dict:
        file_spec = self.ms.file_products.get(code)
        if file_spec is None:
            return {"ok": False, "errors": [f"product {code} is not in data/policies.yaml; delete it instead"]}
        if self.ms.products.get(code) == file_spec:
            return {"ok": True, "unchanged": True, "product": self.get_product(code)}
        old = self.ms.products.get(code)
        if old and (old["kind"] != file_spec["kind"] or old["rate_type"] != file_spec["rate_type"]) and self.holders(code):
            return {"ok": False, "errors": [f"product {code}: the file version has a different kind or rate type "
                                            "and customers hold the product"]}
        changes = diff(old, file_spec)
        self._apply_product(code, copy.deepcopy(file_spec))
        await self.ms.durable.put("products", code, {"code": code, "spec": None})
        self._audit(actor, "product.reverted_to_file", code, changes)
        return {"ok": True, "product": self.get_product(code), "changes": changes}

    async def delete_product(self, code: str, actor: str = "admin") -> dict:
        if code not in self.ms.products:
            return {"ok": False, "errors": [f"no product {code}"]}
        if code in self.ms.file_products:
            return {"ok": False, "errors": [f"product {code} comes from data/policies.yaml; revert it instead, "
                                            "or remove it from the file"]}
        if self.holders(code):
            return {"ok": False, "errors": [f"product {code}: {self.holders(code)} customer account(s) hold it"]}
        if not any(p["kind"] == "loan" for c, p in self.ms.products.items() if c != code):
            return {"ok": False, "errors": ["at least one loan product is needed"]}
        self._apply_product(code, None)
        await self.ms.durable.put("products", code, {"code": code, "spec": None})
        self._audit(actor, "product.deleted", code, [])
        return {"ok": True}

    # ------------------------------------------------------------ customers
    def list_customers(self, q: str = "", scope: str = "named", demo: bool = False, limit: int = 50) -> list[dict]:
        where, args = [], []
        if scope != "all":
            where.append("c.source != 'generated'")
        if demo:
            where.append("c.demo = 1 AND c.status = 'active'")
        q = (q or "").strip()
        if q:
            where.append("(c.id = ? OR c.name LIKE ? OR c.phone LIKE ?)")
            args += [q.upper(), f"%{q}%", f"%{q}%"]
        sql = ("SELECT c.id, c.name, c.phone, c.dob, c.city, c.segment, c.email, c.status, c.demo, c.story, c.source, "
               "(SELECT GROUP_CONCAT(id, ', ') FROM loans WHERE customer_id = c.id) AS loan_id, "
               "(SELECT GROUP_CONCAT(product_code, ', ') FROM loans WHERE customer_id = c.id) AS loan_product, "
               "(SELECT GROUP_CONCAT(id, ', ') FROM cards WHERE customer_id = c.id) AS card_id, "
               "(SELECT GROUP_CONCAT(product, ', ') FROM cards WHERE customer_id = c.id) AS card_product "
               "FROM customers c" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY c.id LIMIT ?")
        rows = self.ms.q(sql, *args, max(1, min(int(limit or 50), 200)))
        for r in rows:
            r["demo"] = bool(r["demo"])
        return rows

    def get_customer(self, cid: str) -> dict | None:
        rows = self.list_customers(cid, scope="all", limit=1)
        rows = [r for r in rows if r["id"] == cid]
        if not rows:
            return None
        c = rows[0]
        c["loans"] = self.ms.q("SELECT id, product_code, principal, tenure, disbursal_date, first_due FROM loans "
                               "WHERE customer_id=? ORDER BY id", cid)
        c["cards"] = self.ms.q("SELECT id, product, last4, credit_limit, statement_balance, min_due, due_date FROM cards "
                               "WHERE customer_id=? ORDER BY id", cid)
        if c["loans"]:
            c["loan"] = c["loans"][0]
        if c["cards"]:
            c["card"] = c["cards"][0]
        c["open_requests"] = self.ms.q("SELECT COUNT(*) n FROM tickets WHERE customer_id=? AND status!='resolved'",
                                       cid)[0]["n"]
        c["record"] = self.records.get(cid)
        return c

    def _next_id(self, table: str, col: str, prefix: str, lo: int, hi: int, width: int, extra=()) -> str:
        used = {r[col] for r in self.ms.q(f"SELECT {col} FROM {table} WHERE {col} LIKE ?", f"{prefix}%")}  # noqa: S608
        used |= set(extra)
        for n in range(lo, hi + 1):
            cand = f"{prefix}{n:0{width}d}"
            if cand not in used:
                return cand
        raise ValueError(f"no free {prefix} id")

    def _profile_errors(self, p: dict, cid: str | None) -> list[str]:
        errs = []
        if "name" in p and not NAME_RE.match(str(p["name"]).strip()):
            errs.append("name: 2-80 letters (spaces, dots, apostrophes and hyphens allowed)")
        if "phone" in p:
            phone = str(p["phone"])
            if not re.fullmatch(r"[6-9]\d{9}", phone):
                errs.append("phone: a 10-digit Indian mobile number starting with 6, 7, 8 or 9")
            elif self.ms.q("SELECT 1 FROM customers WHERE phone=? AND id!=?", phone, cid or ""):
                errs.append("phone: this mobile number is already registered")
        if "dob" in p:
            try:
                age = age_on(date.fromisoformat(str(p["dob"])), self.ms.as_of)
                if not 18 <= age <= 100:
                    errs.append("dob: the customer must be 18 to 100 years old")
            except ValueError:
                errs.append("dob: must be a date, YYYY-MM-DD")
        if p.get("email") and not EMAIL_RE.match(str(p["email"])):
            errs.append("email: not a valid address")
        if "segment" in p and p["segment"] not in SEGMENTS:
            errs.append(f"segment: one of {', '.join(SEGMENTS)}")
        if "city" in p and len(str(p["city"])) > 40:
            errs.append("city: 40 characters at most")
        if "story" in p and len(str(p["story"])) > 160:
            errs.append("story: 160 characters at most")
        if "demo" in p and not isinstance(p["demo"], bool):
            errs.append("demo: true or false")
        return errs

    def prepare_persona(self, persona: dict) -> dict:
        """Fill in IDs and generated details for a new customer."""
        p = copy.deepcopy(persona)
        # C1500+ / L-15000+ / CARD-5500+ are kept for customers created here, so a later edit of
        # data/customers.yaml (C1001-C1499) can never collide with one.
        p["id"] = p.get("id") or self._next_id("customers", "id", "C1", 500, 999, 3)
        taken_loans, taken_cards = set(), set()
        for ln in loans_of(p):
            if not ln.get("id"):
                ln["id"] = self._next_id("loans", "id", "L-1", 5000, 9999, 4, extra=taken_loans)
            taken_loans.add(ln["id"])
        for cd in cards_of(p):
            if not cd.get("id"):
                cd["id"] = self._next_id("cards", "id", "CARD-5", 500, 999, 3, extra=taken_cards)
            taken_cards.add(cd["id"])
            cd.setdefault("last4", f"{self.rng.randint(1000, 9999)}")
        return p

    def check_customer(self, persona: dict, check_phone: bool = True) -> list[str]:
        p = persona
        errs = [re.sub(r"^customer #1 \([^)]*\)\s*", "", e) for e in validate_personas([p], self.ms.products)]
        prof = {k: p[k] for k in PROFILE_FIELDS if k in p}
        if not check_phone:
            prof.pop("phone", None)
        errs += [e for e in self._profile_errors(prof, None) if e not in errs]
        if self.ms.q("SELECT 1 FROM customers WHERE id=?", p.get("id", "")):
            errs.append(f"id: {p['id']} already exists")
        for ln in loans_of(p):
            if self.ms.q("SELECT 1 FROM loans WHERE id=?", ln.get("id", "")):
                errs.append(f"loan id: {ln['id']} already exists")
        for cd in cards_of(p):
            if self.ms.q("SELECT 1 FROM cards WHERE id=?", cd.get("id", "")):
                errs.append(f"card id: {cd['id']} already exists")
        return errs

    async def create_customer(self, persona: dict, actor: str = "admin", source: str = "admin",
                              dry_run: bool = False, check_phone: bool = True) -> dict:
        from ledgerflow.mocks.seed import seed_persona
        try:
            p = self.prepare_persona(persona)
        except ValueError as e:              # every ID in the named range is used
            return {"ok": False, "errors": [f"cannot create more customers: {e}"]}
        errors = self.check_customer(p, check_phone=check_phone)
        if errors:
            return {"ok": False, "errors": errors}
        if dry_run:
            return {"ok": True, "dry_run": True}
        seed_persona(self.ms.conn, p, self.ms.products, self.ms.as_of, self.rng, source=source)
        self.ms.conn.commit()
        rec = {"id": p["id"], "persona": p, "profile": {}, "status": "active", "source": source}
        self.records[p["id"]] = rec
        await self.ms.durable.put("customers", p["id"], rec)
        what = [f"{k}: {p[k]}" for k in ("name", "city") if p.get(k)] + \
               [f"loan {ln['id']} {ln['product']}" for ln in loans_of(p)] + \
               [f"card {cd['id']} {cd.get('product', '')}".strip() for cd in cards_of(p)]
        self._audit(actor, f"customer.created ({source})", p["id"], what)
        return {"ok": True, "customer": self.get_customer(p["id"])}

    def _record(self, cid: str) -> dict:
        return self.records.setdefault(cid, {"id": cid, "persona": None, "profile": {}, "status": "active",
                                             "source": self.ms.q("SELECT source FROM customers WHERE id=?", cid)[0]["source"]})

    async def update_customer(self, cid: str, fields: dict, actor: str = "admin") -> dict:
        row = self.ms.q("SELECT * FROM customers WHERE id=?", cid)
        if not row:
            return {"ok": False, "errors": [f"no customer {cid}"], "not_found": True}
        before = row[0]
        upd = {k: fields[k] for k in PROFILE_FIELDS if k in fields}
        for k in ("name", "phone", "dob", "city", "segment", "email", "story"):
            if k in upd:
                upd[k] = str(upd[k] or "").strip()
        errors = self._profile_errors(upd, cid)
        demo = upd.get("demo", bool(before["demo"]))
        if demo and not (upd.get("story", before["story"]) or "").strip():
            errors.append("story: customers shown on the sign-in screen need a one-line story")
        if errors:
            return {"ok": False, "errors": errors}
        old = {k: (bool(before[k]) if k == "demo" else before[k]) for k in upd}
        changes = diff(old, upd)
        if not changes:
            return {"ok": True, "unchanged": True, "customer": self.get_customer(cid)}
        sets = ", ".join(f"{k}=?" for k in upd)
        self.ms.conn.execute(f"UPDATE customers SET {sets} WHERE id=?",  # noqa: S608 (fixed column names)
                             (*[int(v) if k == "demo" else v for k, v in upd.items()], cid))
        self.ms.conn.commit()
        rec = self._record(cid)
        rec["profile"].update(upd)
        await self.ms.durable.put("customers", cid, rec)
        self._audit(actor, "customer.updated", cid, [c if not c.startswith("phone") else "phone changed" for c in changes])
        return {"ok": True, "customer": self.get_customer(cid), "changes": changes}

    async def set_status(self, cid: str, status: str, reason: str = "", actor: str = "admin") -> dict:
        if status not in ("active", "disabled"):
            return {"ok": False, "errors": ["status: active or disabled"]}
        row = self.ms.q("SELECT status FROM customers WHERE id=?", cid)
        if not row:
            return {"ok": False, "errors": [f"no customer {cid}"], "not_found": True}
        if row[0]["status"] == status:
            return {"ok": True, "unchanged": True, "customer": self.get_customer(cid)}
        self.ms.conn.execute("UPDATE customers SET status=? WHERE id=?", (status, cid))
        self.ms.conn.commit()
        rec = self._record(cid)
        rec["status"] = status
        await self.ms.durable.put("customers", cid, rec)
        self._audit(actor, "customer.disabled" if status == "disabled" else "customer.enabled", cid,
                    [f"reason: {reason[:200]}"] if reason else [])
        return {"ok": True, "customer": self.get_customer(cid)}

    # ------------------------------------------------------------ restore on start
    async def restore(self, durable) -> dict:
        """Re-apply admin changes on a freshly seeded database: products first (customers may hold
        new ones), then created customers, then profile and status overrides."""
        from ledgerflow.mocks.seed import seed_persona
        counts = {"products": 0, "customers": 0, "skipped": 0}
        for rec in await durable.all("products"):
            code, spec = rec.get("code"), rec.get("spec")
            if spec is None:
                if code in self.ms.file_products:
                    self._apply_product(code, copy.deepcopy(self.ms.file_products[code]))
                elif code in self.ms.products and not self.holders(code):
                    self._apply_product(code, None)
                continue
            errors, norm = self.check_product(code, spec)
            errors = [e for e in errors if "new version" not in e]     # restoring, not editing
            if errors:
                counts["skipped"] += 1
                logs.event(_log, "admin product not restored", level=40, code=code, errors=errors[:5])
                continue
            self._apply_product(code, validate_policies({**self.ms.products, code: spec})[1][code])
            counts["products"] += 1
        for rec in sorted(await durable.all("customers"), key=lambda r: r.get("id", "")):
            cid = rec.get("id")
            existing = self.ms.q("SELECT source FROM customers WHERE id=?", cid)
            if rec.get("persona") and existing and existing[0]["source"] == "file":
                # an ID created here was later also used in data/customers.yaml: keep the file's customer untouched
                counts["skipped"] += 1
                logs.event(_log, "admin customer conflicts with customers.yaml", level=40, customer=cid)
                continue
            if rec.get("persona") and not existing:
                errs = self.check_customer(rec["persona"])
                if errs:
                    counts["skipped"] += 1
                    logs.event(_log, "admin customer not restored", level=40, customer=cid, errors=errs[:5])
                    continue
                seed_persona(self.ms.conn, rec["persona"], self.ms.products, self.ms.as_of, self.rng,
                             source=rec.get("source", "admin"))
            if not self.ms.q("SELECT 1 FROM customers WHERE id=?", cid):
                continue
            prof = {k: v for k, v in (rec.get("profile") or {}).items() if k in PROFILE_FIELDS}
            if prof and not self._profile_errors(prof, cid):
                self.ms.conn.execute(f"UPDATE customers SET {', '.join(f'{k}=?' for k in prof)} WHERE id=?",  # noqa: S608
                                     (*[int(v) if k == "demo" else v for k, v in prof.items()], cid))
            self.ms.conn.execute("UPDATE customers SET status=? WHERE id=?", (rec.get("status", "active"), cid))
            self.records[cid] = rec
            counts["customers"] += 1
        self.ms.conn.commit()
        for entry in (await durable.all("admin_audit"))[-300:]:
            self.log.append(entry)
        return counts
