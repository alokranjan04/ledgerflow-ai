# ADR 0005: Scale out with Redis-shared state and a gateway / systems split

Status: accepted (2026-11-26); supersedes ADR 0003 for production. Deployment awaits cost approval.

## Context
ADR 0003 kept everything in one process. To grow toward 1,000 concurrent conversations the gateway must
run as N instances, so state that must agree across instances has to leave the process: sessions
(identity, history, pending actions), rate limits and login lockouts. The simulated enterprise systems
hold mutable data (service requests), so they cannot be copied into every gateway instance either.

## Decision
1. **State in Redis (Memorystore)** behind the existing async `SessionStore` interface, selected by
   `REDIS_URL`. Each instance keeps a write-through cache of the sessions it is actively serving; while in
   use, that object is authoritative on that instance. WebSocket session affinity keeps a customer's chat
   and voice sockets together. Every cached lookup runs `EXPIRE` (one round trip), which refreshes the TTL
   and proves the session still exists, so a logout on any instance revokes it everywhere.
2. **Limits and lockouts in Redis**: fixed-window counters (`INCR` + `EXPIRE`) shared by all instances.
3. **One image, three roles** (`SERVICE_ROLE`): `all` (PoC default), `gateway` (stateless, autoscaled) and
   `systems` (simulated systems only, private). The gateway reaches the systems with a shared internal
   token plus a Google ID token for a private Cloud Run service (IAM invoker), the same pattern real
   systems would use.
4. **Durable audit via Cloud Logging**: every audit event is also a structured `audit` log entry, routable
   by a log sink to a retention-locked bucket or BigQuery. The local append-only table remains for the
   activity panel.

## Consequences
- The gateway scales horizontally; losing an instance loses no session.
- One extra network hop to Redis per request (sub-millisecond on the VPC).
- Two instances serving the same session at the same moment would be last-write-wins. Affinity makes this
  rare, and a per-session Redis lock is the upgrade if it matters.
- Cost: Memorystore Basic 1 GB (about US$35/month) plus additional instances, so `deploy_scaleout.sh` asks
  before creating anything.
