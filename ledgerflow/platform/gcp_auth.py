"""Service-to-service auth on Cloud Run: Google-signed ID tokens.

The gateway runs as its own service account; the private systems service only
lets that account invoke it (roles/run.invoker). Tokens come from the metadata
server via google-auth (keyless) and are cached until shortly before expiry.
"""
from __future__ import annotations

import asyncio
import time


class IdTokenProvider:
    def __init__(self, audience: str, fetch=None, lifetime_s: float = 3000.0):
        self.audience = audience
        self._fetch = fetch or self._google_fetch
        self.lifetime_s = lifetime_s          # Google ID tokens last 1 h; refresh after 50 min
        self._token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    def _google_fetch(audience: str) -> str:
        import google.auth.transport.requests
        import google.oauth2.id_token
        return google.oauth2.id_token.fetch_id_token(google.auth.transport.requests.Request(), audience)

    async def headers(self) -> dict:
        async with self._lock:
            if self._token is None or time.time() >= self._expires:
                self._token = await asyncio.to_thread(self._fetch, self.audience)
                self._expires = time.time() + self.lifetime_s
        return {"Authorization": f"Bearer {self._token}"}
