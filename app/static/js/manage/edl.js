// Schnittliste (EDL) als reine Funktionen, ohne DOM. Alle Zeiten in Millisekunden Sitzungszeit.
// Form: {trim_start_ms, trim_end_ms | null, cuts: [{from_ms, to_ms}]}, wie in SPEC Abschnitt 5.

export const MIN_CUT_MS = 50;
export const MIN_KEEP_MS = 1000;

export function emptyEdl() {
  return { trim_start_ms: 0, trim_end_ms: null, cuts: [] };
}

export function clone(edl) {
  return { trim_start_ms: edl.trim_start_ms || 0, trim_end_ms: edl.trim_end_ms ?? null, cuts: (edl.cuts || []).map((c) => ({ from_ms: c.from_ms, to_ms: c.to_ms })) };
}

export function equal(a, b) {
  return JSON.stringify(normalize(a)) === JSON.stringify(normalize(b));
}

/** Sortiert, verschmilzt überlappende Schnitte, verwirft leere. Mit `total` zusätzlich auf die Länge begrenzt. */
export function normalize(edl, total = Infinity) {
  const ts = Math.max(0, Math.round(edl.trim_start_ms || 0));
  let te = edl.trim_end_ms == null ? null : Math.round(edl.trim_end_ms);
  if (te !== null && (te <= ts || te >= total)) te = null;
  const raw = (edl.cuts || [])
    .map((c) => [Math.max(0, Math.round(c.from_ms)), Math.min(total, Math.round(c.to_ms))])
    .filter(([a, b]) => b - a >= 1)
    .sort((x, y) => x[0] - y[0]);
  const merged = [];
  for (const [a, b] of raw) {
    const last = merged[merged.length - 1];
    if (last && a <= last[1]) last[1] = Math.max(last[1], b);
    else merged.push([a, b]);
  }
  return { trim_start_ms: ts, trim_end_ms: te, cuts: merged.map(([a, b]) => ({ from_ms: a, to_ms: b })) };
}

export function endOf(edl, total) {
  return edl.trim_end_ms == null ? total : Math.min(edl.trim_end_ms, total);
}

export function addCut(edl, from, to, total) {
  const a = Math.max(0, Math.min(from, to));
  const b = Math.min(total, Math.max(from, to));
  if (b - a < MIN_CUT_MS) return normalize(edl, total);
  return normalize({ ...edl, cuts: [...edl.cuts, { from_ms: a, to_ms: b }] }, total);
}

export function removeCut(edl, index) {
  return { ...clone(edl), cuts: edl.cuts.filter((_, i) => i !== index).map((c) => ({ ...c })) };
}

export function setTrimStart(edl, ms, total) {
  const limit = endOf(edl, total) - MIN_KEEP_MS;
  return normalize({ ...edl, trim_start_ms: Math.max(0, Math.min(ms, limit)) }, total);
}

export function setTrimEnd(edl, ms, total) {
  const v = Math.min(total, Math.max(ms, (edl.trim_start_ms || 0) + MIN_KEEP_MS));
  return normalize({ ...edl, trim_end_ms: v >= total ? null : v }, total);
}

/** Index des Schnitts, der `ms` enthält, sonst -1. */
export function cutAt(edl, ms) {
  return edl.cuts.findIndex((c) => ms >= c.from_ms && ms < c.to_ms);
}

/** Dauer der Folge nach Schnitt und Trimmen. */
export function editedDuration(edl, total) {
  const start = edl.trim_start_ms || 0;
  const end = endOf(edl, total);
  let len = Math.max(0, end - start);
  for (const c of edl.cuts) {
    const a = Math.max(c.from_ms, start);
    const b = Math.min(c.to_ms, end);
    if (b > a) len -= b - a;
  }
  return Math.max(0, len);
}

/** Wohin der Abspielkopf beim Vorhören springen muss, wenn `ms` in einem ausgeschnittenen Bereich liegt.
 *  Liefert `ms` selbst, wenn nichts zu überspringen ist, und `Infinity`, wenn die Wiedergabe enden muss. */
export function skipTarget(edl, ms, total, extra = []) {
  const end = endOf(edl, total);
  const start = edl.trim_start_ms || 0;
  const all = [...edl.cuts.map((c) => [c.from_ms, c.to_ms]), ...extra.map((c) => [c.from_ms, c.to_ms])];
  let t = Math.max(ms, start);
  for (let guard = 0; guard < all.length + 2; guard++) {
    const hit = all.find(([a, b]) => t >= a && t < b);
    if (!hit) break;
    t = hit[1];
  }
  return t >= end ? Infinity : t;
}

/** Sitzungszeit in Folgenzeit, wie der Server (core.session_to_episode_ms). */
export function sessionToEpisode(edl, ms, total) {
  const start = edl.trim_start_ms || 0;
  const end = endOf(edl, total);
  let t = Math.min(Math.max(ms, start), end);
  let removed = 0;
  for (const c of edl.cuts) {
    const a = Math.max(c.from_ms, start);
    const b = Math.min(c.to_ms, end);
    if (b <= a) continue;
    if (t >= b) removed += b - a;
    else if (t > a) t = a;
  }
  return Math.max(0, t - start - removed);
}
