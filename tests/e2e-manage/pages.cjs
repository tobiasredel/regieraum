// Jede Seite laden: keine Konsolenfehler, keine CSP-Verstöße.
const L = require("./lib.cjs");
const { mockPlan } = require("./plan-mock.cjs");
(async () => {
  const b = await L.launch();
  const urls = ["/episodes", `/episodes/${L.REVIEW}`, "/episodes/demoep-proc", "/episodes/demoep-failed", "/episodes/demoep-pub", "/episodes/demoep-imp2", "/sounds", "/settings", "/plan"];
  for (const u of urls) {
    const page = await L.newPage(b);
    if (u === "/plan") await mockPlan(page);
    await page.goto(L.BASE + u);
    await page.waitForSelector("h1");
    await page.waitForTimeout(1200);
    L.ok(page.problems.length === 0, `${u}: keine Konsolen- oder Netzfehler ${page.problems.join(" | ")}`);
    L.ok((await L.cspViolations(page)).length === 0, `${u}: keine CSP-Verstöße`);
    await page.context().close();
  }
  // Plan ohne Server-Unterstützung: ruhiger Hinweis
  const page = await L.newPage(b, { allow: ["segments", "draft"] });
  await mockPlan(page, { missing: true });
  await page.goto(L.BASE + "/plan");
  await page.waitForSelector(".m-empty");
  L.ok((await page.textContent(".m-empty")).includes("noch nicht bereit"), "/plan bei 404: ruhiger Hinweis");
  await b.close();
  L.done();
})();
