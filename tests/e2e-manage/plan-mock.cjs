// Abgefangene Antworten für die Vorbereitung (Sendeplan), solange der Server die Endpunkte aus SPEC Abschnitt 14 nicht kennt.
// Verhält sich wie der künftige Server: Abschnitte anlegen, ändern, löschen, ordnen, Recherche (läuft zwei Abfragen, dann fertig).
const DOSSIER_BLITZ = {
  lage: "Die Bahn will Verspätungen künftig mit neuen Begriffen benennen. Kritik kommt von Fahrgastverbänden, die Transparenz fordern. Der Konzern verweist auf eine einheitliche Sprachregelung.",
  fakten: [
    { text: "Die Neuregelung gilt ab dem Fahrplanwechsel im Dezember.", quelle_titel: "Bahn Pressemitteilung", quelle_url: "https://example.org/presse/begriffe", datum: "2026-11-08" },
    { text: "Fahrgastverbände sehen darin eine Verharmlosung.", quelle_titel: "Fahrgastverband", quelle_url: "https://example.org/fahrgast/kritik", datum: "2026-11-09" },
    { text: "Im Oktober war jeder dritte Fernzug mehr als sechs Minuten verspätet.", quelle_titel: "Statistik Oktober", quelle_url: "https://example.org/statistik/oktober", datum: "2026-11-03" },
  ],
  zahlen: [{ text: "31,4 % der Fernzüge im Oktober über sechs Minuten verspätet", quelle_url: "https://example.org/statistik/oktober" }],
  positionen: [{ wer: "Konzern", position: "Einheitliche Begriffe machen Meldungen vergleichbar." }, { wer: "Fahrgastverband", position: "Neue Wörter ändern keine Fahrzeiten." }],
  gegenargumente: ["Eine einheitliche Sprache kann Meldungen tatsächlich verständlicher machen."],
  fragen: ["Wer hat sich den Begriff ausgedacht?", "Was kostet die Umstellung der Anzeigen?"],
  aufhaenger: ["Verzögerung im Betriebsablauf klingt wie ein Wellnessangebot."],
  begriffe: ["Fahrplanwechsel", "Betriebsablauf", "Fahrgastverband"],
  quellen: [{ titel: "Bahn Pressemitteilung", url: "https://example.org/presse/begriffe", datum: "2026-11-08" }, { titel: "Statistik Oktober", url: "https://example.org/statistik/oktober", datum: "2026-11-03" }],
  stand: "2026-11-11T09:30:00+01:00",
};
const DOSSIER_GOOD = {
  lage: "Kein Werk angegeben. Passend zum Hauptthema Zuständigkeit kommen drei Kandidaten infrage.",
  kandidaten: [
    { titel: "Das Schloss", urheber: "Franz Kafka", typ: "read", url: "https://example.org/schloss", warum: "Der Klassiker über Zuständigkeiten, die niemand erklären kann." },
    { titel: "Brazil", urheber: "Terry Gilliam", typ: "watch", url: "https://example.org/brazil", warum: "Satire über Formulare und Ämter." },
  ],
  fragen: [], aufhaenger: [], begriffe: [], quellen: [], stand: "2026-11-11T09:31:00+01:00",
};

function initialSegments() {
  const mk = (i, kind, title, input, extra = {}) => ({ id: `seg${i}`, episode_id: "demodraft", sort: i, kind, title, input, good_type: null, status: "empty", dossier: null, notes: "", visited_at: null, updated_at: 0, ...extra });
  return [
    mk(0, "blitzlicht", "Bahn und neue Begriffe", "Verspätung, Verzögerung im Betriebsablauf, Pressemitteilung\nhttps://example.org/presse/begriffe", { status: "ready", dossier: DOSSIER_BLITZ, notes: "Pointe am Ende: Wellness.", cost_usd: 0.4149 }),
    mk(1, "blitzlicht", "", ""),
    mk(2, "blitzlicht", "Wochenrückblick", "drei Sätze, keine Hoffnung"),
    mk(3, "hauptthema", "Zuständigkeit", "These: Zuständigkeit ist ein Zustand, den man nicht erreicht, nur beschreibt. Formulare, Ämter, Bahn."),
    mk(4, "good", "", "", { good_type: "read", status: "ready", dossier: DOSSIER_GOOD }),
  ];
}

