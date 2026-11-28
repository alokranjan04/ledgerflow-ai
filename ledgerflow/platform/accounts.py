"""Which account a question is about, when a customer holds several (home loan, car loan,
two cards). Shared by every workflow.

Rule: never guess. A tool resolves the account from, in order:
  1. what the customer or model named: an ID (L-10011, CARD-5011), the last digits (ending 7781),
     or a product word ("car", "home loan", "rewards card");
  2. the only account of that kind;
  3. the account this conversation was already about (so "and the next EMI?" stays on the car loan);
otherwise it raises NeedsChoice, and the assistant asks "your home loan or your car loan?",
naming each one. Every answer carries the account it is about.
"""
from __future__ import annotations

import re

# words a customer uses -> the words in product names they mean
SYNONYMS = {"car": "car", "auto": "car", "vehicle": "car", "gaadi": "car", "गाड़ी": "car", "कार": "car",
            "home": "home", "house": "home", "housing": "home", "ghar": "home", "घर": "home", "होम": "home",
            "personal": "personal", "पर्सनल": "personal"}


# words that never pick an account on their own
STOP = {"loan", "card", "my", "the", "credit", "account", "mera", "meri", "मेरा", "मेरी", "लोन", "कार्ड", "wala", "wali",
        "one", "ending", "what", "s", "is", "on", "of", "a", "an", "for", "and", "due", "emi", "next", "minimum",
        "statement", "balance", "me", "i", "when", "how", "much", "can", "do", "does", "it", "this", "that"}


class NeedsChoice(Exception):
    """More than one account fits; the customer must say which."""

    def __init__(self, kind: str, options: list[dict], unmatched: str = ""):
        super().__init__(f"{len(options)} {kind}s")
        self.kind, self.options, self.unmatched = kind, options, unmatched

    def result(self) -> dict:
        names = ", ".join(o["label"] for o in self.options)
        what = f"Nothing matched '{self.unmatched}'. " if self.unmatched else ""
        return {"ok": False, "needs_choice": True, "account_type": self.kind, "options": self.options,
                "error": f"{what}The customer has {len(self.options)} {self.kind}s: {names}. Ask which one they mean "
                         f"(name each), then call again with {self.kind}_id. Do not guess."}


def _words(text: str) -> set[str]:
    return {SYNONYMS.get(w, w) for w in re.findall(r"[\wऀ-ॿ]+", (text or "").lower())}


def match(accounts: list[dict], hint: str) -> list[dict]:
    """Accounts the hint names: by ID, by trailing digits (3+), or by product words."""
    h = (hint or "").strip()
    if not h:
        return []
    up = h.upper()
    exact = [a for a in accounts if a["id"].upper() == up]
    if exact:
        return exact
    digits = re.findall(r"\d{3,}", h)
    if digits:
        by_digits = [a for a in accounts if any(a["id"].endswith(d) or a.get("last4", "") == d[-4:] for d in digits)]
        if by_digits:
            return by_digits
    want = _words(h) - STOP
    if not want:
        return []
    return [a for a in accounts if want & _words(a["label"])]


SOFT = "~"     # a hint taken from the customer's whole message: use it only if it names an account


def pick(memory: dict, kind: str, accounts: list[dict], hint: str = "") -> dict:
    """The account to use; remembers it for follow-ups. accounts: [{"id", "label", ...}]
    A hint starting with SOFT is the customer's whole question (fast lane): it counts only when it
    names exactly one account, otherwise the normal rules apply (only account, then the remembered one)."""
    if not accounts:
        raise ValueError(f"The customer has no active {kind} with us.")
    key = f"current_{kind}"
    if hint and hint.startswith(SOFT):
        found = match(accounts, hint[1:])
        hint = found[0]["id"] if len(found) == 1 else ""
    if hint and hint.strip():
        found = match(accounts, hint)
        if len(found) == 1:
            memory[key] = found[0]["id"]
            return found[0]
        if len(accounts) > 1:
            raise NeedsChoice(kind, [{"id": a["id"], "label": a["label"]} for a in (found or accounts)],
                              "" if found else hint)
        # one account: a vague or mistaken word ("my loan") still means that account
    if len(accounts) == 1:
        memory[key] = accounts[0]["id"]
        return accounts[0]
    current = next((a for a in accounts if a["id"] == memory.get(key)), None)
    if current:
        return current
    raise NeedsChoice(kind, [{"id": a["id"], "label": a["label"]} for a in accounts])


def soft_hint(text: str) -> str:
    """The customer's message as a soft hint (see pick): matched against their own account names,
    so any product word works ("rewards card", "platinum"), not only the words listed here."""
    return SOFT + (text or "") if text else ""


def hint_from_text(text: str) -> str:
    """The part of a customer's message that names an account (IDs, last digits, loan words)."""
    t = (text or "").lower()
    m = re.search(r"\b(?:l-\d{4,6}|card-\d{4})\b", t)
    if m:
        return m.group(0)
    m = re.search(r"(?:ending|ending in|last four|ends with)\s*(\d{4})", t)
    if m:
        return m.group(1)
    found = [w for w in re.findall(r"[\wऀ-ॿ]+", t) if w in SYNONYMS]
    return " ".join(found)
