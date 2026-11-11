// Vorbereitung: Sendeplan der nächsten Folge mit Dossiers (SPEC Abschnitt 14).
// Erwartete Aufrufe:
//   GET    /api/host/episodes                         aktuelle Folge = Status draft (sonst recording oder uploading)
//   POST   /api/host/episodes/draft                   legt eine Entwurfsfolge an, wenn es keine gibt
//   GET    /api/host/episodes/{id}/segments
//   POST   /api/host/episodes/{id}/segments           {kind, title?, input?, good_type?}
//   PATCH  /api/host/segments/{id}                    {title?, input?, good_type?, notes?}
//   DELETE /api/host/segments/{id}
//   POST   /api/host/segments/{id}/research
//   POST   /api/host/episodes/{id}/segments/order     {ids}
import {
  $, $$, api, button, clear, confirmDialog, debounce, errorText, field, fmtDate, fmtDateTime, h, icon, poll, safeUrl, toast,
} from "./common.js";

const root = $("#app");
const state = { episode: null, segments: [], stop: null, cards: new Map(), unavailable: false, format: null };

// Namen der Abschnittsarten und der Standardplan kommen aus den Einstellungen (Sendeformat); die Schlüssel sind fest
const DEFAULT_LABELS = { blitzlicht: "Kurzthema", hauptthema: "Hauptthema", good: "Empfehlung", frei: "Freier Abschnitt", read: "Lesetipp", watch: "Sehtipp", listen: "Hörtipp" };
const labels = () => ({ ...DEFAULT_LABELS, ...((state.format && state.format.labels) || {}) });
const kinds = () => { const l = labels(); return [["blitzlicht", l.blitzlicht], ["hauptthema", l.hauptthema], ["good", l.good], ["frei", l.frei]]; };
const goodTypes = () => { const l = labels(); return [["read", l.read], ["watch", l.watch], ["listen", l.listen]]; };
const STATUS = {
  empty: ["Noch keine Recherche", ""],
  researching: ["Recherche läuft", "tr-status-warn"],
  ready: ["Recherche fertig", "tr-status-ok"],
  failed: ["Recherche fehlgeschlagen", "tr-status-error"],
};

// --- Laden ---------------------------------------------------------------------------------------
async function start() {
  try {
    const [list, settings] = await Promise.all([api("/episodes"), api("/settings").catch(() => null)]);
    state.format = settings && settings.format;
    let cur = list.find((e) => e.status === "draft") || list.find((e) => e.status === "recording" || e.status === "uploading");
    if (!cur) {
      try {
        const made = await api("/episodes/draft", { method: "POST" });
        cur = made && (made.episode || made);
      } catch (e) {
        if (e.status === 404 || e.status === 405) return unavailable("Eine neue Folge lässt sich auf diesem Server noch nicht aus der Vorbereitung anlegen. Sie entsteht beim Öffnen des Studios.");
        throw e;
      }
    }
    if (!cur || !cur.id) return unavailable("Es gibt keine aktuelle Folge");
    state.episode = cur;
    await loadSegments();
    render();
    schedulePoll();
  } catch (e) {
    clear(root);
    root.append(h("p", { class: "tr-error", role: "alert" }, `Die Vorbereitung konnte nicht geladen werden. ${errorText(e)}`));
  }
}

function unavailable(text) {
  clear(root);
  root.append(h("div", { class: "m-empty" }, h("p", null, "Die Vorbereitung steht noch nicht bereit"), h("p", { class: "tr-small" }, text)));
}

async function loadSegments() {
  try {
    const res = await api(`/episodes/${encodeURIComponent(state.episode.id)}/segments`);
    const list = Array.isArray(res) ? res : res.segments || [];
    state.segments = list.map(norm).sort((a, b) => (a.sort ?? 0) - (b.sort ?? 0));
    state.unavailable = false;
  } catch (e) {
    if (e.status === 404 || e.status === 405) {
      state.unavailable = true;
      state.segments = [];
      return;
    }
    throw e;
  }
}

function norm(s) {
  let d = s.dossier;
  if (typeof d === "string") {
    try { d = JSON.parse(d); } catch { d = null; }
  }
  return { ...s, dossier: d && typeof d === "object" ? d : null, input: s.input || "", notes: s.notes || "", title: s.title || "", status: s.status || "empty" };
}

