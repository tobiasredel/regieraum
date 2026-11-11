"""Manifest für die Pipeline (SPEC Abschnitt 9). Alle Zeiten im Manifest sind Sitzungszeit, außer *_server_ms."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import core, i18n, util
from .config import Config
from .db import DB


def _sound_path(cfg: Config, sound_id: str) -> Path:
    d = core.sound_dir(cfg, sound_id)
    originals = sorted(d.glob("original.*"))
    return originals[0] if originals else d / "playback.wav"


def build(db: DB, cfg: Config, episode_id: str) -> dict:
    ep = core.get_episode(db, episode_id)
    if ep is None or ep["rec_start_server_ms"] is None:
        raise ValueError("Die Folge hat keine Aufnahme")
    start = ep["rec_start_server_ms"]
    events = core.events_of(db, episode_id)
    stop = ep["rec_stop_server_ms"] or max([e["server_ms"] for e in events] + [start])
    settings = core.get_settings(db, cfg)
    edir = cfg.episode_dir(episode_id)

    tracks = []
    for t in db.all("SELECT * FROM tracks WHERE episode_id=? ORDER BY created_at", (episode_id,)):
        chunks = db.all("SELECT seq,first_frame,frames FROM chunks WHERE track_id=? ORDER BY seq", (t["id"],))
        if not chunks:
            continue
        meta = util.loads(t["meta"], {}) or {}
        tracks.append({
            "id": t["id"], "participant_id": t["participant_id"], "participant_name": t["participant_name"],
            "dir": str(edir / "tracks" / t["id"]), "sample_rate": t["sample_rate"], "format": t["format"],
            "start_server_ms": t["start_server_ms"],
            "total_frames": t["total_frames"] if t["total_frames"] is not None else max(c["first_frame"] + c["frames"] for c in chunks),
            "chunks": chunks,
            "clock_points": db.all("SELECT sample_index,server_ms FROM clock_points WHERE track_id=? ORDER BY sample_index", (t["id"],)),
            "meta": {"headphones": meta.get("headphones"), "echo_cancellation": meta.get("echo_cancellation"), **meta},
        })

    sounds_by_id = {s["id"]: s for s in db.all("SELECT * FROM sounds")}
    stops = {e["ref_event_id"]: e for e in events if e["kind"] == "sound.stop"}
    titles = ep["ai"].get("chapter_titles", {})
    segs = core.segments_by_id(db, episode_id)
    fmt = core.get_format(db)
    sounds = []
    for e in events:
        if e["kind"] != "sound.play" or e["sound_id"] not in sounds_by_id:
            continue
        s = sounds_by_id[e["sound_id"]]
        st = stops.get(e["id"])
        chapter = None
        if core.makes_chapter(s, e, segs):
            chapter = titles.get(e["id"]) or core.chapter_default(s, e, segs, fmt)
        sounds.append({
            "event_id": e["id"], "sound_id": s["id"], "path": str(_sound_path(cfg, s["id"])), "at_ms": e["server_ms"] - start,
            "stop_ms": (st["server_ms"] - start) if st else None,
            "fade_ms": (st["payload"].get("fade_ms") if st else None) or s["fade_out_ms"],
            "gain_db": s["gain_db"], "kind": s["kind"], "chapter_title": chapter,
        })
    marker_name = i18n.tr(i18n.ui_lang(db), "Marker")
    # Kapitel-Marker; Merkpunkte (`payload.kind == "note"`) sind nur für den Schnitt und gehen als eigene Liste mit
    markers = [{"at_ms": e["server_ms"] - start, "label": e["payload"].get("label") or marker_name}
               for e in events if e["kind"] == "marker" and not core.is_note(e)]
    notes = [{"at_ms": n["session_ms"], "label": n["label"]} for n in core.notes_of(db, ep)]

    show = settings["show"]
    lang = i18n.show_lang(db)  # Sprache der Inhalte: Show-Sprache für Transkription und Dateikennzeichnung
    title = ep["title"] or (i18n.tr(lang, "Folge {n}", n=ep["number"]) if ep["number"] else i18n.tr(lang, "Neue Folge"))
    cover = cfg.cover_dir / show["cover_key"] if show.get("cover_key") else None
    year = dt.datetime.fromtimestamp(start / 1000, dt.timezone.utc).year
    return {
        "episode_id": episode_id, "language": lang, "work_dir": str(edir / "work"), "out_dir": str(edir / "out"),
        "rec_start_server_ms": start, "rec_stop_server_ms": stop,
        "tracks": tracks, "sounds": sounds, "markers": markers, "notes": notes, "edl": ep["edl"], "audio": settings["audio"],
        "tags": {"title": title, "artist": show["author"], "album": show["title"], "track": ep["number"], "year": year,
                 "cover_path": str(cover) if cover and cover.exists() else None},
    }


def write(db: DB, cfg: Config, episode_id: str) -> Path:
    m = build(db, cfg, episode_id)
    d = Path(m["work_dir"])
    d.mkdir(parents=True, exist_ok=True)
    Path(m["out_dir"]).mkdir(parents=True, exist_ok=True)
    path = d / "manifest.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1))
    tmp.replace(path)
    return path
