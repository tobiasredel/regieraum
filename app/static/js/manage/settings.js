// Einstellungen: Show-Daten, Cover, Klang, Einladungen, Import, Feed-Vorschau, Systemstatus.
import {
  $, api, button, clear, confirmDialog, copyText, errorText, field, fmtBytes, fmtDateTime, fmtUsd, h, icon, LANG, poll, progressBar, t, toast,
} from "./common.js";

const root = $("#app");
let settings = null;
let invites = [];

const section = (key, title, ...c) => h("section", { class: "m-section", id: key, "aria-labelledby": `${key}-h` }, h("h2", { class: "m-h2", id: `${key}-h` }, title), ...c);
const input = (type, value, extra = {}) => h("input", { class: "tr-input", type, value: value ?? "", ...extra });

async function start() {
  try {
    [settings, invites] = await Promise.all([api("/settings"), api("/invites")]);
  } catch (e) {
    clear(root);
    root.append(h("p", { class: "tr-error", role: "alert" }, t("Die Einstellungen konnten nicht geladen werden. {message}", { message: errorText(e) })));
    return;
  }
  render();
  loadStatus();
  const resume = safeSession("get");
  if (resume) watchImport(resume);
  if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();
}

function safeSession(op, value) {
  try {
    if (op === "get") return sessionStorage.getItem("ps-import-job");
    if (op === "set") sessionStorage.setItem("ps-import-job", value);
    if (op === "del") sessionStorage.removeItem("ps-import-job");
  } catch { /* privater Modus */ }
  return null;
}

function render() {
  clear(root);
  const toc = h("nav", { class: "m-toc", "aria-label": t("Auf dieser Seite") },
    ...[["sprache", t("Sprache")], ["show", t("Show")], ["format", t("Sendung und Format")], ["cover", t("Cover")], ["klang", t("Klang")], ["archiv", t("Rohdaten und Archiv")], ["einladungen", t("Einladungen")], ["import", t("Import")], ["feed", t("Feed")], ["nummern", t("Nummerierung")], ["seite", t("Öffentliche Seite")], ["downloads", t("Downloads")], ["status", t("Systemstatus")]]
      .map(([k, label]) => h("a", { href: `#${k}` }, label)));
  root.append(toc, languageSection(), showSection(), formatSection(), coverSection(), audioSection(), archiveSection(), inviteSection(), importSection(), feedSection(), numberingSection(), siteSection(), statsSection(), h("section", { class: "m-section", id: "status" }, h("h2", { class: "m-h2" }, t("Systemstatus")), h("div", { id: "m-status" }, h("p", { class: "tr-small" }, t("Status wird geladen")))));
}

// --- Sprache ------------------------------------------------------------------------------------
function languageSection() {
  const current = (settings.ui && settings.ui.language) || LANG;
  // Die Namen der Sprachen werden nicht übersetzt: Wer die Sprache nicht versteht, findet sie trotzdem
  const sel = h("select", { class: "tr-input m-select", id: "m-ui-language" },
    h("option", { value: "de", selected: current === "de" }, "Deutsch"),
    h("option", { value: "en", selected: current === "en" }, "English"));
  const state = h("span", { class: "m-save-state", role: "status" });
  const save = async () => {
    try {
      settings = await api("/settings", { method: "PUT", json: { ui: { language: sel.value } } });
      state.textContent = t("Gespeichert");
      state.className = "m-save-state tr-status-ok";
      if (sel.value !== LANG) location.reload();
    } catch (e) {
      state.textContent = errorText(e);
      state.className = "m-save-state tr-status-error";
    }
  };
  return section("sprache", t("Sprache"),
    field(t("Sprache der Oberfläche"), sel, { id: "m-ui-language", hint: t("Die Seite wird nach dem Speichern neu geladen") }),
    h("div", { class: "app-row m-ed-save" }, button(t("Speichern"), { onclick: save }), state));
}

// --- Show ---------------------------------------------------------------------------------------
function showSection() {
  const s = settings.show;
  const f = {
    title: input("text", s.title, { maxlength: "200" }),
    author: input("text", s.author, { maxlength: "200" }),
    owner_name: input("text", s.owner_name, { maxlength: "200" }),
    owner_email: input("email", s.owner_email, { maxlength: "200", autocomplete: "off" }),
    description: h("textarea", { class: "tr-input", rows: 5, maxlength: "4000" }),
    language: input("text", s.language, { maxlength: "10" }),
    category: input("text", s.category, { maxlength: "100" }),
    subcategory: input("text", s.subcategory, { maxlength: "100" }),
    link: input("url", s.link, { maxlength: "500" }),
    copyright: input("text", s.copyright, { maxlength: "300" }),
    explicit: h("input", { type: "checkbox", checked: !!s.explicit, id: "m-show-explicit" }),
  };
  f.description.value = s.description || "";
  const state = h("span", { class: "m-save-state", role: "status" });
  const save = async () => {
    const body = {};
    for (const [k, el] of Object.entries(f)) body[k] = el.type === "checkbox" ? el.checked : el.value.trim();
    if (!body.title) { state.textContent = t("Der Titel darf nicht leer sein"); state.className = "m-save-state tr-status-error"; return; }
    try {
      settings = await api("/settings", { method: "PUT", json: { show: body } });
      state.textContent = t("Gespeichert");
      state.className = "m-save-state tr-status-ok";
    } catch (e) {
      state.textContent = errorText(e);
      state.className = "m-save-state tr-status-error";
    }
  };
  return section("show", t("Show"),
    h("p", { class: "m-lead" }, t("Diese Angaben stehen im Feed und erscheinen in den Podcast-Apps")),
    h("div", { class: "m-grid2" },
      field(t("Titel der Show"), f.title),
      field(t("Autor"), f.author, { hint: t("Wird in den Apps als Urheber angezeigt") }),
      field(t("Inhaber"), f.owner_name),
      field(t("E-Mail des Inhabers"), f.owner_email, { hint: t("Darüber prüft Spotify die Inhaberschaft") })),
    field(t("Beschreibung"), f.description),
    h("div", { class: "m-grid3" },
      field(t("Sprache"), f.language, { hint: t("Kürzel, zum Beispiel de") }),
      field(t("Kategorie"), f.category),
      field(t("Unterkategorie"), f.subcategory)),
    h("div", { class: "m-grid2" },
      field(t("Webseite"), f.link),
      field(t("Urheberrecht"), f.copyright)),
    h("label", { class: "m-check", for: "m-show-explicit" }, f.explicit, h("span", null, t("Die Show enthält explizite Inhalte"))),
    h("div", { class: "app-row m-ed-save" }, button(t("Show-Daten speichern"), { onclick: save }), state));
}

