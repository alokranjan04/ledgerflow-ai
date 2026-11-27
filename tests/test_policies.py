"""Product policies from data/policies.yaml: a new product and a new customer need no code change;
the file is validated by product, rule and field; answers show readable dates."""
import copy

import pytest
import yaml

from ledgerflow.mocks import policies as policies_mod
from ledgerflow.mocks import seed as seed_mod
from ledgerflow.mocks.personas import PersonaError, load_personas
from ledgerflow.mocks.policies import PolicyError, load_policies, validate
from ledgerflow.platform.fastlane import readable_dates

from .conftest import PRIYA

REAL = yaml.safe_load(open(policies_mod.POLICIES_FILE, encoding="utf-8"))["products"]

# A new loan product and a new card product, defined only as data.
PLUS = {"name": "Personal Loan Plus (floating)", "kind": "loan", "rate_type": "floating",
        "policy": {"version": "2027.01", "effective": "2026-11-01",
                   "rules": {"foreclosure": {"min_emis_paid": 3, "charge_pct": 1, "clause": "PLP-5.1"},
                             "part_prepayment": {"min_emis_paid": 6, "min_amount": 25000,
                                                 "max_pct_outstanding_per_fy": 40, "charge_pct": 0, "clause": "PLP-5.2"},
                             "rate_reset": {"benchmark": "RBI repo rate", "frequency": "quarterly", "clause": "PLP-3.1"},
                             "bounce_charge": {"amount": 750, "gst": True, "clause": "PLP-7.1"},
                             "emi_date_change": {"allowed_days": [1, 7], "max_per_year": 2, "clause": "PLP-4.1"}},
                   "clauses": {"PLP-3.1": "Rate linked to the repo rate, reset quarterly.",
                               "PLP-4.1": "EMI date can move to the 1st or 7th, twice a year.",
                               "PLP-5.1": "Foreclosure after 3 EMIs with a 1% charge plus GST.",
                               "PLP-5.2": "Part-prepayment from Rs 25,000, up to 40% a year, no charge.",
                               "PLP-7.1": "A failed debit costs Rs 750 plus GST."}}}
GOLD = {"name": "LedgerFlow Gold Credit Card", "short_name": "Gold card", "kind": "card",
        "policy": {"version": "2027.01", "effective": "2026-11-01",
                   "rules": {"min_due": {"pct": 3, "floor": 100, "clause": "CG-4.1"},
                             "finance_charge": {"monthly_pct": 2.75, "clause": "CG-4.2"},
                             "dispute": {"window_days": 30, "temporary_credit_working_days": 7, "clause": "CG-9.1"}},
                   "clauses": {"CG-4.1": "Minimum due is 3% of the balance or Rs 100.",
                               "CG-4.2": "Finance charges are 2.75% a month on the unpaid balance.",
                               "CG-9.1": "Disputes within 30 days; temporary credit within 7 working days."}}}
VIKRAM = {"id": "C1005", "name": "Vikram Rao", "phone": "9800000005", "dob": "1988-02-14", "city": "Pune",
          "segment": "salaried", "demo": True, "story": "New Plus loan and a Gold card",
          "loan": {"id": "L-10005", "product": "PL-PLUS", "principal": 500000, "tenure_months": 36, "emis_paid": 8,
                   "rate": 11.25, "failed_payments": [{"emis_ago": 1, "reason": "INSUFFICIENT_FUNDS"}]},
          "card": {"id": "CARD-5005", "product": "CC-GOLD", "last4": "9090", "limit": 150000,
                   "duplicate_charge": {"merchant": "Tata Cliq", "amount": 2999.00}}}


