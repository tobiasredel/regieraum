// Live-Studio für Host und Gast (SPEC Abschnitt 8). Ein Kern, zwei Seiten:
//   /studio        data-role="host"
//   /join/<token>  data-role="guest" data-invite="<token>"
// Ablauf: Unterstützung prüfen → Identität → Gerätecheck → Studio betreten (Rekorder, Raum, Live-Gespräch, Jingles).

const V = new URL(import.meta.url).search;
const imp = (m) => import(`./${m}${V}`);
const [U, C, Store, Rec, Net, RTC, Snd, Media, Id, Plan] = await Promise.all([
  imp("util.js"), imp("clock.js"), imp("store.js"), imp("recorder.js"), imp("net.js"), imp("rtc.js"),
  imp("sounds.js"), imp("media.js"), imp("identity.js"), imp("plan.js"),
]);
const { h, fill, icon, dur, num1, toDb, toast, params, isMobile, api } = U;

const app = document.getElementById("ps-app");
const ROLE = app.dataset.role === "guest" ? "guest" : "host";
const INVITE = app.dataset.invite || "";
const P = params();
const TEST = P.get("testsignal"); // "1"/"server" oder "local"

const S = {
  role: ROLE, token: null, me: null, room: null, ctx: null, audioClock: null, clock: new C.ServerClock(), store: null,
  recorder: null, mesh: null, jingles: null, socket: null, plan: null,
  mic: null, rtcAudio: null, micId: null, headphones: !isMobile(), mobile: isMobile(), cam: false, camStream: null,
  muted: false, peerMeter: new Map(), progress: new Map(), media: new Map(), localMeter: null,
  stopped: null, wakeLock: null, wasHidden: false, entered: false, fatal: null, lastStatus: null,
};
window.__studio = S; // für Tests und Fehlersuche

// ====================================================================== Start
boot().catch((e) => { console.error(e); showFatal("Das Studio konnte nicht starten", String(e && e.message || e)); });

async function boot() {
  const missing = Media.missingSupport();
  if (missing) { showFatal("Browser nicht unterstützt", missing); return; }
  if (ROLE === "host") {
    const id = await Id.hostSession().catch((e) => { throw new Error(e.message); });
    S.token = id.token; S.me = id.participant;
    await startRecovery();
    renderCheck({});
  } else {
    let info;
    try { info = await Id.inviteInfo(INVITE); } catch (e) {
      const t = { invite_unknown: "Dieser Einladungslink ist unbekannt", invite_revoked: "Dieser Einladungslink wurde widerrufen", invite_expired: "Dieser Einladungslink ist abgelaufen" };
      showFatal(t[e.code] || "Link nicht nutzbar", e.status ? "Bitte beim Host einen neuen Link anfragen" : e.message);
      return;
    }
    if (!info.info.valid && !info.stored) {
      showFatal("Dieser Einmal-Link wurde bereits benutzt", "Bitte beim Host einen neuen Link anfragen");
      return;
    }
    if (info.stored) {
      // Wiederkehrender Gast (z. B. nach Neuladen): Identität prüfen und Liegengebliebenes sofort nachladen
      try {
        S.me = await api("GET", "/api/p/me", { token: info.stored });
        S.token = info.stored;
        await startRecovery();
      } catch (e) { if (e.status === 401) Id.forgetGuest(INVITE); }
    }
    renderCheck({ name: info.storedName, label: info.info.label });
  }
}

/** Rekorder ohne Audio anlegen und unterbrochene Spuren aus dem lokalen Speicher hochladen (vor allem Neuen). */
async function startRecovery() {
  S.store = await Store.openStore(P.get("store"));
  S.recorder = new Rec.Recorder({
    ctx: null, audioClock: null, serverClock: S.clock, store: S.store, token: () => S.token,
    participantId: S.me.id, send: (m) => S.socket && S.socket.send(m),
    workletUrl: new URL(`./recorder-worklet.js${V}`, import.meta.url).href,
  });
  window.addEventListener("beforeunload", (e) => {
    if (S.recorder && S.recorder.busy) { e.preventDefault(); e.returnValue = ""; return ""; }
    return undefined;
  });
  const n = await S.recorder.recover();
  S.recovering = n;
}

function showFatal(title, text, action) {
  fill(app, h("section", { class: "ps-fatal", role: "alert" },
    h("h1", {}, title), h("p", { class: "ps-lead" }, text), action || null));
}

