"""Per-second caller/agent loudness for a stereo call recording.

    python scripts/analyze_recording.py recordings/<file>.wav
    gcloud storage cp gs://<bucket>/recordings/... rec.wav && python scripts/analyze_recording.py rec.wav

Reading it: speech is roughly 1000-6000 RMS, an open-but-silent line ~5-15,
digital silence 0. A caller column near 10 for the whole call while the agent
column shows speech means the phone leg carried no caller audio (telephony
problem), not an agent problem.
"""
import audioop
import sys
import wave


def main(path: str) -> None:
    with wave.open(path) as w:
        ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        data = w.readframes(n)
    print(f"{path}: {ch} channel(s), {rate} Hz, {n / rate:.1f}s")
    step = rate * width * ch
    for i in range(0, len(data), step):
        block = data[i:i + step]
        if ch == 2:
            caller = audioop.rms(audioop.tomono(block, width, 1, 0), width)
            agent = audioop.rms(audioop.tomono(block, width, 0, 1), width)
            bar = lambda v: "#" * min(30, v // 200)  # noqa: E731
            print(f"{i // step:4d}s  caller {caller:5d} {bar(caller):<30}  agent {agent:5d} {bar(agent)}")
        else:
            print(f"{i // step:4d}s  {audioop.rms(block, width):5d}  (mono: old recorder format)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
