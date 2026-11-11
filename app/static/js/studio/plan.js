// Spalte „Sendeplan“ mit Live-Begleiter, nur für den Host (SPEC Abschnitte 14 und 15).
//
// Ruhig gehalten: Die einzige Bewegung ist das Einblenden einer neuen Karte. Große, lesbare Schrift. Fehlen die
// Server-Felder (`plan`, `assist.*`), zeigt die Spalte „Kein Sendeplan“ und schickt keine assist-Nachrichten.

const V = new URL(import.meta.url).search;
const { h, fill, icon, api } = await import(`./util.js${V}`);
const { t } = await import(`../i18n.js${V}`);

// Namen der Abschnittsarten: Standard, bis der Raum das Sendeformat der Show liefert (`room.format.labels`)
const DEFAULT_LABELS = { blitzlicht: "Kurzthema", hauptthema: "Hauptthema", good: "Empfehlung", frei: "Freier Abschnitt", read: "Lesetipp", watch: "Sehtipp", listen: "Hörtipp" };
// Die deutschen Standardnamen werden übersetzt; vom Sendeformat der Show benannte Abschnitte bleiben, wie sie sind
const LABEL_TEXT = {
  Kurzthema: () => t("Kurzthema"), Hauptthema: () => t("Hauptthema"), Empfehlung: () => t("Empfehlung"),
  "Freier Abschnitt": () => t("Freier Abschnitt"), Lesetipp: () => t("Lesetipp"), Sehtipp: () => t("Sehtipp"), Hörtipp: () => t("Hörtipp"),
};
let LABELS = { ...DEFAULT_LABELS };
const CARD_KIND = { stichwort: () => t("Stichwort"), frage: () => t("Frage"), fakt: () => t("Fakt"), recherche: () => t("Recherche") };
const STATUS = { empty: () => t("ohne Dossier"), researching: () => t("Recherche läuft"), ready: () => t("Dossier bereit"), failed: () => t("Recherche fehlgeschlagen") };
const SAID = { frage: () => t("gestellt"), fakt: () => t("genannt"), zahl: () => t("genannt"), aufhaenger: () => t("gebracht") };
const BIG_MS = 120000;      // Sendemodus: so lange steht die neueste Karte groß
const STREAM_MS = 600000;   // danach fällt eine Karte aus dem Strom
const LIVE_CARDS = 4;       // höchstens so viele Karten im Strom, damit nichts scrollt

function mmss(ms) {
  const sec = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}

function ago(ms) {
  return ms < 45000 ? t("gerade eben") : t("vor {n} min", { n: Math.max(1, Math.round(ms / 60000)) });
}

function kindLabel(seg) {
  const name = seg.kind === "good" && seg.good_type && LABELS[seg.good_type] ? LABELS[seg.good_type] : LABELS[seg.kind];
  if (!name) return t("Abschnitt");
  return LABEL_TEXT[name] ? LABEL_TEXT[name]() : name;
}

function asObj(x) {
  if (!x) return null;
  if (typeof x === "string") { try { return JSON.parse(x); } catch { return null; } }
  return x;
}

export class PlanColumn {
  constructor(root, { send, token, now }) {
    this.root = root;
    this.send = send;
    this.token = token;
    this.now = now || (() => Date.now());
    this.live = false;             // Sendemodus: Schritte in der Kopfzeile, Karten in eigener Spalte
    this.said = new Set();         // "abschnitt|art|nr": im Gespräch gefallen (vom Begleiter gemeldet)
    this.manual = new Map();       // dieselben Schlüssel, von Hand umgeschaltet (hat Vorrang)
    this.plan = null;              // [{id, kind, title, status, visited}] aus dem Raum, null = keine Unterstützung
    this.current = null;
    this.episodeId = null;
    this.segments = new Map();     // id → vollständiger Abschnitt mit Dossier
    this.cards = new Map();        // id → Karte
    this.order = [];               // Karten-IDs, neueste zuerst
    this.supported = false;
    this.assist = null;            // letzter assist.status
    this.lastLine = null;
    this.fetchSeq = 0;
    this.build();
    window.addEventListener("resize", () => { if (this.live) this.renderCards(null); });
  }

