"""Bisherigen Feed (Spotify for Creators / anchor.fm oder jeder andere Podcast-Feed) übernehmen.

Der Import liest Show-Daten und Cover, lädt jede Audiodatei streamend in eine Temp-Datei, prüft Typ und Größe und
legt sie im Speicher ab. GUID, Veröffentlichungsdatum und alle Metadaten bleiben unverändert. Wiederholbar: Folgen
in `skip_guids` werden übersprungen; eine unterbrochene Datei wird beim nächsten Lauf neu geladen.

Eigenheiten von anchor.fm, die berücksichtigt sind:
- Enclosure-URLs der Form `https://anchor.fm/s/<id>/podcast/play/<n>/https%3A%2F%2F<cdn>%2F….mp3` führen per Weiterleitung
  auf die eigentliche Datei; das URL-kodierte Ziel bleibt unverändert (nicht dekodieren, nicht neu kodieren).
- Neben `description` (HTML in CDATA) steht `itunes:summary` mit maskiertem HTML; die Beschreibung kommt aus
  content:encoded, sonst description, sonst itunes:summary, der Klartext-Auszug aus itunes:summary.
- Dauer als `HH:MM:SS`, `MM:SS` oder Sekunden; fehlt sie oder ist null, wird sie aus der Datei gemessen.
"""
from __future__ import annotations

import email.utils
import hashlib
import html as _html
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote, urlparse

import httpx
from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException
from xml.etree.ElementTree import Element, ParseError

from .imageinfo import image_info
from .models import ImportedEpisode, ImportResult
from .naming import EXT_BY_MIME, media_key, short_hash, slugify
from .sanitize import clean_html, html_to_text, looks_like_html, one_line
from .storage import IMMUTABLE, Storage, StorageError

USER_AGENT = "podcast-studio-importer/1.0"
MAX_FEED_BYTES = 64 * 1024 * 1024
MAX_AUDIO_BYTES = 2 * 1024**3
MAX_IMAGE_BYTES = 32 * 1024 * 1024
ATTEMPTS = 4
BACKOFF_SECONDS = (1.0, 3.0, 8.0)
_sleep = time.sleep  # in Tests ersetzt

_NS = {
    "itunes": "itunes.com/dtds/podcast-1.0.dtd",
    "content": "purl.org/rss/1.0/modules/content",
    "podcast": "podcastindex.org/namespace/1.0",
    "dc": "purl.org/dc/elements/1.1",
}

ProgressFn = Callable[[float, str], None]


class FeedImportError(Exception):
    """Der Feed konnte nicht gelesen werden (deutsche Meldung für Menschen)."""


class _Permanent(Exception):
    """Fehler, bei dem eine Wiederholung nichts bringt."""


# --------------------------------------------------------------------------- XML-Helfer

def _local(tag: object) -> tuple[str, str]:
    if not isinstance(tag, str):
        return "", ""
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return "", tag


def _ns_matches(ns: str, kind: str | None) -> bool:
    if kind is None:
        return ns == ""
    return _NS[kind] in ns


def _kids(el: Element, name: str, kind: str | None = None) -> list[Element]:
    out = []
    for c in el:
        ns, n = _local(c.tag)
        if n == name and _ns_matches(ns, kind):
            out.append(c)
    return out


def _first(el: Element, name: str, kind: str | None = None) -> Element | None:
    k = _kids(el, name, kind)
    return k[0] if k else None


def _text(el: Element, name: str, kind: str | None = None) -> str:
    c = _first(el, name, kind)
    return (c.text or "").strip() if c is not None else ""


# --------------------------------------------------------------------------- Wert-Parser

