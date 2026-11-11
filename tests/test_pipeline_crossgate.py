"""Stufe 1: Pegelangleich und Crossgate mit echter Sprache, 12 dB Pegelunterschied und Übersprechen.

Max sitzt ohne Kopfhörer: Tobias' Stimme kommt mit −20 dB und 150 ms Verzögerung in Max' Spur. Es gibt eine Stelle mit
gleichzeitigem Sprechen. Gemessen wird gegen denselben Lauf ohne Crossgate (gleiche Kette, nur das Gate fehlt).
"""
import json
import shutil

import numpy as np
import pyloudnorm as pyln
import pytest
import soundfile as sf

from tests.pipeline_fixtures import SR, Session, Speaker, intervals_minus, rms_db, run_cli, take

DUR = 90.0
TURNS_A = [(1.0, 12.0), (24.0, 35.0), (47.0, 60.0), (72.0, 84.0)]
TURNS_B = [(12.5, 23.5), (35.5, 46.5), (55.0, 71.5), (84.5, 89.0)]   # 55–60 s: beide reden


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    root = tmp_path_factory.mktemp("crossgate")
    a = Speaker("pA", "Tobias", "a", 0.0, TURNS_A, ppm=30.0, noise_db=-65)
    b = Speaker("pB", "Max", "b", -12.0, TURNS_B, ppm=-20.0, noise_db=-68, bleed_from="pA", bleed_db=-20.0,
                bleed_delay_ms=150.0)
    s = Session(root, DUR, [a, b], audio={"denoise": "off", "crossgate": False})
    manifest, _ = s.build()
    out = {}
    for gate in (False, True):
        m = json.loads(manifest.read_text())
        m["audio"]["crossgate"] = gate
        manifest.write_text(json.dumps(m))
        code, _, err = run_cli("stage1", str(manifest))
        assert code == 0, err
        stems = root / "episode" / "work" / "stems"
        out[gate] = {pid: sf.read(str(stems / f"{pid}.flac"), dtype="float32")[0] for pid in ("pA", "pB")}
        out[gate]["report"] = json.loads((root / "episode" / "work" / "report.json").read_text())
        shutil.copytree(stems, root / f"stems_{gate}")
    # derselbe Lauf mit Rauschunterdrückung „normal“ für den Pegeltest über die ganze Kette
    m = json.loads(manifest.read_text())
    m["audio"].update(crossgate=True, denoise="normal")
    manifest.write_text(json.dumps(m))
    code, _, err = run_cli("stage1", str(manifest))
    assert code == 0, err
    stems = root / "episode" / "work" / "stems"
    out["full"] = {pid: sf.read(str(stems / f"{pid}.flac"), dtype="float32")[0] for pid in ("pA", "pB")}
    out["full"]["report"] = json.loads((root / "episode" / "work" / "report.json").read_text())
    return out


A_ONLY = intervals_minus(TURNS_A, [(t0 - 0.3, t1 + 0.3) for t0, t1 in TURNS_B])
B_ONLY = intervals_minus(TURNS_B, [(t0 - 0.3, t1 + 0.3) for t0, t1 in TURNS_A])
BOTH = [(55.0, 60.0)]


def _speech_lufs(x, spans):
    y = take(x, spans, pad_s=0.2).astype(np.float64)
    return pyln.Meter(SR).integrated_loudness(y)


def test_crossgate_reduces_bleed_by_8db(runs):
    # Max' Spur, während nur Tobias spricht: dort ist nur Übersprechen (und Rauschen)
    spans = [(t0 + 0.4, t1) for t0, t1 in A_ONLY]   # die ersten 400 ms nach Sprecherwechsel nicht werten
    off = rms_db(take(runs[False]["pB"], spans))
    on = rms_db(take(runs[True]["pB"], spans))
    print(f"Übersprechen in Max' Spur: ohne Gate {off:.1f} dB, mit Gate {on:.1f} dB")
    assert off - on >= 8.0


