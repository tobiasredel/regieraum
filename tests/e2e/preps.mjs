// Mehrere Vorbereitungen: auf der Seite „Vorbereitung“ anlegen, wechseln, benennen und fürs Studio wählen; im Studio
// festlegen, worauf die Aufnahme aufbaut (eine Vorbereitung oder keine).
//
//   PS_JOBS=0 ./dev.sh serve-e2e 8196      (anderes Terminal)
//   node tests/e2e/preps.mjs

import path from "node:path";
import { launch, page, enter, clearCurrent, api, BASE, SHOTS, sleep, log, ensureDir } from "./lib.mjs";

const failures = [];
const check = (ok, what) => { if (!ok) { failures.push(what); log("FEHLER:", what); } else log("ok:", what); };

async function main() {
  ensureDir(SHOTS);
  await clearCurrent();
  for (const p of (await api("GET", "/api/host/preparations")).preparations) await api("DELETE", `/api/host/episodes/${p.id}`);
  const a = await api("POST", "/api/host/preparations", { label: "Woche 46" });
  const b = await api("POST", "/api/host/preparations", { label: "Sonderfolge Wahl" });
  const segsA = await api("GET", `/api/host/episodes/${a.id}/segments`);
  await api("PATCH", `/api/host/segments/${segsA[0].id}`, { title: "Bahnstreik" });

  const browser = await launch();
  const p = await page(browser, "host", { viewport: { width: 1280, height: 800 }, quiet: true });
  // Vorbereitung: ohne Wahl öffnet die neueste
  await p.goto(BASE + "/plan");
  await p.waitForSelector("#m-prep");
  check((await p.inputValue("#m-prep")) === b.id && (await p.inputValue("#m-prep-label")) === "Sonderfolge Wahl", "ohne Wahl öffnet die neueste Vorbereitung");
  check(/im Studio gewählt/.test(await p.textContent(`#m-prep option[value="${b.id}"]`)), "die Wahl des Studios ist markiert");
  // wechseln
  await p.selectOption("#m-prep", a.id);
  await p.waitForFunction((id) => location.search.includes(id) && document.querySelector("#m-prep-label").value === "Woche 46", a.id);
  check((await p.inputValue(".m-seg input >> nth=0")) === "Bahnstreik", "Wechsel zeigt den Sendeplan der gewählten Vorbereitung");
  // umbenennen
  await p.fill("#m-prep-label", "Woche 46, Bahn");
  await p.waitForFunction(() => /Gespeichert|Saved/.test(document.querySelector(".m-prep-head .m-save-state").textContent), null, { timeout: 5000 });
  check((await api("GET", "/api/host/preparations")).preparations.find((x) => x.id === a.id).label === "Woche 46, Bahn", "Arbeitstitel gespeichert");
  // im Studio verwenden
  await p.click("text=Im Studio verwenden");
  await p.waitForSelector("text=Das Studio nimmt als Nächstes hierauf auf");
  check((await api("GET", "/api/host/preparations")).selected === a.id, "„Im Studio verwenden“ setzt die Wahl");
  // neue Vorbereitung
  await p.click("text=Neue Vorbereitung");
  await p.waitForFunction((n) => document.querySelectorAll("#m-prep option").length === n, 3);
  check((await p.inputValue("#m-prep-label")) === "" && (await p.locator(".m-seg").count()) === 5, "neue Vorbereitung mit Standardplan");
  const c = await p.inputValue("#m-prep");
  await p.screenshot({ path: path.join(SHOTS, "preps-plan-1280.png") });
  // löschen
  await p.click("text=Vorbereitung löschen");
  await p.click("dialog[open] button.m-danger, dialog[open] button:has-text('Löschen')");
  await p.waitForFunction((n) => document.querySelectorAll("#m-prep option").length === n, 2);
  check(!(await api("GET", "/api/host/preparations")).preparations.some((x) => x.id === c), "Vorbereitung gelöscht");
  // Folgenliste zeigt Vorbereitungen nicht als Folgen
  await p.goto(BASE + "/episodes");
  await p.waitForSelector("main");
  await sleep(500);
  const txt = await p.textContent("main");
  check(/2 Vorbereitungen sind in Arbeit/.test(txt) && !/Woche 46/.test(txt), "Folgenliste nennt die Vorbereitungen nur als Hinweis");

  // Studio: Auswahl vor der Aufnahme
  await p.goto(`${BASE}/studio?testsignal=local`);
  await enter(p);
  await p.waitForSelector("#ps-prep");
  const opts = await p.$$eval("#ps-prep option", (os) => os.map((o) => [o.value, o.textContent, o.selected]));
  check(opts.length === 3 && opts[2][0] === "" && /Ohne Vorbereitung/.test(opts[2][1]), `Auswahl: zwei Vorbereitungen und „Ohne Vorbereitung“ (${opts.map((o) => o[1]).join(" | ")})`);
  check(opts.find((o) => o[2])[0] === a.id && /Woche 46, Bahn · 0 von 5 Dossiers/.test(opts.find((o) => o[2])[1]), "gewählt ist die Vorbereitung aus der Seite Vorbereitung");
  await p.waitForFunction(() => /Bahnstreik/.test(document.querySelector(".ps-plan").textContent));
  check(true, "Sendeplan-Spalte zeigt die gewählte Vorbereitung");
  // ohne Vorbereitung
  await p.selectOption("#ps-prep", "");
  await p.waitForFunction(() => /Kein Sendeplan|No running order/.test(document.querySelector(".ps-plan").textContent), null, { timeout: 8000 });
  check((await api("GET", "/api/host/preparations")).selected === null, "„Ohne Vorbereitung“ gewählt, Sendeplan-Spalte leer");
  // zurück zu B und aufnehmen
  await p.selectOption("#ps-prep", b.id);
  await p.waitForFunction(() => !/Kein Sendeplan/.test(document.querySelector(".ps-plan").textContent), null, { timeout: 8000 });
  await p.screenshot({ path: path.join(SHOTS, "preps-studio-1280.png") });
  await p.click("#ps-start");
  await p.waitForFunction(() => document.body.classList.contains("ps-live"), null, { timeout: 15000 });
  const rec = (await api("GET", "/api/host/episodes")).find((e) => e.status === "recording");
  check(rec && rec.id === b.id, "die Aufnahme läuft in der gewählten Vorbereitung");
  check((await api("GET", "/api/host/preparations")).preparations.map((x) => x.id).join() === a.id, "die übrige Vorbereitung bleibt");
  await p.click("#ps-stop");
  await p.click("#ps-stop-confirm");
  await p.waitForSelector("#ps-discard", { timeout: 20000 });
  await p.click("#ps-discard");
  await p.click("#ps-ask-ok");
  await p.waitForSelector("#ps-prep", { timeout: 20000 });
  const after = await api("GET", "/api/host/preparations");
  check(after.selected === b.id && after.preparations.length === 2, "nach dem Verwerfen ist die Folge wieder Vorbereitung und gewählt");

  const errs = p.__errors.filter((e) => !/favicon|ERR_|Failed to load resource|404|Chunk verworfen/.test(e));
  check(!errs.length, `keine Konsolenfehler${errs.length ? ": " + errs.slice(0, 3).join(" | ") : ""}`);
  await browser.close();
  await clearCurrent();
  if (failures.length) { log(`${failures.length} Fehler`); process.exit(1); }
  log("alles in Ordnung");
}

main().catch((e) => { console.error(e); process.exit(1); });
