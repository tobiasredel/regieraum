"""Aufnahme-Ablauf über zwei WebSocket-Clients, Chunks, Manifest, Job-Automatik."""
from __future__ import annotations

import json
import time

from app import jobs, manifest

from .app_util import (auth, create_track, drain_until_room, host_login, join_guest, put_chunk, recv_until,
                       upload_sound)


def _connect(client, token):
    return client.websocket_connect(f"/ws/p?token={token}")


def test_ws_auth_and_welcome(client):
    with _connect(client, "falsch") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "unauthorized"
    htoken, hp = host_login(client)
    with _connect(client, htoken) as ws:
        w = ws.receive_json()
        assert w["type"] == "welcome" and w["self"]["role"] == "host" and "server_ms" in w
        assert w["room"]["episode"]["status"] == "idle" and w["room"]["episode"]["id"] is None
        ws.send_json({"type": "sync", "t0": 123})
        s = recv_until(ws, "sync")
        assert s["t0"] == 123 and isinstance(s["server_ms"], int)


def test_guest_cannot_send_host_messages(client):
    htoken, _ = host_login(client)
    gtoken, _ = join_guest(client)
    with _connect(client, gtoken) as g:
        g.receive_json()
        for msg in ({"type": "rec.start"}, {"type": "sound.play", "sound_id": "x"}, {"type": "marker", "label": "a"},
                    {"type": "rec.stop"}):
            g.send_json(msg)
            err = recv_until(g, "error")
            assert err["code"] == "forbidden"
    assert client.get("/api/host/episodes").json() == []  # nichts angelegt


def test_room_full_and_rtc_and_meter_relay(client):
    tokens = [host_login(client)[0]] + [join_guest(client, f"G{i}")[0] for i in range(4)]
    ws = []
    ctx = [_connect(client, t) for t in tokens[:4]]
    conns = [c.__enter__() for c in ctx]
    try:
        welcomes = [c.receive_json() for c in conns]
        a, b = welcomes[0]["self"]["id"], welcomes[1]["self"]["id"]
        with _connect(client, tokens[4]) as fifth:
            err = fifth.receive_json()
            assert err["code"] == "room_full" and "voll" in err["message"]
        conns[0].send_json({"type": "rtc", "to": b, "data": {"description": {"type": "offer", "sdp": "x"}}})
        msg = recv_until(conns[1], "rtc")
        assert msg["from"] == a and msg["data"]["description"]["type"] == "offer"
        conns[1].send_json({"type": "meter", "peak_db": -6.5, "rms_db": -20, "clip": True})
        m = recv_until(conns[0], "peer.meter")
        assert m["id"] == b and m["clip"] is True and m["peak_db"] == -6.5
        # gedrosselt: zweiter Pegel sofort danach wird verschluckt, Clip-Flag bleibt für den nächsten erhalten
        conns[1].send_json({"type": "meter", "peak_db": -1, "rms_db": -1, "clip": False})
        conns[1].send_json({"type": "state", "mic": False, "cam": True, "headphones": True, "device": "mobile"})
        room = drain_until_room(conns[0], lambda r: any(p["id"] == b and p["cam"] for p in r["peers"]))
        peer = next(p for p in room["peers"] if p["id"] == b)
        assert peer["mic"] is False and peer["device"] == "mobile" and peer["headphones"] is True
    finally:
        for c in reversed(ctx):
            c.__exit__(None, None, None)


