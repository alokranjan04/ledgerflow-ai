"""Loan-servicing domain: semantic tools over Loan Management, Payments and
Policy Knowledge. Every fact and number comes from the systems or the finance
library; the language model only chooses tools and phrases the result.
"""
from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

from ledgerflow.finance.loans import accrued_interest, add_months, charge_with_gst, inr, money, simulate_prepayment
from ledgerflow.platform.common_tools import create_ticket, proposal
from ledgerflow.platform.tools import REGISTRY, ToolContext, ToolResult


# ---------------------------------------------------------------- data access
async def _loan(ctx: ToolContext) -> dict:
    loans = await ctx.connectors["loans"].get(f"/customers/{ctx.customer_id}/loans", trace=ctx.trace)
    active = [ln for ln in loans if ln["status"] == "active"]
    if not active:
        raise ValueError("The customer has no active loan.")
    return await ctx.connectors["loans"].get(f"/loans/{active[0]['id']}", trace=ctx.trace)


async def _policy(ctx: ToolContext, product: str) -> dict:
    return (await ctx.connectors["policy"].get(f"/products/{product}", trace=ctx.trace))["policy"]


async def _payments(ctx: ToolContext, loan_id: str, since: date | None = None) -> list[dict]:
    params = {"since": since.isoformat()} if since else None
    return await ctx.connectors["payments"].get(f"/loans/{loan_id}/payments", params, trace=ctx.trace)


def _position(loan: dict, as_of: date) -> dict:
    rows = loan["schedule"]
    paid = [r for r in rows if date.fromisoformat(r["due_date"]) <= as_of]
    upcoming = [r for r in rows if date.fromisoformat(r["due_date"]) > as_of]
    last = paid[-1] if paid else None
    outstanding = Decimal(last["closing_balance"]) if last else Decimal(loan["principal"])
    current_rate = Decimal(upcoming[0]["rate_pct"]) if upcoming else Decimal(last["rate_pct"])
    return {"paid": paid, "upcoming": upcoming, "last": last, "outstanding": outstanding,
            "current_rate": current_rate, "emis_paid": len(paid), "remaining": len(upcoming),
            "current_emi": Decimal(upcoming[0]["emi"]) if upcoming else Decimal(0)}


def _fy_start(d: date) -> date:
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


# ---------------------------------------------------------------- read tools
@REGISTRY.tool("get_loan_summary",
               "Current loan position: outstanding principal, interest rate, current EMI, EMIs paid and left, "
               "next due date and amount.")
async def get_loan_summary(ctx: ToolContext) -> ToolResult:
    loan = await _loan(ctx)
    pos = _position(loan, ctx.as_of)
    nxt = pos["upcoming"][0] if pos["upcoming"] else None
    data = {
        "ok": True, "loan_id": loan["id"], "product": loan["product_code"],
        "rate_type": "floating" if loan["product_code"] == "PL-FLOAT" else "fixed",
        "sanctioned_amount": inr(loan["principal"]), "disbursal_date": loan["disbursal_date"],
        "tenure_months": loan["tenure"], "emis_paid": pos["emis_paid"], "emis_remaining": pos["remaining"],
        "current_interest_rate_pct": str(pos["current_rate"]), "outstanding_principal": inr(pos["outstanding"]),
        "current_emi": inr(pos["current_emi"]),
        "next_due_date": nxt["due_date"] if nxt else None, "next_due_amount": inr(nxt["emi"]) if nxt else None,
    }
    return ToolResult(data, card={"type": "loan_summary", "data": data})


@REGISTRY.tool("explain_emi_change",
               "Explain why the EMI changed: compares previous and current EMI, the interest-rate resets behind it, "
               "their dates and reasons, and the policy clause.")
