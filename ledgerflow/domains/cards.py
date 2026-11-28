"""Credit-card servicing (workflow 2). Only domain tools live here: identity,
connectors, policy lookup, two-phase actions, audit, grounding, text/voice
runtime and UI are reused from the platform unchanged."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from ledgerflow.finance.loans import inr
from ledgerflow.platform.accounts import pick
from ledgerflow.platform.common_tools import create_ticket, proposal
from ledgerflow.platform.tools import REGISTRY, ToolContext, ToolResult

CARD_ID = {"card_id": {"type": "string", "description": (
    "Which card, when the customer has more than one: its ID (CARD-5011), its last four digits, or the card "
    "name they said ('platinum', 'rewards'). Omit when they have one card or already said which.")}}


async def _card(ctx: ToolContext, card_id: str = "") -> dict:
    """The customer's card this question is about (see platform/accounts.py)."""
    cards = await ctx.connectors["cards"].get(f"/customers/{ctx.customer_id}/cards", trace=ctx.trace)
    labelled = [{**c, "label": f"{c.get('product_short_name') or c['product']} ending {c['last4']}"} for c in cards]
    ctx.session.memory["cards_held"] = len(labelled)
    return pick(ctx.session.memory, "card", labelled, card_id)


async def _product(ctx: ToolContext, card: dict) -> dict:
    """The card's product (name and policy) from the Policy system: terms differ by card product."""
    return await ctx.connectors["policy"].get(f"/products/{card['product']}", trace=ctx.trace)


@REGISTRY.tool("get_card_summary",
               "Credit card statement: statement balance, minimum amount due, due date, credit limit and available limit.",
               params=CARD_ID)
async def get_card_summary(ctx: ToolContext, card_id: str = "") -> ToolResult:
    c = await _card(ctx, card_id)
    product = await _product(ctx, c)
    policy = product["policy"]
    rule = policy["rules"]["min_due"]
    fin = policy["rules"]["finance_charge"]
    data = {"ok": True, "card": f"{product['short_name']} ending {c['last4']}", "statement_date": c["statement_date"],
            "statement_balance": inr(c["statement_balance"]), "minimum_due": inr(c["min_due"]),
            "payment_due_date": c["due_date"], "credit_limit": inr(c["credit_limit"]),
            "available_limit": inr(c["available"]), "min_due_clause": rule["clause"],
            "min_due_rule": policy["clauses"][rule["clause"]],
            "finance_charge_monthly_pct": fin["monthly_pct"], "finance_charge_clause": fin["clause"],
            "interest_if_only_minimum_paid": policy["clauses"][fin["clause"]]}
    return ToolResult(data, card={"type": "card_summary", "data": data})


@REGISTRY.tool("get_card_transactions",
               "Recent card transactions, optionally filtered by merchant name. Flags likely duplicate charges.",
               params={"merchant": {"type": "string", "description": "Part of a merchant name to filter by"}, **CARD_ID})
async def get_card_transactions(ctx: ToolContext, merchant: str = "", card_id: str = "") -> ToolResult:
    c = await _card(ctx, card_id)
    txns = await ctx.connectors["cards"].get(f"/cards/{c['id']}/transactions", trace=ctx.trace)
    if merchant:
        txns = [t for t in txns if merchant.lower() in t["merchant"].lower()]
    seen, items = {}, []
    for t in sorted(txns, key=lambda t: t["ts"]):
        key = (t["merchant"], t["amount"])
        dup_of = None
        if key in seen and abs((datetime.fromisoformat(t["ts"]) - datetime.fromisoformat(seen[key]["ts"])).total_seconds()) < 600:
            dup_of = seen[key]["id"]
        seen[key] = t
        items.append({"transaction_id": t["id"], "date_time": t["ts"], "merchant": t["merchant"],
                      "amount": inr(t["amount"]), "status": t["status"],
                      **({"possible_duplicate_of": dup_of} if dup_of else {})})
    items.reverse()
    data = {"ok": True, "card": c["label"], "count": len(items), "transactions": items[:20]}
    return ToolResult(data, card={"type": "card_txns", "data": data})


@REGISTRY.tool("propose_card_dispute",
               "Prepare a dispute for a card transaction (does NOT submit). Returns a confirmation ID and summary to read back.",
               params={"transaction_id": {"type": "string", "description": "Transaction ID from get_card_transactions"},
                       "reason": {"type": "string", "description": "Why, in the customer's words (e.g. charged twice)"}},
               required=["transaction_id", "reason"], side_effect="write")
async def propose_card_dispute(ctx: ToolContext, transaction_id: str, reason: str) -> ToolResult:
    tid = transaction_id.strip().upper()
    cards = await ctx.connectors["cards"].get(f"/customers/{ctx.customer_id}/cards", trace=ctx.trace)
    c = next((k for k in cards if tid.startswith(f"T-{k['id'][5:]}-")), None)          # IDs carry the card
    if c is None:
        return ToolResult({"ok": False, "error": "No such transaction on this customer's cards."})
    c["label"] = f"{c.get('product_short_name') or c['product']} ending {c['last4']}"
    txns = await ctx.connectors["cards"].get(f"/cards/{c['id']}/transactions", trace=ctx.trace)
    t = next((x for x in txns if x["id"] == tid), None)
    if t is None:
        return ToolResult({"ok": False, "error": "No such transaction on this customer's card."})
    policy = (await _product(ctx, c))["policy"]
    rule = policy["rules"]["dispute"]
    credit_days = rule["temporary_credit_working_days"]
    age = (ctx.as_of - date.fromisoformat(t["ts"][:10])).days
    if age > rule["window_days"]:
        return ToolResult({"ok": False, "eligible": False, "clause": rule["clause"],
                           "error": f"The transaction is {age} days old; disputes are accepted within {rule['window_days']} days."})
    summary = (f"Dispute {inr(t['amount'])} at {t['merchant']} on {t['ts'][:10]} ({t['id']}), reason: {reason}. "
               f"A temporary credit is given within {credit_days} working days while it is investigated.")
    return proposal(ctx, "card_dispute", {"transaction_id": t["id"], "card_id": c["id"], "amount": t["amount"],
                                          "merchant": t["merchant"], "reason": reason[:300],
                                          "temporary_credit_working_days": credit_days},
                    summary, {"transaction_id": t["id"], "amount": inr(t["amount"]), "merchant": t["merchant"],
                              "clause": rule["clause"]})


def register_actions(actions) -> None:
    async def card_dispute(ctx, params, idem):
        res = await create_ticket(ctx, "card_dispute",
                                  f"Card dispute {params['transaction_id']} {inr(Decimal(params['amount']))} "
                                  f"{params['merchant']}: {params['reason']}", params, idem)
        return {**res, "temporary_credit": f"within {params.get('temporary_credit_working_days', 10)} working days"}

    actions.register("card_dispute", card_dispute)
