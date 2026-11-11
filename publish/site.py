"""Öffentliche Seite der Show: statisches HTML (ohne JavaScript, CSS inline) für den Speicher.

`build_pages` liefert `index.html` (Cover, Titel, Beschreibung, Links zu Podcast-Apps, Folgen neueste zuerst) und je Folge
`folgen/<guid-slug>.html` (Player, Shownotes als bereinigtes HTML, Kapitel mit Zeiten). Die Farben stammen aus dem Thema der
Installation (`theme_colors` liest `--paper --surface --ink --slate --line --petrol` aus dessen CSS, hell und dunkel);
Texte kommen über `SiteOptions.tr` (deutscher Text → Text der Sprache). Das Paket kennt weder Datenbank noch Server.
"""
from __future__ import annotations

import hashlib
import html as _html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .models import FeedEpisode, Show
from .naming import guid_slug
from .sanitize import clean_html, one_line

INDEX_KEY = "index.html"
COLOR_NAMES = ("paper", "surface", "ink", "slate", "line", "petrol")
FALLBACK_LIGHT = {"paper": "#F5F7FA", "surface": "#FFFFFF", "ink": "#111827", "slate": "#4B5565", "line": "#DDE1E8", "petrol": "#2447C9"}
FALLBACK_DARK = {"paper": "#0F131A", "surface": "#171C26", "ink": "#E8ECF2", "slate": "#A3ACBA", "line": "#2B3340", "petrol": "#7C99F2"}
MAX_PLATFORMS = 6
MONTHS = {
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
}


@dataclass
class SiteOptions:
    """Einstellungen für die Seite. `platforms`: [{name, url}]; `lang`: Sprache der Texte (de/en); `css`: CSS des Themas;
    `chapters`: guid -> [{start_ms, title}]; `known_hashes`: Schlüssel -> Prüfsumme der zuletzt hochgeladenen Seite
    (wird beim Hochladen fortgeschrieben, unveränderte Seiten werden übersprungen); `episode_links`: Feed-Einträge
    zeigen auf die Folgenseiten."""

    platforms: list[dict[str, str]] = field(default_factory=list)
    lang: str = "de"
    tr: Callable[..., str] = lambda text, **p: text.format(**p) if p else text
    css: str = ""
    chapters: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    known_hashes: dict[str, str] = field(default_factory=dict)
    episode_links: bool = True


def episode_page_key(guid: str) -> str:
    return f"folgen/{guid_slug(guid)}.html"


def theme_colors(css: str) -> tuple[dict[str, str], dict[str, str]]:
    """Hell- und Dunkelwerte der sechs Farben aus dem CSS des Themas (`:root` und `[data-theme="dark"]`)."""
    def block(selector: str) -> str:
        m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css or "")
        return m.group(1) if m else ""

    def read(body: str, base: dict[str, str]) -> dict[str, str]:
        out = dict(base)
        for name in COLOR_NAMES:
            m = re.search(r"--" + name + r"\s*:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|hsla?\([^)]*\))\s*;", body)
            if m:
                out[name] = m.group(1)
        return out

    light = read(block(":root"), FALLBACK_LIGHT)
    dark_block = block('[data-theme="dark"]')
    dark = read(dark_block, light) if dark_block else dict(light)  # ohne dunkle Werte im Thema bleibt es bei den hellen
    return light, dark


def _e(text: Any) -> str:
    return _html.escape(str(text if text is not None else ""), quote=True)


def safe_http_url(url: str | None) -> str | None:
    u = (url or "").strip()
    return u if re.match(r"https?://[^\s<>\"']+$", u, re.I) else None


def clean_platforms(items: Iterable[Any] | None) -> list[dict[str, str]]:
    """Nur Einträge mit Namen und http(s)-Adresse, höchstens `MAX_PLATFORMS`."""
    out = []
    for it in items or []:
        name = str((it or {}).get("name") or "").strip()[:60]
        url = safe_http_url((it or {}).get("url"))
        if name and url:
            out.append({"name": name, "url": url})
    return out[:MAX_PLATFORMS]


