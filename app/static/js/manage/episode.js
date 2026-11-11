// Folge prüfen: Hören, Schneiden, Angaben, Klangbericht, Transkript, Rohspuren, Veröffentlichen.
import {
  $, api, button, clear, confirmDialog, errorText, field, fmtClock, fmtDate, fmtDateTime, fmtDuration, h, icon, poll,
  progressBar, rich, statusClass, statusText, t, toast,
} from "./common.js";
import { CutEditor } from "./waveform.js";
import { ShownotesEditor, sanitizeHtml } from "./shownotes.js";
import { renderReport } from "./report.js";

const root = $("#app");
const id = root.dataset.episode;
const state = { ep: null, sounds: [], settings: null, editor: null, notes: null, stopWatch: null, sig: "", meta: null, base: null, banner: null, ui: {} };

const isBusy = (ep) => ep.status === "processing" || ep.status === "uploading" || (ep.jobs || []).some((j) => j.status === "queued" || j.status === "running");
const signature = (ep) => `${ep.status}|${JSON.stringify(ep.files)}|${ep.source}`;
const runningJob = (ep) => (ep.jobs || []).find((j) => j.status === "running") || (ep.jobs || []).find((j) => j.status === "queued");

// --- Laden -----------------------------------------------------------------------------------
async function start() {
  try {
    state.ep = await api(`/episodes/${encodeURIComponent(id)}`);
  } catch (e) {
    clear(root);
    root.append(
      h("p", { class: "tr-eyebrow mit-punkt" }, t("Sendung")),
      h("h1", { class: "m-h1" }, e.status === 404 ? t("Folge nicht gefunden") : t("Folge konnte nicht geladen werden")),
      h("p", { class: "tr-error", role: "alert" }, errorText(e)),
      h("p", null, h("a", { href: "/episodes" }, t("Zur Folgenliste"))));
    return;
  }
  state.sounds = await api("/sounds").catch(() => []);
  state.settings = await api("/settings").catch(() => null);
  state.sig = signature(state.ep);
  render();
  watch();
}

function watch() {
  state.stopWatch?.();
  state.stopWatch = null;
  if (!isBusy(state.ep)) return;
  state.stopWatch = poll(async () => {
    const next = await api(`/episodes/${encodeURIComponent(id)}`);
    const aiChanged = JSON.stringify(next.ai) !== JSON.stringify(state.ep.ai);
    state.ep = next;
    const sig = signature(next);
    if (sig !== state.sig) {
      state.sig = sig;
      render();
    } else {
      renderBanner();
      if (aiChanged) renderSuggestions();
      updatePublishJob();
    }
    return isBusy(next);
  }, 2500);
}

window.addEventListener("beforeunload", (e) => {
  if (!state.leaving && dirty()) {
    e.preventDefault();
    e.returnValue = "";
  }
});

// --- Rahmen ----------------------------------------------------------------------------------
function section(key, title, ...children) {
  return h("section", { class: "m-section", id: key, "aria-labelledby": `${key}-h` }, h("h2", { class: "m-h2", id: `${key}-h` }, title), ...children);
}

function render() {
  state.editor?.destroy();
  state.editor = null;
  state.meta = null;
  const ep = state.ep;
  clear(root);
  root.append(header(ep));
  state.banner = h("div", { class: "m-banner-slot" });
  root.append(state.banner);
  renderBanner();

  const reviewable = ["review", "scheduled", "published"].includes(ep.status);
  const nav = [];
  if (ep.audio_key || ep.files.episode) {
    root.append(listenSection(ep));
  }
  if (reviewable && ep.source === "recorded" && ep.files.mix && ep.files.peaks) {
    root.append(editorSection(ep));
    nav.push(["schnitt", t("Schnitt")]);
  }
  if (reviewable) {
    root.append(detailsSection(ep));
    nav.push(["angaben", t("Angaben")]);
  }
  if (ep.source === "recorded" && ep.report) {
    root.append(section("klang", t("Klangbericht"), renderReport(ep.report, state.settings?.audio?.target_lufs ?? null)));
    nav.push(["klang", t("Klangbericht")]);
  }
  if (ep.files.transcript) {
    root.append(transcriptSection(ep));
    nav.push(["transkript", t("Transkript")]);
  }
  if (ep.tracks.length) {
    root.append(tracksSection(ep));
    nav.push(["spuren", t("Rohspuren")]);
  }
  if (reviewable) {
    root.append(publishSection(ep));
    nav.push(["veroeffentlichen", t("Veröffentlichen")]);
  }
  if (ep.source === "recorded" && ["uploading", "processing", "review", "failed"].includes(ep.status)) root.append(discardSection(ep));
  if (["draft", "review", "failed"].includes(ep.status)) root.append(deleteSection(ep));
  if (nav.length) {
    const links = h("nav", { class: "m-toc", "aria-label": t("Auf dieser Seite") });
    nav.forEach(([k, label]) => links.append(h("a", { href: `#${k}` }, label)));
    root.querySelector(".m-head").after(links);
  }
  updateDirty();
}

