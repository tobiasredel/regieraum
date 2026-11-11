"""Merkpunkte statt Kapitel, Rohdaten im Archiv, Kosten der KI-Vorgänge."""
from __future__ import annotations

import asyncio
import dataclasses
import threading
import time

import pytest

DAY = 24 * 3600 * 1000


def _episode(app, cfg, status="review", **fields):
    from app import core

    ep = core.current_episode(app.state.db, create=True)
    cols = {"status": status, "rec_start_server_ms": 1000, "rec_stop_server_ms": 61000, "title": "Pilot", **fields}
    core.update_episode(app.state.db, ep["id"], **cols)
    return ep["id"]


def _drive(app):
    async def go():
        while await app.state.runner.run_one():
            pass

    asyncio.run(go())


def _tick(app):
    asyncio.run(app.state.runner.tick())


# --- Merkpunkte ------------------------------------------------------------------------------------------------
def _markers(app, eid):
    from app import core

    db = app.state.db
    core.add_event(db, eid, "marker", 1000 + 10_000, payload={"label": "Altes Kapitel"})  # ohne payload.kind = Kapitel
    core.add_event(db, eid, "marker", 1000 + 20_000, payload={"label": "Neues Kapitel", "kind": "chapter"})
    core.add_event(db, eid, "marker", 1000 + 30_000, payload={"label": "Zahl prüfen", "kind": "note"})
    core.add_event(db, eid, "marker", 1000 + 40_000, payload={"label": "", "kind": "note"})


def test_notes_are_not_chapters_but_listed_separately(client, app, cfg):
    eid = _episode(app, cfg)
    _markers(app, eid)
    ep = client.get(f"/api/host/episodes/{eid}").json()
    assert [(c["title"], c["session_ms"]) for c in ep["chapters_preview"]] == [("Altes Kapitel", 10_000), ("Neues Kapitel", 20_000)]
    assert [(n["label"], n["session_ms"]) for n in ep["notes"]] == [("Zahl prüfen", 30_000), ("Merkpunkt", 40_000)]
    assert all(n["event_id"] for n in ep["notes"])


def test_manifest_has_notes_apart_from_chapter_markers(client, app, cfg):
    from app import manifest
    from pipeline import timeline

    eid = _episode(app, cfg)
    _markers(app, eid)
    m = manifest.build(app.state.db, cfg, eid)
    assert [x["label"] for x in m["markers"]] == ["Altes Kapitel", "Neues Kapitel"]
    assert m["notes"] == [{"at_ms": 30_000, "label": "Zahl prüfen"}, {"at_ms": 40_000, "label": "Merkpunkt"}]
    assert [c["title"] for c in timeline.chapters(m)] == ["Altes Kapitel", "Neues Kapitel"]


def test_notes_are_translated_default_label(client, app, cfg):
    eid = _episode(app, cfg)
    _markers(app, eid)
    client.put("/api/host/settings", json={"ui": {"language": "en"}})
    assert [n["label"] for n in client.get(f"/api/host/episodes/{eid}").json()["notes"]] == ["Zahl prüfen", "Note"]


# --- Archiv ----------------------------------------------------------------------------------------------------
@pytest.fixture()
def arch(app, cfg, tmp_path):
    d = tmp_path / "archive"
    d.mkdir()
    app.state.cfg = dataclasses.replace(cfg, archive_dir=d)
    return d


def _published(app, cfg, clock, days_ago=20, **fields):
    eid = _episode(app, cfg, status="published", published_at=clock.now() - days_ago * DAY, **fields)
    base = cfg.episode_dir(eid)
    (base / "out").mkdir(parents=True, exist_ok=True)
    (base / "out" / "episode.mp3").write_bytes(b"ID3" * 100)
    (base / "out" / "peaks.json").write_text("{}")
    tdir = base / "tracks" / "t1"
    tdir.mkdir(parents=True)
    (tdir / "raw.flac").write_bytes(b"f" * 5000)
    (tdir / "chunks").mkdir()
    (tdir / "chunks" / "000000.pcm").write_bytes(b"p" * 700)
    (base / "work" / "stems").mkdir(parents=True)
    (base / "work" / "stems" / "a.wav").write_bytes(b"w" * 3000)
    (base / "work" / "manifest.json").write_text("{}")
    app.state.db.run("INSERT INTO tracks(id,episode_id,participant_id,participant_name,sample_rate,format,start_server_ms,status,created_at)"
                     " VALUES('t1',?,'p','Anna',48000,'s24le',1000,'assembled',1)", (eid,))
    return eid, base


