import json
import re

import httpx
import pytest

from publish.feed import build_feed
from publish.naming import sibling_keys
from publish.release import CoverError, publish_feed, remove_episode_assets, render_feed, upload_cover, upload_episode_assets
from publish.storage import LocalStorage
from publish.validate import validate_feed, validate_feed_issues
from tests.publish_fixtures.helpers import episode, fake_mp3, jpeg_bytes, png_bytes, show


class Spy(LocalStorage):
    def __init__(self, *a):
        super().__init__(*a)
        self.puts, self.deletes, self.meta = [], [], {}

    def put_file(self, key, path, content_type, cache_control=None):
        self.puts.append(key)
        self.meta[key] = (content_type, cache_control)
        super().put_file(key, path, content_type, cache_control)

    def put_bytes(self, key, data, content_type, cache_control=None):
        self.puts.append(key)
        self.meta[key] = (content_type, cache_control)
        super().put_bytes(key, data, content_type, cache_control)

    def delete(self, key):
        self.deletes.append(key)
        super().delete(key)


@pytest.fixture
def st(tmp_path):
    return Spy(tmp_path / "pub", "https://pod.example.org")


def files(tmp_path, mp3_seed="a", chapters="{}", vtt="WEBVTT\n"):
    d = tmp_path / "out"
    d.mkdir(exist_ok=True)
    (d / "episode.mp3").write_bytes(fake_mp3(5000, mp3_seed))
    (d / "chapters.json").write_text(chapters)
    (d / "transcript.vtt").write_text(vtt)
    return d / "episode.mp3", d / "chapters.json", d / "transcript.vtt"


def test_upload_assets_keys_and_headers(st, tmp_path):
    mp3, ch, vtt = files(tmp_path)
    ep = {"guid": "abc-123", "title": "Öl & Gas: Folge 7"}
    r = upload_episode_assets(st, ep, mp3, ch, vtt)
    assert re.fullmatch(r"media/abc-123/oel-gas-folge-7-[0-9a-f]{8}\.mp3", r["audio_key"])
    assert (r["audio_bytes"], r["audio_mime"]) == (5000, "audio/mpeg")
    assert (r["chapters_key"], r["transcript_key"]) == sibling_keys(r["audio_key"])
    for k in (r["audio_key"], r["chapters_key"], r["transcript_key"]):
        assert st.exists(k)
        assert st.meta[k][1] == "public, max-age=31536000, immutable"
    assert st.meta[r["audio_key"]][0] == "audio/mpeg"
    assert st.meta[r["chapters_key"]][0] == "application/json+chapters"
    assert st.meta[r["transcript_key"]][0] == "text/vtt"


def test_upload_is_idempotent_and_replaces_old_files(st, tmp_path):
    mp3, ch, vtt = files(tmp_path)
    ep = {"guid": "g1", "title": "T", "source": "recorded"}
    r1 = upload_episode_assets(st, ep, mp3, ch, vtt)
    n = len(st.puts)
    r2 = upload_episode_assets(st, {**ep, **r1}, mp3, ch, vtt)  # gleicher Inhalt: nichts neu hochgeladen, nichts gelöscht
    assert r2 == r1 and len(st.puts) == n and st.deletes == []
    mp3b, chb, vttb = files(tmp_path, mp3_seed="neu")  # neu gemischt
    r3 = upload_episode_assets(st, {**ep, **r1}, mp3b, chb, vttb)
    assert r3["audio_key"] != r1["audio_key"]
    assert not st.exists(r1["audio_key"]) and not st.exists(r1["chapters_key"]) and not st.exists(r1["transcript_key"])
    assert st.exists(r3["audio_key"])
    assert set(st.deletes) == {r1["audio_key"], r1["chapters_key"], r1["transcript_key"]}


def test_delete_old_false_keeps_old_files_until_delete_keys(st, tmp_path):
    from publish.release import delete_keys

    mp3, ch, vtt = files(tmp_path)
    ep = {"guid": "g1", "title": "T", "source": "recorded"}
    r1 = upload_episode_assets(st, ep, mp3, ch, vtt)
    mp3b, chb, vttb = files(tmp_path, mp3_seed="neu")
    r2 = upload_episode_assets(st, {**ep, **r1}, mp3b, chb, vttb, delete_old=False)
    assert st.deletes == [] and st.exists(r1["audio_key"]) and st.exists(r2["audio_key"])  # Feed darf noch auf alt zeigen
    assert set(r2["obsolete_keys"]) == {r1["audio_key"], r1["chapters_key"], r1["transcript_key"]}
    delete_keys(st, r2["obsolete_keys"])
    assert not st.exists(r1["audio_key"]) and st.exists(r2["audio_key"])


