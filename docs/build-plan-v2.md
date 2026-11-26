# Loan-Servicing AI Platform — Spec-Driven Build Plan (v2)

Supersedes: *Loan-servicing AI PoC: architecture and build plan* (Nov 12, 2026).
Source requirements: *FDE Leader — Enterprise AI Engineering Challenge* (`docs/`).

---

## 0. Review of the v1 plan

### Keep (the v1 design is right on these)
| v1 decision | Why it stays |
|---|---|
| LLM orchestrates and phrases; deterministic tools produce every fact and number | Directly answers "not an LLM + vector DB" and "verifiable answers" |
| Grounding check on numbers in the reply | Makes accuracy measurable and enforceable |
| Semantic layer (named metrics) instead of text-to-SQL | Safer, faster, one function + one registry line per new question |
| Identity before data; tools scoped to the verified customer | Security constraint in the brief |
| Two-phase actions: propose → confirm → execute with idempotency + audit | Required for "authorized actions" |
| Config-driven agent runtime + connector/tool registry | Challenge 5 and the live change request use the same mechanism |
| Connectors with timeout, retry, circuit breaker, cache, parallel fan-out, fillers | Challenge 3/4 "slow and failing backends" |
| Policy as versioned rules with cited clauses | Precise, explainable eligibility answers |
| Provider interface with a deterministic fake LLM | Makes tests and CI deterministic and offline |

### Gaps and fixes
| # | Gap in v1 | Fix in v2 |
|---|---|---|
| G1 | No written specs or acceptance criteria. "Done" is defined by activity rather than verifiable behaviour | Spec-first workflow (§1): every feature starts as a spec with EARS requirements and Given/When/Then acceptance criteria, with traceability enforced in CI |
| G2 | Eval set mentioned (50–100 questions) but no metrics, thresholds, or gating | Eval framework with named metrics, pass bars, and a CI gate (§6) |
| G3 | Second workflow is inconsistent (credit cards in the table and journey 5, collections in decision 5 and day 4) | **Credit-card servicing** is the second workflow (the brief names credit cards first). Outbound collections is rehearsed as a live change request |
| G4 | Grounding check not specified: "₹50,000" vs "50000" vs "fifty thousand", lakh notation, rounded and derived numbers | Grounding spec with a normalisation and derivation whitelist (SPEC-AGT-004) |
| G5 | Customer ID could flow through LLM tool arguments | Tools receive `customer_id` from the authenticated session context only and never from model output (SPEC-SEC-002) |
| G6 | Voice relies on the browser Web Speech API. Barge-in and latency control are weak, and the API is Chrome-only | Server-side streaming STT/TTS behind an interface; Web Speech is only the offline fallback. Explicit turn-taking state machine (SPEC-VOX-001) |
| G7 | Barge-in cancellation semantics undefined for write actions | Read work is cancelled; confirmed writes always complete and are reported (SPEC-VOX-003) |
| G8 | Security, observability and CI are left to the final write-up | Each is a phase with tests: threat model, OWASP checks, OTel tracing, CI pipeline from Phase 0 |
| G9 | SQLite + in-memory sessions only | Postgres + Redis in docker-compose (sessions, idempotency keys, rate limits); SQLite only in unit tests |
| G10 | Go edge + Python brain adds a network hop on the voice path, two toolchains, and two test stacks | **Recommendation:** Python-only backend (FastAPI/asyncio handles WebSockets and audio fine). See Decision D1 |
| G11 | Money handled implicitly | `Decimal` everywhere, rounding rules specified (half-up to paise), property-based tests |
| G12 | Load test with real LLM confounds platform vs model latency | Two load profiles: fake LLM (platform capacity) and a sampled real-LLM run (end-to-end latency) |

---

## 1. Spec-driven development workflow

Every unit of work follows the same loop. Nothing is merged without passing every step.

```
 1. SPEC     specs/<area>/SPEC-XXX-NNN.md  (requirements + acceptance criteria)
 2. REVIEW   spec reviewed, open questions closed, status: approved
 3. TESTS    acceptance tests written first, tagged with requirement IDs (failing)
 4. BUILD    smallest implementation that makes them pass
 5. VERIFY   unit + integration + eval + latency checks green in CI
 6. TRACE    traceability matrix regenerated; every MUST requirement has ≥1 passing test
```