// ====================================================================== Gerätecheck
function renderCheck({ name, label }) {
  const pr = Id.prefs.load();
  S.headphones = S.mobile ? false : pr.headphones !== false;
  S.micId = pr.micId || null;
  const err = h("p", { class: "ps-error", role: "alert", hidden: true });
  const nameIn = h("input", { class: "tr-input", id: "ps-name", name: "name", autocomplete: "name", maxlength: 60, value: name || "", placeholder: "Vorname" });
  const micSel = h("select", { class: "tr-input ps-select", id: "ps-mic", disabled: true }, h("option", {}, "Erst Mikrofon freigeben"));
  const meterFill = h("span", { class: "ps-meter-fill" });
  const meterText = h("span", { class: "ps-meter-db" }, "–");
  const clipText = h("span", { class: "ps-meter-clip tr-status-error", hidden: true }, "Übersteuert");
  const hpYes = h("input", { type: "radio", name: "hp", id: "ps-hp-yes", value: "1", checked: S.headphones });
  const hpNo = h("input", { type: "radio", name: "hp", id: "ps-hp-no", value: "0", checked: !S.headphones });
  const camBox = h("input", { type: "checkbox", id: "ps-cam" });
  const camPrev = h("video", { class: "ps-cam-preview", muted: true, playsinline: true, autoplay: true, hidden: true });
  camPrev.muted = true;
  const toneBtn = h("button", { class: "tr-btn tr-btn-secondary", type: "button", disabled: true }, icon("speaker"), "Testton abspielen");
  const go = h("button", { class: "tr-btn tr-btn-primary", type: "submit", id: "ps-go" }, icon("mic"), "Mikrofon freigeben");
  const hint = h("p", { class: "ps-small", id: "ps-go-hint" }, "Der Browser fragt nach dem Mikrofon. Danach erscheint der Pegel.");

  const form = h("form", { class: "ps-check", novalidate: true },
    h("header", { class: "ps-check-head" },
      h("h1", {}, ROLE === "host" ? "Studio" : "Beitreten"),
      h("p", { class: "ps-lead" }, ROLE === "host" ? "Gerätecheck vor der Aufnahme" : (label ? `Einladung: ${label}. Kurzer Gerätecheck, dann geht es ins Studio.` : "Kurzer Gerätecheck, dann geht es ins Studio"))),
    err,
    h("div", { class: "ps-check-grid" },
      ROLE === "guest" ? h("div", { class: "ps-field" }, h("label", { class: "tr-label", for: "ps-name" }, "Name"), nameIn) : null,
      h("div", { class: "ps-field" }, h("label", { class: "tr-label", for: "ps-mic" }, "Mikrofon"), micSel,
        h("div", { class: "ps-meter", "aria-label": "Eingangspegel", role: "meter", "aria-valuemin": "-60", "aria-valuemax": "0" }, h("span", { class: "ps-meter-track" }, meterFill), meterText),
        clipText),
      h("fieldset", { class: "ps-field ps-choice" }, h("legend", { class: "tr-label" }, "Kopfhörer"),
        h("label", { for: "ps-hp-yes" }, hpYes, h("span", {}, "Mit Kopfhörern"), h("small", {}, "unbearbeitetes Signal, beste Qualität")),
        h("label", { for: "ps-hp-no" }, hpNo, h("span", {}, "Ohne Kopfhörer"), h("small", {}, "Echounterdrückung an"))),
      h("div", { class: "ps-field" }, h("span", { class: "tr-label" }, "Kamera"),
        h("label", { class: "ps-check-line", for: "ps-cam" }, camBox, h("span", {}, "Kamera einschalten (nur live, wird nie aufgezeichnet)")), camPrev),
      h("div", { class: "ps-field" }, h("span", { class: "tr-label" }, "Ausgang"), h("div", {}, toneBtn), h("small", {}, "Ist der Ton zu hören, passt der Ausgang"))),
    h("div", { class: "ps-check-actions" }, go, hint));
  fill(app, form);

  let analyser = null, meterTimer = 0, granted = false, srcNode = null, quick = false;
  // Rückkehr mitten in einer Aufnahme (Neuladen): ein Klick genügt, wenn das Mikrofon schon erlaubt ist
  if (S.recovering > 0) {
    micPermission().then((st) => {
      if (st !== "granted" && !TEST) return;
      quick = true;
      fill(go, icon("mic"), "Wieder ins Studio");
      hint.textContent = "Die Aufnahme läuft. Unterbrochene Teile werden im Hintergrund nachgeladen.";
    });
  }
  const showErr = (t) => { err.textContent = t; err.hidden = !t; };

  async function ensureCtx() {
    if (S.ctx) { if (S.ctx.state !== "running") await S.ctx.resume().catch(() => {}); return; }
    const AC = window.AudioContext || window.webkitAudioContext;
    try { S.ctx = new AC({ sampleRate: 48000, latencyHint: "interactive" }); } catch { S.ctx = new AC({ latencyHint: "interactive" }); }
    await S.ctx.resume().catch(() => {});
  }

  async function openMic() {
    const old = S.mic;
    if (TEST) {
      S.mic = Media.testSignal(S.clock, TEST === "local" ? "local" : "server");
    } else {
      S.mic = await Media.getMic(S.micId, S.headphones);
    }
    if (old && old !== S.mic) stopStream(old);
    // Context und Mikrofon müssen dieselbe Abtastrate vertragen; sonst Context mit Standardrate (Firefox)
    try {
      srcNode && srcNode.disconnect();
      srcNode = S.ctx.createMediaStreamSource(S.mic);
    } catch (e) {
      if (e && e.name === "NotSupportedError") {
        await S.ctx.close().catch(() => {});
        const AC = window.AudioContext || window.webkitAudioContext;
        S.ctx = new AC({ latencyHint: "interactive" });
        await S.ctx.resume().catch(() => {});
        srcNode = S.ctx.createMediaStreamSource(S.mic);
      } else throw e;
    }
    analyser = new AnalyserNode(S.ctx, { fftSize: 2048 });
    srcNode.connect(analyser);
    const t = S.mic.getAudioTracks()[0];
    if (t && !TEST) S.micId = t.getSettings().deviceId || S.micId;
  }

  async function fillDevices() {
    if (TEST) { fill(micSel, h("option", { value: "" }, "Testsignal (Klick je Sekunde)")); micSel.disabled = true; return; }
    const { mics } = await Media.listDevices();
    fill(micSel, mics.map((d, i) => h("option", { value: d.deviceId, selected: d.deviceId === S.micId }, d.label || `Mikrofon ${i + 1}`)));
    micSel.disabled = mics.length < 2;
  }

  function startMeter() {
    clearInterval(meterTimer);
    const buf = new Float32Array(2048);
    let clipAt = 0;
    meterTimer = setInterval(() => {
      if (!analyser) return;
      analyser.getFloatTimeDomainData(buf);
      let pk = 0;
      for (const s of buf) { const a = Math.abs(s); if (a > pk) pk = a; }
      if (pk >= 0.999) clipAt = performance.now();
      const db = toDb(pk);
      setMeter(meterFill, meterText, db);
      clipText.hidden = performance.now() - clipAt > 2000;
    }, 100);
  }

  micSel.addEventListener("change", async () => {
    S.micId = micSel.value;
    Id.prefs.save({ micId: S.micId });
    try { await openMic(); } catch (e) { showErr(Media.mediaError(e)); }
  });
  for (const r of [hpYes, hpNo]) r.addEventListener("change", async () => {
    S.headphones = hpYes.checked;
    Id.prefs.save({ headphones: S.headphones });
    if (granted) { try { await openMic(); } catch (e) { showErr(Media.mediaError(e)); } }
  });
  camBox.addEventListener("change", async () => {
    showErr("");
    if (camBox.checked) {
      try {
        S.camStream = await Media.getCam();
        camPrev.srcObject = S.camStream; camPrev.hidden = false; S.cam = true;
      } catch (e) { camBox.checked = false; S.cam = false; showErr(Media.mediaError(e, "Kamera")); }
    } else {
      stopStream(S.camStream); S.camStream = null; S.cam = false; camPrev.hidden = true; camPrev.srcObject = null;
    }
  });
  toneBtn.addEventListener("click", async () => { await ensureCtx(); Media.testTone(S.ctx); });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showErr("");
    go.disabled = true;
    try {
      await ensureCtx();
      if (!granted) {
        await openMic();
        granted = true;
        await fillDevices();
        startMeter();
        toneBtn.disabled = false;
        if (quick) { await finishEnter(); return; }
        fill(go, icon("mic"), "Studio betreten");
        hint.textContent = "Pegel prüfen: Beim normalen Sprechen sollte der Balken etwa bis zur Hälfte reichen";
        go.disabled = false;
        return;
      }
      await finishEnter();
    } catch (err2) {
      console.error(err2);
      showErr(err2 && err2.name && /Error$/.test(err2.name) && err2.name !== "Error" && err2.name !== "TypeError" ? Media.mediaError(err2) : (err2.message || String(err2)));
      go.disabled = false;
    }
  });

  async function finishEnter() {
    if (ROLE === "guest") {
      const nm = nameIn.value.trim();
      if (!nm) { nameIn.classList.add("is-error"); showErr("Bitte einen Namen angeben"); nameIn.focus(); go.disabled = false; return; }
      nameIn.classList.remove("is-error");
      Id.prefs.save({ name: nm });
      try {
        const id = await Id.joinGuest(INVITE, nm);
        S.token = id.token; S.me = id.participant;
      } catch (err2) {
        if (err2.code === "invite_used") { showFatal("Dieser Einmal-Link wurde bereits benutzt", "Bitte beim Host einen neuen Link anfragen"); return; }
        throw err2;
      }
    }
    clearInterval(meterTimer);
    try { srcNode && srcNode.disconnect(); } catch { /* egal */ }
    await enterStudio();
  }
}

async function micPermission() {
  try { return (await navigator.permissions.query({ name: "microphone" })).state; } catch { return "unknown"; }
}

function stopStream(s) { if (s) for (const t of s.getTracks()) t.stop(); }

function setMeter(fillEl, textEl, db) {
  const pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
  fillEl.style.width = pct.toFixed(1) + "%";
  if (textEl) textEl.textContent = db <= -60 ? "–" : `${num1(db).replace("-", "−")} dB`;
}

// ====================================================================== Studio
async function enterStudio() {
  S.entered = true;
  S.audioClock = new C.AudioClock(S.ctx);
  if (!S.recorder || S.recorder.participantId !== S.me.id) {
    if (S.recorder) clearInterval(S.recorder.timer);
    await startRecovery(); // neue Identität (erster Beitritt als Gast)
  }
  await S.recorder.init(S.ctx, S.audioClock);
  S.recorder.attach(S.mic, { headphones: S.headphones, device_label: micLabel(), test_signal: TEST || undefined });
  wireRecorder();
  if (S.recovering) toast(S.recovering === 1 ? "Eine unterbrochene Spur wird nachgeladen" : `${S.recovering} unterbrochene Spuren werden nachgeladen`);

  S.jingles = new Snd.JinglePlayer(S.ctx, S.audioClock, S.clock, () => S.token);
  S.rtcAudio = S.mic.getAudioTracks()[0].clone();
  const ice = await api("GET", "/api/p/ice", { token: S.token }).catch(() => ({ iceServers: [{ urls: "stun:stun.cloudflare.com:3478" }] }));
  S.mesh = new RTC.Mesh({
    selfId: S.me.id, session: Math.random().toString(36).slice(2) + Date.now().toString(36),
    send: (m) => S.socket && S.socket.send(m), iceServers: ice.iceServers || [],
    audioTrack: S.rtcAudio, videoTrack: S.camStream ? S.camStream.getVideoTracks()[0] : null,
  });
  S.mesh.addEventListener("media", (e) => attachMedia(e.detail.id));
  S.mesh.addEventListener("closed", (e) => detachMedia(e.detail.id));
  S.mesh.addEventListener("stats", () => renderPeers());

  buildStudio();
  S.socket = new Net.RoomSocket(S.token, S.clock);
  S.socket.addEventListener("message", (e) => onMessage(e.detail));
  S.socket.addEventListener("open", () => { sendState(); setConn(""); S.recorder.pump(true); });
  S.socket.addEventListener("close", () => setConn("Verbindung zum Studio unterbrochen, neuer Versuch läuft"));
  S.socket.addEventListener("fatal", (e) => onSocketFatal(e.detail));
  S.socket.connect();

  window.addEventListener("online", () => { S.socket.kick(); S.recorder.pump(true); S.mesh.nudge(); });
  document.addEventListener("visibilitychange", onVisibility);
  document.addEventListener("keydown", onKey);
  navigator.mediaDevices.addEventListener && navigator.mediaDevices.addEventListener("devicechange", () => refreshDeviceSelect());
  setInterval(tick, 250);
}

