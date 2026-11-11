// Gemeinsame Hilfen der Browser-Tests für die Verwaltung (Paket E).
// Aufruf: NODE_PATH=~/projects/qa-run/node_modules node tests/e2e-manage/<skript>.cjs
// Voraussetzung: ./dev.sh serve 8195 läuft und scripts/seed_demo.py wurde ausgeführt.
const path = require("path");
let pw;
try {
  pw = require("playwright");
} catch {
  pw = require(path.join(process.env.PLAYWRIGHT_PATH || path.join(process.env.HOME, "projects/qa-run/node_modules"), "playwright"));
}

const BASE = process.env.BASE || "http://127.0.0.1:8195";
const REVIEW = "demoep-review";

let failures = 0;
function ok(cond, msg) {
  if (cond) console.log(`  ok    ${msg}`);
  else {
    failures++;
    console.log(`  FEHLER ${msg}`);
  }
}
function done() {
  console.log(failures ? `\n${failures} Fehler` : "\nalles in Ordnung");
  process.exit(failures ? 1 : 0);
}

async function launch(opts = {}) {
  const browser = await pw.chromium.launch({ headless: true, args: ["--autoplay-policy=no-user-gesture-required", ...(opts.args || [])] });
  return browser;
}

/** Neue Seite mit Sammler für Konsolenfehler, Seitenfehler, CSP-Verstöße und fehlgeschlagene Antworten. */
async function newPage(browser, { width = 1280, height = 800, mobile = false, allow = [] } = {}) {
  const context = await browser.newContext({
    viewport: { width, height }, deviceScaleFactor: mobile ? 2 : 1, hasTouch: mobile, isMobile: mobile, locale: "de-DE", timezoneId: "Europe/Berlin",
    acceptDownloads: true,
  });
  await context.addInitScript(() => {
    window.__csp = [];
    document.addEventListener("securitypolicyviolation", (e) => window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`));
  });
  const page = await context.newPage();
  page.problems = [];
  page.on("console", (m) => {
    if (m.type() === "error" || m.type() === "warning") {
      const t = m.text();
      if (!allow.some((a) => t.includes(a))) page.problems.push(`console.${m.type()}: ${t}`);
    }
  });
  page.on("pageerror", (e) => page.problems.push(`pageerror: ${e.message}`));
  page.on("response", (r) => {
    if (r.status() >= 400 && !allow.some((a) => r.url().includes(a))) page.problems.push(`HTTP ${r.status()} ${r.url()}`);
  });
  return page;
}

async function cspViolations(page) {
  return page.evaluate(() => window.__csp || []);
}

async function api(page, p, opts = {}) {
  return page.evaluate(async ([p, opts]) => {
    const r = await fetch("/api/host" + p, { method: opts.method || "GET", headers: opts.json ? { "Content-Type": "application/json" } : {}, body: opts.json ? JSON.stringify(opts.json) : undefined });
    const t = await r.text();
    try { return { status: r.status, body: JSON.parse(t) }; } catch { return { status: r.status, body: t }; }
  }, [p, opts]);
}

module.exports = { pw, BASE, REVIEW, ok, done, launch, newPage, cspViolations, api, get failures() { return failures; } };
