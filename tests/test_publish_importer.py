import re
from collections import Counter
from urllib.parse import unquote

import httpx
import pytest

from publish import importer
from publish.importer import FeedImportError, import_feed, parse_duration_ms, parse_explicit, parse_pub_date_ms
from publish.storage import LocalStorage
from tests.publish_fixtures.helpers import FIXTURES, fake_m4a, fake_mp3, jpeg_bytes, png_bytes

FEED_URL = "https://anchor.fm/s/0a1b2c3d/podcast/rss"
SAMPLE = (FIXTURES / "anchor_sample.xml").read_text(encoding="utf-8")
GUIDS = ["11111111-aaaa-4aaa-8aaa-000000000001", "11111111-aaaa-4aaa-8aaa-000000000002", "11111111-aaaa-4aaa-8aaa-000000000003"]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(importer, "_sleep", lambda s: None)


class Site:
    """Bildet anchor.fm nach: Feed, Weiterleitung der Enclosure-URLs auf ein CDN mit URL-kodiertem Ziel, Cover, Bilder."""

    def __init__(self, feed=SAMPLE):
        self.feed = feed
        self.hits = Counter()
        self.cdn = Counter()  # nur Anfragen direkt ans CDN (nach der Weiterleitung)
        self.raw_play_paths = []
        self.fail = {}      # url-teil -> Liste von Antworten/Exceptions, die nacheinander kommen
        self.audio = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.hits[url] += 1
        if request.url.host.endswith("cloudfront.net"):
            self.cdn[url] += 1
        for part, queue in self.fail.items():
            if part in url and queue:
                nxt = queue.pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt
        if url == FEED_URL:
            return httpx.Response(200, content=self.feed.encode("utf-8"), headers={"content-type": "application/rss+xml; charset=utf-8"})
        if request.url.host == "anchor.fm" and "/podcast/play/" in request.url.path:
            raw = request.url.raw_path.decode()
            self.raw_play_paths.append(raw)
            target = unquote(raw.split("/podcast/play/")[1].split("/", 1)[1])
            return httpx.Response(302, headers={"location": target})
        if url.endswith(".mp3") or url.endswith(".m4a"):
            data = self.audio.get(url) or (fake_m4a(20000) if url.endswith(".m4a") else fake_mp3(30000, seed=url))
            return httpx.Response(200, content=data, headers={"content-type": "audio/mpeg" if url.endswith(".mp3") else "audio/x-m4a"})
        if "10000001-1725277704829" in url:  # Show-Cover
            return httpx.Response(200, content=png_bytes(1400, 1400), headers={"content-type": "image/png"})
        if url.endswith(".jpg"):
            return httpx.Response(200, content=jpeg_bytes(400, 400), headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    def cdn_hits(self):
        return sum(n for u, n in self.cdn.items() if u.endswith((".mp3", ".m4a")))

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler), follow_redirects=True)


def run(site, storage, **kw):
    with site.client() as c:
        return import_feed(FEED_URL, storage, client=c, **kw)


def test_import_anchor_sample(tmp_path):
    site = Site()
    st = LocalStorage(tmp_path / "pub", "https://pod.example.org")
    progress = []
    res = run(site, st, on_progress=lambda p, m: progress.append((p, m)))
    assert res.ok and res.feed_episode_count == 3
    assert [e["guid"] for e in res.episodes] == GUIDS  # GUIDs unverändert, Feed-Reihenfolge

    s = res.show
    assert s["title"] == "Die Beispielrunde" and s["language"] == "el"
    assert s["owner_email"] == "kontakt@beispielrunde.example" and s["owner_name"] == "Beispielrunde"
    assert (s["category"], s["subcategory"]) == ("Sports", "Basketball")
    assert s["explicit"] is False and s["author"] == "Beispielrunde" and s["link"].startswith("https://beispielrunde")
    assert s["description"].startswith("🎙️Καλημέρα") and "<" not in s["description"]
    assert re.fullmatch(r"cover-[0-9a-f]{8}\.png", s["cover_key"]) and st.exists(s["cover_key"])

    e0, e1, e2 = res.episodes
    assert e0["pub_date_ms"] == parse_pub_date_ms("Fri, 09 Oct 2026 00:00:00 GMT") or e0["pub_date_ms"] > 0
    assert e0["duration_ms"] == (46 * 60 + 49) * 1000
    assert e1["duration_ms"] == (1 * 3600 + 43 * 60 + 28) * 1000
    assert (e2["season"], e2["number"], e2["episode_type"]) == (2, 11, "full")
    assert e0["explicit"] is False
    assert e0["audio_mime"] == "audio/mpeg" and e1["audio_mime"] == "audio/x-m4a"
    assert e1["audio_key"].endswith(".m4a") and e0["audio_key"].endswith(".mp3")
    for e in res.episodes:
        assert re.fullmatch(r"media/[0-9a-f-]{36}/[a-z0-9-]+-[0-9a-f]{8}\.(mp3|m4a)", e["audio_key"]), e["audio_key"]
        assert st.size(e["audio_key"]) == e["audio_bytes"]
        assert e["legacy"]["guid_is_permalink"] is False
        assert e["legacy"]["enclosure_url"].startswith("https://anchor.fm/s/0a1b2c3d/podcast/play/")
        assert e["legacy"]["image_key"] and st.exists(e["legacy"]["image_key"])
    assert e0["audio_key"].startswith("media/11111111-aaaa-4aaa-8aaa-000000000001/")
    assert e0["audio_key"].rsplit("/", 1)[1].startswith("sample-round-11-preview-")
    # HTML bleibt erhalten, bereinigt; summary ist Klartext aus dem maskierten itunes:summary
    assert e0["description_html"].startswith("<p>") and "<strong>" in e0["description_html"]
    assert "<" not in e0["summary"] and e0["summary"].startswith("🏀")
    # Weiterleitung: das URL-kodierte Ziel erreicht den Server unverändert
    assert site.raw_play_paths and all("%3A%2F%2F" in p for p in site.raw_play_paths)
    assert progress[0][0] == 0.0 and progress[-1][0] == 1.0
    assert [p for p, _ in progress] == sorted(p for p, _ in progress)


