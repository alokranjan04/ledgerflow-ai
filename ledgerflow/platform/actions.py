"""Two-phase authorised actions: propose -> customer confirms -> execute.

Guarantees, enforced here rather than trusted to the model:
- an action only executes after a NEW customer turn that follows the proposal
  (the model cannot propose and confirm in one breath);
- confirmation tokens are bound to the session and expire;
- each execution carries an idempotency key, so a repeated confirm or a retry
  never creates a second request;
- every step is written to an append-only audit log.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ledgerflow.platform.sessions import Session

PENDING_TTL_SECS = 300

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, session_hash TEXT,
  customer_id TEXT, workflow TEXT, action TEXT, event TEXT, token TEXT, idempotency_key TEXT,
  params TEXT, result TEXT);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""


@dataclass
class Pending:
    token: str
    action: str
    params: dict
    summary: str
    proposed_seq: int
    expires: float


Executor = Callable[..., Awaitable[dict]]


class AuditLog:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(AUDIT_SCHEMA)

    def write(self, session: Session, action: str, event: str, token: str = "", idem: str = "",
              params: dict | None = None, result: dict | None = None) -> None:
        self.conn.execute(
            "INSERT INTO audit(ts,session_hash,customer_id,workflow,action,event,token,idempotency_key,params,result)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (time.strftime("%Y-%m-%dT%H:%M:%S"), hashlib.sha256(session.id.encode()).hexdigest()[:12],
             session.customer_id, session.workflow_id, action, event, token, idem,
             json.dumps(params or {}, default=str), json.dumps(result or {}, default=str)[:4000]))
        self.conn.commit()

    def for_customer(self, customer_id: str, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT ts, workflow, action, event, token, idempotency_key, result FROM audit "
            "WHERE customer_id=? ORDER BY id DESC LIMIT ?", (customer_id, limit))]


class ActionService:
    def __init__(self, audit: AuditLog):
        self.audit = audit
        self.executors: dict[str, Executor] = {}

    def register(self, action: str, executor: Executor) -> None:
        self.executors[action] = executor

    def propose(self, session: Session, action: str, params: dict, summary: str) -> Pending:
        if action not in self.executors:
            raise KeyError(f"unknown action {action}")
        p = Pending(secrets.token_hex(4).upper(), action, params, summary, session.user_turn_seq,
                    time.time() + PENDING_TTL_SECS)
        session.pending_actions[p.token] = p
        self.audit.write(session, action, "proposed", p.token, params=params)
        return p

    async def confirm(self, session: Session, token: str, ctx) -> dict:
        token = (token or "").strip().upper()
        if token in session.completed_actions:
            return {**session.completed_actions[token], "replayed": True}
        p: Pending | None = session.pending_actions.get(token)
        if p is None:
            return {"ok": False, "error": "No pending request with that confirmation ID. Propose the action first."}
        if time.time() > p.expires:
            session.pending_actions.pop(token, None)
            self.audit.write(session, p.action, "expired", token)
            return {"ok": False, "error": "The confirmation expired. Propose the action again."}
        if session.user_turn_seq <= p.proposed_seq:
            self.audit.write(session, p.action, "confirm_rejected_no_customer_turn", token)
            return {"ok": False, "error": "Not executed: the customer has not confirmed yet. Read the summary "
                                          "back, ask for an explicit yes, and wait for their answer."}
        idem = hashlib.sha256(f"{session.id}:{token}".encode()).hexdigest()[:32]
        try:
            result = await self.executors[p.action](ctx, p.params, idem)
            result = {"ok": True, **result}
            self.audit.write(session, p.action, "executed", token, idem, p.params, result)
        except Exception as e:  # connector failure etc.: report, keep pending so the customer can retry
            self.audit.write(session, p.action, "failed", token, idem, p.params, {"error": str(e)[:300]})
            return {"ok": False, "error": f"The request could not be completed right now ({type(e).__name__}). "
                                          "It is safe to try again."}
        session.pending_actions.pop(token, None)
        session.completed_actions[token] = result
        return result

    def cancel(self, session: Session, token: str) -> dict:
        p = session.pending_actions.pop((token or "").strip().upper(), None)
        if p:
            self.audit.write(session, p.action, "cancelled", p.token)
            return {"ok": True, "cancelled": p.action}
        return {"ok": False, "error": "No pending request with that ID."}
