# SPEC-FIN-001: Loan calculations
Status: implemented     Owner: platform     Version: 1.0

## Context
Problem A: customers need precise, customer-specific numbers ("If I prepay ₹50,000, how much interest
would I save?"). Every figure the assistant states comes from this library, never from the model.
Code: `ledgerflow/finance/loans.py`.

## Requirements
- **REQ-FIN-001.1** [MUST] THE SYSTEM SHALL compute the EMI with the standard annuity formula in `Decimal` and round half-up to the paisa.
- **REQ-FIN-001.2** [MUST] WHEN an amortisation schedule is built THE SYSTEM SHALL make the principal components sum exactly to the sanctioned amount, with a closing balance of zero and never negative.
- **REQ-FIN-001.3** [MUST] WHEN the interest rate resets THE SYSTEM SHALL apply the new rate from the period starting after the reset date and recompute the EMI over the remaining tenure (tenure unchanged).
- **REQ-FIN-001.4** [MUST] WHEN a part-prepayment is simulated THE SYSTEM SHALL compare "reduce tenure" (same EMI) with "reduce EMI" (same tenure), report the interest saved by each, and never show a larger prepayment saving less interest.
- **REQ-FIN-001.5** [MUST] THE SYSTEM SHALL compute broken-period interest for foreclosure as simple daily interest, actual/365.
- **REQ-FIN-001.6** [MUST] THE SYSTEM SHALL format rupee amounts with Indian digit grouping (₹12,34,567.50), dropping paise for whole rupees.
- **REQ-FIN-001.7** [MUST] WHEN adding months to a date THE SYSTEM SHALL clamp to the last day of shorter months.
- **REQ-FIN-001.8** [SHOULD] THE SYSTEM SHALL fold a rounding residue (< 2% of an EMI) into the final instalment instead of adding an extra month.

## Acceptance criteria
- Given ₹1,00,000 at 12% for 12 months, the EMI is ₹8,884.88; ₹8,00,000 at 10.5% for 60 months gives ₹17,195.12 (spreadsheet PMT). → .1
- Given random principal, rate and tenure (property test), the schedule closes at zero and its principal sums to the sanctioned amount. → .2
- Given a reset from 10.50% to 11.00% effective 20 Sep, the instalment due 5 Oct still uses 10.50% and the one due 5 Nov uses 11.00%, with the EMI recomputed on the remaining balance. → .3
- Given two prepayment amounts (property test), the larger one never leaves more interest, and reduce-tenure saves at least as much as reduce-EMI. → .4

## Non-goals
Tax treatment, insurance premiums, moratoriums.

## Test mapping
Generated: `reports/traceability.md`.
