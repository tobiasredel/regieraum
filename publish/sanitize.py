"""HTML bereinigen (nh3) und in Klartext umwandeln.

Erlaubt sind nur: p, br, a (nur href, nur http/https/mailto), ul, ol, li, strong, em.
Gängige Fremd-Tags aus importierten Feeds werden vorher umgeschrieben (b -> strong, i -> em,
Überschriften -> Absatz mit strong, div -> p), damit beim Import möglichst wenig Gliederung verloren geht.
"""
from __future__ import annotations

import html as _html
import re

import nh3

ALLOWED_TAGS = {"p", "br", "a", "ul", "ol", "li", "strong", "em"}
_ALLOWED_ATTRS = {"a": {"href"}}
_SCHEMES = {"http", "https", "mailto"}

_REWRITES = [
    (re.compile(r"<(/?)b(\s[^>]*)?>", re.I), r"<\1strong>"),
    (re.compile(r"<(/?)i(\s[^>]*)?>", re.I), r"<\1em>"),
    (re.compile(r"<h[1-6](\s[^>]*)?>", re.I), "<p><strong>"),
    (re.compile(r"</h[1-6]\s*>", re.I), "</strong></p>"),
    (re.compile(r"<div(\s[^>]*)?>", re.I), "<p>"),
    (re.compile(r"</div\s*>", re.I), "</p>"),
]
_TAG_RE = re.compile(r"<[a-zA-Z/!][^>]*>")


def looks_like_html(text: str) -> bool:
    return bool(_TAG_RE.search(text or ""))


def plain_to_html(text: str) -> str:
    """Klartext in Absätze (p) und Zeilenumbrüche (br) umwandeln, mit Escaping."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    return "".join("<p>" + _html.escape(p.strip(), quote=False).replace("\n", "<br>") + "</p>" for p in paras)


def clean_html(html: str | None) -> str:
    """Bereinigtes HTML. Klartext ohne Tags wird in Absätze umgewandelt. Leere Eingabe ergibt ''."""
    if not html or not html.strip():
        return ""
    if not looks_like_html(html):
        html = plain_to_html(html)
    for rx, rep in _REWRITES:
        html = rx.sub(rep, html)
    out = nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes=_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )
    return out.strip()


def html_to_text(html: str | None) -> str:
    """Klartext aus HTML: Absätze und Listenpunkte werden zu Zeilenumbrüchen, Entities aufgelöst."""
    if not html:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    s = re.sub(r"</(p|li|ul|ol|div|h[1-6])\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "- ", s, flags=re.I)
    s = nh3.clean(s, tags=set(), clean_content_tags={"script", "style"})
    s = _html.unescape(s)
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def one_line(text: str | None) -> str:
    """Klartext auf eine Zeile verdichten."""
    return re.sub(r"\s+", " ", html_to_text(text or "")).strip()