def test_import_is_repeatable(tmp_path):
    site = Site()
    st = LocalStorage(tmp_path / "pub", "https://x")
    first = run(site, st)
    audio_hits = site.cdn_hits()
    assert audio_hits == 3
    # Der Server reicht bekannte GUIDs weiter: nichts wird neu geladen
    second = run(site, st, skip_guids=[e["guid"] for e in first.episodes])
    assert second.episodes == [] and second.skipped_guids == GUIDS
    assert site.cdn_hits() == audio_hits
    # Ohne skip: gleiche Schlüssel, keine Doppelten im Speicher
    third = run(site, st)
    assert [e["audio_key"] for e in third.episodes] == [e["audio_key"] for e in first.episodes]
    n_files = len([p for p in (tmp_path / "pub").rglob("*") if p.is_file()])
    run(site, st)
    assert len([p for p in (tmp_path / "pub").rglob("*") if p.is_file()]) == n_files


def test_partial_skip(tmp_path):
    site = Site()
    res = run(site, LocalStorage(tmp_path, "https://x"), skip_guids={GUIDS[0], GUIDS[2]})
    assert [e["guid"] for e in res.episodes] == [GUIDS[1]] and res.skipped_guids == [GUIDS[0], GUIDS[2]]


def test_retries_on_server_and_network_errors(tmp_path):
    site = Site()
    cdn = "cloudfront.net/staging/2026-10-9"
    site.fail[cdn] = [httpx.Response(503), httpx.ConnectError("boom")]
    site.fail["cloudfront.net/staging/2024-4-20"] = [httpx.ReadError("abgebrochen")]
    res = run(site, LocalStorage(tmp_path, "https://x"))
    assert res.ok and len(res.episodes) == 3
    assert sum(n for u, n in site.cdn.items() if cdn in u) == 3


def test_permanent_failure_does_not_stop_the_rest(tmp_path):
    site = Site()
    site.fail["cloudfront.net/staging/2024-4-20"] = [httpx.Response(404)]
    res = run(site, LocalStorage(tmp_path, "https://x"))
    assert [e["guid"] for e in res.episodes] == [GUIDS[0], GUIDS[2]]
    assert len(res.errors) == 1 and res.errors[0]["guid"] == GUIDS[1] and "404" in res.errors[0]["message"]
    assert not res.ok
    again = run(site, LocalStorage(tmp_path, "https://x"), skip_guids=[e["guid"] for e in res.episodes])
    assert [e["guid"] for e in again.episodes] == [GUIDS[1]] and again.ok  # erneuter Lauf holt nach


def test_gives_up_after_retries(tmp_path):
    site = Site()
    site.fail["cloudfront.net/staging/2024-4-20"] = [httpx.Response(500)] * 10
    res = run(site, LocalStorage(tmp_path, "https://x"))
    assert len(res.errors) == 1 and "Versuchen" in res.errors[0]["message"]
    assert sum(n for u, n in site.cdn.items() if "2024-4-20" in u) == importer.ATTEMPTS


def test_html_error_page_is_rejected(tmp_path):
    site = Site()
    html = b"<html><body>" + b"Zugriff verweigert " * 200 + b"</body></html>"
    site.fail["cloudfront.net/staging/2024-4-20"] = [httpx.Response(200, content=html, headers={"content-type": "text/html"})]
    res = run(site, LocalStorage(tmp_path, "https://x"))
    assert res.errors and "keine Audiodatei" in res.errors[0]["message"]


