"""Sendeplan und Dossiers (SPEC Abschnitt 14): Host-API unter /api/host/ und Recherche-Läufer."""
from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from . import core, i18n, util
from .auth import host_guard
from .errors import ApiError

log = logging.getLogger("podcast.plan")

router = APIRouter(prefix="/api/host", dependencies=[Depends(host_guard)])

MAX_PARALLEL_RESEARCH = 4
RESTART_NOTE = "Die Recherche wurde durch einen Neustart des Servers unterbrochen. Bitte erneut starten"
NO_KEY_NOTE = "Für die Recherche fehlt der Schlüssel des KI-Dienstes (ANTHROPIC_API_KEY). Bitte in der Konfiguration eintragen"


def mark_stale_research(db) -> None:
    """Beim Start: hängende Recherchen können nicht mehr laufen."""
    db.run("UPDATE segments SET status='failed', error=?, updated_at=? WHERE status='researching'", (RESTART_NOTE, util.now_ms()))


def segment_public(seg: dict, progress: dict | None = None, lang: str = "de", cost_usd: float = 0.0) -> dict:
    """Abschnitt für die Oberfläche. Fehler und Fortschrittsmeldungen werden in `lang` übersetzt (gespeichert ist Deutsch)."""
    out = {k: seg[k] for k in ("id", "episode_id", "sort", "kind", "title", "input", "good_type", "status", "dossier",
                               "notes", "visited_at", "updated_at", "error")}
    out["cost_usd"] = cost_usd  # bisherige Recherchekosten dieses Abschnitts in US-Dollar
    if out["error"]:
        out["error"] = i18n.tr(lang, out["error"])
    if seg["status"] == "researching" and progress:
        out["progress"], out["message"] = progress.get("progress", 0), i18n.tr(lang, progress.get("message", ""))
    return out


