"""Typed tool registry shared by every workflow, and by text and voice alike.

A tool is a plain async function plus a spec (description, parameters,
side-effect class). The registry:
- emits Gemini function declarations for exactly the tools a workflow allows;
- refuses tools outside the active workflow (workflow isolation);
- refuses customer-data tools until identity is verified;
- never lets the model choose whose data is read: `customer_id` is not a
  parameter of any tool and is stripped if the model sends one anyway;
- turns connector failures into structured "unavailable" results so the
  assistant degrades gracefully, and times every call.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ledgerflow.platform.connectors import ConnectorError, ConnectorRegistry
from ledgerflow.platform.sessions import Session
from ledgerflow.platform.telemetry import METRICS, Trace

FORBIDDEN_ARGS = {"customer_id", "customerId", "customer"}


@dataclass
class ToolResult:
    data: dict                       # what the model sees
    card: dict | None = None         # what the UI renders (never sent to the model)
    ends_call: bool = False


@dataclass
class ToolContext:
    session: Session
    connectors: ConnectorRegistry
    actions: Any                     # ActionService
    trace: Trace
    as_of: date
    mode: str = "text"               # text | voice | phone

    @property
    def customer_id(self) -> str:
        if not (self.session.verified and self.session.customer_id):
            raise PermissionError("identity not verified")
        return self.session.customer_id


@dataclass
class ToolSpec:
    name: str
    description: str
    params: dict = field(default_factory=dict)       # name -> {"type": ..., "description": ...}
    required: list[str] = field(default_factory=list)
    side_effect: str = "read"                        # read | calc | write | auth | control
    requires_auth: bool = True
    fn: Callable[..., Awaitable[ToolResult | dict]] | None = None

    def declaration(self) -> dict:
        def conv(p: dict) -> dict:
            out = {"type": p.get("type", "string").upper(), "description": p.get("description", "")}
            if "enum" in p:
                out["enum"] = p["enum"]
            if p.get("type") == "array":
                out["items"] = conv(p.get("items", {"type": "string"}))
            return out
        d = {"name": self.name, "description": self.description}
        if self.params:
            d["parameters"] = {"type": "OBJECT", "properties": {k: conv(v) for k, v in self.params.items()},
                               "required": self.required}
        return d


class ToolRegistry:
    def __init__(self):
        self.specs: dict[str, ToolSpec] = {}

    def tool(self, name: str, description: str, params: dict | None = None, required: list[str] | None = None,
             side_effect: str = "read", requires_auth: bool = True):
        def deco(fn):
            if name in self.specs:
                raise ValueError(f"duplicate tool {name}")
            self.specs[name] = ToolSpec(name, description, params or {}, required or [], side_effect,
                                        requires_auth, fn)
            return fn
        return deco

    def declarations(self, names: list[str]) -> list[dict]:
        return [{"functionDeclarations": [self.specs[n].declaration() for n in names]}]

    async def run(self, name: str, args: dict, ctx: ToolContext, allowed: list[str],
                  budget_s: float = 8.0) -> ToolResult:
        t0 = time.perf_counter()
        spec = self.specs.get(name)
        if spec is None or name not in allowed:
            METRICS.incr("tool.rejected_not_in_workflow")
            return ToolResult({"ok": False, "error": f"Tool {name} is not available in this conversation."})
        if spec.requires_auth and not ctx.session.verified:
            METRICS.incr("tool.rejected_unverified")
            return ToolResult({"ok": False, "error": "Verify the customer's identity first."})
        args = {k: v for k, v in (args or {}).items() if k not in FORBIDDEN_ARGS and k in spec.params}
        ok = True
        try:
            with ctx.trace.span(f"tool {name}"):
                out = await asyncio.wait_for(spec.fn(ctx, **args), timeout=budget_s)
            res = out if isinstance(out, ToolResult) else ToolResult(out)
        except TimeoutError:
            ok = False
            res = ToolResult({"ok": False, "error": "This is taking longer than expected.",
                              "unavailable": "timeout"})
        except ConnectorError as e:
            ok = False
            res = ToolResult({"ok": False, "unavailable": e.system, "error_kind": e.kind,
                              "error": f"The {e.system} system is not responding right now."})
        except PermissionError as e:
            ok = False
            res = ToolResult({"ok": False, "error": str(e)})
        except (ValueError, KeyError) as e:
            ok = False
            res = ToolResult({"ok": False, "error": str(e)})
        METRICS.observe(f"tool.{name}", (time.perf_counter() - t0) * 1000)
        if not ok:
            METRICS.incr(f"tool.{name}.failed")
        return res


REGISTRY = ToolRegistry()
