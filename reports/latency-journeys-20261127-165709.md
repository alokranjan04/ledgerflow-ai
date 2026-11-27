# Latency — journeys — 2026-11-27 16:57 — target https://ledgerflow-ai-vccwx7nvsa-el.a.run.app

| Journey turn | runs | first tool started p50 ms | turn total p50 ms | turn total max ms | tools |
|---|---|---|---|---|---|
| J1 EMI increase + ₹50k prepayment :: My EMI increased this month. Why? And if I make  | 2 | 1340 | 2742 | 2902 | explain_emi_change, simulate_prepayment |
| J2 intent change (next EMI → last payment → ₹20k tenure) :: What's my next EMI? | 2 | 52 | 730 | 804 | get_loan_summary |
| J2 intent change (next EMI → last payment → ₹20k tenure) :: Actually, check my last payment first. | 2 | 383 | 975 | 1244 | get_last_payment |
| J2 intent change (next EMI → last payment → ₹20k tenure) :: Okay, now tell me whether paying ₹20,000 extra w | 2 | 2229 | 3921 | 4463 | simulate_prepayment |
| J3 failed payments → dispute :: Show me the payments that failed in the last six | 2 | 1106 | 4529 | 5894 | get_payments |
| J3 failed payments → dispute :: Please raise a dispute for the September one, I  | 2 | 8139 | 9337 | 16396 | propose_payment_dispute |
| J3 failed payments → dispute :: Yes, go ahead. | 2 | 1184 | 1739 | 2014 | confirm_action |
| J4 foreclosure eligibility :: Am I eligible for foreclosure without a penalty? | 2 | 140 | 585 | 720 | check_foreclosure |
| J5 options :: Based on my current loan and applicable terms, w | 2 | 1490 | 3445 | 3818 | get_servicing_options |

Server-side:

| Metric | n | p50 | p95 |
|---|---|---|---|
| llm.text.call | 54 | 810.7 | 1205.3 |
| tool.check_foreclosure | 8 | 322.8 | 670.8 |
| tool.confirm_action | 6 | 1.0 | 337.8 |
| tool.explain_emi_change | 4 | 387.8 | 408.3 |
| tool.get_card_summary | 1 | 207.1 | 207.1 |
| tool.get_card_transactions | 2 | 259.0 | 317.6 |
| tool.get_last_payment | 5 | 248.0 | 3477.1 |
| tool.get_loan_summary | 6 | 291.6 | 527.4 |
| tool.get_payments | 4 | 939.3 | 1156.5 |
| tool.get_servicing_options | 3 | 846.3 | 888.2 |
| tool.propose_card_dispute | 1 | 617.4 | 617.4 |
| tool.propose_payment_dispute | 3 | 679.8 | 696.4 |
| tool.request_human_agent | 1 | 1195.6 | 1195.6 |
| tool.simulate_prepayment | 9 | 261.6 | 604.6 |
| tool.verify_otp | 2 | 0.6 | 0.7 |
| turn.text.fast_lane | 16 | 306.4 | 941.7 |
| turn.text.total | 42 | 1537.9 | 2891.9 |
| voice.setup | 2 | 334.7 | 341.3 |
| voice.voice.tool_round | 8 | 313.4 | 3477.4 |
| voice.voice.user_to_first_audio | 8 | 876.9 | 1017.2 |

Grounding counters: {"grounding.checked": 26, "grounding.voice_checked": 10}