  build() {
    this.elPlan = h("div", { class: "ps-plan-list" });
    this.elDossier = h("div", { class: "ps-dossier" });
    this.elStatus = h("p", { class: "ps-assist-status", "aria-live": "polite" });
    this.elLine = h("p", { class: "ps-assist-line" });
    this.toggle = h("input", { type: "checkbox", id: "ps-assist-on", onchange: () => this.send({ type: "assist.toggle", on: this.toggle.checked }) });
    this.lookupInput = h("input", { class: "tr-input", id: "ps-lookup", type: "text", placeholder: t("Begriff oder Frage"), autocomplete: "off", enterkeyhint: "search" });
    const lookupForm = h("form", { class: "ps-lookup", onsubmit: (e) => { e.preventDefault(); this.lookup(this.lookupInput.value); } },
      h("label", { class: "ps-visually-hidden", for: "ps-lookup" }, t("Nachschlagen")),
      h("span", { class: "ps-lookup-field" }, this.lookupInput, h("kbd", { class: "ps-kbd", "aria-hidden": "true" }, "/")),
      h("button", { class: "tr-btn tr-btn-secondary", type: "submit", "aria-label": t("Nachschlagen") }, icon("search"), h("span", {}, t("Nachschlagen"))));
    this.elControls = h("div", { class: "ps-assist-controls" },
      h("div", { class: "ps-assist-head" },
        h("label", { class: "ps-switch", for: "ps-assist-on" }, this.toggle, h("span", {}, t("Begleiter"))),
        h("button", { class: "tr-btn tr-btn-secondary", type: "button", id: "ps-pulse", onclick: () => this.pulse() }, icon("zap"), t("Impuls jetzt"), h("kbd", { class: "ps-kbd", "aria-hidden": "true" }, "I"))),
      lookupForm, this.elStatus, this.elLine);
    this.elCards = h("ol", { class: "ps-cards", "aria-label": t("Hinweise") });
    this.elPinned = h("ol", { class: "ps-cards is-pinned", "aria-label": t("Angepinnte Hinweise") });
    this.elEmpty = h("p", { class: "ps-cards-empty", hidden: true });
    this.elAssist = h("section", { class: "ps-assist", hidden: true, "aria-label": t("Live-Begleiter") }, this.elControls, this.elPinned, this.elCards, this.elEmpty);
    fill(this.root,
      h("h2", { class: "ps-col-title" }, t("Sendeplan")),
      this.elPlan, this.elDossier, this.elAssist);
    this.render();
  }

  /** Sendemodus an oder aus: Die Abschnitte wandern in die Kopfzeile, der Begleiter in die Kartenspalte. */
  setLive(on, hosts) {
    if (this.live === on) return;
    this.live = on;
    this.root.classList.toggle("is-live", on);
    this.elAssist.classList.toggle("is-live", on);
    if (on) { hosts.steps.append(this.elPlan); hosts.cards.append(this.elAssist); }
    else { this.root.insertBefore(this.elPlan, this.elDossier); this.root.append(this.elAssist); }
    this.dossierKey = null;
    this.render();
    this.renderCards(null);
  }

  /** Zustand des Begleiters beim Betreten (auch nach dem Neuladen): Status, Karten, Abgehaktes. */
  onWelcome(a) {
    if (!a) return;
    if (a.status) { this.supported = true; this.assist = a.status; }
    for (const c of a.cards || []) if (c && c.id && !this.cards.has(c.id)) { this.cards.set(c.id, c); this.order.unshift(c.id); }
    for (const it of a.said || []) this.said.add(`${it.segment_id}|${it.art}|${it.nr}`);
    this.dossierKey = null;
    this.render();
    this.renderCards(null);
  }

  pulse() { this.send({ type: "assist.pulse" }); }

  focusLookup() { this.lookupInput.focus(); }

  /** Einen Abschnitt weiter (+1) oder zurück (−1). */
  step(dir) {
    const p = this.plan || [];
    if (!p.length) return;
    const i = p.findIndex((s) => s.id === this.current);
    const j = i < 0 ? (dir > 0 ? 0 : -1) : i + dir;
    if (j >= 0 && j < p.length) this.choose(p[j].id);
  }

