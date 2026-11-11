"""Ganze Kette über die CLI (`process`, dann `stage2` neu und `transcribe`): Jingles, Schnitt, Master, MP3, Kapitel."""
import json
import math
import subprocess

import numpy as np
import pyloudnorm as pyln
import pytest
from mutagen.id3 import ID3

from pipeline import timeline
from tests.pipeline_fixtures import (SR, Session, Speaker, decode, find_clicks_sr, make_jingle, rms_db, run_cli)

DUR = 75.0
TURNS_A = [(4.0, 14.0), (26.0, 36.0), (50.0, 58.0)]
TURNS_B = [(14.5, 21.0), (37.0, 46.0), (59.0, 69.0)]
INTRO_AT = 500          # ms, Sprecher still
SEG_AT, SEG_STOP, SEG_FADE = 22000, 24500, 400   # Segment-Jingle, wird nach 2,5 s mit 400 ms ausgeblendet
OUTRO_AT = 72000        # läuft über den Stopp (75 s) hinaus
CLICK_IN_JINGLE = 0.05  # s
EDL = {"trim_start_ms": 200, "trim_end_ms": None,
       "cuts": [{"from_ms": 47000, "to_ms": 49000}, {"from_ms": 30000, "to_ms": 31000}]}
TARGET = -16.0
TP = -1.0


@pytest.fixture(scope="module")
def done(tmp_path_factory):
    root = tmp_path_factory.mktemp("process")
    jd = root / "sounds"
    jd.mkdir()
    make_jingle(jd / "intro.wav", 3.0, level_db=-10)
    make_jingle(jd / "seg.wav", 8.0, freq=330, level_db=-20)
    make_jingle(jd / "outro.wav", 6.0, freq=196, level_db=-12)
    # Jingles in verschiedenen Formaten, wie sie hochgeladen werden
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(jd / "outro.wav"), "-c:a", "libmp3lame", "-b:a", "192k",
                    str(jd / "outro.mp3")], check=True)
    cover = root / "cover.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x1f6f78:s=1400x1400", "-frames:v",
                    "1", str(cover)], check=True)
    sounds = [
        {"event_id": "ev_intro", "sound_id": "s_intro", "path": str(jd / "intro.wav"), "at_ms": INTRO_AT, "stop_ms": None,
         "fade_ms": 400, "gain_db": 0, "kind": "intro", "chapter_title": "Intro"},
        {"event_id": "ev_seg", "sound_id": "s_seg", "path": str(jd / "seg.wav"), "at_ms": SEG_AT, "stop_ms": SEG_STOP,
         "fade_ms": SEG_FADE, "gain_db": -3, "kind": "segment", "chapter_title": "Die Bahn"},
        {"event_id": "ev_sting", "sound_id": "s_sting", "path": str(jd / "intro.wav"), "at_ms": 30200, "stop_ms": None,
         "fade_ms": 400, "gain_db": 0, "kind": "sting", "chapter_title": None},   # liegt im Schnitt
        {"event_id": "ev_outro", "sound_id": "s_outro", "path": str(jd / "outro.mp3"), "at_ms": OUTRO_AT,
         "stop_ms": None, "fade_ms": 400, "gain_db": 0, "kind": "outro", "chapter_title": "Outro"},
    ]
    a = Speaker("pA", "Tobias", "a", 0.0, TURNS_A, ppm=35.0, noise_db=-58, clip_at=[33.0])
    b = Speaker("pB", "Max", "b", -12.0, TURNS_B, ppm=-20.0, noise_db=-62, bleed_from="pA", bleed_db=-24,
                bleed_delay_ms=120, missing_chunks=[(0, 4)])   # fehlender Chunk: 16 … 20 s der Spur ≈ 14 … 18 s Sitzung
    s = Session(root, DUR, [a, b], sounds=sounds, markers=[{"at_ms": 48000, "label": "Wetter"}], edl=EDL,
                audio={"target_lufs": TARGET, "true_peak_db": TP, "denoise": "normal", "crossgate": True, "mp3_kbps": 160},
                tags={"title": "Folge 7: Bahnchaos", "artist": "Anna Beispiel & Ben Muster",
                      "album": "Zwei am Mikro", "track": 7, "year": 2026, "cover_path": str(cover)})
    manifest, truth = s.build()
    code, lines, err = run_cli("process", str(manifest))
    assert code == 0, err
    ep = root / "episode"
    return {"root": root, "ep": ep, "manifest": manifest, "lines": lines, "stderr": err,
            "report": json.loads((ep / "work" / "report.json").read_text())}