def parse_duration_ms(value: str | None) -> int | None:
    """Dauer in Millisekunden aus allen üblichen Schreibweisen: `3723`, `3723.5`, `01:02:03`, `1:02:03.250`,
    `62:03`, `1h 2m 3s`, `62 min`, `PT1H2M3S`. Leer, null oder unlesbar ergibt None."""
    if value is None:
        return None
    s = value.strip().lower().replace(",", ".")
    if not s:
        return None
    secs: float | None = None
    if re.fullmatch(r"\d+(\.\d+)?", s):
        secs = float(s)
    elif re.fullmatch(r"\d{1,3}(:\d{1,2}){1,2}(\.\d+)?", s):
        parts = s.split(":")
        nums = [float(p) for p in parts]
        secs = 0.0
        for n in nums:
            secs = secs * 60 + n
    elif re.fullmatch(r"p(\d+d)?(t(\d+(\.\d+)?h)?(\d+(\.\d+)?m)?(\d+(\.\d+)?s)?)?", s):
        d = re.search(r"(\d+)d", s)
        h = re.search(r"(\d+(?:\.\d+)?)h", s)
        m = re.search(r"t.*?(\d+(?:\.\d+)?)m", s)
        sec = re.search(r"(\d+(?:\.\d+)?)s", s)
        secs = (int(d.group(1)) * 86400 if d else 0) + (float(h.group(1)) * 3600 if h else 0) \
            + (float(m.group(1)) * 60 if m else 0) + (float(sec.group(1)) if sec else 0)
    else:
        units = re.findall(r"(\d+(?:\.\d+)?)\s*(h|std|stunden?|hours?|m|min|minutes?|minuten?|s|sec|secs|seconds?|sek)\b", s)
        if units and re.fullmatch(r"(\s*\d+(?:\.\d+)?\s*[a-zäö]+\.?)+", s):
            secs = 0.0
            for num, unit in units:
                n = float(num)
                secs += n * (3600 if unit.startswith(("h", "std")) else 60 if unit.startswith("m") else 1)
    if secs is None or secs <= 0:
        return None
    return int(round(secs * 1000))


def parse_explicit(value: str | None, default: bool = False) -> bool:
    v = (value or "").strip().lower()
    if v in ("true", "yes", "explicit"):
        return True
    if v in ("false", "no", "clean"):
        return False
    return default


def parse_pub_date_ms(value: str | None) -> int | None:
    v = (value or "").strip()
    if not v:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _pos_int(value: str | None) -> int | None:
    try:
        n = int((value or "").strip())
    except ValueError:
        return None
    return n if n > 0 else None


# --------------------------------------------------------------------------- Netzwerk

def _backoff(attempt: int) -> None:
    _sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])


def _fetch_bytes(client: httpx.Client, url: str, max_bytes: int, what: str) -> tuple[bytes, str]:
    """Kleine Datei laden (Feed, Cover, Bild), mit Wiederholungen. -> (Inhalt, Content-Type)."""
    last = ""
    for attempt in range(ATTEMPTS):
        try:
            with client.stream("GET", url) as r:
                if r.status_code in (408, 429) or r.status_code >= 500:
                    last = f"HTTP {r.status_code}"
                elif r.status_code >= 400:
                    raise _Permanent(f"{what}: Der Server antwortet mit HTTP {r.status_code}.")
                else:
                    declared = int(r.headers.get("content-length") or 0)
                    if declared > max_bytes:
                        raise _Permanent(f"{what}: Datei ist mit {declared // 1024 // 1024} MB zu groß.")
                    buf = bytearray()
                    for chunk in r.iter_bytes(64 * 1024):
                        buf += chunk
                        if len(buf) > max_bytes:
                            raise _Permanent(f"{what}: Datei ist zu groß.")
                    if declared and len(buf) != declared and r.headers.get("content-encoding") in (None, "", "identity"):
                        last = "unvollständig übertragen"
                    else:
                        return bytes(buf), r.headers.get("content-type", "")
        except httpx.InvalidURL as e:
            raise _Permanent(f"{what}: ungültige Adresse ({e}).") from e
        except httpx.TransportError as e:
            last = f"{type(e).__name__}"
        if attempt < ATTEMPTS - 1:
            _backoff(attempt)
    raise _Permanent(f"{what}: nach {ATTEMPTS} Versuchen nicht erreichbar ({last}).")