// --- Sendung und Format ---------------------------------------------------------------------------
const PLAN_ENTRIES = ["blitzlicht", "hauptthema", "good:read", "good:watch", "good:listen", "frei"];

function formatSection() {
  const p = settings.profile || {};
  const fmt = settings.format || { labels: {}, plan: [], short_minutes: 3 };
  const l = fmt.labels;
  const f = {
    host_name: input("text", p.host_name, { maxlength: "60", autocomplete: "off" }),
    cohost_name: input("text", p.cohost_name, { maxlength: "60", autocomplete: "off" }),
    genre: input("text", p.genre, { maxlength: "200" }),
    tone: input("text", p.tone, { maxlength: "200" }),
  };
  const names = {};
  for (const k of ["blitzlicht", "hauptthema", "good", "frei", "read", "watch", "listen"]) names[k] = input("text", l[k], { maxlength: "40" });
  const minutes = input("number", fmt.short_minutes, { min: "1", max: "30", step: "1", inputmode: "numeric" });
  const plan = [...fmt.plan];
  const entryName = (e) => ({ blitzlicht: names.blitzlicht.value, hauptthema: names.hauptthema.value, "good:read": names.read.value, "good:watch": names.watch.value, "good:listen": names.listen.value, frei: names.frei.value })[e] || e;
  const list = h("ol", { class: "m-planlist" });
  const drawPlan = () => {
    clear(list);
    plan.forEach((entry, i) => {
      const sel = h("select", { class: "tr-input m-select", "aria-label": t("Abschnitt {n}", { n: i + 1 }) });
      PLAN_ENTRIES.forEach((e) => sel.append(h("option", { value: e, selected: e === entry }, entryName(e))));
      sel.addEventListener("change", () => { plan[i] = sel.value; });
      list.append(h("li", null, sel,
        button("", { icon: "trash", ariaLabel: t("Abschnitt {n} entfernen", { n: i + 1 }), title: t("Entfernen"), disabled: plan.length <= 1, onclick: () => { plan.splice(i, 1); drawPlan(); } })));
    });
  };
  drawPlan();
  Object.values(names).forEach((el) => el.addEventListener("change", drawPlan));
  const state = h("span", { class: "m-save-state", role: "status" });
  const save = async () => {
    const labels = {};
    for (const [k, el] of Object.entries(names)) { if (el.value.trim()) labels[k] = el.value.trim(); }
    const profile = {};
    for (const [k, el] of Object.entries(f)) profile[k] = el.value.trim();
    const mins = Math.round(Number(minutes.value));
    if (!(mins >= 1 && mins <= 30)) { state.textContent = t("Die Dauer muss zwischen 1 und 30 Minuten liegen"); state.className = "m-save-state tr-status-error"; return; }
    try {
      settings = await api("/settings", { method: "PUT", json: { profile, format: { labels, short_minutes: mins, plan } } });
      state.textContent = t("Gespeichert");
      state.className = "m-save-state tr-status-ok";
      const box = document.getElementById("m-invites");
      if (box) drawInvites(box);
    } catch (e) {
      state.textContent = errorText(e);
      state.className = "m-save-state tr-status-error";
    }
  };
  return section("format", t("Sendung und Format"),
    h("p", { class: "m-lead" }, t("Wer spricht, worum es geht und woraus eine Folge besteht. Vorbereitung, Begleiter und Shownotes richten sich danach.")),
    h("div", { class: "m-grid-pair" },
      field(t("Rufname des Hosts"), f.host_name, { hint: t("Wer die App bedient und die Karten liest") }),
      field(t("Rufname des Co-Hosts"), f.cohost_name, { hint: t("Trägt den Dauerlink ins Studio") }),
      field(t("Thema der Show"), f.genre, { hint: t("Zum Beispiel Politik und Zeitgeschehen") }),
      field(t("Ton der Show"), f.tone, { hint: t("Zum Beispiel trocken, neugierig, humorvoll") })),
    h("h3", { class: "m-h3" }, t("Abschnittsarten")),
    h("p", { class: "tr-small" }, t("Vier Arten mit festem Verhalten, die Namen sind frei. Ein kurzes Thema bekommt ein knappes Dossier, das Hauptthema ein ausführliches, eine Empfehlung stellt ein Werk vor.")),
    h("div", { class: "m-grid-pair" },
      field(t("Kurzes Thema"), names.blitzlicht),
      field(t("Hauptthema"), names.hauptthema),
      field(t("Freier Abschnitt"), names.frei),
      field(t("Empfehlung"), names.good),
      field(t("Empfehlung zum Lesen"), names.read),
      field(t("Empfehlung zum Sehen"), names.watch),
      field(t("Empfehlung zum Hören"), names.listen),
      field(t("Dauer eines kurzen Themas in Minuten"), minutes)),
    h("h3", { class: "m-h3" }, t("Plan einer neuen Folge")),
    h("p", { class: "tr-small" }, t("Jede neue Folge beginnt mit diesen Abschnitten. In der Vorbereitung lässt sich der Plan je Folge ändern.")),
    list,
    h("div", { class: "app-row" }, button(t("Abschnitt hinzufügen"), { icon: "plus", onclick: () => { if (plan.length < 12) { plan.push("frei"); drawPlan(); } } })),
    h("div", { class: "app-row m-ed-save" }, button(t("Sendung und Format speichern"), { onclick: save }), state));
}

