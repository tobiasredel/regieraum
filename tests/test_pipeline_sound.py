"""`python -m pipeline sound` für verschiedene Formate und Fehlerfälle der CLI."""
import json
import subprocess

import numpy as np
import pytest
import soundfile as sf

from tests.pipeline_fixtures import SR, make_jingle, run_cli

FORMATS = {
    "mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
    "wav": ["-c:a", "pcm_s16le"],
    "m4a": ["-c:a", "aac", "-b:a", "160k"],
    "flac": ["-c:a", "flac"],
    "ogg": ["-c:a", "libvorbis", "-q:a", "5"],
}


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    d = tmp_path_factory.mktemp("sound")
    src = d / "src.wav"
    make_jingle(src, 4.0, level_db=-12)
    return d, src


@pytest.mark.parametrize("ext", list(FORMATS))
def test_sound_formats(source, ext):
    d, src = source
    f = d / f"jingle.{ext}"
    args = ["-ar", "44100"] if ext in ("mp3", "ogg") else []
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), *args, *FORMATS[ext], str(f)], check=True)
    out = d / f"out_{ext}"
    code, lines, err = run_cli("sound", str(f), str(out))
    assert code == 0, err
    info = sf.info(str(out / "playback.wav"))
    assert (info.samplerate, info.channels, info.subtype) == (SR, 2, "PCM_16")
    meta = json.loads((out / "meta.json").read_text())
    assert set(meta) == {"duration_ms", "lufs", "peak_db"}
    assert meta["duration_ms"] == pytest.approx(4000, abs=60)   # Kodierer-Rand bei mp3/aac/ogg
    ref = json.loads((d / "out_wav" / "meta.json").read_text()) if (d / "out_wav").exists() else None
    if ref:
        assert meta["lufs"] == pytest.approx(ref["lufs"], abs=0.5)
    assert -30 < meta["lufs"] < -5
    assert -15 < meta["peak_db"] <= 0.5
    assert lines[-1]["progress"] == 1.0


def test_sound_rejects_garbage(tmp_path):
    bad = tmp_path / "kaputt.mp3"
    bad.write_bytes(b"das ist kein audio" * 100)
    code, _, err = run_cli("sound", str(bad), str(tmp_path / "out"))
    assert code == 1
    assert err.strip().splitlines()[-1] == "Die Datei lässt sich nicht als Audio lesen"
    assert not (tmp_path / "out" / "playback.wav").exists()


def test_sound_rejects_silence(tmp_path):
    f = tmp_path / "still.wav"
    sf.write(str(f), np.zeros((SR, 2), np.float32), SR)
    code, _, err = run_cli("sound", str(f), str(tmp_path / "out"))
    assert code == 1 and "stumm" in err


def test_cli_errors_are_german(tmp_path):
    code, _, err = run_cli("process", str(tmp_path / "fehlt.json"))
    assert code == 1 and err.strip().splitlines()[-1].startswith("Manifest nicht lesbar")
    m = tmp_path / "m.json"
    m.write_text(json.dumps({"work_dir": str(tmp_path / "w"), "out_dir": str(tmp_path / "o"),
                             "rec_start_server_ms": 1000, "rec_stop_server_ms": 5000, "tracks": []}))
    code, _, err = run_cli("stage1", str(m))
    assert code == 1 and err.strip().splitlines()[-1] == "Die Folge hat keine Spuren"
    code, _, err = run_cli("stage2", str(m))
    assert code == 1 and "Stufe 1" in err
    code, _, err = run_cli("unsinn", str(m))
    assert code == 2
