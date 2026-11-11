"""Sprache der Meldungen und der erzeugten Inhalte auf der Serverseite.

Oberfläche (`ui.language`): Fehlermeldungen, Job-Meldungen, Benachrichtigungen, Standardnamen.
Inhalte (`show.language`): Prompts, Karten des Begleiters, Mitschrift, Transkript."""
from __future__ import annotations

import ast
import asyncio
import dataclasses
import io
import json
import re
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from app import core, i18n, jobs, manifest
from app.errors import ApiError
from tests.app_util import host_login

ROOT = Path(__file__).resolve().parent.parent
EN = {k: v for f in sorted((ROOT / "app" / "static" / "i18n").glob("en.*.json")) for k, v in json.loads(f.read_text(encoding="utf-8")).items()}
EN_WAVES: dict = {}  # früher eigene Dateien je Paket; EN umfasst inzwischen alle Wörterbücher


def set_ui(client, lang: str) -> None:
    r = client.put("/api/host/settings", json={"ui": {"language": lang}})
    assert r.status_code == 200, r.text


def make_episode(app, cfg, status="review", **fields):
    ep = core.current_episode(app.state.db, create=True)
    cols = {"status": status, "rec_start_server_ms": 1000, "rec_stop_server_ms": 61000, "title": "Pilot", **fields}
    core.update_episode(app.state.db, ep["id"], **cols)
    return ep["id"]


def add_job(app, eid, message, status="done", kind="import"):
    jid = "j" + str(abs(hash(message)) % 10**8)
    app.state.db.run("INSERT INTO jobs(id,episode_id,kind,status,progress,message,created_at) VALUES(?,?,?,?,?,?,?)",
                     (jid, eid, kind, status, 0.5, message, 1))
    return jid


# --- ApiError ---------------------------------------------------------------------------------------------------
def test_api_error_keeps_german_text_and_localizes_template():
    e = ApiError(409, "number_taken", "Die Folgennummer {number} ist schon vergeben", number=7)
    assert str(e) == e.message == "Die Folgennummer 7 ist schon vergeben"
    assert e.template == "Die Folgennummer {number} ist schon vergeben" and e.params == {"number": 7}
    assert e.localized("en") == "Episode number 7 is already taken"
    plain = ApiError(404, "episode_not_found", "Folge nicht gefunden")
    assert str(plain) == plain.message == "Folge nicht gefunden" and plain.localized("en") == "Episode not found"


def test_endpoint_errors_404_and_409_in_both_languages(client, app, cfg):
    eid = make_episode(app, cfg)
    app.state.db.run("INSERT INTO episodes(id,status,source,explicit,episode_type,guid,created_at,number) VALUES(?,?,?,?,?,?,?,?)",
                     ("e2", "published", "imported", 0, "full", "g2", 1, 7))
    r = client.get("/api/host/episodes/nope")
    assert r.status_code == 404 and r.json()["error"] == {"code": "episode_not_found", "message": "Folge nicht gefunden"}
    r = client.patch(f"/api/host/episodes/{eid}", json={"number": 7})
    assert r.status_code == 409 and r.json()["error"] == {"code": "number_taken", "message": "Die Folgennummer 7 ist schon vergeben"}

    set_ui(client, "en")
    r = client.get("/api/host/episodes/nope")
    assert r.status_code == 404 and r.json()["error"] == {"code": "episode_not_found", "message": "Episode not found"}
    r = client.patch(f"/api/host/episodes/{eid}", json={"number": 7})
    assert r.status_code == 409 and r.json()["error"] == {"code": "number_taken", "message": "Episode number 7 is already taken"}
    r = client.post("/api/host/episodes/nope/process")
    assert r.json()["error"]["message"] == "Episode not found"


def test_validation_and_http_errors_follow_language(client):
    bad = {"ui": {"language": "fr"}}
    r = client.put("/api/host/settings", json=bad)
    assert r.status_code == 422 and r.json()["error"]["message"] == "Ungültige Angaben: ui.language"
    assert client.get("/api/host/gibt-es-nicht").json()["error"] == {"code": "not_found", "message": "Nicht gefunden"}
    set_ui(client, "en")
    r = client.put("/api/host/settings", json=bad)
    assert r.status_code == 422 and r.json()["error"] == {"code": "invalid", "message": "Invalid input: ui.language"}
    assert client.get("/api/host/gibt-es-nicht").json()["error"]["message"] == "Not found"
    assert client.put("/api/host/settings", json={"show": {"title": ""}}).json()["error"]["message"] == "Invalid input: show.title"