// --- Cover --------------------------------------------------------------------------------------
function coverSection() {
  const img = h("img", { class: "m-cover-img", alt: t("Cover der Show"), width: 200, height: 200 });
  const none = h("div", { class: "m-cover-none", role: "img", "aria-label": t("Kein Cover") }, t("Noch kein Cover"));
  const frame = h("div", { class: "m-cover" });
  const state = h("p", { class: "m-save-state", role: "status" });
  const showImg = () => {
    clear(frame);
    if (settings.show.cover_key) {
      img.onerror = () => { clear(frame); frame.append(none); };
      img.src = `/api/host/settings/cover?v=${encodeURIComponent(settings.show.cover_key)}`;
      frame.append(img);
    } else frame.append(none);
  };
  const file = h("input", { type: "file", accept: "image/jpeg,image/png", class: "app-vh", id: "m-cover-file", tabindex: "-1" });
  file.addEventListener("change", async () => {
    const f = file.files[0];
    file.value = "";
    if (!f) return;
    state.className = "m-save-state";
    state.textContent = t("Wird geprüft");
    const dims = await new Promise((res) => {
      const url = URL.createObjectURL(f);
      const im = new Image();
      im.onload = () => { res([im.naturalWidth, im.naturalHeight]); URL.revokeObjectURL(url); };
      im.onerror = () => { res(null); URL.revokeObjectURL(url); };
      im.src = url;
    });
    if (dims && (dims[0] !== dims[1] || dims[0] < 1400 || dims[0] > 3000)) {
      state.textContent = t("Das Cover ist {w} × {h} px. Gefordert ist quadratisch, 1400 bis 3000 px.", { w: dims[0], h: dims[1] });
      state.className = "m-save-state tr-status-error";
      return;
    }
    const form = new FormData();
    form.append("file", f);
    try {
      settings = await api("/settings/cover", { method: "POST", form });
      showImg();
      state.textContent = t("Cover gespeichert");
      state.className = "m-save-state tr-status-ok";
    } catch (e) {
      state.textContent = errorText(e);
      state.className = "m-save-state tr-status-error";
    }
  });
  showImg();
  return section("cover", t("Cover"),
    h("div", { class: "m-cover-row" }, frame,
      h("div", { class: "m-cover-side" },
        h("p", null, t("Quadratisch, 1400 bis 3000 Pixel Kantenlänge, JPEG oder PNG, höchstens 10 MB")),
        h("p", { class: "tr-small" }, t("Das Cover erscheint im Feed und in den Podcast-Apps und wird in jede neue Folge eingebettet")),
        h("div", { class: "app-row" }, button(t("Cover wählen"), { icon: "upload", onclick: () => file.click() }), file), state)));
}

// --- Klang --------------------------------------------------------------------------------------
const denoiseLevels = () => [["off", t("Aus")], ["leicht", t("Leicht")], ["normal", t("Normal")], ["stark", t("Stark")]];

function audioSection() {
  const a = settings.audio;
  const lufs = input("number", a.target_lufs, { step: "0.5", min: "-30", max: "-10", inputmode: "decimal" });
  const crossgate = h("input", { type: "checkbox", checked: !!a.crossgate, id: "m-crossgate" });
  const rates = [96, 128, 160, 192, 256, 320];
  if (!rates.includes(a.mp3_kbps)) rates.push(a.mp3_kbps);
  rates.sort((x, y) => x - y);
  const kbps = h("select", { class: "tr-input m-select" });
  rates.forEach((r) => kbps.append(h("option", { value: r, selected: r === a.mp3_kbps }, t("{n} kbit/s", { n: r }))));
  const radios = h("div", { class: "m-radios", role: "radiogroup", "aria-labelledby": "m-dn-label" });
  denoiseLevels().forEach(([v, name]) => radios.append(h("label", { class: "m-check", for: `m-dn-${v}` }, h("input", { type: "radio", name: "m-dn", id: `m-dn-${v}`, value: v, checked: a.denoise === v }), h("span", null, name))));
  const state = h("span", { class: "m-save-state", role: "status" });
  const save = async () => {
    const v = parseFloat(lufs.value);
    if (Number.isNaN(v) || v < -30 || v > -10) { state.textContent = t("Die Ziel-Lautheit muss zwischen −30 und −10 LUFS liegen"); state.className = "m-save-state tr-status-error"; return; }
    const dn = radios.querySelector("input:checked")?.value || "normal";
    try {
      settings = await api("/settings", { method: "PUT", json: { audio: { target_lufs: v, denoise: dn, crossgate: crossgate.checked, mp3_kbps: parseInt(kbps.value, 10) } } });
      state.textContent = t("Gespeichert");
      state.className = "m-save-state tr-status-ok";
    } catch (e) {
      state.textContent = errorText(e);
      state.className = "m-save-state tr-status-error";
    }
  };
  return section("klang", t("Klang"),
    h("p", { class: "m-lead" }, t("Änderungen gelten ab der nächsten Verarbeitung oder dem nächsten Neumischen einer Folge")),
    field(t("Ziel-Lautheit in LUFS"), lufs, { hint: t("−16 LUFS ist der übliche Podcast-Wert, Spotify empfiehlt −14. Je höher der Wert, desto lauter die Folge.") }),
    h("div", { class: "tr-field m-field" }, h("span", { class: "tr-label", id: "m-dn-label" }, t("Rauschunterdrückung")), radios,
      h("p", { class: "tr-small m-hint" }, t("Je stärker die Stufe, desto mehr Rauschen verschwindet, desto eher können Stimmen dünner klingen"))),
    h("div", { class: "tr-field m-field" }, h("span", { class: "tr-label" }, "Crossgate"),
      h("label", { class: "m-check", for: "m-crossgate" }, crossgate, h("span", null, t("Übersprechen absenken"))),
      h("p", { class: "tr-small m-hint" }, t("Spricht nur eine Person, wird das Übersprechen in den Spuren der anderen abgesenkt, höchstens um 15 dB. Das schützt Aufnahmen ohne Kopfhörer."))),
    field(t("MP3-Bitrate"), kbps, { id: "m-kbps", hint: t("160 kbit/s ist für Sprache ein guter Standard") }),
    h("div", { class: "app-row m-ed-save" }, button(t("Klang speichern"), { onclick: save }), state));
}

