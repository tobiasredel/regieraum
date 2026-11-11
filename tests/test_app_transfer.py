"""Abschnitt in eine andere Vorbereitung verschieben oder kopieren."""
from __future__ import annotations


def _two(client, app):
    a = client.post("/api/host/preparations", json={"label": "A"}).json()
    b = client.post("/api/host/preparations", json={"label": "B"}).json()
    db = app.state.db
    db.run("UPDATE segments SET title='Rente', input='Rentenniveau', status='ready', dossier=?, notes='Notiz', "
           "visited_at=5 WHERE episode_id=? AND sort=0", ('{"lage": "Stand der Dinge"}', a["id"]))
    sid = db.one("SELECT id FROM segments WHERE episode_id=? AND sort=0", (a["id"],))["id"]
    return a, b, sid


def _plan(client, eid):
    return client.get(f"/api/host/episodes/{eid}/segments").json()


def test_move_keeps_content_resets_visited_and_renumbers(client, app):
    a, b, sid = _two(client, app)
    r = client.post(f"/api/host/segments/{sid}/transfer", json={"target_episode_id": b["id"], "mode": "move"})
    assert r.status_code == 200, r.text
    moved = r.json()
    assert moved["id"] == sid and moved["episode_id"] == b["id"] and moved["sort"] == 5
    assert moved["title"] == "Rente" and moved["notes"] == "Notiz" and moved["dossier"] == {"lage": "Stand der Dinge"}
    assert moved["visited_at"] is None and moved["status"] == "ready"
    assert [s["sort"] for s in _plan(client, a["id"])] == [0, 1, 2, 3] and len(_plan(client, b["id"])) == 6


def test_copy_gets_new_id_and_dossier(client, app):
    a, b, sid = _two(client, app)
    r = client.post(f"/api/host/segments/{sid}/transfer", json={"target_episode_id": b["id"], "mode": "copy"})
    assert r.status_code == 200, r.text
    copy = r.json()
    assert copy["id"] != sid and copy["episode_id"] == b["id"] and copy["sort"] == 5
    assert copy["dossier"] == {"lage": "Stand der Dinge"} and copy["notes"] == "Notiz" and copy["input"] == "Rentenniveau"
    assert copy["visited_at"] is None
    orig = next(s for s in _plan(client, a["id"]) if s["id"] == sid)
    assert orig["visited_at"] == 5 and len(_plan(client, a["id"])) == 5  # Quelle bleibt unberührt


def test_transfer_rejections(client, app):
    a, b, sid = _two(client, app)
    url = f"/api/host/segments/{sid}/transfer"
    assert client.post(url, json={"target_episode_id": a["id"], "mode": "move"}).status_code == 422  # dieselbe
    assert client.post(url, json={"target_episode_id": "gibtsnicht", "mode": "move"}).status_code == 404
    assert client.post("/api/host/segments/x/transfer", json={"target_episode_id": b["id"], "mode": "move"}).status_code == 404
    assert client.post(url, json={"target_episode_id": b["id"], "mode": "quatsch"}).status_code == 422
    app.state.db.run("UPDATE episodes SET status='published' WHERE id=?", (b["id"],))
    r = client.post(url, json={"target_episode_id": b["id"], "mode": "copy"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_a_preparation"
    app.state.db.run("UPDATE episodes SET status='draft' WHERE id=?", (b["id"],))
    app.state.db.run("UPDATE segments SET status='researching' WHERE id=?", (sid,))
    r = client.post(url, json={"target_episode_id": b["id"], "mode": "move"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "researching"
    assert len(_plan(client, b["id"])) == 5 and len(_plan(client, a["id"])) == 5