class Researcher:
    """Startet `assist.prep.research_async` als asyncio-Task neben dem Job-Läufer. Höchstens vier laufen zugleich,
    weitere warten (Status bleibt `researching`)."""

    def __init__(self, app):
        self.app = app
        self.tasks: dict[str, asyncio.Task] = {}
        self.progress: dict[str, dict] = {}
        self._sem: asyncio.Semaphore | None = None

    @property
    def db(self):
        return self.app.state.db

    def running(self, segment_id: str) -> bool:
        t = self.tasks.get(segment_id)
        return t is not None and not t.done()

    def start(self, segment_id: str) -> bool:
        """False, wenn für den Abschnitt schon eine Recherche läuft."""
        if self.running(segment_id):
            return False
        if self._sem is None:
            self._sem = asyncio.Semaphore(MAX_PARALLEL_RESEARCH)
        self.db.run("UPDATE segments SET status='researching', error=NULL, updated_at=? WHERE id=?", (util.now_ms(), segment_id))
        self.progress[segment_id] = {"progress": 0.0, "message": "Wartet auf einen freien Platz"}
        self.tasks[segment_id] = asyncio.get_running_loop().create_task(self._run(segment_id))
        return True

    async def stop(self) -> None:
        tasks = [t for t in self.tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        mark_stale_research(self.db)

    def _on_progress(self, segment_id: str, p: float, msg: str) -> None:
        self.progress[segment_id] = {"progress": p, "message": msg}

    async def _run(self, segment_id: str) -> None:
        status, fields = "failed", {}
        try:
            async with self._sem:
                seg = self.db.one("SELECT * FROM segments WHERE id=?", (segment_id,))
                if seg is None:
                    return
                self.progress[segment_id] = {"progress": 0.0, "message": "Recherche startet"}
                ctx = self._context(seg)
                from assist import prep  # erst hier: der Server startet auch ohne das Paket

                dossier = await prep.research_async(
                    {"id": seg["id"], "kind": seg["kind"], "title": seg["title"], "input": seg["input"],
                     "good_type": seg["good_type"]},
                    ctx["show"], ctx["previous_titles"], ctx["plan_context"],
                    on_progress=lambda p, m: self._on_progress(segment_id, p, m))
            status, fields = "ready", {"dossier": util.dumps(dossier), "error": None}
            core.add_cost(self.db, seg["episode_id"], "research", (dossier.get("meta") or {}).get("kosten_usd") or 0, segment_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - AssistError trägt deutschen Klartext
            from assist.llm import AssistError

            if isinstance(exc, AssistError):
                msg = str(exc)
            else:
                log.exception("Recherche %s abgestürzt", segment_id)
                msg = "Die Recherche ist unerwartet fehlgeschlagen. Bitte erneut versuchen"
            fields = {"error": msg}
        finally:
            self.progress.pop(segment_id, None)
        if not fields:
            return
        # nur Status und Dossier schreiben: Eingaben des Hosts bleiben unangetastet; gelöschte Abschnitte ändern nichts
        sets = ", ".join(f"{k}=?" for k in fields)
        n = self.db.run(f"UPDATE segments SET status=?, {sets}, updated_at=? WHERE id=?",
                        (status, *fields.values(), util.now_ms(), segment_id))
        if n:
            seg = self.db.one("SELECT episode_id FROM segments WHERE id=?", (segment_id,))
            await self.app.state.room.plan_changed(seg["episode_id"])

    def _context(self, seg: dict) -> dict:
        db = self.db
        show = core.assist_show(db, self.app.state.cfg)
        previous = [r["title"] for r in db.all(
            "SELECT title FROM episodes WHERE id!=? AND title IS NOT NULL AND title!='' "
            "ORDER BY COALESCE(published_at, created_at) DESC LIMIT 30", (seg["episode_id"],))]
        others = [{"id": s["id"], "kind": s["kind"], "title": s["title"], "input": s["input"], "good_type": s["good_type"]}
                  for s in core.plan_of(db, seg["episode_id"]) if s["id"] != seg["id"]]
        return {"show": show, "previous_titles": previous, "plan_context": others}


# --- REST ------------------------------------------------------------------------------------------------
def _seg_or_404(request: Request, segment_id: str) -> dict:
    seg = request.app.state.db.one("SELECT * FROM segments WHERE id=?", (segment_id,))
    if seg is None:
        raise ApiError(404, "segment_not_found", "Abschnitt nicht gefunden")
    return core.segment_load(seg)


def _episode_or_404(request: Request, episode_id: str) -> dict:
    ep = core.get_episode(request.app.state.db, episode_id)
    if ep is None:
        raise ApiError(404, "episode_not_found", "Folge nicht gefunden")
    return ep


def _out(request: Request, seg: dict, costs: dict | None = None) -> dict:
    db = request.app.state.db
    if costs is None:
        costs = core.segment_costs(db, seg["episode_id"])
    return segment_public(seg, request.app.state.researcher.progress.get(seg["id"]), i18n.ui_lang(db),
                          costs.get(seg["id"], 0.0))


def _renumber(c, episode_id: str, ids: list[str]) -> None:
    for i, sid in enumerate(ids):
        c.execute("UPDATE segments SET sort=? WHERE id=? AND episode_id=?", (i, sid, episode_id))


@router.get("/episodes/{episode_id}/segments")
def segments_list(episode_id: str, request: Request) -> list[dict]:
    _episode_or_404(request, episode_id)
    costs = core.segment_costs(request.app.state.db, episode_id)
    return [_out(request, s, costs) for s in core.plan_of(request.app.state.db, episode_id)]


class SegmentBody(BaseModel):
    kind: Literal["blitzlicht", "hauptthema", "good", "frei"]
    title: str = Field(default="", max_length=200)
    input: str = Field(default="", max_length=20000)
    good_type: Literal["read", "watch", "listen"] | None = None


@router.post("/episodes/draft")
def episode_draft(request: Request) -> dict:
    """Die aktuelle Folge (`draft`; läuft schon eine Aufnahme oder ein Upload, diese), bei Bedarf neu mit Standardplan."""
    from .api_host import episode_item

    ep = core.current_episode(request.app.state.db, create=True)
    return {"episode": episode_item(ep)}


@router.post("/episodes/{episode_id}/segments", status_code=201)
async def segment_create(episode_id: str, body: SegmentBody, request: Request) -> dict:
    db = request.app.state.db
    _episode_or_404(request, episode_id)
    good = (body.good_type or "read") if body.kind == "good" else None
    sid = util.new_id()
    with db.tx() as c:
        top = c.execute("SELECT COALESCE(MAX(sort),-1) AS m FROM segments WHERE episode_id=?", (episode_id,)).fetchone()["m"]
        c.execute("INSERT INTO segments(id,episode_id,sort,kind,title,input,good_type,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                  (sid, episode_id, top + 1, body.kind, body.title.strip(), body.input, good, util.now_ms()))
    await request.app.state.room.plan_changed(episode_id)
    return _out(request, _seg_or_404(request, sid))


class SegmentPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    input: str | None = Field(default=None, max_length=20000)
    good_type: Literal["read", "watch", "listen"] | None = None
    notes: str | None = Field(default=None, max_length=50000)
    sort: int | None = Field(default=None, ge=0)


@router.patch("/segments/{segment_id}")
async def segment_patch(segment_id: str, body: SegmentPatch, request: Request) -> dict:
    db = request.app.state.db
    seg = _seg_or_404(request, segment_id)
    changes = body.model_dump(exclude_unset=True)
    fields: dict = {}
    for k in ("title", "input", "notes"):
        if changes.get(k) is not None:
            fields[k] = changes[k].strip() if k == "title" else changes[k]
    if "good_type" in changes and seg["kind"] == "good":
        fields["good_type"] = changes["good_type"] or "read"
    with db.tx() as c:
        if fields:
            c.execute(f"UPDATE segments SET {', '.join(k + '=?' for k in fields)}, updated_at=? WHERE id=?",
                      (*fields.values(), util.now_ms(), segment_id))
        if changes.get("sort") is not None:
            ids = [r["id"] for r in c.execute("SELECT id FROM segments WHERE episode_id=? ORDER BY sort, rowid",
                                              (seg["episode_id"],)).fetchall()]
            ids.remove(segment_id)
            ids.insert(min(changes["sort"], len(ids)), segment_id)
            _renumber(c, seg["episode_id"], ids)
    await request.app.state.room.plan_changed(seg["episode_id"])
    return _out(request, _seg_or_404(request, segment_id))


@router.delete("/segments/{segment_id}")
async def segment_delete(segment_id: str, request: Request) -> dict:
    db = request.app.state.db
    seg = _seg_or_404(request, segment_id)
    with db.tx() as c:
        c.execute("DELETE FROM segments WHERE id=?", (segment_id,))
        ids = [r["id"] for r in c.execute("SELECT id FROM segments WHERE episode_id=? ORDER BY sort, rowid", (seg["episode_id"],)).fetchall()]
        _renumber(c, seg["episode_id"], ids)
    await request.app.state.room.plan_changed(seg["episode_id"])
    return {"deleted": True}


class OrderBody(BaseModel):
    ids: list[str]


@router.post("/episodes/{episode_id}/segments/order")
async def segments_order(episode_id: str, body: OrderBody, request: Request) -> list[dict]:
    db = request.app.state.db
    _episode_or_404(request, episode_id)
    have = [r["id"] for r in db.all("SELECT id FROM segments WHERE episode_id=? ORDER BY sort, rowid", (episode_id,))]
    if len(set(body.ids)) != len(body.ids) or not set(body.ids) <= set(have):
        raise ApiError(422, "invalid", "Die Reihenfolge enthält unbekannte oder doppelte Abschnitte")
    ids = body.ids + [i for i in have if i not in body.ids]  # nicht genannte bleiben hinten, in alter Reihenfolge
    with db.tx() as c:
        _renumber(c, episode_id, ids)
    await request.app.state.room.plan_changed(episode_id)
    costs = core.segment_costs(db, episode_id)
    return [_out(request, s, costs) for s in core.plan_of(db, episode_id)]


@router.post("/segments/{segment_id}/research", status_code=202)
async def segment_research(segment_id: str, request: Request) -> dict:
    st = request.app.state
    seg = _seg_or_404(request, segment_id)
    if st.researcher.running(segment_id):  # läuft schon: nicht doppelt starten
        return _out(request, seg)
    if not st.cfg.anthropic_key:
        raise ApiError(503, "no_ai", NO_KEY_NOTE)
    if seg["kind"] != "good" and not (seg["title"].strip() or seg["input"].strip()):
        raise ApiError(422, "empty_segment", "Bitte zuerst ein Thema oder Stichworte eintragen")
    st.researcher.start(segment_id)
    await st.room.plan_changed(seg["episode_id"])
    return _out(request, _seg_or_404(request, segment_id))


class TransferBody(BaseModel):
    target_episode_id: str
    mode: Literal["move", "copy"]


@router.post("/segments/{segment_id}/transfer")
async def segment_transfer(segment_id: str, body: TransferBody, request: Request) -> dict:
    """Abschnitt samt Eingabe, Notizen und Dossier in eine andere Vorbereitung verschieben oder kopieren. Die Kopie
    bekommt eine neue ID und hängt hinten an; Verschieben setzt `visited_at` zurück. Das Ziel muss eine andere
    Vorbereitung (`draft`) sein; läuft für den Abschnitt gerade eine Recherche, gibt es 409."""
    st = request.app.state
    db = st.db
    seg = _seg_or_404(request, segment_id)
    target = _episode_or_404(request, body.target_episode_id)
    if target["id"] == seg["episode_id"]:
        raise ApiError(422, "same_preparation", "Das Ziel ist dieselbe Vorbereitung")
    if target["status"] != "draft":
        raise ApiError(422, "not_a_preparation", "Das Ziel ist keine Vorbereitung")
    if seg["status"] == "researching" or st.researcher.running(segment_id):
        raise ApiError(409, "researching", "Für diesen Abschnitt läuft gerade eine Recherche. Bitte warten, bis sie fertig ist")
    now = util.now_ms()
    source_id, new_id = seg["episode_id"], segment_id
    with db.tx() as c:
        cur = c.execute("SELECT status FROM segments WHERE id=?", (segment_id,)).fetchone()
        if cur is None or cur["status"] == "researching":
            raise ApiError(409, "researching", "Für diesen Abschnitt läuft gerade eine Recherche. Bitte warten, bis sie fertig ist")
        top = c.execute("SELECT COALESCE(MAX(sort),-1) AS m FROM segments WHERE episode_id=?", (target["id"],)).fetchone()["m"]
        if body.mode == "copy":
            raw = c.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()
            new_id = util.new_id()
            c.execute("INSERT INTO segments(id,episode_id,sort,kind,title,input,good_type,status,dossier,notes,visited_at,updated_at,error) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
                      (new_id, target["id"], top + 1, raw["kind"], raw["title"], raw["input"], raw["good_type"], raw["status"],
                       raw["dossier"], raw["notes"], now, raw["error"]))
        else:
            c.execute("UPDATE segments SET episode_id=?, sort=?, visited_at=NULL, updated_at=? WHERE id=?",
                      (target["id"], top + 1, now, segment_id))
            ids = [r["id"] for r in c.execute("SELECT id FROM segments WHERE episode_id=? ORDER BY sort, rowid", (source_id,)).fetchall()]
            _renumber(c, source_id, ids)
    if body.mode == "move":
        await st.room.plan_changed(source_id)
    await st.room.plan_changed(target["id"])
    return _out(request, _seg_or_404(request, new_id))
