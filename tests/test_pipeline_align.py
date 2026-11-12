"""Stufe 1: Zusammensetzen und Ausrichten einer langen Sitzung mit Drift, Neuverbindung und fehlendem Chunk.

Rauschunterdrückung und Crossgate sind aus, damit nur die Lage gemessen wird.
"""
import json

import numpy as np
import pytest
import soundfile as sf

from pipeline import align
from pipeline.util import Warnings
from tests.pipeline_fixtures import REC_START, SR, Session, Speaker, find_clicks, run_cli

DUR = 600.0
CLICKS = [5.0 + 30.0 * k for k in range(20)]          # alle 30 s ein Klick, von beiden Mikrofonen aufgenommen
SPLIT = (300.0, 300.8)                                 # Neuverbindung: Spur 1 bis 300 s, Spur 2 ab 300,8 s
MISSING_SEQ = 30                                       # fehlender Chunk bei Max (Sample 120 s … 124 s seiner Spur)


def _turns(offset):
    return [(t + offset, t + offset + 9.0) for t in np.arange(8.0, DUR - 12, 30.0)]


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    root = tmp_path_factory.mktemp("align")
    clicks = [(t, 0.5) for t in CLICKS]
    a = Speaker("pA", "Tobias", "a", 0.0, _turns(0.0), ppm=40.0, noise_db=-60, clicks=clicks,
                tracks=[(-2.0, SPLIT[0]), (SPLIT[1], DUR + 1.0)])
    b = Speaker("pB", "Max", "b", -12.0, _turns(13.0), ppm=-25.0, noise_db=-64, clicks=clicks,
                missing_chunks=[(0, MISSING_SEQ)], start_error_ms=-70.0)
    s = Session(root, DUR, [a, b], audio={"denoise": "off", "crossgate": False})
    manifest, truth = s.build()
    code, lines, err = run_cli("stage1", str(manifest))
    assert code == 0, err
    m = json.loads(manifest.read_text())
    report = json.loads((root / "episode" / "work" / "report.json").read_text())
    return {"root": root, "manifest": m, "report": report, "lines": lines, "truth": truth}


def _stem(session, pid):
    x, sr = sf.read(str(session["root"] / "episode" / "work" / "stems" / f"{pid}.flac"), dtype="float32")
    assert sr == SR
    return x


def test_stems_exist_with_session_length(session):
    for pid in ("pA", "pB"):
        info = sf.info(str(session["root"] / "episode" / "work" / "stems" / f"{pid}.flac"))
        assert info.samplerate == SR and info.channels == 1
        assert info.frames == int(DUR * SR)


def test_markers_aligned_within_5ms(session):
    a, b = _stem(session, "pA"), _stem(session, "pB")
    # Klicks direkt an der Neuverbindung liegen in der Lücke
    expect = [t for t in CLICKS if not (SPLIT[0] - 0.1 < t < SPLIT[1] + 0.1)]
    ta = np.array(find_clicks(a, expect))
    tb = np.array(find_clicks(b, expect))
    e = np.array(expect)
    err_ab = np.abs(ta - tb) * 1000
    err_a = np.abs(ta - e) * 1000
    err_b = np.abs(tb - e) * 1000
    print("Abweichung A-B ms:", np.round(err_ab, 2))
    print("A gegen Wahrheit:", np.round(err_a, 2), "B gegen Wahrheit:", np.round(err_b, 2))
    assert err_ab.max() <= 5.0
    assert err_a.max() <= 5.0 and err_b.max() <= 5.0
    # auch am Ende der langen Sitzung (ohne Korrektur wären es 0,6 ms je 10 s Drift-Differenz, hier ~39 ms)
    assert err_ab[-1] <= 5.0


def test_drift_and_offset_estimated(session):
    tr = {t["track_id"]: t for t in session["report"]["tracks"]}
    assert tr["pB_t0"]["drift_ppm"] == pytest.approx(-25.0, abs=8.0)
    assert tr["pA_t1"]["drift_ppm"] == pytest.approx(40.0, abs=15.0)
    # Startversatz relativ zu rec.start (Spur lief 2 s vorher los); Client-Schätzung lag 70 ms daneben
    assert tr["pB_t0"]["start_offset_ms"] == pytest.approx(-2000.0, abs=5.0)
    assert tr["pB_t0"]["start_estimate_error_ms"] == pytest.approx(70.0, abs=6.0)


