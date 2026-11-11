"""RSS-Feed bauen: RSS 2.0 mit iTunes-, Podcasting-2.0- und content-Namespace.

Anforderungen (Stand 11.11.2026, siehe docs/SPEC.md Abschnitt 10):
- Apple „A Podcaster's Guide to RSS“ (help.apple.com/itc/podcasts_connect): Pflicht im Kanal title, description (höchstens
  4000 Byte), itunes:image, language, itunes:category, itunes:explicit (true/false); empfohlen itunes:author, link.
  Pro Folge Pflicht title, enclosure (url, length, type), guid; empfohlen pubDate (RFC 2822), description (höchstens
  10 000 Zeichen), itunes:duration (Sekunden), itunes:explicit, itunes:image. Namespaces auf der zweiten Zeile, UTF-8,
  Sonderzeichen als Entities, Dateinamen nur ASCII mit Endung.
- Spotify: liest den Feed wie Apple und prüft die Inhaberschaft über itunes:owner/itunes:email.
- Podcast-Namespace (podcastindex.org): podcast:guid, podcast:chapters (application/json+chapters),
  podcast:transcript (text/vtt, rel="captions"); URLs ausschließlich https.

Die Ausgabe ist deterministisch: gleiche Eingabe ergibt gleichen Text (kein „jetzt“, keine Zufallswerte).
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Callable, Iterable

from .models import FeedEpisode, Show
from .sanitize import clean_html, one_line

GENERATOR = "Podcast-Studio"
NS_PODCAST_GUID = uuid.UUID("ead4c236-bf58-58c6-a2c6-a6b28d128cb6")

_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_ENT = {"&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&apos;", '"': "&quot;",
        "©": "&#xA9;", "℗": "&#x2117;", "™": "&#x2122;"}


def esc(text: object) -> str:
    s = _BAD_XML.sub("", str(text if text is not None else ""))
    return "".join(_ENT.get(c, c) for c in s)


def cdata(text: str) -> str:
    s = _BAD_XML.sub("", text).replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{s}]]>"


def rfc2822(ms: int) -> str:
    return format_datetime(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))


def podcast_guid(feed_url: str) -> str:
    """podcast:guid nach Podcast-Namespace: UUIDv5 der Feed-Adresse ohne Schema und ohne abschließenden Schrägstrich."""
    s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", feed_url.strip()).rstrip("/")
    return str(uuid.uuid5(NS_PODCAST_GUID, s))


def _is_abs(v: str) -> bool:
    return v.startswith("http://") or v.startswith("https://")


def build_feed(show: Show, episodes: Iterable[FeedEpisode], *,
               url_for: Callable[[str], str] | None = None, feed_url: str | None = None,
               enclosure_url: Callable[[str], str] | None = None,
               episode_link: Callable[[FeedEpisode], str | None] | None = None) -> str:
    """Feed als Text. `url_for` macht aus Speicherschlüsseln absolute URLs (`storage.url`); ohne sie müssen die
    Schlüssel schon absolute URLs sein. `feed_url` (sonst `show.feed_url`, sonst `url_for("feed.xml")`) erzeugt
    atom:link und podcast:guid. `enclosure_url` (Audioschlüssel -> Adresse) ersetzt `url_for` nur für die Audiodateien der
    Enclosures (Download-Zählung über die App oder einen Dienst, SPEC Abschnitt 10); Cover, Kapitel und Transkripte
    bleiben unberührt. `episode_link` liefert die Adresse der Folgenseite für `link`; sie geht einem importierten Link der Folge vor."""
    def absu(v: str) -> str:
        return v if _is_abs(v) or url_for is None else url_for(v)

    feed_url = feed_url or show.feed_url or (url_for("feed.xml") if url_for else "")
    eps = sorted(episodes, key=lambda e: (-(e.pub_date_ms or 0), e.guid))
    L: list[str] = []
    a = L.append
    a('<?xml version="1.0" encoding="UTF-8"?>')
    a('<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
      'xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom" '
      'xmlns:podcast="https://podcastindex.org/namespace/1.0">')
    a("<channel>")
    a(f"<title>{esc(show.title)}</title>")
    if show.link:
        a(f"<link>{esc(show.link)}</link>")
    a(f"<description>{esc(show.description)}</description>")
    a(f"<language>{esc(show.language or 'de')}</language>")
    if show.copyright:
        a(f"<copyright>{esc(show.copyright)}</copyright>")
    a(f"<generator>{GENERATOR}</generator>")
    newest = max((e.pub_date_ms for e in eps if e.pub_date_ms), default=None)
    if newest:
        a(f"<lastBuildDate>{rfc2822(newest)}</lastBuildDate>")
    if feed_url:
        a(f'<atom:link href="{esc(feed_url)}" rel="self" type="application/rss+xml"/>')
        a(f"<podcast:guid>{podcast_guid(feed_url)}</podcast:guid>")
    a(f"<itunes:type>{'serial' if show.show_type == 'serial' else 'episodic'}</itunes:type>")
    author = show.author or show.owner_name
    if author:
        a(f"<itunes:author>{esc(author)}</itunes:author>")
    a(f"<itunes:explicit>{'true' if show.explicit else 'false'}</itunes:explicit>")
    a("<itunes:owner>")
    if show.owner_name:
        a(f"<itunes:name>{esc(show.owner_name)}</itunes:name>")
    if show.owner_email:
        a(f"<itunes:email>{esc(show.owner_email)}</itunes:email>")
    a("</itunes:owner>")
    if show.cover_key:
        a(f'<itunes:image href="{esc(absu(show.cover_key))}"/>')
    if show.category:
        if show.subcategory:
            a(f'<itunes:category text="{esc(show.category)}"><itunes:category text="{esc(show.subcategory)}"/></itunes:category>')
        else:
            a(f'<itunes:category text="{esc(show.category)}"/>')

    for e in eps:
        a("<item>")
        a(f"<title>{esc(e.title)}</title>")
        if e.itunes_title and e.itunes_title != e.title:
            a(f"<itunes:title>{esc(e.itunes_title)}</itunes:title>")
        html = clean_html(e.description_html) or clean_html(e.summary)
        if html:
            a(f"<description>{cdata(html)}</description>")
            a(f"<content:encoded>{cdata(html)}</content:encoded>")
        summary = (e.summary or one_line(html))[:4000]
        if summary:
            a(f"<itunes:summary>{esc(summary)}</itunes:summary>")
        # eigene Folgenseite vor dem importierten Link: nach dem Umzug soll der Feed nicht mehr zum alten Hoster führen
        link = (episode_link(e) if episode_link else None) or (e.legacy or {}).get("link") or show.link
        if link:
            a(f"<link>{esc(link)}</link>")
        a(f'<guid isPermaLink="false">{esc(e.guid)}</guid>')
        if e.pub_date_ms:
            a(f"<pubDate>{rfc2822(e.pub_date_ms)}</pubDate>")
        a(f'<enclosure url="{esc(enclosure_url(e.audio_key) if enclosure_url and not _is_abs(e.audio_key) else absu(e.audio_key))}" length="{int(e.audio_bytes)}" type="{esc(e.audio_mime or "audio/mpeg")}"/>')
        if e.duration_ms:
            a(f"<itunes:duration>{max(1, round(e.duration_ms / 1000))}</itunes:duration>")
        if e.season:
            a(f"<itunes:season>{int(e.season)}</itunes:season>")
        if e.number:
            a(f"<itunes:episode>{int(e.number)}</itunes:episode>")
        et = e.episode_type if e.episode_type in ("full", "trailer", "bonus") else "full"
        a(f"<itunes:episodeType>{et}</itunes:episodeType>")
        explicit = show.explicit if e.explicit is None else e.explicit
        a(f"<itunes:explicit>{'true' if explicit else 'false'}</itunes:explicit>")
        img = (e.legacy or {}).get("image_key") or (e.legacy or {}).get("image_url")
        if img:
            a(f'<itunes:image href="{esc(absu(img))}"/>')
        if e.chapters_key:
            a(f'<podcast:chapters url="{esc(absu(e.chapters_key))}" type="application/json+chapters"/>')
        if e.transcript_key:
            a(f'<podcast:transcript url="{esc(absu(e.transcript_key))}" type="text/vtt" language="{esc(show.language or "de")}" rel="captions"/>')
        a("</item>")
    a("</channel>")
    a("</rss>")
    return "\n".join(L) + "\n"
