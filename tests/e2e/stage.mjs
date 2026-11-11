// Bühne: Kamerabilder groß im Studio. Host und Gast mit Testkamera; geprüft werden Sichtbarkeit, Größe, laufendes
// Bild, die Anordnung (schmal: eigenes Bild klein in der Ecke, breit: nebeneinander) und der Breiten-Sweep
// 360 bis 1920 px ohne horizontalen Überlauf. Screenshots nach tests/e2e/shots/stage-*.png.
//
//   ./dev.sh serve-e2e 8196      (anderes Terminal)
//   node tests/e2e/stage.mjs

import path from "node:path";
import { launch, page, enter, newInvite, clearCurrent, BASE, SHOTS, sleep, log, ensureDir } from "./lib.mjs";

const failures = [];
const check = (ok, what) => { if (!ok) { failures.push(what); log("FEHLER:", what); } else log("ok:", what); };

const stage = (p) => p.evaluate(() => {
  const wrap = document.querySelector(".ps-stage-wrap");
  const box = (el) => { const r = el.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }; };
  return {
    hidden: !wrap || wrap.hidden,
    others: document.querySelector(".ps-stage")?.dataset.others,
    wrap: wrap ? box(wrap) : null,
    tiles: [...document.querySelectorAll(".ps-tile")].map((t) => {
      const v = t.querySelector("video");
      return { ...box(t), self: t.classList.contains("is-self"), off: t.classList.contains("is-off"), abs: getComputedStyle(t).position === "absolute",
        playing: !!v && v.videoWidth > 0 && !v.paused, vw: v ? v.videoWidth : 0, vh: v ? v.videoHeight : 0,
        tag: t.querySelector(".ps-tile-tag") && !t.querySelector(".ps-tile-tag").hidden ? t.querySelector(".ps-tile-tag").textContent : "", big: t.querySelector(".ps-tile-big").hidden ? "" : t.querySelector(".ps-tile-big").textContent };
    }),
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    miniVideos: document.querySelectorAll(".ps-peers video, .ps-self video").length,
  };
});

async function sweep(p, name) {
  const bad = [];
  for (let w = 360; w <= 1920; w += 8) {
    await p.setViewportSize({ width: w, height: 900 });
    const r = await p.evaluate(() => {
      const de = document.documentElement;
      const tiles = [...document.querySelectorAll(".ps-tile")].map((t) => t.getBoundingClientRect());
      const wrap = document.querySelector(".ps-stage-wrap").getBoundingClientRect();
      return { over: de.scrollWidth - de.clientWidth, out: tiles.some((t) => t.left < wrap.left - 1 || t.right > wrap.right + 1 || t.width < 90) };
    });
    if (r.over > 0 || r.out) bad.push(`${w}px${r.over > 0 ? " Überlauf " + r.over : ""}${r.out ? " Kachel außerhalb oder zu klein" : ""}`);
  }
  check(!bad.length, `${name}: Breiten-Sweep 360–1920 ohne Überlauf${bad.length ? " → " + bad.slice(0, 6).join(", ") : ""}`);
}

