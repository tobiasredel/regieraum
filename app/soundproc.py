"""Jingle-Verarbeitung: `python -m pipeline sound`, Ausweich ffmpeg (playback.wav 48 kHz stereo s16 + meta.json)."""
from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

from . import util

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_EXT = {"wav", "mp3", "m4a", "aac", "ogg", "oga", "opus", "flac", "aif", "aiff", "wma"}


class SoundError(Exception):
    pass


def _via_pipeline(src: Path, out: Path) -> bool:
    try:
        r = subprocess.run([sys.executable, "-m", "pipeline", "sound", str(src), str(out)], cwd=ROOT,
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and (out / "playback.wav").exists() and (out / "meta.json").exists()


def _via_ffmpeg(src: Path, out: Path) -> None:
    wav = out / "playback.wav"
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src), "-vn", "-ar", "48000", "-ac", "2",
                            "-c:a", "pcm_s16le", str(wav)], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SoundError("Die Audiodatei konnte nicht verarbeitet werden") from exc
    if r.returncode != 0 or not wav.exists():
        wav.unlink(missing_ok=True)
        raise SoundError("Die Datei lässt sich nicht als Audio lesen")
    with wave.open(str(wav)) as w:
        duration_ms = round(w.getnframes() * 1000 / w.getframerate())
    (out / "meta.json").write_text(util.dumps({"duration_ms": duration_ms, "lufs": None, "peak_db": None}))


def process_sound(src: Path, out: Path) -> int:
    """Erzeugt playback.wav und meta.json in `out`, liefert duration_ms."""
    if not _via_pipeline(src, out):
        _via_ffmpeg(src, out)
    meta = util.loads((out / "meta.json").read_text(), {}) or {}
    duration = int(meta.get("duration_ms") or 0)
    if duration <= 0:
        raise SoundError("Die Datei enthält kein hörbares Audio")
    return duration