def test_crossgate_keeps_active_speaker(runs):
    for pid, spans in (("pA", A_ONLY), ("pB", B_ONLY)):
        off = rms_db(take(runs[False][pid], spans, pad_s=0.05))
        on = rms_db(take(runs[True][pid], spans, pad_s=0.05))
        print(f"{pid} aktiv: ohne Gate {off:.2f} dB, mit Gate {on:.2f} dB")
        assert abs(off - on) < 0.5


def test_double_talk_not_cut(runs):
    for pid in ("pA", "pB"):
        off = rms_db(take(runs[False][pid], BOTH, pad_s=0.05))
        on = rms_db(take(runs[True][pid], BOTH, pad_s=0.05))
        print(f"{pid} gleichzeitig: ohne Gate {off:.2f} dB, mit Gate {on:.2f} dB")
        assert abs(off - on) < 1.0


def _onset(x, t0, level_db):
    """Erste 5-ms-Stelle nach t0, an der das Signal über level_db liegt (Wortanfang)."""
    n0 = int(t0 * SR)
    w = 240
    for k in range(400):
        seg = x[n0 + k * w:n0 + (k + 1) * w]
        if rms_db(seg) > level_db:
            return (n0 + k * w) / SR
    return None


def test_word_onsets_not_clipped(runs):
    # die ersten 60 ms ab dem ersten Wortanfang jedes Sprecherwechsels: mit Gate höchstens 1 dB leiser als ohne
    for pid, turns, own in (("pA", TURNS_A, A_ONLY), ("pB", TURNS_B, B_ONLY)):
        ref = rms_db(take(runs[False][pid], own))
        for t0, _ in turns[1:]:
            on_t = _onset(runs[False][pid], t0, ref - 20)
            assert on_t is not None
            seg = [(on_t - 0.005, on_t + 0.06)]
            off = rms_db(take(runs[False][pid], seg))
            on = rms_db(take(runs[True][pid], seg))
            print(f"{pid} Wortanfang {on_t:.3f} s: ohne Gate {off:.2f} dB, mit Gate {on:.2f} dB")
            assert off - on < 1.0, (pid, on_t, off, on)


@pytest.mark.parametrize("which", [False, True, "full"])
def test_levels_matched_within_1_5_lu(runs, which):
    la = _speech_lufs(runs[which]["pA"], A_ONLY)
    lb = _speech_lufs(runs[which]["pB"], B_ONLY)
    print(f"{which}: Tobias {la:.2f} LUFS, Max {lb:.2f} LUFS")
    assert abs(la - lb) <= 1.5
    st = {s["participant_id"]: s for s in runs[which]["report"]["stems"]}
    assert st["pB"]["gain_db"] - st["pA"]["gain_db"] == pytest.approx(12.0, abs=2.0)


def test_bleed_model_reported(runs):
    st = {s["participant_id"]: s for s in runs[True]["report"]["stems"]}
    bl = st["pB"]["bleed"][0]
    assert bl["from"] == "Tobias"
    assert bl["delay_ms"] == pytest.approx(150, abs=15)
    # −20 dB bezogen auf Tobias' Rohpegel; in Max' Spur (12 dB leiser) also −20 dB gegen Tobias' Hüllkurve
    assert bl["gain_db"] == pytest.approx(-20, abs=5)


def test_full_chain_noise_reduced(runs):
    st = {s["participant_id"]: s for s in runs["full"]["report"]["stems"]}
    for pid in ("pA", "pB"):
        nf = st[pid]["noise_floor_db"]
        speech_to_noise_before = st[pid]["lufs_speech_before"] - nf["before"]
        speech_to_noise_after = st[pid]["lufs_speech_after"] - nf["after"]
        print(pid, round(speech_to_noise_before, 1), round(speech_to_noise_after, 1))
        assert speech_to_noise_after > speech_to_noise_before + 6