// --- Archiv -------------------------------------------------------------------------------------
function archiveSection() {
  const a = settings.archive || { after_days: 14, available: false };
  const days = input("number", a.after_days, { min: "0", max: "3650", inputmode: "numeric", id: "m-arch-days", disabled: !a.available });
  const state = h("span", { class: "m-save-state", role: "status" });
  const save = async () => {
    const v = parseInt(days.value, 10);
    if (Number.isNaN(v) || v < 0 || v > 3650) { state.textContent = t("Die Frist muss zwischen 0 und 3650 Tagen liegen"); state.className = "m-save-state tr-status-error"; return; }
    try {
      settings = await api("/settings", { method: "PUT", json: { archive: { after_days: v } } });
      state.textContent = t("Gespeichert");
      state.className = "m-save-state tr-status-ok";
    } catch (e) {
      state.textContent = errorText(e);
      state.className = "m-save-state tr-status-error";
    }
  };
  return section("archiv", t("Rohdaten und Archiv"),
    h("p", { class: "m-lead" }, t("Rohspuren, Zwischenstände und Mischungen belegen etwa 1 bis 2 GB je Stunde Aufnahme. Veröffentlichte Folgen können sie nach einer Frist ins Archiv verschieben. Die fertige Folge, Wellenform, Transkript und Kapitel bleiben lokal.")),
    field(t("Rohdaten ins Archiv, Tage nach dem Veröffentlichen"), days, { id: "m-arch-days", hint: a.available ? t("0 verschiebt sofort nach dem Veröffentlichen, Vorgabe sind 14 Tage") : t("Es ist kein Archiv eingerichtet. Die Umgebungsvariable PS_ARCHIVE_DIR muss auf einen Ordner im Container zeigen") }),
    h("div", { class: "app-row m-ed-save" }, button(t("Frist speichern"), { onclick: save, disabled: !a.available }), state));
}

// --- Einladungen --------------------------------------------------------------------------------
function inviteSection() {
  const box = h("div", { id: "m-invites" });
  const sec = section("einladungen", t("Einladungen"), box);
  drawInvites(box);
  return sec;
}

function linkRow(url, labelText) {
  const field_ = h("input", { class: "tr-input m-url", type: "text", readonly: true, value: url, "aria-label": labelText, onfocus: (e) => e.target.select() });
  const copy = button(t("Kopieren"), { icon: "copy", onclick: async () => toast((await copyText(url, field_)) ? t("Link kopiert") : t("Kopieren nicht möglich, Link markiert"), { error: false }) });
  return h("div", { class: "m-linkrow" }, field_, copy);
}

function inviteState(inv) {
  if (inv.revoked_at) return [t("widerrufen"), ""];
  if (!inv.active) return [t("abgelaufen"), ""];
  if (inv.kind === "single" && inv.uses > 0) return [t("benutzt, gültig bis {date}", { date: fmtDateTime(inv.expires_at) }), "tr-status-ok"];
  return [inv.expires_at ? t("gültig bis {date}", { date: fmtDateTime(inv.expires_at) }) : t("gültig"), "tr-status-ok"];
}

/** Wem der Dauerlink gehört: Co-Host aus dem Show-Profil, sonst die Bezeichnung des Links. */
function cohost(permanent) {
  return (settings.profile && settings.profile.cohost_name) || (permanent && permanent.label) || t("den Co-Host");
}

