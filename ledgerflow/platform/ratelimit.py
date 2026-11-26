"""Token-bucket rate limits per key (session, phone). In-memory for one
instance; the same interface maps onto Redis (INCR + EXPIRE) when scaled out."""
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
