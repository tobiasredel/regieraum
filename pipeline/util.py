"""Gemeinsame Hilfen: Fehler, Fortschritt, ffmpeg, Dateien."""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

THREADS = max(1, min(4, int(os.environ.get("PS_THREADS", "4") or 4)))


class PipelineError(Exception):
    """Fehler mit einer Meldung für Menschen (deutsch)."""


# ---------------------------------------------------------------- Fortschritt

class Progress:
    """Schreibt JSON-Zeilen `{"progress": p, "message": m}` auf stdout.

    `sub(a, b)` liefert einen Teil-Fortschritt, der 0..1 auf a..b des Elternteils abbildet. Ausgaben werden gedrosselt
    (höchstens alle 0,5 s, außer bei neuer Meldung) und sind monoton.
    """

    def __init__(self, stream=None, *, _root: "Progress | None" = None, a: float = 0.0, b: float = 1.0):
        self._root = _root or self
        self.a, self.b = a, b
        if _root is None:
            self.stream = stream if stream is not None else sys.stdout
            self.last_p = -1.0
            self.last_t = 0.0
            self.last_msg = ""

    def sub(self, a: float, b: float) -> "Progress":
        return Progress(_root=self._root, a=self.a + (self.b - self.a) * a, b=self.a + (self.b - self.a) * b)

    def __call__(self, frac: float, message: str | None = None) -> None:
        r = self._root
        p = self.a + (self.b - self.a) * min(1.0, max(0.0, frac))
        p = max(p, r.last_p)
        msg = message if message is not None else r.last_msg
        now = time.monotonic()
        if msg == r.last_msg and now - r.last_t < 0.5 and p < 1.0:
            r.last_p = p
            return
        r.last_p, r.last_t, r.last_msg = p, now, msg
        r.stream.write(json.dumps({"progress": round(p, 4), "message": msg}, ensure_ascii=False) + "\n")
        r.stream.flush()


class Warnings(list):
    """Warnungen in deutschem Klartext; doppelte werden zusammengefasst."""

    def add(self, text: str) -> None:
        if text not in self:
            self.append(text)
            print(f"Warnung: {text}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- Zahlen

def db(x: float, floor: float = -200.0) -> float:
    return 10 * math.log10(x) if x > 0 else floor


def db_arr(x: np.ndarray, floor: float = 1e-20) -> np.ndarray:
    return 10.0 * np.log10(np.maximum(x, floor))


def r1(x: float | None, nd: int = 1):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return None
    return round(float(x), nd)


# ---------------------------------------------------------------- Dateien

def write_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------- ffmpeg

def ffmpeg(args: list[str], what: str, *, stdin=None, stdout=None, timeout: float | None = None) -> subprocess.CompletedProcess:
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-threads", str(THREADS), *args]
    try:
        r = subprocess.run(cmd, stdin=stdin, stdout=stdout if stdout is not None else subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(f"{what}: ffmpeg hat zu lange gebraucht") from exc
    if r.returncode != 0:
        tail = r.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        raise PipelineError(f"{what} fehlgeschlagen ({' / '.join(tail) or 'ffmpeg-Fehler'})")
    return r


def decode_audio(path: Path, channels: int = 2, sr: int = 48000, max_seconds: float | None = None) -> np.ndarray:
    """Beliebige Audiodatei → float32 (frames, channels) bei `sr`, hochwertig resampelt (soxr)."""
    args = ["-i", str(path), "-vn", "-map", "0:a:0"]
    if max_seconds:
        args += ["-t", str(max_seconds)]
    args += ["-af", "aresample=resampler=soxr:precision=28", "-ar", str(sr), "-ac", str(channels),
             "-f", "f32le", "-c:a", "pcm_f32le", "pipe:1"]
    r = ffmpeg(args, "Audiodatei lesen", stdout=subprocess.PIPE)
    data = np.frombuffer(r.stdout, dtype="<f4")
    return data.reshape(-1, channels).copy()


def ebur128(path: Path) -> dict:
    """Unabhängige Messung mit ffmpeg (ebur128, True Peak): {"lufs", "true_peak_db", "lra"}."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-threads", str(THREADS), "-i", str(path),
           "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    text = r.stderr.decode("utf-8", "replace")
    if r.returncode != 0:
        raise PipelineError("Lautheitsmessung mit ffmpeg fehlgeschlagen")
    summary = text[text.rfind("Summary:"):]
    out = {"lufs": None, "true_peak_db": None, "lra": None}
    section = None
    for line in summary.splitlines():
        s = line.strip()
        if s.startswith("Integrated loudness"):
            section = "I"
        elif s.startswith("Loudness range"):
            section = "LRA"
        elif s.startswith("True peak"):
            section = "TP"
        elif s.startswith("I:") and section == "I":
            out["lufs"] = _num(s)
        elif s.startswith("LRA:") and section == "LRA":
            out["lra"] = _num(s)
        elif s.startswith("Peak:") and section == "TP":
            out["true_peak_db"] = _num(s)
    return out


def _num(s: str) -> float | None:
    try:
        v = float(s.split(":", 1)[1].split()[0])
    except (ValueError, IndexError):
        return None
    return v if math.isfinite(v) else None
