"""Customers with several loans and cards: the assistant names the account and never guesses."""
import pytest

from ledgerflow import config
from ledgerflow.platform.accounts import NeedsChoice, hint_from_text, match, pick
from ledgerflow.platform.gemini_text import TextAgent
from ledgerflow.platform.grounding import Grounder

from .conftest import PRIYA
from .test_fastlane import NoModel, _noop

ARJUN = ("9800000010", "1985-06-20")
LOANS = [{"id": "L-10010", "label": "Home loan L-10010"}, {"id": "L-10011", "label": "Car loan L-10011"}]
CARDS = [{"id": "CARD-5010", "label": "Platinum card ending 2290", "last4": "2290"},
         {"id": "CARD-5011", "label": "Rewards card ending 7781", "last4": "7781"}]


# ----------------------------------------------------------------- choosing an account
@pytest.mark.req("REQ-ACC-001.2")
@pytest.mark.parametrize("hint,expect", [
    ("L-10011", "L-10011"), ("l-10010", "L-10010"), ("car", "L-10011"), ("my car loan", "L-10011"),
    ("auto", "L-10011"), ("gaadi wala", "L-10011"), ("home loan", "L-10010"), ("ghar", "L-10010"), ("10011", "L-10011")])
def test_named_loan_is_found(hint, expect):
    assert pick({}, "loan", LOANS, hint)["id"] == expect


@pytest.mark.req("REQ-ACC-001.2")
def test_cards_by_last_digits_or_name():
    assert pick({}, "card", CARDS, "ending 7781")["id"] == "CARD-5011"
    assert pick({}, "card", CARDS, "7781")["id"] == "CARD-5011"
    assert pick({}, "card", CARDS, "the platinum one")["id"] == "CARD-5010"
    assert match(CARDS, "rewards card") == [CARDS[1]]


@pytest.mark.req("REQ-ACC-001.2", "REQ-ACC-001.3")
def test_no_guessing_and_follow_ups_stay_on_the_chosen_account():
    memory = {}
    with pytest.raises(NeedsChoice) as e:
        pick(memory, "loan", LOANS)
    assert [o["label"] for o in e.value.options] == ["Home loan L-10010", "Car loan L-10011"]
    assert "Do not guess" in e.value.result()["error"]
    with pytest.raises(NeedsChoice) as e:                      # a word that matches nothing is not a choice
        pick(memory, "loan", LOANS, "boat")
    assert "Nothing matched 'boat'" in e.value.result()["error"]
    assert pick(memory, "loan", LOANS, "car")["id"] == "L-10011"
    assert pick(memory, "loan", LOANS)["id"] == "L-10011"       # "and the next EMI?" stays on the car loan
    assert pick(memory, "loan", LOANS, "home")["id"] == "L-10010"
    single = [LOANS[0]]
    assert pick({}, "loan", single)["id"] == "L-10010" and pick({}, "loan", single, "my loan")["id"] == "L-10010"


@pytest.mark.req("REQ-ACC-001.4")
def test_account_words_are_picked_out_of_a_question():
    assert hint_from_text("What's the next EMI on my car loan?") == "car"
    assert hint_from_text("statement for the card ending 7781") == "7781"
    assert hint_from_text("foreclosure for L-10010") == "l-10010"
    assert hint_from_text("What's my next EMI?") == ""


