// Soundboard: hochladen, benennen, Art, Sendeplan-Zuordnung, Lautstärke, Ausblenden, Taste, Reihenfolge, Vorhören, Löschen.
import { $, api, button, clear, confirmDialog, errorText, fmtDec, fmtDuration, h, icon, toast } from "./common.js";

const root = $("#app");
const KINDS = [["intro", "Intro"], ["outro", "Outro"], ["segment", "Abschnitt"], ["sting", "Zwischenton"]];
// Zuordnung zum Sendeplan: Die Namen der Abschnittsarten kommen aus den Einstellungen (Sendeformat)
let SEGMENT_KINDS = [["", "Keine"], ["blitzlicht", "Kurzthema"], ["hauptthema", "Hauptthema"], ["good", "Empfehlung"]];
const KIND_NOTE = {
  intro: "Setzt die Kapitelmarke „Intro“",
  outro: "Setzt die Kapitelmarke „Outro“",
  segment: "Setzt eine Kapitelmarke mit dem Kapiteltitel, ohne Titel mit der Bezeichnung",
  sting: "Setzt keine Kapitelmarke, außer er schaltet den Sendeplan auf einen Abschnitt mit Titel weiter",
};

let sounds = [];
let serverHasSegmentKind = true;
const localSegmentKind = new Map(); // nur solange der Server das Feld noch nicht kennt
const audio = new Audio();
let playingId = null;

audio.addEventListener("ended", () => setPlaying(null));
audio.addEventListener("error", () => { if (playingId) { toast("Der Sound lässt sich nicht abspielen", { error: true }); setPlaying(null); } });

