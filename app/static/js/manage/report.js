// Klangbericht in Klartext. Liest work/report.json (SPEC Abschnitt 9, Punkt 11) tolerant:
// Die Schlüsselnamen der Pipeline stehen nicht bis ins Letzte fest, deshalb werden mehrere Schreibweisen akzeptiert.
import { fmtClock, fmtDec, fmtDuration, fmtInt, h, neg, signed, t } from "./common.js";

const first = (...vals) => vals.find((v) => v !== undefined && v !== null && v !== "");
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

function gapInfo(tr) {
  const g = first(tr.gaps, tr.gap_list);
  let count = null;
  let ms = null;
  if (Array.isArray(g)) {
    count = g.length;
    const sr = num(tr.sample_rate) || 48000;
    ms = g.reduce((sum, x) => {
      if (Array.isArray(x)) return sum + ((num(x[1]) || 0) / sr) * 1000; // [first_frame, frames]
      if (x && typeof x === "object") return sum + (num(x.ms) ?? ((num(x.to_ms) ?? 0) - (num(x.from_ms) ?? 0)));
      return sum;
    }, 0);
  } else if (num(g) !== null) {
    count = g;
  }
  count = first(count, num(tr.gap_count));
  ms = first(ms, num(tr.gap_ms), num(tr.gap_total_ms));
  return { count, ms };
}

function trackView(tr) {
  const name = first(tr.participant_name, tr.name, tr.speaker, t("Spur"));
  const before = num(first(tr.lufs_speech_before, tr.lufs_before, tr.loudness_before, tr.before?.lufs, tr.lufs_in, tr.in_lufs));
  const after = num(first(tr.lufs_speech_after, tr.lufs_after, tr.loudness_after, tr.after?.lufs, tr.lufs_out, tr.out_lufs, tr.lufs));
  const gain = num(first(tr.gain_db, tr.applied_gain_db, tr.leveling_gain_db));
  const drift = num(first(tr.drift_ppm, tr.clock?.drift_ppm));
  const offset = num(first(tr.start_offset_ms, tr.offset_ms, tr.clock?.start_offset_ms));
  const clips = num(first(tr.clip_events, tr.clips, tr.clipped));
  const range = Array.isArray(tr.session_range_ms) ? tr.session_range_ms.map(num) : [null, null];
  return { key: first(tr.participant_id, name), name, before, after, gain, drift, offset, clips, gaps: gapInfo(tr), range };
}

/** Spuren einer Person zusammenfassen: Ein Gerätewechsel oder eine kurze Unterbrechung beginnt eine neue Spur,
 *  im Bericht bleibt es eine Person. `splits`: Beginn jeder weiteren Spur und die Lücke davor (Sitzungszeit). */
function personView(list) {
  const sorted = [...list].sort((a, b) => (a.range[0] ?? 0) - (b.range[0] ?? 0));
  const main = sorted.reduce((a, b) => ((b.range[1] ?? 0) - (b.range[0] ?? 0) > (a.range[1] ?? 0) - (a.range[0] ?? 0) ? b : a));
  const sum = (k) => (sorted.every((x) => x[k] === null) ? null : sorted.reduce((n, x) => n + (x[k] || 0), 0));
  const splits = sorted.slice(1).map((x, i) => {
    const prevEnd = sorted[i].range[1], start = x.range[0];
    return { at: start, gap: start !== null && prevEnd !== null ? Math.max(0, start - prevEnd) : null };
  });
  const gapsCount = sorted.every((x) => x.gaps.count === null) ? null : sorted.reduce((n, x) => n + (x.gaps.count || 0), 0);
  const gapsMs = sorted.some((x) => x.gaps.ms) ? sorted.reduce((n, x) => n + (x.gaps.ms || 0), 0) : null;
  return {
    name: sorted[0].name, before: main.before, after: main.after, gain: main.gain,
    drifts: sorted.map((x) => x.drift).filter((v) => v !== null), offset: sorted[0].offset,
    clips: sum("clips"), gaps: { count: gapsCount, ms: gapsMs }, splits,
  };
}

function groupPersons(tracks) {
  const by = new Map();
  for (const tr of tracks) { if (!by.has(tr.key)) by.set(tr.key, []); by.get(tr.key).push(tr); }
  return [...by.values()].map(personView);
}

const lufs = (v) => (v === null ? "–" : t("{n} LUFS", { n: neg(v) }));
const fmtGap = (ms) => (ms < 1000 ? t("{s} s", { s: fmtDec(ms / 1000) }) : fmtDuration(ms));