function header(ep) {
  const bits = [h("span", { class: statusClass(ep.status) }, statusText(ep.status))];
  if (ep.duration_ms) bits.push(h("span", { class: "m-num" }, fmtDuration(ep.duration_ms)));
  if (ep.status === "published" && ep.published_at) bits.push(h("span", { class: "m-num" }, t("veröffentlicht {date}", { date: fmtDate(ep.published_at) })));
  else if (ep.status === "scheduled" && ep.publish_at) bits.push(h("span", { class: "m-num" }, t("geplant für {date}", { date: fmtDateTime(ep.publish_at) })));
  else if (ep.rec_start_server_ms) bits.push(h("span", { class: "m-num" }, t("aufgenommen {date}", { date: fmtDate(ep.rec_start_server_ms) })));
  const meta = h("p", { class: "m-meta" });
  bits.forEach((b, i) => {
    if (i) meta.append(h("span", { class: "m-sep", "aria-hidden": "true" }, "·"));
    meta.append(b);
  });
  if (ep.source === "imported") meta.append(h("span", { class: "m-sep", "aria-hidden": "true" }, "·"), h("span", null, t("importiert")));
  return h("header", { class: "m-head" },
    h("p", { class: "m-crumb" }, h("a", { href: "/episodes" }, t("Alle Folgen"))),
    h("p", { class: "tr-eyebrow mit-punkt" }, ep.number != null ? (ep.season ? t("Folge {n}, Staffel {season}", { n: ep.number, season: ep.season }) : t("Folge {n}", { n: ep.number })) : t("Folge ohne Nummer")),
    h("h1", { class: "m-h1", id: "m-title" }, ep.title || t("Ohne Titel")),
    meta);
}

// --- Zustände ---------------------------------------------------------------------------------
function renderBanner() {
  const ep = state.ep;
  const slot = state.banner;
  if (!slot) return;
  clear(slot);
  const job = runningJob(ep);
  const box = (cls, ...c) => slot.append(h("div", { class: `m-banner ${cls}`, role: cls.includes("error") ? "alert" : "status" }, ...c));
  if (ep.status === "draft" || ep.status === "recording") {
    box("", h("p", null, ep.status === "recording" ? t("Die Aufnahme läuft gerade im Studio. Prüfen und Schneiden folgen danach.") : t("Diese Folge ist ein Entwurf und noch nicht aufgenommen")),
      h("p", null, h("a", { href: "/studio" }, t("Zum Studio"))));
  } else if (ep.status === "uploading") {
    const lines = ep.tracks.map((tk) => {
      const pct = tk.total_frames ? Math.min(100, Math.round((tk.uploaded_frames / tk.total_frames) * 100)) : null;
      const progress = pct !== null ? t("{pct} % hochgeladen", { pct }) : t("{n} Teile erhalten", { n: tk.chunks });
      const name = tk.participant_name;
      return h("li", null, ["complete", "assembled"].includes(tk.status) ? t("{name}: {progress}, vollständig", { name, progress }) : t("{name}: {progress}", { name, progress }));
    });
    box("", h("p", { class: "m-banner-title" }, t("Die Aufnahme wird noch hochgeladen")),
      h("p", null, t("Die Browser der Teilnehmer laden die Spuren hoch. Die Verarbeitung startet von selbst, sobald alles da ist.")),
      lines.length ? h("ul", { class: "m-plain" }, lines) : null,
      h("div", { class: "app-row" }, button(t("Mit dem Vorhandenen verarbeiten"), { onclick: processNow })),
      h("p", { class: "tr-small" }, t("Fehlende Teile bleiben dabei als Stille in der Folge")));
  } else if (ep.status === "processing") {
    box("", h("p", { class: "m-banner-title" }, job?.kind === "remix" ? t("Die Folge wird neu gemischt") : job?.kind === "publish" ? t("Die Folge wird veröffentlicht") : t("Die Folge wird verarbeitet")),
      job ? h("p", { class: "tr-small" }, jobLine(job)) : null,
      job && job.status === "running" ? progressBar(job.progress, t("Verarbeitung, Fortschritt")) : null,
      h("p", { class: "tr-small" }, t("Die Seite aktualisiert sich von selbst")));
  } else if (ep.status === "failed") {
    const failed = (ep.jobs || []).find((j) => j.status === "failed");
    box("m-banner-error", h("p", { class: "m-banner-title tr-status-error" }, t("Die Verarbeitung ist fehlgeschlagen")),
      h("p", null, failed?.message || t("Eine Meldung liegt nicht vor")),
      h("div", { class: "app-row" }, button(t("Erneut verarbeiten"), { icon: "refresh", onclick: processNow })));
  } else if (job && (job.kind === "suggest" || job.kind === "publish" || job.kind === "unpublish")) {
    box("", h("p", { class: "tr-small" }, withPct(job.kind === "suggest" ? t("Vorschlag wird erzeugt") : job.kind === "publish" ? t("Wird veröffentlicht") : t("Wird zurückgezogen"), job)));
  }
}

