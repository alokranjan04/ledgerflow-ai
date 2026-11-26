# LedgerFlow AI — Production and enterprise readiness

What is in place today, what was fixed in the hardening pass, and the ordered roadmap to running this for
a lender's real customers. Each control names the spec that tests it (`specs/`, `reports/traceability.md`).

## 1. Hardening pass: findings and fixes

| # | Finding (severity) | Fix | Spec |
|---|---|---|---|
| 1 | **High.** Simulated enterprise systems were reachable on the public URL: `GET /mock/crm/customers/C1001` returned a customer record without authentication | Mock sub-app accepts only calls carrying a per-process internal token, held only by the in-process connectors; external calls get 403 | SEC-002.2 |
| 2 | **High.** Session IDs in URLs (`?session=`) end up in Cloud Run request logs and browser history | HttpOnly, Secure, SameSite=Strict cookie; IDs never in bodies or URLs; API clients use the `X-Session` header | SEC-002.1 |
| 3 | **Medium.** Fault panel open to anyone on the deployed URL | Admin calls require `X-Admin-Token` (constant-time compare) and are refused when deployed without one; deploy generates a token | SEC-002.3 |
| 4 | **Medium.** Gemini key planned as a plain env var | Secret Manager, mounted at runtime; the service account has `secretAccessor` on those secrets only | SEC-002.6 |
| 5 | **Medium.** No CSP/HSTS; WebSockets accepted any Origin | CSP (self-only scripts, no third-party framing), HSTS, Permissions-Policy (mic only), `no-store` on APIs, Origin check on WebSockets | SEC-002.4/.5 |
| 6 | **Medium.** A model outage or quota exhaustion broke every text turn | Fallback model with per-model circuit breakers; end-to-end turn budget | OPS-001.1/.4 |
| 7 | **Medium.** No abuse limits on the expensive paths (model calls) | Token-bucket limits per session on chat turns and voice starts | OPS-001.3 |
| 8 | **Low.** Unstructured `print` logs, no correlation | JSON logs (Cloud Logging severity, trace link, request ID, hashed session, turn), no PII or transcripts | OPS-001.5 |
| 9 | **Low.** No readiness signal; Cloud Run could route to a cold instance | `/healthz` liveness and `/readyz` readiness (systems + audit store), wired as Cloud Run probes | OPS-001.2 |
| 10 | **Process.** No CI and no model-quality gate | GitHub Actions: lint → tests + traceability gate → pip-audit → gitleaks; nightly golden-set evals against the real model; WIF deploy behind a protected environment | OPS-001.6/.7 |

## 2. Controls in place (summary)

- **Identity and data:** verified identity before any data; session-scoped tools (the model never chooses the customer); lockouts per session and per phone; cross-customer records read as not found; workflow tool isolation.
- **Actions:** propose → new customer turn → confirm; session-bound, expiring tokens; idempotency keys end to end; append-only audit (enforced by DB triggers).
- **Accuracy:** deterministic finance and rules; a grounding gate for text with regenerate-then-template fallback; a grounding monitor for voice; golden-set evals with numeric exactness from live tool values.
- **Resilience:** per-system deadlines, retries for safe calls only, circuit breakers, bulkheads, TTL cache for reference data, degraded answers naming the missing system, LLM fallback.
- **Operations:** probes, JSON logs with correlation, `/api/metrics` p50/p95/p99 per connector, tool, LLM and voice hop, a per-turn trace in the UI, live fault injection.
- **Supply chain:** pinned major versions, pip-audit, secret scanning, a non-root container, the source build in Cloud Build, keyless runtime identity.

## 3. Roadmap to production (ordered by risk reduction)