def test_archive_after_deadline_copies_verifies_and_frees_local_space(client, app, cfg, clock, arch):
    from app import core

    eid, base = _published(app, cfg, clock, days_ago=20)
    guid = core.get_episode(app.state.db, eid)["guid"]
    before = client.get(f"/api/host/episodes/{eid}").json()["storage"]
    assert before["archived"] is False and before["archive_available"] is True and before["local_bytes"] > 8700
    _tick(app)
    assert [j["kind"] for j in app.state.db.all("SELECT * FROM jobs")] == ["archive"]
    _drive(app)
    job = app.state.db.one("SELECT * FROM jobs")
    assert job["status"] == "done", job
    # lokal bleibt nur out/, im Archiv liegt alles andere
    assert sorted(p.name for p in base.iterdir()) == ["out"]
    assert (base / "out" / "episode.mp3").exists()
    assert (arch / guid / "tracks" / "t1" / "raw.flac").read_bytes() == b"f" * 5000
    assert (arch / guid / "work" / "stems" / "a.wav").exists() and not (arch / (guid + ".partial")).exists()
    ep = client.get(f"/api/host/episodes/{eid}").json()
    assert ep["archived_at"] and ep["archive_path"] == str(arch / guid)
    assert ep["storage"]["archived"] is True and ep["storage"]["local_bytes"] < 400
    assert ep["tracks"][0]["has_raw"] is False
    # kein zweiter Auftrag
    _tick(app)
    assert len(app.state.db.all("SELECT * FROM jobs")) == 1


def test_archive_respects_deadline_setting(client, app, cfg, clock, arch):
    eid, base = _published(app, cfg, clock, days_ago=10)
    assert client.get("/api/host/settings").json()["archive"] == {"after_days": 14, "available": True}
    _tick(app)
    assert app.state.db.all("SELECT * FROM jobs") == []  # 10 Tage < 14
    r = client.put("/api/host/settings", json={"archive": {"after_days": 7}})
    assert r.json()["archive"] == {"after_days": 7, "available": True}
    _tick(app)
    assert [j["kind"] for j in app.state.db.all("SELECT * FROM jobs")] == ["archive"]
    assert client.put("/api/host/settings", json={"archive": {"after_days": -1}}).status_code == 422


def test_archive_after_days_zero_means_right_after_publishing(client, app, cfg, clock, arch):
    eid, base = _published(app, cfg, clock, days_ago=0)
    client.put("/api/host/settings", json={"archive": {"after_days": 0}})
    _tick(app)
    _drive(app)
    assert sorted(p.name for p in base.iterdir()) == ["out"]


def test_nothing_happens_without_archive(client, app, cfg, clock):
    eid, base = _published(app, cfg, clock, days_ago=400)
    assert client.get("/api/host/settings").json()["archive"]["available"] is False
    _tick(app)
    assert app.state.db.all("SELECT * FROM jobs") == []
    assert (base / "tracks" / "t1" / "raw.flac").exists()
    ep = client.get(f"/api/host/episodes/{eid}").json()
    assert ep["archived_at"] is None and ep["storage"]["archive_available"] is False


def test_episode_in_review_is_never_archived(client, app, cfg, clock, arch):
    eid, base = _published(app, cfg, clock, days_ago=400)
    app.state.db.run("UPDATE episodes SET status='review' WHERE id=?", (eid,))
    client.put("/api/host/settings", json={"archive": {"after_days": 0}})
    _tick(app)
    assert app.state.db.all("SELECT * FROM jobs") == []
    from app import archive

    with pytest.raises(archive.ArchiveError):
        archive.archive_episode(app.state.db, app.state.cfg, eid)
    assert (base / "tracks" / "t1" / "raw.flac").exists()
    # auch Importe und noch nicht veröffentlichte Folgen bleiben unberührt
    app.state.db.run("UPDATE episodes SET status='published', source='imported' WHERE id=?", (eid,))
    _tick(app)
    assert app.state.db.all("SELECT * FROM jobs") == []


