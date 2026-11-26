"""Step-up verification: a one-time code to the registered mobile before any write.

Phone + date of birth is knowledge-based and weak; anything that changes an
account (dispute, statement, prepayment, service request) additionally needs
possession of the registered phone. A verified step-up stays valid for a short
window, so several requests in one conversation need one code.

Security properties:
- 6-digit code from `secrets`, stored only as a salted hash on the session;
- expires after 5 minutes; 3 wrong attempts burn it; at most 3 codes per 15 minutes;
- the code is NEVER part of a tool result the model sees, nor of any log line.

Delivery is pluggable (`SmsSender`). `DemoSms` is the PoC inbox: the code shows on
the customer's own screen as a clearly labelled simulated SMS. A production
deployment plugs an SMS / WhatsApp gateway into the same interface.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Protocol

from ledgerflow.platform import logs
from ledgerflow.platform.telemetry import METRICS

CODE_TTL_S = 300
MAX_ATTEMPTS = 3
MAX_SENDS, SEND_WINDOW_S = 3, 900

_log = logs.get("ledgerflow.stepup")


class SmsSender(Protocol):
    demo: bool

    async def send(self, phone: str, text: str) -> None: ...


class DemoSms:
    """Simulated SMS for the PoC: the code is handed back to the customer's own
    screen (never to the model or the logs). Replace with a real gateway."""

    demo = True

    async def send(self, phone: str, text: str) -> None:
        logs.event(_log, "sms (demo) delivered", to=logs.mask_phone(phone))


def _hash(session_id: str, code: str) -> str:
    return hashlib.sha256(f"{session_id}:{code}".encode()).hexdigest()


class StepUp:
    def __init__(self, sender: SmsSender, valid_secs: int = 600, clock=time.time):
        self.sender, self.valid_secs, self.clock = sender, valid_secs, clock

    def satisfied(self, session) -> bool:
        at = session.memory.get("step_up_at", 0)
        return bool(at) and self.clock() - at < self.valid_secs

    async def challenge(self, session) -> dict:
        """Send a code. Returns what the model may see, plus `_demo_code` that the
        caller must move to the customer's screen and strip from the model's view."""
        now = self.clock()
        if not session.phone:
            return {"ok": False, "error": "No registered mobile on this session; offer a human agent instead."}
        sends = [t for t in session.memory.get("otp_sends", []) if now - t < SEND_WINDOW_S]
        if len(sends) >= MAX_SENDS:
            METRICS.incr("stepup.send_limited")
            return {"ok": False, "step_up_required": True,
                    "error": "Too many codes requested. Ask the customer to try again in 15 minutes, or offer a human agent."}
        code = f"{secrets.randbelow(1_000_000):06d}"
        session.memory["otp"] = {"hash": _hash(session.id, code), "expires": now + CODE_TTL_S, "attempts": 0}
        session.memory["otp_sends"] = sends + [now]
        await self.sender.send(session.phone, f"LedgerFlow code {code}. Valid 5 minutes. Never share it with anyone.")
        METRICS.incr("stepup.sent")
        out = {"ok": False, "step_up_required": True, "sent_to": logs.mask_phone(session.phone),
               "next_step": "A 6-digit code was sent to the customer's registered mobile. Ask them to read it "
                            "out or type it, call verify_otp with it, then call confirm_action again."}
        if getattr(self.sender, "demo", False):
            out["_demo_code"] = code
        return out

    def verify(self, session, code: str) -> dict:
        now = self.clock()
        otp = session.memory.get("otp")
        if not otp:
            return {"ok": False, "error": "No code is pending. Confirm the request again to receive a code."}
        if now > otp["expires"]:
            session.memory.pop("otp", None)
            METRICS.incr("stepup.expired")
            return {"ok": False, "error": "The code expired. Confirm the request again to receive a new code."}
        digits = "".join(ch for ch in str(code) if ch.isdigit())
        if hmac.compare_digest(_hash(session.id, digits), otp["hash"]):
            session.memory.pop("otp", None)
            session.memory["step_up_at"] = now
            METRICS.incr("stepup.verified")
            return {"ok": True, "verified": True, "valid_minutes": self.valid_secs // 60}
        otp["attempts"] += 1
        METRICS.incr("stepup.failed")
        left = MAX_ATTEMPTS - otp["attempts"]
        if left <= 0:
            session.memory.pop("otp", None)
            return {"ok": False, "locked": True,
                    "error": "Too many wrong codes. The code is cancelled; offer to send a new one or a human agent."}
        return {"ok": False, "attempts_left": left, "error": "That code did not match."}
