# SPEC-SEC-001: Identity, data scoping and web hardening
Status: implemented     Owner: platform     Version: 1.0

## Context
Constraint "appropriate controls for customer data and authorized actions". Code:
`ledgerflow/platform/common_tools.py`, `ledgerflow/platform/tools.py`, `app.py`.

## Requirements
- **REQ-SEC-001.1** [MUST] THE SYSTEM SHALL refuse every customer-data tool and endpoint until identity is verified (phone + date of birth), and SHALL reject unknown or expired sessions.
- **REQ-SEC-001.2** [MUST] IF verification fails 3 times in a session THEN THE SYSTEM SHALL lock verification for that session.
- **REQ-SEC-001.3** [MUST] IF web logins for one phone number fail 5 times in 15 minutes THEN THE SYSTEM SHALL refuse further logins for that number, even with correct details.
- **REQ-SEC-001.4** [MUST] THE SYSTEM SHALL take the customer ID only from the verified session: no tool declares a customer parameter, and one injected by the model SHALL be ignored.
- **REQ-SEC-001.5** [MUST] IF a request names a record belonging to another customer THEN THE SYSTEM SHALL treat it as not found.
- **REQ-SEC-001.6** [MUST] THE SYSTEM SHALL allow only the tools of the active workflow.
- **REQ-SEC-001.7** [SHOULD] THE SYSTEM SHALL mask phone numbers in logs.
- **REQ-SEC-001.8** [SHOULD] Workflow prompts SHALL tell the model that tool and policy data are reference material, not instructions (prompt-injection hygiene).
- **REQ-SEC-001.9** [SHOULD] THE SYSTEM SHALL send `X-Content-Type-Options`, `X-Frame-Options` and `Referrer-Policy` headers.

## Acceptance criteria
- Given an unverified session, `get_loan_summary` returns "Verify the customer's identity first". → .1
- Given Priya's session and Rahul's payment ID, the dispute is refused as not found. → .5
- Given the model sends `customer_id: C1002`, Priya's loan is returned. → .4
- Given the card workflow, `propose_prepayment_request` is not available. → .6

## Non-goals
Real OTP delivery; customer-facing account recovery.
