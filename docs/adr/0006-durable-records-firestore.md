# ADR 0006: Service requests and the audit trail are mirrored to Firestore

Status: accepted (2026-11-27). Implements SPEC-DUR-001.

## Context
The single-instance PoC (ADR 0003) re-seeds its SQLite database on every start. That is right for the
synthetic account data, but it also wiped every service request, hand-off and audit event on each redeploy:
a customer told "your foreclosure letter request SR-1A2B3C4D is raised" could find nothing the next day.
Options considered: Cloud SQL (Postgres), Firestore, or a GCS snapshot of the SQLite file.

## Decision
Mirror the two records that must survive (service requests and the audit trail) to **Firestore (native mode,
asia-south1)** and restore them into SQLite when a container starts. SQLite stays the working copy and query
engine; Firestore is the durable copy.

- Requests: written on create, escalate and accept, awaited before the API answers; idempotency keys travel
  with them, so a retried confirm after a redeploy still replays instead of duplicating.
- Audit: create-only documents keyed by nanosecond time (append-only, like the table's triggers); written
  asynchronously and drained on shutdown. Cloud Logging keeps its independent copy (ADR 0005).
- Firestore is called over REST with aiohttp and the runtime service account (`roles/datastore.user`),
  so no gRPC dependency and no key files.
- A Firestore outage never fails the customer: SQLite has the record, the failure is logged and counted.

## Why Firestore over Cloud SQL
Serverless and free at demo scale (no instance to pay for or patch), in the same region, and zero schema
work for two collections. Cloud SQL remains the target when the audit trail must be queried with SQL by
compliance, or when an insert-only database role is required; the `durable` interface is the seam.

## Consequences
- Requests and the activity panel survive redeploys and instance restarts.
- One Firestore write (tens of milliseconds) is added to each request change; reads are unaffected.
- With several gateway instances, each restores the audit trail at start; requests live in the systems
  service, which restores them once.
