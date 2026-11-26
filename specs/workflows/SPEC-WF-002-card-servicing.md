# SPEC-WF-002: Credit-card servicing (workflow 2)
Status: implemented     Owner: cards     Version: 1.0

## Context
Challenge 5 demonstration: a second product on the same platform. Code: `workflows/card_servicing.yaml`,
`ledgerflow/domains/cards.py`.

## Requirements
- **REQ-CRD-001.1** [MUST] WHEN asked about the card statement THE SYSTEM SHALL return the statement balance, the minimum due (5% or ₹200, whichever is higher; clause CC-PL-4.1), the due date and the limits.
- **REQ-CRD-001.2** [MUST] WHEN listing transactions THE SYSTEM SHALL flag identical charges at the same merchant within 10 minutes as possible duplicates.
- **REQ-CRD-001.3** [MUST] THE SYSTEM SHALL dispute a transaction through the shared two-phase action service.
- **REQ-CRD-001.4** [MUST] IF a transaction is older than the 60-day dispute window THEN THE SYSTEM SHALL refuse the dispute and cite clause CC-PL-9.2.
