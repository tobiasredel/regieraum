"""LiveSession mit gemockten Diensten: Mitschrift, Überlast, Fehler, Takt, Karten."""
from __future__ import annotations

import asyncio
import json

from assist.live import LiveConfig, LiveSession, _Pending
from assist.llm import AssistError
from assist.vad import Utterance
from tests.assist_fixtures.fakes import SR, Clock, FakeLLM, FakeTranscriber, chunks, synth, to_s24le

import numpy as np

PROVIDERS = ("openai", "anthropic", "claude", "gpt", "sonnet", "opus", "whisper")
DOSSIER_URL = "https://www.destatis.de/rente-2026"
PLAN = [
    {"id": "s1", "kind": "blitzlicht", "title": "Rente", "input": "Rentenniveau",
     "dossier": {"lage": "…", "fakten": [{"text": "Rentenniveau 48 %", "quelle_titel": "Destatis",
                                          "quelle_url": DOSSIER_URL, "datum": "2026-11-01"}],
                 "begriffe": ["Bärbel Bas", "Haltelinie"], "quellen": []}},
    {"id": "s2", "kind": "hauptthema", "title": "Haushalt", "dossier": {"begriffe": ["Lars Klingbeil"]}},
]


def run(coro):
    return asyncio.run(coro)


def make(clock=None, **kw):
    rec = {"lines": [], "cards": [], "status": []}
    cfg = kw.pop("config", None) or LiveConfig(poll_s=0.01)
    s = LiveSession(rec_start_server_ms=0.0, server_clock=clock or Clock(0.0),
                    on_line=rec["lines"].append, on_card=rec["cards"].append, on_status=rec["status"].append,
                    config=cfg, **kw)
    return s, rec


def add_line(s: LiveSession, speaker: str, start: float, end: float, text: str, track="t1", snr=30.0):
    u = Utterance(track, speaker, start, end, np.zeros(10, np.float32), -30.0, snr, int(end - start))
    p = _Pending(u, text=text, ready_at=s.server_clock() - 10_000, done=True)
    s._output(p)


async def feed_tracks(s: LiveSession, tracks: dict, clock: Clock):
    """tracks: name -> Signal (48 kHz). Schiebt alle Chunks rein, Uhr läuft mit."""
    parts = {n: list(chunks(x)) for n, x in tracks.items()}
    for k in range(max(len(p) for p in parts.values())):
        for name, p in parts.items():
            if k < len(p):
                i, c = p[k]
                s.feed_chunk(f"t-{name}", name, SR, "s24le", i, to_s24le(c), 0.0)
        clock.t = (k + 1) * 2000 + 300
        await asyncio.sleep(0.02)
    s.end_of_audio()
    await s.drain(5)


# ------------------------------------------------------------------ Mitschrift

def test_lines_in_session_time_and_jsonl(tmp_path):
    async def main():
        clock = Clock(0.0)
        tr = FakeTranscriber(["Erste Äußerung zur Rente", "Zweite Äußerung zum Haushalt"])
        s, rec = make(clock, transcriber=tr, llm=None, work_dir=tmp_path)
        await s.start()
        s.set_plan(PLAN)
        await feed_tracks(s, {"Tobias": synth(8, [(1.0, 2.5), (4.0, 5.0)])}, clock)
        await s.close()
        return rec, tr
    rec, tr = run(main())
    assert [l["text"] for l in rec["lines"]] == ["Erste Äußerung zur Rente", "Zweite Äußerung zum Haushalt"]
    l0 = rec["lines"][0]
    assert set(l0) == {"speaker", "start_ms", "end_ms", "text"}
    assert l0["speaker"] == "Tobias" and 750 <= l0["start_ms"] <= 1000 and 2450 <= l0["end_ms"] <= 2700
    assert "Bärbel Bas" in tr.calls[0]["terms"] and "Lars Klingbeil" in tr.calls[0]["terms"]
    rows = [json.loads(x) for x in (tmp_path / "live_transcript.jsonl").read_text().splitlines()]
    assert rows[0]["text"] == "Erste Äußerung zur Rente" and rows[0]["track_id"] == "t-Tobias"