function drawInvites(box) {
  clear(box);
  const perm = invites.filter((i) => i.kind === "permanent" && i.active);
  const permanent = perm[0];
  const who = cohost(permanent);
  box.append(h("h3", { class: "m-h3" }, t("Dauerlink für {who}", { who })));
  if (permanent) {
    box.append(
      h("p", { class: "tr-small" }, t("Dieser Link führt {who} ohne Anmeldung ins Studio und bleibt gültig, bis er erneuert wird", { who })),
      linkRow(permanent.url, t("Dauerlink für {who}", { who })),
      h("div", { class: "app-row" }, button(t("Erneuern"), { icon: "refresh", onclick: () => renewPermanent(perm, box) })),
      h("p", { class: "tr-small" }, t("Beim Erneuern wird der bisherige Link ungültig und ein neuer muss verschickt werden")));
  } else {
    box.append(h("p", { class: "tr-small" }, t("Es gibt keinen gültigen Dauerlink")), h("div", { class: "app-row" }, button(t("Dauerlink erzeugen"), { onclick: () => renewPermanent([], box) })));
  }

  const name = h("input", { class: "tr-input", type: "text", maxlength: "60", placeholder: t("Name des Gastes"), autocomplete: "off" });
  const created = h("div", { class: "m-created" });
  const make = async () => {
    const label = name.value.trim();
    if (!label) { name.focus(); toast(t("Bitte den Namen des Gastes eintragen"), { error: true }); return; }
    try {
      const inv = await api("/invites", { method: "POST", json: { label, kind: "single" } });
      invites = await api("/invites");
      drawInvites(box);
      const c = $("#m-created");
      if (c) { c.append(h("p", null, t("Einmal-Link für {label}, 48 Stunden gültig", { label: inv.label })), linkRow(inv.url, t("Einmal-Link für {label}", { label: inv.label }))); }
    } catch (e) {
      toast(errorText(e), { error: true });
    }
  };
  created.id = "m-created";
  box.append(
    h("h3", { class: "m-h3 m-h3-gap" }, t("Einmal-Link für Gäste")),
    h("p", { class: "tr-small" }, t("Ein Einmal-Link gilt für eine Person und läuft nach 48 Stunden oder nach dem Widerruf ab")),
    h("div", { class: "m-linkrow" }, field(t("Name des Gastes"), name, { cls: "m-grow" }), button(t("Einmal-Link erzeugen"), { onclick: make, cls: "m-align-end" })),
    created);
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); make(); } });

  const singles = invites.filter((i) => i.kind === "single");
  box.append(h("h3", { class: "m-h3 m-h3-gap" }, t("Ausgestellte Einmal-Links")));
  if (!singles.length) {
    box.append(h("p", { class: "tr-small" }, t("Noch keine Einmal-Links ausgestellt")));
  } else {
    const ul = h("ul", { class: "tr-list" });
    singles.slice(0, 15).forEach((inv) => {
      const [txt, cls] = inviteState(inv);
      ul.append(h("li", null, h("div", { class: "tr-item m-track" },
        h("div", { class: "m-ep-text" }, h("strong", { class: "m-ep-title" }, inv.label), h("p", { class: "m-meta" }, h("span", { class: cls }, txt))),
        inv.active ? h("div", { class: "m-sound-actions" },
          button(t("Kopieren"), { icon: "copy", ariaLabel: t("Link für {label} kopieren", { label: inv.label }), onclick: async () => toast((await copyText(inv.url)) ? t("Link kopiert") : t("Kopieren nicht möglich"), {}) }),
          button(t("Widerrufen"), { onclick: () => revoke(inv, box), ariaLabel: t("Link für {label} widerrufen", { label: inv.label }) })) : null)));
    });
    box.append(ul);
  }
}

async function renewPermanent(old, box) {
  if (old.length) {
    const ok = await confirmDialog({ title: t("Dauerlink erneuern"), text: t("Der bisherige Link wird ungültig. Der neue Link muss verschickt und einmal geöffnet werden."), ok: t("Erneuern") });
    if (!ok) return;
  }
  try {
    await api("/invites", { method: "POST", json: { label: (settings.profile && settings.profile.cohost_name) || old[0]?.label || "Co-Host", kind: "permanent" } });
    for (const o of old) await api(`/invites/${encodeURIComponent(o.id)}`, { method: "DELETE" });
    invites = await api("/invites");
    drawInvites(box);
    toast(t("Neuer Dauerlink erzeugt"));
  } catch (e) {
    toast(errorText(e), { error: true });
  }
}

async function revoke(inv, box) {
  const ok = await confirmDialog({ title: t("Link widerrufen"), text: t("Der Einmal-Link für {label} wird sofort ungültig", { label: inv.label }), ok: t("Widerrufen"), danger: true });
  if (!ok) return;
  try {
    await api(`/invites/${encodeURIComponent(inv.id)}`, { method: "DELETE" });
    invites = await api("/invites");
    drawInvites(box);
  } catch (e) {
    toast(errorText(e), { error: true });
  }
}