### Spec template (`specs/_template.md`)
```markdown
# SPEC-<AREA>-<NNN>: <title>
Status: draft | approved | implemented     Owner:     Version:
## Context            — why; which challenge/problem it serves
## Requirements       — EARS form, each with an ID
   REQ-<AREA>-<NNN>.1  WHEN <trigger> THE SYSTEM SHALL <response>.   [MUST]
   REQ-<AREA>-<NNN>.2  IF <unwanted condition> THEN THE SYSTEM SHALL ... [MUST]
## Acceptance criteria — Given / When / Then, each mapped to REQ IDs
## Interfaces         — schemas, events, endpoints (link to OpenAPI / JSON Schema)
## Non-functional     — latency budget, security, observability
## Non-goals
## Test mapping       — test file::name ↔ REQ ID
```

### Traceability
- Tests carry markers: `@pytest.mark.req("REQ-ACT-001.2")` (Vitest/Playwright use a tag in the test title).
- `scripts/trace_matrix.py` parses specs and test reports and writes `reports/traceability.md`. **CI fails if any MUST requirement has no passing test.**
- Architecture decisions are recorded as ADRs in `docs/adr/NNNN-title.md`.

---

## 2. Target architecture

```
Browser (React + TS + Vite)
  chat · voice (AudioWorklet mic, VAD, playback) · account cards · action confirm cards
  · live trace/latency panel · demo fault-injection panel
        │  WebSocket  (versioned event protocol, JWT session)
        ▼
Gateway (FastAPI, async, stateless — N replicas)
  ├─ Auth & identity: OTP/DOB verification → session JWT (customer_id, scopes)
  ├─ Voice pipeline: streaming STT ⇄ turn manager (state machine, barge-in) ⇄ sentence-streamed TTS
  ├─ Agent runtime  ◄── PLATFORM CAPABILITY (workflow YAML)
  │     fast-lane intent router → planner LLM (tool calling, parallel) → executor
  │     (budgets, cancellation) → grounding verifier → composer (text + UI cards + citations)
  ├─ Tool registry   typed tools: read tools · calculators · policy rules · actions
  ├─ Query engine    semantic layer · finance lib (Decimal) · policy rules engine · clause retrieval
  ├─ Action service  propose → confirm token → execute (idempotency key) → audit log
  ├─ Connector layer timeout · retry+jitter · circuit breaker · bulkhead · TTL cache · OTel spans
  └─ LLM provider    Anthropic/OpenRouter adapter · scripted fake (tests) · model routing
        │
        ▼  HTTP (OpenAPI contracts)
Mock enterprise systems (separate FastAPI processes, each with latency profile + fault injection)
  CRM · Loan Mgmt · Payments · Policy Knowledge · Service Ops · (Cards — workflow 2)

State: Postgres (systems of record, audit) · Redis (sessions, pending actions, idempotency, rate limits)
Telemetry: OpenTelemetry → Jaeger (traces) + Prometheus/Grafana (metrics) — compose profile "obs"
```

**Model routing.** Use `claude-haiku-5-5` for routing, the fast lane and phrasing. Use `claude-sonnet-5-5` for multi-tool planning turns. Both sit behind `LLMProvider`. A `ScriptedLLM` stands in for all deterministic tests.

---

## 3. Repository layout

