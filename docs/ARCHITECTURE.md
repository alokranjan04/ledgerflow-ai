# LedgerFlow AI — Architecture and decisions

A loan-servicing assistant with text and live voice (browser and phone). It reads from five simulated
enterprise systems, gives account-specific answers whose numbers are computed by code and checked, and
completes service requests behind explicit confirmation. A second workflow (credit cards) runs on the
same platform without changes to it.

## 1. System

```
 Browser (vanilla JS)                      Phone (Vobiz / Twilio, mu-law 8 kHz)
  chat · mic (AudioWorklet 16 kHz)               │ POST /answer → XML → WSS /stream
  cards · trace · fault panel                    │
     │ /ws/chat (JSON events)  /ws/voice (PCM)   │
     ▼                                           ▼
 ┌──────────────────────── Cloud Run: ledgerflow-ai (aiohttp) ────────────────────────┐
 │ Session (identity, shared text+voice context, pending actions)                      │
 │                                                                                    │
 │ Text runtime ── Gemini (function calling) ──┐      Voice runtime ── Gemini Live     │
 │   grounding gate + 1 regeneration           │        native audio, barge-in, tools │
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
 └────────────────────────────────────────────────────────────────────────────────────┘
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
- **Slow backends**: the prompt makes the agent say a short filler before a tool call, tools run in
  parallel, and degraded tools return partial answers that name the missing system.

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
- Secrets come only from env (`.env` locally, Cloud Run env on deploy). The service runs as a dedicated
  service account with no key file.

## 6. Resilience

Each system has its own timeout, retries (GET, or writes carrying an Idempotency-Key) with exponential
backoff + jitter, a circuit breaker (it judges whole calls after retries, so a flaky system is retried
while a dead one is cut off), a bulkhead semaphore and, for reference data only, a TTL cache. Tool failures
become structured `unavailable` results. The assistant answers what it can, names what it could not
check and offers a ticket. The Ops panel switches any system to slow, flaky or down live during a demo.

## 7. Performance evidence

- `scripts/bench.py platform`: concurrent sessions without the LLM. Local run, single process, with the
  default simulated latency and 3% payment errors: 200 concurrent sessions, **0 errors**. Account lookup
  (Loans + Cards + Policy) p50 843 ms / p95 1.13 s at 200 sessions, against 281 / 684 ms at 25. The
  growth is CPU in the in-process mock (amortisation recomputed per request), not the gateway.
- `scripts/bench.py journeys`: the brief's journeys over the real chat WebSocket with the real model,
  showing time to first progress event and end-to-end turn time. Results go to `reports/`.
- Live: `/api/metrics` gives p50/p95/p99 per connector, tool, LLM call, text turn and voice
  "customer stopped speaking → first audio". The Ops panel shows the per-turn waterfall.

## 8. Toward 1,000 concurrent conversations

Today, by design for the PoC, there is one Cloud Run instance holding sessions and synthetic systems.
The path to scale:

1. Sessions and pending actions → Memorystore (Redis). The `SessionStore` interface is the seam.
2. Mock systems → their own services (or the real systems). Connectors already speak HTTP.
3. Audit → Cloud SQL (Postgres) with an append-only role.
4. Gateway → N instances (stateless after step 1), with WebSocket session affinity.
5. The real bottleneck is model concurrency and rate limits (Gemini Live sessions per project). Plan
   quota, use the text fast lane for simple lookups, and add a second provider behind the same runtime
   interface for failover.

## 9. Decisions and deviations from build plan v2

| Plan v2 | Built | Why |
|---|---|---|
| FastAPI | aiohttp | Reuses the production-proven Mydoot voice bridge (aiohttp WebSockets) unchanged in spirit |
| Claude for text, separate STT/TTS for voice | Gemini text + Gemini Live native audio | One provider, one tool schema for both modes; speech-to-speech gives the lowest voice latency and natural barge-in |
| React + TS + Vite | Vanilla JS, no build step | Fewer moving parts for a 3-day PoC; one container serves everything |
| Postgres + Redis, docker-compose | SQLite in container, in-memory sessions, single instance | Near-zero cloud cost; the scale path is above |
| Separate mock processes | Mock systems mounted at `/mock/*` in the same service | One Cloud Run service; connectors still make real HTTP calls |
| Web Speech fallback | None | Gemini Live in the browser; phone line as a second voice channel |