def test_full_recording_flow(client, app, clock):
    db = app.state.db
    htoken, hp = host_login(client)
    gtoken, gp = join_guest(client, "Max")
    intro = upload_sound(client, "Intro", "intro", seconds=1.0, fade_out_ms=300)
    sting = upload_sound(client, "Sting", "sting", seconds=0.5)
    T0 = clock.now()
    with _connect(client, htoken) as h, _connect(client, gtoken) as g:
        h.receive_json(); g.receive_json()
        # Vorhören ohne laufende Aufnahme: verteilt, aber nicht gespeichert
        h.send_json({"type": "sound.play", "sound_id": sting["id"]})
        pre = recv_until(g, "sound.play")
        assert pre["at"] == T0 + 300 and pre["duration_ms"] > 0
        assert db.all("SELECT * FROM events") == []

        # Spuren (Rekorder laufen ab Betreten, schon im Entwurf)
        ht = create_track(client, htoken, T0 - 5000)
        gt = create_track(client, gtoken, T0 - 4000)
        ep = db.one("SELECT * FROM episodes")
        assert ep["status"] == "draft"

        h.send_json({"type": "rec.start"})
        started = recv_until(g, "rec.start")
        assert started["start_at"] == T0 + 1500 and started["episode_id"] == ep["id"]
        clock.advance(1500)  # Aufnahme beginnt (T = T0+1500)

        clock.advance(2000)
        h.send_json({"type": "sound.play", "sound_id": intro["id"]})
        play = recv_until(g, "sound.play")
        assert play["at"] == clock.now() + 300
        room = drain_until_room(g, lambda r: len(r["active_sounds"]) == 1 and not r["active_sounds"][0]["preview"])

        clock.advance(5000)  # Pause bei Sitzungszeit 7000
        h.send_json({"type": "rec.pause"})
        assert recv_until(g, "rec.pause")["at"] == clock.now()
        h.send_json({"type": "sound.play", "sound_id": intro["id"]})
        assert recv_until(h, "error")["code"] == "paused"
        clock.advance(4000)  # Weiter bei 11000
        h.send_json({"type": "rec.resume"})
        recv_until(g, "rec.resume")
        h.send_json({"type": "marker", "label": "Hauptthema"})
        mk = recv_until(g, "marker")
        assert mk["at"] == 11000
        clock.advance(3000)
        g.send_json({"type": "rec.stop"})
        assert recv_until(g, "error")["code"] == "forbidden"
        h.send_json({"type": "rec.stop"})
        stopped = recv_until(g, "rec.stop")
        assert stopped["at"] == clock.now()
        room = drain_until_room(g, lambda r: r["episode"]["status"] == "uploading")
        assert room["episode"]["pauses"] == [{"from": 7000, "to": 11000}]

        # Host trennt: nichts bricht ab
    ep = db.one("SELECT * FROM episodes")
    assert ep["status"] == "uploading"
    assert json.loads(ep["edl"])["cuts"] == [{"from_ms": 7000, "to_ms": 11000}]
    assert jobs.check_uploading(db) == []  # Spuren noch nicht fertig

    # Upload: Host vollständig, Gast mit Lücke
    for seq in range(3):
        assert put_chunk(client, htoken, ht, seq).json() == {"received": seq + 1}
    r = client.post(f"/api/p/tracks/{ht}/finish", headers=auth(htoken), json={"total_frames": 300, "total_chunks": 3})
    assert r.json() == {"complete": True, "missing": []}
    put_chunk(client, gtoken, gt, 0)
    put_chunk(client, gtoken, gt, 2)
    r = client.post(f"/api/p/tracks/{gt}/finish", headers=auth(gtoken), json={"total_frames": 300, "total_chunks": 3})
    assert r.json() == {"complete": False, "missing": [1]}
    assert db.one("SELECT status FROM episodes")["status"] == "uploading"
    assert db.all("SELECT * FROM jobs") == []
    st = client.get(f"/api/p/tracks/{gt}", headers=auth(gtoken)).json()
    assert st["received"] == [[0, 0], [2, 2]] and st["missing"] == [1] and st["status"] == "finishing"

    # Nachgeliefert: Spur wird komplett, Job process wird eingereiht
    put_chunk(client, gtoken, gt, 1)
    assert client.get(f"/api/p/tracks/{gt}", headers=auth(gtoken)).json()["status"] == "complete"
    deadline = time.time() + 2
    while not db.all("SELECT * FROM jobs") and time.time() < deadline:
        time.sleep(0.05)
    jobs_rows = db.all("SELECT * FROM jobs")
    assert [(j["kind"], j["status"]) for j in jobs_rows] == [("process", "queued")]
    assert db.one("SELECT status FROM episodes")["status"] == "processing"