def test_outputs_exist(done):
    out = done["ep"] / "out"
    for f in ("episode.mp3", "mix_uncut.m4a", "peaks.json", "chapters.json"):
        assert (out / f).exists(), f
    assert (done["ep"] / "work" / "report.json").exists()
    leftovers = [p.name for p in (done["ep"] / "work").rglob("*.wav")]
    assert leftovers == []


def test_progress_lines(done):
    ps = [ln["progress"] for ln in done["lines"]]
    assert ps[-1] == 1.0 and ps == sorted(ps)
    assert len(ps) >= 10


def test_duration_follows_edl(done):
    rep = done["report"]
    session_ms = OUTRO_AT + 6000   # Outro endet nach dem Stopp
    assert rep["session_duration_ms"] == session_ms
    expect = session_ms - 200 - 2000 - 1000
    assert rep["duration_ms"] == expect
    assert expect == timeline.episode_duration_ms(EDL, session_ms)
    x = decode(done["ep"] / "out" / "episode.mp3", 44100)
    assert len(x) / 44100 * 1000 == pytest.approx(expect, abs=30)


def test_loudness_and_true_peak(done):
    mp3 = done["ep"] / "out" / "episode.mp3"
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(mp3), "-af", "ebur128=peak=true",
                        "-f", "null", "-"], capture_output=True, text=True)
    summ = r.stderr[r.stderr.rfind("Summary:"):]
    lufs = float(summ.split("I:")[1].split()[0])
    tp = float(summ.split("Peak:")[1].split()[0])
    x = decode(mp3, 48000).astype(np.float64)
    lufs2 = pyln.Meter(SR).integrated_loudness(x)
    print(f"MP3: ffmpeg {lufs} LUFS, pyloudnorm {lufs2:.2f} LUFS, True Peak {tp} dBTP")
    assert abs(lufs - TARGET) <= 0.7
    assert abs(lufs2 - TARGET) <= 0.7
    assert tp <= TP
    assert np.max(np.abs(x)) < 1.0  # kein Clipping
    v = done["report"]["master"]["verified"]
    assert v["lufs"] == pytest.approx(lufs, abs=0.15) and v["true_peak_db"] <= TP


def _episode_s(session_ms):
    return timeline.session_to_episode(session_ms, EDL) / 1000


def test_jingles_sample_accurate_in_mp3_and_preview(done):
    mp3 = decode(done["ep"] / "out" / "episode.mp3", 44100)[:, 0]
    m4a = decode(done["ep"] / "out" / "mix_uncut.m4a", 48000)[:, 0]
    for at in (INTRO_AT, SEG_AT, OUTRO_AT):
        click_session = at + CLICK_IN_JINGLE * 1000
        exp_ep = _episode_s(click_session)
        got_ep = find_clicks_sr(mp3, [exp_ep], 44100, 30)[0]
        got_un = find_clicks_sr(m4a, [click_session / 1000], 48000, 30)[0]
        print(f"Jingle {at} ms: MP3 {1000 * (got_ep - exp_ep):+.2f} ms, Vorschau {1000 * (got_un - click_session / 1000):+.2f} ms")
        assert abs(got_ep - exp_ep) * 1000 <= 2.0
        assert abs(got_un - click_session / 1000) * 1000 <= 2.0


def test_stop_fades_out(done):
    x = decode(done["ep"] / "out" / "episode.mp3", 48000)[:, 0]
    stop = _episode_s(SEG_STOP)
    before = rms_db(x[int((stop - 0.8) * SR):int((stop - 0.1) * SR)])
    after = rms_db(x[int((stop + SEG_FADE / 1000 + 0.05) * SR):int((stop + 1.4) * SR)])
    mid = rms_db(x[int((stop + 0.15) * SR):int((stop + 0.25) * SR)])
    print(f"Segment-Jingle: vor Stopp {before:.1f} dB, in der Blende {mid:.1f} dB, danach {after:.1f} dB")
    assert before - after > 30
    assert after < mid < before


def test_sting_in_cut_is_gone_and_chapters(done):
    ch = json.loads((done["ep"] / "out" / "chapters.json").read_text())
    assert ch["version"] == "1.2.0"
    titles = [(round(c["startTime"], 2), c["title"]) for c in ch["chapters"]]
    exp = [(round(_episode_s(INTRO_AT), 2), "Intro"), (round(_episode_s(SEG_AT), 2), "Die Bahn"),
           (round(timeline.snap_to_episode(48000, EDL) / 1000, 2), "Wetter"), (round(_episode_s(OUTRO_AT), 2), "Outro")]
    assert titles == exp
    # Marker im Schnitt rückt an die Schnittkante
    assert exp[2][0] == round(timeline.session_to_episode(49000, EDL) / 1000, 2)