def test_websocket_error_is_translated(client):
    token, _ = host_login(client)

    def stop():
        with client.websocket_connect(f"/ws/p?token={token}") as ws:
            ws.receive_json()
            ws.send_json({"type": "rec.stop"})
            for _ in range(20):
                msg = ws.receive_json()
                if msg["type"] == "error":
                    return msg
        raise AssertionError("keine Fehlermeldung")

    assert stop()["message"] == "Es läuft keine Aufnahme"
    set_ui(client, "en")
    msg = stop()
    assert msg["code"] == "not_recording" and msg["message"] == "No recording is running"


def test_login_messages_follow_language(cfg):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(dataclasses.replace(cfg, host_auth="password", host_password="geheim-genug"))
    with TestClient(app) as c:
        assert c.get("/api/host/settings").json()["error"]["message"] == "Bitte anmelden"
        app.state.db.set_setting("ui", {"language": "en"})
        assert c.get("/api/host/settings").json()["error"]["message"] == "Please sign in"
        r = c.post("/login", content="password=falsch", headers={"content-type": "application/x-www-form-urlencoded"})
        assert r.status_code == 401


# --- Muster und Wörterbuch ---------------------------------------------------------------------------------------------
def test_tr_exact_pattern_nested_and_unknown():
    assert i18n.tr("en", "Fertig") == "Done"
    assert i18n.tr("en", "3 Folgen übernommen, 2 schon vorhanden") == "3 episodes imported, 2 already present"
    assert i18n.tr("en", "12 Folgen übernommen, 0 schon vorhanden, 4 fehlgeschlagen (erneut importieren holt sie nach)") == \
        "12 episodes imported, 0 already present, 4 failed (importing again will fetch them)"
    assert i18n.tr("en", "Folge 3 von 10: Der Rest vom Fest") == "Episode 3 of 10: Der Rest vom Fest"  # das längere Muster gewinnt
    assert i18n.tr("en", "Unerwarteter Fehler: Zeitüberschreitung") == "Unexpected error: timeout"  # {detail} wird mitübersetzt
    assert i18n.tr("en", "Das gibt es in keinem Wörterbuch") == "Das gibt es in keinem Wörterbuch"
    assert i18n.tr("de", "3 Folgen übernommen, 2 schon vorhanden") == "3 Folgen übernommen, 2 schon vorhanden"
    assert i18n.content_lang("de-DE") == "de" and i18n.content_lang("en_GB") == "en" and i18n.content_lang(None) == "de"
    assert i18n.content_lang("English") == "de" and i18n.content_lang("FR") == "fr"


def test_pattern_entries_have_matching_placeholders():
    for de, en in EN.items():
        assert set(re.findall(r"\{(\w+)\}", de)) == set(re.findall(r"\{(\w+)\}", en)), de


def test_job_messages_translated_on_delivery_not_in_storage(client, app, cfg):
    eid = make_episode(app, cfg)
    plain = add_job(app, eid, "Abgebrochen, die Aufnahme wurde verworfen")
    counted = add_job(app, eid, "3 Folgen übernommen, 2 schon vorhanden")
    stage = add_job(app, eid, "Stimme 2 von 3: Analyse", status="running", kind="process")
    failed = add_job(app, eid, "Die Audio-Verarbeitung ist fehlgeschlagen (Code 1). Zu wenig Arbeitsspeicher für die Verarbeitung",
                     status="failed")

    def msgs():
        return {j: client.get(f"/api/host/jobs/{j}").json()["message"] for j in (plain, counted, stage, failed)}

    de = msgs()
    assert de[counted] == "3 Folgen übernommen, 2 schon vorhanden" and de[plain] == "Abgebrochen, die Aufnahme wurde verworfen"
    set_ui(client, "en")
    en = msgs()
    assert en[plain] == "Cancelled, the recording was discarded"
    assert en[counted] == "3 episodes imported, 2 already present"
    assert en[stage] == "Voice 2 of 3: analysis"
    assert en[failed] == "Audio processing failed (code 1). Not enough memory for processing"
    # in der Datenbank steht weiterhin Deutsch: ein Sprachwechsel wirkt sofort, auch rückwärts
    assert app.state.db.one("SELECT message FROM jobs WHERE id=?", (counted,))["message"] == "3 Folgen übernommen, 2 schon vorhanden"
    assert [j for j in client.get(f"/api/host/episodes/{eid}").json()["jobs"] if j["id"] == counted][0]["message"] == en[counted]
    set_ui(client, "de")
    assert msgs() == de


