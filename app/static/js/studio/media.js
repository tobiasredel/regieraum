// Geräte: Unterstützung prüfen, Mikrofon und Kamera holen, verständliche Fehlermeldungen, Testton, Testquelle.

const V = new URL(import.meta.url).search;
const { t } = await import(`../i18n.js${V}`);

/** Fehlt etwas Nötiges? Liefert eine deutsche Meldung oder null. Nur Feature-Erkennung, keine Browser-Weichen. */
export function missingSupport() {
  if (!window.isSecureContext) return t("Das Studio funktioniert nur über eine sichere Verbindung (https). Bitte den Link genau so öffnen, wie er verschickt wurde");
  const need = [];
  if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function") need.push(t("Mikrofonzugriff"));
  const AC = window.AudioContext || window.webkitAudioContext;
  if (!AC || !("audioWorklet" in AC.prototype) || typeof AudioWorkletNode === "undefined") need.push("AudioWorklet");
  if (typeof RTCPeerConnection === "undefined") need.push("WebRTC");
  if (typeof WebSocket === "undefined") need.push("WebSocket");
  if (!need.length) return null;
  return t("Dieser Browser kann hier nicht aufnehmen (es fehlt: {missing}). Bitte ein aktuelles Chrome, Edge, Firefox oder Safari verwenden", { missing: need.join(", ") });
}

export function audioConstraints(deviceId, headphones) {
  return {
    deviceId: deviceId ? { exact: deviceId } : undefined,
    channelCount: { ideal: 1 },
    sampleRate: { ideal: 48000 },
    sampleSize: { ideal: 24 },
    // Mit Kopfhörern unbearbeitetes Signal; ohne Kopfhörer nur Echounterdrückung (SPEC Abschnitt 8)
    echoCancellation: !headphones,
    noiseSuppression: false,
    autoGainControl: false,
  };
}

export async function getMic(deviceId, headphones) {
  try {
    return await navigator.mediaDevices.getUserMedia({ audio: audioConstraints(deviceId, headphones) });
  } catch (e) {
    // Gerät verschwunden: mit dem Standardgerät erneut versuchen
    if (deviceId && (e.name === "OverconstrainedError" || e.name === "NotFoundError")) {
      return navigator.mediaDevices.getUserMedia({ audio: audioConstraints(null, headphones) });
    }
    throw e;
  }
}

export async function getCam() {
  // 16:9 für die Bühne. Nur Wunschwerte: Handykameras liefern eigene Formate, die Bitrate begrenzt rtc.js
  return navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 640 }, height: { ideal: 360 }, aspectRatio: { ideal: 16 / 9 }, frameRate: { ideal: 24, max: 30 }, facingMode: "user" } });
}

export async function listDevices() {
  const all = await navigator.mediaDevices.enumerateDevices();
  return {
    mics: all.filter((d) => d.kind === "audioinput"),
    cams: all.filter((d) => d.kind === "videoinput"),
  };
}

export function mediaError(e, what = "Mikrofon") {
  const n = e && e.name;
  const cam = what === "Kamera";
  if (n === "NotAllowedError" || n === "SecurityError") {
    return cam
      ? t("Zugriff auf die Kamera wurde verweigert. In den Website-Einstellungen des Browsers die Kamera für diese Seite erlauben und die Seite neu laden.")
      : t("Zugriff auf das Mikrofon wurde verweigert. In den Website-Einstellungen des Browsers das Mikrofon für diese Seite erlauben und die Seite neu laden.");
  }
  if (n === "NotFoundError" || n === "OverconstrainedError") {
    return cam ? t("Keine Kamera gefunden. Bitte ein Gerät anschließen und die Seite neu laden.") : t("Kein Mikrofon gefunden. Bitte ein Gerät anschließen und die Seite neu laden.");
  }
  if (n === "NotReadableError" || n === "AbortError") {
    return cam ? t("Die Kamera lässt sich nicht öffnen. Vermutlich nutzt ein anderes Programm sie gerade.") : t("Das Mikrofon lässt sich nicht öffnen. Vermutlich nutzt ein anderes Programm es gerade.");
  }
  return t("{what} nicht verfügbar ({reason})", { what: cam ? t("Kamera") : t("Mikrofon"), reason: (e && e.message) || n || t("unbekannter Fehler") });
}

