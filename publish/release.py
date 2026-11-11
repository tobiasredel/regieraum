"""Hilfen für den Server beim Veröffentlichen: Dateien hochladen, Feed bauen und hochladen, Dateien entfernen.

Schlüssel-Schema je selbst aufgenommener Folge (alles unveränderlich, Cache-Control immutable):

    media/<guid>/<titel-slug>-<hash>.mp3
    media/<guid>/<titel-slug>-<hash>.chapters.json
    media/<guid>/<titel-slug>-<hash>.vtt

`<hash>` sind die ersten 8 Hex-Zeichen von SHA-256 über alle drei Dateien. Ändert sich eine davon, entsteht ein neuer
Name und die alten Dateien werden gelöscht. Aus dem Audio-Schlüssel lassen sich die beiden anderen ableiten
(`publish.naming.sibling_keys`); der Server muss deshalb nur `audio_key` speichern.
"""
from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote

from .feed import build_feed
from .imageinfo import image_info
from .models import FeedEpisode, Show
from . import site as site_pages
from .naming import EXT_BY_MIME, guid_slug, short_hash, sibling_keys, slugify
from .storage import FEED_CACHE, IMMUTABLE, Storage

FEED_KEY = "feed.xml"
FEED_TYPE = "application/rss+xml; charset=utf-8"
PAGE_TYPE = "text/html; charset=utf-8"


class CoverError(ValueError):
    """Das Cover erfüllt die Vorgaben nicht (deutsche Meldung für Menschen)."""


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    try:  # sqlite3.Row
        if hasattr(obj, "keys") and name in obj.keys():
            return obj[name]
    except Exception:
        pass
    return getattr(obj, name, default)


def _hash_files(paths: Iterable[Path | None]) -> str:
    h = hashlib.sha256()
    for p in paths:
        if p is None:
            h.update(b"\0")
            continue
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        h.update(b"\1")
    return h.hexdigest()[:8]


def _put(storage: Storage, key: str, path: Path, content_type: str) -> None:
    if storage.exists(key) and storage.size(key) == path.stat().st_size:
        return  # gleicher unveränderlicher Name, gleiche Größe: schon da
    storage.put_file(key, path, content_type, IMMUTABLE)


def _old_keys(episode: Any) -> set[str]:
    keys: set[str] = set()
    audio = _get(episode, "audio_key")
    if audio:
        keys.add(audio)
        if _get(episode, "source") == "recorded" or audio.endswith(".mp3"):
            keys.update(sibling_keys(audio))
    for n in ("chapters_key", "transcript_key"):
        if _get(episode, n):
            keys.add(_get(episode, n))
    return keys


def delete_keys(storage: Storage, keys: Iterable[str]) -> list[str]:
    """Löscht Dateien (Aufräumen nach dem Feed-Update). -> gelöschte Schlüssel."""
    done = sorted(set(keys))
    for k in done:
        storage.delete(k)
    return done


