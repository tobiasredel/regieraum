"""Transkript: faster-whisper je Stem (Sprecher ist dadurch bekannt), Deutsch, VAD an.

transcript.json in Sitzungszeit, transcript.vtt in Folgenzeit mit Sprecher (<v Name>).
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from . import SR, timeline
from .util import THREADS, PipelineError, Progress, read_json, write_json


def _load_16k(path: Path) -> np.ndarray:
    """Stem (48 kHz) → 16 kHz float32, blockweise, damit 90 Minuten nicht als float64 im Speicher landen."""
    parts = []
    with sf.SoundFile(str(path)) as f:
        for blk in f.blocks(SR * 60, dtype="float32", overlap=0):
            parts.append(resample_poly(blk, 1, 3).astype(np.float32))
    return np.concatenate(parts) if parts else np.zeros(0, np.float32)


def _mask_16k(spans: list, n: int) -> np.ndarray:
    """1 in den Bereichen eigener Sprache (50 ms Rampen), sonst 0: Übersprechen anderer wird nicht transkribiert."""
    m = np.zeros(n, np.float32)
    r = 800
    for a, b in spans:
        a, b = int(a * 16), int(b * 16)
        a0, b1 = max(0, a - r), min(n, b + r)
        if b1 <= a0:
            continue
        idx = np.arange(a0, b1)
        w = np.clip(np.minimum((idx - (a - r)) / r, ((b + r) - idx) / r), 0, 1).astype(np.float32)
        m[a0:b1] = np.maximum(m[a0:b1], w)
    return m


def _regions(spans: list, total_ms: int, gap_ms: int = 1500, pad_ms: int = 200) -> list[list[int]]:
    """Sprechbereiche zu Redebeiträgen zusammenfassen (Lücken unter 1,5 s schließen, 200 ms Rand)."""
    out: list[list[int]] = []
    for a, b in sorted(spans):
        a, b = max(0, int(a) - pad_ms), min(total_ms, int(b) + pad_ms)
        if out and a - out[-1][1] < gap_ms:
            out[-1][1] = max(out[-1][1], b)
        elif b > a:
            out.append([a, b])
    return out


def _ts(ms: float) -> str:
    ms = max(0, int(round(ms)))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, f = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{f:03d}"


def run(m: dict, prog: Progress) -> dict:
    work, out_dir = Path(m["work_dir"]), Path(m["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    model_name = os.environ.get("PS_WHISPER_MODEL") or "large-v3-turbo"
    models = Path(os.environ.get("PS_DATA") or "/data") / "models"
    models.mkdir(parents=True, exist_ok=True)
    # Hugging-Face-Cache (Sperren, Metadaten) ebenfalls unter $PS_DATA/models; im Betrieb ist das ohnehin HF_HOME
    os.environ["HF_HOME"] = str(models)
    from faster_whisper import WhisperModel  # schwerer Import erst nach HF_HOME
    people = []
    for t in m.get("tracks") or []:
        pid = t.get("participant_id") or t["id"]
        if pid not in [p[0] for p in people]:
            people.append((pid, t.get("participant_name") or "Unbekannt"))
    stems = [(pid, name, work / "stems" / f"{pid}.flac") for pid, name in people]
    stems = [s for s in stems if s[2].exists()]
    if not stems:
        raise PipelineError("Es gibt keine bearbeiteten Stimmen; die Verarbeitung muss zuerst laufen")

    prog(0.0, f"Sprachmodell {model_name} wird geladen")
    try:
        model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=THREADS, num_workers=1,
                             download_root=str(models))
    except Exception as exc:  # noqa: BLE001
        raise PipelineError(f"Sprachmodell {model_name} konnte nicht geladen werden ({exc})") from exc

    segments = []
    for i, (pid, name, path) in enumerate(stems):
        p = prog.sub(0.05 + 0.9 * i / len(stems), 0.05 + 0.9 * (i + 1) / len(stems))
        p(0.0, f"Transkript für {name}")
        audio = _load_16k(path)
        dur = len(audio) / 16000
        speech = read_json(path.with_name(f"{pid}.speech.json"))
        spans = speech.get("speech_ms") if isinstance(speech, dict) else None
        if isinstance(spans, list):
            audio *= _mask_16k(spans, len(audio))
            regions = _regions(spans, len(audio) // 16)
        else:
            regions = [[0, len(audio) // 16]]
        # je Redebeitrag einzeln: Segmente reichen so nie über einen Sprecherwechsel hinweg
        for r0, r1 in regions:
            seg_audio = audio[r0 * 16:r1 * 16]
            if len(seg_audio) < 16000 * 0.3:
                continue
            segs, _info = model.transcribe(seg_audio, language="de", vad_filter=True,
                                           vad_parameters={"min_silence_duration_ms": 500}, beam_size=5,
                                           condition_on_previous_text=False)
            for sg in segs:
                text = sg.text.strip()
                if text:
                    segments.append({"start_ms": r0 + int(round(sg.start * 1000)),
                                     "end_ms": min(r1, r0 + int(round(sg.end * 1000))),
                                     "speaker": name, "participant_id": pid, "text": text})
            p(min(1.0, r1 / 1000 / max(dur, 1e-3)), f"Transkript für {name}")
        del audio
    segments.sort(key=lambda s: (s["start_ms"], s["end_ms"]))
    write_json(out_dir / "transcript.json", {"segments": segments, "model": model_name, "language": "de"})

    edl = m.get("edl") or {}
    report = read_json(work / "report.json", {}) or {}
    session_ms = report.get("session_duration_ms")
    lines = ["WEBVTT", ""]
    n = 0
    for s in segments:
        a = timeline.snap_to_episode(s["start_ms"], edl, session_ms)
        b = timeline.snap_to_episode(s["end_ms"], edl, session_ms)
        if a is None:
            continue
        if b is None:
            b = timeline.episode_duration_ms(edl, session_ms) if session_ms else a
        if b - a < 200:  # ganz herausgeschnitten
            continue
        n += 1
        lines += [str(n), f"{_ts(a)} --> {_ts(b)}", f"<v {s['speaker']}>{s['text']}", ""]
    tmp = out_dir / "transcript.vtt.tmp"
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(out_dir / "transcript.vtt")
    prog(1.0, "Transkript fertig")
    return {"segments": len(segments)}
