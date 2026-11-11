// Aufnahme verwerfen und neu aufnehmen: Host und Gast nehmen auf, der Host beendet und verwirft. Danach ist das
// Studio wieder bereit, der Sendeplan mit seinem Inhalt steht noch da, und eine neue Aufnahme läuft sauber an.
//
//   ./dev.sh serve-e2e 8196      (anderes Terminal; PS_JOBS=0, damit nichts verarbeitet wird)
//   node tests/e2e/discard.mjs

import { launch, page, enter, newInvite, clearCurrent, api, BASE, sleep, log } from "./lib.mjs";

const failures = [];
const check = (ok, what) => { if (!ok) { failures.push(what); log("FEHLER:", what); } else log("ok:", what); };
const status = (p) => p.evaluate(() => document.querySelector(".ps-status").textContent);

async function main() {
  await clearCurrent();
  const { episode } = await api("POST", "/api/host/episodes/draft");
  const segs = await api("GET", `/api/host/episodes/${episode.id}/segments`);
  await api("PATCH", `/api/host/segments/${segs[0].id}`, { title: "Rente mit 70", notes: "mit Hamburg einsteigen" });

  const browser = await launch();
  const host = await page(browser, "host");
  await host.goto(BASE + "/studio");
  await enter(host);
  const inv = await newInvite("Max", "single");
  const guest = await page(browser, "gast");
  await guest.goto(inv.url.replace(/^https?:\/\/[^/]+/, BASE));
  await enter(guest, { name: "Max" });

  await host.click("#ps-start");
  await host.waitForFunction(() => /Aufnahme läuft/.test(document.querySelector(".ps-status").textContent), null, { timeout: 20000 });
  await sleep(5000);
  check(await host.locator("#ps-discard").count() === 0, "während der Aufnahme gibt es keinen Verwerfen-Knopf");
  await host.click("#ps-stop");
  await host.click("#ps-stop-confirm");
  await host.waitForSelector("#ps-discard", { timeout: 20000 });
  check(true, "nach dem Beenden bietet der Host „Aufnahme verwerfen“ an");
  check(await guest.locator("#ps-discard").count() === 0, "der Gast hat keinen Verwerfen-Knopf");

  await host.click("#ps-discard");
  await host.waitForSelector("#ps-ask-ok");
  const text = await host.evaluate(() => document.querySelector("dialog[open] p").textContent);
  check(/Sendeplan mit den Dossiers bleibt/.test(text), "Rückfrage sagt, dass der Sendeplan bleibt");
  await host.click("#ps-ask-ok");
  await host.waitForFunction(() => document.querySelector(".ps-status").textContent === "Bereit" && document.querySelector("#ps-start"), null, { timeout: 20000 });
  check(true, "Host: wieder „Bereit“ mit Startknopf");
  await guest.waitForFunction(() => document.querySelector(".ps-status").textContent === "Bereit" && document.querySelector(".ps-upload").hidden, null, { timeout: 20000 });
  check(true, "Gast: wieder „Bereit“, kein Upload-Hinweis mehr");
  check(await host.evaluate(() => document.querySelector(".ps-upload").hidden), "Host: Upload-Hinweis ist weg");

  const eps = await api("GET", "/api/host/episodes");
  check(!eps.some((e) => e.id === episode.id), "die verworfene Folge gibt es nicht mehr");
  const cur = (await api("POST", "/api/host/episodes/draft")).episode;
  const plan = await api("GET", `/api/host/episodes/${cur.id}/segments`);
  check(cur.id !== episode.id && plan[0].id === segs[0].id && plan[0].title === "Rente mit 70" && plan[0].notes === "mit Hamburg einsteigen", "der Sendeplan steht im neuen Entwurf");
  await host.waitForFunction(() => /Rente mit 70/.test(document.querySelector(".ps-plan").textContent), null, { timeout: 10000 });
  check(true, "Studio zeigt den übernommenen Sendeplan");

  // neue Aufnahme läuft sauber an, beide laden wieder hoch
  await sleep(1500);
  await host.click("#ps-start");
  await host.waitForFunction(() => /Aufnahme läuft/.test(document.querySelector(".ps-status").textContent), null, { timeout: 20000 });
  await sleep(6000);
  const ep2 = await api("GET", `/api/host/episodes/${cur.id}`);
  const names = ep2.tracks.map((t) => t.participant_name).sort();
  check(ep2.status === "recording" && names.length === 2, `neue Aufnahme hat Spuren von beiden (${names.join(", ")})`);
  const prog = await host.evaluate(() => [...window.__studio.progress.values()].map((p) => p.uploaded_frames || 0));
  check(prog.length >= 1 && prog.every((f) => f > 0), `Upload der neuen Aufnahme läuft (${prog.join(", ")} Frames)`);
  await host.click("#ps-stop");
  await host.click("#ps-stop-confirm");
  await host.waitForSelector("#ps-discard", { timeout: 20000 });

  const errs = [...host.__errors, ...guest.__errors].filter((e) => !/favicon|ERR_|Failed to load resource|404/.test(e));
  check(!errs.length, `keine Konsolenfehler${errs.length ? ": " + errs.slice(0, 3).join(" | ") : ""}`);
  await browser.close();
  await clearCurrent();
  if (failures.length) { log(`${failures.length} Fehler`); process.exit(1); }
  log("alles in Ordnung");
}

main().catch((e) => { console.error(e); process.exit(1); });