def test_report_warnings_and_segment_errors_are_translated(client, app, cfg):
    eid = make_episode(app, cfg, report={"warnings": ["Spur von Anna: keine Uhrenpunkte, Lage nur aus der Startzeit geschätzt"]})
    seg = client.get(f"/api/host/episodes/{eid}/segments").json()[0]
    app.state.db.run("UPDATE segments SET status='failed', error=? WHERE id=?",
                     ("Recherche fehlgeschlagen: Zeitüberschreitung", seg["id"]))
    set_ui(client, "en")
    assert client.get(f"/api/host/episodes/{eid}").json()["report"]["warnings"] == \
        ["Track of Anna: no clock points, position estimated from the start time only"]
    got = [s for s in client.get(f"/api/host/episodes/{eid}/segments").json() if s["id"] == seg["id"]][0]
    assert got["error"] == "Research failed: timeout"


def test_notifications_use_ui_language(app, cfg, monkeypatch):
    from app import ntfy

    sent = []
    monkeypatch.setattr(ntfy, "_post", lambda url, headers, body, auth: sent.append((headers, body)))
    app.state.cfg = dataclasses.replace(cfg, ntfy_url="https://ntfy.example")
    eid = make_episode(app, cfg, status="processing", title=None, number=4)
    runner = app.state.runner
    job = {"episode_id": eid, "kind": "process"}
    asyncio.run(runner._failed(job, "Unerwarteter Fehler: Zeitüberschreitung"))
    assert sent[-1][0]["Title"] == "Verarbeitung fehlgeschlagen"
    assert sent[-1][1] == "Folge 4: Unerwarteter Fehler: Zeitüberschreitung"
    app.state.db.set_setting("ui", {"language": "en"})
    core.update_episode(app.state.db, eid, status="processing")
    asyncio.run(runner._failed(job, "Unerwarteter Fehler: Zeitüberschreitung"))
    assert sent[-1][0]["Title"] == "Processing failed"
    assert sent[-1][1] == "Episode 4: Unexpected error: timeout"
    core.update_episode(app.state.db, eid, status="processing")
    asyncio.run(runner._to_review(eid, "ohne Transkript"))
    assert sent[-1][0]["Title"] == "Episode ready for review" and sent[-1][1] == "Episode 4 (without transcript)"


# --- Standardwerte ---------------------------------------------------------------------------------------------------------
def test_default_names_follow_ui_language_until_saved(client, app):
    db = app.state.db
    s = client.get("/api/host/settings").json()
    assert s["format"]["labels"]["blitzlicht"] == "Kurzthema" and s["show"]["title"] == "Mein Podcast"
    set_ui(client, "en")
    s = client.get("/api/host/settings").json()
    assert s["format"]["labels"] == {"blitzlicht": "Short topic", "hauptthema": "Main topic", "good": "Recommendation",
                                     "frei": "Free segment", "read": "Reading tip", "watch": "Watching tip",
                                     "listen": "Listening tip"}
    assert s["show"]["title"] == "My podcast" and core.host_first_name(db) == "Host"
    assert core.segment_chapter_title({"kind": "blitzlicht", "title": "Rail"}, core.get_format(db)) == "Short topic: Rail"
    # ein gespeicherter Name bleibt, wie er ist; die übrigen folgen weiter der Sprache
    r = client.put("/api/host/settings", json={"format": {"labels": {"good": "Tipp"}}, "show": {"author": "Anna"}})
    assert r.json()["format"]["labels"]["good"] == "Tipp" and r.json()["format"]["labels"]["blitzlicht"] == "Short topic"
    assert db.get_setting("format")["labels"] == {"good": "Tipp"}
    assert "title" not in db.get_setting("show")
    set_ui(client, "de")
    s = client.get("/api/host/settings").json()
    assert s["format"]["labels"]["good"] == "Tipp" and s["format"]["labels"]["blitzlicht"] == "Kurzthema"
    assert s["show"]["title"] == "Mein Podcast"
    client.put("/api/host/settings", json={"show": {"title": "Zwei am Mikro"}})
    set_ui(client, "en")
    assert client.get("/api/host/settings").json()["show"]["title"] == "Zwei am Mikro"


