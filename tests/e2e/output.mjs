// Ausgang wählen: Gerätecheck und Studio-Panel, Stimmen sofort, Jingles (AudioContext) erst nach der Aufnahme,
// Wahl bleibt nach dem Neuladen, Browser ohne setSinkId (Safari) sehen keine Auswahl. Chromium bringt mit
// --use-fake-device-for-media-stream zwei Testausgänge mit.
//
//   ./dev.sh serve-e2e 8196      (anderes Terminal)
//   node tests/e2e/output.mjs

import { launch, page, enter, newInvite, clearCurrent, BASE, sleep, log } from "./lib.mjs";

const failures = [];
const check = (ok, what) => { if (!ok) { failures.push(what); log("FEHLER:", what); } else log("ok:", what); };

const state = (p) => p.evaluate(() => {
  const S = window.__studio;
  return {
    outId: S.outId, ctxSink: typeof S.ctx?.sinkId === "string" ? S.ctx.sinkId : "",
    peerSinks: [...S.media.values()].map((m) => m.audio.sinkId),
    prefs: JSON.parse(localStorage.getItem("ps.prefs") || "{}").outId,
  };
});

async function main() {
  await clearCurrent();
  const browser = await launch();
  const host = await page(browser, "host", { viewport: { width: 1280, height: 900 } });
  await host.goto(BASE + "/studio");
  await host.waitForSelector("#ps-out", { timeout: 15000 });
  check(await host.locator("#ps-out").isVisible() && await host.locator("#ps-out").isDisabled(), "Gerätecheck: Ausgang sichtbar, vor der Freigabe gesperrt");
  await host.click("#ps-go");
  await host.waitForFunction(() => !document.querySelector("#ps-out").disabled, null, { timeout: 15000 });
  const opts = await host.$$eval("#ps-out option", (os) => os.map((o) => ({ v: o.value, t: o.textContent })));
  check(opts[0].v === "" && opts.length >= 3, `Auswahl: Systemstandard und Geräte (${opts.map((o) => o.t).join(", ")})`);
  const [out1, out2] = [opts[1].v, opts[2].v];
  await host.selectOption("#ps-out", out2);
  await host.waitForFunction((id) => window.__studio.ctx && window.__studio.ctx.sinkId === id, out2, { timeout: 5000 });
  let s = await state(host);
  check(s.outId === out2 && s.ctxSink === out2 && s.prefs === out2, "Gerätecheck: Ausgang gewählt, Testton und Jingles laufen darüber, Wahl gespeichert");
  await host.click("#ps-go");
  await host.waitForSelector(".ps-studio", { timeout: 15000 });
  await host.waitForFunction(() => window.__studio.socket && window.__studio.socket.connected && window.__studio.room, null, { timeout: 15000 });
  check(await host.$eval("#ps-out2", (el) => el.value) === out2, "Studio: Feld Ausgang zeigt die Wahl aus dem Gerätecheck");

  const inv = await newInvite("Max", "single");
  const guest = await page(browser, "gast", { viewport: { width: 1280, height: 900 } });
  await guest.goto(inv.url.replace(/^https?:\/\/[^/]+/, BASE));
  await enter(guest, { name: "Max" });
  await host.waitForFunction(() => window.__studio.media.size === 1 && [...window.__studio.media.values()][0].audio.srcObject, null, { timeout: 20000 });
  await host.waitForFunction((id) => [...window.__studio.media.values()][0].audio.sinkId === id, out2, { timeout: 5000 });
  check(true, "die Stimme des Gasts läuft über den gewählten Ausgang");

  // Während der Aufnahme: Stimme wechselt sofort, der AudioContext (Rekorder) erst danach
  await host.click("#ps-start");
  await host.waitForFunction(() => /Aufnahme läuft/.test(document.querySelector(".ps-status").textContent), null, { timeout: 20000 });
  await host.click("#ps-tech-open"); // im Sendemodus liegen die Geräte im Fenster „Technik“
  await host.selectOption("#ps-out2", out1);
  await host.waitForFunction((id) => [...window.__studio.media.values()][0].audio.sinkId === id, out1, { timeout: 5000 });
  s = await state(host);
  check(s.ctxSink === out2, "während der Aufnahme bleibt der AudioContext beim alten Ausgang");
  check(await host.locator("text=Jingles wechseln den Ausgang nach der Aufnahme").count() === 1, "Hinweis: Jingles wechseln nach der Aufnahme");
  await sleep(2000);
  await host.click("#ps-tech-close");
  await host.click("#ps-stop");
  await host.click("#ps-stop-confirm");
  await host.waitForFunction((id) => window.__studio.ctx.sinkId === id, out1, { timeout: 15000 });
  check(true, "nach der Aufnahme wechselt auch der AudioContext");

  // Neuladen: Wahl bleibt
  await host.reload();
  await host.click("#ps-go");
  await host.waitForFunction(() => !document.querySelector("#ps-out").disabled, null, { timeout: 15000 });
  const label1 = opts[1].t;
  check(await host.$eval("#ps-out", (el) => el.selectedOptions[0].textContent) === label1, "nach dem Neuladen ist der zuletzt gewählte Ausgang vorausgewählt (wiedergefunden über den Namen)");
  check(await host.evaluate(() => window.__studio.ctx.sinkId === window.__studio.outId && !!window.__studio.outId), "nach dem Neuladen laufen Jingles wieder über diesen Ausgang");

  // Browser ohne setSinkId (Safari)
  const ctx = await browser.newContext();
  await ctx.addInitScript(() => { delete HTMLMediaElement.prototype.setSinkId; delete AudioContext.prototype.setSinkId; });
  const safari = await ctx.newPage();
  await safari.goto(BASE + "/studio");
  await safari.waitForSelector("#ps-go");
  check(await safari.locator("#ps-out").isHidden() && /im System eingestellt/.test(await safari.textContent("#ps-out-note")), "ohne setSinkId: keine Auswahl, Hinweis auf den Systemausgang");
  await ctx.close();

  const errs = [...host.__errors, ...guest.__errors].filter((e) => !/favicon|ERR_|Failed to load resource/.test(e));
  check(!errs.length, `keine Konsolenfehler${errs.length ? ": " + errs.slice(0, 3).join(" | ") : ""}`);
  await browser.close();
  if (failures.length) { log(`${failures.length} Fehler`); process.exit(1); }
  log("alles in Ordnung");
}

main().catch((e) => { console.error(e); process.exit(1); });
