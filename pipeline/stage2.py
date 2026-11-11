"""Stufe 2: mischen, ungeschnittene Vorschau, schneiden, mastern, kodieren → out/"""
from __future__ import annotations

import math
import os
import subprocess
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from . import SR, timeline
from .dsp import LoudnessMeter, limit_file, loudness_of
from .tags import write_tags
from .util import PipelineError, Progress, Warnings, decode_audio, ebur128, ffmpeg, r1, read_json, write_json

BLOCK = SR * 10
PEAK_SPAN = 960            # 50 Werte je Sekunde
XF_HALF = int(0.0075 * SR)  # 15 ms Überblendung an Schnittkanten
EDGE = int(0.01 * SR)       # Ein-/Ausblenden am Anfang und Ende der Folge
MUSIC_BELOW_SPEECH = 2.0
MAX_SOUND_S = 30 * 60
KEEP = bool(os.environ.get("PS_KEEP_WORK"))  # Zwischenstände behalten (Fehlersuche)


def _ms(n: float) -> int:
    return int(round(n * 1000 / SR))


def _load_sounds(m: dict, speech_ref: float, warnings: Warnings) -> list[dict]:
    out = []
    cache: dict[str, tuple[np.ndarray, float]] = {}
    for s in m.get("sounds") or []:
        path = s.get("path")
        if not path or not Path(path).is_file():
            warnings.add(f"Jingle-Datei fehlt ({Path(path or '?').name}), Jingle nicht gemischt")
            continue
        if path not in cache:
            try:
                x = decode_audio(Path(path), 2, SR, max_seconds=MAX_SOUND_S)
            except PipelineError:
                warnings.add(f"Jingle-Datei {Path(path).name} lässt sich nicht lesen, Jingle nicht gemischt")
                continue
            cache[path] = (x, loudness_of(x) if len(x) else -math.inf)
        x, lufs = cache[path]
        if len(x) == 0:
            continue
        if not math.isfinite(lufs):
            gain = float(s.get("gain_db") or 0.0)
            warnings.add(f"Jingle {Path(path).name} ist zu leise zum Messen, ohne Angleich gemischt")
        else:
            gain = speech_ref - MUSIC_BELOW_SPEECH + float(s.get("gain_db") or 0.0) - lufs
        y = x * np.float32(10 ** (gain / 20))
        at = int(round(float(s.get("at_ms") or 0) * SR / 1000))
        if s.get("stop_ms") is not None:
            stop = int(round(float(s["stop_ms"]) * SR / 1000)) - at
            fade = max(0, int(round(float(s.get("fade_ms") or 0) * SR / 1000)))
            if stop < len(y):
                stop = max(0, stop)
                end = min(len(y), stop + fade)
                y = y[:end].copy()
                if end > stop:
                    t = (np.arange(end - stop) + 0.5) / max(1, fade)
                    y[stop:end] *= (np.cos(np.clip(t, 0, 1) * np.pi / 2) ** 2)[:, None].astype(np.float32)
        if at < 0:
            y = y[-at:]
            at = 0
        out.append({"event_id": s.get("event_id"), "sound_id": s.get("sound_id"), "n0": at, "data": y,
                    "lufs": r1(lufs), "gain_db": r1(gain), "duration_ms": _ms(len(x)), "at_ms": s.get("at_ms"),
                    "played_ms": _ms(len(y))})
    return out