def test_first_permanent_invite_gets_name_in_ui_language(cfg, monkeypatch):
    from app.main import create_app

    monkeypatch.setenv("PS_LANG", "en")
    app = create_app(cfg)
    assert app.state.db.one("SELECT label FROM invites WHERE kind='permanent'")["label"] == "Co-host"


# --- Prompts: Sprache der Show -----------------------------------------------------------------------------------------------
def test_show_block_and_prompts_follow_show_language():
    from assist import prompts

    de = prompts.show_block({"title": "X", "language": "de"})
    en = prompts.show_block({"title": "X", "language": "en"})
    assert "Sprache der Show: Deutsch." in de and "Sprache der Show: Englisch." in en
    assert prompts.show_block({"title": "X"}) == de  # ohne Angabe Deutsch
    assert "Short topic" in en and "Kurzthema" in de  # Standardnamen der Abschnittsarten
    for lang_less in ({}, {"language": "de"}, {"language": "de-AT"}):
        assert prompts.prep_system(lang_less) == prompts.PREP_SYSTEM  # Deutsch: Wortlaut unverändert
        assert prompts.live_system(lang_less) == prompts.LIVE_SYSTEM
        assert prompts.lookup_system(lang_less) == prompts.LOOKUP_SYSTEM
        assert prompts.summary_system(lang_less) == prompts.SUMMARY_SYSTEM
    assert "Alles auf Deutsch, Zahlen im deutschen Format (1.234,5)" in prompts.prep_system({"language": "de"})
    prep = prompts.prep_system({"language": "en"})
    assert "Alles auf Englisch, Zahlen im englischen Format (1,234.5)" in prep and "Alles auf Deutsch" not in prep
    assert "deutschsprachigen" not in prep
    live = prompts.live_system({"language": "en-GB"})
    assert "Text beginnt mit „Correction:“" in live and "Englisch, Zahlen im englischen Format." in live
    assert "„Nothing reliable found: …“" in prompts.lookup_system({"language": "en"})
    fr = prompts.prep_system({"language": "fr"})
    assert "Alles auf Französisch" in fr and "Alles auf Deutsch" not in fr
    msg = prompts.live_user_message(mode="check", current=None, segment_elapsed_ms=None, now_ms=1000, summary="", lines=[],
                                    cards=[], host_name="Anna", show={"language": "en"})
    assert "mit „Correction:“ beginnt" in msg


def test_shownotes_prompt_follows_show_language():
    from publish import shownotes

    de = shownotes.build_prompt("t", {"title": "T", "description": "d"}, [], [])[0]
    en = shownotes.build_prompt("t", {"title": "T", "description": "d", "language": "en"}, [], [])[0]
    assert "Sprache: Deutsch. Die Vorschläge" in de
    assert "Sprache: Englisch" in en and "Zahlen im englischen Format" in en and "Sprache: Deutsch" not in en
    from publish.models import Show

    assert "Sprache: Englisch" in shownotes.build_prompt("t", Show(title="T", language="en-GB"), [], [])[0]


def test_assist_show_carries_show_language(client, app, cfg):
    db = app.state.db
    assert core.assist_show(db, cfg)["language"] == "de"
    client.put("/api/host/settings", json={"show": {"language": "en-GB"}})
    show = core.assist_show(db, cfg)
    assert show["language"] == "en" and show["format"]["labels"]["blitzlicht"] == "Short topic"
    set_ui(client, "de")  # Standardnamen der Prompts folgen der Show, nicht der Oberfläche
    assert core.assist_show(db, cfg)["format"]["labels"]["blitzlicht"] == "Short topic"
    assert core.get_format(db)["labels"]["blitzlicht"] == "Kurzthema"


