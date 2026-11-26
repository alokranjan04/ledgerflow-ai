"""Conversation sessions: identity, shared text/voice context, pending actions.

One session spans text and voice, so a customer can start typing, switch to
the mic, and switch back without repeating themselves.

Two stores share one async interface:
- `SessionStore`: in-process, for a single instance and for tests.
- `RedisSessionStore`: Redis (Memorystore) is the source of truth, so any
  instance can serve any session and a restart loses nothing. Each instance
  keeps a write-through cache of the sessions it is actively serving: a chat
  socket and a voice socket of one customer then share a single object on
  that instance (WebSocket session affinity keeps them together), instead of
  each loading its own copy and overwriting the other's updates on save.
"""
from __future__ import annotations

import dataclasses
import json
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
    pending_actions: dict = field(default_factory=dict)     # token -> Pending (or its dict form after a reload)
    completed_actions: dict = field(default_factory=dict)
    user_turn_seq: int = 0                                   # increments on every customer turn
    memory: dict = field(default_factory=dict)              # entity memory, e.g. last listed payments
    language: str = "auto"                                    # auto | en | hi | hi-Latn; sticky once detected

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

    # ---- serialisation (Redis) --------------------------------------------
    def to_json(self) -> str:
        d = dataclasses.asdict(self)          # nested Pending dataclasses become dicts
        d["transcript"] = d["transcript"][-200:]
        return json.dumps(d, default=str)

    @classmethod
    def from_json(cls, raw: str | bytes) -> Session:
        d = json.loads(raw)
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


def _new_id() -> str:
    return secrets.token_urlsafe(24)


class SessionStore:
    """In-process store (single instance, tests)."""

    backend = "memory"

    def __init__(self, ttl_secs: int = 3600):
        self.ttl = ttl_secs
        self.sessions: dict[str, Session] = {}

    async def create(self, workflow_id: str, channel: str = "web") -> Session:
        self._sweep()
        s = Session(id=_new_id(), workflow_id=workflow_id, channel=channel)
        self.sessions[s.id] = s
        return s

    async def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        s = self.sessions.get(sid)
        if s and time.time() - s.last_seen > self.ttl:
            self.sessions.pop(sid, None)
            return None
        if s:
            s.last_seen = time.time()
        return s

    async def save(self, s: Session) -> None:
        s.last_seen = time.time()
        self.sessions[s.id] = s

    async def drop(self, sid: str) -> None:
        self.sessions.pop(sid, None)

    async def count(self) -> int:
        self._sweep()
        return len(self.sessions)

    def _sweep(self) -> None:
        now = time.time()
        for sid in [k for k, s in self.sessions.items() if now - s.last_seen > self.ttl]:
            self.sessions.pop(sid, None)

    async def close(self) -> None:
        return None


class RedisSessionStore:
    """Redis-backed sessions with a per-instance write-through cache."""

    backend = "redis"
    PREFIX = "lf:sess:"
    INDEX = "lf:sessions"           # sorted set: session id -> last seen (for counts)

    def __init__(self, redis, ttl_secs: int = 3600, idle_secs: float = 600.0):
        self.r = redis
        self.ttl = ttl_secs
        self.idle_secs = idle_secs
        self.cache: dict[str, Session] = {}

    async def create(self, workflow_id: str, channel: str = "web") -> Session:
        s = Session(id=_new_id(), workflow_id=workflow_id, channel=channel)
        await self.save(s)
        return s

    async def get(self, sid: str | None) -> Session | None:
        """While this instance is actively serving a session, its cached object is
        authoritative (a live voice call may hold unsaved changes); after
        `idle_secs` without use it is evicted and the next request reloads it from
        Redis, which is how another instance picks a session up after a restart
        or rebalancing."""
        if not sid:
            return None
        now = time.time()
        s = self.cache.get(sid)
        if s is not None and now - s.last_seen <= self.idle_secs:
            # One round trip both extends the TTL and proves the session still exists,
            # so a logout or revocation on any instance takes effect here immediately.
            if not await self.r.expire(self.PREFIX + sid, self.ttl):
                self.cache.pop(sid, None)
                return None
            s.last_seen = now
            return s
        raw = await self.r.get(self.PREFIX + sid)
        if raw is None:
            self.cache.pop(sid, None)
            return None
        s = Session.from_json(raw)
        s.last_seen = now
        self.cache[sid] = s
        await self.r.expire(self.PREFIX + sid, self.ttl)
        return s

    async def save(self, s: Session) -> None:
        s.last_seen = time.time()
        pipe = self.r.pipeline()
        pipe.set(self.PREFIX + s.id, s.to_json(), ex=self.ttl)
        pipe.zadd(self.INDEX, {s.id: s.last_seen})
        await pipe.execute()
        self.cache[s.id] = s
        if len(self.cache) > 20_000:            # bound memory: evict idle entries
            cutoff = time.time() - self.idle_secs
            for sid in [k for k, v in self.cache.items() if v.last_seen < cutoff]:
                self.cache.pop(sid, None)

    async def drop(self, sid: str) -> None:
        self.cache.pop(sid, None)
        pipe = self.r.pipeline()
        pipe.delete(self.PREFIX + sid)
        pipe.zrem(self.INDEX, sid)
        await pipe.execute()

    async def count(self) -> int:
        cutoff = time.time() - self.ttl
        await self.r.zremrangebyscore(self.INDEX, 0, cutoff)
        return int(await self.r.zcard(self.INDEX))

    async def close(self) -> None:
        await self.r.aclose()
