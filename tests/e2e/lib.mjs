// Gemeinsame Hilfen für die Playwright-Skripte des Studios.
// Server: ./dev.sh serve 8196  (PS_HOST_AUTH=none), Daten unter /tmp/podcast-studio-dev-8196
// Aufruf: node tests/e2e/<skript>.mjs   (BASE und DATA über Umgebungsvariablen änderbar)

import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

const require = createRequire(import.meta.url);

function loadPlaywright() {
  const tries = [process.env.PLAYWRIGHT_PATH, "playwright", "/home/tobiasredel/projects/qa-run/node_modules/playwright"].filter(Boolean);
  for (const t of tries) { try { return require(t); } catch { /* weiter */ } }
  throw new Error("Playwright nicht gefunden (PLAYWRIGHT_PATH setzen)");
}
export const { chromium } = loadPlaywright();

export const BASE = process.env.BASE || "http://127.0.0.1:8196";
export const DATA = process.env.DATA || "/tmp/podcast-studio-dev-8196";
export const SHOTS = path.join(path.dirname(new URL(import.meta.url).pathname), "shots");

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function log(...a) { console.log(new Date().toISOString().slice(11, 23), ...a); }

export async function api(method, url, body, headers = {}) {
  const opts = { method, headers: { ...headers } };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  const r = await fetch(BASE + url, opts);
  const text = await r.text();
  let data = null;
  try { data = JSON.parse(text); } catch { data = text; }
  if (!r.ok) throw new Error(`${method} ${url} → ${r.status} ${text.slice(0, 300)}`);
  return data;
}

/** WAV mono 16 bit: Sinus-Akkord mit weichem Ende, damit es nach Jingle klingt. */
export function wav(seconds, freq = 440, rate = 48000) {
  const n = Math.round(seconds * rate);
  const buf = Buffer.alloc(44 + n * 2);
  buf.write("RIFF", 0); buf.writeUInt32LE(36 + n * 2, 4); buf.write("WAVE", 8); buf.write("fmt ", 12);
  buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22); buf.writeUInt32LE(rate, 24);
  buf.writeUInt32LE(rate * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34); buf.write("data", 36); buf.writeUInt32LE(n * 2, 40);
  for (let i = 0; i < n; i++) {
    const env = Math.min(1, i / 2400, (n - i) / 9600);
    const v = 0.3 * env * (Math.sin(2 * Math.PI * freq * i / rate) + 0.5 * Math.sin(2 * Math.PI * freq * 1.5 * i / rate));
    buf.writeInt16LE(Math.round(v * 32767), 44 + i * 2);
  }
  return buf;
}

export async function ensureSounds() {
  const have = await api("GET", "/api/host/sounds");
  const want = [["Intro", "intro", 6, 330, "1"], ["Blitzlicht", "segment", 3, 520, "2"], ["Tusch", "sting", 1.5, 660, "3"]];
  for (const [label, kind, sec, f, key] of want) {
    if (have.some((s) => s.label === label)) continue;
    const fd = new FormData();
    fd.append("file", new Blob([wav(sec, f)], { type: "audio/wav" }), `${label}.wav`);
    fd.append("label", label); fd.append("kind", kind); fd.append("hotkey", key); fd.append("fade_out_ms", "800");
    await api("POST", "/api/host/sounds", fd);
  }
  return api("GET", "/api/host/sounds");
}

export async function newInvite(label = "Max", kind = "single") {
  return api("POST", "/api/host/invites", { label, kind });
}

/** Aktuelle Folge abräumen (Reste abgebrochener Läufe): laufende Aufnahme stoppen, `uploading` weiterschalten. */
export async function clearCurrent() {
  let eps = await api("GET", "/api/host/episodes");
  if (eps.some((e) => e.status === "recording")) {
    const { participant_token } = await api("POST", "/api/host/session");
    const ws = new WebSocket(BASE.replace(/^http/, "ws") + "/ws/p?token=" + participant_token);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    ws.send(JSON.stringify({ type: "rec.stop" }));
    await sleep(500);
    ws.close();
    eps = await api("GET", "/api/host/episodes");
  }
  for (const e of eps) {
    if (e.status === "uploading") await api("POST", `/api/host/episodes/${e.id}/process`).catch(() => {});
  }
}

export async function launch(extraArgs = []) {
  return chromium.launch({
    args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required", ...extraArgs],
  });
}

