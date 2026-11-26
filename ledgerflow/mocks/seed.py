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

from ledgerflow.finance.loans import RateChange, add_months, build_schedule, money

SCHEMA = """
CREATE TABLE customers(id TEXT PRIMARY KEY, name TEXT, phone TEXT UNIQUE, dob TEXT, email TEXT,
  city TEXT, segment TEXT, language TEXT);
CREATE TABLE interactions(id INTEGER PRIMARY KEY, customer_id TEXT, ts TEXT, channel TEXT, summary TEXT);
CREATE TABLE products(code TEXT PRIMARY KEY, name TEXT, kind TEXT, rate_type TEXT, policy TEXT);
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

# Product terms live in the Policy Knowledge system. Clause IDs are what the
# assistant cites. Rules are data, evaluated by the deterministic rules engine.
PRODUCTS = {
    "PL-FLOAT": {
        "name": "Personal Loan (floating rate, EBLR-linked)", "kind": "loan", "rate_type": "floating",
        "policy": {
            "version": "2026.10", "effective": "2026-10-01",
            "rules": {
                "foreclosure": {"min_emis_paid": 1, "charge_pct": "0", "clause": "PL-FL-5.1"},
                "part_prepayment": {"min_emis_paid": 6, "min_amount": "10000", "max_pct_outstanding_per_fy": "25",
                                    "charge_pct": "0", "clause": "PL-FL-5.2"},
                "rate_reset": {"benchmark": "External Benchmark Lending Rate (RBI repo-linked)",
                               "frequency": "quarterly", "clause": "PL-FL-3.4"},
                "bounce_charge": {"amount": "500", "gst": True, "clause": "PL-FL-7.1"},
                "emi_date_change": {"allowed_days": [5, 10, 15], "max_per_year": 1, "clause": "PL-FL-4.3"},
            },
            "clauses": {
                "PL-FL-3.4": "The interest rate is linked to the External Benchmark Lending Rate (EBLR). When the RBI repo rate changes, the rate is reset at the next quarterly reset date and the EMI is recalculated over the remaining tenure.",
                "PL-FL-4.3": "The EMI date can be changed to the 5th, 10th or 15th of the month, once per year. Broken-period interest is charged for the shift.",
                "PL-FL-5.1": "Floating-rate loans to individuals carry no foreclosure charge. Foreclosure is allowed any time after the first EMI. The payoff amount is outstanding principal plus interest accrued since the last EMI.",
                "PL-FL-5.2": "Part-prepayment is allowed after 6 EMIs, minimum Rs 10,000, up to 25% of the outstanding principal per financial year, with no charge. The customer chooses to reduce the tenure or reduce the EMI.",
                "PL-FL-7.1": "A failed EMI debit caused by insufficient funds or a cancelled mandate attracts a bounce charge of Rs 500 plus GST. No charge applies when the failure is on the bank's side.",
            },
        },
    },
    "PL-FIX": {
        "name": "Personal Loan (fixed rate)", "kind": "loan", "rate_type": "fixed",
        "policy": {
            "version": "2026.04", "effective": "2026-04-01",
            "rules": {
                "foreclosure": {"lock_in_emis": 12, "charge_pct_until_24m": "4", "charge_pct_after_24m": "2",
                                "clause_lock_in": "PL-FX-6.1", "clause": "PL-FX-6.2"},
                "part_prepayment": {"min_emis_paid": 12, "min_amount": "10000", "max_pct_outstanding_per_fy": "25",
                                    "charge_pct": "2", "clause": "PL-FX-6.3"},
                "bounce_charge": {"amount": "500", "gst": True, "clause": "PL-FX-7.1"},
                "emi_date_change": {"allowed_days": [5, 10, 15], "max_per_year": 1, "clause": "PL-FX-4.3"},
            },
            "clauses": {
                "PL-FX-4.3": "The EMI date can be changed to the 5th, 10th or 15th of the month, once per year.",
                "PL-FX-6.1": "Foreclosure and part-prepayment are not permitted until 12 EMIs have been paid (lock-in).",
                "PL-FX-6.2": "After the lock-in, foreclosure attracts a charge of 4% of the outstanding principal plus GST if the loan is less than 24 months old from disbursal, and 2% plus GST after that.",
                "PL-FX-6.3": "After 12 EMIs, part-prepayment of at least Rs 10,000 and up to 25% of the outstanding principal per financial year is allowed, with a 2% charge plus GST on the amount prepaid.",
                "PL-FX-7.1": "A failed EMI debit caused by insufficient funds or a cancelled mandate attracts a bounce charge of Rs 500 plus GST.",
            },
        },
    },
    "CC-PLAT": {
        "name": "LedgerFlow Platinum Credit Card", "kind": "card", "rate_type": "n/a",
        "policy": {
            "version": "2026.07", "effective": "2026-07-01",
            "rules": {
                "dispute": {"window_days": 60, "clause": "CC-PL-9.2"},
                "min_due": {"pct": "5", "floor": "200", "clause": "CC-PL-4.1"},
            },
            "clauses": {
                "CC-PL-4.1": "The minimum amount due is 5% of the statement balance or Rs 200, whichever is higher, plus any EMI instalments and overdue amounts.",
                "CC-PL-4.2": "Paying less than the total amount due attracts finance charges at 3.5% per month on the unpaid balance from the transaction date.",
                "CC-PL-9.2": "Transactions can be disputed within 60 days of the transaction date. A temporary credit is given within 10 working days while the dispute is investigated.",
            },
        },
    },
}

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


def _last_due_on_or_before(as_of: date, day: int = 5) -> date:
    d = date(as_of.year, as_of.month, day)
    return d if d <= as_of else add_months(d, -1)


def seed(conn: sqlite3.Connection, as_of: date, n_random: int = 500, rng_seed: int = 42) -> None:
    conn.executescript(SCHEMA)
    for code, p in PRODUCTS.items():
        conn.execute("INSERT INTO products VALUES(?,?,?,?,?)",
                     (code, p["name"], p["kind"], p["rate_type"], json.dumps(p["policy"])))
    rng = random.Random(rng_seed)
    last_due = _last_due_on_or_before(as_of)

    # --- Scenario customers -------------------------------------------------
    _customer(conn, "C1001", "Priya Sharma", "9800000001", "1990-04-12", "Mumbai", "salaried")
    _customer(conn, "C1002", "Rahul Verma", "9800000002", "1986-11-03", "Bengaluru", "self-employed")
    _customer(conn, "C1003", "Meera Nair", "9800000003", "1994-07-21", "Pune", "salaried")
    _customer(conn, "C1004", "Ananya Iyer", "9800000004", "1992-02-14", "Chennai", "salaried")

    # Priya: floating rate, EMI went up after the latest reset, 3 failed debits in 6 months.
    first_due = add_months(last_due, -19)
    disb = first_due - timedelta(days=31)
    resets = [RateChange(disb, Decimal("10.50"), "Sanction rate: EBLR 9.00% + spread 1.50%"),
              RateChange(add_months(last_due, -9) - timedelta(days=10), Decimal("10.25"),
                         "Quarterly EBLR reset after the RBI repo rate cut of 0.25%"),
              RateChange(add_months(last_due, -1) - timedelta(days=10), Decimal("11.00"),
                         "Quarterly EBLR reset after the RBI repo rate increase of 0.75%")]
    _loan(conn, "L-10001", "C1001", "PL-FLOAT", Decimal(800000), disb, first_due, 60, resets, as_of,
          failures={-2: ("INSUFFICIENT_FUNDS", 3), -4: ("BANK_TECHNICAL", 2), -5: ("MANDATE_CANCELLED", 6)})
    _card(conn, rng, "CARD-5001", "C1001", "4417", Decimal(200000), as_of, duplicate=False)

    # Rahul: fixed rate, past the 12-EMI lock-in but < 24 months -> 4% foreclosure charge.
    first_due = add_months(last_due, -13)
    disb = first_due - timedelta(days=30)
    _loan(conn, "L-10002", "C1002", "PL-FIX", Decimal(500000), disb, first_due, 36,
          [RateChange(disb, Decimal("13.50"), "Fixed rate at sanction")], as_of, failures={-7: ("ACCOUNT_FROZEN", 4)})

    # Meera: fixed rate, inside the lock-in.
    first_due = add_months(last_due, -6)
    disb = first_due - timedelta(days=30)
    _loan(conn, "L-10003", "C1003", "PL-FIX", Decimal(300000), disb, first_due, 24,
          [RateChange(disb, Decimal("14.00"), "Fixed rate at sanction")], as_of, failures={})

    # Ananya: card holder with a duplicate charge to dispute (workflow 2).
    _card(conn, rng, "CARD-5002", "C1004", "9021", Decimal(150000), as_of, duplicate=True)

    for cid, ts_days, ch, s in [("C1001", 40, "branch", "Asked why the EMI changed; was told to call back"),
                                ("C1001", 12, "phone", "Reported a failed auto-debit; waited 18 minutes"),
                                ("C1002", 30, "email", "Requested foreclosure charges")]:
        conn.execute("INSERT INTO interactions(customer_id,ts,channel,summary) VALUES(?,?,?,?)",
                     (cid, (as_of - timedelta(days=ts_days)).isoformat(), ch, s))

    # --- Background population ---------------------------------------------
    for i in range(n_random):
        cid = f"C{2000 + i}"
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        dob = date(rng.randint(1965, 2000), rng.randint(1, 12), rng.randint(1, 28)).isoformat()
        _customer(conn, cid, name, f"97{rng.randint(10000000, 99999999)}", dob, rng.choice(CITIES),
                  rng.choice(["salaried", "self-employed"]))
        product = rng.choice(["PL-FLOAT", "PL-FIX"])
        months_ago = rng.randint(2, 40)
        tenure = rng.choice([24, 36, 48, 60])
        if months_ago >= tenure:
            months_ago = tenure - 1
        first_due = add_months(last_due, -(months_ago - 1))
        disb = first_due - timedelta(days=30)
        base = Decimal(rng.choice(["10.25", "10.75", "11.50", "12.00", "13.25", "14.50"]))
        hist = [RateChange(disb, base, "Rate at sanction")]
        if product == "PL-FLOAT" and months_ago > 6:
            hist.append(RateChange(add_months(last_due, -rng.randint(1, 5)) - timedelta(days=9),
                                   base + Decimal("0.50"), "Quarterly EBLR reset"))
        fails = {}
        if rng.random() < 0.2:
            fails[-rng.randint(0, min(5, months_ago - 1))] = (rng.choice(list(FAILURE_REASONS)), rng.randint(1, 6))
        _loan(conn, f"L-{20000 + i}", cid, product, Decimal(rng.randrange(100000, 1500000, 25000)), disb,
              first_due, tenure, hist, as_of, failures=fails)
        if rng.random() < 0.3:
            _card(conn, rng, f"CARD-{6000 + i}", cid, f"{rng.randint(1000, 9999)}",
                  Decimal(rng.choice([50000, 100000, 200000])), as_of, duplicate=False)
    conn.commit()


def _customer(conn, cid, name, phone, dob, city, segment):
    email = name.lower().replace(" ", ".") + "@example.com"
    conn.execute("INSERT INTO customers VALUES(?,?,?,?,?,?,?,?)",
                 (cid, name, phone, dob, email, city, segment, "en"))


def _loan(conn, lid, cid, product, principal, disb, first_due, tenure, history, as_of, failures):
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
            charge = money(500) if chargeable else money(0)
            gst = money(charge * Decimal("0.18"))
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


def _card(conn, rng, card_id, cid, last4, limit, as_of, duplicate):
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
        txns.append((ts, "Croma Electronics", money("18499.00")))
        txns.append((ts + timedelta(minutes=1), "Croma Electronics", money("18499.00")))
    txns.sort()
    balance = money(sum(a for _, _, a in txns))
    min_due = max(money(balance * Decimal("0.05")), money(200))
    conn.execute("INSERT INTO cards VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (card_id, cid, "CC-PLAT", last4, str(limit), stmt.isoformat(), due.isoformat(), str(balance),
                  str(min_due), str(money(limit - balance))))
    for k, (ts, m, a) in enumerate(txns):
        conn.execute("INSERT INTO card_txns VALUES(?,?,?,?,?,?)",
                     (f"T-{card_id[5:]}-{k + 1:03d}", card_id, ts.isoformat(timespec="minutes"),
                      m, str(a), "posted"))