def test_failed_copy_keeps_local_data(client, app, cfg, clock, arch, monkeypatch):
    from app import archive

    eid, base = _published(app, cfg, clock, days_ago=20)

    def boom(*a, **k):
        raise OSError("Platte voll")

    monkeypatch.setattr(archive.shutil, "copytree", boom)
    _tick(app)
    _drive(app)
    job = app.state.db.one("SELECT * FROM jobs")
    assert job["status"] == "failed" and "Platte voll" in job["message"]
    assert (base / "tracks" / "t1" / "raw.flac").exists() and not list(arch.iterdir())
    assert client.get(f"/api/host/episodes/{eid}").json()["archived_at"] is None
    _tick(app)  # nach einem Fehlschlag nicht sofort wieder
    assert len(app.state.db.all("SELECT * FROM jobs")) == 1


def test_remix_blocked_while_archived_then_restore(client, app, cfg, clock, arch):
    from app import core

    eid, base = _published(app, cfg, clock, days_ago=20)
    _tick(app)
    _drive(app)
    guid = core.get_episode(app.state.db, eid)["guid"]
    app.state.db.run("UPDATE episodes SET status='review' WHERE id=?", (eid,))  # zurückgezogen: Prüfung
    r = client.post(f"/api/host/episodes/{eid}/remix")
    assert r.status_code == 409 and r.json()["error"]["code"] == "archived"
    r = client.get(f"/api/host/episodes/{eid}/tracks/t1/raw")
    assert r.status_code == 409 and r.json()["error"]["code"] == "archived"
    app.state.db.run("UPDATE episodes SET status='failed' WHERE id=?", (eid,))
    assert client.post(f"/api/host/episodes/{eid}/process").json()["error"]["code"] == "archived"
    app.state.db.run("UPDATE episodes SET status='review' WHERE id=?", (eid,))

    r = client.post(f"/api/host/episodes/{eid}/restore")
    assert r.status_code == 200
    _drive(app)
    jobs = [j for j in app.state.db.all("SELECT * FROM jobs") if j["kind"] == "restore"]
    assert jobs and jobs[0]["status"] == "done", jobs
    ep = client.get(f"/api/host/episodes/{eid}").json()
    assert ep["archived_at"] is None and ep["archive_path"] is None
    assert (base / "tracks" / "t1" / "raw.flac").read_bytes() == b"f" * 5000
    assert (base / "work" / "stems" / "a.wav").exists() and (base / "out" / "episode.mp3").exists()
    assert not (arch / guid).exists()
    assert client.get(f"/api/host/episodes/{eid}/tracks/t1/raw").status_code == 200
    assert client.post(f"/api/host/episodes/{eid}/remix").status_code == 200
    assert client.post(f"/api/host/episodes/{eid}/restore").json()["error"]["code"] == "not_archived"


def test_restore_without_reachable_archive_is_409(client, app, cfg, clock, arch):
    import shutil

    eid, base = _published(app, cfg, clock, days_ago=20)
    _tick(app)
    _drive(app)
    shutil.rmtree(arch / app.state.db.one("SELECT guid FROM episodes WHERE id=?", (eid,))["guid"])
    r = client.post(f"/api/host/episodes/{eid}/restore")
    assert r.status_code == 409 and r.json()["error"]["code"] == "archive_unreachable"


def test_deleting_or_discarding_removes_archive_copy(client, app, cfg, clock, arch):
    eid, base = _published(app, cfg, clock, days_ago=20)
    _tick(app)
    _drive(app)
    copy = arch / app.state.db.one("SELECT guid FROM episodes WHERE id=?", (eid,))["guid"]
    assert copy.exists()
    app.state.db.run("UPDATE episodes SET status='review' WHERE id=?", (eid,))
    assert client.post(f"/api/host/episodes/{eid}/discard").status_code == 200
    assert not copy.exists()
    ep = client.get(f"/api/host/episodes/{eid}").json()
    assert ep["archived_at"] is None and ep["status"] == "draft"


