// Jingles: vorladen, dekodieren, zur Serverzeit sample-genau über Web Audio starten (SPEC Abschnitte 1, 6, 8).
//
// Der Jingle-Weg im Audio-Graphen ist vom Mikrofon getrennt: BufferSource → Gain → Ziel. Er erreicht weder den
// Rekorder (der hängt nur am Mikrofon) noch das Live-Gespräch (das sendet den Mikrofon-Track direkt).
// In der Folge sitzt ohnehin die Originaldatei an der Stelle; die Wiedergabe hier ist nur zum Hören.

const V = new URL(import.meta.url).search;
const { dbToGain } = await import(`./util.js${V}`);

export class JinglePlayer extends EventTarget {
  constructor(ctx, audioClock, serverClock, token) {
    super();
    this.ctx = ctx;
    this.audioClock = audioClock;
    this.serverClock = serverClock;
    this.token = token;
    this.buffers = new Map();   // sound_id → Promise<AudioBuffer>
    this.loaded = new Set();
    this.failed = new Set();
    this.playing = new Map();   // event_id → {src, gain, at, duration_ms, sound_id, stopAt, endsAt}
    this.master = new GainNode(ctx, { gain: 1 });
    this.master.connect(ctx.destination);
  }

  /** Alle Sounds des Raums vorladen. Bereits geladene bleiben im Speicher. */
  preload(sounds) {
    for (const s of sounds || []) this.load(s);
  }

  load(s) {
    if (this.buffers.has(s.id)) return this.buffers.get(s.id);
    const p = (async () => {
      const r = await fetch(s.url, { headers: { Authorization: `Bearer ${this.token()}` } });
      if (!r.ok) throw new Error("Sound nicht ladbar: " + r.status);
      const data = await r.arrayBuffer();
      const buf = await this.ctx.decodeAudioData(data);
      this.loaded.add(s.id);
      this.emit("loaded", { id: s.id });
      return buf;
    })();
    p.catch(() => { this.failed.add(s.id); this.buffers.delete(s.id); this.emit("loaded", { id: s.id, failed: true }); });
    this.buffers.set(s.id, p);
    return p;
  }

  /** Context-Zeit, zu der Serverzeit `serverMs` aus dem Lautsprecher kommen soll. */
  ctxTimeFor(serverMs) {
    return this.audioClock.ctxTimeForOutput(this.serverClock.serverToPerf(serverMs));
  }

  /** sound.play {event_id, sound_id, at, gain_db, duration_ms} oder Eintrag aus active_sounds. */
  async play(ev, sound) {
    if (this.playing.has(ev.event_id)) return;
    const entry = { sound_id: ev.sound_id, at: ev.at, duration_ms: ev.duration_ms ?? (sound && sound.duration_ms) ?? 0, src: null, gain: null, stopAt: null, endsAt: ev.at + (ev.duration_ms ?? 0) };
    this.playing.set(ev.event_id, entry);
    this.emit("change");
    let buf;
    try {
      buf = await (this.buffers.get(ev.sound_id) || (sound ? this.load(sound) : Promise.reject(new Error("unbekannt"))));
    } catch {
      this.playing.delete(ev.event_id);
      this.emit("change");
      return;
    }
    if (!this.playing.has(ev.event_id) || entry.cancelled) return;
    const ctx = this.ctx;
    const when = this.ctxTimeFor(ev.at);
    const now = ctx.currentTime;
    let offset = 0;
    let start = when;
    if (when < now + 0.005) { offset = now + 0.02 - when; start = now + 0.02; } // später Beitritt: an der richtigen Stelle einsteigen
    if (offset >= buf.duration) { this.playing.delete(ev.event_id); this.emit("change"); return; }
    const gain = new GainNode(ctx, { gain: dbToGain(ev.gain_db ?? (sound && sound.gain_db) ?? 0) });
    const src = new AudioBufferSourceNode(ctx, { buffer: buf });
    src.connect(gain).connect(this.master);
    src.start(start, offset);
    src.onended = () => {
      if (this.playing.get(ev.event_id) === entry) { this.playing.delete(ev.event_id); this.emit("change"); }
    };
    entry.src = src; entry.gain = gain;
    if (entry.pendingStop) this.stop(entry.pendingStop);
  }

  /** sound.stop {event_id, at, fade_ms}: zur Serverzeit ausblenden. */
  stop(ev) {
    const e = this.playing.get(ev.event_id);
    if (!e) return;
    e.stopAt = ev.at;
    e.endsAt = Math.min(e.endsAt, ev.at + (ev.fade_ms || 0));
    if (!e.src) { e.pendingStop = ev; this.emit("change"); return; }
    const ctx = this.ctx;
    const t = Math.max(ctx.currentTime, this.ctxTimeFor(ev.at));
    const fade = Math.max(0.01, (ev.fade_ms || 0) / 1000);
    const g = e.gain.gain;
    g.cancelScheduledValues(t);
    g.setValueAtTime(g.value, t);
    g.linearRampToValueAtTime(0, t + fade);
    try { e.src.stop(t + fade + 0.02); } catch { /* schon gestoppt */ }
    this.emit("change");
  }

  /** Raumzustand: laufende Sounds übernehmen, die hier noch nicht spielen (später Beitritt, Neuladen). */
  syncActive(active, soundsById) {
    const now = this.serverClock.now();
    for (const a of active || []) {
      if (this.playing.has(a.event_id)) continue;
      if (a.at + a.duration_ms <= now) continue;
      this.play({ event_id: a.event_id, sound_id: a.sound_id, at: a.at, duration_ms: a.duration_ms }, soundsById.get(a.sound_id));
    }
  }

  /** Restzeit in ms je laufendem Ereignis. */
  remaining() {
    const now = this.serverClock.now();
    const out = [];
    for (const [id, e] of this.playing) out.push({ event_id: id, sound_id: e.sound_id, left: Math.max(0, e.endsAt - now), stopping: e.stopAt != null, started: now >= e.at });
    return out;
  }

  emit(type, detail) { this.dispatchEvent(new CustomEvent(type, { detail })); }
}
