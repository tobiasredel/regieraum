"""Öffentliche Seite der Show (index.html, folgen/<slug>.html) und `link` im Feed."""
from __future__ import annotations

import json
from html.parser import HTMLParser

from app import core, publishing, util

DIRTY = '<p>Erster <strong>Absatz</strong> mit <a href="https://example.org/x" onclick="boese()">Link</a></p><script>alert(1)</script><img src=x onerror=y>'


def add_episode(app, cfg, title="Pilot", guid="g-1", day=0, source="imported", **extra):
    eid = util.new_id()
    key = f"media/{guid}/pilot-abc12345.mp3"
    (cfg.public_dir / key).parent.mkdir(parents=True, exist_ok=True)
    (cfg.public_dir / key).write_bytes(b"ID3" + b"0" * 100)
    cols = {"id": eid, "status": "published", "source": source, "title": title, "summary": "Kurz und gut", "description_html": DIRTY,
            "guid": guid, "created_at": 1, "published_at": 1_800_000_000_000 + day * 86_400_000, "duration_ms": 125 * 60_000,
            "audio_key": key, "audio_bytes": 103, "audio_mime": "audio/mpeg", "edl": "{}", **extra}
    app.state.db.run(f"INSERT INTO episodes({','.join(cols)}) VALUES({','.join('?' * len(cols))})", list(cols.values()))
    return eid, key


class Check(HTMLParser):
    """Grobe Validität: Tags sauber verschachtelt, Überschriften, Alt-Texte, kein Skript."""

    VOID = {"meta", "link", "img", "br", "hr", "input", "source"}

    def __init__(self):
        super().__init__()
        self.stack, self.errors, self.tags, self.headings, self.images = [], [], set(), [], []

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        if tag in ("h1", "h2", "h3"):
            self.headings.append(tag)
        if tag == "img":
            self.images.append(dict(attrs))
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unerwartetes </{tag}> bei {self.stack[-3:]}")
        else:
            self.stack.pop()


def parse(html: str) -> Check:
    c = Check()
    c.feed(html)
    c.close()
    assert not c.errors and not c.stack, (c.errors, c.stack)
    assert html.startswith("<!doctype html>") and "<script" not in html.lower()
    return c


def build(app, cfg):
    publishing.rebuild_feed(app.state.db, cfg)
    pub = cfg.public_dir
    return pub, (pub / "index.html").read_text(encoding="utf-8")


def test_pages_are_built_with_feed(client, app, cfg):
    app.state.db.set_setting("show", {"title": "Zwei & Mikrofon", "description": "Zwei reden.\n\nÜber Dinge.", "language": "de", "author": "A und B"})
    app.state.db.set_setting("site", {"platforms": [{"name": "Apple Podcasts", "url": "https://podcasts.apple.com/x"},
                                                      {"name": "Kaputt", "url": "javascript:alert(1)"}]})
    _, key1 = add_episode(app, cfg, "Pilot <1>", "g-1", 0)
    add_episode(app, cfg, "Zweite Folge", "g-2", 7)
    pub, index = build(app, cfg)
    c = parse(index)
    assert '<html lang="de">' in index and "<title>Zwei &amp; Mikrofon</title>" in index
    assert "Zwei reden." in index and "Über Dinge." in index
    assert 'href="https://podcasts.apple.com/x"' in index and "javascript:" not in index and "RSS-Feed" in index
    assert index.index("Zweite Folge") < index.index("Pilot &lt;1&gt;")  # neueste zuerst
    assert "22. Januar 2027" in index and "15. Januar 2027" in index and "2 Std. 05 Min." in index and "Kurz und gut" in index
    assert f'<audio controls preload="none" src="http://testserver/public/{key1}">' in index
    assert 'href="folgen/g-1.html"' in index and "folgen/g-2.html" in index
    assert c.headings[0] == "h1" and "h2" in c.headings and "h3" in c.headings
    assert "@media (prefers-color-scheme:dark)" in index and "--petrol:#2447C9" in index  # Farben aus dem Thema

    page = (pub / "folgen" / "g-1.html").read_text(encoding="utf-8")
    c = parse(page)
    assert "<h1>Pilot &lt;1&gt;</h1>" in page and "Shownotes" in page and "<strong>Absatz</strong>" in page
    assert "onclick" not in page and "onerror" not in page and "alert(1)" not in page and 'href="https://example.org/x"' in page
    assert 'href="../index.html"' in page and "<audio controls" in page


def test_chapters_with_times(client, app, cfg):
    eid, _ = add_episode(app, cfg, "Pilot", "g-1", source="recorded")
    out = cfg.episode_dir(eid) / "out"
    out.mkdir(parents=True)
    (out / "chapters.json").write_text(json.dumps({"chapters": [{"startTime": 0, "title": "Intro"}, {"startTime": 3725, "title": "Hauptthema"}]}))
    pub, _ = build(app, cfg)
    page = (pub / "folgen" / "g-1.html").read_text(encoding="utf-8")
    parse(page)
    assert "<h2 id=\"chap\">Kapitel</h2>" in page and "<time>0:00</time><span>Intro</span>" in page and "<time>1:02:05</time><span>Hauptthema</span>" in page