def test_crosstalk_duplicate_louder_track_wins():
    async def main():
        clock = Clock(0.0)
        # Max spricht; Tobias' Mikro hört ihn 10 dB leiser mit (zu laut für die Pegelprüfung) → gleicher Text
        tr = FakeTranscriber(lambda audio, n: "Die Schuldenbremse ist ein Taschenspielertrick der Koalition")
        s, rec = make(clock, transcriber=tr, llm=None)
        await s.start()
        loud = synth(6, [(1.0, 3.0)], speech_db=-25, noise_db=-70, seed=5)
        bleed = synth(6, [(1.0, 3.0)], speech_db=-35, noise_db=-70, seed=5)
        await feed_tracks(s, {"Max": loud, "Tobias": bleed}, clock)
        await s.close()
        return rec, s, tr
    rec, s, tr = run(main())
    assert len(tr.calls) == 2
    assert len(rec["lines"]) == 1
    assert rec["lines"][0]["speaker"] == "Max"
    assert s.stats.lines_deduped == 1


def test_strong_bleed_is_not_transcribed_at_all():
    async def main():
        clock = Clock(0.0)
        tr = FakeTranscriber(lambda audio, n: "Die Schuldenbremse ist ein Taschenspielertrick der Koalition")
        s, rec = make(clock, transcriber=tr, llm=None)
        await s.start()
        loud = synth(8, [(1.0, 3.0)], speech_db=-25, noise_db=-70, seed=5)
        # Tobias' Spur: Übersprechen von Max (24 dB leiser), danach eigene Rede
        own = synth(8, [(5.0, 6.5)], speech_db=-25, noise_db=-70, seed=8)
        bleed = synth(8, [(1.0, 3.0)], speech_db=-49, noise_db=-200, seed=5)
        await feed_tracks(s, {"Max": loud, "Tobias": own + bleed}, clock)
        await s.close()
        return rec, s, tr
    rec, s, tr = run(main())
    assert s.stats.bleed_skipped == 1
    assert len(tr.calls) == 2  # Max' Äußerung und Tobias' eigene, nicht das Übersprechen
    assert [l["speaker"] for l in rec["lines"]] == ["Max", "Tobias"]


def test_bleed_tail_of_own_utterance_is_trimmed():
    async def main():
        clock = Clock(0.0)
        tr = FakeTranscriber(lambda audio, n: f"Text {n} mit genug Wörtern drin")
        s, rec = make(clock, transcriber=tr, llm=None)
        await s.start()
        # Tobias spricht 1,0–2,5 s, direkt danach (300 ms Lücke) spricht Max laut; Tobias' Mikro hört das mit
        tob = synth(8, [(1.0, 2.5)], speech_db=-25, noise_db=-70, seed=8) + synth(8, [(2.8, 4.0)], speech_db=-49, noise_db=-200, seed=5)
        mx = synth(8, [(2.8, 4.0)], speech_db=-25, noise_db=-70, seed=5)
        await feed_tracks(s, {"Max": mx, "Tobias": tob}, clock)
        await s.close()
        return tr, rec
    tr, rec = run(main())
    # die an Tobias' Äußerung hängenden Übersprech-Rahmen sind im Audio für die Mitschrift abgesenkt
    # Tobias' Äußerung endet an seinem letzten eigenen Wort, nicht am Ende des Übersprechens
    tob = [l for l in rec["lines"] if l["speaker"] == "Tobias"]
    assert len(tob) == 1 and 2500 <= tob[0]["end_ms"] <= 2700, tob
    assert all(c["samples"] < 16000 * 2.0 for c in tr.calls)


def test_simultaneous_different_text_both_kept():
    async def main():
        clock = Clock(0.0)
        texts = iter(["Ich finde das völlig falsch so", "Moment mal, lass mich ausreden bitte"])
        tr = FakeTranscriber(lambda audio, n: next(texts))
        s, rec = make(clock, transcriber=tr, llm=None)
        await s.start()
        await feed_tracks(s, {"Max": synth(6, [(1.0, 3.0)], seed=5), "Tobias": synth(6, [(1.2, 3.0)], seed=9)}, clock)
        await s.close()
        return rec
    rec = run(main())
    assert len(rec["lines"]) == 2