  /** Art des Abschnitts, der laut Ablauf als Nächstes kommt (für die Markierung im Soundboard). */
  nextKind() {
    const p = this.plan || [];
    const i = p.findIndex((s) => s.id === this.current);
    const next = p.slice(i + 1).find((s) => !s.visited);
    return next ? next.kind : null;
  }

  /** Viermal je Sekunde: Laufzeit des Abschnitts und das Altern der Karten. */
  tick() {
    if (!this.live) return;
    if (this.elSince && this.sinceAt) {
      const text = t("seit {time}", { time: mmss(this.now() - this.sinceAt) });
      if (this.elSince.textContent !== text) this.elSince.textContent = text;
    }
    if (this.cardsKey !== this.cardsState().key) this.renderCards(null);
  }

  /** Raumzustand (nur Host): plan und current_segment_id. */
  onRoom(room) {
    const ep = room.episode || {};
    let refetch = false;
    if (ep.id !== this.episodeId) {
      this.episodeId = ep.id;
      this.segments.clear();
      refetch = true;
    }
    if (room.format && room.format.labels) {
      const next = { ...DEFAULT_LABELS, ...room.format.labels };
      if (JSON.stringify(next) !== JSON.stringify(LABELS)) { LABELS = next; this.dossierKey = null; }
    }
    if (Array.isArray(room.plan)) {
      this.supported = true;
      const before = JSON.stringify(this.plan);
      this.plan = room.plan;
      if ("current_segment_id" in room) this.current = room.current_segment_id;
      if (refetch || before !== JSON.stringify(this.plan)) this.fetchSegments();
    } else if (!("plan" in room)) {
      this.plan = null;
    }
    this.render();
  }

  onMessage(msg) {
    switch (msg.type) {
      case "assist.status":
        this.supported = true;
        this.assist = msg;
        break;
      case "assist.line":
        this.lastLine = msg;
        break;
      case "assist.card":
        this.supported = true;
        this.addCard(msg.card || msg);
        return; // eigenes Rendern
      case "assist.said":
        for (const it of msg.items || []) this.said.add(`${it.segment_id}|${it.art}|${it.nr}`);
        this.dossierKey = null;
        break;
      case "assist.segment":
        this.current = msg.segment_id;
        if (!this.segments.has(this.current)) this.fetchSegments();
        break;
      default:
        return;
    }
    this.render();
  }

  async fetchSegments() {
    if (!this.episodeId) return;
    const seq = ++this.fetchSeq;
    try {
      const data = await api("GET", `/api/host/episodes/${this.episodeId}/segments`, { token: this.token() });
      if (seq !== this.fetchSeq) return;
      const list = Array.isArray(data) ? data : (data && data.segments) || [];
      this.segments.clear();
      for (const s of list) this.segments.set(s.id, s);
      this.render();
    } catch { /* Server kennt die Route noch nicht: Plan ohne Dossier */ }
  }

  choose(id) {
    this.current = id;
    this.send({ type: "assist.segment", segment_id: id });
    this.render();
  }

  lookup(q) {
    q = (q || "").trim();
    if (!q) { this.lookupInput.focus(); return; }
    this.send({ type: "assist.lookup", query: q });
    this.lookupInput.value = "";
  }

  // ------------------------------------------------------------------ Darstellung
  render() {
    this.renderPlan();
    this.renderDossier();
    this.renderAssist();
  }

