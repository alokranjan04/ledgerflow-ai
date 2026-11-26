# SPEC-SEC-002: Web session handling and service perimeter
Status: implemented     Owner: platform     Version: 1.0

## Context
Production hardening after the PoC review. Two concrete risks were found:
1. Session IDs travelled in URLs (`?session=`), which end up in proxy and access logs and browser history.
2. The simulated enterprise systems shared the public port, so `/mock/crm/customers/<id>` returned
   customer records to anyone without authentication.

## Requirements
- **REQ-SEC-002.1** [MUST] THE SYSTEM SHALL carry the web session only in an `HttpOnly`, `SameSite=Strict` cookie (`Secure` over HTTPS); it SHALL NOT return the session ID in response bodies or accept it from the query string.
- **REQ-SEC-002.2** [MUST] THE SYSTEM SHALL serve the enterprise-system endpoints only to the service's own connectors (per-process internal token); external calls SHALL get 403.
- **REQ-SEC-002.3** [MUST] WHEN deployed (public URL set) THE SYSTEM SHALL refuse admin operations (fault injection, metrics reset) without a valid admin token, compared in constant time.
- **REQ-SEC-002.4** [MUST] THE SYSTEM SHALL send a Content-Security-Policy (self-only scripts, no framing by other origins), HSTS over HTTPS, a microphone-only Permissions-Policy, and `Cache-Control: no-store` on API responses.
- **REQ-SEC-002.5** [MUST] THE SYSTEM SHALL refuse WebSocket handshakes whose Origin differs from the service host (cross-site WebSocket hijacking).
- **REQ-SEC-002.6** [SHOULD] Deployed secrets (Gemini key, admin token, stream token) SHALL be stored in Secret Manager and mounted at runtime; only non-secret settings travel as plain environment variables. (Evidence: `scripts/deploy.sh`)

## Acceptance criteria
- Given a successful login, the response body has no session ID and `Set-Cookie` has `HttpOnly; SameSite=Strict`; a `?session=` query is rejected with 401. → .1
- Given an external `GET /mock/crm/customers/C1001`, the answer is 403. → .2
- Given `PUBLIC_URL` set and no token, `POST /api/faults` is 403; with the right `X-Admin-Token`, 200. → .3
- Given `Origin: https://evil.example`, `/ws/chat` refuses the handshake. → .5
