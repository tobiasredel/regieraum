"""Sendeplan, Recherche, Raumzustand mit Plan, Abschnittswechsel, Live-Begleiter im Server (SPEC Abschnitte 14 und 15)."""
from __future__ import annotations

import asyncio
import dataclasses
import threading
import time

import pytest

from .app_util import auth, create_track, host_login, join_guest, put_chunk, recv_until, upload_sound


def _ws(client, token):
    return client.websocket_connect(f"/ws/p?token={token}")


def _kinds(client, eid):
    return [s["kind"] for s in client.get(f"/api/host/episodes/{eid}/segments").json()]


@pytest.fixture()
def keyed(cfg):
    """Konfiguration mit Schlüsseln für Recherche und Mitschrift."""
    return dataclasses.replace(cfg, anthropic_key="a-test", openai_key="o-test")


@pytest.fixture()
def kapp(keyed):
    from app.main import create_app

    return create_app(keyed)


@pytest.fixture()
def kclient(kapp):
    from fastapi.testclient import TestClient

    with TestClient(kapp) as c:
        yield c


def _draft(client) -> str:
    r = client.post("/api/host/episodes/draft")
    assert r.status_code == 200, r.text
    return r.json()["episode"]["id"]


# --- Sendeplan ---------------------------------------------------------------------------------------------
def test_draft_creates_episode_with_default_plan(client):
    r = client.post("/api/host/episodes/draft")
    ep = r.json()["episode"]
    assert ep["status"] == "draft" and ep["source"] == "recorded"
    segs = client.get(f"/api/host/episodes/{ep['id']}/segments").json()
    assert [s["kind"] for s in segs] == ["blitzlicht", "blitzlicht", "blitzlicht", "hauptthema", "good"]
    assert [s["sort"] for s in segs] == [0, 1, 2, 3, 4]
    assert segs[4]["good_type"] == "read" and segs[0]["good_type"] is None
    assert all(s["status"] == "empty" and s["dossier"] is None and s["notes"] == "" and s["visited_at"] is None for s in segs)
    assert _draft(client) == ep["id"]  # dieselbe Folge, kein zweiter Plan
    assert len(client.get(f"/api/host/episodes/{ep['id']}/segments").json()) == 5


def test_episode_created_by_studio_gets_default_plan(client):
    htoken, _ = host_login(client)
    create_track(client, htoken, 1_000)  # legt die Folge an wie das Studio
    ep = client.get("/api/host/episodes").json()[0]
    assert _kinds(client, ep["id"]) == ["blitzlicht"] * 3 + ["hauptthema", "good"]


def test_segment_crud_order_and_validation(client):
    eid = _draft(client)
    base = client.get(f"/api/host/episodes/{eid}/segments").json()
    new = client.post(f"/api/host/episodes/{eid}/segments", json={"kind": "frei", "title": " Extra ", "input": "x"})
    assert new.status_code == 201
    n = new.json()
    assert n["title"] == "Extra" and n["sort"] == 5 and n["kind"] == "frei" and n["good_type"] is None
    assert client.post(f"/api/host/episodes/{eid}/segments", json={"kind": "quatsch"}).status_code == 422
    assert client.post("/api/host/episodes/nix/segments", json={"kind": "frei"}).status_code == 404
    g = client.post(f"/api/host/episodes/{eid}/segments", json={"kind": "good", "good_type": "watch"}).json()
    assert g["good_type"] == "watch"

    p = client.patch(f"/api/host/segments/{n['id']}", json={"title": "Neu", "input": "Stichwort", "notes": "Eigene Notiz",
                                                            "good_type": "listen"})
    assert p.status_code == 200
    d = p.json()
    assert (d["title"], d["input"], d["notes"], d["good_type"]) == ("Neu", "Stichwort", "Eigene Notiz", None)  # good_type nur bei good
    assert client.patch(f"/api/host/segments/{g['id']}", json={"good_type": "listen"}).json()["good_type"] == "listen"
    assert client.patch("/api/host/segments/nix", json={"title": "x"}).status_code == 404

    # sort in PATCH setzt den Abschnitt an diese Stelle
    assert client.patch(f"/api/host/segments/{n['id']}", json={"sort": 0}).json()["sort"] == 0
    ids = [s["id"] for s in client.get(f"/api/host/episodes/{eid}/segments").json()]
    assert ids[0] == n["id"] and ids[1:6] == [s["id"] for s in base]

    # Reihenfolge per POST
    rev = list(reversed(ids))
    r = client.post(f"/api/host/episodes/{eid}/segments/order", json={"ids": rev})
    assert [s["id"] for s in r.json()] == rev
    assert [s["id"] for s in client.get(f"/api/host/episodes/{eid}/segments").json()] == rev
    assert client.post(f"/api/host/episodes/{eid}/segments/order", json={"ids": ["nix"]}).status_code == 422
    assert client.post(f"/api/host/episodes/{eid}/segments/order", json={"ids": [ids[0], ids[0]]}).status_code == 422
    # nicht genannte bleiben hinten
    client.post(f"/api/host/episodes/{eid}/segments/order", json={"ids": [ids[3]]})
    after = [s["id"] for s in client.get(f"/api/host/episodes/{eid}/segments").json()]
    assert after[0] == ids[3] and len(after) == len(ids)

    assert client.delete(f"/api/host/segments/{n['id']}").json() == {"deleted": True}
    left = client.get(f"/api/host/episodes/{eid}/segments").json()
    assert n["id"] not in [s["id"] for s in left] and [s["sort"] for s in left] == list(range(len(left)))
    assert client.delete(f"/api/host/segments/{n['id']}").status_code == 404


