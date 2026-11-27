"""Plain-language agent steps for non-engineers: what the assistant planned, which
systems it checked and what they returned, which guardrails applied, and how the
answer was verified. Built only from tool results and runtime facts (never from
model text), so the explanation itself is grounded."""
from __future__ import annotations

# tool -> (what it does in plain words, systems it touches)
TOOLS = {
    "get_loan_summary": ("Looked up your loan position", "Loan Management"),
    "explain_emi_change": ("Compared your EMIs and the rate history", "Loan Management + Policy"),
    "get_payments": ("Pulled your payment history", "Payments"),
    "get_last_payment": ("Checked your latest payment", "Payments"),
    "simulate_prepayment": ("Ran the prepayment calculator under the product rules", "Finance engine + Policy"),
    "check_foreclosure": ("Applied the foreclosure rules and priced a payoff", "Finance engine + Policy"),
    "get_servicing_options": ("Checked every option on your loan", "Loans + Payments + Policy"),
    "search_policy": ("Searched the policy clauses", "Policy Knowledge"),
    "list_service_requests": ("Listed your service requests", "Service Operations"),
    "propose_payment_dispute": ("Prepared a payment dispute for your approval", "Payments"),
    "propose_foreclosure_statement": ("Prepared a foreclosure statement request for your approval", "Loan Management"),
    "propose_prepayment_request": ("Prepared a prepayment request for your approval", "Loan Management + Policy"),
    "propose_service_request": ("Prepared a service request for your approval", "Service Operations"),
    "propose_card_dispute": ("Prepared a card dispute for your approval", "Cards + Policy"),
    "confirm_action": ("Submitted the approved request", "Service Operations"),
    "cancel_action": ("Cancelled the request", "Service Operations"),
    "verify_otp": ("Checked your one-time code", "Security"),
    "verify_identity": ("Verified your identity", "CRM"),
    "request_human_agent": ("Handed you to a human agent with the conversation", "Service Operations"),
    "get_card_summary": ("Looked up your card statement", "Cards + Policy"),
    "get_card_transactions": ("Pulled your card transactions", "Cards"),
}


def planned(names: list[str]) -> dict:
    labels = [TOOLS.get(n, (n, ""))[0] for n in names]
    together = " (in parallel)" if len(names) > 1 else ""
    return {"kind": "plan", "text": f"Planned {len(names)} step{'s' if len(names) > 1 else ''}{together}",
            "items": labels}


def tool_step(name: str, data: dict, ms: float) -> dict:
    label, systems = TOOLS.get(name, (name.replace("_", " "), ""))
    ok = bool(data.get("ok", True))
    if not ok and data.get("unavailable"):
        return {"kind": "tool", "ok": False, "text": label, "systems": systems, "ms": round(ms),
                "result": f"{data['unavailable']} did not respond, so this part is left out and named in the answer"}
    return {"kind": "tool", "ok": ok and not data.get("step_up_required"), "text": label, "systems": systems,
            "ms": round(ms), "result": summarize(name, data)}


def summarize(name: str, d: dict) -> str:
    """One line of what the systems returned; values copied from the tool result."""
    try:
        if d.get("step_up_required"):
            return (f"Paused for security: a one-time code went to the mobile ending {d['sent_to'][-4:]}"
                    if d.get("sent_to") else d.get("error", "Verification needed"))
        if not d.get("ok", True):
            return d.get("error", "Not completed")
        if name == "get_loan_summary":
            return f"Next EMI {d['next_due_amount']} due {d['next_due_date']}; outstanding {d['outstanding_principal']}"
        if name == "explain_emi_change":
            if not d.get("emi_changed"):
                return "No EMI change in the schedule"
            return (f"EMI {d['previous_emi']} → {d['new_emi']} after the rate moved {d['previous_rate_pct']}% → "
                    f"{d['new_rate_pct']}% (clause {d['clause']})")
        if name == "get_payments":
            return f"{d['count']} payments since {d['from_date']}, {d['failed_count']} failed"
        if name == "get_last_payment":
            return f"{d['amount']} {d['status']} on {d['paid_on']}"
        if name == "simulate_prepayment":
            if not d.get("eligible"):
                return "Not eligible: " + "; ".join(d.get("ineligible_reasons", []))
            return (f"Keep EMI: save {d['reduce_tenure']['interest_saved']}; lower EMI to "
                    f"{d['reduce_emi']['new_emi']}: save {d['reduce_emi']['interest_saved']} (clause {d['clause']})")
        if name == "check_foreclosure":
            if not d.get("eligible"):
                return f"Not eligible yet (clause {d['clause']})"
            fee = "no charge" if d.get("penalty_free") else f"{d['foreclosure_charge_pct']}% charge"
            return f"Eligible, {fee}; payoff {d['total_payoff_amount']} (clause {d['clause']})"
        if name == "get_servicing_options":
            avail = [o["option"].replace("_", " ") for o in d.get("options", []) if o.get("available")]
            return f"{len(avail)} options available: " + ", ".join(avail)
        if name == "search_policy":
            return "Found clauses " + ", ".join(c["clause"] for c in d.get("clauses", [])[:3])
        if name.startswith("propose_"):
            return f"Waiting for your yes (confirmation ID {d['confirmation_id']})"
        if name == "confirm_action":
            return f"Request {d['request_id']} created" + (" (already submitted, no duplicate)" if d.get("replayed") else "")
        if name == "verify_otp":
            return "Code verified" if d.get("verified") else d.get("error", "Code not accepted")
        if name == "request_human_agent":
            return f"Hand-off {d['request_id']}, queue position {d['queue_position']}"
        if name == "get_card_summary":
            return f"Statement {d['statement_balance']}, minimum due {d['minimum_due']} by {d['payment_due_date']}"
        if name == "get_card_transactions":
            dups = sum(1 for t in d.get("transactions", []) if t.get("possible_duplicate_of"))
            return f"{d['count']} transactions" + (f", {dups} possible duplicate" if dups else "")
        if name == "list_service_requests":
            return f"{len(d.get('requests', []))} requests on file"
        if name == "verify_identity":
            return "Identity confirmed"
    except (KeyError, TypeError, IndexError):
        pass
    return "Done"


def verified(figures: int, outcome: str) -> dict:
    if outcome == "regenerated":
        return {"kind": "check", "ok": True, "text": "Caught a figure no system returned and rewrote the answer",
                "result": f"{figures} figure{'s' if figures != 1 else ''} now traced to system data"}
    if outcome == "fallback":
        return {"kind": "check", "ok": True, "text": "Could not phrase the figures safely, so answered with the exact "
                                                     "system values instead"}
    if figures == 0:
        return {"kind": "check", "ok": True, "text": "No figures to verify in this answer"}
    return {"kind": "check", "ok": True, "text": f"Checked {figures} figure{'s' if figures != 1 else ''} against "
                                                 "system data", "result": "all traced to a system or calculation"}


def answered(ms: float, language: str) -> dict:
    lang = {"hi": " in Hindi", "hi-Latn": " in Hinglish"}.get(language, "")
    return {"kind": "done", "text": f"Answered{lang} in {ms / 1000:.1f} s"}