def _download_to(client: httpx.Client, url: str, dest: Path, max_bytes: int, what: str) -> tuple[int, str, str]:
    """Streaming-Download in `dest`. -> (Bytes, Content-Type, sha256-Hex). Wiederholt bei Netzfehlern, 5xx und
    abgebrochener Übertragung."""
    last = ""
    for attempt in range(ATTEMPTS):
        h = hashlib.sha256()
        n = 0
        try:
            with client.stream("GET", url) as r:
                if r.status_code in (408, 429) or r.status_code >= 500:
                    last = f"HTTP {r.status_code}"
                elif r.status_code >= 400:
                    raise _Permanent(f"{what}: Der Server antwortet mit HTTP {r.status_code}.")
                else:
                    declared = int(r.headers.get("content-length") or 0)
                    if declared > max_bytes:
                        raise _Permanent(f"{what}: Datei ist mit {declared // 1024 // 1024} MB zu groß.")
                    with open(dest, "wb") as f:
                        for chunk in r.iter_bytes(256 * 1024):
                            n += len(chunk)
                            if n > max_bytes:
                                raise _Permanent(f"{what}: Datei ist zu groß.")
                            h.update(chunk)
                            f.write(chunk)
                    if declared and n != declared and r.headers.get("content-encoding") in (None, "", "identity"):
                        last = f"unvollständig ({n} von {declared} Byte)"
                    else:
                        return n, r.headers.get("content-type", ""), h.hexdigest()
        except httpx.InvalidURL as e:
            raise _Permanent(f"{what}: ungültige Adresse ({e}).") from e
        except httpx.TransportError as e:
            last = f"{type(e).__name__}"
        if attempt < ATTEMPTS - 1:
            _backoff(attempt)
    raise _Permanent(f"{what}: nach {ATTEMPTS} Versuchen nicht vollständig geladen ({last}).")


def sniff_audio(path: Path) -> str | None:
    """Audio-MIME-Typ anhand der ersten Bytes, None wenn es keine Audiodatei ist (z. B. eine HTML-Fehlerseite)."""
    with open(path, "rb") as f:
        head = f.read(65536)
    if head[:3] == b"ID3":
        return "audio/mpeg"
    if head[4:8] == b"ftyp":
        return "audio/x-m4a" if head[8:12] in (b"M4A ", b"M4B ", b"mp42", b"isom", b"M4P ", b"dash") else "video/mp4"
    if head[:4] == b"OggS":
        return "audio/ogg"
    if head[:4] == b"fLaC":
        return "audio/flac"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    for i in range(0, max(0, len(head) - 3)):  # MPEG-Frame-Synchronisation
        if head[i] == 0xFF and (head[i + 1] & 0xE0) == 0xE0 and (head[i + 1] & 0x18) != 0x08 and (head[i + 2] & 0xF0) != 0xF0:
            return "audio/mpeg"
    return None


_MIME_ALIASES = {
    "audio/mp3": "audio/mpeg", "audio/x-mpeg": "audio/mpeg", "audio/mpeg3": "audio/mpeg", "audio/x-mp3": "audio/mpeg",
    "audio/m4a": "audio/x-m4a", "audio/mp4a-latm": "audio/x-m4a", "audio/aac": "audio/x-m4a",
    "audio/x-wav": "audio/wav", "audio/wave": "audio/wav", "application/ogg": "audio/ogg",
}


def _norm_mime(m: str) -> str:
    m = (m or "").split(";")[0].strip().lower()
    return _MIME_ALIASES.get(m, m)


