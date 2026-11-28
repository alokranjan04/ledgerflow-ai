# LedgerFlow AI — Architecture and decisions

A loan-servicing assistant with text and live voice (browser and phone), in English, Hindi and Hinglish.
It reads from five simulated enterprise systems (plus cards), gives account-specific answers whose numbers
are computed by code and checked, and completes service requests behind explicit confirmation and a
one-time code. A second workflow (credit cards) runs on the same platform without changes to it. Product
policies and customers are data, managed in an admin panel; customers can sign up themselves.

## 1. System

```
 Browser (vanilla JS)                      Phone (Vobiz / Twilio, mu-law 8 kHz)
  chat · mic (AudioWorklet 16 kHz)               │ POST /answer → XML → WSS /stream
  cards · agent steps · trace · fault panel      │       /admin (products, customers, change log)
  sign-up with one-time code                     │       /agent (hand-off desk)  /dashboard (KPIs)
     │ /ws/chat (JSON events)  /ws/voice (PCM)   │
     ▼                                           ▼
 ┌──────────────────────── Cloud Run: ledgerflow-ai (aiohttp) ────────────────────────┐
 │ Session (identity, shared text+voice context, pending actions)                      │
 │                                                                                    │
 │ Text runtime: fast lane (no model) for simple   Voice runtime ── Gemini Live          │
 │   questions, else Gemini function calling ──┐    native audio, barge-in, tools,     │
 │   grounding gate + 1 regeneration           │    "one moment" clip while tools run  │
 │                                             ▼                                      │
 │            Tool registry (workflow-scoped, identity-gated, customer_id from session)│
 │   platform tools: identity · policy search · service requests · confirm/cancel     │
 │   loan tools ─────┐  card tools ─┐   finance library (Decimal) · rules engine      │
 │                   ▼              ▼                                                  │
 │   Action service: propose → customer turn → confirm (idempotency key) → audit log  │
 │   Connector layer: deadline · retry+jitter · circuit breaker · bulkhead · TTL cache │
 │                   │ HTTP                                                            │
 │   /mock/crm  /mock/loans  /mock/payments  /mock/policy  /mock/ops  /mock/cards      │
 │   (log-normal latency per system, error rate, hard-down; switchable at runtime)     │
 │   seeded from data/policies.yaml + data/customers.yaml; /mock/admin for back office │
 └──────────────────────────────────────┬─────────────────────────────────────────────┘
                                        │ requests, audit trail, admin changes (mirror + restore)
                                        ▼
                              Firestore (asia-south1)      Cloud Logging / Monitoring (KPIs)
```

## 2. How a question is answered

"My EMI increased this month. Why? And if I prepay ₹50,000 today, how much would I save?"

1. The text runtime sends the question to Gemini with the tool declarations of the active workflow.
2. The model calls `explain_emi_change` and `simulate_prepayment(50000)` **in the same step**; they run in
   parallel. Each fans out through connectors (Loans, Policy), which retry, time out and trip breakers.
3. Tools compute every figure with the finance library (Decimal, half-up to the paisa): the schedule with
   EBLR resets, old vs new EMI, the prepayment comparison (reduce tenure vs reduce EMI), charges and
   eligibility from versioned policy rules with clause IDs.
