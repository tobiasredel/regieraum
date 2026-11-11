"""Downloads zählen: /d/<audio_key>, Feed-Adressen je Modus, Auswertung, öffentlich erreichbar."""
from __future__ import annotations

import dataclasses

from fastapi.testclient import TestClient

from app import publishing
from tests.test_app_site import add_episode

UA = "Overcast/3.0 (+http://overcast.fm/; iOS podcast app)"


def get(client, key, ua=UA, method="GET", **headers):
    return client.request(method, f"/d/{key}", headers={"User-Agent": ua, **headers}, follow_redirects=False)


def count(app):
    return app.state.db.one("SELECT COUNT(*) AS n FROM downloads")["n"]


def test_counts_once_per_day_and_visitor_and_redirects(client, app, cfg, clock):
    eid, key = add_episode(app, cfg)
    r = get(client, key)
    assert r.status_code == 302 and r.headers["location"] == f"http://testserver/public/{key}" and r.headers["cache-control"] == "no-store"
    assert count(app) == 1
    get(client, key)  # derselbe Besucher, derselbe Tag
    assert count(app) == 1
    get(client, key, ua="Pocket Casts/7.0")  # anderer User-Agent: anderer Besucher
    assert count(app) == 2
    clock.advance(86_400_000)  # nächster Tag
    get(client, key)
    assert count(app) == 3
    rows = app.state.db.all("SELECT * FROM downloads")
    assert {r["episode_id"] for r in rows} == {eid} and all(len(r["visitor"]) == 16 for r in rows)
    assert len({r["day"] for r in rows}) == 2


def test_no_ip_is_stored_and_salt_is_persistent(client, app, cfg):
    _, key = add_episode(app, cfg)
    get(client, key, **{"X-Forwarded-For": "203.0.113.7"})
    dump = " ".join(str(v) for r in app.state.db.all("SELECT * FROM downloads") for v in r.values())
    assert "203.0.113.7" not in dump
    salt = (cfg.data / "stats_salt").read_bytes()
    assert len(salt) == 32 and ((cfg.data / "stats_salt").stat().st_mode & 0o777) == 0o600
    get(client, key, **{"X-Forwarded-For": "203.0.113.8"})  # andere IP: anderer Besucher
    assert count(app) == 2 and (cfg.data / "stats_salt").read_bytes() == salt


def test_range_bots_and_head(client, app, cfg):
    _, key = add_episode(app, cfg)
    assert get(client, key, method="HEAD").status_code == 302 and count(app) == 0  # HEAD zählt nicht
    get(client, key, ua="Mozilla/5.0 (compatible; Googlebot/2.1)")
    get(client, key, ua="curl/8.0")
    get(client, key, ua="")
    assert count(app) == 0
    get(client, key, Range="bytes=1000-")  # Fortsetzung mitten in der Datei
    assert count(app) == 0
    get(client, key, Range="bytes=0-1")  # Beginn der Datei
    assert count(app) == 1
    get(client, key, ua="Other/1", Range="bytes=0-")
    assert count(app) == 2


def test_unknown_key_and_unpublished_are_404(client, app, cfg):
    eid, key = add_episode(app, cfg)
    assert get(client, "media/nix.mp3").status_code == 404
    app.state.db.run("UPDATE episodes SET status='review' WHERE id=?", (eid,))
    assert get(client, key).status_code == 404 and count(app) == 0


def test_feed_urls_per_mode(client, app, cfg):
    _, key = add_episode(app, cfg)
    app.state.db.set_setting("show", {"cover_key": "cover-abc.jpg"})
    direct = f"http://testserver/public/{key}"
    publishing.rebuild_feed(app.state.db, cfg)
    feed = (cfg.public_dir / "feed.xml").read_text()
    assert f'<enclosure url="{direct}"' in feed
    assert f'src="{direct}"' in (cfg.public_dir / "index.html").read_text()

    assert client.put("/api/host/settings", json={"stats": {"mode": "app"}}).json()["stats"]["mode"] == "app"
    feed, index = (cfg.public_dir / "feed.xml").read_text(), (cfg.public_dir / "index.html").read_text()
    assert f'<enclosure url="http://testserver/d/{key}"' in feed and f'src="http://testserver/d/{key}"' in index
    assert 'itunes:image href="http://testserver/public/cover-abc.jpg"' in feed  # Cover bleibt direkt

    s = client.put("/api/host/settings", json={"stats": {"mode": "prefix", "prefix": "https://op3.dev/e"}}).json()
    assert s["stats"] == {"mode": "prefix", "prefix": "https://op3.dev/e"}
    feed, index = (cfg.public_dir / "feed.xml").read_text(), (cfg.public_dir / "index.html").read_text()
    assert f'<enclosure url="https://op3.dev/e/{direct}"' in feed and f'src="https://op3.dev/e/{direct}"' in index
    assert client.get("/api/host/feed/preview").text.count("op3.dev/e/") == 1

    client.put("/api/host/settings", json={"stats": {"mode": "off"}})
    assert f'<enclosure url="{direct}"' in (cfg.public_dir / "feed.xml").read_text()


def test_stats_endpoints(client, app, cfg, clock):
    e1, k1 = add_episode(app, cfg, "Eins", "g-1")
    e2, _ = add_episode(app, cfg, "Zwei", "g-2")
    assert client.get("/api/host/stats/downloads").json() == {"mode": "off", "totals": {}}
    for i in range(3):
        get(client, k1, ua=f"App{i}/1")
    clock.advance(2 * 86_400_000)
    get(client, k1, ua="App0/1")
    d = client.get(f"/api/host/episodes/{e1}/downloads").json()
    assert (d["total"], d["last7"], d["last30"]) == (4, 4, 4) and len(d["days"]) == 30
    assert d["days"][-1] == {"day": d["days"][-1]["day"], "count": 1} and d["days"][-3]["count"] == 3
    assert client.get("/api/host/stats/downloads").json()["totals"] == {e1: 4}
    assert client.get(f"/api/host/episodes/{e2}/downloads").json()["total"] == 0
    clock.advance(10 * 86_400_000)
    d = client.get(f"/api/host/episodes/{e1}/downloads").json()
    assert (d["total"], d["last7"], d["last30"]) == (4, 0, 4)
    assert client.get("/api/host/episodes/nix/downloads").status_code == 404


def test_download_route_is_public_with_password_auth(cfg):
    from app.main import create_app

    pcfg = dataclasses.replace(cfg, host_auth="password", host_password="geheim-1234")
    with TestClient(create_app(pcfg), follow_redirects=False) as c:
        eid, key = add_episode(c.app, pcfg)
        r = c.get(f"/d/{key}", headers={"User-Agent": UA})
        assert r.status_code == 302 and r.headers["location"].endswith(key)  # ohne Anmeldung erreichbar
        assert c.get("/api/host/stats/downloads").status_code == 401  # die Auswertung bleibt geschützt
        assert c.get(f"/api/host/episodes/{eid}/downloads").status_code == 401
