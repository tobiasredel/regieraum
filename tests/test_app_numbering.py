"""Nummerierung: Staffel nach Modus, Präfix im Feed-Titel, nächste Nummer, Neu nummerieren (SPEC Abschnitt 10)."""
from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET

from app import core, publishing, util

from .test_app_site import add_episode

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
DAY = 86_400_000


def ms(year, month=6, day=1):
    return int(dt.datetime(year, month, day, 12).timestamp() * 1000)


def feed_items(cfg):
    root = ET.fromstring((cfg.public_dir / "feed.xml").read_text(encoding="utf-8"))
    return root.findall("channel/item")


def test_defaults_and_validation(client):
    s = client.get("/api/host/settings").json()
    assert s["numbering"] == {"season": "keep", "prefix": ""}
    r = client.put("/api/host/settings", json={"numbering": {"season": "year", "prefix": "#{number} "}})
    assert r.status_code == 200 and r.json()["numbering"] == {"season": "year", "prefix": "#{number} "}
    for bad in ("#{nummer} ", "{number", "Folge }"):
        r = client.put("/api/host/settings", json={"numbering": {"prefix": bad}})
        assert r.status_code == 422, bad
    assert client.get("/api/host/settings").json()["numbering"]["prefix"] == "#{number} "


def test_numbered_title():
    assert core.numbered_title("Titel", 12, 2026, "#{number} ") == "#12 Titel"
    assert core.numbered_title("Titel", 12, 2026, "{season} – Folge {number}: ") == "2026 – Folge 12: Titel"
    assert core.numbered_title("Titel", None, 2026, "#{number} ") == "Titel"
    assert core.numbered_title("Titel", 12, None, "S{season}E{number} ") == "Titel"
    assert core.numbered_title("Titel", 12, 1, "") == "Titel"


def test_renumber_by_date_with_year_as_season(client, app, cfg):
    db = app.state.db
    # durcheinander wie nach einem Import: doppelte Nummer, Lücke, falsche Reihenfolge, alles Staffel 1
    a, _ = add_episode(app, cfg, "Erste", "g-1", number=1, season=1, published_at=ms(2023, 9))
    b, _ = add_episode(app, cfg, "Zweite", "g-2", number=6, season=1, published_at=ms(2023, 11))
    c, _ = add_episode(app, cfg, "Dritte", "g-3", number=6, season=1, published_at=ms(2024, 2))
    d, _ = add_episode(app, cfg, "Vierte", "g-4", number=3, season=1, published_at=ms(2025, 1))
    db.run("INSERT INTO episodes(id,status,title,guid,created_at,edl,number) VALUES(?,?,?,?,?,?,?)",
           (util.new_id(), "draft", "Vorbereitung", "g-draft", util.now_ms(), "{}", None))
    client.put("/api/host/settings", json={"numbering": {"season": "year"}})

    pre = client.get("/api/host/episodes/renumber").json()
    assert pre["total"] == 4 and pre["next_number"] == 5
    by_id = {x["id"]: x for x in pre["changes"]}
    assert by_id[a]["new"] == {"number": 1, "season": 2023} and by_id[a]["old"] == {"number": 1, "season": 1}
    assert by_id[b]["new"] == {"number": 2, "season": 2023}
    assert by_id[c]["new"] == {"number": 3, "season": 2024}
    assert by_id[d]["new"] == {"number": 4, "season": 2025}
    assert db.one("SELECT number FROM episodes WHERE id=?", (b,))["number"] == 6  # Vorschau speichert nichts

    assert client.post("/api/host/episodes/renumber", json={"count": 99}).status_code == 409
    r = client.post("/api/host/episodes/renumber", json={"count": len(pre["changes"])})
    assert r.status_code == 200 and r.json() == {"changed": 4, "next_number": 5}
    assert client.get("/api/host/episodes/renumber").json()["changes"] == []
    assert db.one("SELECT number FROM episodes WHERE status='draft'")["number"] is None

    items = feed_items(cfg)  # der Feed ist neu gebaut
    seasons = sorted((int(i.findtext(f"{ITUNES}season")), int(i.findtext(f"{ITUNES}episode"))) for i in items)
    assert seasons == [(2023, 1), (2023, 2), (2024, 3), (2025, 4)]


def test_prefix_in_feed_not_on_site(client, app, cfg):
    add_episode(app, cfg, "Der blaue Tsunami", "g-1", number=109, season=2026, published_at=ms(2026, 11, 4))
    add_episode(app, cfg, "Ohne Nummer", "g-2", published_at=ms(2026, 11, 5))
    client.put("/api/host/settings", json={"numbering": {"season": "year", "prefix": "#{number} "}})
    items = {i.findtext("guid"): i for i in feed_items(cfg)}
    one = items["g-1"]
    assert one.findtext("title") == "#109 Der blaue Tsunami"
    assert one.findtext(f"{ITUNES}title") == "Der blaue Tsunami"  # Apple zeigt die Nummer selbst
    assert items["g-2"].findtext("title") == "Ohne Nummer" and items["g-2"].find(f"{ITUNES}title") is None
    # gespeichert bleibt der Titel ohne Präfix
    assert app.state.db.one("SELECT title FROM episodes WHERE guid='g-1'")["title"] == "Der blaue Tsunami"
    page = next((cfg.public_dir / "folgen").glob("*.html")).parent
    html = "".join(p.read_text(encoding="utf-8") for p in page.glob("*.html"))
    assert "<h1>Der blaue Tsunami</h1>" in html and "#109" not in html
    assert "2026 · Folge 109" in html and "Staffel 2026" not in html


def test_number_taken_is_global_with_year(client, app, cfg):
    a, _ = add_episode(app, cfg, "A", "g-1", number=7, season=2025, published_at=ms(2025))
    b, _ = add_episode(app, cfg, "B", "g-2", number=8, season=2026, published_at=ms(2026))
    assert client.patch(f"/api/host/episodes/{b}", json={"number": 7}).status_code == 200  # „von Hand“: je Staffel
    client.patch(f"/api/host/episodes/{b}", json={"number": 8})
    client.put("/api/host/settings", json={"numbering": {"season": "year"}})
    r = client.patch(f"/api/host/episodes/{b}", json={"number": 7})
    assert r.status_code == 409 and "number_taken" in r.text


def test_next_number_and_publish_sets_year(client, app, cfg):
    db = app.state.db
    add_episode(app, cfg, "Alt", "g-1", number=41, season=2025, published_at=ms(2025))
    assert core.next_number(db) == 42
    client.put("/api/host/settings", json={"numbering": {"season": "year"}})
    ep = core.new_draft(db, plan=False)
    assert core.season_for(ms(2026, 1, 2), "year") == 2026
    assert core.season_for(ms(2026), "none", 3) is None and core.season_for(ms(2026), "keep", 3) == 3
    # Aufnahme beginnt: nächste Nummer und Staffel stehen sofort
    db.run("UPDATE episodes SET status='recording', number=?, season=? WHERE id=?",
           (core.next_number(db), core.season_for(ms(2026), "year"), ep["id"]))
    row = db.one("SELECT number, season FROM episodes WHERE id=?", (ep["id"],))
    assert row == {"number": 42, "season": 2026}
    # verwerfen: wieder Vorbereitung ohne Nummer, die nächste Aufnahme bekommt dieselbe Nummer
    core.discard_recording(db, core.get_episode(db, ep["id"]))
    assert db.one("SELECT number, season FROM episodes WHERE id=?", (ep["id"],)) == {"number": None, "season": None}
    assert core.next_number(db) == 42
