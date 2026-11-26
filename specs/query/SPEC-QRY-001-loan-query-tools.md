# SPEC-QRY-001: Loan query engine (semantic tools and policy rules)
Status: implemented     Owner: loans     Version: 1.0

## Context
Challenge 2: answer questions that combine account data, policy rules, calculations, context and
several systems, which document retrieval alone cannot answer. The model picks tools; the tools return
facts, computed figures and policy clause IDs. Code: `ledgerflow/domains/loans.py`; rules and clauses
are versioned data in the Policy system.

## Requirements
- **REQ-QRY-001.1** [MUST] WHEN asked about the loan position THE SYSTEM SHALL return the outstanding principal, current rate, current EMI, EMIs paid and remaining, and the next due date and amount.
- **REQ-QRY-001.2** [MUST] WHEN asked why the EMI changed THE SYSTEM SHALL return the previous and new EMI with their due dates, the old and new rate, the reset reason, the method, the rate history and the governing clause.
- **REQ-QRY-001.3** [MUST] WHEN asked about payments THE SYSTEM SHALL return payments in the requested window, filterable to failures, with each failure's reason, bounce charge and GST, and the retry that collected it.
- **REQ-QRY-001.4** [MUST] WHEN asked about a part-prepayment THE SYSTEM SHALL check eligibility under the product rules (EMIs paid, minimum amount, yearly cap) and return charges plus the reduce-tenure and reduce-EMI outcomes.
- **REQ-QRY-001.5** [MUST] WHEN asked about foreclosure THE SYSTEM SHALL decide eligibility (lock-in), the charge (by rate type and loan age) and, if eligible, a payoff quote: principal + accrued interest + charge + GST.
- **REQ-QRY-001.6** [MUST] WHEN asked "what are my options" THE SYSTEM SHALL fetch loans, payments and policy in parallel and list the available options, degrading if a system is down.
- **REQ-QRY-001.7** [MUST] THE SYSTEM SHALL attach the policy clause ID to every eligibility or charge decision.

## Acceptance criteria (journeys from the brief)
- J1 Given Priya, the EMI rose from ₹17,112.63 to ₹17,323.49 (10.25% → 11.00%, clause PL-FL-3.4), and ₹50,000 prepaid with the same EMI saves ₹21,003.30 at no charge. → .2, .4, .7
- J2 Given Priya, the next EMI is ₹17,323.49 on 5 Dec, the last payment succeeded on 5 Nov, and ₹20,000 extra shortens the tenure. → .1, .3, .4
- J3 Given Priya, 3 failed debits in 6 months, the September one for insufficient funds. → .3
- J4 Given Priya, foreclosure is penalty-free (PL-FL-5.1); Rahul pays 4% plus GST (PL-FX-6.2); Meera is locked in (PL-FX-6.1). → .5, .7

## Non-goals
Free-form text-to-SQL; advice on whether to prepay.
