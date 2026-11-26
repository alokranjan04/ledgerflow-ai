from decimal import Decimal

import pytest

from ledgerflow.platform.grounding import Grounder, extract_numbers, words_to_number

TOOL = [{"previous_emi": "₹17,112.63", "new_emi": "₹17,323.49", "emi_difference": "₹210.86",
         "new_rate_pct": "11.00", "previous_rate_pct": "10.25", "reduce_tenure": {"interest_saved": "₹21,003.30",
                                                                                   "tenure_reduced_by_months": 5}}]
G = Grounder()


@pytest.mark.req("REQ-AGT-004.1")
def test_words_to_number():
    assert words_to_number("twenty one thousand and three") == Decimal(21003)
    assert words_to_number("five lakh seventy seven thousand") == Decimal(577000)
    assert words_to_number("eleven point two five") == Decimal("11.25")


@pytest.mark.req("REQ-AGT-004.2")
def test_exact_and_rounded_figures_pass():
    reply = "Your EMI rose from **₹17,112.63** to ₹17,323 because the rate went from 10.25% to 11%."
    assert G.check(reply, TOOL, []) == []


@pytest.mark.req("REQ-AGT-004.2")
def test_derived_difference_passes():
    assert G.check("That's ₹210.86 more per month.", TOOL, []) == []
    assert G.check("The rate rose by 0.75%.", TOOL, []) == []   # 11.00 - 10.25


@pytest.mark.req("REQ-AGT-004.6")
def test_hallucinated_number_is_caught():
    bad = G.check("You would save ₹25,000 in interest.", TOOL, [])
    assert bad and "25,000" in bad[0]


@pytest.mark.req("REQ-AGT-004.1")
def test_lakh_notation_and_spoken_words():
    assert G.check("about twenty one thousand rupees saved", TOOL, []) == []
    assert G.check("You'd save 0.21 lakh", TOOL, []) == []
    assert G.check("You'd save thirty thousand rupees", TOOL, [])


@pytest.mark.req("REQ-AGT-004.2", "REQ-AGT-004.3")
def test_user_supplied_numbers_and_dates_are_not_flagged():
    assert G.check("If you prepay ₹50,000 on 5 December 2026, the tenure drops by 5 months.", TOOL,
                   ["what if I prepay 50,000?"]) == []


@pytest.mark.req("REQ-AGT-004.3")
def test_ids_are_ignored():
    assert extract_numbers("Request SR-1A2B3C4D for payment P-10001-018F") == []


@pytest.mark.req("REQ-AGT-004.6")
def test_wrong_count_is_caught():
    assert G.check("Your tenure drops by 9 months.", TOOL, [])
