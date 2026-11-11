// Zeitmodell (SPEC Abschnitt 4).
//
// ServerClock: Abgleich mit der Serveruhr. Alle 2 s `sync {t0}`, Offset = server_ms − (t0 + t1) / 2. Verwendet wird
// die Messung mit der kleinsten Laufzeit der letzten 15 (bei symmetrischem Netz ist sie die genaueste).
//
// AudioClock: Abbildung zwischen AudioContext-Frames und performance.now().
//  - Aufnahme: Wann wurde Frame F aufgenommen? Maßgeblich ist die Ankunftszeit der Worklet-Blöcke im Hauptthread:
//    Ein Block kann nicht vor seiner Verarbeitung ankommen, die frühste Ankunft relativ zur Frame-Nummer (Minimum
//    über ein gleitendes Fenster von 20 s) ist deshalb die Verarbeitungszeit, unabhängig von Browser und Gerät.
//    Gemessen (Chrome, Linux): nach 2 bis 3 s auf unter 1 ms stabil. getOutputTimestamp() minus outputLatency lag
//    dort 12 ms daneben (hängt davon ab, wie ehrlich der Browser die Ausgangslatenz meldet) und fehlt in manchen
//    Browsern; ein Wechsel zwischen beiden Schätzern hätte Sprünge erzeugt. Es wird nur zur Kontrolle mitgeschrieben.
//    Von der Verarbeitungszeit wird die Eingangslatenz abgezogen (MediaStreamTrack.getSettings().latency, sonst 0).
//  - Wiedergabe (Jingles): Welcher Context-Zeitpunkt kommt zur Serverzeit X aus dem Lautsprecher? Dafür ist
//    getOutputTimestamp() gemacht; Rückfall currentTime plus Ausgangslatenz.

export class ServerClock {
  constructor() {
    this.samples = [];
    this.offset = null;   // server_ms − lokale Epoch-ms
    this.rtt = null;
    this.count = 0;
  }
  local() { return performance.timeOrigin + performance.now(); }
  request() { return { type: "sync", t0: this.local() }; }
  onSync(msg) {
    const t1 = this.local();
    const t0 = Number(msg.t0);
    const rtt = t1 - t0;
    if (!isFinite(rtt) || rtt < 0 || rtt > 15000) return;
    this.samples.push({ rtt, offset: msg.server_ms - (t0 + t1) / 2, at: t1 });
    if (this.samples.length > 15) this.samples.shift();
    let best = this.samples[0];
    for (const s of this.samples) if (s.rtt < best.rtt) best = s;
    this.offset = best.offset;
    this.rtt = best.rtt;
    this.count++;
  }
  get ready() { return this.offset != null && this.count >= 3; }
  /** Grober Offset aus `welcome` bis zum ersten echten Abgleich. */
  seed(serverMs) { if (this.offset == null) this.offset = serverMs - this.local(); }
  now() { return this.local() + (this.offset || 0); }
  /** Serverzeit → performance.now()-Zeit (ms). */
  serverToPerf(serverMs) { return serverMs - (this.offset || 0) - performance.timeOrigin; }
  /** performance.now()-Zeit → Serverzeit. */
  perfToServer(perfMs) { return perfMs + performance.timeOrigin + (this.offset || 0); }
}

const WINDOW_MS = 20000;
const MATURE_MS = 3000; // so lange messen, bevor eine Spur ihren Startzeitpunkt bekommt

export class AudioClock {
  constructor(ctx) {
    this.ctx = ctx;
    this.sr = ctx.sampleRate;
    this.arrivals = [];      // {at, d}: d = Ankunft (perf ms) − Frame-Zeit (ms) für Rückfall b
    this.tsOffsets = [];     // Messungen aus getOutputTimestamp: perf − ctxTime·1000 − Ausgangslatenz
    this.inputLatencyMs = 0;
    this.source = "none";
  }

  /** Der Rekorder meldet jeden Block: Frame-Nummer des Blockendes und Ankunftszeit. */
  noteArrival(endFrame, perfMs) {
    const d = perfMs - (endFrame / this.sr) * 1000;
    const a = this.arrivals;
    a.push({ at: perfMs, d });
    while (a.length && perfMs - a[0].at > WINDOW_MS) a.shift();
  }

  /** Eine Messung aus getOutputTimestamp aufnehmen (alle paar hundert ms aufrufen). */
  sampleOutput() {
    const ctx = this.ctx;
    if (typeof ctx.getOutputTimestamp !== "function" || ctx.state !== "running") return;
    const ts = ctx.getOutputTimestamp();
    if (!ts || !(ts.performanceTime > 0) || !(ts.contextTime > 0)) return;
    const outLat = this.outputLatency();
    const now = performance.now();
    // Plausibel: Der ausgegebene Zeitpunkt liegt höchstens 1 s neben jetzt
    if (Math.abs(ts.performanceTime - now) > 1000) return;
    const v = ts.performanceTime - ts.contextTime * 1000 - outLat * 1000;
    const arr = this.tsOffsets;
    arr.push({ at: now, v });
    while (arr.length && now - arr[0].at > WINDOW_MS) arr.shift();
  }

  outputLatency() {
    const ctx = this.ctx;
    if (typeof ctx.outputLatency === "number" && ctx.outputLatency > 0) return ctx.outputLatency;
    return typeof ctx.baseLatency === "number" ? ctx.baseLatency : 0;
  }

  /** Offset perf − Frame-Zeit für die Verarbeitung im Audio-Thread, oder null, solange die Messung zu jung ist. */
  processingOffset() {
    const a = this.arrivals;
    if (a.length < 20 || a[a.length - 1].at - a[0].at < MATURE_MS) return null;
    let m = Infinity;
    for (const x of a) if (x.d < m) m = x.d;
    this.source = "worklet-arrival";
    return m;
  }

  /** Kontrollwert: Abstand des getOutputTimestamp-Schätzers zum maßgeblichen (ms), oder null. */
  outputTimestampBias() {
    const off = this.processingOffset();
    if (off == null || this.tsOffsets.length < 5) return null;
    const vs = this.tsOffsets.map((x) => x.v).sort((x, y) => x - y);
    return vs[vs.length >> 1] - off;
  }

  /** perf-Zeit (ms), zu der Frame F am Eingang aufgenommen wurde. */
  captureTimeOfFrame(frame) {
    const off = this.processingOffset();
    if (off == null) return null;
    return (frame / this.sr) * 1000 + off - this.inputLatencyMs;
  }

  /** Context-Zeit (s), die zur perf-Zeit `perfMs` aus dem Lautsprecher kommt. */
  ctxTimeForOutput(perfMs) {
    const ctx = this.ctx;
    if (typeof ctx.getOutputTimestamp === "function") {
      const ts = ctx.getOutputTimestamp();
      if (ts && ts.performanceTime > 0 && ts.contextTime > 0 && Math.abs(ts.performanceTime - performance.now()) < 1000) {
        return ts.contextTime + (perfMs - ts.performanceTime) / 1000;
      }
    }
    return ctx.currentTime + (perfMs - performance.now()) / 1000 - this.outputLatency();
  }
}