# --- Live-Begleiter: Karten und Regeln je Sprache ------------------------------------------------------------------------------------
def _live(show=None, **kw):
    from assist.live import LiveConfig, LiveSession
    from tests.assist_fixtures.fakes import Clock, FakeLLM, FakeTranscriber

    clock = kw.pop("clock", None) or Clock(0.0)
    rec = {"cards": [], "status": []}
    s = LiveSession(rec_start_server_ms=0.0, server_clock=clock, on_card=rec["cards"].append, on_status=rec["status"].append,
                    config=LiveConfig(poll_s=0.01), transcriber=kw.pop("transcriber", FakeTranscriber()),
                    llm=kw.pop("llm", FakeLLM([])), show=show, **kw)
    return s, rec, clock


def _say(s, text, start=9_000, end=12_500):
    from assist.live import _Pending
    from assist.vad import Utterance

    u = Utterance("t1", "Max", start, end, np.zeros(10, np.float32), -30.0, 30.0, int(end - start))
    s._output(_Pending(u, text=text, ready_at=s.server_clock() - 10_000, done=True))


def test_english_correction_card_is_accepted_in_check_mode():
    from tests.assist_fixtures.fakes import FakeLLM

    async def main(card_text, art):
        card = {"anlass": "x", "art": art, "karte": {"kind": "fakt", "text": card_text, "detail": "", "basis": "allgemein",
                                                      "quelle_url": None}}
        s, rec, clock = _live({"language": "en"}, llm=FakeLLM([card]))
        await s.start()
        s.set_plan([{"id": "s1", "kind": "blitzlicht", "title": "Pensions", "dossier": {"begriffe": ["pension"]}}])
        clock.t = 13_000
        _say(s, "The pension level is 43 percent, I think.")
        await asyncio.sleep(0.1)
        await s.close()
        return s, rec

    s, rec = asyncio.run(main("Correction: official 2026 figures put it at 48 %, not 43 %", "korrektur"))
    assert [c["text"] for c in rec["cards"]] == ["Correction: official 2026 figures put it at 48 %, not 43 %"]
    assert "Correction:" in s.llm.calls[0]["messages"][0]["content"] and "Sprache der Show: Englisch" in str(s.llm.calls[0]["system"])
    # art == korrektur genügt, auch wenn der Text nicht mit dem Wort beginnt
    s, rec = asyncio.run(main("Official 2026 figures put it at 48 %, not 43 %", "korrektur"))
    assert len(rec["cards"]) == 1
    # englisches Wort ohne art, deutsches Wort ohne art: beides zählt; ein beliebiger Fakt nicht
    s, rec = asyncio.run(main("Correction: official 2026 figures put it at 48 %, not 43 %", "sonstiges"))
    assert len(rec["cards"]) == 1
    s, rec = asyncio.run(main("Korrektur: Rentenniveau 48 %, nicht 43 %", "sonstiges"))
    assert len(rec["cards"]) == 1
    s, rec = asyncio.run(main("Official 2026 figures put it at 48 % of average wages", "sonstiges"))
    assert rec["cards"] == [] and s.stats.cards_rejected == 1


def test_number_and_unsure_rules_by_language():
    from assist.live import patterns_for

    hallu, unsure, number = patterns_for("en")
    assert hallu.search("Thanks for watching") and not hallu.search("Untertitel im Auftrag des ZDF")
    assert number.search("about three million people") and number.search("48 percent") and not number.search("Zweihundert")
    assert unsure.search("How many people live there, no idea") and unsure.search("I think it was 2014") is not None
    assert not unsure.search("Thanks, that was a great point")
    h2, u2, n2 = patterns_for("de")
    assert h2.search("Untertitel im Auftrag des ZDF") and u2.search("Wie viele Einwohner hat das Land") and n2.search("dreiundvierzig Prozent")
    h3, u3, n3 = patterns_for("fr")  # andere Sprachen: Deutsch und Englisch zusammen
    assert h3.search("Thanks for watching") and h3.search("Untertitel") and n3.search("three million") and n3.search("tausend")
    assert u3.search("how many") and u3.search("wie viele")


