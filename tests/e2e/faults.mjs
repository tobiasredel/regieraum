// Störfälle während einer Aufnahme:
//  1. Netz des Gastes 20 s weg (context.setOffline) → nichts verloren, Spur läuft durch
//  2. Gast lädt mitten in der Aufnahme neu → liegengebliebene Chunks kommen an, neue Spur beginnt
//  3. Host lädt neu → Aufnahme läuft weiter, Steuerung ist wieder da
// Danach: alle Spuren complete, Chunks lückenlos, Frames passend, Klicks der Spuren zeitlich deckungsgleich.
//
//   node tests/e2e/faults.mjs

import fs from "node:fs";
import path from "node:path";
import { launch, page, enter, api, ensureSounds, newInvite, clearCurrent, BASE, sleep, log, mirrorChunks, ensureDir } from "./lib.mjs";
import { dbQuery, analyseTrack, compareClicks, absError } from "./session.mjs";

const MODE = "local";
const failures = [];
const check = (ok, what) => { if (!ok) { failures.push(what); log("FEHLER:", what); } else log("ok:", what); };

async function reenter(p) {
  try {
    await p.waitForSelector("#ps-go", { timeout: 15000 });
  } catch (e) {
    log(p.__name, "Seite nach Neuladen:", (await p.textContent("body")).replace(/\s+/g, " ").slice(0, 400));
    throw e;
  }
  await p.waitForFunction(() => /Wieder|betreten|freigeben/.test(document.querySelector("#ps-go").textContent), null, { timeout: 5000 });
  await sleep(300);
  const label = await p.textContent("#ps-go");
  log(p.__name, "Knopf nach Neuladen:", label.trim());
  await p.click("#ps-go");
  if (!/Wieder/.test(label)) {
    await p.waitForFunction(() => /betreten/.test(document.querySelector("#ps-go").textContent), null, { timeout: 15000 });
    await p.click("#ps-go");
  }
  await p.waitForSelector(".ps-studio", { timeout: 15000 });
  await p.waitForFunction(() => window.__studio.socket && window.__studio.socket.connected && window.__studio.room, null, { timeout: 15000 });
  return label.trim();
}

const status = (p) => p.evaluate(() => ({ ...window.__studio.recorder.status(), tracks: [...window.__studio.recorder.tracks.values()].map((t) => ({ id: t.id, frames: t.frames, up: t.uploadedFrames, pending: t.pending.size, done: t.done, recovered: t.recovered })) }));