def _audio_mime(sniffed: str | None, header: str, declared: str) -> str | None:
    """Der Inhalt entscheidet; Kopfzeile und Feed-Angabe dienen nur der Verfeinerung (m4a gegen mp4)."""
    if sniffed is None:
        return None
    for cand in (_norm_mime(declared), _norm_mime(header)):
        if cand.startswith("audio/") and cand in EXT_BY_MIME and (cand == sniffed or {cand, sniffed} <= {"audio/x-m4a", "audio/mp4", "video/mp4"}):
            return "audio/x-m4a" if cand == "audio/mp4" else cand
    return sniffed


def _measure_ms(path: Path) -> int | None:
    try:
        import mutagen
        f = mutagen.File(str(path))
        if f is not None and f.info is not None and getattr(f.info, "length", 0):
            return int(round(f.info.length * 1000))
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------- Hauptfunktion

def import_feed(feed_url: str, storage: Storage, on_progress: ProgressFn | None = None,
                skip_guids: Iterable[str] = (), *, client: httpx.Client | None = None,
                max_audio_bytes: int = MAX_AUDIO_BYTES) -> ImportResult:
    """Feed lesen, Show-Daten und Cover übernehmen, alle Folgen samt Audiodatei im Speicher ablegen.

    `on_progress(anteil 0..1, meldung)` wird nach jedem Schritt gerufen. Wirft `FeedImportError`, wenn der Feed selbst
    nicht lesbar ist; Fehler einzelner Folgen landen in `ImportResult.errors`, der Rest wird trotzdem übernommen.
    """
    skip = set(skip_guids)
    own = client is None
    if own:
        client = httpx.Client(follow_redirects=True, max_redirects=10,
                              timeout=httpx.Timeout(30.0, read=90.0), headers={"User-Agent": USER_AGENT})
    try:
        return _run(feed_url, storage, on_progress or (lambda p, m: None), skip, client, max_audio_bytes)
    finally:
        if own:
            client.close()


