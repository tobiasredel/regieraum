"""HTML-Seiten. Die Vorlagen liefern die Frontend-Pakete; fehlt eine, erscheint eine schlichte Platzhalterseite."""
from __future__ import annotations

import html
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter()


def asset_version() -> str:
    """Versions-Query für Assets: neuester Änderungszeitpunkt unter static/, höchstens alle 2 s neu bestimmt."""
    global _version, _version_at
    now = time.monotonic()
    if now - _version_at > 2:
        _version, _version_at = _compute_version(), now
    return _version


def _compute_version() -> str:
    try:
        return str(int(max((p.stat().st_mtime for p in STATIC_DIR.rglob("*") if p.is_file()), default=0)))
    except OSError:
        return "0"


_version = _compute_version()
_version_at = time.monotonic()


def render(request: Request, name: str, title: str, **ctx) -> HTMLResponse:
    from . import i18n

    lang = i18n.ui_lang(getattr(request.app.state, "db", None))
    try:
        show_title = request.app.state.db.get_setting("show", {}).get("title") or i18n.tr(lang, "Mein Podcast")
    except Exception:  # noqa: BLE001 - die Seite soll auch ohne Datenbank einen Titel haben
        show_title = i18n.tr(lang, "Mein Podcast")
    context = {"request": request, "v": asset_version(), "base_url": request.app.state.cfg.base_url,
               "show_title": show_title, "host_auth": request.app.state.cfg.host_auth,
               "lang": lang, "t": lambda text, **kw: i18n.tr(lang, text, **kw), **ctx}
    if (TEMPLATES_DIR / name).exists():
        return templates.TemplateResponse(request, name, context)
    body = (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(i18n.tr(lang, title))}</title>'
            f'<link rel="stylesheet" href="/static/design/app-basis.css?v={asset_version()}"></head>'
            f'<body><main style="max-width:40rem;margin:4rem auto;padding:0 1rem"><h1>{html.escape(i18n.tr(lang, title))}</h1>'
            f'<p>{html.escape(i18n.tr(lang, "Diese Seite ist noch nicht gebaut."))}</p></main></body></html>')
    return HTMLResponse(body)


@router.get("/", include_in_schema=False)
def index(request: Request):
    if (TEMPLATES_DIR / "index.html").exists():
        return render(request, "index.html", "Podcast-Studio")
    return RedirectResponse("/studio", status_code=307)


@router.get("/studio", include_in_schema=False)
def studio(request: Request):
    return render(request, "studio.html", "Studio")


@router.get("/plan", include_in_schema=False)
def plan(request: Request):
    return render(request, "plan.html", "Vorbereitung")


@router.get("/episodes", include_in_schema=False)
def episodes(request: Request):
    return render(request, "episodes.html", "Folgen")


@router.get("/episodes/{episode_id}", include_in_schema=False)
def episode(request: Request, episode_id: str):
    return render(request, "episode.html", "Folge prüfen", episode_id=episode_id)


@router.get("/sounds", include_in_schema=False)
def sounds(request: Request):
    return render(request, "sounds.html", "Soundboard")


@router.get("/settings", include_in_schema=False)
def settings(request: Request):
    return render(request, "settings.html", "Einstellungen")


@router.get("/join/{token}", include_in_schema=False)
def join(request: Request, token: str):
    return render(request, "join.html", "Beitreten", token=token)
