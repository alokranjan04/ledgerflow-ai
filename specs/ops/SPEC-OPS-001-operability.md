# SPEC-OPS-001: Operability — LLM resilience, health, limits, logs, evals
Status: implemented     Owner: platform     Version: 1.0

## Context
Running the assistant for real customers: the model provider will have outages and quota limits; Cloud
Run needs probes; abusive clients must not exhaust model quota; operators need correlated logs without
PII; prompt or model changes need a quality gate.

## Requirements
- **REQ-OPS-001.1** [MUST] IF the primary text model fails THEN THE SYSTEM SHALL retry on a fallback model, and SHALL skip a primary whose circuit breaker is open.
- **REQ-OPS-001.2** [MUST] THE SYSTEM SHALL expose liveness (`/healthz`) and readiness (`/readyz`: enterprise systems reachable, audit store writable) and report not-ready with HTTP 503.
- **REQ-OPS-001.3** [MUST] THE SYSTEM SHALL rate-limit chat turns and voice-session starts per session (token bucket) and tell the customer when a limit is hit.
- **REQ-OPS-001.4** [MUST] THE SYSTEM SHALL bound each text turn by an end-to-end time budget and answer with a clear message instead of hanging.
- **REQ-OPS-001.5** [MUST] THE SYSTEM SHALL log as structured JSON with severity and correlation fields (request ID, Cloud trace, hashed session, turn), never raw session IDs, unmasked phone numbers or conversation text.
- **REQ-OPS-001.6** [MUST] A golden-set evaluation SHALL measure tool selection, numeric exactness (values read from the same run's tool results), clause citations and safety against the real model, with pass bars, and every golden case SHALL reference tools of its workflow.
- **REQ-OPS-001.7** [SHOULD] CI SHALL run lint, tests with the traceability gate, dependency audit and secret scanning on every change, nightly evals, and deploy via Workload Identity Federation behind a protected environment. (Evidence: `.github/workflows/ci.yml`)
- **REQ-OPS-001.8** [MEASURED] Golden-set scores: numeric exactness 100%, tool selection ≥ 95%, citations ≥ 98%, safety 100%. Evidence: `reports/eval-*.md` (needs a Gemini key)