def test_tiny_file_is_rejected(tmp_path):
    site = Site()
    site.fail["cloudfront.net/staging/2024-4-20"] = [httpx.Response(200, content=b"ID3tiny", headers={"content-type": "audio/mpeg"})]
    res = run(site, LocalStorage(tmp_path, "https://x"))
    assert res.errors and "zu klein" in res.errors[0]["message"]


def test_size_mismatch_is_warning_and_real_size_is_used(tmp_path):
    site = Site()
    res = run(site, LocalStorage(tmp_path, "https://x"))
    assert any("weicht" in w for w in res.warnings)  # Feed sagt 44 945 657, die Testdatei ist 30 000
    assert res.episodes[0]["audio_bytes"] == 30000


def test_size_limit(tmp_path):
    res = run(Site(), LocalStorage(tmp_path, "https://x"), max_audio_bytes=10_000)
    assert len(res.errors) == 3 and all("zu groß" in e["message"] for e in res.errors)


def test_large_file_streams_to_storage(tmp_path):
    site = Site()
    big = fake_mp3(6 * 1024 * 1024)
    url = [u for u in re.findall(r'https://anchor\.fm[^"]+2026-10-9[^"]+\.mp3', SAMPLE)][0]
    site.audio["https://d3ctxlq1ktw2nl.cloudfront.net/staging/2026-10-9/100000001-44100-2-aaaaaaaaaaaaaa.mp3"] = big
    st = LocalStorage(tmp_path, "https://x")
    res = run(site, st)
    assert res.episodes[0]["audio_bytes"] == len(big)
    assert (tmp_path / res.episodes[0]["audio_key"]).read_bytes() == big
    assert not list(tmp_path.rglob(".upload-*"))


def test_feed_errors_are_german():
    class Dead(Site):
        def handler(self, request):
            return httpx.Response(404)
    with pytest.raises(FeedImportError, match="nicht geladen"):
        run(Dead(), LocalStorage("/tmp/ps-x-never", "https://x"))
    with pytest.raises(FeedImportError, match="keinen"):
        run(Site(feed="<html><body>hi</body></html>"), LocalStorage("/tmp/ps-x-never", "https://x"))
    with pytest.raises(FeedImportError, match="lesbaren"):
        run(Site(feed="<rss><channel>"), LocalStorage("/tmp/ps-x-never", "https://x"))


def test_xxe_and_entity_bombs_are_refused(tmp_path):
    evil = ('<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
            '<rss version="2.0"><channel><title>&x;</title></channel></rss>')
    with pytest.raises(FeedImportError):
        run(Site(feed=evil), LocalStorage(tmp_path, "https://x"))
    bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
            '<rss version="2.0"><channel><title>&b;</title></channel></rss>')
    with pytest.raises(FeedImportError):
        run(Site(feed=bomb), LocalStorage(tmp_path, "https://x"))


def test_plain_feed_variants(tmp_path):
    feed = """<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Plain</title><description>Nur Text &amp; mehr</description><language>de-DE</language>
<itunes:explicit>yes</itunes:explicit>
<item><title>Ohne GUID</title><enclosure url="https://cdn.example/a.mp3" length="0" type="audio/mp3"/>
<pubDate>Mon, 02 Jan 2023 10:00:00 +0100</pubDate><itunes:duration>1:02:03.5</itunes:duration>
<description>Absatz eins

Absatz zwei <script>x()</script></description><itunes:episode>0</itunes:episode><itunes:season>3</itunes:season>
<itunes:episodeType>Trailer</itunes:episodeType></item>
<item><title>Mit Content</title><guid>abc</guid><enclosure url="https://cdn.example/b.mp3" length="5" type="audio/mpeg"/>
<content:encoded><![CDATA[<h2>Kopf</h2><p onclick="x">Text <b>fett</b></p>]]></content:encoded><itunes:duration>3723</itunes:duration></item>
<item><title>Kein Audio</title><guid>noaudio</guid></item>
<item><title>Doppelt</title><guid>abc</guid><enclosure url="https://cdn.example/c.mp3" length="5" type="audio/mpeg"/></item>
</channel></rss>"""
    site = Site(feed=feed)
    res = run(site, LocalStorage(tmp_path, "https://x"))
    assert res.show["explicit"] is True and res.show["language"] == "de-DE" and res.show["description"] == "Nur Text & mehr"
    assert res.show["cover_key"] == "" and any("Cover" in w for w in res.warnings)
    assert any("Inhaber-E-Mail" in w for w in res.warnings)
    a, b = res.episodes
    assert a["guid"] == "https://cdn.example/a.mp3" and any("keine GUID" in w for w in res.warnings)
    assert a["duration_ms"] == 3723500 and a["number"] is None and a["season"] == 3
    assert a["episode_type"] == "trailer" and a["explicit"] is True  # erbt von der Show
    assert a["description_html"] == "<p>Absatz eins</p><p>Absatz zwei x()</p>" or "<script" not in a["description_html"]
    assert a["pub_date_ms"] == parse_pub_date_ms("Mon, 02 Jan 2023 09:00:00 +0000")
    assert b["guid"] == "abc" and b["description_html"] == "<p><strong>Kopf</strong></p><p>Text <strong>fett</strong></p>"
    assert b["duration_ms"] == 3723000
    assert res.errors == [{"guid": "noaudio", "title": "Kein Audio", "message": "Die Folge hat kein Audio (enclosure fehlt)."}]
    assert any("mehrfach" in w for w in res.warnings)
    assert a["audio_mime"] == "audio/mpeg"  # audio/mp3 wird vereinheitlicht


