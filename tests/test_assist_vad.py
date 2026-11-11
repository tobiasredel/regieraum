"""VAD und Äußerungsschnitt an synthetischem Signal."""
from __future__ import annotations

import numpy as np

from assist.vad import Resampler, UtteranceCutter, decode_pcm
from tests.assist_fixtures.fakes import SR, chunks, synth, to_s24le


def run(x, *, start_ms=0.0, fmt="s24le", chunk_s=2.0, **kw):
    cut = UtteranceCutter("t1", "Tobias", SR, fmt, start_ms, **kw)
    out = []
    for i, c in chunks(x, chunk_s):
        data = to_s24le(c) if fmt == "s24le" else c.astype("<f4").tobytes()
        out += cut.push(i, data)
    out += cut.flush()
    return out


def test_decode_formats_roundtrip():
    x = np.array([0.0, 0.5, -0.5, 0.999, -1.0], dtype=np.float32)
    assert np.allclose(decode_pcm(to_s24le(x), "s24le"), x, atol=1e-6)
    assert np.allclose(decode_pcm((x * 32767).astype("<i2").tobytes(), "s16le"), x, atol=1e-4)
    assert np.allclose(decode_pcm(x.astype("<f4").tobytes(), "f32le"), x)


def test_resampler_is_seamless_across_chunks():
    t = np.arange(SR * 2) / SR
    x = np.sin(2 * np.pi * 440 * t)
    whole = Resampler(SR).process(x)
    r = Resampler(SR)
    parts = np.concatenate([r.process(x[i : i + 7001]) for i in range(0, len(x), 7001)])
    assert len(parts) == len(whole) == 32000
    assert np.allclose(parts, whole, atol=1e-6)


def test_utterance_times_in_session_time():
    x = synth(10, [(1.0, 2.5), (4.0, 5.2)])
    utts = run(x, start_ms=5000.0)
    assert len(utts) == 2
    a, b = utts
    # Vorlauf 200 ms, Abschluss nach Pause mit 120 ms Nachlauf; Sitzungszeit = Spurstart + Position
    assert 5750 <= a.start_ms <= 6000, a.start_ms
    assert 7450 <= a.end_ms <= 7700, a.end_ms
    assert 8750 <= b.start_ms <= 9000
    assert 10150 <= b.end_ms <= 10400
    assert a.track_id == "t1" and a.speaker == "Tobias"
    assert abs(len(a.audio) / 16000 * 1000 - a.duration_ms) < 25
    assert a.snr_db > 20


def test_short_pauses_do_not_split_but_600ms_do():
    x = synth(8, [(1.0, 2.0), (2.3, 3.0), (4.0, 5.0)])  # 300 ms Lücke verbindet, 1 s Lücke trennt
    utts = run(x)
    assert len(utts) == 2
    assert utts[0].end_ms > 2900


def test_long_speech_is_cut_at_12_seconds():
    # 18 s Rede am Stück mit kurzen Atempausen (300 ms, trennen nicht)
    x = synth(20, [(0.5, 4.5), (4.8, 8.8), (9.1, 13.1), (13.4, 18.5)])
    utts = run(x)
    assert len(utts) >= 2
    assert all(u.duration_ms <= 12000 + 40 for u in utts)
    # lückenlos aneinander
    assert abs(utts[1].start_ms - utts[0].end_ms) < 30


def test_very_quiet_raw_microphone_is_detected():
    x = synth(6, [(1.0, 3.0)], speech_db=-56, noise_db=-80)
    utts = run(x)
    assert len(utts) == 1
    assert 800 <= utts[0].start_ms <= 1000


def test_stationary_noise_alone_gives_nothing():
    x = synth(10, [], noise_db=-40)
    assert run(x) == []


def test_noise_floor_adapts_to_louder_room():
    # Lüfter geht an: Rauschen steigt um 20 dB, danach Sprache darüber
    x = synth(14, [(10.0, 12.0)], speech_db=-25, noise_db=-70)
    rng = np.random.default_rng(3)
    x[3 * SR :] += rng.normal(0, 10 ** (-50 / 20), len(x) - 3 * SR).astype(np.float32)
    utts = run(x)
    assert len(utts) == 1
    assert 9700 <= utts[0].start_ms <= 10000


def test_duplicate_chunks_and_gaps():
    x = synth(8, [(1.0, 3.5)])
    cut = UtteranceCutter("t1", "Max", SR, "f32le", 0.0)
    out = []
    parts = list(chunks(x))
    for i, c in parts[:1]:
        out += cut.push(i, c.astype("<f4").tobytes())
    for i, c in parts[:2]:  # erster doppelt, zweiter neu
        out += cut.push(i, c.astype("<f4").tobytes())
    # Chunk 3 fehlt (Lücke): laufende Äußerung wird abgeschlossen
    out += cut.push(parts[3][0], parts[3][1].astype("<f4").tobytes())
    assert len(out) == 1
    assert out[0].end_ms <= 4000 + 50
    assert out[0].start_ms < 1000


def test_other_sample_rate():
    sr = 44100
    t = synth(6, [(1.0, 3.0)], sr=sr)
    cut = UtteranceCutter("t", "Max", sr, "f32le", 0.0)
    out = []
    for i in range(0, len(t), sr * 2):
        out += cut.push(i, t[i : i + sr * 2].astype("<f4").tobytes())
    out += cut.flush()
    assert len(out) == 1
    assert 800 <= out[0].start_ms <= 1000
    assert 3000 <= out[0].end_ms <= 3250
