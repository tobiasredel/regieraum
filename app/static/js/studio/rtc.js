// Live-Gespräch: WebRTC-Mesh bis vier Teilnehmer (SPEC Abschnitte 6 und 8).
//
// - „Perfect negotiation“: höflich ist die Seite mit der lexikografisch größeren Teilnehmer-ID.
// - Transceiver legt nur die Seite mit der kleineren ID an (Audio und Video, beide sendrecv). Die andere Seite
//   übernimmt die Transceiver aus dem Angebot. So entstehen keine doppelten m-Zeilen bei gleichzeitigem Start.
// - Jede Seite trägt eine Sitzungskennung (Seitenaufruf) in ihren Signalen. Ändert sie sich, hat die Gegenseite
//   neu geladen: Die Verbindung wird neu aufgebaut.
// - Opus 96 kbit/s mono, FEC an, DTX aus: per fmtp im empfangenen SDP (bestimmt unseren Encoder) und
//   setParameters(maxBitrate). Video 640×360, 24 Bilder, je Gegenüber höchstens 700 kbit/s (zu zweit), 450 (zu dritt)
//   oder 300 (zu viert). Wird die Leitung eng, senkt der Browser zuerst die Auflösung; reicht es nicht einmal für
//   150 kbit/s neben dem Ton, wird das Video abgeschaltet.
// - Abriss: ICE-Restart, bei Misserfolg Neuaufbau.

const OPUS_FMTP = { minptime: "10", useinbandfec: "1", usedtx: "0", stereo: "0", "sprop-stereo": "0", maxaveragebitrate: "96000", maxplaybackrate: "48000" };
const AUDIO_BPS = 96000;
const VIDEO_MIN_BPS = 150000;
const VIDEO_BPS_BY_PEERS = [700000, 700000, 450000, 300000]; // Index: Zahl der Gegenüber

function mungeOpus(sdp) {
  const m = /a=rtpmap:(\d+) opus\/48000\/2/i.exec(sdp);
  if (!m) return sdp;
  const pt = m[1];
  const lines = sdp.split("\r\n");
  const idx = lines.findIndex((l) => l.startsWith(`a=fmtp:${pt} `));
  const set = (params) => {
    const kv = new Map(params.split(";").filter(Boolean).map((p) => { const [k, v] = p.split("="); return [k.trim(), (v || "").trim()]; }));
    for (const [k, v] of Object.entries(OPUS_FMTP)) kv.set(k, v);
    return [...kv].map(([k, v]) => `${k}=${v}`).join(";");
  };
  if (idx >= 0) {
    lines[idx] = `a=fmtp:${pt} ` + set(lines[idx].slice(`a=fmtp:${pt} `.length));
  } else {
    const r = lines.findIndex((l) => l.startsWith(`a=rtpmap:${pt} `));
    lines.splice(r + 1, 0, `a=fmtp:${pt} ` + set(""));
  }
  return lines.join("\r\n");
}

export class Mesh extends EventTarget {
  /** @param o selfId, session, send(msg), iceServers, audioTrack (Klon des Mikrofons), videoTrack|null */
  constructor(o) {
    super();
    Object.assign(this, o);
    this.peers = new Map();
    this.statsTimer = setInterval(() => this.pollStats(), 2000);
  }

  setIceServers(list) { this.iceServers = list; }

  /**
   * Abgleich mit dem Raum: verbundene Teilnehmer bekommen eine Verbindung. Wer fehlt, wird erst nach einer Schonfrist
   * getrennt: Bricht nur die Signalisierung weg (Server-Neustart, kurzer Netzabriss), läuft das Gespräch Ende zu Ende
   * weiter.
   */
  sync(peersList) {
    const want = new Set(peersList.filter((p) => p.connected && p.id !== this.selfId).map((p) => p.id));
    for (const id of want) {
      const P = this.peers.get(id);
      if (!P) this.open(id, null, null, "neu");
      else P.absentSince = 0;
    }
    for (const [id, P] of this.peers) if (!want.has(id) && !P.absentSince) P.absentSince = performance.now();
    this.reap();
  }

  reap() {
    const now = performance.now();
    for (const [id, P] of [...this.peers]) {
      if (!P.absentSince) continue;
      const ice = P.pc.iceConnectionState;
      const alive = ice === "connected" || ice === "completed";
      if (now - P.absentSince > (alive ? 60000 : 15000)) this.closePeer(id);
    }
  }

