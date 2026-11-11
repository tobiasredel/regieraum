// Sprache der Oberfläche. Deutsch ist die Quellsprache: Jeder sichtbare Text steht als deutscher Schlüssel in t("…").
// Für andere Sprachen liegt je Bereich ein Wörterbuch unter /static/i18n/<sprache>.<bereich>.json (deutscher Text →
// Übersetzung). Fehlt ein Eintrag, erscheint der deutsche Text. Platzhalter: t("Dauerlink für {who}", { who }).
// Die Sprache bestimmt die Installation (Einstellungen → Sprache der Oberfläche); sie steht in <html lang>.

export const LANG = (document.documentElement.lang || "de").slice(0, 2).toLowerCase();
/** Gebietsschema für Zahlen und Datum (Intl). */
export const LOCALE = LANG === "en" ? "en-GB" : "de-DE";

const AREAS = ["studio", "manage", "server"];
let dict = {};
if (LANG !== "de") {
  const v = new URL(import.meta.url).search;
  const parts = await Promise.all(AREAS.map((a) =>
    fetch(`/static/i18n/${LANG}.${a}.json${v}`).then((r) => (r.ok ? r.json() : {})).catch(() => ({}))));
  dict = Object.assign({}, ...parts);
}

/** Text in der Sprache der Oberfläche. `key` ist der deutsche Text, `params` füllt {platzhalter}. */
export function t(key, params) {
  let s = Object.prototype.hasOwnProperty.call(dict, key) ? dict[key] : key;
  if (params) s = s.replace(/\{(\w+)\}/g, (m, k) => (k in params ? String(params[k]) : m));
  return s;
}
