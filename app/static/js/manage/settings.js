// Einstellungen: Show-Daten, Cover, Klang, Einladungen, Import, Feed-Vorschau, Systemstatus.
import {
  $, api, button, clear, confirmDialog, copyText, errorText, field, fmtBytes, fmtDateTime, h, icon, poll, progressBar, toast,
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
    root.append(h("p", { class: "tr-error", role: "alert" }, `Die Einstellungen konnten nicht geladen werden. ${errorText(e)}`));
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
  const toc = h("nav", { class: "m-toc", "aria-label": "Auf dieser Seite" },
    ...[["show", "Show"], ["format", "Sendung und Format"], ["cover", "Cover"], ["klang", "Klang"], ["einladungen", "Einladungen"], ["import", "Import"], ["feed", "Feed"], ["status", "Systemstatus"]]
      .map(([k, t]) => h("a", { href: `#${k}` }, t)));
  root.append(toc, showSection(), formatSection(), coverSection(), audioSection(), inviteSection(), importSection(), feedSection(), h("section", { class: "m-section", id: "status" }, h("h2", { class: "m-h2" }, "Systemstatus"), h("div", { id: "m-status" }, h("p", { class: "tr-small" }, "Status wird geladen"))));
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
    if (!body.title) { state.textContent = "Der Titel darf nicht leer sein"; state.className = "m-save-state tr-status-error"; return; }
    try {
      settings = await api("/settings", { method: "PUT", json: { show: body } });
      state.textContent = "Gespeichert";
      state.className = "m-save-state tr-status-ok";
    } catch (e) {
      state.textContent = errorText(e);
      state.className = "m-save-state tr-status-error";
    }
  };
  return section("show", "Show",
    h("p", { class: "m-lead" }, "Diese Angaben stehen im Feed und erscheinen in den Podcast-Apps"),
    h("div", { class: "m-grid2" },
      field("Titel der Show", f.title),
      field("Autor", f.author, { hint: "Wird in den Apps als Urheber angezeigt" }),
      field("Inhaber", f.owner_name),
      field("E-Mail des Inhabers", f.owner_email, { hint: "Darüber prüft Spotify die Inhaberschaft" })),
    field("Beschreibung", f.description),
    h("div", { class: "m-grid3" },
      field("Sprache", f.language, { hint: "Kürzel, zum Beispiel de" }),
      field("Kategorie", f.category),
      field("Unterkategorie", f.subcategory)),
    h("div", { class: "m-grid2" },
      field("Webseite", f.link),
      field("Urheberrecht", f.copyright)),
    h("label", { class: "m-check", for: "m-show-explicit" }, f.explicit, h("span", null, "Die Show enthält explizite Inhalte")),
    h("div", { class: "app-row m-ed-save" }, button("Show-Daten speichern", { onclick: save }), state));
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
      const sel = h("select", { class: "tr-input m-select", "aria-label": `Abschnitt ${i + 1}` });
      PLAN_ENTRIES.forEach((e) => sel.append(h("option", { value: e, selected: e === entry }, entryName(e))));
      sel.addEventListener("change", () => { plan[i] = sel.value; });
      list.append(h("li", null, sel,
        button("", { icon: "trash", ariaLabel: `Abschnitt ${i + 1} entfernen`, title: "Entfernen", disabled: plan.length <= 1, onclick: () => { plan.splice(i, 1); drawPlan(); } })));
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
    if (!(mins >= 1 && mins <= 30)) { state.textContent = "Die Dauer muss zwischen 1 und 30 Minuten liegen"; state.className = "m-save-state tr-status-error"; return; }
    try {
      settings = await api("/settings", { method: "PUT", json: { profile, format: { labels, short_minutes: mins, plan } } });
      state.textContent = "Gespeichert";
      state.className = "m-save-state tr-status-ok";
      const box = document.getElementById("m-invites");
      if (box) drawInvites(box);
    } catch (e) {
      state.textContent = errorText(e);
      state.className = "m-save-state tr-status-error";
    }
  };
  return section("format", "Sendung und Format",
    h("p", { class: "m-lead" }, "Wer spricht, worum es geht und woraus eine Folge besteht. Vorbereitung, Begleiter und Shownotes richten sich danach."),
    h("div", { class: "m-grid-pair" },
      field("Rufname des Hosts", f.host_name, { hint: "Wer die App bedient und die Karten liest" }),
      field("Rufname des Co-Hosts", f.cohost_name, { hint: "Trägt den Dauerlink ins Studio" }),
      field("Thema der Show", f.genre, { hint: "Zum Beispiel Politik und Zeitgeschehen" }),
      field("Ton der Show", f.tone, { hint: "Zum Beispiel trocken, neugierig, humorvoll" })),
    h("h3", { class: "m-h3" }, "Abschnittsarten"),
    h("p", { class: "tr-small" }, "Vier Arten mit festem Verhalten, die Namen sind frei. Ein kurzes Thema bekommt ein knappes Dossier, das Hauptthema ein ausführliches, eine Empfehlung stellt ein Werk vor."),
    h("div", { class: "m-grid-pair" },
      field("Kurzes Thema", names.blitzlicht),
      field("Hauptthema", names.hauptthema),
      field("Freier Abschnitt", names.frei),
      field("Empfehlung", names.good),
      field("Empfehlung zum Lesen", names.read),
      field("Empfehlung zum Sehen", names.watch),
      field("Empfehlung zum Hören", names.listen),
      field("Dauer eines kurzen Themas in Minuten", minutes)),
    h("h3", { class: "m-h3" }, "Plan einer neuen Folge"),
    h("p", { class: "tr-small" }, "Jede neue Folge beginnt mit diesen Abschnitten. In der Vorbereitung lässt sich der Plan je Folge ändern."),
    list,
    h("div", { class: "app-row" }, button("Abschnitt hinzufügen", { icon: "plus", onclick: () => { if (plan.length < 12) { plan.push("frei"); drawPlan(); } } })),
    h("div", { class: "app-row m-ed-save" }, button("Sendung und Format speichern", { onclick: save }), state));
}

