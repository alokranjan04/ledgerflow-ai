# SPEC-UI-001: Web app and event protocol
Status: implemented     Owner: platform     Version: 1.0

## Context
Challenge 1: a usable web app with text and voice that feels like a product. Code: `frontend/`, `app.py`.

## Requirements
- **REQ-UI-001.1** [MUST] THE SYSTEM SHALL provide sign-in, an account panel (loan or card by workflow), service requests, chat with cards, a mic for live voice, and an ops panel (trace, metrics, fault injection).
- **REQ-UI-001.2** [MUST] The chat WebSocket SHALL emit, per turn: `thinking`, `agent_step` (live), `tool_started`/`tool_finished` (with an optional `card`), `assistant` (with the turn's `steps`), and `trace`.
- **REQ-UI-001.5** [MUST] Every answer SHALL carry a plain-language account of how it was made: the plan (which steps, in parallel or not), each system checked with what it returned and how long it took, guardrails (awaiting approval, one-time code, request created), the figure check, and the total time; shown live while the assistant works and as "How I got this answer" under the reply, in text and voice. The account is built from tool results and runtime facts only, never from model text.
- **REQ-UI-001.3** [SHOULD] The voice WebSocket SHALL carry binary PCM16 16 kHz upstream and 24 kHz downstream, plus JSON events (`voice_ready`, `transcript`, `interrupted`, tool events, `grounding_warning`, `error`). (Evidence: `ledgerflow/voice/pipeline.py`, `frontend/app.js`)
- **REQ-UI-001.4** [SHOULD] The UI SHALL meet basic accessibility: labelled controls, `aria-live` messages, visible focus. (Evidence: `frontend/index.html`)

## Interfaces
`POST /api/login` (sets the `lf_sid` HttpOnly cookie; see SPEC-SEC-002), `POST /api/logout`, `POST /api/workflow`, `GET /api/account`, `GET /api/activity`, `GET /ws/chat`,
`GET /ws/voice`, `GET /api/metrics`, `GET|POST /api/faults`, `GET /healthz`, `GET /readyz`.
