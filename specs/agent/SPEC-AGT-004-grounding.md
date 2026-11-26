# SPEC-AGT-004: Grounding verifier
Status: implemented     Owner: platform     Version: 1.0

## Context
Constraint "customer-specific, verifiable answers". Material numbers in replies must trace to tool
outputs. Code: `ledgerflow/platform/grounding.py`.

## Requirements
- **REQ-AGT-004.1** [MUST] THE SYSTEM SHALL extract numbers written with ₹/Rs, Indian or Western grouping, lakh/crore/k suffixes, percentages, and spoken English number words.
- **REQ-AGT-004.2** [MUST] THE SYSTEM SHALL accept a number that equals a tool value, rounds one (±0.6% or ₹1), is the sum or difference of two tool values, or came from the customer.
- **REQ-AGT-004.3** [MUST] THE SYSTEM SHALL ignore dates, years and identifiers (e.g. P-10001-018F).
- **REQ-AGT-004.4** [MUST] WHEN a text answer contains an unsupported figure THE SYSTEM SHALL regenerate once with feedback, and if it still fails SHALL reply with a templated answer built only from tool values.
- **REQ-AGT-004.5** [SHOULD] WHEN a voice answer has been spoken THE SYSTEM SHALL check its transcript, count unsupported figures and flag them in the UI (a monitor, since the audio is already out).
- **REQ-AGT-004.6** [MUST] THE SYSTEM SHALL flag unsupported money, percentages, counts of months/EMIs/days and any number of 100 or more.

## Non-goals
Checking non-numeric claims (left to tool-first prompting and evals).
