// Screenshots 390×844 und 1280×800 aller Seiten nach tests/e2e-manage/shots/ (nicht committen).
const L = require("./lib.cjs");
const { mockPlan } = require("./plan-mock.cjs");
const fs = require("fs");
const path = require("path");
const out = path.join(__dirname, "shots");
fs.mkdirSync(out, { recursive: true });

const PAGES = [
  ["episodes", "/episodes"],
  ["episode", `/episodes/${L.REVIEW}`],
  ["episode-processing", "/episodes/demoep-proc"],
  ["episode-failed", "/episodes/demoep-failed"],
  ["episode-imported", "/episodes/demoep-imp3"],
  ["sounds", "/sounds"],
  ["settings", "/settings"],
  ["plan", "/plan"],
];
const only = process.argv[2];

(async () => {
  const b = await L.launch();
  for (const [w, h, mobile, tag] of [[390, 844, true, "390"], [1280, 800, false, "1280"]]) {
    for (const [name, url] of PAGES) {
      if (only && name !== only) continue;
      const page = await L.newPage(b, { width: w, height: h, mobile, allow: ["segments", "draft"] });
      if (name === "plan") await mockPlan(page);
      await page.goto(L.BASE + url);
      await page.waitForTimeout(1500);
      await page.screenshot({ path: path.join(out, `${name}-${tag}.png`), fullPage: true });
      if (name === "episode") {
        await page.locator("#schnitt").scrollIntoViewIfNeeded();
        await page.waitForTimeout(300);
        const el = await page.$("#schnitt");
        await el.screenshot({ path: path.join(out, `schnitt-${tag}.png`) });
      }
      console.log(name, tag, page.problems);
      await page.context().close();
    }
  }
  await b.close();
})();