def test_gap_filled_with_silence_and_reported(session):
    b = _stem(session, "pB")
    gaps = {t["track_id"]: t["gaps"] for t in session["report"]["tracks"]}["pB_t0"]
    assert len(gaps) == 1
    # Chunk 30 deckt Samples 120…124 s seiner Spur ab, die Spur begann 2 s vor rec.start
    g0, g1 = gaps[0]["from_ms"], gaps[0]["to_ms"]
    assert g0 == pytest.approx(118000, abs=20) and g1 == pytest.approx(122000, abs=20)
    inner = b[int((g0 + 50) * SR / 1000):int((g1 - 50) * SR / 1000)]
    assert np.max(np.abs(inner)) < 1e-5  # Stille; der Equalizer der Klangfarbe hinterlässt Reste um -117 dBFS
    assert any("Lücke in der Spur von Max" in w for w in session["report"]["warnings"])


def test_reconnect_gap_and_chunks_deleted(session):
    a = _stem(session, "pA")
    gap = a[int((SPLIT[0] + 0.05) * SR):int((SPLIT[1] - 0.05) * SR)]
    assert np.max(np.abs(gap)) < 1e-6
    assert any("Neuverbindung" in w for w in session["report"]["warnings"])
    for t in session["manifest"]["tracks"]:
        d = session["root"] / "episode" / "tracks" / t["id"]
        assert (d / "raw.flac").exists()
        assert not (d / "chunks").exists() or not any((d / "chunks").iterdir())


def test_progress_protocol(session):
    ps = [ln["progress"] for ln in session["lines"]]
    assert ps == sorted(ps) and ps[-1] == 1.0
    assert all(isinstance(ln.get("message"), str) and ln["message"] for ln in session["lines"])


def test_fit_clock_without_points_uses_start_time():
    w = Warnings()
    t = {"participant_name": "Gast", "sample_rate": 48000, "start_server_ms": REC_START + 1234, "clock_points": []}
    c = align.fit_clock(t, REC_START, w)
    assert c["a_ms"] == pytest.approx(1234) and c["drift_ppm"] == 0
    assert c["method"] == "startzeit" and w


def test_fit_clock_rejects_outliers():
    w = Warnings()
    rate = 48000 * (1 + 60e-6)
    pts = [{"sample_index": k * 480000, "server_ms": REC_START + 500 + k * 480000 / rate * 1000} for k in range(60)]
    for i in (3, 17, 18, 40):
        pts[i]["server_ms"] += 120.0
    t = {"participant_name": "Max", "sample_rate": 48000, "start_server_ms": REC_START + 400, "clock_points": pts}
    c = align.fit_clock(t, REC_START, w)
    assert c["drift_ppm"] == pytest.approx(60.0, abs=1.0)
    assert c["a_ms"] == pytest.approx(500.0, abs=0.5)
    assert c["points_used"] == 56


def test_other_rates_and_formats(tmp_path):
    """44,1-kHz-Spur in f32le und 48-kHz-Spur in s16le, ohne Uhrenpunkte bei einer: Lage trotzdem richtig."""
    clicks = [(t, 0.5) for t in (3.0, 20.0, 37.0, 55.0)]
    turns = [(5.0, 15.0), (40.0, 50.0)]
    a = Speaker("pA", "Tobias", "a", 0.0, turns, ppm=30.0, clicks=clicks, sample_rate=44100, fmt="f32le")
    b = Speaker("pB", "Gast", "b", -6.0, [(22.0, 33.0)], ppm=-10.0, clicks=clicks, fmt="s16le",
                with_clock=False, start_error_ms=1.0)
    s = Session(tmp_path, 60.0, [a, b], audio={"denoise": "off", "crossgate": False})
    manifest, _ = s.build()
    code, _, err = run_cli("stage1", str(manifest))
    assert code == 0, err
    st = tmp_path / "episode" / "work" / "stems"
    xa, sra = sf.read(str(st / "pA.flac"), dtype="float32")
    xb, _ = sf.read(str(st / "pB.flac"), dtype="float32")
    assert sra == SR and len(xa) == len(xb) == 60 * SR
    exp = [t for t, _ in clicks]
    ta, tb = np.array(find_clicks(xa, exp)), np.array(find_clicks(xb, exp))
    print("44,1 kHz:", np.round((ta - exp) * 1000, 2), "ohne Uhrenpunkte:", np.round((tb - exp) * 1000, 2))
    assert np.max(np.abs(ta - exp)) * 1000 <= 5.0
    # ohne Uhrenpunkte zählt nur die Startzeit (1 ms daneben) und die Nennrate: −10 ppm über 60 s = 0,6 ms
    assert np.max(np.abs(tb - exp)) * 1000 <= 5.0
    rep = json.loads((tmp_path / "episode" / "work" / "report.json").read_text())
    assert any("keine Uhrenpunkte" in w for w in rep["warnings"])