  /**
   * Neue Verbindung zu `id`. Jede Verbindung hat eine Generation; Signale tragen Sitzung (Seitenaufruf) und
   * Generation des Absenders sowie die bekannte Generation des Empfängers (`to_gen`). Ein „Hallo“ mit neuer
   * Sitzung oder höherer Generation lässt die Gegenseite ihre Verbindung ebenfalls neu aufbauen; veraltete Signale
   * einer früheren Verbindung werden verworfen. So einigen sich beide Seiten ohne Endlosschleife.
   */
  open(id, remoteSession, remoteGen, why = "neu") {
    this.closePeer(id);
    this.log = this.log || [];
    if (this.log.length < 100) this.log.push({ at: Math.round(performance.now()), id, why });
    this.gen = Math.max((this.gen || 0) + 1, Date.now()); // eindeutig über Seitenaufrufe hinweg
    const pc = new RTCPeerConnection({ iceServers: this.iceServers || [], bundlePolicy: "max-bundle" });
    const P = {
      id, pc, gen: this.gen, polite: this.selfId > id, offerer: this.selfId < id, makingOffer: false, ignoreOffer: false,
      remoteSession, remoteGen, audio: null, video: null, stream: new MediaStream(), stats: null, lastStats: null,
      quality: "verbindet", videoAllowed: true, goodSince: 0, restarts: 0, absentSince: 0, failedSince: 0,
    };
    this.peers.set(id, P);
    this.retune();

    pc.onnegotiationneeded = async () => {
      try {
        P.makingOffer = true;
        await pc.setLocalDescription();
        this.signal(P, { description: pc.localDescription });
      } catch (e) {
        console.warn("Angebot fehlgeschlagen", e);
      } finally {
        P.makingOffer = false;
      }
    };
    pc.onicecandidate = ({ candidate }) => { if (candidate) this.signal(P, { candidate }); };
    pc.ontrack = (e) => {
      const tr = e.track;
      for (const old of P.stream.getTracks()) if (old.kind === tr.kind && old !== tr) P.stream.removeTrack(old);
      if (!P.stream.getTracks().includes(tr)) P.stream.addTrack(tr);
      tr.onunmute = () => this.emit("media", { id });
      tr.onended = () => this.emit("media", { id });
      this.emit("media", { id });
    };
    pc.oniceconnectionstatechange = () => this.onIceState(P);
    pc.onconnectionstatechange = () => this.emit("state", { id, state: pc.connectionState });

    // Hallo zuerst, dann (als anbietende Seite) die Transceiver, die das Angebot auslösen
    this.signal(P, { hello: true });
    if (P.offerer) {
      P.audio = pc.addTransceiver(this.audioTrack || "audio", { direction: "sendrecv" });
      P.video = pc.addTransceiver(this.videoTrack || "video", { direction: "sendrecv" });
      this.applyParams(P);
    }
    return P;
  }

  closePeer(id) {
    const P = this.peers.get(id);
    if (!P) return;
    this.peers.delete(id);
    try { P.pc.close(); } catch { /* egal */ }
    this.retune();
    this.emit("closed", { id });
  }

  signal(P, data) {
    this.send({ type: "rtc", to: P.id, data: { ...data, session: this.session, gen: P.gen, to_gen: P.remoteGen ?? null } });
  }

