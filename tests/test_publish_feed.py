import re
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as DET

from publish.feed import build_feed, podcast_guid, rfc2822
from publish.models import FeedEpisode, Show
from publish.sanitize import clean_html, html_to_text
from publish.validate import validate_feed
from tests.publish_fixtures.helpers import episode, show

NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
    "podcast": "https://podcastindex.org/namespace/1.0",
}
URL = lambda k: "https://pod.example.org/" + k  # noqa: E731


def parse(xml):
    return DET.fromstring(xml.encode("utf-8"))


def test_required_channel_fields():
    root = parse(build_feed(show(), [episode(1)], url_for=URL))
    ch = root.find("channel")
    assert root.get("version") == "2.0"
    assert ch.findtext("title") == "Zwei am Mikro"
    assert ch.findtext("language") == "de"
    assert ch.findtext("link") == "https://example.org/"
    assert ch.findtext("copyright") == "© 2026 Beispiel & Muster"
    assert ch.find("itunes:image", NS).get("href") == "https://pod.example.org/cover-abcd1234.jpg"
    assert ch.findtext("itunes:explicit", namespaces=NS) == "true"
    assert ch.findtext("itunes:author", namespaces=NS).startswith("Anna Beispiel")
    assert ch.findtext("itunes:type", namespaces=NS) == "episodic"
    owner = ch.find("itunes:owner", NS)
    assert owner.findtext("itunes:email", namespaces=NS) == "podcast@example.org"
    assert owner.findtext("itunes:name", namespaces=NS) == "Anna Beispiel"
    cat = ch.find("itunes:category", NS)
    assert cat.get("text") == "News" and cat.find("itunes:category", NS).get("text") == "News Commentary"
    assert ch.find("atom:link", NS).get("href") == "https://pod.example.org/feed.xml"
    assert ch.find("atom:link", NS).get("rel") == "self"
    assert ch.findtext("podcast:guid", namespaces=NS) == podcast_guid("https://pod.example.org/feed.xml")


def test_namespaces_on_second_line_and_utf8_declaration():
    xml = build_feed(show(), [episode(1)], url_for=URL)
    lines = xml.splitlines()
    assert lines[0] == '<?xml version="1.0" encoding="UTF-8"?>'
    assert "xmlns:itunes=" in lines[1] and "xmlns:podcast=" in lines[1] and "xmlns:content=" in lines[1]


def test_episode_fields():
    ep = episode(3, season=2, episode_type="bonus", explicit=False, chapters_key="media/guid-3/c.chapters.json",
                 transcript_key="media/guid-3/t.vtt", audio_mime="audio/mpeg")
    it = parse(build_feed(show(), [ep], url_for=URL)).find("channel/item")
    enc = it.find("enclosure")
    assert enc.get("url") == "https://pod.example.org/media/guid-3/folge-3-aaaa1111.mp3"
    assert enc.get("length") == str(ep.audio_bytes) and enc.get("type") == "audio/mpeg"
    g = it.find("guid")
    assert g.text == "guid-3" and g.get("isPermaLink") == "false"
    assert it.findtext("pubDate") == rfc2822(ep.pub_date_ms)
    assert re.fullmatch(r"[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} [+-]\d{4}", it.findtext("pubDate"))
    assert it.findtext("itunes:duration", namespaces=NS) == "3723"
    assert it.findtext("itunes:episode", namespaces=NS) == "3"
    assert it.findtext("itunes:season", namespaces=NS) == "2"
    assert it.findtext("itunes:episodeType", namespaces=NS) == "bonus"
    assert it.findtext("itunes:explicit", namespaces=NS) == "false"
    assert it.find("podcast:chapters", NS).get("type") == "application/json+chapters"
    assert it.find("podcast:chapters", NS).get("url") == "https://pod.example.org/media/guid-3/c.chapters.json"
    tr = it.find("podcast:transcript", NS)
    assert (tr.get("type"), tr.get("rel"), tr.get("language")) == ("text/vtt", "captions", "de")
    assert it.findtext("content:encoded", namespaces=NS) == "<p>Beschreibung 3</p>"
    assert it.findtext("description") == "<p>Beschreibung 3</p>"
    assert it.findtext("itunes:summary", namespaces=NS) == "Kurz 3."


def test_optional_fields_omitted_and_explicit_inherited():
    ep = episode(1, number=None, season=None, chapters_key=None, transcript_key=None, duration_ms=None, explicit=None)
    it = parse(build_feed(show(explicit=True), [ep], url_for=URL)).find("channel/item")
    assert it.find("itunes:episode", NS) is None and it.find("itunes:season", NS) is None
    assert it.find("podcast:chapters", NS) is None and it.find("podcast:transcript", NS) is None
    assert it.find("itunes:duration", NS) is None
    assert it.findtext("itunes:explicit", namespaces=NS) == "true"
    assert it.findtext("itunes:episodeType", namespaces=NS) == "full"