/** Kurzer Testton über den Ausgang (1 s, 440 Hz, leise). */
export function testTone(ctx) {
  const t = ctx.currentTime + 0.05;
  const osc = new OscillatorNode(ctx, { frequency: 440 });
  const g = new GainNode(ctx, { gain: 0 });
  osc.connect(g).connect(ctx.destination);
  g.gain.setValueAtTime(0, t);
  g.gain.linearRampToValueAtTime(0.25, t + 0.03);
  g.gain.setValueAtTime(0.25, t + 0.9);
  g.gain.linearRampToValueAtTime(0, t + 1);
  osc.start(t);
  osc.stop(t + 1.05);
  return 1050;
}

/**
 * Testquelle für Messungen (nur mit ?testsignal=…): ein kurzer Klick zu jeder vollen Sekunde, statt Mikrofon.
 * `mode` "server": volle Serversekunde nach Uhrenabgleich. "local": volle Sekunde der lokalen Systemuhr
 * (nur sinnvoll, wenn Browser und Server dieselbe Uhr haben; prüft dann auch den Uhrenabgleich mit).
 * Die Klicks entstehen in einem eigenen AudioContext und kommen als MediaStream an, wie ein Mikrofon.
 */
export function testSignal(serverClock, mode) {
  const AC = window.AudioContext || window.webkitAudioContext;
  const ctx = new AC({ sampleRate: 48000 });
  const dest = new MediaStreamAudioDestinationNode(ctx, { channelCount: 1 });
  const click = new AudioBuffer({ length: 96, numberOfChannels: 1, sampleRate: ctx.sampleRate });
  const d = click.getChannelData(0);
  for (let i = 0; i < d.length; i++) d[i] = 0.8 * Math.exp(-i / 8); // einseitiger Impuls, kein Nyquist-Anteil
  // dazu leises Rauschen, damit Pegel und Upload „echt“ aussehen (−50 dBFS)
  const noise = new AudioBuffer({ length: ctx.sampleRate, numberOfChannels: 1, sampleRate: ctx.sampleRate });
  const nd = noise.getChannelData(0);
  for (let i = 0; i < nd.length; i++) nd[i] = (Math.random() * 2 - 1) * 0.003;
  const ns = new AudioBufferSourceNode(ctx, { buffer: noise, loop: true });
  ns.connect(dest);
  ns.start();
  let scheduledUntil = 0;
  const epochNow = () => (mode === "local" ? Date.now() : serverClock.now());
  // Abbildung Context-Zeit → performance.now() glätten: currentTime springt blockweise, das Minimum von
  // performance.now() − currentTime über die letzten Sekunden ist die Verarbeitungszeit ohne Blockzittern
  const obs = [];
  const sampleTimer = setInterval(() => {
    if (ctx.state !== "running") return;
    const now = performance.now();
    obs.push({ at: now, d: now - ctx.currentTime * 1000 });
    while (obs.length && now - obs[0].at > 5000) obs.shift();
  }, 7);
  const timer = setInterval(() => {
    if (ctx.state !== "running") { ctx.resume().catch(() => {}); return; }
    if (mode !== "local" && !serverClock.ready) return;
    if (obs.length < 20) return;
    let best = Infinity;
    for (const o of obs) if (o.d < best) best = o.d;
    const nowEpoch = epochNow();
    const perfNow = performance.now();
    for (let s = Math.ceil((nowEpoch + 150) / 1000) * 1000; s < nowEpoch + 1200; s += 1000) {
      if (s <= scheduledUntil) continue;
      const perfTarget = perfNow + (s - nowEpoch);
      const t = Math.round(((perfTarget - best) / 1000) * ctx.sampleRate) / ctx.sampleRate; // auf das Sample-Raster
      if (t <= ctx.currentTime + 0.02) continue;
      const src = new AudioBufferSourceNode(ctx, { buffer: click });
      src.connect(dest);
      src.start(t);
      scheduledUntil = s;
    }
  }, 250);
  const stream = dest.stream;
  stream.__stop = () => { clearInterval(timer); clearInterval(sampleTimer); ctx.close().catch(() => {}); };
  stream.__ctx = ctx;
  return stream;
}
