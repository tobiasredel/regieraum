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
  // Abschnitt in eine andere Vorbereitung kopieren und verschieben
  await p.selectOption("#m-prep", a.id);
  await p.waitForFunction(() => document.querySelector("#m-prep-label").value === "Woche 46, Bahn");
  await p.waitForFunction(() => document.querySelectorAll(".m-seg").length === 5);
  await p.fill("textarea >> nth=1", "Notiz zum Streik"); // Notizen des ersten Abschnitts
  await p.waitForFunction(() => /Gespeichert|Saved/.test(document.querySelector(".m-seg .m-save-state").textContent), null, { timeout: 5000 });
  const transferBtn = p.locator(".m-seg >> nth=0").getByRole("button", { name: /in andere Vorbereitung/i });
  check(await transferBtn.count() === 1, "Knopf „In andere Vorbereitung“ je Abschnitt (zwei Vorbereitungen)");
  await transferBtn.click();
  await p.waitForSelector("dialog[open] #m-transfer-target");
  const targets = await p.$$eval("#m-transfer-target option", (os) => os.map((o) => o.textContent));
  check(targets.length === 1 && /Sonderfolge Wahl/.test(targets[0]), `Dialog bietet nur die andere Vorbereitung an (${targets.join(" | ")})`);
  await p.screenshot({ path: path.join(SHOTS, "preps-transfer-dialog-1280.png") });
  await p.click("dialog[open] button:has-text('Kopieren')");
  await p.waitForSelector(".tr-toast >> text=kopiert");
  let bsegs = await api("GET", `/api/host/episodes/${b.id}/segments`);
  let copied = bsegs.find((x) => x.title === "Bahnstreik");
  check(bsegs.length === 6 && copied && copied.notes === "Notiz zum Streik" && copied.sort === 5, "Kopie hängt hinten an der Ziel-Vorbereitung an, mit Notizen");
  check((await api("GET", `/api/host/episodes/${a.id}/segments`)).length === 5 && (await p.locator(".m-seg").count()) === 5, "die Quelle behält den Abschnitt beim Kopieren");
  await p.locator(".m-seg >> nth=0").getByRole("button", { name: /in andere Vorbereitung/i }).click();
  await p.waitForSelector("dialog[open] #m-transfer-target");
  await p.click("dialog[open] button:has-text('Verschieben')");
  await p.waitForSelector(".tr-toast >> text=verschoben");
  await p.waitForFunction(() => document.querySelectorAll(".m-seg").length === 4);
  bsegs = await api("GET", `/api/host/episodes/${b.id}/segments`);
  check(bsegs.length === 7 && bsegs.filter((x) => x.title === "Bahnstreik").length === 2, "Verschieben nimmt den Abschnitt aus der Quelle und hängt ihn ans Ziel");
  check(!(await p.locator(".m-seg input >> nth=0").inputValue()).includes("Bahnstreik"), "die Quelle zeigt den Abschnitt nicht mehr");

  // zurückstellen, damit die folgenden Prüfungen vom Ausgangszustand ausgehen
  await api("POST", `/api/host/segments/${segsA[0].id}/transfer`, { target_episode_id: a.id, mode: "move" });
  await api("DELETE", `/api/host/segments/${copied.id}`);
  check((await api("GET", `/api/host/episodes/${a.id}/segments`)).length === 5 && (await api("GET", `/api/host/episodes/${b.id}/segments`)).length === 5, "Ausgangszustand wiederhergestellt");

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