def test_changed_chapters_change_all_names(st, tmp_path):
    mp3, ch, vtt = files(tmp_path)
    ep = {"guid": "g1", "title": "T"}
    r1 = upload_episode_assets(st, ep, mp3, ch, vtt)
    ch.write_text('{"version": "1.2.0"}')
    r2 = upload_episode_assets(st, {**ep, **r1}, mp3, ch, vtt)
    assert r2["audio_key"] != r1["audio_key"] and not st.exists(r1["chapters_key"])


def test_upload_without_chapters_or_transcript(st, tmp_path):
    mp3, _, _ = files(tmp_path)
    r = upload_episode_assets(st, {"guid": "g", "title": "T"}, mp3, None, None)
    assert r["chapters_key"] is None and r["transcript_key"] is None and st.exists(r["audio_key"])


def test_upload_rejects_bad_input(st, tmp_path):
    mp3, ch, vtt = files(tmp_path)
    with pytest.raises(ValueError, match="GUID"):
        upload_episode_assets(st, {"title": "x"}, mp3, ch, vtt)
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"x")
    with pytest.raises(ValueError, match="Audioformat"):
        upload_episode_assets(st, {"guid": "g", "title": "x"}, wav, None, None)


def test_remove_assets_keeps_cover(st, tmp_path):
    mp3, ch, vtt = files(tmp_path)
    r = upload_episode_assets(st, {"guid": "g", "title": "T"}, mp3, ch, vtt)
    st.put_bytes("cover-1.jpg", b"x", "image/jpeg")
    st.put_bytes("media/g/image-1.jpg", b"x", "image/jpeg")
    gone = remove_episode_assets(st, {"guid": "g", "source": "recorded", "audio_key": r["audio_key"],
                                      "legacy": json.dumps({"image_key": "media/g/image-1.jpg"})})
    assert set(gone) == {r["audio_key"], r["chapters_key"], r["transcript_key"], "media/g/image-1.jpg"}
    assert not st.exists(r["audio_key"]) and st.exists("cover-1.jpg")
    # Bild, das zugleich das Show-Cover ist, bleibt
    assert remove_episode_assets(st, {"audio_key": "media/g/x.mp3", "legacy": {"image_key": "cover-1.jpg"}}) == \
        sorted(["media/g/x.mp3", *sibling_keys("media/g/x.mp3")])
    assert st.exists("cover-1.jpg")


def test_upload_cover_validation(st, tmp_path):
    def f(data):
        p = tmp_path / "c.bin"
        p.write_bytes(data)
        return p
    k = upload_cover(st, f(png_bytes(1400, 1400)))
    assert re.fullmatch(r"cover-[0-9a-f]{8}\.png", k) and st.exists(k)
    assert st.meta[k] == ("image/png", "public, max-age=31536000, immutable")
    k2 = upload_cover(st, f(jpeg_bytes(3000, 3000)), old_key=k)
    assert k2.endswith(".jpg") and not st.exists(k) and st.exists(k2)
    for data, msg in [(png_bytes(1400, 1000), "quadratisch"), (png_bytes(1000, 1000), "1400"), (png_bytes(3200, 3200), "3000"),
                      (b"GIF89a....", "JPEG- oder PNG")]:
        with pytest.raises(CoverError, match=msg):
            upload_cover(st, f(data))


def test_publish_feed_uploads_and_returns_url(st, tmp_path):
    mp3, ch, vtt = files(tmp_path)
    a = upload_episode_assets(st, {"guid": "g1", "title": "Eins"}, mp3, ch, vtt)
    row = {"guid": "g1", "title": "Eins", "source": "recorded", "published_at": 1_700_000_000_000, "duration_ms": 60000,
           "summary": "s", "description_html": "<p>d</p>", **a}
    url = publish_feed(st, show().to_dict(), [row])
    assert url == "https://pod.example.org/feed.xml"
    assert st.meta["feed.xml"] == ("application/rss+xml; charset=utf-8", "public, max-age=300")
    xml = (tmp_path / "pub/feed.xml").read_text(encoding="utf-8")
    assert f'url="https://pod.example.org/{a["audio_key"]}"' in xml
    assert 'length="5000"' in xml and "podcast:chapters" in xml and "podcast:transcript" in xml
    assert [p for p in validate_feed(xml) if p.startswith("Fehler")] == []


def test_feed_drops_references_to_missing_files(st, tmp_path):
    ep = episode(1, chapters_key="media/guid-1/fehlt.chapters.json", transcript_key="media/guid-1/fehlt.vtt")
    xml = render_feed(st, show(), [ep])
    assert "podcast:chapters" not in xml and "podcast:transcript" not in xml
    mp3, _, _ = files(tmp_path)
    st.put_bytes("media/guid-1/da.vtt", b"WEBVTT", "text/vtt")
    xml = render_feed(st, show(), [episode(1, transcript_key="media/guid-1/da.vtt")])
    assert "https://pod.example.org/media/guid-1/da.vtt" in xml


