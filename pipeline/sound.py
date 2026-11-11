"""Jingle prüfen: beliebige Audiodatei → playback.wav (48 kHz stereo s16) und meta.json {duration_ms, lufs, peak_db}.

`peak_db` ist der True Peak (dBTP) der Wiedergabedatei. Die Pipeline mischt später die Originaldatei (gleicher
Decoder, also gleiche Lage) und gleicht ihre Lautheit selbst an.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
import soundfile as sf

from . import SR
from .dsp import loudness_of, peak_per_sample
from .util import PipelineError, Progress, decode_audio, r1, write_json

MAX_S = 30 * 60


def run(src: Path, dst_dir: Path, prog: Progress) -> dict:
    src, dst_dir = Path(src), Path(dst_dir)
    if not src.is_file():
        raise PipelineError("Die Datei wurde nicht gefunden")
    dst_dir.mkdir(parents=True, exist_ok=True)
    prog(0.1, "Datei wird gelesen")
    try:
        x = decode_audio(src, 2, SR, max_seconds=MAX_S + 1)
    except PipelineError as exc:
        raise PipelineError("Die Datei lässt sich nicht als Audio lesen") from exc
    if len(x) == 0:
        raise PipelineError("Die Datei enthält kein Audio")
    if len(x) > MAX_S * SR:
        raise PipelineError("Die Datei ist länger als 30 Minuten")
    peak = float(np.max(peak_per_sample(x, True)))
    if peak < 10 ** (-80 / 20):
        raise PipelineError("Die Datei ist stumm")
    prog(0.5, "Lautheit wird gemessen")
    lufs = loudness_of(x)
    tmp = dst_dir / "playback.tmp.wav"
    y = np.clip(x, -1.0, 32767 / 32768)
    sf.write(str(tmp), y, SR, subtype="PCM_16", format="WAV")
    os.replace(tmp, dst_dir / "playback.wav")
    meta = {"duration_ms": int(round(len(x) * 1000 / SR)),
            "lufs": r1(lufs) if math.isfinite(lufs) else None,
            "peak_db": r1(20 * math.log10(peak))}
    write_json(dst_dir / "meta.json", meta)
    prog(1.0, "Fertig")
    return meta
