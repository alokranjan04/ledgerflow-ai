"""Shared-state wiring: in-process for one instance, Redis (Memorystore) for many.

Set REDIS_URL (e.g. redis://10.0.0.3:6379/0) to share sessions, rate limits and
login lockouts across instances; leave it empty for a single instance.
"""
from __future__ import annotations

from dataclasses import dataclass

from ledgerflow import config
from ledgerflow.platform.ratelimit import LoginGuard, RateLimiter, RedisLoginGuard, RedisRateLimiter
from ledgerflow.platform.sessions import RedisSessionStore, SessionStore


@dataclass
class SharedState:
    sessions: SessionStore | RedisSessionStore
    chat_limit: RateLimiter | RedisRateLimiter
    voice_limit: RateLimiter | RedisRateLimiter
    login_guard: LoginGuard | RedisLoginGuard
    backend: str

    async def close(self) -> None:
        await self.sessions.close()


def build_state(redis_client=None) -> SharedState:
    """`redis_client` lets tests inject a fake; otherwise REDIS_URL decides."""
    if redis_client is None and config.REDIS_URL:
        import redis.asyncio as aioredis
        redis_client = aioredis.from_url(config.REDIS_URL, socket_timeout=2, socket_connect_timeout=2,
                                         health_check_interval=30)
    if redis_client is not None:
        return SharedState(
            sessions=RedisSessionStore(redis_client, config.SESSION_TTL_SECS),
            chat_limit=RedisRateLimiter(redis_client, "chat", config.CHAT_TURNS_PER_MIN),
            voice_limit=RedisRateLimiter(redis_client, "voice", config.VOICE_STARTS_PER_MIN),
            login_guard=RedisLoginGuard(redis_client),
            backend="redis")
    return SharedState(
        sessions=SessionStore(config.SESSION_TTL_SECS),
        chat_limit=RateLimiter(config.CHAT_TURNS_PER_MIN),
        voice_limit=RateLimiter(config.VOICE_STARTS_PER_MIN),
        login_guard=LoginGuard(),
        backend="memory")
