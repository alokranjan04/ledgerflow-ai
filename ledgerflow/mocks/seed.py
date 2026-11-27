"""Deterministic synthetic data for the mock enterprise systems.

All dates are relative to AS_OF so the demo stays coherent on any day.
Named scenario customers each exercise one customer journey; the rest are
random (fixed seed) so the systems feel populated.
"""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal

from ledgerflow.config import ROOT, env
from ledgerflow.finance.loans import RateChange, add_months, build_schedule, money
from ledgerflow.mocks.personas import load_personas
from ledgerflow.mocks.policies import card_products, load_policies

CUSTOMERS_FILE = ROOT / env("CUSTOMERS_FILE", "data/customers.yaml")

SCHEMA = """
CREATE TABLE customers(id TEXT PRIMARY KEY, name TEXT, phone TEXT UNIQUE, dob TEXT, email TEXT,
  city TEXT, segment TEXT, language TEXT, status TEXT DEFAULT 'active', demo INTEGER DEFAULT 0,
  story TEXT DEFAULT '', source TEXT DEFAULT 'generated');
CREATE TABLE interactions(id INTEGER PRIMARY KEY, customer_id TEXT, ts TEXT, channel TEXT, summary TEXT);
CREATE TABLE products(code TEXT PRIMARY KEY, name TEXT, kind TEXT, rate_type TEXT, policy TEXT, short_name TEXT);
CREATE TABLE loans(id TEXT PRIMARY KEY, customer_id TEXT, product_code TEXT, principal TEXT,
  disbursal_date TEXT, first_due TEXT, tenure INTEGER, status TEXT);
CREATE TABLE rate_history(loan_id TEXT, effective TEXT, rate TEXT, reason TEXT);
CREATE TABLE payments(id TEXT PRIMARY KEY, loan_id TEXT, instalment_no INTEGER, due_date TEXT,
  attempt_date TEXT, amount TEXT, status TEXT, reason_code TEXT, reason TEXT, mode TEXT,
  charge TEXT, charge_gst TEXT);
CREATE TABLE cards(id TEXT PRIMARY KEY, customer_id TEXT, product TEXT, last4 TEXT, credit_limit TEXT,
  statement_date TEXT, due_date TEXT, statement_balance TEXT, min_due TEXT, available TEXT);
CREATE TABLE card_txns(id TEXT PRIMARY KEY, card_id TEXT, ts TEXT, merchant TEXT, amount TEXT,
  status TEXT);
CREATE TABLE tickets(id TEXT PRIMARY KEY, customer_id TEXT, type TEXT, status TEXT, summary TEXT,
  payload TEXT, created TEXT, updated TEXT, idem_key TEXT UNIQUE);
CREATE INDEX ix_loans_c ON loans(customer_id);
CREATE INDEX ix_pay_l ON payments(loan_id);
CREATE INDEX ix_cards_c ON cards(customer_id);
CREATE INDEX ix_tx_c ON card_txns(card_id);
CREATE INDEX ix_t_c ON tickets(customer_id);
"""

FAILURE_REASONS = {
    "INSUFFICIENT_FUNDS": ("Insufficient balance in the linked bank account on the debit date", True),
    "MANDATE_CANCELLED": ("The auto-debit mandate was cancelled at the bank", True),
    "BANK_TECHNICAL": ("The customer's bank had a technical failure while processing the debit", False),
    "ACCOUNT_FROZEN": ("The linked account was temporarily frozen by the bank", True),
}

FIRST = ["Aarav", "Vivaan", "Aditya", "Ishaan", "Kabir", "Diya", "Saanvi", "Aadhya", "Kavya", "Anika",
         "Rohan", "Arjun", "Neha", "Pooja", "Sneha", "Karan", "Siddharth", "Riya", "Tanvi", "Nikhil"]
LAST = ["Sharma", "Verma", "Iyer", "Nair", "Reddy", "Gupta", "Mehta", "Kapoor", "Das", "Patel",
        "Singh", "Rao", "Joshi", "Menon", "Bose"]
CITIES = ["Mumbai", "Bengaluru", "Delhi", "Pune", "Hyderabad", "Chennai", "Kolkata", "Ahmedabad"]
MERCHANTS = [("Swiggy", 300, 1500), ("Amazon India", 400, 9000), ("Uber", 150, 900), ("BigBasket", 800, 4000),
             ("IndiGo", 4000, 15000), ("Reliance Digital", 2000, 40000), ("Zomato", 250, 1400),
             ("Apollo Pharmacy", 200, 2500), ("BookMyShow", 300, 1600), ("Shell Fuel", 1000, 4000)]


def _pct(v) -> Decimal:
    """YAML turns 11.00 into 11.0; rates are always shown to two decimals."""
    return Decimal(str(v)).quantize(Decimal("0.01"))


