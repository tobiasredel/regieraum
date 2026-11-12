"""Host-API unter /api/host/ (SPEC Abschnitt 7). Der Pfad liegt hinter der Host-Anmeldung; Gast-Tokens kommen nie hinein."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import re
import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import archive, core, cover, i18n, jobs, publishing, soundproc, util
from .api_p import serve_sound
from .auth import host_guard
from .errors import ApiError

router = APIRouter(prefix="/api/host", dependencies=[Depends(host_guard)])

MAX_SOUND_BYTES = 60 * 1024 * 1024
MAX_COVER_BYTES = 10 * 1024 * 1024
DENOISE = ("off", "leicht", "normal", "stark")


def _st(request: Request):
    return request.app.state


async def _room_update(request: Request) -> None:
    await request.app.state.room.send_room()


# --- Sitzung ---------------------------------------------------------------------------------
class SessionBody(BaseModel):
    participant_token: str | None = None


@router.post("/session")
def session(request: Request, body: SessionBody | None = None) -> dict:
    db = _st(request).db
    token = body.participant_token if body else None
    if token:
        p = core.participant_by_token(db, token)
        if p is not None and p["role"] == "host":
            return {"participant_token": token, "participant": core.participant_public(p)}
    p, token = core.create_participant(db, core.host_first_name(db), "host", None)
    return {"participant_token": token, "participant": core.participant_public(p)}


# --- Sounds ------------------------------------------------------------------------------------
class SoundPatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=80)
    kind: Literal["intro", "outro", "segment", "sting"] | None = None
    chapter_title: str | None = Field(default=None, max_length=120)
    gain_db: float | None = Field(default=None, ge=-30, le=12)
    fade_out_ms: int | None = Field(default=None, ge=0, le=10000)
    hotkey: str | None = Field(default=None, max_length=1)
    archived: bool | None = None
    segment_kind: Literal["blitzlicht", "hauptthema", "good"] | None = None


def _sound_out(s: dict) -> dict:
    return core.sound_public(s)


@router.get("/sounds")
def sounds_list(request: Request) -> list[dict]:
    return [_sound_out(s) for s in core.list_sounds(_st(request).db)]


@router.post("/sounds", status_code=201)
async def sounds_create(request: Request, file: UploadFile = File(...), label: str = Form(""), kind: str = Form("sting"),
                        chapter_title: str = Form(""), gain_db: float = Form(0.0), fade_out_ms: int = Form(400),
                        hotkey: str = Form(""), segment_kind: str = Form("")) -> dict:
    st = _st(request)
    if kind not in core.SOUND_KINDS:
        raise ApiError(422, "invalid", "Ungültige Art. Erlaubt sind intro, outro, segment und sting")
    name = file.filename or "sound"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in soundproc.ALLOWED_EXT:
        raise ApiError(422, "bad_format", "Dateityp nicht unterstützt. Erlaubt sind WAV, MP3, M4A, AAC, OGG, Opus, FLAC und AIFF")
    if segment_kind and segment_kind not in core.SOUND_SEGMENT_KINDS:
        raise ApiError(422, "invalid", "Ungültige Sendeplan-Zuordnung. Erlaubt sind blitzlicht, hauptthema und good")
    sid = util.new_id()
    d = core.sound_dir(st.cfg, sid)
    d.mkdir(parents=True, exist_ok=True)
    src = d / f"original.{ext}"
    try:
        size = 0
        with open(src, "wb") as f:
            while block := await file.read(1 << 20):
                size += len(block)
                if size > MAX_SOUND_BYTES:
                    raise ApiError(413, "too_large", "Die Datei ist zu groß (höchstens 60 MB)")
                f.write(block)
        import anyio

        try:
            duration = await anyio.to_thread.run_sync(soundproc.process_sound, src, d)
        except soundproc.SoundError as exc:
            raise ApiError(422, "bad_audio", str(exc)) from exc
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    top = st.db.one("SELECT COALESCE(MAX(sort),-1) AS m FROM sounds")["m"]
    hk = hotkey.strip()[:1] or None
    if hk:
        st.db.run("UPDATE sounds SET hotkey=NULL WHERE hotkey=?", (hk,))
    st.db.run(
        "INSERT INTO sounds(id,label,kind,chapter_title,ext,duration_ms,gain_db,fade_out_ms,sort,hotkey,archived,created_at,"
        "segment_kind) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?)",
        (sid, label.strip() or Path(name).stem[:80], kind, chapter_title.strip() or None, ext, duration, gain_db,
         fade_out_ms, top + 1, hk, util.now_ms(), segment_kind or None),
    )
    await _room_update(request)
    return _sound_out(st.db.one("SELECT * FROM sounds WHERE id=?", (sid,)))


def _sound_or_404(request: Request, sound_id: str) -> dict:
    s = _st(request).db.one("SELECT * FROM sounds WHERE id=?", (sound_id,))
    if s is None:
        raise ApiError(404, "sound_not_found", "Sound nicht gefunden")
    return s


@router.patch("/sounds/{sound_id}")
async def sounds_patch(sound_id: str, body: SoundPatch, request: Request) -> dict:
    db = _st(request).db
    _sound_or_404(request, sound_id)
    changes = body.model_dump(exclude_unset=True)
    if "archived" in changes:
        changes["archived"] = int(bool(changes["archived"]))
    if "hotkey" in changes:
        changes["hotkey"] = (changes["hotkey"] or "").strip() or None
        if changes["hotkey"]:
            db.run("UPDATE sounds SET hotkey=NULL WHERE hotkey=? AND id!=?", (changes["hotkey"], sound_id))
    if changes:
        db.run(f"UPDATE sounds SET {', '.join(k + '=?' for k in changes)} WHERE id=?", (*changes.values(), sound_id))
    await _room_update(request)
    return _sound_out(db.one("SELECT * FROM sounds WHERE id=?", (sound_id,)))


@router.delete("/sounds/{sound_id}")
async def sounds_delete(sound_id: str, request: Request) -> dict:
    st = _st(request)
    _sound_or_404(request, sound_id)
    used = st.db.one("SELECT 1 AS x FROM events WHERE sound_id=? LIMIT 1", (sound_id,))
    if used:  # Aufnahmen verweisen darauf: nur ausblenden, damit Neuverarbeitung weiter funktioniert
        st.db.run("UPDATE sounds SET archived=1, hotkey=NULL WHERE id=?", (sound_id,))
        result = {"deleted": False, "archived": True}
    else:
        st.db.run("DELETE FROM sounds WHERE id=?", (sound_id,))
        shutil.rmtree(core.sound_dir(st.cfg, sound_id), ignore_errors=True)
        result = {"deleted": True, "archived": False}
    await _room_update(request)
    return result


class OrderBody(BaseModel):
    ids: list[str]


@router.post("/sounds/order")
async def sounds_order(body: OrderBody, request: Request) -> list[dict]:
    db = _st(request).db
    with db.tx() as c:
        for i, sid in enumerate(body.ids):
            c.execute("UPDATE sounds SET sort=? WHERE id=?", (i, sid))
    await _room_update(request)
    return [_sound_out(s) for s in core.list_sounds(db)]


@router.get("/sounds/{sound_id}/audio")
def sounds_audio(sound_id: str, request: Request):
    return serve_sound(request, sound_id)


# --- Einladungen ---------------------------------------------------------------------------------
class InviteBody(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    kind: Literal["permanent", "single"] = "single"


@router.get("/invites")
def invites_list(request: Request) -> list[dict]:
    st = _st(request)
    rows = st.db.all("SELECT * FROM invites ORDER BY created_at DESC")
    return [core.invite_public(r, st.cfg, st.db) for r in rows]


@router.post("/invites", status_code=201)
def invites_create(body: InviteBody, request: Request) -> dict:
    st = _st(request)
    if body.kind == "permanent":  # Dauerlink „erneuern“: die alten bleiben bis zum Widerruf gültig
        pass
    return core.invite_public(core.create_invite(st.db, body.label.strip(), body.kind), st.cfg, st.db)


@router.delete("/invites/{invite_id}")
def invites_revoke(invite_id: str, request: Request) -> dict:
    st = _st(request)
    inv = st.db.one("SELECT * FROM invites WHERE id=?", (invite_id,))
    if inv is None:
        raise ApiError(404, "invite_not_found", "Einladung nicht gefunden")
    if not inv["revoked_at"]:
        st.db.run("UPDATE invites SET revoked_at=? WHERE id=?", (util.now_ms(), invite_id))
    return core.invite_public(st.db.one("SELECT * FROM invites WHERE id=?", (invite_id,)), st.cfg, st.db)


# --- Einstellungen ---------------------------------------------------------------------------------
class ShowPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    author: str | None = Field(default=None, max_length=200)
    owner_name: str | None = Field(default=None, max_length=200)
    owner_email: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    language: str | None = Field(default=None, max_length=10)
    category: str | None = Field(default=None, max_length=100)
    subcategory: str | None = Field(default=None, max_length=100)
    explicit: bool | None = None
    link: str | None = Field(default=None, max_length=500)
    copyright: str | None = Field(default=None, max_length=300)


class AudioPatch(BaseModel):
    target_lufs: float | None = Field(default=None, ge=-30, le=-10)
    true_peak_db: float | None = Field(default=None, ge=-6, le=0)
    denoise: Literal["off", "leicht", "normal", "stark"] | None = None
    crossgate: bool | None = None
    mp3_kbps: int | None = Field(default=None, ge=64, le=320)


class AssistPatch(BaseModel):
    enabled: bool | None = None


class ProfilePatch(BaseModel):
    """Show-Profil: Rufnamen von Host und Co-Host, Thema und Ton der Show (für Recherche, Begleiter, Shownotes)."""
    host_name: str | None = Field(default=None, max_length=60)
    cohost_name: str | None = Field(default=None, max_length=60)
    genre: str | None = Field(default=None, max_length=200)
    tone: str | None = Field(default=None, max_length=200)


class FormatLabels(BaseModel):
    blitzlicht: str | None = Field(default=None, min_length=1, max_length=40)
    hauptthema: str | None = Field(default=None, min_length=1, max_length=40)
    good: str | None = Field(default=None, min_length=1, max_length=40)
    frei: str | None = Field(default=None, min_length=1, max_length=40)
    read: str | None = Field(default=None, min_length=1, max_length=40)
    watch: str | None = Field(default=None, min_length=1, max_length=40)
    listen: str | None = Field(default=None, min_length=1, max_length=40)


class FormatPatch(BaseModel):
    """Sendeformat: Namen der Abschnittsarten, Dauer eines kurzen Themas und der Plan einer neuen Folge."""
    labels: FormatLabels | None = None
    short_minutes: int | None = Field(default=None, ge=1, le=30)
    plan: list[Literal["blitzlicht", "hauptthema", "good:read", "good:watch", "good:listen", "frei"]] | None = Field(
        default=None, min_length=1, max_length=12)


class ArchivePatch(BaseModel):
    """Rohdaten ins Archiv, so viele Tage nach dem Veröffentlichen (0 = sofort). Wirkt nur mit `PS_ARCHIVE_DIR`."""
    after_days: int | None = Field(default=None, ge=0, le=3650)


class UiPatch(BaseModel):
    """Sprache der Oberfläche dieser Installation."""
    language: Literal["de", "en"] | None = None


class PlatformItem(BaseModel):
    name: str = Field(max_length=60)
    url: str = Field(max_length=500)


class SitePatch(BaseModel):
    """Öffentliche Seite der Show: an/aus und Links zu Podcast-Apps (höchstens 6)."""
    enabled: bool | None = None
    platforms: list[PlatformItem] | None = Field(default=None, max_length=core.MAX_PLATFORMS)


class StatsPatch(BaseModel):
    """Download-Zählung: Modus und Präfix des Zähldienstes (nur für `prefix`, https)."""
    mode: Literal["off", "app", "prefix"] | None = None
    prefix: str | None = Field(default=None, max_length=300)


class NumberingPatch(BaseModel):
    """Nummerierung: Staffel-Modus und Präfix vor dem Titel im Feed (Platzhalter `{number}`, `{season}`)."""
    season: Literal["keep", "year", "none"] | None = None
    prefix: str | None = Field(default=None, max_length=40)


class SettingsPatch(BaseModel):
    show: ShowPatch | None = None
    site: SitePatch | None = None
    stats: StatsPatch | None = None
    audio: AudioPatch | None = None
    assist: AssistPatch | None = None
    profile: ProfilePatch | None = None
    format: FormatPatch | None = None
    ui: UiPatch | None = None
    archive: ArchivePatch | None = None
    numbering: NumberingPatch | None = None


async def _refresh_feed(st) -> None:
    """Show-Daten und Cover stehen im Feed: Sind Folgen veröffentlicht, wird er sofort neu gebaut."""
    try:
        await asyncio.to_thread(publishing.refresh_feed, st.db, st.cfg)
    except publishing.PublishError as exc:
        raise ApiError(502, "storage", "Gespeichert, aber der Feed ließ sich nicht aktualisieren: {detail}", detail=exc) from exc


@router.get("/settings")
def settings_get(request: Request) -> dict:
    st = _st(request)
    return core.get_settings(st.db, st.cfg)


@router.put("/settings")
async def settings_put(body: SettingsPatch, request: Request) -> dict:
    st = _st(request)
    cur = core.get_settings(st.db, st.cfg)
    if body.show:
        patch = body.show.model_dump(exclude_unset=True, exclude_none=True)
        merged = {**cur["show"], **patch}
        if "title" not in patch and "title" not in st.db.get_setting("show", {}):
            merged.pop("title", None)  # der Standardtitel folgt der Sprache der Oberfläche, solange er nicht gespeichert ist
        st.db.set_setting("show", merged)
    if body.audio:
        st.db.set_setting("audio", {**cur["audio"], **body.audio.model_dump(exclude_unset=True, exclude_none=True)})
    if body.archive:
        st.db.set_setting("archive", {**{k: v for k, v in cur["archive"].items() if k != "available"},
                                      **body.archive.model_dump(exclude_unset=True, exclude_none=True)})
    if body.ui and body.ui.language:
        st.db.set_setting("ui", {"language": body.ui.language})
    if body.profile:
        patch = {k: v.strip() for k, v in body.profile.model_dump(exclude_unset=True, exclude_none=True).items()}
        st.db.set_setting("profile", {**cur["profile"], **patch})
    if body.format:
        fmt = body.format.model_dump(exclude_unset=True, exclude_none=True)
        stored = st.db.get_setting("format", {})  # nur Gespeichertes fortschreiben: Standardnamen folgen der Sprache
        new = {**cur["format"], **{k: v for k, v in fmt.items() if k != "labels"}}
        new["labels"] = {**(stored.get("labels") or {}), **{k: v.strip() for k, v in (fmt.get("labels") or {}).items() if v.strip()}}
        st.db.set_setting("format", new)
        st.room.companion.set_plan_format()
        await _room_update(request)
    if body.assist and body.assist.enabled is not None:
        await st.room.companion.set_enabled(body.assist.enabled)
    site_changed = await _site_and_stats_put(body, st, cur)
    if body.numbering:
        patch = body.numbering.model_dump(exclude_unset=True, exclude_none=True)
        new = {**cur["numbering"], **patch}
        if not core.prefix_ok(new["prefix"]):
            raise ApiError(422, "invalid", "Im Präfix sind als Platzhalter nur {a} und {b} erlaubt", a="{number}", b="{season}")
        st.db.set_setting("numbering", new)
        site_changed = True
    if body.show or site_changed:
        await _refresh_feed(st)
    return core.get_settings(st.db, st.cfg)


async def _site_and_stats_put(body: SettingsPatch, st, cur: dict) -> bool:
    """Öffentliche Seite und Download-Zählung speichern. -> True, wenn der Feed neu gebaut werden muss."""
    changed = False
    if body.site:
        patch = body.site.model_dump(exclude_unset=True, exclude_none=True)
        if "platforms" in patch:
            given = len(patch["platforms"])
            patch["platforms"] = core.clean_platforms(patch["platforms"])
            if len(patch["platforms"]) != given:
                raise ApiError(422, "invalid", "Jeder Link braucht einen Namen und eine Adresse mit https:// oder http://")
        new = {**cur["site"], **patch}
        st.db.set_setting("site", new)
        changed = True
        if cur["site"]["enabled"] and not new["enabled"]:
            try:
                await asyncio.to_thread(publishing.remove_site, st.db, st.cfg)
            except Exception as exc:  # noqa: BLE001 - Speicher nicht erreichbar: die Einstellung gilt trotzdem
                raise ApiError(502, "storage", "Gespeichert, aber die Seite ließ sich nicht entfernen: {detail}", detail=exc) from exc
    if body.stats:
        patch = body.stats.model_dump(exclude_unset=True, exclude_none=True)
        new = {**cur["stats"], **patch}
        new["prefix"] = new["prefix"].strip()
        if new["mode"] == "prefix" and not re.fullmatch(r"https://[^\s<>\"']+", new["prefix"]):
            raise ApiError(422, "invalid", "Das Präfix des Zähldienstes muss mit https:// beginnen")
        st.db.set_setting("stats", new)
        changed = True
    return changed


@router.post("/settings/cover")
async def settings_cover(request: Request, file: UploadFile = File(...)) -> dict:
    st = _st(request)
    data = await file.read(MAX_COVER_BYTES + 1)
    if len(data) > MAX_COVER_BYTES:
        raise ApiError(413, "too_large", "Das Cover ist zu groß (höchstens 10 MB)")
    try:
        ext = cover.validate(data)
    except cover.CoverError as exc:
        raise ApiError(422, "bad_cover", str(exc)) from exc
    key = f"cover-{hashlib.sha256(data).hexdigest()[:8]}.{ext}"
    st.cfg.cover_dir.mkdir(parents=True, exist_ok=True)
    (st.cfg.cover_dir / key).write_bytes(data)
    show = st.db.get_setting("show", {})
    old = show.get("cover_key")
    st.db.set_setting("show", {**show, "cover_key": key})
    if old and old != key:
        (st.cfg.cover_dir / old).unlink(missing_ok=True)
    await _refresh_feed(st)
    return core.get_settings(st.db, st.cfg)


@router.get("/settings/cover")
def settings_cover_get(request: Request):
    st = _st(request)
    key = core.get_settings(st.db, st.cfg)["show"].get("cover_key")
    path = st.cfg.cover_dir / key if key else None
    if not path or not path.exists():
        raise ApiError(404, "no_cover", "Es ist kein Cover hochgeladen")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})


# --- Folgen ---------------------------------------------------------------------------------------------
def _episode_or_404(request: Request, episode_id: str) -> dict:
    ep = core.get_episode(_st(request).db, episode_id)
    if ep is None:
        raise ApiError(404, "episode_not_found", "Folge nicht gefunden")
    return ep


def _files(cfg, ep_id: str) -> dict:
    out = cfg.episode_dir(ep_id) / "out"
    return {"mix": (out / "mix_uncut.m4a").exists(), "episode": (out / "episode.mp3").exists(),
            "peaks": (out / "peaks.json").exists(), "transcript": (out / "transcript.json").exists(),
            "chapters": (out / "chapters.json").exists()}


@router.get("/episodes/renumber")
def renumber_preview(request: Request) -> dict:
    """Was „Neu nummerieren“ ändern würde (nichts wird gespeichert)."""
    st = _st(request)
    plan = core.renumber_plan(st.db)
    total = st.db.one("SELECT COUNT(*) AS n FROM episodes WHERE status!='draft'")["n"]
    return {"changes": plan, "total": total, "next_number": total + 1}


class RenumberBody(BaseModel):
    count: int = Field(ge=0)  # Anzahl der Änderungen aus der Vorschau: Hat sich inzwischen etwas geändert, gibt es 409


@router.post("/episodes/renumber")
async def renumber_apply(body: RenumberBody, request: Request) -> dict:
    st = _st(request)
    if st.db.one("SELECT id FROM episodes WHERE status IN ('recording','uploading')"):
        raise ApiError(409, "busy", "Während einer Aufnahme lässt sich nicht neu nummerieren")
    plan = core.renumber_plan(st.db)
    if len(plan) != body.count:
        raise ApiError(409, "changed", "Die Folgen haben sich seit der Vorschau geändert. Bitte die Vorschau neu laden")
    core.apply_renumber(st.db, plan)
    if plan:
        await _refresh_feed(st)
    return {"changed": len(plan), "next_number": core.next_number(st.db)}


@router.get("/episodes")
def episodes_list(request: Request) -> list[dict]:
    st = _st(request)
    lang = i18n.ui_lang(st.db)
    running = {j["episode_id"]: j for j in st.db.all(
        "SELECT * FROM jobs WHERE status IN ('queued','running') AND episode_id IS NOT NULL ORDER BY created_at")}
    out = []
    for r in st.db.all("SELECT * FROM episodes ORDER BY COALESCE(published_at, publish_at, created_at) DESC"):
        item = episode_item(r)
        j = running.get(r["id"])
        item["job"] = job_progress(j, lang) if j else None
        out.append(item)
    return out


def episode_item(r: dict) -> dict:
    """Folge ohne große Felder (Liste, `POST /episodes/draft`)."""
    item = {k: r[k] for k in ("id", "status", "source", "number", "season", "title", "summary", "duration_ms",
                              "created_at", "publish_at", "published_at", "episode_type", "label")}
    item["explicit"] = bool(r["explicit"])
    return item


def job_progress(j: dict, lang: str = "de") -> dict:
    return {"id": j["id"], "kind": j["kind"], "status": j["status"], "progress": j["progress"],
            "message": i18n.tr(lang, j["message"]) if j["message"] else j["message"]}


@router.get("/episodes/{episode_id}")
def episode_get(episode_id: str, request: Request) -> dict:
    st = _st(request)
    ep = _episode_or_404(request, episode_id)
    out = {k: v for k, v in ep.items() if k not in ("legacy",)}
    out["legacy"] = ep["legacy"]
    out["tracks"] = core.tracks_of(st.db, episode_id)
    for t in out["tracks"]:
        t.update(core.track_progress(st.db, t))
        t["has_raw"] = (st.cfg.episode_dir(episode_id) / "tracks" / t["id"] / "raw.flac").exists()
    out["events"] = core.events_of(st.db, episode_id)
    out["jobs"] = [jobs.job_public(j, with_log=False, lang=i18n.ui_lang(st.db)) for j in
                   st.db.all("SELECT * FROM jobs WHERE episode_id=? ORDER BY created_at DESC LIMIT 10", (episode_id,))]
    out["chapters_preview"] = core.chapters_preview(st.db, ep)
    out["notes"] = core.notes_of(st.db, ep)  # Merkpunkte für den Schnitt, Zeit in Sitzungszeit
    out["numbering"] = core.get_numbering(st.db)  # für die Vorschau des Feed-Titels
    out["has_transcript"] = (st.cfg.episode_dir(episode_id) / "out" / "transcript.json").exists()
    out["costs"] = core.costs_of(st.db, episode_id)
    out["storage"] = {"local_bytes": archive.dir_bytes(st.cfg.episode_dir(episode_id)), "archived": bool(ep["archived_at"]),
                      "archive_available": st.cfg.archive_dir is not None}
    out["files"] = _files(st.cfg, episode_id)
    if isinstance(out.get("report"), dict) and out["report"].get("warnings"):  # Warnungen der Pipeline stehen deutsch in der Datenbank
        lang = i18n.ui_lang(st.db)
        out["report"] = {**out["report"], "warnings": [i18n.tr(lang, w) for w in out["report"]["warnings"]]}
    return out


class Cut(BaseModel):
    from_ms: int = Field(ge=0)
    to_ms: int = Field(ge=0)


class EdlBody(BaseModel):
    trim_start_ms: int = Field(default=0, ge=0)
    trim_end_ms: int | None = Field(default=None, ge=0)
    cuts: list[Cut] = Field(default_factory=list, max_length=500)


class LinkItem(BaseModel):
    url: str = Field(max_length=1000)
    title: str = Field(default="", max_length=200)


class EpisodePatch(BaseModel):
    label: str | None = Field(default=None, max_length=120)  # Arbeitstitel einer Vorbereitung
    title: str | None = Field(default=None, max_length=300)
    number: int | None = Field(default=None, ge=0)
    season: int | None = Field(default=None, ge=0)
    summary: str | None = Field(default=None, max_length=4000)
    description_html: str | None = Field(default=None, max_length=50000)
    explicit: bool | None = None
    episode_type: Literal["full", "trailer", "bonus"] | None = None
    edl: EdlBody | None = None
    chapter_titles: dict[str, str] | None = None
    links: list[LinkItem] | None = Field(default=None, max_length=10)  # Good Reads und Links für den Vorschlag


@router.patch("/episodes/{episode_id}")
def episode_patch(episode_id: str, body: EpisodePatch, request: Request) -> dict:
    st = _st(request)
    ep = _episode_or_404(request, episode_id)
    changes = body.model_dump(exclude_unset=True)
    fields: dict = {}
    if "label" in changes and changes["label"] is not None:
        fields["label"] = changes["label"].strip()
    for k in ("title", "number", "season", "summary", "description_html", "explicit", "episode_type"):
        if k in changes:
            if k == "episode_type" and changes[k] is None:
                continue
            fields[k] = changes[k]
    if "number" in fields and fields["number"] is not None:
        if core.get_numbering(st.db)["season"] == "year":  # Jahr als Staffel: die Nummer läuft über alle Jahre
            dup = st.db.one("SELECT id FROM episodes WHERE number=? AND id!=? AND status!='draft'", (fields["number"], episode_id))
        else:
            dup = st.db.one("SELECT id FROM episodes WHERE number=? AND id!=? AND COALESCE(season,0)=COALESCE(?,0)",
                            (fields["number"], episode_id, fields.get("season", ep["season"])))
        if dup:
            raise ApiError(409, "number_taken", "Die Folgennummer {number} ist schon vergeben", number=fields["number"])
    if body.links is not None:
        links = []
        for it in body.links:
            url, title = it.url.strip(), it.title.strip()
            if not re.fullmatch(r"https?://[^\s<>\"']+", url, re.I):
                raise ApiError(422, "invalid", "Jeder Link braucht eine Adresse mit https:// oder http://")
            links.append({"url": url, "title": title})
        fields["links"] = links
    if body.edl is not None:
        fields["edl"] = core.normalize_edl(body.edl.model_dump())
    if body.chapter_titles is not None:
        known = {c["event_id"] for c in core.chapters_preview(st.db, ep)}
        titles = dict(ep["ai"].get("chapter_titles", {}))
        for eid, title in body.chapter_titles.items():
            if eid not in known:
                raise ApiError(422, "invalid", "Unbekanntes Kapitel")
            if title.strip():
                titles[eid] = title.strip()[:200]
            else:
                titles.pop(eid, None)
        fields["ai"] = {**ep["ai"], "chapter_titles": titles}
    core.update_episode(st.db, episode_id, **fields)
    return episode_get(episode_id, request)


@router.delete("/episodes/{episode_id}")
async def episode_delete(episode_id: str, request: Request) -> dict:
    st = _st(request)
    ep = _episode_or_404(request, episode_id)
    if ep["status"] not in ("draft", "review", "failed"):
        raise ApiError(409, "not_deletable", "Löschen geht nur bei Entwurf, Prüfung oder Fehler")
    if st.db.one("SELECT 1 AS x FROM jobs WHERE episode_id=? AND status IN ('queued','running')", (episode_id,)):
        raise ApiError(409, "job_running", "Für diese Folge läuft gerade eine Verarbeitung")
    with st.db.tx() as c:
        c.execute("DELETE FROM clock_points WHERE track_id IN (SELECT id FROM tracks WHERE episode_id=?)", (episode_id,))
        c.execute("DELETE FROM chunks WHERE track_id IN (SELECT id FROM tracks WHERE episode_id=?)", (episode_id,))
        for table in ("tracks", "events", "jobs", "segments", "cards"):
            c.execute(f"DELETE FROM {table} WHERE episode_id=?", (episode_id,))
        c.execute("DELETE FROM episodes WHERE id=?", (episode_id,))
    shutil.rmtree(st.cfg.episode_dir(episode_id), ignore_errors=True)
    archive.remove_copy(ep)
    await _room_update(request)
    return {"deleted": True}


@router.post("/episodes/{episode_id}/discard")
async def episode_discard(episode_id: str, request: Request) -> dict:
    """Aufnahme verwerfen und neu ansetzen: Spuren und alles Abgeleitete weg; die Folge wird wieder zur Vorbereitung
    mit Sendeplan und Dossiers und ist im Studio für die nächste Aufnahme gewählt."""
    st = _st(request)
    ep = _episode_or_404(request, episode_id)
    if ep["source"] != "recorded":
        raise ApiError(409, "bad_state", "Importierte Folgen lassen sich nicht verwerfen")
    if ep["status"] == "recording":
        raise ApiError(409, "recording", "Die Aufnahme läuft noch. Erst beenden, dann verwerfen")
    if ep["status"] not in core.DISCARDABLE:
        raise ApiError(409, "bad_state", "Verwerfen geht nur nach der Aufnahme, während der Verarbeitung, in der Prüfung oder nach einem Fehler")
    runner = request.app.state.runner
    try:
        if not await runner.cancel_episode(episode_id):
            raise ApiError(409, "job_running", "Für diese Folge läuft gerade noch ein Arbeitsschritt. Bitte gleich noch einmal versuchen")
        await st.room.companion.discard_pending()  # der Begleiter schreibt nach dem Stopp noch sein Protokoll
        target = core.discard_recording(st.db, ep)
    finally:
        runner.release_cancel(episode_id)
    shutil.rmtree(st.cfg.episode_dir(episode_id), ignore_errors=True)
    archive.remove_copy(ep)
    await st.room.broadcast({"type": "rec.discard", "episode_id": episode_id})
    await _room_update(request)
    return {"discarded": True, "episode_id": target}


# --- Vorbereitungen ---------------------------------------------------------------------------------
class PreparationBody(BaseModel):
    label: str = Field(default="", max_length=120)


@router.get("/preparations")
def preparations_list(request: Request) -> dict:
    """Alle Vorbereitungen und die Wahl des Studios (`selected`: ID, oder None für „ohne Vorbereitung“)."""
    st = _st(request)
    cur = core.current_episode(st.db)
    busy = cur is not None and cur["status"] in ("recording", "uploading")
    chosen, eid = core.studio_choice(st.db)
    selected = None if (chosen and eid is None) else (cur["id"] if cur and cur["status"] == "draft" else None)
    return {"preparations": core.preparations(st.db), "selected": selected, "busy": busy}


@router.post("/preparations", status_code=201)
async def preparation_create(request: Request, body: PreparationBody | None = None) -> dict:
    """Neue Vorbereitung mit dem Standardplan aus dem Sendeformat."""
    st = _st(request)
    ep = core.new_draft(st.db, label=(body.label.strip() if body else ""))
    await _room_update(request)
    return episode_item(ep)


class StudioChoice(BaseModel):
    episode_id: str | None = None


@router.put("/studio/preparation")
async def studio_preparation(body: StudioChoice, request: Request) -> dict:
    """Worauf die nächste Aufnahme aufbaut: eine Vorbereitung oder (`episode_id: null`) keine."""
    st = _st(request)
    cur = core.current_episode(st.db)
    if cur is not None and cur["status"] in ("recording", "uploading"):
        raise ApiError(409, "busy", "Während einer Aufnahme oder eines Uploads lässt sich die Vorbereitung nicht wechseln")
    if body.episode_id is not None:
        ep = core.get_episode(st.db, body.episode_id)
        if ep is None or ep["status"] != "draft":
            raise ApiError(404, "preparation_not_found", "Vorbereitung nicht gefunden")
    core.set_studio_choice(st.db, body.episode_id)
    await _room_update(request)
    return preparations_list(request)


def _job_response(request: Request, job_id: str) -> dict:
    request.app.state.runner.wake()
    return {"job_id": job_id}


def _not_archived(ep: dict) -> None:
    if ep["archived_at"]:
        raise ApiError(409, "archived", "Die Rohdaten liegen im Archiv. Bitte zuerst zurückholen")


@router.post("/episodes/{episode_id}/restore")
def episode_restore(episode_id: str, request: Request) -> dict:
    """Rohdaten aus dem Archiv zurückholen (Job), damit Neu mischen und Neu verarbeiten wieder gehen."""
    ep = _episode_or_404(request, episode_id)
    if not ep["archived_at"]:
        raise ApiError(409, "not_archived", "Die Rohdaten dieser Folge liegen nicht im Archiv")
    if not ep["archive_path"] or not Path(ep["archive_path"]).is_dir():
        raise ApiError(409, "archive_unreachable", "Das Archiv ist nicht erreichbar")
    return _job_response(request, jobs.enqueue(_st(request).db, episode_id, "restore"))


@router.post("/episodes/{episode_id}/process")
def episode_process(episode_id: str, request: Request) -> dict:
    ep = _episode_or_404(request, episode_id)
    if ep["status"] not in ("uploading", "review", "failed"):
        raise ApiError(409, "bad_state", "Verarbeiten geht nur nach der Aufnahme, in der Prüfung oder nach einem Fehler")
    if ep["rec_start_server_ms"] is None:
        raise ApiError(409, "no_recording", "Die Folge hat keine Aufnahme")
    _not_archived(ep)
    return _job_response(request, jobs.start_processing(_st(request).db, episode_id))


@router.post("/episodes/{episode_id}/remix")
def episode_remix(episode_id: str, request: Request) -> dict:
    st = _st(request)
    ep = _episode_or_404(request, episode_id)
    if ep["status"] not in ("review", "failed") or ep["source"] != "recorded":
        raise ApiError(409, "bad_state", "Neu mischen geht nur in der Prüfung")
    _not_archived(ep)
    if not (st.cfg.episode_dir(episode_id) / "work" / "stems").exists():
        raise ApiError(409, "no_stems", "Es gibt noch keine Zwischenstände. Bitte die Verarbeitung komplett neu starten")
    core.update_episode(st.db, episode_id, status="processing")
    return _job_response(request, jobs.enqueue(st.db, episode_id, "remix"))


@router.post("/episodes/{episode_id}/suggest")
def episode_suggest(episode_id: str, request: Request) -> dict:
    """Vorschlag aus dem Transkript. Fehlt das Transkript (z. B. weil die Transkription scheiterte), entsteht es zuerst;
    der Vorschlag folgt danach von selbst (`kind: "transcribe"` in der Antwort)."""
    st = _st(request)
    ep = _episode_or_404(request, episode_id)
    if not st.cfg.anthropic_key:
        raise ApiError(409, "no_ai", "Kein Schlüssel für Vorschläge eingerichtet")
    if not (st.cfg.episode_dir(episode_id) / "out" / "transcript.json").exists():
        if ep["source"] != "recorded" or not (st.cfg.episode_dir(episode_id) / "out" / "episode.mp3").exists():
            raise ApiError(409, "no_transcript", "Ohne Transkript gibt es keinen Vorschlag, und für diese Folge lässt sich keins erstellen")
        _not_archived(ep)
        return {**_job_response(request, jobs.enqueue(st.db, episode_id, "transcribe")), "kind": "transcribe"}
    return {**_job_response(request, jobs.enqueue(st.db, episode_id, "suggest")), "kind": "suggest"}


class PublishBody(BaseModel):
    publish_at: int | str | None = None


def _parse_time(value: int | str | None) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    try:
        d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(422, "invalid", "Ungültiger Zeitpunkt für die Veröffentlichung") from None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


@router.post("/episodes/{episode_id}/publish")
def episode_publish(episode_id: str, request: Request, body: PublishBody | None = None) -> dict:
    st = _st(request)
    ep = _episode_or_404(request, episode_id)
    if ep["status"] not in ("review", "scheduled", "published") or (ep["source"] == "imported" and ep["status"] != "published"):
        raise ApiError(409, "bad_state", "Veröffentlichen geht nur aus der Prüfung")
    if not ep["title"]:
        raise ApiError(409, "no_title", "Bitte zuerst einen Titel eintragen")
    if ep["source"] == "recorded" and not (st.cfg.episode_dir(episode_id) / "out" / "episode.mp3").exists():
        raise ApiError(409, "no_audio", "Die fertige Folge fehlt. Bitte zuerst verarbeiten")
    at = _parse_time(body.publish_at if body else None)
    now = util.now_ms()
    if at is not None and at > now + 30_000:
        core.update_episode(st.db, episode_id, status="scheduled", publish_at=at)
        return {"status": "scheduled", "publish_at": at, "job_id": None}
    core.update_episode(st.db, episode_id, publish_at=now)
    return {"status": "publishing", "publish_at": now, **_job_response(request, jobs.enqueue(st.db, episode_id, "publish"))}


@router.post("/episodes/{episode_id}/unpublish")
def episode_unpublish(episode_id: str, request: Request) -> dict:
    st = _st(request)
    ep = _episode_or_404(request, episode_id)
    if ep["status"] == "scheduled":
        core.update_episode(st.db, episode_id, status="review", publish_at=None)
        return {"status": "review", "job_id": None}
    if ep["status"] != "published":
        raise ApiError(409, "bad_state", "Die Folge ist nicht veröffentlicht")
    if ep["source"] != "recorded":
        raise ApiError(409, "imported", "Importierte Folgen lassen sich hier nicht zurückziehen")
    return {"status": "unpublishing", **_job_response(request, jobs.enqueue(st.db, episode_id, "unpublish"))}


# --- Dateien ---------------------------------------------------------------------------------------------
def _file(path: Path, media_type: str, missing: str) -> FileResponse:
    if not path.exists():
        raise ApiError(404, "not_found", missing)
    return FileResponse(path, media_type=media_type)  # Starlette beantwortet Range-Anfragen mit 206


@router.get("/episodes/{episode_id}/audio/{which}")
def episode_audio(episode_id: str, which: Literal["mix", "episode"], request: Request):
    st = _st(request)
    ep = _episode_or_404(request, episode_id)
    out = st.cfg.episode_dir(episode_id) / "out"
    if which == "mix":
        return _file(out / "mix_uncut.m4a", "audio/mp4", "Die ungeschnittene Mischung gibt es noch nicht")
    if (out / "episode.mp3").exists():
        return FileResponse(out / "episode.mp3", media_type="audio/mpeg")
    if ep["audio_key"]:  # importierte Folge: liegt im Speicher
        return RedirectResponse(publishing.storage().url(ep["audio_key"]), status_code=307)
    raise ApiError(404, "not_found", "Die fertige Folge gibt es noch nicht")


@router.get("/episodes/{episode_id}/peaks")
def episode_peaks(episode_id: str, request: Request):
    _episode_or_404(request, episode_id)
    return _file(_st(request).cfg.episode_dir(episode_id) / "out" / "peaks.json", "application/json", "Die Wellenform gibt es noch nicht")


@router.get("/episodes/{episode_id}/transcript")
def episode_transcript(episode_id: str, request: Request):
    _episode_or_404(request, episode_id)
    return _file(_st(request).cfg.episode_dir(episode_id) / "out" / "transcript.json", "application/json", "Das Transkript gibt es noch nicht")


@router.get("/episodes/{episode_id}/tracks/{track_id}/raw")
def episode_raw(episode_id: str, track_id: str, request: Request):
    st = _st(request)
    ep = _episode_or_404(request, episode_id)
    t = st.db.one("SELECT * FROM tracks WHERE id=? AND episode_id=?", (track_id, episode_id))
    if t is None:
        raise ApiError(404, "track_not_found", "Spur nicht gefunden")
    path = st.cfg.episode_dir(episode_id) / "tracks" / track_id / "raw.flac"
    if not path.exists() and ep["archived_at"]:
        _not_archived(ep)
    if not path.exists():
        raise ApiError(404, "not_found", "Die Rohspur ist noch nicht zusammengesetzt")
    name = f"{util.slugify(t['participant_name'])}-{track_id}.flac"
    return FileResponse(path, media_type="audio/flac", filename=name)


# --- Import, Jobs, Feed, Status -------------------------------------------------------------------------------
class ImportBody(BaseModel):
    feed_url: str = Field(min_length=8, max_length=1000)


@router.post("/import", status_code=202)
def feed_import(body: ImportBody, request: Request) -> dict:
    if not body.feed_url.startswith(("http://", "https://")):
        raise ApiError(422, "invalid", "Die Feed-Adresse muss mit http:// oder https:// beginnen")
    return _job_response(request, jobs.enqueue(_st(request).db, None, "import", {"feed_url": body.feed_url}))


@router.get("/jobs/{job_id}")
def job_get(job_id: str, request: Request) -> dict:
    j = _st(request).db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if j is None:
        raise ApiError(404, "job_not_found", "Aufgabe nicht gefunden")
    return jobs.job_public(j, lang=i18n.ui_lang(_st(request).db))


@router.get("/feed/preview")
def feed_preview(request: Request):
    st = _st(request)
    try:
        xml = publishing.feed_xml(st.db, st.cfg)
    except publishing.PublishError as exc:
        raise ApiError(409, "storage", str(exc)) from exc
    return Response(xml, media_type="application/rss+xml")


@router.get("/status")
def status(request: Request) -> dict:
    cfg = _st(request).cfg
    if cfg.storage == "s3":
        storage = {"backend": "s3", "configured": cfg.s3_configured and bool(cfg.public_base_url)}
    else:
        storage = {"backend": "local", "configured": True}
    storage["public_base_url"] = cfg.public_base_url
    turn_ok = {"none": False, "static": bool(cfg.turn_urls), "cloudflare": bool(cfg.cf_turn_key_id and cfg.cf_turn_api_token)}
    models = cfg.data / "models"
    return {
        "storage": storage,
        "turn": {"mode": cfg.turn_mode, "configured": turn_ok.get(cfg.turn_mode, False)},
        "ai": {"configured": bool(cfg.anthropic_key), "model": cfg.ai_model},
        "stt": {"configured": bool(cfg.openai_key), "model": cfg.stt_model},
        "assist": _st(request).room.companion.status_summary(),
        "whisper": {"model": cfg.whisper_model, "downloaded": models.exists() and any(models.iterdir())},
        "disk_free_bytes": shutil.disk_usage(cfg.data).free,
        "archive": archive.totals(cfg),
        "costs": core.month_costs(_st(request).db),
    }