def test_deleting_episode_deletes_its_plan_and_cards(client, app):
    eid = _draft(client)
    app.state.db.run("INSERT INTO cards(id,episode_id,server_ms,kind,text) VALUES('c1',?,1,'frage','?')", (eid,))
    assert client.delete(f"/api/host/episodes/{eid}").json() == {"deleted": True}
    assert app.state.db.all("SELECT * FROM segments") == [] and app.state.db.all("SELECT * FROM cards") == []


def test_migration_keeps_data_and_seeds_existing_draft(tmp_path):
    import sqlite3

    from app import util
    from app.db import DB, MIGRATIONS

    path = tmp_path / "old" / "studio.db"
    path.parent.mkdir()
    c = sqlite3.connect(path)
    c.executescript("BEGIN;" + MIGRATIONS[0] + "PRAGMA user_version=1;COMMIT;")
    c.execute("INSERT INTO sounds(id,label,kind,ext,created_at) VALUES('s1','Alt','intro','wav',1)")
    c.execute("INSERT INTO episodes(id,status,guid,created_at) VALUES('e1','draft','g1',1)")
    c.execute("INSERT INTO episodes(id,status,guid,created_at) VALUES('e2','published','g2',1)")
    c.commit()
    c.close()
    db = DB(path)
    assert db.one("PRAGMA user_version")["user_version"] >= 2
    assert db.one("SELECT label, segment_kind FROM sounds") == {"label": "Alt", "segment_kind": None}
    assert [r["kind"] for r in db.all("SELECT kind FROM segments WHERE episode_id='e1' ORDER BY sort")] == \
        ["blitzlicht"] * 3 + ["hauptthema", "good"]
    assert db.all("SELECT * FROM segments WHERE episode_id='e2'") == []
    DB(path)  # zweiter Start ändert nichts
    assert len(db.all("SELECT * FROM segments")) == 5
    assert util.now_ms() > 0


def test_sound_segment_kind_everywhere(client):
    s = upload_sound(client, "Blitz", "segment", segment_kind="blitzlicht")
    assert s["segment_kind"] == "blitzlicht"
    plain = upload_sound(client, "Sting", "sting")
    assert plain["segment_kind"] is None
    bad = client.post("/api/host/sounds", files={"file": ("a.wav", b"x", "audio/wav")}, data={"label": "x", "kind": "sting", "segment_kind": "foo"})
    assert bad.status_code == 422
    lst = {x["id"]: x for x in client.get("/api/host/sounds").json()}
    assert lst[s["id"]]["segment_kind"] == "blitzlicht" and lst[plain["id"]]["segment_kind"] is None
    r = client.patch(f"/api/host/sounds/{plain['id']}", json={"segment_kind": "good"})
    assert r.json()["segment_kind"] == "good"
    assert client.patch(f"/api/host/sounds/{plain['id']}", json={"segment_kind": None}).json()["segment_kind"] is None
    assert client.patch(f"/api/host/sounds/{plain['id']}", json={"segment_kind": "frei"}).status_code == 422
    assert client.post("/api/host/sounds/order", json={"ids": [plain["id"], s["id"]]}).json()[1]["segment_kind"] == "blitzlicht"
    htoken, _ = host_login(client)
    with _ws(client, htoken) as h:
        room = h.receive_json()["room"]
    assert {x["id"]: x["segment_kind"] for x in room["sounds"]}[s["id"]] == "blitzlicht"