// --- Cover --------------------------------------------------------------------------------------
function coverSection() {
  const img = h("img", { class: "m-cover-img", alt: "Cover der Show", width: 200, height: 200 });
  const none = h("div", { class: "m-cover-none", role: "img", "aria-label": "Kein Cover" }, "Noch kein Cover");
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
    state.textContent = "Wird geprüft";
    const dims = await new Promise((res) => {
      const url = URL.createObjectURL(f);
      const im = new Image();
      im.onload = () => { res([im.naturalWidth, im.naturalHeight]); URL.revokeObjectURL(url); };
      im.onerror = () => { res(null); URL.revokeObjectURL(url); };
      im.src = url;
    });
    if (dims && (dims[0] !== dims[1] || dims[0] < 1400 || dims[0] > 3000)) {
      state.textContent = `Das Cover ist ${dims[0]} × ${dims[1]} px. Gefordert ist quadratisch, 1400 bis 3000 px.`;
      state.className = "m-save-state tr-status-error";
      return;
    }
    const form = new FormData();
    form.append("file", f);
    try {
      settings = await api("/settings/cover", { method: "POST", form });
      showImg();
      state.textContent = "Cover gespeichert";
      state.className = "m-save-state tr-status-ok";
    } catch (e) {
      state.textContent = errorText(e);
      state.className = "m-save-state tr-status-error";
    }
  });
  showImg();
  return section("cover", "Cover",
    h("div", { class: "m-cover-row" }, frame,
      h("div", { class: "m-cover-side" },
        h("p", null, "Quadratisch, 1400 bis 3000 Pixel Kantenlänge, JPEG oder PNG, höchstens 10 MB"),
        h("p", { class: "tr-small" }, "Das Cover erscheint im Feed und in den Podcast-Apps und wird in jede neue Folge eingebettet"),
        h("div", { class: "app-row" }, button("Cover wählen", { icon: "upload", onclick: () => file.click() }), file), state)));
}

// --- Klang --------------------------------------------------------------------------------------
const DENOISE = [["off", "Aus"], ["leicht", "Leicht"], ["normal", "Normal"], ["stark", "Stark"]];

