// Soundboard: hochladen, benennen, Art, Sendeplan-Zuordnung, Lautstärke, Ausblenden, Taste, Reihenfolge, Vorhören, Löschen.
import { $, api, button, clear, confirmDialog, errorText, fmtDec, fmtDuration, h, icon, t, toast } from "./common.js";

const root = $("#app");
const kinds = () => [["intro", t("Intro")], ["outro", t("Outro")], ["segment", t("Abschnitt")], ["sting", t("Zwischenton")]];
// Zuordnung zum Sendeplan: Die Namen der Abschnittsarten kommen aus den Einstellungen (Sendeformat)
let segmentLabels = null;
const segmentKinds = () => {
  const l = segmentLabels || { blitzlicht: t("Kurzthema"), hauptthema: t("Hauptthema"), good: t("Empfehlung") };
  return [["", t("Keine")], ["blitzlicht", l.blitzlicht], ["hauptthema", l.hauptthema], ["good", l.good]];
};
function kindNote(kind) {
  switch (kind) {
    case "intro": return t("Setzt die Kapitelmarke „Intro“");
    case "outro": return t("Setzt die Kapitelmarke „Outro“");
    case "segment": return t("Setzt eine Kapitelmarke mit dem Kapiteltitel, ohne Titel mit der Bezeichnung");
    case "sting": return t("Setzt keine Kapitelmarke, außer er schaltet den Sendeplan auf einen Abschnitt mit Titel weiter");
    default: return undefined;
  }
}

let sounds = [];
let serverHasSegmentKind = true;
const localSegmentKind = new Map(); // nur solange der Server das Feld noch nicht kennt
const audio = new Audio();
let playingId = null;

audio.addEventListener("ended", () => setPlaying(null));
audio.addEventListener("error", () => { if (playingId) { toast(t("Der Sound lässt sich nicht abspielen"), { error: true }); setPlaying(null); } });

function setPlaying(sid) {
  playingId = sid;
  document.querySelectorAll("[data-play]").forEach((b) => {
    const on = b.dataset.play === sid;
    b.replaceChildren(icon(on ? "pause" : "play"), document.createTextNode(on ? t("Stopp") : t("Vorhören")));
    b.setAttribute("aria-pressed", String(on));
  });
}

function togglePlay(s) {
  if (playingId === s.id) {
    audio.pause();
    setPlaying(null);
    return;
  }
  audio.pause();
  audio.src = `/api/host/sounds/${encodeURIComponent(s.id)}/audio`;
  audio.currentTime = 0;
  audio.play().then(() => setPlaying(s.id)).catch(() => toast(t("Der Sound lässt sich nicht abspielen"), { error: true }));
}

async function loadSounds() {
  try {
    const l = (await api("/settings")).format.labels;
    segmentLabels = { blitzlicht: l.blitzlicht, hauptthema: l.hauptthema, good: l.good };
  } catch { /* Standardnamen bleiben */ }
  sounds = await api("/sounds");
  serverHasSegmentKind = sounds.length === 0 || sounds.some((s) => "segment_kind" in s);
  for (const s of sounds) if (!("segment_kind" in s)) s.segment_kind = localSegmentKind.get(s.id) || "";
  render();
}

// --- Hochladen ---------------------------------------------------------------------------------
function guessKind(name) {
  const n = name.toLowerCase();
  if (n.includes("intro")) return "intro";
  if (n.includes("outro")) return "outro";
  return "sting";
}

const logLines = []; // Meldungen zu Uploads überstehen das Neuzeichnen der Liste

function drawLog() {
  const log = $("#m-upload-log");
  if (!log) return;
  clear(log);
  logLines.slice(-6).forEach((l) => log.append(h("li", { class: l.cls }, l.text)));
}

async function uploadFiles(files) {
  for (const file of files) {
    const line = { text: t("{name}: wird hochgeladen und geprüft", { name: file.name }), cls: "" };
    logLines.push(line);
    drawLog();
    const form = new FormData();
    form.append("file", file);
    form.append("label", file.name.replace(/\.[^.]+$/, "").slice(0, 80));
    form.append("kind", guessKind(file.name));
    form.append("gain_db", "0");
    form.append("fade_out_ms", "400");
    try {
      await api("/sounds", { method: "POST", form });
      line.text = t("{name}: hinzugefügt", { name: file.name });
      line.cls = "tr-status-ok";
    } catch (e) {
      line.text = `${file.name}: ${errorText(e)}`;
      line.cls = "tr-status-error";
    }
    drawLog();
  }
  await loadSounds().catch(() => {});
  drawLog();
}

