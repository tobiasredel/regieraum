// Gerätecheck: Hörprobe (fünf Sekunden aufnehmen, abspielen, noch einmal) bei Host und Gast, Hinweis auf dem Handy,
// Mikrofon-Zugriff kommt nur einmal und nichts geht an den Server (keine Anfrage mit Audio).
//
//   ./dev.sh serve-e2e 8196      (anderes Terminal)
//   node tests/e2e/probe.mjs

import path from "node:path";
import { launch, page, newInvite, BASE, SHOTS, sleep, log, ensureDir } from "./lib.mjs";

const failures = [];
const check = (ok, what) => { if (!ok) { failures.push(what); log("FEHLER:", what); } else log("ok:", what); };

async function grant(p) {
  await p.waitForSelector("#ps-go", { timeout: 15000 });
  await p.click("#ps-go");
  await p.waitForFunction(() => /betreten/i.test(document.querySelector("#ps-go").textContent), null, { timeout: 15000 });
}

async function probe(p, who) {
  const before = await p.locator("#ps-probe-rec").isDisabled();
  check(before === false, `${who}: Knopf „Hörprobe“ ist nach der Mikrofonfreigabe bedienbar`);
  const uploads = [];
  p.on("request", (r) => { if (r.method() !== "GET" && /audio|chunk|probe/i.test(r.url())) uploads.push(r.url()); });
  const t0 = Date.now();
  await p.click("#ps-probe-rec");
  await p.waitForFunction(() => /noch [1-5] s/.test(document.querySelector(".ps-probe-status").textContent));
  const left1 = await p.textContent(".ps-probe-status");
  await sleep(2200);
  const left2 = await p.textContent(".ps-probe-status");
  check(/noch \d s/.test(left1) && /noch \d s/.test(left2) && left1 !== left2, `${who}: Restzeit läuft herunter („${left1.trim()}“ → „${left2.trim()}“)`);
  await p.waitForFunction(() => document.querySelector("#ps-probe").dataset.state === "ready", null, { timeout: 10000 });
  const took = Date.now() - t0;
  check(took >= 4500 && took < 8000, `${who}: Aufnahme dauert etwa fünf Sekunden (${took} ms)`);
  const vis = await p.evaluate(() => ({ play: !document.querySelector("#ps-probe-play").hidden, again: !document.querySelector("#ps-probe-again").hidden, rec: document.querySelector("#ps-probe-rec").hidden }));
  check(vis.play && vis.again && vis.rec, `${who}: danach „Abspielen“ und „Noch einmal“`);
  await p.click("#ps-probe-play");
  await p.waitForFunction(() => document.querySelector("#ps-probe").dataset.played === "1", null, { timeout: 8000 });
  const pb = await p.evaluate(() => { const el = document.querySelector("#ps-probe"); return { played: el.dataset.played }; });
  check(pb.played === "1", `${who}: Wiedergabe startet`);
  await sleep(1200);
  const pos = parseFloat(await p.evaluate(() => document.querySelector("#ps-probe").dataset.pos || "-1"));
  check(pos > 0.5, `${who}: Wiedergabe läuft weiter (Position ${pos} s)`);
  await p.click("#ps-probe-again");
  await p.waitForFunction(() => document.querySelector("#ps-probe").dataset.state === "recording");
  check(true, `${who}: „Noch einmal“ beginnt eine neue Probe`);
  await p.waitForFunction(() => document.querySelector("#ps-probe").dataset.state === "ready", null, { timeout: 10000 });
  check(uploads.length === 0, `${who}: nichts geht an den Server${uploads.length ? " → " + uploads.join(", ") : ""}`);
}

async function main() {
  ensureDir(SHOTS);
  const b = await launch();

  // Host mit Testsignal (Klick je Sekunde statt Mikrofon)
  const host = await page(b, "host", { quiet: true });
  await host.goto(`${BASE}/studio?testsignal=local`);
  await host.waitForSelector("#ps-go");
  check(await host.locator("#ps-probe-rec").isDisabled(), "Host: vor der Freigabe ist die Hörprobe gesperrt");
  check(await host.locator("#ps-mobile-note").count() === 0, "Host am Rechner: kein Hinweis zum Hintergrund");
  await grant(host);
  await probe(host, "Host (Testsignal)");
  await host.screenshot({ path: path.join(SHOTS, "check-probe-host.png") });

  // Host mit dem Fake-Mikrofon des Browsers, ohne Testsignal
  const host2 = await page(b, "host2", { quiet: true });
  await host2.goto(`${BASE}/studio`);
  await grant(host2);
  await probe(host2, "Host (Mikrofon)");

  // Gast auf dem Handy: Hinweis vorab, Hörprobe, Beitritt klappt danach noch
  const inv = await newInvite("Max", "single");
  const guest = await page(b, "gast", { quiet: true, viewport: { width: 390, height: 844 }, ctx: { isMobile: true, hasTouch: true } });
  await guest.addInitScript(() => { Object.defineProperty(navigator, "userAgentData", { get: () => ({ mobile: true, brands: [], platform: "iPadOS" }) }); });
  await guest.goto(inv.url.replace(/^https?:\/\/[^/]+/, BASE) + "?testsignal=local");
  await guest.waitForSelector("#ps-go");
  const note = await guest.locator("#ps-mobile-note").textContent().catch(() => "");
  check(/Vordergrund/.test(note) && /Bildschirm nicht sperren/.test(note) && /Ladekabel/.test(note), `Gast auf dem Handy: Hinweis vorab („${(note || "").trim().slice(0, 60)}…“)`);
  await grant(guest);
  await probe(guest, "Gast (Handy)");
  const over = await guest.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check(over <= 0, `Gast 390 px: kein Querscrollen (${over})`);
  await guest.screenshot({ path: path.join(SHOTS, "check-probe-gast-390.png") });
  await guest.fill("#ps-name", "Max");
  await guest.click("#ps-go");
  await guest.waitForSelector(".ps-studio", { timeout: 15000 });
  check(true, "Gast: Beitritt nach der Hörprobe funktioniert");

  // Ohne MediaRecorder bleibt der Gerätecheck sauber (Hörprobe ausgeblendet)
  const bare = await page(b, "ohne", { quiet: true });
  await bare.addInitScript(() => { delete window.MediaRecorder; });
  await bare.goto(`${BASE}/studio?testsignal=local`);
  await grant(bare);
  check(await bare.locator("#ps-probe").isHidden(), "Ohne MediaRecorder ist die Hörprobe ausgeblendet");

  const errs = [...host.__errors, ...host2.__errors, ...guest.__errors].filter((e) => !/favicon|ERR_|Failed to load resource|404/.test(e));
  check(!errs.length, `keine Konsolenfehler${errs.length ? ": " + errs.slice(0, 3).join(" | ") : ""}`);
  await b.close();
  if (failures.length) { log(`${failures.length} Fehler`); process.exit(1); }
  log("alles in Ordnung");
}

main().catch((e) => { console.error(e); process.exit(1); });
