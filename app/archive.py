"""Rohdaten archivieren: Rohspuren, Zwischenstände und Mischungen einer veröffentlichten Folge wandern nach Ablauf einer
Frist aus dem Folgenverzeichnis ins Archiv (`PS_ARCHIVE_DIR`, Unterordner `<guid>`). Der Ordner `out/` (fertige Folge,
Wellenform, Transkript, Kapitel) bleibt lokal. Erst wird kopiert und die Größe geprüft, dann lokal gelöscht.
Für „Neu mischen“ und „Neu verarbeiten“ holt `restore` alles zurück. Alle Funktionen blockieren (Job-Läufer ruft sie per Thread)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import core, util
from .config import Config
from .db import DB

DAY_MS = 24 * 3600 * 1000
KEEP = "out"  # bleibt immer lokal
RETRY_AFTER_MS = 6 * 3600 * 1000  # nach einem Fehlschlag frühestens nach sechs Stunden noch einmal


class ArchiveError(Exception):
    """Meldung ist für Menschen gedacht (deutsch)."""


def dir_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _listing(root: Path, skip_top: str | None = None) -> dict[str, int]:
    """Alle Dateien unter root als {relativer Pfad: Größe}; `skip_top` lässt einen obersten Ordner aus."""
    out: dict[str, int] = {}
    for dirpath, dirs, files in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        if skip_top and rel_dir == Path("."):
            dirs[:] = [d for d in dirs if d != skip_top]
            files = [f for f in files if f != skip_top]
        for f in files:
            out[str(rel_dir / f)] = os.lstat(os.path.join(dirpath, f)).st_size
    return out


def _copy_entries(src: Path, dst: Path, skip_top: str | None) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        if skip_top and entry.name == skip_top:
            continue
        target = dst / entry.name
        if entry.is_dir() and not entry.is_symlink():
            shutil.copytree(entry, target, dirs_exist_ok=True, copy_function=shutil.copy2)
        else:
            shutil.copy2(entry, target)


def _verify(src: Path, dst: Path, skip_top: str | None) -> None:
    want = _listing(src, skip_top)
    have = _listing(dst)
    if want != have:
        missing = sorted(set(want) ^ set(have))[:3] or sorted(k for k in want if want[k] != have.get(k))[:3]
        raise ArchiveError(f"Die Kopie stimmt nicht mit dem Original überein ({', '.join(missing)})")


def settings(db: DB) -> dict:
    return {**core.DEFAULT_ARCHIVE, **(db.get_setting("archive", {}) or {})}


def after_days(db: DB) -> int:
    try:
        return max(0, int(settings(db).get("after_days")))
    except (TypeError, ValueError):
        return core.DEFAULT_ARCHIVE["after_days"]


def has_raw_content(cfg: Config, episode_id: str) -> bool:
    d = cfg.episode_dir(episode_id)
    return d.is_dir() and any(e.name != KEEP for e in d.iterdir())


def due_episodes(db: DB, cfg: Config, now_ms: int | None = None) -> list[dict]:
    """Veröffentlichte, selbst aufgenommene Folgen, deren Frist abgelaufen ist und die noch Rohdaten lokal haben.
    Ohne Archiv, ohne Veröffentlichung oder in der Prüfung nie."""
    if cfg.archive_dir is None:
        return []
    now = now_ms if now_ms is not None else util.now_ms()
    limit = after_days(db) * DAY_MS
    out = []
    for ep in db.all("SELECT * FROM episodes WHERE status='published' AND source='recorded' AND archived_at IS NULL "
                     "AND published_at IS NOT NULL"):
        if now - ep["published_at"] < limit:
            continue
        if db.one("SELECT 1 AS x FROM jobs WHERE episode_id=? AND (status IN ('queued','running') OR "
                  "(kind='archive' AND created_at>?)) LIMIT 1", (ep["id"], now - RETRY_AFTER_MS)):
            continue
        if has_raw_content(cfg, ep["id"]):
            out.append(ep)
    return out


def archive_episode(db: DB, cfg: Config, episode_id: str) -> int:
    """Rohdaten einer Folge ins Archiv verschieben. -> Anzahl der freigegebenen Bytes."""
    if cfg.archive_dir is None:
        raise ArchiveError("Es ist kein Archiv eingerichtet")
    ep = core.get_episode(db, episode_id)
    if ep is None:
        raise ArchiveError("Folge nicht gefunden")
    if ep["status"] != "published" or ep["source"] != "recorded":
        raise ArchiveError("Archiviert werden nur veröffentlichte, selbst aufgenommene Folgen")
    if ep["archived_at"]:
        return 0
    src = cfg.episode_dir(episode_id)
    if not has_raw_content(cfg, episode_id):
        return 0
    dst = cfg.archive_dir / ep["guid"]
    part = cfg.archive_dir / (ep["guid"] + ".partial")
    freed = dir_bytes(src) - dir_bytes(src / KEEP)
    try:
        shutil.rmtree(part, ignore_errors=True)
        _copy_entries(src, part, KEEP)
        _verify(src, part, KEEP)
        shutil.rmtree(dst, ignore_errors=True)
        part.rename(dst)
    except ArchiveError:
        shutil.rmtree(part, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(part, ignore_errors=True)
        raise ArchiveError(f"Das Archiv ist nicht beschreibbar oder voll: {exc}") from exc
    core.update_episode(db, episode_id, archived_at=util.now_ms(), archive_path=str(dst))
    for entry in src.iterdir():
        if entry.name == KEEP:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    return freed


def restore_episode(db: DB, cfg: Config, episode_id: str) -> int:
    """Rohdaten aus dem Archiv zurückholen und `archived_at` löschen. -> Anzahl der zurückgeholten Bytes."""
    ep = core.get_episode(db, episode_id)
    if ep is None:
        raise ArchiveError("Folge nicht gefunden")
    if not ep["archived_at"] or not ep["archive_path"]:
        return 0
    src = Path(ep["archive_path"])
    if not src.is_dir():
        raise ArchiveError("Das Archiv ist nicht erreichbar oder die Rohdaten fehlen dort")
    dst = cfg.episode_dir(episode_id)
    try:
        _copy_entries(src, dst, None)
        want = _listing(src)
        have = _listing(dst, KEEP)
        if any(have.get(k) != v for k, v in want.items()):
            raise ArchiveError("Die zurückgeholten Dateien stimmen nicht mit dem Archiv überein")
    except OSError as exc:
        raise ArchiveError(f"Zurückholen fehlgeschlagen: {exc}") from exc
    core.update_episode(db, episode_id, archived_at=None, archive_path=None)
    size = dir_bytes(src)
    shutil.rmtree(src, ignore_errors=True)  # lokal liegt jetzt alles; sonst läge es doppelt
    return size


def remove_copy(ep: dict) -> None:
    """Archivkopie einer Folge löschen (Folge wird gelöscht oder verworfen)."""
    if ep.get("archive_path"):
        shutil.rmtree(ep["archive_path"], ignore_errors=True)


def totals(cfg: Config) -> dict:
    """Speicherbedarf: Summe aller Folgenverzeichnisse und freier Platz des Datenträgers."""
    used = dir_bytes(cfg.episodes_dir) if cfg.episodes_dir.exists() else 0
    out = {"episodes_bytes": used, "configured": cfg.archive_dir is not None}
    if cfg.archive_dir is not None:
        out["archive_free_bytes"] = shutil.disk_usage(cfg.archive_dir).free if cfg.archive_dir.exists() else None
        out["archive_bytes"] = dir_bytes(cfg.archive_dir) if cfg.archive_dir.exists() else 0
    return out