def test_english_session_uses_english_number_rule_and_stt_prompt():
    from tests.assist_fixtures.fakes import FakeLLM

    s, rec, clock = _live({"language": "en", "title": "Two on a Mic"}, llm=FakeLLM([]))
    assert s.language == "en" and "English-language podcast “Two on a Mic”" in s._stt_prompt("Max")
    _say(s, "It was about three hundred people.")
    assert s.number_pending is True
    s2, _, _ = _live({"title": "Zwei am Mikro"})
    assert s2.language == "de" and s2._stt_prompt("Max").startswith("Deutschsprachiger Podcast „Zwei am Mikro“")
    _say(s2, "It was about three hundred people.")
    assert s2.number_pending is False  # englische Zahlwörter zählen bei deutscher Show nicht
    from assist.vad import Utterance

    u = Utterance("t", "Max", 0, 3000, np.zeros(10, np.float32), -30.0, 30.0, 3000)
    assert s._clean_text("Thanks for watching!", u) is None and s2._clean_text("Thanks for watching!", u) == "Thanks for watching!"


def test_lookup_fixed_text_in_show_language():
    from tests.assist_fixtures.fakes import FakeLLM

    async def main(lang):
        s, rec, clock = _live({"language": lang}, llm=FakeLLM([{"text": "irgendwas", "detail": "x", "quelle_url": None}]))
        await s.start()
        s.lookup("Mindestlohn Polen")
        await asyncio.sleep(0.15)
        await s.close()
        return rec

    assert asyncio.run(main("de"))["cards"][0]["text"] == "Nichts Belastbares gefunden: Mindestlohn Polen"
    assert asyncio.run(main("en"))["cards"][0]["text"] == "Nothing reliable found: Mindestlohn Polen"


# --- Mitschrift und Transkript ---------------------------------------------------------------------------------------------------------
def test_stt_gets_show_language(monkeypatch):
    import httpx

    from assist.live import LiveSession
    from assist.stt import OpenAITranscriber, from_env
    from tests.test_assist_stt import parse_multipart

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    assert from_env().language == "de" and from_env(language="en-GB").language == "en"
    s = LiveSession(show={"language": "en"}, llm=None)
    assert s.language == "en" and s.transcriber.language == "en"
    assert LiveSession(show={"title": "x"}, llm=None).transcriber.language == "de"
    assert LiveSession(show={"language": "en"}, language="fr", llm=None).transcriber.language == "fr"
    seen = {}

    def handler(req):
        seen["fields"], _ = parse_multipart(req)
        return httpx.Response(200, json={"text": "Hello"})

    t = OpenAITranscriber(api_key="k", language="en", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(t.transcribe(np.zeros(16000, np.float32), 16000))
    assert ("languages[]", "en") in seen["fields"]


def test_companion_passes_show_language_and_translates_status(cfg):
    from app.main import create_app

    app = create_app(dataclasses.replace(cfg, anthropic_key="a", openai_key="o"))
    db, comp = app.state.db, app.state.room.companion
    seen = []

    class Fake:
        def __init__(self, **kw):
            seen.append(kw)
            self.msg = "Schlägt nach: Einwohner Israel"

        async def start(self):
            pass

        def set_plan(self, segments):
            pass

        def set_current_segment(self, sid):
            pass

        def status(self):
            return {"on": True, "stt": "ok", "ai": "ok", "lag_ms": 5, "message": self.msg}

    comp.factory = Fake
    db.set_setting("assist", {"enabled": True})
    db.set_setting("show", {"language": "en-GB"})
    asyncio.run(comp._create("e1", 1000))
    assert seen[0]["language"] == "en" and seen[0]["show"]["language"] == "en"
    assert comp.status()["message"] == "Schlägt nach: Einwohner Israel"
    db.set_setting("ui", {"language": "en"})
    assert comp.status()["message"] == "Looking up: Einwohner Israel"
    comp.session.msg = "Mitschrift gestört: Zeitüberschreitung"
    assert comp.status()["message"] == "Live transcript disturbed: timeout"
    sent = []

    async def fake_send(msg):
        sent.append(msg)

    comp.room.send_hosts = fake_send
    asyncio.run(comp.report_error("Mitschrift", RuntimeError("x")))
    assert sent[-1]["message"] == "The live companion had an error (live transcript). The recording continues"


def test_manifest_and_transcript_carry_show_language(client, app, cfg, monkeypatch, tmp_path):
    eid = make_episode(app, cfg, title=None, number=3)
    assert manifest.build(app.state.db, cfg, eid)["language"] == "de"
    assert manifest.build(app.state.db, cfg, eid)["tags"]["title"] == "Folge 3"
    client.put("/api/host/settings", json={"show": {"language": "en-US"}})
    m = manifest.build(app.state.db, cfg, eid)
    assert m["language"] == "en" and m["tags"]["title"] == "Episode 3"

    import soundfile as sf

    from pipeline import transcribe
    from pipeline.util import Progress

    calls = []

    class Seg:
        start, end, text = 0.0, 1.0, " Hello there "

    class FakeModel:
        def __init__(self, *a, **kw):
            pass

        def transcribe(self, audio, **kw):
            calls.append(kw["language"])
            return [Seg()], None

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeModel))
    monkeypatch.setenv("PS_DATA", str(tmp_path / "pd"))

    def run(manifest_dict):
        work, out = tmp_path / "work", tmp_path / "out"
        (work / "stems").mkdir(parents=True, exist_ok=True)
        sf.write(str(work / "stems" / "p1.flac"), np.zeros(48000 * 2, np.float32) + 0.1, 48000)
        m = {"work_dir": str(work), "out_dir": str(out), "rec_start_server_ms": 0,
             "tracks": [{"id": "t1", "participant_id": "p1", "participant_name": "Anna"}], **manifest_dict}
        transcribe.run(m, Progress(stream=io.StringIO()))
        return json.loads((out / "transcript.json").read_text()), (out / "transcript.vtt").read_text()

    data, vtt = run({"language": "en"})
    assert calls[-1] == "en" and data["language"] == "en" and "<v Anna>Hello there" in vtt
    data, _ = run({})
    assert calls[-1] == "de" and data["language"] == "de"