/** Text plus Prozentangabe, solange der Auftrag läuft. */
function withPct(text, job) {
  return job.status === "running" ? t("{state}, {pct} %", { state: text, pct: Math.round((job.progress || 0) * 100) }) : text;
}

function jobLine(job) {
  return withPct(job.status === "queued" ? t("Wartet auf den Start") : job.message || t("Läuft"), job);
}

async function processNow() {
  const ep = state.ep;
  if (ep.status === "uploading") {
    const ok = await confirmDialog({ title: t("Mit dem Vorhandenen verarbeiten"), text: t("Noch fehlende Teile der Aufnahme werden durch Stille ersetzt. Das lässt sich später nur über eine erneute Verarbeitung ändern."), ok: t("Verarbeiten") });
    if (!ok) return;
  }
  try {
    await api(`/episodes/${encodeURIComponent(id)}/process`, { method: "POST" });
    await reload();
  } catch (e) {
    toast(errorText(e), { error: true });
  }
}

async function reload() {
  state.ep = await api(`/episodes/${encodeURIComponent(id)}`);
  state.sig = signature(state.ep);
  render();
  watch();
}

// --- Fertige Folge ----------------------------------------------------------------------------
function listenSection(ep) {
  const audio = h("audio", { class: "m-audio", controls: true, preload: "metadata", src: `/api/host/episodes/${encodeURIComponent(id)}/audio/episode?v=${Date.now()}` });
  return section("hoeren", t("Fertige Folge"),
    audio,
    ep.source === "recorded" && ep.status === "review" ? h("p", { class: "tr-small" }, t("Zeigt den Stand der letzten Mischung. Schnitte wirken hier erst nach dem Neumischen.")) : null);
}

// --- Schnitt ----------------------------------------------------------------------------------
function pausesOf(ep) {
  const start = ep.rec_start_server_ms;
  const out = [];
  let open = null;
  for (const e of ep.events) {
    if (e.kind === "rec.pause" && open === null) open = e.server_ms;
    else if (e.kind === "rec.resume" && open !== null) {
      out.push({ from_ms: open - start, to_ms: e.server_ms - start });
      open = null;
    }
  }
  if (open !== null && ep.rec_stop_server_ms) out.push({ from_ms: open - start, to_ms: ep.rec_stop_server_ms - start });
  return out;
}

function editorLines(ep) {
  const start = ep.rec_start_server_ms;
  const lines = [];
  const chapterIds = new Set();
  for (const c of ep.chapters_preview || []) {
    chapterIds.add(c.event_id);
    lines.push({ ms: c.session_ms, label: c.title, kind: "chapter" });
  }
  const byId = Object.fromEntries(state.sounds.map((s) => [s.id, s]));
  for (const e of ep.events) {
    if (e.kind === "sound.play" && !chapterIds.has(e.id)) {
      lines.push({ ms: e.server_ms - start, label: byId[e.sound_id]?.label || t("Sound"), kind: "sound" });
    }
  }
  return lines;
}

function editorSection(ep) {
  const host = h("div", { class: "m-ed-host" }, h("p", { class: "tr-small" }, t("Wellenform wird geladen")));
  const status = h("p", { class: "m-save-state", role: "status" });
  state.ui.edlStatus = status;
  const bSave = button(t("Schnitt speichern"), { icon: null, onclick: () => saveEdl().catch(() => {}) });
  const bMix = button(t("Neu mischen"), { icon: "refresh", onclick: remix, disabled: ep.status !== "review" });
  state.ui.bSave = bSave;
  const sec = section("schnitt", t("Schnitt"),
    h("p", { class: "m-lead" }, t("Die Ansicht zeigt die ungeschnittene Mischung. Pausen sind schon als Schnitte enthalten. Zeiten in der Ansicht sind Zeiten der Aufnahme, die Länge der fertigen Folge steht darunter.")),
    host,
    h("div", { class: "app-row m-ed-save" }, bSave, bMix, status),
    h("p", { class: "tr-small" }, t("Gespeicherte Schnitte und Kapitelnamen wirken in der fertigen Folge erst nach dem Neumischen. Das dauert einige Minuten.")));
  api(`/episodes/${encodeURIComponent(id)}/peaks`).then((peaks) => {
    if (!peaks || !Array.isArray(peaks.peaks) || !peaks.peaks.length) throw new Error(t("Die Wellenform ist leer"));
    clear(host);
    state.editor = new CutEditor({
      audioUrl: `/api/host/episodes/${encodeURIComponent(id)}/audio/mix`,
      peaks, edl: ep.edl, lines: editorLines(ep), pauses: pausesOf(ep),
      onChange: () => updateDirty(),
    });
    host.append(state.editor.el);
    updateDirty();
  }).catch((e) => {
    clear(host);
    host.append(h("p", { class: "tr-error", role: "alert" }, t("Die Wellenform konnte nicht geladen werden. {message}", { message: errorText(e) })));
  });
  return sec;
}

