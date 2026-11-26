"""Shared fixtures: the mock enterprise systems on a real local HTTP server,
connectors, actions and a verified-session factory."""
from datetime import date

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from ledgerflow.domains import cards, loans  # noqa: F401  (registers tools)
from ledgerflow.mocks.server import MockSystems, build_mock_app
from ledgerflow.platform import common_tools
from ledgerflow.platform.actions import ActionService, AuditLog
from ledgerflow.platform.connectors import DEFAULT_POLICIES, ConnectorRegistry, Policy
from ledgerflow.platform.sessions import SessionStore
from ledgerflow.platform.telemetry import Trace
from ledgerflow.platform.tools import REGISTRY, ToolContext
from ledgerflow.platform.workflow import load_workflows

AS_OF = date(2026, 11, 26)


@pytest.fixture
async def mocks(tmp_path):
    ms = MockSystems(str(tmp_path / "t.db"), AS_OF)
    for p in ms.profiles.values():      # fast and deterministic for tests
        p.update(p50=1, p95=2, error_rate=0.0, down=False)
    root = web.Application()
    root.add_subapp("/mock", build_mock_app(ms))
    server = TestServer(root)
    await server.start_server()
    yield ms, str(server.make_url("/mock"))
    await server.close()


@pytest.fixture
async def env(mocks, tmp_path):
    ms, base = mocks
    policies = {k: Policy(**{**v.__dict__, "backoff_base_s": 0.001, "breaker_cooldown_s": 0.2})
                for k, v in DEFAULT_POLICIES.items()}
    con = ConnectorRegistry(base, policies)
    actions = ActionService(AuditLog(tmp_path / "audit.db"))
    common_tools.register_common_actions(actions)
    loans.register_actions(actions)
    cards.register_actions(actions)
    store = SessionStore()
    wfs = load_workflows()

    async def login(phone: str, dob: str, workflow: str = "loan_servicing", mode: str = "text"):
        s = store.create(workflow)
        res = await common_tools.verify(s, con, phone, dob)
        assert res["ok"], res
        return s, ToolContext(s, con, actions, Trace(), AS_OF, mode=mode)

    async def run(ctx, name, **args):
        return await REGISTRY.run(name, args, ctx, wfs[ctx.session.workflow_id].tools)

    yield {"ms": ms, "con": con, "actions": actions, "store": store, "wfs": wfs, "login": login, "run": run}
    await con.close()


PRIYA = ("9800000001", "1990-04-12")
RAHUL = ("9800000002", "1986-11-03")
MEERA = ("9800000003", "1994-07-21")
ANANYA = ("9800000004", "1992-02-14")