async def explain_emi_change(ctx: ToolContext) -> ToolResult:
    loan = await _loan(ctx)
    policy = await _policy(ctx, loan["product_code"])
    rows = loan["schedule"]
    pos = _position(loan, ctx.as_of)
    # Most recent EMI change at or before the next due instalment.
    horizon = pos["paid"] + pos["upcoming"][:1]
    change = None
    for i in range(len(horizon) - 1, 0, -1):
        if horizon[i]["emi"] != horizon[i - 1]["emi"] and horizon[i]["no"] != rows[-1]["no"]:
            change = (horizon[i - 1], horizon[i])
            break
    resets = [{"effective_date": h["effective"], "rate_pct": h["rate"], "reason": h["reason"]}
              for h in loan["rate_history"]]
    rule = policy["rules"].get("rate_reset")
    clause_id = rule["clause"] if rule else None
    if not change:
        data = {"ok": True, "emi_changed": False, "current_emi": inr(pos["current_emi"]), "rate_history": resets,
                "explanation": "The EMI has not changed; the loan is on a fixed EMI." if loan["product_code"] != "PL-FLOAT"
                else "No EMI change in the schedule so far."}
        return ToolResult(data, card={"type": "emi_change", "data": data})
    before, after = change
    old_rate, new_rate = Decimal(before["rate_pct"]), Decimal(after["rate_pct"])
    data = {
        "ok": True, "emi_changed": True,
        "previous_emi": inr(before["emi"]), "previous_emi_due_date": before["due_date"],
        "new_emi": inr(after["emi"]), "new_emi_first_due_date": after["due_date"],
        "emi_difference": inr(Decimal(after["emi"]) - Decimal(before["emi"])),
        "direction": "increased" if Decimal(after["emi"]) > Decimal(before["emi"]) else "decreased",
        "previous_rate_pct": str(old_rate), "new_rate_pct": str(new_rate),
        "rate_change_pct_points": str(new_rate - old_rate),
        "reset_reason": next((h["reason"] for h in reversed(loan["rate_history"]) if Decimal(h["rate"]) == new_rate), ""),
        "outstanding_principal_at_reset": inr(before["closing_balance"]),
        "remaining_emis_at_reset": len(rows) - before["no"],
        "method": "EMI recalculated over the remaining tenure at the new rate; tenure unchanged.",
        "clause": clause_id, "clause_text": policy["clauses"].get(clause_id, "") if clause_id else "",
        "rate_history": resets,
    }
    return ToolResult(data, card={"type": "emi_change", "data": data})


@REGISTRY.tool("get_payments",
               "Payment history for the loan, with failed debits, their reasons and charges. Use status='failed' "
               "for failed payments.",
               params={"months": {"type": "integer", "description": "How many months back (default 6)"},
                       "status": {"type": "string", "enum": ["all", "failed"], "description": "Filter"}})
async def get_payments(ctx: ToolContext, months: int = 6, status: str = "all") -> ToolResult:
    loans = await ctx.connectors["loans"].get(f"/customers/{ctx.customer_id}/loans", trace=ctx.trace)
    if not loans:
        raise ValueError("The customer has no loan.")
    loan_id = loans[0]["id"]
    months = max(1, min(int(months or 6), 24))
    since = add_months(ctx.as_of, -months)
    pays = await _payments(ctx, loan_id, since)
    if status == "failed":
        pays = [p for p in pays if p["status"] == "failed"]
    items = [{"payment_id": p["id"], "instalment_no": p["instalment_no"], "due_date": p["due_date"],
              "attempt_date": p["attempt_date"], "amount": inr(p["amount"]), "status": p["status"],
              "mode": p["mode"], **({"failure_reason": p["reason"], "reason_code": p["reason_code"],
                                    "bounce_charge": inr(p["charge"]), "gst_on_charge": inr(p["charge_gst"])}
                                   if p["status"] == "failed" else {})}
             for p in pays]
    failed = [i for i in items if i["status"] == "failed"]
    ctx.session.memory["last_payments"] = [i["payment_id"] for i in items]
    data = {"ok": True, "loan_id": loan_id, "window_months": months, "from_date": since.isoformat(),
            "count": len(items), "failed_count": len(failed),
            "total_bounce_charges_incl_gst": inr(sum(Decimal(p["charge"]) + Decimal(p["charge_gst"])
                                                     for p in pays if p["status"] == "failed")),
            "payments": items,
            "note": "Each failed debit was later collected by the retry listed with the same instalment_no."}
    return ToolResult(data, card={"type": "payments", "data": data})