// --- Import -------------------------------------------------------------------------------------
function importSection() {
  const url = input("url", "", { placeholder: "https://…", autocomplete: "off", inputmode: "url" });
  const out = h("div", { id: "m-import-out", class: "m-import-out" });
  const go = button(t("Importieren"), { onclick: async () => {
    const v = url.value.trim();
    if (!/^https?:\/\//i.test(v)) { out.replaceChildren(h("p", { class: "tr-error", role: "alert" }, t("Die Feed-Adresse muss mit http:// oder https:// beginnen"))); return; }
    try {
      const r = await api("/import", { method: "POST", json: { feed_url: v } });
      safeSession("set", r.job_id);
      watchImport(r.job_id);
    } catch (e) {
      out.replaceChildren(h("p", { class: "tr-error", role: "alert" }, errorText(e)));
    }
  } });
  return section("import", t("Import des bisherigen Feeds"),
    h("p", { class: "m-lead" }, t("Übernimmt alle Folgen des alten Podcast-Feeds mit unveränderter Kennung (GUID), samt Audio, Show-Daten und Cover. Der Import lässt sich wiederholen, vorhandene Folgen werden übersprungen.")),
    h("div", { class: "m-linkrow" }, field(t("Adresse des alten Feeds"), url, { cls: "m-grow", id: "m-import-url" }), go),
    out);
}

let stopImport = null;
function watchImport(jobId) {
  stopImport?.();
  const out = $("#m-import-out");
  if (!out) return;
  const draw = (j) => {
    clear(out);
    if (j.status === "queued" || j.status === "running") {
      out.append(h("p", null, j.status === "queued" ? t("Der Import wartet auf den Start") : t("{state}, {pct} %", { state: j.message || t("Import läuft"), pct: Math.round((j.progress || 0) * 100) })), progressBar(j.progress, t("Import, Fortschritt")));
    } else if (j.status === "done") {
      out.append(h("p", { class: "tr-status-ok" }, t("Import abgeschlossen")), j.message ? h("p", null, j.message) : null, h("p", null, h("a", { href: "/episodes" }, t("Zu den Folgen"))));
    } else {
      out.append(h("p", { class: "tr-status-error", role: "alert" }, t("Der Import ist fehlgeschlagen")), h("p", null, j.message || ""),
        j.log ? h("details", { class: "m-details" }, h("summary", null, t("Protokoll anzeigen")), h("pre", { class: "m-pre" }, j.log)) : null);
    }
  };
  stopImport = poll(async () => {
    const j = await api(`/jobs/${encodeURIComponent(jobId)}`);
    draw(j);
    if (j.status === "done" || j.status === "failed") { safeSession("del"); return false; }
    return true;
  }, 1500);
  api(`/jobs/${encodeURIComponent(jobId)}`).then(draw).catch(() => safeSession("del"));
}

// --- Feed-Vorschau --------------------------------------------------------------------------------
function feedSection() {
  const pre = h("pre", { class: "m-pre", tabindex: "0", "aria-label": t("Feed als Text") });
  const det = h("details", { class: "m-details" }, h("summary", null, t("Feed-Vorschau anzeigen")), pre);
  det.addEventListener("toggle", async () => {
    if (!det.open) return;
    pre.textContent = t("Wird geladen");
    try {
      pre.textContent = await api("/feed/preview");
    } catch (e) {
      pre.textContent = errorText(e);
    }
  });
  return section("feed", t("Feed"),
    h("p", { class: "m-lead" }, t("So sähe der Feed aus, wenn er jetzt veröffentlicht würde")),
    det,
    h("p", null, h("a", { href: "/api/host/feed/preview", target: "_blank", rel: "noopener" }, t("Feed in neuem Tab öffnen"))));
}

// --- Öffentliche Seite -------------------------------------------------------------------------------
const MAX_PLATFORMS = 6;

function siteSection() {
  const site = settings.site || { enabled: true, platforms: [] };
  const on = h("input", { type: "checkbox", checked: !!site.enabled, id: "m-site-on" });
  const platforms = site.platforms.map((p) => ({ ...p }));
  const list = h("div", { class: "m-plat-list" });
  const addBtn = button(t("Link hinzufügen"), { icon: "plus", onclick: () => { if (platforms.length < MAX_PLATFORMS) { platforms.push({ name: "", url: "" }); draw(); list.querySelector(".m-plat-row:last-child input")?.focus(); } } });
  const draw = () => {
    clear(list);
    platforms.forEach((p, i) => {
      const name = input("text", p.name, { maxlength: "60", placeholder: t("Name, zum Beispiel Apple Podcasts") });
      const url = input("url", p.url, { maxlength: "500", placeholder: "https://…", inputmode: "url", autocomplete: "off" });
      name.addEventListener("input", () => { p.name = name.value; });
      url.addEventListener("input", () => { p.url = url.value; });
      list.append(h("div", { class: "m-plat-row" },
        field(t("Name"), name, { id: `m-plat-name-${i}` }),
        field(t("Adresse"), url, { id: `m-plat-url-${i}` }),
        button("", { icon: "trash", ariaLabel: t("Link {n} entfernen", { n: i + 1 }), title: t("Entfernen"), onclick: () => { platforms.splice(i, 1); draw(); } })));
    });
    addBtn.hidden = platforms.length >= MAX_PLATFORMS;
  };
  draw();
  const address = h("div", { id: "m-site-address" });
  api("/status").then((s) => {
    if (!s.storage.public_base_url) return;
    const url = `${s.storage.public_base_url.replace(/\/$/, "")}/index.html`;
    const box = h("input", { class: "tr-input m-url", type: "text", readonly: true, value: url, onfocus: (e) => e.target.select() });
    address.append(field(t("Adresse der Seite"), box, { id: "m-site-url", hint: t("Die Adresse enthält index.html, weil manche Speicher unter der Domain allein keine Seite ausliefern") }),
      h("div", { class: "app-row" }, button(t("Seite öffnen"), { icon: "external", onclick: () => window.open(url, "_blank", "noopener") })));
  }).catch(() => {});
  const state = h("span", { class: "m-save-state", role: "status" });
  const save = async () => {
    state.className = "m-save-state";
    state.textContent = t("Speichert");
    try {
      const body = { enabled: on.checked, platforms: platforms.filter((p) => p.name.trim() || p.url.trim()).map((p) => ({ name: p.name.trim(), url: p.url.trim() })) };
      settings = await api("/settings", { method: "PUT", json: { site: body } });
      state.textContent = t("Gespeichert");
      state.className = "m-save-state tr-status-ok";
    } catch (e) {
      state.textContent = errorText(e);
      state.className = "m-save-state tr-status-error";
    }
  };
  return section("seite", t("Öffentliche Seite"),
    h("p", { class: "m-lead" }, t("Eine einfache Webseite zur Show mit Folgenliste und Player, ohne Zusatzdienste. Sie wird bei jedem Veröffentlichen neben dem Feed im Speicher aktualisiert.")),
    h("label", { class: "m-check", for: "m-site-on" }, on, h("span", null, t("Öffentliche Seite anlegen"))),
    h("p", { class: "tr-small m-hint" }, t("Ist unter Show keine eigene Webseite eingetragen, verweist der Feed auf diese Seite")),
    address,
    h("h3", { class: "m-h3 m-h3-gap" }, t("Links zu Podcast-Apps")),
    h("p", { class: "tr-small" }, t("Höchstens 6 Links, zum Beispiel zu Apple Podcasts und Spotify")),
    list,
    h("div", { class: "app-row" }, addBtn),
    h("div", { class: "app-row m-ed-save" }, button(t("Öffentliche Seite speichern"), { onclick: save }), state));
}

// --- Downloads zählen -------------------------------------------------------------------------------------
function statsSection() {
  const cur = settings.stats || { mode: "off", prefix: "" };
  const prefix = input("url", cur.prefix, { maxlength: "300", placeholder: "https://op3.dev/e/", inputmode: "url", autocomplete: "off" });
  const modes = [
    ["off", t("Aus"), t("Die Adressen im Feed zeigen direkt auf den Speicher")],
    ["app", t("Über diese App"), t("Die Adressen führen über diese App, die den Abruf zählt und weiterleitet. Gezählt wird je Folge und Tag einmal je Besucher, ohne IP-Adresse zu speichern.")],
    ["prefix", t("Über einen Zähldienst"), t("Vor jede Adresse kommt das Präfix eines Zähldienstes wie OP3, gezählt wird dort")],
  ];
  const radios = h("div", { class: "m-radios", role: "radiogroup", "aria-labelledby": "m-stats-label" });
  modes.forEach(([v, name, desc]) => radios.append(h("div", null,
    h("label", { class: "m-check", for: `m-stats-${v}` }, h("input", { type: "radio", name: "m-stats", id: `m-stats-${v}`, value: v, checked: cur.mode === v, "aria-describedby": `m-stats-${v}-d` }), h("span", null, name)),
    h("p", { class: "tr-small m-radio-desc", id: `m-stats-${v}-d` }, desc))));
  const prefixField = field(t("Präfix des Zähldienstes"), prefix, { id: "m-stats-prefix", hint: t("Zum Beispiel https://op3.dev/e/") });
  const sync = () => { prefixField.hidden = radios.querySelector("input:checked")?.value !== "prefix"; };
  radios.addEventListener("change", sync);
  sync();
  const state = h("span", { class: "m-save-state", role: "status" });
  const save = async () => {
    const mode = radios.querySelector("input:checked")?.value || "off";
    state.className = "m-save-state";
    state.textContent = t("Speichert");
    try {
      settings = await api("/settings", { method: "PUT", json: { stats: { mode, prefix: prefix.value.trim() } } });
      state.textContent = t("Gespeichert");
      state.className = "m-save-state tr-status-ok";
    } catch (e) {
      state.textContent = errorText(e);
      state.className = "m-save-state tr-status-error";
    }
  };
  return section("downloads", t("Downloads zählen"),
    h("p", { class: "m-lead" }, t("Ohne Spotify for Creators gibt es keine Hörerzahlen. Hier lassen sich Abrufe der Audiodateien zählen.")),
    h("div", { class: "tr-field m-field" }, h("span", { class: "tr-label", id: "m-stats-label" }, t("Zählung")), radios),
    prefixField,
    h("p", { class: "tr-small" }, t("Die Adressen der Audiodateien im Feed ändern sich; Podcast-Apps erkennen Folgen an ihrer Kennung und laden nichts neu")),
    h("div", { class: "app-row m-ed-save" }, button(t("Zählung speichern"), { onclick: save }), state));
}

// --- Nummerierung ------------------------------------------------------------------------------
function numberingSection() {
  const cur = settings.numbering || { season: "keep", prefix: "" };
  const modes = [
    ["keep", t("Von Hand"), t("Staffel und Nummer stehen so, wie sie auf der Folgenseite eingetragen sind")],
    ["year", t("Jahr als Staffel"), t("Die Staffel ist das Jahr der Veröffentlichung, die Nummer läuft über alle Jahre weiter")],
    ["none", t("Keine Staffeln"), t("Nur eine durchgehende Nummer")],
  ];
  const radios = h("div", { class: "m-radios", role: "radiogroup", "aria-labelledby": "m-num-label" });
  modes.forEach(([v, name, desc]) => radios.append(h("div", null,
    h("label", { class: "m-check", for: `m-num-${v}` }, h("input", { type: "radio", name: "m-num", id: `m-num-${v}`, value: v, checked: cur.season === v, "aria-describedby": `m-num-${v}-d` }), h("span", null, name)),
    h("p", { class: "tr-small m-radio-desc", id: `m-num-${v}-d` }, desc))));
  const prefix = input("text", cur.prefix, { maxlength: "40", placeholder: "#{number} ", autocomplete: "off", spellcheck: "false" });
  const example = h("p", { class: "tr-small m-created", "aria-live": "polite" });
  const sync = () => {
    const p = prefix.value;
    const shown = p ? p.replaceAll("{number}", "12").replaceAll("{season}", String(new Date().getFullYear())) : "";
    example.textContent = t("Im Feed: „{title}“", { title: shown + t("Titel der Folge") });
  };
  prefix.addEventListener("input", sync);
  sync();
  const state = h("span", { class: "m-save-state", role: "status" });
  const save = async () => {
    state.className = "m-save-state";
    state.textContent = t("Speichert");
    try {
      settings = await api("/settings", { method: "PUT", json: { numbering: { season: radios.querySelector("input:checked")?.value || "keep", prefix: prefix.value } } });
      state.textContent = t("Gespeichert");
      state.className = "m-save-state tr-status-ok";
    } catch (e) {
      state.textContent = errorText(e);
      state.className = "m-save-state tr-status-error";
    }
  };
  return section("nummern", t("Nummerierung"),
    h("p", { class: "m-lead" }, t("Neue Folgen bekommen beim Start der Aufnahme die nächste Nummer. Podcast-Apps zeigen Staffel und Folge aus dem Feed an.")),
    h("div", { class: "tr-field m-field" }, h("span", { class: "tr-label", id: "m-num-label" }, t("Staffel")), radios),
    field(t("Präfix vor dem Titel im Feed"), prefix, { id: "m-num-prefix", hint: t("Platzhalter {number} für die Nummer und {season} für die Staffel, leer für keinen. Der gespeicherte Titel bleibt ohne Präfix") }),
    example,
    h("div", { class: "app-row m-ed-save" }, button(t("Nummerierung speichern"), { onclick: save }), state),
    renumberBox());
}

function renumberBox() {
  const out = h("div", { class: "m-import-out", "aria-live": "polite" });
  const load = async () => {
    clear(out);
    out.append(h("p", { class: "tr-small" }, t("Vorschau wird geladen")));
    let res;
    try { res = await api("/episodes/renumber"); } catch (e) { clear(out); out.append(h("p", { class: "tr-error", role: "alert" }, errorText(e))); return; }
    clear(out);
    if (!res.changes.length) { out.append(h("p", { class: "tr-small" }, t("Alle {n} Folgen sind schon in der richtigen Reihenfolge nummeriert. Die nächste bekommt die {next}", { n: res.total, next: res.next_number }))); return; }
    const label = (v) => (v.number == null ? t("ohne Nummer") : v.season ? `${v.season} · ${v.number}` : String(v.number));
    const list = h("ul", { class: "m-plain" }, ...res.changes.slice(0, 12).map((c) => h("li", { class: "tr-small" },
      h("span", { class: "m-num" }, `${label(c.old)} → ${label(c.new)}`), "  ", c.title || t("Ohne Titel"))));
    const more = res.changes.length > 12 ? h("p", { class: "tr-small" }, t("und {n} weitere", { n: res.changes.length - 12 })) : "";
    const apply = async () => {
      const ok = await confirmDialog({ title: t("Folgen neu nummerieren?"), text: t("{n} von {total} Folgen bekommen eine neue Nummer oder Staffel. Der Feed wird danach neu gebaut; Podcast-Apps laden dadurch nichts neu.", { n: res.changes.length, total: res.total }), ok: t("Neu nummerieren") });
      if (!ok) return;
      try {
        const r = await api("/episodes/renumber", { method: "POST", json: { count: res.changes.length } });
        clear(out);
        out.append(h("p", { class: "tr-small tr-status-ok", role: "status" }, t("{n} Folgen neu nummeriert. Die nächste bekommt die {next}", { n: r.changed, next: r.next_number })));
      } catch (e) {
        out.append(h("p", { class: "tr-error", role: "alert" }, errorText(e)));
      }
    };
    out.append(h("p", null, t("{n} von {total} Folgen ändern sich. Sortiert wird nach dem Datum der Veröffentlichung", { n: res.changes.length, total: res.total })), list, more,
      h("div", { class: "app-row m-ed-save" }, button(t("Neu nummerieren"), { onclick: apply })));
  };
  return h("div", { class: "m-field" },
    h("h3", { class: "m-h3 m-h3-gap" }, t("Alle Folgen neu nummerieren")),
    h("p", { class: "tr-small" }, t("Nummeriert alle Folgen nach Datum ab 1 durch und setzt die Staffel nach der Einstellung oben. Hilft nach einem Import mit doppelten oder fehlenden Nummern")),
    button(t("Vorschau"), { variant: "secondary", onclick: load }), out);
}

// --- Systemstatus -----------------------------------------------------------------------------------
async function loadStatus() {
  const box = $("#m-status");
  try {
    const s = await api("/status");
    const rows = [];
    const storageOk = s.storage.configured;
    rows.push([t("Speicher"), s.storage.backend === "s3" ? t("S3-kompatibler Speicher (Cloudflare R2)") : t("lokal, nur für die Entwicklung"), storageOk ? t("bereit") : t("nicht vollständig eingerichtet"), storageOk ? "ok" : "warn"]);
    if (s.storage.public_base_url) rows.push([t("Öffentliche Adresse der Dateien"), s.storage.public_base_url, "", ""]);
    const turnLabel = { none: t("nicht vorgesehen"), static: t("feste Zugangsdaten"), cloudflare: t("Cloudflare, kurzlebige Zugangsdaten") }[s.turn.mode] || s.turn.mode;
    rows.push([t("Relais für die Live-Verbindung"), turnLabel, s.turn.configured ? t("eingerichtet") : t("nicht eingerichtet"), s.turn.configured ? "ok" : "warn"]);
    rows.push([t("Vorschläge für Titel und Shownotes"), "", s.ai.configured ? t("eingerichtet") : t("nicht eingerichtet, Vorschläge entfallen"), s.ai.configured ? "ok" : "warn"]);
    rows.push([t("Transkription"), t("Modell {model}", { model: s.whisper.model }), s.whisper.downloaded ? t("Modell vorhanden") : t("Modell wird beim ersten Lauf geladen"), s.whisper.downloaded ? "ok" : "warn"]);
    rows.push([t("Freier Speicherplatz"), fmtBytes(s.disk_free_bytes), "", ""]);
    const ar = s.archive || {};
    rows.push([t("Belegt von allen Folgen"), fmtBytes(ar.episodes_bytes), "", ""]);
    if (ar.configured) rows.push([t("Archiv"), fmtBytes(ar.archive_bytes), ar.archive_free_bytes != null ? t("{size} frei", { size: fmtBytes(ar.archive_free_bytes) }) : t("Ordner fehlt"), ar.archive_free_bytes != null ? "ok" : "warn"]);
    else rows.push([t("Archiv"), "", t("nicht eingerichtet. Mit PS_ARCHIVE_DIR lassen sich Rohdaten auslagern"), "warn"]);
    for (const [label, c] of [[t("KI-Kosten im laufenden Monat"), s.costs?.current], [t("KI-Kosten im Vormonat"), s.costs?.previous]]) {
      if (!c) continue;
      rows.push([label, fmtUsd(c.total), c.total > 0 ? t("Recherche {research}, Live-Begleiter {companion}, Shownotes {shownotes}", { research: fmtUsd(c.by_kind.research), companion: fmtUsd(c.by_kind.companion), shownotes: fmtUsd(c.by_kind.shownotes) }) : "", ""]);
    }
    const dl = h("dl", { class: "m-dl m-status-dl" });
    rows.forEach(([k, v, st, cls]) => dl.append(h("div", null, h("dt", null, k), h("dd", null, v ? h("span", { class: "m-num" }, v) : null, v && st ? " · " : null, st ? h("span", { class: cls ? `tr-status-${cls}` : "" }, st) : null))));
    clear(box);
    box.append(dl);
  } catch (e) {
    clear(box);
    box.append(h("p", { class: "tr-error", role: "alert" }, t("Der Status konnte nicht geladen werden. {message}", { message: errorText(e) })));
  }
}

start();