async function main() {
  ensureDir(SHOTS);
  await clearCurrent();
  const browser = await launch();
  const host = await page(browser, "host", { viewport: { width: 1280, height: 800 } });
  await host.goto(BASE + "/studio");
  await enter(host);
  let s = await stage(host);
  check(s.hidden, "ohne Kamera bleibt die Bühne verborgen");

  // Host allein mit Kamera: eigenes Bild groß
  await host.click("#ps-camtoggle");
  await host.waitForFunction(() => { const v = document.querySelector(".ps-tile.is-self video"); return v && v.videoWidth > 0; }, null, { timeout: 15000 });
  s = await stage(host);
  check(!s.hidden && s.tiles.length === 1 && s.tiles[0].self && !s.tiles[0].abs && s.tiles[0].w >= 400, `allein: eigenes Bild groß (${s.tiles[0] && s.tiles[0].w}×${s.tiles[0] && s.tiles[0].h})`);
  check(s.miniVideos === 0, "kein kleines Video mehr in Teilnehmerliste oder „Eigenes Gerät“");
  check(s.tiles[0].vw >= 480, `Kamera liefert mehr als das alte 320er-Bild (${s.tiles[0].vw}×${s.tiles[0].vh})`);

  // Gast kommt ohne Kamera dazu: Namenskachel groß, eigenes Bild klein in der Ecke (Spalte ist schmal)
  const inv = await newInvite("Max", "single");
  const guest = await page(browser, "gast", { viewport: { width: 1280, height: 800 } });
  await guest.goto(inv.url.replace(/^https?:\/\/[^/]+/, BASE));
  await enter(guest, { name: "Max" });
  await host.waitForFunction(() => document.querySelectorAll(".ps-tile").length === 2, null, { timeout: 15000 });
  s = await stage(host);
  const other = s.tiles.find((t) => !t.self), me = s.tiles.find((t) => t.self);
  check(other.off && other.big === "Max", "Gegenüber ohne Kamera: Kachel mit Namen");
  check(me.abs && me.w < other.w / 2, `schmale Spalte: eigenes Bild klein in der Ecke (${me.w} in ${other.w})`);
  await guest.waitForFunction(() => { const v = document.querySelector(".ps-tile:not(.is-self) video"); return v && v.videoWidth > 0; }, null, { timeout: 30000 });
  s = await stage(guest);
  check(!s.hidden && s.tiles.length === 1 && s.tiles[0].w >= 600, `Gast sieht den Host groß, auch ohne eigene Kamera (${s.tiles[0].w}×${s.tiles[0].h})`);

  // Gast schaltet die Kamera ein: beim Host läuft das Bild groß
  await guest.click("#ps-camtoggle");
  await host.waitForFunction(() => { const v = document.querySelector(".ps-tile:not(.is-self) video"); return v && v.videoWidth > 0 && !v.paused; }, null, { timeout: 30000 });
  await guest.waitForFunction(() => { const v = document.querySelector(".ps-tile:not(.is-self) video"); return v && v.videoWidth > 0 && !v.paused; }, null, { timeout: 30000 });
  await sleep(1500);
  s = await stage(host);
  const o2 = s.tiles.find((t) => !t.self);
  check(o2.playing && o2.w >= 400 && o2.tag.startsWith("Max"), `Host 1280: Bild des Gasts groß (${o2.w}×${o2.h}, Quelle ${o2.vw}×${o2.vh}), Namensschild „${o2.tag}“`);
  await host.evaluate(() => window.scrollTo(0, 0));
  await host.screenshot({ path: path.join(SHOTS, "stage-host-1280.png") });

  // nach einem Raum-Update (Stummschalten) läuft das Bild weiter und die Kachel ist dieselbe
  await host.evaluate(() => { document.querySelector(".ps-tile:not(.is-self)").dataset.keep = "1"; });
  await guest.click("#ps-mute");
  await host.waitForFunction(() => /stumm/.test(document.querySelector(".ps-tile:not(.is-self) .ps-tile-tag").textContent) && !document.querySelector(".ps-tile:not(.is-self) .ps-tile-tag .tr-status-warn").hidden, null, { timeout: 10000 });
  s = await stage(host);
  check(s.tiles.find((t) => !t.self).playing && await host.evaluate(() => document.querySelector(".ps-tile:not(.is-self)").dataset.keep === "1"), "Stummschalten: Kachel bleibt stehen, Bild läuft weiter, „stumm“ im Namensschild");
  await guest.click("#ps-mute");

  // Host am großen Bildschirm: Die Spalte neben dem Sendeplan bleibt unter 720 px, also Gegenüber groß, selbst klein
  await host.setViewportSize({ width: 1920, height: 1080 });
  await sleep(300);
  s = await stage(host);
  const ho = s.tiles.find((t) => !t.self), hs = s.tiles.find((t) => t.self);
  check(ho.w >= 640 && hs.abs && hs.x + hs.w <= ho.x + ho.w && hs.y + hs.h <= ho.y + ho.h, `Host 1920: Gegenüber groß (${ho.w}×${ho.h}), eigenes Bild in der Ecke (${hs.w}×${hs.h})`);
  await host.evaluate(() => window.scrollTo(0, 0));
  await host.screenshot({ path: path.join(SHOTS, "stage-host-1920.png") });

  // Gast am Handy und am Rechner
  await guest.setViewportSize({ width: 390, height: 844 });
  await sleep(300);
  s = await stage(guest);
  const go = s.tiles.find((t) => !t.self), gs = s.tiles.find((t) => t.self);
  check(go.w >= 340 && gs.abs && s.overflow <= 0, `Gast 390: Gegenüber über die volle Breite (${go.w}×${go.h}), eigenes Bild in der Ecke (${gs.w})`);
  await guest.evaluate(() => window.scrollTo(0, 0));
  await guest.screenshot({ path: path.join(SHOTS, "stage-gast-390.png") });
  // Gast am Rechner: Bühne ist breit genug, beide Bilder gleich groß nebeneinander
  await guest.setViewportSize({ width: 1280, height: 800 });
  await sleep(300);
  s = await stage(guest);
  const [a, b] = s.tiles;
  check(s.tiles.every((t) => !t.abs) && Math.abs(a.w - b.w) <= 2 && a.y === b.y && a.w >= 380, `Gast 1280: zwei gleich große Bilder nebeneinander (${a.w}×${a.h})`);
  await guest.evaluate(() => window.scrollTo(0, 0));
  await guest.screenshot({ path: path.join(SHOTS, "stage-gast-1280.png") });

  await sweep(host, "Host");
  await sweep(guest, "Gast");

  // Kamera aus: Bühne verschwindet wieder, wenn niemand mehr ein Bild hat
  await host.setViewportSize({ width: 1280, height: 800 });
  await host.click("#ps-camtoggle");
  await guest.click("#ps-camtoggle");
  await host.waitForFunction(() => document.querySelector(".ps-stage-wrap").hidden, null, { timeout: 10000 });
  check(true, "beide Kameras aus: Bühne verborgen");
  const errs = [...host.__errors, ...guest.__errors].filter((e) => !/favicon|ERR_|Failed to load resource/.test(e));
  check(!errs.length, `keine Konsolenfehler${errs.length ? ": " + errs.slice(0, 3).join(" | ") : ""}`);

  await browser.close();
  if (failures.length) { log(`${failures.length} Fehler`); process.exit(1); }
  log("alles in Ordnung");
}

main().catch((e) => { console.error(e); process.exit(1); });