def test_overload_skips_old_utterances_instead_of_queueing():
    async def main():
        clock = Clock(0.0)
        tr = FakeTranscriber(delay=0.3)
        cfg = LiveConfig(poll_s=0.01, stt_concurrency=1, stt_max_pending=2, stt_max_age_ms=5000)
        s, rec = make(clock, transcriber=tr, llm=None, config=cfg)
        await s.start()
        for k in range(12):  # 12 Äußerungen fast gleichzeitig, Sitzungszeit läuft schnell
            u = Utterance("t1", "Tobias", k * 1000, k * 1000 + 800, np.zeros(16000, np.float32), -30, 30, 800)
            clock.t = k * 1000 + 900
            s._enqueue_utterance(u)
        await asyncio.sleep(1.5)
        await s.close()
        return rec, s, tr
    rec, s, tr = run(main())
    assert s.stats.stt_skipped >= 8
    assert len(tr.calls) <= 4
    assert any(st["stt"] == "hinkt" and "übersprungen" in st["message"] for st in rec["status"])


def test_stt_error_and_timeout_become_status_only():
    async def main():
        clock = Clock(0.0)
        tr = FakeTranscriber(fail=AssistError("Zeitüberschreitung"))
        s, rec = make(clock, transcriber=tr, llm=None)
        await s.start()
        await feed_tracks(s, {"Tobias": synth(6, [(1.0, 2.0), (3.0, 4.0)])}, clock)
        assert rec["lines"] == []
        # hängender Dienst: Zeitlimit greift
        s.transcriber = FakeTranscriber(hang=True)
        s.cfg.stt_timeout_s = 0.1
        s._stt_block_until = 0
        u = Utterance("t1", "Tobias", 10_000, 11_000, np.zeros(16000, np.float32), -30, 30, 900)
        clock.t = 11_100
        s._enqueue_utterance(u)
        await asyncio.sleep(1.5)
        # Dienst wieder da
        s.transcriber = FakeTranscriber(["Jetzt geht es wieder"])
        s._stt_block_until = 0
        clock.t = 20_100
        s._enqueue_utterance(Utterance("t1", "Tobias", 19_000, 20_000, np.zeros(16000, np.float32), -30, 30, 900))
        await s.drain(3)
        await s.close()
        return rec, s
    rec, s = run(main())
    assert s.stats.stt_errors >= 3
    msgs = [st["message"] for st in rec["status"]]
    assert any("Mitschrift gestört: Zeitüberschreitung" in m for m in msgs)
    assert rec["lines"][-1]["text"] == "Jetzt geht es wieder"
    assert rec["status"][-1]["stt"] == "ok"
    for m in msgs:
        assert not any(p in m.lower() for p in PROVIDERS)


def test_hallucinations_and_empty_text_are_dropped():
    s, _ = make(transcriber=FakeTranscriber(), llm=None)
    u = Utterance("t", "Max", 0, 800, np.zeros(1), -30, 20, 500)
    assert s._clean_text("Untertitel im Auftrag des ZDF, 2021", u) is None
    assert s._clean_text("  ...  ", u) is None
    assert s._clean_text("Das ist ein ganz langer Satz mit sehr vielen Wörtern drin", u) is None  # 500 ms Sprache
    long = Utterance("t", "Max", 0, 3000, np.zeros(1), -30, 20, 2500)
    assert s._clean_text("Na  gut,   dann  eben so.", long) == "Na gut, dann eben so."


def test_pause_stops_transcription():
    async def main():
        clock = Clock(0.0)
        tr = FakeTranscriber()
        s, rec = make(clock, transcriber=tr, llm=None)
        await s.start()
        s.set_paused(True)
        await feed_tracks(s, {"Tobias": synth(6, [(1.0, 3.0)])}, clock)
        await s.close()
        return rec, tr
    rec, tr = run(main())
    assert tr.calls == [] and rec["lines"] == []
    assert any("Pause" in st["message"] for st in rec["status"])


