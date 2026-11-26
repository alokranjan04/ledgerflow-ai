# SPEC-ACT-001: Two-phase authorised actions and audit
Status: implemented     Owner: platform     Version: 1.0

## Context
Challenge 4: execute meaningful service operations (payment dispute, foreclosure statement,
prepayment request, card dispute, callback) safely across text and voice. Code:
`ledgerflow/platform/actions.py`.

## Requirements
- **REQ-ACT-001.1** [MUST] WHEN an action is proposed THE SYSTEM SHALL return a confirmation ID and a human-readable summary and SHALL NOT change any system.
- **REQ-ACT-001.2** [MUST] THE SYSTEM SHALL execute a proposed action only after a new customer turn following the proposal (the model cannot propose and confirm in one turn).
- **REQ-ACT-001.3** [MUST] Confirmation IDs SHALL be bound to the session that proposed them.
- **REQ-ACT-001.4** [MUST] IF a confirmation is older than 5 minutes THEN THE SYSTEM SHALL reject it.
- **REQ-ACT-001.5** [MUST] WHEN a confirmed action is confirmed again THE SYSTEM SHALL return the original result and create no duplicate (idempotency key per confirmation).
- **REQ-ACT-001.6** [MUST] WHEN the customer declines THE SYSTEM SHALL cancel the pending action so it cannot be confirmed later.
- **REQ-ACT-001.7** [MUST] THE SYSTEM SHALL record proposal, execution, rejection, cancellation and failure in an append-only audit log (updates and deletes refused).
- **REQ-ACT-001.8** [SHOULD] IF execution fails because a system is unavailable THEN THE SYSTEM SHALL say so, keep the action pending, and allow a retry.

## Acceptance criteria
- Given a proposed foreclosure statement, no ticket exists until confirmation. → .1
- Given a confirm in the same turn, the result is "not executed"; in the next turn, a request ID; again, the same ID with `replayed`. → .2, .5
- Given the audit table, `DELETE` and `UPDATE` raise. → .7