def test_republish_keeps_guid_and_order(st):
    eps = [episode(i) for i in (1, 2, 3)]
    publish_feed(st, show(), eps)
    first = (st.root / "feed.xml").read_text()
    publish_feed(st, show(), list(reversed(eps)))
    assert (st.root / "feed.xml").read_text() == first


# ---------------------------------------------------------------- validate

GOOD = lambda **kw: build_feed(show(**kw), [episode(1), episode(2)], url_for=lambda k: "https://pod.example.org/" + k)  # noqa: E731


def has(problems, text):
    return any(text in p for p in problems)


def test_valid_feed_is_clean():
    assert [p for p in validate_feed(GOOD()) if p.startswith("Fehler")] == []


def test_missing_fields_are_reported_in_german():
    xml = GOOD(owner_email="", title="", description="", language="", category="", cover_key="", author="", owner_name="", link="")
    p = validate_feed(xml)
    for text in ("Inhaber-E-Mail", "Show-Titel", "Show-Beschreibung", "Kategorie", "Cover", "Autor"):
        assert has(p, text), (text, p)
    assert all(x.startswith(("Fehler: ", "Hinweis: ")) for x in p)


def test_owner_email_must_look_valid():
    assert has(validate_feed(GOOD(owner_email="kein-at-zeichen")), "sieht nicht gültig aus")


def test_broken_and_foreign_xml():
    assert has(validate_feed("<rss><channel>"), "kein gültiges XML")
    assert has(validate_feed("<html/>"), "rss")
    assert has(validate_feed('<rss version="2.0"/>'), "channel")


def test_missing_namespace_declaration():
    xml = GOOD().replace(' xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"', "")
    assert has(validate_feed(xml), "iTunes-Namensraum") or has(validate_feed(xml), "kein gültiges XML")


def test_item_problems():
    xml = GOOD()
    xml = xml.replace('length="1234568"', 'length="0"').replace('type="audio/mpeg"', 'type="audio/wav"', 1)
    xml = xml.replace("<guid isPermaLink=\"false\">guid-1</guid>", "<guid isPermaLink=\"false\">guid-2</guid>")
    xml = xml.replace("<itunes:episode>1</itunes:episode>", "<itunes:episode>0</itunes:episode>")
    p = validate_feed(xml)
    assert has(p, "Dateigröße") and has(p, "Audio-Typ") and has(p, "GUID") and has(p, "itunes:episode")


def test_long_description_and_future_and_bad_date():
    xml = GOOD(description="x" * 4001)
    assert has(validate_feed(xml), "4000")
    assert has(validate_feed(GOOD().replace("<pubDate>", "<pubDate>gestern ")), "RFC 2822")
    assert has([str(i) for i in validate_feed_issues(GOOD(), now=1.0)], "Zukunft")


def test_empty_feed_needs_an_episode():
    assert has(validate_feed(build_feed(show(), [], url_for=lambda k: "https://x/" + k)), "mindestens eine")


def test_non_ascii_or_plain_http_enclosure():
    ep = episode(1, audio_key="http://x.example/äöü.mp3")
    p = validate_feed(build_feed(show(), [ep], url_for=lambda k: k))
    assert has(p, "ASCII") and has(p, "https")
    ep = episode(1, audio_key="https://x.example/datei")
    assert has(validate_feed(build_feed(show(), [ep], url_for=lambda k: k)), "Dateiendung")


def test_duplicate_enclosure_urls():
    a, b = episode(1, audio_key="https://x.example/a.mp3"), episode(2, audio_key="https://x.example/a.mp3")
    assert has(validate_feed(build_feed(show(cover_key="https://x/c.png"), [a, b])), "doppelte Adressen")


def test_network_checks_cover_and_enclosures():
    def handler(req):
        u = str(req.url)
        if u.endswith("cover-abcd1234.jpg"):
            return httpx.Response(200, content=jpeg_bytes(1000, 1000))
        if u.endswith("folge-1-aaaa1111.mp3"):
            return httpx.Response(200, headers={"content-length": "1234568", "accept-ranges": "bytes", "content-type": "audio/mpeg"})
        if u.endswith("folge-2-aaaa1111.mp3"):
            return httpx.Response(404)
        return httpx.Response(500)
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        p = validate_feed(GOOD(), check_network=True, client=c)
    assert has(p, "Cover ist 1000×1000") and has(p, "„Folge 2: Öl & „Gas““") and has(p, "HTTP 404")
    assert not has(p, "Byte-Range")  # Folge 1 meldet accept-ranges


def test_network_size_mismatch_and_no_range():
    def handler(req):
        if str(req.url).endswith(".jpg"):
            return httpx.Response(200, content=png_bytes(1400, 1400))
        return httpx.Response(200, headers={"content-length": "99"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        p = validate_feed(GOOD(), check_network=True, client=c)
    assert has(p, "stimmt") and has(p, "Byte-Range") and not has(p, "Cover")
