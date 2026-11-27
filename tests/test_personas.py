"""Demo customers come from data/customers.yaml: adding a person needs no code."""
import sqlite3
from datetime import date
from decimal import Decimal

import pytest
import yaml

from ledgerflow.mocks.personas import PersonaError, demo_customers, load_personas
from ledgerflow.mocks.seed import CUSTOMERS_FILE, seed
from ledgerflow.mocks.server import MockSystems

NEW = {"id": "C1005", "name": "Vikram Rao", "phone": "9800000005", "dob": "1988-09-30", "city": "Hyderabad",
       "segment": "salaried", "demo": True, "story": "Floating loan with one bounce last month",
       "loan": {"id": "L-10005", "product": "PL-FLOAT", "principal": 400000, "tenure_months": 36, "emis_paid": 10,
                "rate": 11.25, "failed_payments": [{"emis_ago": 1, "reason": "INSUFFICIENT_FUNDS", "retry_days": 2}]},
       "card": {"id": "CARD-5005", "last4": "7788", "limit": 100000,
                "duplicate_charge": {"merchant": "BigBasket", "amount": 2450}}}


@pytest.mark.req("REQ-DAT-001.5")
def test_adding_a_customer_to_the_yaml_seeds_every_system(tmp_path):
    people = load_personas(CUSTOMERS_FILE) + [NEW]
    f = tmp_path / "customers.yaml"
    f.write_text(yaml.safe_dump({"customers": people}, allow_unicode=True), encoding="utf-8")
    loaded = load_personas(f)
    assert {"name": "Vikram Rao", "phone": "9800000005", "dob": "1988-09-30",
            "story": "Floating loan with one bounce last month"} in demo_customers(loaded)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    seed(conn, date(2026, 11, 26), n_random=0, personas=loaded)
    assert conn.execute("SELECT name FROM customers WHERE phone='9800000005'").fetchone()["name"] == "Vikram Rao"
    paid = conn.execute("SELECT COUNT(DISTINCT instalment_no) n FROM payments WHERE loan_id='L-10005'").fetchone()["n"]
    assert paid == 10
    assert conn.execute("SELECT reason_code FROM payments WHERE loan_id='L-10005' AND status='failed'").fetchone()[0] == "INSUFFICIENT_FUNDS"
    dups = conn.execute("SELECT COUNT(*) FROM card_txns WHERE card_id='CARD-5005' AND merchant='BigBasket' AND amount='2450.00'").fetchone()[0]
    assert dups == 2


@pytest.mark.req("REQ-DAT-001.6")
def test_validation_lists_every_problem_readably(tmp_path):
    bad = [{**NEW, "phone": "98000", "dob": "30-09-1988", "loan": {**NEW["loan"], "product": "HOME", "emis_paid": 40}},
           {**NEW, "id": "C2001", "card": {**NEW["card"], "id": "CARD-5005"}}]
    f = tmp_path / "bad.yaml"
    f.write_text(yaml.safe_dump({"customers": bad}), encoding="utf-8")
    with pytest.raises(PersonaError) as e:
        load_personas(f)
    msg = str(e.value)
    for needle in ("phone must be 10 digits", "dob must be YYYY-MM-DD", "product must be a loan product in data/policies.yaml",
                   "emis_paid must be less than tenure_months", "reserved for the generated", "id L-10005 is used twice",
                   "CARD-5005 is used twice"):
        assert needle in msg, needle


@pytest.mark.req("REQ-DAT-001.5")
def test_shipped_file_reproduces_the_scenarios(tmp_path):
    ms = MockSystems(str(tmp_path / "t.db"), date(2026, 11, 26))
    loan = ms.loan_detail("L-10001")
    assert [h["rate"] for h in loan["rate_history"]] == ["10.50", "10.25", "11.00"]
    assert Decimal(loan["principal"]) == 800000