@REGISTRY.tool("get_last_payment", "The most recent payment attempt on the loan and whether it succeeded.")
async def get_last_payment(ctx: ToolContext) -> ToolResult:
    loans = await ctx.connectors["loans"].get(f"/customers/{ctx.customer_id}/loans", trace=ctx.trace)
    pays = await _payments(ctx, loans[0]["id"], add_months(ctx.as_of, -3))
    if not pays:
        return ToolResult({"ok": True, "last_payment": None})
    p = pays[0]
    data = {"ok": True, "payment_id": p["id"], "instalment_no": p["instalment_no"], "due_date": p["due_date"],
            "paid_on": p["attempt_date"], "amount": inr(p["amount"]), "status": p["status"], "mode": p["mode"]}
    return ToolResult(data, card={"type": "last_payment", "data": data})


# ---------------------------------------------------------------- calculators & rules
def _prepay_rules(loan: dict, policy: dict, pos: dict, amount: Decimal, as_of: date, prepaid_this_fy: Decimal) -> dict:
    rule = policy["rules"]["part_prepayment"]
    reasons = []
    if pos["emis_paid"] < rule["min_emis_paid"]:
        reasons.append(f"needs at least {rule['min_emis_paid']} EMIs paid (paid: {pos['emis_paid']})")
    if amount < Decimal(rule["min_amount"]):
        reasons.append(f"minimum part-prepayment is {inr(rule['min_amount'])}")
    cap = money(pos["outstanding"] * Decimal(rule["max_pct_outstanding_per_fy"]) / 100) - prepaid_this_fy
    if amount > cap:
        reasons.append(f"maximum this financial year is {inr(max(cap, Decimal(0)))}")
    charge, gst = charge_with_gst(amount, Decimal(rule["charge_pct"]))
    return {"eligible": not reasons, "reasons": reasons, "clause": rule["clause"],
            "clause_text": policy["clauses"][rule["clause"]], "charge": charge, "gst": gst, "cap": cap}


@REGISTRY.tool("simulate_prepayment",
               "What-if for a part-prepayment today: checks eligibility under the product rules and compares "
               "'reduce tenure' (same EMI) with 'reduce EMI' (same tenure): interest saved, new tenure, new EMI, charges.",
               params={"amount": {"type": "number", "description": "Prepayment amount in rupees"}},
               required=["amount"], side_effect="calc")
async def tool_simulate_prepayment(ctx: ToolContext, amount: float) -> ToolResult:
    amount = money(Decimal(str(amount)))
    loan = await _loan(ctx)
    policy = await _policy(ctx, loan["product_code"])
    pos = _position(loan, ctx.as_of)
    rules = _prepay_rules(loan, policy, pos, amount, ctx.as_of, Decimal(0))
    out = simulate_prepayment(pos["outstanding"], pos["current_rate"], pos["current_emi"], pos["remaining"], amount)
    data = {
        "ok": True, "prepayment_amount": inr(amount), "eligible": rules["eligible"],
        "ineligible_reasons": rules["reasons"], "clause": rules["clause"], "clause_text": rules["clause_text"],
        "prepayment_charge": inr(rules["charge"]), "gst_on_charge": inr(rules["gst"]),
        "outstanding_principal_now": inr(pos["outstanding"]), "interest_rate_pct": str(pos["current_rate"]),
        "current_emi": inr(pos["current_emi"]), "emis_remaining_now": out.baseline_months,
        "interest_remaining_without_prepayment": inr(out.baseline_interest),
        "reduce_tenure": {"emi": inr(pos["current_emi"]), "emis_remaining": out.reduce_tenure_months,
                          "tenure_reduced_by_months": out.baseline_months - out.reduce_tenure_months,
                          "interest_saved": inr(out.reduce_tenure_saving)},
        "reduce_emi": {"new_emi": inr(out.reduce_emi_new_emi), "emi_reduced_by": inr(pos["current_emi"] - out.reduce_emi_new_emi),
                       "emis_remaining": pos["remaining"], "interest_saved": inr(out.reduce_emi_saving)},
        "net_saving_reduce_tenure": inr(out.reduce_tenure_saving - rules["charge"] - rules["gst"]),
        "net_saving_reduce_emi": inr(out.reduce_emi_saving - rules["charge"] - rules["gst"]),
        "assumption": "Assumes the current interest rate stays the same for the rest of the loan.",
    }
    return ToolResult(data, card={"type": "prepayment", "data": data})