# --- Formate ---------------------------------------------------------------------------------------------
def _dt(ms: int | None) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None


def fmt_date(ms: int | None, lang: str) -> str:
    d = _dt(ms)
    if d is None:
        return ""
    if lang == "de":
        return f"{d.day}. {MONTHS['de'][d.month - 1]} {d.year}"
    if lang == "en":
        return f"{MONTHS['en'][d.month - 1]} {d.day}, {d.year}"
    return d.strftime("%Y-%m-%d")


def fmt_duration(ms: int | None, tr: Callable[..., str]) -> str:
    if not ms:
        return ""
    minutes = max(1, round(ms / 60000))
    if minutes < 60:
        return tr("{n} Min.", n=minutes)
    return tr("{h} Std. {m} Min.", h=minutes // 60, m=f"{minutes % 60:02d}")


def fmt_clock(ms: int) -> str:
    s = max(0, int(ms // 1000))
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _blurb(e: FeedEpisode, limit: int = 280) -> str:
    text = one_line(e.summary or "") or one_line(clean_html(e.description_html))
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


# --- Gestaltung ---------------------------------------------------------------------------------------------
def _vars(c: dict[str, str]) -> str:
    return "".join(f"--{k}:{c[k]};" for k in COLOR_NAMES)


def stylesheet(css: str) -> str:
    light, dark = theme_colors(css)
    return (
        f":root{{{_vars(light)}color-scheme:light dark}}"
        f"@media (prefers-color-scheme:dark){{:root{{{_vars(dark)}}}}}"
        "*{box-sizing:border-box}"
        "html{-webkit-text-size-adjust:100%}"
        "body{margin:0;background:var(--paper);color:var(--ink);font:1rem/1.6 system-ui,-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif}"
        "a{color:var(--petrol);text-underline-offset:.15em}"
        "a:focus-visible,audio:focus-visible{outline:3px solid var(--petrol);outline-offset:3px;border-radius:4px}"
        ".skip{position:absolute;left:16px;top:-48px;background:var(--surface);color:var(--ink);padding:8px 16px;border:1px solid var(--line);border-radius:6px;z-index:2}"
        ".skip:focus{top:8px}"
        ".wrap{max-width:46rem;margin:0 auto;padding:0 16px}"
        "header.top{padding-top:32px;padding-bottom:24px}"
        ".hero{display:grid;grid-template-columns:minmax(0,1fr);gap:24px;align-items:start}"
        ".hero img{width:100%;max-width:240px;height:auto;aspect-ratio:1/1;border-radius:12px;border:1px solid var(--line);background:var(--surface)}"
        "@media (min-width:600px){.hero.has-cover{grid-template-columns:200px minmax(0,1fr)}.hero img{max-width:200px}}"
        "h1{font:700 1.75rem/1.2 Georgia,'Times New Roman',serif;margin:0 0 12px;overflow-wrap:anywhere}"
        "h2{font:700 1.25rem/1.3 Georgia,'Times New Roman',serif;margin:32px 0 12px}"
        "h3{font:600 1.125rem/1.35 system-ui,-apple-system,'Segoe UI',Roboto,Arial,sans-serif;margin:0 0 4px;overflow-wrap:anywhere}"
        "h3 a{color:var(--ink)}"
        "p{margin:0 0 12px}"
        ".lead{color:var(--slate);max-width:38rem}"
        ".muted{color:var(--slate);font-size:.9375rem}"
        ".links{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:8px}"
        ".links a{display:inline-block;padding:8px 16px;border:1px solid var(--line);border-radius:999px;background:var(--surface);text-decoration:none;color:var(--petrol);font-weight:600}"
        ".links a:hover{border-color:var(--petrol)}"
        ".eps{list-style:none;margin:0;padding:0;display:grid;gap:16px}"
        ".ep{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px}"
        ".meta{color:var(--slate);font-size:.875rem;margin:0 0 8px}"
        "audio{display:block;width:100%;margin-top:12px}"
        ".notes{max-width:40rem;overflow-wrap:anywhere}"
        ".notes ul,.notes ol{padding-left:24px}"
        ".chapters{list-style:none;margin:0;padding:0;border-top:1px solid var(--line)}"
        ".chapters li{display:flex;gap:16px;padding:8px 0;border-bottom:1px solid var(--line)}"
        ".chapters time{font-variant-numeric:tabular-nums;color:var(--slate);min-width:4.5rem;flex:none}"
        "nav.crumb{padding:32px 0 16px;font-size:.9375rem}"
        "footer{margin-top:48px;margin-bottom:32px;color:var(--slate);font-size:.875rem}"
    )


def _head(title: str, description: str, lang: str, css: str, feed_url: str, tr, image: str | None = None, canonical: str | None = None) -> str:
    og = [f'<meta property="og:title" content="{_e(title)}">', '<meta property="og:type" content="website">']
    if description:
        og.append(f'<meta property="og:description" content="{_e(description)}">')
    if image:
        og.append(f'<meta property="og:image" content="{_e(image)}">')
    if canonical:
        og.append(f'<meta property="og:url" content="{_e(canonical)}">')
    return (
        f'<!doctype html>\n<html lang="{_e(lang)}">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(title)}</title>\n"
        + (f'<meta name="description" content="{_e(description)}">\n' if description else "")
        + (f'<link rel="alternate" type="application/rss+xml" title="{_e(title)}" href="{_e(feed_url)}">\n' if feed_url else "")
        + "\n".join(og) + f"\n<style>{stylesheet(css)}</style>\n</head>\n"
    )


def _footer(show: Show, tr) -> str:
    return f'<footer class="wrap">{_e(show.copyright)}</footer>' if show.copyright else ""


def _player(e: FeedEpisode, enclosure_url: Callable[[str], str]) -> str:
    src = enclosure_url(e.audio_key)
    return f'<audio controls preload="none" src="{_e(src)}"></audio>'


def _meta_line(e: FeedEpisode, lang: str, tr) -> str:
    parts = []
    when = _dt(e.pub_date_ms)
    if when:
        parts.append(f'<time datetime="{when.strftime("%Y-%m-%d")}">{_e(fmt_date(e.pub_date_ms, lang))}</time>')
    dur = fmt_duration(e.duration_ms, tr)
    if dur:
        parts.append(f"<span>{_e(dur)}</span>")
    if e.season and e.number:
        parts.insert(0, f"<span>{_e(tr('Staffel {s}, Folge {n}', s=e.season, n=e.number))}</span>")
    elif e.number:
        parts.insert(0, f"<span>{_e(tr('Folge {n}', n=e.number))}</span>")
    return '<p class="meta">' + " · ".join(parts) + "</p>" if parts else ""


def render_index(show: Show, eps: list[FeedEpisode], opts: SiteOptions, *, url_for: Callable[[str], str],
                 enclosure_url: Callable[[str], str], feed_url: str) -> str:
    tr, lang = opts.tr, opts.lang
    cover = url_for(show.cover_key) if show.cover_key else None
    page_url = url_for(INDEX_KEY)
    out = [_head(show.title, one_line(show.description)[:300], show.language or lang, opts.css, feed_url, tr, cover, page_url)]
    out.append('<body>\n<a class="skip" href="#inhalt">' + _e(tr("Zum Inhalt springen")) + "</a>")
    out.append('<header class="top wrap"><div class="hero' + (" has-cover" if cover else "") + '">')
    if cover:
        out.append(f'<img src="{_e(cover)}" width="200" height="200" alt="{_e(tr("Cover von {title}", title=show.title))}">')
    out.append("<div>" + f"<h1>{_e(show.title)}</h1>")
    if show.author:
        out.append(f'<p class="muted">{_e(show.author)}</p>')
    for para in [p.strip() for p in re.split(r"\n\s*\n", show.description or "") if p.strip()]:
        out.append(f'<p class="lead">{_e(para).replace(chr(10), "<br>")}</p>')
    out.append("</div></div></header>")
    out.append('<main id="inhalt" class="wrap">')
    out.append(f"<section aria-labelledby=\"abo\"><h2 id=\"abo\">{_e(tr('Podcast abonnieren'))}</h2><ul class=\"links\">")
    for p in opts.platforms:
        out.append(f'<li><a href="{_e(p["url"])}" rel="noopener">{_e(p["name"])}</a></li>')
    if feed_url:
        out.append(f'<li><a href="{_e(feed_url)}">{_e(tr("RSS-Feed"))}</a></li>')
    out.append("</ul></section>")
    out.append(f"<section aria-labelledby=\"folgen\"><h2 id=\"folgen\">{_e(tr('Folgen'))}</h2>")
    if not eps:
        out.append(f"<p class=\"muted\">{_e(tr('Noch keine Folge veröffentlicht'))}</p>")
    else:
        out.append('<ul class="eps">')
        for e in eps:
            blurb = _blurb(e)
            out.append(
                '<li class="ep"><article>'
                f'<h3><a href="{_e(episode_page_key(e.guid))}">{_e(e.title)}</a></h3>'
                + _meta_line(e, lang, tr)
                + (f"<p>{_e(blurb)}</p>" if blurb else "")
                + _player(e, enclosure_url)
                + "</article></li>")
        out.append("</ul>")
    out.append("</section></main>")
    out.append(_footer(show, tr))
    out.append("</body>\n</html>\n")
    return "\n".join(out)


def render_episode(show: Show, e: FeedEpisode, opts: SiteOptions, *, url_for: Callable[[str], str],
                   enclosure_url: Callable[[str], str], feed_url: str) -> str:
    tr, lang = opts.tr, opts.lang
    cover = url_for(show.cover_key) if show.cover_key else None
    page_url = url_for(episode_page_key(e.guid))
    out = [_head(f"{e.title} | {show.title}", _blurb(e, 200), show.language or lang, opts.css, feed_url, tr, cover, page_url)]
    out.append('<body>\n<a class="skip" href="#inhalt">' + _e(tr("Zum Inhalt springen")) + "</a>")
    out.append('<main id="inhalt" class="wrap"><nav class="crumb" aria-label="' + _e(tr("Navigation")) + '">'
               f'<a href="../{INDEX_KEY}">{_e(show.title)}</a> · <a href="../{INDEX_KEY}#folgen">{_e(tr("Alle Folgen"))}</a></nav>')
    out.append(f"<article><h1>{_e(e.title)}</h1>" + _meta_line(e, lang, tr) + _player(e, enclosure_url))
    notes = clean_html(e.description_html) or clean_html(e.summary)
    if notes:
        out.append(f'<section aria-labelledby="notes"><h2 id="notes">{_e(tr("Shownotes"))}</h2><div class="notes">{notes}</div></section>')
    chapters = [c for c in (opts.chapters.get(e.guid) or []) if str(c.get("title") or "").strip()]
    if chapters:
        out.append(f'<section aria-labelledby="chap"><h2 id="chap">{_e(tr("Kapitel"))}</h2><ol class="chapters">')
        for c in chapters:
            out.append(f'<li><time>{fmt_clock(int(c.get("start_ms") or 0))}</time><span>{_e(c["title"])}</span></li>')
        out.append("</ol></section>")
    out.append("</article></main>")
    out.append(_footer(show, tr))
    out.append("</body>\n</html>\n")
    return "\n".join(out)


def build_pages(show: Show, episodes: Iterable[FeedEpisode], opts: SiteOptions, *, url_for: Callable[[str], str],
                enclosure_url: Callable[[str], str] | None = None, feed_url: str = "") -> dict[str, str]:
    """Schlüssel -> HTML: `index.html` und `folgen/<guid-slug>.html`; Folgen neueste zuerst."""
    enc = enclosure_url or url_for
    eps = sorted(episodes, key=lambda e: (-(e.pub_date_ms or 0), e.guid))
    pages = {INDEX_KEY: render_index(show, eps, opts, url_for=url_for, enclosure_url=enc, feed_url=feed_url)}
    for e in eps:
        pages[episode_page_key(e.guid)] = render_episode(show, e, opts, url_for=url_for, enclosure_url=enc, feed_url=feed_url)
    return pages


def checksum(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()[:16]
