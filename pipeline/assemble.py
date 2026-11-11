"""Schritt 1: Chunks einer Spur zu raw.flac zusammensetzen, Lücken mit Stille füllen.

Ist raw.flac schon da (frühere Verarbeitung, Chunks gelöscht), dient es als Grundlage; neu eingetroffene Chunks werden
darüber gelegt. Nach erfolgreichem, geprüftem Schreiben werden die Chunk-Dateien gelöscht (SPEC Abschnitt 2).
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import soundfile as sf

from .util import PipelineError, read_json, write_json

BPS = {"s16le": 2, "s24le": 3, "f32le": 4}
SUBTYPE = {"s16le": "PCM_16", "s24le": "PCM_24", "f32le": "PCM_24"}
CLIP = 0.9995
BLOCK = 48000 * 10


def decode_pcm(raw: bytes, fmt: str) -> np.ndarray:
    if fmt == "s16le":
        n = len(raw) // 2
        return np.frombuffer(raw[:n * 2], dtype="<i2").astype(np.float32) / 32768.0
    if fmt == "s24le":
        n = len(raw) // 3
        b = np.frombuffer(raw[:n * 3], dtype=np.uint8).reshape(n, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        return v.astype(np.float32) / 8388608.0
    if fmt == "f32le":
        n = len(raw) // 4
        return np.nan_to_num(np.frombuffer(raw[:n * 4], dtype="<f4")).astype(np.float32)
    raise PipelineError(f"Unbekanntes Aufnahmeformat „{fmt}“")


def _intervals_subtract(base: list[tuple[int, int]], cut: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out = []
    for a, b in base:
        pieces = [(a, b)]
        for c0, c1 in cut:
            nxt = []
            for p0, p1 in pieces:
                if c1 <= p0 or c0 >= p1:
                    nxt.append((p0, p1))
                else:
                    if c0 > p0:
                        nxt.append((p0, c0))
                    if c1 < p1:
                        nxt.append((c1, p1))
            pieces = nxt
        out.extend(pieces)
    return out


def assemble_track(track: dict, warnings, progress=None) -> dict:
    """→ {path, frames, sample_rate, gaps: [[first_frame, frames]], clipped_samples, clip_events, peak_db}"""
    tdir = Path(track["dir"])
    fmt = track.get("format") or "s24le"
    if fmt not in BPS:
        raise PipelineError(f"Spur {track.get('participant_name')}: unbekanntes Format „{fmt}“")
    bps = BPS[fmt]
    sr = int(track.get("sample_rate") or 48000)
    raw_path = tdir / "raw.flac"
    meta_path = tdir / "raw.json"
    old = read_json(meta_path) if raw_path.exists() else None
    old_cov = [tuple(c) for c in (old or {}).get("covered", [])] if old else []

    chunks = []
    for c in sorted(track.get("chunks") or [], key=lambda c: (c["first_frame"], c["seq"])):
        p = tdir / "chunks" / f"{int(c['seq']):06d}.pcm"
        if p.exists():
            frames = min(int(c["frames"]), p.stat().st_size // bps)
            if frames > 0:
                chunks.append((int(c["first_frame"]), frames, p))
    total = int(track.get("total_frames") or 0)
    ends = [a + n for a, n, _ in chunks] + [b for _, b in old_cov]
    total = max([total] + ends) if ends else total
    if total <= 0:
        if raw_path.exists() and old:
            return old["result"]
        raise PipelineError(f"Spur von {track.get('participant_name') or 'unbekannt'} enthält keine Audiodaten")

    if not chunks and old and old.get("result", {}).get("frames") == total:
        return old["result"]  # nichts Neues, raw.flac ist aktuell

    # Quellen: Chunks haben Vorrang, sonst alter Stand aus raw.flac, sonst Stille
    sources: list[tuple[int, int, str, object]] = []
    covered: list[tuple[int, int]] = []
    pos = 0
    for a, n, p in chunks:
        a2 = max(a, pos)
        if a2 >= a + n:
            continue
        sources.append((a2, a + n, "chunk", (p, a)))
        covered.append((a2, a + n))
        pos = a + n
    for a, b in _intervals_subtract(old_cov, covered):
        sources.append((a, b, "raw", None))
    sources.sort(key=lambda s: s[0])

    tmp = tdir / "raw.flac.tmp"
    clipped = clip_events = 0
    peak = 0.0
    old_f = sf.SoundFile(str(raw_path)) if old_cov else None
    try:
        with sf.SoundFile(str(tmp), "w", sr, 1, SUBTYPE[fmt], format="FLAC") as out:
            pos = 0
            prev_clip = False
            for i, (a, b, kind, info) in enumerate(sources):
                if a > pos:
                    _write_silence(out, a - pos)
                    pos = a
                for s0 in range(a, b, BLOCK):
                    s1 = min(b, s0 + BLOCK)
                    if kind == "chunk":
                        p, first = info
                        with open(p, "rb") as fh:
                            fh.seek((s0 - first) * bps)
                            x = decode_pcm(fh.read((s1 - s0) * bps), fmt)
                    else:
                        old_f.seek(s0)
                        x = old_f.read(s1 - s0, dtype="float32")
                    if len(x) < s1 - s0:
                        x = np.pad(x, (0, s1 - s0 - len(x)))
                    if fmt == "f32le":
                        x = np.clip(x, -1.0, 1.0)
                    ax = np.abs(x)
                    if len(ax):
                        peak = max(peak, float(ax.max()))
                        cl = ax >= CLIP
                        clipped += int(np.count_nonzero(cl))
                        if cl.any():
                            starts = np.count_nonzero(cl[1:] & ~cl[:-1]) + (1 if cl[0] and not prev_clip else 0)
                            clip_events += int(starts)
                        prev_clip = bool(cl[-1])
                    out.write(x)
                pos = b
                if progress:
                    progress((i + 1) / len(sources))
            if pos < total:
                _write_silence(out, total - pos)
    finally:
        if old_f is not None:
            old_f.close()

    info = sf.info(str(tmp))
    if info.frames != total:
        tmp.unlink(missing_ok=True)
        raise PipelineError(f"raw.flac für {track.get('participant_name')} unvollständig geschrieben")
    os.replace(tmp, raw_path)
    cov = sorted([(a, b) for a, b, _, _ in sources])
    merged: list[list[int]] = []
    for a, b in cov:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    gaps = [[a, b - a] for a, b in _intervals_subtract([(0, total)], [tuple(m) for m in merged])]
    result = {"path": str(raw_path), "frames": total, "sample_rate": sr, "gaps": gaps,
              "clipped_samples": clipped, "clip_events": clip_events,
              "peak_db": round(20 * np.log10(max(peak, 1e-9)), 2)}
    write_json(meta_path, {"covered": merged, "result": result})
    for _, _, p in chunks:
        try:
            p.unlink()
        except OSError:
            pass
    try:
        (tdir / "chunks").rmdir()
    except OSError:
        pass
    return result


def _write_silence(out: sf.SoundFile, n: int) -> None:
    step = 48000 * 10
    while n > 0:
        k = min(step, n)
        out.write(np.zeros(k, dtype=np.float32))
        n -= k