4. The UI receives progress events immediately ("Loan Management + Policy…"), then cards rendered from the
   tool data (not from the model's prose).
5. The model writes the answer. The **grounding gate** extracts every material number (₹, %, lakh/crore,
   counts, spoken words) and accepts it only if it matches a tool value, a rounding of one, a sum or
   difference of two, or the customer's own words. On failure the model regenerates once with feedback;
   on a second failure the customer gets a templated answer built from tool values.
6. A trace (per-system spans) goes to the Ops panel and to `/api/metrics`.

## 3. Voice

Voice uses Gemini Live native audio (speech in, speech out, model-side VAD and turn-taking) through
the pipeline proven in the Mydoot and career-coach phone agents: greeting guard, tool guard, barge-in
`clear`, hang-up after the goodbye, stereo recordings. The transport is split out, so the **browser mic
and a phone call share one pipeline**:

| | Browser | Phone |
|---|---|---|
| Up | PCM16 16 kHz from an AudioWorklet | mu-law 8 kHz → PCM16 → 16 kHz (stateful resampler) |
| Down | PCM16 24 kHz, scheduled in Web Audio | 24 kHz → 8 kHz → mu-law → provider `play` frame |
| Barge-in | `interrupted` → stop all queued sources | provider `clear` + recorder truncation |
| Identity | inherited from the web session | `verify_identity` tool (caller ID + date of birth) |

- **Same capabilities as text**: same tool registry, same session, same action rules. Switching between
  typing and talking keeps context: the voice session is seeded with the transcript so far, and voice
  turns are appended back into the text history.
- **Interruptions and changing intent**: the model hears the customer while it speaks; on barge-in the
  queued audio is dropped within one audio buffer. In the browser, caller audio keeps flowing during tool
  calls so "actually, check my last payment first" is heard; on phone lines it is held back (line noise
  there caused false interrupts in production).
- **Slow backends**: the moment a tool call starts, the *server* plays a pre-recorded "one moment" clip
  in the agent's voice and the customer's language. (An earlier version asked the model to say a filler;
  on the live model that made it skip the tool and invent figures, so the prompt is now tool-first and
  the filler is not the model's.) Tools run in parallel, and degraded tools return partial answers that
  name the missing system.
- **Turn-taking**: the customer's turn ends after a 400 ms pause in the browser (700 ms on the phone,
  where lines are noisier). Measured with spoken questions, this took first audio from 1.8 s to 0.81 s.

**Trade-off (stated honestly):** with speech-to-speech, audio is already spoken when its transcript
arrives, so the grounding check in voice is a *monitor* (logged, counted, flagged in the UI), not a gate.
Mitigations: tools return pre-formatted figures, the prompt forbids computed numbers, and the counters
`grounding.voice_checked / grounding.voice_unsupported` measure it. A cascaded STT → text runtime → TTS
path would allow gating at the cost of roughly 0.5–1 s more latency; the runtime is ready for it (the
text runtime already gates), and it is the first thing to add if a reviewer demands zero-tolerance voice.

## 4. Reusable platform capability (Challenge 5)

A workflow is a YAML file (`workflows/*.yaml`): persona prompt, allowed tools, actions, UI panels and
voice. Everything else is shared: identity and lockout, the connector layer, the tool registry (scoping,
gating, error mapping, metrics), two-phase actions with idempotency and audit, the grounding gate, the
text and voice runtimes, and the UI with its generic cards.

The card workflow is the proof: `workflows/card_servicing.yaml`, its prompt, and
`ledgerflow/domains/cards.py` (3 tools, about 120 lines). It reuses `propose → confirm → audit`, Service
Ops tickets, policy clauses and voice with no platform changes. Workflow isolation is enforced: the card
assistant cannot call loan write tools (tested).

### Several accounts per customer

A customer may hold several loans (personal, home, car) and cards. Every loan and card tool takes an
optional account (ID, last digits, or the product word the customer used); with one account it is used,
otherwise the account the conversation is already about; if neither, the tool answers `needs_choice` and
the assistant asks "your Home loan L-10010 or your Car loan L-10011?" instead of guessing. The fast lane
does the same without the model, and every answer names its account (`platform/accounts.py`,
SPEC-ACC-001). Loans at *other* lenders are out of scope: a lender's assistant sees only its own books.

### Policies, customers and the admin panel

Product terms are data: `data/policies.yaml` holds each product's **rules** (the numbers the calculators
use) and **clauses** (the text the assistant cites), validated by product, rule and field; customers are in
`data/customers.yaml`. Adding a product built on existing rule types, or a customer, needs no code. The
**admin panel** (`/admin`) edits both with the same validation, requires a new policy version when terms
change, refuses changes that would break accounts holding a product, and records every change with its
exact values in an append-only change log. **Sign-up** creates a customer after a one-time code to the
mobile; whether a number is already registered is revealed only after that code.

### Durable records

The PoC re-seeds its synthetic systems on every start, but service requests, the audit trail and admin
changes are mirrored to **Firestore** and restored at start-up (ADR 0006), so a redeploy loses nothing a
customer was promised. A Firestore outage never fails a customer request; it is logged and counted.

## 5. Security controls

- Identity before data: phone + DOB on the web, `verify_identity` on voice/phone; 3 attempts per session,
  5 failures per phone per 15 min across sessions.
- **The model never chooses whose data is read**: no tool has a `customer_id` parameter; it comes from
  the verified session, and an injected one is stripped. Records outside the customer's scope come back as
  "not found".
- Actions execute only after a **new customer turn** that follows the proposal. The model cannot propose
  and confirm in one breath. Tokens are session-bound and expire in 5 minutes; execution carries an
  idempotency key, so a replay returns the same request ID. The audit log is append-only, enforced by
  SQLite triggers.
- Policy text and tool data are marked as reference material, not instructions (prompt-injection
  hygiene). Phone numbers are masked in logs. The LLM sees only the tool outputs for the current
  customer.
- **Step-up**: every write additionally needs a 6-digit code to the registered mobile (hashed, 5-minute
  expiry, 3 attempts, rate-limited), never visible to the model or the logs (SPEC-SEC-003).
- Web sessions are HttpOnly, SameSite=Strict cookies, never in URLs; WebSockets check the Origin; CSP,
  HSTS and the other security headers are set; the simulated systems answer only internal callers.