def _run(feed_url, storage, progress: ProgressFn, skip: set[str], client: httpx.Client, max_audio_bytes: int) -> ImportResult:
    res = ImportResult()
    progress(0.0, "Feed wird gelesen")
    try:
        raw, _ = _fetch_bytes(client, feed_url, MAX_FEED_BYTES, "Feed")
    except _Permanent as e:
        raise FeedImportError(f"Der bisherige Feed kann nicht geladen werden. {e}") from e
    try:
        root = DET.fromstring(raw.lstrip(), forbid_dtd=True)
    except (ParseError, DefusedXmlException, ValueError) as e:
        raise FeedImportError(f"Die Adresse liefert keinen lesbaren RSS-Feed ({type(e).__name__}).") from e
    channel = _first(root, "channel") if _local(root.tag)[1] == "rss" else None
    if channel is None:
        raise FeedImportError("Die Adresse liefert keinen RSS-Feed (Element „channel“ fehlt).")
    items = _kids(channel, "item")
    res.feed_episode_count = len(items)

    # ---- Show
    desc_raw = _text(channel, "description") or _text(channel, "summary", "itunes")
    cat = _first(channel, "category", "itunes")
    category = (cat.get("text") or "").strip() if cat is not None else ""
    subcat = ""
    if cat is not None:
        sub = _first(cat, "category", "itunes")
        subcat = (sub.get("text") or "").strip() if sub is not None else ""
    owner = _first(channel, "owner", "itunes")
    show = {
        "title": _text(channel, "title"),
        "author": _text(channel, "author", "itunes"),
        "owner_name": _text(owner, "name", "itunes") if owner is not None else "",
        "owner_email": _text(owner, "email", "itunes") if owner is not None else "",
        "description": html_to_text(desc_raw) if looks_like_html(desc_raw) else desc_raw,
        "language": (_text(channel, "language") or "de").strip(),
        "category": category,
        "subcategory": subcat,
        "explicit": parse_explicit(_text(channel, "explicit", "itunes"), False),
        "link": _text(channel, "link"),
        "copyright": _text(channel, "copyright"),
        "cover_key": "",
    }
    if not show["author"]:
        show["author"] = show["owner_name"]
    res.show = show
    if not show["owner_email"]:
        res.warnings.append("Im bisherigen Feed steht keine Inhaber-E-Mail (itunes:email). Sie muss in den Einstellungen ergänzt werden, damit Spotify die Inhaberschaft prüfen kann.")

    # ---- Cover
    img = _first(channel, "image", "itunes")
    cover_url = (img.get("href") or "").strip() if img is not None else ""
    if not cover_url:
        ri = _first(channel, "image")
        cover_url = _text(ri, "url") if ri is not None else ""
    if cover_url:
        progress(0.01, "Cover wird geladen")
        try:
            data, _ = _fetch_bytes(client, cover_url, MAX_IMAGE_BYTES, "Cover")
            info = image_info(data)
            if info is None:
                res.warnings.append("Das Cover des bisherigen Feeds ist weder JPEG noch PNG und wurde nicht übernommen.")
            else:
                mime, w, h = info
                key = f"cover-{short_hash(data)}.{EXT_BY_MIME[mime]}"
                if not (storage.exists(key) and storage.size(key) == len(data)):
                    storage.put_bytes(key, data, mime, IMMUTABLE)
                show["cover_key"] = key
                if w != h or not (1400 <= w <= 3000):
                    res.warnings.append(f"Das Cover hat {w}×{h} Pixel; Spotify und Apple verlangen quadratisch, 1400 bis 3000 Pixel. Bitte in den Einstellungen ein passendes Cover hochladen.")
        except (_Permanent, StorageError) as e:
            res.warnings.append(f"Das Cover konnte nicht übernommen werden. {e}")
    else:
        res.warnings.append("Der bisherige Feed nennt kein Cover.")

    # ---- Folgen
    total = len(items)
    seen: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="ps-import-") as tmpdir:
        for i, item in enumerate(items, 1):
            title = _text(item, "title")
            enc = _first(item, "enclosure")
            guid_el = _first(item, "guid")
            enc_url = (enc.get("url") or "").strip() if enc is not None else ""
            guid = (guid_el.text or "").strip() if guid_el is not None else ""
            progress(0.02 + 0.98 * (i - 1) / max(total, 1), f"Folge {i} von {total}: {title}")
            if not guid:
                if not enc_url:
                    res.warnings.append(f"Folge „{title}“ hat weder GUID noch Audio-Adresse und wurde übersprungen.")
                    continue
                guid = enc_url
                res.warnings.append(f"Folge „{title}“ hat keine GUID; die Audio-Adresse dient als GUID.")
            if guid in seen:
                res.warnings.append(f"GUID {guid} kommt im Feed mehrfach vor; nur die erste Folge wurde übernommen.")
                continue
            seen.add(guid)
            if guid in skip:
                res.skipped_guids.append(guid)
                continue
            if not enc_url:
                res.errors.append({"guid": guid, "title": title, "message": "Die Folge hat kein Audio (enclosure fehlt)."})
                continue
            try:
                ep = _import_episode(client, storage, item, guid, title, enc, enc_url, show, cover_url, feed_url, Path(tmpdir), max_audio_bytes, res)
                res.episodes.append(ep.to_dict())
            except (_Permanent, StorageError, OSError) as e:
                res.errors.append({"guid": guid, "title": title, "message": str(e)})
    progress(1.0, f"Import fertig: {len(res.episodes)} übernommen, {len(res.skipped_guids)} übersprungen, {len(res.errors)} Fehler")
    return res


