// Folgenliste: Nummer, Titel, Status als Text, Dauer, Datum; laufende Verarbeitung mit Fortschritt (Polling).
import { $, api, clear, fmtDate, fmtDateTime, fmtDuration, fmtInt, h, errorText, poll, progressBar, rich, statusClass, statusText, t } from "./common.js";

const root = $("#app");
function jobLabel(kind) {
  switch (kind) {
    case "process": return t("Verarbeitung");
    case "remix": return t("Neu mischen");
    case "publish": return t("Veröffentlichung");
    case "unpublish": return t("Zurückziehen");
    case "suggest": return t("Vorschlag");
    case "transcribe": return t("Transkript");
    default: return t("Aufgabe");
  }
}
let stop = null;
let downloads = { mode: "off", totals: {} }; // Zählung der Abrufe (SPEC Abschnitt 15)

function downloadsText(ep) {
  if (ep.status !== "published") return null;
  const n = downloads.totals[ep.id] || 0;
  if (!n && downloads.mode === "off") return null;
  return n === 1 ? t("{n} Download", { n: fmtInt(n) }) : t("{n} Downloads", { n: fmtInt(n) });
}

function dateText(ep) {
  if (ep.status === "published" && ep.published_at) return t("veröffentlicht {date}", { date: fmtDate(ep.published_at) });
  if (ep.status === "scheduled" && ep.publish_at) return t("geplant für {date}", { date: fmtDateTime(ep.publish_at) });
  return ep.created_at ? t("angelegt {date}", { date: fmtDate(ep.created_at) }) : "";
}

function row(ep) {
  const parts = [
    h("span", { class: statusClass(ep.status) }, statusText(ep.status)),
    ep.duration_ms ? h("span", { class: "m-num" }, fmtDuration(ep.duration_ms)) : null,
    h("span", { class: "m-num" }, dateText(ep)),
    downloadsText(ep) ? h("span", { class: "m-num" }, downloadsText(ep)) : null,
  ].filter(Boolean);
  const meta = h("p", { class: "m-meta" });
  parts.forEach((p, i) => {
    if (i) meta.append(h("span", { class: "m-sep", "aria-hidden": "true" }, "·"));
    meta.append(p);
  });
  if (ep.source === "imported") meta.append(h("span", { class: "m-sep", "aria-hidden": "true" }, "·"), h("span", null, t("importiert")));
  const text = h("div", { class: "m-ep-text" },
    h("strong", { class: "m-ep-title" }, ep.title || t("Ohne Titel")), meta);
  if (ep.job) {
    const j = ep.job;
    const label = jobLabel(j.kind);
    const state = j.status === "queued" ? t("wartet") : j.message || t("läuft");
    text.append(
      h("div", { class: "m-job" },
        h("p", { class: "tr-small" }, j.status === "running" ? t("{label}: {state}, {pct} %", { label, state, pct: Math.round((j.progress || 0) * 100) }) : t("{label}: {state}", { label, state })),
        j.status === "running" ? progressBar(j.progress, t("{label}, Fortschritt", { label })) : null));
  }
  const no = h("span", { class: "m-ep-no" }, ep.number != null ? String(ep.number) : "–");
  no.setAttribute("aria-label", ep.number != null ? t("Folge {n}", { n: ep.number }) : t("ohne Nummer"));
  return h("li", null, h("a", { class: "tr-item m-ep", href: `/episodes/${encodeURIComponent(ep.id)}` }, no, text));
}

function render(all) {
  clear(root);
  // Vorbereitungen (Zustand draft) stehen unter „Vorbereitung“, nicht in der Liste der Folgen
  const preps = all.filter((e) => e.status === "draft");
  const items = all.filter((e) => e.status !== "draft");
  if (preps.length) {
    root.append(h("p", { class: "m-lead" }, rich(preps.length === 1 ? t("Eine Vorbereitung ist in Arbeit: {link}") : t("{n} Vorbereitungen sind in Arbeit: {link}", { n: preps.length }), {
      link: h("a", { href: "/plan" }, t("zur Vorbereitung")),
    })));
  }
  if (!items.length) {
    root.append(h("div", { class: "m-empty" },
      h("p", null, t("Noch keine Folgen")),
      h("p", { class: "tr-small" }, rich(t("Aufgenommen wird im {studio}. Die bisherigen Folgen lassen sich unter {settings} aus dem alten Feed übernehmen."), {
        studio: h("a", { href: "/studio" }, t("Studio")),
        settings: h("a", { href: "/settings#import" }, t("Einstellungen")),
      }))));
    return;
  }
  const list = h("ul", { class: "tr-list" });
  items.forEach((ep) => list.append(row(ep)));
  root.append(list);
}

async function load() {
  const [items, stats] = await Promise.all([api("/episodes"), api("/stats/downloads").catch(() => null)]);
  if (stats) downloads = stats;
  render(items);
  return items.some((e) => e.job || e.status === "processing" || e.status === "uploading" || e.status === "recording");
}

async function start() {
  try {
    const busy = await load();
    if (busy) stop = poll(load, 3000);
  } catch (e) {
    clear(root);
    root.append(h("p", { class: "tr-error", role: "alert" }, t("Die Liste konnte nicht geladen werden. {message}", { message: errorText(e) })));
  }
}
start();
