"""Klangfarbe und Pausen je Stem (SPEC Abschnitt 9, Stufe 1, Schritt 3b).

Eingebaute Mikrofone (Laptop, Headset) klingen dünn und topfig: zu wenig zwischen 80 und 250 Hz, zu viel um 400 bis
600 Hz. Dieser Schritt vergleicht das Sprachspektrum jedes Stems mit einer Zielkurve für Sprache (Terzbänder 100 Hz
bis 10 kHz, abgeleitet aus fertig produzierten Gesprächspodcasts) und gleicht die Form behutsam an (geglättet,
höchstens ±5 dB, ohne den Pegel zu ändern). Davor senkt ein sanfter Expander das Grundgeräusch zwischen den Sätzen
(Schwelle 10 dB über dem gemessenen Grundgeräusch der Spur, Verhältnis 1:4, höchstens 18 dB Absenkung). Danach wird
die Sprach-Lautheit auf den Wert vor dem Schritt zurückgesetzt, damit der Pegelangleich der Sprecher erhalten bleibt.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import welch

from .dsp import SR, limit_file

CENTERS = [100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000]
# Mittleres Sprachspektrum (dB je Terzband, Mittelwert 0) fertig produzierter Gesprächspodcasts
TARGET = [14.1, 12.5, 11.4, 13.6, 10.6, 8.1, 8.9, 8.6, 7.6, 2.9, 0.5, 1.1, -1.5, -5.0, -5.9, -9.7, -8.7, -13.1, -15.1, -19.0, -22.0]
MAX_DB = 5.0
MAX_SPEECH_S = 600       # so viel Sprache reicht für ein stabiles Spektrum
MIN_PAUSE_S = 5.0        # weniger Pausen: kein Expander (Grundgeräusch nicht messbar)
GATE_ABOVE_FLOOR_DB = 10.0
GATE_RANGE_DB = 18.0
GATE_LIMITS_DB = (-75.0, -40.0)
FIR_DELAY_S = 0.01       # firequalizer verzögert um genau diese Zeit; wird hinterher abgeschnitten (Synchronität)


def _segments(speech_ms: list[list[int]], n: int) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """(Sprache, Pausen) als Sample-Bereiche."""
    sp = [(max(0, a * SR // 1000), min(n, b * SR // 1000)) for a, b in speech_ms]
    sp = [(a, b) for a, b in sp if b > a]
    pauses, last = [], 0
    for a, b in sp:
        if a > last:
            pauses.append((last, a))
        last = max(last, b)
    if last < n:
        pauses.append((last, n))
    return sp, pauses


def _collect(f: sf.SoundFile, ranges: list[tuple[int, int]], limit: int) -> np.ndarray:
    out, total = [], 0
    for a, b in ranges:
        if total >= limit:
            break
        b = min(b, a + limit - total)
        f.seek(a)
        x = f.read(b - a, dtype="float32", always_2d=False)
        out.append(x)
        total += len(x)
    return np.concatenate(out) if out else np.zeros(0, np.float32)


def band_levels(x: np.ndarray) -> np.ndarray:
    f, p = welch(x, SR, nperseg=8192)
    v = []
    for c in CENTERS:
        lo, hi = c / 2 ** (1 / 6), c * 2 ** (1 / 6)
        v.append(p[(f >= lo) & (f < hi)].mean())
    v = 10 * np.log10(np.maximum(np.array(v), 1e-20))
    return v - v.mean()


def correction(levels: np.ndarray) -> list[float]:
    """Zielkurve minus Stem, geglättet, ohne Pegelanteil (Median 0), höchstens ±MAX_DB."""
    d = np.array(TARGET) - levels
    d = np.convolve(np.pad(d, 1, mode="edge"), [0.25, 0.5, 0.25], mode="valid")
    d = d - np.median(d)
    return [round(float(v), 1) for v in np.clip(d, -MAX_DB, MAX_DB)]


def _rms_db(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(np.square(x, dtype=np.float64))) + 1e-12)) if len(x) else -120.0


def measure(path: Path, speech_ms: list[list[int]]) -> dict:
    """Korrekturkurve und Grundgeräusch eines Stems."""
    with sf.SoundFile(str(path)) as f:
        sp, pauses = _segments(speech_ms, f.frames)
        speech = _collect(f, sp, MAX_SPEECH_S * SR)
        quiet = _collect(f, pauses, MAX_SPEECH_S * SR)
    out: dict = {"curve": None, "floor_db": None, "gate_db": None}
    if len(speech) >= 10 * SR:
        out["curve"] = correction(band_levels(speech))
    if len(quiet) >= MIN_PAUSE_S * SR:
        # Grundgeräusch: leiseste Viertel der 50-ms-Fenster in den Pausen (Atmer und Übersprechen zählen nicht)
        hop = SR // 20
        k = len(quiet) // hop
        fr = 20 * np.log10(np.sqrt(np.mean(np.square(quiet[: k * hop].reshape(k, hop), dtype=np.float64), axis=1)) + 1e-12)
        floor = float(np.percentile(fr, 25))
        out["floor_db"] = round(floor, 1)
        out["gate_db"] = round(float(np.clip(floor + GATE_ABOVE_FLOOR_DB, *GATE_LIMITS_DB)), 1)
    out["speech_rms_db"] = round(_rms_db(speech), 2)
    return out


def filters(m: dict, frames: int | None = None) -> list[str]:
    fl = []
    if m.get("gate_db") is not None:
        thr = 10 ** (m["gate_db"] / 20)
        rng = 10 ** (-GATE_RANGE_DB / 20)
        fl.append(f"agate=threshold={thr:.6f}:ratio=4:range={rng:.4f}:attack=5:release=250:knee=6:detection=rms")
    if m.get("curve"):
        entries = ";".join(f"entry({c},{g:.2f})" for c, g in zip(CENTERS, m["curve"]))
        fl.append(f"firequalizer=delay={FIR_DELAY_S}:gain_entry='{entries}'")
        d = round(FIR_DELAY_S * SR)
        fl.append(f"atrim=start_sample={d}" + (f":end_sample={d + frames}" if frames else "") + ",asetpts=PTS-STARTPTS")
    return fl


def apply(path: Path, speech_ms: list[list[int]], tmpdir: Path) -> dict:
    """Wendet Expander und Korrektur auf den Stem an (Datei wird ersetzt). -> Angaben für den Bericht."""
    m = measure(path, speech_ms)
    fl = filters(m, sf.info(str(path)).frames)
    if not fl:
        return {**m, "applied": False}
    tmpdir.mkdir(parents=True, exist_ok=True)
    tmp = tmpdir / f"{path.stem}.tone.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-af", ",".join(fl), "-ac", "1", "-ar", str(SR),
                    "-c:a", "pcm_f32le", "-f", "wav", "-rf64", "auto", str(tmp)], check=True)
    # Sprachpegel wie vorher (der Pegelangleich der Sprecher bleibt), Spitzen begrenzt wie beim Rendern
    with sf.SoundFile(str(tmp)) as f:
        sp, _ = _segments(speech_ms, f.frames)
        after = _rms_db(_collect(f, sp, MAX_SPEECH_S * SR))
    fix = round(m["speech_rms_db"] - after, 2)
    lim = limit_file(tmp, path, gain_db=fix, ceiling_db=-1.0, true_peak=False, subtype="PCM_24", fmt="FLAC")
    tmp.unlink(missing_ok=True)
    return {**m, "applied": True, "level_fix_db": fix, "limiter": lim}


__all__ = ["CENTERS", "TARGET", "measure", "correction", "filters", "apply"]
