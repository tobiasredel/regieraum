// Gemeinsame Bausteine der Verwaltungsseiten: API, DOM-Hilfen, Formate, Hinweise, Rückfragen, Icons.
// Kein Framework, kein Build. Alle Texte werden über textContent gesetzt, nie als HTML.

const BASE = "/api/host";

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/** JSON-Aufruf gegen /api/host. `json` wird als Körper gesendet, `form` (FormData) als multipart. */
export async function api(path, { method = "GET", json, form, signal } = {}) {
  const init = { method, headers: {}, signal };
  if (json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(json);
  } else if (form) {
    init.body = form;
  }
  let res;
  try {
    res = await fetch(BASE + path, init);
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError(0, "network", "Keine Verbindung zum Server");
  }
  if (res.status === 204) return null;
  const type = res.headers.get("content-type") || "";
  const body = type.includes("json") ? await res.json().catch(() => null) : await res.text().catch(() => "");
  if (!res.ok) {
    const err = body && typeof body === "object" ? body.error || {} : {};
    throw new ApiError(res.status, err.code || "error", err.message || (typeof body === "string" && body.length < 200 && body) || `Fehler ${res.status}`);
  }
  return body;
}

// --- DOM ---------------------------------------------------------------------------------------
/** h("div", {class: "x", onclick: fn, dataset: {a: 1}}, "Text", child, ...) */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "disabled" || k === "selected" || k === "hidden" || k === "open") el[k] = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// --- Icons (Lucide, Outline 1,5 px, 20 px) ------------------------------------------------------
const ICONS = {
  "rotate-ccw": '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
  play: '<polygon points="6 3 20 12 6 21 6 3"/>',
  pause: '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
  scissors: '<circle cx="6" cy="6" r="3"/><path d="M8.12 8.12 12 12"/><path d="M20 4 8.12 15.88"/><circle cx="6" cy="18" r="3"/><path d="M14.8 14.8 20 20"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1-5.5 5.5H11"/>',
  zoomIn: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35M11 8v6M8 11h6"/>',
  zoomOut: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35M8 11h6"/>',
  fit: '<path d="m18 8 4 4-4 4"/><path d="M2 12h20"/><path d="m6 8-4 4 4 4"/>',
  trash: '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><path d="M10 11v6M14 11v6"/>',
  up: '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
  down: '<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>',
  copy: '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
  refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
  plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
  printer: '<path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><path d="M6 9V3a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v6"/><rect x="6" y="14" width="12" height="8" rx="1"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>',
  external: '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  mic: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3"/>',
  sparkles: '<path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/>',
};

export function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "tr-icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = ICONS[name] || ""; // feste Zeichenketten aus diesem Modul
  return svg;
}

/** Schaltfläche mit optionalem Icon. variant: primary | secondary | link; small = 36 px. */
export function button(label, { variant = "secondary", icon: ic, small = true, onclick, title, type = "button", disabled, ariaLabel, cls } = {}) {
  const b = h("button", {
    type, title, "aria-label": ariaLabel, disabled,
    class: `tr-btn tr-btn-${variant}${small && variant !== "link" ? " tr-btn-nav" : ""}${cls ? " " + cls : ""}`,
  });
  if (ic) b.append(icon(ic));
  if (label) b.append(document.createTextNode(label));
  if (onclick) b.addEventListener("click", onclick);
  return b;
}