function schedulePoll() {
  state.stop?.();
  if (!state.segments.some((s) => s.status === "researching")) return;
  state.stop = poll(async () => {
    const before = state.segments.map((s) => s.id).join();
    const prev = new Map(state.segments.map((s) => [s.id, s]));
    await loadSegments();
    if (state.segments.map((s) => s.id).join() !== before) { render(); return state.segments.some((s) => s.status === "researching"); }
    for (const s of state.segments) {
      const o = prev.get(s.id);
      // Eingaben des Hosts nie überschreiben: nur Status und Dossier übernehmen
      o.status = s.status; o.dossier = s.dossier; o.updated_at = s.updated_at;
      refreshCard(o);
    }
    state.segments = state.segments.map((s) => prev.get(s.id));
    return state.segments.some((s) => s.status === "researching");
  }, 3000);
}

// --- Beschriftungen --------------------------------------------------------------------------------
function labelOf(seg, index) {
  const l = labels();
  if (seg.kind === "good") return l[seg.good_type] || l.read;
  const same = state.segments.filter((s) => s.kind === seg.kind);
  const name = l[seg.kind] || l.frei;
  return same.length > 1 ? `${name} ${same.findIndex((s) => s.id === seg.id) + 1}` : name; // gleiche Art mehrfach: durchzählen
}

// --- Aufbau ---------------------------------------------------------------------------------------
function render() {
  clear(root);
  state.cards.clear();
  if (state.unavailable) {
    root.append(h("div", { class: "m-empty" },
      h("p", null, "Der Sendeplan steht auf diesem Server noch nicht bereit"),
      h("p", { class: "tr-small" }, "Sobald die Funktion eingebaut ist, erscheinen hier die Abschnitte der nächsten Folge")));
    return;
  }
  const ep = state.episode;
  const label = ep.number != null ? `Folge ${ep.number}` : "nächste Folge";
  const kindSel = h("select", { class: "tr-input m-select", id: "m-add-kind", "aria-label": "Art des neuen Abschnitts" });
  kinds().forEach(([v, t]) => kindSel.append(h("option", { value: v }, t)));
  const bar = h("div", { class: "m-plan-bar m-screen-only" },
    button("Alle recherchieren", { variant: "primary", small: false, onclick: researchAll }),
    button("Drucken", { icon: "printer", small: false, onclick: () => window.print() }),
    h("div", { class: "m-add" }, kindSel, button("Hinzufügen", { icon: "plus", small: false, onclick: () => addSegment(kindSel.value) })));
  root.append(
    h("p", { class: "m-lead m-screen-only" }, `Sendeplan für ${label}. Die App schlägt keine Themen vor: Stichworte, These oder Links eintragen, die Recherche liefert dazu ein Dossier mit Quellen.`),
    h("div", { class: "m-print-only m-print-head" }, h("h1", null, `Sendeplan ${label}`), h("p", null, `${ep.title ? ep.title + ", " : ""}Stand ${fmtDate(Date.now())}`)),
    bar);
  if (!state.segments.length) {
    root.append(h("div", { class: "m-empty" },
      h("p", null, "Der Sendeplan ist noch leer"),
      h("div", { class: "app-row" }, button("Standardplan anlegen", { onclick: defaultPlan })),
      h("p", { class: "tr-small" }, "Woraus der Standardplan besteht, steht in den Einstellungen unter Sendeformat")));
    return;
  }
  const wrap = h("div", { class: "m-segs" });
  state.segments.forEach((s, i) => wrap.append(card(s, i)));
  root.append(wrap);
}

