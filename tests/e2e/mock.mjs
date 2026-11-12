// Mock für Sendeplan und Live-Begleiter (Playwright hängt sich in den WebSocket): gemeinsam für layout.mjs und live.mjs.

// ---------------------------------------------------------------- Mock Sendeplan und Begleiter
export const PLAN = [
  { id: "seg1", kind: "blitzlicht", title: "Bahnstreik", status: "ready", visited: true },
  { id: "seg2", kind: "blitzlicht", title: "Heizungsgesetz, zweiter Anlauf", status: "ready", visited: false },
  { id: "seg3", kind: "blitzlicht", title: "Bundesliga-Montagsspiele", status: "researching", visited: false },
  { id: "seg4", kind: "hauptthema", title: "Mietpreisbremse: Wer zahlt am Ende", status: "ready", visited: false },
  { id: "seg5", kind: "good", good_type: "read", title: "Ein Buch über Stadtplanung", status: "empty", visited: false },
];
export const DOSSIER = {
  lage: "Testinhalt für die Darstellung. Drei Sätze Lage, damit die Spalte wie im Ernstfall gefüllt ist. Der dritte Satz ist etwas länger, um den Umbruch in schmalen Spalten zu prüfen.",
  fakten: [
    { text: "Erste Tatsache mit Quelle, ausreichend lang für zwei Zeilen in der Spalte", quelle_titel: "Beispielquelle", quelle_url: "https://example.org/a", datum: "2026-11-08" },
    { text: "Zweite Tatsache", quelle_titel: "RZP, Shas recommend Netanyahu in mandate talks with Herzog, Yisrael Beytenu backs Eisenkot (Example Times)", quelle_url: "https://www.example.org/b", datum: "2026-11-09" },
  ],
  zahlen: [{ text: "12,5 % mehr als im Vorjahr", quelle_url: "https://example.org/c" }],
  positionen: [{ wer: "Verband A", position: "dafür" }, { wer: "Verband B", position: "dagegen" }],
  gegenargumente: ["Gegenargument eins"],
  fragen: ["Was ändert sich für Mieter konkret ab Januar?", "Wer hat die Ausnahmeregel durchgesetzt und warum?"],
  aufhaenger: ["Ein Aufhänger im Ton der Show"],
  begriffe: ["Mietpreisbremse", "Indexmiete", "Bundesverfassungsgericht"],
  quellen: [{ titel: "Beispielquelle", url: "https://example.org/a", datum: "2026-11-08" }],
  stand: "2026-11-10T18:00:00Z",
};
export const CARDS = [
  { kind: "frage", text: "Nachfrage: Gilt die Ausnahme auch für möblierte Wohnungen?", basis: "dossier" },
  { kind: "fakt", text: "Die Regel gilt seit 2015 und wurde zweimal verlängert", detail: "Ohne Quelle aus dem Allgemeinwissen, deshalb als ungeprüft markiert", basis: "allgemein" },
  { kind: "recherche", text: "Indexmiete: Erhöhung folgt dem Verbraucherpreisindex", basis: "netz", quelle_url: "https://example.org/index" },
  { kind: "stichwort", text: "Übergang zum Good Read: Stadtplanung", basis: "dossier" },
];

export async function installMock(p) {
  const state = { current: "seg2", on: true, cards: 0, sent: [] };
  let client = null;
  const card = (c) => ({ type: "assist.card", card: { id: "c" + (++state.cards), segment_id: state.current, server_ms: Date.now(), ...c } });
  await p.route("**/api/host/episodes/*/segments", (route) => route.fulfill({
    json: PLAN.map((s, i) => ({ ...s, episode_id: "x", sort: i, input: "", notes: s.id === "seg4" ? "Eigene Notiz: mit dem Beispiel aus Hamburg einsteigen" : "", dossier: s.status === "ready" ? DOSSIER : null })),
  }));
  await p.routeWebSocket(/\/ws\/p/, (ws) => {
    client = ws;
    const server = ws.connectToServer();
    const addPlan = (room) => { room.plan = PLAN.map((s) => ({ ...s, visited: s.visited || s.id === state.current, visited_at: s.visited || s.id === state.current ? Date.now() - 130000 : null })); room.current_segment_id = state.current; };
    server.onMessage((raw) => {
      const m = JSON.parse(raw);
      if ((m.type === "room" || m.type === "welcome") && m.room) addPlan(m.room);
      ws.send(JSON.stringify(m));
      if (m.type === "welcome") {
        ws.send(JSON.stringify({ type: "assist.status", on: state.on, stt: true, ai: true, lag_ms: 1200, message: null }));
        ws.send(JSON.stringify({ type: "assist.line", speaker: "Max", start_ms: 1000, end_ms: 4000, text: "und dann hat die Kommission das einfach durchgewunken, ohne dass irgendwer gefragt hätte" }));
      }
    });
    ws.onMessage((raw) => {
      const m = JSON.parse(raw);
      if (!m.type || !m.type.startsWith("assist.")) { server.send(raw); return; }
      state.sent.push(m);
      if (m.type === "assist.toggle") { state.on = !!m.on; ws.send(JSON.stringify({ type: "assist.status", on: state.on, stt: true, ai: true, lag_ms: 0 })); }
      if (m.type === "assist.pulse") ws.send(JSON.stringify(card(CARDS[0])));
      if (m.type === "assist.lookup") ws.send(JSON.stringify(card({ ...CARDS[2], text: `${m.query}: kurze Antwort aus der Netzrecherche` })));
      if (m.type === "assist.segment") { state.current = m.segment_id; ws.send(JSON.stringify({ type: "assist.segment", segment_id: m.segment_id })); }
    });
  });
  return {
    state,
    push(c) { client && client.send(JSON.stringify(card(c))); },
    send(msg) { client && client.send(JSON.stringify(msg)); },
  };
}