def test_without_keys_session_stays_quiet_with_status():
    async def main():
        s, rec = make(transcriber=None, llm=None)
        await s.start()
        s.feed_chunk("t", "Max", SR, "s24le", 0, b"\0" * 300, 0.0)
        s.pulse()
        await asyncio.sleep(0.05)
        await s.close()
        return rec
    rec = run(main())
    assert rec["status"][0] == {"on": True, "stt": "aus", "ai": "aus", "lag_ms": None,
                                "message": "Mitschrift nicht eingerichtet, Begleiter bleibt aus"}


def test_feed_chunk_from_other_thread_does_not_block():
    async def main():
        clock = Clock(0.0)
        tr = FakeTranscriber(["Aus dem Thread geliefert"])
        s, rec = make(clock, transcriber=tr, llm=None)
        await s.start()
        x = synth(6, [(1.0, 3.0)])

        def worker():
            for i, c in chunks(x):
                s.feed_chunk("t", "Max", SR, "s24le", i, to_s24le(c), 0.0)
        await asyncio.to_thread(worker)
        clock.t = 6500
        s.end_of_audio()
        await s.drain(3)
        await s.close()
        return rec
    rec = run(main())
    assert [l["text"] for l in rec["lines"]] == ["Aus dem Thread geliefert"]


# ------------------------------------------------------------------ Hinweise

CARD = {"anlass": "Korrektur", "karte": {"kind": "fakt", "text": "Korrektur: Rentenniveau liegt bei 48 %, nicht 43 %",
                                         "detail": "", "basis": "dossier", "quelle_url": DOSSIER_URL + "/"}}


