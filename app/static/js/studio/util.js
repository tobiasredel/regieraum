// Kleine Hilfen für das Studio: DOM, Zahlen- und Zeitformat, Icons, API-Aufrufe.

const V = new URL(import.meta.url).search;
const { t, LOCALE } = await import(`../i18n.js${V}`);

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Element bauen: h("div", {class: "x", onclick: fn}, kind1, "Text", …). */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** Inhalt eines Elements ersetzen. */
export function fill(el, ...children) {
  el.replaceChildren(...children.flat().filter((c) => c != null && c !== false));
  return el;
}

// Lucide-Icons (ISC-Lizenz), nur die Pfade. 20 px, Strich 1,5 px über .tr-icon.
const ICONS = {
  "rotate-ccw": '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
  mic: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3"/>',
  "mic-off": '<path d="m2 2 20 20M18.89 13.23A7 7 0 0 0 19 12v-2M5 10v2a7 7 0 0 0 12 5M15 9.34V5a3 3 0 0 0-5.68-1.33M9 9v3a3 3 0 0 0 5.12 2.12M12 19v3"/>',
  video: '<path d="m16 13 5.22 3.48a.5.5 0 0 0 .78-.42V7.94a.5.5 0 0 0-.76-.43L16 10.5"/><rect x="2" y="6" width="14" height="12" rx="2"/>',
  "video-off": '<path d="M10.66 6H14a2 2 0 0 1 2 2v2.5l5.25-3.06A.5.5 0 0 1 22 7.87v8.26M16 16a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h2M2 2l20 20"/>',
  headphones: '<path d="M3 14h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a9 9 0 0 1 18 0v7a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3"/>',
  speaker: '<path d="M11 4.7a.7.7 0 0 0-1.2-.5L6.4 7.6A1.4 1.4 0 0 1 5.4 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.4a1.4 1.4 0 0 1 1 .4l3.4 3.4a.7.7 0 0 0 1.2-.5Z"/><path d="M16 9a5 5 0 0 1 0 6M19.36 18.36a9 9 0 0 0 0-12.73"/>',
  circle: '<circle cx="12" cy="12" r="10"/>',
  pause: '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
  play: '<polygon points="6 3 20 12 6 21 6 3"/>',
  square: '<rect width="18" height="18" x="3" y="3" rx="2"/>',
  flag: '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1zM4 22v-7"/>',
  "volume-x": '<path d="M11 4.7a.7.7 0 0 0-1.2-.5L6.4 7.6A1.4 1.4 0 0 1 5.4 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.4a1.4 1.4 0 0 1 1 .4l3.4 3.4a.7.7 0 0 0 1.2-.5ZM22 9l-6 6M16 9l6 6"/>',
  pin: '<path d="M12 17v5M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  "chevron-right": '<path d="m9 18 6-6-6-6"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  zap: '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>',
  "external-link": '<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8M21 3v5h-5M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16M8 16H3v5"/>',
};

export function icon(name, extraClass = "") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", ("tr-icon " + extraClass).trim());
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = ICONS[name] || "";
  return svg;
}

// ---- Format ----
const nf1 = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const nf0 = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 0 });
export const num1 = (x) => nf1.format(x);
export const num0 = (x) => nf0.format(x);

/** Dauer in ms als m:ss bzw. h:mm:ss. */
export function dur(ms) {
  ms = Math.max(0, Math.floor(ms / 1000));
  const s = ms % 60, m = Math.floor(ms / 60) % 60, hh = Math.floor(ms / 3600);
  const ss = String(s).padStart(2, "0");
  return hh ? `${hh}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

export function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

export function dbToGain(db) { return Math.pow(10, (db || 0) / 20); }
export function toDb(a) { return a > 1e-6 ? 20 * Math.log10(a) : -120; }

/** Fehler der API mit Code und deutscher Meldung. */
export class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

/** JSON-Aufruf mit optionalem Teilnehmer-Token. Netzfehler werfen ApiError mit status 0. */
export async function api(method, url, { token, body, headers, signal, raw } = {}) {
  const hd = { ...(headers || {}) };
  if (token) hd.Authorization = `Bearer ${token}`;
  let payload = body;
  if (body !== undefined && !(body instanceof ArrayBuffer) && !ArrayBuffer.isView(body) && !(body instanceof Blob)) {
    hd["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(url, { method, headers: hd, body: payload, signal, cache: "no-store" });
  } catch (e) {
    if (e && e.name === "AbortError") throw e;
    throw new ApiError(0, "network", t("Keine Verbindung zum Server"));
  }
  if (raw) return r;
  let data = null;
  try { data = await r.json(); } catch { /* kein JSON */ }
  if (!r.ok) {
    const err = (data && data.error) || {};
    throw new ApiError(r.status, err.code || "http_" + r.status, err.message || t("Fehler {status}", { status: r.status }));
  }
  return data;
}

/** Geräteart nach Fähigkeiten, nicht nach Browsernamen. */
export function isMobile() {
  if (navigator.userAgentData && typeof navigator.userAgentData.mobile === "boolean") return navigator.userAgentData.mobile;
  const coarse = window.matchMedia && matchMedia("(pointer: coarse)").matches;
  const small = Math.min(screen.width, screen.height) < 820;
  // Tablets zählen als „mobile“ (Kopfhörer anfangs aus); ein iPad meldet sich mit Desktop-Kennung, hat aber Touch
  return !!(coarse && (small || navigator.maxTouchPoints > 1));
}

export function params() { return new URLSearchParams(location.search); }

/** Ruhige Kurzmeldung oben (tr-toast), verschwindet nach einigen Sekunden. */
let toastTimer = 0;
export function toast(text, ms = 3500) {
  let el = document.getElementById("ps-toast");
  if (!el) {
    el = h("div", { id: "ps-toast", class: "tr-toast", role: "status", "aria-live": "polite" });
    document.body.append(el);
  }
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ms);
}
