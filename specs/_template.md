# SPEC-<AREA>-<NNN>: <title>
Status: draft | approved | implemented     Owner:     Version:

## Context
Why this exists; which challenge or problem from the brief it serves.

## Requirements
EARS form, one line each, parsed by `scripts/trace_matrix.py`:

- **REQ-<AREA>-<NNN>.1** [MUST] WHEN <trigger> THE SYSTEM SHALL <response>.
- **REQ-<AREA>-<NNN>.2** [SHOULD] IF <unwanted condition> THEN THE SYSTEM SHALL <response>.
- **REQ-<AREA>-<NNN>.3** [MEASURED] THE SYSTEM SHALL <budget>. Evidence: `reports/...`

Priorities: **MUST** needs at least one passing test (CI fails otherwise). **SHOULD** is reported but
does not fail CI. **MEASURED** is shown by a benchmark report instead of a test.

## Acceptance criteria
Given / When / Then, each naming the REQ IDs it proves.

## Interfaces
Schemas, events, endpoints.

## Non-functional
Latency budget, security, observability.

## Non-goals

## Test mapping
Generated: see `reports/traceability.md` (tests carry `@pytest.mark.req("REQ-...")`).
