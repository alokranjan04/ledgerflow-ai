"""Structured JSON logging with correlation IDs (Cloud Logging compatible).

Every line carries `severity`, `message`, and whatever correlation context is
active: request ID, session hash (never the raw session ID), turn ID, channel.
On Cloud Run, the `X-Cloud-Trace-Context` header links log lines to the request
trace. Customer PII never goes in log fields: phone numbers are masked, and
transcript text stays out of logs.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import sys
import time

_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("lf_log_ctx", default=None)
PROJECT_ID = ""


def bind(**fields) -> contextvars.Token:
    """Add correlation fields for the current task (and tasks it spawns)."""
    return _ctx.set({**(_ctx.get() or {}), **{k: v for k, v in fields.items() if v is not None}})


def reset(token: contextvars.Token) -> None:
    _ctx.reset(token)


def session_hash(session_id: str | None) -> str | None:
    return hashlib.sha256(session_id.encode()).hexdigest()[:12] if session_id else None


def mask_phone(p: str | None) -> str:
    return ("*" * max(0, len(p) - 4)) + p[-4:] if p else ""


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"severity": record.levelname, "message": record.getMessage(), "logger": record.name,
                 "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z"}
        ctx = _ctx.get() or {}
        trace = ctx.get("trace")
        if trace and PROJECT_ID:
            entry["logging.googleapis.com/trace"] = f"projects/{PROJECT_ID}/traces/{trace}"
        entry.update({k: v for k, v in ctx.items() if k != "trace"})
        extra = getattr(record, "fields", None)
        if extra:
            entry.update(extra)
        if record.exc_info:
            entry["stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup(level: str = "INFO", json_logs: bool = True, project_id: str = "") -> None:
    global PROJECT_ID
    PROJECT_ID = project_id
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_logs else logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("aiohttp.access").setLevel("WARNING")   # access lines would carry URLs; request middleware logs instead


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)


def event(logger: logging.Logger, message: str, level: int = logging.INFO, **fields) -> None:
    logger.log(level, message, extra={"fields": fields})
