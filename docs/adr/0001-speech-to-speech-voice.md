# ADR 0001: Speech-to-speech voice with Gemini Live, sharing the text runtime's tools

Status: accepted (2026-11-26)

## Context
Challenge 3 asks for natural voice with interruptions and intent changes, using the same capabilities as
text, and evaluated on the actual interaction. Options were (a) a cascaded STT → LLM → TTS pipeline, or (b)
a native-audio speech-to-speech model with function calling.

## Decision
Use Gemini Live native audio (b), through the transport-agnostic bridge proven in the Mydoot and
career-coach phone agents. It serves both the browser mic and a phone line, with the same tool registry,
session and action rules as text.

## Consequences
- Lowest latency, model-side voice activity detection and natural barge-in; one pipeline for browser and phone.
- Spoken numbers cannot be gated before playback: grounding in voice is a monitor (counted and flagged), not a
  gate. Mitigated by tools returning pre-formatted figures and by prompt rules; the cascaded path is
  roadmap item P2 for zero-tolerance workflows.
- Coupled to one provider for voice; the fallback chain covers text only.
