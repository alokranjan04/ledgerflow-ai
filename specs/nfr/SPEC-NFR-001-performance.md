# SPEC-NFR-001: Performance and scale
Status: partially measured     Owner: platform     Version: 1.0

## Context
Constraints "responsive conversational experience with measured latency" and "architecture capable of
evolving toward 1,000 concurrent conversations". Measured by `scripts/bench.py`; reports in `reports/`.

## Requirements
- **REQ-NFR-001.1** [MEASURED] The platform SHALL serve 200 concurrent sessions (login, account, activity; no LLM) with zero errors on one instance. Evidence: `reports/latency-platform-*.md`
- **REQ-NFR-001.2** [MEASURED] A tool call against a system without injected latency SHALL take under 100 ms p50. Evidence: `reports/latency-platform-*.md`
- **REQ-NFR-001.3** [MEASURED] Text: customer send → first progress event under 1.2 s p50; full answer under 3.0 s p95 for single-tool turns. Evidence: `reports/latency-journeys-*.md` (needs a Gemini key)
- **REQ-NFR-001.4** [MEASURED] Voice: end of speech → first audio under 1.0 s p50 (see REQ-VOX-001.10).

## Status
.1 is met (local and Cloud Run runs, 0 errors). .2 is not yet isolated: the benchmarks use the default
simulated latency. .3 and .4 are pending a Gemini key.