function audioSection() {
  const a = settings.audio;
  const lufs = input("number", a.target_lufs, { step: "0.5", min: "-30", max: "-10", inputmode: "decimal" });
  const crossgate = h("input", { type: "checkbox", checked: !!a.crossgate, id: "m-crossgate" });
  const rates = [96, 128, 160, 192, 256, 320];
  if (!rates.includes(a.mp3_kbps)) rates.push(a.mp3_kbps);
  rates.sort((x, y) => x - y);
  const kbps = h("select", { class: "tr-input m-select" });
  rates.forEach((r) => kbps.append(h("option", { value: r, selected: r === a.mp3_kbps }, `${r} kbit/s`)));
  const radios = h("div", { class: "m-radios", role: "radiogroup", "aria-labelledby": "m-dn-label" });
  DENOISE.forEach(([v, t]) => radios.append(h("label", { class: "m-check", for: `m-dn-${v}` }, h("input", { type: "radio", name: "m-dn", id: `m-dn-${v}`, value: v, checked: a.denoise === v }), h("span", null, t))));
  const state = h("span", { class: "m-save-state", role: "status" });
  const save = async () => {
    const v = parseFloat(lufs.value);
    if (Number.isNaN(v) || v < -30 || v > -10) { state.textContent = "Die Ziel-Lautheit muss zwischen −30 und −10 LUFS liegen"; state.className = "m-save-state tr-status-error"; return; }
    const dn = radios.querySelector("input:checked")?.value || "normal";
    try {
      settings = await api("/settings", { method: "PUT", json: { audio: { target_lufs: v, denoise: dn, crossgate: crossgate.checked, mp3_kbps: parseInt(kbps.value, 10) } } });
      state.textContent = "Gespeichert";
      state.className = "m-save-state tr-status-ok";
    } catch (e) {
      state.textContent = errorText(e);
      state.className = "m-save-state tr-status-error";
    }
  };
  return section("klang", "Klang",
    h("p", { class: "m-lead" }, "Änderungen gelten ab der nächsten Verarbeitung oder dem nächsten Neumischen einer Folge"),
    field("Ziel-Lautheit in LUFS", lufs, { hint: "−16 LUFS ist der übliche Podcast-Wert, Spotify empfiehlt −14. Je höher der Wert, desto lauter die Folge." }),
    h("div", { class: "tr-field m-field" }, h("span", { class: "tr-label", id: "m-dn-label" }, "Rauschunterdrückung"), radios,
      h("p", { class: "tr-small m-hint" }, "Je stärker die Stufe, desto mehr Rauschen verschwindet, desto eher können Stimmen dünner klingen")),
    h("div", { class: "tr-field m-field" }, h("span", { class: "tr-label" }, "Crossgate"),
      h("label", { class: "m-check", for: "m-crossgate" }, crossgate, h("span", null, "Übersprechen absenken")),
      h("p", { class: "tr-small m-hint" }, "Spricht nur eine Person, wird das Übersprechen in den Spuren der anderen abgesenkt, höchstens um 15 dB. Das schützt Aufnahmen ohne Kopfhörer.")),
    field("MP3-Bitrate", kbps, { id: "m-kbps", hint: "160 kbit/s ist für Sprache ein guter Standard" }),
    h("div", { class: "app-row m-ed-save" }, button("Klang speichern", { onclick: save }), state));
}

// --- Einladungen --------------------------------------------------------------------------------
function inviteSection() {
  const box = h("div", { id: "m-invites" });
  const sec = section("einladungen", "Einladungen", box);
  drawInvites(box);
  return sec;
}

function linkRow(url, labelText) {
  const field_ = h("input", { class: "tr-input m-url", type: "text", readonly: true, value: url, "aria-label": labelText, onfocus: (e) => e.target.select() });
  const copy = button("Kopieren", { icon: "copy", onclick: async () => toast((await copyText(url, field_)) ? "Link kopiert" : "Kopieren nicht möglich, Link markiert", { error: false }) });
  return h("div", { class: "m-linkrow" }, field_, copy);
}

function inviteState(inv) {
  if (inv.revoked_at) return ["widerrufen", ""];
  if (!inv.active) return ["abgelaufen", ""];
  if (inv.kind === "single" && inv.uses > 0) return [`benutzt, gültig bis ${fmtDateTime(inv.expires_at)}`, "tr-status-ok"];
  return [inv.expires_at ? `gültig bis ${fmtDateTime(inv.expires_at)}` : "gültig", "tr-status-ok"];
}

/** Wem der Dauerlink gehört: Co-Host aus dem Show-Profil, sonst die Bezeichnung des Links. */
function cohost(permanent) {
  return (settings.profile && settings.profile.cohost_name) || (permanent && permanent.label) || "den Co-Host";
}