def test_manifest_times_from_server_times(client, app, clock, cfg):
    db = app.state.db
    htoken, _ = host_login(client)
    gtoken, _ = join_guest(client)
    intro = upload_sound(client, "Intro", "intro", fade_out_ms=300)
    seg = upload_sound(client, "Jingle Politik", "segment", chapter_title="Politik")
    sting = upload_sound(client, "Sting", "sting")
    T0 = clock.now()
    with _connect(client, htoken) as h:
        h.receive_json()
        ht = create_track(client, htoken, T0 - 1000)
        client.post(f"/api/p/tracks/{ht}/clock", headers=auth(htoken),
                    json={"points": [{"sample_index": 0, "server_ms": T0 - 1000}, {"sample_index": 480000, "server_ms": T0 + 9001}]})
        h.send_json({"type": "rec.start"}); recv_until(h, "rec.start")
        clock.advance(1500)
        start = clock.now()
        clock.advance(1000)
        h.send_json({"type": "sound.play", "sound_id": intro["id"]})
        e1 = recv_until(h, "sound.play")
        clock.advance(10_000)
        h.send_json({"type": "sound.play", "sound_id": seg["id"]})
        e2 = recv_until(h, "sound.play")
        h.send_json({"type": "sound.stop", "event_id": e2["event_id"], "fade_ms": 250})
        stop_msg = recv_until(h, "sound.stop")
        assert stop_msg["fade_ms"] == 250
        clock.advance(2000)
        h.send_json({"type": "rec.pause"}); recv_until(h, "rec.pause")
        clock.advance(6000)
        h.send_json({"type": "rec.resume"}); recv_until(h, "rec.resume")
        h.send_json({"type": "marker", "label": "Wendepunkt", "kind": "chapter"}); recv_until(h, "marker")
        h.send_json({"type": "marker", "label": "Versprecher"}); recv_until(h, "marker")  # ohne Angabe: Merkpunkt
        clock.advance(1000)
        h.send_json({"type": "sound.play", "sound_id": sting["id"]}); recv_until(h, "sound.play")
        clock.advance(500)
        h.send_json({"type": "rec.stop"}); recv_until(h, "rec.stop")
        stop = clock.now()
    for seq in range(2):
        put_chunk(client, htoken, ht, seq, frames=1000, first_frame=seq * 1000)

    ep = db.one("SELECT id FROM episodes")
    m = manifest.build(db, cfg, ep["id"])
    assert m["rec_start_server_ms"] == start and m["rec_stop_server_ms"] >= stop
    assert [s["event_id"] for s in m["sounds"]] == [e1["event_id"], e2["event_id"], m["sounds"][2]["event_id"]]
    s1, s2, s3 = m["sounds"]
    assert s1["at_ms"] == e1["at"] - start == 1300 and s1["kind"] == "intro" and s1["chapter_title"] == "Intro"
    assert s1["stop_ms"] is None and s1["fade_ms"] == 300
    assert s2["at_ms"] == e2["at"] - start and s2["stop_ms"] == stop_msg["at"] - start and s2["fade_ms"] == 250
    assert s2["chapter_title"] == "Politik" and s3["chapter_title"] is None
    assert m["markers"] == [{"at_ms": 1000 + 10000 + 2000 + 6000, "label": "Wendepunkt"}]
    assert [n["label"] for n in m["notes"]] == ["Versprecher"]  # Merkpunkte kommen nie in die Kapitel
    pause_from = 1000 + 10000 + 2000
    assert m["edl"] == {"trim_start_ms": 0, "trim_end_ms": None, "cuts": [{"from_ms": pause_from, "to_ms": pause_from + 6000}]}
    (t,) = m["tracks"]
    assert t["id"] == ht and t["format"] == "s24le" and t["start_server_ms"] == T0 - 1000
    assert t["chunks"] == [{"seq": 0, "first_frame": 0, "frames": 1000}, {"seq": 1, "first_frame": 1000, "frames": 1000}]
    assert [p["sample_index"] for p in t["clock_points"]] == [0, 480000]
    assert t["meta"]["headphones"] is True
    assert m["audio"]["target_lufs"] == -16 and m["tags"]["album"] == "Mein Podcast"
    assert m["sounds"][0]["path"].endswith("original.wav")

    # Kapitelvorschau in Folgenzeit (nach dem Schnitt): Marker liegt nach der Pause
    detail = client.get(f"/api/host/episodes/{ep['id']}").json()
    ch = detail["chapters_preview"]
    assert [c["title"] for c in ch] == ["Intro", "Politik", "Wendepunkt"]
    assert ch[0]["start_ms"] == 1300 and ch[2]["start_ms"] == 1000 + 10000 + 2000  # Pause bereits herausgerechnet


