"""DSP-Bausteine: K-Filter und Lautheit (BS.1770-4), Fensterfilter, Hüllkurven-Rekursionen, Begrenzer.

Alles arbeitet blockweise, damit 90-Minuten-Dateien nicht ganz in den Speicher müssen.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.ndimage import maximum_filter1d, minimum_filter1d
from scipy.signal import butter, firwin, sosfilt

from . import SR

# K-Filter nach ITU-R BS.1770-4 für 48 kHz (Shelf + RLB-Hochpass)
K_SOS = np.array([
    [1.53512485958697, -2.69169618940638, 1.19839281085285, 1.0, -1.69065929318241, 0.73248077421585],
    [1.0, -2.0, 1.0, 1.0, -1.99004745483398, 0.99007225036621],
])


class SosStream:
    """sosfilt mit mitgeführtem Zustand über Blöcke (je Kanal)."""

    def __init__(self, sos: np.ndarray, channels: int = 1):
        self.sos = sos
        self.zi = np.zeros((sos.shape[0], channels, 2))

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x2 = x if x.ndim == 2 else x[:, None]
        y, zo = sosfilt(self.sos, x2, axis=0, zi=self.zi.transpose(0, 2, 1))
        self.zi = zo.transpose(0, 2, 1)
        return y if x.ndim == 2 else y[:, 0]


def highpass_sos(fc: float, order: int = 4) -> np.ndarray:
    return butter(order, fc, "highpass", fs=SR, output="sos")


def bandpass_sos(lo: float, hi: float, order: int = 2) -> np.ndarray:
    return butter(order, [lo, hi], "bandpass", fs=SR, output="sos")


def lowpass_sos(fc: float, order: int = 2) -> np.ndarray:
    return butter(order, fc, "lowpass", fs=SR, output="sos")


# ---------------------------------------------------------------- Lautheit

class LoudnessMeter:
    """Integrierte Lautheit nach BS.1770-4 (Gating -70 LUFS absolut, -10 LU relativ), blockweise gefüttert.

    Kanäle werden mit Gewicht 1 summiert (Mono-Signal auf L und R zählt also +3 dB gegenüber Mono allein)."""

    def __init__(self, channels: int):
        self.k = SosStream(K_SOS, channels)
        self.acc = 0.0
        self.n_acc = 0
        self.sub = []  # Energie je 100 ms (Summe über Kanäle)

    def feed(self, x: np.ndarray) -> None:
        x2 = x if x.ndim == 2 else x[:, None]
        y = self.k(x2.astype(np.float64))
        e = np.sum(y * y, axis=1)
        pos = 0
        step = SR // 10
        while pos < len(e):
            take = min(step - self.n_acc, len(e) - pos)
            self.acc += float(np.sum(e[pos:pos + take]))
            self.n_acc += take
            pos += take
            if self.n_acc == step:
                self.sub.append(self.acc / step)
                self.acc, self.n_acc = 0.0, 0

    def integrated(self) -> float:
        return integrated_from_100ms(np.asarray(self.sub))


def integrated_from_100ms(sub: np.ndarray) -> float:
    if len(sub) < 4:
        return -math.inf
    blocks = (sub[:-3] + sub[1:-2] + sub[2:-1] + sub[3:]) / 4.0  # 400 ms, 75 % Überlappung
    lk = -0.691 + 10 * np.log10(np.maximum(blocks, 1e-30))
    g = blocks[lk > -70.0]
    if len(g) == 0:
        return -math.inf
    rel = -0.691 + 10 * math.log10(float(np.mean(g))) - 10.0
    g2 = blocks[(lk > -70.0) & (lk > rel)]
    return -0.691 + 10 * math.log10(float(np.mean(g2))) if len(g2) else -math.inf


def loudness_of(x: np.ndarray) -> float:
    m = LoudnessMeter(1 if x.ndim == 1 else x.shape[1])
    m.feed(x)
    if m.n_acc:
        m.sub.append(m.acc / m.n_acc)
    return m.integrated()


# ---------------------------------------------------------------- True Peak

_OS_H = firwin(48, 0.235) * 4.0  # 4-fach-Interpolator (12 Taps je Phase), Grenzfrequenz knapp unter der alten Nyquist


_OS_PHASES = [(_OS_H[q::4][::-1]).astype(np.float64) for q in range(4)]


def peak_per_sample(x: np.ndarray, true_peak: bool, threshold: float = 0.0) -> np.ndarray:
    """Spitzenwert je Sample (Maximum über Kanäle); mit true_peak inklusive der Zwischenwerte (4-fach überabgetastet).

    Zwischenwerte werden nur dort berechnet, wo das Sample selbst über `threshold` liegt (Überschwinger zwischen
    Samples sind bei bandbegrenztem Material höchstens einige dB), das spart den Großteil der Rechenzeit. Am Blockrand
    fehlen 6 Samples Kontext; Aufrufer lesen deshalb einen Rand mit.
    """
    x2 = x if x.ndim == 2 else x[:, None]
    pk = np.max(np.abs(x2), axis=1)
    if not true_peak or len(x2) < 16:
        return pk
    n = len(x2)
    cand = np.flatnonzero(pk > threshold)
    cand = cand[(cand >= 5) & (cand < n - 7)]
    if len(cand) == 0:
        return pk
    # Zwischenwerte zwischen n und n+1 brauchen auch n-1 als Kandidat (Überschwinger vor einer Spitze)
    cand = np.unique(np.concatenate([cand - 1, cand]))
    cand = cand[(cand >= 5) & (cand < n - 7)]
    idx = cand[:, None] + np.arange(-5, 7)[None, :]
    out = pk.copy()
    for c in range(x2.shape[1]):
        w = x2[:, c].astype(np.float64)[idx]
        best = np.zeros(len(cand))
        for ph in _OS_PHASES:
            best = np.maximum(best, np.abs(w @ ph))
        out[cand] = np.maximum(out[cand], best)
    return out


# ---------------------------------------------------------------- Fensterfilter

def window_max(x: np.ndarray, before: int, after: int) -> np.ndarray:
    """y[n] = max(x[n-before .. n+after])."""
    size = before + after + 1
    return maximum_filter1d(x, size=size, origin=(size - 1) // 2 - after, mode="nearest")


def window_min(x: np.ndarray, before: int, after: int) -> np.ndarray:
    size = before + after + 1
    return minimum_filter1d(x, size=size, origin=(size - 1) // 2 - after, mode="nearest")


def moving_mean(x: np.ndarray, before: int, after: int) -> np.ndarray:
    """Gleitender Mittelwert über [n-before, n+after] (Ränder mit Randwert fortgesetzt)."""
    xp = np.concatenate([np.full(before, x[0]), x, np.full(after, x[-1])]).astype(np.float64)
    c = np.concatenate([[0.0], np.cumsum(xp)])
    size = before + after + 1
    return (c[size:] - c[:-size]) / size


def release_min(h: np.ndarray, rate: float, y0: float = 0.0) -> np.ndarray:
    """y[n] = min(h[n], y[n-1] + rate): sofort nach unten, mit `rate` (dB je Schritt) zurück. Vektorisiert."""
    j = np.arange(len(h), dtype=np.float64)
    return rate * j + np.minimum(y0 + rate, np.minimum.accumulate(h - rate * j))


def fall_max(t: np.ndarray, rate: float, y0: float = 0.0) -> np.ndarray:
    """y[n] = max(t[n], y[n-1] - rate): sofort nach oben, mit `rate` (dB je Schritt) nach unten."""
    j = np.arange(len(t), dtype=np.float64)
    return -rate * j + np.maximum(y0 - rate, np.maximum.accumulate(t + rate * j))


# ---------------------------------------------------------------- Begrenzer

def limit_file(src: Path, dst: Path, *, gain_db: float = 0.0, ceiling_db: float = -1.0, true_peak: bool = True,
               subtype: str = "FLOAT", fmt: str | None = None, lookahead_ms: float = 5.0, release_db_s: float = 40.0,
               meter: LoudnessMeter | None = None, progress=None, block: int = SR * 10) -> dict:
    """Verstärkung plus Begrenzer mit Vorausschau, Datei → Datei.

    Verstärkungsziel je Sample: gt = min(0, Decke − Spitze). Daraus Minimum über [n-2, n+L] (Vorausschau), dann
    Rückkehr mit fester Rate in dB (sofort runter, langsam hoch), dann Rechteck-Glättung über L+1 Samples. Weil jedes
    Glied der Mittelung ≤ gt[n] ist, überschreitet das Ergebnis die Decke nie (bis auf die Genauigkeit der
    Zwischenwert-Schätzung), und die Glättung verhindert Knackser. Vektorisiert mit Rekursion über cummin.
    """
    L = max(1, int(round(SR * lookahead_ms / 1000)))
    rate = release_db_s / SR
    ctx = 32
    g_lin = 10 ** (gain_db / 20)
    stats = {"limited_samples": 0, "max_reduction_db": 0.0}
    with sf.SoundFile(str(src)) as f:
        n, ch, sr = f.frames, f.channels, f.samplerate
        kw = {"format": fmt} if fmt else {}
        with sf.SoundFile(str(dst), "w", sr, ch, subtype, **kw) as g:
            y_hist = np.zeros(L)
            y_prev = 0.0
            for s in range(0, n, block):
                e = min(n, s + block)
                r0, r1 = max(0, s - ctx), min(n, e + L + ctx)
                f.seek(r0)
                x = f.read(r1 - r0, dtype="float32", always_2d=True) * np.float32(g_lin)
                pk = peak_per_sample(x, true_peak, threshold=10 ** ((ceiling_db - 4.0) / 20))
                gt = np.minimum(0.0, ceiling_db - 20 * np.log10(np.maximum(pk, 1e-9)))
                off = s - r0
                h = window_min(gt, 2, L)[off:off + (e - s)]
                y = release_min(h, rate, y_prev)
                y_prev = float(y[-1])
                yy = np.concatenate([y_hist, y])
                c = np.concatenate([[0.0], np.cumsum(yy)])
                gsm = (c[L + 1:] - c[:-(L + 1)]) / (L + 1)
                y_hist = yy[-L:]
                out = x[off:off + (e - s)] * (10 ** (gsm / 20)).astype(np.float32)[:, None]
                if meter is not None:
                    meter.feed(out)
                red = gsm < -0.01
                stats["limited_samples"] += int(np.count_nonzero(red))
                if red.any():
                    stats["max_reduction_db"] = min(stats["max_reduction_db"], float(gsm.min()))
                g.write(out if ch > 1 else out[:, 0])
                if progress:
                    progress(e / n)
    stats["max_reduction_db"] = round(-stats["max_reduction_db"], 2)
    return stats