function micLabel() {
  const t = S.mic && S.mic.getAudioTracks()[0];
  return TEST ? "Testsignal" : (t && t.label) || "";
}

function wireRecorder() {
  const R = S.recorder;
  let n = 0, clipAcc = false;
  R.addEventListener("meter", (e) => {
    const d = e.detail;
    S.localMeter = { peak_db: toDb(d.peak), rms_db: toDb(d.rms), clip: d.clip, at: performance.now(), clipAt: d.clip ? performance.now() : (S.localMeter && S.localMeter.clipAt) || 0 };
    clipAcc = clipAcc || d.clip;
    // höchstens 5 Pegel je Sekunde an den Server
    if (++n % 2 === 0 && S.socket) {
      S.socket.send({ type: "meter", peak_db: Math.round(S.localMeter.peak_db * 10) / 10, rms_db: Math.round(S.localMeter.rms_db * 10) / 10, clip: clipAcc });
      clipAcc = false;
    }
    updateMeter(S.me.id, S.localMeter);
  });
  R.addEventListener("fatal", (e) => showBanner("Zugang abgelaufen, Upload gestoppt: " + e.detail.error.message, "error"));
  R.addEventListener("store-error", (e) => showBanner(e.detail.message, "warn"));
  R.addEventListener("track-error", (e) => toast(e.detail.message));
  R.addEventListener("discontinuity", () => splitTrack("Audio war unterbrochen"));
  R.addEventListener("track-incomplete", () => showBanner("Einige Teile der Aufnahme konnten nicht hochgeladen werden", "warn"));
}

/** Neue Spur beginnen (Gerätewechsel, Unterbrechung). Die alte wird abgeschlossen und hochgeladen. */
function splitTrack(reason) {
  const R = S.recorder;
  if (!R.track) return;
  const ep = S.room && S.room.episode;
  R.finishActive(reason || "split");
  if (ep && ep.status === "recording") R.ensureTrack(ep.id, null);
}

// ---------------------------------------------------------------------- Nachrichten
function onMessage(m) {
  switch (m.type) {
    case "welcome":
      S.me = { ...S.me, ...m.self };
      applyRoom(m.room);
      if (S.plan && m.assist) S.plan.onWelcome(m.assist);
      break;
    case "room":
      applyRoom(m.room);
      break;
    case "rtc":
      S.mesh.onSignal(m.from, m.data);
      break;
    case "peer.meter":
      S.peerMeter.set(m.id, { ...m, at: performance.now(), clipAt: m.clip ? performance.now() : ((S.peerMeter.get(m.id) || {}).clipAt || 0) });
      updateMeter(m.id, S.peerMeter.get(m.id));
      break;
    case "track.progress":
      S.progress.set(m.participant_id, m);
      renderPeers();
      break;
    case "rec.start":
      S.stopped = null;
      S.recorder.ensureTrack(m.episode_id, m.start_at);
      requestWakeLock();
      break;
    case "rec.stop": {
      let until = m.at;
      for (const a of (S.room && S.room.active_sounds) || []) if (!a.preview) until = Math.max(until, a.at + a.duration_ms);
      S.stopped = { at: m.at, episodeId: S.room && S.room.episode && S.room.episode.id };
      S.recorder.stopAt(until);
      renderBar();
      break;
    }
    case "rec.pause": case "rec.resume":
      break; // Raumzustand folgt
    case "rec.discard":
      S.stopped = null;
      S.progress.clear();
      S.recorder.discardEpisode(m.episode_id);
      UI.uploadKey = null;
      toast(ROLE === "host" ? "Aufnahme verworfen. Der Sendeplan steht für die neue Aufnahme bereit" : "Die Aufnahme wurde verworfen und wird neu begonnen", 6000);
      renderAll();
      break;
    case "sound.play":
      S.jingles.play(m, soundById(m.sound_id));
      break;
    case "sound.stop":
      S.jingles.stop(m);
      break;
    case "marker":
      if (ROLE === "host") toast(`Marker gesetzt bei ${dur(m.at)}`);
      break;
    case "error":
      if (m.code === "unknown_type") break; // z. B. Begleiter, den der Server noch nicht kennt
      toast(m.message || "Fehler");
      break;
    default:
      if (m.type && m.type.startsWith("assist.") && S.plan) {
        S.plan.onMessage(m);
        if (m.type === "assist.segment") renderBoard(); // der nächste Jingle ist ein anderer
      }
  }
}

function soundById(id) { return ((S.room && S.room.sounds) || []).find((s) => s.id === id); }

function applyRoom(room) {
  const prevEp = S.room && S.room.episode;
  S.room = room;
  const ep = room.episode || {};
  S.mesh.sync(room.peers || []);
  S.jingles.preload(room.sounds);
  S.jingles.syncActive(room.active_sounds, new Map((room.sounds || []).map((s) => [s.id, s])));
  const R = S.recorder;
  if (ep.status === "recording") {
    if (!R.track && !R.creating) R.ensureTrack(ep.id, ep.rec_start_server_ms);
    else if (R.track && R.track.episodeId !== ep.id) { R.finishActive("episode"); R.ensureTrack(ep.id, ep.rec_start_server_ms); }
    requestWakeLock();
  } else if (R.track && R.track.stopAtServer == null) {
    // rec.stop verpasst (z. B. während einer Netzunterbrechung): jetzt zu Ende aufnehmen
    S.stopped = S.stopped || { at: S.clock.now(), episodeId: R.track.episodeId };
    R.stopAt(S.clock.now());
  }
  if (ep.status !== "recording") releaseWakeLock();
  if (prevEp && prevEp.status === "recording" && ep.status !== "recording" && !S.stopped) S.stopped = { at: S.clock.now(), episodeId: prevEp.id };
  for (const p of room.peers || []) if (p.track) S.progress.set(p.id, { ...(S.progress.get(p.id) || {}), track_id: p.track.id, recorded_frames: p.track.recorded_frames, uploaded_frames: p.track.uploaded_frames, status: p.track.status });
  if (S.plan) S.plan.onRoom(room);
  renderAll();
}

function onSocketFatal(d) {
  if (d.code === "replaced") {
    S.recorder.finishActive("replaced");
    showBanner("Das Studio ist in einem anderen Fenster geöffnet. Dieses Fenster nimmt nicht mehr auf.", "error");
    return;
  }
  if (d.code === "room_full") {
    showFatal("Das Studio ist voll", d.message || "Es sind höchstens vier Teilnehmer gleichzeitig möglich",
      h("button", { class: "tr-btn tr-btn-primary", type: "button", onclick: () => location.reload() }, "Erneut versuchen"));
    return;
  }
  // unauthorized
  if (ROLE === "guest") Id.forgetGuest(INVITE); else Id.forgetHost();
  showFatal("Zugang abgelaufen oder ungültig", d.message || "Bitte den Einladungslink erneut öffnen");
}

function sendState() {
  if (!S.socket) return;
  S.socket.send({ type: "state", mic: !S.muted, cam: !!S.cam, headphones: !!S.headphones, device: S.mobile ? "mobile" : "desktop" });
}

// ---------------------------------------------------------------------- Eigene Geräte
async function setMuted(on) {
  S.muted = !!on;
  S.recorder.setMute(S.muted);
  if (S.rtcAudio) S.rtcAudio.enabled = !S.muted;
  sendState();
  renderSelf();
}