def test_escaping_umlauts_and_special_characters():
    s = show(title='Öl & Gas <live> "Zitat" \'x\'', description="Müller & Söhne ℗ ™ ©")
    ep = episode(1, title="Straße > Weg & „Ziel“", summary="a < b & c", description_html="<p>Zeile ]]> Ende & mehr</p>")
    xml = build_feed(s, [ep], url_for=URL)
    ch = parse(xml).find("channel")  # parsebar = wohlgeformt
    assert ch.findtext("title") == 'Öl & Gas <live> "Zitat" \'x\''
    assert ch.findtext("description") == "Müller & Söhne ℗ ™ ©"
    assert "&#xA9;" in xml and "&#x2117;" in xml and "&#x2122;" in xml  # Apple: Zeichen als Entities
    assert "Öl" in xml and "Straße" in xml  # Umlaute bleiben UTF-8
    it = ch.find("item")
    assert it.findtext("title") == "Straße > Weg & „Ziel“"
    assert it.findtext("itunes:summary", namespaces=NS) == "a < b & c"
    assert "]]>" not in it.findtext("description")  # CDATA-Ende wird geteilt, Text bleibt gleich
    assert "Zeile " in it.findtext("description") and "Ende &amp; mehr" in it.findtext("description")


def test_control_characters_are_removed():
    ep = episode(1, title="A\x00B\x0bC")
    assert parse(build_feed(show(), [ep], url_for=URL)).find("channel/item/title").text == "ABC"


def test_html_is_sanitized_in_feed():
    ep = episode(1, description_html='<p onclick="x()">Hi <script>alert(1)</script><b>fett</b> <a href="javascript:alert(1)">bö</a> <a href="https://ok.example/">ok</a></p><div>Teil</div>')
    d = parse(build_feed(show(), [ep], url_for=URL)).find("channel/item/description").text
    assert "<script" not in d and "onclick" not in d and "javascript:" not in d
    assert "<strong>fett</strong>" in d and 'href="https://ok.example/"' in d and "Teil" in d


def test_order_newest_first_and_guids_unchanged():
    eps = [episode(1), episode(3), episode(2, guid="11111111-aaaa-4aaa-8aaa-000000000003")]
    root = parse(build_feed(show(), eps, url_for=URL))
    guids = [g.text for g in root.findall("channel/item/guid")]
    assert guids == ["guid-3", "11111111-aaaa-4aaa-8aaa-000000000003", "guid-1"]
    assert root.find("channel/lastBuildDate").text == rfc2822(eps[1].pub_date_ms)


def test_deterministic():
    eps = [episode(i) for i in range(1, 6)]
    a = build_feed(show(), eps, url_for=URL)
    assert a == build_feed(show(), list(reversed(eps)), url_for=URL)
    assert a == build_feed(show(), eps, url_for=URL)


def test_absolute_urls_pass_through_and_legacy_fields():
    ep = episode(1, audio_key="https://cdn.old.example/a.mp3",
                 legacy={"link": "https://old.example/ep1", "image_key": "media/guid-1/image-ab.jpg"})
    it = parse(build_feed(show(), [ep], url_for=URL)).find("channel/item")
    assert it.find("enclosure").get("url") == "https://cdn.old.example/a.mp3"
    assert it.findtext("link") == "https://old.example/ep1"
    assert it.find("itunes:image", NS).get("href") == "https://pod.example.org/media/guid-1/image-ab.jpg"


def test_feed_without_url_for_and_feed_url():
    xml = build_feed(show(cover_key="https://x/c.jpg", feed_url="https://own.example/feed.xml"), [episode(1, audio_key="https://x/a.mp3")])
    ch = parse(xml).find("channel")
    assert ch.find("atom:link", NS).get("href") == "https://own.example/feed.xml"


def test_podcast_guid_ignores_scheme_and_trailing_slash():
    assert podcast_guid("https://a.example/feed.xml") == podcast_guid("http://a.example/feed.xml/")
    assert podcast_guid("https://a.example/feed.xml") != podcast_guid("https://b.example/feed.xml")


def test_valid_feed_has_no_errors():
    xml = build_feed(show(), [episode(1), episode(2)], url_for=URL, )
    problems = validate_feed(xml, check_network=False)
    assert [p for p in problems if p.startswith("Fehler")] == []


def test_model_from_dict_and_row():
    s = Show.from_dict({"title": "T", "explicit": 1, "unbekannt": 5, "cover_key": None})
    assert s.explicit is True and s.cover_key == ""
    row = {"guid": "g", "title": "t", "audio_key": "media/g/x-1.mp3", "audio_bytes": 10, "audio_mime": None,
           "published_at": 5, "publish_at": 9, "duration_ms": 1000, "summary": None, "description_html": None,
           "number": 0, "season": None, "episode_type": None, "explicit": 1, "source": "recorded", "legacy": '{"a": 1}'}
    e = FeedEpisode.from_row(row)
    assert e.pub_date_ms == 5 and e.audio_mime == "audio/mpeg" and e.number is None and e.explicit is True
    assert e.chapters_key == "media/g/x-1.chapters.json" and e.transcript_key == "media/g/x-1.vtt"
    assert e.legacy == {"a": 1}
    imp = FeedEpisode.from_row({**row, "source": "imported"})
    assert imp.chapters_key is None and imp.transcript_key is None


def test_clean_html_and_text():
    assert clean_html("") == "" and clean_html(None) == ""
    assert clean_html("Zeile 1\n\nZeile 2 & Co") == "<p>Zeile 1</p><p>Zeile 2 &amp; Co</p>"
    out = clean_html("<h2>Titel</h2><ul><li><i>a</i></li></ul><img src=x onerror=y><style>p{}</style>")
    assert out == "<p><strong>Titel</strong></p><ul><li><em>a</em></li></ul>"
    assert clean_html(out) == out  # idempotent
    assert html_to_text("<p>Eins &amp; zwei</p><ul><li>a</li><li>b</li></ul>") == "Eins & zwei\n- a\n- b"