# ----------------------------------------------------------------- tools on a two-loan, two-card customer
@pytest.mark.req("REQ-ACC-001.1", "REQ-ACC-001.2", "REQ-ACC-001.3")
async def test_loan_tools_ask_then_answer_for_the_named_loan(env):
    s, ctx = await env["login"](*ARJUN)
    held = (await env["run"](ctx, "list_accounts")).data
    assert [x["loan"] for x in held["loans"]] == ["Home loan L-10010", "Car loan L-10011"]
    assert [x["card"] for x in held["cards"]] == ["Platinum card ending 2290", "Rewards card ending 7781"]
    ask = await env["run"](ctx, "get_loan_summary")
    assert ask.data["needs_choice"] and ask.card["type"] == "choose_account" and len(ask.data["options"]) == 2
    car = (await env["run"](ctx, "get_loan_summary", loan_id="car")).data
    assert car["loan"] == "Car loan L-10011" and car["product"] == "AL-FIX" and car["rate_type"] == "fixed"
    failed = (await env["run"](ctx, "get_payments", status="failed")).data           # stays on the car loan
    assert failed["loan"] == "Car loan L-10011" and failed["payments"][0]["bounce_charge"] == "₹600"
    fc = (await env["run"](ctx, "check_foreclosure")).data
    assert fc["eligible"] and fc["foreclosure_charge_pct"] == "5" and fc["clause"] == "AL-FX-6.2"
    home = (await env["run"](ctx, "explain_emi_change", loan_id="home")).data
    assert home["loan"] == "Home loan L-10010" and home["emi_changed"] and home["new_rate_pct"] == "9.00"
    pre = (await env["run"](ctx, "simulate_prepayment", amount=25000)).data           # now on the home loan
    assert pre["loan"] == "Home loan L-10010" and pre["eligible"] and pre["prepayment_charge"] == "₹0"
    # a dispute names its payment, and the payment ID says which loan it is on
    pid = failed["payments"][0]["payment_id"]
    s.user_turn_seq += 1
    prop = (await env["run"](ctx, "propose_payment_dispute", payment_id=pid, reason="mandate was active")).data
    assert prop["ok"] and "₹708" in prop["summary"]


@pytest.mark.req("REQ-ACC-001.1", "REQ-ACC-001.2")
async def test_card_tools_ask_then_answer_for_the_named_card(env):
    s, ctx = await env["login"](*ARJUN, workflow="card_servicing")
    assert (await env["run"](ctx, "get_card_summary")).data["needs_choice"]
    rew = (await env["run"](ctx, "get_card_summary", card_id="7781")).data
    assert rew["card"] == "Rewards card ending 7781" and rew["min_due_clause"] == "CR-4.1"
    assert "3.75%" in rew["interest_if_only_minimum_paid"]
    txns = (await env["run"](ctx, "get_card_transactions", merchant="Flipkart")).data
    assert txns["card"] == "Rewards card ending 7781"
    dup = next(t for t in txns["transactions"] if t.get("possible_duplicate_of"))
    s.user_turn_seq += 1
    prop = (await env["run"](ctx, "propose_card_dispute", transaction_id=dup["transaction_id"], reason="twice")).data
    assert prop["clause"] == "CR-9.1" and "within 7 working days" in prop["summary"]
    other = await env["run"](ctx, "propose_card_dispute", transaction_id="T-5001-001", reason="x")   # Priya's card
    assert not other.data["ok"]


@pytest.mark.req("REQ-ACC-001.1")
async def test_single_account_customers_are_unchanged(env):
    s, ctx = await env["login"](*PRIYA)
    res = (await env["run"](ctx, "get_loan_summary")).data
    assert res["loan"] == "Personal loan L-10001" and res["current_emi"] == "₹17,323.49"


# ----------------------------------------------------------------- fast lane, no model
@pytest.mark.req("REQ-ACC-001.4")
async def test_fast_lane_asks_which_loan_then_names_it(env, monkeypatch):
    monkeypatch.setattr(config, "FAST_LANE", True)
    s, _ = await env["login"](*ARJUN)
    agent = TextAgent(NoModel(), env["wfs"], env["con"], env["actions"])
    reply = await agent.turn(s, "What's my next EMI?", _noop)
    assert reply == "You have 2 loans with us. Which one do you mean: Home loan L-10010 or Car loan L-10011?"
    reply = await agent.turn(s, "When is the next EMI on my car loan?", _noop)
    assert reply.startswith("**Car loan L-10011**: ") and "₹" in reply
    assert Grounder().check(reply, s.memory["tool_results"], ["When is the next EMI on my car loan?"]) == []