def _mix(stems: list[Path], sounds: list[dict], n_mix: int, dst: Path, progress) -> tuple[float, np.ndarray, np.ndarray]:
    files = [sf.SoundFile(str(p)) for p in stems]
    meter = LoudnessMeter(2)
    nb = -(-n_mix // PEAK_SPAN)
    pmin = np.zeros(nb, np.float32)
    pmax = np.zeros(nb, np.float32)
    try:
        with sf.SoundFile(str(dst), "w", SR, 2, "FLOAT", format="RF64") as out:
            for s in range(0, n_mix, BLOCK):
                e = min(n_mix, s + BLOCK)
                acc = np.zeros((e - s, 2), np.float32)
                for f in files:
                    if s < f.frames:
                        f.seek(s)
                        x = f.read(min(e, f.frames) - s, dtype="float32")
                        acc[: len(x)] += x[:, None]
                for snd in sounds:
                    a, b = snd["n0"], snd["n0"] + len(snd["data"])
                    lo, hi = max(a, s), min(b, e)
                    if hi > lo:
                        acc[lo - s:hi - s] += snd["data"][lo - a:hi - a]
                meter.feed(acc)
                # Wellenform: Min/Max je 960 Samples (BLOCK ist ein Vielfaches)
                k0 = s // PEAK_SPAN
                mono_max = acc.max(axis=1)
                mono_min = acc.min(axis=1)
                pad = (-len(acc)) % PEAK_SPAN
                if pad:
                    mono_max = np.concatenate([mono_max, np.full(pad, mono_max[-1])])
                    mono_min = np.concatenate([mono_min, np.full(pad, mono_min[-1])])
                pmax[k0:k0 + len(mono_max) // PEAK_SPAN] = mono_max.reshape(-1, PEAK_SPAN).max(axis=1)
                pmin[k0:k0 + len(mono_min) // PEAK_SPAN] = mono_min.reshape(-1, PEAK_SPAN).min(axis=1)
                out.write(acc)
                progress(e / n_mix)
    finally:
        for f in files:
            f.close()
    return meter.integrated(), pmin, pmax


def _cut(src: Path, segs: list[tuple[int, int]], dst: Path, progress) -> tuple[int, float]:
    """Schneidet nach Segmenten (Samples) mit gleichleistungs-Überblendung (15 ms) an jeder Kante."""
    meter = LoudnessMeter(2)
    k = len(segs)
    halves = []
    for i in range(k - 1):
        (a0, b0), (a1, b1) = segs[i], segs[i + 1]
        h = min(XF_HALF, (b0 - a0) // 2, (b1 - a1) // 2)
        halves.append(max(0, h))
    total_out = sum(b - a for a, b in segs)
    written = 0
    pending = None
    with sf.SoundFile(str(src)) as f, sf.SoundFile(str(dst), "w", SR, 2, "FLOAT", format="RF64") as out:
        n_src = f.frames

        def read(a, b):
            lo, hi = max(0, a), min(n_src, b)
            res = np.zeros((b - a, 2), np.float32)
            if hi > lo:
                f.seek(lo)
                res[lo - a:hi - a] = f.read(hi - lo, dtype="float32", always_2d=True)
            return res

        def emit(x):
            nonlocal written
            if len(x) == 0:
                return
            x = x.copy()
            # Anfang und Ende der Folge weich
            if written < EDGE:
                idx = np.arange(written, written + len(x))
                w = np.clip((idx + 0.5) / EDGE, 0, 1)
                x *= w[:, None].astype(np.float32)
            if written + len(x) > total_out - EDGE:
                idx = np.arange(written, written + len(x))
                w = np.clip((total_out - idx - 0.5) / EDGE, 0, 1)
                x *= w[:, None].astype(np.float32)
            meter.feed(x)
            out.write(x)
            written += len(x)
            progress(written / max(1, total_out))

        for i, (a, b) in enumerate(segs):
            h_in = halves[i - 1] if i > 0 else 0
            h_out = halves[i] if i < k - 1 else 0
            start, end = a - h_in, b + h_out
            main_end = end - 2 * h_out
            first = True
            for s in range(start, main_end, BLOCK):
                e = min(main_end, s + BLOCK)
                x = read(s, e)
                if first and h_in:
                    t = (np.arange(2 * h_in) + 0.5) / (2 * h_in)
                    x[: 2 * h_in] *= np.sin(t * np.pi / 2)[:, None].astype(np.float32)
                    x[: 2 * h_in] += pending
                    pending = None
                first = False
                emit(x)
            if h_out:
                x = read(main_end, end)
                t = (np.arange(2 * h_out) + 0.5) / (2 * h_out)
                pending = x * np.cos(t * np.pi / 2)[:, None].astype(np.float32)
    if written != total_out:
        raise PipelineError("Schnitt ergab eine unerwartete Länge")
    return total_out, meter.integrated()


def _codec_offset(cut: Path, gain_db: float, kbps: int, work: Path) -> float:
    """Lautheit MP3 minus Lautheit WAV an 60 s aus der Mitte der Folge."""
    with sf.SoundFile(str(cut)) as f:
        n = f.frames
        k = min(n, SR * 60)
        f.seek(max(0, (n - k) // 2))
        x = f.read(k, dtype="float32", always_2d=True) * np.float32(10 ** (gain_db / 20))
    lw = loudness_of(x)
    if not math.isfinite(lw):
        return 0.0
    wav, mp3 = work / "codec_probe.wav", work / "codec_probe.mp3"
    try:
        sf.write(str(wav), np.clip(x, -1, 1), SR, subtype="FLOAT")
        _encode_mp3(wav, mp3, kbps)
        y = decode_audio(mp3, 2, SR)
        lm = loudness_of(y)
    finally:
        wav.unlink(missing_ok=True)
        mp3.unlink(missing_ok=True)
    off = lm - lw if math.isfinite(lm) else 0.0
    return float(np.clip(off, -2.0, 2.0))


def _encode_mp3(src: Path, dst: Path, kbps: int) -> None:
    tmp = dst.with_name(dst.name + ".tmp.mp3")
    ffmpeg(["-y", "-i", str(src), "-af", "aresample=resampler=soxr:precision=28", "-ar", "44100", "-ac", "2",
            "-c:a", "libmp3lame", "-b:a", f"{int(kbps)}k", "-map_metadata", "-1", "-id3v2_version", "0",
            "-write_id3v1", "0", "-f", "mp3", str(tmp)], "MP3 kodieren")
    os.replace(tmp, dst)


def run(m: dict, prog: Progress) -> dict:
    work, out_dir = Path(m["work_dir"]), Path(m["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    stems_dir = work / "stems"
    warnings = Warnings()
    audio = m.get("audio") or {}
    target = float(audio.get("target_lufs", -16) if audio.get("target_lufs") is not None else -16)
    tp_limit = float(audio.get("true_peak_db", -1.0) if audio.get("true_peak_db") is not None else -1.0)
    kbps = int(audio.get("mp3_kbps") or 160)
    report = read_json(work / "report.json", {}) or {}

    pids = []
    for t in m.get("tracks") or []:
        pid = t.get("participant_id") or t["id"]
        if pid not in pids:
            pids.append(pid)
    stems = [stems_dir / f"{pid}.flac" for pid in pids if (stems_dir / f"{pid}.flac").exists()]
    if not stems:
        raise PipelineError("Es gibt keine bearbeiteten Stimmen; Stufe 1 muss zuerst laufen")
    n_stem = max(sf.info(str(p)).frames for p in stems)

    t0 = time.monotonic()
    timing: dict[str, float] = {}

    def lap(name: str) -> None:
        nonlocal t0
        now = time.monotonic()
        timing[name] = round(timing.get(name, 0.0) + now - t0, 1)
        t0 = now

    # 5) Mischen
    prog(0.0, "Jingles werden vorbereitet")
    louds = [s.get("lufs_speech_after") for s in report.get("stems") or [] if s.get("lufs_speech_after") is not None]
    speech_mono = float(np.mean(louds)) if louds else -20.0
    speech_ref = speech_mono + 10 * math.log10(2)  # Mono-Stimme auf beiden Kanälen
    sounds = _load_sounds(m, speech_ref, warnings)
    n_mix = max([n_stem] + [s["n0"] + len(s["data"]) for s in sounds])
    mix_path = work / "mix.wav"
    p_mix = prog.sub(0.02, 0.25)
    lufs_uncut, pmin, pmax = _mix(stems, sounds, n_mix, mix_path, lambda f: p_mix(f, "Stimmen und Jingles werden gemischt"))
    for s in sounds:
        s.pop("data", None)

    lap("mischen")
    # 6) ungeschnittene Vorschau (läuft parallel zum Schneiden und Mastern) und Wellenform
    prog(0.25, "Vorschau wird kodiert")
    g_uncut = (target - lufs_uncut) if math.isfinite(lufs_uncut) else 0.0
    lim = 10 ** ((tp_limit - 0.5) / 20)
    preview_tmp = out_dir / "mix_uncut.tmp.m4a"
    preview = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-threads", "1", "-i", str(mix_path), "-af",
         f"volume={g_uncut:.2f}dB,alimiter=limit={lim:.4f}:level=disabled:latency=1:attack=5:release=50",
         "-c:a", "aac", "-b:a", "96k", "-ar", str(SR), "-ac", "2", "-movflags", "+faststart", "-f", "mp4",
         str(preview_tmp)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        gl = 10 ** (g_uncut / 20)
        q = lambda v: np.clip(np.round(v * gl * 127), -128, 127).astype(int)  # noqa: E731
        pk = np.empty(2 * len(pmin), dtype=int)
        pk[0::2], pk[1::2] = q(pmin), q(pmax)
        write_json(out_dir / "peaks.json", {"sample_rate": SR, "samples_per_peak": PEAK_SPAN, "peaks": pk.tolist()})

        # 7) Schneiden
        session_ms = n_mix * 1000 / SR
        segs_ms = timeline.segments(m.get("edl"), session_ms)
        segs = []
        for a, b in segs_ms:
            a_n, b_n = int(round(a * SR / 1000)), int(round(b * SR / 1000))
            if b_n > a_n:
                segs.append((a_n, b_n))
        if not segs:
            raise PipelineError("Nach dem Schnitt bleibt nichts von der Folge übrig")
        cut_path = work / "cut.wav"
        p_cut = prog.sub(0.3, 0.45)
        n_out, lufs_cut = _cut(mix_path, segs, cut_path, lambda f: p_cut(f, "Folge wird geschnitten"))
        lap("schneiden")
        if not math.isfinite(lufs_cut):
            raise PipelineError("Die geschnittene Folge ist stumm")

        # 8) Mastern (zweistufig: gemessen → Verstärkung + True-Peak-Begrenzer → nachgemessen) und 9) MP3
        master_path = work / "master.wav"
        mp3 = out_dir / "episode.mp3"
        gain = target - lufs_cut
        ceiling = tp_limit - 0.8
        attempts = []
        verified = {}
        lufs_master = None
        # Der MP3-Kodierer verschiebt die Lautheit je nach Bitrate um einige Zehntel LU; an einem Ausschnitt vorab messen,
        # damit in der Regel ein Durchgang genügt (nachgemessen wird trotzdem am fertigen MP3).
        prog(0.45, "Kodierer wird eingemessen")
        codec_offset = _codec_offset(cut_path, gain, kbps, work)
        wav_target = target - codec_offset
        gain = wav_target - lufs_cut
        for attempt in range(5):
            p_m = prog.sub(0.45 + 0.1 * min(attempt, 3), 0.55 + 0.1 * min(attempt, 3))
            meter = LoudnessMeter(2)
            stats = limit_file(cut_path, master_path, gain_db=gain, ceiling_db=ceiling, true_peak=True, meter=meter,
                               fmt="RF64", progress=lambda f, p=p_m: p(f * 0.7, "Lautheit wird angeglichen"))
            lufs_master = meter.integrated()
            attempts.append({"gain_db": r1(gain, 2), "ceiling_db": r1(ceiling, 2), "lufs": r1(lufs_master, 2),
                             "limiter": stats})
            if abs(lufs_master - wav_target) > 0.3 and attempt < 2:
                gain += wav_target - lufs_master
                continue
            p_m(0.7, "MP3 wird kodiert")
            _encode_mp3(master_path, mp3, kbps)
            verified = ebur128(mp3)
            attempts[-1]["verified"] = verified
            tp, lv = verified.get("true_peak_db"), verified.get("lufs")
            if lv is not None:
                wav_target = target - (lv - lufs_master)
            redo = False
            if tp is not None and tp > tp_limit - 0.05:
                ceiling -= (tp - tp_limit) + 0.2
                redo = True
            if lv is not None and abs(lv - target) > 0.2:
                # der MP3-Kodierer verschiebt die Lautheit je nach Bitrate leicht; am fertigen Produkt nachregeln
                gain += target - lv
                redo = True
            if not redo or attempt == 4:
                break
        if not KEEP:
            master_path.unlink(missing_ok=True)
            cut_path.unlink(missing_ok=True)
        if verified.get("lufs") is not None and abs(verified["lufs"] - target) > 0.7:
            warnings.add(f"Lautheit der fertigen Folge {verified['lufs']:.1f} LUFS statt {target:.0f} LUFS")
        if verified.get("true_peak_db") is not None and verified["true_peak_db"] > tp_limit:
            warnings.add(f"True Peak der fertigen Folge {verified['true_peak_db']:.1f} dBTP liegt über {tp_limit:.1f} dBTP")
        if attempts[-1]["limiter"]["max_reduction_db"] > 6:
            warnings.add(f"Der Begrenzer musste stellenweise um {attempts[-1]['limiter']['max_reduction_db']:.0f} dB "
                         "eingreifen (laute Jingles oder Lacher)")

        lap("mastern_kodieren")
    except BaseException:
        preview.kill()
        preview.wait()
        raise
    _, perr = preview.communicate()
    if preview.returncode != 0 or not preview_tmp.exists():
        preview_tmp.unlink(missing_ok=True)
        raise PipelineError(f"Vorschau kodieren fehlgeschlagen ({perr.decode('utf-8', 'replace').strip()[-300:]})")
    os.replace(preview_tmp, out_dir / "mix_uncut.m4a")
    lap("vorschau_warten")
    if not KEEP:
        mix_path.unlink(missing_ok=True)
    # 10) Kapitel, Tags
    prog(0.95, "Kapitel und Tags werden geschrieben")
    mm = dict(m)
    mm["sounds"] = [dict(s, duration_ms=next((x["played_ms"] for x in sounds if x["event_id"] == s.get("event_id")
                                                and x["event_id"] is not None), None))
                    for s in m.get("sounds") or []]
    mm["rec_stop_server_ms"] = float(m["rec_start_server_ms"]) + session_ms
    chapters = timeline.chapters(mm)
    duration_ms = _ms(n_out)
    chapters = [c for c in chapters if c["startTime"] * 1000 < duration_ms]
    write_json(out_dir / "chapters.json", {"version": "1.2.0",
                                           "chapters": [{"startTime": c["startTime"], "title": c["title"]}
                                                        for c in chapters]})
    write_tags(mp3, m.get("tags") or {}, chapters, duration_ms)

    # 11) Bericht
    report.update({
        "duration_ms": duration_ms,
        "session_duration_ms": _ms(n_mix),
        "mix": {"speech_lufs_reference": r1(speech_ref), "sounds": sounds, "lufs_uncut": r1(lufs_uncut),
                "preview_gain_db": r1(g_uncut)},
        "master": {"target_lufs": target, "codec_offset_lu": r1(codec_offset, 2), "true_peak_limit_db": tp_limit, "lufs_before": r1(lufs_cut),
                   "gain_db": r1(gain, 2), "lufs": r1(lufs_master, 2),
                   "limiter_max_reduction_db": attempts[-1]["limiter"]["max_reduction_db"],
                   "verified": {"tool": "ffmpeg ebur128 (episode.mp3)", **{k: r1(v, 2) for k, v in verified.items()}},
                   "attempts": attempts},
        "mp3": {"kbps": kbps, "sample_rate": 44100, "bytes": mp3.stat().st_size},
        "chapters": [{"startTime": c["startTime"], "title": c["title"]} for c in chapters],
        "edl": {"segments_ms": [[_ms(a), _ms(b)] for a, b in segs]},
        "warnings_stage2": list(warnings),
        "timing_stage2_s": timing,
    })
    report["warnings"] = list(report.get("warnings_stage1") or []) + list(warnings)
    write_json(work / "report.json", report)
    prog(1.0, "Fertig")
    return report