  async onSignal(from, data) {
    if (!data) return;
    let P = this.peers.get(from);
    const isNew = !P || P.pc.signalingState === "closed"
      || (P.remoteSession && data.session !== P.remoteSession)
      || (data.hello && P.remoteGen != null && data.gen > P.remoteGen);
    if (isNew) {
      // Nur ein Hallo oder ein Angebot eröffnet eine neue Verbindung; Reste einer alten werden verworfen
      if (!data.hello && !(data.description && data.description.type === "offer")) return;
      P = this.open(from, data.session, data.gen, !P ? "Signal ohne Verbindung" : data.session !== P.remoteSession ? "Gegenseite neu geladen" : "Gegenseite neu verbunden");
    } else {
      if (data.to_gen != null && data.to_gen !== P.gen) return;          // an eine frühere eigene Verbindung
      if (P.remoteGen != null && data.gen < P.remoteGen) return;         // von einer früheren Verbindung der Gegenseite
      if (P.remoteSession == null) P.remoteSession = data.session;
      if (P.remoteGen == null) P.remoteGen = data.gen;
    }
    if (data.hello) return;
    const pc = P.pc;
    try {
      if (data.description) {
        const desc = { type: data.description.type, sdp: mungeOpus(data.description.sdp || "") };
        const collision = desc.type === "offer" && (P.makingOffer || pc.signalingState !== "stable");
        P.ignoreOffer = !P.polite && collision;
        if (P.ignoreOffer) return;
        await pc.setRemoteDescription(desc);
        if (desc.type === "offer") {
          this.adoptTransceivers(P);
          await pc.setLocalDescription();
          this.signal(P, { description: pc.localDescription });
        }
        this.applyParams(P);
      } else if (data.candidate) {
        try { await pc.addIceCandidate(data.candidate); } catch (e) { if (!P.ignoreOffer) console.warn("ICE-Kandidat", e); }
      }
    } catch (e) {
      console.warn("Signalisierung", e);
    }
  }

  /** Antwortende Seite: Transceiver aus dem Angebot übernehmen und eigene Spuren anhängen. */
  adoptTransceivers(P) {
    if (P.offerer) return;
    for (const t of P.pc.getTransceivers()) {
      const kind = t.receiver.track && t.receiver.track.kind;
      if (kind === "audio" && !P.audio) P.audio = t;
      if (kind === "video" && !P.video) P.video = t;
    }
    for (const [t, track] of [[P.audio, this.audioTrack], [P.video, this.videoTrack]]) {
      if (!t) continue;
      if (t.direction !== "sendrecv") t.direction = "sendrecv";
      if (t.sender.track !== (track || null)) t.sender.replaceTrack(track || null).catch(() => {});
    }
  }

  applyParams(P) {
    const tune = async (t, maxBitrate, priority, active) => {
      if (!t || !t.sender || typeof t.sender.getParameters !== "function") return;
      const p = t.sender.getParameters();
      if (!p.encodings || !p.encodings.length) return; // vor der Aushandlung noch leer
      for (const e of p.encodings) {
        e.maxBitrate = maxBitrate;
        e.priority = priority;
        e.networkPriority = priority;
        if (active != null) e.active = active;
      }
      if (t.sender.track && t.sender.track.kind === "video") p.degradationPreference = "maintain-framerate";
      try { await t.sender.setParameters(p); } catch {
        // manche Browser kennen nicht alle Felder: dann nur Bitrate und An/Aus
        try {
          const q = t.sender.getParameters();
          for (const e of q.encodings || []) { e.maxBitrate = maxBitrate; if (active != null) e.active = active; }
          await t.sender.setParameters(q);
        } catch { /* bleibt bei den Standardwerten */ }
      }
    };
    tune(P.audio, AUDIO_BPS, "high", true);
    tune(P.video, this.videoBps(), "low", P.videoAllowed);
  }

  videoBps() { return VIDEO_BPS_BY_PEERS[Math.min(this.peers.size, VIDEO_BPS_BY_PEERS.length - 1)]; }

  /** Kommt oder geht jemand, teilt sich die Bandbreite neu auf. */
  retune() { for (const P of this.peers.values()) this.applyParams(P); }

  /** Eigene Spuren tauschen (Mikrofonwechsel, Kamera an/aus), ohne neu auszuhandeln. */
  setTracks(audioTrack, videoTrack) {
    this.audioTrack = audioTrack;
    this.videoTrack = videoTrack;
    for (const P of this.peers.values()) {
      if (P.audio) P.audio.sender.replaceTrack(audioTrack || null).catch(() => {});
      if (P.video) P.video.sender.replaceTrack(videoTrack || null).catch(() => {});
      this.applyParams(P);
    }
  }

  onIceState(P) {
    const s = P.pc.iceConnectionState;
    if (s === "connected" || s === "completed") { P.failedSince = 0; P.restarts = 0; }
    else if ((s === "disconnected" || s === "failed") && !P.failedSince) P.failedSince = performance.now();
    this.emit("state", { id: P.id, state: s });
  }