def upload_episode_assets(storage: Storage, episode: Any, mp3_path: str | Path, chapters_path: str | Path | None,
                          transcript_vtt_path: str | Path | None, *, delete_old: bool = True) -> dict[str, Any]:
    """Lädt MP3, Kapitel und Transkript einer Folge hoch und löscht die alten Dateien (falls `episode` schon
    `audio_key` usw. trägt). Mit `delete_old=False` bleiben sie liegen und stehen als `obsolete_keys` im Ergebnis:
    Der Server baut erst den Feed neu und ruft dann `delete_keys`, damit der öffentliche Feed nie auf eine gelöschte
    Datei zeigt. `episode` ist eine DB-Zeile, ein Dict oder ein Objekt mit `guid`, `title` und optional
    `audio_key`. -> {audio_key, audio_bytes, audio_mime, chapters_key, transcript_key} (die beiden letzten None, wenn
    die jeweilige Datei fehlt)."""
    mp3 = Path(mp3_path)
    chapters = Path(chapters_path) if chapters_path else None
    vtt = Path(transcript_vtt_path) if transcript_vtt_path else None
    guid = _get(episode, "guid")
    if not guid:
        raise ValueError("Die Folge hat keine GUID.")
    suffix = mp3.suffix.lower().lstrip(".") or "mp3"
    mime = {"mp3": "audio/mpeg", "m4a": "audio/x-m4a"}.get(suffix)
    if mime is None:
        raise ValueError(f"Nicht unterstütztes Audioformat: .{suffix}")
    base = f"media/{guid_slug(guid)}/{slugify(_get(episode, 'title') or '')}-{_hash_files([mp3, chapters, vtt])}"
    audio_key = f"{base}.{suffix}"
    chapters_key = f"{base}.chapters.json" if chapters else None
    transcript_key = f"{base}.vtt" if vtt else None

    _put(storage, audio_key, mp3, mime)
    if chapters:
        _put(storage, chapters_key, chapters, "application/json+chapters")
    if vtt:
        _put(storage, transcript_key, vtt, "text/vtt")

    new = {audio_key, chapters_key, transcript_key}
    obsolete = sorted(_old_keys(episode) - new)
    result = {
        "audio_key": audio_key,
        "audio_bytes": mp3.stat().st_size,
        "audio_mime": mime,
        "chapters_key": chapters_key,
        "transcript_key": transcript_key,
    }
    if delete_old:
        delete_keys(storage, obsolete)
    else:
        result["obsolete_keys"] = obsolete
    return result


def remove_episode_assets(storage: Storage, episode: Any) -> list[str]:
    """Löscht alle Dateien einer Folge (`unpublish`). Das gemeinsame Show-Cover bleibt. -> gelöschte Schlüssel."""
    keys = _old_keys(episode)
    legacy = _get(episode, "legacy") or {}
    if isinstance(legacy, str):
        import json
        legacy = json.loads(legacy or "{}")
    img = legacy.get("image_key")
    if img and not img.startswith("cover-"):
        keys.add(img)
    for k in sorted(keys):
        storage.delete(k)
    return sorted(keys)


def upload_cover(storage: Storage, path: str | Path, old_key: str | None = None) -> str:
    """Prüft ein Cover (JPEG oder PNG, quadratisch, 1400 bis 3000 Pixel) und lädt es als `cover-<hash>.<jpg|png>`
    hoch. -> Schlüssel. Wirft `CoverError` mit deutscher Meldung. Der alte Schlüssel wird gelöscht."""
    data = Path(path).read_bytes()
    info = image_info(data)
    if info is None:
        raise CoverError("Das Cover muss ein JPEG- oder PNG-Bild sein.")
    mime, w, h = info
    if w != h:
        raise CoverError(f"Das Cover muss quadratisch sein, ist aber {w}×{h} Pixel groß.")
    if not 1400 <= w <= 3000:
        raise CoverError(f"Das Cover muss zwischen 1400×1400 und 3000×3000 Pixel groß sein, ist aber {w}×{h}.")
    key = f"cover-{short_hash(data)}.{EXT_BY_MIME[mime]}"
    if not (storage.exists(key) and storage.size(key) == len(data)):
        storage.put_bytes(key, data, mime, IMMUTABLE)
    if old_key and old_key != key:
        storage.delete(old_key)
    return key


def _coerce(show: Any, episodes: Iterable[Any]) -> tuple[Show, list[FeedEpisode]]:
    if not isinstance(show, Show):
        show = Show.from_dict(show)
    eps = [e if isinstance(e, FeedEpisode) else FeedEpisode.from_row(e) for e in episodes]
    return show, eps


def enclosure_url_fn(storage: Storage, mode: str = "off", prefix: str = "", app_base: str = "") -> Callable[[str], str] | None:
    """Adresse der Audiodatei im Feed und auf der Seite je Zählmodus (SPEC Abschnitt 10, Downloads):
    `app` -> `<app_base>/d/<schlüssel>` (die App zählt und leitet weiter), `prefix` -> Präfix eines Zähldienstes vor der
    Adresse im Speicher (z. B. `https://op3.dev/e/`), sonst None (Adresse im Speicher). Gilt nur für Audiodateien."""
    if mode == "app" and app_base:
        return lambda key: f"{app_base.rstrip('/')}/d/{quote(key)}"
    if mode == "prefix" and prefix.strip():
        pre = prefix.strip()
        pre = pre if pre.endswith("/") else pre + "/"
        return lambda key: pre + storage.url(key)
    return None


