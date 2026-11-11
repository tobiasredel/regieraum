// Sendemodus: Solange die Aufnahme läuft, passt beim Host alles auf einen Bildschirm, ohne Scrollen.
// Geprüft: Umschalten bei Start und Stopp, Anordnung (Kopfzeile, Gespräch, Karten, Dossier), Abhaken, Altern der
// Karten, Tasten, Markierung des nächsten Jingles, Dialog „Technik“, Laptop (1280×800) und großer Bildschirm.
//
//   ./dev.sh serve-e2e 8196      (anderes Terminal)
//   node tests/e2e/live.mjs

import path from "node:path";
import { launch, page, enter, newInvite, ensureSounds, clearCurrent, api, BASE, SHOTS, sleep, log, ensureDir } from "./lib.mjs";
import { installMock } from "./mock.mjs";

const failures = [];
const check = (ok, what) => { if (!ok) { failures.push(what); log("FEHLER:", what); } else log("ok:", what); };

const geo = (p) => p.evaluate(() => {
  const box = (sel) => { const el = document.querySelector(sel); if (!el) return null; const r = el.getBoundingClientRect(); const cs = getComputedStyle(el); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height), r: Math.round(r.right), b: Math.round(r.bottom), shown: cs.display !== "none" && r.width > 0 }; };
  const de = document.documentElement;
  return {
    live: document.body.classList.contains("ps-live"),
    scrollY: de.scrollHeight - window.innerHeight, scrollX: de.scrollWidth - window.innerWidth, vh: window.innerHeight, vw: window.innerWidth,
    sidebar: box(".tr-sidebar"), header: box(".tr-header"),
    bar: box(".ps-bar"), main: box(".ps-main"), cards: box(".ps-live-cards"), plan: box(".ps-plan"), stage: box(".ps-stage-wrap"), board: box(".ps-sounds"), livebox: box(".ps-livebox"),
    steps: [...document.querySelectorAll(".ps-steps-host .ps-segment")].map((b) => { const r = b.getBoundingClientRect(); return { y: Math.round(r.y), text: b.textContent, cur: b.classList.contains("is-current") }; }),
    planScroll: (() => { const el = document.querySelector(".ps-plan"); return el ? el.scrollHeight - el.clientHeight : null; })(),
    cardsCut: (() => { const el = document.querySelector(".ps-assist.is-live .ps-cards:not(.is-pinned)"); return el ? el.scrollHeight - el.clientHeight : null; })(),
  };
});

