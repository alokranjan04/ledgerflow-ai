from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ledgerflow.finance.loans import (
    RateChange,
    accrued_interest,
    add_months,
    amortise,
    build_schedule,
    emi,
    inr,
    money,
    simulate_prepayment,
)


@pytest.mark.req("REQ-FIN-001.1")
def test_emi_golden_values():
    # Cross-checked with spreadsheet PMT(rate/12, n, -P)
    assert emi(Decimal(100000), Decimal(12), 12) == Decimal("8884.88")
    assert emi(Decimal(800000), Decimal("10.5"), 60) == Decimal("17195.12")
    assert emi(Decimal(500000), Decimal("13.5"), 36) == Decimal("16967.64")
    assert emi(Decimal(120000), Decimal(0), 12) == Decimal("10000.00")


@pytest.mark.req("REQ-FIN-001.1")
def test_rounding_half_up_to_paisa():
    assert money("1.005") == Decimal("1.01")
    assert money("2.344999") == Decimal("2.34")


@pytest.mark.req("REQ-FIN-001.6")
def test_inr_indian_grouping():
    assert inr(Decimal("1234567.5")) == "₹12,34,567.50"
    assert inr(800000) == "₹8,00,000"
    assert inr(999) == "₹999"
    assert inr(Decimal("-1500")) == "-₹1,500"


@pytest.mark.req("REQ-FIN-001.7")
def test_add_months_clamps_month_end():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 11, 5), 2) == date(2027, 1, 5)


@pytest.mark.req("REQ-FIN-001.2")
def test_schedule_principal_sums_to_sanctioned_and_ends_at_zero():
    s = build_schedule(Decimal(800000), 60, date(2025, 4, 5), [RateChange(date(2025, 3, 5), Decimal("10.5"))])
    assert sum(r.principal for r in s.rows) == Decimal(800000)
    assert s.rows[-1].closing_balance == 0
    assert len(s.rows) == 60


@pytest.mark.req("REQ-FIN-001.3")
def test_rate_reset_recomputes_emi_over_remaining_tenure():
    hist = [RateChange(date(2025, 3, 5), Decimal("10.50")), RateChange(date(2025, 9, 20), Decimal("11.00"))]
    s = build_schedule(Decimal(800000), 60, date(2025, 4, 5), hist)
    before = [r for r in s.rows if r.due_date <= date(2025, 10, 5)]
    after = [r for r in s.rows if r.due_date >= date(2025, 11, 5)]
    assert before[-1].rate_pct == Decimal("10.50")       # period starting 5 Sep still at the old rate
    assert after[0].rate_pct == Decimal("11.00")
    assert after[0].emi > before[-1].emi
    assert after[0].emi == emi(before[-1].closing_balance, Decimal("11.00"), 60 - before[-1].no)
    assert sum(r.principal for r in s.rows) == Decimal(800000)


@pytest.mark.req("REQ-FIN-001.4")
def test_prepayment_reduce_tenure_saves_at_least_reduce_emi():
    out = simulate_prepayment(Decimal("577913.11"), Decimal("11"), Decimal("17323.49"), 40, Decimal(50000))
    assert out.baseline_months == 40
    assert out.reduce_tenure_saving > out.reduce_emi_saving > 0
    assert out.reduce_tenure_months < 40
    assert out.reduce_emi_new_emi < Decimal("17323.49")


@pytest.mark.req("REQ-FIN-001.5")
def test_accrued_interest_actual_365():
    assert accrued_interest(Decimal(365000), Decimal(10), date(2026, 1, 1), date(2026, 1, 11)) == Decimal("1000.00")


@pytest.mark.req("REQ-FIN-001.2")
@settings(max_examples=60, deadline=None)
@given(p=st.integers(50_000, 2_000_000), rate=st.decimals("6", "24", places=2), n=st.integers(6, 84))
def test_property_schedule_never_negative_and_closes(p, rate, n):
    s = build_schedule(Decimal(p), n, date(2025, 1, 5), [RateChange(date(2024, 12, 5), rate)])
    assert all(r.closing_balance >= 0 for r in s.rows)
    assert s.rows[-1].closing_balance == 0
    assert sum(r.principal for r in s.rows) == Decimal(p)


@pytest.mark.req("REQ-FIN-001.4")
@settings(max_examples=40, deadline=None)
@given(bal=st.integers(100_000, 1_500_000), rate=st.decimals("8", "18", places=2), n=st.integers(12, 72),
       a=st.integers(10_000, 50_000), b=st.integers(10_000, 50_000))
def test_property_larger_prepayment_never_increases_interest(bal, rate, n, a, b):
    lo, hi = sorted((a, b))
    e = emi(Decimal(bal), rate, n)
    small = simulate_prepayment(Decimal(bal), rate, e, n, Decimal(lo))
    large = simulate_prepayment(Decimal(bal), rate, e, n, Decimal(hi))
    assert large.reduce_tenure_interest <= small.reduce_tenure_interest
    assert large.reduce_emi_interest <= small.reduce_emi_interest
    assert small.reduce_tenure_saving >= small.reduce_emi_saving


@pytest.mark.req("REQ-FIN-001.8")
def test_amortise_folds_rounding_residue_into_last_emi():
    e = emi(Decimal(100000), Decimal(12), 12)
    months, _ = amortise(Decimal(100000), Decimal(12), e)
    assert months == 12
