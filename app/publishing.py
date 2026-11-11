"""Einzige Stelle, die `publish/` aufruft (Paket D). Importe erst in den Funktionen, damit der Server auch
ohne das Paket startet. Alle Funktionen sind blockierend; der Job-Läufer ruft sie per asyncio.to_thread.

Genutzte Schnittstellen aus publish/: storage.from_env(), models.Show/FeedEpisode, feed.build_feed(show, episodes,
url_for=…), naming.media_key/sibling_keys, importer.import_feed(feed_url, storage, on_progress) -> ImportResult,
shownotes.suggest(transcript, show, chapters, previous_titles)."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from . import core, i18n, util
from .config import Config
from .db import DB

IMMUTABLE = "public, max-age=31536000, immutable"
FEED_CACHE = "public, max-age=300"
COVER_MIME = {"jpg": "image/jpeg", "png": "image/png"}


class PublishError(Exception):
    """Meldung ist für Menschen gedacht (deutsch)."""


def storage():
    from publish import storage as st

    try:
        return st.from_env()
    except st.StorageError as exc:
        raise PublishError(str(exc)) from exc


def _published_rows(db: DB) -> list[dict]:
    """Veröffentlichte Folgen für Feed und Seite. Mit Präfix der Nummerierung wird `title` zum Titel mit Nummer
    (`<title>` im Feed) und `itunes_title` zum Titel ohne (`<itunes:title>`, Apple zeigt die Nummer selbst an)."""
    rows = db.all("SELECT * FROM episodes WHERE status='published' AND audio_key IS NOT NULL AND audio_bytes IS NOT NULL")
    prefix = core.get_numbering(db)["prefix"]
    for r in rows:
        full = core.numbered_title(r["title"] or "", r["number"], r["season"], prefix)
        if full != (r["title"] or ""):
            r["itunes_title"], r["title"] = r["title"], full
    return rows


def _theme_css() -> str:
    try:
        return (Path(__file__).parent / "static" / "design" / "tokens-unprefixed.css").read_text(encoding="utf-8")
    except OSError:
        return ""


def _chapters_by_guid(cfg: Config, rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        path = cfg.episode_dir(r["id"]) / "out" / "chapters.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out[r["guid"]] = [{"start_ms": int(float(c.get("startTime") or 0) * 1000), "title": c.get("title") or ""}
                              for c in data.get("chapters") or []]
        except (OSError, ValueError, AttributeError):
            continue
    return out


def _feed_parts(db: DB, cfg: Config, st, rows: list[dict]) -> dict:
    """Show-Daten, Audioadressen und Optionen der öffentlichen Seite für `release.render_feed`/`publish_feed`.

    `link` im Feed: Hat der Nutzer einen eigenen gespeichert (nicht die Standardadresse der App), gilt er. Sonst zeigt er auf
    die öffentliche Seite (`index.html` im Speicher), solange sie an ist, sonst bleibt es bei `PS_BASE_URL`."""
    from publish import release, site as site_pages

    settings = core.get_settings(db, cfg)
    show = dict(settings["show"])
    site_cfg, stats = settings["site"], settings["stats"]
    site = None
    if site_cfg["enabled"]:
        if not core.show_link_is_custom(db, cfg):
            show["link"] = st.url(site_pages.INDEX_KEY)
        lang = i18n.content_lang(show.get("language"))
        page_lang = lang if lang in i18n.LANGUAGES else "en"
        stored = db.get_setting("site_pages", {}) or {}
        known = dict(stored.get("hashes") or {}) if stored.get("base") == st.url(site_pages.INDEX_KEY) else {}
        site = site_pages.SiteOptions(
            platforms=site_cfg["platforms"], lang=page_lang, tr=lambda text, **p: i18n.tr(page_lang, text, **p),
            css=_theme_css(), chapters=_chapters_by_guid(cfg, rows), known_hashes=known,
            episode_links=not core.show_link_is_custom(db, cfg),
            season_is_year=settings["numbering"]["season"] == "year")
    enclosure = release.enclosure_url_fn(st, stats["mode"], stats["prefix"], cfg.base_url)
    return {"show": show, "enclosure_url": enclosure, "site": site}


def feed_xml(db: DB, cfg: Config) -> str:
    from publish import release

    st, rows = storage(), _published_rows(db)
    parts = _feed_parts(db, cfg, st, rows)
    return release.render_feed(st, parts["show"], rows, enclosure_url=parts["enclosure_url"], site=parts["site"])


def rebuild_feed(db: DB, cfg: Config, st=None) -> None:
    """Feed und, wenn eingeschaltet, öffentliche Seite (`index.html`, `folgen/<slug>.html`) neu bauen und hochladen."""
    from publish import release, site as site_pages

    st, rows = st or storage(), _published_rows(db)
    parts = _feed_parts(db, cfg, st, rows)
    release.publish_feed(st, parts["show"], rows, enclosure_url=parts["enclosure_url"], site=parts["site"])
    if parts["site"] is not None:
        db.set_setting("site_pages", {"base": st.url(site_pages.INDEX_KEY), "hashes": parts["site"].known_hashes})


def remove_site(db: DB, cfg: Config) -> None:
    """Öffentliche Seite abgeschaltet: ihre Dateien aus dem Speicher nehmen (der Feed bleibt)."""
    from publish import release

    release.remove_site(storage(), _published_rows(db))
    db.set_setting("site_pages", {})


def refresh_feed(db: DB, cfg: Config) -> bool:
    """Nach geänderten Show-Daten oder neuem Cover: Cover hochladen und den veröffentlichten Feed neu bauen.
    Ohne veröffentlichte Folge gibt es noch keinen Feed und nichts zu tun. -> True, wenn der Feed neu gebaut wurde."""
    from publish.storage import StorageError

    if not _published_rows(db):
        return False
    st = storage()
    try:
        _upload_cover(cfg, db, st)
        rebuild_feed(db, cfg, st)
    except StorageError as exc:
        raise PublishError(str(exc)) from exc
    return True


def _upload_cover(cfg: Config, db: DB, st) -> None:
    key = core.get_settings(db, cfg)["show"].get("cover_key")
    if not key:
        return
    path = cfg.cover_dir / key
    if path.exists() and not st.exists(key):
        st.put_file(key, path, COVER_MIME.get(path.suffix.lstrip("."), "image/jpeg"), IMMUTABLE)


def _keep_cover_copy(cfg: Config, key: str | None, st) -> None:
    """Der Import legt das Cover nur im Speicher ab. Einstellungen und Verarbeitung (Cover in der MP3) lesen es aus
    `cover_dir`, deshalb kommt hier eine Kopie dorthin. Scheitert das, bleibt nur die Vorschau leer."""
    if not key or (cfg.cover_dir / key).exists():
        return
    try:
        local = cfg.public_dir / key
        if local.exists():
            data = local.read_bytes()
        else:
            import httpx

            r = httpx.get(st.url(key), timeout=20, follow_redirects=True)
            r.raise_for_status()
            data = r.content
        cfg.cover_dir.mkdir(parents=True, exist_ok=True)
        (cfg.cover_dir / key).write_bytes(data)
    except Exception:  # noqa: BLE001
        pass


def publish_episode(db: DB, cfg: Config, episode_id: str, progress: Callable[[float, str], None] = lambda *_: None) -> None:
    """Dateien hochladen, Folge als veröffentlicht markieren, Feed neu bauen."""
    from publish import release

    ep = core.get_episode(db, episode_id)
    if ep is None:
        raise PublishError("Folge nicht gefunden")
    st = storage()
    fields: dict = {}
    obsolete: list[str] = []
    if ep["source"] == "recorded":
        out = cfg.episode_dir(episode_id) / "out"
        mp3 = out / "episode.mp3"
        if not mp3.exists():
            raise PublishError("Die fertige Folge (episode.mp3) fehlt. Bitte zuerst neu verarbeiten")
        progress(0.05, "Audiodatei, Kapitel und Transkript werden hochgeladen")
        chapters = out / "chapters.json"
        vtt = out / "transcript.vtt"
        res = release.upload_episode_assets(st, ep, mp3, chapters if chapters.exists() else None,
                                            vtt if vtt.exists() else None, delete_old=False)
        obsolete = res["obsolete_keys"]
        fields.update(audio_key=res["audio_key"], audio_bytes=res["audio_bytes"], audio_mime=res["audio_mime"])
    if not (fields.get("audio_key") or ep["audio_key"]):
        raise PublishError("Zu dieser Folge gibt es keine Audiodatei")
    _upload_cover(cfg, db, st)
    now = util.now_ms()
    published_at = ep["publish_at"] if ep["publish_at"] and ep["publish_at"] <= now else now
    numbering = core.get_numbering(db)
    if numbering["season"] == "year":  # Staffel = Jahr der Veröffentlichung, nicht der Aufnahme
        fields["season"] = core.season_for(published_at, "year")
    if not ep["number"]:
        fields["number"] = core.next_number(db)
    core.update_episode(db, episode_id, status="published", published_at=published_at, **fields)
    progress(0.9, "Feed wird aktualisiert")
    rebuild_feed(db, cfg, st)
    # erst jetzt zeigt der öffentliche Feed auf die neuen Dateien: die alten dürfen weg
    release.delete_keys(st, obsolete)


def unpublish_episode(db: DB, cfg: Config, episode_id: str) -> None:
    from publish import release, site as site_pages

    ep = core.get_episode(db, episode_id)
    if ep is None:
        raise PublishError("Folge nicht gefunden")
    if ep["source"] != "recorded":
        raise PublishError("Importierte Folgen lassen sich hier nicht zurückziehen")
    st = storage()
    core.update_episode(db, episode_id, status="review", published_at=None, publish_at=None,
                        audio_key=None, audio_bytes=None, audio_mime=None)
    rebuild_feed(db, cfg, st)  # zuerst die Folge aus dem Feed nehmen, dann ihre Dateien löschen
    release.remove_episode_assets(st, ep)
    release.delete_keys(st, [site_pages.episode_page_key(ep["guid"])])  # Folgenseite der öffentlichen Seite


def import_feed(db: DB, cfg: Config, feed_url: str, progress: Callable[[float, str], None] = lambda *_: None) -> str:
    from publish.importer import import_feed as _import

    st = storage()

    def on_progress(*args) -> None:
        # Signatur des Importers: (Anteil, Meldung) oder ein dict {"progress", "message"}
        try:
            if args and isinstance(args[0], dict):
                progress(float(args[0].get("progress", 0)), str(args[0].get("message", "")))
            else:
                progress(float(args[0]), str(args[1]) if len(args) > 1 else "")
        except (TypeError, ValueError, IndexError):
            pass

    result = _import(feed_url, st, on_progress)
    data = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    show = db.get_setting("show", {})
    for k, v in (data.get("show") or {}).items():
        if v not in (None, "") and not show.get(k):
            show[k] = v
    db.set_setting("show", show)
    _keep_cover_copy(cfg, show.get("cover_key"), st)
    created = 0
    for e in data.get("episodes", []):
        if db.one("SELECT id FROM episodes WHERE guid=?", (e["guid"],)):
            continue
        db.run(
            "INSERT INTO episodes(id,status,source,number,season,title,summary,description_html,explicit,episode_type,guid,"
            "created_at,duration_ms,published_at,audio_key,audio_bytes,audio_mime,edl,legacy) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (util.new_id(), "published", "imported", e.get("number"), e.get("season"), e.get("title"), e.get("summary"),
             e.get("description_html"), int(bool(e.get("explicit"))), e.get("episode_type") or "full", e["guid"],
             e.get("pub_date_ms") or util.now_ms(), e.get("duration_ms"), e.get("pub_date_ms"), e["audio_key"],
             e.get("audio_bytes"), e.get("audio_mime") or "audio/mpeg", util.dumps(core.DEFAULT_EDL),
             util.dumps(e.get("legacy") or {})),
        )
        created += 1
    progress(0.97, "Feed wird gebaut")
    rebuild_feed(db, cfg, st)
    msg = f"{created} Folgen übernommen, {len(data.get('skipped_guids', []))} schon vorhanden"
    if data.get("errors"):
        msg += f", {len(data['errors'])} fehlgeschlagen (erneut importieren holt sie nach)"
        raise PublishError(msg)
    return msg


def suggest(db: DB, cfg: Config, episode_id: str) -> dict | None:
    """Titel, Zusammenfassung, Shownotes und Kapiteltitel aus dem Transkript. None, wenn nichts zu tun ist."""
    if not cfg.anthropic_key:
        return None
    path = cfg.episode_dir(episode_id) / "out" / "transcript.json"
    if not path.exists():
        return None
    from publish.shownotes import ShownotesUnavailable
    from publish.shownotes import suggest as _suggest

    ep = core.get_episode(db, episode_id)
    transcript = json.loads(path.read_text())
    chapters = [{"event_id": c["event_id"], "start_ms": c["start_ms"], "title": c["title"]}
                for c in core.chapters_preview(db, ep)]
    previous = [r["title"] for r in db.all(
        "SELECT title FROM episodes WHERE id!=? AND title IS NOT NULL ORDER BY COALESCE(published_at, created_at) DESC LIMIT 30",
        (episode_id,))]
    show = core.assist_show(db, cfg)
    extra: dict = {}
    plan = [{k: s[k] for k in ("kind", "title", "input", "good_type", "dossier")} for s in core.plan_of(db, episode_id)]
    if any(s["title"] or s["input"] for s in plan):
        extra["plan"] = plan  # Quellen und Good Read aus dem Sendeplan
    try:
        result = _suggest(transcript, show, chapters, previous, model=cfg.ai_model, **extra)
    except ShownotesUnavailable as exc:
        raise PublishError(str(exc)) from exc
    if isinstance(result, dict):
        core.add_cost(db, episode_id, "shownotes", result.pop("cost_usd", 0) or 0)
    return result
