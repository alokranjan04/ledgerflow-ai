# Latency — journeys — 2026-11-28 08:53 — target https://ledgerflow-ai-vccwx7nvsa-el.a.run.app

| Journey turn | runs | first tool started p50 ms | turn total p50 ms | turn total max ms | tools |
|---|---|---|---|---|---|
| J1 EMI increase + ₹50k prepayment :: My EMI increased this month. Why? And if I make  | 2 | 1417 | 3466 | 3721 | explain_emi_change, simulate_prepayment |
| J2 intent change (next EMI → last payment → ₹20k tenure) :: What's my next EMI? | 2 | 31 | 429 | 550 | get_loan_summary |
| J2 intent change (next EMI → last payment → ₹20k tenure) :: Actually, check my last payment first. | 2 | 32 | 387 | 439 | get_last_payment |
| J2 intent change (next EMI → last payment → ₹20k tenure) :: Okay, now tell me whether paying ₹20,000 extra w | 2 | 1153 | 2425 | 2511 | simulate_prepayment |
| J3 failed payments → dispute :: Show me the payments that failed in the last six | 2 | 1335 | 3767 | 4164 | get_payments |
| J3 failed payments → dispute :: Please raise a dispute for the September one, I  | 2 | 1282 | 3365 | 3481 | propose_payment_dispute |
| J3 failed payments → dispute :: Yes, go ahead. | 2 | 1105 | 2276 | 2357 | confirm_action |
| J4 foreclosure eligibility :: Am I eligible for foreclosure without a penalty? | 2 | 66 | 249 | 255 | check_foreclosure |
| J5 options :: Based on my current loan and applicable terms, w | 2 | 1108 | 3555 | 3969 | get_servicing_options |

Server-side:

| Metric | n | p50 | p95 |
|---|---|---|---|
| llm.text.call | 56 | 1166.3 | 1664.2 |
| tool.check_foreclosure | 6 | 204.9 | 352.3 |
| tool.confirm_action | 6 | 1.0 | 376.9 |
| tool.explain_emi_change | 4 | 322.4 | 390.2 |
| tool.get_card_summary | 2 | 207.4 | 307.5 |
| tool.get_card_transactions | 2 | 259.0 | 317.3 |
| tool.get_last_payment | 4 | 303.6 | 403.1 |
| tool.get_loan_summary | 6 | 278.8 | 515.3 |
| tool.get_payments | 4 | 939.3 | 1158.2 |
| tool.get_servicing_options | 3 | 433.0 | 1272.9 |
| tool.propose_card_dispute | 1 | 613.9 | 613.9 |
| tool.propose_payment_dispute | 3 | 696.2 | 804.4 |
| tool.request_human_agent | 1 | 1221.6 | 1221.6 |
| tool.simulate_prepayment | 7 | 251.4 | 601.1 |
| tool.verify_otp | 2 | 0.7 | 1.2 |
| turn.text.fast_lane | 19 | 280.3 | 516.6 |
| turn.text.total | 46 | 2209.9 | 3938.9 |

Grounding counters: {"grounding.checked": 27}