async function main() {
  ensureDir(SHOTS);
  await clearCurrent();
  const sounds = await ensureSounds();
  await api("PATCH", `/api/host/sounds/${sounds[1].id}`, { segment_kind: "blitzlicht" });
  const b = await launch();
  const host = await page(b, "host", { viewport: { width: 1280, height: 800 }, quiet: true });
  const mock = await installMock(host);
  await host.goto(`${BASE}/studio?testsignal=local`);
  await enter(host);
  const inv = await newInvite("Max", "single");
  const guest = await page(b, "gast", { viewport: { width: 1280, height: 800 }, quiet: true });
  await guest.goto(inv.url.replace(/^https?:\/\/[^/]+/, BASE) + "?testsignal=local");
  await enter(guest, { name: "Max" });
  await host.waitForSelector(".ps-segment");

  let g = await geo(host);
  check(!g.live && g.sidebar.shown, "vor der Aufnahme: gewohnte Ansicht mit Navigation");

  await host.click("#ps-start");
  await host.waitForFunction(() => document.body.classList.contains("ps-live") && /Aufnahme läuft/.test(document.querySelector(".ps-status").textContent), null, { timeout: 15000 });
  await host.waitForSelector(".ps-plan.is-live .ps-point");
  await sleep(500);
  g = await geo(host);
  check(g.live && !g.sidebar.shown && !g.header.shown, "Aufnahme läuft: Sendemodus, Navigation und Seitenkopf ausgeblendet");
  check(g.scrollY <= 0 && g.scrollX <= 0, `1280×800: Seite scrollt nicht (hoch ${g.scrollY}, quer ${g.scrollX})`);
  check(g.bar.y < g.main.y && g.bar.w > g.vw - 80, "Kopfzeile über die ganze Breite");
  check(g.main.r <= g.cards.x && g.cards.r <= g.plan.x && Math.abs(g.main.y - g.cards.y) <= 2 && Math.abs(g.cards.y - g.plan.y) <= 2, `drei Spalten nebeneinander: Gespräch ${g.main.w}, Karten ${g.cards.w}, Dossier ${g.plan.w}`);
  check(g.plan.b <= g.vh && g.cards.b <= g.vh && g.main.b <= g.vh, "alle Spalten enden im Bildschirm");
  check(g.steps.length === 5 && new Set(g.steps.map((s) => s.y)).size === 1, "fünf Abschnitte als Leiste in einer Zeile");
  const cur = g.steps.find((s) => s.cur);
  check(cur && /Heizungsgesetz/.test(cur.text) && /seit \d+:\d\d/.test(cur.text), `aktueller Abschnitt mit Laufzeit: „${cur && cur.text}“`);

  // Dossier ohne Klappen, Abhaken durch den Begleiter und von Hand
  const dossier = await host.evaluate(() => ({ folds: document.querySelectorAll(".ps-plan details:not(.ps-more)").length, points: document.querySelectorAll(".ps-plan .ps-point").length, heads: [...document.querySelectorAll(".ps-plan .ps-group-head")].map((x) => x.textContent) }));
  check(dossier.folds === 0 && dossier.points >= 5 && dossier.heads.some((t) => t.startsWith("Fragen")) && dossier.heads.some((t) => t.startsWith("Fakten und Zahlen")), `Dossier ohne Klappen: ${dossier.points} Punkte sofort lesbar (${dossier.heads.join(" | ")})`);
  mock.send({ type: "assist.said", items: [{ segment_id: mock.state.current, art: "frage", nr: 1 }, { segment_id: mock.state.current, art: "zahl", nr: 1 }] });
  await host.waitForFunction(() => document.querySelectorAll(".ps-point.is-said").length === 2);
  const said = await host.evaluate(() => [...document.querySelectorAll(".ps-point.is-said")].map((x) => x.querySelector(".ps-point-state").textContent));
  const counts = await host.evaluate(() => [...document.querySelectorAll(".ps-group-head .ps-count")].map((x) => x.textContent));
  check(said.join(",") === "gestellt,genannt" && counts.includes("1 von 2 gestellt"), `Gefallenes tritt zurück: ${said.join(", ")}; Zähler ${counts.join(" | ")}`);
  await host.click(".ps-point:not(.is-said) button.ps-point-text");
  check(await host.locator(".ps-point.is-said").count() === 3, "Klick auf einen Punkt hakt ihn von Hand ab");
  await host.click(".ps-point.is-said button.ps-point-text >> nth=0");
  check(await host.locator(".ps-point.is-said").count() === 2, "erneuter Klick macht ihn wieder offen");

  // Karten: neueste groß, ältere klein, Angepinntes unten
  mock.push({ kind: "stichwort", text: "Ältere Karte: Übergangsfrist für Bestandsgebäude", basis: "dossier", server_ms: Date.now() - 200000 });
  mock.push({ kind: "frage", text: "Zweite ältere Karte: Wer zahlt am Ende?", basis: "dossier", server_ms: Date.now() - 150000 });
  mock.push({ kind: "fakt", text: "Österreich: rund 9,2 Mio. Einwohner (Stand 2024)", detail: "Antwort auf eine Frage im Gespräch", basis: "allgemein" });
  await host.waitForFunction(() => document.querySelectorAll(".ps-live-cards .ps-card").length === 3);
  let cards = await host.evaluate(() => [...document.querySelectorAll(".ps-live-cards .ps-cards:not(.is-pinned) .ps-card")].map((c) => ({ big: c.classList.contains("is-big"), old: c.classList.contains("is-old"), text: c.querySelector(".ps-card-text").textContent.slice(0, 20), age: c.querySelector(".ps-card-age").textContent, size: parseFloat(getComputedStyle(c.querySelector(".ps-card-text")).fontSize) })));
  check(cards[0].big && /Österreich/.test(cards[0].text) && cards[0].age === "gerade eben" && cards[1].old && cards[2].old && cards[0].size > cards[1].size, `neueste Karte groß (${cards[0].size} px, ${cards[0].age}), ältere klein (${cards[1].size} px, ${cards[1].age})`);
  await host.click(".ps-live-cards .ps-card.is-old >> nth=0 >> [aria-label='Anpinnen']");
  await host.waitForSelector(".ps-live-cards .ps-cards.is-pinned .ps-card");
  g = await geo(host);
  const pinY = await host.evaluate(() => document.querySelector(".ps-cards.is-pinned").getBoundingClientRect().y);
  const streamY = await host.evaluate(() => document.querySelector(".ps-live-cards .ps-cards:not(.is-pinned)").getBoundingClientRect().y);
  check(pinY > streamY && g.scrollY <= 0, "Angepinntes steht unter dem Kartenstrom, weiterhin kein Scrollen");

  // nächster Jingle: aktuell seg2, als Nächstes kommt wieder ein kurzes Thema (vor den Tasten prüfen: Besuchtes zählt nicht mehr)
  const next = await host.evaluate(() => [...document.querySelectorAll(".ps-sound.is-next")].map((x) => x.textContent));
  check(next.length === 1 && /als Nächstes dran/.test(next[0]), `der passende Jingle ist als nächster markiert (${next.join(" | ").slice(0, 60)})`);

  // Tasten
  mock.state.sent.length = 0;
  await host.keyboard.press("ArrowRight");
  await host.keyboard.press("i");
  await host.keyboard.press("/");
  const focused = await host.evaluate(() => document.activeElement && document.activeElement.id);
  await host.keyboard.press("Escape");
  await host.evaluate(() => document.activeElement && document.activeElement.blur());
  await host.keyboard.press("m");
  await host.waitForFunction(() => /Merkpunkt bei/.test((document.querySelector(".tr-toast") || {}).textContent || ""));
  const toastNote = await host.textContent(".tr-toast");
  await host.keyboard.press("k");
  await host.waitForFunction(() => /^Kapitel bei/.test((document.querySelector(".tr-toast") || {}).textContent || ""));
  const toastChapter = await host.textContent(".tr-toast");
  await sleep(400);
  const sent = mock.state.sent.map((m) => m.type);
  check(sent.includes("assist.segment") && mock.state.sent.find((m) => m.type === "assist.segment").segment_id === "seg3", "Pfeil nach rechts schaltet auf den nächsten Abschnitt");
  check(sent.includes("assist.pulse") && focused === "ps-lookup", "I gibt einen Impuls, / springt ins Nachschlagen");
  const ev = (await api("GET", "/api/host/episodes")).find((e) => e.status === "recording");
  const marks = (await api("GET", `/api/host/episodes/${ev.id}`)).events.filter((e) => e.kind === "marker");
  const kinds = marks.map((e) => (e.payload || {}).kind);
  check(marks.length === 2 && kinds[0] === "note" && kinds[1] === "chapter", `M setzt einen Merkpunkt, K ein Kapitel (${kinds.join(", ")})`);
  check(/^Merkpunkt bei \d+:\d\d$/.test(toastNote.trim()) && /^Kapitel bei \d+:\d\d$/.test(toastChapter.trim()), `Rückmeldung nennt Art und Zeit: „${toastNote.trim()}“, „${toastChapter.trim()}“`);
  const keysHint = await host.evaluate(() => [...document.querySelectorAll(".ps-hint-keys")].map((x) => x.textContent.replace(/\s+/g, " ")).join("|"));
  check(/M\s*Merkpunkt/.test(keysHint) && /K\s*Kapitel/.test(keysHint), `Sendemodus zeigt die Tasten: „${keysHint}“`);
  await host.keyboard.press("ArrowLeft");
  await sleep(300);

  // Technik
  await host.click("#ps-tech-open");
  const tech = await host.evaluate(() => { const d = document.querySelector(".ps-tech"); return { open: d.open, peers: !!d.querySelector(".ps-peers"), self: !!d.querySelector("#ps-mic2") }; });
  check(tech.open && tech.peers && tech.self, "„Technik“ öffnet Teilnehmerliste und Gerätewahl");
  await host.click("#ps-tech-close");
  const state = await host.textContent(".ps-live-state");
  const meters = await host.locator(".ps-live-meters .ps-meter").count();
  check(meters === 2 && /Verbindung gut/.test(state), `Pegelzeile: ${meters} Pegel, Zustand „${state}“`);
  await guest.click("#ps-mute");
  await host.waitForFunction(() => /Max: stumm/.test(document.querySelector(".ps-live-state").textContent), null, { timeout: 8000 });
  check(true, "Problem wird laut: „Max: stumm“ in der Zustandszeile");
  await guest.click("#ps-mute");

  // Kamera an: Bühne in der linken Spalte, weiter ohne Scrollen
  await host.click("#ps-live-cam");
  await host.waitForFunction(() => { const v = document.querySelector(".ps-tile.is-self video"); return v && v.videoWidth > 0; }, null, { timeout: 15000 });
  await guest.click("#ps-camtoggle");
  await host.waitForFunction(() => { const v = document.querySelector(".ps-tile:not(.is-self) video"); return v && v.videoWidth > 0 && !v.paused; }, null, { timeout: 30000 });
  await sleep(800);
  g = await geo(host);
  check(g.stage.shown && g.stage.x >= g.main.x && g.stage.r <= g.main.r + 1 && g.scrollY <= 0 && g.board.b <= g.vh, `mit Kamera: Bild ${g.stage.w}×${g.stage.h} links, Soundboard endet im Bildschirm, kein Scrollen`);
  check(g.planScroll <= 0, `Dossier passt ohne Scrollen in seine Spalte (Überhang ${g.planScroll})`);
  await host.screenshot({ path: path.join(SHOTS, "live-1280.png") });

  // großer Bildschirm
  await host.setViewportSize({ width: 1920, height: 1080 });
  await sleep(500);
  g = await geo(host);
  check(g.live && g.scrollY <= 0 && g.scrollX <= 0 && g.plan.b <= g.vh, `1920×1080: Sendemodus ohne Scrollen (Gespräch ${g.main.w}, Karten ${g.cards.w}, Dossier ${g.plan.w})`);
  await host.screenshot({ path: path.join(SHOTS, "live-1920.png") });

  // Tablet hochkant: Kopfzeile, zwei Spalten (Gespräch, Karten), darunter das Dossier über die volle Breite
  for (const [w, hh] of [[820, 1180], [768, 1024], [1024, 1366]]) {
    await host.setViewportSize({ width: w, height: hh });
    await sleep(500);
    g = await geo(host);
    check(g.live && !g.sidebar.shown && g.scrollY <= 0 && g.scrollX <= 0, `${w}×${hh}: Sendemodus, Seite scrollt nicht (hoch ${g.scrollY}, quer ${g.scrollX})`);
    check(g.bar.y < g.main.y && g.bar.r <= g.vw && g.bar.w > g.vw - 48, `${w}×${hh}: Kopfzeile über die ganze Breite, ohne Überlauf`);
    check(g.main.r <= g.cards.x + 1 && Math.abs(g.main.y - g.cards.y) <= 2 && g.cards.r <= g.vw, `${w}×${hh}: Gespräch ${g.main.w} und Karten ${g.cards.w} nebeneinander`);
    check(g.plan.y >= Math.max(g.main.b, g.cards.b) - 2 && g.plan.w > g.vw - 48 && g.plan.b <= g.vh, `${w}×${hh}: Dossier darunter über die volle Breite (${g.plan.w}×${g.plan.h}), endet im Bildschirm`);
    check(g.board.b <= g.vh && g.main.b <= g.plan.y + 2, `${w}×${hh}: Soundboard endet über dem Dossier`);
    const over = await host.evaluate(() => [...document.querySelectorAll(".ps-studio.is-live *")].filter((el) => { const r = el.getBoundingClientRect(); return r.width > 0 && (r.right > window.innerWidth + 1 || r.left < -1); }).map((el) => el.className || el.tagName).slice(0, 5));
    check(!over.length, `${w}×${hh}: nichts ragt seitlich heraus${over.length ? " → " + over.join(", ") : ""}`);
    if (w === 820) await host.screenshot({ path: path.join(SHOTS, "live-820x1180.png") });
    if (w === 768) await host.screenshot({ path: path.join(SHOTS, "live-768x1024.png") });
  }
  // Querformat-Tablet ohne genug Höhe bleibt in der einfachen Ansicht
  await host.setViewportSize({ width: 900, height: 700 });
  await sleep(400);
  g = await geo(host);
  check(!g.live && g.scrollX <= 0, "900×700: einfache Ansicht, kein Sendemodus, kein Querscrollen");
  // Sweep Tablet: 760 bis 1099 px bei 1000 px Höhe, nie Überlauf
  const bad2 = [];
  for (let w = 1099; w >= 760; w -= 7) {
    await host.setViewportSize({ width: w, height: 1000 });
    await host.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
    const r = await host.evaluate(() => ({ live: document.body.classList.contains("ps-live"), x: document.documentElement.scrollWidth - window.innerWidth, y: document.documentElement.scrollHeight - window.innerHeight }));
    if (r.x > 0 || r.y > 0 || !r.live) bad2.push(`${w}px quer ${r.x} hoch ${r.y} live ${r.live}`);
  }
  check(!bad2.length, `Sweep 1099–760 px bei 1000 px Höhe: Sendemodus, nie Überlauf${bad2.length ? " → " + bad2.slice(0, 5).join("; ") : ""}`);
  await host.setViewportSize({ width: 1920, height: 1080 });
  await sleep(400);

  // Breiten-Sweep im Sendemodus und darunter: nie quer scrollen, über 1100 px nie hoch
  const bad = [];
  for (let w = 1920; w >= 360; w -= 8) {
    await host.setViewportSize({ width: w, height: 800 });
    await host.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
    const r = await host.evaluate(() => ({ live: document.body.classList.contains("ps-live"), x: document.documentElement.scrollWidth - window.innerWidth, y: document.documentElement.scrollHeight - window.innerHeight }));
    if (r.x > 0 || (r.live && r.y > 0) || r.live !== (w >= 1100)) bad.push(`${w}px quer ${r.x} hoch ${r.y} live ${r.live}`);
  }
  check(!bad.length, `Sweep 1920–360 px: Sendemodus ab 1100 px, nie Überlauf${bad.length ? " → " + bad.slice(0, 5).join("; ") : ""}`);
  await host.setViewportSize({ width: 1280, height: 800 });
  await sleep(300);
  const playing = await host.evaluate(() => [...document.querySelectorAll(".ps-tile video")].every((v) => v.videoWidth > 0 && !v.paused));
  check(playing && (await geo(host)).live, "nach dem Hin- und Herschalten laufen die Kamerabilder weiter");

  // Stopp: zurück in die gewohnte Ansicht
  await host.click("#ps-stop");
  await host.click("#ps-stop-confirm");
  await host.waitForFunction(() => !document.body.classList.contains("ps-live"), null, { timeout: 15000 });
  g = await geo(host);
  const order = await host.evaluate(() => [...document.querySelector(".ps-main").children].filter((x) => !x.hidden).map((x) => x.className.split(" ")[0]));
  check(g.sidebar.shown && order.indexOf("ps-bar") < order.indexOf("ps-peers-wrap") && order.indexOf("ps-peers-wrap") < order.indexOf("ps-sounds") && order.indexOf("ps-sounds") < order.indexOf("ps-self"), `nach dem Beenden: gewohnte Ansicht in alter Reihenfolge (${order.join(", ")})`);
  check(await host.evaluate(() => !!document.querySelector(".ps-plan .ps-segments") && !!document.querySelector(".ps-plan .ps-assist")), "Sendeplan und Begleiter stehen wieder in ihrer Spalte");

  const errs = host.__errors.filter((e) => !/favicon|ERR_|Failed to load resource|404|Chunk verworfen/.test(e));
  check(!errs.length, `keine Konsolenfehler${errs.length ? ": " + errs.slice(0, 3).join(" | ") : ""}`);
  await b.close();
  await clearCurrent();
  if (failures.length) { log(`${failures.length} Fehler`); process.exit(1); }
  log("alles in Ordnung");
}

main().catch((e) => { console.error(e); process.exit(1); });