@pytest.mark.parametrize("text,ms", [
    ("3723", 3723000), ("3723.5", 3723500), ("  62  ", 62000), ("01:02:03", 3723000), ("1:02:03", 3723000),
    ("1:02:03.250", 3723250), ("1:02:03,5", 3723500), ("62:03", 3723000), ("00:45", 45000), ("0:05", 5000),
    ("1h 2m 3s", 3723000), ("1 h 2 min 3 sec", 3723000), ("62 min", 3720000), ("45 Minuten", 2700000),
    ("1 Std 5 Min", 3900000), ("PT1H2M3S", 3723000), ("PT45M", 2700000), ("PT30S", 30000), ("PT1H2M3.5S", 3723500),
    ("P0DT1H2M3S", 3723000), ("99:59:59", 359999000),
])
def test_parse_duration_formats(text, ms):
    assert parse_duration_ms(text) == ms


@pytest.mark.parametrize("text", [None, "", "  ", "0", "00:00:00", "abc", "-5", "12:", "1:2:3:4", "PT", "h"])
def test_parse_duration_invalid(text):
    assert parse_duration_ms(text) is None


def test_slugs_are_ascii():
    from publish.naming import guid_slug, slugify
    assert slugify("Müller & Söhne: Straße!") == "mueller-soehne-strasse"
    assert slugify("Καλημέρα κόσμε") == "episode"
    assert guid_slug("11111111-aaaa-4aaa-8aaa-000000000003") == "11111111-aaaa-4aaa-8aaa-000000000003"
    g = guid_slug("https://example.com/ep?id=1&x=ä")
    assert re.fullmatch(r"[A-Za-z0-9-]+", g) and g != guid_slug("https://example.com/ep?id=1&x=ö")


def test_parse_explicit_and_dates():
    assert [parse_explicit(x) for x in ("true", "YES", "explicit", "false", "no", "clean", "", None, "x")] == \
        [True, True, True, False, False, False, False, False, False]
    assert parse_explicit(None, True) is True
    assert parse_pub_date_ms("Mon, 27 Sep 2021 10:36:28 GMT") == 1632738988000
    assert parse_pub_date_ms("2021-09-27T10:36:28Z") == 1632738988000
    assert parse_pub_date_ms("Mon, 27 Sep 2021 12:36:28 +0200") == 1632738988000
    assert parse_pub_date_ms("kaputt") is None and parse_pub_date_ms("") is None


def test_measures_duration_when_feed_has_none(tmp_path, monkeypatch):
    feed = SAMPLE.replace("<itunes:duration>00:46:49</itunes:duration>", "<itunes:duration>00:00:00</itunes:duration>")
    monkeypatch.setattr(importer, "_measure_ms", lambda p: 1234000)
    res = run(Site(feed=feed), LocalStorage(tmp_path, "https://x"))
    assert res.episodes[0]["duration_ms"] == 1234000


def test_wrong_cover_size_warns(tmp_path):
    site = Site()
    orig = site.handler
    site.handler = lambda r: httpx.Response(200, content=png_bytes(800, 600)) if "10000001-1725277704829" in str(r.url) else orig(r)
    res = run(site, LocalStorage(tmp_path, "https://x"))
    assert any("800×600" in w for w in res.warnings) and res.show["cover_key"]


def test_user_agent_is_generic():
    seen = []
    site = Site()
    orig = site.handler
    site.handler = lambda r: (seen.append(r.headers.get("user-agent")), orig(r))[1]
    importer_client = httpx.Client(transport=httpx.MockTransport(site.handler), follow_redirects=True,
                                   headers={"User-Agent": importer.USER_AGENT})
    import_feed(FEED_URL, LocalStorage("/tmp/ps-ua-test", "https://x"), client=importer_client, skip_guids=GUIDS)
    assert set(seen) == {"podcast-studio-importer/1.0"}