async function saveEdl() {
  const ed = state.editor;
  if (!ed) return;
  try {
    const res = await api(`/episodes/${encodeURIComponent(id)}`, { method: "PATCH", json: { edl: ed.getEdl() } });
    state.ep = { ...state.ep, ...res };
    ed.setSaved(res.edl);
    toast(t("Schnitt gespeichert"));
  } catch (e) {
    toast(t("Schnitt nicht gespeichert. {message}", { message: errorText(e) }), { error: true });
    throw e;
  }
}

async function remix() {
  const ok = await confirmDialog({ title: t("Neu mischen"), text: t("Die Folge wird mit den gespeicherten Schnitten und Kapitelnamen neu gemischt und kodiert. Währenddessen ist die Prüfung gesperrt."), ok: t("Neu mischen") });
  if (!ok) return;
  try {
    if (dirty()) await saveAll();
    await api(`/episodes/${encodeURIComponent(id)}/remix`, { method: "POST" });
    await reload();
  } catch (e) {
    toast(errorText(e), { error: true });
  }
}

// --- Angaben ----------------------------------------------------------------------------------
function detailsSection(ep) {
  const m = (state.meta = {});
  m.title = h("input", { class: "tr-input", type: "text", value: ep.title || "", maxlength: "300", autocomplete: "off" });
  m.number = h("input", { class: "tr-input", type: "number", min: "0", inputmode: "numeric", value: ep.number ?? "" });
  m.season = h("input", { class: "tr-input", type: "number", min: "0", inputmode: "numeric", value: ep.season ?? "" });
  m.summary = h("textarea", { class: "tr-input", rows: 3, maxlength: "4000" });
  m.summary.value = ep.summary || "";
  m.explicit = h("input", { type: "checkbox", checked: ep.explicit });
  m.notes = new ShownotesEditor({ value: ep.description_html || "", onChange: updateDirty });
  m.chapters = {};
  state.base = {
    title: ep.title || "", number: ep.number ?? null, season: ep.season ?? null, summary: ep.summary || "", explicit: !!ep.explicit,
    description_html: sanitizeHtml(ep.description_html || ""),
    chapters: Object.fromEntries((ep.chapters_preview || []).map((c) => [c.event_id, c.title])),
  };
  [m.title, m.number, m.season, m.summary, m.explicit].forEach((el) => el.addEventListener("input", () => { updateDirty(); if (el === m.title) $("#m-title").textContent = el.value.trim() || t("Ohne Titel"); }));
  state.ui.titleSuggest = h("div", { class: "m-suggest" });
  state.ui.summarySuggest = h("div", { class: "m-suggest" });
  state.ui.notesSuggest = h("div", { class: "m-suggest" });
  state.ui.numberError = h("p", { class: "tr-error", role: "alert", hidden: true });
  state.ui.metaState = h("p", { class: "m-save-state", role: "status" });
  state.ui.bSuggest = button(t("Vorschlag neu erzeugen"), { icon: "sparkles", onclick: suggestAgain });

  const chapters = h("div", { class: "m-chapters" });
  const list = ep.chapters_preview || [];
  if (list.length) {
    const ul = h("ul", { class: "m-chap-list" });
    const kindOf = (kind) => (kind === "intro" ? t("Intro") : kind === "outro" ? t("Outro") : kind === "segment" ? t("Abschnitt") : kind === "marker" ? t("Marker") : "");
    list.forEach((c, i) => {
      const input = h("input", { class: "tr-input", type: "text", value: c.title, maxlength: "200", "aria-label": t("Name von Kapitel {n}, {kind}, bei {time}", { n: i + 1, kind: kindOf(c.kind), time: fmtClock(c.start_ms) }) });
      input.addEventListener("input", updateDirty);
      m.chapters[c.event_id] = input;
      ul.append(h("li", null, h("span", { class: "m-num m-chap-time" }, fmtClock(c.start_ms)), input, h("span", { class: "tr-small m-chap-kind" }, kindOf(c.kind))));
    });
    chapters.append(ul, h("p", { class: "tr-small" }, t("Zeiten in der Folge nach dem Schnitt. Geänderte Kapitelnamen wirken nach dem Neumischen.")));
  } else {
    chapters.append(h("p", { class: "tr-small" }, t("Es gibt keine Kapitel. Kapitel entstehen aus Intro-, Outro- und Abschnitts-Jingles und gesetzten Markern.")));
  }

  const sec = section("angaben", t("Angaben"),
    h("div", { class: "app-row m-section-actions" }, state.ui.bSuggest),
    field(t("Titel"), m.title),
    state.ui.titleSuggest,
    h("div", { class: "m-grid3" },
      field(t("Folgennummer"), m.number, { id: "m-number" }),
      field(t("Staffel"), m.season, { hint: t("Leer lassen, wenn es keine Staffeln gibt") }),
      h("div", { class: "tr-field m-field" }, h("span", { class: "tr-label" }, t("Inhalt")), h("label", { class: "m-check", for: "m-explicit" }, (m.explicit.id = "m-explicit", m.explicit), h("span", null, t("Explizite Inhalte"))))),
    state.ui.numberError,
    field(t("Kurzbeschreibung"), m.summary),
    state.ui.summarySuggest,
    h("div", { class: "tr-field m-field" }, h("span", { class: "tr-label", id: "m-notes-label" }, t("Shownotes")), m.notes.el),
    state.ui.notesSuggest,
    h("h3", { class: "m-h3 m-h3-gap" }, t("Kapitel")), chapters,
    h("div", { class: "app-row m-ed-save" }, button(t("Angaben speichern"), { onclick: () => saveMeta().catch(() => {}) }), state.ui.metaState));
  renderSuggestions();
  return sec;
}

