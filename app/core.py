"""Fachlogik, die REST, WebSocket und Jobs gemeinsam nutzen."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import util
from .config import Config
from .db import DB, seed_default_plan

CURRENT_STATUSES = ("draft", "recording", "uploading")
BYTES_PER_FRAME = {"s24le": 3, "s16le": 2, "f32le": 4}
SOUND_KINDS = ("intro", "outro", "segment", "sting")
CHAPTER_KINDS = ("intro", "outro", "segment")
DEFAULT_EDL = {"trim_start_ms": 0, "trim_end_ms": None, "cuts": []}

SEGMENT_KINDS = ("blitzlicht", "hauptthema", "good", "frei")
SOUND_SEGMENT_KINDS = ("blitzlicht", "hauptthema", "good")
GOOD_TYPES = ("read", "watch", "listen")

DEFAULT_SHOW = {
    "title": "Mein Podcast",
    "author": "",
    "owner_name": "",
    "owner_email": "",
    "description": "",
    "language": "de",
    "category": "Society & Culture",
    "subcategory": "",
    "explicit": False,
    "link": "",
    "copyright": "",
    "cover_key": None,
}
DEFAULT_AUDIO = {"target_lufs": -16, "true_peak_db": -1.0, "denoise": "normal", "crossgate": True, "mp3_kbps": 160}


# Show-Profil: wer spricht und in welchem Ton (für Vorbereitung, Begleiter, Shownotes und die Oberfläche)
DEFAULT_PROFILE = {"host_name": "", "cohost_name": "", "genre": "", "tone": ""}
# Sendeformat: Die Abschnittsarten haben feste Schlüssel und Verhalten (`blitzlicht` = kurzes Thema, `hauptthema`,
# `good` = Empfehlung mit read/watch/listen, `frei`). Wie sie heißen und woraus eine neue Folge besteht, ist frei.
DEFAULT_FORMAT = {
    "labels": {"blitzlicht": "Kurzthema", "hauptthema": "Hauptthema", "good": "Empfehlung", "frei": "Freier Abschnitt",
               "read": "Lesetipp", "watch": "Sehtipp", "listen": "Hörtipp"},
    "short_minutes": 3,
    "plan": ["blitzlicht", "blitzlicht", "blitzlicht", "hauptthema", "good:read"],
}


def get_profile(db: DB) -> dict:
    return {**DEFAULT_PROFILE, **{k: v for k, v in db.get_setting("profile", {}).items() if k in DEFAULT_PROFILE}}


def get_format(db: DB) -> dict:
    stored = db.get_setting("format", {})
    labels = {**DEFAULT_FORMAT["labels"], **{k: v for k, v in (stored.get("labels") or {}).items()
                                              if k in DEFAULT_FORMAT["labels"] and v}}
    from .db import PLAN_ENTRIES

    plan = [e for e in (stored.get("plan") or []) if e in PLAN_ENTRIES] or list(DEFAULT_FORMAT["plan"])
    return {"labels": labels, "short_minutes": stored.get("short_minutes") or DEFAULT_FORMAT["short_minutes"], "plan": plan}


def kind_label(fmt: dict, kind: str | None, good_type: str | None = None) -> str:
    labels = fmt["labels"]
    if kind == "good":
        return labels.get(good_type or "", labels["good"])
    return labels.get(kind or "frei", kind or "")


def host_first_name(db: DB) -> str:
    """Rufname des Hosts: aus dem Show-Profil, sonst der erste Name des Inhabers, sonst „Host“."""
    name = get_profile(db)["host_name"] or (db.get_setting("show", {}).get("owner_name") or "")
    return (name.split() or ["Host"])[0]


def assist_show(db: DB, cfg: Config) -> dict:
    """Show-Profil für Recherche, Begleiter und Shownotes (siehe assist.prompts.show_block)."""
    show = get_settings(db, cfg)["show"]
    return {"title": show["title"], "description": show["description"], "hosts": show["author"],
            **get_profile(db), "format": get_format(db)}


def get_settings(db: DB, cfg: Config) -> dict:
    show = {**DEFAULT_SHOW, "link": cfg.base_url, **db.get_setting("show", {})}
    audio = {**DEFAULT_AUDIO, **db.get_setting("audio", {})}
    assist = {"enabled": False, **db.get_setting("assist", {})}
    return {"show": show, "audio": audio, "assist": assist, "profile": get_profile(db), "format": get_format(db)}


# --- Teilnehmer und Einladungen -----------------------------------------
def participant_public(p: dict) -> dict:
    return {"id": p["id"], "name": p["name"], "role": p["role"]}


def invite_public(inv: dict, cfg: Config, db: DB | None = None) -> dict:
    out = {k: inv[k] for k in ("id", "token", "label", "kind", "created_at", "expires_at", "revoked_at")}
    out["url"] = f"{cfg.base_url}/join/{inv['token']}"
    out["active"] = invite_active(inv)
    if db is not None:
        out["uses"] = db.one("SELECT COUNT(*) AS n FROM participants WHERE invite_id=?", (inv["id"],))["n"]
    return out


def invite_active(inv: dict) -> bool:
    if inv["revoked_at"]:
        return False
    return not (inv["expires_at"] and inv["expires_at"] <= util.now_ms())


def ensure_permanent_invite(db: DB) -> None:
    if db.one("SELECT id FROM invites WHERE kind='permanent' LIMIT 1") is None:
        create_invite(db, get_profile(db)["cohost_name"] or "Co-Host", "permanent")


def create_invite(db: DB, label: str, kind: str) -> dict:
    now = util.now_ms()
    inv = {
        "id": util.new_id(), "token": util.new_token(), "label": label, "kind": kind,
        "created_at": now, "expires_at": now + 48 * 3600 * 1000 if kind == "single" else None, "revoked_at": None,
    }
    db.run(
        "INSERT INTO invites(id,token,label,kind,created_at,expires_at,revoked_at) VALUES(?,?,?,?,?,?,?)",
        (inv["id"], inv["token"], inv["label"], inv["kind"], inv["created_at"], inv["expires_at"], None),
    )
    return inv


def create_participant(db: DB, name: str, role: str, invite_id: str | None) -> tuple[dict, str]:
    token = util.new_token()
    p = {"id": util.new_id(), "name": name, "role": role, "invite_id": invite_id}
    now = util.now_ms()
    db.run(
        "INSERT INTO participants(id,token_hash,name,role,invite_id,created_at,last_seen_at) VALUES(?,?,?,?,?,?,?)",
        (p["id"], util.sha256(token), name, role, invite_id, now, now),
    )
    return p, token


def participant_by_token(db: DB, token: str | None) -> dict | None:
    if not token:
        return None
    return db.one("SELECT * FROM participants WHERE token_hash=?", (util.sha256(token),))


# --- Sounds ---------------------------------------------------------------
def sound_public(s: dict) -> dict:
    return {
        "id": s["id"], "label": s["label"], "kind": s["kind"], "chapter_title": s["chapter_title"],
        "duration_ms": s["duration_ms"], "gain_db": s["gain_db"], "fade_out_ms": s["fade_out_ms"],
        "hotkey": s["hotkey"], "sort": s["sort"], "segment_kind": s.get("segment_kind"), "url": f"/api/p/sounds/{s['id']}/audio",
    }


def list_sounds(db: DB) -> list[dict]:
    return db.all("SELECT * FROM sounds WHERE archived=0 ORDER BY sort, created_at")


def sound_dir(cfg: Config, sound_id: str) -> Path:
    return cfg.sounds_dir / sound_id


# --- Folgen ---------------------------------------------------------------
def episode_load(row: dict | None) -> dict | None:
    if row is None:
        return None
    ep = dict(row)
    ep["explicit"] = bool(ep["explicit"])
    ep["edl"] = util.loads(ep["edl"], None) or dict(DEFAULT_EDL, cuts=[])
    ep["ai"] = util.loads(ep["ai"], None) or {}
    ep["report"] = util.loads(ep["report"], None)
    ep["legacy"] = util.loads(ep["legacy"], None)
    return ep


def get_episode(db: DB, episode_id: str) -> dict | None:
    return episode_load(db.one("SELECT * FROM episodes WHERE id=?", (episode_id,)))


def current_episode(db: DB, create: bool = False) -> dict | None:
    marks = ",".join("?" * len(CURRENT_STATUSES))
    row = db.one(f"SELECT * FROM episodes WHERE status IN ({marks}) ORDER BY created_at DESC LIMIT 1", CURRENT_STATUSES)
    if row is None and create:
        show = db.get_setting("show", {})
        eid = util.new_id()
        import uuid

        db.run(
            "INSERT INTO episodes(id,status,source,explicit,episode_type,guid,created_at,edl) VALUES(?,?,?,?,?,?,?,?)",
            (eid, "draft", "recorded", int(bool(show.get("explicit"))), "full", str(uuid.uuid4()), util.now_ms(),
             util.dumps(DEFAULT_EDL)),
        )
        with db.tx() as c:
            seed_default_plan(c, eid)
        row = db.one("SELECT * FROM episodes WHERE id=?", (eid,))
    return episode_load(row)


DISCARDABLE = ("uploading", "processing", "review", "failed")


def plan_has_content(db: DB, episode_id: str) -> bool:
    """Hat der Sendeplan dieser Folge schon etwas Eigenes (Titel, Eingabe, Notiz, Dossier)?"""
    return db.one("SELECT 1 AS x FROM segments WHERE episode_id=? AND (title!='' OR input!='' OR notes!='' "
                  "OR dossier IS NOT NULL OR status!='empty') LIMIT 1", (episode_id,)) is not None


def discard_plan_target(db: DB, ep: dict) -> tuple[str, dict | None]:
    """Wohin der Sendeplan einer verworfenen Aufnahme geht. -> (was, andere aktuelle Folge)
    `none`: Die Aufnahme hat keinen Sendeplan mit Inhalt. `move`: Er geht in den (neuen oder leeren) Entwurf.
    `conflict`: Es gibt schon eine aktuelle Folge mit eigenem Plan oder eine laufende Aufnahme; übernehmen geht nicht."""
    cur = current_episode(db)
    other = cur if cur and cur["id"] != ep["id"] else None
    if not plan_has_content(db, ep["id"]):
        return "none", other
    if other is None:
        return "move", None
    if other["status"] != "draft" or plan_has_content(db, other["id"]):
        return "conflict", other
    return "move", other


def discard_recording(db: DB, ep: dict, move_plan: bool) -> str:
    """Aufnahme samt Spuren, Ereignissen, Jobs und Karten löschen. Mit `move_plan` wandert der Sendeplan (Abschnitte,
    Eingaben, Notizen, Dossiers) in den aktuellen Entwurf; gibt es keinen, entsteht einer. Besuchsmarken werden
    zurückgesetzt. Dateien löscht der Aufrufer. -> ID des Entwurfs, mit dem es weitergeht."""
    import uuid

    eid = ep["id"]
    cur = current_episode(db)
    other = cur if cur and cur["id"] != eid else None
    with db.tx() as c:
        target = other["id"] if other else util.new_id()
        if other is None:
            show = db.get_setting("show", {})
            c.execute("INSERT INTO episodes(id,status,source,explicit,episode_type,guid,created_at,edl) VALUES(?,?,?,?,?,?,?,?)",
                      (target, "draft", "recorded", int(bool(show.get("explicit"))), "full", str(uuid.uuid4()),
                       util.now_ms(), util.dumps(DEFAULT_EDL)))
        if move_plan:
            c.execute("DELETE FROM segments WHERE episode_id=?", (target,))
            c.execute("UPDATE segments SET episode_id=?, visited_at=NULL, updated_at=? WHERE episode_id=?",
                      (target, util.now_ms(), eid))
        elif other is None:
            seed_default_plan(c, target)
        c.execute("DELETE FROM clock_points WHERE track_id IN (SELECT id FROM tracks WHERE episode_id=?)", (eid,))
        c.execute("DELETE FROM chunks WHERE track_id IN (SELECT id FROM tracks WHERE episode_id=?)", (eid,))
        for table in ("tracks", "events", "jobs", "segments", "cards"):
            c.execute(f"DELETE FROM {table} WHERE episode_id=?", (eid,))
        c.execute("DELETE FROM episodes WHERE id=?", (eid,))
    return target


def update_episode(db: DB, episode_id: str, **fields: Any) -> None:
    if not fields:
        return
    for k in ("edl", "ai", "report", "legacy"):
        if k in fields:
            fields[k] = util.dumps(fields[k])
    if "explicit" in fields:
        fields["explicit"] = int(bool(fields["explicit"]))
    cols = ", ".join(f"{k}=?" for k in fields)
    db.run(f"UPDATE episodes SET {cols} WHERE id=?", (*fields.values(), episode_id))


def events_of(db: DB, episode_id: str) -> list[dict]:
    rows = db.all("SELECT * FROM events WHERE episode_id=? ORDER BY server_ms, rowid", (episode_id,))
    for r in rows:
        r["payload"] = util.loads(r["payload"], {})
    return rows


def add_event(db: DB, episode_id: str, kind: str, server_ms: int, sound_id: str | None = None,
              ref_event_id: str | None = None, payload: dict | None = None, event_id: str | None = None) -> str:
    eid = event_id or util.new_id()
    db.run(
        "INSERT INTO events(id,episode_id,kind,server_ms,sound_id,ref_event_id,payload) VALUES(?,?,?,?,?,?,?)",
        (eid, episode_id, kind, server_ms, sound_id, ref_event_id, util.dumps(payload or {})),
    )
    return eid


def pause_intervals(events: list[dict], close_at: int | None = None) -> list[tuple[int, int | None]]:
    """Pausen als (von, bis) in Serverzeit; offene Pause endet bei close_at oder bleibt None."""
    out: list[tuple[int, int | None]] = []
    open_from: int | None = None
    for e in events:
        if e["kind"] == "rec.pause" and open_from is None:
            open_from = e["server_ms"]
        elif e["kind"] == "rec.resume" and open_from is not None:
            out.append((open_from, e["server_ms"]))
            open_from = None
    if open_from is not None:
        out.append((open_from, close_at))
    return out


def normalize_edl(edl: dict | None, extra_cuts: list[dict] | None = None) -> dict:
    edl = edl or {}
    ts = max(0, int(edl.get("trim_start_ms") or 0))
    te_raw = edl.get("trim_end_ms")
    te = int(te_raw) if te_raw is not None else None
    if te is not None and te <= ts:
        te = None
    cuts = []
    for c in list(edl.get("cuts") or []) + list(extra_cuts or []):
        a, b = int(c["from_ms"]), int(c["to_ms"])
        if b > a and a >= 0:
            cuts.append([a, b])
    cuts.sort()
    merged: list[list[int]] = []
    for a, b in cuts:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return {"trim_start_ms": ts, "trim_end_ms": te, "cuts": [{"from_ms": a, "to_ms": b} for a, b in merged]}


def session_to_episode_ms(ms: int, edl: dict) -> int:
    """Sitzungszeit -> Folgenzeit; Zeitpunkte in Schnitten oder Rändern rücken an die Kante."""
    ts = edl.get("trim_start_ms") or 0
    te = edl.get("trim_end_ms")
    ms = max(ms, ts)
    if te is not None:
        ms = min(ms, te)
    removed = 0
    for c in edl.get("cuts", []):
        c0 = max(c["from_ms"], ts)
        c1 = min(c["to_ms"], te) if te is not None else c["to_ms"]
        if c1 <= c0:
            continue
        if ms >= c1:
            removed += c1 - c0
        elif ms > c0:
            ms = c0
    return max(0, ms - ts - removed)


def edited_duration_ms(ep: dict) -> int | None:
    if ep["rec_start_server_ms"] is None or ep["rec_stop_server_ms"] is None:
        return None
    edl = ep["edl"]
    total = ep["rec_stop_server_ms"] - ep["rec_start_server_ms"]
    return session_to_episode_ms(total if edl.get("trim_end_ms") is None else edl["trim_end_ms"], edl) if total > 0 else 0


def chapters_preview(db: DB, ep: dict) -> list[dict]:
    """Kapitelvorschau aus Jingle-Klicks und Markern, Zeiten in Folgenzeit (nach dem Schnitt)."""
    start = ep["rec_start_server_ms"]
    if start is None:
        return []
    titles = (ep.get("ai") or {}).get("chapter_titles", {})
    sounds = {s["id"]: s for s in db.all("SELECT * FROM sounds")}
    segs = segments_by_id(db, ep["id"])
    fmt = get_format(db)
    items = []
    for e in events_of(db, ep["id"]):
        if e["kind"] == "sound.play" and e["sound_id"] in sounds and makes_chapter(sounds[e["sound_id"]], e, segs):
            s = sounds[e["sound_id"]]
            default = chapter_default(s, e, segs, fmt)
            items.append((e, s["kind"], default, e["sound_id"]))
        elif e["kind"] == "marker":
            items.append((e, "marker", e["payload"].get("label") or "Marker", None))
    out, seen = [], set()
    for e, kind, default, sound_id in items:
        session_ms = e["server_ms"] - start
        t = session_to_episode_ms(session_ms, ep["edl"])
        if t in seen:
            continue
        seen.add(t)
        out.append({"event_id": e["id"], "kind": kind, "sound_id": sound_id, "session_ms": session_ms,
                    "start_ms": t, "title": titles.get(e["id"]) or default})
    out.sort(key=lambda c: c["start_ms"])
    return out


def makes_chapter(sound: dict, event: dict, segs: dict[str, dict]) -> bool:
    """Intro, Outro und Abschnitts-Sounds setzen immer eine Kapitelmarke. Ein Zwischenton (`sting`) nur dann, wenn er
    den Sendeplan auf einen Abschnitt mit Titel weitergeschaltet hat (so bekommt jedes Blitzlicht sein Kapitel)."""
    if sound["kind"] in CHAPTER_KINDS:
        return True
    seg = segs.get((event.get("payload") or {}).get("segment_id") or "")
    return bool(seg and (seg.get("title") or "").strip())


def chapter_default(sound: dict, event: dict, segs: dict[str, dict], fmt: dict | None = None) -> str:
    """Kapitelname eines Jingle-Klicks ohne Host-Umbenennung: Intro/Outro fest, bei einem Sound mit
    Sendeplan-Abschnitt dessen Titel, sonst Kapiteltitel oder Bezeichnung des Sounds."""
    if sound["kind"] in ("intro", "outro"):
        return {"intro": "Intro", "outro": "Outro"}[sound["kind"]]
    seg = segs.get((event.get("payload") or {}).get("segment_id") or "")
    return (segment_chapter_title(seg, fmt) if seg else None) or sound["chapter_title"] or sound["label"]


def tracks_of(db: DB, episode_id: str) -> list[dict]:
    rows = db.all("SELECT * FROM tracks WHERE episode_id=? ORDER BY created_at", (episode_id,))
    for r in rows:
        r["meta"] = util.loads(r["meta"], {})
        r.pop("upload_token_hash", None)
    return rows


def track_progress(db: DB, track: dict) -> dict:
    row = db.one("SELECT COALESCE(SUM(frames),0) AS f, COUNT(*) AS n, COALESCE(MAX(first_frame+frames),0) AS end "
                 "FROM chunks WHERE track_id=?", (track["id"],))
    return {"uploaded_frames": row["f"], "chunks": row["n"], "end_frame": row["end"]}


def received_ranges(seqs: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for s in sorted(seqs):
        if out and s == out[-1][1] + 1:
            out[-1][1] = s
        else:
            out.append([s, s])
    return out


def missing_seqs(db: DB, track: dict) -> list[int]:
    have = {r["seq"] for r in db.all("SELECT seq FROM chunks WHERE track_id=?", (track["id"],))}
    if track["total_chunks"] is not None:
        top = track["total_chunks"]
    else:
        top = (max(have) + 1) if have else 0
    return [s for s in range(top) if s not in have]


# --- Sendeplan (SPEC Abschnitt 14) ----------------------------------------
def segment_load(row: dict) -> dict:
    seg = dict(row)
    seg["dossier"] = util.loads(seg.get("dossier"), None)
    return seg


def plan_of(db: DB, episode_id: str) -> list[dict]:
    return [segment_load(r) for r in db.all("SELECT * FROM segments WHERE episode_id=? ORDER BY sort, rowid", (episode_id,))]


def segments_by_id(db: DB, episode_id: str) -> dict[str, dict]:
    return {r["id"]: r for r in db.all("SELECT id,kind,title,good_type FROM segments WHERE episode_id=?", (episode_id,))}


def segment_chapter_title(seg: dict, fmt: dict | None = None) -> str | None:
    """„<Name der Art>: <Titel>“ bei kurzen Themen und Empfehlungen, sonst nur der Titel; ohne Titel None.
    Die Namen kommen aus dem Sendeformat (`get_format`)."""
    title = (seg.get("title") or "").strip()
    if not title:
        return None
    fmt = fmt or DEFAULT_FORMAT
    if seg["kind"] == "blitzlicht":
        return f"{kind_label(fmt, 'blitzlicht')}: {title}"
    if seg["kind"] == "good":
        return f"{kind_label(fmt, 'good', seg.get('good_type') or 'read')}: {title}"
    return title


def next_unvisited_segment(db: DB, episode_id: str, kind: str) -> dict | None:
    return db.one("SELECT * FROM segments WHERE episode_id=? AND kind=? AND visited_at IS NULL ORDER BY sort, rowid LIMIT 1",
                  (episode_id, kind))


def planned_chapter_events(db: DB, episode_id: str) -> set[str]:
    """Jingle-Ereignisse, deren Kapitelname vom Titel eines Sendeplan-Abschnitts kommt."""
    segs = segments_by_id(db, episode_id)
    sounds = {s["id"]: s for s in db.all("SELECT id,kind FROM sounds")}
    return {e["id"] for e in events_of(db, episode_id)
            if e["kind"] == "sound.play" and sounds.get(e["sound_id"], {}).get("kind") in ("segment", "sting")
            and (segs.get((e["payload"] or {}).get("segment_id") or "") or {}).get("title")}
