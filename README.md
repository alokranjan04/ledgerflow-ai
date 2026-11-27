# LedgerFlow AI

An AI loan-servicing assistant with **text and live voice** (in the browser and over a phone line). It
answers customer-specific questions from five simulated enterprise systems with numbers computed by code
and verified, and completes service requests behind explicit confirmation. A **credit-card workflow** runs
on the same platform to show reuse.

Architecture, decisions and trade-offs: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · production hardening and roadmap:
[docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md) · decisions: [docs/adr/](docs/adr/).

C4 diagrams (interactive HTML): [C1 context](.archify/c1-context-20261127-163321/c1-context.html) ·
[C2 containers](.archify/c2-containers-20261127-163321/c2-containers.html) ·
[C3 components](.archify/c3-components-20261127-163321/c3-components.html) ·
[C4 code](.archify/c4-code-20261127-163321/c4-code.html).

## Run it locally

Requires Python 3.11+ and a Gemini API key (Google AI Studio) for chat and voice.
Without a key the app still runs: accounts, tools and the Ops panel work, and chat says the language
service is unavailable.

```bash
python -m venv .venv && .venv/Scripts/activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env                                   # set GEMINI_API_KEY
python scripts/check_env.py                            # verifies the key with a real text call and a Live session
python app.py                                          # http://localhost:8080
```

Pick a demo customer on the sign-in screen. All data is synthetic, and dates are relative to today (or
`LEDGERFLOW_AS_OF`).

| Customer | Mobile | DOB | Set up for |
|---|---|---|---|
| Priya Sharma | 9800000001 | 1990-04-12 | EMI rose after a rate reset · 3 failed debits (one in Sept) · penalty-free foreclosure · credit card |
| Rahul Verma | 9800000002 | 1986-11-03 | Fixed rate past lock-in: foreclosure carries a 4% charge |
| Meera Nair | 9800000003 | 1994-07-21 | Fixed rate inside the 12-EMI lock-in |
| Ananya Iyer | 9800000004 | 1992-02-14 | Credit card with a duplicate charge to dispute |

### Add a demo customer

Customers on the sign-in screen are defined in [data/customers.yaml](data/customers.yaml); the same file seeds the
CRM, loan, payment and card systems on every start. Copy a block, give it a new `id`, `phone`, loan and card ID,
then check the file and restart (or redeploy):

```bash
python scripts/validate_customers.py      # checks policies + customers; lists every problem by field
python app.py                             # locally; or scripts/deploy.sh for Cloud Run
```

### Admin panel and sign-up

- **`/admin`** (admin token on a deployed service; your name goes in the change log): edit any product's rules
  and clauses with the same checks as the YAML file (a new policy version is required when terms change), create,
  copy, revert or delete products; search customers, edit profiles, put them on the sign-in list, disable or
  enable sign-in, and create customers with demo accounts. Every change is in the append-only **change log** and
  survives redeploys (Firestore).
- **Sign-up** (sign-in screen → *Create an account*): profile plus a demo loan and/or card, confirmed with a
  one-time code to the mobile (simulated SMS in the PoC), then signed straight in.

### Change a policy or add a product

Product terms live in [data/policies.yaml](data/policies.yaml): **rules** (the numbers the calculators use, such
as the bounce charge, prepayment minimum or dispute window) and **clauses** (the text the assistant quotes and
cites by ID). Edit a value or a clause, or copy a product block under a new code to add a product built on the
existing rule types; then give customers that product in `data/customers.yaml` (`loan.product`, `card.product`).
The same check command validates both files: unknown rules or fields, missing rules, clauses that do not exist and
out-of-range percentages are reported by product, rule and field. A product needing a new *kind* of rule (a
moratorium, a top-up) needs calculator code in `ledgerflow/domains/`.

## Demo script