def talk(s, start_s, n=4, speaker="Max", numbers=True):
    for k in range(n):
        text = ("Das Rentenniveau sinkt doch ohnehin auf dreiundvierzig Prozent, sagen alle Experten." if numbers
                else "Das Rentenniveau sinkt doch ohnehin immer weiter nach unten, sagen alle Experten im Land.")
        label = f"{start_s}-{k}" if numbers else "ABCDEFGH"[start_s // 30] + "abcdefgh"[k]
        add_line(s, speaker, (start_s + k * 5) * 1000, (start_s + k * 5 + 4) * 1000, f"Satz {label}: {text}")


def test_tick_rhythm_silence_and_pause():
    async def main():
        clock = Clock(0.0)
        llm = FakeLLM([CARD, {"anlass": "läuft", "karte": None}])
        s, rec = make(clock, transcriber=FakeTranscriber(), llm=llm)
        await s.start()
        s.set_plan(PLAN)
        s.set_current_segment("s1")
        talk(s, 0, numbers=False)
        clock.t = 20_000
        await asyncio.sleep(0.1)
        assert llm.calls == []  # vor 35 s kein Hinweis
        clock.t = 36_000
        await asyncio.sleep(0.1)
        assert len(llm.calls) == 1 and len(rec["cards"]) == 1
        clock.t = 60_000
        await asyncio.sleep(0.1)
        assert len(llm.calls) == 1  # nichts Neues gesagt → kein Aufruf
        talk(s, 60, numbers=False)
        clock.t = 66_000
        await asyncio.sleep(0.1)
        assert len(llm.calls) == 1  # Takt läuft erst 35 s nach der letzten Karte wieder
        clock.t = 72_000
        await asyncio.sleep(0.1)
        assert len(llm.calls) == 2 and len(rec["cards"]) == 1  # Modell schweigt
        s.set_paused(True)
        talk(s, 120, numbers=False)
        clock.t = 200_000
        await asyncio.sleep(0.1)
        assert len(llm.calls) == 2  # in der Pause nichts
        await s.close()
        return rec, llm, s
    rec, llm, s = run(main())
    c = rec["cards"][0]
    assert set(c) == {"id", "kind", "text", "detail", "basis", "quelle_url", "segment_id", "server_ms"}
    assert c["kind"] == "fakt" and c["basis"] == "dossier" and c["quelle_url"] == DOSSIER_URL
    assert c["segment_id"] == "s1" and len(c["id"]) == 12
    # Prompt-Aufbau: Dossiers im zwischengespeicherten Präfix, Mitschrift und Karten in der Nachricht
    call = llm.calls[1]
    assert call["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "Rentenniveau 48 %" in call["system"][-1]["text"]
    assert "Korrektur: Rentenniveau" in call["messages"][0]["content"]  # bisherige Karten
    assert "Satz Ca" in call["messages"][0]["content"]
    assert s.stats.hint_silent == 1


def test_char_limits_enforced():
    async def main():
        long = {"anlass": "x", "karte": {"kind": "frage", "text": "Warum " + "sehr " * 60 + "lange Frage?",
                                         "detail": "Detail " * 100, "basis": "allgemein", "quelle_url": "https://x.de"}}
        llm = FakeLLM([long])
        s, rec = make(transcriber=FakeTranscriber(), llm=llm)
        await s.start()
        await s.pulse()
        await s.close()
        return rec
    rec = run(main())
    c = rec["cards"][0]
    assert len(c["text"]) <= 140 and c["text"].endswith("…")
    assert len(c["detail"]) <= 300
    assert c["quelle_url"] is None  # allgemein hat nie eine Quelle


def test_repetition_and_already_said_are_rejected():
    async def main():
        same = {"anlass": "x", "karte": {"kind": "frage", "text": "Wer zahlt am Ende die Zeche für die Rentengarantie?",
                                         "detail": "", "basis": "allgemein", "quelle_url": None}}
        reworded = {"anlass": "x", "karte": {"kind": "frage", "text": "Wer zahlt am Ende eigentlich die Zeche für die Rentengarantie?",
                                             "detail": "", "basis": "allgemein", "quelle_url": None}}
        said = {"anlass": "x", "karte": {"kind": "stichwort", "text": "Rentenniveau sinkt auf dreiundvierzig Prozent laut Experten",
                                         "detail": "", "basis": "allgemein", "quelle_url": None}}
        llm = FakeLLM([same, reworded, said])
        clock = Clock(100_000)
        s, rec = make(clock, transcriber=FakeTranscriber(), llm=llm)
        await s.start()
        talk(s, 50)
        await s.pulse()
        await s.pulse()
        await s.pulse()
        await s.close()
        return rec, s
    rec, s = run(main())
    assert len(rec["cards"]) == 1
    assert s.stats.cards_rejected == 2


def test_dossier_fact_with_unknown_url_loses_url():
    async def main():
        card = {"anlass": "x", "karte": {"kind": "fakt", "text": "Haltelinie gilt bis 2031 laut Gesetz",
                                         "detail": "", "basis": "dossier", "quelle_url": "https://erfunden.example/x"}}
        s, rec = make(transcriber=FakeTranscriber(), llm=FakeLLM([card]))
        await s.start()
        s.set_plan(PLAN)
        await s.pulse()
        await s.close()
        return rec
    rec = run(main())
    assert rec["cards"][0]["quelle_url"] is None and rec["cards"][0]["basis"] == "dossier"


def test_llm_error_becomes_status_and_session_continues():
    async def main():
        clock = Clock(0.0)
        llm = FakeLLM([asyncio.TimeoutError(), CARD])
        s, rec = make(clock, transcriber=FakeTranscriber(), llm=llm)
        await s.start()
        s.set_plan(PLAN)
        await s.pulse()
        await s.pulse()
        await s.close()
        return rec
    rec = run(main())
    assert any(st["ai"] == "fehler" and st["message"] == "Hinweise gestört: Zeitüberschreitung" for st in rec["status"])
    assert len(rec["cards"]) == 1
    assert rec["status"][-1]["ai"] == "ok"


def test_callback_exceptions_do_not_break_session():
    async def main():
        def boom(_):
            raise RuntimeError("UI kaputt")
        s = LiveSession(rec_start_server_ms=0, server_clock=Clock(0), on_card=boom, on_status=boom, on_line=boom,
                        transcriber=FakeTranscriber(), llm=FakeLLM([CARD]), config=LiveConfig(poll_s=0.01))
        await s.start()
        await s.pulse()
        add_line(s, "Max", 0, 1000, "geht trotzdem")
        await s.close()
        return s
    s = run(main())
    assert s.stats.cards == 1 and s.stats.lines == 1


def test_lookup_requires_source_from_search():
    async def main():
        good = {"text": "Mindestlohn seit 1.1.2026: 13,90 € je Stunde", "detail": "", "quelle_url": "https://www.bundesregierung.de/mindestlohn"}
        bad = {"text": "Mindestlohn liegt bei 15 €", "detail": "", "quelle_url": "https://erfunden.example"}
        llm = FakeLLM([good, bad], sources=["https://bundesregierung.de/mindestlohn/"])
        s, rec = make(transcriber=FakeTranscriber(), llm=llm)
        await s.start()
        await s.lookup("Mindestlohn aktuell")
        await s.lookup("Mindestlohn aktuell")
        await s.close()
        return rec, llm
    rec, llm = run(main())
    a, b = rec["cards"]
    assert a["kind"] == "recherche" and a["basis"] == "netz" and a["quelle_url"] == "https://bundesregierung.de/mindestlohn/"
    assert b["text"] == "Nichts Belastbares gefunden: Mindestlohn aktuell" and b["quelle_url"] is None
    assert llm.calls[0]["tools"][0]["type"].startswith("web_search")


def test_summary_rolls_old_transcript_forward():
    async def main():
        clock = Clock(0.0)
        llm = FakeLLM(lambda kw: {"kurzfassung": "Rente besprochen: 43 % genannt."} if "kurzfassung" in json.dumps(kw["schema"]) else {"anlass": "", "karte": None})
        cfg = LiveConfig(poll_s=0.01, hint_interval_ms=10**9)
        s, rec = make(clock, transcriber=FakeTranscriber(), llm=llm, config=cfg)
        await s.start()
        talk(s, 0, n=8)
        clock.t = 300_000  # alles älter als vier Minuten
        await asyncio.sleep(0.2)
        await s.close()
        return s
    s = run(main())
    assert s.summary == "Rente besprochen: 43 % genannt."
    assert s.summarized_upto == 8


def test_number_triggers_quick_correction_check():
    async def main():
        clock = Clock(0.0)
        stichwort = {"anlass": "x", "karte": {"kind": "stichwort", "text": "Noch offen: Haltelinie bis 2031",
                                              "detail": "", "basis": "dossier", "quelle_url": None}}
        llm = FakeLLM([CARD, stichwort])
        s, rec = make(clock, transcriber=FakeTranscriber(), llm=llm)
        await s.start()
        s.set_plan(PLAN)
        clock.t = 13_000
        add_line(s, "Max", 9_000, 12_500, "Das Rentenniveau liegt doch bei 43 Prozent.")
        await asyncio.sleep(0.1)
        assert len(llm.calls) == 1 and "Zahlen-Prüfung" in llm.calls[0]["messages"][0]["content"]
        assert rec["cards"][0]["text"].startswith("Korrektur")
        clock.t = 26_000
        add_line(s, "Max", 22_000, 25_000, "Und die Haltelinie gilt bis zweitausendeinunddreißig.")
        await asyncio.sleep(0.1)
        assert len(llm.calls) == 2  # Prüfung trotz frischer Karte, aber nur Korrekturen zählen
        assert len(rec["cards"]) == 1
        await s.close()
        return s
    s = run(main())
    assert s.stats.cards_rejected == 1


def test_pulse_waits_for_running_tick():
    async def main():
        clock = Clock(0.0)
        q = {"anlass": "x", "karte": {"kind": "frage", "text": "Wer zahlt am Ende die Zeche für die Rentengarantie?",
                                      "detail": "", "basis": "allgemein", "quelle_url": None}}
        llm = FakeLLM([CARD, q], delay=0.2)
        s, rec = make(clock, transcriber=FakeTranscriber(), llm=llm)
        await s.start()
        s.set_plan(PLAN)
        tick = asyncio.ensure_future(s._hint("tick"))
        await asyncio.sleep(0.01)
        await s.pulse()
        await tick
        prompts_seen = [c["messages"][0]["content"] for c in llm.calls]
        await s.close()
        return rec, prompts_seen
    rec, seen = run(main())
    assert len(rec["cards"]) == 2
    assert "Korrektur: Rentenniveau" in seen[1]  # der Impuls kennt die Karte des Takts


# ------------------------------------------------------------------ Wissenslücke

ANSWER = {"anlass": "Tobias sucht die Zahl", "art": "antwort", "nachschlagen": None,
          "karte": {"kind": "fakt", "text": "Israel: rund 10 Mio. Einwohner (Stand 2024)", "detail": "",
                    "basis": "allgemein", "quelle_url": None}}


def test_unsure_pattern_spots_fact_questions_not_opinions():
    from assist.live import _UNSURE
    for text in ("Wie viele Einwohner hat Israel eigentlich?", "Neun Millionen, glaube ich.",
                 "Das weiß ich jetzt gerade nicht.", "Da bin ich mir nicht ganz sicher.",
                 "Das müsste man mal nachschauen.", "Seit wann gilt das eigentlich?", "So um die zwanzig Prozent."):
        assert _UNSURE.search(text), text
    for text in ("Ich glaube, dass die Koalition das nicht durchhält.", "Das ist doch völlig absurd.",
                 "Und dann hat er einfach weitergemacht."):
        assert not _UNSURE.search(text), text


def test_knowledge_gap_gets_answer_without_dossier():
    async def main():
        clock = Clock(0.0)
        llm = FakeLLM([ANSWER])
        s, rec = make(clock, transcriber=FakeTranscriber(), llm=llm)
        await s.start()  # kein Sendeplan: früher gab es dann gar keine schnelle Prüfung
        clock.t = 13_000
        add_line(s, "Tobias", 9_000, 12_500, "Die Einwohnerzahl von Israel, wie viele sind das, weiß ich gerade nicht.")
        await asyncio.sleep(0.1)
        await s.close()
        return rec, llm
    rec, llm = run(main())
    assert len(llm.calls) == 1 and "Wissenslücke" in llm.calls[0]["messages"][0]["content"]
    card = rec["cards"][0]  # trotz gleicher Wörter wie im Gesagten: Eine Antwort wiederholt die Frage
    assert card["kind"] == "fakt" and card["basis"] == "allgemein" and card["quelle_url"] is None
    assert card["text"].startswith("Israel: rund 10 Mio.")


def test_check_still_rejects_plain_facts_that_are_neither_correction_nor_answer():
    async def main():
        clock = Clock(0.0)
        llm = FakeLLM([{**ANSWER, "art": "sonstiges"}])
        s, rec = make(clock, transcriber=FakeTranscriber(), llm=llm)
        await s.start()
        clock.t = 13_000
        add_line(s, "Tobias", 9_000, 12_500, "Wie viele Einwohner hat Israel eigentlich?")
        await asyncio.sleep(0.1)
        await s.close()
        return s, rec
    s, rec = run(main())
    assert rec["cards"] == [] and s.stats.cards_rejected == 1


def test_unknown_answer_is_looked_up_automatically_once():
    async def main():
        clock = Clock(0.0)
        ask = {"anlass": "ändert sich, nicht im Dossier", "art": "sonstiges", "nachschlagen": "Einwohnerzahl Israel aktuell", "karte": None}
        found = {"text": "Israel: 10,1 Mio. Einwohner (CBS, Ende 2025)", "detail": "", "quelle_url": "https://www.cbs.gov.il/bevoelkerung"}
        llm = FakeLLM([ask, found, ask, ask, {"text": "Nichts Belastbares gefunden: Mindestlohn Polen", "detail": "", "quelle_url": None}],
                      sources=["https://www.cbs.gov.il/bevoelkerung"])
        cfg = LiveConfig(poll_s=0.01, auto_lookup_min_gap_ms=20_000)
        s, rec = make(clock, transcriber=FakeTranscriber(), llm=llm, config=cfg)
        await s.start()
        s.set_plan(PLAN)
        clock.t = 13_000
        add_line(s, "Tobias", 9_000, 12_500, "Wie viele Einwohner hat Israel inzwischen, keine Ahnung.")
        await asyncio.sleep(0.15)
        first = list(rec["cards"])
        calls_after_first = len(llm.calls)
        # dieselbe Lücke noch einmal: geprüft wird, nachgeschlagen nicht (gleicher Begriff)
        clock.t = 60_000
        add_line(s, "Max", 56_000, 59_000, "Ja, wie viele sind das denn jetzt?")
        await asyncio.sleep(0.15)
        calls_after_second = len(llm.calls)
        # anderer Begriff, Suche ohne Beleg: keine Karte, nur eine Statuszeile
        llm.responses[0] = {**ask, "nachschlagen": "Mindestlohn Polen 2026"}
        clock.t = 110_000
        add_line(s, "Max", 106_000, 109_000, "Und der Mindestlohn in Polen, wie hoch ist der?")
        await asyncio.sleep(0.15)
        await s.close()
        return rec, llm, first, calls_after_first, calls_after_second
    rec, llm, first, c1, c2 = run(main())
    assert c1 == 2 and llm.calls[1]["tools"][0]["type"].startswith("web_search")
    assert [c["kind"] for c in first] == ["recherche"] and first[0]["basis"] == "netz"
    assert first[0]["quelle_url"] == "https://www.cbs.gov.il/bevoelkerung"
    assert c2 == 3  # nur die Prüfung, keine zweite Suche zum selben Begriff
    assert len(llm.calls) == 5 and len(rec["cards"]) == 1
    messages = [s["message"] for s in rec["status"]]
    assert "Schlägt nach: Einwohnerzahl Israel aktuell" in messages
    assert "Nichts Belastbares gefunden: Mindestlohn Polen 2026" in messages
    assert not any(p in m.lower() for m in messages for p in PROVIDERS)


# ------------------------------------------------------------------ Abhaken

def test_said_dossier_points_are_validated_deduped_and_reported():
    async def main():
        clock = Clock(0.0)
        said = []
        first = {"anlass": "x", "art": "sonstiges", "nachschlagen": None, "karte": None,
                 "genannt": [{"abschnitt": "s1", "art": "fakt", "nr": 1},      # gibt es
                             {"abschnitt": "s1", "art": "fakt", "nr": 7},      # Nummer gibt es nicht
                             {"abschnitt": "sX", "art": "frage", "nr": 1},     # Abschnitt gibt es nicht
                             {"abschnitt": "s1", "art": "quatsch", "nr": 1}]}
        again = {**first, "genannt": [{"abschnitt": "s1", "art": "fakt", "nr": 1}]}
        llm = FakeLLM([first, again])
        s, rec = make(clock, transcriber=FakeTranscriber(), llm=llm, on_said=said.append)
        await s.start()
        s.set_plan(PLAN)
        clock.t = 13_000
        add_line(s, "Max", 9_000, 12_500, "Das Rentenniveau liegt bei 48 Prozent.")
        await asyncio.sleep(0.1)
        clock.t = 30_000
        add_line(s, "Max", 26_000, 29_000, "Also 48 Prozent, wie gesagt.")
        await asyncio.sleep(0.1)
        await s.close()
        return s, said, llm
    s, said, llm = run(main())
    assert len(llm.calls) == 2
    assert said == [[{"segment_id": "s1", "art": "fakt", "nr": 1}]]  # einmal gemeldet, Ungültiges verworfen
    assert s.said == {("s1", "fakt", 1)}
    assert "Fakt 1: Rentenniveau 48 %" in s.plan_text
