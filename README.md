# LedgerFlow AI

An AI loan-servicing assistant with **text and live voice** (in the browser and over a phone line). It
answers customer-specific questions from five simulated enterprise systems with numbers computed by code
and verified, and completes service requests behind explicit confirmation. A **credit-card workflow** runs
on the same platform to show reuse.

Architecture, decisions and trade-offs: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

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

```bash
pytest -q                                              # finance (incl. property tests), grounding, journeys, security, resilience, agent loop
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
tests/                      pytest suite
docs/                       architecture, build plan, brief
```
