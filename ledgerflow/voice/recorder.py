"""Stereo call recording on a shared wall-clock timeline (8 kHz, 16-bit).

Left = caller, right = agent. Caller audio arrives in real time. Agent audio
arrives from Gemini faster than real time and the phone plays it back-to-back,
so each chunk is placed where it will actually be heard. A barge-in clear drops
agent audio that was queued but never played.

Appending both directions to one list in arrival order (the naive approach)
chops the agent's speech into slivers and makes the file longer than the call.
"""
import time
import warnings
import wave

with warnings.catch_warnings():   # stdlib audioop is deprecated in 3.12 and gone in 3.13,
    warnings.simplefilter("ignore", DeprecationWarning)   # where the audioop-lts package provides it
    import audioop  # noqa: E402

BYTES_PER_SEC = 8000 * 2


class CallRecorder:
    def __init__(self):
        self.t0 = time.time()
        self.caller = bytearray()
        self.agent = bytearray()

    def _now_pos(self) -> int:
        return int((time.time() - self.t0) * BYTES_PER_SEC) & ~1

    def add_caller(self, pcm8: bytes) -> None:
        start = max(len(self.caller), self._now_pos() - len(pcm8))
        self.caller.extend(b"\x00" * (start - len(self.caller)))
        self.caller.extend(pcm8)

    def add_agent(self, pcm8: bytes) -> None:
        start = max(len(self.agent), self._now_pos())
        self.agent.extend(b"\x00" * (start - len(self.agent)))
        self.agent.extend(pcm8)

    def clear_agent(self) -> None:
        del self.agent[self._now_pos():]

    def __bool__(self) -> bool:
        return bool(self.caller or self.agent)

    def write_wav(self, path) -> None:
        n = max(len(self.caller), len(self.agent))
        left = bytes(self.caller) + b"\x00" * (n - len(self.caller))
        right = bytes(self.agent) + b"\x00" * (n - len(self.agent))
        stereo = audioop.add(audioop.tostereo(left, 2, 1, 0),
                             audioop.tostereo(right, 2, 0, 1), 2)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(8000)
            wf.writeframes(stereo)
