"""Demo customer personas from data/customers.yaml: load, validate, and expose the
sign-in list. Validation reports every problem at once, by customer and field,
so a non-developer editing the file gets a readable list instead of a stack trace."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

from ledgerflow.mocks.policies import card_products, load_policies, loan_products

FAILURE_REASONS = {"INSUFFICIENT_FUNDS", "MANDATE_CANCELLED", "BANK_TECHNICAL", "ACCOUNT_FROZEN"}
ID_RE = {"customer": re.compile(r"^C\d{4}$"), "loan": re.compile(r"^L-\d{5}$"), "card": re.compile(r"^CARD-\d{4}$")}


class PersonaError(ValueError):
    pass


def loans_of(c: dict) -> list[dict]:
    """A customer's loans: `loans:` (a list) or the shorthand `loan:` (one)."""
    return list(c.get("loans") or ([c["loan"]] if c.get("loan") else []))


def cards_of(c: dict) -> list[dict]:
    return list(c.get("cards") or ([c["card"]] if c.get("card") else []))


FILE_ID_LIMIT = 1500    # C1001-C1499 for data/customers.yaml; C1500+ are created in the admin panel or by sign-up


def load_personas(path: Path, products: dict | None = None) -> list[dict]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise PersonaError(f"{path}: not valid YAML: {e}") from None
    customers = raw.get("customers")
    if not isinstance(customers, list) or not customers:
        raise PersonaError(f"{path}: needs a non-empty 'customers:' list")
    errors = validate(customers, products)
    for c in customers:
        cid = str(c.get("id", "")) if isinstance(c, dict) else ""
        if re.fullmatch(r"C1\d{3}", cid) and int(cid[1:]) >= FILE_ID_LIMIT:
            errors.append(f"customer {cid}: ids C1500-C1999 are kept for customers created in the admin panel "
                          "or by sign-up; use C1001-C1499 in this file")
    if errors:
        raise PersonaError(f"{path} has {len(errors)} problem(s):\n  - " + "\n  - ".join(errors))
    return customers


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(customers: list[dict], products: dict | None = None) -> list[str]:
    """products: the loaded policies (data/policies.yaml by default); loans and cards must use them."""
    products = products if products is not None else load_policies()
    loans_ok, cards_ok = loan_products(products), card_products(products)
    errs: list[str] = []
    seen = {"id": set(), "phone": set(), "loan": set(), "card": set()}
    for i, c in enumerate(customers):
        who = f"customer #{i + 1} ({c.get('name') or c.get('id') or '?'})"
        if not isinstance(c, dict):
            errs.append(f"{who}: must be a mapping")
            continue
        for field in ("id", "name", "phone", "dob"):
            if not c.get(field):
                errs.append(f"{who}: '{field}' is required")
        cid, phone = str(c.get("id", "")), str(c.get("phone", ""))
        if cid and not ID_RE["customer"].match(cid):
            errs.append(f"{who}: id '{cid}' must look like C1234")
        if cid and cid.startswith("C2"):
            errs.append(f"{who}: ids C2000-C2999 are reserved for the generated background customers")
        if phone and not re.fullmatch(r"\d{10}", phone):
            errs.append(f"{who}: phone must be 10 digits in quotes, e.g. \"9800000005\"")
        try:
            date.fromisoformat(str(c.get("dob", "")))
        except ValueError:
            errs.append(f"{who}: dob must be YYYY-MM-DD in quotes")
        for key, val in (("id", cid), ("phone", phone)):
            if val and val in seen[key]:
                errs.append(f"{who}: {key} {val} is used twice")
            seen[key].add(val)
        if c.get("demo") and not c.get("story"):
            errs.append(f"{who}: demo customers need a one-line 'story' for the sign-in screen")
        if not loans_of(c) and not cards_of(c):
            errs.append(f"{who}: needs at least one loan or card ('loan'/'loans', 'card'/'cards')")
        for one, many in (("loan", "loans"), ("card", "cards")):
            if one in c and many in c:
                errs.append(f"{who}: use either '{one}' or '{many}', not both")
            if c.get(many) is not None and not isinstance(c.get(many), list):
                errs.append(f"{who}: '{many}' must be a list")
        for loan in loans_of(c) if isinstance(c.get("loans", []), list) else []:
            lw = f"{who} loan"
            if not ID_RE["loan"].match(str(loan.get("id", ""))):
                errs.append(f"{lw}: id must look like L-12345")
            elif str(loan["id"]).startswith("L-2"):
                errs.append(f"{lw}: ids L-20000-L-29999 are reserved for generated loans")
            elif loan["id"] in seen["loan"]:
                errs.append(f"{lw}: id {loan['id']} is used twice")
            seen["loan"].add(loan.get("id"))
            if loan.get("product") not in loans_ok:
                errs.append(f"{lw}: product must be a loan product in data/policies.yaml: {sorted(loans_ok)}")
            for f, lo in (("principal", 10_000), ("rate", 1), ("tenure_months", 3), ("emis_paid", 1)):
                if not _num(loan.get(f)) or loan[f] < lo:
                    errs.append(f"{lw}: '{f}' must be a number >= {lo}")
            if _num(loan.get("emis_paid")) and _num(loan.get("tenure_months")) and loan["emis_paid"] >= loan["tenure_months"]:
                errs.append(f"{lw}: emis_paid must be less than tenure_months (the loan must still be running)")
            resets = loan.get("rate_resets") or []
            if resets and loan.get("product") in loans_ok and products[loan["product"]]["rate_type"] != "floating":
                errs.append(f"{lw}: rate_resets apply only to floating-rate loans")
            for r in resets:
                if not (_num(r.get("months_ago")) and _num(r.get("rate"))):
                    errs.append(f"{lw}: each rate reset needs months_ago and rate")
                elif _num(loan.get("emis_paid")) and r["months_ago"] >= loan["emis_paid"]:
                    errs.append(f"{lw}: a rate reset {r['months_ago']} months ago is before the first EMI")
            for f in loan.get("failed_payments") or []:
                if f.get("reason") not in FAILURE_REASONS:
                    errs.append(f"{lw}: failed payment reason must be one of {sorted(FAILURE_REASONS)}")
                if not _num(f.get("emis_ago")) or (_num(loan.get("emis_paid")) and f["emis_ago"] >= loan["emis_paid"]):
                    errs.append(f"{lw}: failed payment emis_ago must be 0 .. emis_paid-1")
        for card in cards_of(c) if isinstance(c.get("cards", []), list) else []:
            cw = f"{who} card"
            if not ID_RE["card"].match(str(card.get("id", ""))):
                errs.append(f"{cw}: id must look like CARD-1234")
            elif str(card["id"]).startswith("CARD-6"):
                errs.append(f"{cw}: ids CARD-6000-CARD-6999 are reserved for generated cards")
            elif card["id"] in seen["card"]:
                errs.append(f"{cw}: id {card['id']} is used twice")
            seen["card"].add(card.get("id"))
            if card.get("product") is not None and card["product"] not in cards_ok:
                errs.append(f"{cw}: product must be a card product in data/policies.yaml: {cards_ok}")
            elif not cards_ok:
                errs.append(f"{cw}: data/policies.yaml has no card product")
            if not re.fullmatch(r"\d{4}", str(card.get("last4", ""))):
                errs.append(f"{cw}: last4 must be 4 digits in quotes")
            if not _num(card.get("limit")) or card["limit"] < 10_000:
                errs.append(f"{cw}: limit must be a number >= 10000")
            dup = card.get("duplicate_charge")
            if dup and not (dup.get("merchant") and _num(dup.get("amount"))):
                errs.append(f"{cw}: duplicate_charge needs merchant and amount")
    return errs


def demo_customers(customers: list[dict]) -> list[dict]:
    """The sign-in screen's list (synthetic data, shown on purpose)."""
    return [{"name": c["name"], "phone": str(c["phone"]), "dob": str(c["dob"]), "story": c.get("story", "")}
            for c in customers if c.get("demo")]
