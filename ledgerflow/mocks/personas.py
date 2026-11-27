"""Demo customer personas from data/customers.yaml: load, validate, and expose the
sign-in list. Validation reports every problem at once, by customer and field,
so a non-developer editing the file gets a readable list instead of a stack trace."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

PRODUCTS = {"PL-FLOAT", "PL-FIX"}
FAILURE_REASONS = {"INSUFFICIENT_FUNDS", "MANDATE_CANCELLED", "BANK_TECHNICAL", "ACCOUNT_FROZEN"}
ID_RE = {"customer": re.compile(r"^C\d{4}$"), "loan": re.compile(r"^L-\d{5}$"), "card": re.compile(r"^CARD-\d{4}$")}


class PersonaError(ValueError):
    pass


def load_personas(path: Path) -> list[dict]:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise PersonaError(f"{path}: not valid YAML: {e}") from None
    customers = raw.get("customers")
    if not isinstance(customers, list) or not customers:
        raise PersonaError(f"{path}: needs a non-empty 'customers:' list")
    errors = validate(customers)
    if errors:
        raise PersonaError(f"{path} has {len(errors)} problem(s):\n  - " + "\n  - ".join(errors))
    return customers


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(customers: list[dict]) -> list[str]:
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
        if not c.get("loan") and not c.get("card"):
            errs.append(f"{who}: needs a 'loan', a 'card' or both")
        loan = c.get("loan")
        if loan:
            lw = f"{who} loan"
            if not ID_RE["loan"].match(str(loan.get("id", ""))):
                errs.append(f"{lw}: id must look like L-12345")
            elif str(loan["id"]).startswith("L-2"):
                errs.append(f"{lw}: ids L-20000-L-29999 are reserved for generated loans")
            elif loan["id"] in seen["loan"]:
                errs.append(f"{lw}: id {loan['id']} is used twice")
            seen["loan"].add(loan.get("id"))
            if loan.get("product") not in PRODUCTS:
                errs.append(f"{lw}: product must be one of {sorted(PRODUCTS)}")
            for f, lo in (("principal", 10_000), ("rate", 1), ("tenure_months", 3), ("emis_paid", 1)):
                if not _num(loan.get(f)) or loan[f] < lo:
                    errs.append(f"{lw}: '{f}' must be a number >= {lo}")
            if _num(loan.get("emis_paid")) and _num(loan.get("tenure_months")) and loan["emis_paid"] >= loan["tenure_months"]:
                errs.append(f"{lw}: emis_paid must be less than tenure_months (the loan must still be running)")
            resets = loan.get("rate_resets") or []
            if resets and loan.get("product") != "PL-FLOAT":
                errs.append(f"{lw}: rate_resets apply only to PL-FLOAT loans")
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
        card = c.get("card")
        if card:
            cw = f"{who} card"
            if not ID_RE["card"].match(str(card.get("id", ""))):
                errs.append(f"{cw}: id must look like CARD-1234")
            elif str(card["id"]).startswith("CARD-6"):
                errs.append(f"{cw}: ids CARD-6000-CARD-6999 are reserved for generated cards")
            elif card["id"] in seen["card"]:
                errs.append(f"{cw}: id {card['id']} is used twice")
            seen["card"].add(card.get("id"))
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