def test_status_shows_storage_and_archive_hint(client, app, cfg, clock, arch):
    eid, base = _published(app, cfg, clock, days_ago=0)
    st = client.get("/api/host/status").json()
    assert st["archive"]["configured"] is True and st["archive"]["episodes_bytes"] >= 8700
    assert st["archive"]["archive_bytes"] == 0 and st["archive"]["archive_free_bytes"] > 0
    assert st["disk_free_bytes"] > 0


def test_status_without_archive_says_unconfigured(client, app, cfg):
    st = client.get("/api/host/status").json()
    assert st["archive"] == {"episodes_bytes": 0, "configured": False}


# --- Kosten ----------------------------------------------------------------------------------------------------
def test_research_cost_is_written_per_segment(client, app, cfg, monkeypatch):
    from assist import prep

    kapp_cfg = dataclasses.replace(cfg, anthropic_key="a-test")
    app.state.cfg = kapp_cfg
    eid = client.post("/api/host/episodes/draft").json()["episode"]["id"]
    seg = client.get(f"/api/host/episodes/{eid}/segments").json()[0]
    client.patch(f"/api/host/segments/{seg['id']}", json={"title": "Parken"})
    gate = threading.Event()

    async def fake(segment, show=None, previous_titles=None, plan_context=None, *, on_progress=None, **kw):
        gate.wait(5)
        return {"lage": "L", "fakten": [], "quellen": [], "meta": {"kosten_usd": 0.4149}}

    monkeypatch.setattr(prep, "research_async", fake)
    assert client.post(f"/api/host/segments/{seg['id']}/research").status_code == 202
    gate.set()
    for _ in range(200):
        cur = client.get(f"/api/host/episodes/{eid}/segments").json()[0]
        if cur["status"] == "ready":
            break
        time.sleep(0.02)
    assert cur["status"] == "ready" and cur["cost_usd"] == 0.4149
    rows = app.state.db.all("SELECT * FROM costs")
    assert [(r["kind"], r["episode_id"], r["segment_id"], r["usd"]) for r in rows] == [("research", eid, seg["id"], 0.4149)]
    # zweite Recherche addiert
    gate.clear()
    client.post(f"/api/host/segments/{seg['id']}/research")
    gate.set()
    for _ in range(200):
        if len(app.state.db.all("SELECT * FROM costs")) == 2:
            break
        time.sleep(0.02)
    segs = client.get(f"/api/host/episodes/{eid}/segments").json()
    assert segs[0]["cost_usd"] == 0.8298 and segs[1]["cost_usd"] == 0
    assert client.get(f"/api/host/episodes/{eid}").json()["costs"]["by_kind"]["research"] == 0.8298


def test_failed_research_costs_nothing(client, app, cfg, monkeypatch):
    from assist import prep
    from assist.llm import AssistError

    app.state.cfg = dataclasses.replace(cfg, anthropic_key="a-test")
    eid = client.post("/api/host/episodes/draft").json()["episode"]["id"]
    seg = client.get(f"/api/host/episodes/{eid}/segments").json()[0]
    client.patch(f"/api/host/segments/{seg['id']}", json={"title": "Parken"})

    async def fake(*a, **k):
        raise AssistError("Recherche fehlgeschlagen: x")

    monkeypatch.setattr(prep, "research_async", fake)
    client.post(f"/api/host/segments/{seg['id']}/research")
    for _ in range(200):
        if client.get(f"/api/host/episodes/{eid}/segments").json()[0]["status"] == "failed":
            break
        time.sleep(0.02)
    assert app.state.db.all("SELECT * FROM costs") == []