def test_manifest_written_and_runner_process_flow(client, app, clock, cfg, monkeypatch):
    """Job `process` baut work/manifest.json, ruft die Pipeline, liest Fortschrittszeilen und setzt die Folge auf review."""
    import asyncio

    htoken, _ = host_login(client)
    T0 = clock.now()
    with _connect(client, htoken) as h:
        h.receive_json()
        ht = create_track(client, htoken, T0)
        h.send_json({"type": "rec.start"}); recv_until(h, "rec.start")
        clock.advance(4000)
        h.send_json({"type": "rec.stop"}); recv_until(h, "rec.stop")
    put_chunk(client, htoken, ht, 0)
    client.post(f"/api/p/tracks/{ht}/finish", headers=auth(htoken), json={"total_frames": 100, "total_chunks": 1})
    db = app.state.db
    ep_id = db.one("SELECT id FROM episodes")["id"]
    runner = app.state.runner
    cmds = []

    async def fake_cmd(job_id, cmd):
        cmds.append(cmd)
        runner._progress(job_id, 0.5, "halb")
        out = cfg.episode_dir(ep_id)
        (out / "work").mkdir(parents=True, exist_ok=True)
        (out / "work" / "report.json").write_text(json.dumps({"duration_ms": 4000, "warnings": []}))

    monkeypatch.setattr(runner, "run_cmd", fake_cmd)
    sent = []
    from app import ntfy
    monkeypatch.setattr(ntfy, "_post", lambda url, headers, body, auth: sent.append((url, headers, body)))
    app.state.cfg = __import__("dataclasses").replace(cfg, ntfy_url="https://ntfy.example", ntfy_user="u", ntfy_pass="p")

    async def drive():
        while await runner.run_one():
            pass

    asyncio.run(drive())
    kinds = [(j["kind"], j["status"]) for j in db.all("SELECT * FROM jobs ORDER BY created_at, rowid")]
    assert kinds == [("process", "done"), ("transcribe", "done"), ("suggest", "done")]
    assert cmds[0][-2:] == ["process", str(cfg.episode_dir(ep_id) / "work" / "manifest.json")]
    assert cmds[0][:3] == ["nice", "-n", "10"] and cmds[1][-2] == "transcribe"
    assert json.loads((cfg.episode_dir(ep_id) / "work" / "manifest.json").read_text())["episode_id"] == ep_id
    ep = db.one("SELECT * FROM episodes")
    assert ep["status"] == "review" and ep["duration_ms"] == 4000 and ep["number"] == 1
    url, headers, body = sent[-1]
    assert url == "https://ntfy.example/podcast" and headers["Priority"] == "default"
    assert headers["Click"] == f"http://testserver/episodes/{ep_id}" and "Tags" in headers


def test_upload_timeout_starts_processing_with_what_exists(client, app, clock):
    htoken, _ = host_login(client)
    T0 = clock.now()
    with _connect(client, htoken) as h:
        h.receive_json()
        ht = create_track(client, htoken, T0)
        h.send_json({"type": "rec.start"}); recv_until(h, "rec.start")
        clock.advance(3000)
        h.send_json({"type": "rec.stop"}); recv_until(h, "rec.stop")
    db = app.state.db
    put_chunk(client, htoken, ht, 0)
    assert jobs.check_uploading(db) == []
    clock.advance(14 * 60 * 1000)
    assert jobs.check_uploading(db) == []
    clock.advance(2 * 60 * 1000)
    assert len(jobs.check_uploading(db)) == 1
    rep = json.loads(db.one("SELECT report FROM episodes")["report"])
    assert "Vorhandenen" in rep["warnings"][0]
    assert db.one("SELECT status FROM tracks")["status"] == "incomplete"


