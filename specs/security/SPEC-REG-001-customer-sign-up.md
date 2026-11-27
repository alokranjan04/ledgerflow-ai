# SPEC-REG-001: Customer sign-up with mobile verification
Status: implemented     Owner: platform     Version: 1.0

## Context
New people could only be added by editing a file. A sign-up form lets anyone try the assistant on their own
(synthetic) accounts. Registration must prove the mobile number belongs to the applicant, and must not let
someone discover which numbers are already customers.

## Requirements
- **REQ-REG-001.1** [MUST] The sign-up form SHALL collect name, mobile, date of birth, city, optional email, employment, consent, and at least one demo account (a personal loan from the loan products with amount, tenure and EMIs paid, and/or a credit card from the card products with a limit); the product choices come from the live product list.
- **REQ-REG-001.2** [MUST] THE SYSTEM SHALL create the customer only after a correct 6-digit code sent to that mobile (hashed, 5-minute expiry, 3 attempts), then sign the customer in to the matching workflow.
- **REQ-REG-001.3** [MUST] Whether a mobile number is already registered SHALL be revealed only after its code is verified; the applicant is then told to sign in instead.
- **REQ-REG-001.4** [MUST] Every form problem (consent, age 18-100, mobile format, amount ₹50,000-₹40,00,000, offered tenures and limits, product kinds) SHALL be listed before any code is sent.
- **REQ-REG-001.5** [MUST] THE SYSTEM SHALL send at most 3 codes per mobile and accept at most 5 applications per client in 15 minutes.
- **REQ-REG-001.6** [MUST] Sign-up SHALL work in a browser end to end, through to an assistant answer on the new account, with no serious or critical accessibility violations.

## Non-goals (production)
Real KYC (PAN, Aadhaar e-KYC, video KYC), credit checks and account opening: sign-up here creates a demo
profile with synthetic accounts. A real SMS gateway replaces the on-screen simulated SMS (`SMS_PROVIDER`).
Pending sign-ups live in the instance's memory; with several instances they move to Redis like sessions.

## Test mapping
Generated: see `reports/traceability.md`.
