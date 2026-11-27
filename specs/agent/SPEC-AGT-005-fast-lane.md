# SPEC-AGT-005: Fast lane and model-outage fallback
Status: implemented     Owner: platform     Version: 1.0

## Context
Build plan v2 set a text fast-lane SLO (full answer p50 < 400 ms) and required degraded answers when a
dependency fails. Simple questions need one lookup and one sentence; paying 2-3 model calls for them adds
seconds and makes the assistant useless during a model outage. Code: `ledgerflow/platform/fastlane.py`.

## Requirements
- **REQ-AGT-005.1** [MUST] WHEN a verified customer asks one simple question (next EMI, outstanding, EMI change, last payment, failed payments, foreclosure, options, card statement, card transactions) in English, Hindi or Hinglish THE SYSTEM SHALL answer it from the systems with no model call.
- **REQ-AGT-005.2** [MUST] THE SYSTEM SHALL leave to the model every message with an amount, an action (dispute, prepay, cancel, agent), a what-if, more than one intent or clause, or an unrecognised intent.
- **REQ-AGT-005.3** [MUST] Fast-lane answers SHALL be templates in the session's language that copy figures and clause IDs from tool results (grounded by construction, still checked), show the same cards, and record the same agent steps.
- **REQ-AGT-005.4** [MUST] WHEN the model is unavailable or over budget THE SYSTEM SHALL answer every recognised intent (up to two) from the systems with a note that the assistant service is degraded, and say so plainly when nothing is recognised.
- **REQ-AGT-005.5** [MUST] WHILE an action awaits the customer's confirmation THE SYSTEM SHALL route turns to the model, which keeps the pending request in view.
- **REQ-AGT-005.6** [MEASURED] Fast-lane turn latency p50 < 400 ms on the deployed service. Evidence: `/api/metrics` `turn.text.fast_lane`
