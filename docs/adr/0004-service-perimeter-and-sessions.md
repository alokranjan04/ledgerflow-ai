# ADR 0004: Cookie sessions and an internal-only perimeter for co-located systems

Status: accepted (2026-11-26)

## Context
The hardening review found session IDs in URLs (logged by proxies) and the simulated enterprise systems
reachable from the internet on the shared port.

## Decision
- Web sessions travel only in an `HttpOnly; Secure; SameSite=Strict` cookie. Scripts and integrations use
  an `X-Session` header. Query strings are never accepted.
- Co-located internal services require a per-process secret header, generated at startup and held only by the
  connectors. External callers get 403. Real systems will use IAM ID tokens or mTLS instead.
- Admin operations need a token, are refused outright when deployed without one, and compare in constant time.

## Consequences
- Session IDs no longer appear in Cloud Run request logs or browser history.
- Browser WebSockets carry the cookie automatically; the Origin check prevents cross-site hijacking.
- The demo's fault panel needs `?admin=<token>` on the deployed URL.