async function switchMic({ deviceId, headphones }) {
  const R = S.recorder;
  if (deviceId !== undefined) S.micId = deviceId;
  if (headphones !== undefined) S.headphones = headphones;
  Id.prefs.save({ micId: S.micId, headphones: S.headphones });
  let stream;
  try {
    stream = TEST ? Media.testSignal(S.clock, TEST === "local" ? "local" : "server") : await Media.getMic(S.micId, S.headphones);
  } catch (e) { toast(Media.mediaError(e), 6000); renderSelf(); return; }
  const old = S.mic;
  const ep = S.room && S.room.episode;
  // Umschalten beginnt eine neue Spur (SPEC Abschnitt 8)
  if (R.track) R.finishActive("device");
  S.mic = stream;
  R.attach(stream, { headphones: S.headphones, device_label: micLabel(), test_signal: TEST || undefined });
  R.setMute(S.muted);
  const oldRtc = S.rtcAudio;
  S.rtcAudio = stream.getAudioTracks()[0].clone();
  S.rtcAudio.enabled = !S.muted;
  S.mesh.setTracks(S.rtcAudio, S.camStream ? S.camStream.getVideoTracks()[0] : null);
  if (oldRtc) oldRtc.stop();
  if (old) { if (old.__stop) old.__stop(); stopStream(old); }
  if (ep && ep.status === "recording") R.ensureTrack(ep.id, null);
  sendState();
  renderSelf();
}

async function setCam(on) {
  if (on) {
    try { S.camStream = await Media.getCam(); } catch (e) { toast(Media.mediaError(e, "Kamera"), 6000); return; }
  } else { stopStream(S.camStream); S.camStream = null; }
  S.cam = !!S.camStream;
  S.mesh.setTracks(S.rtcAudio, S.camStream ? S.camStream.getVideoTracks()[0] : null);
  sendState();
  renderSelf();
}

async function refreshDeviceSelect() {
  if (!UI.micSel || TEST) return;
  const { mics } = await Media.listDevices().catch(() => ({ mics: [] }));
  fill(UI.micSel, mics.map((d, i) => h("option", { value: d.deviceId, selected: d.deviceId === S.micId }, d.label || `Mikrofon ${i + 1}`)));
}

// ---------------------------------------------------------------------- Live-Medien
function attachMedia(id) {
  const peer = S.mesh.peers.get(id);
  if (!peer) return;
  let m = S.media.get(id);
  if (!m) {
    m = { audio: h("audio", { autoplay: true }), video: h("video", { autoplay: true, playsinline: true, muted: true }) };
    m.video.muted = true;
    m.video.setAttribute("playsinline", "");
    UI.audioSink.append(m.audio);
    S.media.set(id, m);
  }
  const a = peer.stream.getAudioTracks()[0];
  const v = peer.stream.getVideoTracks()[0];
  if (a && (!m.audio.srcObject || m.audio.srcObject.getAudioTracks()[0] !== a)) {
    m.audio.srcObject = new MediaStream([a]);
    m.audio.play().catch(() => needAudioUnlock());
  }
  if (v && (!m.video.srcObject || m.video.srcObject.getVideoTracks()[0] !== v)) {
    m.video.srcObject = new MediaStream([v]);
    m.video.play().catch(() => {});
  }
  renderPeers();
}

function detachMedia(id) {
  const m = S.media.get(id);
  if (!m) return;
  m.audio.srcObject = null; m.video.srcObject = null; m.audio.remove(); m.video.remove();
  S.media.delete(id);
}

function needAudioUnlock() {
  if (UI.unlock) return;
  UI.unlock = h("div", { class: "ps-banner is-warn", role: "alert" },
    h("p", {}, "Der Browser hat die Wiedergabe angehalten"),
    h("button", { class: "tr-btn tr-btn-secondary", type: "button", onclick: async () => {
      await S.ctx.resume().catch(() => {});
      for (const m of S.media.values()) m.audio.play().catch(() => {});
      UI.unlock.remove(); UI.unlock = null;
    } }, icon("speaker"), "Ton einschalten"));
  UI.banners.append(UI.unlock);
}

// ---------------------------------------------------------------------- Wake Lock, Hintergrund, Tasten
async function requestWakeLock() {
  if (S.wakeLock || !("wakeLock" in navigator) || document.visibilityState !== "visible") return;
  try {
    S.wakeLock = await navigator.wakeLock.request("screen");
    S.wakeLock.addEventListener("release", () => { S.wakeLock = null; });
  } catch { /* nicht erlaubt (z. B. Energiesparmodus) */ }
}
function releaseWakeLock() { if (S.wakeLock) { S.wakeLock.release().catch(() => {}); S.wakeLock = null; } }

function isRecording() { const ep = S.room && S.room.episode; return !!ep && ep.status === "recording"; }

function onVisibility() {
  if (document.visibilityState === "hidden") {
    if (isRecording() || S.recorder.busy) S.wasHidden = true;
  } else {
    if (S.ctx.state !== "running") S.ctx.resume().catch(() => {});
    if (isRecording()) requestWakeLock();
    if (S.wasHidden) {
      S.wasHidden = false;
      showBanner(S.mobile
        ? "Das Studio war im Hintergrund. Auf Handy und Tablet kann das die Aufnahme unterbrechen. Bitte im Vordergrund lassen."
        : "Das Studio war im Hintergrund. Die Aufnahme läuft weiter, das Fenster bitte offen lassen.", "warn", 12000);
    }
  }
}

