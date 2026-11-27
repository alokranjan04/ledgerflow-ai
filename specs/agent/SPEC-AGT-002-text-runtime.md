# SPEC-AGT-002: Text conversation runtime
Status: implemented     Owner: platform     Version: 1.0

## Context
Challenges 1 and 2: a responsive chat that follows topic changes without re-asking. Code:
`ledgerflow/platform/gemini_text.py`.

## Requirements
- **REQ-AGT-002.1** [MUST] WHEN the model requests several tools in one step THE SYSTEM SHALL run them concurrently.
- **REQ-AGT-002.2** [MUST] THE SYSTEM SHALL emit progress events (tool started/finished, with cards built from tool data) before the final answer.
- **REQ-AGT-002.3** [MUST] THE SYSTEM SHALL keep earlier turns and their tool results in context, so follow-ups ("the September one") resolve without asking again.
- **REQ-AGT-002.4** [MUST] IF the language model is unavailable THEN THE SYSTEM SHALL tell the customer and keep the session usable.
- **REQ-AGT-002.6** [MUST] Dates in text answers SHALL read as the customer writes them ("10 December 2026"; Hindi "10 दिसंबर 2026"), never as ISO dates, after the grounding check has compared them with the tools' values.
- **REQ-AGT-002.5** [SHOULD] THE SYSTEM SHALL emit a per-turn trace of spans (model calls, tools, connectors) for the UI and metrics. (Evidence: `tests/test_app.py::test_chat_websocket_event_protocol`)