  /** Abriss behandeln (aus dem Stats-Takt): nach 4 s ICE-Restart, alle 10 s erneut, nach drei Versuchen Neuaufbau. */
  heal(P) {
    if (!P.failedSince || this.peers.get(P.id) !== P) return;
    const now = performance.now();
    const waited = now - P.failedSince;
    if (waited < 4000 || now - (P.lastRestart || 0) < 10000) return;
    P.lastRestart = now;
    P.restarts++;
    // Neuaufbau mit unbekannter Gegen-Generation: Die Gegenseite baut auf unser Hallo hin neu auf, wir übernehmen ihre
    if (P.restarts > 3) { this.open(P.id, P.remoteSession, null, "ICE-Restart erfolglos"); return; }
    try { P.pc.restartIce(); } catch { this.open(P.id, P.remoteSession, null, "ohne restartIce"); }
  }

  /** Nach Rückkehr des Netzes: hängende Verbindungen sofort neu anstoßen. */
  nudge() {
    for (const P of this.peers.values()) if (P.failedSince) { P.lastRestart = 0; P.failedSince = Math.min(P.failedSince, performance.now() - 4000); this.heal(P); }
  }

  async pollStats() {
    this.reap();
    for (const P of [...this.peers.values()]) this.heal(P);
    for (const P of this.peers.values()) {
      let rep;
      try { rep = await P.pc.getStats(); } catch { continue; }
      const st = { rtt: null, audioIn: 0, audioLost: 0, audioRecv: 0, jitter: null, audioOut: 0, availOut: null, videoIn: 0, at: performance.now() };
      rep.forEach((r) => {
        if (r.type === "candidate-pair" && r.nominated && r.state === "succeeded") {
          if (typeof r.currentRoundTripTime === "number") st.rtt = r.currentRoundTripTime * 1000;
          if (typeof r.availableOutgoingBitrate === "number") st.availOut = r.availableOutgoingBitrate;
        }
        if (r.type === "inbound-rtp" && r.kind === "audio") {
          st.audioIn = r.bytesReceived || 0; st.audioLost = r.packetsLost || 0; st.audioRecv = r.packetsReceived || 0;
          if (typeof r.jitter === "number") st.jitter = r.jitter * 1000;
        }
        if (r.type === "inbound-rtp" && r.kind === "video") st.videoIn = r.bytesReceived || 0;
        if (r.type === "outbound-rtp" && r.kind === "audio") st.audioOut = r.bytesSent || 0;
      });
      const prev = P.lastStats;
      P.lastStats = st;
      let loss = 0, inRate = 0;
      if (prev) {
        const dRecv = st.audioRecv - prev.audioRecv, dLost = Math.max(0, st.audioLost - prev.audioLost);
        loss = dRecv + dLost > 0 ? dLost / (dRecv + dLost) : 0;
        inRate = ((st.audioIn - prev.audioIn) * 8) / ((st.at - prev.at) / 1000);
      }
      st.loss = loss; st.inRate = inRate;
      const ice = P.pc.iceConnectionState;
      let q;
      if (ice === "new" || ice === "checking") q = "verbindet";
      else if (ice !== "connected" && ice !== "completed") q = "getrennt";
      else if (prev && inRate < 1000) q = "kein Ton";
      else if (loss > 0.08 || (st.rtt != null && st.rtt > 400)) q = "schwach";
      else if (loss > 0.02 || (st.rtt != null && st.rtt > 200)) q = "mittel";
      else q = "gut";
      P.quality = q;
      P.stats = st;
      // Video zuerst opfern: bei schwacher Verbindung oder wenig Bandbreite aus, nach 20 s guter Werte wieder an
      const tight = q === "schwach" || (st.availOut != null && st.availOut < AUDIO_BPS + VIDEO_MIN_BPS + 50000);
      if (tight && P.videoAllowed) { P.videoAllowed = false; P.goodSince = 0; this.applyParams(P); }
      else if (!tight && !P.videoAllowed) {
        if (!P.goodSince) P.goodSince = st.at;
        else if (st.at - P.goodSince > 20000) { P.videoAllowed = true; this.applyParams(P); }
      }
    }
    this.emit("stats");
  }

  destroy() {
    clearInterval(this.statsTimer);
    for (const id of [...this.peers.keys()]) this.closePeer(id);
  }

  emit(type, detail) { this.dispatchEvent(new CustomEvent(type, { detail })); }
}