# ----------------------------------------------------------------- account panel
@pytest.mark.req("REQ-ACC-001.5")
async def test_account_panel_lists_every_account_without_changing_the_conversation(env):
    import app as lfapp
    from ledgerflow.platform.tools import REGISTRY

    s, ctx = await env["login"](*ARJUN)
    await env["run"](ctx, "get_loan_summary", loan_id="car")

    class Req:
        app = {}

    async def direct(request, sess, name, args=None):
        return await REGISTRY.run(name, args or {}, ctx, [name])
    orig = lfapp._direct_tool
    lfapp._direct_tool = direct

    async def session(request):
        return s
    orig_session, lfapp._session = lfapp._session, session
    try:
        import json
        body = json.loads((await lfapp.api_account(Req())).body)
    finally:
        lfapp._direct_tool, lfapp._session = orig, orig_session
    assert [x["loan"] for x in body["loans"]] == ["Home loan L-10010", "Car loan L-10011"]
    assert [x["card"] for x in body["cards"]] == ["Platinum card ending 2290", "Rewards card ending 7781"]
    assert s.memory["current_loan"] == "L-10011"                # still the car loan in the conversation


# ----------------------------------------------------------------- data rules
@pytest.mark.req("REQ-ACC-001.6")
def test_customer_file_rules_for_several_accounts(tmp_path):
    import yaml

    from ledgerflow.mocks.personas import PersonaError, load_personas
    bad = [{"id": "C1600", "name": "A B", "phone": "9800000099", "dob": "1990-01-01",
            "loan": {"id": "L-10099", "product": "PL-FIX", "principal": 100000, "tenure_months": 12, "emis_paid": 2,
                     "rate": 12}, "loans": []}]
    f = tmp_path / "c.yaml"
    f.write_text(yaml.safe_dump({"customers": bad}), encoding="utf-8")
    with pytest.raises(PersonaError) as e:
        load_personas(f)
    msg = str(e.value)
    assert "use either 'loan' or 'loans'" in msg and "ids C1500-C1999 are kept" in msg


@pytest.mark.req("REQ-ACC-001.6")
async def test_created_customers_get_ids_that_never_collide_with_the_file(tmp_path):
    from ledgerflow.mocks.server import MockSystems
    from ledgerflow.platform.durable import FirestoreStore

    from .conftest import AS_OF
    from .test_durable import FakeToken, fake_firestore
    ms = MockSystems(str(tmp_path / "a.db"), AS_OF)
    two = {"name": "Kabir Shah", "phone": "9123400001", "dob": "1990-05-05",
           "loans": [{"product": "HL-FLOAT", "principal": 3000000, "tenure_months": 180, "emis_paid": 10, "rate": 8.9},
                     {"product": "AL-FIX", "principal": 700000, "tenure_months": 48, "emis_paid": 9, "rate": 9.8}],
           "cards": [{"product": "CC-REW", "limit": 100000}]}
    res = await ms.admin.create_customer(two)
    c = res["customer"]
    assert c["id"] == "C1500" and [x["id"] for x in c["loans"]] == ["L-15000", "L-15001"]
    assert [x["id"] for x in c["cards"]] == ["CARD-5500"]
    # a record created in the admin panel whose ID later also appears in customers.yaml leaves the file's customer alone
    fapp, docs, _ = fake_firestore()
    from aiohttp.test_utils import TestServer
    srv = TestServer(fapp)
    await srv.start_server()
    store = FirestoreStore("p", base_url=str(srv.make_url("/v1")), token=FakeToken(), prefix="lf")
    try:
        await store.put("customers", "C1010", {"id": "C1010", "persona": {**two, "id": "C1010"}, "profile": {"city": "X"},
                                               "status": "disabled", "source": "admin"})
        b = MockSystems(str(tmp_path / "b.db"), AS_OF)
        counts = await b.admin.restore(store)
        row = b.q("SELECT name, city, status FROM customers WHERE id='C1010'")[0]
        assert counts["skipped"] == 1 and row == {"name": "Arjun Mehta", "city": "Bengaluru", "status": "active"}
    finally:
        await store.close()
        await srv.close()
