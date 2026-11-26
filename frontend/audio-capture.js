// AudioWorklet: microphone -> PCM16 mono at 16 kHz, posted in ~40 ms chunks.
class Capture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.target = (options.processorOptions && options.processorOptions.targetRate) || 16000;
    this.ratio = sampleRate / this.target;
    this.pos = 0;
    this.buf = new Int16Array(Math.round(this.target * 0.04));
    this.n = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    // Linear-interpolation resampler; good enough for speech going to a speech model.
    while (this.pos < ch.length - 1) {
      const i = Math.floor(this.pos), f = this.pos - i;
      const s = ch[i] * (1 - f) + ch[i + 1] * f;
      this.buf[this.n++] = Math.max(-32768, Math.min(32767, Math.round(s * 32767)));
      if (this.n === this.buf.length) {
        this.port.postMessage(this.buf.buffer.slice(0));
        this.n = 0;
      }
      this.pos += this.ratio;
    }
    this.pos -= ch.length;
    return true;
  }
}
registerProcessor('capture', Capture);
