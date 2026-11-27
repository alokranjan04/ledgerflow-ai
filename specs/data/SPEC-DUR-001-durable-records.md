# SPEC-DUR-001: Durable service requests and audit trail
Status: implemented     Owner: platform     Version: 1.0

## Context
The PoC keeps the simulated systems and the audit trail in SQLite inside the container. A redeploy or a
restarted instance therefore wiped every service request and hand-off the customer had been promised, and the
activity panel forgot every action. Requests and the audit trail are the records a bank cannot lose.
Decision: mirror them to Firestore and restore on start-up (ADR 0006). The synthetic account data is still
re-seeded from `data/customers.yaml` on purpose: it is deterministic demo data, not customer state.

## Requirements
- **REQ-DUR-001.1** [MUST] WHEN a service request is created, escalated or accepted THE SYSTEM SHALL write the full request (including its idempotency key) to the durable store before answering.
- **REQ-DUR-001.2** [MUST] WHEN the service starts on an empty database THE SYSTEM SHALL restore every stored request, so a request created before a redeploy can be read, listed, queued and escalated after it, and a replayed idempotency key still returns the original request instead of creating a second one.
- **REQ-DUR-001.3** [MUST] Every audit event SHALL be written to the durable store as a create-only document keyed by time, and restored in order into an empty audit table; restoring SHALL NOT remove the append-only triggers.
- **REQ-DUR-001.4** [MUST] IF the durable store is unreachable THEN THE SYSTEM SHALL still serve the customer from SQLite, log the failure and count it (`durable.write_failed`); a failed restore SHALL NOT stop the service from starting.
- **REQ-DUR-001.5** [MUST] Durability SHALL be off unless `DURABLE_STORE=firestore`, use keyless service-account credentials (no key files) and keep collections under a configurable prefix.
- **REQ-DUR-001.6** [SHOULD] Firestore SHALL be reached over its REST API, so the image needs no gRPC dependency.

## Acceptance criteria
- Given a request created on one instance, when a new instance starts with an empty database against the same store, then the customer sees the request and a retry with the same idempotency key replays it (DUR-001.1, .2).
- Given audit events written, when a new instance starts, then the activity panel shows them in the original order and UPDATE/DELETE are still refused (DUR-001.3).
- Given Firestore returning errors, then creating a request still returns 201 and `durable.write_failed` increases (DUR-001.4).

## Interfaces
Collections `<prefix>_tickets` (document ID = request ID) and `<prefix>_audit` (document ID = nanosecond time
plus a random suffix). Each document holds `json` (the SQLite row) plus `customer_id`, `status` and `type` when present.
Settings: `DURABLE_STORE` (none | firestore), `FIRESTORE_DATABASE` (default `(default)`), `FIRESTORE_PREFIX`.

## Non-functional
One Firestore write per request change (tens of milliseconds in-region). Audit writes are asynchronous and
drained on shutdown. The runtime service account needs only `roles/datastore.user`.

## Non-goals
Multi-region replication; Firestore as the query engine (SQLite stays the working copy); retention
policies (set by the bank's records schedule).

## Test mapping
Generated: see `reports/traceability.md`.