def _import_episode(client, storage, item, guid, title, enc, enc_url, show, cover_url, feed_url, tmpdir: Path,
                    max_audio_bytes, res: ImportResult) -> ImportedEpisode:
    what = f"Audio von „{title or guid}“"
    tmp = tmpdir / "episode.bin"
    try:
        n, ctype, digest = _download_to(client, enc_url, tmp, max_audio_bytes, what)
        if n < 1024:
            raise _Permanent(f"{what}: Die Datei ist mit {n} Byte zu klein für eine Audiodatei.")
        mime = _audio_mime(sniff_audio(tmp), ctype, enc.get("type") or "")
        if mime is None:
            raise _Permanent(f"{what}: Die Adresse liefert keine Audiodatei (Typ laut Server: {ctype or 'unbekannt'}).")
        declared_len = _pos_int(enc.get("length"))
        if declared_len and abs(declared_len - n) > max(1024, n // 100):
            res.warnings.append(f"Folge „{title}“: Die Größe im Feed ({declared_len}) weicht von der echten Datei ({n}) ab; die echte wird verwendet.")
        ext = EXT_BY_MIME.get(mime, "mp3")
        stem = slugify(title)
        key = media_key(guid, stem, digest[:8], ext)
        if not (storage.exists(key) and storage.size(key) == n):
            storage.put_file(key, tmp, mime, IMMUTABLE)
        duration = parse_duration_ms(_text(item, "duration", "itunes")) or _measure_ms(tmp)
    finally:
        tmp.unlink(missing_ok=True)

    content = _text(item, "encoded", "content")
    desc = _text(item, "description")
    isum = _text(item, "summary", "itunes")
    html_src = content or desc or isum
    description_html = clean_html(html_src)
    summary = one_line(isum) if isum else one_line(description_html)
    guid_el = _first(item, "guid")
    legacy: dict = {
        "source_feed": feed_url,
        "enclosure_url": enc_url,
        "guid_is_permalink": (guid_el.get("isPermaLink", "true").lower() != "false") if guid_el is not None else False,
    }
    link = _text(item, "link")
    if link:
        legacy["link"] = link
    creator = _text(item, "creator", "dc") or _text(item, "author", "itunes")
    if creator:
        legacy["author"] = creator
    itunes_title = _text(item, "title", "itunes")
    if itunes_title and itunes_title != title:
        legacy["title_itunes"] = itunes_title
    pub = _text(item, "pubDate")
    pub_ms = parse_pub_date_ms(pub)
    if pub and pub_ms is None:
        res.warnings.append(f"Folge „{title}“: Das Datum „{pub}“ ist nicht lesbar und fehlt im neuen Feed.")
    legacy["pub_date_raw"] = pub

    iimg = _first(item, "image", "itunes")
    iurl = (iimg.get("href") or "").strip() if iimg is not None else ""
    if iurl:
        legacy["image_url"] = iurl
        if iurl == cover_url and show["cover_key"]:
            legacy["image_key"] = show["cover_key"]
            iurl = ""
    if iurl:
        try:
            data, _ = _fetch_bytes(client, iurl, MAX_IMAGE_BYTES, "Folgenbild")
            info = image_info(data)
            if info:
                ikey = media_key(guid, "image", short_hash(data), EXT_BY_MIME[info[0]])
                if not (storage.exists(ikey) and storage.size(ikey) == len(data)):
                    storage.put_bytes(ikey, data, info[0], IMMUTABLE)
                legacy["image_key"] = ikey
        except (_Permanent, StorageError):
            res.warnings.append(f"Folge „{title}“: Das Folgenbild konnte nicht übernommen werden; die alte Adresse bleibt im Feed.")

    return ImportedEpisode(
        guid=guid,
        title=title,
        description_html=description_html,
        summary=summary,
        pub_date_ms=pub_ms,
        duration_ms=duration,
        number=_pos_int(_text(item, "episode", "itunes")),
        season=_pos_int(_text(item, "season", "itunes")),
        episode_type=(_text(item, "episodeType", "itunes").lower() if _text(item, "episodeType", "itunes").lower() in ("full", "trailer", "bonus") else "full"),
        explicit=parse_explicit(_text(item, "explicit", "itunes"), show["explicit"]),
        audio_key=key,
        audio_bytes=n,
        audio_mime=mime,
        legacy=legacy,
    )