def test_mp3_tags_and_chapters_in_ffprobe(done):
    mp3 = done["ep"] / "out" / "episode.mp3"
    tags = ID3(str(mp3))
    assert tags.version[:2] == (2, 3)
    assert str(tags["TIT2"]) == "Folge 7: Bahnchaos"
    assert str(tags["TPE1"]).startswith("Anna Beispiel")
    assert str(tags["TALB"]) == "Zwei am Mikro"
    assert str(tags["TRCK"]) == "7"
    assert "TYER" in tags or "TDRC" in tags
    assert tags.getall("APIC")[0].mime == "image/png"
    assert len(tags.getall("CHAP")) == 4 and len(tags.getall("CTOC")) == 1
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_chapters", "-show_format",
                        "-show_streams", str(mp3)], capture_output=True, text=True, check=True)
    info = json.loads(r.stdout)
    chaps = info["chapters"]
    assert [c["tags"]["title"] for c in chaps] == ["Intro", "Die Bahn", "Wetter", "Outro"]
    assert float(chaps[1]["start_time"]) == pytest.approx(_episode_s(SEG_AT), abs=0.002)
    st = info["streams"][0]
    assert st["codec_name"] == "mp3" and int(st["sample_rate"]) == 44100 and st["channels"] == 2
    assert int(info["format"]["bit_rate"]) == pytest.approx(160000, rel=0.05)


def test_peaks_json(done):
    pk = json.loads((done["ep"] / "out" / "peaks.json").read_text())
    assert pk["sample_rate"] == SR and pk["samples_per_peak"] == 960
    n = done["report"]["session_duration_ms"] * SR // 1000
    assert len(pk["peaks"]) == 2 * math.ceil(n / 960)
    v = np.array(pk["peaks"])
    assert v.min() >= -128 and v.max() <= 127
    assert np.all(v[0::2] <= v[1::2])
    assert v[1::2].max() > 40   # sichtbare Wellenform


def test_report_contents(done):
    rep = done["report"]
    w = " ".join(rep["warnings"])
    assert "Lücke in der Spur von Max" in w
    assert "Übersteuerung in der Spur von Tobias" in w
    tr = {t["track_id"]: t for t in rep["tracks"]}
    assert tr["pB_t0"]["gaps"] and tr["pA_t0"]["clip_events"] >= 1
    for t in rep["tracks"]:
        for k in ("drift_ppm", "start_offset_ms", "lufs_speech_before", "lufs_speech_after", "gain_db"):
            assert t[k] is not None, k
    assert rep["mix"]["sounds"][0]["gain_db"] is not None
    assert rep["master"]["lufs_before"] is not None and rep["master"]["gain_db"] is not None


def test_remix_with_new_edl(done):
    m = json.loads(done["manifest"].read_text())
    m["edl"] = {"trim_start_ms": 0, "trim_end_ms": 60000, "cuts": []}
    done["manifest"].write_text(json.dumps(m))
    code, lines, err = run_cli("stage2", str(done["manifest"]))
    assert code == 0, err
    rep = json.loads((done["ep"] / "work" / "report.json").read_text())
    assert rep["duration_ms"] == 60000
    assert any("Lücke" in w for w in rep["warnings"])   # Warnungen aus Stufe 1 bleiben erhalten
    ch = json.loads((done["ep"] / "out" / "chapters.json").read_text())
    assert [c["title"] for c in ch["chapters"]] == ["Intro", "Die Bahn", "Wetter"]   # Outro liegt hinter dem Ende


def test_transcribe_tiny(done, monkeypatch):
    monkeypatch.setenv("PS_WHISPER_MODEL", "tiny")   # in Tests ausschließlich tiny
    code, lines, err = run_cli("transcribe", str(done["manifest"]), timeout=900)
    assert code == 0, err
    tr = json.loads((done["ep"] / "out" / "transcript.json").read_text())
    segs = tr["segments"]
    assert len(segs) >= 4
    speakers = {s["speaker"] for s in segs}
    assert speakers == {"Tobias", "Max"}
    turns = {"Tobias": TURNS_A, "Max": TURNS_B}
    for s in segs:
        assert 0 <= s["start_ms"] < s["end_ms"] <= DUR * 1000 + 6000
        # jedes Segment liegt überwiegend in Sprechabschnitten seines Sprechers (kein Übersprechen transkribiert)
        a_s, b_s = s["start_ms"] / 1000, s["end_ms"] / 1000
        inside = sum(max(0.0, min(b_s, b + 0.5) - max(a_s, a - 0.5)) for a, b in turns[s["speaker"]])
        assert inside >= 0.7 * (b_s - a_s), s
        assert s["text"]
    vtt = (done["ep"] / "out" / "transcript.vtt").read_text()
    assert vtt.startswith("WEBVTT") and "<v Tobias>" in vtt and "<v Max>" in vtt
