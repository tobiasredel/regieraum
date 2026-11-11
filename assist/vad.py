"""Pegel-VAD und Äußerungsschnitt je Spur.

Signalweg je Chunk: dekodieren (s24le/s16le/f32le, mono) → auf 16 kHz (zustandsbehaftet) → Hochpass 100 Hz →
20-ms-Rahmen → Pegel in dBFS. Sprache heißt: Pegel liegt deutlich über dem Grundrauschen der Spur (relative Schwelle).
Das Grundrauschen ist das 10-%-Perzentil der letzten 8 s, damit leise unbearbeitete Mikrofone und wechselnde
Raumgeräusche funktionieren; springt das Geräusch (eine Sekunde gleichmäßiger Pegel), folgt die Schätzung sofort. Eine Äußerung endet nach 600 ms ohne Sprache oder spätestens nach 12 s, dann an der
leisesten Stelle der letzten 1,5 s, damit Wörter möglichst nicht zerschnitten werden.

Zeiten in Sitzungszeit: ``track_start_session_ms + quell_frame / sample_rate * 1000``.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from math import gcd

import numpy as np
from scipy import signal

TARGET_SR = 16000
FRAME = 320  # 20 ms bei 16 kHz
FRAME_MS = 20


@dataclass
class Utterance:
    track_id: str
    speaker: str
    start_ms: float
    end_ms: float
    audio: np.ndarray  # float32, 16 kHz
    level_db: float  # mittlerer Pegel der Sprachrahmen
    snr_db: float  # Abstand zum Grundrauschen
    speech_ms: int
    created: float = field(default_factory=time.monotonic)
    frame_db: np.ndarray | None = None  # Pegel je 20-ms-Rahmen ab start_ms
    speech_mask: np.ndarray | None = None  # Rahmen über der Sprachschwelle

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms


def decode_pcm(data: bytes, fmt: str) -> np.ndarray:
    """Rohdaten mono → float32 in [-1, 1]."""
    if fmt == "s16le":
        n = len(data) // 2
        return np.frombuffer(data[: n * 2], dtype="<i2").astype(np.float32) / 32768.0
    if fmt == "f32le":
        n = len(data) // 4
        return np.frombuffer(data[: n * 4], dtype="<f4").astype(np.float32)
    if fmt == "s24le":
        n = len(data) // 3
        b = np.frombuffer(data[: n * 3], dtype=np.uint8).reshape(n, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v & 0x800000, v - 0x1000000, v)
        return v.astype(np.float32) / 8388608.0
    raise ValueError(f"unbekanntes Format {fmt}")


def bytes_per_frame(fmt: str) -> int:
    return {"s16le": 2, "s24le": 3, "f32le": 4}[fmt]


class Resampler:
    """Zustandsbehaftete Umrechnung auf 16 kHz. Ganzzahlige Verhältnisse über FIR + Dezimierung (nahtlos über
    Chunk-Grenzen), sonst ``resample_poly`` je Block."""

    def __init__(self, sr: int):
        self.sr = sr
        self.reset()

    def reset(self):
        sr = self.sr
        self.factor = sr // TARGET_SR if sr % TARGET_SR == 0 else None
        if self.factor and self.factor > 1:
            self.taps = signal.firwin(16 * self.factor + 1, 7200 / (sr / 2))
            self.zi = np.zeros(len(self.taps) - 1)
            self.phase = 0
        g = gcd(sr, TARGET_SR)
        self.up, self.down = TARGET_SR // g, sr // g

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.sr == TARGET_SR:
            return x
        if self.factor:
            y, self.zi = signal.lfilter(self.taps, 1.0, x, zi=self.zi)
            out = y[self.phase :: self.factor]
            self.phase = (self.phase - len(x)) % self.factor
            return out.astype(np.float32)
        return signal.resample_poly(x, self.up, self.down).astype(np.float32)


class UtteranceCutter:
    def __init__(self, track_id: str, speaker: str, sample_rate: int, fmt: str, track_start_session_ms: float, *,
                 end_silence_ms: int = 600, max_ms: int = 12000, min_speech_ms: int = 240, preroll_ms: int = 200,
                 tail_ms: int = 120, enter_db: float = 10.0, stay_db: float = 6.0, abs_min_db: float = -72.0,
                 floor_window_s: float = 8.0):
        self.track_id = track_id
        self.speaker = speaker
        self.sr = int(sample_rate)
        self.fmt = fmt
        self.track_start_ms = float(track_start_session_ms)
        self.end_frames = end_silence_ms // FRAME_MS
        self.max_frames = max_ms // FRAME_MS
        self.min_speech_frames = min_speech_ms // FRAME_MS
        self.preroll_frames = preroll_ms // FRAME_MS
        self.tail_frames = tail_ms // FRAME_MS
        self.enter_db, self.stay_db, self.abs_min_db = enter_db, stay_db, abs_min_db
        self.resampler = Resampler(self.sr)
        self.sos = signal.butter(2, 100, "highpass", fs=TARGET_SR, output="sos")
        self.hp_zi = signal.sosfilt_zi(self.sos) * 0.0
        self.levels: deque[float] = deque(maxlen=int(floor_window_s * 1000 / FRAME_MS))
        self.recent: deque[float] = deque(maxlen=50)
        self.hist_ms: deque[float] = deque(maxlen=1500)  # Rahmenpegel der letzten 30 s (Sitzungszeit)
        self.hist_db: deque[float] = deque(maxlen=1500)
        self.floor_db: float | None = None
        self.next_src_frame: int | None = None  # erwarteter nächster Quell-Frame
        self.base_src_frame = 0  # Quell-Frame zum 16-kHz-Index 0 seit dem letzten Reset
        self.n16 = 0  # 16-kHz-Samples seit Reset (bis Ende des Puffers)
        self.rest = np.zeros(0, dtype=np.float32)  # unvollständiger Rahmen
        self.ring: deque[tuple[np.ndarray, float]] = deque(maxlen=self.preroll_frames)
        self._reset_utt()

    # ------------------------------------------------------------------ Zeit
    def _ms_at16(self, idx16: float) -> float:
        src = self.base_src_frame + idx16 * self.sr / TARGET_SR
        return self.track_start_ms + src * 1000.0 / self.sr

    @property
    def active_since_ms(self) -> float | None:
        """Sitzungszeit des Beginns einer gerade laufenden Äußerung (für Übersprech-Abgleich)."""
        if not self.in_utt:
            return None
        return self._ms_at16(self.utt_start16)

    @property
    def position_ms(self) -> float:
        return self._ms_at16(self.n16)

    def _reset_utt(self):
        self.in_utt = False
        self.frames: list[np.ndarray] = []
        self.frame_db: list[float] = []
        self.frame_speech: list[bool] = []
        self.utt_start16 = 0
        self.silence_run = 0
        self.onset_run = 0

    # ------------------------------------------------------------------ Eingabe
    def push(self, first_frame: int, pcm: bytes) -> list[Utterance]:
        out: list[Utterance] = []
        bpf = bytes_per_frame(self.fmt)
        nframes = len(pcm) // bpf
        if nframes <= 0:
            return out
        if self.next_src_frame is None:
            self.next_src_frame = first_frame
            self.base_src_frame = first_frame
        if first_frame + nframes <= self.next_src_frame:
            return out  # doppelt oder veraltet
        if first_frame < self.next_src_frame:  # teilweise doppelt: Überlappung abschneiden
            skip = self.next_src_frame - first_frame
            pcm = pcm[skip * bpf :]
            first_frame = self.next_src_frame
            nframes -= skip
        elif first_frame > self.next_src_frame:  # Lücke: laufende Äußerung abschließen, Zustand zurücksetzen
            out += self.flush()
            self.resampler.reset()
            self.hp_zi = self.hp_zi * 0.0
            self.rest = np.zeros(0, dtype=np.float32)
            self.ring.clear()
            self.base_src_frame = first_frame
            self.n16 = 0
        self.next_src_frame = first_frame + nframes
        x = self.resampler.process(decode_pcm(pcm, self.fmt).astype(np.float64))
        x, self.hp_zi = signal.sosfilt(self.sos, x, zi=self.hp_zi)
        x = np.concatenate([self.rest, x.astype(np.float32)])
        n_full = len(x) // FRAME
        start16 = self.n16 - len(self.rest)
        for i in range(n_full):
            fr = x[i * FRAME : (i + 1) * FRAME]
            out += self._frame(fr, start16 + i * FRAME)
        self.rest = x[n_full * FRAME :]
        self.n16 = start16 + len(x)
        return out

    def _frame(self, fr: np.ndarray, idx16: int) -> list[Utterance]:
        db = float(10 * np.log10(np.mean(fr.astype(np.float64) ** 2) + 1e-12))
        self.levels.append(db)
        self.hist_ms.append(self._ms_at16(idx16))
        self.hist_db.append(db)
        self.recent.append(db)
        # Geräuschsprung (Lüfter, Klimaanlage): eine Sekunde lang gleichmäßiger Pegel ist keine Sprache,
        # das Grundrauschen springt sofort auf das neue Niveau
        if len(self.recent) == self.recent.maxlen and float(np.std(self.recent)) < 1.5:
            m = float(np.mean(self.recent))
            if self.floor_db is not None and m > self.floor_db + 3:
                self.levels = deque(self.recent, maxlen=self.levels.maxlen)
        floor = float(np.percentile(self.levels, 10)) if len(self.levels) >= 10 else min(self.levels)
        self.floor_db = floor
        thr = self.stay_db if self.in_utt else self.enter_db
        speech = db > floor + thr and db > self.abs_min_db
        out: list[Utterance] = []
        if not self.in_utt:
            self.onset_run = self.onset_run + 1 if speech else 0
            self.ring.append((fr, db))
            if self.onset_run >= 3:  # 60 ms Sprache am Stück: Beginn
                self.in_utt = True
                pre = list(self.ring)
                self.frames = [f for f, _ in pre]
                self.frame_db = [d for _, d in pre]
                n_on = min(3, len(pre))
                self.frame_speech = [False] * (len(pre) - n_on) + [True] * n_on
                self.utt_start16 = idx16 + FRAME - len(pre) * FRAME
                self.silence_run = 0
                self.ring.clear()
            return out
        self.frames.append(fr)
        self.frame_db.append(db)
        self.frame_speech.append(speech)
        self.silence_run = 0 if speech else self.silence_run + 1
        if self.silence_run >= self.end_frames:
            keep = len(self.frames) - self.silence_run + self.tail_frames
            u = self._emit(keep)
            self._reset_utt()
            if u:
                out.append(u)
        elif len(self.frames) >= self.max_frames:
            # an der leisesten Stelle der letzten 1,5 s schneiden, der Rest beginnt die nächste Äußerung
            win = min(len(self.frames) - 1, 75)
            dbs = self.frame_db[-win:]
            cut = len(self.frames) - win + int(np.argmin(dbs)) + 1
            rest_frames, rest_db, rest_sp = self.frames[cut:], self.frame_db[cut:], self.frame_speech[cut:]
            start_next = self.utt_start16 + cut * FRAME
            u = self._emit(cut)
            self._reset_utt()
            if u:
                out.append(u)
            self.in_utt = True
            self.frames, self.frame_db, self.frame_speech = rest_frames, rest_db, rest_sp
            self.utt_start16 = start_next
            self.silence_run = 0
        return out

    def _emit(self, n: int) -> Utterance | None:
        n = max(0, min(n, len(self.frames)))
        sp = [d for d, s in zip(self.frame_db[:n], self.frame_speech[:n]) if s]
        if len(sp) < self.min_speech_frames:
            return None
        # stationäres Geräusch (Lüfter springt an, Brummen) statt Sprache: Sprache schwankt im Pegel stark
        if len(sp) >= 25 and float(np.std(sp)) < 1.5:
            return None
        audio = np.concatenate(self.frames[:n]) if n else np.zeros(0, dtype=np.float32)
        lin = float(np.mean(np.power(10.0, np.array(sp) / 10.0)))
        level = 10 * np.log10(lin + 1e-12)
        start_ms = self._ms_at16(self.utt_start16)
        end_ms = self._ms_at16(self.utt_start16 + n * FRAME)
        return Utterance(self.track_id, self.speaker, start_ms, end_ms, audio.astype(np.float32), float(level),
                         float(level - (self.floor_db or -90.0)), len(sp) * FRAME_MS,
                         frame_db=np.array(self.frame_db[:n], dtype=np.float32),
                         speech_mask=np.array(self.frame_speech[:n], dtype=bool))

    def db_at(self, times_ms: np.ndarray) -> np.ndarray:
        """Rahmenpegel dieser Spur zu den angegebenen Sitzungszeiten (NaN, wo nichts vorliegt)."""
        out = np.full(len(times_ms), np.nan)
        if not self.hist_ms:
            return out
        hm = np.fromiter(self.hist_ms, float)
        hd = np.fromiter(self.hist_db, float)
        idx = np.clip(np.searchsorted(hm, times_ms), 1, len(hm) - 1) if len(hm) > 1 else np.zeros(len(times_ms), int)
        if len(hm) > 1:
            left = hm[idx - 1]
            choose = np.where(np.abs(times_ms - left) <= np.abs(times_ms - hm[idx]), idx - 1, idx)
        else:
            choose = idx
        ok = np.abs(hm[choose] - times_ms) <= FRAME_MS * 1.5
        out[ok] = hd[choose][ok]
        return out

    def flush(self) -> list[Utterance]:
        """Laufende Äußerung abschließen (Spurende, Lücke, Pause)."""
        if not self.in_utt:
            return []
        keep = len(self.frames) - max(0, self.silence_run - self.tail_frames)
        u = self._emit(keep)
        self._reset_utt()
        return [u] if u else []