  renderPlan() {
    if (!this.plan || !this.plan.length) {
      fill(this.elPlan, h("p", { class: "ps-empty" }, t("Kein Sendeplan")),
        h("p", { class: "ps-empty-hint" }, h("a", { href: "/plan" }, t("Vorbereitung öffnen"))));
      return;
    }
    this.elSince = null;
    this.sinceAt = null;
    fill(this.elPlan, h("ol", { class: "ps-segments" }, this.plan.map((s) => {
      const cur = s.id === this.current;
      let since = null;
      if (cur && this.live && s.visited_at) {
        this.sinceAt = s.visited_at;
        this.elSince = since = h("span", { class: "ps-segment-since" }, t("seit {time}", { time: mmss(this.now() - s.visited_at) }));
      }
      return h("li", {},
        h("button", { type: "button", class: "ps-segment" + (cur ? " is-current" : "") + (s.visited ? " is-visited" : ""), "aria-current": cur ? "step" : null, onclick: () => this.choose(s.id) },
          cur ? h("span", { class: "tr-dot", "aria-hidden": "true" }) : h("span", { class: "ps-dot-space", "aria-hidden": "true" }),
          h("span", { class: "ps-segment-text" },
            h("span", { class: "ps-segment-kind" }, kindLabel(s), since ? " · " : null, since),
            h("span", { class: "ps-segment-title" }, s.title || kindLabel(s))),
          s.status && s.status !== "ready" ? h("span", { class: "ps-segment-status" + (s.status === "failed" ? " tr-status-error" : "") }, STATUS[s.status] ? STATUS[s.status]() : s.status) : null));
    })));
  }

  renderDossier() {
    const plan = this.plan || [];
    const id = this.current;
    if (!plan.length || !id) { this.dossierKey = null; fill(this.elDossier); return; }
    const base = plan.find((s) => s.id === id) || {};
    const seg = { ...base, ...(this.segments.get(id) || {}) };
    // nur neu zeichnen, wenn sich etwas geändert hat: aufgeklappte Abschnitte bleiben offen
    const mine = (k) => k.startsWith(id + "|");
    // Besuchsmarken ändern nichts am Dossier: nicht neu zeichnen (sonst klappt Aufgeklapptes wieder zu)
    const { visited, visited_at: _va, ...stable } = seg;
    const key = JSON.stringify([id, stable, this.supported, this.live, [...this.said].filter(mine), [...this.manual].filter(([k]) => mine(k))]);
    if (key === this.dossierKey) return;
    this.dossierKey = key;
    const d = asObj(seg.dossier);
    if (this.live) {
      fill(this.elDossier, this.liveDossier(seg, d));
      if (this.dossierFor !== id) { this.dossierFor = id; this.root.scrollTop = 0; } // neuer Abschnitt beginnt oben
      return;
    }
    const blocks = [];
    blocks.push(h("h3", { class: "ps-dossier-title" }, seg.title || kindLabel(seg)));
    if (!d) {
      blocks.push(h("p", { class: "ps-small" }, seg.status === "researching" ? t("Recherche läuft") : t("Kein Dossier für diesen Abschnitt")));
    } else {
      if (d.lage) blocks.push(h("p", { class: "ps-lage" }, d.lage));
      const list = (title, items, render, open = false) => {
        if (!Array.isArray(items) || !items.length) return null;
        return h("details", { class: "ps-fold", open },
          h("summary", {}, icon("chevron-right", "ps-chev"), h("span", {}, title), h("span", { class: "ps-count" }, String(items.length))),
          h("ul", { class: "ps-fold-list" }, items.map((x) => h("li", {}, render(x)))));
      };
      const src = (url, title) => url ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer", class: "ps-source" }, title || new URL(url, location.href).hostname.replace(/^www\./, "")) : null;
      blocks.push(list(t("Fragen"), d.fragen, (q) => q, true));
      blocks.push(list(t("Fakten"), d.fakten, (f) => [h("span", {}, f.text || String(f)), " ", src(f.quelle_url, f.quelle_titel)]));
      blocks.push(list(t("Aufhänger"), d.aufhaenger, (a) => a));
      // Seltener gebrauchtes in einem Abschnitt, damit die Spalte kurz bleibt und der Kartenstrom sichtbar
      const more = [
        ...(d.zahlen || []).map((f) => [h("span", {}, f.text || String(f)), " ", src(f.quelle_url)]),
        ...(d.positionen || []).map((p) => [h("span", { class: "ps-who" }, p.wer || ""), " ", p.position || ""]),
        ...(d.gegenargumente || []).map((a) => [t("Gegenargument: {text}", { text: a })]),
        ...(d.kandidaten || []).map((k) => [h("span", {}, k.titel || ""), k.urheber ? ` (${k.urheber})` : "", " ", src(k.url)]),
      ];
      blocks.push(list(d.kandidaten && d.kandidaten.length && !(d.zahlen || []).length ? t("Kandidaten") : t("Zahlen und Positionen"), more, (x) => x));
      if (Array.isArray(d.begriffe) && d.begriffe.length) {
        blocks.push(h("details", { class: "ps-fold" },
          h("summary", {}, icon("chevron-right", "ps-chev"), h("span", {}, t("Begriffe")), h("span", { class: "ps-count" }, String(d.begriffe.length))),
          h("p", { class: "ps-terms" }, d.begriffe.map((b) => h("button", { type: "button", class: "ps-term", title: t("Nachschlagen"), onclick: () => this.lookup(b), disabled: !this.supported }, b)))));
      }
    }
    if (seg.notes) {
      blocks.push(h("details", { class: "ps-fold", open: true },
        h("summary", {}, icon("chevron-right", "ps-chev"), h("span", {}, t("Eigene Notizen"))),
        h("p", { class: "ps-notes" }, seg.notes)));
    }
    fill(this.elDossier, blocks);
  }

