"""pipeline.timeline: Sitzungszeit ↔ Folgenzeit, Kapitel."""
import random

import pytest

from pipeline import timeline as tl

EDL = {"trim_start_ms": 1000, "trim_end_ms": 60000, "cuts": [{"from_ms": 20000, "to_ms": 25000},
                                                            {"from_ms": 24000, "to_ms": 30000},  # überlappt, wird verschmolzen
                                                            {"from_ms": 50000, "to_ms": 51000}]}


def test_segments_and_duration():
    assert tl.segments(EDL) == [(1000, 20000), (30000, 50000), (51000, 60000)]
    assert tl.episode_duration_ms(EDL, 70000) == 19000 + 20000 + 9000
    assert tl.segments({"trim_start_ms": 0, "trim_end_ms": None, "cuts": []}) == [(0, None)]
    assert tl.segments({}, 5000) == [(0, 5000)]


def test_session_to_episode():
    assert tl.session_to_episode(1000, EDL) == 0
    assert tl.session_to_episode(500, EDL) is None          # vor dem Anfang
    assert tl.session_to_episode(19999, EDL) == 18999
    assert tl.session_to_episode(20000, EDL) is None        # im Schnitt
    assert tl.session_to_episode(29999, EDL) is None
    assert tl.session_to_episode(30000, EDL) == 19000
    assert tl.session_to_episode(51000, EDL) == 39000
    assert tl.session_to_episode(60000, EDL) == 48000       # Ende bildet aufs Ende ab
    assert tl.session_to_episode(60001, EDL) is None
    assert isinstance(tl.session_to_episode(30000, EDL), int)
    assert tl.session_to_episode(30000.5, EDL) == pytest.approx(19000.5)


def test_roundtrip_consistent():
    rnd = random.Random(3)
    dur = tl.episode_duration_ms(EDL, 70000)
    for _ in range(2000):
        e = rnd.randrange(0, dur)
        s = tl.episode_to_session(e, EDL)
        assert tl.session_to_episode(s, EDL) == e
    for _ in range(2000):
        s = rnd.randrange(0, 70000)
        e = tl.session_to_episode(s, EDL)
        if e is not None:
            assert tl.episode_to_session(e, EDL) == s


def test_episode_to_session_edges():
    assert tl.episode_to_session(0, EDL) == 1000
    assert tl.episode_to_session(19000, EDL) == 30000      # Schnittkante → Anfang des nächsten Bereichs
    assert tl.episode_to_session(-5, EDL) == 1000
    assert tl.episode_to_session(10 ** 9, EDL) == 60000
    open_end = {"trim_start_ms": 0, "trim_end_ms": None, "cuts": [{"from_ms": 100, "to_ms": 200}]}
    assert tl.episode_to_session(5000, open_end) == 5100


def test_chapters_move_to_cut_edge_and_dedupe():
    m = {"rec_start_server_ms": 0, "rec_stop_server_ms": 70000, "edl": EDL,
         "sounds": [
             {"event_id": "e0", "kind": "intro", "at_ms": 0, "chapter_title": None},          # vor trim_start → 0
             {"event_id": "e1", "kind": "segment", "at_ms": 21000, "chapter_title": "Bahn"},  # im Schnitt → Kante 19 s
             {"event_id": "e2", "kind": "segment", "at_ms": 30000, "chapter_title": "Doppelt"},  # gleiche Zeit, entfällt
             {"event_id": "e3", "kind": "sting", "at_ms": 35000},                              # nie ein Kapitel
             {"event_id": "e4", "kind": "outro", "at_ms": 55000},
             {"event_id": "e5", "kind": "segment", "at_ms": 65000, "chapter_title": "Zu spät"},  # hinter dem Ende
         ],
         "markers": [{"at_ms": 40000, "label": "Wetter"}]}
    ch = tl.chapters(m)
    assert [(c["startTime"], c["title"]) for c in ch] == [(0.0, "Intro"), (19.0, "Bahn"), (29.0, "Wetter"), (43.0, "Outro")]
    assert ch[1]["event_id"] == "e1"


def test_snap_and_sound_end():
    assert tl.snap_to_episode(22000, EDL) == 19000
    assert tl.snap_to_episode(0, EDL) == 0
    assert tl.snap_to_episode(61000, EDL) is None
    assert tl.sound_end_ms({"at_ms": 1000, "duration_ms": 5000, "stop_ms": 3000, "fade_ms": 400}) == 3400
    assert tl.sound_end_ms({"at_ms": 1000, "duration_ms": 5000, "stop_ms": None}) == 6000
