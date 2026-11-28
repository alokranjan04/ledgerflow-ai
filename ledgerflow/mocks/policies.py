"""Product policies from data/policies.yaml: load, validate and normalise.

The file is edited by people who are not developers, so validation reports every
problem at once, by product, rule and field, and refuses anything the calculators
would not understand (an unknown rule or field is almost always a typo, and a rule
nobody reads would silently change nothing). Amounts and percentages come back as
strings, the form the Decimal finance code reads exactly.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from ledgerflow.config import ROOT, env

POLICIES_FILE = ROOT / env("POLICIES_FILE", "data/policies.yaml")

# field type per rule, per product kind. int: whole number >= 0; money: rupees >= 0;
# pct: 0-100; days: list of month days 1-28; clause: an ID in the product's clauses.
RULES = {
    "loan": {
        "part_prepayment": {"min_emis_paid": "int", "min_amount": "money", "max_pct_outstanding_per_fy": "pct",
                            "charge_pct": "pct", "clause": "clause"},
        "bounce_charge": {"amount": "money", "gst": "bool", "clause": "clause"},
        "emi_date_change": {"allowed_days": "days", "max_per_year": "int", "clause": "clause"},
        "rate_reset": {"benchmark": "text", "frequency": "text", "clause": "clause"},
    },
    "card": {
        "min_due": {"pct": "pct", "floor": "money", "clause": "clause"},
        "finance_charge": {"monthly_pct": "pct", "clause": "clause"},
        "dispute": {"window_days": "int", "temporary_credit_working_days": "int", "clause": "clause"},
    },
}
# Foreclosure has two shapes: allowed after N EMIs with one charge, or a lock-in then a charge by loan age.
FORECLOSURE = {
    "open": {"min_emis_paid": "int", "charge_pct": "pct", "clause": "clause"},
    "lock_in": {"lock_in_emis": "int", "charge_pct_until_24m": "pct", "charge_pct_after_24m": "pct",
                "clause_lock_in": "clause", "clause": "clause"},
}
REQUIRED = {"loan": {"foreclosure", "part_prepayment", "bounce_charge", "emi_date_change"},
            "card": {"min_due", "finance_charge", "dispute"}}
CODE_RE = re.compile(r"^[A-Z0-9]+(-[A-Z0-9]+)*$")
CLAUSE_RE = re.compile(r"^[A-Z0-9]+(-[A-Z0-9.]+)+$")


class PolicyError(ValueError):
    pass


def load_policies(path: Path | str | None = None) -> dict[str, dict]:
    path = path or POLICIES_FILE            # read at call time, so tests and POLICIES_FILE can point elsewhere
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise PolicyError(f"{path}: not valid YAML: {e}") from None
    products = raw.get("products")
    if not isinstance(products, dict) or not products:
        raise PolicyError(f"{path}: needs a non-empty 'products:' mapping")
    errors, out = validate(products)
    if errors:
        raise PolicyError(f"{path} has {len(errors)} problem(s):\n  - " + "\n  - ".join(errors))
    return out


def loan_products(products: dict) -> set[str]:
    return {c for c, p in products.items() if p["kind"] == "loan"}


def card_products(products: dict) -> list[str]:
    return [c for c, p in products.items() if p["kind"] == "card"]


def _value(kind: str, v, clauses: dict) -> tuple[object, str | None]:
    """Normalised value and an error message (None if valid)."""
    if kind == "int":
        ok = isinstance(v, int) and not isinstance(v, bool) and v >= 0
        return v, None if ok else "must be a whole number, 0 or more"
    if kind in ("money", "pct"):
        if isinstance(v, bool):
            return v, "must be a number"
        try:
            d = Decimal(str(v))
        except (InvalidOperation, ValueError):
            return v, "must be a number"
        if d < 0 or (kind == "pct" and d > 100):
            return v, "must be between 0 and 100" if kind == "pct" else "must be 0 or more"
        return format(d.normalize(), "f"), None
    if kind == "bool":
        return v, None if isinstance(v, bool) else "must be true or false"
    if kind == "text":
        return v, None if isinstance(v, str) and v.strip() else "must be non-empty text"
    if kind == "days":
        ok = isinstance(v, list) and v and all(isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= 28 for x in v)
        return v, None if ok else "must be a list of days of the month between 1 and 28"
    if kind == "clause":
        return v, None if v in clauses else f"names clause {v!r}, which is not in this product's clauses"
    raise AssertionError(kind)


def validate(products: dict) -> tuple[list[str], dict]:
    errs: list[str] = []
    out: dict[str, dict] = {}
    for code, p in products.items():
        who = f"product {code}"
        if not CODE_RE.match(str(code)):
            errs.append(f"{who}: code must be capital letters, digits and dashes (e.g. PL-FLOAT)")
        if not isinstance(p, dict):
            errs.append(f"{who}: must be a block with name, kind and policy")
            continue
        kind = p.get("kind")
        if kind not in RULES:
            errs.append(f"{who}: kind must be 'loan' or 'card'")
            continue
        if not str(p.get("name", "")).strip():
            errs.append(f"{who}: needs a name")
        rate_type = p.get("rate_type") if kind == "loan" else "n/a"
        if kind == "loan" and rate_type not in ("floating", "fixed"):
            errs.append(f"{who}: rate_type must be 'floating' or 'fixed'")
        if kind == "card" and not str(p.get("short_name", "")).strip():
            errs.append(f"{who}: card products need a short_name, e.g. 'Platinum card'")
        pol = p.get("policy") or {}
        if not str(pol.get("version", "")).strip():
            errs.append(f"{who}: policy needs a version, e.g. \"2026.10\"")
        eff = pol.get("effective")
        try:
            eff = eff.isoformat() if isinstance(eff, date) else date.fromisoformat(str(eff)).isoformat()
        except ValueError:
            errs.append(f"{who}: policy effective date must be YYYY-MM-DD")
        clauses = pol.get("clauses") or {}
        if not isinstance(clauses, dict) or not clauses:
            errs.append(f"{who}: policy needs clauses (ID: text)")
            clauses = {}
        for cid, text in clauses.items():
            if not CLAUSE_RE.match(str(cid)):
                errs.append(f"{who}: clause ID {cid!r} should look like PL-FL-5.1")
            if not isinstance(text, str) or not text.strip():
                errs.append(f"{who}: clause {cid} has no text")
        rules = pol.get("rules") or {}
        if not isinstance(rules, dict):
            errs.append(f"{who}: policy rules must be a mapping")
            rules = {}
        needed = REQUIRED[kind] | ({"rate_reset"} if rate_type == "floating" else set())
        for missing in sorted(needed - set(rules)):
            errs.append(f"{who}: missing rule '{missing}'")
        known = set(RULES[kind]) | ({"foreclosure"} if kind == "loan" else set())
        norm_rules = {}
        for name, rule in rules.items():
            rw = f"{who} rule {name}"
            if name not in known:
                errs.append(f"{rw}: no calculation uses this rule; {kind} rules are {sorted(known)}")
                continue
            if not isinstance(rule, dict):
                errs.append(f"{rw}: must be a block of fields")
                continue
            if name == "rate_reset" and rate_type != "floating":
                errs.append(f"{rw}: only floating-rate loans have rate resets")
            schema = (FORECLOSURE["lock_in" if "lock_in_emis" in rule else "open"] if name == "foreclosure"
                      else RULES[kind][name])
            for f in sorted(set(rule) - set(schema)):
                errs.append(f"{rw}: unknown field '{f}'; expected {sorted(schema)}")
            norm = {}
            for f, t in schema.items():
                if f not in rule:
                    errs.append(f"{rw}: missing field '{f}'")
                    continue
                v, problem = _value(t, rule[f], clauses)
                if problem:
                    errs.append(f"{rw}: {f} {problem}")
                norm[f] = v
            norm_rules[name] = norm
        if "short_name" in p and not str(p.get("short_name") or "").strip():
            errs.append(f"{who}: short_name, when given, must not be empty")
        out[code] = {"name": p.get("name"), "kind": kind, "rate_type": rate_type,
                     **({"short_name": p["short_name"]} if p.get("short_name") else {}),
                     "policy": {"version": str(pol.get("version", "")), "effective": eff,
                                "rules": norm_rules, "clauses": clauses}}
    if not any(p.get("kind") == "loan" for p in products.values() if isinstance(p, dict)):
        errs.append("at least one loan product is needed")
    return errs, out