  isSaid(key) { return this.manual.has(key) ? this.manual.get(key) : this.said.has(key); }

  toggleSaid(key) {
    this.manual.set(key, !this.isSaid(key));
    this.dossierKey = null;
    this.renderDossier();
  }

  /** Dossier im Sendemodus: ohne Klappen, alles Wichtige sofort lesbar, Gefallenes grau (Klick schaltet um). */
  liveDossier(seg, d) {
    const blocks = [h("h3", { class: "ps-dossier-title" }, seg.title || kindLabel(seg))];
    if (seg.notes) blocks.push(h("div", { class: "ps-own" }, h("span", { class: "ps-own-label" }, t("Eigene Notiz")), h("p", { class: "ps-notes" }, seg.notes)));
    if (!d) {
      blocks.push(h("p", { class: "ps-small" }, seg.status === "researching" ? t("Recherche läuft") : t("Kein Dossier für diesen Abschnitt")));
      return blocks;
    }
    const src = (url, title) => {
      if (!url) return null;
      let host = title;
      if (!host) { try { host = new URL(url, location.href).hostname.replace(/^www\./, ""); } catch { host = t("Quelle"); } }
      return h("a", { href: url, target: "_blank", rel: "noopener noreferrer", class: "ps-source" }, host);
    };
    const point = (art, nr, text, extra) => {
      const key = `${seg.id}|${art}|${nr}`;
      const said = this.isSaid(key);
      const el = h("li", { class: "ps-point" + (said ? " is-said" : "") },
        h("button", { type: "button", class: "ps-point-text", "aria-pressed": said ? "true" : "false", title: said ? t("Wieder als offen markieren") : t("Als erledigt markieren"), onclick: () => this.toggleSaid(key) }, text),
        said ? h("span", { class: "ps-point-state" }, SAID[art]()) : (extra || null));
      return { el, said };
    };
    const group = (title, items, doneText) => {
      if (!items.length) return;
      const done = items.filter((i) => i.said).length;
      blocks.push(h("section", { class: "ps-group" },
        h("p", { class: "ps-group-head" }, h("span", {}, title), done ? h("span", { class: "ps-count" }, doneText(done, items.length)) : null),
        h("ul", { class: "ps-points" }, items.map((i) => i.el))));
    };
    const w = d.werk;
    if (w && (w.titel || w.kurzfassung)) {
      blocks.push(h("section", { class: "ps-group" },
        h("p", { class: "ps-group-head" }, h("span", {}, [w.titel, w.urheber].filter(Boolean).join(" · "))),
        w.kurzfassung ? h("p", { class: "ps-werk" }, w.kurzfassung) : null,
        Array.isArray(w.kernaussagen) && w.kernaussagen.length ? h("ul", { class: "ps-points" }, w.kernaussagen.map((k) => h("li", { class: "ps-point" }, h("span", { class: "ps-point-text" }, k)))) : null));
    }
    group(t("Fragen"), (d.fragen || []).map((q, i) => point("frage", i + 1, q)), (done, total) => t("{done} von {total} gestellt", { done, total }));
    group(t("Fakten und Zahlen"), [
      ...(d.fakten || []).map((f, i) => point("fakt", i + 1, f.text || String(f), src(f.quelle_url, f.quelle_titel))),
      ...(d.zahlen || []).map((z, i) => point("zahl", i + 1, z.text || String(z), src(z.quelle_url))),
    ], (done, total) => t("{done} von {total} genannt", { done, total }));
    group(t("Aufhänger"), (d.aufhaenger || []).map((a, i) => point("aufhaenger", i + 1, a)), (done, total) => t("{done} von {total} gebracht", { done, total }));
    // Seltener Gebrauchtes hinter einer Zeile, damit die Spalte ohne Scrollen auskommt
    const more = [], names = [];
    if (d.lage) { names.push(t("Lage")); more.push(h("p", { class: "ps-lage" }, d.lage)); }
    const list = (name, items, render) => {
      if (!Array.isArray(items) || !items.length) return;
      names.push(name);
      more.push(h("p", { class: "ps-group-head" }, h("span", {}, name)), h("ul", { class: "ps-points" }, items.map((x) => h("li", { class: "ps-point" }, h("span", { class: "ps-point-text" }, render(x))))));
    };
    list(t("Positionen"), d.positionen, (x) => [h("span", { class: "ps-who" }, x.wer || ""), " ", x.position || ""]);
    list(t("Gegenargumente"), d.gegenargumente, (x) => x);
    list(t("Kandidaten"), d.kandidaten, (k) => [k.titel || "", k.urheber ? ` (${k.urheber})` : "", k.warum ? `: ${k.warum}` : ""]);
    if (Array.isArray(d.begriffe) && d.begriffe.length) {
      names.push(t("Begriffe"));
      more.push(h("p", { class: "ps-group-head" }, h("span", {}, t("Begriffe zum Nachschlagen"))),
        h("p", { class: "ps-terms" }, d.begriffe.map((b) => h("button", { type: "button", class: "ps-term", title: t("Nachschlagen"), onclick: () => this.lookup(b), disabled: !this.supported }, b))));
    }
    if (more.length) {
      // aufgeklappt bleibt aufgeklappt, auch wenn das Dossier neu gezeichnet wird
      const fold = h("details", { class: "ps-fold ps-more", open: this.moreOpen === seg.id },
        h("summary", {}, icon("chevron-right", "ps-chev"), h("span", {}, names.join(", "))), more);
      fold.addEventListener("toggle", () => { this.moreOpen = fold.open ? seg.id : null; });
      blocks.push(fold);
    }
    return blocks;
  }

