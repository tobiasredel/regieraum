// Vollständige Sitzung mit Host und Gast in getrennten Browser-Kontexten (eine Browser-Instanz):
// Beitritt, WebRTC verbunden und Ton fließt, Aufnahme, zwei Jingles, Pause und Weiter, Marker, Stopp, Upload fertig,
// Spuren complete. Danach Daten auf der Platte und in der API prüfen und die Zeit-Genauigkeit messen
// (Testquelle ?testsignal=local: Klick zu jeder vollen Sekunde der Systemuhr, die Browser und Server teilen).
//
//   node tests/e2e/session.mjs            Ergebnis als JSON auf stdout, Exit 1 bei Fehlschlag

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import {
  launch, page, enter, api, ensureSounds, newInvite, clearCurrent, BASE, DATA, sleep, log, mirrorChunks, ensureDir,
  readTrackChunks, decodeS24, findClicks, fitLine,
} from "./lib.mjs";

const RECORD_S = Number(process.env.RECORD_S || 30);
const MODE = process.env.TESTSIGNAL || "local";
const failures = [];
const check = (ok, what) => { if (!ok) { failures.push(what); log("FEHLER:", what); } else log("ok:", what); };

export function dbQuery(sql) {
  const py = `import sqlite3,json,sys\nc=sqlite3.connect('file:${DATA}/studio.db?mode=ro',uri=True)\nc.row_factory=sqlite3.Row\nprint(json.dumps([dict(r) for r in c.execute(sys.argv[1])]))`;
  return JSON.parse(execFileSync("python3", ["-c", py, sql]).toString());
}

/** Spur prüfen: Chunks lückenlos, Framezahl, Uhrenpunkte. Liefert Kennzahlen. */
export function analyseTrack(t, mirrorDir, rec = {}) {
  const chunks = dbQuery(`SELECT seq, first_frame, frames, bytes FROM chunks WHERE track_id='${t.id}' ORDER BY seq`);
  const clock = dbQuery(`SELECT sample_index, server_ms FROM clock_points WHERE track_id='${t.id}' ORDER BY sample_index`);
  let contiguous = true, expect = 0, seqOk = true;
  chunks.forEach((c, i) => {
    if (c.seq !== i) seqOk = false;
    if (c.first_frame !== expect) contiguous = false;
    expect = c.first_frame + c.frames;
  });
  const sr = t.sample_rate;
  const res = {
    id: t.id, name: t.participant_name, status: t.status, chunks: chunks.length, total_chunks: t.total_chunks, frames: expect,
    total_frames: t.total_frames, seq_ok: seqOk, contiguous, duration_s: expect / sr, start_server_ms: t.start_server_ms,
    clock_points: clock.length,
  };
  if (clock.length >= 2) {
    const fit = fitLine(clock.map((p) => [p.sample_index, p.server_ms]));
    res.clock_rate_error_pct = ((fit.b * sr) / 1000 - 1) * 100;
    res.clock_intercept_vs_start_ms = fit.a - t.start_server_ms;
    res.clock_max_residual_ms = Math.max(...clock.map((p) => Math.abs(fit.a + fit.b * p.sample_index - p.server_ms)));
    const last = clock[clock.length - 1];
    res.duration_vs_clock_pct = ((expect / sr) * 1000 / Math.max(1, last.server_ms - t.start_server_ms) - 1) * 100;
    res.fit = fit;
  }
  // Rohdaten aus der Spiegelkopie
  const dir = path.join(mirrorDir, t.id);
  if (fs.existsSync(dir)) {
    const files = readTrackChunks(dir);
    res.files = files.length;
    const samples = decodeS24(files.map((f) => fs.readFileSync(f.path)));
    res.samples = samples.length;
    const clicks = findClicks(samples, sr);
    const toServer = (i) => (res.fit ? res.fit.a + res.fit.b * i : t.start_server_ms + (i / sr) * 1000);
    res.clicks = clicks.map((i) => ({ i, ms: toServer(i) }));
    // Übersprechen der Jingles: außerhalb der Klicks darf nur das Testrauschen (−50 dBFS) sein
    let maxOther = 0;
    const near = new Set();
    for (const i of clicks) for (let k = i - 48; k < i + 960; k++) near.add(k);
    for (let i = 0; i < samples.length; i++) if (!near.has(i)) { const a = Math.abs(samples[i]); if (a > maxOther) maxOther = a; }
    res.max_outside_clicks = maxOther;
    res.samples_ref = samples; // nur intern
  }
  Object.assign(res, rec);
  return res;
}