async function mockPlan(page, { missing = false } = {}) {
  const segs = initialSegments();
  let n = 10;
  const researching = new Map();
  const json = (route, body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  const err = (route, status, code, message) => json(route, { error: { code, message } }, status);
  const prep = { id: "demodraft", label: "", title: null, number: 14, created_at: Date.parse("2026-11-10T09:00:00Z"), segments: segs.length, ready: 2 };
  await page.route(/\/api\/host\/(episodes(\/draft|\/demodraft|\/[^/]+\/segments.*)?|segments\/.*|preparations|studio\/preparation)$/, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const p = url.pathname.replace("/api/host", "");
    const m = req.method();
    if (missing && /segments|draft|preparations/.test(p)) return err(route, 404, "not_found", "Nicht gefunden");
    if (p === "/preparations" && m === "GET") return json(route, { preparations: [prep], selected: prep.id, busy: false });
    if (p === "/preparations" && m === "POST") return json(route, { id: prep.id, status: "draft", label: "" }, 201);
    if (p === "/studio/preparation" && m === "PUT") return json(route, { preparations: [prep], selected: prep.id, busy: false });
    if (p === "/episodes/demodraft" && m === "PATCH") { Object.assign(prep, JSON.parse(req.postData() || "{}")); return json(route, prep); }
    if (p === "/episodes/demodraft") return json(route, { deleted: true });
    if (p === "/episodes" && m === "GET") {
      const real = await (await route.fetch()).json();
      return json(route, [{ id: "demodraft", status: "draft", source: "recorded", number: 14, title: null, duration_ms: null, job: null }, ...real.filter((e) => e.status !== "draft")]);
    }
    if (p === "/episodes" ) return route.fallback();
    if (p === "/episodes/draft" && m === "POST") return json(route, { id: "demodraft", status: "draft", number: 14 }, 201);
    let mt;
    if ((mt = p.match(/^\/episodes\/[^/]+\/segments\/order$/)) && m === "POST") {
      const ids = JSON.parse(req.postData()).ids;
      ids.forEach((id, i) => (segs.find((s) => s.id === id).sort = i));
      segs.sort((a, b) => a.sort - b.sort);
      return json(route, segs);
    }
    if ((mt = p.match(/^\/episodes\/[^/]+\/segments$/))) {
      if (m === "GET") {
        // laufende Recherchen werden nach zwei Abfragen fertig
        for (const s of segs) if (s.status === "researching") {
          const c = (researching.get(s.id) || 0) + 1;
          researching.set(s.id, c);
          if (c >= 2) { s.status = "ready"; s.dossier = s.kind === "good" ? DOSSIER_GOOD : DOSSIER_BLITZ; s.cost_usd = Math.round(((s.cost_usd || 0) + 0.27) * 10000) / 10000; }
        }
        return json(route, segs);
      }
      const b = JSON.parse(req.postData() || "{}");
      const s = { id: `seg${n++}`, episode_id: "demodraft", sort: segs.length, kind: b.kind, title: b.title || "", input: b.input || "", good_type: b.good_type || null, status: "empty", dossier: null, notes: "", visited_at: null, updated_at: 0 };
      segs.push(s);
      return json(route, s, 201);
    }
    if ((mt = p.match(/^\/segments\/([^/]+)\/research$/)) && m === "POST") {
      const s = segs.find((x) => x.id === mt[1]);
      s.status = "researching"; researching.set(s.id, 0);
      return json(route, { status: "researching" }, 202);
    }
    if ((mt = p.match(/^\/segments\/([^/]+)$/))) {
      const i = segs.findIndex((x) => x.id === mt[1]);
      if (i < 0) return err(route, 404, "segment_not_found", "Abschnitt nicht gefunden");
      if (m === "DELETE") { segs.splice(i, 1); return json(route, { deleted: true }); }
      if (m === "PATCH") { Object.assign(segs[i], JSON.parse(req.postData())); return json(route, segs[i]); }
    }
    return route.fallback();
  });
  return { segs };
}

module.exports = { mockPlan };