  renderAssist() {
    this.elAssist.hidden = !this.supported;
    if (!this.supported) return;
    const st = this.assist || {};
    this.toggle.checked = !!st.on;
    let text;
    if (st.message) text = st.message;
    else if (!st.on) text = t("Begleiter aus");
    else if (st.stt === false || st.stt === "off") text = t("Mitschrift nicht verfügbar");
    else if (typeof st.lag_ms === "number" && st.lag_ms > 4000) text = t("Hört mit, {s} s Verzug", { s: Math.round(st.lag_ms / 1000) });
    else text = t("Hört mit");
    if (this.live && (st.message || st.on)) text = t("Begleiter · {status}", { status: `${text.charAt(0).toLowerCase()}${text.slice(1)}` });
    this.elStatus.textContent = text;
    this.elStatus.className = "ps-assist-status" + (st.message && st.ok === false ? " tr-status-warn" : "");
    this.elLine.textContent = this.lastLine ? `${this.lastLine.speaker ? this.lastLine.speaker + ": " : ""}${this.lastLine.text}` : "";
    this.elLine.hidden = !this.lastLine;
  }

  addCard(card) {
    if (!card || !card.id) return;
    const known = this.cards.get(card.id);
    const merged = { ...(known || {}), ...card };
    this.cards.set(card.id, merged);
    if (!known) this.order.unshift(card.id);
    this.renderCards(known ? null : card.id);
  }

