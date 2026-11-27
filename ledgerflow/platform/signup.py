"""Customer self-registration with a one-time code to the mobile number.

Flow: the form is validated (format, age, products) by the systems in a dry run, so the
customer is not asked for a code for an application that would fail; then a 6-digit code
goes to the mobile. Only a correct code creates the customer, so nobody can register a
number they do not hold. Whether a number is already registered is only revealed after
the code, so the form cannot be used to discover which numbers bank with us.

The PoC registers a profile plus demo accounts the customer chooses (a personal loan,
a credit card or both) so the assistant has something to serve; the data is synthetic.

Same code rules as step-up (SPEC-SEC-003): hashed, 5-minute expiry, 3 attempts, at most
3 codes per number per 15 minutes; plus at most 5 applications per client per 15 minutes.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass, field

from ledgerflow.platform import logs
from ledgerflow.platform.telemetry import METRICS

CODE_TTL_S = 300
MAX_ATTEMPTS = 3
MAX_SENDS, SEND_WINDOW_S = 3, 900
MAX_STARTS_PER_CLIENT = 5
TENURES = (12, 24, 36, 48, 60)
CARD_LIMITS = (50000, 100000, 200000, 300000)
DEFAULT_RATE = {"floating": 10.50, "fixed": 11.50}
_log = logs.get("ledgerflow.signup")


@dataclass
class Pending:
    persona: dict
    code_hash: str
    expires: float
    attempts: int = 0


@dataclass
class _Window:
    times: list[float] = field(default_factory=list)

    def hit(self, now: float, limit: int, window: float) -> bool:
        self.times = [t for t in self.times if now - t < window]
        if len(self.times) >= limit:
            return False
        self.times.append(now)
        return True


def _hash(sid: str, code: str) -> str:
    return hashlib.sha256(f"signup:{sid}:{code}".encode()).hexdigest()


def build_persona(form: dict, products: list[dict]) -> tuple[dict | None, list[str]]:
    """The sign-up form as a persona; field errors in the form's own words."""
    errs: list[str] = []
    name = " ".join(str(form.get("name", "")).split())
    phone = re.sub(r"\D", "", str(form.get("phone", "")))[-10:]
    p = {"name": name, "phone": phone, "dob": str(form.get("dob", "")).strip(),
         "city": str(form.get("city", "")).strip()[:40], "segment": form.get("segment") or "salaried", "demo": False}
    if form.get("email"):
        p["email"] = str(form["email"]).strip()
    if form.get("consent") is not True:
        errs.append("consent: please accept the terms to continue")
    by_code = {x["code"]: x for x in products}
    loan, card = form.get("loan"), form.get("card")
    if not loan and not card:
        errs.append("accounts: choose a personal loan, a credit card or both")
    if loan:
        prod = by_code.get(str(loan.get("product", "")))
        if not prod or prod["kind"] != "loan":
            errs.append("loan product: choose one of the loan products")
        try:
            principal, tenure, paid = int(loan.get("principal", 0)), int(loan.get("tenure_months", 0)), \
                int(loan.get("emis_paid", 0))
        except (TypeError, ValueError):
            principal = tenure = paid = 0
        if not 50_000 <= principal <= 4_000_000:
            errs.append("loan amount: between ₹50,000 and ₹40,00,000")
        if tenure not in TENURES:
            errs.append(f"loan tenure: one of {', '.join(map(str, TENURES))} months")
        if tenure and not 1 <= paid < tenure:
            errs.append("EMIs paid: at least 1 and fewer than the tenure")
        if prod and prod["kind"] == "loan":
            p["loan"] = {"product": prod["code"], "principal": principal, "tenure_months": tenure, "emis_paid": paid,
                         "rate": DEFAULT_RATE.get(prod["rate_type"], 11.0)}
    if card:
        prod = by_code.get(str(card.get("product", "")))
        if not prod or prod["kind"] != "card":
            errs.append("card product: choose one of the card products")
        try:
            limit = int(card.get("limit", 0))
        except (TypeError, ValueError):
            limit = 0
        if limit not in CARD_LIMITS:
            errs.append("card limit: choose one of the offered limits")
        if prod and prod["kind"] == "card":
            p["card"] = {"product": prod["code"], "limit": limit}
    return (None if errs else p), errs


class Signup:
    def __init__(self, sender, clock=time.time):
        self.sender, self.clock = sender, clock
        self.pending: dict[str, Pending] = {}
        self.sends: dict[str, _Window] = {}
        self.starts: dict[str, _Window] = {}

    def _gc(self, now: float) -> None:
        for k in [k for k, v in self.pending.items() if v.expires < now]:
            self.pending.pop(k, None)

    def allow_start(self, client: str) -> bool:
        return self.starts.setdefault(client, _Window()).hit(self.clock(), MAX_STARTS_PER_CLIENT, SEND_WINDOW_S)

    async def send_code(self, persona: dict) -> dict:
        now = self.clock()
        self._gc(now)
        if not self.sends.setdefault(persona["phone"], _Window()).hit(now, MAX_SENDS, SEND_WINDOW_S):
            METRICS.incr("signup.code_rate_limited")
            return {"ok": False, "errors": ["Too many codes for this number. Try again in 15 minutes."]}
        sid = secrets.token_urlsafe(18)
        code = f"{secrets.randbelow(1_000_000):06d}"
        self.pending[sid] = Pending(persona, _hash(sid, code), now + CODE_TTL_S)
        await self.sender.send(persona["phone"], f"LedgerFlow: your sign-up code is {code}. It expires in 5 minutes.")
        METRICS.incr("signup.code_sent")
        logs.event(_log, "sign-up code sent", to=logs.mask_phone(persona["phone"]))
        out = {"ok": True, "signup_id": sid, "sent_to": logs.mask_phone(persona["phone"]), "expires_in_s": CODE_TTL_S}
        if getattr(self.sender, "demo", False):
            out["demo_code"] = code          # simulated SMS, shown on the applicant's own screen
        return out

    def check_code(self, sid: str, code: str) -> tuple[dict | None, str]:
        """The persona when the code is right; otherwise an error for the applicant."""
        now = self.clock()
        p = self.pending.get(sid or "")
        if p is None or p.expires < now:
            self.pending.pop(sid or "", None)
            return None, "This code has expired. Start again to get a new one."
        code = re.sub(r"\D", "", str(code or ""))
        if not hmac.compare_digest(p.code_hash, _hash(sid, code)):
            p.attempts += 1
            METRICS.incr("signup.code_wrong")
            if p.attempts >= MAX_ATTEMPTS:
                self.pending.pop(sid, None)
                return None, "Too many wrong codes. Start again to get a new one."
            return None, f"That code is not right. {MAX_ATTEMPTS - p.attempts} attempt(s) left."
        self.pending.pop(sid, None)
        return p.persona, ""
