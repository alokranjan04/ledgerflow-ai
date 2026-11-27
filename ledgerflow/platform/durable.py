"""Durable copy of the records a customer must never lose: service requests and the audit trail.

The working copy stays in SQLite (fast queries, append-only triggers). Every write is
mirrored to Firestore, and a fresh container restores from it at start-up, so a redeploy
or a restarted instance no longer wipes requests the customer was promised (ADR 0006).

Firestore is called over its REST API with aiohttp: no gRPC dependency, and the same
keyless service-account credentials as the rest of the service. Each record is stored as
one JSON string plus the fields worth filtering on, so the SQLite rows round-trip exactly.

DURABLE_STORE=firestore turns it on; the default ("none") keeps local runs and tests offline.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.parse

import aiohttp

from ledgerflow.platform import logs
from ledgerflow.platform.telemetry import METRICS

_log = logs.get("ledgerflow.durable")
SCOPE = "https://www.googleapis.com/auth/datastore"


class NullStore:
    """Durability off: nothing is mirrored and nothing is restored."""
    enabled = False
    backend = "none"

    async def put(self, collection: str, doc_id: str, record: dict, create_only: bool = False) -> bool:
        return True

    def put_soon(self, collection: str, doc_id: str, record: dict, create_only: bool = False) -> None:
        return None

    async def all(self, collection: str) -> list[dict]:
        return []

    async def drain(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _GoogleToken:
    """OAuth access token from the runtime service account (metadata server on Cloud Run,
    gcloud application-default credentials locally), refreshed shortly before it expires."""

    def __init__(self):
        self._creds = None
        self._lock = asyncio.Lock()

    def _refresh(self) -> None:
        import google.auth
        import google.auth.transport.requests
        if self._creds is None:
            self._creds, _ = google.auth.default(scopes=[SCOPE])
        self._creds.refresh(google.auth.transport.requests.Request())

    async def header(self) -> dict:
        async with self._lock:
            c = self._creds
            if c is None or not c.token or (c.expiry and c.expiry.timestamp() - time.time() < 300):
                await asyncio.to_thread(self._refresh)
        return {"Authorization": f"Bearer {self._creds.token}"}


class FirestoreStore(NullStore):
    enabled = True
    backend = "firestore"

    def __init__(self, project: str, database: str = "(default)", prefix: str = "ledgerflow",
                 base_url: str = "https://firestore.googleapis.com/v1", token=None, timeout_s: float = 5.0):
        if not project:
            raise SystemExit("DURABLE_STORE=firestore needs GCP_PROJECT (or GOOGLE_CLOUD_PROJECT)")
        self.root = f"{base_url.rstrip('/')}/projects/{project}/databases/{database}/documents"
        self.prefix = prefix
        self.token = token or _GoogleToken()
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._http: aiohttp.ClientSession | None = None
        self._pending: set[asyncio.Task] = set()

    def _coll(self, collection: str) -> str:
        return f"{self.prefix}_{collection}" if self.prefix else collection

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=self.timeout)
        return self._http

    async def put(self, collection: str, doc_id: str, record: dict, create_only: bool = False) -> bool:
        """Write one record. create_only: refuse to overwrite (the audit trail is append-only).
        Never raises: the SQLite copy already holds the record, so a failure is logged and counted."""
        coll = self._coll(collection)
        body = {"fields": {"json": {"stringValue": json.dumps(record, default=str, sort_keys=True)}}}
        for k in ("customer_id", "status", "type"):
            if isinstance(record.get(k), str):
                body["fields"][k] = {"stringValue": record[k]}
        quoted = urllib.parse.quote(doc_id, safe="")
        if create_only:
            url, method = f"{self.root}/{coll}?documentId={quoted}", "POST"
        else:
            url, method = f"{self.root}/{coll}/{quoted}", "PATCH"
        t0 = time.perf_counter()
        try:
            http = await self._session()
            async with http.request(method, url, json=body, headers=await self.token.header()) as r:
                if r.status == 409 and create_only:
                    return True                    # already stored: a retried write, not a failure
                if r.status >= 300:
                    raise RuntimeError(f"HTTP {r.status}: {(await r.text())[:200]}")
            METRICS.observe("durable.write", (time.perf_counter() - t0) * 1000)
            return True
        except Exception as e:  # noqa: BLE001 - durability must never break the customer's request
            METRICS.incr("durable.write_failed")
            logs.event(_log, "durable write failed", level=40, collection=coll, doc=doc_id, error=str(e)[:300])
            return False

    def put_soon(self, collection: str, doc_id: str, record: dict, create_only: bool = False) -> None:
        """Mirror from synchronous code (the audit log) without blocking the event loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.put(collection, doc_id, record, create_only))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def all(self, collection: str) -> list[dict]:
        """Every record of a collection, ordered by document id."""
        coll, out, page = self._coll(collection), [], ""
        http = await self._session()
        while True:
            q = {"pageSize": "300", "orderBy": "__name__"}
            if page:
                q["pageToken"] = page
            async with http.get(f"{self.root}/{coll}", params=q, headers=await self.token.header()) as r:
                if r.status == 404:
                    return out
                if r.status >= 300:
                    raise RuntimeError(f"Firestore list {coll}: HTTP {r.status}: {(await r.text())[:200]}")
                data = await r.json()
            for d in data.get("documents", []):
                out.append(json.loads(d["fields"]["json"]["stringValue"]))
            page = data.get("nextPageToken", "")
            if not page:
                return out

    async def drain(self) -> None:
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def close(self) -> None:
        await self.drain()
        if self._http is not None:
            await self._http.close()


def build_store(kind: str, project: str, database: str, prefix: str):
    kind = (kind or "none").lower()
    if kind == "none":
        return NullStore()
    if kind == "firestore":
        return FirestoreStore(project, database, prefix)
    raise SystemExit(f"DURABLE_STORE must be none | firestore, not {kind!r}")