function drawInvites(box) {
  clear(box);
  const perm = invites.filter((i) => i.kind === "permanent" && i.active);
  const permanent = perm[0];
  const who = cohost(permanent);
  box.append(h("h3", { class: "m-h3" }, `Dauerlink für ${who}`));
  if (permanent) {
    box.append(
      h("p", { class: "tr-small" }, `Dieser Link führt ${who} ohne Anmeldung ins Studio und bleibt gültig, bis er erneuert wird`),
      linkRow(permanent.url, `Dauerlink für ${who}`),
      h("div", { class: "app-row" }, button("Erneuern", { icon: "refresh", onclick: () => renewPermanent(perm, box) })),
      h("p", { class: "tr-small" }, "Beim Erneuern wird der bisherige Link ungültig und ein neuer muss verschickt werden"));
  } else {
    box.append(h("p", { class: "tr-small" }, "Es gibt keinen gültigen Dauerlink"), h("div", { class: "app-row" }, button("Dauerlink erzeugen", { onclick: () => renewPermanent([], box) })));
  }

  const name = h("input", { class: "tr-input", type: "text", maxlength: "60", placeholder: "Name des Gastes", autocomplete: "off" });
  const created = h("div", { class: "m-created" });
  const make = async () => {
    const label = name.value.trim();
    if (!label) { name.focus(); toast("Bitte den Namen des Gastes eintragen", { error: true }); return; }
    try {
      const inv = await api("/invites", { method: "POST", json: { label, kind: "single" } });
      invites = await api("/invites");
      drawInvites(box);
      const c = $("#m-created");
      if (c) { c.append(h("p", null, `Einmal-Link für ${inv.label}, 48 Stunden gültig`), linkRow(inv.url, `Einmal-Link für ${inv.label}`)); }
    } catch (e) {
      toast(errorText(e), { error: true });
    }
  };
  created.id = "m-created";
  box.append(
    h("h3", { class: "m-h3 m-h3-gap" }, "Einmal-Link für Gäste"),
    h("p", { class: "tr-small" }, "Ein Einmal-Link gilt für eine Person und läuft nach 48 Stunden oder nach dem Widerruf ab"),
    h("div", { class: "m-linkrow" }, field("Name des Gastes", name, { cls: "m-grow" }), button("Einmal-Link erzeugen", { onclick: make, cls: "m-align-end" })),
    created);
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); make(); } });

  const singles = invites.filter((i) => i.kind === "single");
  box.append(h("h3", { class: "m-h3 m-h3-gap" }, "Ausgestellte Einmal-Links"));
  if (!singles.length) {
    box.append(h("p", { class: "tr-small" }, "Noch keine Einmal-Links ausgestellt"));
  } else {
    const ul = h("ul", { class: "tr-list" });
    singles.slice(0, 15).forEach((inv) => {
      const [txt, cls] = inviteState(inv);
      ul.append(h("li", null, h("div", { class: "tr-item m-track" },
        h("div", { class: "m-ep-text" }, h("strong", { class: "m-ep-title" }, inv.label), h("p", { class: "m-meta" }, h("span", { class: cls }, txt))),
        inv.active ? h("div", { class: "m-sound-actions" },
          button("Kopieren", { icon: "copy", ariaLabel: `Link für ${inv.label} kopieren`, onclick: async () => toast((await copyText(inv.url)) ? "Link kopiert" : "Kopieren nicht möglich", {}) }),
          button("Widerrufen", { onclick: () => revoke(inv, box), ariaLabel: `Link für ${inv.label} widerrufen` })) : null)));
    });
    box.append(ul);
  }
}

async function renewPermanent(old, box) {
  if (old.length) {
    const ok = await confirmDialog({ title: "Dauerlink erneuern", text: "Der bisherige Link wird ungültig. Der neue Link muss verschickt und einmal geöffnet werden.", ok: "Erneuern" });
    if (!ok) return;
  }
  try {
    await api("/invites", { method: "POST", json: { label: (settings.profile && settings.profile.cohost_name) || old[0]?.label || "Co-Host", kind: "permanent" } });
    for (const o of old) await api(`/invites/${encodeURIComponent(o.id)}`, { method: "DELETE" });
    invites = await api("/invites");
    drawInvites(box);
    toast("Neuer Dauerlink erzeugt");
  } catch (e) {
    toast(errorText(e), { error: true });
  }
}