@REGISTRY.tool("check_foreclosure",
               "Foreclosure (full early closure) eligibility, charges under the product rules, and a payoff quote.",
               params={"on_date": {"type": "string", "description": "Payoff date YYYY-MM-DD; omit for today"}},
               side_effect="calc")
async def check_foreclosure(ctx: ToolContext, on_date: str = "") -> ToolResult:
    loan = await _loan(ctx)
    policy = await _policy(ctx, loan["product_code"])
    when = date.fromisoformat(on_date) if on_date else ctx.as_of
    if when < ctx.as_of:
        when = ctx.as_of
    pos = _position(loan, when)
    rule = policy["rules"]["foreclosure"]
    disb = date.fromisoformat(loan["disbursal_date"])
    months_since = (when.year - disb.year) * 12 + when.month - disb.month - (1 if when.day < disb.day else 0)
    eligible, reasons, charge_pct, clause = True, [], Decimal(0), rule["clause"]
    if "lock_in_emis" in rule:
        if pos["emis_paid"] < rule["lock_in_emis"]:
            eligible = False
            clause = rule["clause_lock_in"]
            reasons.append(f"inside the lock-in: {rule['lock_in_emis']} EMIs must be paid (paid: {pos['emis_paid']})")
        charge_pct = Decimal(rule["charge_pct_until_24m"] if months_since < 24 else rule["charge_pct_after_24m"])
    else:
        if pos["emis_paid"] < rule.get("min_emis_paid", 0):
            eligible = False
            reasons.append("foreclosure is allowed after the first EMI")
        charge_pct = Decimal(rule.get("charge_pct", "0"))
    last_due = date.fromisoformat(pos["last"]["due_date"]) if pos["last"] else disb
    accrued = accrued_interest(pos["outstanding"], pos["current_rate"], last_due, when)
    charge, gst = charge_with_gst(pos["outstanding"], charge_pct)
    payoff = money(pos["outstanding"] + accrued + charge + gst)
    data = {"ok": True, "eligible": eligible, "ineligible_reasons": reasons,
            "penalty_free": eligible and charge_pct == 0, "quote_date": when.isoformat(),
            "outstanding_principal": inr(pos["outstanding"]), "accrued_interest": inr(accrued),
            "accrued_since": last_due.isoformat(), "foreclosure_charge_pct": str(charge_pct),
            "foreclosure_charge": inr(charge), "gst_on_charge": inr(gst),
            "total_payoff_amount": inr(payoff) if eligible else None,
            "loan_age_months": months_since, "clause": clause, "clause_text": policy["clauses"][clause]}
    if "lock_in_emis" in rule:
        lock_in_ends = add_months(date.fromisoformat(loan["first_due"]), rule["lock_in_emis"] - 1)
        cheaper_from = add_months(disb, 24)
        data["lock_in_ends_after_emi_due"] = lock_in_ends.isoformat()
        if months_since < 24:
            data["lower_charge_from"] = cheaper_from.isoformat()
            data["lower_charge_pct"] = rule["charge_pct_after_24m"]
    return ToolResult(data, card={"type": "foreclosure", "data": data})


@REGISTRY.tool("get_servicing_options",
               "Overview of what the customer can do with their loan right now: part-prepayment, foreclosure, EMI "
               "date change, open charges to dispute. Use for 'what are my options' questions.")