def test_english_page(client, app, cfg):
    app.state.db.set_setting("show", {"title": "The Show", "description": "Two people talk", "language": "en-US"})
    add_episode(app, cfg, "Pilot", "g-1")
    pub, index = build(app, cfg)
    parse(index)
    assert '<html lang="en-US">' in index and "Subscribe to the podcast" in index and "Episodes" in index
    assert "January 15, 2027" in index and "2 h 05 min" in index and "Podcast abonnieren" not in index
    page = (pub / "folgen" / "g-1.html").read_text(encoding="utf-8")
    assert "Show notes" in page and "All episodes" in page


def test_feed_link_points_to_page_unless_custom(client, app, cfg):
    add_episode(app, cfg, "Pilot", "g-1")
    publishing.rebuild_feed(app.state.db, cfg)
    feed = (cfg.public_dir / "feed.xml").read_text()
    assert "<link>http://testserver/public/index.html</link>" in feed
    assert "<link>http://testserver/public/folgen/g-1.html</link>" in feed  # Folge ohne eigenen Link
    # eigener Link des Nutzers gilt
    client.put("/api/host/settings", json={"show": {"link": "https://meine-show.example"}})
    feed = (cfg.public_dir / "feed.xml").read_text()
    assert "<link>https://meine-show.example</link>" in feed and "index.html</link>" not in feed
    # die Standardadresse der App zählt nicht als eigener Link
    client.put("/api/host/settings", json={"show": {"link": "http://testserver"}})
    assert "<link>http://testserver/public/index.html</link>" in (cfg.public_dir / "feed.xml").read_text()


def test_site_off_removes_pages_and_feed_falls_back(client, app, cfg):
    add_episode(app, cfg, "Pilot", "g-1")
    publishing.rebuild_feed(app.state.db, cfg)
    assert (cfg.public_dir / "index.html").exists() and (cfg.public_dir / "folgen" / "g-1.html").exists()
    s = client.put("/api/host/settings", json={"site": {"enabled": False}}).json()
    assert s["site"]["enabled"] is False
    assert not (cfg.public_dir / "index.html").exists() and not (cfg.public_dir / "folgen" / "g-1.html").exists()
    feed = (cfg.public_dir / "feed.xml").read_text()
    assert "<link>http://testserver</link>" in feed and "index.html" not in feed
    client.put("/api/host/settings", json={"site": {"enabled": True}})
    assert (cfg.public_dir / "index.html").exists()


def test_site_settings_validation_and_defaults(client):
    s = client.get("/api/host/settings").json()
    assert s["site"] == {"enabled": True, "platforms": []} and s["stats"] == {"mode": "off", "prefix": ""}
    plats = [{"name": f"P{i}", "url": f"https://example.org/{i}"} for i in range(6)]
    assert client.put("/api/host/settings", json={"site": {"platforms": plats}}).json()["site"]["platforms"] == plats
    assert client.put("/api/host/settings", json={"site": {"platforms": plats + plats[:1]}}).status_code == 422
    assert client.put("/api/host/settings", json={"site": {"platforms": [{"name": "X", "url": "ftp://x"}]}}).status_code == 422
    assert client.put("/api/host/settings", json={"stats": {"mode": "prefix", "prefix": "http://op3.dev/e/"}}).status_code == 422
    assert client.put("/api/host/settings", json={"stats": {"mode": "quatsch"}}).status_code == 422


def test_unpublish_removes_episode_page(client, app, cfg):
    eid, _ = add_episode(app, cfg, "Pilot", "g-1", source="recorded")
    add_episode(app, cfg, "Zwei", "g-2")
    publishing.rebuild_feed(app.state.db, cfg)
    assert (cfg.public_dir / "folgen" / "g-1.html").exists()
    publishing.unpublish_episode(app.state.db, cfg, eid)
    assert not (cfg.public_dir / "folgen" / "g-1.html").exists() and (cfg.public_dir / "folgen" / "g-2.html").exists()
    assert "Pilot" not in (cfg.public_dir / "index.html").read_text()


def test_unchanged_pages_are_not_uploaded_again(client, app, cfg):
    add_episode(app, cfg, "Pilot", "g-1")
    publishing.rebuild_feed(app.state.db, cfg)
    page = cfg.public_dir / "folgen" / "g-1.html"
    page.write_text("manuell geändert")
    publishing.rebuild_feed(app.state.db, cfg)
    assert page.read_text() == "manuell geändert"  # gleiche Prüfsumme: nicht neu hochgeladen
    client.put("/api/host/settings", json={"show": {"copyright": "© Neu"}})
    assert "© Neu" in page.read_text()  # Show-Daten ändern alle Seiten


def test_theme_colors_follow_the_theme():
    from publish.site import stylesheet, theme_colors

    css = ':root{--paper:#fff;--surface:#eee;--ink:#000;--slate:#333;--line:#ccc;--petrol:#c00;}'
    light, dark = theme_colors(css)
    assert light["petrol"] == "#c00" and dark == light  # ohne dunkles Thema bleibt es hell
    assert "--petrol:#c00" in stylesheet(css)
    css += '[data-theme="dark"]{--paper:#111;--ink:#eee;--petrol:#f88;}'
    light, dark = theme_colors(css)
    assert dark["paper"] == "#111" and dark["petrol"] == "#f88" and dark["line"] == "#ccc"
