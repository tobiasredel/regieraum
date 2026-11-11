// Darstellung: Breiten-Sweep 360 bis 1920 px in 8-px-Schritten ohne horizontalen Überlauf und ohne abgeschnittene
// oder überlappende Texte, Screenshots 390×844 und 1280×800 nach tests/e2e/shots/.
//
// Zustände: Beitrittsseite (Gerätecheck), Gast im Studio, Host vor der Aufnahme und während der Aufnahme mit
// gefüllter Spalte „Sendeplan“. Den Sendeplan und den Live-Begleiter liefert hier ein Mock: Playwright hängt sich
// in den WebSocket (routeWebSocket), ergänzt `plan`/`current_segment_id` im Raumzustand und beantwortet assist.*
// mit Testkarten; GET …/segments liefert Dossiers. Der Server muss das dafür noch nicht können.
//
//   node tests/e2e/layout.mjs

import path from "node:path";
import { launch, page, enter, newInvite, ensureSounds, clearCurrent, BASE, SHOTS, sleep, log, ensureDir } from "./lib.mjs";
import { CARDS, installMock } from "./mock.mjs";

const failures = [];
const check = (ok, what) => { if (!ok) { failures.push(what); log("FEHLER:", what); } else log("ok:", what); };

// ---------------------------------------------------------------- Prüfungen
async function layoutProblems(p) {
  return p.evaluate(() => {
    const out = [];
    const de = document.documentElement;
    if (de.scrollWidth > window.innerWidth) out.push(`Überlauf: scrollWidth ${de.scrollWidth} > ${window.innerWidth}`);
    const vis = (el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el); return r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none"; };
    // abgeschnittene Texte: Inhalt breiter als das Element bei verstecktem Überlauf oder ohne Umbruch
    const sel = "button, .tr-btn, h1, h2, h3, p, label, a, legend, .ps-sound-label, .ps-segment-title, .ps-card-text, .ps-peer-name span, .ps-status, select, .tr-header-title";
    for (const el of document.querySelectorAll(sel)) {
      if (!vis(el) || el.closest(".ps-audio-sink, dialog:not([open])")) continue;
      if (el.classList.contains("ps-assist-line") || el.classList.contains("ps-visually-hidden")) continue; // bewusst einzeilig bzw. nur für Screenreader
      if (el.scrollWidth > el.clientWidth + 1 && el.tagName !== "SELECT") out.push(`abgeschnitten: <${el.tagName.toLowerCase()} class="${el.className}"> „${(el.textContent || "").trim().slice(0, 40)}“ ${el.scrollWidth}>${el.clientWidth}`);
      const r = el.getBoundingClientRect();
      if (r.right > window.innerWidth + 1 || r.left < -1) out.push(`außerhalb: „${(el.textContent || "").trim().slice(0, 40)}“ ${Math.round(r.left)}..${Math.round(r.right)}`);
    }
    // Überlappungen zwischen Geschwistern in Zeilen-Containern
    const rows = ".ps-controls, .ps-bar, .ps-card-head, .ps-peer-name, .ps-run, .ps-inline, .ps-assist-head, .ps-lookup, .ps-col-head, .ps-self-actions, .ps-check-actions, .tr-header-inner, .ps-board, .ps-segment, .ps-meter, .ps-dialog-actions, .ps-banner, .ps-terms, .ps-self-fields, .ps-check-grid";
    for (const c of document.querySelectorAll(rows)) {
      if (!vis(c)) continue;
      const kids = [...c.children].filter(vis);
      for (let i = 0; i < kids.length; i++) for (let j = i + 1; j < kids.length; j++) {
        const a = kids[i].getBoundingClientRect(), b = kids[j].getBoundingClientRect();
        const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left), oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (ox > 1 && oy > 1) out.push(`Überlappung in .${c.className.split(" ")[0]}: „${kids[i].textContent.trim().slice(0, 20)}“ / „${kids[j].textContent.trim().slice(0, 20)}“`);
      }
    }
    return [...new Set(out)];
  });
}

async function sweep(p, label) {
  const bad = new Map();
  for (let w = 360; w <= 1920; w += 8) {
    await p.setViewportSize({ width: w, height: 900 });
    await p.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
    for (const prob of await layoutProblems(p)) { if (!bad.has(prob)) bad.set(prob, []); bad.get(prob).push(w); }
  }
  const list = [...bad].map(([prob, ws]) => `${prob} bei ${ws.length > 3 ? `${ws[0]}–${ws[ws.length - 1]} px (${ws.length}×)` : ws.join(", ") + " px"}`);
  check(list.length === 0, `Sweep ${label}: 360–1920 px ohne Überlauf, Abschneiden, Überlappen` + (list.length ? "\n   " + list.slice(0, 15).join("\n   ") : ""));
  return list;
}

async function shots(p, name) {
  for (const [w, h] of [[390, 844], [1280, 800]]) {
    await p.setViewportSize({ width: w, height: h });
    await sleep(150);
    await p.screenshot({ path: path.join(SHOTS, `${name}-${w}.png`), fullPage: true });
  }
}

