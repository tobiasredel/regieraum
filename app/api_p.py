"""Teilnehmer-API: Beitritt, ICE, Sounds, Spuren, Chunks (SPEC Abschnitt 7)."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import core, turn, util
from .auth import participant
from .errors import ApiError

MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_CLOCK_POINTS = 2000

router = APIRouter()


# --- Beitritt (öffentlich) ----------------------------------------------------
class JoinBody(BaseModel):
    invite_token: str
    name: str = Field(min_length=1, max_length=60)
    participant_token: str | None = None


def _invite(request: Request, token: str) -> dict:
    inv = request.app.state.db.one("SELECT * FROM invites WHERE token=?", (token,))
    if inv is None:
        raise ApiError(404, "invite_unknown", "Dieser Einladungslink ist unbekannt")
    if inv["revoked_at"]:
        raise ApiError(410, "invite_revoked", "Dieser Einladungslink wurde widerrufen")
    if inv["expires_at"] and inv["expires_at"] <= util.now_ms():
        raise ApiError(410, "invite_expired", "Dieser Einladungslink ist abgelaufen")
    return inv


@router.get("/api/join/{invite_token}")
def join_info(invite_token: str, request: Request) -> dict:
    """Für die Beitrittsseite: ist der Link gültig, wie heißt er?"""
    inv = _invite(request, invite_token)
    used = request.app.state.db.one("SELECT COUNT(*) AS n FROM participants WHERE invite_id=?", (inv["id"],))["n"]
    return {"valid": inv["kind"] == "permanent" or used == 0, "label": inv["label"], "kind": inv["kind"]}


@router.post("/api/join")
def join(body: JoinBody, request: Request) -> dict:
    db = request.app.state.db
    inv = _invite(request, body.invite_token)
    # Wiederkehrender Gast: gleiches Token, gleiche Identität
    if body.participant_token:
        p = core.participant_by_token(db, body.participant_token)
        if p is not None and p["invite_id"] == inv["id"]:
            return {"participant_token": body.participant_token, "participant": core.participant_public(p)}
    name = body.name.strip()
    if not name:
        raise ApiError(422, "invalid", "Bitte einen Namen angeben")
    if inv["kind"] == "single" and db.one("SELECT id FROM participants WHERE invite_id=?", (inv["id"],)):
        raise ApiError(410, "invite_used", "Dieser Einmal-Link wurde bereits benutzt")
    p, token = core.create_participant(db, name, "guest", inv["id"])
    return {"participant_token": token, "participant": core.participant_public(p)}


# --- Angemeldete Teilnehmer -----------------------------------------------------
@router.get("/api/p/me")
def me(p: dict = Depends(participant)) -> dict:
    return core.participant_public(p)


@router.get("/api/p/ice")
async def ice(request: Request, p: dict = Depends(participant)) -> dict:
    return {"iceServers": await turn.ice_servers(request.app.state.cfg)}


@router.get("/api/p/sounds/{sound_id}/audio")
def sound_audio(sound_id: str, request: Request, p: dict = Depends(participant)) -> FileResponse:
    return serve_sound(request, sound_id)


def serve_sound(request: Request, sound_id: str) -> FileResponse:
    s = request.app.state.db.one("SELECT id FROM sounds WHERE id=?", (sound_id,))
    path = core.sound_dir(request.app.state.cfg, sound_id) / "playback.wav"
    if s is None or not path.exists():
        raise ApiError(404, "sound_not_found", "Sound nicht gefunden")
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "private, max-age=86400"})


class TrackBody(BaseModel):
    sample_rate: int = Field(ge=8000, le=192000)
    format: str = "s24le"
    start_server_ms: int
    meta: dict = Field(default_factory=dict)


@router.post("/api/p/tracks")
async def create_track(body: TrackBody, request: Request, p: dict = Depends(participant)) -> dict:
    db = request.app.state.db
    if body.format not in core.BYTES_PER_FRAME:
        raise ApiError(422, "invalid", "Unbekanntes Format. Erlaubt sind s24le, s16le und f32le")
    ep = core.current_episode(db, create=True)
    if ep["status"] == "uploading":
        raise ApiError(409, "uploading", "Der Upload der letzten Aufnahme läuft noch. Neue Spuren sind erst danach möglich")
    tid = util.new_id()
    db.run(
        "INSERT INTO tracks(id,episode_id,participant_id,participant_name,sample_rate,format,start_server_ms,status,meta,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (tid, ep["id"], p["id"], p["name"], body.sample_rate, body.format, body.start_server_ms, "recording",
         util.dumps(body.meta), util.now_ms()),
    )
    await request.app.state.room.send_room()
    return {"track_id": tid}


def _own_track(request: Request, p: dict, track_id: str) -> dict:
    t = request.app.state.db.one("SELECT * FROM tracks WHERE id=?", (track_id,))
    if t is None:
        raise ApiError(404, "track_not_found", "Spur nicht gefunden")
    if t["participant_id"] != p["id"]:
        raise ApiError(403, "forbidden", "Diese Spur gehört einem anderen Teilnehmer")
    return t


def _chunk_path(cfg, t: dict, seq: int) -> Path:
    return cfg.episode_dir(t["episode_id"]) / "tracks" / t["id"] / "chunks" / f"{seq:06d}.pcm"


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _maybe_complete(request: Request, t: dict) -> str:
    db = request.app.state.db
    fresh = db.one("SELECT * FROM tracks WHERE id=?", (t["id"],))
    if fresh["status"] == "finishing" and fresh["total_chunks"] is not None and not core.missing_seqs(db, fresh):
        db.run("UPDATE tracks SET status='complete' WHERE id=?", (t["id"],))
        return "complete"
    return fresh["status"]


@router.put("/api/p/tracks/{track_id}/chunks/{seq}")
async def put_chunk(track_id: str, seq: int, request: Request, p: dict = Depends(participant)) -> dict:
    cfg, db = request.app.state.cfg, request.app.state.db
    t = _own_track(request, p, track_id)
    if t["status"] == "assembled":
        raise ApiError(409, "track_closed", "Diese Spur ist bereits zusammengesetzt")
    if not 0 <= seq < 1_000_000 or (t["total_chunks"] is not None and seq >= t["total_chunks"]):
        raise ApiError(400, "bad_seq", "Ungültige Chunk-Nummer")
    try:
        first_frame = int(request.headers["x-first-frame"])
        frames = int(request.headers["x-frames"])
    except (KeyError, ValueError):
        raise ApiError(400, "bad_headers", "Die Header X-First-Frame und X-Frames fehlen oder sind ungültig") from None
    if first_frame < 0 or frames <= 0:
        raise ApiError(400, "bad_headers", "Die Header X-First-Frame und X-Frames sind ungültig")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_CHUNK_BYTES:
        raise ApiError(413, "too_large", "Chunk ist zu groß (höchstens 8 MB)")
    buf = bytearray()
    async for part in request.stream():
        buf += part
        if len(buf) > MAX_CHUNK_BYTES:
            raise ApiError(413, "too_large", "Chunk ist zu groß (höchstens 8 MB)")
    if len(buf) != frames * core.BYTES_PER_FRAME[t["format"]]:
        raise ApiError(400, "bad_length", "Die Größe des Chunks passt nicht zu X-Frames")
    await anyio.to_thread.run_sync(_write_atomic, _chunk_path(cfg, t, seq), bytes(buf))
    db.run(
        "INSERT INTO chunks(track_id,seq,first_frame,frames,bytes,received_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(track_id,seq) DO UPDATE SET first_frame=excluded.first_frame, frames=excluded.frames, "
        "bytes=excluded.bytes, received_at=excluded.received_at",
        (track_id, seq, first_frame, frames, len(buf), util.now_ms()),
    )
    status = _maybe_complete(request, t)
    # Live-Begleiter (nie blockierend, nie mit Ausnahme): der Upload antwortet unabhängig davon
    await request.app.state.room.companion.on_chunk(t, first_frame, bytes(buf))
    received = db.one("SELECT COUNT(*) AS n FROM chunks WHERE track_id=?", (track_id,))["n"]
    await request.app.state.room.track_progress(track_id)
    if status == "complete":
        await request.app.state.runner.on_change()
    return {"received": received}


class ClockPoint(BaseModel):
    sample_index: int
    server_ms: int


class ClockBody(BaseModel):
    points: list[ClockPoint]


@router.post("/api/p/tracks/{track_id}/clock")
def clock(track_id: str, body: ClockBody, request: Request, p: dict = Depends(participant)) -> dict:
    _own_track(request, p, track_id)
    if len(body.points) > MAX_CLOCK_POINTS:
        raise ApiError(413, "too_many", "Zu viele Uhrenpunkte auf einmal")
    with request.app.state.db.tx() as c:
        c.executemany(
            "INSERT OR REPLACE INTO clock_points(track_id,sample_index,server_ms) VALUES(?,?,?)",
            [(track_id, pt.sample_index, pt.server_ms) for pt in body.points],
        )
    return {"stored": len(body.points)}


class FinishBody(BaseModel):
    total_frames: int = Field(ge=0)
    total_chunks: int = Field(ge=0)


@router.post("/api/p/tracks/{track_id}/finish")
async def finish(track_id: str, body: FinishBody, request: Request, p: dict = Depends(participant)) -> dict:
    db = request.app.state.db
    t = _own_track(request, p, track_id)
    if t["status"] != "assembled":
        db.run("UPDATE tracks SET total_frames=?, total_chunks=?, status='finishing' WHERE id=?",
               (body.total_frames, body.total_chunks, track_id))
        t = db.one("SELECT * FROM tracks WHERE id=?", (track_id,))
        missing = core.missing_seqs(db, t)
        if not missing:
            db.run("UPDATE tracks SET status='complete' WHERE id=?", (track_id,))
    else:
        missing = []
    status = db.one("SELECT status FROM tracks WHERE id=?", (track_id,))["status"]
    await request.app.state.room.track_progress(track_id)
    await request.app.state.runner.on_change()
    return {"complete": status in ("complete", "assembled"), "missing": missing}


@router.get("/api/p/tracks/{track_id}")
def track_status(track_id: str, request: Request, p: dict = Depends(participant)) -> dict:
    db = request.app.state.db
    t = _own_track(request, p, track_id)
    have = [r["seq"] for r in db.all("SELECT seq FROM chunks WHERE track_id=?", (track_id,))]
    return {"status": t["status"], "received": core.received_ranges(have), "missing": core.missing_seqs(db, t),
            "total_chunks": t["total_chunks"], "total_frames": t["total_frames"]}
