// Breiten-Sweep 360 bis 1920 px in 8-px-Schritten über die fünf Seiten: kein horizontaler Überlauf,
// keine abgeschnittenen oder überlappenden Texte.
const L = require("./lib.cjs");
const { mockPlan } = require("./plan-mock.cjs");

const PAGES = [["Folgen", "/episodes"], ["Folge prüfen", `/episodes/${L.REVIEW}`], ["Soundboard", "/sounds"], ["Einstellungen", "/settings"], ["Vorbereitung", "/plan"]];

function measure() {
  const vw = window.innerWidth;
  const res = [];
  if (document.documentElement.scrollWidth > vw) res.push(`Seite läuft über: scrollWidth ${document.documentElement.scrollWidth} > ${vw}`);
  const vis = (el) => {
    const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    if (!(r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none")) return false;
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) { // aus einem Scrollbereich herausgescrollt = nicht sichtbar
      const o = getComputedStyle(p); if (o.overflowY === "auto" || o.overflowY === "scroll") { const q = p.getBoundingClientRect(); if (r.bottom < q.top || r.top > q.bottom) return false; }
    }
    return true;
  };
  const inScroller = (el) => { for (let p = el.parentElement; p; p = p.parentElement) { const o = getComputedStyle(p).overflowX; if ((o === "auto" || o === "scroll" || o === "hidden") && p.scrollWidth > p.clientWidth) return true; } return false; };
  const desc = (el) => `${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}.${(el.className || "").toString().split(" ")[0]} "${(el.textContent || el.value || "").trim().slice(0, 30)}"`;
  for (const el of document.querySelectorAll("main *, header *")) {
    if (!vis(el)) continue;
    if (el.closest("canvas, svg, audio")) continue;
    const r = el.getBoundingClientRect();
    if (r.right > vw + 1 && !inScroller(el)) res.push(`ragt über den Rand: ${desc(el)} rechts ${Math.round(r.right)}`);
    const cs = getComputedStyle(el);
    if (el.children.length === 0 && el.textContent.trim() && (cs.overflowX === "hidden" || cs.textOverflow === "ellipsis") && el.scrollWidth > el.clientWidth + 1) res.push(`Text abgeschnitten: ${desc(el)}`);
  }
  const ctrls = [...document.querySelectorAll("main button, main input:not([type=checkbox]):not([type=radio]):not(.app-vh), main select, main textarea, main a.tr-btn, header button")].filter(vis);
  for (let i = 0; i < ctrls.length; i++) for (let j = i + 1; j < ctrls.length; j++) {
    const a = ctrls[i], b = ctrls[j];
    if (a.contains(b) || b.contains(a)) continue;
    if (!!a.closest("header") !== !!b.closest("header")) continue; // die klebende Kopfleiste liegt bewusst über gescrolltem Inhalt
    const x = a.getBoundingClientRect(), y = b.getBoundingClientRect();
    const ox = Math.min(x.right, y.right) - Math.max(x.left, y.left), oy = Math.min(x.bottom, y.bottom) - Math.max(x.top, y.top);
    if (ox > 2 && oy > 2) res.push(`überlappen: ${desc(a)} und ${desc(b)}`);
  }
  return res;
}

(async () => {
  const b = await L.launch();
  for (const [name, url] of PAGES) {
    const page = await L.newPage(b, { width: 1920, height: 900, allow: ["segments", "draft"] });
    if (url === "/plan") await mockPlan(page);
    await page.goto(L.BASE + url);
    await page.waitForSelector("h1");
    await page.waitForTimeout(1200);
    if (url.startsWith("/episodes/")) { await page.click("summary:has-text('Transkript')"); await page.waitForSelector(".m-tr-list"); }
    const seen = new Map();
    let n = 0;
    for (let w = 360; w <= 1920; w += 8) {
      await page.setViewportSize({ width: w, height: 900 });
      await page.waitForTimeout(40);
      n++;
      for (const p of await page.evaluate(measure)) if (!seen.has(p)) seen.set(p, w);
    }
    L.ok(seen.size === 0, `${name}: ${n} Breiten ohne Überlauf, Abschneiden oder Überlappung`);
    for (const [p, w] of seen) console.log(`      ab ${w} px: ${p}`);
    await page.context().close();
  }
  await b.close();
  L.done();
})();