@pytest.fixture
def new_data(tmp_path, monkeypatch):
    """Point the systems at policy and customer files that add PL-PLUS, CC-GOLD and Vikram."""
    pol = tmp_path / "policies.yaml"
    pol.write_text(yaml.safe_dump({"products": {**REAL, "PL-PLUS": PLUS, "CC-GOLD": GOLD}}, allow_unicode=True),
                   encoding="utf-8")
    people = yaml.safe_load(open(seed_mod.CUSTOMERS_FILE, encoding="utf-8"))["customers"] + [VIKRAM]
    cust = tmp_path / "customers.yaml"
    cust.write_text(yaml.safe_dump({"customers": people}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(policies_mod, "POLICIES_FILE", pol)
    monkeypatch.setattr(seed_mod, "CUSTOMERS_FILE", cust)


@pytest.mark.req("REQ-DAT-001.7")
async def test_a_new_product_and_customer_work_end_to_end_without_code(new_data, env):
    s, ctx = await env["login"]("9800000005", "1988-02-14")
    summary = (await env["run"](ctx, "get_loan_summary")).data
    assert summary["product"] == "PL-PLUS" and summary["rate_type"] == "floating"
    failed = {"failed_payments": (await env["run"](ctx, "get_payments", status="failed")).data["payments"]}
    assert failed["failed_payments"][0]["bounce_charge"] == "₹750"          # the new product's charge
    assert failed["failed_payments"][0]["gst_on_charge"] == "₹135"
    small = (await env["run"](ctx, "simulate_prepayment", amount=20000)).data
    assert not small["eligible"] and small["clause"] == "PLP-5.2"               # its own minimum and clause
    assert "₹25,000" in " ".join(small["ineligible_reasons"])
    options = (await env["run"](ctx, "get_servicing_options")).data["options"]
    assert {o["clause"] for o in options} >= {"PLP-5.2", "PLP-5.1", "PLP-4.1"}

    s.workflow_id = "card_servicing"
    card = (await env["run"](ctx, "get_card_summary")).data
    assert card["card"] == "Gold card ending 9090" and card["min_due_clause"] == "CG-4.1"
    assert card["finance_charge_clause"] == "CG-4.2" and "2.75%" in card["interest_if_only_minimum_paid"]
    row = env["ms"].q("SELECT statement_balance, min_due FROM cards WHERE id='CARD-5005'")[0]
    from decimal import Decimal
    assert Decimal(row["min_due"]) == max((Decimal(row["statement_balance"]) * 3 / 100).quantize(Decimal("0.01")),
                                          Decimal(100))
    txns = (await env["run"](ctx, "get_card_transactions", merchant="Tata")).data["transactions"]
    dup = next(t for t in txns if t.get("possible_duplicate_of"))
    prop = await env["run"](ctx, "propose_card_dispute", transaction_id=dup["transaction_id"], reason="charged twice")
    assert prop.data["clause"] == "CG-9.1" and "within 7 working days" in prop.data["summary"]


@pytest.mark.req("REQ-DAT-001.7")
async def test_existing_customers_are_unchanged(env):
    s, ctx = await env["login"](*PRIYA)
    failed = (await env["run"](ctx, "get_payments", status="failed")).data["payments"]
    assert {p["bounce_charge"] for p in failed} == {"₹500", "₹0"}
    s.workflow_id = "card_servicing"
    card = (await env["run"](ctx, "get_card_summary")).data
    assert card["card"] == "Platinum card ending 4417" and card["finance_charge_clause"] == "CC-PL-4.2"


@pytest.mark.req("REQ-DAT-001.8")
def test_policy_file_problems_are_listed_by_product_rule_and_field():
    bad = copy.deepcopy({"PL-FLOAT": REAL["PL-FLOAT"], "PL-FIX": REAL["PL-FIX"], "CC-PLAT": REAL["CC-PLAT"]})
    fl, fx, cc = (bad[k]["policy"]["rules"] for k in ("PL-FLOAT", "PL-FIX", "CC-PLAT"))
    fl["bounce_charge"]["ammount"] = fl["bounce_charge"].pop("amount")          # typo in a field
    fl["part_prepayment"]["charge_pct"] = 140                                    # not a percentage
    fl["late_fee"] = {"amount": 100, "clause": "PL-FL-7.1"}                      # a rule nothing calculates
    fx["emi_date_change"]["clause"] = "PL-FX-9.9"                                # clause that does not exist
    fx["rate_reset"] = {"benchmark": "x", "frequency": "y", "clause": "PL-FX-4.3"}   # fixed rate cannot reset
    del cc["dispute"]                                                            # required rule missing
    bad["CC-PLAT"].pop("short_name")
    bad["pl float"] = {"kind": "mortgage"}
    errors, _ = validate(bad)
    text = "\n".join(errors)
    for needle in ("product PL-FLOAT rule bounce_charge: unknown field 'ammount'",
                   "product PL-FLOAT rule bounce_charge: missing field 'amount'",
                   "product PL-FLOAT rule part_prepayment: charge_pct must be between 0 and 100",
                   "product PL-FLOAT rule late_fee: no calculation uses this rule",
                   "product PL-FIX rule emi_date_change: clause names clause 'PL-FX-9.9'",
                   "product PL-FIX rule rate_reset: only floating-rate loans have rate resets",
                   "product CC-PLAT: missing rule 'dispute'",
                   "product CC-PLAT: card products need a short_name",
                   "product pl float: code must be capital letters",
                   "product pl float: kind must be 'loan' or 'card'"):
        assert needle in text, needle


@pytest.mark.req("REQ-DAT-001.8")
def test_customers_may_only_hold_products_the_file_defines(tmp_path):
    products = load_policies()
    f = tmp_path / "c.yaml"
    f.write_text(yaml.safe_dump({"customers": [VIKRAM]}), encoding="utf-8")
    with pytest.raises(PersonaError) as e:
        load_personas(f, products)
    assert "product must be a loan product in data/policies.yaml" in str(e.value)
    assert "product must be a card product in data/policies.yaml" in str(e.value)
    broken = tmp_path / "p.yaml"
    broken.write_text("products: {X-1: {kind: loan}}", encoding="utf-8")
    with pytest.raises(PolicyError, match="problem"):
        load_policies(broken)


@pytest.mark.req("REQ-DAT-001.7")
def test_amounts_come_back_exact_for_the_decimal_engine():
    p = load_policies()
    assert p["PL-FLOAT"]["policy"]["rules"]["part_prepayment"]["min_amount"] == "10000"
    assert p["CC-PLAT"]["policy"]["rules"]["finance_charge"]["monthly_pct"] == "3.5"
    assert p["PL-FIX"]["policy"]["rules"]["foreclosure"]["lock_in_emis"] == 12


@pytest.mark.req("REQ-AGT-002.6")
def test_dates_read_naturally_in_answers():
    assert readable_dates("due by **2026-12-10**.", "en") == "due by **10 December 2026**."
    assert readable_dates("देय तिथि 2026-12-10 है", "hi") == "देय तिथि 10 दिसंबर 2026 है"
    untouched = "T-5001-003 at 2026-11-20T15:42, SR-2026-12-10X, 2026-13-01"
    assert readable_dates(untouched, "en") == untouched


@pytest.mark.req("REQ-AGT-002.6")
async def test_model_answer_with_iso_date_reaches_the_customer_readable(env):
    from ledgerflow.platform.gemini_text import TextAgent

    from .test_text_agent import ScriptedLLM, fc, text
    s, _ = await env["login"](*PRIYA)
    agent = TextAgent(ScriptedLLM([fc("get_loan_summary"), text("Your next EMI of ₹17,323.49 is due on 2026-12-05.")]),
                      env["wfs"], env["con"], env["actions"])
    events = []

    async def emit(e):
        events.append(e)
    reply = await agent.turn(s, "When is my next EMI due?", emit)
    assert reply == "Your next EMI of ₹17,323.49 is due on 5 December 2026."
    assert next(e for e in events if e["type"] == "assistant")["text"] == reply
