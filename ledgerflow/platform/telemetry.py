"""Latency measurement: per-turn span traces (shown live in the UI) and
process-wide histograms (served at /api/metrics, used by the latency report).

Deliberately dependency-free for the PoC; the span API mirrors OpenTelemetry
so swapping in an OTel exporter is local to this file.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field

MAX_SAMPLES = 2000


class Metrics:
    def __init__(self):
        self.samples: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_SAMPLES))
        self.counters: dict[str, int] = defaultdict(int)

    def observe(self, name: str, ms: float) -> None:
        self.samples[name].append(ms)

    def incr(self, name: str, n: int = 1) -> None:
        self.counters[name] += n

    @staticmethod
    def _pct(sorted_vals: list[float], p: float) -> float:
        if not sorted_vals:
            return 0.0
        k = max(0, min(len(sorted_vals) - 1, round(p / 100 * (len(sorted_vals) - 1))))
        return sorted_vals[k]

    def summary(self) -> dict:
        out = {}
        for name, vals in sorted(self.samples.items()):
            s = sorted(vals)
            out[name] = {"n": len(s), "p50": round(self._pct(s, 50), 1), "p95": round(self._pct(s, 95), 1),
                         "p99": round(self._pct(s, 99), 1), "max": round(s[-1], 1) if s else 0}
        return {"latency_ms": out, "counters": dict(self.counters)}

    def reset(self) -> None:
        self.samples.clear()
        self.counters.clear()


METRICS = Metrics()


@dataclass
class Span:
    name: str
    start: float
    end: float = 0.0
    ok: bool = True
    detail: str = ""

    @property
    def ms(self) -> float:
        return (self.end - self.start) * 1000


@dataclass
class Trace:
    """Spans for one conversational turn."""
    t0: float = field(default_factory=time.perf_counter)
    spans: list[Span] = field(default_factory=list)

    @contextmanager
    def span(self, name: str, metric: str | None = None):
        s = Span(name, time.perf_counter())
        self.spans.append(s)
        try:
            yield s
        except Exception as e:
            s.ok = False
            s.detail = type(e).__name__
            raise
        finally:
            s.end = time.perf_counter()
            if metric:
                METRICS.observe(metric, s.ms)

    def mark(self, name: str, ms: float, ok: bool = True, detail: str = "") -> None:
        now = time.perf_counter()
        self.spans.append(Span(name, now - ms / 1000, now, ok, detail))

    def to_event(self) -> list[dict]:
        return [{"name": s.name, "offset_ms": round((s.start - self.t0) * 1000, 1), "ms": round(s.ms, 1),
                 "ok": s.ok, "detail": s.detail} for s in self.spans]
