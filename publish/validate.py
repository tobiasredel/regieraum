"""Feed prüfen und Hinweise in deutschem Klartext liefern (für die Einstellungsseite).

`validate_feed(xml)` prüft nur den Text. Mit `check_network=True` werden zusätzlich das Cover geladen (Format und
Größe) und die Audiodateien der neuesten Folgen per HEAD abgefragt (erreichbar, Größe, Byte-Range).
Jeder Hinweis beginnt mit „Fehler:“ (Apple oder Spotify lehnen den Feed so ab) oder „Hinweis:“ (sollte behoben werden).
"""
from __future__ import annotations

import email.utils
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException
from xml.etree.ElementTree import Element, ParseError

from .imageinfo import image_info

NS_ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
NS_CONTENT = "http://purl.org/rss/1.0/modules/content/"
NS_PODCAST = "https://podcastindex.org/namespace/1.0"
I = "{%s}" % NS_ITUNES
P = "{%s}" % NS_PODCAST

APPLE_CATEGORIES = {
    "Arts", "Business", "Comedy", "Education", "Fiction", "Government", "History", "Health & Fitness", "Kids & Family",
    "Leisure", "Music", "News", "Religion & Spirituality", "Science", "Society & Culture", "Sports", "Technology",
    "True Crime", "TV & Film",
}
AUDIO_TYPES = {"audio/mpeg", "audio/x-m4a", "audio/mp4", "video/mp4", "video/x-m4v", "video/quicktime", "application/pdf"}
_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>.]{2,}$")


@dataclass(frozen=True)
class Issue:
    level: str  # "Fehler" oder "Hinweis"
    text: str

    def __str__(self) -> str:
        return f"{self.level}: {self.text}"


def _t(el: Element | None, tag: str) -> str:
    if el is None:
        return ""
    c = el.find(tag)
    return (c.text or "").strip() if c is not None else ""


