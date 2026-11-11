// AudioWorklet-Prozessor des Rekorders. Läuft im Audio-Thread.
//
// Liefert die Samples des Eingangs als Float32-Blöcke an den Hauptthread, jeweils mit der Nummer des ersten
// Frames auf der Zeitachse des AudioContext (`currentFrame`). Diese Zählung ist die verlässlichste, die der Browser
// hat: Sie läuft mit jedem Render-Quantum weiter, auch wenn der Hauptthread hängt oder der Tab im Hintergrund ist.
// Der Hauptthread erkennt Lücken (fehlende Frame-Nummern) und füllt sie mit Stille, statt Samples zu verschieben.
//
// Stumm heißt hier: Nullen schreiben, Zählung läuft weiter. Der Pegel wird trotzdem vom unbearbeiteten Eingang
// gemessen, damit die Oberfläche zeigen kann, dass bei Stummschaltung gesprochen wird.

const CLIP = 0.999; // ab hier gilt ein Sample als übersteuert (−0,01 dBFS)

class RecorderProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    this.blockFrames = o.blockFrames || 2048;   // ~43 ms bei 48 kHz: genug Takt für den Hauptthread, wenig Nachrichten
    this.meterFrames = Math.round(sampleRate / 10); // 10 Pegelmeldungen je Sekunde
    this.buf = new Float32Array(this.blockFrames);
    this.n = 0;
    this.start = 0;          // currentFrame des ersten Samples in buf
    this.mute = false;
    this.peak = 0; this.sq = 0; this.mn = 0; this.clip = false;
    this.running = true;
    this.port.onmessage = (e) => {
      const d = e.data || {};
      if ("mute" in d) this.mute = !!d.mute;
      if (d.flush) this.flush();
      if (d.stop) { this.flush(); this.running = false; }
    };
  }

  flush() {
    if (this.n === 0) return;
    const data = this.n === this.buf.length ? this.buf : this.buf.slice(0, this.n);
    this.port.postMessage({ t: "pcm", frame: this.start, data }, [data.buffer]);
    this.buf = new Float32Array(this.blockFrames);
    this.n = 0;
  }

  process(inputs, outputs) {
    if (!this.running) return false;
    const input = inputs[0];
    const ch = input && input.length ? input[0] : null;
    const out = outputs[0] && outputs[0][0];
    const frames = ch ? ch.length : (out ? out.length : 128);
    const f0 = currentFrame;
    if (this.n > 0 && this.start + this.n !== f0) this.flush(); // Sprung in der Zählung: Block abschließen
    if (this.n === 0) this.start = f0;
    for (let i = 0; i < frames; i++) {
      const s = ch ? ch[i] : 0;
      const a = s < 0 ? -s : s;
      if (a > this.peak) this.peak = a;
      if (a >= CLIP) this.clip = true;
      this.sq += s * s;
      this.mn++;
      this.buf[this.n++] = this.mute ? 0 : s;
      if (this.n === this.buf.length) { this.flush(); this.start = f0 + i + 1; }
    }
    if (this.mn >= this.meterFrames) {
      this.port.postMessage({ t: "meter", peak: this.peak, rms: Math.sqrt(this.sq / this.mn), clip: this.clip, frame: f0 + frames });
      this.peak = 0; this.sq = 0; this.mn = 0; this.clip = false;
    }
    return true;
  }
}

registerProcessor("ps-recorder", RecorderProcessor);
