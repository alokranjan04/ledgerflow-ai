"""Fast lane: simple, single-intent questions answered straight from the systems.

Two uses:
1. Speed: "What's my next EMI?" needs one lookup and one sentence. A deterministic
   router + template answers it in well under a second, with no model call.
2. Resilience: when the language model is down (outage, quota, timeout), the same
   templates still answer what they recognise instead of returning an error.

The router is deliberately conservative: anything with an amount, an action, more
than one intent, or several clauses goes to the model in normal operation. Templates
only print values copied from tool results, so answers are grounded by construction
(and still pass through the grounding check).
"""
from __future__ import annotations

import re

from ledgerflow.platform.grounding import normalise_indic

# intent -> (tool, tool args)
INTENTS = {
    "emi_change": ("explain_emi_change", {}),
    "next_emi": ("get_loan_summary", {}),
    "outstanding": ("get_loan_summary", {}),
    "last_payment": ("get_last_payment", {}),
    "failed_payments": ("get_payments", {"status": "failed", "months": 6}),
    "foreclosure": ("check_foreclosure", {}),
    "options": ("get_servicing_options", {}),
    "card_summary": ("get_card_summary", {}),
    "card_transactions": ("get_card_transactions", {}),
}

_ANY = lambda *ws: "(?:" + "|".join(ws) + ")"  # noqa: E731
EMI = _ANY(r"\bemi", "instal+ments?", "किस्त")
PATTERNS = {
    "emi_change": [EMI, _ANY("increas", "went up", "go up", "gone up", "higher", "chang", "different", "badh",
                             "बढ़", r"\bwhy\b", "kyun", "kyon", "क्यों")],
    "next_emi": [EMI, _ANY(r"\bnext\b", "upcoming", r"\bagli\b", "अगली", r"\bdue\b", r"\bkab\b", "कब", r"\bwhen\b")],
    "outstanding": [_ANY("outstanding", r"\bbalance\b", r"\bbaki\b", r"\bbaaki\b", "बकाया", "principal"),
                    _ANY(r"\bloan\b", "लोन", "principal", "outstanding")],
    "last_payment": [_ANY(r"\blast\b", "latest", "previous", "pichl", "पिछल"), _ANY("payment", r"\bpaid\b", "भुगतान",
                                                                                     "पेमेंट")],
    "failed_payments": [_ANY("fail", "bounce", "reject", "फेल", "बाउंस"), _ANY("payment", "debit", EMI, "पेमेंट",
                                                                               "auto")],
    "foreclosure": [_ANY("foreclos", r"close (?:my |the )?loan", r"loan (?:band|close)", "लोन बंद", r"pay ?off")],
    "options": [_ANY(r"\boptions?\b", "विकल्प")],
    "card_summary": [_ANY("statement", r"minimum due", r"min due", r"card bill", "बिल", "स्टेटमेंट")],
    "card_transactions": [_ANY("transactions?", "charged twice", "double charge", "ट्रांज़ैक्शन")],
}
# Questions that need the model (amounts, actions, what-ifs) never take the fast lane.
NEEDS_MODEL = re.compile("|".join([
    r"₹", r"\brs\.?\s*\d", r"\d{3,}", r"\b(?:lakh|lac|crore|thousand|hazaa?r|k)\b", "हज़ार", "लाख",
    r"\bdispute", r"\braise\b", r"\bcancel", r"\bprepay", r"\bpart.?pay", r"\bagent\b", r"\bhuman\b", r"\bperson\b",
    r"\bcomplain", r"\bblock", r"\bupdate\b", r"\bchange my\b", r"\bwhat if\b", r"\bshould i\b", r"\bsave\b",
    "शिकायत", "विवाद",
]), re.IGNORECASE)
MULTI = re.compile(r"\band\b|\balso\b|\baur\b|और|\bthen\b|;|\?.+\?", re.IGNORECASE)


def route(text: str, allowed_tools: list[str], strict: bool = True) -> list[str]:
    """Intents to answer directly. strict (normal operation): exactly one intent, no
    amount, action or second clause, else [] (the model handles it). Not strict
    (model unavailable): every recognised intent, up to two."""
    t = (text or "").lower()
    norm = normalise_indic(t).lower()          # "पचास हज़ार" -> "fifty thousand": an amount, so the model handles it
    if strict and (len(t) > 160 or NEEDS_MODEL.search(t) or NEEDS_MODEL.search(norm) or MULTI.search(t)):
        return []
    hits = [name for name, pats in PATTERNS.items()
            if INTENTS[name][0] in allowed_tools and all(re.search(p, t) for p in pats)]
    if "emi_change" in hits:                      # "why did my next EMI go up" is about the change
        hits = [h for h in hits if h != "next_emi"]
    if "failed_payments" in hits:                 # "failed in the last six months" is not "my last payment"
        hits = [h for h in hits if h != "last_payment"]
    if "outstanding" in hits and "next_emi" in hits:
        hits.remove("outstanding")
    if strict:
        return hits if len(hits) == 1 else []
    return hits[:2]


