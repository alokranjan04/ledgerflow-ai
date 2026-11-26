# SPEC-HND-001: Human hand-off with context, and the containment KPI
Status: implemented (agent desk in the PoC; contact-centre platform integration pending)     Owner: platform     Version: 1.0

## Context
Customers must reach a person easily, and must not repeat themselves when they do. Hand-offs also define the
PRD's north-star and containment KPIs, which need counting at the source.

## Requirements
- **REQ-HND-001.1** [MUST] WHEN the customer asks for a person, is distressed, reports fraud or needs something out of scope THE SYSTEM SHALL create a hand-off request carrying the reason, urgency, channel, workflow, verification status, customer name (only if verified), masked mobile and the last 12 conversation turns.
- **REQ-HND-001.2** [MUST] Repeated hand-off requests in one session within 10 minutes SHALL resolve to the same request (idempotent), and count once.
- **REQ-HND-001.3** [MUST] An unverified customer SHALL still reach a human; the request is marked unverified and carries no account data.
- **REQ-HND-001.4** [MUST] An agent desk SHALL list open hand-offs oldest first with their context, and let an agent accept one (open → in progress); on a deployed service it requires the admin token.
- **REQ-HND-001.5** [MUST] THE SYSTEM SHALL count engaged conversations (first customer turn), hand-offs and executed actions, and report containment = 1 − hand-offs / engaged at `/api/metrics`.
- **REQ-HND-001.6** [SHOULD] On the phone channel the hand-off SHALL promise a call back on the registered mobile; a live transfer through the telephony provider is the production follow-up. (Evidence: `ledgerflow/platform/common_tools.py`)

## Acceptance criteria
- Given Priya mentions a duplicate bounce fee and asks twice for a person, one hand-off exists with her name, masked mobile, urgency "high" and that sentence in its transcript. → .1, .2
- Given an unverified phone caller, the hand-off is created with customer "UNVERIFIED" and an empty name. → .3
- Given 4 engaged sessions and 1 hand-off, containment = 0.75. → .5