  setCard(id, change) {
    const c = this.cards.get(id);
    if (!c) return;
    Object.assign(c, change);
    this.send({ type: "assist.card", id, ...change });
    this.renderCards(null);
  }

  /** Sichtbare Karten und ein Schlüssel, an dem `tick` erkennt, ob sich durch das Altern etwas ändert. */
  cardsState() {
    const now = this.now();
    let pinned = [], stream = [];
    for (const id of this.order) {
      const c = this.cards.get(id);
      if (!c || c.dismissed) continue;
      (c.pinned ? pinned : stream).push(c);
    }
    const age = (c) => Math.max(0, now - (c.server_ms || now));
    let big = null;
    if (this.live) {
      stream = stream.filter((c) => age(c) < STREAM_MS).slice(0, LIVE_CARDS);
      pinned = pinned.slice(0, 3);
      if (stream[0] && age(stream[0]) < BIG_MS) big = stream[0].id;
    } else {
      stream = stream.slice(0, 8); // höchstens acht Karten im Strom; Ältere fallen still heraus
    }
    const label = (c) => (this.live ? ago(age(c)) : "");
    const key = JSON.stringify([this.live, big, pinned.map((c) => c.id), stream.map((c) => [c.id, label(c)])]);
    return { pinned, stream, big, label, key };
  }

  renderCards(newId) {
    const { pinned, stream, big, label, key } = this.cardsState();
    this.cardsKey = key;
    const card = (c) => {
      const basis = c.basis === "allgemein" ? h("span", { class: "ps-card-basis tr-status-warn" }, t("ungeprüft"))
        : c.basis === "netz" ? h("span", { class: "ps-card-basis" }, t("Netz"))
        : c.basis === "dossier" ? h("span", { class: "ps-card-basis" }, t("Dossier")) : null;
      const size = !this.live ? "" : c.id === big ? " is-big" : " is-old";
      const li = h("li", { class: "ps-card" + size + (c.id === newId ? " is-new" : ""), "data-id": c.id },
        h("div", { class: "ps-card-head" },
          h("span", { class: "ps-card-kind" }, CARD_KIND[c.kind] ? CARD_KIND[c.kind]() : t("Hinweis")), basis,
          this.live && !c.pinned ? h("span", { class: "ps-card-age" }, label(c)) : null,
          h("span", { class: "ps-card-actions" },
            h("button", { type: "button", class: "ps-icon-btn", "aria-label": c.pinned ? t("Lösen") : t("Anpinnen"), title: c.pinned ? t("Lösen") : t("Anpinnen"), "aria-pressed": c.pinned ? "true" : "false", onclick: () => this.setCard(c.id, { pinned: !c.pinned }) }, icon("pin")),
            h("button", { type: "button", class: "ps-icon-btn", "aria-label": t("Wegklicken"), title: t("Wegklicken"), onclick: () => this.setCard(c.id, { dismissed: true }) }, icon("x")))),
        h("p", { class: "ps-card-text" }, c.text || ""),
        c.detail ? h("p", { class: "ps-card-detail" }, c.detail) : null,
        c.quelle_url ? h("a", { class: "ps-source", href: c.quelle_url, target: "_blank", rel: "noopener noreferrer" }, t("Quelle")) : null);
      return li;
    };
    fill(this.elPinned, pinned.map(card));
    this.elPinned.hidden = !pinned.length;
    fill(this.elCards, stream.map(card));
    // Sendemodus: nie eine angeschnittene Karte zeigen. Was nicht mehr ganz hineinpasst, fällt weg
    if (this.live) while (this.elCards.children.length > 1 && this.elCards.scrollHeight > this.elCards.clientHeight + 1) this.elCards.lastElementChild.remove();
    const empty = this.live && !pinned.length && !stream.length;
    this.elEmpty.hidden = !empty;
    if (empty) this.elEmpty.textContent = this.assist && this.assist.on ? t("Hier erscheinen Hinweise, sobald der Begleiter etwas beizutragen hat") : t("Der Begleiter ist aus. Einschalten geht unten");
  }
}
