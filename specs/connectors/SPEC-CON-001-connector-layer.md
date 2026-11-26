# SPEC-CON-001: Connector layer and resilience
Status: implemented     Owner: platform     Version: 1.0

## Context
Challenges 3 and 4: stay responsive and correct when backends are slow or failing. Every system call
goes through one reusable connector. Code: `ledgerflow/platform/connectors.py`.

## Requirements
- **REQ-CON-001.1** [MUST] THE SYSTEM SHALL enforce a per-system deadline on every call and raise a typed `Timeout` when it passes.
- **REQ-CON-001.2** [MUST] THE SYSTEM SHALL retry with exponential backoff and jitter only for reads, or for writes carrying an Idempotency-Key; other writes SHALL be attempted once.
- **REQ-CON-001.3** [MUST] THE SYSTEM SHALL keep a circuit breaker per system (closed → open after N failed calls, after retries → half-open with a single trial call after a cooldown → closed on success), and SHALL fail fast with `CircuitOpen` while it is open.
- **REQ-CON-001.4** [SHOULD] IF a system answers with a client error (4xx) THEN THE SYSTEM SHALL NOT count it towards the breaker.
- **REQ-CON-001.5** [SHOULD] THE SYSTEM SHALL cache reference data (policy) with a TTL, and SHALL NOT cache balances or payments.
- **REQ-CON-001.6** [MUST] WHEN a connector fails THE SYSTEM SHALL return a structured "unavailable" tool result naming the system, never an unhandled exception, and composite answers SHALL degrade to the parts that succeeded.

## Acceptance criteria
- Given a 100 ms deadline and a 400 ms system, the call raises `Timeout`. → .1
- Given a 30% error rate, at least 85 of 100 two-call lookups succeed; given a failing non-idempotent POST, exactly one attempt is made. → .2
- Given loans down, after 5 failed calls the breaker is open and the next call fails fast; after the cooldown, with loans up, it closes again. → .3
- Given 10 not-found responses, the breaker stays closed. → .4

## Non-functional
Connector overhead under 5 ms p50 on top of system latency (see platform benchmark).