def render_feed(storage: Storage, show: Any, episodes: Iterable[Any], *,
                enclosure_url: Callable[[str], str] | None = None, site: "site_pages.SiteOptions | None" = None) -> str:
    """Feed-Text, wie `publish_feed` ihn hochladen würde (für die Vorschau). Verweise auf Kapitel- oder
    Transkriptdateien, die im Speicher fehlen, entfallen."""
    show, eps = _coerce(show, episodes)
    fixed = []
    for e in eps:
        ch, tr = e.chapters_key, e.transcript_key
        if ch and not storage.exists(ch):
            ch = None
        if tr and not storage.exists(tr):
            tr = None
        fixed.append(dataclasses.replace(e, chapters_key=ch, transcript_key=tr) if (ch, tr) != (e.chapters_key, e.transcript_key) else e)
    link = (lambda e: storage.url(site_pages.episode_page_key(e.guid))) if site is not None and site.episode_links else None
    return build_feed(show, fixed, url_for=storage.url, enclosure_url=enclosure_url, episode_link=link)


def publish_site(storage: Storage, show: Any, episodes: Iterable[Any], site: "site_pages.SiteOptions", *,
                 enclosure_url: Callable[[str], str] | None = None) -> list[str]:
    """Baut die öffentliche Seite (`index.html`, `folgen/<slug>.html`) und lädt geänderte Seiten hoch (Cache 5 Minuten).
    Was seit dem letzten Lauf unverändert ist (`site.known_hashes`), bleibt liegen. -> hochgeladene Schlüssel."""
    show, eps = _coerce(show, episodes)
    pages = site_pages.build_pages(show, eps, site, url_for=storage.url, enclosure_url=enclosure_url, feed_url=storage.url(FEED_KEY))
    done = []
    for key, html in pages.items():
        digest = site_pages.checksum(html)
        if site.known_hashes.get(key) == digest:
            continue
        storage.put_bytes(key, html.encode("utf-8"), PAGE_TYPE, FEED_CACHE)
        site.known_hashes[key] = digest
        done.append(key)
    for key in [k for k in site.known_hashes if k not in pages]:
        site.known_hashes.pop(key)  # Folge nicht mehr veröffentlicht (ihre Seite löscht `remove_episode_assets`)
    return done


def remove_site(storage: Storage, episodes: Iterable[Any]) -> list[str]:
    """Löscht die öffentliche Seite (Seite abgeschaltet): `index.html` und die Seiten der übergebenen Folgen."""
    keys = [site_pages.INDEX_KEY] + [site_pages.episode_page_key(_get(e, "guid")) for e in episodes if _get(e, "guid")]
    return delete_keys(storage, keys)


def publish_feed(storage: Storage, show: Any, episodes: Iterable[Any], *,
                 enclosure_url: Callable[[str], str] | None = None, site: "site_pages.SiteOptions | None" = None) -> str:
    """Baut `feed.xml` aus den veröffentlichten Folgen, lädt ihn hoch (Cache 5 Minuten) und liefert die Feed-URL.
    `episodes` enthält genau die Folgen, die im Feed stehen sollen (Status `published`). Mit `site` entstehen zugleich
    die Seiten der Show (`publish_site`); `enclosure_url` siehe `enclosure_url_fn`."""
    episodes = list(episodes)
    xml = render_feed(storage, show, episodes, enclosure_url=enclosure_url, site=site)
    storage.put_bytes(FEED_KEY, xml.encode("utf-8"), FEED_TYPE, FEED_CACHE)
    if site is not None:
        publish_site(storage, show, episodes, site, enclosure_url=enclosure_url)
    return storage.url(FEED_KEY)
