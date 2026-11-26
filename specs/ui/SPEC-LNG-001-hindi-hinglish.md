# SPEC-LNG-001: Hindi and Hinglish conversations
Status: implemented     Owner: platform     Version: 1.0

## Context
Most borrowers of an Indian lender are more comfortable in Hindi or code-mixed Hinglish than in English,
on chat and especially on the phone. Language must not weaken accuracy: figures stay exact, and the
grounding check must read Hindi as well as English. The language lock comes from the Mydoot phone agent,
where noisy audio made the model switch languages mid-call.

## Requirements
- **REQ-LNG-001.1** [MUST] WHEN the conversation language is automatic THE SYSTEM SHALL detect Hindi (Devanagari) or Hinglish from the customer's words and lock the session to it from the first non-English turn.
- **REQ-LNG-001.2** [MUST] Replies SHALL be in the session's language, keeping amounts exactly as tools return them (₹ and Indian grouping in text) and IDs, clause IDs and product names unchanged.
- **REQ-LNG-001.3** [MUST] The grounding check SHALL read Devanagari digits, Hindi cardinals 1–99, Hindi and Hinglish scale and unit words (हज़ार, लाख, करोड़, रुपये, महीने; hazaar, mahine), and SHALL flag unsupported figures in Hindi and Hinglish exactly as in English.
- **REQ-LNG-001.4** [MUST] Voice SHALL use the session's language with a lock in both directions; the phone greeting SHALL be bilingual and ask the caller's preference.
- **REQ-LNG-001.5** [MUST] The system's own fixed messages (model unavailable, time budget, grounding fallback) SHALL be in the session's language.
- **REQ-LNG-001.6** [MUST] THE SYSTEM SHALL let the customer choose the language explicitly (Auto, English, Hindi, Hinglish) at sign-in or at any time, and show suggestions in that language.

## Acceptance criteria
- Given "मेरी अगली EMI कितनी है?", the session locks to Hindi and stays Hindi after a later "ok thanks". → .1
- Given tool value ₹21,003.30, "इक्कीस हज़ार रुपये" passes and "पचास हज़ार रुपये" is flagged; "9 mahine" is flagged when the tool says 5 months. → .3

## Non-goals
Languages beyond Hindi in this release (Tamil, Telugu, Bengali and others follow the same pattern: a detector
entry, a prompt block, number words for grounding). Translating policy clause text (cited by ID, explained in
the reply's language).
