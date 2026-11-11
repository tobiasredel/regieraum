// Folgenliste: Nummer, Titel, Status als Text, Dauer, Datum; laufende Verarbeitung mit Fortschritt (Polling).
import { $, api, clear, fmtDate, fmtDateTime, fmtDuration, h, errorText, poll, progressBar, statusClass, statusText } from "./common.js";

const root = $("#app");
const JOB_LABEL = { process: "Verarbeitung", remix: "Neu mischen", publish: "Veröffentlichung", unpublish: "Zurückziehen", suggest: "Vorschlag", transcribe: "Transkript" };
let stop = null;

function dateText(ep) {
  if (ep.status === "published" && ep.published_at) return `veröffentlicht ${fmtDate(ep.published_at)}`;
  if (ep.status === "scheduled" && ep.publish_at) return `geplant für ${fmtDateTime(ep.publish_at)}`;
  return ep.created_at ? `angelegt ${fmtDate(ep.created_at)}` : "";
}

function row(ep) {
  const parts = [
    h("span", { class: statusClass(ep.status) }, statusText(ep.status)),
    ep.duration_ms ? h("span", { class: "m-num" }, fmtDuration(ep.duration_ms)) : null,
    h("span", { class: "m-num" }, dateText(ep)),
  ].filter(Boolean);
  const meta = h("p", { class: "m-meta" });
  parts.forEach((p, i) => {
    if (i) meta.append(h("span", { class: "m-sep", "aria-hidden": "true" }, "·"));
    meta.append(p);
  });
  if (ep.source === "imported") meta.append(h("span", { class: "m-sep", "aria-hidden": "true" }, "·"), h("span", null, "importiert"));
  const text = h("div", { class: "m-ep-text" },
    h("strong", { class: "m-ep-title" }, ep.title || "Ohne Titel"), meta);
  if (ep.job) {
    const j = ep.job;
    const label = JOB_LABEL[j.kind] || "Aufgabe";
    const state = j.status === "queued" ? "wartet" : j.message || "läuft";
    text.append(
      h("div", { class: "m-job" },
        h("p", { class: "tr-small" }, `${label}: ${state}${j.status === "running" ? `, ${Math.round((j.progress || 0) * 100)} %` : ""}`),
        j.status === "running" ? progressBar(j.progress, `${label}, Fortschritt`) : null));
  }
  const no = h("span", { class: "m-ep-no" }, ep.number != null ? String(ep.number) : "–");
  no.setAttribute("aria-label", ep.number != null ? `Folge ${ep.number}` : "ohne Nummer");
  return h("li", null, h("a", { class: "tr-item m-ep", href: `/episodes/${encodeURIComponent(ep.id)}` }, no, text));
}

function render(items) {
  clear(root);
  if (!items.length) {
    root.append(h("div", { class: "m-empty" },
      h("p", null, "Noch keine Folgen"),
      h("p", { class: "tr-small" }, "Aufgenommen wird im ", h("a", { href: "/studio" }, "Studio"),
        ". Die bisherigen Folgen lassen sich unter ", h("a", { href: "/settings#import" }, "Einstellungen"), " aus dem alten Feed übernehmen.")));
    return;
  }
  const list = h("ul", { class: "tr-list" });
  items.forEach((ep) => list.append(row(ep)));
  root.append(list);
}

async function load() {
  const items = await api("/episodes");
  render(items);
  return items.some((e) => e.job || e.status === "processing" || e.status === "uploading" || e.status === "recording");
}

async function start() {
  try {
    const busy = await load();
    if (busy) stop = poll(load, 3000);
  } catch (e) {
    clear(root);
    root.append(h("p", { class: "tr-error", role: "alert" }, `Die Liste konnte nicht geladen werden. ${errorText(e)}`));
  }
}
start();