async def get_servicing_options(ctx: ToolContext) -> ToolResult:
    loan = await _loan(ctx)
    since = add_months(ctx.as_of, -6)
    policy_t = asyncio.create_task(_policy(ctx, loan["product_code"]))
    pays_t = asyncio.create_task(_payments(ctx, loan["id"], since))
    unavailable = []
    policy = await policy_t
    try:
        pays = await pays_t
    except Exception:
        pays, unavailable = None, ["payments"]
    pos = _position(loan, ctx.as_of)
    r = policy["rules"]
    pp = r["part_prepayment"]
    fc = r["foreclosure"]
    options = []
    pp_ok = pos["emis_paid"] >= pp["min_emis_paid"]
    options.append({"option": "part_prepayment", "available": pp_ok, "clause": pp["clause"],
                    "detail": f"Minimum {inr(pp['min_amount'])}, up to {pp['max_pct_outstanding_per_fy']}% of outstanding "
                              f"({inr(money(pos['outstanding'] * Decimal(pp['max_pct_outstanding_per_fy']) / 100))}) this year, "
                              f"charge {pp['charge_pct']}%."
                              + ("" if pp_ok else f" Available after {pp['min_emis_paid']} EMIs.")})
    if "lock_in_emis" in fc:
        fc_ok = pos["emis_paid"] >= fc["lock_in_emis"]
        disb = date.fromisoformat(loan["disbursal_date"])
        pct = fc["charge_pct_until_24m"] if (ctx.as_of - disb).days < 730 else fc["charge_pct_after_24m"]
        options.append({"option": "foreclosure", "available": fc_ok, "clause": fc["clause"] if fc_ok else fc["clause_lock_in"],
                        "detail": f"Charge {pct}% of outstanding plus GST." if fc_ok else
                        f"Locked in until {fc['lock_in_emis']} EMIs are paid."})
    else:
        options.append({"option": "foreclosure", "available": True, "clause": fc["clause"],
                        "detail": "No foreclosure charge (floating rate)."})
    edc = r["emi_date_change"]
    options.append({"option": "emi_date_change", "available": True, "clause": edc["clause"],
                    "detail": f"Move the EMI date to the {', '.join(str(d) + 'th' for d in edc['allowed_days'])}, once a year."})
    if pays is not None:
        charged = [p for p in pays if p["status"] == "failed" and Decimal(p["charge"]) > 0]
        if charged:
            options.append({"option": "dispute_bounce_charges", "available": True,
                            "clause": r["bounce_charge"]["clause"],
                            "detail": f"{len(charged)} failed debit(s) in 6 months carried bounce charges totalling "
                                      f"{inr(sum(Decimal(p['charge']) + Decimal(p['charge_gst']) for p in charged))}.",
                            "payment_ids": [p["id"] for p in charged]})
    data = {"ok": True, "loan_id": loan["id"], "outstanding_principal": inr(pos["outstanding"]),
            "current_emi": inr(pos["current_emi"]), "interest_rate_pct": str(pos["current_rate"]),
            "emis_paid": pos["emis_paid"], "emis_remaining": pos["remaining"], "options": options,
            "unavailable_systems": unavailable}
    return ToolResult(data, card={"type": "options", "data": data})


# ---------------------------------------------------------------- actions (two-phase)
@REGISTRY.tool("propose_payment_dispute",
               "Prepare a dispute for a failed payment or its bounce charge (does NOT submit). Returns a confirmation "
               "ID and summary to read back.",
               params={"payment_id": {"type": "string", "description": "Payment ID of the FAILED payment from get_payments "
                                                                        "(ends with F)"},
                       "reason": {"type": "string", "description": "Why the customer disputes it, in their words"}},
               required=["payment_id", "reason"], side_effect="write")