```
/
├─ specs/                     # source of truth (§1)
│   ├─ _template.md
│   ├─ platform/  data/  systems/  connectors/  query/  agent/  actions/
│   ├─ security/  voice/  ui/  workflows/  nfr/
├─ docs/  adr/  ARCHITECTURE.md  LATENCY_REPORT.md  DEMO_SCRIPT.md  THREAT_MODEL.md  RUNBOOK.md
├─ backend/
│   ├─ platform/              # reusable: runtime, registry, connectors, actions, identity, voice, llm, telemetry
│   ├─ domains/loans/         # loan tools, semantic metrics, policy rules
│   ├─ domains/cards/         # workflow 2 (new code kept minimal and measured)
│   ├─ finance/               # pure Decimal finance library
│   ├─ gateway/               # FastAPI app, WS protocol
│   └─ tests/  unit/ integration/ contract/ chaos/ security/
├─ mock_systems/  crm/ loans/ payments/ policy/ service_ops/ cards/  + common latency/fault middleware
├─ workflows/  loan_servicing.yaml  card_servicing.yaml
├─ data/  seed generator + scenario fixtures
├─ frontend/  src/  tests/ (vitest)  e2e/ (playwright)
├─ evals/  golden/*.yaml  multi_turn/*.yaml  runner/  reports/
├─ load/  k6 or locust WS scenarios
├─ scripts/  trace_matrix.py  seed.py  demo_reset.py
├─ docker-compose.yml  (profiles: core, obs, load)
├─ .github/workflows/ci.yml
└─ Makefile   (make up | test | eval | load | trace | demo)
```

---

## 4. Phased build plan

Each phase lists the **specs to write first**, the **build scope**, the **tests**, and an **exit gate**. A phase is done only when its exit gate passes in CI.

### Phase 0: Foundations
- **Specs:** SPEC-PLT-001 Repo & toolchain, SPEC-PLT-002 CI pipeline, SPEC-PLT-003 Config & secrets.
- **Build:** monorepo skeleton; `uv` + ruff + mypy (strict on `platform/`, `finance/`), pytest; pnpm + TS strict + eslint + vitest + playwright; pre-commit; docker-compose (Postgres, Redis, gateway, mocks, frontend); `.env.example` with secrets only from env; Makefile; spec + ADR templates; `trace_matrix.py`.
- **CI:** lint → typecheck → unit → integration (compose) → e2e (fake LLM) → eval (fake LLM) → traceability → build images. Add gitleaks secret scanning and pip-audit / `pnpm audit` dependency checks.
- **Exit gate:** `make up` starts everything with one command, CI is green on the skeleton, and the traceability report generates.

### Phase 1: Domain model, synthetic data, finance library
- **Specs:** SPEC-DAT-001 Data model, SPEC-DAT-002 Seed scenarios, SPEC-FIN-001 Finance calculations (EMI, amortisation, prepayment reduce-EMI vs reduce-tenure, foreclosure quote, late fees, interest-paid-in-FY, floating-rate reset).
- **Build:**
  - Entities: customers, products, loans, rate_history, schedules, payments, payment_failures, tickets, actions, audit_log, plus cards for Phase 8.
  - A deterministic seed generator (fixed seed, about 2,000 customers) plus **named scenario customers**, each engineered for one journey:
    - `C-RATE-RESET`: EMI went up after a repo-linked reset
    - `C-FAILED-PAY`: 3 failed debits in 6 months, one in September
    - `C-FORECLOSE-OK`: past the lock-in, penalty-free
    - `C-FORECLOSE-PENALTY`: inside the lock-in
    - `C-CARD`: a card holder with a disputed transaction
- **Tests:**
  - Table-driven golden values cross-checked against spreadsheet PMT/IPMT.
  - Hypothesis property tests: Σprincipal = sanctioned amount; balance never negative; larger prepayment never increases total interest; reduce-tenure saves ≥ reduce-EMI interest.
  - Rounding tests (half-up to paise).
  - Seed determinism test: same seed gives the same hash.
- **Exit gate:** 100% branch coverage on `finance/`, and every scenario customer is asserted by a fixture test.

### Phase 2: Mock enterprise systems
- **Specs:** SPEC-SYS-001…005 (one per system, each with an OpenAPI 3.1 contract written **before** code), SPEC-SYS-010 Latency & fault-injection model.
- **Build:**
  - Five FastAPI services sharing one middleware with a per-endpoint latency distribution (log-normal, configurable p50/p95) and faults (error rate, 503, timeout, slow tail, malformed payload).
  - An admin API `/admin/faults` for runtime toggles, used by the demo panel.
  - Write endpoints require an `Idempotency-Key` header.
  - Service Ops implements a ticket state machine (open → in_progress → resolved/escalated).