function onKey(e) {
  if (ROLE !== "host" || e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
  const t = e.target;
  if (t && (t.closest("input, textarea, select, [contenteditable=''], [contenteditable='true']") || t.isContentEditable)) return;
  if (document.querySelector("dialog[open]")) return;
  // Sendeplan und Begleiter: Pfeile schalten den Abschnitt, I gibt einen Impuls, / springt ins Nachschlagen, M setzt einen Marker
  if (S.plan && (e.key === "ArrowRight" || e.key === "ArrowLeft")) { e.preventDefault(); S.plan.step(e.key === "ArrowRight" ? 1 : -1); return; }
  if (S.plan && S.plan.supported && (e.key === "i" || e.key === "I")) { e.preventDefault(); S.plan.pulse(); return; }
  if (S.plan && S.plan.supported && e.key === "/") { e.preventDefault(); S.plan.focusLookup(); return; }
  if ((e.key === "m" || e.key === "M") && S.room && S.room.episode && S.room.episode.status === "recording") { e.preventDefault(); ctl({ type: "marker", label: "" }); return; }
  if (!/^[1-9]$/.test(e.key)) return;
  const s = ((S.room && S.room.sounds) || []).find((x) => x.hotkey === e.key);
  if (!s) return;
  e.preventDefault();
  playSound(s);
}

/** Steuerbefehl des Hosts senden; ohne Verbindung sagen, dass er nicht ankam. */
function ctl(msg) {
  if (S.socket && S.socket.send(msg)) return true;
  toast("Keine Verbindung zum Studio. Bitte gleich noch einmal versuchen.", 5000);
  return false;
}

function playSound(s) {
  const ep = S.room && S.room.episode;
  if (ep && ep.paused) { toast("In der Pause sind Jingles gesperrt"); return; }
  ctl({ type: "sound.play", sound_id: s.id });
}

// ====================================================================== Oberfläche
const UI = {};
const LIVE_MQ = window.matchMedia("(min-width: 1100px) and (min-height: 600px)"); // darunter bleibt die einfache Ansicht

function buildStudio() {
  const isHost = ROLE === "host";
  UI.banners = h("div", { class: "ps-banners" });
  UI.conn = h("p", { class: "ps-conn tr-status-warn", role: "status", hidden: true });
  UI.status = h("p", { class: "ps-status", "aria-live": "polite" });
  UI.statusSub = h("p", { class: "ps-status-sub" });
  UI.controls = h("div", { class: "ps-controls" });
  UI.upload = h("div", { class: "ps-upload", hidden: true, role: "status", "aria-live": "polite" });
  UI.audioSink = h("div", { class: "ps-audio-sink", hidden: true });

  UI.stepsHost = h("div", { class: "ps-steps-host" }); // Sendemodus: hier stehen die Abschnitte als Leiste
  const bar = UI.bar = h("section", { class: "ps-bar", "aria-label": "Aufnahme" },
    h("div", { class: "ps-bar-text" }, UI.status, UI.statusSub,
      h("p", { class: "ps-small ps-keys ps-live-only ps-step-keys" }, "Abschnitt", kbd("←"), kbd("→"))),
    UI.stepsHost, UI.controls);

  // Eigenes Gerät
  UI.muteBtn = h("button", { class: "tr-btn tr-btn-secondary", type: "button", id: "ps-mute", onclick: () => setMuted(!S.muted) });
  UI.camBtn = h("button", { class: "tr-btn tr-btn-secondary", type: "button", id: "ps-camtoggle", onclick: () => setCam(!S.cam) });
  UI.hpSel = h("select", { class: "tr-input ps-select", id: "ps-hp", onchange: () => switchMic({ headphones: UI.hpSel.value === "1" }) },
    h("option", { value: "1" }, "Mit Kopfhörern"), h("option", { value: "0" }, "Ohne Kopfhörer"));
  UI.micSel = h("select", { class: "tr-input ps-select", id: "ps-mic2", disabled: !!TEST, onchange: () => switchMic({ deviceId: UI.micSel.value }) });
  UI.muteNote = h("p", { class: "ps-mute-note tr-status-warn", hidden: true }, "Stumm: Live-Ton und Aufnahme sind still");
  UI.selfMeterFill = h("span", { class: "ps-meter-fill" });
  UI.selfMeterDb = h("span", { class: "ps-meter-db" }, "–");
  UI.selfClip = h("span", { class: "ps-meter-clip tr-status-error", hidden: true }, "Übersteuert");
  UI.selfVideo = h("video", { autoplay: true, playsinline: true, muted: true });
  UI.selfVideo.muted = true;
  UI.selfVideo.setAttribute("playsinline", "");
  // Bühne: Kamerabilder groß, sobald eine Kamera läuft (nur live, nie aufgezeichnet)
  UI.tiles = new Map();
  UI.stage = h("div", { class: "ps-stage" });
  UI.stageWrap = h("section", { class: "ps-stage-wrap", "aria-label": "Kamerabilder", hidden: true }, UI.stage);
  const self = UI.selfSec = h("section", { class: "ps-self", "aria-label": "Eigenes Gerät" },
    h("h2", { class: "ps-col-title" }, "Eigenes Gerät"),
    h("div", { class: "ps-meter is-large", role: "meter", "aria-label": "Eigener Pegel" }, h("span", { class: "ps-meter-track" }, UI.selfMeterFill), UI.selfMeterDb),
    UI.selfClip, UI.muteNote,
    h("div", { class: "ps-self-actions" }, UI.muteBtn, UI.camBtn),
    h("div", { class: "ps-self-fields" },
      h("div", { class: "ps-field" }, h("label", { class: "tr-label", for: "ps-hp" }, "Hören über"), UI.hpSel),
      h("div", { class: "ps-field" }, h("label", { class: "tr-label", for: "ps-mic2" }, "Mikrofon"), UI.micSel)),
    h("p", { class: "ps-small" }, "Ein Wechsel beginnt eine neue Spur. Bisheriges bleibt erhalten."));

  UI.peers = h("ul", { class: "ps-peers", "aria-label": "Teilnehmer" });
  const peers = UI.peersSec = h("section", { class: "ps-peers-wrap", "aria-label": "Teilnehmer" }, h("h2", { class: "ps-col-title" }, "Teilnehmer"), UI.peers);

  const main = [UI.banners, UI.conn, bar, UI.upload, UI.stageWrap];
  if (isHost) {
    UI.board = h("div", { class: "ps-board" });
    UI.running = h("ul", { class: "ps-running", "aria-label": "Laufende Sounds" });
    UI.markerIn = h("input", { class: "tr-input", id: "ps-marker", type: "text", maxlength: 120, placeholder: "Bezeichnung (optional)", autocomplete: "off" });
    UI.markerBtn = h("button", { class: "tr-btn tr-btn-secondary", type: "submit" }, icon("flag"), "Marker setzen");
    const marker = h("form", { class: "ps-marker", onsubmit: (e) => { e.preventDefault(); ctl({ type: "marker", label: UI.markerIn.value.trim() }); UI.markerIn.value = ""; } },
      h("label", { class: "tr-label", for: "ps-marker" }, "Marker"), h("div", { class: "ps-inline" }, UI.markerIn, UI.markerBtn));
    const board = UI.boardSec = h("section", { class: "ps-sounds", "aria-label": "Soundboard" },
      h("div", { class: "ps-col-head" }, h("h2", { class: "ps-col-title" }, "Soundboard"), h("span", { class: "ps-small ps-keys" }, kbd("1"), "bis", kbd("9"))),
      UI.board, UI.running, marker, h("p", { class: "ps-small ps-live-only ps-hint-keys" }, "Marker setzen", kbd("M")));
    // Sendemodus (nur während der Aufnahme): Pegel und Zustand in einer Zeile, alles Übrige hinter „Technik“
    UI.liveMeters = h("div", { class: "ps-live-meters" });
    UI.liveState = h("p", { class: "ps-live-state", role: "status" });
    UI.liveMute = h("button", { class: "ps-icon-btn", type: "button", id: "ps-live-mute", onclick: () => setMuted(!S.muted) });
    UI.liveCam = h("button", { class: "ps-icon-btn", type: "button", id: "ps-live-cam", onclick: () => setCam(!S.cam) });
    UI.liveBox = h("section", { class: "ps-livebox", "aria-label": "Pegel und Zustand", hidden: true },
      UI.liveMeters,
      h("div", { class: "ps-live-foot" }, UI.liveState,
        h("div", { class: "ps-live-actions" }, UI.liveMute, UI.liveCam,
          h("button", { class: "tr-btn tr-btn-secondary ps-btn-compact", type: "button", id: "ps-tech-open", onclick: () => UI.tech.showModal() }, "Technik"))));
    UI.techBody = h("div", { class: "ps-tech-body" });
    UI.tech = h("dialog", { class: "tr-modal ps-dialog ps-tech", "aria-labelledby": "ps-tech-title" },
      h("div", { class: "ps-tech-head" }, h("h2", { id: "ps-tech-title" }, "Technik"),
        h("button", { class: "tr-btn tr-btn-secondary", type: "button", id: "ps-tech-close", onclick: () => UI.tech.close() }, "Schließen")),
      UI.techBody);
    main.push(UI.liveBox, peers, board, self);
    UI.main = h("div", { class: "ps-main" }, main);
    UI.cardsCol = h("div", { class: "ps-live-cards", hidden: true });
    UI.planCol = h("aside", { class: "ps-plan", "aria-label": "Sendeplan" });
    S.plan = new Plan.PlanColumn(UI.planCol, { send: (m) => S.socket && S.socket.send(m), token: () => S.token, now: () => S.clock.now() });
    UI.root = h("div", { class: "ps-studio is-host" }, UI.main, UI.cardsCol, UI.planCol);
    fill(app, UI.root, UI.audioSink, stopDialog(), UI.tech);
    LIVE_MQ.addEventListener("change", () => renderAll());
  } else {
    main.push(self, peers);
    UI.root = h("div", { class: "ps-studio is-guest" }, h("div", { class: "ps-main" }, main));
    fill(app, UI.root, UI.audioSink);
  }
  refreshDeviceSelect();
  if (TEST) fill(UI.micSel, h("option", {}, "Testsignal"));
  renderAll();
}

function stopDialog() {
  UI.dialog = h("dialog", { class: "tr-modal ps-dialog", "aria-labelledby": "ps-dlg-title" },
    h("form", { method: "dialog" },
      h("h2", { id: "ps-dlg-title" }, "Aufnahme beenden?"),
      h("p", {}, "Danach laden alle Teilnehmer ihre Spuren fertig hoch. Weiter aufnehmen geht dann nicht mehr."),
      h("div", { class: "ps-dialog-actions" },
        h("button", { class: "tr-btn tr-btn-secondary", value: "cancel" }, "Weiter aufnehmen"),
        h("button", { class: "tr-btn tr-btn-primary", value: "stop", id: "ps-stop-confirm" }, icon("square"), "Beenden"))));
  UI.dialog.addEventListener("close", () => { if (UI.dialog.returnValue === "stop") ctl({ type: "rec.stop" }); });
  return UI.dialog;
}

/** Tastenkappe für ein Tastenkürzel. */
function kbd(key) { return h("kbd", { class: "ps-kbd" }, key); }

function discardButton(episodeId) {
  return h("button", { class: "tr-btn tr-btn-secondary", type: "button", id: "ps-discard", onclick: () => discardRecording(episodeId) }, icon("rotate-ccw"), "Aufnahme verwerfen");
}

/** Rückfrage als Dialog. Liefert true, wenn bestätigt wurde. */
function ask(title, text, okLabel) {
  return new Promise((resolve) => {
    const dlg = h("dialog", { class: "tr-modal ps-dialog", "aria-labelledby": "ps-ask-title" },
      h("form", { method: "dialog" },
        h("h2", { id: "ps-ask-title" }, title),
        h("p", {}, text),
        h("div", { class: "ps-dialog-actions" },
          h("button", { class: "tr-btn tr-btn-secondary", value: "cancel" }, "Abbrechen"),
          h("button", { class: "tr-btn tr-btn-primary", value: "ok", id: "ps-ask-ok" }, okLabel))));
    dlg.addEventListener("close", () => { const ok = dlg.returnValue === "ok"; dlg.remove(); resolve(ok); });
    document.body.append(dlg);
    dlg.showModal();
  });
}

/** Aufnahme verwerfen und neu ansetzen: Spuren weg, der Sendeplan mit den Dossiers bleibt (SPEC Abschnitt 16). */
async function discardRecording(episodeId) {
  if (!(await ask("Aufnahme verwerfen?", "Alle Spuren dieser Aufnahme werden gelöscht. Der Sendeplan mit den Dossiers bleibt und steht für die neue Aufnahme bereit.", "Verwerfen"))) return;
  const call = (body) => api("POST", `/api/host/episodes/${encodeURIComponent(episodeId)}/discard`, { token: S.token, body });
  try {
    await call({});
  } catch (e) {
    if (e.code !== "plan_conflict") { toast(e.message || "Verwerfen fehlgeschlagen", 6000); return; }
    if (!(await ask("Sendeplan geht verloren", e.message, "Trotzdem verwerfen"))) return;
    try { await call({ drop_plan: true }); } catch (e2) { toast(e2.message || "Verwerfen fehlgeschlagen", 6000); }
  }
}

function setConn(text) { if (!UI.conn) return; UI.conn.textContent = text; UI.conn.hidden = !text; }

function showBanner(text, kind = "warn", ms = 0) {
  if (!UI.banners) { toast(text, 8000); return; }
  const el = h("div", { class: `ps-banner is-${kind}`, role: kind === "error" ? "alert" : "status" },
    h("p", {}, text),
    h("button", { type: "button", class: "ps-icon-btn", "aria-label": "Schließen", onclick: () => el.remove() }, icon("x")));
  UI.banners.append(el);
  if (ms) setTimeout(() => el.remove(), ms);
}

/**
 * Sendemodus: Solange die Aufnahme läuft, passt beim Host alles auf einen Bildschirm (SPEC Abschnitt 8).
 * Kopfzeile mit Ablauf, links Gespräch und Soundboard, in der Mitte die Karten, rechts das Dossier. Die Navigation
 * der Seite ist ausgeblendet, Teilnehmerliste und Gerätewahl liegen hinter „Technik“. Nur auf breiten Bildschirmen.
 */
function syncLive() {
  if (ROLE !== "host" || !UI.root || !S.plan) return;
  const ep = S.room && S.room.episode;
  const on = !!(ep && ep.status === "recording" && LIVE_MQ.matches);
  if (on === !!UI.live) return;
  UI.live = on;
  document.body.classList.toggle("ps-live", on);
  UI.root.classList.toggle("is-live", on);
  if (on) {
    UI.root.prepend(UI.bar);
    UI.techBody.append(UI.peersSec, UI.selfSec);
  } else {
    if (UI.tech.open) UI.tech.close();
    UI.main.insertBefore(UI.bar, UI.upload);
    UI.main.insertBefore(UI.peersSec, UI.boardSec);
    UI.main.append(UI.selfSec);
  }
  UI.liveBox.hidden = !on;
  UI.cardsCol.hidden = !on;
  S.plan.setLive(on, { steps: UI.stepsHost, cards: UI.cardsCol });
  UI.boardKey = null;
  window.scrollTo(0, 0);
}

function renderAll() {
  if (!UI.status) return;
  syncLive();
  renderBar();
  renderSelf();
  renderPeers();
  renderBoard();
}

function epState() {
  const ep = (S.room && S.room.episode) || { status: "idle" };
  const now = S.clock.now();
  const start = ep.rec_start_server_ms;
  return { ep, now, start, elapsed: start ? now - start : 0 };
}

function renderBar() {
  const { ep, now, start, elapsed } = epState();
  const isHost = ROLE === "host";
  let status = "", cls = "", sub = "";
  if (ep.status === "recording") {
    if (now < start) { status = `Aufnahme beginnt in ${Math.ceil((start - now) / 1000)}`; cls = "tr-status-error"; }
    else if (ep.paused) {
      const p = ep.pauses[ep.pauses.length - 1];
      status = `Pause ${dur(elapsed)}`; cls = "tr-status-warn";
      sub = `Pausiert seit ${dur(elapsed - p.from)}. Aufnahme läuft lokal weiter, der Bereich wird später herausgeschnitten.`;
    } else {
      status = `Aufnahme läuft ${dur(elapsed)}`; cls = "tr-status-error";
      const paused = (ep.pauses || []).reduce((a, p) => a + ((p.to ?? elapsed) - p.from), 0);
      if (paused > 0) sub = `davon Pause ${dur(paused)}`;
    }
  } else if (ep.status === "uploading") {
    status = "Aufnahme beendet"; sub = "Spuren werden hochgeladen";
  } else {
    status = S.socket && S.socket.connected ? "Bereit" : "Verbindet";
    sub = isHost ? "Aufnahme starten, sobald alle da sind" : "Der Host startet die Aufnahme";
  }
  if (S.muted && ep.status === "recording") sub = "Stumm: Die eigene Spur nimmt Stille auf" + (sub ? `. ${sub}` : "");
  setText(UI.status, status);
  UI.status.className = "ps-status " + cls;
  setText(UI.statusSub, sub);

  if (isHost) {
    const key = `${ep.status}|${ep.paused}|${now < (start || 0)}`;
    if (key !== UI.controlsKey) {
      UI.controlsKey = key;
      const b = [];
      if (ep.status === "recording") {
        b.push(ep.paused
          ? h("button", { class: "tr-btn tr-btn-secondary", type: "button", id: "ps-resume", onclick: () => ctl({ type: "rec.resume" }) }, icon("play"), "Weiter")
          : h("button", { class: "tr-btn tr-btn-secondary", type: "button", id: "ps-pause", onclick: () => ctl({ type: "rec.pause" }) }, icon("pause"), "Pause"));
        b.push(h("button", { class: "tr-btn tr-btn-secondary", type: "button", id: "ps-stop", onclick: () => UI.dialog.showModal() }, icon("square"), "Beenden"));
      } else if (ep.status === "uploading") {
        b.push(h("span", { class: "ps-small" }, "Neue Aufnahme, sobald alle Spuren oben sind"));
        b.push(discardButton(ep.id));
      } else {
        b.push(h("button", { class: "tr-btn tr-btn-primary", type: "button", id: "ps-start", onclick: () => ctl({ type: "rec.start" }) }, icon("circle"), "Aufnahme starten"));
      }
      fill(UI.controls, b);
    }
  }
  renderUpload();
}

function renderUpload() {
  const R = S.recorder;
  const st = R.status();
  const recovered = [...R.tracks.values()].some((t) => t.recovered && !t.done);
  const show = !!S.stopped || recovered || (ROLE === "host" && S.room && S.room.episode.status === "uploading");
  UI.upload.hidden = !show;
  if (!show) return;
  const total = st.totalFrames, done = st.uploadedFrames;
  const own = st.openTracks > 0 || st.recording || st.creating;
  const others = ROLE === "host" ? peersPending() : 0;
  const key = [own, others, Math.round((done / Math.max(1, total)) * 1000), st.retrying, S.stopped && S.stopped.episodeId].join("|");
  if (key === UI.uploadKey) return;
  UI.uploadKey = key;
  if (own) {
    const pct = total > 0 ? Math.min(100, (done / total) * 100) : 0;
    fill(UI.upload,
      h("p", { class: "ps-upload-title tr-status-warn" }, "Upload läuft, Fenster offen lassen"),
      h("div", { class: "ps-progress", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": pct.toFixed(0) }, h("span", { style: `width:${pct.toFixed(1)}%` })),
      h("p", { class: "ps-small" }, `${dur((done / st.sr) * 1000)} von ${dur((total / st.sr) * 1000)} hochgeladen` + (st.retrying ? ". Netz gestört, neuer Versuch läuft" : "")));
  } else if (others > 0) {
    fill(UI.upload,
      h("p", { class: "ps-upload-title tr-status-ok" }, "Eigene Spur vollständig hochgeladen"),
      h("p", { class: "ps-small" }, others === 1 ? "Ein Teilnehmer lädt noch hoch" : `${others} Teilnehmer laden noch hoch`));
  } else {
    const epId = S.stopped && S.stopped.episodeId;
    fill(UI.upload,
      h("p", { class: "ps-upload-title tr-status-ok", id: "ps-done" }, "Fertig, Fenster kann geschlossen werden"),
      ROLE === "host" && epId ? h("div", { class: "ps-upload-actions" }, h("a", { href: `/episodes/${epId}`, id: "ps-episode-link" }, "Zur Folge"), discardButton(epId)) : null);
  }
}

function peersPending() {
  let n = 0;
  for (const p of (S.room && S.room.peers) || []) {
    if (p.id === S.me.id || !p.track) continue;
    const pr = S.progress.get(p.id) || p.track;
    if (pr.status && pr.status !== "complete" && pr.status !== "assembled") n++;
  }
  return n;
}

function setText(el, t) { if (el.textContent !== t) el.textContent = t; }

function renderSelf() {
  if (!UI.muteBtn) return;
  fill(UI.muteBtn, icon(S.muted ? "mic-off" : "mic"), S.muted ? "Stummschaltung aufheben" : "Stummschalten");
  UI.muteBtn.setAttribute("aria-pressed", S.muted ? "true" : "false");
  UI.muteNote.hidden = !S.muted;
  fill(UI.camBtn, icon(S.cam ? "video-off" : "video"), S.cam ? "Kamera aus" : "Kamera an");
  UI.camBtn.setAttribute("aria-pressed", S.cam ? "true" : "false");
  UI.hpSel.value = S.headphones ? "1" : "0";
  if (UI.liveMute) {
    fill(UI.liveMute, icon(S.muted ? "mic-off" : "mic"));
    UI.liveMute.setAttribute("aria-label", S.muted ? "Stummschaltung aufheben" : "Stummschalten");
    UI.liveMute.title = S.muted ? "Stummschaltung aufheben" : "Stummschalten";
    UI.liveMute.setAttribute("aria-pressed", S.muted ? "true" : "false");
    fill(UI.liveCam, icon(S.cam ? "video-off" : "video"));
    UI.liveCam.setAttribute("aria-label", S.cam ? "Kamera aus" : "Kamera an");
    UI.liveCam.title = S.cam ? "Kamera aus" : "Kamera an";
    UI.liveCam.setAttribute("aria-pressed", S.cam ? "true" : "false");
  }
  if (S.camStream) { if (UI.selfVideo.srcObject !== S.camStream) UI.selfVideo.srcObject = S.camStream; }
  else UI.selfVideo.srcObject = null;
  renderStage();
}

/**
 * Bühne: je verbundenem Gegenüber eine große Kachel (Bild oder, ohne Kamera, der Name), dazu das eigene Bild.
 * Die Kacheln bleiben im Dokument stehen und werden nur angepasst, damit laufende Videos nicht neu starten.
 * Sichtbar ist die Bühne nur, wenn mindestens eine Kamera läuft.
 */
function renderStage() {
  if (!UI.stage || !S.room) return;
  const want = [];
  for (const p of S.room.peers || []) {
    if (p.id === S.me.id || !p.connected) continue;
    const m = S.media.get(p.id);
    want.push({ id: p.id, name: p.name, video: m && p.cam && m.video.srcObject ? m.video : null, muted: !p.mic, self: false });
  }
  want.sort((a, b) => a.name.localeCompare(b.name));
  const others = want.length;
  if (S.camStream) want.push({ id: S.me.id, name: S.me.name, video: UI.selfVideo, muted: S.muted, self: true });
  const seen = new Set();
  let prev = null;
  for (const w of want) {
    let t = UI.tiles.get(w.id);
    if (!t) {
      t = { big: h("span", { class: "ps-tile-big" }), name: h("span", {}), mute: h("span", { class: "tr-status-warn" }, "stumm") };
      t.tag = h("p", { class: "ps-tile-tag" }, t.name, t.mute);
      t.el = h("div", { class: "ps-tile" }, t.big, t.tag);
      UI.tiles.set(w.id, t);
    }
    seen.add(w.id);
    t.el.classList.toggle("is-self", w.self);
    t.el.classList.toggle("is-off", !w.video);
    const cur = t.el.querySelector("video");
    if (cur && cur !== w.video) cur.remove();
    if (w.video && w.video.parentNode !== t.el) { t.el.prepend(w.video); w.video.play().catch(() => {}); }
    setText(t.big, w.name);
    t.big.hidden = !!w.video;
    setText(t.name, w.name);
    t.name.hidden = !w.video; // ohne Bild steht der Name schon groß in der Kachel
    t.mute.hidden = !w.muted;
    t.tag.hidden = !w.video && !w.muted;
    const next = prev ? prev.nextSibling : UI.stage.firstChild;
    if (next !== t.el) UI.stage.insertBefore(t.el, next);
    prev = t.el;
  }
  for (const [id, t] of [...UI.tiles]) if (!seen.has(id)) { t.el.remove(); UI.tiles.delete(id); }
  const on = want.some((w) => w.video);
  UI.stage.dataset.others = String(Math.min(others, 3));
  UI.stage.dataset.self = S.camStream ? "1" : "0";
  UI.stageWrap.hidden = !on;
  UI.root.classList.toggle("has-stage", on);
}

const QUALITY_CLASS = { gut: "tr-status-ok", mittel: "tr-status-warn", schwach: "tr-status-error", getrennt: "tr-status-error", "kein Ton": "tr-status-warn" };

function renderPeers() {
  if (!UI.peers || !S.room) return;
  const list = (S.room.peers || []).slice().sort((a, b) => (a.id === S.me.id ? -1 : b.id === S.me.id ? 1 : a.role === "host" ? -1 : b.role === "host" ? 1 : a.name.localeCompare(b.name)));
  const isHost = ROLE === "host";
  const rows = list.map((p) => {
    const self = p.id === S.me.id;
    const conn = self ? null : S.mesh.peers.get(p.id);
    const facts = [];
    if (!p.connected) facts.push(h("span", { class: "tr-status-error" }, "nicht verbunden"));
    if (p.connected && !p.mic) facts.push(h("span", { class: "tr-status-warn" }, "stumm"));
    facts.push(p.headphones ? "Kopfhörer" : "ohne Kopfhörer");
    if (isHost || self) facts.push(p.device === "mobile" ? "Handy oder Tablet" : "Computer");
    if (conn && p.connected) facts.push(h("span", { class: QUALITY_CLASS[conn.quality] || "" }, `Verbindung ${conn.quality}`));
    const pr = S.progress.get(p.id);
    let up = null;
    if (pr && pr.track_id && (isHost || self)) {
      const sr = 48000; // Anzeige: Server meldet Frames; alle Spuren laufen mit 48 kHz, sonst nur leicht ungenau
      const rec = pr.recorded_frames || 0, upl = pr.uploaded_frames || 0;
      const complete = pr.status === "complete" || pr.status === "assembled";
      up = h("span", { class: complete ? "tr-status-ok" : "" }, complete ? "Upload vollständig" : `Upload ${dur((upl / sr) * 1000)} von ${dur((Math.max(rec, upl) / sr) * 1000)}`);
    }
    if (up) facts.push(up);
    const fillEl = h("span", { class: "ps-meter-fill", "data-meter": p.id });
    const dbEl = h("span", { class: "ps-meter-db", "data-meter-db": p.id }, "–");
    const clipEl = h("span", { class: "ps-meter-clip tr-status-error", "data-clip": p.id, hidden: true }, "Übersteuert");
    const li = h("li", { class: "ps-peer" + (p.connected ? "" : " is-away"), "data-peer": p.id },
      h("div", { class: "ps-peer-body" },
        h("p", { class: "ps-peer-name" }, h("span", {}, p.name), h("span", { class: "ps-peer-role" }, self ? "dieses Gerät" : p.role === "host" ? "Host" : "Gast")),
        h("div", { class: "ps-meter", role: "meter", "aria-label": `Pegel ${p.name}` }, h("span", { class: "ps-meter-track" }, fillEl), dbEl),
        clipEl,
        h("p", { class: "ps-peer-facts" }, facts.flatMap((f, i) => (i ? [" · ", f] : [f])))));
    const m = self ? S.localMeter : S.peerMeter.get(p.id);
    if (m) applyMeter(fillEl, dbEl, clipEl, m);
    return li;
  });
  fill(UI.peers, rows);
  renderLiveBox(list);
  renderStage();
}

/** Sendemodus: je Teilnehmer ein schmaler Pegel, darunter eine Zeile, die nur bei Problemen laut wird. */
function renderLiveBox(list) {
  if (!UI.liveBox || UI.liveBox.hidden) return;
  const issues = [];
  const rows = list.filter((p) => p.connected).map((p) => {
    const fillEl = h("span", { class: "ps-meter-fill", "data-lmeter": p.id });
    const dbEl = h("span", { class: "ps-meter-db", "data-lmeter-db": p.id }, "–");
    const m = p.id === S.me.id ? S.localMeter : S.peerMeter.get(p.id);
    if (m) applyMeter(fillEl, dbEl, null, m);
    return h("div", { class: "ps-meter", role: "meter", "aria-label": `Pegel ${p.name}` }, h("span", { class: "ps-meter-name" }, p.name), h("span", { class: "ps-meter-track" }, fillEl), dbEl);
  });
  for (const p of list) {
    const self = p.id === S.me.id;
    const conn = self ? null : S.mesh.peers.get(p.id);
    if (!p.connected) { issues.push([`${p.name}: nicht verbunden`, "tr-status-error"]); continue; }
    if (!p.mic) issues.push([`${p.name}: stumm`, "tr-status-warn"]);
    if (conn && (conn.quality === "schwach" || conn.quality === "getrennt")) issues.push([`${p.name}: Verbindung ${conn.quality}`, "tr-status-error"]);
    if (conn && conn.quality === "kein Ton") issues.push([`${p.name}: kein Ton`, "tr-status-warn"]);
    const m = self ? S.localMeter : S.peerMeter.get(p.id);
    if (m && m.clipAt && performance.now() - m.clipAt < 2500) issues.push([`${p.name}: übersteuert`, "tr-status-error"]);
    const pr = S.progress.get(p.id);
    const lag = pr && pr.track_id ? ((pr.recorded_frames || 0) - (pr.uploaded_frames || 0)) / 48000 : 0;
    if (lag > 30) issues.push([`${p.name}: Upload ${Math.round(lag)} s zurück`, "tr-status-warn"]);
  }
  fill(UI.liveMeters, rows);
  fill(UI.liveState, issues.length
    ? issues.flatMap(([text, cls], i) => (i ? [" · ", h("span", { class: cls }, text)] : [h("span", { class: cls }, text)]))
    : h("span", {}, "Verbindung gut, alle Spuren laden mit"));
}

function updateMeter(id, m) {
  if (!UI.peers) return;
  const fillEl = UI.peers.querySelector(`[data-meter="${CSS.escape(id)}"]`);
  const dbEl = UI.peers.querySelector(`[data-meter-db="${CSS.escape(id)}"]`);
  const clipEl = UI.peers.querySelector(`[data-clip="${CSS.escape(id)}"]`);
  if (fillEl) applyMeter(fillEl, dbEl, clipEl, m);
  if (id === S.me.id && UI.selfMeterFill) applyMeter(UI.selfMeterFill, UI.selfMeterDb, UI.selfClip, m);
  if (UI.liveMeters && UI.live) {
    const lf = UI.liveMeters.querySelector(`[data-lmeter="${CSS.escape(id)}"]`);
    if (lf) applyMeter(lf, UI.liveMeters.querySelector(`[data-lmeter-db="${CSS.escape(id)}"]`), null, m);
  }
}

function applyMeter(fillEl, dbEl, clipEl, m) {
  const stale = performance.now() - m.at > 1500;
  setMeter(fillEl, dbEl, stale ? -120 : m.peak_db);
  if (clipEl) clipEl.hidden = !(m.clipAt && performance.now() - m.clipAt < 2500);
}

function renderBoard() {
  if (ROLE !== "host" || !UI.board) return;
  const sounds = (S.room && S.room.sounds) || [];
  const ep = S.room && S.room.episode;
  const locked = !!(ep && ep.paused);
  // Sendemodus: Der Jingle, der laut Ablauf als Nächstes kommt, ist markiert
  const nextKind = UI.live && S.plan ? S.plan.nextKind() : null;
  const isNext = (s) => !!nextKind && s.segment_kind === nextKind;
  const key = JSON.stringify([sounds.map((s) => [s.id, s.label, s.hotkey, s.duration_ms]), locked, [...S.jingles.failed], nextKind]);
  if (key !== UI.boardKey) {
    UI.boardKey = key;
    if (!sounds.length) {
      fill(UI.board, h("p", { class: "ps-empty" }, "Noch keine Sounds. ", h("a", { href: "/sounds" }, "Soundboard einrichten")));
    } else {
      fill(UI.board, sounds.map((s) => h("button", { type: "button", class: "ps-sound" + (isNext(s) ? " is-next" : ""), "data-sound": s.id, disabled: locked, title: locked ? "In der Pause gesperrt" : null, onclick: () => playSound(s) },
        h("span", { class: "ps-key" + (s.hotkey ? "" : " is-empty") }, s.hotkey || ""),
        h("span", { class: "ps-sound-label" }, s.label),
        h("span", { class: "ps-sound-dur" }, S.jingles.failed.has(s.id) ? "nicht geladen" : isNext(s) ? "als Nächstes dran" : dur(s.duration_ms)))));
    }
  }
  renderRunning();
}

function renderRunning() {
  if (!UI.running) return;
  const list = S.jingles.remaining();
  const key = list.map((r) => `${r.event_id}:${Math.ceil(r.left / 1000)}:${r.stopping}`).join(",");
  if (key === UI.runningKey) return;
  UI.runningKey = key;
  fill(UI.running, list.map((r) => {
    const s = soundById(r.sound_id) || { label: "Sound", fade_out_ms: 400 };
    return h("li", { class: "ps-run" },
      h("span", { class: "ps-run-label" }, s.label),
      h("span", { class: "ps-run-left" }, r.stopping ? "blendet aus" : `noch ${dur(r.left + 999)}`),
      r.stopping ? null : h("button", { type: "button", class: "tr-btn tr-btn-secondary ps-run-stop", onclick: () => ctl({ type: "sound.stop", event_id: r.event_id, fade_ms: s.fade_out_ms ?? 400 }) }, icon("volume-x"), "Ausblenden"));
  }));
}

function tick() {
  if (!UI.status) return;
  renderBar();
  renderRunning();
  if (S.plan) S.plan.tick();
  // veraltete Pegel auslaufen lassen
  for (const [id, m] of S.peerMeter) if (performance.now() - m.at > 1500 && performance.now() - m.at < 2000) updateMeter(id, m);
}