- Admin and KPI endpoints need the admin token on a deployed service; a disabled customer cannot sign in
  by app or phone. (Production: named admin accounts with roles through the bank's identity provider.)
- Secrets live in Secret Manager, mounted at deploy; the service runs as a dedicated service account with
  no key file.

## 6. Resilience

Each system has its own timeout, retries (GET, or writes carrying an Idempotency-Key) with exponential
backoff + jitter, a circuit breaker (it judges whole calls after retries, so a flaky system is retried
while a dead one is cut off), a bulkhead semaphore and, for reference data only, a TTL cache. Tool failures
become structured `unavailable` results. The assistant answers what it can, names what it could not
check and offers a ticket. The Ops panel switches any system to slow, flaky or down live during a demo.

## 7. Performance evidence

Measured on the deployed service (Cloud Run, asia-south1) from a client in India, 28 Nov 2026. Reports are
in `reports/`, and every number below can be re-run with the scripts named.

| What | Result | Target | Script |
|---|---|---|---|
| Voice: end of a **spoken** question → first agent audio | **0.81 s p50, 0.88 s p95** (8 turns, right tool every time) | < 1.0 s p50, < 2.0 s p95 | `voice_bench.py --speech` |
| Voice: end of question → the answer itself (after the tool) | 1.6–2.0 s typical | — | same |
| Text, fast lane (next EMI, last payment, foreclosure) | 0.25–0.55 s end to end | — | `bench.py journeys` |
| Text, model turns (EMI change + prepayment, options, dispute) | 2.3–3.8 s p50; the dispute turn 3.4 s (was 9.3 s) | — | same |
| Accuracy on the real model, 18 golden conversations | 100% numbers, tools, citations, safety, language, accounts | 100% / ≥95% | `eval.py` |
| 1,000 concurrent conversations (no model, one process) | all answered; p50 4.3 s; 7.6% degraded by the simulated payments system | — | `bench.py conversations` |

How the voice number was reached: the model's default end-of-speech wait gave 1.8 s; a 400 ms pause
(`VOICE_END_SILENCE_MS`) gives 0.81 s with every question still heard whole; the "one moment" clip covers
the tool round, so the customer hears the agent within a second while the figures are fetched.

Live: `/api/metrics` gives p50/p95/p99 per connector, tool, LLM call, text turn and voice "customer stopped
speaking → first audio"; the Ops panel shows each turn's waterfall; Cloud Monitoring holds the KPIs.

## 8. Toward 1,000 concurrent conversations

**Measured (one process, laptop):** `scripts/bench.py conversations` opens N chat conversations *at the
same time* and asks three questions in each, through the whole path except the language model (fast lane):

| Open at once | Turns | Turn p50 | Turn p95 | Degraded answers |
|---|---|---|---|---|
| 250 | 750 | 1.2 s | 2.5 s | 0 |
| 500 | 1,500 | 2.2 s | 4.8 s | 1 |
| 1,000 | 3,000 | 4.3 s | 8.0 s | 227 (7.6%) |

All 1,000 conversations stayed open and every question was answered. The degraded answers are all from the
simulated payments system: 50 requests at a time through its bulkhead, a built-in 3% error rate, and a
circuit breaker that stops calling it when it is swamped, so customers hear "I couldn't reach the payments
system" instead of waiting. Latency grows because one process (sharing the laptop with 1,000 clients) does
everything. Report: `reports/latency-conversations-*.md`.

**Built for scale-out, not yet deployed (cost approval):** ADR 0005 and `scripts/deploy_scaleout.sh`.

1. Sessions, rate limits and lockouts in Memorystore (Redis) behind the existing interfaces: the gateway is
   stateless, autoscales to N instances, and a logout on one instance revokes the session everywhere.
2. One image, three roles: `SERVICE_ROLE=gateway` (autoscaled), `systems` (private service, Google ID token
   + internal token) and `all` (the PoC). Requests, audit trail and admin changes are already in Firestore.
3. The real limit is the model: about 1,000 concurrent Gemini Live sessions and the text model's rate limits
   per project. Plan quota, keep simple questions on the fast lane (no model call), and add a second provider
   behind the same runtime interface for failover.

## 9. Decisions and deviations from build plan v2

| Plan v2 | Built | Why |
|---|---|---|
| FastAPI | aiohttp | Reuses the production-proven Mydoot voice bridge (aiohttp WebSockets) unchanged in spirit |
| Claude for text, separate STT/TTS for voice | Gemini text + Gemini Live native audio | One provider, one tool schema for both modes; speech-to-speech gives the lowest voice latency and natural barge-in |
| React + TS + Vite | Vanilla JS, no build step | Fewer moving parts for a 3-day PoC; one container serves everything |
| Postgres + Redis, docker-compose | SQLite working copy + Firestore for durable records; Redis-backed sessions coded, single instance deployed | Near-zero cloud cost; the scale path is above |
| Separate mock processes | Mock systems mounted at `/mock/*` in the same service | One Cloud Run service; connectors still make real HTTP calls |
| Web Speech fallback | None | Gemini Live in the browser; phone line as a second voice channel |