def validate_feed_issues(xml: str, *, check_network: bool = False, client: httpx.Client | None = None,
                         max_enclosures: int = 10, now: float | None = None) -> list[Issue]:
    out: list[Issue] = []
    err = lambda t: out.append(Issue("Fehler", t))
    hint = lambda t: out.append(Issue("Hinweis", t))
    try:
        root = DET.fromstring(xml.lstrip().encode("utf-8"), forbid_dtd=True)
    except (ParseError, DefusedXmlException, ValueError) as e:
        return [Issue("Fehler", f"Der Feed ist kein gültiges XML ({e}).")]
    if root.tag != "rss":
        return [Issue("Fehler", "Das Wurzelelement muss „rss“ sein (RSS 2.0).")]
    if root.get("version") != "2.0":
        err("Das Attribut version von „rss“ muss „2.0“ sein.")
    ch = root.find("channel")
    if ch is None:
        return out + [Issue("Fehler", "Das Element „channel“ fehlt.")]

    head = xml.lstrip().splitlines()[:3]
    declared = " ".join(head)
    if NS_ITUNES not in declared:
        err("Der iTunes-Namensraum (xmlns:itunes) ist nicht in den ersten Zeilen des Feeds deklariert; Apple ignoriert sonst alle itunes-Felder.")
    if NS_PODCAST not in declared:
        hint("Der Podcast-Namensraum (xmlns:podcast) fehlt; Kapitel und Transkripte würden nicht erkannt.")

    # ---- Kanal
    if not _t(ch, "title"):
        err("Der Show-Titel fehlt.")
    desc = _t(ch, "description")
    if not desc:
        err("Die Show-Beschreibung fehlt.")
    elif len(desc.encode("utf-8")) > 4000:
        err(f"Die Show-Beschreibung ist mit {len(desc.encode('utf-8'))} Byte länger als die erlaubten 4000.")
    lang = _t(ch, "language")
    if not lang:
        err("Die Sprache (language) fehlt.")
    elif not re.fullmatch(r"[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*", lang):
        err(f"Die Sprache „{lang}“ ist kein gültiger ISO-Code (z. B. „de“).")
    if not _t(ch, "link"):
        hint("Die Webseite der Show (link) fehlt; Apple empfiehlt sie.")
    if not _t(ch, I + "author"):
        hint("Der Autor (itunes:author) fehlt; Apple empfiehlt ihn.")
    ex = _t(ch, I + "explicit")
    if ex not in ("true", "false"):
        err("itunes:explicit fehlt oder ist nicht „true“ oder „false“.")
    typ = _t(ch, I + "type")
    if typ and typ not in ("episodic", "serial"):
        err(f"itunes:type „{typ}“ ist ungültig; erlaubt sind „episodic“ und „serial“.")
    owner = ch.find(I + "owner")
    email_ = _t(owner, I + "email")
    if not email_:
        err("Die Inhaber-E-Mail (itunes:owner mit itunes:email) fehlt; Spotify prüft darüber die Inhaberschaft.")
    elif not _EMAIL.match(email_):
        err(f"Die Inhaber-E-Mail „{email_}“ sieht nicht gültig aus.")
    if owner is not None and not _t(owner, I + "name"):
        hint("Der Inhaber-Name (itunes:owner mit itunes:name) fehlt.")
    cat = ch.find(I + "category")
    if cat is None or not (cat.get("text") or "").strip():
        err("Die Kategorie (itunes:category) fehlt.")
    elif cat.get("text") not in APPLE_CATEGORIES:
        hint(f"Die Kategorie „{cat.get('text')}“ steht nicht in der bekannten Apple-Liste; bitte auf die genaue englische Schreibweise achten.")
    img = ch.find(I + "image")
    cover_url = (img.get("href") or "").strip() if img is not None else ""
    if not cover_url:
        err("Das Cover (itunes:image) fehlt.")
    else:
        if not cover_url.startswith("https://"):
            hint("Das Cover sollte über https erreichbar sein.")
        ext = urlparse(cover_url).path.lower().rsplit(".", 1)[-1]
        if ext not in ("jpg", "jpeg", "png"):
            err("Die Cover-Adresse muss auf .jpg oder .png enden.")

    # ---- Folgen
    items = ch.findall("item")
    if not items:
        err("Der Feed enthält noch keine Folge; Apple und Spotify verlangen mindestens eine.")
    guids: dict[str, int] = {}
    encl: dict[str, int] = {}
    now_ts = time.time() if now is None else now
    for n, it in enumerate(items, 1):
        name = f"Folge {n} („{_t(it, 'title')[:50]}“)" if _t(it, "title") else f"Folge {n}"
        if not _t(it, "title"):
            err(f"{name}: Der Titel fehlt.")
        g = it.find("guid")
        if g is None or not (g.text or "").strip():
            err(f"{name}: Die GUID fehlt.")
        else:
            guids[g.text.strip()] = guids.get(g.text.strip(), 0) + 1
        e = it.find("enclosure")
        if e is None:
            err(f"{name}: Das Audio (enclosure) fehlt.")
        else:
            url, length, mime = (e.get("url") or "").strip(), (e.get("length") or "").strip(), (e.get("type") or "").strip()
            if not url:
                err(f"{name}: Die Audio-Adresse fehlt.")
            else:
                encl[url] = encl.get(url, 0) + 1
                if not url.startswith("https://"):
                    hint(f"{name}: Die Audio-Adresse sollte über https erreichbar sein.")
                path = urlparse(url).path
                if not re.search(r"\.(mp3|m4a|mp4|m4v|mov|pdf)$", path, re.I):
                    err(f"{name}: Die Audio-Adresse muss mit der Dateiendung enden (z. B. .mp3).")
                if not re.fullmatch(r"[\x21-\x7e]+", url):
                    err(f"{name}: Die Audio-Adresse enthält Zeichen außerhalb von ASCII.")
            if not length.isdigit() or int(length) <= 0:
                err(f"{name}: Die Dateigröße (length) fehlt oder ist null.")
            if mime not in AUDIO_TYPES:
                err(f"{name}: Der Audio-Typ „{mime}“ ist ungültig (z. B. audio/mpeg).")
        pd = _t(it, "pubDate")
        if not pd:
            hint(f"{name}: Das Datum (pubDate) fehlt.")
        else:
            try:
                ts = email.utils.parsedate_to_datetime(pd).timestamp()
                if ts > now_ts + 600:
                    hint(f"{name}: Das Datum liegt in der Zukunft; Apple zeigt die Folge erst dann an.")
            except (TypeError, ValueError, IndexError):
                err(f"{name}: Das Datum „{pd}“ entspricht nicht RFC 2822.")
        if not _t(it, I + "duration"):
            hint(f"{name}: Die Dauer (itunes:duration) fehlt.")
        d_el = it.find("description")
        if d_el is not None and len(d_el.text or "") > 10000:
            hint(f"{name}: Die Beschreibung ist länger als 10 000 Zeichen; Apple kürzt sie.")
        for tag in ("episode", "season"):
            v = _t(it, I + tag)
            if v and (not v.isdigit() or int(v) < 1):
                err(f"{name}: itunes:{tag} muss eine positive ganze Zahl sein.")
        et = _t(it, I + "episodeType")
        if et and et not in ("full", "trailer", "bonus"):
            err(f"{name}: itunes:episodeType „{et}“ ist ungültig (full, trailer oder bonus).")
        ex_i = _t(it, I + "explicit")
        if ex_i and ex_i not in ("true", "false"):
            err(f"{name}: itunes:explicit muss „true“ oder „false“ sein.")
        for pc in it.findall(P + "chapters"):
            if not (pc.get("url") or "").startswith("https://"):
                hint(f"{name}: Die Kapitel-Adresse sollte https verwenden.")
        for pt in it.findall(P + "transcript"):
            if not (pt.get("url") or "").startswith("https://"):
                hint(f"{name}: Die Transkript-Adresse sollte https verwenden.")
            if pt.get("type") not in ("text/vtt", "application/srt", "application/x-subrip", "text/plain", "text/html", "application/json"):
                hint(f"{name}: Der Transkript-Typ „{pt.get('type')}“ ist unbekannt.")
    for g, c in guids.items():
        if c > 1:
            err(f"Die GUID „{g}“ kommt {c}-mal vor; GUIDs müssen eindeutig sein.")
    for u, c in encl.items():
        if c > 1:
            err(f"Die Audio-Adresse {u} wird von {c} Folgen verwendet; Apple ignoriert doppelte Adressen.")

    if check_network:
        own = client is None
        client = client or httpx.Client(follow_redirects=True, timeout=20.0, headers={"User-Agent": "podcast-studio-validator/1.0"})
        try:
            _network(out, client, cover_url, items, max_enclosures)
        finally:
            if own:
                client.close()
    return out