| Phase | Work | Why | Effort |
|---|---|---|---|
| P1 ✅ | **Sessions, limits and lockouts → Memorystore (Redis)**: done in code (SPEC-SCL-001, ADR 0005). `SERVICE_ROLE=gateway` runs stateless and autoscaled; `SERVICE_ROLE=systems` runs the simulated systems as a private service with an ID token plus internal token. `scripts/deploy_scaleout.sh` provisions it (not yet run: cost approval) | Removes the single-instance limit; a lost instance loses no session | done |
| P1 | **Audit → Cloud SQL (Postgres)** with an insert-only role. Interim: every audit event is already a structured Cloud Logging entry (SPEC-SCL-002.5); add a log sink to a retention-locked bucket / BigQuery | Queryable, tamper-evident audit outside the app instance | 1–2 days |
| P1 | **Real systems behind connectors**: replace `/mock/*` with service URLs; mTLS or IAM-authenticated calls (ID tokens) per system; keep the internal-token pattern for anything co-located | Connectors already speak HTTP with typed errors, so this is configuration and auth | per system |
| P1 ✅ | **Step-up OTP before every write** (SPEC-SEC-003): hashed 6-digit code, 5-minute expiry, 3 attempts, 3 sends / 15 min, 10-minute validity, never seen by the model or logs. Remaining: a real SMS / WhatsApp adapter behind `SmsSender`, and the lender's IdP for app users | Possession factor on top of phone + DOB | adapter: 1 day |
| P2 ✅ | **Hindi and Hinglish** (SPEC-LNG-001): detection + language lock in text and voice, bilingual phone greeting, Hindi-aware grounding (Devanagari digits, Hindi 1–99, लाख/हज़ार/hazaar). Next languages follow the same pattern | Most borrowers prefer Hindi; accuracy unchanged | done |
| P2 ✅ | **KPI dashboard** (SPEC-KPI-001): in-app `/dashboard` + Cloud Monitoring log-based metrics and dashboard as code (`monitoring/`, `scripts/setup_monitoring.sh`), live in the project. Remaining: CSAT survey and cost-per-contact feeds from the pilot lender | The north-star KPI is visible to the lender, durable and summed across instances | done |
| P2 | **OpenTelemetry**: export the existing spans (turn → LLM → tool → connector) to Cloud Trace; SLO alerts on turn latency, LLM error rate, breaker-open events | Same span model as `telemetry.py`; swap the exporter | 1 day |
| P2 | **Data protection**: Cloud DLP (or a regex + NER pass) on transcripts before storage; retention policy (e.g. 90 days) with TTL deletes; CMEK on the bucket and DB | DPDP Act / RBI data-localisation expectations: keep data in asia-south1 | 2 days |
| P2 | **LLM governance**: prompt and workflow YAML versioning with canary (a % of sessions on the new version), eval gate before promotion, model allow-list, per-tenant quotas | Prompt changes are deploys; ship them like code | 2 days |
| P2 | **Voice gating option**: cascaded STT → text runtime (grounding gate) → streaming TTS path for workflows that need zero-tolerance numbers, with Gemini Live kept for low latency | Closes the voice grounding trade-off (ARCHITECTURE §3) | 3–4 days |
| P3 → done ✅ | **Human hand-off with context** (SPEC-HND-001): idempotent hand-off ticket with identity status and the last 12 turns, an agent desk (`/agent`), and the containment KPI at `/api/metrics`. Remaining: live transfer via Vobiz / Twilio and a contact-centre platform (Genesys, Twilio Flex) | Escalation customers trust; the north-star KPI is now measured | integration: 2 days |
| P3 | **Multi-region active/passive** (asia-south1 → asia-south2), Cloud Armor WAF + rate limits at the edge, load tests to 1,000 concurrent sessions with fake-LLM and sampled real-LLM profiles | Availability and abuse protection at scale | 3 days |

## 4. Capacity view for 1,000 concurrent conversations

- **Gateway:** stateless after P1. At about 250 concurrent WebSockets per 1 vCPU instance (voice is I/O-bound
  relay), roughly 4–6 instances with headroom. Autoscale on concurrent requests.
- **Model provider (the real limit):** 1,000 concurrent conversations means up to about 1,000 concurrent Live
  sessions for voice and roughly 50–150 requests/s of text generation at peak. This needs a provisioned quota
  increase and a second provider or region behind `GeminiREST`'s fallback chain.
- **Systems:** connector bulkheads (50 concurrent per system per instance) protect backends; tune them with
  the system owners' rate limits.