async function main() {
  const work = ensureDir(`/tmp/ps-e2e-faults-${Date.now()}`);
  await clearCurrent();
  await ensureSounds();
  const inv = await newInvite("Max");
  const b = await launch();
  const host = await page(b, "host", { quiet: true });
  const guest = await page(b, "guest", { quiet: true });
  const out = { failures };
  try {
    await host.goto(`${BASE}/studio?testsignal=${MODE}`);
    await enter(host);
    await guest.goto(`${BASE}/join/${inv.token}?testsignal=${MODE}`);
    await enter(guest, { name: "Max" });
    await host.waitForFunction(() => [...window.__studio.mesh.peers.values()].some((p) => p.stats && p.stats.inRate > 20000), null, { timeout: 20000 });
    await host.click("#ps-start");
    await host.waitForFunction(() => window.__studio.room.episode.status === "recording", null, { timeout: 5000 });
    const epId = await host.evaluate(() => window.__studio.room.episode.id);
    const mirror = mirrorChunks(epId, path.join(work, "chunks"));
    await sleep(6000);

    // ---- 1. Netz weg für 20 s
    log("Gast offline");
    const offAt = Date.now();
    await guest.context().setOffline(true);
    await sleep(10000);
    const during = await status(guest);
    log("Gast während offline", JSON.stringify({ queue: during.queue, retrying: during.retrying, pending: during.pendingFrames }));
    await sleep(10000);
    await guest.context().setOffline(false);
    const onAt = Date.now();
    log("Gast wieder online");
    await guest.waitForFunction(() => window.__studio.recorder.status().queue === 0 && window.__studio.recorder.status().active === 0, null, { timeout: 60000 });
    log("Rückstand des Gastes abgebaut nach", Date.now() - onAt, "ms");
    out.offline = { seconds: (onAt - offAt) / 1000, queued_during: during.queue, catchup_ms: Date.now() - onAt };
    check(during.queue >= 5, `während offline staut sich die Warteschlange (${during.queue} Chunks)`);
    await host.waitForFunction(() => [...window.__studio.mesh.peers.values()].some((p) => p.quality === "gut"), null, { timeout: 30000 }).catch(() => log("Gesprächsverbindung noch nicht wieder gut"));
    await sleep(4000);

    // ---- 2. Gast lädt neu
    const before = await status(guest);
    log("Gast lädt neu, aktive Spur", JSON.stringify(before.tracks));
    await guest.reload();
    const lbl = await reenter(guest);
    out.guest_reload_button = lbl;
    await guest.waitForFunction(() => window.__studio.recorder.track, null, { timeout: 15000 });
    const after = await status(guest);
    log("Gast nach Neuladen", JSON.stringify(after.tracks));
    check(after.tracks.some((t) => t.recovered), "Gast: liegengebliebene Spur wird nachgeladen");
    await guest.waitForFunction(() => [...window.__studio.recorder.tracks.values()].filter((t) => t.recovered).every((t) => t.done), null, { timeout: 30000 });
    check(true, "Gast: nachgeladene Spur abgeschlossen");
    await sleep(6000);

    // ---- 3. Host lädt neu
    await host.reload();
    out.host_reload_button = await reenter(host);
    const ep = await host.evaluate(() => window.__studio.room.episode);
    check(ep.status === "recording" && ep.id === epId, "Host nach Neuladen: Aufnahme läuft weiter");
    await host.waitForSelector("#ps-stop", { timeout: 5000 });
    check(true, "Host nach Neuladen: Steuerung wieder da");
    await host.waitForFunction(() => window.__studio.recorder.track, null, { timeout: 15000 });
    await sleep(6000);

    // ---- Stopp
    await host.click("#ps-stop");
    await host.click("#ps-stop-confirm");
    await guest.waitForSelector("#ps-done", { timeout: 60000 });
    await host.waitForSelector("#ps-done", { timeout: 60000 });
    await sleep(1500);
    mirror.stop();

    const tracks = dbQuery(`SELECT * FROM tracks WHERE episode_id='${epId}' ORDER BY created_at`);
    log("Spuren", JSON.stringify(tracks.map((t) => [t.participant_name, t.status, t.total_chunks, t.total_frames])));
    check(tracks.length === 4, `vier Spuren (je zwei für Host und Gast), gefunden ${tracks.length}`);
    check(tracks.every((t) => t.status === "complete" || t.status === "assembled"), "alle Spuren complete");
    const rows = tracks.map((t) => analyseTrack(t, path.join(work, "chunks")));
    for (const r of rows) {
      check(r.seq_ok && r.contiguous && r.frames === r.total_frames && r.chunks === r.total_chunks,
        `${r.name} ${r.id}: ${r.chunks}/${r.total_chunks} Chunks lückenlos, ${r.frames} Frames = total_frames`);
      // Frames je Serverzeit (Steigung der Uhrenpunkte) muss zur Abtastrate passen; nach einem Neuladen fehlt der
      // letzte Uhrenpunkt der alten Spur, deshalb hier die Steigung statt der Gesamtdauer
      if (r.clock_points >= 2) check(Math.abs(r.clock_rate_error_pct) <= 0.5 && r.clock_max_residual_ms < 15, `${r.name} ${r.id}: Uhrenpunkte auf einer Geraden (Rest ${r.clock_max_residual_ms.toFixed(2)} ms, Rate ${r.clock_rate_error_pct.toFixed(3)} %)`);
    }
    // Abdeckung: Lücke zwischen den Spuren einer Person (Neuladen)
    const byName = {};
    for (const r of rows) (byName[r.name] = byName[r.name] || []).push(r);
    out.coverage = {};
    for (const [name, rs] of Object.entries(byName)) {
      rs.sort((a, b) => a.start_server_ms - b.start_server_ms);
      const gaps = [];
      for (let i = 1; i < rs.length; i++) gaps.push(rs[i].start_server_ms - (rs[i - 1].start_server_ms + rs[i - 1].duration_s * 1000));
      out.coverage[name] = gaps.map((g) => Math.round(g));
    }
    log("Lücken zwischen den Spuren je Person (ms)", JSON.stringify(out.coverage));
    // Zeitlage: jede Spur gegen jede überlappende vergleichen
    out.clicks = [];
    for (let i = 0; i < rows.length; i++) for (let j = i + 1; j < rows.length; j++) {
      if (rows[i].name === rows[j].name) continue;
      const c = compareClicks(rows[i], rows[j]);
      if (c.n) out.clicks.push({ a: `${rows[i].name}/${rows[i].id}`, b: `${rows[j].name}/${rows[j].id}`, n: c.n, mean_ms: +c.mean_ms.toFixed(2), max_abs_ms: +c.max_abs_ms.toFixed(2) });
    }
    log("Klickvergleich", JSON.stringify(out.clicks));
    check(out.clicks.length >= 2 && out.clicks.every((c) => c.max_abs_ms <= 20), "Klicks aller überlappenden Spuren höchstens 20 ms auseinander");
    out.absolute = rows.map((r) => ({ name: r.name, id: r.id, ...absError(r) }));
    out.tracks = rows.map(({ samples_ref, clicks, fit, ...r }) => r);
    out.errors = { host: host.__errors.slice(0, 20), guest: guest.__errors.slice(0, 20) };
    fs.writeFileSync(path.join(work, "result.json"), JSON.stringify(out, null, 2));
    console.log(JSON.stringify(out, null, 2));
  } finally {
    await b.close();
  }
  process.exit(failures.length ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(2); });