def _network(out: list[Issue], client: httpx.Client, cover_url: str, items: list[Element], max_enclosures: int) -> None:
    if cover_url:
        try:
            r = client.get(cover_url)
            if r.status_code != 200:
                out.append(Issue("Fehler", f"Das Cover ist nicht erreichbar (HTTP {r.status_code})."))
            else:
                info = image_info(r.content)
                if info is None:
                    out.append(Issue("Fehler", "Das Cover ist weder JPEG noch PNG."))
                else:
                    _, w, h = info
                    if w != h:
                        out.append(Issue("Fehler", f"Das Cover ist nicht quadratisch ({w}×{h} Pixel)."))
                    elif not 1400 <= w <= 3000:
                        out.append(Issue("Fehler", f"Das Cover ist {w}×{h} Pixel groß; erlaubt sind 1400 bis 3000 Pixel."))
        except httpx.HTTPError as e:
            out.append(Issue("Fehler", f"Das Cover ist nicht erreichbar ({type(e).__name__})."))
    sorted_items = sorted(items, key=lambda it: _pub(it), reverse=True)[:max_enclosures]
    for it in sorted_items:
        e = it.find("enclosure")
        if e is None or not e.get("url"):
            continue
        title = _t(it, "title")[:50]
        try:
            r = client.head(e.get("url"))
            if r.status_code != 200:
                out.append(Issue("Fehler", f"Das Audio von „{title}“ ist nicht erreichbar (HTTP {r.status_code})."))
                continue
            cl = r.headers.get("content-length")
            if cl and e.get("length", "").isdigit() and int(cl) != int(e.get("length")):
                out.append(Issue("Fehler", f"Die Dateigröße im Feed ({e.get('length')}) stimmt bei „{title}“ nicht mit der Datei ({cl}) überein."))
            if "bytes" not in r.headers.get("accept-ranges", "").lower():
                out.append(Issue("Hinweis", f"Der Speicher meldet bei „{title}“ keine Byte-Range-Unterstützung; Apple braucht sie für das Streaming."))
        except httpx.HTTPError as ex:
            out.append(Issue("Fehler", f"Das Audio von „{title}“ ist nicht erreichbar ({type(ex).__name__})."))


def _pub(it: Element) -> float:
    try:
        return email.utils.parsedate_to_datetime(_t(it, "pubDate")).timestamp()
    except (TypeError, ValueError, IndexError):
        return 0.0


def validate_feed(xml: str, *, check_network: bool = False, client: httpx.Client | None = None,
                  max_enclosures: int = 10) -> list[str]:
    """Deutsche Klartext-Hinweise zum Feed, leere Liste = nichts zu beanstanden."""
    return [str(i) for i in validate_feed_issues(xml, check_network=check_network, client=client, max_enclosures=max_enclosures)]