function card(seg, index) {
  const refs = { seg };
  const p = `seg-${seg.id}`;
  const saveState = h("span", { class: "m-save-state m-screen-only", role: "status" });
  const flush = debounce(async () => {
    const body = refs.pending;
    refs.pending = null;
    if (!body) return;
    saveState.textContent = "Speichert";
    saveState.className = "m-save-state m-screen-only";
    try {
      await api(`/segments/${encodeURIComponent(seg.id)}`, { method: "PATCH", json: body });
      saveState.textContent = "Gespeichert";
      saveState.className = "m-save-state m-screen-only tr-status-ok";
    } catch (e) {
      refs.pending = { ...body, ...(refs.pending || {}) };
      saveState.textContent = `Nicht gespeichert. ${errorText(e)}`;
      saveState.className = "m-save-state m-screen-only tr-status-error";
    }
  }, 800);
  refs.flush = flush;
  const queue = (patch) => { Object.assign(seg, patch); refs.pending = { ...(refs.pending || {}), ...patch }; flush(); };

  const title = h("input", { class: "tr-input", type: "text", id: `${p}-title`, value: seg.title, maxlength: "200", placeholder: seg.kind === "good" ? "Titel des Werks, optional" : "Arbeitstitel" });
  const input = h("textarea", { class: "tr-input", id: `${p}-input`, rows: 4, placeholder: "Stichworte, These, Links" });
  input.value = seg.input;
  const notes = h("textarea", { class: "tr-input", id: `${p}-notes`, rows: 4, placeholder: "Eigene Notizen, werden nie überschrieben" });
  notes.value = seg.notes;
  const printTitle = h("p", { class: "m-print-only m-print-title" }, seg.title);
  const printInput = h("p", { class: "m-print-only m-pre-line" }, seg.input);
  const printNotes = h("div", { class: "m-print-only" }, h("h3", { class: "m-h3" }, "Eigene Notizen"), h("p", { class: "m-pre-line" }, seg.notes));
  const heading = h("h2", { class: "m-seg-title" }, labelOf(seg, index));
  title.addEventListener("input", () => { printTitle.textContent = title.value; queue({ title: title.value.trim() }); });
  input.addEventListener("input", () => { printInput.textContent = input.value; queue({ input: input.value }); });
  notes.addEventListener("input", () => { printNotes.lastChild.textContent = notes.value; printNotes.hidden = !notes.value.trim(); queue({ notes: notes.value }); });
  printNotes.hidden = !seg.notes.trim();
  printInput.hidden = !seg.input.trim();

  let goodSel = null;
  if (seg.kind === "good") {
    goodSel = h("select", { class: "tr-input m-select", id: `${p}-good` });
    goodTypes().forEach(([v, t]) => goodSel.append(h("option", { value: v, selected: (seg.good_type || "read") === v }, t)));
    goodSel.addEventListener("change", () => { queue({ good_type: goodSel.value }); heading.textContent = labelOf(seg, index); });
  }
  refs.title = title; refs.input = input; refs.goodSel = goodSel; refs.heading = heading; refs.printTitle = printTitle; refs.printInput = printInput;

  refs.status = h("p", { class: "m-seg-status" });
  refs.dossier = h("div", { class: "m-dossier-wrap" });
  refs.research = button("Recherchieren", { icon: "search", onclick: () => research(refs), cls: "m-screen-only" });

  const el = h("section", { class: "m-seg", id: p, "data-kind": seg.kind, "aria-labelledby": `${p}-h` },
    h("div", { class: "m-seg-head" },
      h("div", { class: "m-seg-headtext" }, (heading.id = `${p}-h`, heading), refs.status),
      h("div", { class: "m-sound-actions m-screen-only" },
        button("", { icon: "up", ariaLabel: `${labelOf(seg, index)} nach oben`, title: "Nach oben", disabled: index === 0, onclick: () => move(index, -1) }),
        button("", { icon: "down", ariaLabel: `${labelOf(seg, index)} nach unten`, title: "Nach unten", disabled: index === state.segments.length - 1, onclick: () => move(index, 1) }),
        button("", { icon: "trash", ariaLabel: `${labelOf(seg, index)} löschen`, title: "Löschen", onclick: () => removeSegment(seg) }))),
    printTitle, printInput,
    h("div", { class: "m-seg-form m-screen-only" },
      h("div", { class: goodSel ? "m-grid-title-good" : "" },
        field("Titel", title, { id: `${p}-title` }),
        goodSel ? h("div", { class: "tr-field m-field" }, h("label", { class: "tr-label", for: `${p}-good` }, "Art"), goodSel) : null),
      field("Stichworte, These, Links", input, { id: `${p}-input` }),
      h("div", { class: "app-row" }, refs.research, saveState)),
    refs.dossier,
    h("div", { class: "m-screen-only" }, field("Eigene Notizen", notes, { id: `${p}-notes` })),
    printNotes);
  state.cards.set(seg.id, refs);
  refreshCard(seg);
  return el;
}

function refreshCard(seg) {
  const refs = state.cards.get(seg.id);
  if (!refs) return;
  refs.seg = seg;
  const [text, cls] = STATUS[seg.status] || [seg.status, ""];
  clear(refs.status);
  refs.status.append(h("span", { class: cls }, text));
  if (seg.status === "ready" && seg.dossier?.stand) refs.status.append(h("span", { class: "m-sep", "aria-hidden": "true" }, "·"), h("span", { class: "m-num tr-small" }, `Stand ${fmtDateTime(Date.parse(seg.dossier.stand))}`));
  if (seg.status === "failed") refs.status.append(h("span", { class: "tr-small" }, " Erneut versuchen"));
  refs.research.disabled = seg.status === "researching";
  refs.research.replaceChildren(icon("search"), document.createTextNode(seg.status === "researching" ? "Recherche läuft" : seg.status === "ready" ? "Neu recherchieren" : "Recherchieren"));
  clear(refs.dossier);
  if (seg.dossier && (seg.status === "ready" || seg.dossier.lage)) refs.dossier.append(dossier(seg));
}

