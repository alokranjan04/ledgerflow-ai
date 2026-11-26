"""Grounding verifier: every material number in an answer must trace back to
a tool result (or the customer's own words), allowing for rounding and simple
derivations (sum / difference of two tool values).

"Material" = money (₹, Rs, rupees, lakh, crore), percentages, counts of
months/EMIs/days, and any number >= 100. Date parts (days, years) are ignored.
Handles Indian grouping (1,23,456), lakh/crore/k suffixes and spoken English
number words, because voice transcripts arrive as words.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from itertools import combinations

UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
SCALES = {"hundred": 100, "thousand": 1_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000,
          "crore": 10_000_000, "crores": 10_000_000, "million": 1_000_000}
MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
          "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")

NUM_RE = re.compile(
    r"(?P<cur>₹|rs\.?|inr)?\s*(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<suf>lakhs?|lacs?|crores?|k\b|%|percent|per cent)?",
    re.IGNORECASE)
_WORDS = "|".join([*UNITS, *TENS, *SCALES, "and", "point"])
WORD_SEQ_RE = re.compile(rf"\b(?:(?:{_WORDS})[\s-]*)+", re.IGNORECASE)
DATE_RE = re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{MONTHS})\b(?:,?\s+\d{{4}})?|"
                     rf"\b(?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?:,?\s+\d{{4}})?|\b\d{{4}}-\d{{2}}-\d{{2}}\b|"
                     rf"\b(?:{MONTHS})\s+\d{{4}}\b", re.IGNORECASE)
ID_RE = re.compile(r"\b[A-Z]{1,5}-[A-Z0-9-]+\b")
COUNT_UNIT_RE = re.compile(r"^\s*(months?|emis?|instal(?:l)?ments?|days?|years?|payments?|debits?)\b", re.IGNORECASE)


def words_to_number(text: str) -> Decimal | None:
    tokens = [t for t in re.split(r"[\s-]+", text.lower().strip()) if t and t != "and"]
    if not any(t in UNITS or t in TENS for t in tokens):   # "lakh" alone (as in "0.21 lakh") is not a number
        return None
    total, current, frac_digits, in_frac = Decimal(0), Decimal(0), [], False
    for t in tokens:
        if t == "point":
            in_frac = True
            continue
        if in_frac:
            if t in UNITS and UNITS[t] < 10:
                frac_digits.append(str(UNITS[t]))
            continue
        if t in UNITS:
            current += UNITS[t]
        elif t in TENS:
            current += TENS[t]
        elif t == "hundred":
            current = (current or 1) * 100
        elif t in SCALES:
            total += (current or 1) * SCALES[t]
            current = Decimal(0)
        else:
            return None
    value = total + current
    if frac_digits:
        value += Decimal("0." + "".join(frac_digits))
    return value


def _to_decimal(num: str, suf: str | None) -> Decimal | None:
    try:
        v = Decimal(num.replace(",", ""))
    except InvalidOperation:
        return None
    s = (suf or "").lower()
    if s.startswith("lakh") or s.startswith("lac"):
        v *= 100_000
    elif s.startswith("crore"):
        v *= 10_000_000
    elif s == "k":
        v *= 1000
    return v


def extract_numbers(text: str, material_only: bool = True) -> list[tuple[Decimal, str]]:
    """Return (value, snippet) for each number in free text."""
    text = DATE_RE.sub(" ", text or "")
    text = ID_RE.sub(" ", text)
    out = []
    for m in NUM_RE.finditer(text):
        v = _to_decimal(m.group("num"), m.group("suf"))
        if v is None:
            continue
        after = text[m.end():m.end() + 20]
        is_money = bool(m.group("cur")) or bool(re.match(r"\s*(rupees|rs\b)", after, re.I)) or \
            (m.group("suf") or "").lower().startswith(("lakh", "lac", "crore", "k"))
        is_pct = (m.group("suf") or "").lower() in ("%", "percent", "per cent")
        is_count = bool(COUNT_UNIT_RE.match(after))
        is_year = not is_money and not is_pct and v == v.to_integral() and 1900 <= v <= 2100
        if is_year:
            continue
        if material_only and not (is_money or is_pct or is_count or v >= 100):
            continue
        out.append((v, m.group(0).strip()))
    for m in WORD_SEQ_RE.finditer(text):
        v = words_to_number(m.group(0))
        if v is None:
            continue
        after = text[m.end():m.end() + 20]
        is_money = bool(re.match(r"\s*(rupees|rs\b)", after, re.I))
        is_pct = bool(re.match(r"\s*(percent|per cent|%)", after, re.I))
        is_count = bool(COUNT_UNIT_RE.match(after))
        if material_only and not (is_money or is_pct or is_count or v >= 100):
            continue
        out.append((v, m.group(0).strip()))
    return out


def numbers_in_data(obj) -> set[Decimal]:
    """All numbers appearing anywhere in tool results (values and strings)."""
    found: set[Decimal] = set()
    blob = json.dumps(obj, ensure_ascii=False, default=str)
    for m in NUM_RE.finditer(blob):
        v = _to_decimal(m.group("num"), m.group("suf"))
        if v is not None:
            found.add(v)
    return found


class Grounder:
    def __init__(self, tolerance_pct: Decimal = Decimal("0.6"), abs_tol: Decimal = Decimal("1")):
        self.tolerance_pct = tolerance_pct
        self.abs_tol = abs_tol

    def allowed(self, tool_results: list, user_texts: list[str]) -> set[Decimal]:
        base = set()
        for r in tool_results:
            base |= numbers_in_data(r)
        for t in user_texts:
            base |= {v for v, _ in extract_numbers(t, material_only=False)}
        big = sorted(v for v in base if abs(v) >= 1)[:150]
        derived = set()
        for a, b in combinations(big, 2):
            derived.add(a + b)
            derived.add(abs(a - b))
        return base | derived

    def _matches(self, v: Decimal, allowed: set[Decimal]) -> bool:
        if v in allowed:
            return True
        tol = max(self.abs_tol, abs(v) * self.tolerance_pct / 100)
        return any(abs(v - a) <= tol for a in allowed)

    def check(self, reply: str, tool_results: list, user_texts: list[str]) -> list[str]:
        """Return the snippets of unsupported numbers (empty = grounded)."""
        allowed = self.allowed(tool_results, user_texts)
        return [snip for v, snip in extract_numbers(reply) if not self._matches(v, allowed)]