# --- Recherche ------------------------------------------------------------------------------------------------
def _wait_status(client, sid, want, eid, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        seg = next(s for s in client.get(f"/api/host/episodes/{eid}/segments").json() if s["id"] == sid)
        if seg["status"] in want:
            return seg
        time.sleep(0.02)
    raise AssertionError(f"Status {want} nicht erreicht: {seg['status']}")


def test_research_without_key_is_503(client):
    eid = _draft(client)
    sid = client.get(f"/api/host/episodes/{eid}/segments").json()[0]["id"]
    client.patch(f"/api/host/segments/{sid}", json={"title": "Thema"})
    r = client.post(f"/api/host/segments/{sid}/research")
    assert r.status_code == 503 and r.json()["error"]["code"] == "no_ai" and "Schlüssel" in r.json()["error"]["message"]


def test_research_success_failure_and_no_double_start(kclient, kapp, monkeypatch):
    from assist import prep
    from assist.llm import AssistError

    client = kclient
    kapp.state.db.run("INSERT INTO episodes(id,status,guid,created_at,title) VALUES('old','published','g-old',1,'Alte Folge')")
    eid = _draft(client)
    segs = client.get(f"/api/host/episodes/{eid}/segments").json()
    b1, b2, haupt, good = segs[0], segs[1], segs[3], segs[4]
    client.patch(f"/api/host/segments/{b1['id']}", json={"title": "Parken", "input": "teuer"})
    client.patch(f"/api/host/segments/{haupt['id']}", json={"title": "Verkehrswende"})
    assert client.post(f"/api/host/segments/{b2['id']}/research").status_code == 422  # nichts eingetragen
    assert client.post("/api/host/segments/nix/research").status_code == 404

    calls, gate = [], threading.Event()

    async def fake(segment, show=None, previous_titles=None, plan_context=None, *, on_progress=None, **kw):
        calls.append({"segment": segment, "show": show, "previous": previous_titles, "context": plan_context})
        if on_progress:
            on_progress(0.5, "Sucht: Parken")
        while not gate.is_set():
            await asyncio.sleep(0.01)
        if segment["id"] == good["id"]:
            raise AssistError("Recherche fehlgeschlagen: Der Dienst ist nicht erreichbar")
        return {"lage": "Lage", "fakten": [], "quellen": [], "begriffe": ["Begriff"]}

    monkeypatch.setattr(prep, "research_async", fake)
    r = client.post(f"/api/host/segments/{b1['id']}/research")
    assert r.status_code == 202 and r.json()["status"] == "researching"
    again = client.post(f"/api/host/segments/{b1['id']}/research")  # läuft schon: nicht doppelt starten
    assert again.status_code == 202 and again.json()["status"] == "researching"
    cur = _wait_status(client, b1["id"], {"researching"}, eid)
    assert cur["message"] == "Sucht: Parken" and cur["progress"] == 0.5
    client.post(f"/api/host/segments/{good['id']}/research")  # good darf ohne Eingabe (Kandidaten)
    gate.set()
    done = _wait_status(client, b1["id"], {"ready", "failed"}, eid)
    assert done["status"] == "ready" and done["dossier"]["begriffe"] == ["Begriff"] and done["error"] is None
    assert done["title"] == "Parken" and done["input"] == "teuer"  # Eingaben bleiben unangetastet
    failed = _wait_status(client, good["id"], {"ready", "failed"}, eid)
    assert failed["status"] == "failed" and failed["error"] == "Recherche fehlgeschlagen: Der Dienst ist nicht erreichbar"
    assert [c["segment"]["id"] for c in calls].count(b1["id"]) == 1
    c = next(c for c in calls if c["segment"]["id"] == b1["id"])
    assert c["segment"] == {"id": b1["id"], "kind": "blitzlicht", "title": "Parken", "input": "teuer", "good_type": None}
    assert c["previous"] == ["Alte Folge"] and c["show"]["title"] == "Mein Podcast" and c["show"]["format"]["labels"]["blitzlicht"] == "Kurzthema"
    ctx = {x["id"]: x for x in c["context"]}
    assert b1["id"] not in ctx and ctx[haupt["id"]] == {"id": haupt["id"], "kind": "hauptthema", "title": "Verkehrswende", "input": "", "good_type": None}
    # erneut starten ist nach Fehler möglich
    r = client.post(f"/api/host/segments/{good['id']}/research")
    assert r.status_code == 202
    _wait_status(client, good["id"], {"failed"}, eid)


def test_research_unexpected_error_gets_german_message(kclient, monkeypatch):
    from assist import prep

    async def boom(*a, **k):
        raise RuntimeError("intern")

    monkeypatch.setattr(prep, "research_async", boom)
    eid = _draft(kclient)
    sid = kclient.get(f"/api/host/episodes/{eid}/segments").json()[0]["id"]
    kclient.patch(f"/api/host/segments/{sid}", json={"title": "x"})
    kclient.post(f"/api/host/segments/{sid}/research")
    seg = _wait_status(kclient, sid, {"failed"}, eid)
    assert "unerwartet" in seg["error"] and "intern" not in seg["error"]


def test_research_limit_four_parallel(kclient, monkeypatch):
    from assist import prep

    running, peak, gate = 0, 0, threading.Event()

    async def fake(segment, *a, **k):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        while not gate.is_set():
            await asyncio.sleep(0.01)
        running -= 1
        return {"lage": "x"}

    monkeypatch.setattr(prep, "research_async", fake)
    eid = _draft(kclient)
    for _ in range(2):
        kclient.post(f"/api/host/episodes/{eid}/segments", json={"kind": "frei", "title": "extra"})
    segs = kclient.get(f"/api/host/episodes/{eid}/segments").json()
    assert len(segs) == 7
    for s in segs:
        kclient.patch(f"/api/host/segments/{s['id']}", json={"title": "t"})
        assert kclient.post(f"/api/host/segments/{s['id']}/research").status_code == 202
    time.sleep(0.3)
    assert peak == 4
    gate.set()
    for s in segs:
        _wait_status(kclient, s["id"], {"ready"}, eid)


def test_stale_researching_becomes_failed_on_start(cfg):
    from app.db import DB
    from app.main import create_app

    db = DB(cfg.db_path)
    db.run("INSERT INTO segments(id,episode_id,kind,status,updated_at) VALUES('s1','e','blitzlicht','researching',1)")
    db.run("INSERT INTO segments(id,episode_id,kind,status,updated_at) VALUES('s2','e','blitzlicht','ready',1)")
    create_app(cfg)
    s1, s2 = db.one("SELECT * FROM segments WHERE id='s1'"), db.one("SELECT * FROM segments WHERE id='s2'")
    assert s1["status"] == "failed" and "Neustart" in s1["error"] and s2["status"] == "ready"


# --- Raumzustand ---------------------------------------------------------------------------------------------------
def test_room_state_plan_only_for_host(client):
    eid = _draft(client)
    htoken, _ = host_login(client)
    gtoken, _ = join_guest(client)
    with _ws(client, htoken) as h, _ws(client, gtoken) as g:
        hw, gw = h.receive_json(), g.receive_json()
        assert [p["kind"] for p in hw["room"]["plan"]] == ["blitzlicht"] * 3 + ["hauptthema", "good"]
        assert set(hw["room"]["plan"][0]) >= {"id", "kind", "title", "status", "visited"}
        assert hw["room"]["plan"][0]["visited"] is False and hw["room"]["current_segment_id"] is None
        assert "assist" in hw and hw["assist"]["cards"] == [] and hw["assist"]["status"]["on"] is False
        assert "plan" not in gw["room"] and "current_segment_id" not in gw["room"] and "assist" not in gw
        # Änderungen am Plan: Host bekommt den neuen Raumzustand, Gast ohne Plan
        sid = hw["room"]["plan"][0]["id"]
        client.patch(f"/api/host/segments/{sid}", json={"title": "Neuer Titel"})
        room = None
        for _ in range(10):
            m = h.receive_json()
            if m["type"] == "room" and m["room"]["plan"][0]["title"] == "Neuer Titel":
                room = m["room"]
                break
        assert room is not None
        for _ in range(10):  # der Gast bekommt Raumzustände, aber nie mit Plan
            m = g.receive_json()
            if m["type"] == "room":
                assert "plan" not in m["room"] and "current_segment_id" not in m["room"]
                break
    assert eid


def test_room_state_without_tracks_still_has_plan_from_draft(client):
    _draft(client)
    htoken, _ = host_login(client)
    with _ws(client, htoken) as h:
        room = h.receive_json()["room"]
    assert room["episode"]["status"] == "draft" and room["peers"][0]["track"] is None and len(room["plan"]) == 5


def test_room_state_has_rec_stop_while_uploading(client, clock):
    htoken, _ = host_login(client)
    with _ws(client, htoken) as h:
        h.receive_json()
        create_track(client, htoken, clock.now())
        h.send_json({"type": "rec.start"})
        recv_until(h, "rec.start")
        clock.advance(5000)
        h.send_json({"type": "rec.stop"})
        stop = recv_until(h, "rec.stop")
        room = recv_until(h, "room")["room"]
    assert room["episode"]["status"] == "uploading" and room["episode"]["rec_stop_server_ms"] >= stop["at"]
    with _ws(client, htoken) as h2:  # wer rec.stop verpasst hat
        assert h2.receive_json()["room"]["episode"]["rec_stop_server_ms"] >= stop["at"]


# --- Abschnittswechsel und Kapitel ---------------------------------------------------------------------------------
def test_segment_switch_by_click_and_by_sound_with_chapter_titles(client, app, clock, cfg):
    from app import manifest

    db = app.state.db
    # eigene Namen für die Abschnittsarten: Kapitel und Raumzustand tragen sie
    assert client.put("/api/host/settings", json={"format": {"labels": {"blitzlicht": "Blitzlicht", "watch": "Good Watch"}}}).status_code == 200
    eid = _draft(client)
    segs = client.get(f"/api/host/episodes/{eid}/segments").json()
    titles = ["Parken", "Radwege", "Bahn", "Verkehrswende", "Die Akte Amtsstube"]
    for s, t in zip(segs, titles):
        client.patch(f"/api/host/segments/{s['id']}", json={"title": t})
    client.patch(f"/api/host/segments/{segs[4]['id']}", json={"good_type": "watch"})
    blitz = upload_sound(client, "Blitz", "segment", segment_kind="blitzlicht", chapter_title="Altname")
    haupt = upload_sound(client, "Haupt", "segment", segment_kind="hauptthema")
    good = upload_sound(client, "Good", "segment", segment_kind="good")
    plain = upload_sound(client, "Plain", "segment", chapter_title="Politik")
    intro = upload_sound(client, "Intro", "intro", segment_kind="blitzlicht")
    htoken, _ = host_login(client)
    gtoken, _ = join_guest(client)
    T0 = clock.now()
    with _ws(client, htoken) as h, _ws(client, gtoken) as g:
        h.receive_json(), g.receive_json()
        # Klick auf einen Abschnitt
        h.send_json({"type": "assist.segment", "segment_id": segs[1]["id"]})
        assert recv_until(h, "assist.segment")["segment_id"] == segs[1]["id"]
        room = h.receive_json()
        while room["type"] != "room":
            room = h.receive_json()
        assert room["room"]["current_segment_id"] == segs[1]["id"]
        assert [p["visited"] for p in room["room"]["plan"]] == [False, True, False, False, False]
        h.send_json({"type": "assist.segment", "segment_id": "nix"})
        assert recv_until(h, "error")["code"] == "segment_not_found"
        h.send_json({"type": "assist.segment", "segment_id": segs[1]["id"]})  # wieder zurück: Blitzlicht 2 gilt als besucht
        recv_until(h, "assist.segment")

        # Vorhören (keine Aufnahme) springt nie
        h.send_json({"type": "sound.play", "sound_id": haupt["id"]})
        recv_until(h, "sound.play")
        assert db.one("SELECT visited_at FROM segments WHERE id=?", (segs[3]["id"],))["visited_at"] is None

        create_track(client, htoken, T0)
        h.send_json({"type": "rec.start"})
        recv_until(h, "rec.start")
        clock.advance(2000)
        events = {}
        for name, snd in (("b1", blitz), ("b2", blitz), ("h", haupt), ("g", good), ("p", plain), ("i", intro)):
            clock.advance(1000)
            h.send_json({"type": "sound.play", "sound_id": snd["id"]})
            events[name] = recv_until(h, "sound.play")["event_id"]
            clock.advance(snd["duration_ms"] + 500)
        # Blitzlicht 2 war schon per Klick besucht: der Jingle springt auf Blitzlicht 1, dann 3, danach nirgends mehr
        pay = {e["id"]: e["payload"] for e in __import__("app.core", fromlist=["x"]).events_of(db, eid)}
        assert pay[events["b1"]] == {"segment_id": segs[0]["id"]}
        assert pay[events["b2"]] == {"segment_id": segs[2]["id"]}
        assert pay[events["h"]] == {"segment_id": segs[3]["id"]} and pay[events["g"]] == {"segment_id": segs[4]["id"]}
        assert pay[events["p"]] == {} and pay[events["i"]] == {}  # ohne segment_kind bzw. alle Abschnitte besucht
        vis = db.all("SELECT visited_at FROM segments ORDER BY sort")
        assert all(v["visited_at"] is not None for v in vis)
        assert h  # Host-Verbindung blieb offen

    ep = client.get(f"/api/host/episodes/{eid}").json()
    got = {c["event_id"]: c["title"] for c in ep["chapters_preview"]}
    assert got[events["b1"]] == "Blitzlicht: Parken" and got[events["b2"]] == "Blitzlicht: Bahn"
    assert got[events["h"]] == "Verkehrswende" and got[events["g"]] == "Good Watch: Die Akte Amtsstube"
    assert got[events["p"]] == "Politik" and got[events["i"]] == "Intro"
    # Manifest nennt dieselben Titel
    m = manifest.build(db, cfg, eid)
    assert {s["event_id"]: s["chapter_title"] for s in m["sounds"]} == got
    # Host-Umbenennung hat Vorrang, ein leerer Abschnittstitel fällt auf den Sound zurück
    client.patch(f"/api/host/episodes/{eid}", json={"chapter_titles": {events["h"]: "Mein Titel"}})
    client.patch(f"/api/host/segments/{segs[0]['id']}", json={"title": ""})
    got = {c["event_id"]: c["title"] for c in client.get(f"/api/host/episodes/{eid}").json()["chapters_preview"]}
    assert got[events["h"]] == "Mein Titel" and got[events["b1"]] == "Altname"
    assert {s["event_id"]: s["chapter_title"] for s in manifest.build(db, cfg, eid)["sounds"]}[events["h"]] == "Mein Titel"


def test_guest_gets_no_assist_messages_and_cannot_send_them(client):
    htoken, _ = host_login(client)
    gtoken, _ = join_guest(client)
    with _ws(client, gtoken) as g:
        g.receive_json()
        for t in ("assist.toggle", "assist.pulse", "assist.segment", "assist.lookup", "assist.card"):
            g.send_json({"type": t, "on": True, "query": "x", "id": "x", "segment_id": "x"})
            assert recv_until(g, "error")["code"] == "forbidden"
    assert client.get("/api/host/settings").json()["assist"] == {"enabled": False}


# --- Live-Begleiter -------------------------------------------------------------------------------------------------
class FakeLive:
    instances: list = []

    def __init__(self, **kw):
        self.kw = kw
        self.calls = []
        self.chunks = []
        self.started = self.closed = self.ended = 0
        self.boom = None
        self.emitted = False
        FakeLive.instances.append(self)

    async def start(self):
        self.started += 1

    def set_plan(self, segments):
        self.calls.append(("plan", segments))

    def set_current_segment(self, sid):
        self.calls.append(("segment", sid))

    def set_paused(self, p):
        self.calls.append(("paused", p))

    def pulse(self):
        self.calls.append(("pulse",))

    def lookup(self, q):
        self.calls.append(("lookup", q))

    def feed_chunk(self, track_id, speaker, sr, fmt, first_frame, pcm, tss):
        if self.boom:
            raise self.boom
        self.chunks.append({"track": track_id, "speaker": speaker, "sr": sr, "fmt": fmt, "first": first_frame, "len": len(pcm),
                            "tss": tss})
        if not self.emitted:
            self.emitted = True
            loop = asyncio.get_running_loop()
            loop.create_task(self.kw["on_line"]({"speaker": speaker, "start_ms": 100, "end_ms": 900, "text": "Hallo Welt"}))
            loop.create_task(self.kw["on_card"]({"id": "card1", "kind": "frage", "text": "Und wer zahlt das?", "detail": None,
                                                 "basis": "allgemein", "quelle_url": None, "segment_id": None, "server_ms": 5}))

    def end_of_audio(self):
        self.ended += 1

    async def drain(self, timeout=15.0):
        return None

    async def close(self):
        self.closed += 1

    def summary_stats(self):
        return {"stt_cost_usd": 0.3, "llm_cost_usd": 0.12}

    def status(self):
        return {"on": True, "stt": "ok", "ai": "ok", "lag_ms": 1200, "message": ""}


@pytest.fixture()
def live(kapp):
    FakeLive.instances = []
    comp = kapp.state.room.companion
    comp.factory = FakeLive
    comp.grace_s = 0
    return comp


def _assist_on(client):
    r = client.put("/api/host/settings", json={"assist": {"enabled": True}})
    assert r.status_code == 200 and r.json()["assist"] == {"enabled": True}


def test_live_lifecycle_chunks_pause_stop_and_cards(kclient, kapp, live, clock):
    client = kclient
    _assist_on(client)
    eid = _draft(client)
    segs = client.get(f"/api/host/episodes/{eid}/segments").json()
    htoken, hp = host_login(client)
    gtoken, _ = join_guest(client, "Max")
    T0 = clock.now()
    with _ws(client, htoken) as h, _ws(client, gtoken) as g:
        h.receive_json(), g.receive_json()
        ht = create_track(client, htoken, T0 - 1000)
        gt = create_track(client, gtoken, T0 + 1500)
        # Chunk vor rec.start: kein Begleiter, kein Fehler
        assert put_chunk(client, htoken, ht, 0, frames=48000).status_code == 200
        assert FakeLive.instances == []

        h.send_json({"type": "rec.start"})
        st = recv_until(h, "assist.status")  # die Sitzung steht, bevor rec.start ankommt
        assert st["on"] is True
        start = recv_until(h, "rec.start")["start_at"]
        recv_until(g, "rec.start")
        (fl,) = FakeLive.instances
        assert fl.started == 1 and fl.kw["rec_start_server_ms"] == start and str(fl.kw["work_dir"]).endswith(f"{eid}/work")
        assert fl.calls[0][0] == "plan" and [s["id"] for s in fl.calls[0][1]] == [s["id"] for s in segs]
        assert set(fl.calls[0][1][0]) >= {"id", "kind", "title", "input", "notes", "dossier"}

        # Chunk, der komplett vor rec.start liegt, wird nicht weitergegeben; einer mittendrin wird gekürzt
        clock.advance(1500)
        assert put_chunk(client, htoken, ht, 1, frames=24000, first_frame=48000).status_code == 200  # Ende bei T0+500 < start
        assert fl.chunks == []
        r = put_chunk(client, htoken, ht, 2, frames=96000, first_frame=60000)  # beginnt T0+250, start = T0+1500
        assert r.status_code == 200
        (c,) = fl.chunks
        # 1250 ms vor Start = 60000 Frames zu überspringen: übrig bleiben 36000 Frames ab Frame 120000
        assert c["first"] == 60000 + 60000 and c["len"] == 36000 * 3 and c["tss"] == (T0 - 1000) - start
        assert c["speaker"] == hp["name"] and c["sr"] == 48000 and c["fmt"] == "s24le"
        put_chunk(client, gtoken, gt, 0, frames=1000)
        assert fl.chunks[-1]["tss"] == 0 and fl.chunks[-1]["track"] == gt

        # Zeile und Karte: nur beim Host
        line = recv_until(h, "assist.line")
        assert line["text"] == "Hallo Welt" and line["speaker"] == hp["name"]
        card = recv_until(h, "assist.card")
        assert card["id"] == "card1" and card["pinned"] is False and card["dismissed"] is False
        row = kapp.state.db.one("SELECT * FROM cards")
        assert row["episode_id"] == eid and row["text"] == "Und wer zahlt das?" and row["pinned"] == 0
        h.send_json({"type": "assist.card", "id": "card1", "pinned": True})
        assert recv_until(h, "assist.card")["pinned"] is True
        assert kapp.state.db.one("SELECT pinned, dismissed FROM cards") == {"pinned": 1, "dismissed": 0}
        h.send_json({"type": "assist.card", "id": "nix", "pinned": True})
        assert recv_until(h, "error")["code"] == "card_not_found"

        # Plan- und Abschnittsänderungen gehen an die Sitzung
        client.patch(f"/api/host/segments/{segs[0]['id']}", json={"title": "Neu"})
        assert [x for x in fl.calls if x[0] == "plan"][-1][1][0]["title"] == "Neu"
        h.send_json({"type": "assist.segment", "segment_id": segs[2]["id"]})
        recv_until(h, "assist.segment")
        assert ("segment", segs[2]["id"]) in fl.calls

        # Pause und Weiter, Impuls, Nachschlagen
        h.send_json({"type": "rec.pause"})
        recv_until(h, "rec.pause")
        h.send_json({"type": "rec.resume"})
        recv_until(h, "rec.resume")
        assert [x for x in fl.calls if x[0] == "paused"] == [("paused", True), ("paused", False)]
        h.send_json({"type": "assist.pulse"})
        h.send_json({"type": "assist.lookup", "query": " Parkgebühren "})
        h.send_json({"type": "assist.lookup", "query": " "})
        assert recv_until(h, "error")["code"] == "invalid"
        assert ("pulse",) in fl.calls and ("lookup", "Parkgebühren") in fl.calls

        # Stopp: Ende der Mitschrift, danach schließen; späte Chunks der Spur werden angenommen, solange offen
        h.send_json({"type": "rec.stop"})
        recv_until(h, "rec.stop")
        end = time.time() + 5
        while fl.closed < 1 and time.time() < end:
            time.sleep(0.02)
        assert fl.ended >= 2 and fl.closed == 1  # sofort beim Stopp und nach der Nachlaufzeit
        end = time.time() + 5
        while not kapp.state.db.all("SELECT * FROM costs") and time.time() < end:
            time.sleep(0.02)
        assert [(r["kind"], r["episode_id"], r["usd"]) for r in kapp.state.db.all("SELECT * FROM costs")] == [("companion", eid, 0.42)]

        # Reconnect des Hosts: Status und nicht weggeklickte Karten im welcome
    with _ws(client, htoken) as h2:
        w = h2.receive_json()
        assert [c["id"] for c in w["assist"]["cards"]] == ["card1"] and w["assist"]["cards"][0]["pinned"] is True
        h2.send_json({"type": "assist.card", "id": "card1", "dismissed": True})
        recv_until(h2, "assist.card")
    with _ws(client, htoken) as h3:
        assert h3.receive_json()["assist"]["cards"] == []
    # Gast: nie assist.*-Nachrichten (Zählung über die mitgeschnittenen Gast-Nachrichten)


def test_guest_never_receives_assist_messages(kclient, kapp, live, clock):
    client = kclient
    _assist_on(client)
    htoken, _ = host_login(client)
    gtoken, _ = join_guest(client, "Max")
    T0 = clock.now()
    with _ws(client, htoken) as h, _ws(client, gtoken) as g:
        h.receive_json(), g.receive_json()
        ht = create_track(client, htoken, T0 + 1500)  # Sample 0 genau bei rec.start
        h.send_json({"type": "rec.start"})
        recv_until(h, "rec.start")
        clock.advance(1600)
        put_chunk(client, htoken, ht, 0, frames=48000)
        recv_until(h, "assist.card")
        h.send_json({"type": "rec.stop"})
        recv_until(h, "rec.stop")
        client.put("/api/host/settings", json={"assist": {"enabled": False}})
        # Der Gast liest alles, was bisher ankam: nie ein assist.*
        seen = []
        g.send_json({"type": "sync", "t0": 1})
        while True:
            m = g.receive_json()
            seen.append(m["type"])
            if m["type"] == "sync":
                break
        assert not [t for t in seen if t.startswith("assist.")]


def test_exception_in_companion_does_not_disturb_upload(kclient, kapp, live, clock):
    client = kclient
    _assist_on(client)
    htoken, _ = host_login(client)
    T0 = clock.now()
    with _ws(client, htoken) as h:
        h.receive_json()
        ht = create_track(client, htoken, T0 + 1500)
        h.send_json({"type": "rec.start"})
        recv_until(h, "rec.start")
        clock.advance(1600)
        (fl,) = FakeLive.instances
        fl.boom = RuntimeError("Attrappe kaputt")
        r = put_chunk(client, htoken, ht, 0, frames=1000)
        assert r.status_code == 200 and r.json() == {"received": 1}
        st = recv_until(h, "assist.status")
        while "Fehler" not in st["message"]:
            st = recv_until(h, "assist.status")
        assert st["stt"] == "fehler" and "Aufnahme läuft weiter" in st["message"] and "Attrappe" not in st["message"]
        assert kapp.state.db.one("SELECT COUNT(*) AS n FROM chunks")["n"] == 1
        fl.boom = None
        assert put_chunk(client, htoken, ht, 1, frames=1000).status_code == 200
        # kaputte Steuerung (set_paused wirft) stört Aufnahmesteuerung nicht
        fl.set_paused = lambda p: (_ for _ in ()).throw(RuntimeError("x"))
        h.send_json({"type": "rec.pause"})
        assert recv_until(h, "rec.pause")["at"]
        h.send_json({"type": "rec.resume"})
        recv_until(h, "rec.resume")
        h.send_json({"type": "rec.stop"})
        recv_until(h, "rec.stop")
    assert kapp.state.db.one("SELECT status FROM episodes")["status"] == "uploading"


def test_factory_failure_does_not_block_recording(kclient, kapp, live, clock):
    def broken(**kw):
        raise RuntimeError("kein Paket")

    live.factory = broken
    _assist_on(kclient)
    htoken, _ = host_login(kclient)
    with _ws(kclient, htoken) as h:
        h.receive_json()
        create_track(kclient, htoken, clock.now())
        h.send_json({"type": "rec.start"})
        st = recv_until(h, "assist.status")
        assert st["stt"] == "fehler"
        recv_until(h, "rec.start")
    assert kapp.state.db.one("SELECT status FROM episodes")["status"] == "recording"


def test_toggle_during_recording_and_disabled_by_default(kclient, kapp, live, clock):
    client = kclient
    htoken, _ = host_login(client)
    T0 = clock.now()
    with _ws(client, htoken) as h:
        h.receive_json()
        create_track(client, htoken, T0)
        h.send_json({"type": "rec.start"})
        recv_until(h, "rec.start")
        assert FakeLive.instances == []  # standardmäßig aus
        h.send_json({"type": "assist.toggle", "on": True})
        st = recv_until(h, "assist.status")
        assert st["on"] is True
        (fl,) = FakeLive.instances
        assert fl.started == 1
        h.send_json({"type": "assist.toggle", "on": False})
        st = recv_until(h, "assist.status")
        while st["on"]:
            st = recv_until(h, "assist.status")
        assert fl.closed == 1 and client.get("/api/host/settings").json()["assist"] == {"enabled": False}
        h.send_json({"type": "assist.pulse"})
        assert "nur während" in recv_until(h, "assist.status")["message"]


def test_no_openai_key_keeps_companion_off(client, app, clock):
    htoken, _ = host_login(client)
    with _ws(client, htoken) as h:
        h.receive_json()
        h.send_json({"type": "assist.toggle", "on": True})
        st = recv_until(h, "assist.status")
        assert st["on"] is True and st["stt"] == "aus" and "Schlüssel" in st["message"]
        create_track(client, htoken, clock.now())
        h.send_json({"type": "rec.start"})
        recv_until(h, "rec.start")
    assert not app.state.room.companion.session
    st = client.get("/api/host/status").json()
    assert st["stt"] == {"configured": False, "model": "gpt-transcribe"} and st["assist"]["available"] is False
    assert st["assist"]["enabled"] is True


def test_status_shows_stt_and_assist(kclient):
    st = kclient.get("/api/host/status").json()
    assert st["stt"] == {"configured": True, "model": "gpt-transcribe"}
    assert st["assist"] == {"enabled": False, "available": True, "active": False}


# --- Kleinigkeiten ----------------------------------------------------------------------------------------------
def test_static_scripts_and_styles_revalidate(client):
    r = client.get("/static/js/manage/common.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache" and r.headers.get("etag")
    assert client.get("/static/js/manage/common.js", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    c = client.get("/static/css/app.css")
    assert c.status_code == 200 and c.headers["cache-control"] == "no-cache"
    assert "no-cache" not in client.get("/static/design/app-basis.css").headers.get("cache-control", "")
    assert client.get("/healthz").headers.get("cache-control") != "no-cache"


def test_republish_deletes_old_files_only_after_feed_update(client, app, cfg, monkeypatch):
    from app import core, publishing
    from publish import release

    from .test_app_host import _make_episode

    eid, out = _make_episode(app, cfg, status="review", title="Pilot")
    (out / "episode.mp3").write_bytes(b"ID3" + b"\x00" * 5000)
    publishing.publish_episode(app.state.db, cfg, eid)
    first = core.get_episode(app.state.db, eid)["audio_key"]
    pub = cfg.public_dir
    assert (pub / first).exists()

    (out / "episode.mp3").write_bytes(b"ID3" + b"\x01" * 6000)  # neu gemischt
    core.update_episode(app.state.db, eid, status="review")
    seen = {}
    real_delete, real_feed = release.delete_keys, release.publish_feed

    def feed(*a, **k):
        seen["old_exists_at_feed"] = (pub / first).exists()
        r = real_feed(*a, **k)
        seen["feed"] = (pub / "feed.xml").read_text()
        return r

    def delete(storage, keys):
        seen["feed_before_delete"] = "feed" in seen
        seen["new_in_feed"] = core.get_episode(app.state.db, eid)["audio_key"] in seen["feed"]
        return real_delete(storage, keys)

    monkeypatch.setattr(release, "publish_feed", feed)
    monkeypatch.setattr(release, "delete_keys", delete)
    publishing.publish_episode(app.state.db, cfg, eid)
    second = core.get_episode(app.state.db, eid)["audio_key"]
    assert second != first and (pub / second).exists() and not (pub / first).exists()
    assert seen["old_exists_at_feed"] is True and seen["feed_before_delete"] is True and seen["new_in_feed"] is True
    assert first not in seen["feed"]


def test_suggest_gets_plan_and_keeps_planned_chapter_titles(client, app, cfg, monkeypatch):
    from app import core, publishing

    eid = _draft(client)
    segs = client.get(f"/api/host/episodes/{eid}/segments").json()
    client.patch(f"/api/host/segments/{segs[0]['id']}", json={"title": "Parken"})
    out = cfg.episode_dir(eid) / "out"
    out.mkdir(parents=True)
    (out / "transcript.json").write_text('{"segments":[{"start_ms":0,"end_ms":1,"speaker":"A","text":"x"}]}')
    app.state.cfg = dataclasses.replace(cfg, anthropic_key="k")
    got = {}

    def fake(transcript, show, chapters, previous, model=None, **kw):
        got.update(kw)
        return {"titles": [], "summary": "", "description_html": "", "chapter_titles": {}}

    monkeypatch.setattr("publish.shownotes.suggest", fake)
    publishing.suggest(app.state.db, app.state.cfg, eid)
    assert [p["title"] for p in got["plan"]][:1] == ["Parken"] and got["plan"][0]["kind"] == "blitzlicht"
    # leerer Plan: kein plan-Argument (bestehende Aufrufe bleiben gültig)
    app.state.db.run("UPDATE segments SET title='', input=''")
    got.clear()
    publishing.suggest(app.state.db, app.state.cfg, eid)
    assert "plan" not in got
    assert core.get_episode(app.state.db, eid)


def test_sting_makes_chapter_only_when_it_advances_the_plan(client, app, clock, cfg):
    """Zwischenton zwischen den Blitzlichtern: schaltet weiter und setzt dann das Kapitel des Abschnitts."""
    from app import manifest
    from pipeline import timeline

    eid = _draft(client)
    segs = client.get(f"/api/host/episodes/{eid}/segments").json()
    for s, t in zip(segs[:2], ["Parken", "Radwege"]):
        client.patch(f"/api/host/segments/{s['id']}", json={"title": t})
    blitz = upload_sound(client, "Blitz", "segment", segment_kind="blitzlicht")
    between = upload_sound(client, "Zwischenton", "sting", segment_kind="blitzlicht")
    tusch = upload_sound(client, "Tusch", "sting")
    htoken, _ = host_login(client)
    T0 = clock.now()
    events = {}
    with _ws(client, htoken) as h:
        h.receive_json()
        create_track(client, htoken, T0)
        h.send_json({"type": "rec.start"})
        recv_until(h, "rec.start")
        clock.advance(2000)
        # Blitzlicht 1, Zwischenton → Blitzlicht 2 (mit Titel), Zwischenton → Blitzlicht 3 (ohne Titel), Tusch
        for name, snd in (("b1", blitz), ("z2", between), ("z3", between), ("t", tusch)):
            clock.advance(1000)
            h.send_json({"type": "sound.play", "sound_id": snd["id"]})
            events[name] = recv_until(h, "sound.play")["event_id"]
            clock.advance(snd["duration_ms"] + 500)
    got = {c["event_id"]: c["title"] for c in client.get(f"/api/host/episodes/{eid}").json()["chapters_preview"]}
    assert got == {events["b1"]: "Kurzthema: Parken", events["z2"]: "Kurzthema: Radwege"}
    m = manifest.build(app.state.db, cfg, eid)
    by_event = {s["event_id"]: s["chapter_title"] for s in m["sounds"]}
    assert by_event[events["z2"]] == "Kurzthema: Radwege" and by_event[events["z3"]] is None and by_event[events["t"]] is None
    m["rec_stop_server_ms"] = m.get("rec_stop_server_ms") or clock.now()
    assert [c["title"] for c in timeline.chapters(m)] == ["Kurzthema: Parken", "Kurzthema: Radwege"]