function renderSuggestions() {
  const ai = state.ep.ai || {};
  const m = state.meta;
  if (!m) return;
  clear(state.ui.titleSuggest);
  if ((ai.titles || []).length) {
    state.ui.titleSuggest.append(h("p", { class: "tr-small" }, t("Vorschläge für den Titel")));
    const ul = h("ul", { class: "m-chips" });
    ai.titles.forEach((title) => ul.append(h("li", null, h("button", { type: "button", class: "m-chip", onclick: () => { m.title.value = title; m.title.dispatchEvent(new Event("input")); m.title.focus(); } }, title))));
    state.ui.titleSuggest.append(ul);
  }
  clear(state.ui.summarySuggest);
  if (ai.summary && ai.summary !== m.summary.value) {
    state.ui.summarySuggest.append(
      h("p", { class: "tr-small" }, t("Vorschlag")), h("p", { class: "m-suggest-text" }, ai.summary),
      h("div", { class: "app-row" }, button(t("Vorschlag übernehmen"), { onclick: () => { m.summary.value = ai.summary; updateDirty(); renderSuggestions(); } })));
  }
  clear(state.ui.notesSuggest);
  if (ai.description_html && sanitizeHtml(ai.description_html) !== m.notes.getHtml()) {
    const box = h("div", { class: "m-rendered" });
    box.innerHTML = sanitizeHtml(ai.description_html); // bereinigt auf p, br, a, ul, ol, li, strong, em
    box.querySelectorAll("a").forEach((a) => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
    state.ui.notesSuggest.append(
      h("p", { class: "tr-small" }, t("Vorschlag für die Shownotes")), box,
      h("div", { class: "app-row" }, button(t("Vorschlag übernehmen"), { onclick: () => { m.notes.setHtml(ai.description_html); updateDirty(); renderSuggestions(); } })));
  }
  if (!ai.titles && !ai.summary && !ai.description_html) {
    state.ui.titleSuggest.append(h("p", { class: "tr-small" }, t("Noch kein Vorschlag vorhanden. „Vorschlag neu erzeugen“ erstellt Titel, Kurzbeschreibung und Shownotes aus dem Transkript.")));
  }
}

async function suggestAgain() {
  const b = state.ui.bSuggest;
  b.disabled = true;
  try {
    await api(`/episodes/${encodeURIComponent(id)}/suggest`, { method: "POST" });
    toast(t("Vorschlag wird erzeugt"));
    state.ep = await api(`/episodes/${encodeURIComponent(id)}`);
    watch();
    renderBanner();
    // Auch wenn der Job schon fertig ist: einmal nachladen
    setTimeout(async () => {
      try {
        const next = await api(`/episodes/${encodeURIComponent(id)}`);
        if (JSON.stringify(next.ai) !== JSON.stringify(state.ep.ai)) { state.ep = next; renderSuggestions(); }
      } catch { /* Polling übernimmt */ }
    }, 4000);
  } catch (e) {
    toast(errorText(e), { error: true });
  } finally {
    b.disabled = false;
  }
}

function metaPatch() {
  const m = state.meta;
  if (!m) return {};
  const b = state.base;
  const p = {};
  const title = m.title.value.trim();
  if (title !== b.title) p.title = title;
  for (const k of ["number", "season"]) {
    const v = m[k].value.trim() === "" ? null : parseInt(m[k].value, 10);
    if (v !== b[k] && !(Number.isNaN(v))) p[k] = v;
  }
  if (m.summary.value.trim() !== b.summary) p.summary = m.summary.value.trim();
  if (m.explicit.checked !== b.explicit) p.explicit = m.explicit.checked;
  const html = m.notes.getHtml();
  if (html !== b.description_html) p.description_html = html;
  const ct = {};
  for (const [eid, input] of Object.entries(m.chapters)) if (input.value.trim() !== b.chapters[eid]) ct[eid] = input.value.trim();
  if (Object.keys(ct).length) p.chapter_titles = ct;
  return p;
}

function dirty() {
  return !!(state.editor?.isDirty() || Object.keys(metaPatch()).length);
}

function updateDirty() {
  const ed = state.editor?.isDirty();
  const ms = state.ui.edlStatus;
  if (ms) {
    ms.textContent = ed ? t("Ungespeicherte Änderungen am Schnitt") : "";
    ms.className = `m-save-state${ed ? " tr-status-warn" : ""}`;
  }
  const mm = state.ui.metaState;
  if (mm && state.meta) {
    const d = Object.keys(metaPatch()).length > 0;
    mm.textContent = d ? t("Ungespeicherte Änderungen an den Angaben") : "";
    mm.className = `m-save-state${d ? " tr-status-warn" : ""}`;
  }
}

async function saveMeta() {
  const patch = metaPatch();
  if (!Object.keys(patch).length) return;
  state.ui.numberError.hidden = true;
  try {
    const res = await api(`/episodes/${encodeURIComponent(id)}`, { method: "PATCH", json: patch });
    state.ep = res;
    const b = state.base;
    for (const k of ["title", "number", "season", "summary", "explicit"]) if (k in patch) b[k] = patch[k];
    if ("description_html" in patch) b.description_html = patch.description_html;
    if (patch.chapter_titles) for (const [eid, title] of Object.entries(patch.chapter_titles)) b.chapters[eid] = title || (res.chapters_preview.find((c) => c.event_id === eid)?.title ?? "");
    for (const c of res.chapters_preview || []) if (state.meta.chapters[c.event_id]) { state.meta.chapters[c.event_id].value = c.title; b.chapters[c.event_id] = c.title; }
    $("#m-title").textContent = res.title || t("Ohne Titel");
    updateDirty();
    renderSuggestions();
    toast(t("Angaben gespeichert"));
  } catch (e) {
    if (e.code === "number_taken") {
      state.ui.numberError.textContent = e.message;
      state.ui.numberError.hidden = false;
      state.meta.number.focus();
    }
    toast(t("Nicht gespeichert. {message}", { message: errorText(e) }), { error: true });
    throw e;
  }
}

async function saveAll() {
  if (state.editor?.isDirty()) await saveEdl();
  await saveMeta();
}

// --- Transkript -------------------------------------------------------------------------------
function transcriptSection(ep) {
  const body = h("div", { class: "m-tr-body" });
  const search = h("input", { class: "tr-input", type: "search", placeholder: t("Im Transkript suchen"), "aria-label": t("Im Transkript suchen") });
  let segs = null;
  const draw = () => {
    clear(body);
    const q = search.value.trim().toLowerCase();
    const list = h("ol", { class: "m-tr-list" });
    let shown = 0;
    for (const s of segs) {
      if (q && !s.text.toLowerCase().includes(q) && !(s.speaker || "").toLowerCase().includes(q)) continue;
      shown++;
      const jump = state.editor
        ? h("button", { type: "button", class: "m-linkbtn m-num", title: t("In der Schnittansicht zu dieser Stelle springen"), onclick: () => jumpTo(s.start_ms) }, fmtClock(s.start_ms))
        : h("span", { class: "m-num tr-small" }, fmtClock(s.start_ms));
      list.append(h("li", null, jump, h("span", { class: "m-tr-speaker" }, s.speaker || ""), h("span", { class: "m-tr-text" }, s.text)));
    }
    body.append(shown ? list : h("p", { class: "tr-small" }, t("Keine Treffer")));
  };
  search.addEventListener("input", draw);
  const det = h("details", { class: "m-details" }, h("summary", null, t("Transkript anzeigen")));
  det.append(h("div", { class: "m-tr-tools" }, search), body);
  det.addEventListener("toggle", async () => {
    if (!det.open || segs) return;
    body.append(h("p", { class: "tr-small" }, t("Transkript wird geladen")));
    try {
      const tx = await api(`/episodes/${encodeURIComponent(id)}/transcript`);
      segs = tx.segments || [];
      draw();
    } catch (e) {
      clear(body);
      body.append(h("p", { class: "tr-error", role: "alert" }, errorText(e)));
    }
  });
  return section("transkript", t("Transkript"), h("p", { class: "tr-small" }, t("Zeiten in Sitzungszeit, wie in der Schnittansicht")), det);
}

function jumpTo(ms) {
  const ed = state.editor;
  if (!ed) return;
  $("#schnitt").scrollIntoView({ behavior: "smooth", block: "start" });
  ed.scrollTo(ms);
  ed.seek(ms);
  ed.stage.focus({ preventScroll: true });
}

// --- Rohspuren --------------------------------------------------------------------------------
function trackStatus(status) {
  switch (status) {
    case "recording": return t("Aufnahme läuft");
    case "finishing": return t("Wird abgeschlossen");
    case "complete": return t("Vollständig hochgeladen");
    case "incomplete": return t("Unvollständig");
    case "assembled": return t("Zusammengesetzt");
    default: return status;
  }
}

function tracksSection(ep) {
  const ul = h("ul", { class: "tr-list m-tracklist" });
  for (const tk of ep.tracks) {
    const dur = tk.total_frames && tk.sample_rate ? fmtDuration((tk.total_frames / tk.sample_rate) * 1000) : "";
    const link = tk.has_raw
      ? h("a", { class: "tr-btn tr-btn-secondary tr-btn-nav", href: `/api/host/episodes/${encodeURIComponent(id)}/tracks/${encodeURIComponent(tk.id)}/raw`, download: "" }, icon("download"), t("Rohspur laden"))
      : h("span", { class: "tr-small" }, t("Noch nicht zusammengesetzt"));
    ul.append(h("li", null, h("div", { class: "tr-item m-track" },
      h("div", { class: "m-ep-text" }, h("strong", { class: "m-ep-title" }, tk.participant_name), h("p", { class: "m-meta" }, h("span", { class: tk.status === "incomplete" ? "tr-status-warn" : "" }, trackStatus(tk.status)), dur ? h("span", { class: "m-sep", "aria-hidden": "true" }, "·") : null, dur ? h("span", { class: "m-num" }, dur) : null)),
      link)));
  }
  return section("spuren", t("Rohspuren"), h("p", { class: "tr-small" }, t("Die unbearbeiteten Aufnahmen der einzelnen Teilnehmer als FLAC-Datei")), ul);
}

// --- Veröffentlichen --------------------------------------------------------------------------
function publishSection(ep) {
  const sec = section("veroeffentlichen", t("Veröffentlichen"));
  state.ui.publish = h("div", { class: "m-publish" });
  sec.append(state.ui.publish);
  drawPublish();
  return sec;
}

function pad(n) { return String(n).padStart(2, "0"); }
function localInput(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`; }

function drawPublish() {
  const ep = state.ep;
  const box = state.ui.publish;
  if (!box) return;
  clear(box);
  const job = (ep.jobs || []).find((j) => (j.kind === "publish" || j.kind === "unpublish") && (j.status === "running" || j.status === "queued"));
  if (job) {
    box.append(h("p", null, job.kind === "publish" ? t("Die Folge wird veröffentlicht") : t("Die Folge wird zurückgezogen")), progressBar(job.progress, t("Fortschritt")));
    return;
  }
  const failedPub = (ep.jobs || []).find((j) => j.kind === "publish" && j.status === "failed");
  if (ep.status === "scheduled") {
    box.append(h("p", null, rich(t("Geplant für {when}. Die Folge erscheint dann von selbst im Feed."), { when: h("strong", { class: "m-num" }, fmtDateTime(ep.publish_at)) })),
      h("div", { class: "app-row" }, button(t("Zurückziehen"), { onclick: () => unpublish(t("Die Planung wird aufgehoben, die Folge bleibt in der Prüfung"), t("Planung aufheben")) })));
    return;
  }
  if (ep.status === "published") {
    const pubState = h("span", { class: "tr-status-ok" }, t("Veröffentlicht"));
    box.append(h("p", null, ep.published_at ? rich(t("{status} am {date}"), { status: pubState, date: fmtDateTime(ep.published_at) }) : pubState));
    if (ep.source === "recorded") box.append(h("div", { class: "app-row" }, button(t("Zurückziehen"), { onclick: () => unpublish(t("Die Folge verschwindet aus dem Feed, ihre Dateien werden gelöscht. Sie bleibt in der Prüfung erhalten."), t("Zurückziehen")) })));
    else box.append(h("p", { class: "tr-small" }, t("Importierte Folgen lassen sich hier nicht zurückziehen")));
    return;
  }
  // review
  const mode = h("div", { class: "m-radios", role: "radiogroup", "aria-label": t("Zeitpunkt") });
  const now = h("input", { type: "radio", name: "m-when", id: "m-when-now", value: "now", checked: true });
  const later = h("input", { type: "radio", name: "m-when", id: "m-when-later", value: "later" });
  const dt = h("input", { class: "tr-input", type: "datetime-local", id: "m-when-dt", disabled: true, "aria-label": t("Datum und Uhrzeit, Ortszeit") });
  const soon = new Date(Date.now() + 24 * 3600 * 1000);
  soon.setMinutes(0, 0, 0);
  dt.value = localInput(soon);
  dt.min = localInput(new Date());
  const err = h("p", { class: "tr-error", role: "alert", hidden: true });
  const sync = () => { dt.disabled = !later.checked; btn.textContent = later.checked ? t("Veröffentlichung planen") : t("Veröffentlichen"); };
  const btn = button(t("Veröffentlichen"), { variant: "primary", small: false, onclick: () => publish(later.checked ? dt.value : null, err) });
  now.addEventListener("change", sync);
  later.addEventListener("change", sync);
  mode.append(
    h("label", { class: "m-check", for: "m-when-now" }, now, h("span", null, t("Sofort"))),
    h("label", { class: "m-check", for: "m-when-later" }, later, h("span", null, t("Zu Datum und Uhrzeit"))),
    h("div", { class: "m-when-dt" }, dt, h("span", { class: "tr-small" }, t("Ortszeit"))));
  box.append(mode, err);
  if (failedPub) box.append(h("p", { class: "tr-error", role: "alert" }, t("Die letzte Veröffentlichung ist fehlgeschlagen. {message}", { message: failedPub.message || "" })));
  const can = () => !!(state.meta ? state.meta.title.value.trim() : ep.title) && !(ep.source === "recorded" && !ep.files.episode);
  btn.disabled = !can();
  const hint = h("p", { class: "tr-small" });
  const setHint = () => {
    const missing = [];
    if (!(state.meta ? state.meta.title.value.trim() : ep.title)) missing.push(t("Titel fehlt"));
    if (ep.source === "recorded" && !ep.files.episode) missing.push(t("Die fertige Folge fehlt"));
    hint.textContent = missing.length ? (missing.length > 1 ? `${missing.join(". ")}.` : missing[0]) : t("Ungespeicherte Änderungen werden vorher gespeichert");
    btn.disabled = missing.length > 0;
  };
  setHint();
  box.append(h("div", { class: "app-row" }, btn), hint);
  if (state.meta) state.meta.title.addEventListener("input", () => { if (btn.isConnected) setHint(); });
}

function updatePublishJob() {
  if (state.ui.publish && (state.ep.jobs || []).some((j) => j.kind === "publish" || j.kind === "unpublish")) drawPublish();
}

async function publish(when, errEl) {
  errEl.hidden = true;
  let body = {};
  if (when !== null) {
    const at = new Date(when);
    if (!when || Number.isNaN(at.getTime()) || at.getTime() < Date.now() + 60000) {
      errEl.textContent = t("Datum und Uhrzeit müssen in der Zukunft liegen");
      errEl.hidden = false;
      return;
    }
    body = { publish_at: at.toISOString() };
  } else {
    const ok = await confirmDialog({ title: t("Jetzt veröffentlichen"), text: t("Die Folge erscheint sofort im Feed und wird von den Podcast-Apps abgeholt"), ok: t("Veröffentlichen") });
    if (!ok) return;
  }
  try {
    if (dirty()) await saveAll();
    const res = await api(`/episodes/${encodeURIComponent(id)}/publish`, { method: "POST", json: body });
    toast(res.status === "scheduled" ? t("Veröffentlichung geplant") : t("Veröffentlichung gestartet"));
    await reload();
  } catch (e) {
    errEl.textContent = errorText(e);
    errEl.hidden = false;
  }
}

async function unpublish(text, ok) {
  if (!(await confirmDialog({ title: ok, text, ok }))) return;
  try {
    await api(`/episodes/${encodeURIComponent(id)}/unpublish`, { method: "POST" });
    await reload();
  } catch (e) {
    toast(errorText(e), { error: true });
  }
}

// --- Verwerfen und neu aufnehmen ----------------------------------------------------------------
function discardSection(ep) {
  const run = async (dropPlan) => {
    await api(`/episodes/${encodeURIComponent(id)}/discard`, { method: "POST", json: { drop_plan: dropPlan } });
    state.leaving = true;
    location.href = "/studio";
  };
  return h("section", { class: "m-section m-danger-zone", "aria-label": t("Aufnahme verwerfen") },
    h("h2", {}, t("Neu aufnehmen")),
    h("p", { class: "m-hint" }, t("Verwirft diese Aufnahme mit Spuren, Schnitten und Angaben. Der Sendeplan mit den Dossiers bleibt und steht im Studio für die neue Aufnahme bereit.")),
    h("div", { class: "app-row" },
      button(t("Aufnahme verwerfen"), { icon: "rotate-ccw", cls: "m-danger-btn", onclick: async () => {
        const ok = await confirmDialog({ title: t("Aufnahme verwerfen"), text: t("Alle Spuren dieser Aufnahme werden endgültig gelöscht. Der Sendeplan mit den Dossiers bleibt erhalten."), ok: t("Verwerfen"), danger: true });
        if (!ok) return;
        if (state.editor) state.editor.pause();
        try {
          await run(false);
        } catch (e) {
          if (e.code !== "plan_conflict") { toast(errorText(e), { error: true }); return; }
          if (!(await confirmDialog({ title: t("Sendeplan geht verloren"), text: e.message, ok: t("Trotzdem verwerfen"), danger: true }))) return;
          try { await run(true); } catch (e2) { toast(errorText(e2), { error: true }); }
        }
      } })));
}

// --- Löschen ----------------------------------------------------------------------------------
function deleteSection(ep) {
  return h("section", { class: "m-section m-danger-zone", "aria-label": t("Folge löschen") },
    h("div", { class: "app-row" },
      button(t("Folge löschen"), { icon: "trash", cls: "m-danger-btn", onclick: async () => {
        const ok = await confirmDialog({ title: t("Folge löschen"), text: t("Aufnahmen, Schnitte und Angaben dieser Folge werden endgültig gelöscht"), ok: t("Löschen"), danger: true });
        if (!ok) return;
        try {
          if (state.editor) state.editor.pause();
          await api(`/episodes/${encodeURIComponent(id)}`, { method: "DELETE" });
          state.leaving = true;
          location.href = "/episodes";
        } catch (e) {
          toast(errorText(e), { error: true });
        }
      } })));
}

start();