// --- Formate (deutsch) -------------------------------------------------------------------------
const NF0 = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 0 });
const NF1 = new Intl.NumberFormat("de-DE", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const DF = new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" });
const TF = new Intl.DateTimeFormat("de-DE", { hour: "2-digit", minute: "2-digit" });

export const fmtInt = (n) => NF0.format(n);
export const fmtDec = (n, digits = 1) => (digits === 1 ? NF1.format(n) : new Intl.NumberFormat("de-DE", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(n));
export const fmtDate = (ms) => (ms ? DF.format(new Date(ms)) : "");
export const fmtTimeOfDay = (ms) => (ms ? TF.format(new Date(ms)) : "");
export const fmtDateTime = (ms) => (ms ? `${fmtDate(ms)}, ${fmtTimeOfDay(ms)} Uhr` : "");
/** Minuszeichen statt Bindestrich. */
export const signed = (n, digits = 1) => (n < 0 ? "−" : n > 0 ? "+" : "") + fmtDec(Math.abs(n), digits);
export const neg = (n, digits = 1) => (n < 0 ? "−" : "") + fmtDec(Math.abs(n), digits);

/** Uhrzeit-Anzeige für Positionen: 3:07 oder 1:02:03. */
export function fmtClock(ms, withTenths = false) {
  if (!Number.isFinite(ms)) return "–";
  const neg_ = ms < 0;
  ms = Math.abs(ms);
  const total = Math.floor(ms / 1000);
  const hh = Math.floor(total / 3600);
  const mm = Math.floor((total % 3600) / 60);
  const ss = total % 60;
  let s = hh ? `${hh}:${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}` : `${mm}:${String(ss).padStart(2, "0")}`;
  if (withTenths) s += "," + String(Math.floor((ms % 1000) / 100));
  return (neg_ ? "−" : "") + s;
}

/** Dauer in Worten: 47 min, 1 h 12 min, 38 s. */
export function fmtDuration(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return "";
  const s = Math.round(ms / 1000);
  if (s < 90) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  const hh = Math.floor(m / 60);
  const mm = m % 60;
  return mm ? `${hh} h ${mm} min` : `${hh} h`;
}

export function fmtBytes(b) {
  if (!Number.isFinite(b)) return "";
  if (b >= 1e9) return `${fmtDec(b / 1e9, 1)} GB`;
  if (b >= 1e6) return `${fmtInt(b / 1e6)} MB`;
  return `${fmtInt(b / 1e3)} kB`;
}

export const STATUS = {
  draft: ["Entwurf", ""],
  recording: ["Aufnahme läuft", "error"],
  uploading: ["Upload läuft", "warn"],
  processing: ["Wird verarbeitet", ""],
  review: ["Zur Prüfung", "warn"],
  scheduled: ["Geplant", ""],
  published: ["Veröffentlicht", ""],
  failed: ["Fehlgeschlagen", "error"],
};

export function statusText(status) {
  return (STATUS[status] || [status, ""])[0];
}

export function statusClass(status) {
  const c = (STATUS[status] || ["", ""])[1];
  return c ? `tr-status-${c}` : "";
}

export function safeUrl(u) {
  try {
    const url = new URL(u, location.href);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

// --- Hinweise und Rückfragen --------------------------------------------------------------------
let toastTimer = null;
export function toast(message, { error = false } = {}) {
  let el = $("#m-toast");
  if (!el) {
    el = h("div", { id: "m-toast", class: "tr-toast", role: "status", "aria-live": "polite", hidden: true });
    document.body.append(el);
  }
  el.textContent = message;
  el.classList.toggle("m-toast-error", error);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), error ? 7000 : 3500);
}

export function errorText(e) {
  return e && e.message ? e.message : "Unbekannter Fehler";
}

/** Rückfrage als Dialog. Liefert true bei Bestätigung. */
export function confirmDialog({ title, text, ok = "Bestätigen", cancel = "Abbrechen", danger = false }) {
  return new Promise((resolve) => {
    const dlg = h("dialog", { class: "tr-modal m-dialog", "aria-labelledby": "m-dlg-title" });
    const done = (v) => {
      dlg.close();
      dlg.remove();
      resolve(v);
    };
    dlg.append(
      h("h2", { id: "m-dlg-title", class: "m-dialog-title" }, title),
      text ? h("p", { class: "m-dialog-text" }, text) : null,
      h("div", { class: "app-row m-dialog-actions" },
        button(ok, { variant: "primary", small: false, cls: danger ? "m-danger" : "", onclick: () => done(true) }),
        button(cancel, { variant: "secondary", small: false, onclick: () => done(false) })),
    );
    dlg.addEventListener("cancel", (e) => {
      e.preventDefault();
      done(false);
    });
    document.body.append(dlg);
    dlg.showModal();
  });
}

/** Zwischenablage mit Ausweich über markiertes Feld. */
export async function copyText(text, fallbackEl) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    if (fallbackEl) {
      fallbackEl.focus();
      fallbackEl.select();
      try {
        return document.execCommand("copy");
      } catch {
        return false;
      }
    }
    return false;
  }
}

/** Wiederholt fn, bis sie false liefert. Liefert eine stop()-Funktion. */
export function poll(fn, ms = 2500) {
  let stopped = false;
  let t = null;
  const tick = async () => {
    if (stopped) return;
    let again = true;
    try {
      again = await fn();
    } catch {
      again = true;
    }
    if (!stopped && again !== false) t = setTimeout(tick, ms);
  };
  t = setTimeout(tick, ms);
  return () => {
    stopped = true;
    clearTimeout(t);
  };
}

export function debounce(fn, ms = 700) {
  let t = null;
  const d = (...a) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
  d.flush = (...a) => {
    clearTimeout(t);
    return fn(...a);
  };
  d.cancel = () => clearTimeout(t);
  return d;
}

export function progressBar(value, label) {
  const pct = Math.max(0, Math.min(1, value || 0));
  const bar = h("div", { class: "app-progress", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(pct * 100), "aria-label": label || "Fortschritt" });
  bar.append(h("span", { style: `width:${(pct * 100).toFixed(1)}%` }));
  return bar;
}

export function field(label, control, { hint, id, cls } = {}) {
  const fid = id || control.id || `f-${Math.random().toString(36).slice(2, 8)}`;
  control.id = fid;
  return h("div", { class: `tr-field m-field${cls ? " " + cls : ""}` },
    h("label", { class: "tr-label", for: fid }, label),
    control,
    hint ? h("p", { class: "tr-small m-hint" }, hint) : null);
}