- **Tests:**
  - Schemathesis contract tests against each OpenAPI.
  - Latency-distribution tests: measured p50/p95 within tolerance.
  - Fault-rate test.
  - Idempotency replay test: the same key returns the same response and creates no duplicate.
- **Exit gate:** all contracts pass, and the fault toggles work at runtime.

### Phase 3: Connector layer (platform)
- **Specs:** SPEC-CON-001 Connector contract & error taxonomy (`Timeout`, `Unavailable`, `CircuitOpen`, `NotFound`, `Forbidden`, `Invalid`), SPEC-CON-002 Resilience policy, SPEC-CON-003 Caching.
- **Build:**
  - A `BaseConnector` with per-call deadline from the turn budget.
  - Retries with exponential backoff + jitter, for **idempotent/GET calls only, or writes carrying an idempotency key**.
  - Circuit breaker (closed/open/half-open), bulkhead semaphore per system, and a TTL cache for reference data (policies, products) but **not** for balances.
  - Typed Pydantic responses, an OTel span per call, and a connector registry loaded from config.
- **Tests:**
  - Breaker state-machine unit tests.
  - Retry/backoff timing with a fake clock.
  - Chaos suite against the mocks at 30% error rate, checking typed errors and no unhandled exceptions.
  - Cancellation test: a cancelled turn leaves no orphan tasks.
- **Exit gate:** chaos suite green, and p50 overhead per connector call under 5 ms.

### Phase 4: Query engine (semantic layer, policy rules, tools)
- **Specs:**
  - SPEC-QRY-001 Semantic layer catalogue (`outstanding_principal`, `next_due`, `emi_history`, `rate_history`, `dpd`, `failed_payments(window)`, `payoff_quote(date)`, `interest_paid(fy)`).
  - SPEC-QRY-002 Policy rules engine (versioned YAML rules, effective dates, clause IDs, product scoping).
  - SPEC-QRY-003 Tool spec format (name, description, JSON-schema input, output schema, scopes, side-effect class `read|calc|write`, timeout budget, optional fast-lane template).
  - SPEC-QRY-004 Policy clause retrieval for open-ended wording (BM25 + embeddings over clauses, citations required).
- **Build:** tool registry, loan read tools, calculator tools, eligibility tools (prepayment, foreclosure penalty, EMI-date change), and `explain_emi_change` that joins rate_history with schedules.
- **Tests:**
  - Each tool runs against every scenario customer.
  - Rule-engine decision tables, including boundary dates such as the last day of the lock-in.
  - A retrieval test that the right clause appears in the top 3 for 30 policy questions.
- **Exit gate:** all tools return correct values for the scenario customers, and every eligibility answer carries a clause ID.

### Phase 5: Identity, security baseline, agent runtime, actions
- **Specs:**
  - SPEC-SEC-001 Verification (phone + DOB or mock OTP, attempt limits, lockout), SPEC-SEC-002 Data scoping, SPEC-SEC-003 Prompt-injection handling, SPEC-SEC-004 PII handling (masking in logs, traces, LLM context minimisation).
  - SPEC-AGT-001 Workflow YAML schema, SPEC-AGT-002 Turn loop & budgets, SPEC-AGT-003 Conversation state & entity memory, SPEC-AGT-004 Grounding verifier, SPEC-AGT-005 Fast lane, SPEC-AGT-006 Degraded answers when systems fail.
  - SPEC-ACT-001 Two-phase actions, SPEC-ACT-002 Audit log.
- **Build:**
  - Session JWT. **Tools get `customer_id` from the session context; the LLM's tool schema does not contain it.**
  - Turn loop with a per-turn latency budget, parallel tool calls, and an async cancellation scope.
  - Entity memory ("the September one" resolves to a payment ID; "my loan" resolves to the active loan).
  - Grounding verifier:
    - Extract numbers and normalise them (₹, commas, lakh/crore, %, words).
    - Accept a number only if it matches a tool output value, or a whitelisted derivation of tool values (difference, sum, rounding).
    - On failure, regenerate once with feedback. If it fails again, fall back to a templated answer, and log the event.
  - Actions:
    - `propose_action` returns a confirmation card with a token bound to session, action hash and expiry.
    - Execute only with a valid token and an explicit user confirmation.
    - Each execute carries an idempotency key and writes to an append-only audit log.
  - Actions to implement: raise payment dispute, request foreclosure statement, initiate prepayment request, create servicing ticket / escalate to human.