def _last_due_on_or_before(as_of: date, day: int = 5) -> date:
    d = date(as_of.year, as_of.month, day)
    return d if d <= as_of else add_months(d, -1)


def seed(conn: sqlite3.Connection, as_of: date, n_random: int = 500, rng_seed: int = 42,
         personas: list[dict] | None = None, products: dict | None = None) -> None:
    """Product terms come from data/policies.yaml (the Policy Knowledge system's content);
    named customers from data/customers.yaml; the rest is random with a fixed seed."""
    conn.executescript(SCHEMA)
    products = products if products is not None else load_policies()
    for code, p in products.items():
        conn.execute("INSERT INTO products VALUES(?,?,?,?,?,?)",
                     (code, p["name"], p["kind"], p["rate_type"], json.dumps(p["policy"]), p.get("short_name") or p["name"]))
    rng = random.Random(rng_seed)
    last_due = _last_due_on_or_before(as_of)

    # --- Named personas (data/customers.yaml) --------------------------------
    default_card = (card_products(products) or [None])[0]
    for c in personas if personas is not None else load_personas(CUSTOMERS_FILE, products):
        seed_persona(conn, c, products, as_of, rng, source="file")

    # --- Background population ---------------------------------------------
    for i in range(n_random):
        cid = f"C{2000 + i}"
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        dob = date(rng.randint(1965, 2000), rng.randint(1, 12), rng.randint(1, 28)).isoformat()
        _customer(conn, cid, name, f"97{rng.randint(10000000, 99999999)}", dob, rng.choice(CITIES),
                  rng.choice(["salaried", "self-employed"]))
        product = rng.choice(sorted(c for c, p in products.items() if p["kind"] == "loan"))
        months_ago = rng.randint(2, 40)
        tenure = rng.choice([24, 36, 48, 60])
        if months_ago >= tenure:
            months_ago = tenure - 1
        first_due = add_months(last_due, -(months_ago - 1))
        disb = first_due - timedelta(days=30)
        base = Decimal(rng.choice(["10.25", "10.75", "11.50", "12.00", "13.25", "14.50"]))
        hist = [RateChange(disb, base, "Rate at sanction")]
        if products[product]["rate_type"] == "floating" and months_ago > 6:
            hist.append(RateChange(add_months(last_due, -rng.randint(1, 5)) - timedelta(days=9),
                                   base + Decimal("0.50"), "Quarterly EBLR reset"))
        fails = {}
        if rng.random() < 0.2:
            fails[-rng.randint(0, min(5, months_ago - 1))] = (rng.choice(list(FAILURE_REASONS)), rng.randint(1, 6))
        _loan(conn, f"L-{20000 + i}", cid, product, Decimal(rng.randrange(100000, 1500000, 25000)), disb,
              first_due, tenure, hist, as_of, failures=fails, policy=products[product]["policy"])
        if default_card and rng.random() < 0.3:
            _card(conn, rng, f"CARD-{6000 + i}", cid, f"{rng.randint(1000, 9999)}",
                  Decimal(rng.choice([50000, 100000, 200000])), as_of, duplicate=None, product=default_card,
                  policy=products[default_card]["policy"])
    conn.commit()


def seed_persona(conn, c: dict, products: dict, as_of: date, rng: random.Random, source: str) -> None:
    """One named customer (a validated persona: customers.yaml, the admin panel or sign-up) into
    every system: CRM profile, loan with its schedule and payments, card with its transactions."""
    last_due = _last_due_on_or_before(as_of)
    _customer(conn, c["id"], c["name"], str(c["phone"]), str(c["dob"]), c.get("city", ""), c.get("segment", ""),
              email=c.get("email") or None, demo=bool(c.get("demo")), story=c.get("story", ""), source=source)
    loan = c.get("loan")
    if loan:
        first_due = add_months(last_due, -(int(loan["emis_paid"]) - 1))
        disb = first_due - timedelta(days=int(loan.get("disbursal_days_before_first_emi", 30)))
        hist = [RateChange(disb, _pct(loan["rate"]),
                           loan.get("rate_reason") or ("Fixed rate at sanction"
                                                       if products[loan["product"]]["rate_type"] == "fixed"
                                                       else "Rate at sanction"))]
        for r in loan.get("rate_resets") or []:
            hist.append(RateChange(add_months(last_due, -int(r["months_ago"])) - timedelta(days=10),
                                   _pct(r["rate"]), r.get("reason", "Rate reset")))
        fails = {-int(f["emis_ago"]): (f["reason"], int(f.get("retry_days", 3)))
                 for f in loan.get("failed_payments") or []}
        _loan(conn, loan["id"], c["id"], loan["product"], Decimal(str(loan["principal"])), disb, first_due,
              int(loan["tenure_months"]), hist, as_of, failures=fails, policy=products[loan["product"]]["policy"])
    card = c.get("card")
    if card:
        code = card.get("product") or (card_products(products) or [None])[0]
        _card(conn, rng, card["id"], c["id"], str(card["last4"]), Decimal(str(card["limit"])), as_of,
              duplicate=card.get("duplicate_charge"), product=code, policy=products[code]["policy"])
    for it in c.get("interactions") or []:
        conn.execute("INSERT INTO interactions(customer_id,ts,channel,summary) VALUES(?,?,?,?)",
                     (c["id"], (as_of - timedelta(days=int(it["days_ago"]))).isoformat(), it["channel"],
                      it["summary"]))


