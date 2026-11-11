// Klangbericht in Klartext. Liest work/report.json (SPEC Abschnitt 9, Punkt 11) tolerant:
// Die Schlüsselnamen der Pipeline stehen nicht bis ins Letzte fest, deshalb werden mehrere Schreibweisen akzeptiert.
import { fmtClock, fmtDec, fmtDuration, fmtInt, h, neg, signed } from "./common.js";

const first = (...vals) => vals.find((v) => v !== undefined && v !== null && v !== "");
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

function gapInfo(t) {
  const g = first(t.gaps, t.gap_list);
  let count = null;
  let ms = null;
  if (Array.isArray(g)) {
    count = g.length;
    const sr = num(t.sample_rate) || 48000;
    ms = g.reduce((sum, x) => {
      if (Array.isArray(x)) return sum + ((num(x[1]) || 0) / sr) * 1000; // [first_frame, frames]
      if (x && typeof x === "object") return sum + (num(x.ms) ?? ((num(x.to_ms) ?? 0) - (num(x.from_ms) ?? 0)));
      return sum;
    }, 0);
  } else if (num(g) !== null) {
    count = g;
  }
  count = first(count, num(t.gap_count));
  ms = first(ms, num(t.gap_ms), num(t.gap_total_ms));
  return { count, ms };
}

function trackView(t) {
  const name = first(t.participant_name, t.name, t.speaker, "Spur");
  const before = num(first(t.lufs_speech_before, t.lufs_before, t.loudness_before, t.before?.lufs, t.lufs_in, t.in_lufs));
  const after = num(first(t.lufs_speech_after, t.lufs_after, t.loudness_after, t.after?.lufs, t.lufs_out, t.out_lufs, t.lufs));
  const gain = num(first(t.gain_db, t.applied_gain_db, t.leveling_gain_db));
  const drift = num(first(t.drift_ppm, t.clock?.drift_ppm));
  const offset = num(first(t.start_offset_ms, t.offset_ms, t.clock?.start_offset_ms));
  const clips = num(first(t.clip_events, t.clips, t.clipped));
  return { name, before, after, gain, drift, offset, clips, gaps: gapInfo(t) };
}

const lufs = (v) => (v === null ? "–" : `${neg(v)} LUFS`);

export function renderReport(report, targetLufs = null) {
  const box = h("div", { class: "m-report" });
  if (!report || typeof report !== "object") {
    box.append(h("p", { class: "tr-small" }, "Es liegt noch kein Klangbericht vor. Er entsteht bei der Verarbeitung."));
    return box;
  }
  const tracks = (Array.isArray(first(report.tracks, report.stems, report.speakers)) ? first(report.tracks, report.stems, report.speakers) : []).map(trackView);
  const final = num(first(report.master?.verified?.lufs, report.final_lufs, report.lufs_after, report.loudness?.after_lufs, report.loudness?.lufs, report.final?.lufs, report.master?.lufs, report.loudness_after?.lufs, typeof report.loudness === "number" ? report.loudness : undefined));
  const peak = num(first(report.master?.verified?.true_peak_db, report.true_peak_db, report.final_true_peak_db, report.loudness?.true_peak_db, report.final?.true_peak_db, report.master?.true_peak_db));
  const target = num(first(report.master?.target_lufs, report.target_lufs, report.loudness?.target_lufs, targetLufs));
  const duration = num(report.duration_ms);

  // Satz für Satz in Klartext
  const lines = [];
  if (final !== null) {
    lines.push(`Die fertige Folge liegt bei ${lufs(final)}${target !== null ? ` (Ziel ${lufs(target)})` : ""}${peak !== null ? `, True Peak ${neg(peak)} dB` : ""}.`);
  } else if (duration !== null) {
    lines.push(`Länge der fertigen Folge: ${fmtClock(duration)}.`);
  }
  const lev = tracks.filter((t) => t.gain !== null);
  if (lev.length) {
    lines.push("Pegelangleich: " + lev.map((t) => (Math.abs(t.gain) < 0.1 ? `${t.name} unverändert` : `${t.name} ${t.gain > 0 ? "angehoben" : "abgesenkt"} um ${fmtDec(Math.abs(t.gain))} dB`)).join(", ") + ".");
  }
  const gapped = tracks.filter((t) => t.gaps.count > 0);
  if (tracks.length && tracks.some((t) => t.gaps.count !== null)) {
    lines.push(gapped.length
      ? "Lücken: " + gapped.map((t) => `${t.name} ${fmtInt(t.gaps.count)}${t.gaps.ms ? `, zusammen ${fmtDuration(t.gaps.ms) || "unter 1 s"}` : ""}`).join("; ") + ". Sie sind mit Stille gefüllt."
      : "Keine Lücken in den Aufnahmen.");
  }
  const clipped = tracks.filter((t) => t.clips > 0);
  if (tracks.some((t) => t.clips !== null)) {
    lines.push(clipped.length ? "Übersteuerungen: " + clipped.map((t) => `${t.name} ${fmtInt(t.clips)}`).join(", ") + "." : "Keine Übersteuerungen.");
  }
  if (lines.length) {
    const ul = h("ul", { class: "m-plain" });
    lines.forEach((l) => ul.append(h("li", null, l)));
    box.append(ul);
  }

  if (tracks.length) {
    const grid = h("div", { class: "m-rep-tracks" });
    for (const t of tracks) {
      const rows = [
        ["Sprach-Lautheit vorher", lufs(t.before)],
        ["Sprach-Lautheit nachher", lufs(t.after)],
        ["Verstärkung", t.gain === null ? "–" : `${signed(t.gain)} dB`],
        ["Drift", t.drift === null ? "–" : `${signed(t.drift, 0)} ppm`],
        ["Startversatz", t.offset === null ? "–" : `${fmtInt(t.offset)} ms`],
        ["Lücken", t.gaps.count === null ? "–" : t.gaps.count ? `${fmtInt(t.gaps.count)}${t.gaps.ms ? ` (${fmtDuration(t.gaps.ms) || "unter 1 s"})` : ""}` : "keine"],
        ["Übersteuerungen", t.clips === null ? "–" : t.clips ? fmtInt(t.clips) : "keine"],
      ];
      const dl = h("dl", { class: "m-dl" });
      rows.forEach(([k, v]) => dl.append(h("div", null, h("dt", null, k), h("dd", { class: "m-num" }, v))));
      grid.append(h("div", { class: "m-rep-track" }, h("h3", { class: "m-h3" }, t.name), dl));
    }
    box.append(grid);
  }

  const warns = Array.isArray(report.warnings) ? report.warnings : [];
  if (warns.length) {
    const ul = h("ul", { class: "m-plain m-warn" });
    warns.forEach((w) => ul.append(h("li", { class: "tr-status-warn" }, String(w))));
    box.append(h("h3", { class: "m-h3" }, "Warnungen"), ul);
  } else {
    box.append(h("p", { class: "tr-small" }, "Keine Warnungen."));
  }
  return box;
}