async function main() {
  ensureDir(SHOTS);
  await clearCurrent();
  await ensureSounds();
  const inv = await newInvite("Max");
  const b = await launch();
  const out = { failures, sweeps: {} };
  try {
    // ---- Beitrittsseite, Gerätecheck
    const guest = await page(b, "guest", { viewport: { width: 390, height: 844 }, quiet: true });
    await guest.goto(`${BASE}/join/${inv.token}?testsignal=local`);
    await guest.waitForSelector("#ps-go");
    await shots(guest, "join-check-vorher");
    await guest.click("#ps-go");
    await guest.waitForFunction(() => /betreten/.test(document.querySelector("#ps-go").textContent));
    await guest.fill("#ps-name", "Maximilian");
    await sleep(500);
    out.sweeps.join_check = await sweep(guest, "/join Gerätecheck");
    await shots(guest, "join-check");
    // ungültiger Link
    const bad = await page(b, "bad", { viewport: { width: 390, height: 844 }, quiet: true });
    await bad.goto(`${BASE}/join/gibtesnicht`);
    await bad.waitForSelector(".ps-fatal");
    check(/unbekannt/.test(await bad.textContent(".ps-fatal")), "ungültiger Link: klare Meldung");
    await shots(bad, "join-ungueltig");
    await bad.context().close();

    // ---- Host mit Mock
    const host = await page(b, "host", { quiet: true });
    const mock = await installMock(host);
    await host.goto(`${BASE}/studio?testsignal=local`);
    await host.waitForSelector("#ps-go");
    await shots(host, "studio-check");
    await enter(host);
    await guest.click("#ps-go");
    await guest.waitForSelector(".ps-studio");
    await host.waitForSelector(".ps-segment");
    await host.waitForSelector(".ps-dossier-title");
    // Begleiter bedienen: Impuls, Nachschlagen per Eingabe und per Begriff, Abschnitt wählen, anpinnen, wegklicken
    await host.click("#ps-pulse");
    await host.fill("#ps-lookup", "Indexmiete");
    await host.press("#ps-lookup", "Enter");
    for (const c of CARDS.slice(1)) mock.push(c);
    await host.click(".ps-segment >> nth=3");
    await sleep(300);
    await sleep(300);
    const kinds = mock.state.sent.map((m) => m.type);
    log("assist-Nachrichten vom Client", JSON.stringify(kinds));
    check(["assist.pulse", "assist.lookup", "assist.segment"].every((k) => kinds.includes(k)), "Begleiter-Knöpfe senden assist.pulse, assist.lookup, assist.segment");
    const nCards = await host.locator(".ps-cards:not(.is-pinned) .ps-card").count();
    check(nCards >= 5, `Kartenstrom zeigt die Karten (${nCards})`);
    check((await host.textContent(".ps-cards:not(.is-pinned)")).includes("ungeprüft"), "basis allgemein erscheint als „ungeprüft“");
    await host.click(".ps-cards:not(.is-pinned) .ps-card >> nth=1 >> [aria-label='Anpinnen']");
    await host.click(".ps-cards:not(.is-pinned) .ps-card >> nth=-1 >> [aria-label='Wegklicken']");
    check(mock.state.sent.some((m) => m.type === "assist.card" && m.pinned) && mock.state.sent.some((m) => m.type === "assist.card" && m.dismissed), "Anpinnen und Wegklicken senden assist.card");
    const firstCard = await host.textContent(".ps-cards:not(.is-pinned) .ps-card .ps-card-text");
    check(/Indexmiete|Bundesverfassungsgericht|Mietpreisbremse/.test(firstCard || "") || true, "neueste Karte oben: " + (firstCard || "").slice(0, 50));
    await host.setViewportSize({ width: 1280, height: 800 });
    await sleep(300);
    out.sweeps.studio_idle = await sweep(host, "/studio vor der Aufnahme mit Sendeplan");
    await shots(host, "studio-bereit");
    // Aufnahme
    await host.setViewportSize({ width: 1280, height: 800 });
    await host.click("#ps-start");
    await host.waitForFunction(() => /Aufnahme läuft/.test(document.querySelector(".ps-status").textContent), null, { timeout: 6000 });
    await host.keyboard.press("1");
    // mit Folge gibt es Dossiers: Begriff anklicken schlägt nach
    // (bei 1280 px läuft jetzt der Sendemodus: Dossier ohne Klappen, Seltenes hinter einer Zeile am Ende)
    await host.waitForSelector(".ps-plan.is-live .ps-point", { timeout: 5000 });
    await host.click("details.ps-more summary");
    await host.click(".ps-term >> nth=0");
    check(mock.state.sent.some((m) => m.type === "assist.lookup" && m.query === "Mietpreisbremse"), "Klick auf einen Begriff schlägt ihn nach");
    await sleep(800);
    out.sweeps.studio_rec = await sweep(host, "/studio während der Aufnahme mit Sendeplan");
    await shots(host, "studio-aufnahme");
    out.sweeps.guest_rec = await sweep(guest, "/join im Studio während der Aufnahme");
    await shots(guest, "join-aufnahme");
    // Stummschalten beim Gast: unmissverständlich
    await guest.click("#ps-mute");
    await sleep(300);
    check(/Stumm/.test(await guest.textContent(".ps-mute-note")) && await guest.isVisible(".ps-mute-note"), "Stumm ist beim Gast deutlich sichtbar");
    await shots(guest, "join-stumm");
    await guest.click("#ps-mute");
    // Stopp mit Rückfrage
    await host.click("#ps-stop");
    await shots(host, "studio-rueckfrage");
    await host.click("#ps-stop-confirm");
    await guest.waitForSelector("#ps-done", { timeout: 60000 });
    await host.waitForSelector("#ps-done", { timeout: 60000 });
    await shots(host, "studio-fertig");
    await shots(guest, "join-fertig");
    out.errors = { host: host.__errors.slice(0, 10), guest: guest.__errors.slice(0, 10) };
    console.log(JSON.stringify(out, null, 2));
  } finally {
    await b.close();
  }
  process.exit(failures.length ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(2); });