def test_shownotes_cost_is_written_after_each_suggestion(client, app, cfg, monkeypatch):
    from app import core, publishing

    app.state.cfg = dataclasses.replace(cfg, anthropic_key="k")
    eid = _episode(app, app.state.cfg)
    out = cfg.episode_dir(eid) / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "transcript.json").write_text('{"segments":[{"start_ms":0,"end_ms":1,"speaker":"A","text":"x"}]}')

    def fake(transcript, show, chapters, previous, model=None, **kw):
        return {"titles": ["T"], "summary": "s", "description_html": "", "chapter_titles": {}, "cost_usd": 0.0312}

    monkeypatch.setattr("publish.shownotes.suggest", fake)
    res = publishing.suggest(app.state.db, app.state.cfg, eid)
    assert "cost_usd" not in res and res["titles"] == ["T"]
    publishing.suggest(app.state.db, app.state.cfg, eid)
    assert [(r["kind"], r["usd"]) for r in app.state.db.all("SELECT * FROM costs")] == [("shownotes", 0.0312)] * 2
    assert core.costs_of(app.state.db, eid) == {"total": 0.0624, "by_kind": {"research": 0.0, "companion": 0.0, "shownotes": 0.0624}}


def test_shownotes_suggest_computes_cost_from_usage():
    from types import SimpleNamespace

    from publish import shownotes

    resp = SimpleNamespace(stop_reason="end_turn", usage=SimpleNamespace(input_tokens=10_000, output_tokens=1_000),
                           content=[SimpleNamespace(type="text", text='{"titles":["Ein Titel"],"summary":"s","description_html":"","chapter_titles":[]}')])

    class Msgs:
        def create(self, **kw):
            return resp

    client = SimpleNamespace(messages=Msgs(), beta=SimpleNamespace(messages=Msgs()))
    out = shownotes.suggest({"segments": [{"start_ms": 0, "end_ms": 1, "speaker": "A", "text": "Hallo Welt"}]}, {"title": "S"}, [], [],
                            client=client, model="claude-sonnet-5-5")
    assert out["cost_usd"] == pytest.approx((10_000 * 2.0 + 1_000 * 10.0) / 1e6, abs=1e-4)
    assert out["titles"] == ["Ein Titel"]


def test_companion_writes_its_total_when_the_session_closes(client, app, cfg):
    from app import core

    eid = _episode(app, cfg, status="uploading")
    comp = app.state.room.companion

    class Session:
        def end_of_audio(self):
            pass

        async def drain(self, timeout=0):
            pass

        async def close(self):
            pass

        def summary_stats(self):
            return {"stt_cost_usd": 0.1234, "llm_cost_usd": 0.0166}

    comp.grace_s = 0
    asyncio.run(comp._finish(Session(), eid))
    assert [(r["kind"], r["usd"]) for r in app.state.db.all("SELECT * FROM costs")] == [("companion", 0.14)]
    assert client.get(f"/api/host/episodes/{eid}").json()["costs"]["by_kind"]["companion"] == 0.14

    class Broken(Session):
        def summary_stats(self):
            raise RuntimeError("kaputt")

    asyncio.run(comp._finish(Broken(), eid))  # Fehler stört nie
    assert len(app.state.db.all("SELECT * FROM costs")) == 1


def test_zero_and_nonsense_costs_are_ignored(app):
    from app import core

    for usd in (0, -1, None, "x", float("nan"), float("inf")):
        core.add_cost(app.state.db, "e", "research", usd)
    core.add_cost(app.state.db, "e", "other", 1.0)
    assert app.state.db.all("SELECT * FROM costs") == []


def test_status_shows_costs_this_and_last_month(client, app, clock):
    from app import core

    db = app.state.db
    now = clock.now()  # 2027-01-15
    core.add_cost(db, "e", "research", 0.41)
    core.add_cost(db, "e", "companion", 1.0)
    for kind, usd, ms in (("shownotes", 0.05, now - 20 * DAY), ("research", 2.0, now - 25 * DAY), ("companion", 9.0, now - 70 * DAY)):
        db.run("INSERT INTO costs(id,episode_id,kind,usd,created_at) VALUES(?,?,?,?,?)", (f"c{ms}", "e", kind, usd, ms))
    costs = client.get("/api/host/status").json()["costs"]
    assert costs["current"]["month"] == "2027-01" and costs["current"]["total"] == 1.41
    assert costs["current"]["by_kind"] == {"research": 0.41, "companion": 1.0, "shownotes": 0.0}
    assert costs["previous"]["month"] == "2026-12" and costs["previous"]["total"] == 2.05
