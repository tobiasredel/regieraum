// Ausfall von Mikrofon und Ausgang während der Aufnahme: Wechsel auf das Standardgerät (neue Spur, Banner), Rückkehr,
// sobald das Gerät wieder da ist, gespeicherte Wahl bleibt. Das Abziehen wird simuliert: enumerateDevices blendet das
// Gerät aus, dann kommen „ended“ an der Mikrofonspur bzw. „devicechange“.
//
//   ./dev.sh serve-e2e 8196      (anderes Terminal)
//   node tests/e2e/failover.mjs

import { launch, page, enter, newInvite, clearCurrent, BASE, sleep, log } from "./lib.mjs";

const failures = [];
const check = (ok, what) => { if (!ok) { failures.push(what); log("FEHLER:", what); } else log("ok:", what); };

const HIDE = () => {
  const orig = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
  window.__hide = [];
  navigator.mediaDevices.enumerateDevices = async () => (await orig()).filter((d) => !window.__hide.some((x) => d.label.includes(x)));
};
const st = (p) => p.evaluate(() => {
  const S = window.__studio;
  const tr = S.mic.getAudioTracks()[0];
  return { mic: tr.label, track: S.recorder.track && S.recorder.track.id, out: S.outId, ctx: typeof S.ctx.sinkId === "string" ? S.ctx.sinkId : "",
    peer: [...S.media.values()].map((m) => m.audio.sinkId)[0], prefs: JSON.parse(localStorage.getItem("ps.prefs") || "{}"),
    banners: [...document.querySelectorAll(".ps-banner p")].map((b) => b.textContent) };
});

async function main() {
  await clearCurrent();
  const browser = await launch();
  const host = await page(browser, "host", { viewport: { width: 1280, height: 900 } });
  await host.addInitScript(HIDE);
  await host.goto(BASE + "/studio");
  await host.click("#ps-go");
  await host.waitForFunction(() => !document.querySelector("#ps-out").disabled, null, { timeout: 15000 });
  await host.selectOption("#ps-mic", { label: "Fake Audio Input 2" });
  await host.waitForFunction(() => window.__studio.mic.getAudioTracks()[0].label === "Fake Audio Input 2", null, { timeout: 10000 });
  await host.selectOption("#ps-out", { label: "Fake Audio Output 2" });
  await host.click("#ps-go");
  await host.waitForSelector(".ps-studio", { timeout: 15000 });
  await host.waitForFunction(() => window.__studio.socket && window.__studio.socket.connected && window.__studio.room, null, { timeout: 15000 });

  const inv = await newInvite("Max", "single");
  const guest = await page(browser, "gast");
  await guest.goto(inv.url.replace(/^https?:\/\/[^/]+/, BASE));
  await enter(guest, { name: "Max" });
  await host.waitForFunction(() => window.__studio.media.size === 1, null, { timeout: 20000 });

  await host.click("#ps-start");
  await host.waitForFunction(() => /Aufnahme läuft/.test(document.querySelector(".ps-status").textContent) && window.__studio.recorder.track, null, { timeout: 20000 });
  await sleep(1500);
  const before = await st(host);
  const outWanted = before.out;

  // Mikrofon abgezogen
  await host.evaluate(() => { window.__hide.push("Fake Audio Input 2"); window.__studio.mic.getAudioTracks()[0].dispatchEvent(new Event("ended")); });
  await host.waitForFunction((t) => window.__studio.mic.getAudioTracks()[0].label !== "Fake Audio Input 2" && window.__studio.recorder.track && window.__studio.recorder.track.id !== t, before.track, { timeout: 10000 });
  let s = await st(host);
  check(s.mic !== "Fake Audio Input 2" && s.track && s.track !== before.track, `Mikrofon weg: weiter mit „${s.mic}“ in einer neuen Spur`);
  check(s.banners.some((b) => /Fake Audio Input 2.*ist weg/.test(b)), "Banner nennt das ausgefallene Mikrofon");
  check(s.prefs.micId === before.prefs.micId, "die gespeicherte Wahl bleibt das ursprüngliche Mikrofon");

  // wieder eingesteckt
  await host.evaluate(() => { window.__hide = window.__hide.filter((x) => x !== "Fake Audio Input 2"); navigator.mediaDevices.dispatchEvent(new Event("devicechange")); });
  await host.waitForFunction((t) => window.__studio.mic.getAudioTracks()[0].label === "Fake Audio Input 2" && window.__studio.recorder.track && window.__studio.recorder.track.id !== t, s.track, { timeout: 10000 });
  s = await st(host);
  check(s.banners.some((b) => /wieder da und nimmt wieder auf/.test(b)), "Mikrofon wieder da: zurückgewechselt, Banner");

  // Ausgang abgezogen
  await host.evaluate(() => { window.__hide.push("Fake Audio Output 2"); navigator.mediaDevices.dispatchEvent(new Event("devicechange")); });
  await host.waitForFunction(() => window.__studio.outId === "", null, { timeout: 10000 });
  s = await st(host);
  check(s.peer === "" && s.ctx === "", "Ausgang weg: Stimmen und Jingles über den Systemstandard, auch während der Aufnahme");
  check(s.banners.some((b) => /Ausgang „Fake Audio Output 2“ ist weg/.test(b)) && s.prefs.outId === outWanted, "Banner, gespeicherte Wahl bleibt");
  await host.evaluate(() => { window.__hide = []; navigator.mediaDevices.dispatchEvent(new Event("devicechange")); });
  await host.waitForFunction((id) => window.__studio.outId === id, outWanted, { timeout: 10000 });
  s = await st(host);
  check(s.peer === outWanted && s.banners.some((b) => /ist wieder da$/.test(b)), "Ausgang wieder da: Stimmen sofort zurück");

  // Kein Ersatz vorhanden: klare Meldung, kein Absturz
  await host.evaluate(() => { window.__hide.push("Audio Input"); window.__studio.mic.getAudioTracks()[0].dispatchEvent(new Event("ended")); });
  await host.waitForFunction(() => [...document.querySelectorAll(".ps-banner p")].some((b) => /es gibt kein anderes/.test(b.textContent)), null, { timeout: 10000 });
  check(true, "ohne Ersatzmikrofon: Meldung „Bitte ein Mikrofon anschließen“");
  await host.evaluate(() => { window.__hide = []; navigator.mediaDevices.dispatchEvent(new Event("devicechange")); });
  await host.waitForFunction(() => window.__studio.mic.getAudioTracks()[0].label === "Fake Audio Input 2", null, { timeout: 10000 });
  check(true, "danach wieder eingesteckt: nimmt wieder mit dem gewählten Mikrofon auf");

  await host.click("#ps-stop");
  await host.click("#ps-stop-confirm");
  await host.waitForSelector("#ps-discard", { timeout: 20000 });
  const tracks = await host.evaluate(async () => (await (await fetch(`/api/host/episodes/${window.__studio.room.episode.id}`)).json()).tracks.filter((t) => t.participant_id === window.__studio.me.id).length);
  check(tracks === 4, `Host hat ${tracks} Spuren: vorher, Ersatz, wieder das eigene, nach der Lücke ohne Ersatz noch einmal das eigene`);

  const errs = [...host.__errors, ...guest.__errors].filter((e) => !/favicon|ERR_|Failed to load resource/.test(e));
  check(!errs.length, `keine Konsolenfehler${errs.length ? ": " + errs.slice(0, 3).join(" | ") : ""}`);
  await browser.close();
  if (failures.length) { log(`${failures.length} Fehler`); process.exit(1); }
  log("alles in Ordnung");
}

main().catch((e) => { console.error(e); process.exit(1); });
