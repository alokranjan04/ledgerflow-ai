"""Record the short "one moment" clips the voice pipeline plays while a tool runs.

Gemini TTS in the same prebuilt voice as the live agent (Aoede), saved as raw PCM16
mono 24 kHz, the format the pipeline already plays. Run once; the clips are committed.

    .venv/Scripts/python scripts/make_fillers.py
"""
from __future__ import annotations

import base64
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ledgerflow import config  # noqa: E402
from ledgerflow.voice.fillers import FILLER_DIR, PHRASES  # noqa: E402

MODEL = "gemini-3.8-flash-tts"


def synth(text: str, voice: str = "") -> bytes:
    body = {"contents": [{"parts": [{"text": text}]}],   # a style prefix gets read aloud by this model
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {
                "prebuiltVoiceConfig": {"voiceName": voice or config.GEMINI_VOICE}}}}}
    req = urllib.request.Request(f"{config.GEMINI_REST}/models/{MODEL}:generateContent",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-goog-api-key": config.GEMINI_API_KEY})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    part = data["candidates"][0]["content"]["parts"][0]["inlineData"]
    raw = base64.b64decode(part["data"])
    if "wav" in part["mimeType"]:
        import io
        import wave
        with wave.open(io.BytesIO(raw)) as w:
            assert w.getsampwidth() == 2, "expected 16-bit audio"
            rate, ch, raw = w.getframerate(), w.getnchannels(), w.readframes(w.getnframes())
    else:                                   # audio/L16;codec=pcm;rate=24000
        rate, ch = int(part["mimeType"].split("rate=")[1].split(";")[0]), 1
    from ledgerflow.voice.pipeline import audioop
    if ch == 2:
        raw = audioop.tomono(raw, 2, 0.5, 0.5)
    if rate != 24000:
        raw, _ = audioop.ratecv(raw, 2, 1, rate, 24000, None)
    return raw


def trim(pcm: bytes, threshold: int = 300) -> bytes:
    """Drop leading/trailing near-silence so the clip starts the instant it is played."""
    import array
    a = array.array("h", pcm)
    loud = [i for i, v in enumerate(a) if abs(v) > threshold]
    if not loud:
        return pcm
    start, end = max(0, loud[0] - 240), min(len(a), loud[-1] + 2400)     # 10 ms lead, 100 ms tail
    return a[start:end].tobytes()


if __name__ == "__main__":
    FILLER_DIR.mkdir(parents=True, exist_ok=True)
    for lang, text in PHRASES.items():
        pcm = trim(synth(text))
        (FILLER_DIR / f"{lang}.pcm").write_bytes(pcm)
        print(f"{lang}: {len(pcm) / 48000:.2f}s  {text}")