function dropzone() {
  const input = h("input", { type: "file", accept: "audio/*,.wav,.mp3,.m4a,.aac,.ogg,.opus,.flac,.aif,.aiff", multiple: true, class: "app-vh", id: "m-file", tabindex: "-1" });
  const log = h("ul", { class: "m-plain m-upload-log", id: "m-upload-log", "aria-live": "polite" });
  const zone = h("div", { class: "m-drop" },
    h("p", { class: "m-drop-title" }, t("Audiodateien hineinziehen oder wählen")),
    h("p", { class: "tr-small" }, t("WAV, MP3, M4A, AAC, OGG, Opus, FLAC oder AIFF, höchstens 60 MB je Datei")),
    button(t("Dateien wählen"), { icon: "upload", onclick: () => input.click() }),
    input);
  input.addEventListener("change", () => { if (input.files.length) uploadFiles([...input.files]); input.value = ""; });
  ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("is-over"); }));
  ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("is-over"); }));
  zone.addEventListener("drop", (e) => { const f = [...(e.dataTransfer?.files || [])]; if (f.length) uploadFiles(f); });
  return h("div", null, zone, log);
}

// --- Liste -------------------------------------------------------------------------------------
function select(options, value, id, label) {
  const sel = h("select", { class: "tr-input m-select", id });
  options.forEach(([v, name]) => sel.append(h("option", { value: v, selected: String(v) === String(value ?? "") }, name)));
  return h("div", { class: "tr-field m-field" }, h("label", { class: "tr-label", for: id }, label), sel);
}

function numberField(label, id, value, { step, min, max }) {
  const input = h("input", { class: "tr-input", type: "number", id, value, step, min, max, inputmode: "decimal" });
  return h("div", { class: "tr-field m-field" }, h("label", { class: "tr-label", for: id }, label), input);
}

async function patch(s, body, state) {
  try {
    const res = await api(`/sounds/${encodeURIComponent(s.id)}`, { method: "PATCH", json: body });
    Object.assign(s, res);
    if ("segment_kind" in body) {
      if (!("segment_kind" in res)) {
        s.segment_kind = body.segment_kind;
        localSegmentKind.set(s.id, body.segment_kind);
        serverHasSegmentKind = false;
        noteSegmentKind();
      }
    }
    const saved = t("Gespeichert");
    state.textContent = saved;
    state.className = "m-save-state tr-status-ok";
    setTimeout(() => { if (state.textContent === saved) state.textContent = ""; }, 2500);
    if ("hotkey" in body) await loadSounds();
    return true;
  } catch (e) {
    state.textContent = errorText(e);
    state.className = "m-save-state tr-status-error";
    return false;
  }
}

function row(s, index) {
  const p = `s-${s.id}`;
  const state = h("span", { class: "m-save-state", role: "status" });
  const label = h("input", { class: "tr-input", type: "text", id: `${p}-label`, value: s.label, maxlength: "80" });
  const kind = select(kinds(), s.kind, `${p}-kind`, t("Art"));
  const chapter = h("input", { class: "tr-input", type: "text", id: `${p}-chap`, value: s.chapter_title || "", maxlength: "120", placeholder: s.label });
  const note = h("p", { class: "tr-small m-hint" }, kindNote(s.kind));
  const seg = select(segmentKinds(), s.segment_kind || "", `${p}-seg`, t("Sendeplan"));
  const gain = numberField(t("Lautstärke in dB"), `${p}-gain`, s.gain_db, { step: "0.5", min: "-30", max: "12" });
  const fade = numberField(t("Ausblenden in ms"), `${p}-fade`, s.fade_out_ms, { step: "50", min: "0", max: "10000" });
  const key = select([["", t("Keine")], ...Array.from({ length: 9 }, (_, i) => [String(i + 1), String(i + 1)])], s.hotkey || "", `${p}-key`, t("Taste"));
  const syncChapter = () => { const seg_ = kind.querySelector("select").value === "segment"; chapter.disabled = !seg_; note.textContent = kindNote(kind.querySelector("select").value); };

  label.addEventListener("change", () => {
    const v = label.value.trim();
    if (!v) { label.value = s.label; return; }
    if (v !== s.label) patch(s, { label: v }, state).then(() => { head.querySelector("strong").textContent = s.label; chapter.placeholder = s.label; });
  });
  kind.querySelector("select").addEventListener("change", (e) => { syncChapter(); patch(s, { kind: e.target.value }, state); });
  chapter.addEventListener("change", () => patch(s, { chapter_title: chapter.value.trim() }, state));
  seg.querySelector("select").addEventListener("change", (e) => patch(s, { segment_kind: e.target.value || null }, state));
  gain.querySelector("input").addEventListener("change", (e) => {
    const v = parseFloat(e.target.value);
    if (Number.isNaN(v)) { e.target.value = s.gain_db; return; }
    patch(s, { gain_db: Math.max(-30, Math.min(12, v)) }, state);
  });
  fade.querySelector("input").addEventListener("change", (e) => {
    const v = parseInt(e.target.value, 10);
    if (Number.isNaN(v)) { e.target.value = s.fade_out_ms; return; }
    patch(s, { fade_out_ms: Math.max(0, Math.min(10000, v)) }, state);
  });
  key.querySelector("select").addEventListener("change", (e) => patch(s, { hotkey: e.target.value }, state));

  const playBtn = button(t("Vorhören"), { icon: "play", onclick: () => togglePlay(s) });
  playBtn.dataset.play = s.id;
  playBtn.setAttribute("aria-pressed", "false");
  const head = h("div", { class: "m-sound-head" },
    h("div", { class: "m-ep-text" },
      h("strong", { class: "m-ep-title" }, s.label),
      h("p", { class: "m-meta m-num" }, fmtDuration(s.duration_ms) || t("{s} s", { s: fmtDec(s.duration_ms / 1000) }), s.hotkey ? h("kbd", { class: "app-kbd", title: t("Taste {key}", { key: s.hotkey }), "aria-label": t("Taste {key}", { key: s.hotkey }) }, s.hotkey) : null)),
    h("div", { class: "m-sound-actions" },
      playBtn,
      button("", { icon: "up", ariaLabel: t("{label} nach oben", { label: s.label }), title: t("Nach oben"), disabled: index === 0, onclick: () => move(index, -1) }),
      button("", { icon: "down", ariaLabel: t("{label} nach unten", { label: s.label }), title: t("Nach unten"), disabled: index === sounds.length - 1, onclick: () => move(index, 1) }),
      button("", { icon: "trash", ariaLabel: t("{label} löschen", { label: s.label }), title: t("Löschen"), onclick: () => remove(s) })));
  const li = h("li", { class: "m-sound" },
    head,
    h("div", { class: "m-sound-fields" },
      h("div", { class: "tr-field m-field m-span2" }, h("label", { class: "tr-label", for: `${p}-label` }, t("Bezeichnung")), label),
      kind,
      h("div", { class: "tr-field m-field" }, h("label", { class: "tr-label", for: `${p}-chap` }, t("Kapiteltitel")), chapter),
      seg, gain, fade, key),
    h("div", { class: "m-sound-foot" }, note, state));
  syncChapter();
  return li;
}

