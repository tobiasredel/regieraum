"""Klangfarbe und Pausen (pipeline/tone.py)."""
import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt

from pipeline import tone
from pipeline.dsp import SR


def _rms_db(x):
    return 20 * np.log10(np.sqrt(np.mean(np.square(x))) + 1e-12)


DEV = {100: -8, 125: -8, 160: -8, 200: -8, 400: 5, 500: 5, 630: 5}  # Laptop: wenig Fülle, Topf um 500 Hz


def _laptop_speech(seconds, seed=1):
    """Sprachähnliches Signal: Zielform je Terzband plus typische Laptop-Abweichung, mit Silbenrhythmus."""
    rng = np.random.default_rng(seed)
    x = np.zeros(seconds * SR)
    for c, g in zip(tone.CENTERS, tone.TARGET):
        lo, hi = c / 2 ** (1 / 6), c * 2 ** (1 / 6)
        band = sosfilt(butter(4, [lo, hi], "bandpass", fs=SR, output="sos"), rng.standard_normal(seconds * SR))
        x += band * 10 ** ((g + DEV.get(c, 0)) / 20)
    env = (np.sin(np.linspace(0, seconds * 2 * np.pi * 3, seconds * SR)) > -0.3).astype(float)  # Silben
    x *= env
    return 0.1 * x / np.sqrt(np.mean(x ** 2))


def test_correction_shape_limits_and_no_level_change():
    flat = np.array(tone.TARGET) - 20  # Stem mit genau der Zielform, nur leiser: keine Korrektur
    assert all(abs(v) < 0.01 for v in tone.correction(flat - np.mean(flat)))
    extreme = np.zeros(len(tone.CENTERS))
    extreme[:5] = -40  # kaum Tiefen
    c = tone.correction(extreme - extreme.mean())
    assert max(c) <= tone.MAX_DB and min(c) >= -tone.MAX_DB and np.median(c) == 0
    assert c[0] == tone.MAX_DB  # Tiefen werden angehoben, gedeckelt


def test_measure_and_apply(tmp_path):
    speech = _laptop_speech(40)
    noise = 10 ** (-60 / 20) * np.random.default_rng(2).standard_normal(20 * SR)  # Lüfter, -60 dBFS
    x = np.concatenate([noise[: 10 * SR], speech, noise[10 * SR:]]) + 10 ** (-60 / 20) * np.random.default_rng(3).standard_normal(60 * SR)
    path = tmp_path / "stem.flac"
    sf.write(path, x.astype(np.float32), SR, subtype="PCM_24")
    speech_ms = [[10_000, 50_000]]

    m = tone.measure(path, speech_ms)
    assert -62 < m["floor_db"] < -55 and m["gate_db"] == round(m["floor_db"] + 10, 1)
    low = [g for c, g in zip(tone.CENTERS, m["curve"]) if c <= 200]
    mid = [g for c, g in zip(tone.CENTERS, m["curve"]) if 400 <= c <= 630]
    assert min(low) > 2 and max(mid) < -1  # mehr Fülle, weniger Topf

    before_speech = _rms_db(x[11 * SR:49 * SR])
    info = tone.apply(path, speech_ms, tmp_path / "tmp")
    assert info["applied"]
    y, sr = sf.read(path)
    assert sr == SR and len(y) == len(x)
    # keine Verschiebung: der Filter verzögert intern, das wird abgeschnitten (sonst liefen die Stimmen auseinander)
    seg_x, seg_y = x[20 * SR:22 * SR], y[20 * SR:22 * SR]
    lags = range(-200, 201)
    corr = [np.dot(seg_x[200:-200], np.roll(seg_y, -k)[200:-200]) for k in lags]
    assert lags[int(np.argmax(corr))] == 0
    assert abs(_rms_db(y[11 * SR:49 * SR]) - before_speech) < 0.5  # Sprachpegel bleibt
    assert _rms_db(y[2 * SR:8 * SR]) < _rms_db(x[2 * SR:8 * SR]) - 8  # Pausen deutlich leiser
    assert np.abs(y).max() <= 10 ** (-1 / 20) + 1e-3


def test_without_pauses_no_gate_without_speech_no_curve(tmp_path):
    path = tmp_path / "s.flac"
    sf.write(path, _laptop_speech(20).astype(np.float32), SR, subtype="PCM_24")
    m = tone.measure(path, [[0, 20_000]])
    assert m["gate_db"] is None and m["curve"] is not None
    m = tone.measure(path, [])
    assert m["curve"] is None