async def propose_payment_dispute(ctx: ToolContext, payment_id: str, reason: str) -> ToolResult:
    loans = await ctx.connectors["loans"].get(f"/customers/{ctx.customer_id}/loans", trace=ctx.trace)
    pays = await _payments(ctx, loans[0]["id"])
    p = next((x for x in pays if x["id"] == payment_id.strip().upper()), None)
    if p is None:  # scoped: a payment of another customer is simply "not found"
        return ToolResult({"ok": False, "error": "No such payment on this customer's loan. List payments first."})
    if p["status"] != "failed":
        return ToolResult({"ok": False, "error": "That payment succeeded; only failed debits can be disputed."})
    total = money(Decimal(p["charge"]) + Decimal(p["charge_gst"]))
    summary = (f"Dispute the failed EMI debit of {inr(p['amount'])} due {p['due_date']} ({p['id']}, "
               f"reason: {p['reason']}) and request reversal of the {inr(total)} bounce charge incl. GST.")
    details = {"payment_id": p["id"], "due_date": p["due_date"], "amount": inr(p["amount"]),
               "charge_in_dispute": inr(total)}
    return proposal(ctx, "payment_dispute", {"payment_id": p["id"], "reason": reason[:300], "loan_id": p["loan_id"],
                                             "charge": str(total)}, summary, details)


@REGISTRY.tool("propose_foreclosure_statement",
               "Prepare a request for an official foreclosure statement sent to the registered email (does NOT submit).",
               side_effect="write")
async def propose_foreclosure_statement(ctx: ToolContext) -> ToolResult:
    loan = await _loan(ctx)
    summary = f"Send the official foreclosure statement for loan {loan['id']} to the registered email address."
    return proposal(ctx, "foreclosure_statement", {"loan_id": loan["id"]}, summary)


@REGISTRY.tool("propose_prepayment_request",
               "Prepare a part-prepayment request (does NOT submit). Run simulate_prepayment first and use the "
               "customer's chosen option.",
               params={"amount": {"type": "number", "description": "Amount in rupees"},
                       "option": {"type": "string", "enum": ["reduce_tenure", "reduce_emi"],
                                  "description": "Keep EMI and shorten tenure, or keep tenure and lower EMI"}},
               required=["amount", "option"], side_effect="write")
async def propose_prepayment_request(ctx: ToolContext, amount: float, option: str) -> ToolResult:
    amount = money(Decimal(str(amount)))
    loan = await _loan(ctx)
    policy = await _policy(ctx, loan["product_code"])
    pos = _position(loan, ctx.as_of)
    rules = _prepay_rules(loan, policy, pos, amount, ctx.as_of, Decimal(0))
    if not rules["eligible"]:
        return ToolResult({"ok": False, "eligible": False, "reasons": rules["reasons"], "clause": rules["clause"]})
    label = "reduce the tenure (same EMI)" if option == "reduce_tenure" else "reduce the EMI (same tenure)"
    summary = (f"Part-prepay {inr(amount)} on loan {loan['id']} and {label}. Charge {inr(rules['charge'] + rules['gst'])} "
               f"incl. GST. A payment link will be sent to the registered mobile.")
    return proposal(ctx, "prepayment_request", {"loan_id": loan["id"], "amount": str(amount), "option": option},
                    summary, {"amount": inr(amount), "option": option})


def register_actions(actions) -> None:
    async def payment_dispute(ctx, params, idem):
        return await create_ticket(ctx, "payment_dispute",
                                   f"Dispute failed debit {params['payment_id']}: {params['reason']}", params, idem)

    async def foreclosure_statement(ctx, params, idem):
        res = await create_ticket(ctx, "foreclosure_statement", f"Foreclosure statement for {params['loan_id']}",
                                  params, idem)
        return {**res, "delivery": "registered email within 1 working day"}

    async def prepayment_request(ctx, params, idem):
        res = await create_ticket(ctx, "prepayment_request",
                                  f"Part-prepayment {inr(params['amount'])} ({params['option']})", params, idem)
        return {**res, "next_step": "payment link sent to the registered mobile, valid 24 hours"}

    actions.register("payment_dispute", payment_dispute)
    actions.register("foreclosure_statement", foreclosure_statement)
    actions.register("prepayment_request", prepayment_request)