export function renderReport(report, targetLufs = null) {
  const box = h("div", { class: "m-report" });
  if (!report || typeof report !== "object") {
    box.append(h("p", { class: "tr-small" }, t("Es liegt noch kein Klangbericht vor. Er entsteht bei der Verarbeitung.")));
    return box;
  }
  const tracks = groupPersons((Array.isArray(first(report.tracks, report.stems, report.speakers)) ? first(report.tracks, report.stems, report.speakers) : []).map(trackView));
  const final = num(first(report.master?.verified?.lufs, report.final_lufs, report.lufs_after, report.loudness?.after_lufs, report.loudness?.lufs, report.final?.lufs, report.master?.lufs, report.loudness_after?.lufs, typeof report.loudness === "number" ? report.loudness : undefined));
  const peak = num(first(report.master?.verified?.true_peak_db, report.true_peak_db, report.final_true_peak_db, report.loudness?.true_peak_db, report.final?.true_peak_db, report.master?.true_peak_db));
  const target = num(first(report.master?.target_lufs, report.target_lufs, report.loudness?.target_lufs, targetLufs));
  const duration = num(report.duration_ms);

  // Satz für Satz in Klartext
  const lines = [];
  if (final !== null) {
    const p = { lufs: lufs(final), target: target !== null ? lufs(target) : "", peak: peak !== null ? neg(peak) : "" };
    lines.push(target !== null
      ? (peak !== null ? t("Die fertige Folge liegt bei {lufs} (Ziel {target}), True Peak {peak} dB.", p) : t("Die fertige Folge liegt bei {lufs} (Ziel {target}).", p))
      : (peak !== null ? t("Die fertige Folge liegt bei {lufs}, True Peak {peak} dB.", p) : t("Die fertige Folge liegt bei {lufs}.", p)));
  } else if (duration !== null) {
    lines.push(t("Länge der fertigen Folge: {time}.", { time: fmtClock(duration) }));
  }
  const lev = tracks.filter((tr) => tr.gain !== null);
  if (lev.length) {
    const items = lev.map((tr) => (Math.abs(tr.gain) < 0.1
      ? t("{name} unverändert", { name: tr.name })
      : tr.gain > 0 ? t("{name} angehoben um {db} dB", { name: tr.name, db: fmtDec(Math.abs(tr.gain)) }) : t("{name} abgesenkt um {db} dB", { name: tr.name, db: fmtDec(Math.abs(tr.gain)) })));
    lines.push(t("Pegelangleich: {items}.", { items: items.join(", ") }));
  }
  const gapped = tracks.filter((tr) => tr.gaps.count > 0);
  if (tracks.length && tracks.some((tr) => tr.gaps.count !== null)) {
    lines.push(gapped.length
      ? t("Lücken: {items}. Sie sind mit Stille gefüllt.", {
        items: gapped.map((tr) => (tr.gaps.ms
          ? t("{name} {count}, zusammen {duration}", { name: tr.name, count: fmtInt(tr.gaps.count), duration: fmtDuration(tr.gaps.ms) || t("unter 1 s") })
          : t("{name} {count}", { name: tr.name, count: fmtInt(tr.gaps.count) }))).join("; "),
      })
      : t("Keine Lücken in den Aufnahmen."));
  }
  const clipped = tracks.filter((tr) => tr.clips > 0);
  if (tracks.some((tr) => tr.clips !== null)) {
    lines.push(clipped.length ? t("Übersteuerungen: {items}.", { items: clipped.map((tr) => t("{name} {count}", { name: tr.name, count: fmtInt(tr.clips) })).join(", ") }) : t("Keine Übersteuerungen."));
  }
  for (const p of tracks.filter((x) => x.splits.length)) {
    lines.push(t("{name}: neue Spur bei {times}. Das Studio beginnt eine, wenn der Ton kurz aussetzt oder das Gerät wechselt; dazwischen fehlen {gap}.", {
      name: p.name, times: p.splits.map((x) => fmtClock(x.at ?? 0)).join(", "),
      gap: fmtGap(p.splits.reduce((n, x) => n + (x.gap || 0), 0)),
    }));
  }
  if (lines.length) {
    const ul = h("ul", { class: "m-plain" });
    lines.forEach((l) => ul.append(h("li", null, l)));
    box.append(ul);
  }

  if (tracks.length) {
    const grid = h("div", { class: "m-rep-tracks" });
    for (const tr of tracks) {
      const rows = [
        [t("Sprach-Lautheit vorher"), lufs(tr.before)],
        [t("Sprach-Lautheit nachher"), lufs(tr.after)],
        [t("Verstärkung"), tr.gain === null ? "–" : t("{n} dB", { n: signed(tr.gain) })],
        [t("Drift"), tr.drifts.length ? tr.drifts.map((d) => t("{n} ppm", { n: signed(d, 0) })).join(" · ") : "–"],
        [t("Spurbeginn"), tr.offset === null ? "–" : tr.offset < 0 ? t("{s} s vor dem Start", { s: fmtDec(-tr.offset / 1000) }) : t("bei {time}", { time: fmtClock(tr.offset) })],
        ...(tr.splits.length ? [[t("Weitere Spuren"), tr.splits.map((x) => t("ab {time}", { time: fmtClock(x.at ?? 0) })).join(", ")]] : []),
        [t("Lücken"), tr.gaps.count === null ? "–" : tr.gaps.count ? (tr.gaps.ms ? t("{count} ({duration})", { count: fmtInt(tr.gaps.count), duration: fmtDuration(tr.gaps.ms) || t("unter 1 s") }) : fmtInt(tr.gaps.count)) : t("keine")],
        [t("Übersteuerungen"), tr.clips === null ? "–" : tr.clips ? fmtInt(tr.clips) : t("keine")],
      ];
      const dl = h("dl", { class: "m-dl" });
      rows.forEach(([k, v]) => dl.append(h("div", null, h("dt", null, k), h("dd", { class: "m-num" }, v))));
      grid.append(h("div", { class: "m-rep-track" }, h("h3", { class: "m-h3" }, tr.name), dl));
    }
    box.append(grid);
  }

  const warns = Array.isArray(report.warnings) ? report.warnings : [];
  if (warns.length) {
    const ul = h("ul", { class: "m-plain m-warn" });
    warns.forEach((w) => ul.append(h("li", { class: "tr-status-warn" }, String(w))));
    box.append(h("h3", { class: "m-h3" }, t("Warnungen")), ul);
  } else {
    box.append(h("p", { class: "tr-small" }, t("Keine Warnungen.")));
  }
  return box;
}
