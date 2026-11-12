"""Fachlogik, die REST, WebSocket und Jobs gemeinsam nutzen."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import i18n, util
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
DEFAULT_ARCHIVE = {"after_days": 14}  # Rohdaten ins Archiv, so viele Tage nach dem Veröffentlichen (0 = sofort)
DEFAULT_AUDIO = {"target_lufs": -16, "true_peak_db": -1.0, "denoise": "normal", "crossgate": True, "tone": True, "mp3_kbps": 160}


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


def default_labels(lang: str) -> dict:
    """Standardnamen der Abschnittsarten in der Sprache `lang` (Deutsch ist die Quelle)."""
    return {k: i18n.tr(lang, v) for k, v in DEFAULT_FORMAT["labels"].items()}


def get_format(db: DB, lang: str | None = None) -> dict:
    """Sendeformat. Namen, die nicht gespeichert sind, stehen in der Sprache der Oberfläche (`lang` überschreibt das,
    z. B. für Prompts in der Sprache der Show); gespeicherte Namen bleiben unangetastet."""
    stored = db.get_setting("format", {})
    defaults = default_labels(lang or i18n.ui_lang(db))
    labels = {**defaults, **{k: v for k, v in (stored.get("labels") or {}).items() if k in defaults and v}}
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
    return (name.split() or [i18n.tr(i18n.ui_lang(db), "Host")])[0]


def assist_show(db: DB, cfg: Config) -> dict:
    """Show-Profil für Recherche, Begleiter und Shownotes (siehe assist.prompts.show_block). Inhalte folgen der Sprache der
    Show (`language`, zweibuchstabig), nicht der der Oberfläche; auch die Standardnamen der Abschnittsarten."""
    show = get_settings(db, cfg)["show"]
    lang = i18n.content_lang(show.get("language"))
    return {"title": show["title"], "description": show["description"], "hosts": show["author"], "language": lang,
            **get_profile(db), "format": get_format(db, lang)}


def get_settings(db: DB, cfg: Config) -> dict:
    show = {**DEFAULT_SHOW, "title": i18n.tr(i18n.ui_lang(db), DEFAULT_SHOW["title"]), "link": cfg.base_url,
            **db.get_setting("show", {})}
    audio = {**DEFAULT_AUDIO, **db.get_setting("audio", {})}
    assist = {"enabled": False, **db.get_setting("assist", {})}
    archive = {**DEFAULT_ARCHIVE, **(db.get_setting("archive", {}) or {}), "available": cfg.archive_dir is not None}
    return {"show": show, "audio": audio, "assist": assist, "profile": get_profile(db), "format": get_format(db),
            "ui": {"language": i18n.ui_lang(db)}, "archive": archive, "site": get_site(db), "stats": get_stats(db),
            "numbering": get_numbering(db)}


# --- Öffentliche Seite und Download-Zählung (SPEC Abschnitte 10 und 15) ---------------------------------
STATS_MODES = ("off", "app", "prefix")
MAX_PLATFORMS = 6


def get_site(db: DB) -> dict:
    """Öffentliche Seite der Show: `enabled` (Standard an) und `platforms` ([{name, url}], höchstens 6)."""
    stored = db.get_setting("site", {}) or {}
    return {"enabled": bool(stored.get("enabled", True)), "platforms": clean_platforms(stored.get("platforms"))}


def clean_platforms(items) -> list[dict]:
    """Nur Einträge mit Namen und http(s)-Adresse, höchstens `MAX_PLATFORMS`."""
    out = []
    for it in items or []:
        name = str((it or {}).get("name") or "").strip()[:60]
        url = str((it or {}).get("url") or "").strip()
        if name and re.fullmatch(r"https?://[^\s<>\"']+", url, re.I):
            out.append({"name": name, "url": url})
    return out[:MAX_PLATFORMS]


def get_stats(db: DB) -> dict:
    """Download-Zählung: `mode` (`off`, `app`, `prefix`) und `prefix` (Adresse des Zähldienstes für `prefix`)."""
    stored = db.get_setting("stats", {}) or {}
    mode = stored.get("mode") if stored.get("mode") in STATS_MODES else "off"
    return {"mode": mode, "prefix": str(stored.get("prefix") or "")}


# --- Nummerierung (SPEC Abschnitt 10, Nummerierung) ------------------------------------------------------
SEASON_MODES = ("keep", "year", "none")
PREFIX_FIELDS = ("number", "season")
_PREFIX_FIELD = re.compile(r"\{([^{}]*)\}")


def get_numbering(db: DB) -> dict:
    """Nummerierung: `season` (`keep` = von Hand, `year` = Jahr der Veröffentlichung, `none` = keine Staffeln) und
    `prefix` (Vorlage vor dem Titel im Feed, z. B. `#{number} `; leer = aus)."""
    stored = db.get_setting("numbering", {}) or {}
    season = stored.get("season") if stored.get("season") in SEASON_MODES else "keep"
    return {"season": season, "prefix": str(stored.get("prefix") or "")}


def prefix_ok(prefix: str) -> bool:
    """Nur `{number}` und `{season}` als Platzhalter, keine einzelnen Klammern."""
    if any(f not in PREFIX_FIELDS for f in _PREFIX_FIELD.findall(prefix)):
        return False
    return "{" not in _PREFIX_FIELD.sub("", prefix) and "}" not in _PREFIX_FIELD.sub("", prefix)


def numbered_title(title: str, number: int | None, season: int | None, prefix: str) -> str:
    """Titel mit Präfix (`#{number} ` -> `#12 Titel`). Ohne Nummer oder ohne Präfix bleibt der Titel, wie er ist;
    braucht das Präfix eine Staffel, die fehlt, ebenso."""
    if not prefix or not number or ("{season}" in prefix and not season):
        return title
    return prefix.replace("{number}", str(number)).replace("{season}", str(season or "")) + title


def season_for(ms: int | None, mode: str, current: int | None = None) -> int | None:
    """Staffel nach Modus: Jahr (Ortszeit des Servers) aus `ms`, keine, oder unverändert."""
    if mode == "year" and ms:
        import datetime as dt
        return dt.datetime.fromtimestamp(ms / 1000).year
    if mode == "none":
        return None
    return current


def next_number(db: DB) -> int:
    """Nächste freie Folgennummer: eins mehr als die höchste vergebene (Vorbereitungen haben keine Nummer)."""
    row = db.one("SELECT MAX(number) AS n FROM episodes WHERE status!='draft'")
    return int((row or {}).get("n") or 0) + 1


def _episode_order_ms(e: dict) -> int:
    return int(e.get("published_at") or e.get("publish_at") or e.get("rec_start_server_ms") or e.get("created_at") or 0)


def renumber_plan(db: DB, numbering: dict | None = None) -> list[dict]:
    """Alle Folgen außer Vorbereitungen nach Datum (Veröffentlichung, sonst Planung, Aufnahme, Anlage) ab 1
    durchnummerieren; die Staffel folgt dem Modus. -> nur die Folgen, die sich ändern:
    [{id, title, date_ms, old: {number, season}, new: {number, season}}]."""
    numbering = numbering or get_numbering(db)
    eps = db.all("SELECT id, title, number, season, status, published_at, publish_at, rec_start_server_ms, created_at "
                 "FROM episodes WHERE status!='draft'")
    eps.sort(key=lambda e: (_episode_order_ms(e), e["created_at"] or 0, e["id"]))
    out = []
    for i, e in enumerate(eps, start=1):
        when = _episode_order_ms(e)
        new = {"number": i, "season": season_for(when, numbering["season"], e["season"])}
        old = {"number": e["number"], "season": e["season"]}
        if new != old:
            out.append({"id": e["id"], "title": e["title"] or "", "date_ms": when, "old": old, "new": new})
    return out


def apply_renumber(db: DB, plan: list[dict]) -> None:
    with db.tx() as c:
        for p in plan:
            c.execute("UPDATE episodes SET number=?, season=? WHERE id=?", (p["new"]["number"], p["new"]["season"], p["id"]))


def show_link_is_custom(db: DB, cfg: Config) -> bool:
    """Hat der Nutzer einen eigenen `link` der Show gespeichert? Die Standardadresse der App (`PS_BASE_URL`) zählt nicht."""
    link = str((db.get_setting("show", {}) or {}).get("link") or "").strip()
    return bool(link) and link.rstrip("/") != cfg.base_url.rstrip("/")


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
        create_invite(db, get_profile(db)["cohost_name"] or i18n.tr(i18n.ui_lang(db), "Co-Host"), "permanent")


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
    ep["links"] = util.loads(ep.get("links"), None) or []
    return ep


def get_episode(db: DB, episode_id: str) -> dict | None:
    return episode_load(db.one("SELECT * FROM episodes WHERE id=?", (episode_id,)))


# --- Vorbereitungen und die Folge des Studios ----------------------------------------------------------
# Eine Vorbereitung ist eine Folge im Zustand `draft` mit Sendeplan und Arbeitstitel (`label`); davon kann es beliebig
# viele geben. Das Studio nimmt in die gewählte Vorbereitung auf (Einstellung `studio.episode_id`) oder, mit
# `studio.episode_id = None`, ohne Vorbereitung in eine neue, leere Folge. Ohne Wahl gilt die neueste Vorbereitung.


def new_draft(db: DB, *, plan: bool = True, label: str = "") -> dict:
    """Neue Vorbereitung: Folge im Zustand `draft`, mit Standardplan oder leer."""
    import uuid

    show = db.get_setting("show", {})
    eid = util.new_id()
    with db.tx() as c:
        c.execute("INSERT INTO episodes(id,status,source,explicit,episode_type,guid,created_at,edl,label) VALUES(?,?,?,?,?,?,?,?,?)",
                  (eid, "draft", "recorded", int(bool(show.get("explicit"))), "full", str(uuid.uuid4()), util.now_ms(),
                   util.dumps(DEFAULT_EDL), label))
        if plan:
            seed_default_plan(c, eid)
    return episode_load(db.one("SELECT * FROM episodes WHERE id=?", (eid,)))


def preparations(db: DB) -> list[dict]:
    """Alle Vorbereitungen, neueste zuerst, mit Zahl der Abschnitte und fertigen Dossiers."""
    return db.all(
        "SELECT e.id, e.label, e.title, e.number, e.created_at, "
        "(SELECT COUNT(*) FROM segments s WHERE s.episode_id=e.id) AS segments, "
        "(SELECT COUNT(*) FROM segments s WHERE s.episode_id=e.id AND s.status='ready') AS ready "
        "FROM episodes e WHERE e.status='draft' ORDER BY e.created_at DESC, e.rowid DESC")


def studio_choice(db: DB) -> tuple[bool, str | None]:
    """Wahl im Studio: (gewählt, Folge). `(True, None)` heißt ausdrücklich ohne Vorbereitung."""
    sel = db.get_setting("studio", {}) or {}
    if "episode_id" not in sel:
        return False, None
    return True, sel["episode_id"]


def set_studio_choice(db: DB, episode_id: str | None, chosen: bool = True) -> None:
    db.set_setting("studio", {"episode_id": episode_id} if chosen else {})


def current_episode(db: DB, create: bool = False) -> dict | None:
    """Die Folge des Studios: eine laufende Aufnahme oder ein laufender Upload; sonst die gewählte Vorbereitung,
    ohne Wahl die neueste. Mit `create` entsteht bei Bedarf eine Folge (ohne Vorbereitung leer, sonst mit
    Standardplan)."""
    marks = ",".join("?" * len(CURRENT_STATUSES[1:]))
    row = db.one(f"SELECT * FROM episodes WHERE status IN ({marks}) ORDER BY created_at DESC LIMIT 1", CURRENT_STATUSES[1:])
    if row is not None:
        return episode_load(row)
    chosen, eid = studio_choice(db)
    if chosen and eid is None:
        if not create:
            return None
        ep = new_draft(db, plan=False)
        set_studio_choice(db, ep["id"])
        return ep
    if chosen:
        row = db.one("SELECT * FROM episodes WHERE id=? AND status='draft'", (eid,))
    if row is None:
        row = db.one("SELECT * FROM episodes WHERE status='draft' ORDER BY created_at DESC, rowid DESC LIMIT 1")
    if row is None and create:
        return new_draft(db)
    return episode_load(row)


DISCARDABLE = ("uploading", "processing", "review", "failed")


def plan_has_content(db: DB, episode_id: str) -> bool:
    """Hat der Sendeplan dieser Folge schon etwas Eigenes (Titel, Eingabe, Notiz, Dossier)?"""
    return db.one("SELECT 1 AS x FROM segments WHERE episode_id=? AND (title!='' OR input!='' OR notes!='' "
                  "OR dossier IS NOT NULL OR status!='empty') LIMIT 1", (episode_id,)) is not None


def discard_recording(db: DB, ep: dict) -> str:
    """Aufnahme verwerfen: Spuren, Ereignisse, Jobs, Karten und alles Abgeleitete (Schnitt, Angaben, Bericht) weg,
    die Folge wird wieder zur Vorbereitung (`draft`) mit ihrem Sendeplan samt Dossiers und Notizen; Besuchsmarken
    werden zurückgesetzt. Das Studio wählt sie für die nächste Aufnahme. Dateien löscht der Aufrufer. -> ID der Folge."""
    eid = ep["id"]
    with db.tx() as c:
        c.execute("DELETE FROM clock_points WHERE track_id IN (SELECT id FROM tracks WHERE episode_id=?)", (eid,))
        c.execute("DELETE FROM chunks WHERE track_id IN (SELECT id FROM tracks WHERE episode_id=?)", (eid,))
        for table in ("tracks", "events", "jobs", "cards"):
            c.execute(f"DELETE FROM {table} WHERE episode_id=?", (eid,))
        c.execute("UPDATE segments SET visited_at=NULL, updated_at=? WHERE episode_id=?", (util.now_ms(), eid))
        c.execute("UPDATE episodes SET status='draft', rec_start_server_ms=NULL, rec_stop_server_ms=NULL, duration_ms=NULL, "
                  "publish_at=NULL, published_at=NULL, audio_key=NULL, audio_bytes=NULL, audio_mime=NULL, edl=?, ai=NULL, "
                  "report=NULL, title=NULL, summary=NULL, description_html=NULL, archived_at=NULL, archive_path=NULL, "
                  "number=NULL, season=NULL "
                  "WHERE id=?", (util.dumps(DEFAULT_EDL), eid))
    set_studio_choice(db, eid)
    return eid


def update_episode(db: DB, episode_id: str, **fields: Any) -> None:
    if not fields:
        return
    for k in ("edl", "ai", "report", "legacy", "links"):
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


def is_note(event: dict) -> bool:
    """Ein Marker-Ereignis mit `payload.kind == "note"` ist ein Merkpunkt für den Schnitt; alles andere (auch alte
    Ereignisse ohne `kind`) setzt ein Kapitel."""
    return event["kind"] == "marker" and (event.get("payload") or {}).get("kind") == "note"


def notes_of(db: DB, ep: dict) -> list[dict]:
    """Merkpunkte der Aufnahme: [{event_id, session_ms, label}] in Sitzungszeit, aufsteigend. Sie sind nur für den
    Schnitt da und kommen weder in Kapitel noch in den Feed."""
    start = ep["rec_start_server_ms"]
    if start is None:
        return []
    default = i18n.tr(i18n.ui_lang(db), "Merkpunkt")
    return [{"event_id": e["id"], "session_ms": e["server_ms"] - start,
             "label": ((e["payload"] or {}).get("label") or "").strip() or default}
            for e in events_of(db, ep["id"]) if is_note(e)]


def chapters_preview(db: DB, ep: dict) -> list[dict]:
    """Kapitelvorschau aus Jingle-Klicks und Kapitel-Markern, Zeiten in Folgenzeit (nach dem Schnitt)."""
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
        elif e["kind"] == "marker" and not is_note(e):
            items.append((e, "marker", e["payload"].get("label") or i18n.tr(i18n.ui_lang(db), "Marker"), None))
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


# --- Kosten der KI-Vorgänge --------------------------------------------------------------------------------
COST_KINDS = ("research", "companion", "shownotes")


def add_cost(db: DB, episode_id: str | None, kind: str, usd: float, segment_id: str | None = None) -> None:
    """Kosten eines Vorgangs in US-Dollar festhalten (Recherche, Live-Begleiter, Shownotes). Null und Unsinn zählen nicht."""
    try:
        usd = float(usd)
    except (TypeError, ValueError):
        return
    if kind not in COST_KINDS or not usd > 0 or usd == float("inf"):
        return
    db.run("INSERT INTO costs(id,episode_id,segment_id,kind,usd,created_at) VALUES(?,?,?,?,?,?)",
           (util.new_id(), episode_id, segment_id, kind, round(usd, 4), util.now_ms()))


def _sum_costs(rows: list[dict]) -> dict:
    by_kind = {k: 0.0 for k in COST_KINDS}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0.0) + r["usd"]
    return {"total": round(sum(by_kind.values()), 4), "by_kind": {k: round(v, 4) for k, v in by_kind.items()}}


def costs_of(db: DB, episode_id: str) -> dict:
    """Kosten einer Folge: {total, by_kind: {research, companion, shownotes}} in US-Dollar."""
    return _sum_costs(db.all("SELECT kind, usd FROM costs WHERE episode_id=?", (episode_id,)))


def segment_costs(db: DB, episode_id: str) -> dict[str, float]:
    """Recherchekosten je Abschnitt (US-Dollar)."""
    out: dict[str, float] = {}
    for r in db.all("SELECT segment_id, SUM(usd) AS usd FROM costs WHERE episode_id=? AND kind='research' AND segment_id IS NOT NULL "
                    "GROUP BY segment_id", (episode_id,)):
        out[r["segment_id"]] = round(r["usd"], 4)
    return out


def month_costs(db: DB, now_ms: int | None = None) -> dict:
    """Kosten im laufenden und im Vormonat (UTC): {current: {month, total, by_kind}, previous: {...}}."""
    import datetime as dt

    now = dt.datetime.fromtimestamp((now_ms if now_ms is not None else util.now_ms()) / 1000, dt.timezone.utc)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev = (first - dt.timedelta(days=1)).replace(day=1)
    out = {}
    for name, a, b in (("current", first, None), ("previous", prev, first)):
        lo = int(a.timestamp() * 1000)
        hi = int(b.timestamp() * 1000) if b else 2 ** 62
        rows = db.all("SELECT kind, usd FROM costs WHERE created_at>=? AND created_at<?", (lo, hi))
        out[name] = {"month": a.strftime("%Y-%m"), **_sum_costs(rows)}
    return out
