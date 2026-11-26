# SPEC-SCL-001: Scale-out — shared state and service split
Status: implemented (deployment of the split topology pending approval of its cost)     Owner: platform     Version: 1.0

## Context
Constraint "architecture capable of evolving toward 1,000 concurrent customer conversations". The PoC ran
as one instance because sessions, limits and the simulated systems lived in process memory (ADR 0003).
This spec removes that limit without changing domain or runtime code. Decision record: ADR 0005.

## Requirements — shared state (REQ-SCL-001)
- **REQ-SCL-001.1** [MUST] WHEN `REDIS_URL` is set THE SYSTEM SHALL store sessions in Redis, so any instance can serve a session created by another and a restart loses none.
- **REQ-SCL-001.2** [MUST] A pending action proposed on one instance SHALL be confirmable on another, keeping the new-customer-turn, expiry and idempotency rules.
- **REQ-SCL-001.3** [MUST] WHILE an instance actively serves a session THE SYSTEM SHALL use that instance's live object, so a voice call's unsaved changes are not replaced by an older Redis copy.
- **REQ-SCL-001.4** [MUST] Chat and voice rate limits and the per-phone login lockout SHALL be enforced across all instances.
- **REQ-SCL-001.5** [MUST] THE SYSTEM SHALL persist the session after login, workflow switch, each chat turn and each voice session, so a customer can log in on one instance and continue on another.
- **REQ-SCL-001.6** [MUST] WHEN a session is dropped (logout) on any instance THE SYSTEM SHALL refuse it on every instance immediately, including copies cached on other instances.

## Requirements — service split (REQ-SCL-002)
- **REQ-SCL-002.1** [MUST] With `SERVICE_ROLE=systems` THE SYSTEM SHALL serve only the enterprise-system APIs (no customer UI or APIs), to callers holding the shared internal token.
- **REQ-SCL-002.2** [MUST] With `SERVICE_ROLE=gateway` THE SYSTEM SHALL serve the customer experience without co-located systems, reaching the systems service over HTTP, and SHALL proxy the operator fault-injection API to it.
- **REQ-SCL-002.3** [MUST] IF a split role starts without `INTERNAL_TOKEN` THEN THE SYSTEM SHALL refuse to start.
- **REQ-SCL-002.4** [SHOULD] WHEN `SYSTEMS_AUDIENCE` is set THE SYSTEM SHALL attach a Google-signed ID token (cached until shortly before expiry) to every systems call, so the systems service can be private (IAM invoker only).
- **REQ-SCL-002.5** [MUST] Every audit event SHALL also be emitted as a structured `audit` log entry (hashed session, customer, action, event, idempotency key, request ID), giving an instance-independent, durable trail through Cloud Logging sinks.
- **REQ-SCL-002.6** [SHOULD] A deploy script SHALL provision the split topology: Memorystore, a private systems service, and an autoscaled gateway with Direct VPC egress. (Evidence: `scripts/deploy_scaleout.sh`)

## Acceptance criteria
- Given instances A and B on one Redis: log in on A, chat on B, and A sees B's turn. → 001.1, 001.5
- Given a proposal saved on A and reloaded on B with a new customer turn, confirmation succeeds. → 001.2
- Given 3 chat turns per minute, the 4th turn across two instances is refused. → 001.4
- Given a logout on B, A's cached copy is refused on its next use. → 001.6
- Given a systems service and a gateway: `/mock/*` is 404 on the gateway and 403 without the token on the systems service; login and account work through the gateway; fault injection reaches the systems service. → 002.1, 002.2

## Non-goals
Cross-instance chat-turn locking (WebSocket session affinity keeps a customer's sockets on one instance);
durable SQL audit (Cloud Logging is the durable audit copy; Cloud SQL is the next roadmap item).