/** Seite mit Konsolen-Mitschnitt. */
export async function page(browser, name, opts = {}) {
  const ctx = await browser.newContext({ viewport: opts.viewport || { width: 1280, height: 800 }, permissions: ["microphone", "camera"], ...(opts.ctx || {}) });
  const p = await ctx.newPage();
  p.__name = name;
  p.__errors = [];
  p.on("console", (m) => {
    if (m.type() === "error" || m.type() === "warning") { p.__errors.push(m.text()); if (!opts.quiet) log(`[${name}:${m.type()}]`, m.text().slice(0, 300)); }
  });
  p.on("pageerror", (e) => { p.__errors.push(String(e)); log(`[${name}:pageerror]`, String(e).slice(0, 500)); });
  p.on("dialog", (d) => d.accept().catch(() => {}));
  return p;
}

/** Gerätecheck durchklicken. */
export async function enter(p, { name } = {}) {
  await p.waitForSelector("#ps-go", { timeout: 15000 });
  await p.click("#ps-go"); // Mikrofon freigeben
  await p.waitForFunction(() => document.querySelector("#ps-go") && /betreten|enter|join/i.test(document.querySelector("#ps-go").textContent), null, { timeout: 15000 });
  if (name) await p.fill("#ps-name", name);
  await p.click("#ps-go");
  await p.waitForSelector(".ps-studio", { timeout: 15000 });
  await p.waitForFunction(() => window.__studio && window.__studio.socket && window.__studio.socket.connected && window.__studio.room, null, { timeout: 15000 });
}

export function ensureDir(d) { fs.mkdirSync(d, { recursive: true }); return d; }

/** Chunk-Dateien einer Folge laufend nach `dest` kopieren (falls die Pipeline sie nach dem Zusammensetzen löscht). */
export function mirrorChunks(episodeId, dest) {
  const src = path.join(DATA, "episodes", episodeId, "tracks");
  const copy = () => {
    if (!fs.existsSync(src)) return;
    for (const t of fs.readdirSync(src)) {
      const cdir = path.join(src, t, "chunks");
      if (!fs.existsSync(cdir)) continue;
      const out = ensureDir(path.join(dest, t));
      for (const f of fs.readdirSync(cdir)) {
        if (!f.endsWith(".pcm")) continue;
        const a = path.join(cdir, f), b = path.join(out, f);
        try { const st = fs.statSync(a); if (!fs.existsSync(b) || fs.statSync(b).size !== st.size) fs.copyFileSync(a, b); } catch { /* gerade umbenannt */ }
      }
    }
  };
  const timer = setInterval(copy, 500);
  return { stop() { clearInterval(timer); copy(); } };
}

/** Lückenlosigkeit einer Spur aus der Episoden-API und den Dateien prüfen. */
export function readTrackChunks(dir) {
  return fs.readdirSync(dir).filter((f) => f.endsWith(".pcm")).sort().map((f) => ({ seq: parseInt(f, 10), path: path.join(dir, f), bytes: fs.statSync(path.join(dir, f)).size }));
}

/** s24le-Datei(en) zu Float32Array. */
export function decodeS24(buffers) {
  const total = buffers.reduce((a, b) => a + b.length / 3, 0);
  const out = new Float32Array(total);
  let o = 0;
  for (const b of buffers) {
    for (let i = 0; i + 2 < b.length; i += 3) {
      let v = b[i] | (b[i + 1] << 8) | (b[i + 2] << 16);
      if (v & 0x800000) v -= 0x1000000;
      out[o++] = v / 8388608;
    }
  }
  return out;
}

/** Klick-Einsätze finden: erstes Sample über der Schwelle nach mindestens 0,5 s Ruhe. */
export function findClicks(samples, sr, thr = 0.4) {
  const out = [];
  let last = -Infinity;
  for (let i = 0; i < samples.length; i++) {
    const a = samples[i] < 0 ? -samples[i] : samples[i];
    if (a >= thr && i - last > sr * 0.5) { out.push(i); last = i; }
    if (a >= thr) last = i;
  }
  return out;
}

/** Gerade durch Punkte (kleinste Quadrate): y = a + b x. */
export function fitLine(pts) {
  const n = pts.length;
  const mx = pts.reduce((s, p) => s + p[0], 0) / n, my = pts.reduce((s, p) => s + p[1], 0) / n;
  let sxy = 0, sxx = 0;
  for (const [x, y] of pts) { sxy += (x - mx) * (y - my); sxx += (x - mx) ** 2; }
  const b = sxx ? sxy / sxx : 0;
  return { a: my - b * mx, b };
}