- **Tests:**
  - ScriptedLLM integration tests for each journey.
  - Security suite:
    - Cross-customer access attempts.
    - Injection planted in a ticket description and in a policy text ("ignore instructions…").
    - Executing an action without confirmation, with a replayed token, or with an expired token.
    - OTP brute force.
  - Grounding tests: a hallucinated number gets caught; a legitimate derived number passes.
  - Degraded-mode test: Payments down still produces a partial answer, names the missing source, and offers a ticket.
- **Exit gate:** security suite at 100%, zero cross-customer leaks, and every action is audited.

### Phase 6: Text UI and WebSocket protocol
- **Specs:** SPEC-UI-001 WS event protocol (versioned: `user_text`, `assistant_delta`, `tool_started/finished`, `card`, `action_proposed/confirmed/result`, `trace`, `error`, `interrupt`), SPEC-UI-002 Screens & cards, SPEC-UI-003 Accessibility (WCAG 2.1 AA basics).
- **Build:**
  - Login/verify, chat with streaming tokens, and account cards (loan summary, EMI history chart, payments table, prepayment comparison, confirmation card).
  - A trace panel showing per-span latency, and a fault-injection panel for the demo.
  - Reconnect with session resume.
- **Tests:**
  - Vitest component tests.
  - Protocol schema tests, so frontend and backend share JSON Schemas.
  - Playwright E2E for journeys 1, 3 and 4 (fake LLM).
  - axe accessibility scan.
- **Exit gate:** all 3 text journeys pass E2E with no manual steps.

### Phase 7: Real-time voice
- **Specs:**
  - SPEC-VOX-001 Turn-taking state machine (`IDLE → LISTENING → THINKING → SPEAKING`, with `INTERRUPTED`).
  - SPEC-VOX-002 Audio transport (16 kHz PCM frames over WS, AudioWorklet capture).
  - SPEC-VOX-003 Barge-in semantics: client VAD stops playback within 150 ms, and the server cancels LLM, TTS and read tools. **Confirmed writes are never cancelled** and are reported once they finish.
  - SPEC-VOX-004 Voice style (short sentences, rupee amounts in Indian format, read back numbers before confirming an action, verbal confirmation phrase).
  - SPEC-VOX-005 Progress fillers (after 700 ms with no first token, and never more than one per 3 s).
- **Build:**
  - `STTProvider` / `TTSProvider` interfaces. One hosted streaming provider for each (see D3), with Web Speech as the offline fallback.
  - Sentence-chunked TTS streaming.
  - The same agent runtime and session as text, so a user can start in text and continue in voice without losing context.
- **Tests:**
  - State-machine unit tests, including interruption in every state.
  - A replay harness feeding recorded WAV fixtures and scripted transcripts with timings to assert event order and cancellation.
  - A barge-in latency test.
  - Playwright voice smoke with a fake audio device (Chromium `--use-file-for-fake-audio-capture`).
- **Exit gate:**
  - Journey 2 (next EMI, then "actually, last payment", then the ₹20,000 tenure question) passes in the replay harness.
  - End-of-speech to first audio p50 is under 1.0 s against the mocks, measured.

### Phase 8: Second workflow (credit-card servicing) to prove the platform
- **Specs:** SPEC-WF-002 Card servicing workflow, SPEC-SYS-006 Cards mock system.
- **Build:**
  - `workflows/card_servicing.yaml`.
  - A cards mock system and connector.
  - 3 tools: statement due, minimum due, dispute transaction. The dispute reuses the generic action service.
  - **No changes to `backend/platform/`** (enforced by CI diff check on that PR).
