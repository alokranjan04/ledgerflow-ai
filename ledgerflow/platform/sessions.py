"""Conversation sessions: identity, shared text/voice context, pending actions.

One session spans text and voice, so a customer can start typing, switch to
the mic, and switch back without repeating themselves. In-memory for the PoC
(single Cloud Run instance); the interface is what a Redis-backed store would
implement for horizontal scale.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field


@dataclass
class Session:
    id: str
    workflow_id: str
    channel: str = "web"                     # web | phone
    customer_id: str | None = None          # set ONLY by identity verification
    customer_name: str = ""
    phone: str = ""
    verified: bool = False
    verify_attempts: int = 0
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    history: list[dict] = field(default_factory=list)       # Gemini contents for the text agent
    transcript: list[dict] = field(default_factory=list)    # {role, text, mode, ts} across text + voice
    pending_actions: dict = field(default_factory=dict)
    completed_actions: dict = field(default_factory=dict)
    user_turn_seq: int = 0                                   # increments on every customer turn
    memory: dict = field(default_factory=dict)              # entity memory, e.g. last listed payments

    def add_transcript(self, role: str, text: str, mode: str) -> None:
        text = (text or "").strip()
        if text:
            self.transcript.append({"role": role, "text": text, "mode": mode, "ts": time.time()})

    def recent_transcript(self, n: int = 16) -> str:
        lines = []
        for t in self.transcript[-n:]:
            who = "Customer" if t["role"] == "user" else "Assistant"
            lines.append(f"{who} ({t['mode']}): {t['text']}")
        return "\n".join(lines)


class SessionStore:
    def __init__(self, ttl_secs: int = 3600):
        self.ttl = ttl_secs
        self.sessions: dict[str, Session] = {}

    def create(self, workflow_id: str, channel: str = "web") -> Session:
        self.sweep()
        s = Session(id=secrets.token_urlsafe(24), workflow_id=workflow_id, channel=channel)
        self.sessions[s.id] = s
        return s

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        s = self.sessions.get(sid)
        if s and time.time() - s.last_seen > self.ttl:
            self.sessions.pop(sid, None)
            return None
        if s:
            s.last_seen = time.time()
        return s

    def drop(self, sid: str) -> None:
        self.sessions.pop(sid, None)

    def sweep(self) -> None:
        now = time.time()
        for sid in [k for k, s in self.sessions.items() if now - s.last_seen > self.ttl]:
            self.sessions.pop(sid, None)

    def __len__(self) -> int:
        return len(self.sessions)