/** Klicks zweier Spuren vergleichen: je volle Sekunde Abstand in ms. */
export function compareClicks(a, b) {
  const sec = (x) => Math.round(x.ms / 1000);
  const mb = new Map(b.clicks.map((c) => [sec(c), c.ms]));
  const diffs = [];
  for (const c of a.clicks) if (mb.has(sec(c))) diffs.push(c.ms - mb.get(sec(c)));
  const abs = diffs.map(Math.abs);
  return { n: diffs.length, mean_ms: diffs.reduce((s, x) => s + x, 0) / Math.max(1, diffs.length), max_abs_ms: abs.length ? Math.max(...abs) : null, diffs };
}

export function absError(t) {
  const e = t.clicks.map((c) => c.ms - Math.round(c.ms / 1000) * 1000);
  return { n: e.length, mean_ms: e.reduce((s, x) => s + x, 0) / Math.max(1, e.length), max_abs_ms: e.length ? Math.max(...e.map(Math.abs)) : null };
}

async function main() {
  const shotsDir = ensureDir(path.join(path.dirname(new URL(import.meta.url).pathname), "shots"));
  const work = ensureDir(`/tmp/ps-e2e-${Date.now()}`);
  await clearCurrent();
  const sounds = await ensureSounds();
  const inv = await newInvite("Max");
  const b = await launch();
  const host = await page(b, "host");
  const guest = await page(b, "guest", { viewport: { width: 390, height: 844 }, ctx: { isMobile: false } });
  try {
    await host.goto(`${BASE}/studio?testsignal=${MODE}`);
    await enter(host);
    await guest.goto(`${BASE}/join/${inv.token}?testsignal=${MODE}`);
    await enter(guest, { name: "Max" });
    log("beide im Studio");

    // ---- Live-Gespräch: verbunden und Ton fließt (getStats)
    await host.waitForFunction(() => [...window.__studio.mesh.peers.values()].some((p) => p.stats && p.stats.inRate > 20000), null, { timeout: 20000 });
    await guest.waitForFunction(() => [...window.__studio.mesh.peers.values()].some((p) => p.stats && p.stats.inRate > 20000), null, { timeout: 20000 });
    const rtc = await Promise.all([host, guest].map((p) => p.evaluate(() => [...window.__studio.mesh.peers.values()].map((x) => ({ ice: x.pc.iceConnectionState, kbps: Math.round(x.stats.inRate / 1000), quality: x.quality })))));
    log("WebRTC", JSON.stringify(rtc));
    check(rtc.every((r) => r.length === 1 && r[0].kbps > 20 && r[0].ice.startsWith("co")), "WebRTC verbunden, Ton fließt in beide Richtungen");
    // Opus-Parameter der Gegenseite angewandt?
    const fmtp = await host.evaluate(() => { const P = [...window.__studio.mesh.peers.values()][0]; return P.pc.remoteDescription.sdp.split("\r\n").find((l) => /useinbandfec/.test(l)); });
    check(/maxaveragebitrate=96000/.test(fmtp || "") && /usedtx=0/.test(fmtp || ""), "Opus 96 kbit/s, FEC an, DTX aus im SDP");

    // ---- Aufnahme starten
    const t0 = Date.now();
    await host.click("#ps-start");
    await host.waitForFunction(() => window.__studio.room.episode.status === "recording", null, { timeout: 5000 });
    const epId = await host.evaluate(() => window.__studio.room.episode.id);
    const mirror = mirrorChunks(epId, path.join(work, "chunks"));
    log("Aufnahme läuft, Folge", epId);
    await host.waitForFunction(() => /Aufnahme läuft/.test(document.querySelector(".ps-status").textContent), null, { timeout: 5000 });
    await host.screenshot({ path: path.join(shotsDir, "studio-host-recording-1280.png") });
    await guest.screenshot({ path: path.join(shotsDir, "join-guest-recording-390.png") });
    await sleep(3000);

    // Jingle 1 per Taste
    await host.keyboard.press("1");
    const intro = sounds.find((s) => s.hotkey === "1");
    await host.waitForFunction(() => window.__studio.jingles.playing.size > 0, null, { timeout: 3000 });
    await guest.waitForFunction(() => window.__studio.jingles.playing.size > 0, null, { timeout: 3000 });
    check(true, "Jingle 1 spielt bei Host und Gast");
    // Eingabefeld mit Fokus: Taste löst nichts aus
    await host.focus("#ps-marker");
    await host.keyboard.press("3");
    await sleep(400);
    const afterKeyInField = await host.evaluate(() => window.__studio.jingles.playing.size);
    check(afterKeyInField === 1, "Taste im Eingabefeld löst keinen Jingle aus");
    await host.fill("#ps-marker", "");
    await host.evaluate(() => document.activeElement.blur());
    await sleep(2500);

    // Pause und Weiter
    await host.click("#ps-pause");
    await host.waitForFunction(() => window.__studio.room.episode.paused, null, { timeout: 3000 });
    await sleep(2000);
    await host.click("#ps-resume");
    await host.waitForFunction(() => !window.__studio.room.episode.paused, null, { timeout: 3000 });

    // Marker
    await host.fill("#ps-marker", "Testmarker");
    await host.press("#ps-marker", "Enter");
    await sleep(500);

    // Jingle 2 per Klick, nach 1 s ausblenden
    const blitz = sounds.find((s) => s.hotkey === "2");
    await host.click(`[data-sound="${blitz.id}"]`);
    await sleep(1200);
    await host.click(".ps-run-stop >> nth=-1");
    await sleep(1500);

    // bis zur Ziellänge weiter aufnehmen
    while (Date.now() - t0 < RECORD_S * 1000) await sleep(500);
    await host.click("#ps-stop");
    await host.click("#ps-stop-confirm");
    const stopClick = Date.now();
    log("Stopp");
    await host.waitForFunction(() => /Upload läuft|Fertig/.test((document.querySelector(".ps-upload") || {}).textContent || ""), null, { timeout: 5000 });
    await guest.screenshot({ path: path.join(shotsDir, "join-guest-upload-390.png") });
    await guest.waitForSelector("#ps-done", { timeout: 60000 });
    await host.waitForSelector("#ps-done", { timeout: 60000 });
    log("Upload fertig nach", Date.now() - stopClick, "ms");
    await host.screenshot({ path: path.join(shotsDir, "studio-host-done-1280.png") });
    await guest.screenshot({ path: path.join(shotsDir, "join-guest-done-390.png") });
    const link = await host.getAttribute("#ps-episode-link", "href");
    check(link === `/episodes/${epId}`, "Host bekommt den Link zur Folge");
    await sleep(1500);
    mirror.stop();

    // ---- Daten prüfen
    const ep = await api("GET", `/api/host/episodes/${epId}`);
    const tracks = dbQuery(`SELECT * FROM tracks WHERE episode_id='${epId}'`);
    check(tracks.length === 2, `zwei Spuren (${tracks.length})`);
    check(tracks.every((t) => ["complete", "assembled"].includes(t.status)), "Spuren complete: " + tracks.map((t) => t.status).join(", "));
    const rows = tracks.map((t) => analyseTrack(t, path.join(work, "chunks")));
    for (const r of rows) {
      check(r.seq_ok && r.contiguous, `${r.name}: Chunks lückenlos (${r.chunks} Stück, first_frame fortlaufend)`);
      check(r.frames === r.total_frames, `${r.name}: Frames ${r.frames} = total_frames ${r.total_frames}`);
      check(Math.abs(r.duration_vs_clock_pct) <= 0.5, `${r.name}: Framezahl passt zur Dauer (${r.duration_vs_clock_pct.toFixed(3)} %)`);
      check(r.max_outside_clicks < 0.05, `${r.name}: kein Jingle-Ton in der Spur (max ${r.max_outside_clicks.toFixed(4)})`);
    }
    // Ereignisse
    const ev = ep.events || dbQuery(`SELECT kind, server_ms, payload FROM events WHERE episode_id='${epId}' ORDER BY server_ms`);
    const kinds = ev.map((e) => e.kind);
    log("Ereignisse", JSON.stringify(ev.map((e) => [e.kind, e.server_ms - ep.rec_start_server_ms])));
    for (const k of ["rec.start", "rec.pause", "rec.resume", "marker", "rec.stop"]) check(kinds.includes(k), `Ereignis ${k}`);
    check(kinds.filter((k) => k === "sound.play").length === 2 && kinds.includes("sound.stop"), "zwei Jingles und ein Ausblenden als Ereignis");
    const sorted = ev.every((e, i) => i === 0 || e.server_ms >= ev[i - 1].server_ms);
    const start = ep.rec_start_server_ms;
    const inWindow = ev.every((e) => e.server_ms >= start && e.server_ms <= ep.rec_stop_server_ms + 1);
    check(sorted && inWindow, "Ereigniszeiten plausibel (aufsteigend, innerhalb der Aufnahme)");
    const play1 = ev.find((e) => e.kind === "sound.play");
    check(play1.server_ms - start > 2500 && play1.server_ms - start < 6000, `Jingle 1 bei ${(play1.server_ms - start)} ms Sitzungszeit (erwartet ~3–4 s)`);
    // Zeit-Genauigkeit
    const cmp = compareClicks(rows[0], rows[1]);
    const abs = rows.map(absError);
    log("Klickvergleich", JSON.stringify({ n: cmp.n, mean_ms: cmp.mean_ms, max_abs_ms: cmp.max_abs_ms }), "absolut", JSON.stringify(abs));
    check(cmp.n >= RECORD_S - 4 && cmp.max_abs_ms <= 20, `Klicks beider Spuren höchstens 20 ms auseinander (max ${cmp.max_abs_ms?.toFixed(2)} ms über ${cmp.n} Sekunden)`);
    const result = {
      episode: epId, failures, rtc, tracks: rows.map(({ samples_ref, clicks, fit, ...r }) => r), clicks: { between: { n: cmp.n, mean_ms: cmp.mean_ms, max_abs_ms: cmp.max_abs_ms }, absolute: abs },
      events: ev.map((e) => ({ kind: e.kind, session_ms: e.server_ms - start })),
      recorder: await Promise.all([host, guest].map((p) => p.evaluate(() => ({ ...window.__studio.recorder.status(), gaps: window.__studio.recorder.gaps, tsOffsets: window.__studio.audioClock.tsOffsets.length, outputLatency: window.__studio.ctx.outputLatency, baseLatency: window.__studio.ctx.baseLatency })))),
      errors: { host: host.__errors, guest: guest.__errors },
      work,
    };
    fs.writeFileSync(path.join(work, "result.json"), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result, null, 2));
  } catch (e) {
    for (const p of [host, guest]) {
      await p.screenshot({ path: path.join(work, `fail-${p.__name}.png`) }).catch(() => {});
      log(p.__name, "Zustand:", await p.evaluate(() => JSON.stringify({ ep: window.__studio.room && window.__studio.room.episode, rec: window.__studio.recorder.status(), upload: (document.querySelector(".ps-upload") || {}).outerHTML })).catch((x) => String(x)));
    }
    log("Fehlerbilder in", work);
    throw e;
  } finally {
    await b.close();
  }
  process.exit(failures.length ? 1 : 0);
}

if (process.argv[1] && process.argv[1].endsWith("session.mjs")) main().catch((e) => { console.error(e); process.exit(2); });
