"""Reusable connector layer: every enterprise-system call goes through here.

Per system: deadline, retries with exponential backoff + jitter (reads, or
writes carrying an Idempotency-Key only), a circuit breaker, a bulkhead
(concurrency cap) and an optional TTL cache for reference data. Failures
surface as typed errors so tools can degrade gracefully instead of crashing.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field

import aiohttp

from ledgerflow.platform.telemetry import METRICS, Trace


class ConnectorError(Exception):
    kind = "error"

    def __init__(self, system: str, msg: str = ""):
        super().__init__(f"{system}: {msg or self.kind}")
        self.system = system


class Timeout(ConnectorError):
    kind = "timeout"


class Unavailable(ConnectorError):
    kind = "unavailable"


class CircuitOpen(ConnectorError):
    kind = "circuit_open"


class NotFound(ConnectorError):
    kind = "not_found"


class Invalid(ConnectorError):
    kind = "invalid"


class Unauthorized(ConnectorError):
    kind = "unauthorized"


@dataclass
class Policy:
    timeout_s: float = 2.0
    retries: int = 2
    backoff_base_s: float = 0.08
    breaker_threshold: int = 5
    breaker_cooldown_s: float = 15.0
    max_concurrency: int = 50
    cache_ttl_s: float = 0.0


class Breaker:
    """closed -> open after N consecutive failures -> half-open after cooldown
    (one trial call) -> closed on success / open again on failure."""

    def __init__(self, threshold: int, cooldown: float, clock=time.monotonic):
        self.threshold, self.cooldown, self.clock = threshold, cooldown, clock
        self.failures = 0
        self.opened_at: float | None = None
        self.trial_in_flight = False

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if self.clock() - self.opened_at >= self.cooldown:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        st = self.state
        if st == "closed":
            return True
        if st == "half_open" and not self.trial_in_flight:
            self.trial_in_flight = True
            return True
        return False

    def success(self) -> None:
        self.failures, self.opened_at, self.trial_in_flight = 0, None, False

    def failure(self) -> None:
        self.trial_in_flight = False
        self.failures += 1
        if self.state == "half_open" or self.failures >= self.threshold:
            self.opened_at = self.clock()


@dataclass
class Connector:
    system: str
    base_url: str
    policy: Policy = field(default_factory=Policy)

    def __post_init__(self):
        self.breaker = Breaker(self.policy.breaker_threshold, self.policy.breaker_cooldown_s)
        self.sem = asyncio.Semaphore(self.policy.max_concurrency)
        self.cache: dict[str, tuple[float, object]] = {}
        self._session: aiohttp.ClientSession | None = None

    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def get(self, path: str, params: dict | None = None, trace: Trace | None = None):
        return await self.call("GET", path, params=params, trace=trace)

    async def post(self, path: str, body: dict, idempotency_key: str | None = None, trace: Trace | None = None):
        return await self.call("POST", path, body=body, idempotency_key=idempotency_key, trace=trace)

    async def call(self, method: str, path: str, params=None, body=None, idempotency_key=None,
                   trace: Trace | None = None):
        cache_key = f"{method} {path} {json.dumps(params, sort_keys=True)}"
        if method == "GET" and self.policy.cache_ttl_s and cache_key in self.cache:
            ts, val = self.cache[cache_key]
            if time.monotonic() - ts < self.policy.cache_ttl_s:
                METRICS.incr(f"connector.{self.system}.cache_hit")
                if trace:
                    trace.mark(f"{self.system} {path} (cache)", 0.0)
                return val
        retryable = method == "GET" or bool(idempotency_key)
        attempts = 1 + (self.policy.retries if retryable else 0)
        last: ConnectorError | None = None
        t_start = time.perf_counter()
        # The breaker judges whole calls (after retries), so a flaky-but-alive
        # system is retried, while a dead one is cut off after N failed calls.
        if not self.breaker.allow():
            METRICS.incr(f"connector.{self.system}.circuit_open")
            if trace:
                trace.mark(f"{self.system} {path}", 0.0, ok=False, detail="circuit open")
            raise CircuitOpen(self.system)
        for attempt in range(attempts):
            try:
                val = await self._once(method, path, params, body, idempotency_key)
                self.breaker.success()
                ms = (time.perf_counter() - t_start) * 1000
                METRICS.observe(f"connector.{self.system}", ms)
                if trace:
                    trace.mark(f"{self.system} {path}", ms, detail=f"attempt {attempt + 1}" if attempt else "")
                if method == "GET" and self.policy.cache_ttl_s:
                    self.cache[cache_key] = (time.monotonic(), val)
                return val
            except (NotFound, Invalid, Unauthorized):
                self.breaker.success()   # the system answered; the request was wrong
                ms = (time.perf_counter() - t_start) * 1000
                if trace:
                    trace.mark(f"{self.system} {path}", ms, detail="4xx")
                raise
            except ConnectorError as e:
                METRICS.incr(f"connector.{self.system}.{e.kind}")
                last = e
                if attempt + 1 < attempts:
                    await asyncio.sleep(self.policy.backoff_base_s * (2 ** attempt) * (0.5 + random.random()))
        self.breaker.failure()
        ms = (time.perf_counter() - t_start) * 1000
        METRICS.observe(f"connector.{self.system}", ms)
        if trace:
            trace.mark(f"{self.system} {path}", ms, ok=False, detail=last.kind if last else "")
        raise last or Unavailable(self.system)

    async def _once(self, method, path, params, body, idempotency_key):
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        async with self.sem:
            try:
                async with self.session().request(
                        method, self.base_url + path, params=params, json=body, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=self.policy.timeout_s)) as resp:
                    if resp.status == 404:
                        raise NotFound(self.system, path)
                    if resp.status == 401:
                        raise Unauthorized(self.system)
                    if 400 <= resp.status < 500:
                        raise Invalid(self.system, await resp.text())
                    if resp.status >= 500:
                        raise Unavailable(self.system, f"HTTP {resp.status}")
                    return await resp.json()
            except TimeoutError:
                raise Timeout(self.system, f"no answer in {self.policy.timeout_s}s") from None
            except aiohttp.ClientError as e:
                raise Unavailable(self.system, type(e).__name__) from None


class ConnectorRegistry:
    """Connectors are configured once and shared by every workflow."""

    def __init__(self, base_url: str, policies: dict[str, Policy]):
        self.connectors = {name: Connector(name, f"{base_url}/{name}", pol) for name, pol in policies.items()}

    def __getitem__(self, name: str) -> Connector:
        return self.connectors[name]

    def health(self) -> dict:
        return {n: c.breaker.state for n, c in self.connectors.items()}

    async def close(self) -> None:
        for c in self.connectors.values():
            await c.close()


DEFAULT_POLICIES = {
    "crm": Policy(timeout_s=1.5),
    "loans": Policy(timeout_s=2.0),
    "payments": Policy(timeout_s=2.5, retries=2),
    "policy": Policy(timeout_s=1.5, cache_ttl_s=300),   # reference data: safe to cache
    "ops": Policy(timeout_s=3.0, retries=2),             # writes carry Idempotency-Key
    "cards": Policy(timeout_s=2.0),
}