// --- Dossier ---------------------------------------------------------------------------------------
function link(url, text) {
  const u = safeUrl(url);
  if (!u) return document.createTextNode(text || "");
  let t = text;
  if (!t) { try { t = new URL(u).hostname.replace(/^www\./, ""); } catch { t = u; } }
  return h("a", { href: u, target: "_blank", rel: "noopener noreferrer" }, t);
}

function block(title, content) {
  return h("div", { class: "m-dos-block" }, h("h4", { class: "m-dos-h" }, title), content);
}

const list = (items, cls = "m-plain") => h("ul", { class: cls }, items.map((t) => h("li", null, t)));

function dossier(seg) {
  const d = seg.dossier;
  const det = h("details", { class: "m-details m-dossier", open: true }, h("summary", null, "Dossier"));
  const body = h("div", { class: "m-dossier" });
  if (d.lage) body.append(block("Lage", h("p", null, d.lage)));
  if (Array.isArray(d.kandidaten) && d.kandidaten.length) {
    const ul = h("ul", { class: "m-cands" });
    d.kandidaten.forEach((k) => ul.append(h("li", null,
      h("div", { class: "m-cand-text" },
        h("strong", null, k.titel || "Ohne Titel"),
        h("p", { class: "tr-small" }, [k.urheber, ({ read: "Lesen", watch: "Ansehen", listen: "Hören" })[String(k.typ).toLowerCase()] || k.typ].filter(Boolean).join(", ")),
        k.warum ? h("p", null, k.warum) : null,
        k.url ? h("p", { class: "tr-small" }, link(k.url)) : null),
      button("Übernehmen", { cls: "m-screen-only", onclick: () => adopt(seg, k) }))));
    body.append(block("Kandidaten", ul));
  }
  if (d.fakten?.length) {
    body.append(block("Fakten", list(d.fakten.map((f) => [
      h("span", null, f.text),
      " ",
      h("span", { class: "tr-small m-src" }, "Quelle: ", link(f.quelle_url, f.quelle_titel), f.datum ? `, ${formatDay(f.datum)}` : ""),
    ]))));
  }
  if (d.zahlen?.length) body.append(block("Zahlen", list(d.zahlen.map((z) => [h("span", null, z.text), z.quelle_url ? [" ", h("span", { class: "tr-small m-src" }, "Quelle: ", link(z.quelle_url))] : null]))));
  if (d.positionen?.length) {
    const dl = h("dl", { class: "m-dl m-dl-plain" });
    d.positionen.forEach((x) => dl.append(h("div", null, h("dt", null, x.wer), h("dd", null, x.position))));
    body.append(block("Positionen", dl));
  }
  if (d.gegenargumente?.length) body.append(block("Gegenargumente", list(d.gegenargumente)));
  if (d.fragen?.length) body.append(block("Fragen", list(d.fragen)));
  if (d.aufhaenger?.length) body.append(block("Aufhänger", list(d.aufhaenger)));
  if (d.begriffe?.length) body.append(block("Begriffe", h("ul", { class: "m-tags" }, d.begriffe.map((b) => h("li", null, h("span", { class: "tr-tag" }, b))))));
  if (d.quellen?.length) {
    body.append(block("Quellen", h("ol", { class: "m-plain m-sources" }, d.quellen.map((q) => h("li", null, link(q.url, q.titel), q.datum ? h("span", { class: "tr-small" }, `, ${formatDay(q.datum)}`) : null)))));
  }
  if (d.stand) body.append(h("p", { class: "tr-small m-num" }, `Stand der Recherche: ${fmtDateTime(Date.parse(d.stand))}`));
  det.append(body);
  return det;
}

function formatDay(s) {
  const t = Date.parse(s);
  return Number.isNaN(t) ? s : fmtDate(t);
}

