# SPEC-VOX-001: Live voice (browser and phone)
Status: implemented     Owner: platform     Version: 1.0

## Context
Challenge 3: natural voice with follow-ups, interruptions and changing intent, using the same
capabilities as text. Built on the Mydoot telephony bridge with Gemini Live native audio. Code:
`ledgerflow/voice/`.

## Requirements
- **REQ-VOX-001.1** [MUST] THE SYSTEM SHALL serve voice with the same session, tool registry, identity and action rules as text.
- **REQ-VOX-001.2** [MUST] WHEN the model reports an interruption THE SYSTEM SHALL clear audio queued on the client or phone.
- **REQ-VOX-001.3** [MUST] WHEN the customer starts speaking THE SYSTEM SHALL count a new customer turn (this gates action confirmation).
- **REQ-VOX-001.4** [MUST] WHEN a voice session ends THE SYSTEM SHALL append its transcript to the text history, so typing continues the same conversation.
- **REQ-VOX-001.5** [MUST] WHEN a voice session starts THE SYSTEM SHALL seed it with the conversation so far.
- **REQ-VOX-001.6** [MUST] The phone channel SHALL answer Vobiz and Twilio webhooks with stream XML and convert mu-law 8 kHz ⇄ PCM16 16/24 kHz.
- **REQ-VOX-001.7** [SHOULD] WHEN `end_call` succeeds on a phone call THE SYSTEM SHALL hang up once the goodbye has played.
- **REQ-VOX-001.8** [SHOULD] IF the voice model rejects the session (invalid key, credits depleted) THEN THE SYSTEM SHALL log the close code and tell the customer.
- **REQ-VOX-001.9** [SHOULD] THE SYSTEM SHALL record "customer speech → first agent audio" latency.
- **REQ-VOX-001.11** [MUST] The voice prompt SHALL require a tool call before any account figure, date or eligibility is spoken, and SHALL NOT ask for a spoken filler before tool calls (on the live model a filler made it skip the tool and invent figures).
- **REQ-VOX-001.12** [MUST] WHEN the agent starts a tool call before it has spoken in the customer's turn THE SYSTEM SHALL play a pre-recorded "one moment" clip in the agent's voice and the customer's language (English or Hindi; Hinglish hears Hindi), without involving the model; no clip when the agent has already spoken, for `end_call`, or when `VOICE_FILLER=0`. Clips carry no figures. The customer's wait (`user_to_first_audio`) ends at the clip; `tool_result_to_audio` is recorded separately.
- **REQ-VOX-001.10** [MEASURED] End of customer speech to first agent audio SHALL be under 1.0 s p50 and 2.0 s p95. Evidence: `scripts/voice_bench.py` (real Gemini Live session, text-injected turns) → `reports/voice-latency-*.md`.

## Non-goals
Gating spoken numbers before playback (needs a cascaded STT → text → TTS path; see ARCHITECTURE §3).
