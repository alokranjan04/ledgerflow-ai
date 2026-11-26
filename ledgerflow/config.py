"""Configuration from .env only (one source of truth; see the voice-agent skill's
"placeholder trap"). Never log GEMINI_WS_URL — it contains the key."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_TEXT_MODEL = env("GEMINI_TEXT_MODEL", "gemini-flash-latest")
GEMINI_LIVE_MODEL = env("GEMINI_LIVE_MODEL", "models/gemini-3.1-flash-live-preview")
GEMINI_VOICE = env("GEMINI_VOICE", "Aoede")
GEMINI_REST = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage"
    ".v1beta.GenerativeService.BidiGenerateContent?key=" + GEMINI_API_KEY
)

PORT = int(env("PORT", "8080"))
PUBLIC_URL = env("PUBLIC_URL").rstrip("/")
# Connectors call the mock systems over HTTP; in one container that is loopback.
MOCK_BASE_URL = env("MOCK_BASE_URL", f"http://127.0.0.1:{PORT}/mock").rstrip("/")

AS_OF = date.fromisoformat(env("LEDGERFLOW_AS_OF")) if env("LEDGERFLOW_AS_OF") else date.today()
DATA_DIR = Path(env("DATA_DIR", str(ROOT / "data" / "local")))
DB_PATH = DATA_DIR / "ledgerflow.db"
AUDIT_DB_PATH = DATA_DIR / "audit.db"

# Telephony (optional phone channel)
TELEPHONY_PROVIDER = env("TELEPHONY_PROVIDER", "vobiz").lower()
STREAM_TOKEN = env("STREAM_TOKEN")
RECORD_CALLS = env("RECORD_CALLS", "0") == "1"
RECORDINGS_DIR = DATA_DIR / "recordings"
GCS_RECORDINGS_BUCKET = env("GCS_RECORDINGS_BUCKET")

# Admin: fault-injection panel and metrics reset. Empty = open (local demo only).
ADMIN_TOKEN = env("ADMIN_TOKEN")
SESSION_TTL_SECS = int(env("SESSION_TTL_SECS", "3600"))
DEFAULT_WORKFLOW = env("DEFAULT_WORKFLOW", "loan_servicing")
