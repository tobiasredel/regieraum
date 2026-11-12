"""SQLite (WAL), Schema aus SPEC Abschnitt 5, Versionierung über PRAGMA user_version."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import util

MIGRATIONS: list[str] = [
    # Version 1
    """
    CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE invites(
      id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, label TEXT NOT NULL, kind TEXT NOT NULL,
      created_at INTEGER NOT NULL, expires_at INTEGER, revoked_at INTEGER);
    CREATE TABLE participants(
      id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL,
      invite_id TEXT, created_at INTEGER NOT NULL, last_seen_at INTEGER);
    CREATE TABLE sounds(
      id TEXT PRIMARY KEY, label TEXT NOT NULL, kind TEXT NOT NULL, chapter_title TEXT, ext TEXT NOT NULL,
      duration_ms INTEGER NOT NULL DEFAULT 0, gain_db REAL NOT NULL DEFAULT 0, fade_out_ms INTEGER NOT NULL DEFAULT 400,
      sort INTEGER NOT NULL DEFAULT 0, hotkey TEXT, archived INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
    CREATE TABLE episodes(
      id TEXT PRIMARY KEY, status TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'recorded', number INTEGER, season INTEGER,
      title TEXT, summary TEXT, description_html TEXT, explicit INTEGER NOT NULL DEFAULT 0,
      episode_type TEXT NOT NULL DEFAULT 'full', guid TEXT UNIQUE NOT NULL, created_at INTEGER NOT NULL,
      rec_start_server_ms INTEGER, rec_stop_server_ms INTEGER, duration_ms INTEGER, publish_at INTEGER, published_at INTEGER,
      audio_key TEXT, audio_bytes INTEGER, audio_mime TEXT, edl TEXT, ai TEXT, report TEXT, legacy TEXT);
    CREATE TABLE tracks(
      id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, participant_id TEXT NOT NULL, participant_name TEXT NOT NULL,
      upload_token_hash TEXT, sample_rate INTEGER NOT NULL, format TEXT NOT NULL, start_server_ms INTEGER NOT NULL,
      total_frames INTEGER, total_chunks INTEGER, status TEXT NOT NULL, meta TEXT, created_at INTEGER NOT NULL);
    CREATE INDEX tracks_episode ON tracks(episode_id);
    CREATE TABLE chunks(
      track_id TEXT NOT NULL, seq INTEGER NOT NULL, first_frame INTEGER NOT NULL, frames INTEGER NOT NULL,
      bytes INTEGER NOT NULL, received_at INTEGER NOT NULL, PRIMARY KEY(track_id, seq));
    CREATE TABLE clock_points(
      track_id TEXT NOT NULL, sample_index INTEGER NOT NULL, server_ms INTEGER NOT NULL, PRIMARY KEY(track_id, sample_index));
    CREATE TABLE events(
      id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, kind TEXT NOT NULL, server_ms INTEGER NOT NULL,
      sound_id TEXT, ref_event_id TEXT, payload TEXT);
    CREATE INDEX events_episode ON events(episode_id, server_ms);
    CREATE TABLE jobs(
      id TEXT PRIMARY KEY, episode_id TEXT, kind TEXT NOT NULL, status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
      message TEXT, log TEXT, created_at INTEGER NOT NULL, started_at INTEGER, finished_at INTEGER, payload TEXT);
    CREATE INDEX jobs_status ON jobs(status, created_at);
    """,
    # Version 2: Sendeplan, Karten des Live-Begleiters, Sendeplan-Art der Sounds
    """
    CREATE TABLE segments(
      id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL,
      title TEXT NOT NULL DEFAULT '', input TEXT NOT NULL DEFAULT '', good_type TEXT, status TEXT NOT NULL DEFAULT 'empty',
      dossier TEXT, notes TEXT NOT NULL DEFAULT '', visited_at INTEGER, updated_at INTEGER NOT NULL, error TEXT);
    CREATE INDEX segments_episode ON segments(episode_id, sort);
    CREATE TABLE cards(
      id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, server_ms INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
      detail TEXT, basis TEXT, quelle_url TEXT, segment_id TEXT, pinned INTEGER NOT NULL DEFAULT 0,
      dismissed INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX cards_episode ON cards(episode_id, server_ms);
    ALTER TABLE sounds ADD COLUMN segment_kind TEXT;
    """,
    # Version 3: mehrere Vorbereitungen nebeneinander; jede ist eine Folge im Zustand draft mit Arbeitstitel
    """
    ALTER TABLE episodes ADD COLUMN label TEXT NOT NULL DEFAULT '';
    """,
    # Version 4: Kosten der KI-Vorgänge, Rohdaten im Archiv
    """
    CREATE TABLE costs(
      id TEXT PRIMARY KEY, episode_id TEXT, segment_id TEXT, kind TEXT NOT NULL, usd REAL NOT NULL, created_at INTEGER NOT NULL);
    CREATE INDEX costs_episode ON costs(episode_id);
    CREATE INDEX costs_created ON costs(created_at);
    ALTER TABLE episodes ADD COLUMN archived_at INTEGER;
    ALTER TABLE episodes ADD COLUMN archive_path TEXT;
    """,
    # Version 5: Downloads je Tag (datensparsam: kein Klartext-Besucher, nur ein Tages-Hash)
    """
    CREATE TABLE downloads(
      day TEXT NOT NULL, episode_id TEXT NOT NULL, visitor TEXT NOT NULL, PRIMARY KEY(day, episode_id, visitor));
    CREATE INDEX downloads_episode ON downloads(episode_id, day);
    """,
    # Version 6: Links des Hosts für die Shownotes (Good Reads, Empfehlungen), JSON [{url, title}]
    """
    ALTER TABLE episodes ADD COLUMN links TEXT;
    """,
]

DEFAULT_PLAN: list[tuple[str, str | None]] = [
    ("blitzlicht", None), ("blitzlicht", None), ("blitzlicht", None), ("hauptthema", None), ("good", "read")]


PLAN_ENTRIES = ("blitzlicht", "hauptthema", "good:read", "good:watch", "good:listen", "frei")


def default_plan(c: sqlite3.Connection) -> list[tuple[str, str | None]]:
    """Standardplan einer neuen Folge: Einstellung `format.plan` (Einträge wie `blitzlicht` oder `good:read`),
    sonst `DEFAULT_PLAN`."""
    try:
        row = c.execute("SELECT value FROM settings WHERE key='format'").fetchone()
        plan = (util.loads(row[0], {}) or {}).get("plan") if row else None
    except sqlite3.Error:
        plan = None
    out = []
    for entry in plan or []:
        if entry in PLAN_ENTRIES:
            kind, _, good = entry.partition(":")
            out.append((kind, good or None))
    return out or list(DEFAULT_PLAN)


def seed_default_plan(c: sqlite3.Connection, episode_id: str) -> None:
    now = util.now_ms()
    for i, (kind, good_type) in enumerate(default_plan(c)):
        c.execute("INSERT INTO segments(id,episode_id,sort,kind,good_type,updated_at) VALUES(?,?,?,?,?,?)",
                  (util.new_id(), episode_id, i, kind, good_type, now))


class DB:
    """Dünne Hülle: je Aufruf eine Verbindung (WAL erlaubt parallele Leser), `tx()` für Mehrfach-Schreiben."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        c = self._connect()
        c.execute("PRAGMA journal_mode=WAL")
        version = c.execute("PRAGMA user_version").fetchone()[0]
        for i in range(version, len(MIGRATIONS)):
            c.executescript("BEGIN;" + MIGRATIONS[i] + f"PRAGMA user_version={i + 1};COMMIT;")
            if i == 1:  # bestehende Entwurfsfolge bekommt den Standardplan
                for row in c.execute("SELECT id FROM episodes WHERE status='draft'").fetchall():
                    seed_default_plan(c, row["id"])
        c.close()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def all(self, sql: str, args: tuple | list = ()) -> list[dict]:
        c = self._connect()
        try:
            return [dict(r) for r in c.execute(sql, args).fetchall()]
        finally:
            c.close()

    def one(self, sql: str, args: tuple | list = ()) -> dict | None:
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def run(self, sql: str, args: tuple | list = ()) -> int:
        c = self._connect()
        try:
            return c.execute(sql, args).rowcount
        finally:
            c.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    # --- Einstellungen -------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return util.loads(row["value"], default) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.run(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, util.dumps(value)),
        )
