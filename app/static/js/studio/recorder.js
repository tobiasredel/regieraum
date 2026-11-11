// Rekorder (SPEC Abschnitte 4, 7, 8 und 16). Von ihm hängt die Folge ab, deshalb die Regeln vorweg:
//
//  - Gezählt wird in Frames des AudioContext (`currentFrame` im Worklet). Jede Spur beginnt bei einem bestimmten
//    Context-Frame; Sample i der Spur ist Context-Frame frame0 + i. Fehlt im Strom ein Stück (Lücke in der Zählung),
//    wird es mit Stille gefüllt, doppelte Stücke werden verworfen. So läuft first_frame lückenlos fort.
//  - Chunks sind 2 s lang, s24le mono. Jeder Chunk wird erst lokal gesichert (store.js), dann hochgeladen, nach der
//    Bestätigung gelöscht. Fehlschläge werden mit wachsendem Abstand wiederholt.
//  - Vor `rec.start` hält der Rekorder höchstens 60 s Vorlauf im Speicher und lädt nichts hoch. Beim Start wird die
//    Spur angelegt; sie beginnt einige Sekunden vor `start_at` (den Rest schneidet die Pipeline weg).
//  - Nach `rec.stop` nimmt der Rekorder weiter auf, bis das Ende (Stopp oder späteres Ende eines laufenden Jingles)
//    sicher im Kasten ist, schließt die Spur ab und ruft `finish` auf. Fehlende Chunks werden nachgeladen.
//  - Nach dem Neuladen der Seite werden liegengebliebene Spuren aus dem lokalen Speicher nachgeladen und
//    abgeschlossen, bevor Neues beginnt. Eine Spur gehört dem Tab, der sie per Web Lock hält.

const V = new URL(import.meta.url).search;
const { api, ApiError, sleep } = await import(`./util.js${V}`);
const { t } = await import(`../i18n.js${V}`);

const CHUNK_SECONDS = 2;
const PREROLL_MAX_S = 60;          // SPEC: höchstens 60 s Vorlauf
const PREROLL_KEEP_MS = 5000;      // davon vor start_at hochladen
const STOP_MARGIN_MS = 1200;       // nach dem Ende noch so lange aufnehmen
const CLOCK_EVERY_MS = 10000;      // Uhrenpunkt alle 10 s
const RECORDED_EVERY_MS = 2000;    // track.recorded für die Fortschrittsanzeige
const JUMP_LIMIT_MS = 150;         // Sprung zwischen zwei Uhrenpunkten, ab dem eine neue Spur beginnt
const PARALLEL_UPLOADS = 2;
const MEM_CACHE_BYTES = 64 << 20;  // so viel Chunk-Daten bleiben zusätzlich im Speicher

function encodeS24(src, dst, offsetFrames) {
  let o = offsetFrames * 3;
  for (let i = 0; i < src.length; i++) {
    let s = src[i];
    if (!(s === s)) s = 0; // NaN
    s = s < -1 ? -1 : s > 1 ? 1 : s;
    const v = s < 0 ? Math.round(s * 8388608) : Math.round(s * 8388607);
    dst[o++] = v & 0xff;
    dst[o++] = (v >> 8) & 0xff;
    dst[o++] = (v >> 16) & 0xff;
  }
}

const hasLocks = () => !!(navigator.locks && typeof navigator.locks.request === "function");

export class Recorder extends EventTarget {
  /**
   * @param {object} o  ctx, audioClock, serverClock, store, token(), participantId, send(msg), workletUrl
   */
  constructor(o) {
    super();
    Object.assign(this, o);
    // Ohne AudioContext nutzbar für das Nachladen nach einem Neuladen; init() hängt das Audio später an
    this.sr = this.ctx ? this.ctx.sampleRate : 48000;
    this.chunkFrames = Math.round(CHUNK_SECONDS * this.sr);
    this.timer = setInterval(() => this.tick(), 500);
    this.cur = null;            // laufender Chunk {frame0, bytes, n}
    this.preroll = [];          // fertige Chunks ohne Spur
    this.expected = null;       // nächster erwarteter Context-Frame
    this.track = null;          // aktive Spur
    this.creating = null;       // Promise, solange eine Spur angelegt wird
    this.tracks = new Map();    // alle Spuren dieses Tabs inkl. nachgeladener
    this.queue = [];            // Upload-Warteschlange
    this.active = 0;
    this.backoff = 0;
    this.backoffUntil = 0;
    this.memBytes = 0;
    this.persistQ = Promise.resolve();
    this.gapFrames = 0;
    this.droppedFrames = 0;
    this.gaps = [];
    this.lostChunks = 0;
    this.muted = false;
    this.fatal = null;
    this.trackMeta = {};
    this.stopAtServer = null;
    this.lastMeter = null;
  }

