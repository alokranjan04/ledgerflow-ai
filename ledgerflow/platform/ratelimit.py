"""Abuse limits shared by every instance when Redis is configured.

- `RateLimiter` / `RedisRateLimiter`: per-key limits on expensive paths (chat turns, voice starts).
- `LoginGuard` / `RedisLoginGuard`: failed-login lockout per phone number across sessions.

All expose the same async methods, so the app does not care which backend runs.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Bucket:
    capacity: float
    refill_per_s: float
    tokens: float = -1.0
    updated: float = field(default_factory=time.monotonic)

    def take(self, now: float) -> bool:
        if self.tokens < 0:
            self.tokens = self.capacity
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_per_s)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class RateLimiter:
    """In-process token bucket (single instance)."""

    def __init__(self, per_minute: int, burst: int | None = None, clock=time.monotonic):
        self.capacity = float(burst or per_minute)
        self.refill = per_minute / 60.0
        self.clock = clock
        self.buckets: dict[str, Bucket] = {}

    def allow(self, key: str) -> bool:
        b = self.buckets.get(key)
        if b is None:
            if len(self.buckets) > 50_000:      # bound memory under abuse
                self.buckets.clear()
            b = self.buckets[key] = Bucket(self.capacity, self.refill, updated=self.clock())
        return b.take(self.clock())

    async def hit(self, key: str) -> bool:
        return self.allow(key)


class RedisRateLimiter:
    """Fixed one-minute window per key, shared across instances (INCR + EXPIRE).
    Coarser than a token bucket at window edges, but atomic and one round trip."""

    def __init__(self, redis, name: str, per_minute: int, clock=time.time):
        self.r, self.name, self.limit, self.clock = redis, name, per_minute, clock

    async def hit(self, key: str) -> bool:
        window = int(self.clock() // 60)
        k = f"lf:rl:{self.name}:{key}:{window}"
        pipe = self.r.pipeline()
        pipe.incr(k)
        pipe.expire(k, 120)
        count, _ = await pipe.execute()
        return int(count) <= self.limit


class LoginGuard:
    """In-process failed-login lockout per phone number."""

    def __init__(self, max_failures: int = 5, window_s: int = 900, clock=time.time):
        self.max, self.window, self.clock = max_failures, window_s, clock
        self.failures: dict[str, list[float]] = {}

    async def locked(self, phone: str) -> bool:
        now = self.clock()
        recent = [t for t in self.failures.get(phone, []) if now - t < self.window]
        self.failures[phone] = recent
        return len(recent) >= self.max

    async def record_failure(self, phone: str) -> None:
        self.failures.setdefault(phone, []).append(self.clock())

    def clear(self) -> None:
        self.failures.clear()


class RedisLoginGuard:
    """Lockout shared by all instances: a counter per phone that expires with the window."""

    def __init__(self, redis, max_failures: int = 5, window_s: int = 900):
        self.r, self.max, self.window = redis, max_failures, window_s

    async def locked(self, phone: str) -> bool:
        v = await self.r.get(f"lf:loginfail:{phone}")
        return v is not None and int(v) >= self.max

    async def record_failure(self, phone: str) -> None:
        k = f"lf:loginfail:{phone}"
        pipe = self.r.pipeline()
        pipe.incr(k)
        pipe.expire(k, self.window, nx=True)     # window starts at the first failure
        await pipe.execute()

    def clear(self) -> None:
        return None