async function revoke(inv, box) {
  const ok = await confirmDialog({ title: "Link widerrufen", text: `Der Einmal-Link für ${inv.label} wird sofort ungültig`, ok: "Widerrufen", danger: true });
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
  const go = button("Importieren", { onclick: async () => {
    const v = url.value.trim();
    if (!/^https?:\/\//i.test(v)) { out.replaceChildren(h("p", { class: "tr-error", role: "alert" }, "Die Feed-Adresse muss mit http:// oder https:// beginnen")); return; }
    try {
      const r = await api("/import", { method: "POST", json: { feed_url: v } });
      safeSession("set", r.job_id);
      watchImport(r.job_id);
    } catch (e) {
      out.replaceChildren(h("p", { class: "tr-error", role: "alert" }, errorText(e)));
    }
  } });
  return section("import", "Import des bisherigen Feeds",
    h("p", { class: "m-lead" }, "Übernimmt alle Folgen des alten Podcast-Feeds mit unveränderter Kennung (GUID), samt Audio, Show-Daten und Cover. Der Import lässt sich wiederholen, vorhandene Folgen werden übersprungen."),
    h("div", { class: "m-linkrow" }, field("Adresse des alten Feeds", url, { cls: "m-grow", id: "m-import-url" }), go),
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
      out.append(h("p", null, j.status === "queued" ? "Der Import wartet auf den Start" : `${j.message || "Import läuft"}, ${Math.round((j.progress || 0) * 100)} %`), progressBar(j.progress, "Import, Fortschritt"));
    } else if (j.status === "done") {
      out.append(h("p", { class: "tr-status-ok" }, "Import abgeschlossen"), j.message ? h("p", null, j.message) : null, h("p", null, h("a", { href: "/episodes" }, "Zu den Folgen")));
    } else {
      out.append(h("p", { class: "tr-status-error", role: "alert" }, "Der Import ist fehlgeschlagen"), h("p", null, j.message || ""),
        j.log ? h("details", { class: "m-details" }, h("summary", null, "Protokoll anzeigen"), h("pre", { class: "m-pre" }, j.log)) : null);
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
  const pre = h("pre", { class: "m-pre", tabindex: "0", "aria-label": "Feed als Text" });
  const det = h("details", { class: "m-details" }, h("summary", null, "Feed-Vorschau anzeigen"), pre);
  det.addEventListener("toggle", async () => {
    if (!det.open) return;
    pre.textContent = "Wird geladen";
    try {
      pre.textContent = await api("/feed/preview");
    } catch (e) {
      pre.textContent = errorText(e);
    }
  });
  return section("feed", "Feed",
    h("p", { class: "m-lead" }, "So sähe der Feed aus, wenn er jetzt veröffentlicht würde"),
    det,
    h("p", null, h("a", { href: "/api/host/feed/preview", target: "_blank", rel: "noopener" }, "Feed in neuem Tab öffnen")));
}

// --- Systemstatus -----------------------------------------------------------------------------------
async function loadStatus() {
  const box = $("#m-status");
  try {
    const s = await api("/status");
    const rows = [];
    const storageOk = s.storage.configured;
    rows.push(["Speicher", s.storage.backend === "s3" ? "S3-kompatibler Speicher (Cloudflare R2)" : "lokal, nur für die Entwicklung", storageOk ? "bereit" : "nicht vollständig eingerichtet", storageOk ? "ok" : "warn"]);
    if (s.storage.public_base_url) rows.push(["Öffentliche Adresse der Dateien", s.storage.public_base_url, "", ""]);
    const turnLabel = { none: "nicht vorgesehen", static: "feste Zugangsdaten", cloudflare: "Cloudflare, kurzlebige Zugangsdaten" }[s.turn.mode] || s.turn.mode;
    rows.push(["Relais für die Live-Verbindung", turnLabel, s.turn.configured ? "eingerichtet" : "nicht eingerichtet", s.turn.configured ? "ok" : "warn"]);
    rows.push(["Vorschläge für Titel und Shownotes", "", s.ai.configured ? "eingerichtet" : "nicht eingerichtet, Vorschläge entfallen", s.ai.configured ? "ok" : "warn"]);
    rows.push(["Transkription", `Modell ${s.whisper.model}`, s.whisper.downloaded ? "Modell vorhanden" : "Modell wird beim ersten Lauf geladen", s.whisper.downloaded ? "ok" : "warn"]);
    rows.push(["Freier Speicherplatz", fmtBytes(s.disk_free_bytes), "", ""]);
    const dl = h("dl", { class: "m-dl m-status-dl" });
    rows.forEach(([k, v, st, cls]) => dl.append(h("div", null, h("dt", null, k), h("dd", null, v ? h("span", { class: "m-num" }, v) : null, v && st ? " · " : null, st ? h("span", { class: cls ? `tr-status-${cls}` : "" }, st) : null))));
    clear(box);
    box.append(dl);
  } catch (e) {
    clear(box);
    box.append(h("p", { class: "tr-error", role: "alert" }, `Der Status konnte nicht geladen werden. ${errorText(e)}`));
  }
}

start();