- **Tests:** journey 5 E2E (text and voice); a workflow-isolation test (card workflow cannot call loan write tools).
- **Exit gate:** a reuse report (`reports/reuse.md`) showing new LOC vs reused modules, and the time it took to build.

### Phase 9: Evaluation, performance and resilience evidence
- **Specs:** SPEC-NFR-001 SLOs, SPEC-NFR-002 Eval methodology, SPEC-NFR-003 Load & chaos methodology.
- **Build:** eval runner, k6/locust WebSocket scenarios, chaos scenarios, and report generators.
- **Exit gate:** all thresholds in §6 met, or any miss is explained honestly in `LATENCY_REPORT.md`.

### Phase 10: Production hardening and delivery docs
- **Build and write:**
  - `THREAT_MODEL.md` (STRIDE over the data-flow diagram).
  - Rate limiting per session/IP, CORS/CSP, secure headers, data retention policy, `RUNBOOK.md` (breaker open, LLM outage, provider failover).
  - Grafana dashboard JSON and SLO alerts.
  - `ARCHITECTURE.md` (decisions + trade-offs), `LATENCY_REPORT.md`, `DEMO_SCRIPT.md`, README (one-command start, with and without LLM keys).
  - A production-path section covering Postgres HA, Redis cluster, horizontal autoscaling on WS connections, multi-region, model fallback, and canary rollout of workflow YAMLs.
- **Rehearse change requests:**
  - Outbound collections reminder (new YAML + 1 tool).
  - Hindi/Hinglish replies.
  - Human hand-off.
  - A new policy rule.
  - Each rehearsal is timed and goes through spec → test → build.
- **Exit gate:** a fresh clone can run `make up && make demo` on a clean machine, and all reports are generated from CI artefacts.

---

## 5. Test strategy (pyramid)

| Layer | Tooling | What it proves | Runs |
|---|---|---|---|
| Unit | pytest, hypothesis, vitest | Finance math, rules, breaker, grounding, state machines | Every commit |
| Contract | schemathesis, JSON-Schema | Mocks match OpenAPI; WS protocol shared FE/BE | Every commit |
| Integration | pytest + compose, ScriptedLLM | Tool → connector → mock; full turn loop; actions + audit | Every PR |
| Security | pytest security suite | Scoping, injection, confirmation, OTP limits, PII masking | Every PR |
| Chaos | fault-injection profiles | Degraded answers, no hangs, breaker behaviour | Every PR (short), nightly (long) |
| E2E | Playwright (text + fake-mic voice) | The 5 journeys as a user sees them | Every PR |
| Eval | eval runner (ScriptedLLM + real LLM) | Accuracy, grounding, tool choice, multi-turn context | PR (fake), nightly (real) |
| Load | k6/locust over WS | p50/p95/p99 at 50 → 1,000 concurrent sessions | Nightly / on demand |

Coverage targets: `finance/` 100% branch; `platform/` ≥ 90%; overall ≥ 80%.

---

## 6. Evaluation framework and pass bars

**Golden set** (`evals/golden/`): about 120 cases across 8 categories: single-fact, EMI-change explanation, prepayment what-ifs, eligibility/foreclosure, failed payments, multi-system "what are my options", actions, and refusals/out-of-scope. **Multi-turn set** (`evals/multi_turn/`): 25 scripted conversations with topic switches, references back to earlier turns, and corrections. Each case declares the expected tools, expected numeric values (from the finance library, never hand-typed), required clause IDs, and forbidden content.

| Metric | Definition | Pass bar |
|---|---|---|
| Numeric exactness | Every expected number present and exact (after normalisation) | **100%** |
| Grounding violations | Numbers in replies not traceable to tool outputs | **0** |
| Tool-selection accuracy | Expected tool set ⊆ called tools, no forbidden tools | ≥ 95% |
| Citation correctness | Required clause IDs cited for policy answers | ≥ 98% |
| Context carry-over | Multi-turn cases answered without re-asking known info | ≥ 95% |
| Action safety | Writes executed without confirmation | **0** |
| Cross-customer leakage | Any data from another customer | **0** |
| Refusal correctness | Out-of-scope / advice / injection handled per spec | ≥ 95% |
| Answer quality (LLM-judge) | Clarity, tone, completeness, scored by rubric; **never used for numbers** | ≥ 4.0 / 5 |

