"""Stufe 1: zusammensetzen, ausrichten, verbessern → work/stems/<participant_id>.flac"""
from __future__ import annotations

import math
import time
from pathlib import Path

import soundfile as sf

from . import SR, align, assemble, enhance, tone
from .util import PipelineError, Progress, Warnings, r1, read_json, write_json


def _ms(n: float) -> int:
    return int(round(n * 1000 / SR))


def run(m: dict, prog: Progress) -> dict:
    work = Path(m["work_dir"])
    stems_dir = work / "stems"
    stems_dir.mkdir(parents=True, exist_ok=True)
    warnings = Warnings()
    audio = m.get("audio") or {}
    rec_start = float(m["rec_start_server_ms"])
    rec_stop = float(m.get("rec_stop_server_ms") or rec_start)
    n_session = int(round((rec_stop - rec_start) * SR / 1000))
    if n_session <= 0:
        raise PipelineError("Die Aufnahme hat keine Dauer (Stopp liegt nicht nach dem Start)")
    tracks = m.get("tracks") or []
    if not tracks:
        raise PipelineError("Die Folge hat keine Spuren")

    timing: dict[str, float] = {}
    t_last = [time.monotonic()]

    def lap(name: str) -> None:
        now = time.monotonic()
        timing[name] = round(timing.get(name, 0.0) + now - t_last[0], 1)
        t_last[0] = now

    # 1) zusammensetzen
    p_asm = prog.sub(0.0, 0.12)
    track_reports = []
    for i, t in enumerate(tracks):
        p_asm(i / len(tracks), f"Spur {i + 1} von {len(tracks)} wird zusammengesetzt")
        res = assemble.assemble_track(t, warnings, p_asm.sub(i / len(tracks), (i + 1) / len(tracks)))
        t["_raw"] = res
        if res["clip_events"]:
            warnings.add(f"Übersteuerung in der Spur von {t.get('participant_name')}: {res['clip_events']} Stellen")

    lap("zusammensetzen")
    # 2) ausrichten, je Person ein Stem
    p_al = prog.sub(0.12, 0.3)
    people: dict[str, dict] = {}
    for t in tracks:
        pid = t.get("participant_id") or t["id"]
        people.setdefault(pid, {"pid": pid, "name": t.get("participant_name") or "Unbekannt", "tracks": []})["tracks"].append(t)
    for i, t in enumerate(tracks):
        p_al(i / len(tracks), "Spuren werden ausgerichtet")
        clock = align.fit_clock(t, rec_start, warnings)
        t["_clock"] = clock
        tmp = stems_dir / f"{t['id']}.track.wav"
        n0, n = align.render_track(Path(t["_raw"]["path"]), clock, t["_raw"]["frames"], n_session, tmp,
                                   p_al.sub(i / len(tracks), (i + 0.9) / len(tracks)))
        t["_part"] = {"path": str(tmp), "n0": n0, "n": n}
        gaps = []
        sr = t["_raw"]["sample_rate"]
        for g0, gn in t["_raw"]["gaps"]:
            a = clock["a_ms"] + clock["b_ms"] * g0
            b = clock["a_ms"] + clock["b_ms"] * (g0 + gn)
            a, b = max(0.0, a), min(rec_stop - rec_start, b)
            if b > a:
                gaps.append({"from_ms": int(a), "to_ms": int(math.ceil(b))})
                warnings.add(f"Lücke in der Spur von {t.get('participant_name')} bei {_clock_str(a)} "
                             f"({(b - a) / 1000:.1f} s fehlen, mit Stille gefüllt)")
        track_reports.append({
            "track_id": t["id"], "participant_id": t.get("participant_id"), "participant_name": t.get("participant_name"),
            "frames": t["_raw"]["frames"], "sample_rate": sr,
            "drift_ppm": clock["drift_ppm"], "start_offset_ms": clock["start_offset_ms"],
            "start_estimate_error_ms": clock["start_estimate_error_ms"], "clock_method": clock["method"],
            "clock_points": clock["points"], "clock_points_used": clock["points_used"],
            "clock_residual_ms": clock["residual_ms"], "gaps": gaps,
            "clipped_samples": t["_raw"]["clipped_samples"], "clip_events": t["_raw"]["clip_events"],
            "peak_db": t["_raw"]["peak_db"], "session_range_ms": [_ms(n0), _ms(n0 + n)],
            "meta": t.get("meta") or {},
        })
        if clock.get("start_estimate_error_ms") is not None and abs(clock["start_estimate_error_ms"]) > 250:
            warnings.add(f"Startzeit der Spur von {t.get('participant_name')} lag {clock['start_estimate_error_ms']:.0f} ms "
                         "daneben und wurde über die Uhrenpunkte korrigiert")

    stems = []
    for j, (pid, p) in enumerate(people.items()):
        aligned = stems_dir / f"{pid}.aligned.wav"
        gaps = align.combine([t["_part"] for t in p["tracks"]], n_session, aligned)
        for t in p["tracks"]:
            Path(t["_part"]["path"]).unlink(missing_ok=True)
        for g0, g1 in gaps:
            if g1 - g0 > SR // 2 and 0 < g0 and g1 < n_session:
                warnings.add(f"{p['name']}: zwischen zwei Spuren fehlen {(g1 - g0) / SR:.1f} s bei {_clock_str(_ms(g0))} "
                             "(Neuverbindung), mit Stille gefüllt")
        cover = n_session - sum(g1 - g0 for g0, g1 in gaps)
        if cover < n_session * 0.5:
            warnings.add(f"Die Spur von {p['name']} deckt nur {100 * cover / n_session:.0f} % der Aufnahme ab")
        stems.append({"pid": pid, "name": p["name"], "aligned": aligned, "gaps": gaps, "tracks": p["tracks"]})

    lap("ausrichten")
    # 3) verbessern
    level = (audio.get("denoise") or "normal")
    if level not in enhance.DENOISE_ATTEN:
        warnings.add(f"Unbekannte Stufe der Rauschunterdrückung „{level}“, „normal“ verwendet")
        level = "normal"
    atten = enhance.DENOISE_ATTEN[level]
    p_en = prog.sub(0.3, 0.85)
    k = len(stems)
    for j, st in enumerate(stems):
        p_en(0.1 * j / k, f"Stimme {j + 1} von {k}: Hochpass und Messung")
        hp = stems_dir / f"{st['pid']}.hp.wav"
        info = enhance.highpass_file(st["aligned"], hp, p_en.sub(0.1 * j / k, 0.1 * (j + 1) / k))
        st["aligned"].unlink(missing_ok=True)
        st["pre"] = info["eK"]
        st["lufs_in"] = info["lufs"]
        st["src"] = hp
        if atten is not None:
            pg = enhance.pre_gain_for(info["lufs"], info["peak"])
            st["dn_job"] = enhance.denoise_prepare(hp, atten, pg, stems_dir / f"tmp_{st['pid']}")
    lap("hochpass")
    if atten is not None:
        p_dn = p_en.sub(0.1, 0.85)
        enhance.denoise_run([st["dn_job"] for st in stems], lambda f: p_dn(f, "Rauschunterdrückung läuft"))
        for st in stems:
            dn = stems_dir / f"{st['pid']}.dn.wav"
            enhance.denoise_finish(st.pop("dn_job"), dn)
            st["src"].unlink(missing_ok=True)
            st["src"] = dn
        lap("rauschunterdrueckung")
    for j, st in enumerate(stems):
        p_en(0.85 + 0.15 * j / k, f"Stimme {j + 1} von {k}: Analyse")
        st["an"] = enhance.analyze_file(st["src"], p_en.sub(0.85 + 0.15 * j / k, 0.85 + 0.15 * (j + 1) / k))
    lap("analyse")

    prog(0.85, "Pegel, Crossgate und Übersprechen werden berechnet")
    enhance.plan(stems, bool(audio.get("crossgate", True)), warnings)
    lap("planung")

    p_r = prog.sub(0.86, 1.0)
    out_stems = []
    for j, st in enumerate(stems):
        p_r(j / k, f"Stimme {j + 1} von {k} wird gerendert")
        dst = stems_dir / f"{st['pid']}.flac"
        lim = enhance.render(st["src"], st, stems_dir / f"{st['pid']}.gain.wav", dst, p_r.sub(j / k, (j + 0.7) / k))
        st["src"].unlink(missing_ok=True)
        speech_ms = enhance.speech_intervals(st["own_mask"])
        tone_info = None
        if audio.get("tone", True):
            p_r((j + 0.7) / k, f"Stimme {j + 1} von {k}: Klangfarbe und Pausen")
            tone_info = tone.apply(dst, speech_ms, stems_dir / f"tmp_tone_{st['pid']}")
        an_after = enhance.analyze_file(dst, p_r.sub((j + 0.8) / k, (j + 1) / k), keys=("eK", "eS"))
        summ = enhance.summarize(st, an_after)
        summ.update(participant_id=st["pid"], participant_name=st["name"], path=str(dst),
                    lufs_integrated_before=r1(st["lufs_in"]), limiter=lim,
                    gaps_ms=[[_ms(a), _ms(b)] for a, b in st["gaps"]], tone=tone_info)
        out_stems.append(summ)
        write_json(stems_dir / f"{st['pid']}.speech.json", {"hop_ms": 1, "speech_ms": speech_ms})
        st.pop("an", None)
        lap("rendern")

    # Spuren mit Stem-Angaben zusammenführen
    by_pid = {s["participant_id"]: s for s in out_stems}
    for tr in track_reports:
        s = by_pid.get(tr["participant_id"] or tr["track_id"])
        if s:
            tr["lufs_speech_before"] = s["lufs_speech_before"]
            tr["lufs_speech_after"] = s["lufs_speech_after"]
            tr["gain_db"] = s["gain_db"]

    louds = [s["lufs_speech_after"] for s in out_stems if s["lufs_speech_after"] is not None]
    if len(louds) > 1 and max(louds) - min(louds) > 1.5:
        warnings.add(f"Die Stimmen sind nach dem Angleich noch {max(louds) - min(louds):.1f} LU auseinander")

    report = read_json(work / "report.json", {}) or {}
    report.update({
        "version": 1,
        "session_duration_ms": _ms(n_session),
        "denoise": level, "crossgate": bool(audio.get("crossgate", True)),
        "tracks": track_reports,
        "stems": out_stems,
        "warnings_stage1": list(warnings),
        "timing_stage1_s": timing,
    })
    report["warnings"] = list(warnings) + list(report.get("warnings_stage2") or [])
    write_json(work / "report.json", report)
    prog(1.0, "Stufe 1 fertig")
    return report


def _clock_str(ms: float) -> str:
    s = int(ms // 1000)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