  // ---------------------------------------------------------------- Audio
  async init(ctx, audioClock) {
    if (ctx) { this.ctx = ctx; this.audioClock = audioClock; }
    this.sr = this.ctx.sampleRate;
    this.chunkFrames = Math.round(CHUNK_SECONDS * this.sr);
    await this.ctx.audioWorklet.addModule(this.workletUrl);
    this.node = new AudioWorkletNode(this.ctx, "ps-recorder", {
      numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1], channelCount: 1, channelCountMode: "explicit",
      channelInterpretation: "speakers", processorOptions: { blockFrames: 2048 },
    });
    // Ausgang stumm an das Ziel hängen, damit der Graph den Knoten in jedem Browser verarbeitet
    this.sink = new GainNode(this.ctx, { gain: 0 });
    this.node.connect(this.sink).connect(this.ctx.destination);
    this.node.port.onmessage = (e) => this.onWorklet(e.data);
    this.ctx.addEventListener("statechange", () => {
      if (this.ctx.state !== "running" && this.track) this.track.interrupted = true;
    });
  }

  /** Mikrofon (oder Testquelle) anschließen. `meta` beschreibt die Einstellungen für die Spur. */
  attach(stream, meta) {
    if (this.source) { try { this.source.disconnect(); } catch { /* egal */ } }
    this.stream = stream;
    this.source = new MediaStreamAudioSourceNode(this.ctx, { mediaStream: stream });
    this.source.connect(this.node);
    this.trackMeta = meta || {};
    const t = stream.getAudioTracks()[0];
    const lat = t && t.getSettings ? t.getSettings().latency : undefined;
    this.audioClock.inputLatencyMs = typeof lat === "number" && lat > 0 && lat < 1 ? lat * 1000 : 0;
  }

  setMute(on) {
    this.muted = !!on;
    if (this.node) this.node.port.postMessage({ mute: this.muted });
  }

  onWorklet(d) {
    if (d.t === "meter") {
      this.lastMeter = d;
      this.dispatchEvent(new CustomEvent("meter", { detail: d }));
      return;
    }
    if (d.t !== "pcm") return;
    let frame = d.frame;
    let data = d.data;
    this.audioClock.noteArrival(frame + data.length, performance.now());
    if (this.expected != null) {
      if (frame > this.expected) {
        const gap = frame - this.expected;
        this.gapFrames += gap;
        if (this.gaps.length < 50) this.gaps.push({ frame: this.expected, frames: gap, track: !!this.track });
        this.appendZeros(gap);
      } else if (frame < this.expected) {
        const skip = this.expected - frame;
        this.droppedFrames += Math.min(skip, data.length);
        if (skip >= data.length) return;
        data = data.subarray(skip);
        frame = this.expected;
      }
    }
    this.append(data, frame);
    this.expected = frame + data.length;
    const T = this.track;
    if (T && T.stopAtServer != null) {
      const at = this.serverMsOfFrame(this.expected);
      if (at != null && at >= T.stopAtServer) this.finishActive("stop");
    }
  }

  appendZeros(n) {
    const z = new Float32Array(Math.min(n, this.chunkFrames));
    let left = n;
    let f = this.expected;
    while (left > 0) {
      const k = Math.min(left, z.length);
      this.append(k === z.length ? z : z.subarray(0, k), f);
      f += k; left -= k;
    }
  }

  append(data, frame) {
    let i = 0;
    while (i < data.length) {
      if (!this.cur) this.cur = { frame0: frame + i, bytes: new Uint8Array(this.chunkFrames * 3), n: 0 };
      const c = this.cur;
      const take = Math.min(this.chunkFrames - c.n, data.length - i);
      encodeS24(data.subarray(i, i + take), c.bytes, c.n);
      c.n += take; i += take;
      if (c.n === this.chunkFrames) { this.cur = null; this.completeChunk(c); }
    }
  }

  completeChunk(c) {
    const chunk = { frame0: c.frame0, frames: c.n, bytes: c.n * 3 === c.bytes.length ? c.bytes : c.bytes.slice(0, c.n * 3) };
    if (this.track && !this.creating) { this.addToTrack(this.track, chunk); return; }
    this.preroll.push(chunk);
    if (!this.creating) {
      const max = Math.ceil(PREROLL_MAX_S / CHUNK_SECONDS);
      while (this.preroll.length > max) this.preroll.shift();
    }
  }

  // ---------------------------------------------------------------- Zeit
  /** Serverzeit (ms), zu der Context-Frame F aufgenommen wurde, oder null. */
  serverMsOfFrame(frame) {
    const p = this.audioClock.captureTimeOfFrame(frame);
    if (p == null || this.serverClock.offset == null) return null;
    return this.serverClock.perfToServer(p);
  }

  /** Neuester Frame, der im Hauptthread angekommen ist (plus laufender Chunk). */
  get headFrame() { return this.expected; }

  // ---------------------------------------------------------------- Spuren
  get recording() { return !!(this.track || this.creating); }

  /** Spur für die laufende Aufnahme anlegen, falls noch keine da ist. `startAt`: Serverzeit von rec.start. */
  async ensureTrack(episodeId, startAt) {
    if (this.fatal || this.track || this.creating) return this.creating;
    this.creating = this._create(episodeId, startAt).finally(() => { this.creating = null; });
    return this.creating;
  }

  async _create(episodeId, startAt) {
    for (let attempt = 0; ; attempt++) {
      while (this.expected == null || !this.serverClock.ready || this.audioClock.processingOffset() == null) await sleep(100);
      if (startAt != null) {
        const cut = startAt - PREROLL_KEEP_MS;
        while (this.preroll.length) {
          const c = this.preroll[0];
          const end = this.serverMsOfFrame(c.frame0 + c.frames);
          if (end != null && end < cut) this.preroll.shift(); else break;
        }
      }
      const frame0 = this.preroll.length ? this.preroll[0].frame0 : this.cur ? this.cur.frame0 : this.expected;
      const startServer = Math.round(this.serverMsOfFrame(frame0));
      const settings = (this.stream && this.stream.getAudioTracks()[0] && this.stream.getAudioTracks()[0].getSettings()) || {};
      const meta = {
        user_agent: navigator.userAgent,
        device_label: this.trackMeta.device_label || "",
        echo_cancellation: !!settings.echoCancellation,
        noise_suppression: !!settings.noiseSuppression,
        auto_gain: !!settings.autoGainControl,
        headphones: !!this.trackMeta.headphones,
        context_sample_rate: this.sr,
        clock_source: this.audioClock.source,
        output_timestamp_bias_ms: (() => { const b = this.audioClock.outputTimestampBias(); return b == null ? null : Math.round(b * 10) / 10; })(),
        input_latency_ms: Math.round(this.audioClock.inputLatencyMs),
        test_signal: this.trackMeta.test_signal || undefined,
      };
      let res;
      try {
        res = await api("POST", "/api/p/tracks", { token: this.token(), body: { sample_rate: this.sr, format: "s24le", start_server_ms: startServer, meta } });
      } catch (e) {
        if (e.status === 401) { this.setFatal(e); return null; }
        if (e.status === 409) { // uploading: die letzte Aufnahme lädt noch, später erneut (der Raum meldet sich)
          this.emit("track-error", { message: e.message });
          return null;
        }
        await sleep(Math.min(10000, 1000 * 2 ** Math.min(attempt, 4)));
        continue;
      }
      const T = this.newTrack(res.track_id, episodeId, frame0, startServer);
      this.track = T;
      await this.saveMeta(T);
      // Vorlauf und alles, was während des Anlegens fertig wurde, gehört jetzt zur Spur
      const pre = this.preroll;
      this.preroll = [];
      for (const c of pre) if (c.frame0 >= frame0) this.addToTrack(T, c);
      T.clockQueue.push({ sample_index: 0, server_ms: startServer });
      T.lastClock = { idx: 0, ms: startServer, at: performance.now() };
      this.postClock(T);
      this.holdLock(T);
      this.emit("track", { track: T });
      return T;
    }
  }

  newTrack(id, episodeId, frame0, startServer) {
    const T = {
      id, episodeId, frame0, startServer, nextSeq: 0, frames: 0, uploadedFrames: 0, pending: new Set(),
      closing: false, finishing: false, done: false, lost: [], clockQueue: [], lastClock: null, stopAtServer: null,
      totalChunks: null, totalFrames: null, recovered: false, createdAt: Date.now(), lastRecordedSent: 0,
    };
    this.tracks.set(id, T);
    return T;
  }

  holdLock(T) {
    if (!hasLocks()) return;
    T.lockDone = new Promise((res) => { T.releaseLock = res; });
    navigator.locks.request("ps-track-" + T.id, () => T.lockDone).catch(() => {});
  }

  metaOf(T) {
    return {
      track_id: T.id, owner: this.participantId, episode_id: T.episodeId, sample_rate: this.sr, next_seq: T.nextSeq,
      frames: T.frames, closed: T.closing, total_chunks: T.totalChunks, total_frames: T.totalFrames,
      clock: T.clockQueue.slice(-200), updated_at: Date.now(),
    };
  }

  saveMeta(T) {
    const m = this.metaOf(T);
    this.persistQ = this.persistQ.then(() => this.store.setMeta(T.id, m)).catch((e) => this.storeError(e));
    return this.persistQ;
  }

  storeError(e) {
    if (!this.storeFailed) {
      this.storeFailed = true;
      this.emit("store-error", { message: t("Lokale Sicherung nicht möglich. Die Aufnahme läuft, ist bei einem Neuladen aber nicht geschützt."), error: String(e) });
    }
  }

  addToTrack(T, chunk) {
    const seq = T.nextSeq++;
    const firstFrame = chunk.frame0 - T.frame0;
    if (firstFrame !== T.frames) console.warn("Rekorder: first_frame passt nicht", firstFrame, T.frames);
    T.frames += chunk.frames;
    T.pending.add(seq);
    const item = { T, seq, firstFrame, frames: chunk.frames, bytes: chunk.bytes, tries: 0 };
    const meta = this.metaOf(T);
    this.persistQ = this.persistQ
      .then(() => this.store.putChunk(T.id, seq, firstFrame, chunk.frames, chunk.bytes))
      .then(() => this.store.setMeta(T.id, meta))
      .catch((e) => this.storeError(e))
      .then(() => this.enqueue(item));
  }

  enqueue(item) {
    if (item.bytes) {
      // über der Grenze aus dem lokalen Speicher nachladen, außer der ist ausgefallen
      if (this.memBytes + item.bytes.length > MEM_CACHE_BYTES && !this.storeFailed) item.bytes = null;
      else this.memBytes += item.bytes.length;
    }
    this.queue.push(item);
    this.pump();
  }

  /** Aktive Spur abschließen (Stopp, Gerätewechsel, Unterbrechung). */
  finishActive(reason) {
    const T = this.track;
    if (!T) return null;
    if (this.cur && this.cur.n > 0) {
      const c = this.cur;
      this.cur = null;
      this.addToTrack(T, { frame0: c.frame0, frames: c.n, bytes: c.bytes.slice(0, c.n * 3) });
    }
    this.track = null;
    T.closing = true;
    T.closeReason = reason;
    T.totalChunks = T.nextSeq;
    T.totalFrames = T.frames;
    const endMs = this.serverMsOfFrame(T.frame0 + T.frames);
    if (endMs != null) T.clockQueue.push({ sample_index: T.frames, server_ms: Math.round(endMs) });
    this.saveMeta(T);
    this.emit("track-closing", { track: T, reason });
    this.persistQ.then(() => this.checkFinish(T));
    return T;
  }

  /** rec.stop: bis `untilServer` weiter aufnehmen, dann abschließen. */
  stopAt(untilServer) {
    const T = this.track;
    if (T) T.stopAtServer = untilServer + STOP_MARGIN_MS;
    if (this.creating) this.creating.then((t) => { if (t && this.track === t) t.stopAtServer = untilServer + STOP_MARGIN_MS; });
  }

  // ---------------------------------------------------------------- Uhrenpunkte
  async postClock(T) {
    if (T.clockBusy || !T.clockQueue.length) return;
    T.clockBusy = true;
    const pts = T.clockQueue.slice(0, 500);
    try {
      await api("POST", `/api/p/tracks/${T.id}/clock`, { token: this.token(), body: { points: pts } });
      T.clockQueue.splice(0, pts.length);
    } catch (e) {
      if (e.status === 404 || e.status === 403 || e.status === 422) T.clockQueue.splice(0, pts.length);
    } finally {
      T.clockBusy = false;
    }
  }

  clockPoint(T) {
    if (this.expected == null) return;
    const idx = this.expected - T.frame0;
    const ms = this.serverMsOfFrame(this.expected);
    if (ms == null || idx <= 0) return;
    const p = { sample_index: idx, server_ms: Math.round(ms) };
    if (T.lastClock) {
      const want = ((idx - T.lastClock.idx) / this.sr) * 1000;
      const got = ms - T.lastClock.ms;
      if (Math.abs(got - want) > JUMP_LIMIT_MS) {
        // Die Zählung passt nicht mehr zur Uhr (Context war unterbrochen o. Ä.): neue Spur
        this.emit("discontinuity", { track: T, want, got });
        return;
      }
    }
    T.lastClock = { idx, ms, at: performance.now() };
    T.clockQueue.push(p);
    this.postClock(T);
  }

  // ---------------------------------------------------------------- Takt
  tick() {
    if (this.audioClock) this.audioClock.sampleOutput();
    const T = this.track;
    const now = performance.now();
    if (T && !this.creating) {
      if (T.interrupted && this.ctx.state === "running") { T.interrupted = false; this.emit("discontinuity", { track: T, reason: "interrupted" }); }
      if (!T.lastClock || now - T.lastClock.at >= CLOCK_EVERY_MS) this.clockPoint(T);
      if (now - T.lastRecordedSent >= RECORDED_EVERY_MS) {
        T.lastRecordedSent = now;
        this.send({ type: "track.recorded", track_id: T.id, recorded_frames: this.recordedFrames(T) });
      }
    }
    for (const t of this.tracks.values()) {
      if (t.done || !t.clockQueue.length || t.clockBusy || now - (t.clockTried || 0) < 5000) continue;
      t.clockTried = now;
      this.postClock(t);
    }
    if (this.queue.length && this.active === 0 && Date.now() >= this.backoffUntil) this.pump();
    this.emit("progress", this.status());
  }

  recordedFrames(T) {
    return T.frames + (this.track === T && this.cur ? this.cur.n : 0);
  }

  // ---------------------------------------------------------------- Upload
  /** Uploads anstoßen; `now` (Netz ist zurück) verwirft die laufende Wartezeit. */
  pump(now = false) {
    if (this.fatal) return;
    if (now) { this.backoffUntil = 0; this.backoff = 0; }
    while (this.active < PARALLEL_UPLOADS && this.queue.length && Date.now() >= this.backoffUntil) {
      const it = this.queue.shift();
      this.active++;
      this.uploadOne(it).finally(() => { this.active--; this.pump(); });
    }
  }

  async uploadOne(it) {
    const T = it.T;
    let bytes = it.bytes;
    if (!bytes) bytes = await this.store.getChunk(T.id, it.seq).catch(() => null);
    if (!bytes) {
      this.lostChunks++;
      T.pending.delete(it.seq);
      T.lost.push(it.seq);
      this.checkFinish(T);
      return;
    }
    let ok = false, drop = false, status = 0;
    try {
      const r = await fetch(`/api/p/tracks/${T.id}/chunks/${it.seq}`, {
        method: "PUT",
        headers: { Authorization: `Bearer ${this.token()}`, "Content-Type": "application/octet-stream", "X-First-Frame": String(it.firstFrame), "X-Frames": String(it.frames) },
        body: bytes,
        cache: "no-store",
      });
      status = r.status;
      ok = r.ok;
      if (!ok) {
        let code = "";
        try { code = ((await r.json()).error || {}).code || ""; } catch { /* egal */ }
        if (status === 401) { this.setFatal(new ApiError(401, "unauthorized", t("Zugang abgelaufen oder ungültig"))); }
        // nicht wiederholbar: Spur gehört uns nicht, existiert nicht mehr, ist zu, Chunk kaputt
        if ([400, 403, 404, 409, 413, 422].includes(status)) { drop = true; console.warn("Chunk verworfen", T.id, it.seq, status, code); }
      }
    } catch { /* Netz */ }
    if (ok || drop) {
      if (it.bytes) { this.memBytes -= it.bytes.length; it.bytes = null; }
      this.backoff = 0;
      T.pending.delete(it.seq);
      if (ok) T.uploadedFrames += it.frames; else T.lost.push(it.seq);
      if (drop && status === 404) T.gone = true;
      this.persistQ = this.persistQ.then(() => this.store.delChunk(T.id, it.seq)).catch(() => {});
      this.checkFinish(T);
      return;
    }
    // wiederholen, mit wachsendem Abstand (1, 2, 4, 8, 16, 20 s)
    it.tries++;
    this.backoff = Math.min(this.backoff + 1, 6);
    const wait = Math.min(20000, 500 * 2 ** this.backoff);
    this.backoffUntil = Date.now() + wait;
    this.queue.unshift(it);
    setTimeout(() => this.pump(), wait + 10);
    this.emit("upload-retry", { wait, status });
  }

  async checkFinish(T) {
    if (!T.closing || T.finishing || T.done || T.pending.size) return;
    if (T.gone) { await this.dropTrack(T, "Spur gibt es auf dem Server nicht mehr"); return; }
    T.finishing = true;
    try {
      await this.persistQ;
      await this.postClock(T);
      for (let attempt = 0; ; attempt++) {
        let res;
        try {
          res = await api("POST", `/api/p/tracks/${T.id}/finish`, { token: this.token(), body: { total_frames: T.totalFrames, total_chunks: T.totalChunks } });
        } catch (e) {
          if (e.status === 404 || e.status === 403) { await this.dropTrack(T, e.message); return; }
          if (e.status === 401) { this.setFatal(e); return; }
          await sleep(Math.min(20000, 1000 * 2 ** Math.min(attempt, 4)));
          continue;
        }
        if (res.complete) { await this.trackDone(T); return; }
        // fehlende Chunks aus dem lokalen Speicher nachladen
        const stored = new Set((await this.store.listChunks(T.id)).map((c) => c.seq));
        const missing = res.missing || [];
        const again = missing.filter((s) => stored.has(s) && !T.pending.has(s));
        if (!again.length) {
          T.lost = Array.from(new Set([...T.lost, ...missing]));
          T.incomplete = true;
          this.emit("track-incomplete", { track: T, missing });
          await this.trackDone(T, false);
          return;
        }
        const list = await this.store.listChunks(T.id);
        for (const c of list) {
          if (!again.includes(c.seq)) continue;
          T.pending.add(c.seq);
          this.enqueue({ T, seq: c.seq, firstFrame: c.firstFrame, frames: c.frames, bytes: null, tries: 0 });
        }
        return; // checkFinish läuft wieder, wenn alles oben ist
      }
    } finally {
      T.finishing = false;
    }
  }

  async trackDone(T, complete = true) {
    T.done = true;
    T.complete = complete;
    await this.persistQ;
    if (complete || !T.pending.size) await this.store.delTrack(T.id).catch(() => {});
    if (T.releaseLock) T.releaseLock();
    this.emit("track-done", { track: T, complete });
  }

  /** Die Aufnahme wurde verworfen: alle Spuren dieser Folge ohne weiteren Upload fallen lassen. */
  async discardEpisode(episodeId) {
    if (this.track && this.track.episodeId === episodeId) this.finishActive("discarded");
    for (const T of [...this.tracks.values()]) {
      if (T.episodeId !== episodeId || T.done) continue;
      T.gone = true;
      await this.dropTrack(T, "verworfen");
    }
    this.emit("progress", this.status());
  }

  async dropTrack(T, why) {
    T.done = true; T.complete = false;
    await this.store.delTrack(T.id).catch(() => {});
    if (T.releaseLock) T.releaseLock();
    this.queue = this.queue.filter((i) => i.T !== T);
    this.emit("track-done", { track: T, complete: false, why });
  }

  setFatal(e) {
    if (this.fatal) return;
    this.fatal = e;
    this.emit("fatal", { error: e });
  }

  // ---------------------------------------------------------------- Wiederaufnahme nach Neuladen
  /** Liegengebliebene Spuren dieses Teilnehmers nachladen und abschließen. Liefert die Zahl der Spuren. */
  async recover() {
    let metas = [];
    try { metas = await this.store.metas(); } catch { return 0; }
    let n = 0;
    for (const m of metas) {
      if (!m || !m.track_id || this.tracks.has(m.track_id)) continue;
      if (m.owner !== this.participantId) continue; // gehört einer anderen Identität in diesem Browser
      const take = async (ready) => {
        const list = await this.store.listChunks(m.track_id);
        const T = this.newTrack(m.track_id, m.episode_id, 0, null);
        T.recovered = true;
        const last = list[list.length - 1];
        T.nextSeq = Math.max(m.next_seq || 0, last ? last.seq + 1 : 0);
        T.frames = Math.max(m.frames || 0, last ? last.firstFrame + last.frames : 0);
        T.totalChunks = m.closed && m.total_chunks != null ? Math.max(m.total_chunks, T.nextSeq) : T.nextSeq;
        T.totalFrames = m.closed && m.total_frames != null ? Math.max(m.total_frames, T.frames) : T.frames;
        T.closing = true;
        T.clockQueue = Array.isArray(m.clock) ? m.clock : [];
        let open = 0;
        for (const c of list) {
          T.pending.add(c.seq);
          open += c.frames;
          this.queue.push({ T, seq: c.seq, firstFrame: c.firstFrame, frames: c.frames, bytes: null, tries: 0 });
        }
        T.uploadedFrames = Math.max(0, T.frames - open); // schon vor dem Neuladen oben
        if (hasLocks()) T.lockDone = new Promise((res) => { T.releaseLock = res; });
        n++;
        this.emit("recovered", { track: T, chunks: list.length });
        ready();
        this.pump();
        this.checkFinish(T);
        return T.lockDone;
      };
      await new Promise((resolve) => {
        if (hasLocks()) {
          // Nur Spuren, die kein anderer Tab gerade hält; das Lock bleibt bis zum Abschluss gehalten
          navigator.locks.request("ps-track-" + m.track_id, { ifAvailable: true }, (lock) => {
            if (!lock) { resolve(); return undefined; }
            return take(resolve).catch(() => resolve());
          }).catch(() => resolve());
        } else if (Date.now() - (m.updated_at || 0) > 15000) {
          take(resolve).catch(() => resolve());
        } else resolve();
      });
    }
    return n;
  }

  // ---------------------------------------------------------------- Stand
  status() {
    let pendingFrames = 0, uploaded = 0, total = 0, openTracks = 0;
    for (const T of this.tracks.values()) {
      if (T.done) continue;
      openTracks++;
      const all = T === this.track ? this.recordedFrames(T) : T.frames;
      total += all;
      uploaded += T.uploadedFrames;
      pendingFrames += Math.max(0, all - T.uploadedFrames);
    }
    return {
      recording: !!this.track, creating: !!this.creating, openTracks, pendingFrames, uploadedFrames: uploaded, totalFrames: total,
      queue: this.queue.length, active: this.active, retrying: this.backoffUntil > Date.now(), sr: this.sr,
      gapFrames: this.gapFrames, droppedFrames: this.droppedFrames, lostChunks: this.lostChunks, store: this.store.kind,
      clockSource: this.audioClock ? this.audioClock.source : null,
    };
  }

  /** Stehen noch Chunks aus? (für beforeunload) */
  get busy() {
    if (this.track || this.creating) return true;
    for (const T of this.tracks.values()) if (!T.done) return true;
    return false;
  }

  emit(type, detail) { this.dispatchEvent(new CustomEvent(type, { detail })); }
}
