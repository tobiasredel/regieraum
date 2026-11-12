// Hörprobe im Gerätecheck: fünf Sekunden vom gewählten Mikrofon aufnehmen, danach abspielen.
// Rein lokal: MediaRecorder auf dem Mikrofon-Stream, den auch die Aufnahme nutzen würde (gleiche Einstellungen
// für Kopfhörer und Echounterdrückung). Nichts davon geht an den Server und nichts wird gespeichert.

const V = new URL(import.meta.url).search;
const { t } = await import(`../i18n.js${V}`);
const { h, icon } = await import(`./util.js${V}`);

export const PROBE_MS = 5000;

/** Gibt es in diesem Browser einen MediaRecorder für Audio? Sonst blendet der Gerätecheck die Probe aus. */
export function probeSupported() {
  return typeof MediaRecorder !== "undefined";
}

function pickType() {
  for (const m of ["audio/webm;codecs=opus", "audio/mp4", "audio/webm", "audio/ogg;codecs=opus"]) {
    try { if (MediaRecorder.isTypeSupported(m)) return m; } catch { /* nächster */ }
  }
  return "";
}

/**
 * @param {() => MediaStream | null} getStream  liefert den aktuell geöffneten Mikrofon-Stream (oder die Testquelle)
 * @returns {{ el: HTMLElement, enable: () => void, dispose: () => void }}
 */
export function createProbe(getStream, getSink = () => "") {
  let state = "idle"; // idle | recording | ready
  let rec = null, timer = 0, url = "", audio = null, enabled = false;

  const status = h("span", { class: "ps-probe-status ps-small", role: "status", "aria-live": "polite" });
  const start = h("button", { class: "tr-btn tr-btn-secondary", type: "button", id: "ps-probe-rec", disabled: true }, icon("mic"), t("Hörprobe"));
  const play = h("button", { class: "tr-btn tr-btn-secondary", type: "button", id: "ps-probe-play", hidden: true }, icon("play"), t("Abspielen"));
  const again = h("button", { class: "tr-btn tr-btn-secondary", type: "button", id: "ps-probe-again", hidden: true }, icon("refresh"), t("Noch einmal"));
  const el = h("div", { class: "ps-field ps-probe", id: "ps-probe", "data-state": "idle", hidden: !probeSupported() },
    h("span", { class: "tr-label" }, t("Hörprobe")),
    h("div", { class: "ps-inline ps-probe-row" }, start, play, again, status),
    h("small", {}, t("Fünf Sekunden sprechen und danach anhören. Darauf achten: Hall, Rauschen, Abstand zum Mikrofon")));

  function set(s, text = "") {
    state = s;
    el.dataset.state = s;
    status.textContent = text;
    start.hidden = s !== "idle";
    start.disabled = !enabled || s !== "idle";
    play.hidden = again.hidden = s !== "ready";
  }

  function release() {
    clearInterval(timer);
    if (audio) { audio.pause(); audio = null; }
    if (url) { URL.revokeObjectURL(url); url = ""; }
  }

  function begin() {
    const stream = getStream();
    if (!stream || !probeSupported()) return;
    release();
    const chunks = [];
    const type = pickType();
    try {
      // nur die Audiospur: ein Testsignal oder Mikrofon-Stream ohne Bild
      rec = new MediaRecorder(new MediaStream(stream.getAudioTracks()), type ? { mimeType: type } : undefined);
    } catch {
      el.hidden = true;
      return;
    }
    rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    rec.onstop = () => {
      clearInterval(timer);
      if (state !== "recording") return; // abgebrochen
      const blob = new Blob(chunks, { type: rec.mimeType || type || "audio/webm" });
      if (!blob.size) { set("idle", t("Es kam kein Ton an. Bitte noch einmal versuchen")); return; }
      url = URL.createObjectURL(blob);
      set("ready", t("Aufgenommen"));
    };
    const t0 = performance.now();
    set("recording");
    const tick = () => {
      const left = Math.max(0, Math.ceil((PROBE_MS - (performance.now() - t0)) / 1000));
      status.textContent = t("Aufnahme läuft, noch {s} s", { s: left });
    };
    tick();
    timer = setInterval(tick, 200);
    rec.start();
    setTimeout(() => { if (rec && rec.state === "recording") rec.stop(); }, PROBE_MS);
  }

  function playBack() {
    if (!url) return;
    if (audio) audio.pause();
    const a = audio = new Audio(url);
    a.id = "ps-probe-audio";
    el.dataset.played = "0";
    a.addEventListener("playing", () => { el.dataset.played = "1"; play.disabled = true; status.textContent = t("Wiedergabe läuft"); });
    a.addEventListener("timeupdate", () => { el.dataset.pos = a.currentTime.toFixed(2); });
    a.addEventListener("ended", () => { if (a !== audio) return; play.disabled = false; status.textContent = t("Aufgenommen"); });
    a.addEventListener("error", () => { if (a !== audio) return; play.disabled = false; status.textContent = t("Die Wiedergabe ist nicht möglich"); });
    const sink = getSink();
    Promise.resolve(sink && a.setSinkId ? a.setSinkId(sink).catch(() => {}) : null)
      .then(() => a.play())
      .catch(() => { play.disabled = false; status.textContent = t("Die Wiedergabe ist nicht möglich"); });
  }

  start.addEventListener("click", begin);
  again.addEventListener("click", () => { release(); set("idle"); begin(); });
  play.addEventListener("click", playBack);
  set("idle");

  return {
    el,
    enable() { enabled = true; if (state === "idle") start.disabled = false; },
    dispose() {
      if (rec && rec.state === "recording") { state = "idle"; try { rec.stop(); } catch { /* egal */ } }
      release();
    },
  };
}