async function adopt(seg, k) {
  const refs = state.cards.get(seg.id);
  const typ = String(k.typ || "").toLowerCase();
  const good = /watch|video|film|doku|serie|schau/.test(typ) ? "watch" : /listen|podcast|audio|hör|hoer/.test(typ) ? "listen" : "read";
  const patch = { title: k.titel || seg.title, input: [k.titel, k.urheber, k.url].filter(Boolean).join("\n"), good_type: good };
  try {
    await api(`/segments/${encodeURIComponent(seg.id)}`, { method: "PATCH", json: patch });
    Object.assign(seg, patch);
    refs.title.value = patch.title;
    refs.input.value = patch.input;
    refs.printTitle.textContent = patch.title;
    refs.printInput.textContent = patch.input;
    refs.printInput.hidden = false;
    if (refs.goodSel) refs.goodSel.value = good;
    refs.heading.textContent = labelOf(seg);
    toast("Übernommen. Jetzt für das gewählte Werk neu recherchieren");
  } catch (e) {
    toast(errorText(e), { error: true });
  }
}

// --- Aktionen ---------------------------------------------------------------------------------------
async function research(refs) {
  const seg = refs.seg;
  await refs.flush.flush();
  try {
    await api(`/segments/${encodeURIComponent(seg.id)}/research`, { method: "POST" });
    seg.status = "researching";
    refreshCard(seg);
    schedulePoll();
  } catch (e) {
    toast(errorText(e), { error: true });
  }
}

async function researchAll() {
  let started = 0;
  let skipped = 0;
  for (const seg of state.segments) {
    if (seg.status === "researching") continue;
    const refs = state.cards.get(seg.id);
    if (!seg.input.trim() && !seg.title.trim() && seg.kind !== "good") { skipped++; continue; }
    try {
      if (refs) await refs.flush.flush();
      await api(`/segments/${encodeURIComponent(seg.id)}/research`, { method: "POST" });
      seg.status = "researching";
      refreshCard(seg);
      started++;
    } catch (e) {
      toast(`${labelOf(seg)}: ${errorText(e)}`, { error: true });
    }
  }
  toast(`${started} ${started === 1 ? "Recherche gestartet" : "Recherchen gestartet"}${skipped ? `, ${skipped} ohne Eingabe übersprungen` : ""}`);
  schedulePoll();
}

async function addSegment(kind) {
  try {
    await api(`/episodes/${encodeURIComponent(state.episode.id)}/segments`, { method: "POST", json: { kind } });
    await loadSegments();
    render();
    const last = $$(".m-seg").pop();
    last?.querySelector("input")?.focus();
  } catch (e) {
    toast(errorText(e), { error: true });
  }
}

async function defaultPlan() {
  try {
    const plan = (state.format && state.format.plan) || ["blitzlicht", "blitzlicht", "blitzlicht", "hauptthema", "good:read"];
    for (const entry of plan) {
      const [kind, good] = entry.split(":");
      await api(`/episodes/${encodeURIComponent(state.episode.id)}/segments`, { method: "POST", json: { kind, ...(kind === "good" ? { good_type: good || "read" } : {}) } });
    }
    await loadSegments();
    render();
  } catch (e) {
    toast(errorText(e), { error: true });
  }
}

async function removeSegment(seg) {
  const ok = await confirmDialog({ title: "Abschnitt löschen", text: `„${seg.title || labelOf(seg)}“ wird samt Dossier und Notizen gelöscht`, ok: "Löschen", danger: true });
  if (!ok) return;
  try {
    await api(`/segments/${encodeURIComponent(seg.id)}`, { method: "DELETE" });
    await loadSegments();
    render();
    schedulePoll();
  } catch (e) {
    toast(errorText(e), { error: true });
  }
}

async function move(i, d) {
  const j = i + d;
  if (j < 0 || j >= state.segments.length) return;
  const segs = [...state.segments];
  [segs[i], segs[j]] = [segs[j], segs[i]];
  try {
    await api(`/episodes/${encodeURIComponent(state.episode.id)}/segments/order`, { method: "POST", json: { ids: segs.map((s) => s.id) } });
    segs.forEach((s, k) => (s.sort = k));
    state.segments = segs;
    render();
    const moved = document.getElementById(`seg-${segs[j].id}`);
    moved?.querySelector(`.m-sound-actions button[aria-label$="${d < 0 ? "oben" : "unten"}"]`)?.focus();
  } catch (e) {
    toast(errorText(e), { error: true });
  }
}

// Beim Drucken alle Dossiers aufklappen
window.addEventListener("beforeprint", () => $$(".m-dossier").forEach((d) => (d.open = true)));
window.addEventListener("beforeunload", (e) => {
  if ([...state.cards.values()].some((r) => r.pending)) { e.preventDefault(); e.returnValue = ""; }
});

start();