# --- Vollständigkeit ------------------------------------------------------------------------------------------------------------------------
def _literal_messages() -> dict[str, list[str]]:
    """Alle festen Meldungen der `ApiError(...)`-Aufrufe (und der Datei-Meldungen von `_file`) in app/*.py."""
    found: dict[str, list[str]] = {}
    for path in sorted((ROOT / "app").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts = {n.targets[0].id: n.value.value for n in tree.body
                  if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                  and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)}
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Name):
                continue
            idx = {"ApiError": 2, "_file": 2}.get(n.func.id)
            if idx is None or len(n.args) <= idx:
                continue
            arg = n.args[idx]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.setdefault(arg.value, []).append(f"{path.name}:{n.lineno}")
            elif isinstance(arg, ast.Name) and arg.id in consts:
                found.setdefault(consts[arg.id], []).append(f"{path.name}:{n.lineno}")
            else:  # zusammengesetzte Meldungen (str(exc), Variablen) gehen über Muster oder `{detail}`
                found.setdefault("<dynamisch>", []).append(f"{path.name}:{n.lineno} {ast.unparse(arg)}")
    return found


def test_every_api_error_message_has_an_english_entry():
    found = _literal_messages()
    assert len(found) > 60  # der Sammler findet die Meldungen wirklich
    dynamic = found.pop("<dynamisch>", [])
    missing = {m: where for m, where in found.items() if m not in EN and m not in EN_WAVES}
    assert not missing, json.dumps(missing, ensure_ascii=False, indent=1)
    # nur die bekannten Fälle dürfen dynamisch sein: Text aus Ausnahmen anderer Pakete (Dateien, Cover, Speicher)
    assert all(any(k in d for k in ("str(exc)", "missing")) for d in dynamic), dynamic
    for msg in found:  # Platzhalter der festen Meldungen kommen im Wörterbuch genauso vor
        assert i18n.tr("en", msg, **{k: "x" for k in re.findall(r"\{(\w+)\}", msg)}) != msg
