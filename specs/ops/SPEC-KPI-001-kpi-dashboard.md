# SPEC-KPI-001: KPI dashboard (in-app and Cloud Monitoring as code)
Status: implemented     Owner: platform     Version: 1.0

## Context
The PRD sets a north star (resolved in one contact, measured as containment) and pilot targets. The pilot's
weekly KPI review needs them visible to the lender, correct across instances and durable across deploys,
which per-process counters are not.

## Requirements
- **REQ-KPI-001.1** [MUST] An in-app dashboard SHALL show containment, conversations, hand-offs, completed requests, grounding fallbacks, voice figure flags, step-up outcomes and latency against the PRD pilot targets.
- **REQ-KPI-001.2** [MUST] Every KPI event SHALL be emitted as a structured log line: conversation engaged (once per conversation, with channel), hand-offs, executed actions and step-up outcomes (audit lines), and text turns with outcome, grounding result and latency.
- **REQ-KPI-001.3** [MUST] On a deployed service, metrics and KPIs SHALL require the admin token (business volumes are not public).
- **REQ-KPI-001.4** [MUST] Log-based metrics and a Cloud Monitoring dashboard SHALL be defined as code in `monitoring/`, and every metric SHALL filter on log messages and fields the code actually emits (checked by a test).
- **REQ-KPI-001.5** [SHOULD] `scripts/setup_monitoring.sh` SHALL create or update the metrics and replace the dashboard idempotently. (Evidence: `scripts/setup_monitoring.sh`)
- **REQ-KPI-001.6** [MEASURED] Containment over a rolling 24 hours is shown on the Cloud Monitoring dashboard with the 40% pilot threshold. Evidence: Cloud Monitoring dashboard "LedgerFlow AI — KPIs"