1. **Precise answer** (Priya, text): *"My EMI increased this month. Why? And if I make a ₹50,000 prepayment
   today, how much interest would I save?"* Two tools run in parallel; cards show the rate reset (clause
   PL-FL-3.4) and reduce-tenure vs reduce-EMI savings.
2. **Voice with interruption** (tap the mic): *"What's my next EMI?"* → interrupt with *"Actually, check my
   last payment first"* → *"Would paying ₹20,000 extra reduce my tenure?"*
3. **Failed payments → action**: *"Show me the payments that failed in the last six months"* → *"Dispute the
   September one"*. Aria reads back the summary and waits for "yes". A request ID appears, and the audit
   trail records proposal and execution.
4. **Eligibility**: *"Am I eligible for foreclosure without a penalty?"* Priya: yes (PL-FL-5.1). Rahul: 4%
   charge (PL-FX-6.2). Meera: locked in (PL-FX-6.1).
5. **Second workflow**: switch to *Credit card* at the top. Ananya: *"I think I was charged twice at
   Croma"* → dispute → confirm.
6. **Resilience**: in the Ops panel set *payments* to *down* and ask *"What are my options?"*. Aria answers
   from Loans + Policy, says what it couldn't check, and offers a ticket. Watch the breaker open and the
   latency waterfall.

## Tests and evidence

Development is spec-first: requirements live in [specs/](specs/) (15 specs, EARS format, MUST / SHOULD /
MEASURED). Every test carries `@pytest.mark.req("REQ-...")`, and the traceability gate fails if a MUST
requirement has no passing test or a test cites an unknown ID.

```bash
pytest -q                                              # 76 tests: finance, grounding, journeys, security, actions, connectors, voice (fake Gemini Live), app
python scripts/trace_matrix.py                         # runs tests, writes reports/traceability.md, exit 1 on any MUST gap
ruff check .
python scripts/bench.py platform --url http://localhost:8080 --steps 25,50,100,200
python scripts/bench.py journeys --url http://localhost:8080 --runs 3   # needs GEMINI_API_KEY
```

Reports are written to `reports/`. Live percentiles are at `/api/metrics`.

## Deploy to Google Cloud Run

```bash
scripts/deploy.sh             # keyless: runs as ledgerflow-ai-runtime@<project>; env from .env only
scripts/deploy.sh --env-only  # push .env changes without a rebuild
```

The defaults target project `project-b2f8fd7f-3661-4447-8b6`, region `asia-south1`, service `ledgerflow-ai`;
override them with `PROJECT`, `ACCOUNT`, `REGION`, `SERVICE`. It runs one always-on instance (sessions and
synthetic systems are in-process) with a 60-minute request timeout for long voice sessions.

### Phone line (optional)

Point the Vobiz application's **Answer, Hangup and Fallback URLs** (or Twilio's "A call comes in"
webhook) at `https://<service>/answer` (POST), then reopen the console to confirm the change saved. Callers
verify with their date of birth; the caller ID is used as the registered mobile. Test without a phone:
`python scripts/fake_call.py wss://<service>/stream`.

## Layout

```
app.py                      HTTP/WebSocket entry: UI, chat, voice, phone, ops, mock systems
ledgerflow/platform/        reusable: connectors, tool registry, actions+audit, identity, grounding,
                            sessions, telemetry, workflow loader, Gemini text runtime
ledgerflow/voice/           Gemini Live pipeline (browser + phone transports), telephony adapters, recorder
ledgerflow/finance/         Decimal loan maths: EMI, schedules with rate resets, prepayment, foreclosure
ledgerflow/domains/         loans.py (workflow 1), cards.py (workflow 2)
ledgerflow/mocks/           synthetic data + mock CRM, Loans, Payments, Policy, Service Ops, Cards
workflows/                  workflow YAML + prompts
frontend/                   single-page app (no build step)
scripts/                    check_env, bench, deploy, fake_call, analyze_recording
specs/                      requirements (EARS), one spec per area
tests/                      pytest suite, tagged with requirement IDs
docs/                       architecture, build plan, brief
```