def test_host_disconnect_does_not_stop_and_sound_ends_by_itself(client, app, clock):
    db = app.state.db
    htoken, hp = host_login(client)
    gtoken, _ = join_guest(client)
    jingle = upload_sound(client, "Kurz", "sting", seconds=0.3)
    outro = upload_sound(client, "Outro", "outro", seconds=3.0)
    T0 = clock.now()
    with _connect(client, gtoken) as g:
        g.receive_json()
        with _connect(client, htoken) as h:
            h.receive_json()
            create_track(client, htoken, T0)
            h.send_json({"type": "rec.start"}); recv_until(g, "rec.start")
            clock.advance(2000)
            h.send_json({"type": "sound.play", "sound_id": jingle["id"]})
            drain_until_room(g, lambda r: len(r["active_sounds"]) == 1)
            # endet nach duration_ms von selbst (echte Zeit, 0,3 s + Vorlauf)
            drain_until_room(g, lambda r: r["active_sounds"] == [])
            clock.advance(1000)
            h.send_json({"type": "sound.play", "sound_id": outro["id"]})
            play = recv_until(g, "sound.play")
            h.send_json({"type": "rec.stop"}); recv_until(g, "rec.stop")
        # Host-Verbindung ist weg
        room = drain_until_room(g, lambda r: not any(p["id"] == hp["id"] and p["connected"] for p in r["peers"]))
        assert room["episode"]["status"] == "uploading"
    ep = db.one("SELECT * FROM episodes")
    # Die Folge endet mit dem späteren von Stop und Sound-Ende
    assert ep["rec_stop_server_ms"] == play["at"] + play["duration_ms"] > clock.now()


def test_host_disconnect_keeps_recording_running(client, app, clock):
    htoken, _ = host_login(client)
    with _connect(client, htoken) as h:
        h.receive_json()
        create_track(client, htoken, clock.now())
        h.send_json({"type": "rec.start"}); recv_until(h, "rec.start")
    assert app.state.db.one("SELECT status FROM episodes")["status"] == "recording"
    with _connect(client, htoken) as h2:
        w = h2.receive_json()
        assert w["room"]["episode"]["status"] == "recording" and w["room"]["peers"][0]["track"]["status"] == "recording"


def test_marker_kinds(client, app, clock):
    """Merkpunkt (note) ist der Standard, Kapitel (chapter) nur auf Wunsch; Unbekanntes wird zum Merkpunkt."""
    db = app.state.db
    htoken, _ = host_login(client)
    with _connect(client, htoken) as h:
        h.receive_json()
        create_track(client, htoken, clock.now() - 1000)
        h.send_json({"type": "rec.start"})
        recv_until(h, "rec.start")
        clock.advance(1500)
        sent = [{"type": "marker", "label": "Versprecher"},
                {"type": "marker", "label": "Thema 2", "kind": "chapter"},
                {"type": "marker", "kind": "note"},
                {"type": "marker", "label": "kaputt", "kind": "irgendwas"},
                {"type": "marker", "kind": "chapter"}]
        got = []
        for msg in sent:
            clock.advance(1000)
            h.send_json(msg)
            got.append(recv_until(h, "marker"))
        assert [m["kind"] for m in got] == ["note", "chapter", "note", "note", "chapter"]
        assert [m["label"] for m in got] == ["Versprecher", "Thema 2", "Merkpunkt", "kaputt", "Marker"]
        rows = db.all("SELECT payload FROM events WHERE kind = 'marker' ORDER BY server_ms")
        assert [json.loads(r["payload"])["kind"] for r in rows] == ["note", "chapter", "note", "note", "chapter"]
