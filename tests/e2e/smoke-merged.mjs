// Rauchtest nach dem Zusammenführen: Host-Seiten laden ohne Konsolenfehler, Sendeplan kommt vom echten Server.
import { chromium, BASE, api, sleep } from "./lib.mjs";
const out = process.env.OUT || "/tmp";
const browser = await chromium.launch({ args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"] });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, permissions: ["microphone", "camera"] });
const errors = [];
const page = await ctx.newPage();
page.on("console", (m) => { if (m.type() === "error") errors.push(`${page.url()} ${m.text()}`); });
page.on("pageerror", (e) => errors.push(`${page.url()} ${e.message}`));
const draft = await api("POST", "/api/host/episodes/draft");
const eid = (draft.episode || draft).id;
const segs = await api("GET", `/api/host/episodes/${eid}/segments`);
const list = Array.isArray(segs) ? segs : segs.segments;
await api("PATCH", `/api/host/segments/${list[0].id}`, { title: "Parkgebühren", input: "Stichworte" });
for (const [name, url] of [["plan", "/plan"], ["episodes", "/episodes"], ["sounds", "/sounds"], ["settings", "/settings"], ["studio", "/studio"]]) {
  await page.goto(BASE + url, { waitUntil: "networkidle" });
  await sleep(1500);
  await page.screenshot({ path: `${out}/merged-${name}.png`, fullPage: false });
}
// Studio betreten: ersten Primary-Button des Gerätechecks drücken
await page.getByRole("button", { name: "Mikrofon freigeben" }).click({ timeout: 15000 });
await page.getByRole("button", { name: "Studio betreten" }).click({ timeout: 15000 });
await page.getByText("Soundboard").first().waitFor({ timeout: 20000 });
await sleep(3000);
await page.screenshot({ path: `${out}/merged-studio-drin.png`, fullPage: true });
const text = await page.locator("body").innerText();
console.log(JSON.stringify({ errors, hatPlan: /Parkgebühren/.test(text), hatBlitzlicht: /Blitzlicht/.test(text), keinPlan: /Kein Sendeplan/.test(text) }, null, 1));
await browser.close();
