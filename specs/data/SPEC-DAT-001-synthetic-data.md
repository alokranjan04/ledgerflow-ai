# SPEC-DAT-001: Synthetic data and scenario customers
Status: implemented     Owner: platform     Version: 1.0

## Context
The brief allows synthetic data. The demo needs realistic volume plus named customers built for each
journey, and it must stay coherent on any day it is shown. Code: `ledgerflow/mocks/seed.py`.

## Requirements
- **REQ-DAT-001.1** [MUST] THE SYSTEM SHALL generate the same data for the same seed and as-of date (deterministic).
- **REQ-DAT-001.2** [MUST] THE SYSTEM SHALL provide named scenario customers: Priya (floating rate, EMI up after the latest reset, 3 failed debits in 6 months, a card), Rahul (fixed rate past the 12-EMI lock-in, under 24 months old), Meera (fixed rate inside the lock-in) and Ananya (a card with a duplicate charge).
- **REQ-DAT-001.3** [MUST] THE SYSTEM SHALL place all scenario dates relative to the as-of date, so every scenario holds on any demo day.
- **REQ-DAT-001.4** [SHOULD] THE SYSTEM SHALL add a background population of about 500 random customers so the systems feel populated. (Evidence: `ledgerflow/mocks/seed.py`)

## Acceptance criteria
- Given two seeds with the same date, the SQL dumps are identical. → .1
- Given as-of dates in Nov 2026, Mar 2027 and Aug 2027, Priya's latest EMI is higher than the previous one with 3 failures; Rahul has 12–23 EMIs paid; Meera has fewer than 12; Ananya has two identical Croma charges. → .2, .3

## Non-goals
Real customer data; data persistence across restarts (data is re-seeded on start).
