// Englische Oberfläche: Auf einer frischen Installation mit ui.language = en darf in Verwaltung und Studio kein
// deutscher Text mehr sichtbar sein (Sprachnamen in der Sprachwahl ausgenommen). Danach wieder Deutsch.
//
//   PS_JOBS=0 ./dev.sh serve-e2e 8196      (anderes Terminal, leeres Datenverzeichnis)
//   node tests/e2e/english.mjs

import path from "node:path";
import { launch, page, enter, newInvite, ensureSounds, clearCurrent, api, BASE, SHOTS, sleep, log, ensureDir } from "./lib.mjs";

const failures = [];
const check = (ok, what) => { if (!ok) { failures.push(what); log("FEHLER:", what); } else log("ok:", what); };

// Umlaute oder häufige deutsche Wörter, die in englischem Text nicht vorkommen
const GERMAN = /[äöüÄÖÜß]|\b(und|oder|nicht|der|die|das|den|dem|ein|eine|für|mit|von|zu|zum|zur|ist|sind|wird|werden|noch|kein|keine|Aufnahme|Folge|Folgen|Einstellungen|Speichern|Teilnehmer|Mikrofon|Kopfhörer|Sendeplan|Vorbereitung|Abschnitt|Begleiter|Bezeichnung|Hinzufügen|Abbrechen|Schließen|Kamera|Technik|Stumm|Beenden|Hören|Tasten|bis|Bereit|Gast|Verbindung|Spuren|laden|Pegel)\b/;
const ALLOW = [/^Deutsch$/, /^Sound$/, /^Intro$/, /^Outro$/, /^Blitzlicht$/, /^Tusch$/]; // Sprachname, Testdaten aus ensureSounds

async function leftovers(p) {
  const lines = await p.evaluate(() => {
    const out = new Set();
    const push = (s) => { for (const l of String(s || "").split("\n")) { const x = l.trim(); if (x) out.add(x); } };
    push(document.body.innerText);
    for (const el of document.querySelectorAll("[title],[aria-label],[placeholder],[alt]")) {
      if (el.closest("[hidden]")) continue;
      for (const a of ["title", "aria-label", "placeholder", "alt"]) if (el.getAttribute(a)) push(el.getAttribute(a));
    }
    for (const o of document.querySelectorAll("option")) push(o.textContent);
    return [...out];
  });
  return lines.filter((l) => GERMAN.test(l) && !ALLOW.some((a) => a.test(l)));
}

async function main() {
  ensureDir(SHOTS);
  await clearCurrent();
  await ensureSounds();
  await api("PUT", "/api/host/settings", { ui: { language: "en" } });
  const b = await launch();
  try {
    const host = await page(b, "host", { viewport: { width: 1280, height: 800 }, quiet: true });
    for (const [url, wait] of [["/episodes", "main"], ["/plan", ".m-seg"], ["/sounds", ".m-sound"], ["/settings", "#format"]]) {
      await host.goto(BASE + url);
      await host.waitForSelector(wait, { timeout: 15000 });
      await sleep(600);
      const rest = await leftovers(host);
      check(rest.length === 0, `${url}: kein deutscher Text` + (rest.length ? "\n   " + rest.slice(0, 12).join("\n   ") : ""));
      check((await host.evaluate(() => document.documentElement.lang)) === "en", `${url}: <html lang="en">`);
    }
    await host.screenshot({ path: path.join(SHOTS, "en-settings-1280.png") });

    await host.goto(`${BASE}/studio?testsignal=local`);
    await host.waitForSelector("#ps-go");
    let rest = await leftovers(host);
    check(rest.length === 0, "Studio, Gerätecheck: kein deutscher Text" + (rest.length ? "\n   " + rest.slice(0, 12).join("\n   ") : ""));
    await enter(host);
    const inv = await newInvite("Sam", "single");
    const guest = await page(b, "gast", { viewport: { width: 390, height: 844 }, quiet: true });
    await guest.goto(inv.url.replace(/^https?:\/\/[^/]+/, BASE) + "?testsignal=local");
    await guest.waitForSelector("#ps-go");
    rest = await leftovers(guest);
    check(rest.length === 0, "Gast, Beitrittsseite: kein deutscher Text" + (rest.length ? "\n   " + rest.slice(0, 12).join("\n   ") : ""));
    await enter(guest, { name: "Sam" });
    await sleep(800);
    rest = await leftovers(host);
    check(rest.length === 0, "Studio vor der Aufnahme: kein deutscher Text" + (rest.length ? "\n   " + rest.slice(0, 12).join("\n   ") : ""));
    rest = await leftovers(guest);
    check(rest.length === 0, "Gast im Studio: kein deutscher Text" + (rest.length ? "\n   " + rest.slice(0, 12).join("\n   ") : ""));

    await host.click("#ps-start");
    await host.waitForFunction(() => document.body.classList.contains("ps-live"), null, { timeout: 15000 });
    await host.waitForFunction(() => /\d+:\d\d/.test(document.querySelector(".ps-status").textContent), null, { timeout: 15000 });
    const status = await host.textContent(".ps-status");
    check(/^Recording \d+:\d\d/.test(status), `Statuszeile englisch: „${status}“`);
    rest = await leftovers(host);
    check(rest.length === 0, "Sendemodus: kein deutscher Text" + (rest.length ? "\n   " + rest.slice(0, 12).join("\n   ") : ""));
    await host.screenshot({ path: path.join(SHOTS, "en-live-1280.png") });
    await host.click("#ps-tech-open");
    rest = await leftovers(host);
    check(rest.length === 0, "Dialog Technik: kein deutscher Text" + (rest.length ? "\n   " + rest.slice(0, 12).join("\n   ") : ""));
    await host.click("#ps-tech-close");
    await host.click("#ps-stop");
    rest = await leftovers(host);
    check(rest.length === 0, "Rückfrage beim Beenden: kein deutscher Text" + (rest.length ? "\n   " + rest.slice(0, 12).join("\n   ") : ""));
    await host.click("#ps-stop-confirm");
    await host.waitForSelector("#ps-discard", { timeout: 20000 });
    await sleep(500);
    rest = await leftovers(host);
    check(rest.length === 0, "nach dem Beenden: kein deutscher Text" + (rest.length ? "\n   " + rest.slice(0, 12).join("\n   ") : ""));
    // Servermeldung in der Sprache der Oberfläche
    const r = await fetch(BASE + "/api/host/episodes/gibtesnicht", { method: "DELETE" });
    const msg = (await r.json()).error.message;
    check(!GERMAN.test(msg), `Fehlermeldung des Servers englisch: „${msg}“`);
  } finally {
    await api("PUT", "/api/host/settings", { ui: { language: "de" } }).catch(() => {});
    await b.close();
    await clearCurrent().catch(() => {});
  }
  if (failures.length) { log(`${failures.length} Fehler`); process.exit(1); }
  log("alles in Ordnung");
}

main().catch((e) => { console.error(e); process.exit(1); });
