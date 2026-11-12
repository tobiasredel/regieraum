// Ausgabegerät (Kopfhörer, Lautsprecher) für Stimmen, Jingles, Testton und Hörprobe (SPEC Abschnitt 8, Geräte).
// Stimmen und Hörprobe laufen über <audio>-Elemente (HTMLMediaElement.setSinkId: Chrome, Edge, Firefox), Jingles und
// Testton über den AudioContext (AudioContext.setSinkId: Chrome, Edge). Safari kann beides nicht; dort bleibt die Wahl
// verborgen und es gilt der Ausgang des Systems. "" heißt immer: Standardausgang des Systems.
const V = new URL(import.meta.url).search; // gleiche Version wie die übrigen Module (Cache)
const { t } = await import(`../i18n.js${V}`);

export const elementSupported = () => typeof HTMLMediaElement !== "undefined" && "setSinkId" in HTMLMediaElement.prototype;
export const contextSupported = (ctx) => !!ctx && typeof ctx.setSinkId === "function";
/** Firefox zeigt Ausgänge erst nach einer Auswahl im eigenen Dialog. */
export const canPick = () => !!(navigator.mediaDevices && navigator.mediaDevices.selectAudioOutput);

/** Ausgänge mit Namen, ohne die Sammeleinträge "default"/"communications" (die stehen als „Systemstandard“). */
export async function listOutputs() {
  const all = await navigator.mediaDevices.enumerateDevices();
  return all.filter((d) => d.kind === "audiooutput" && d.deviceId && d.deviceId !== "default" && d.deviceId !== "communications");
}

/** Firefox: Auswahldialog des Browsers. -> deviceId oder null (abgebrochen). */
export async function pick() {
  try { return (await navigator.mediaDevices.selectAudioOutput()).deviceId || null; } catch { return null; }
}

/** Optionen für ein <select>: Systemstandard zuerst, gewähltes Gerät markiert. */
export function options(h, devices, current) {
  return [
    h("option", { value: "", selected: !current }, t("Systemstandard")),
    ...devices.map((d, i) => h("option", { value: d.deviceId, selected: d.deviceId === current }, d.label || t("Ausgang {n}", { n: i + 1 }))),
  ];
}

export async function setElement(el, id) {
  if (!el || !elementSupported() || (el.sinkId || "") === (id || "")) return true;
  try { await el.setSinkId(id || ""); return true; } catch (e) { console.warn("setSinkId", e); return false; }
}

/** Aktueller Ausgang des Context als Zeichenkette ("" = Standard; ein AudioSinkInfo-Objekt zählt als Standard). */
export const contextSink = (ctx) => (typeof ctx.sinkId === "string" ? ctx.sinkId : "");

export async function setContext(ctx, id) {
  if (!contextSupported(ctx) || contextSink(ctx) === (id || "")) return true;
  try { await ctx.setSinkId(id || ""); return true; } catch (e) { console.warn("AudioContext.setSinkId", e); return false; }
}
