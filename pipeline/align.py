"""Schritt 2: Spuren auf die Sitzungszeit legen und je Person zu einem Stem zusammenfügen.

Uhrenmodell: server_ms(sample) = a + b · sample (a relativ zu rec.start). Gerade robust per RANSAC über Punktpaare
(Ausreißer durch Netzwerk-Jitter fallen heraus), dann kleinste Quadrate über die übrigen Punkte. Die effektive Abtastrate
1000/b wird mit soxr (beliebiges, nicht ganzzahliges Verhältnis, VHQ) auf 48 kHz gebracht: damit verschwindet die
Drift, die Tonhöhe ändert sich nur um die ppm der Drift. Sub-Sample-Versatz (< 21 µs) wird bewusst ignoriert.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr

from . import SR
from .util import PipelineError

MAX_PPM = 1000.0       # mehr ist keine Soundkarten-Drift, sondern ein Messfehler
MIN_SPAN_S = 20.0      # kürzere Uhrenreihen liefern keine verlässliche Steigung
PRIOR_PPM = 100.0      # typische Soundkarten-Drift (Vorwissen für kurze Spuren)
TOL_MS = 12.0          # Uhrenpunkte weiter weg von der Geraden gelten als Ausreißer (Netzwerk-Jitter)
XFADE = int(0.05 * SR)  # Überblendung bei überlappenden Spuren einer Person
EDGE_FADE = int(0.005 * SR)
BLOCK = SR * 10


def fit_clock(track: dict, rec_start: float, warnings) -> dict:
    sr = float(track.get("sample_rate") or SR)
    b0 = 1000.0 / sr
    name = track.get("participant_name") or "unbekannt"
    pts = [(float(p["sample_index"]), float(p["server_ms"]) - rec_start) for p in track.get("clock_points") or []
           if p.get("sample_index") is not None and p.get("server_ms") is not None]
    pts.sort()
    start_est = (float(track["start_server_ms"]) - rec_start) if track.get("start_server_ms") is not None else None
    res = {"points": len(pts), "points_used": 0, "method": "", "residual_ms": None}
    a, b = None, b0
    if pts:
        x = np.array([p[0] for p in pts])
        y = np.array([p[1] for p in pts]) - b0 * x  # Abweichung von der Nennrate (ms)
        keep, sl, ic = _robust_line(x, y, sr, start_est)
        used = int(keep.sum())
        span_s = (x[keep].max() - x[keep].min()) / sr if used else 0.0
        if used >= 3 and span_s >= MIN_SPAN_S and abs(sl / b0) * 1e6 <= MAX_PPM:
            b, a = b0 + sl, ic
            res["method"] = "gerade"
        else:
            # zu wenig Punkte oder zu kurze Spanne für eine Steigung: Nennrate, Versatz als Median
            a = float(np.median(y[keep]))
            res["method"] = "versatz"
            if used >= 3 and span_s >= MIN_SPAN_S:
                warnings.add(f"Spur von {name}: unplausible Uhrendrift, Nennrate verwendet")
        resid = y[keep] - (a + (b - b0) * x[keep])
        res.update(points_used=used, residual_ms=round(float(np.max(np.abs(resid))), 2) if used else None)
        if len(pts) - used:
            res["points_rejected"] = len(pts) - used
    elif start_est is not None:
        a = start_est
        res["method"] = "startzeit"
        warnings.add(f"Spur von {name}: keine Uhrenpunkte, Lage nur aus der Startzeit geschätzt")
    else:
        raise PipelineError(f"Spur von {name} hat weder Uhrenpunkte noch Startzeit")
    eff = 1000.0 / b
    res.update(a_ms=a, b_ms=b, effective_rate=eff, drift_ppm=round((eff / sr - 1.0) * 1e6, 2),
               start_offset_ms=round(a, 1),
               start_estimate_error_ms=round(a - start_est, 1) if start_est is not None else None)
    return res


def _robust_line(x: np.ndarray, y: np.ndarray, sr: float, prior_ic: float | None = None) -> tuple[np.ndarray, float, float]:
    """Robuste Gerade y = ic + sl·x durch Uhrenpunkte (y = Abweichung von der Nennrate in ms).

    RANSAC über Punktpaare (bei vielen Punkten eine Stichprobe), nur Steigungen bis MAX_PPM: gewählt wird die Gerade mit
    den meisten Punkten innerhalb TOL_MS (Gleichstand: kleinster Median-Rest), danach kleinste Quadrate über diese
    Punkte. Die Steigung wird zur Nennrate hin geschrumpft (Vorwissen: Drift typisch ±PRIOR_PPM), je stärker, je
    kürzer und verrauschter die Reihe ist; lange Sitzungen bleiben praktisch unbeeinflusst. Bei Gleichstand zwischen
    Kandidaten entscheidet die Nähe zur vom Client geschätzten Startzeit.
    → (Maske der verwendeten Punkte, Steigung, Achsenabschnitt)
    """
    n = len(x)
    if n == 1:
        return np.ones(1, bool), 0.0, float(y[0])
    max_sl = MAX_PPM * 1e-6 * 1000.0 / sr
    cands = [(0.0, float(v)) for v in y]  # reine Versatz-Kandidaten (Nennrate)
    rng = np.random.default_rng(0)
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)] if n <= 60 else \
        [tuple(sorted(rng.choice(n, 2, replace=False))) for _ in range(2000)]
    for i, j in pairs:
        dx = x[j] - x[i]
        if dx / sr < MIN_SPAN_S / 2:
            continue
        sl = (y[j] - y[i]) / dx
        if abs(sl) <= max_sl:
            cands.append((sl, float(y[i] - sl * x[i])))
    best = None
    for sl, ic in cands:
        r = np.abs(y - (ic + sl * x))
        inl = r <= TOL_MS
        tie = -abs(ic - prior_ic) if prior_ic is not None else -float(np.median(r[inl])) if inl.any() else -1e9
        key = (int(inl.sum()), tie)
        if best is None or key > best[0]:
            best = (key, inl)
    keep = best[1]
    if keep.sum() >= 2 and np.ptp(x[keep]) / sr >= MIN_SPAN_S / 2:
        sl, ic = _shrunk_fit(x[keep], y[keep], sr)
        keep2 = np.abs(y - (ic + sl * x)) <= TOL_MS  # zweite Runde nahe der ausgeglichenen Geraden
        if keep2.sum() >= keep.sum():
            keep = keep2
            sl, ic = _shrunk_fit(x[keep], y[keep], sr)
        return keep, sl, ic
    return keep, 0.0, float(np.median(y[keep]))


def _shrunk_fit(x: np.ndarray, y: np.ndarray, sr: float) -> tuple[float, float]:
    sl, ic = np.polyfit(x, y, 1)
    xm = float(x.mean())
    sxx = float(np.sum((x - xm) ** 2))
    resid = y - (ic + sl * x)
    sigma = max(2.0, float(np.std(resid)) * math.sqrt(len(x) / max(1, len(x) - 2)))
    var_m = sigma ** 2 / sxx                       # Varianz der Steigungsschätzung
    var_p = (PRIOR_PPM * 1e-6 * 1000.0 / sr) ** 2  # Vorwissen über die Drift
    sl = float(sl) * var_p / (var_p + var_m)
    ic = float(np.mean(y - sl * x))
    return sl, ic


def render_track(raw_path: Path, clock: dict, frames: int, n_session: int, dst: Path, progress=None) -> tuple[int, int]:
    """Resampelt eine Spur auf die Sitzungszeit (48 kHz) in eine eigene Datei. → (n0, länge) in Sitzungs-Samples."""
    a, b = clock["a_ms"], clock["b_ms"]
    # Eingangs-Sample, das zur Sitzungszeit 0 gehört
    s_at0 = -a / b
    i0 = max(0, int(math.floor(s_at0)))
    n0 = int(round((a + b * i0) * SR / 1000.0))
    if i0 >= frames or n0 >= n_session:
        return 0, 0
    ratio = SR / clock["effective_rate"]
    want = min(n_session - n0, int(math.floor((frames - i0) * ratio)))
    skip = max(0, -n0)  # bei n0 < 0 (Rundung) vorne verwerfen
    if want - skip <= 0:
        return 0, 0
    rs = soxr.ResampleStream(clock["effective_rate"], SR, 1, dtype="float32", quality="VHQ")
    written = 0
    dropped = 0
    with sf.SoundFile(str(raw_path)) as f, sf.SoundFile(str(dst), "w", SR, 1, "FLOAT", format="RF64") as out:
        f.seek(i0)
        pos = i0
        while written < want - skip:
            x = f.read(BLOCK, dtype="float32")
            pos += len(x)
            last = len(x) < BLOCK or pos >= frames
            y = rs.resample_chunk(x, last=last)
            if dropped < skip:
                k = min(skip - dropped, len(y))
                y = y[k:]
                dropped += k
            y = y[: (want - skip) - written]
            out.write(y)
            written += len(y)
            if progress:
                progress(written / max(1, want - skip))
            if last:
                break
    return max(0, n0), written


def combine(parts: list[dict], n_session: int, dst: Path, progress=None) -> list[list[int]]:
    """Legt die gerenderten Spuren einer Person auf einen Stem. parts: [{path, n0, n}] → Lücken [[n0, n1]].

    Überlappungen werden in der Mitte mit bis zu 50 ms linear überblendet (gleiches Mikrofon, korreliertes Signal);
    freie Ränder bekommen 5 ms Ein-/Ausblendung, außer am Anfang und Ende der Sitzung.
    """
    parts = sorted([p for p in parts if p["n"] > 0], key=lambda p: p["n0"])
    # Spuren, die ganz in einer anderen liegen, entfallen
    kept: list[dict] = []
    for p in parts:
        if kept and p["n0"] + p["n"] <= kept[-1]["n0"] + kept[-1]["n"]:
            continue
        kept.append(dict(p))
    for i, p in enumerate(kept):
        p["lo"], p["hi"] = p["n0"], p["n0"] + p["n"]
        p["fade_in"] = (p["lo"], EDGE_FADE if p["lo"] > 0 else 0)
        p["fade_out"] = (p["hi"], EDGE_FADE if p["hi"] < n_session else 0)
    for p, q in zip(kept, kept[1:]):
        ov = p["hi"] - q["lo"]
        if ov > 0:
            x = min(XFADE, ov)
            m = (q["lo"] + p["hi"]) // 2
            p["fade_out"] = (m + x // 2, x)
            q["fade_in"] = (m - x // 2, x)
    gaps = []
    pos = 0
    for p in kept:
        if p["lo"] > pos:
            gaps.append([pos, p["lo"]])
        pos = max(pos, p["hi"])
    if pos < n_session:
        gaps.append([pos, n_session])

    files = [sf.SoundFile(p["path"]) for p in kept]
    try:
        with sf.SoundFile(str(dst), "w", SR, 1, "FLOAT", format="RF64") as out:
            for s in range(0, n_session, BLOCK):
                e = min(n_session, s + BLOCK)
                acc = np.zeros(e - s, dtype=np.float32)
                for p, f in zip(kept, files):
                    lo = max(s, p["lo"])
                    fi_start, fi_len = p["fade_in"]   # Einblendung ab fi_start über fi_len
                    fo_end, fo_len = p["fade_out"]    # Ausblendung endet bei fo_end
                    hi = min(e, p["hi"], fo_end)
                    lo = max(lo, fi_start)
                    if hi <= lo:
                        continue
                    f.seek(lo - p["n0"])
                    x = f.read(hi - lo, dtype="float32")
                    if len(x) < hi - lo:
                        x = np.pad(x, (0, hi - lo - len(x)))
                    n = np.arange(lo, hi)
                    w = np.ones(hi - lo, dtype=np.float32)
                    if fi_len:
                        w = np.minimum(w, np.clip((n - fi_start + 0.5) / fi_len, 0, 1))
                    if fo_len:
                        w = np.minimum(w, np.clip((fo_end - n - 0.5) / fo_len, 0, 1))
                    acc[lo - s:hi - s] += x * w
                out.write(acc)
                if progress:
                    progress(e / n_session)
    finally:
        for f in files:
            f.close()
    return gaps