async function move(i, d) {
  const ids = sounds.map((s) => s.id);
  const j = i + d;
  if (j < 0 || j >= ids.length) return;
  [ids[i], ids[j]] = [ids[j], ids[i]];
  try {
    const res = await api("/sounds/order", { method: "POST", json: { ids } });
    const old = new Map(sounds.map((s) => [s.id, s]));
    sounds = res.map((s) => ({ ...s, segment_kind: "segment_kind" in s ? s.segment_kind : old.get(s.id)?.segment_kind || "" }));
    render();
    const moved = document.querySelector(`#s-${CSS.escape(ids[j])}-label`);
    moved?.closest("li")?.querySelector(`.m-sound-actions button:nth-child(${d < 0 ? 2 : 3})`)?.focus();
  } catch (e) {
    toast(errorText(e), { error: true });
  }
}

async function remove(s) {
  const ok = await confirmDialog({ title: t("Sound löschen"), text: t("„{label}“ wird gelöscht. Wurde der Sound schon in einer Aufnahme benutzt, wird er nur ausgeblendet, damit die Folge neu gemischt werden kann.", { label: s.label }), ok: t("Löschen"), danger: true });
  if (!ok) return;
  try {
    if (playingId === s.id) { audio.pause(); setPlaying(null); }
    const res = await api(`/sounds/${encodeURIComponent(s.id)}`, { method: "DELETE" });
    toast(res.archived ? t("Sound ausgeblendet, weil Aufnahmen darauf verweisen") : t("Sound gelöscht"));
    await loadSounds();
  } catch (e) {
    toast(errorText(e), { error: true });
  }
}

function noteSegmentKind() {
  const n = $("#m-segnote");
  if (n) n.hidden = serverHasSegmentKind;
}

function render() {
  clear(root);
  root.append(
    h("p", { class: "m-lead" }, t("Jingles und Töne für das Studio. Ein Klick im Studio spielt den Ton für alle hörbar ab, im fertigen Mix sitzt die Originaldatei an dieser Stelle. Die Tasten 1 bis 9 lösen Sounds aus.")),
    h("section", { class: "m-section" }, h("h2", { class: "m-h2" }, t("Hinzufügen")), dropzone()),
    h("section", { class: "m-section" },
      h("h2", { class: "m-h2" }, t("Wie die Arten wirken")),
      h("dl", { class: "m-dl m-dl-plain" },
        ...kinds().flatMap(([k, name]) => [h("div", null, h("dt", null, name), h("dd", null, kindNote(k)))]))),
    h("section", { class: "m-section" },
      h("h2", { class: "m-h2" }, t("Sounds ({n})", { n: sounds.length })),
      h("p", { id: "m-segnote", class: "tr-small m-hint", hidden: serverHasSegmentKind }, t("Die Zuordnung zum Sendeplan wird vom Server noch nicht gespeichert und gilt nur bis zum Neuladen der Seite")),
      sounds.length
        ? h("ul", { class: "tr-list m-sounds" }, sounds.map((s, i) => row(s, i)))
        : h("p", { class: "tr-small" }, t("Noch keine Sounds. Oben Dateien hinzufügen."))));
  setPlaying(playingId);
  drawLog();
}

loadSounds().catch((e) => {
  clear(root);
  root.append(h("p", { class: "tr-error", role: "alert" }, t("Die Sounds konnten nicht geladen werden. {message}", { message: errorText(e) })));
});