function setPlaying(sid) {
  playingId = sid;
  document.querySelectorAll("[data-play]").forEach((b) => {
    const on = b.dataset.play === sid;
    b.replaceChildren(icon(on ? "pause" : "play"), document.createTextNode(on ? "Stopp" : "Vorhören"));
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
  audio.play().then(() => setPlaying(s.id)).catch(() => toast("Der Sound lässt sich nicht abspielen", { error: true }));
}

async function loadSounds() {
  try {
    const l = (await api("/settings")).format.labels;
    SEGMENT_KINDS = [["", "Keine"], ["blitzlicht", l.blitzlicht], ["hauptthema", l.hauptthema], ["good", l.good]];
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
    const line = { text: `${file.name}: wird hochgeladen und geprüft`, cls: "" };
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
      line.text = `${file.name}: hinzugefügt`;
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
    h("p", { class: "m-drop-title" }, "Audiodateien hineinziehen oder wählen"),
    h("p", { class: "tr-small" }, "WAV, MP3, M4A, AAC, OGG, Opus, FLAC oder AIFF, höchstens 60 MB je Datei"),
    button("Dateien wählen", { icon: "upload", onclick: () => input.click() }),
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
  options.forEach(([v, t]) => sel.append(h("option", { value: v, selected: String(v) === String(value ?? "") }, t)));
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
    state.textContent = "Gespeichert";
    state.className = "m-save-state tr-status-ok";
    setTimeout(() => { if (state.textContent === "Gespeichert") state.textContent = ""; }, 2500);
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
  const kind = select(KINDS, s.kind, `${p}-kind`, "Art");
  const chapter = h("input", { class: "tr-input", type: "text", id: `${p}-chap`, value: s.chapter_title || "", maxlength: "120", placeholder: s.label });
  const note = h("p", { class: "tr-small m-hint" }, KIND_NOTE[s.kind]);
  const seg = select(SEGMENT_KINDS, s.segment_kind || "", `${p}-seg`, "Sendeplan");
  const gain = numberField("Lautstärke in dB", `${p}-gain`, s.gain_db, { step: "0.5", min: "-30", max: "12" });
  const fade = numberField("Ausblenden in ms", `${p}-fade`, s.fade_out_ms, { step: "50", min: "0", max: "10000" });
  const key = select([["", "Keine"], ...Array.from({ length: 9 }, (_, i) => [String(i + 1), String(i + 1)])], s.hotkey || "", `${p}-key`, "Taste");
  const syncChapter = () => { const seg_ = kind.querySelector("select").value === "segment"; chapter.disabled = !seg_; note.textContent = KIND_NOTE[kind.querySelector("select").value]; };

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

  const playBtn = button("Vorhören", { icon: "play", onclick: () => togglePlay(s) });
  playBtn.dataset.play = s.id;
  playBtn.setAttribute("aria-pressed", "false");
  const head = h("div", { class: "m-sound-head" },
    h("div", { class: "m-ep-text" },
      h("strong", { class: "m-ep-title" }, s.label),
      h("p", { class: "m-meta m-num" }, fmtDuration(s.duration_ms) || `${fmtDec(s.duration_ms / 1000)} s`, s.hotkey ? h("kbd", { class: "app-kbd", title: `Taste ${s.hotkey}`, "aria-label": `Taste ${s.hotkey}` }, s.hotkey) : null)),
    h("div", { class: "m-sound-actions" },
      playBtn,
      button("", { icon: "up", ariaLabel: `${s.label} nach oben`, title: "Nach oben", disabled: index === 0, onclick: () => move(index, -1) }),
      button("", { icon: "down", ariaLabel: `${s.label} nach unten`, title: "Nach unten", disabled: index === sounds.length - 1, onclick: () => move(index, 1) }),
      button("", { icon: "trash", ariaLabel: `${s.label} löschen`, title: "Löschen", onclick: () => remove(s) })));
  const li = h("li", { class: "m-sound" },
    head,
    h("div", { class: "m-sound-fields" },
      h("div", { class: "tr-field m-field m-span2" }, h("label", { class: "tr-label", for: `${p}-label` }, "Bezeichnung"), label),
      kind,
      h("div", { class: "tr-field m-field" }, h("label", { class: "tr-label", for: `${p}-chap` }, "Kapiteltitel"), chapter),
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
    moved?.closest("li")?.querySelector(`.m-sound-actions button[aria-label$="${d < 0 ? "oben" : "unten"}"]`)?.focus();
  } catch (e) {
    toast(errorText(e), { error: true });
  }
}

async function remove(s) {
  const ok = await confirmDialog({ title: "Sound löschen", text: `„${s.label}“ wird gelöscht. Wurde der Sound schon in einer Aufnahme benutzt, wird er nur ausgeblendet, damit die Folge neu gemischt werden kann.`, ok: "Löschen", danger: true });
  if (!ok) return;
  try {
    if (playingId === s.id) { audio.pause(); setPlaying(null); }
    const res = await api(`/sounds/${encodeURIComponent(s.id)}`, { method: "DELETE" });
    toast(res.archived ? "Sound ausgeblendet, weil Aufnahmen darauf verweisen" : "Sound gelöscht");
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
    h("p", { class: "m-lead" }, "Jingles und Töne für das Studio. Ein Klick im Studio spielt den Ton für alle hörbar ab, im fertigen Mix sitzt die Originaldatei an dieser Stelle. Die Tasten 1 bis 9 lösen Sounds aus."),
    h("section", { class: "m-section" }, h("h2", { class: "m-h2" }, "Hinzufügen"), dropzone()),
    h("section", { class: "m-section" },
      h("h2", { class: "m-h2" }, "Wie die Arten wirken"),
      h("dl", { class: "m-dl m-dl-plain" },
        ...KINDS.flatMap(([k, t]) => [h("div", null, h("dt", null, t), h("dd", null, KIND_NOTE[k]))]))),
    h("section", { class: "m-section" },
      h("h2", { class: "m-h2" }, `Sounds (${sounds.length})`),
      h("p", { id: "m-segnote", class: "tr-small m-hint", hidden: serverHasSegmentKind }, "Die Zuordnung zum Sendeplan wird vom Server noch nicht gespeichert und gilt nur bis zum Neuladen der Seite"),
      sounds.length
        ? h("ul", { class: "tr-list m-sounds" }, sounds.map((s, i) => row(s, i)))
        : h("p", { class: "tr-small" }, "Noch keine Sounds. Oben Dateien hinzufügen.")));
  setPlaying(playingId);
  drawLog();
}

loadSounds().catch((e) => {
  clear(root);
  root.append(h("p", { class: "tr-error", role: "alert" }, `Die Sounds konnten nicht geladen werden. ${errorText(e)}`));
});
