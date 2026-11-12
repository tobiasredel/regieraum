"""Host-API: Sounds, Einstellungen, Folgen, Dateien mit Range, Chunk-Upload."""
from __future__ import annotations

import asyncio
import json
import struct
import zlib

from .app_util import auth, create_track, host_login, join_guest, put_chunk, upload_sound


def _png(size: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    raw = b"".join(b"\x00" + b"\x10\x20\x30" * size for _ in range(size))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# --- Sounds -------------------------------------------------------------------------------------
def test_sounds_crud_and_range(client, app):
    s = upload_sound(client, "Intro", "intro", seconds=1.5, gain_db=-2, fade_out_ms=500, hotkey="1")
    assert s["label"] == "Intro" and 1400 <= s["duration_ms"] <= 1600 and s["hotkey"] == "1"
    d = app.state.cfg.sounds_dir / s["id"]
    assert (d / "original.wav").exists() and (d / "playback.wav").exists()
    meta = json.loads((d / "meta.json").read_text())
    assert meta["duration_ms"] == s["duration_ms"]
    b = upload_sound(client, "Outro", "outro", hotkey="1")
    assert {x["id"]: x["hotkey"] for x in client.get("/api/host/sounds").json()} == {s["id"]: None, b["id"]: "1"}

    r = client.patch(f"/api/host/sounds/{s['id']}", json={"label": "Intro neu", "kind": "segment", "chapter_title": "Los", "gain_db": 1.5})
    assert r.status_code == 200 and r.json()["label"] == "Intro neu" and r.json()["kind"] == "segment"
    assert client.patch(f"/api/host/sounds/{s['id']}", json={"kind": "quatsch"}).status_code == 422
    assert client.post("/api/host/sounds/order", json={"ids": [b["id"], s["id"]]}).json()[0]["id"] == b["id"]

    # Audio mit Range (Host und Teilnehmer)
    full = client.get(f"/api/host/sounds/{s['id']}/audio")
    assert full.status_code == 200 and full.headers["accept-ranges"] == "bytes"
    size = len(full.content)
    part = client.get(f"/api/host/sounds/{s['id']}/audio", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and part.content == full.content[:100]
    assert part.headers["content-range"] == f"bytes 0-99/{size}"
    tail = client.get(f"/api/host/sounds/{s['id']}/audio", headers={"Range": "bytes=-10"})
    assert tail.status_code == 206 and tail.content == full.content[-10:]
    gtoken, _ = join_guest(client)
    pr = client.get(f"/api/p/sounds/{s['id']}/audio", headers={**auth(gtoken), "Range": "bytes=44-47"})
    assert pr.status_code == 206 and len(pr.content) == 4 and "max-age=86400" in pr.headers["cache-control"]
    assert client.get(f"/api/p/sounds/{s['id']}/audio").status_code == 401

    assert client.delete(f"/api/host/sounds/{s['id']}").json() == {"deleted": True, "archived": False}
    assert not d.exists() and client.get(f"/api/host/sounds/{s['id']}/audio").status_code == 404


def test_sound_upload_rejects_bad_files(client):
    r = client.post("/api/host/sounds", files={"file": ("x.exe", b"MZ", "application/octet-stream")}, data={"kind": "sting"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_format"
    r = client.post("/api/host/sounds", files={"file": ("x.wav", b"kein audio", "audio/wav")}, data={"kind": "sting"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_audio"
    assert client.get("/api/host/sounds").json() == []


def test_played_sound_is_archived_not_deleted(client, app):
    from .app_util import recv_until

    htoken, _ = host_login(client)
    s = upload_sound(client, "Intro", "intro")
    with client.websocket_connect(f"/ws/p?token={htoken}") as h:
        h.receive_json()
        create_track(client, htoken, 0)
        h.send_json({"type": "rec.start"}); recv_until(h, "rec.start")
        h.send_json({"type": "sound.play", "sound_id": s["id"]}); recv_until(h, "sound.play")
        r = client.delete(f"/api/host/sounds/{s['id']}")
        assert r.json() == {"deleted": False, "archived": True}
    assert client.get("/api/host/sounds").json() == []
    assert (app.state.cfg.sounds_dir / s["id"] / "original.wav").exists()


# --- Einstellungen ----------------------------------------------------------------------------------
def test_settings_and_cover(client, app):
    cur = client.get("/api/host/settings").json()
    assert cur["audio"] == {"target_lufs": -16, "true_peak_db": -1.0, "denoise": "normal", "crossgate": True, "mp3_kbps": 160}
    assert cur["show"]["title"] == "Mein Podcast" and cur["show"]["cover_key"] is None
    r = client.put("/api/host/settings", json={"show": {"owner_email": "a@b.de"}, "audio": {"target_lufs": -18, "denoise": "stark"}})
    assert r.json()["show"]["owner_email"] == "a@b.de" and r.json()["audio"]["target_lufs"] == -18
    assert r.json()["show"]["title"] == "Mein Podcast"
    assert client.put("/api/host/settings", json={"audio": {"denoise": "laut"}}).status_code == 422
    assert client.put("/api/host/settings", json={"audio": {"target_lufs": 5}}).status_code == 422

    bad = client.post("/api/host/settings/cover", files={"file": ("c.png", _png(300), "image/png")})
    assert bad.status_code == 422 and "1400" in bad.json()["error"]["message"]
    notsq = client.post("/api/host/settings/cover", files={"file": ("c.txt", b"hallo", "text/plain")})
    assert notsq.status_code == 422
    ok = client.post("/api/host/settings/cover", files={"file": ("c.png", _png(1400), "image/png")})
    assert ok.status_code == 200
    key = ok.json()["show"]["cover_key"]
    assert key.startswith("cover-") and key.endswith(".png")
    assert client.get("/api/host/settings/cover").headers["content-type"] == "image/png"
    assert client.get("/api/host/status").json()["storage"]["backend"] == "local"


def test_status(client):
    st = client.get("/api/host/status").json()
    assert set(st) == {"storage", "turn", "ai", "stt", "assist", "whisper", "disk_free_bytes", "archive", "costs"}
    assert st["turn"] == {"mode": "none", "configured": False} and st["ai"]["configured"] is False
    assert st["disk_free_bytes"] > 0


# --- Chunk-Upload ------------------------------------------------------------------------------------
def test_chunk_idempotent_atomic_and_limits(client, app):
    gtoken, _ = join_guest(client)
    other, _ = join_guest(client, "Zweiter")
    tid = create_track(client, gtoken, 1000)
    assert put_chunk(client, gtoken, tid, 0).json() == {"received": 1}
    assert put_chunk(client, gtoken, tid, 0).json() == {"received": 1}  # gleiche seq überschreibt
    assert put_chunk(client, gtoken, tid, 1).json() == {"received": 2}
    ep = app.state.db.one("SELECT id FROM episodes")["id"]
    cdir = app.state.cfg.episode_dir(ep) / "tracks" / tid / "chunks"
    assert sorted(p.name for p in cdir.iterdir()) == ["000000.pcm", "000001.pcm"]  # keine Temp-Reste
    assert app.state.db.one("SELECT COUNT(*) AS n FROM chunks")["n"] == 2

    # fremde Spur, falsche Länge, fehlende Header, zu groß
    assert put_chunk(client, other, tid, 2).status_code == 403
    bad = client.put(f"/api/p/tracks/{tid}/chunks/2", headers={**auth(gtoken), "X-First-Frame": "0", "X-Frames": "100"}, content=b"x" * 10)
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "bad_length"
    assert client.put(f"/api/p/tracks/{tid}/chunks/2", headers=auth(gtoken), content=b"x").status_code == 400
    big = client.put(f"/api/p/tracks/{tid}/chunks/2", headers={**auth(gtoken), "X-First-Frame": "0", "X-Frames": "3000000"},
                     content=b"\0" * (8 * 1024 * 1024 + 1))
    assert big.status_code == 413
    assert sorted(p.name for p in cdir.iterdir()) == ["000000.pcm", "000001.pcm"]
    assert client.put("/api/p/tracks/unbekannt/chunks/0", headers=auth(gtoken), content=b"").status_code == 404

    # Uhrenpunkte und Finish mit Lücke
    r = client.post(f"/api/p/tracks/{tid}/clock", headers=auth(gtoken), json={"points": [{"sample_index": 0, "server_ms": 1000}]})
    assert r.json() == {"stored": 1}
    fin = client.post(f"/api/p/tracks/{tid}/finish", headers=auth(gtoken), json={"total_frames": 400, "total_chunks": 4})
    assert fin.json() == {"complete": False, "missing": [2, 3]}


def test_track_progress_broadcast(client):
    from .app_util import recv_until

    htoken, _ = host_login(client)
    gtoken, gp = join_guest(client)
    with client.websocket_connect(f"/ws/p?token={htoken}") as h:
        h.receive_json()
        tid = create_track(client, gtoken, 0)
        put_chunk(client, gtoken, tid, 0, frames=200)
        msg = recv_until(h, "track.progress")
        assert msg["track_id"] == tid and msg["participant_id"] == gp["id"] and msg["uploaded_frames"] == 200
        assert msg["status"] == "recording" and msg["recorded_frames"] >= 200


def test_no_new_track_while_uploading(client, clock):
    from .app_util import recv_until

    htoken, _ = host_login(client)
    with client.websocket_connect(f"/ws/p?token={htoken}") as h:
        h.receive_json()
        create_track(client, htoken, clock.now())
        h.send_json({"type": "rec.start"}); recv_until(h, "rec.start")
        clock.advance(2000)
        h.send_json({"type": "rec.stop"}); recv_until(h, "rec.stop")
        r = client.post("/api/p/tracks", headers=auth(htoken), json={"sample_rate": 48000, "start_server_ms": 1, "meta": {}})
        assert r.status_code == 409 and r.json()["error"]["code"] == "uploading"
        h.send_json({"type": "rec.start"})
        assert recv_until(h, "error")["code"] == "uploading"


# --- Folgen -------------------------------------------------------------------------------------------------
def _make_episode(app, cfg, status="review", **fields):
    from app import core, util

    ep = core.current_episode(app.state.db, create=True)
    cols = {"status": status, "rec_start_server_ms": 1000, "rec_stop_server_ms": 61000, "title": "Pilot", **fields}
    core.update_episode(app.state.db, ep["id"], **cols)
    out = cfg.episode_dir(ep["id"]) / "out"
    out.mkdir(parents=True, exist_ok=True)
    return ep["id"], out


def test_episode_files_range_patch_delete(client, app, cfg):
    eid, out = _make_episode(app, cfg)
    payload = bytes(range(256)) * 40
    (out / "mix_uncut.m4a").write_bytes(payload)
    (out / "episode.mp3").write_bytes(b"ID3" + payload)
    (out / "peaks.json").write_text('{"sample_rate":48000,"samples_per_peak":960,"peaks":[0,1]}')
    (out / "transcript.json").write_text('{"segments":[]}')
    r = client.get(f"/api/host/episodes/{eid}/audio/mix", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == payload[100:200] and r.headers["content-range"] == f"bytes 100-199/{len(payload)}"
    assert client.get(f"/api/host/episodes/{eid}/audio/episode").content[:3] == b"ID3"
    assert client.get(f"/api/host/episodes/{eid}/audio/quatsch").status_code == 422
    assert client.get(f"/api/host/episodes/{eid}/peaks").json()["peaks"] == [0, 1]
    assert client.get(f"/api/host/episodes/{eid}/transcript").json() == {"segments": []}
    assert client.get(f"/api/host/episodes/{eid}/tracks/x/raw").status_code == 404

    d = client.get(f"/api/host/episodes/{eid}").json()
    assert d["files"]["mix"] and d["files"]["episode"] and d["status"] == "review" and d["edl"]["cuts"] == []
    p = client.patch(f"/api/host/episodes/{eid}", json={
        "title": "Neuer Titel", "number": 3, "explicit": True, "episode_type": "bonus",
        "edl": {"trim_start_ms": 500, "trim_end_ms": 50000, "cuts": [{"from_ms": 9000, "to_ms": 12000}, {"from_ms": 11000, "to_ms": 15000}, {"from_ms": 5, "to_ms": 5}]}})
    assert p.status_code == 200
    j = p.json()
    assert j["title"] == "Neuer Titel" and j["number"] == 3 and j["explicit"] is True and j["episode_type"] == "bonus"
    assert j["edl"] == {"trim_start_ms": 500, "trim_end_ms": 50000, "cuts": [{"from_ms": 9000, "to_ms": 15000}]}
    assert client.patch(f"/api/host/episodes/{eid}", json={"chapter_titles": {"nix": "x"}}).status_code == 422
    lst = client.get("/api/host/episodes").json()
    assert [e["id"] for e in lst] == [eid] and "description_html" not in lst[0] and lst[0]["job"] is None

    assert client.delete(f"/api/host/episodes/{eid}").json() == {"deleted": True}
    assert not (app.state.cfg.episode_dir(eid)).exists() and client.get(f"/api/host/episodes/{eid}").status_code == 404


def test_delete_only_allowed_states(client, app, cfg):
    eid, _ = _make_episode(app, cfg, status="published")
    r = client.delete(f"/api/host/episodes/{eid}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_deletable"


def test_process_remix_suggest_enqueue(client, app, cfg):
    eid, out = _make_episode(app, cfg, status="review")
    r = client.post(f"/api/host/episodes/{eid}/process")
    assert r.status_code == 200
    assert app.state.db.one("SELECT status FROM episodes")["status"] == "processing"
    assert client.post(f"/api/host/episodes/{eid}/process").status_code == 409
    job = client.get(f"/api/host/jobs/{r.json()['job_id']}").json()
    assert job["kind"] == "process" and job["status"] == "queued" and job["progress"] == 0
    assert client.get("/api/host/episodes").json()[0]["job"]["kind"] == "process"
    assert client.get("/api/host/jobs/nix").status_code == 404
    assert client.post(f"/api/host/episodes/{eid}/suggest").json()["error"]["code"] == "no_ai"


def test_publish_schedule_and_job(client, app, cfg, clock, monkeypatch):
    from app import core, jobs

    eid, out = _make_episode(app, cfg, status="review", title="Pilot")
    assert client.post(f"/api/host/episodes/{eid}/publish").status_code == 409  # episode.mp3 fehlt
    (out / "episode.mp3").write_bytes(b"ID3" + b"\x00" * 5000)
    (out / "chapters.json").write_text('{"version":"1.2.0","chapters":[]}')
    future = clock.now() + 3_600_000
    r = client.post(f"/api/host/episodes/{eid}/publish", json={"publish_at": future})
    assert r.json() == {"status": "scheduled", "publish_at": future, "job_id": None}
    assert app.state.db.all("SELECT * FROM jobs") == []

    async def tick():
        await app.state.runner.tick()

    asyncio.run(tick())
    assert app.state.db.all("SELECT * FROM jobs") == []  # noch nicht fällig
    clock.advance(3_600_001)
    asyncio.run(tick())
    assert [j["kind"] for j in app.state.db.all("SELECT * FROM jobs")] == ["publish"]

    async def drive():
        while await app.state.runner.run_one():
            pass

    asyncio.run(drive())
    job = app.state.db.one("SELECT * FROM jobs")
    assert job["status"] == "done", job
    ep = core.get_episode(app.state.db, eid)
    assert ep["status"] == "published" and ep["audio_key"].startswith("media/") and ep["audio_bytes"] == 5003
    pub = cfg.public_dir
    assert (pub / ep["audio_key"]).exists() and (pub / "feed.xml").exists()
    feed = (pub / "feed.xml").read_text()
    assert "<title>Pilot</title>" in feed and ep["guid"] in feed
    assert client.get("/public/feed.xml").status_code == 200
    assert "Pilot" in client.get("/api/host/feed/preview").text

    # geänderte Show-Daten und ein neues Cover stehen sofort im veröffentlichten Feed
    assert "inhaber@example.org" not in feed
    assert client.put("/api/host/settings", json={"show": {"owner_email": "inhaber@example.org"}}).status_code == 200
    assert "<itunes:email>inhaber@example.org</itunes:email>" in (pub / "feed.xml").read_text()
    key = client.post("/api/host/settings/cover", files={"file": ("c.png", _png(1400), "image/png")}).json()["show"]["cover_key"]
    assert (pub / key).exists() and key in (pub / "feed.xml").read_text()

    old_key = ep["audio_key"]
    r = client.post(f"/api/host/episodes/{eid}/unpublish")
    assert r.json()["status"] == "unpublishing"
    asyncio.run(drive())
    ep = core.get_episode(app.state.db, eid)
    assert ep["status"] == "review" and ep["audio_key"] is None and not (pub / old_key).exists()
    assert (out / "episode.mp3").exists()  # Ausgangsdatei bleibt für erneutes Veröffentlichen
    assert "Pilot" not in (pub / "feed.xml").read_text()


def test_import_enqueues_job(client, app):
    r = client.post("/api/host/import", json={"feed_url": "https://anchor.fm/s/abc/podcast/rss"})
    assert r.status_code == 202
    job = app.state.db.one("SELECT * FROM jobs WHERE id=?", (r.json()["job_id"],))
    assert job["kind"] == "import" and json.loads(job["payload"]) == {"feed_url": "https://anchor.fm/s/abc/podcast/rss"}
    assert client.post("/api/host/import", json={"feed_url": "ftp://x"}).status_code == 422


def test_stale_running_jobs_requeued(cfg):
    from app import util
    from app.db import DB
    from app.main import create_app

    db = DB(cfg.db_path)
    db.run("INSERT INTO jobs(id,episode_id,kind,status,progress,created_at,started_at) VALUES('j1',NULL,'import','running',0.4,1,2)")
    create_app(cfg)
    row = db.one("SELECT * FROM jobs WHERE id='j1'")
    assert row["status"] == "queued" and row["progress"] == 0


def test_suggest_job_fills_empty_fields_and_keeps_chapter_edits(client, app, cfg, monkeypatch):
    from app import core, publishing

    eid, _ = _make_episode(app, cfg, status="processing", title=None)
    core.update_episode(app.state.db, eid, ai={"chapter_titles": {"ev1": "Mein Titel"}})
    monkeypatch.setattr(publishing, "suggest", lambda db, cfg, i: {
        "titles": ["Erster", "Zweiter"], "summary": "Kurz", "description_html": "<p>Lang</p>",
        "chapter_titles": {"ev1": "KI-Titel", "ev2": "Nur KI"}})
    from app import jobs

    jid = jobs.enqueue(app.state.db, eid, "suggest")

    async def drive():
        while await app.state.runner.run_one():
            pass

    asyncio.run(drive())
    assert app.state.db.one("SELECT status FROM jobs WHERE id=?", (jid,))["status"] == "done"
    ep = core.get_episode(app.state.db, eid)
    assert ep["status"] == "review" and ep["title"] == "Erster" and ep["summary"] == "Kurz"
    assert ep["ai"]["titles"] == ["Erster", "Zweiter"]
    assert ep["ai"]["chapter_titles"] == {"ev1": "Mein Titel", "ev2": "Nur KI"}


def test_import_keeps_cover_copy_for_settings(client, cfg):
    from app import publishing

    cfg.ensure_dirs()
    (cfg.public_dir / "cover-abc12345.jpg").write_bytes(b"jpg")
    publishing._keep_cover_copy(cfg, "cover-abc12345.jpg", None)
    assert (cfg.cover_dir / "cover-abc12345.jpg").read_bytes() == b"jpg"
    publishing._keep_cover_copy(cfg, None, None)  # ohne Cover im Feed passiert nichts


# --- Aufnahme verwerfen und neu aufnehmen -----------------------------------------------------------
def _fill_plan(app, eid):
    db = app.state.db
    db.run("UPDATE segments SET title='Rente', input='Rentenniveau', status='ready', dossier=?, notes='mit Hamburg einsteigen', "
           "visited_at=5 WHERE episode_id=? AND sort=0", ('{"lage": "Stand der Dinge"}', eid))
    return db.one("SELECT id FROM segments WHERE episode_id=? AND sort=0", (eid,))["id"]


def test_discard_review_turns_episode_back_into_preparation(client, app, cfg):
    from app import core

    db = app.state.db
    eid, out = _make_episode(app, cfg, status="review")
    (out / "episode.mp3").write_bytes(b"ID3" + b"\0" * 100)
    seg = _fill_plan(app, eid)
    core.update_episode(db, eid, title="Vorschlag", label="Woche 46")
    db.run("INSERT INTO tracks(id,episode_id,participant_id,participant_name,sample_rate,format,start_server_ms,status,created_at) "
           "VALUES('t1',?,'p1','Max',48000,'s24le',1000,'assembled',1)", (eid,))
    db.run("INSERT INTO cards(id,episode_id,server_ms,kind,text) VALUES('c1',?,1,'fakt','x')", (eid,))
    other = core.new_draft(db, label="Sonderfolge")  # eine zweite Vorbereitung bleibt unberührt

    r = client.post(f"/api/host/episodes/{eid}/discard")
    assert r.status_code == 200 and r.json() == {"discarded": True, "episode_id": eid}
    ep = core.get_episode(db, eid)
    assert ep["status"] == "draft" and ep["label"] == "Woche 46" and ep["title"] is None and ep["rec_start_server_ms"] is None
    assert not cfg.episode_dir(eid).exists()
    assert db.all("SELECT * FROM tracks") == [] and db.all("SELECT * FROM cards") == []
    plan = core.plan_of(db, eid)
    assert len(plan) == 5 and plan[0]["id"] == seg and plan[0]["title"] == "Rente" and plan[0]["dossier"] == {"lage": "Stand der Dinge"}
    assert plan[0]["notes"] == "mit Hamburg einsteigen" and plan[0]["visited_at"] is None  # Besuchsmarke zurückgesetzt
    assert len(core.plan_of(db, other["id"])) == 5
    assert core.current_episode(db)["id"] == eid  # das Studio nimmt als Nächstes wieder hier auf
    assert client.post(f"/api/host/episodes/{eid}/discard").status_code == 409  # jetzt Vorbereitung, nichts zu verwerfen


def test_discard_right_after_stop_keeps_plan(client, app, cfg):
    from app import core

    db = app.state.db
    eid, _ = _make_episode(app, cfg, status="uploading")
    seg = _fill_plan(app, eid)
    r = client.post(f"/api/host/episodes/{eid}/discard", json={})
    assert r.status_code == 200
    cur = core.current_episode(db)
    assert cur["id"] == eid and cur["status"] == "draft" and core.plan_of(db, eid)[0]["id"] == seg


def test_discard_refused_while_recording_or_published(client, app, cfg):
    eid, _ = _make_episode(app, cfg, status="recording")
    r = client.post(f"/api/host/episodes/{eid}/discard")
    assert r.status_code == 409 and r.json()["error"]["code"] == "recording"
    from app import core
    core.update_episode(app.state.db, eid, status="published")
    assert client.post(f"/api/host/episodes/{eid}/discard").json()["error"]["code"] == "bad_state"


def test_cancel_episode_stops_queued_and_running_jobs(app, cfg):
    from app import jobs

    async def main():
        runner = app.state.runner
        db = app.state.db
        eid, _ = _make_episode(app, cfg, status="processing")
        queued = jobs.enqueue(db, eid, "transcribe")
        started = asyncio.Event()

        async def slow(job):  # steht für die Pipeline: läuft, bis der Unterprozess beendet wird
            proc = await asyncio.create_subprocess_exec("sleep", "30")
            runner._running["proc"] = proc
            started.set()
            if await proc.wait() != 0:
                raise jobs.JobError("abgebrochen")

        runner.h_process = slow
        running = jobs.enqueue(db, eid, "process", dedupe=False)
        db.run("UPDATE jobs SET created_at=0 WHERE id=?", (running,))  # läuft zuerst
        task = asyncio.create_task(runner.run_one())
        await asyncio.wait_for(started.wait(), 5)
        assert await runner.cancel_episode(eid, wait_s=5) is True
        await task
        runner.release_cancel(eid)
        return {j["id"]: j for j in db.all("SELECT * FROM jobs")}, queued, running, eid

    rows, queued, running, eid = asyncio.run(main())
    assert rows[queued]["status"] == "failed" and rows[running]["status"] == "failed"
    assert "verworfen" in rows[running]["message"]
    from app import core
    assert core.get_episode(app.state.db, eid)["status"] == "processing"  # kein Fehlerstatus, kein Alarm


# --- Show-Profil und Sendeformat --------------------------------------------------------------------
def test_profile_and_format_settings_drive_names_and_default_plan(client, app, cfg):
    from app import core

    db = app.state.db
    cur = client.get("/api/host/settings").json()
    assert cur["profile"] == {"host_name": "", "cohost_name": "", "genre": "", "tone": ""}
    assert cur["format"]["labels"]["blitzlicht"] == "Kurzthema" and cur["format"]["short_minutes"] == 3
    assert cur["format"]["plan"] == ["blitzlicht", "blitzlicht", "blitzlicht", "hauptthema", "good:read"]
    assert core.host_first_name(db) == "Host"

    r = client.put("/api/host/settings", json={
        "show": {"title": "Zwei am Mikro", "author": "Anna Beispiel, Ben Muster", "owner_name": "Anna Beispiel"},
        "profile": {"host_name": "Anna", "cohost_name": "Ben", "genre": "Technik und Alltag", "tone": "neugierig, trocken"},
        "format": {"labels": {"blitzlicht": "Kurz notiert", "good": "Tipp", "read": "Lesetipp der Woche"}, "short_minutes": 5,
                   "plan": ["hauptthema", "blitzlicht", "good:watch"]}})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["profile"]["cohost_name"] == "Ben" and s["format"]["labels"]["blitzlicht"] == "Kurz notiert"
    assert s["format"]["labels"]["hauptthema"] == "Hauptthema"  # nicht genannte Namen bleiben
    assert core.host_first_name(db) == "Anna"

    # neue Folge folgt dem eigenen Plan
    ep = core.current_episode(db, create=True)
    assert [(x["kind"], x["good_type"]) for x in core.plan_of(db, ep["id"])] == [("hauptthema", None), ("blitzlicht", None), ("good", "watch")]
    # Kapitelnamen tragen die eigenen Namen
    fmt = core.get_format(db)
    assert core.segment_chapter_title({"kind": "blitzlicht", "title": "Bahn"}, fmt) == "Kurz notiert: Bahn"
    assert core.segment_chapter_title({"kind": "good", "good_type": "read", "title": "Ein Buch"}, fmt) == "Lesetipp der Woche: Ein Buch"
    assert core.segment_chapter_title({"kind": "hauptthema", "title": "Wohnen"}, fmt) == "Wohnen"
    # das Show-Profil für Recherche und Begleiter
    from assist import prompts
    block = prompts.show_block(core.assist_show(db, cfg))
    assert "„Zwei am Mikro“, Hosts: Anna Beispiel, Ben Muster." in block
    assert "heißt Anna." in block and "Co-Host am Mikrofon heißt Ben." in block
    assert "Thema der Show: Technik und Alltag." in block and "Ton der Show: neugierig, trocken." in block
    assert "Ablauf einer Folge: Intro, Hauptthema, Kurz notiert (etwa 5 Minuten), Sehtipp, Outro." in block

    # ungültige Eingaben
    assert client.put("/api/host/settings", json={"format": {"plan": ["quatsch"]}}).status_code == 422
    assert client.put("/api/host/settings", json={"format": {"plan": []}}).status_code == 422
    assert client.put("/api/host/settings", json={"format": {"short_minutes": 0}}).status_code == 422


def test_prompts_carry_no_fixed_show_or_names():
    """Die Prompts sprechen von Rollen; wer die Show ist, kommt allein aus dem Show-Profil."""
    import inspect

    from assist import prompts
    text = inspect.getsource(prompts)
    assert "{title}" not in text and "Hosts:" in text  # Titel und Hosts werden nur aus dem Profil eingesetzt
    block = prompts.show_block(None)
    assert block.startswith("Show: „Podcast“.") and "Kurzthema" in block and "Lesetipp" in block



# --- mehrere Vorbereitungen ------------------------------------------------------------------------------
def test_several_preparations_and_studio_choice(client, app, cfg):
    from app import core

    db = app.state.db
    assert client.get("/api/host/preparations").json() == {"preparations": [], "selected": None, "busy": False}
    a = client.post("/api/host/preparations", json={"label": "Woche 46"}).json()
    b = client.post("/api/host/preparations", json={"label": "Sonderfolge Wahl"}).json()
    assert a["status"] == "draft" and a["label"] == "Woche 46"
    res = client.get("/api/host/preparations").json()
    assert [p["label"] for p in res["preparations"]] == ["Sonderfolge Wahl", "Woche 46"]  # neueste zuerst
    assert res["preparations"][0]["segments"] == 5 and res["selected"] == b["id"]  # ohne Wahl: die neueste

    # Arbeitstitel ändern
    assert client.patch(f"/api/host/episodes/{a['id']}", json={"label": "  Woche 46, Bahn  "}).status_code == 200
    assert core.get_episode(db, a["id"])["label"] == "Woche 46, Bahn"

    # das Studio baut auf Woche 46 auf
    r = client.put("/api/host/studio/preparation", json={"episode_id": a["id"]})
    assert r.status_code == 200 and r.json()["selected"] == a["id"]
    assert core.current_episode(db)["id"] == a["id"]

    # ohne Vorbereitung: vor der Aufnahme keine Folge, beim Start eine leere
    r = client.put("/api/host/studio/preparation", json={"episode_id": None})
    assert r.json()["selected"] is None and core.current_episode(db) is None
    ep = core.current_episode(db, create=True)
    assert ep["id"] not in (a["id"], b["id"]) and core.plan_of(db, ep["id"]) == []
    assert core.current_episode(db)["id"] == ep["id"]

    # unbekannte oder nicht mehr vorbereitete Folge
    assert client.put("/api/host/studio/preparation", json={"episode_id": "gibtesnicht"}).status_code == 404
    core.update_episode(db, b["id"], status="review")
    assert client.put("/api/host/studio/preparation", json={"episode_id": b["id"]}).status_code == 404

    # während einer Aufnahme kein Wechsel
    core.update_episode(db, a["id"], status="recording")
    r = client.put("/api/host/studio/preparation", json={"episode_id": None})
    assert r.status_code == 409 and r.json()["error"]["code"] == "busy"
    assert client.get("/api/host/preparations").json()["busy"] is True

    # eine Vorbereitung löschen
    core.update_episode(db, a["id"], status="draft")
    assert client.delete(f"/api/host/episodes/{a['id']}").status_code == 200
    assert a["id"] not in [p["id"] for p in client.get("/api/host/preparations").json()["preparations"]]


def test_recording_uses_chosen_preparation_and_resets_choice(client, app, clock):
    """Über das Studio: Aufnahme startet in der gewählten Vorbereitung, danach gilt wieder die neueste."""
    from app import core

    from .app_util import host_login

    db = app.state.db
    a = core.new_draft(db, label="A")
    b = core.new_draft(db, label="B")
    core.set_studio_choice(db, a["id"])
    token, _ = host_login(client)
    with client.websocket_connect(f"/ws/p?token={token}") as h:
        welcome = h.receive_json()
        assert welcome["room"]["prep_id"] == a["id"]
        assert [p["label"] for p in welcome["room"]["preparations"]] == ["B", "A"]
        h.send_json({"type": "rec.start"})
        for _ in range(10):
            m = h.receive_json()
            if m.get("type") == "rec.start":
                break
        assert m["episode_id"] == a["id"]
        assert core.get_episode(db, a["id"])["status"] == "recording" and core.studio_choice(db) == (False, None)
        assert core.get_episode(db, b["id"])["status"] == "draft"


def test_suggest_without_transcript_transcribes_first(client, app, cfg):
    import dataclasses

    app.state.cfg = dataclasses.replace(cfg, anthropic_key="k")
    eid, out = _make_episode(app, cfg, status="review")
    r = client.post(f"/api/host/episodes/{eid}/suggest")
    assert r.status_code == 409 and "no_transcript" in r.text  # weder Transkript noch fertige Folge
    (out / "episode.mp3").write_bytes(b"ID3" + b"\x00" * 100)
    r = client.post(f"/api/host/episodes/{eid}/suggest").json()
    assert r["kind"] == "transcribe"  # das Transkript entsteht zuerst, der Vorschlag folgt im Job danach
    assert client.get(f"/api/host/jobs/{r['job_id']}").json()["kind"] == "transcribe"
    assert client.get(f"/api/host/episodes/{eid}").json()["has_transcript"] is False
    (out / "transcript.json").write_text('{"segments": []}')
    r = client.post(f"/api/host/episodes/{eid}/suggest").json()
    assert r["kind"] == "suggest"


def test_host_links_saved_validated_and_passed_to_suggestion(client, app, cfg, monkeypatch):
    import dataclasses

    from app import publishing
    from publish import shownotes

    eid, out = _make_episode(app, cfg, status="review")
    r = client.patch(f"/api/host/episodes/{eid}", json={"links": [{"url": "ftp://x"}]})
    assert r.status_code == 422
    links = [{"url": "https://buch.example/a", "title": "Regime Change"}, {"url": "https://podcast.example/ezra"}]
    r = client.patch(f"/api/host/episodes/{eid}", json={"links": links})
    assert r.status_code == 200 and r.json()["links"] == [{"url": "https://buch.example/a", "title": "Regime Change"},
                                                          {"url": "https://podcast.example/ezra", "title": ""}]

    class Page:
        text = '<html><head><meta property="og:title" content="The Ezra Klein Show &amp; Co"><title>x</title></head></html>'

    fetched = []
    monkeypatch.setattr("httpx.get", lambda url, **kw: (fetched.append(url), Page())[1])
    seen = {}
    monkeypatch.setattr(shownotes, "suggest", lambda *a, **kw: (seen.update(kw), {"titles": [], "summary": "", "description_html": "", "chapter_titles": {}})[1])
    (out / "transcript.json").write_text('{"segments": [{"start_ms": 0, "end_ms": 1000, "speaker": "A", "text": "Hallo"}]}')
    publishing.suggest(app.state.db, dataclasses.replace(cfg, anthropic_key="k"), eid)
    assert fetched == ["https://podcast.example/ezra"]  # nur der Link ohne Titel wird abgerufen
    assert seen["links"] == [{"url": "https://buch.example/a", "title": "Regime Change"},
                             {"url": "https://podcast.example/ezra", "title": "The Ezra Klein Show & Co"}]
    assert client.patch(f"/api/host/episodes/{eid}", json={"links": []}).json()["links"] == []
