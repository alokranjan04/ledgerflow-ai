# SPEC-SEC-003: Step-up verification before write actions
Status: implemented (demo SMS sender; production gateway adapter pending)     Owner: platform     Version: 1.0

## Context
Mobile number + date of birth is knowledge-based authentication: enough to read an account, weak for
changing one. Before any write (dispute, statement, prepayment, service request) the customer proves
possession of the registered mobile with a one-time code. Roadmap item "step-up OTP" (PRODUCTION_READINESS P1).

## Requirements
- **REQ-SEC-003.1** [MUST] WHEN a customer confirms a proposed write without a valid step-up THE SYSTEM SHALL send a 6-digit one-time code to the registered mobile and SHALL NOT execute the action until the code is verified.
- **REQ-SEC-003.2** [MUST] THE SYSTEM SHALL keep the code out of everything the model sees; it is stored only as a salted hash and shown only on the customer's own channel (simulated SMS in the PoC).
- **REQ-SEC-003.3** [MUST] A code SHALL expire after 5 minutes and be cancelled after 3 wrong attempts.
- **REQ-SEC-003.4** [MUST] THE SYSTEM SHALL send at most 3 codes per session per 15 minutes.
- **REQ-SEC-003.5** [MUST] A verified step-up SHALL stay valid for a configurable window (default 10 minutes), after which the next write needs a new code.
- **REQ-SEC-003.6** [MUST] Codes SHALL never appear in logs; challenges and verification outcomes SHALL be audited.
- **REQ-SEC-003.7** [SHOULD] SMS delivery SHALL be pluggable (`SmsSender`): a demo sender for the PoC, an SMS or WhatsApp gateway in production. (Evidence: `ledgerflow/platform/stepup.py`)

## Acceptance criteria
- Given a proposal and a "yes", confirm answers `step_up_required` with the masked mobile, no ticket exists, and the tool result has no code; after `verify_otp` with the code (spoken with a pause), confirm executes. → .1, .2
- Given three wrong codes, the code is cancelled; a new confirm sends a fresh code, which fails if used after 301 seconds. → .3
- Given four confirms without verifying, the fourth sends nothing and says too many codes. → .4
- Given a verified step-up, a second write within 10 minutes executes directly; after 601 seconds it is challenged. → .5

## Non-goals
OTP for login (identity stays phone + DOB; the lender's IdP replaces both later). Redacting a used code from
the transcript: it is single-use and already burned.