def _customer(conn, cid, name, phone, dob, city, segment, email=None, demo=False, story="", source="generated"):
    email = email or name.lower().replace(" ", ".") + "@example.com"
    conn.execute("INSERT INTO customers(id,name,phone,dob,email,city,segment,language,status,demo,story,source) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (cid, name, phone, dob, email, city, segment, "en", "active", int(demo), story, source))


def _loan(conn, lid, cid, product, principal, disb, first_due, tenure, history, as_of, failures, policy):
    bounce = policy["rules"]["bounce_charge"]
    conn.execute("INSERT INTO loans VALUES(?,?,?,?,?,?,?,?)",
                 (lid, cid, product, str(principal), disb.isoformat(), first_due.isoformat(), tenure, "active"))
    for h in history:
        conn.execute("INSERT INTO rate_history VALUES(?,?,?,?)", (lid, h.effective.isoformat(), str(h.rate_pct), h.reason))
    sched = build_schedule(principal, tenure, first_due, history)
    paid = sched.paid_until(as_of)
    n = len(paid)
    for idx, row in enumerate(paid):
        offset = idx - (n - 1)          # 0 = latest instalment, -1 = previous ...
        pid = f"P-{lid[2:]}-{row.no:03d}"
        if offset in failures:
            code, retry_days = failures[offset]
            reason, chargeable = FAILURE_REASONS[code]
            charge = money(bounce["amount"]) if chargeable else money(0)
            gst = money(charge * Decimal("0.18")) if bounce["gst"] else money(0)
            conn.execute("INSERT INTO payments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (pid + "F", lid, row.no, row.due_date.isoformat(), row.due_date.isoformat(), str(row.emi),
                          "failed", code, reason, "NACH auto-debit", str(charge), str(gst)))
            retry = min(row.due_date + timedelta(days=retry_days), as_of)
            conn.execute("INSERT INTO payments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (pid, lid, row.no, row.due_date.isoformat(), retry.isoformat(), str(row.emi),
                          "success", None, None, "UPI (manual retry)", "0.00", "0.00"))
        else:
            conn.execute("INSERT INTO payments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (pid, lid, row.no, row.due_date.isoformat(), row.due_date.isoformat(), str(row.emi),
                          "success", None, None, "NACH auto-debit", "0.00", "0.00"))


def _card(conn, rng, card_id, cid, last4, limit, as_of, duplicate, product, policy):
    rule = policy["rules"]["min_due"]
    stmt = date(as_of.year, as_of.month, 20)
    if stmt > as_of:
        stmt = add_months(stmt, -1)
    due = stmt + timedelta(days=20)
    txns = []
    stmt_dt = datetime(stmt.year, stmt.month, stmt.day, 18, 0)
    for _ in range(14):
        m, lo, hi = rng.choice(MERCHANTS)
        ts = stmt_dt - timedelta(days=rng.randint(1, 29), hours=rng.randint(0, 17), minutes=rng.randint(0, 59))
        txns.append((ts, m, money(rng.randint(lo * 100, hi * 100) / 100)))
    if duplicate:
        ts = stmt_dt - timedelta(days=6, hours=3, minutes=12)
        amount = money(str(duplicate["amount"]))
        txns.append((ts, duplicate["merchant"], amount))
        txns.append((ts + timedelta(minutes=1), duplicate["merchant"], amount))
    txns.sort()
    balance = money(sum(a for _, _, a in txns))
    min_due = max(money(balance * Decimal(rule["pct"]) / 100), money(rule["floor"]))
    conn.execute("INSERT INTO cards VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (card_id, cid, product, last4, str(limit), stmt.isoformat(), due.isoformat(), str(balance),
                  str(min_due), str(money(limit - balance))))
    for k, (ts, m, a) in enumerate(txns):
        conn.execute("INSERT INTO card_txns VALUES(?,?,?,?,?,?)",
                     (f"T-{card_id[5:]}-{k + 1:03d}", card_id, ts.isoformat(timespec="minutes"),
                      m, str(a), "posted"))