# ---------------------------------------------------------------- templates
MONTHS = {"en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                 "November", "December"],
          "hi": ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर",
                 "दिसंबर"]}


def day(iso: str | None, lang: str) -> str:
    if not iso:
        return "—"
    y, m, d = (int(x) for x in str(iso)[:10].split("-"))
    names = MONTHS["hi"] if lang == "hi" else MONTHS["en"]
    return f"{d} {names[m - 1]} {y}"


ISO_DATE = re.compile(r"(?<![\w-])(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?![\w-])")


def readable_dates(text: str, lang: str) -> str:
    """Tools return ISO dates (2026-12-10); customers read '10 December 2026' (Hindi: '10 दिसंबर 2026').
    Applied to every text answer after grounding, so the verifier still compares the tool's own value.
    Date-times and IDs that merely contain digits are left alone."""
    return ISO_DATE.sub(lambda m: day(m.group(0), lang), text or "")


def _t(lang: str, en: str, hi: str, hil: str) -> str:
    return {"hi": hi, "hi-Latn": hil}.get(lang, en)


def answer(intent: str, d: dict, lang: str) -> str:
    """One grounded answer from one tool result. Every figure is copied from `d`."""
    if not d.get("ok", True):
        if d.get("unavailable"):
            return _t(lang, f"I couldn't reach the {d['unavailable']} system just now, so I can't answer that "
                            "reliably. Please try again in a minute, or I can raise a service request.",
                      f"अभी {d['unavailable']} सिस्टम से जवाब नहीं मिल रहा, इसलिए मैं इसका सही जवाब नहीं दे सकती। "
                      "कृपया एक मिनट बाद फिर पूछें।",
                      f"Abhi {d['unavailable']} system se jawab nahi mil raha, isliye sahi jawab nahi de sakti. "
                      "Ek minute baad phir poochiye.")
        return d.get("error", "That isn't available right now.")
    if intent == "next_emi":
        return _t(lang,
                  f"Your next EMI is **{d['next_due_amount']}**, due on **{day(d['next_due_date'], lang)}**. "
                  f"You have {d['emis_remaining']} EMIs left; outstanding principal is {d['outstanding_principal']}.",
                  f"आपकी अगली EMI **{d['next_due_amount']}** है, जो **{day(d['next_due_date'], lang)}** को देय है। "
                  f"{d['emis_remaining']} EMI बाकी हैं; बकाया मूलधन {d['outstanding_principal']} है।",
                  f"Aapki agli EMI **{d['next_due_amount']}** hai, due date **{day(d['next_due_date'], lang)}**. "
                  f"{d['emis_remaining']} EMI baaki hain; outstanding principal {d['outstanding_principal']} hai.")
    if intent == "outstanding":
        return _t(lang,
                  f"Your outstanding principal on loan {d['loan_id']} is **{d['outstanding_principal']}** at "
                  f"{d['current_interest_rate_pct']}% ({d['rate_type']}). Next EMI: {d['next_due_amount']} on "
                  f"{day(d['next_due_date'], lang)}.",
                  f"लोन {d['loan_id']} पर बकाया मूलधन **{d['outstanding_principal']}** है, ब्याज दर "
                  f"{d['current_interest_rate_pct']}%। अगली EMI: {d['next_due_amount']}, {day(d['next_due_date'], lang)} को।",
                  f"Loan {d['loan_id']} par outstanding principal **{d['outstanding_principal']}** hai, rate "
                  f"{d['current_interest_rate_pct']}%. Agli EMI: {d['next_due_amount']}, {day(d['next_due_date'], lang)} ko.")
    if intent == "emi_change":
        if not d.get("emi_changed"):
            return _t(lang, f"Your EMI has not changed: it is {d['current_emi']}.",
                      f"आपकी EMI नहीं बदली है: यह {d['current_emi']} है।",
                      f"Aapki EMI nahi badli hai: yeh {d['current_emi']} hai.")
        up = d["direction"] == "increased"
        return _t(lang,
                  f"Your EMI {d['direction']} from **{d['previous_emi']}** to **{d['new_emi']}** from "
                  f"{day(d['new_emi_first_due_date'], lang)}. Reason: {d['reset_reason']}, which moved your rate from "
                  f"{d['previous_rate_pct']}% to {d['new_rate_pct']}% (clause {d['clause']}). {d['method']}",
                  f"आपकी EMI **{d['previous_emi']}** से {'बढ़कर' if up else 'घटकर'} **{d['new_emi']}** हो गई "
                  f"({day(d['new_emi_first_due_date'], lang)} से)। वजह: {d['reset_reason']} — ब्याज दर "
                  f"{d['previous_rate_pct']}% से {d['new_rate_pct']}% हुई (क्लॉज़ {d['clause']})।",
                  f"Aapki EMI **{d['previous_emi']}** se {'badhkar' if up else 'ghatkar'} **{d['new_emi']}** ho gayi "
                  f"({day(d['new_emi_first_due_date'], lang)} se). Wajah: {d['reset_reason']}; rate "
                  f"{d['previous_rate_pct']}% se {d['new_rate_pct']}% hua (clause {d['clause']}).")
    if intent == "last_payment":
        if not d.get("payment_id"):
            return _t(lang, "I don't see a payment in the last three months.", "पिछले तीन महीनों में कोई भुगतान नहीं दिखा।",
                      "Pichhle teen mahino mein koi payment nahi dikha.")
        return _t(lang,
                  f"Your last payment of **{d['amount']}** (EMI due {day(d['due_date'], lang)}) was **{d['status']}** on "
                  f"{day(d['paid_on'], lang)} via {d['mode']}.",
                  f"आपका पिछला भुगतान **{d['amount']}** (EMI देय {day(d['due_date'], lang)}) {day(d['paid_on'], lang)} को "
                  f"**{'सफल' if d['status'] == 'success' else d['status']}** रहा ({d['mode']})।",
                  f"Aapka pichhla payment **{d['amount']}** (EMI due {day(d['due_date'], lang)}) {day(d['paid_on'], lang)} "
                  f"ko **{d['status']}** raha ({d['mode']}).")
    if intent == "failed_payments":
        rows = [p for p in d.get("payments", []) if p.get("status") == "failed"]
        if not rows:
            return _t(lang, f"No failed payments since {day(d['from_date'], lang)}.",
                      f"{day(d['from_date'], lang)} से कोई पेमेंट फेल नहीं हुआ।",
                      f"{day(d['from_date'], lang)} se koi payment fail nahi hua.")
        head = _t(lang, f"You had **{d['failed_count']}** failed payment(s) since {day(d['from_date'], lang)}:",
                  f"{day(d['from_date'], lang)} से **{d['failed_count']}** पेमेंट फेल हुए:",
                  f"{day(d['from_date'], lang)} se **{d['failed_count']}** payment fail hue:")
        lines = [f"- **{day(p['due_date'], lang)}**: {p['amount']} — {p['failure_reason']}; "
                 f"{_t(lang, 'charge', 'चार्ज', 'charge')} {p['bounce_charge']} + GST {p['gst_on_charge']}" for p in rows]
        tail = _t(lang, "Each was collected later by a retry. I can raise a dispute for a charge if you want.",
                  "हर पेमेंट बाद में दोबारा कोशिश में वसूल हो गया। चाहें तो मैं किसी चार्ज पर विवाद दर्ज कर सकती हूँ।",
                  "Har payment baad mein retry se collect ho gaya. Chahein to main kisi charge par dispute raise kar sakti hoon.")
        return "\n".join([head, *lines, "", tail])
    if intent == "foreclosure":
        if not d.get("eligible"):
            why = "; ".join(d.get("ineligible_reasons", []))
            return _t(lang, f"Not yet: {why} (clause {d['clause']}).",
                      f"अभी नहीं: {why} (क्लॉज़ {d['clause']})।", f"Abhi nahi: {why} (clause {d['clause']}).")
        if d.get("penalty_free"):
            return _t(lang,
                      f"Yes, you can foreclose without a penalty (clause {d['clause']}). Payoff as of "
                      f"{day(d['quote_date'], lang)}: **{d['total_payoff_amount']}** (principal "
                      f"{d['outstanding_principal']} + interest {d['accrued_interest']}).",
                      f"हाँ, आप बिना पेनल्टी लोन बंद कर सकते हैं (क्लॉज़ {d['clause']})। {day(d['quote_date'], lang)} तक "
                      f"कुल राशि: **{d['total_payoff_amount']}** (मूलधन {d['outstanding_principal']} + ब्याज {d['accrued_interest']})।",
                      f"Haan, aap bina penalty loan band kar sakte hain (clause {d['clause']}). {day(d['quote_date'], lang)} "
                      f"tak payoff: **{d['total_payoff_amount']}** (principal {d['outstanding_principal']} + interest "
                      f"{d['accrued_interest']}).")
        later = (f" The charge drops to {d['lower_charge_pct']}% from {day(d['lower_charge_from'], lang)}."
                 if d.get("lower_charge_from") else "")
        return _t(lang,
                  f"You can foreclose, but a **{d['foreclosure_charge_pct']}% charge** applies (clause {d['clause']}): "
                  f"{d['foreclosure_charge']} + GST {d['gst_on_charge']}. Payoff as of {day(d['quote_date'], lang)}: "
                  f"**{d['total_payoff_amount']}**.{later}",
                  f"आप लोन बंद कर सकते हैं, लेकिन **{d['foreclosure_charge_pct']}% चार्ज** लगेगा (क्लॉज़ {d['clause']}): "
                  f"{d['foreclosure_charge']} + GST {d['gst_on_charge']}। {day(d['quote_date'], lang)} तक कुल राशि: "
                  f"**{d['total_payoff_amount']}**।",
                  f"Aap loan band kar sakte hain, lekin **{d['foreclosure_charge_pct']}% charge** lagega (clause "
                  f"{d['clause']}): {d['foreclosure_charge']} + GST {d['gst_on_charge']}. Payoff: **{d['total_payoff_amount']}**.")
    if intent == "options":
        rows = [f"- **{o['option'].replace('_', ' ')}** ({'available' if o['available'] else 'not yet'}): {o['detail']} "
                f"[{o['clause']}]" for o in d.get("options", [])]
        head = _t(lang,
                  f"Your loan {d['loan_id']}: outstanding principal **{d['outstanding_principal']}** at "
                  f"{d['interest_rate_pct']}%, current EMI {d['current_emi']}, {d['emis_remaining']} EMIs left. "
                  "Your options right now:",
                  f"आपका लोन {d['loan_id']}: बकाया मूलधन **{d['outstanding_principal']}**, ब्याज दर "
                  f"{d['interest_rate_pct']}%, मौजूदा EMI {d['current_emi']}, {d['emis_remaining']} EMI बाकी। "
                  "अभी आपके विकल्प:",
                  f"Aapka loan {d['loan_id']}: outstanding principal **{d['outstanding_principal']}**, rate "
                  f"{d['interest_rate_pct']}%, current EMI {d['current_emi']}, {d['emis_remaining']} EMI baaki. "
                  "Abhi aapke options:")
        return "\n".join([head, *rows])
    if intent == "card_summary":
        return _t(lang,
                  f"Your statement balance is **{d['statement_balance']}**; minimum due **{d['minimum_due']}** by "
                  f"**{day(d['payment_due_date'], lang)}**. Available limit: {d['available_limit']}.",
                  f"आपका स्टेटमेंट बैलेंस **{d['statement_balance']}** है; मिनिमम ड्यू **{d['minimum_due']}**, "
                  f"**{day(d['payment_due_date'], lang)}** तक। उपलब्ध लिमिट: {d['available_limit']}।",
                  f"Aapka statement balance **{d['statement_balance']}** hai; minimum due **{d['minimum_due']}**, "
                  f"**{day(d['payment_due_date'], lang)}** tak. Available limit: {d['available_limit']}.")
    if intent == "card_transactions":
        dups = [t for t in d.get("transactions", []) if t.get("possible_duplicate_of")]
        rows = [f"- {t['date_time'][:10]} · {t['merchant']} · {t['amount']}"
                + (" — possible duplicate" if t.get("possible_duplicate_of") else "") for t in d.get("transactions", [])[:6]]
        head = _t(lang, f"Your latest transactions ({len(dups)} possible duplicate):" if dups else "Your latest transactions:",
                  "आपके हाल के ट्रांज़ैक्शन:", "Aapke recent transactions:")
        return "\n".join([head, *rows])
    return ""


OUTAGE_NOTE = {"en": "My assistant service is unavailable right now, but here is what your account shows:",
               "hi": "मेरी सहायक सेवा अभी उपलब्ध नहीं है, लेकिन आपके खाते में यह दिख रहा है:",
               "hi-Latn": "Meri assistant service abhi available nahi hai, lekin aapke account mein yeh dikh raha hai:"}
