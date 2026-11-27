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
GEMINI_TEXT_MODEL = env("GEMINI_TEXT_MODEL", "gemini-flash-lite-latest")
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

# Admin: fault-injection panel and metrics reset. Empty = open only when running
# locally; a deployed service (PUBLIC_URL set) refuses admin calls without a token.
ADMIN_TOKEN = env("ADMIN_TOKEN")
SESSION_TTL_SECS = int(env("SESSION_TTL_SECS", "3600"))
DEFAULT_WORKFLOW = env("DEFAULT_WORKFLOW", "loan_servicing")

# LLM resilience: a second model when the primary fails, and an end-to-end turn budget.
GEMINI_FALLBACK_MODEL = env("GEMINI_FALLBACK_MODEL", "gemini-flash-latest")
# The tools do the reasoning; the model only routes and phrases, so deep "thinking" adds seconds
# per call for no accuracy gain (measured: flash default p50 3.0 s vs flash-lite + low 0.7 s).
GEMINI_THINKING_LEVEL = env("GEMINI_THINKING_LEVEL", "low")
# Fast lane: single, simple questions (next EMI, failed payments, foreclosure...) answered straight
# from the systems with no model call. Always used as the fallback when the model is down.
FAST_LANE = env("FAST_LANE", "1") == "1"
LLM_TURN_BUDGET_S = float(env("LLM_TURN_BUDGET_S", "30"))

# Abuse limits per session (token buckets).
CHAT_TURNS_PER_MIN = int(env("CHAT_TURNS_PER_MIN", "20"))
VOICE_STARTS_PER_MIN = int(env("VOICE_STARTS_PER_MIN", "4"))

# Logging: JSON for Cloud Logging; plain text is easier to read locally.
LOG_JSON = env("LOG_JSON", "1") == "1"
LOG_LEVEL = env("LOG_LEVEL", "INFO")
GCP_PROJECT = env("GOOGLE_CLOUD_PROJECT") or env("GCP_PROJECT")


# ---- Scale-out ------------------------------------------------------------
# all      : gateway + simulated systems in one process (single instance, default)
# gateway  : stateless gateway; systems reached at MOCK_BASE_URL; state in Redis
# systems  : only the simulated enterprise systems (/mock/*), internal callers only
SERVICE_ROLE = env("SERVICE_ROLE", "all").lower()
# Shared state across instances (Memorystore). Empty = in-process state.
REDIS_URL = env("REDIS_URL")
# Shared secret between gateway and systems service. Empty in "all" = random per process.
INTERNAL_TOKEN = env("INTERNAL_TOKEN")
# When the systems service is private on Cloud Run, the gateway sends a Google ID
# token for this audience (the systems service URL). Empty = no ID token.
SYSTEMS_AUDIENCE = env("SYSTEMS_AUDIENCE")


# ---- Step-up verification before write actions ------------------------------
STEP_UP_OTP = env("STEP_UP_OTP", "1") == "1"
STEP_UP_VALID_SECS = int(env("STEP_UP_VALID_SECS", "600"))
# demo = simulated SMS shown on the customer's own screen (PoC). Production: an SMS/WhatsApp gateway.
SMS_PROVIDER = env("SMS_PROVIDER", "demo")


def deployed() -> bool:
    return bool(PUBLIC_URL)
