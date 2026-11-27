"""Server-side "one moment" clips, played the instant the agent starts a tool call.

Why on the server and not in the prompt: asking the live model to say a filler before
calling a tool made it skip the tool and invent figures (REQ-VOX-001.11). A pre-recorded
clip carries no figures, cannot change what the model does, and covers the tool round
plus the model's time to first audio, which is most of what the customer waits through.
"""
from __future__ import annotations

from functools import cache
from pathlib import Path

FILLER_DIR = Path(__file__).resolve().parent / "fillers"
# language -> what is said (recorded by scripts/make_fillers.py). Hinglish callers hear the Hindi clip.
PHRASES = {"en": "One moment, let me check.", "hi": "एक पल, मैं देखती हूँ।"}
# Tools that do not make the customer wait for an answer: no clip.
SKIP_TOOLS = {"end_call"}
FRAME_BYTES = 960 * 2          # 40 ms of PCM16 24 kHz: the size the model's own audio arrives in


@cache
def clip(language: str) -> bytes:
    lang = "hi" if language in ("hi", "hi-Latn") else "en"
    path = FILLER_DIR / f"{lang}.pcm"
    return path.read_bytes() if path.exists() else b""


def frames(pcm: bytes) -> list[bytes]:
    return [pcm[i:i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]
