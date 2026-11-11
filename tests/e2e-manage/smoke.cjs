const L = require("./lib.cjs");
(async () => {
  const b = await L.launch();
  const page = await L.newPage(b);
  for (const p of ["/episodes", "/sounds", "/settings"]) {
    await page.goto(L.BASE + p);
    await page.waitForTimeout(800);
    console.log(p, await page.title(), page.problems);
  }
  await b.close();
})();
