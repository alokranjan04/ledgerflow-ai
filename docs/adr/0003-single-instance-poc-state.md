# ADR 0003: In-process state on one Cloud Run instance for the PoC

Status: accepted for the PoC; superseded at production phase P1 (docs/PRODUCTION_READINESS.md)

## Context
The PoC needs a working cloud deployment at near-zero cost. Sessions, pending actions, rate-limit buckets,
synthetic systems (SQLite) and the audit log all live in the process.

## Decision
Run one always-on instance (`--min-instances=1 --max-instances=1`, WebSocket session affinity). Keep the
seams explicit: `SessionStore`, `RateLimiter`, `AuditLog` and `ConnectorRegistry` are the interfaces a
Redis / Cloud SQL / real-system implementation replaces.

## Consequences
- A redeploy resets sessions, synthetic data and the audit log.
- No horizontal scale yet: capacity was measured at 200 concurrent sessions locally and 100 on Cloud Run,
  with 0 errors (`reports/`).
- Moving to Redis + Cloud SQL changes no domain or runtime code.