**SLOs (SPEC-NFR-001)**

| Path | p50 | p95 |
|---|---|---|
| Text: user send → first token | < 1.2 s | < 3.0 s |
| Text fast lane: full answer | < 400 ms | < 800 ms |
| Voice: end of speech → first audio (filler counts) | < 1.0 s | < 2.0 s |
| Voice: end of speech → first *content* audio | < 1.8 s | < 3.5 s |
| Barge-in: user speech → playback stopped | < 150 ms | < 300 ms |
| Tool call vs mock (no injected latency) | < 100 ms | < 250 ms |
| Turn error rate under 10% backend faults | — | < 1% unhandled |

**Load methodology:** step at 50 / 100 / 250 / 500 / 1,000 concurrent WS sessions with the fake LLM (with model latency simulated from measured distributions) to prove platform capacity. Separately, run a 25-session real-LLM sample for true end-to-end numbers. Report both, and extrapolate honestly to 1,000 with the bottleneck stated (LLM rate limits and provider concurrency).

---

## 7. Production-readiness checklist

- **Security:** verified identity gates all customer data; session-scoped tools; confirmation tokens; idempotency; append-only audit; PII masked in logs, traces and eval reports; minimal LLM context; secrets only from env/secret manager; rate limits; CSP/CORS; dependency and secret scanning in CI; threat model.
- **Reliability:** timeouts on every hop; retries only where safe; circuit breakers; bulkheads; graceful degradation with tickets; LLM provider failover (Sonnet to Haiku, then a templated answer); health/readiness probes.
- **Observability:** OTel traces per turn (STT, router, LLM, each tool, grounding, TTS); RED metrics per connector; SLO dashboards; structured JSON logs with correlation IDs; eval and latency reports archived per build.
- **Scalability:** stateless gateway; Redis sessions; per-connector concurrency limits; prompt caching; fast lane to cut LLM calls; horizontal scaling keyed on WS connections.
- **Operability:** one-command start; seed/reset scripts; runbook; versioned workflow YAMLs and policy rules with effective dates; canary rollout path.

---

## 8. Customer journeys (acceptance anchors)

1. **EMI went up, plus ₹50,000 prepayment savings.** The rate reset is explained (old vs new rate, date, clause), the prepayment rule is checked, and the reduce-EMI and reduce-tenure options are compared side by side.
2. **Voice with intent change and barge-in.** "Next EMI?", then "Actually, last payment first" (interrupting), then "Would ₹20,000 extra cut my tenure?"
3. **Failed payments, then dispute.** Shows failures in the last 6 months with reasons, then "dispute the September one": confirm, get a ticket ID, and the action is audited.
4. **Penalty-free foreclosure?** Eligibility decided by rules (with the clause), a payoff quote, and a foreclosure statement request.
5. **Credit card (workflow 2).** Statement and minimum due, then dispute a transaction. Same runtime, new YAML and connector.
6. **Degraded mode (demo of resilience).** Payments toggled down live. The assistant answers what it can, says what it could not check, and offers a ticket.

---

## 9. Decisions needed

| ID | Decision | Recommendation |
|---|---|---|
| D1 | Backend language: keep the Go edge + Python brain, or go Python-only | **Python-only.** It removes a hop on the voice path and a second toolchain. Only Phases 2 and 7 change if you keep Go |
| D2 | Repo: build fresh in this folder, or in `servicing/` of the existing `agentic-loan-copilot` repo | Fresh repo here (cleaner submission, own CI) |
| D3 | Voice providers (streaming STT/TTS) | One hosted streaming STT and TTS behind interfaces, with Web Speech fallback; pick by which keys you have |
| D4 | LLM access: OpenRouter vs direct Anthropic API | Either, behind `LLMProvider`; direct API if available for lower latency and prompt caching |
| D5 | Timeline: the v1 due date (Nov 17) has passed | Confirm the new deadline so phases can be time-boxed |
