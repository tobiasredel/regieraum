"""Umzug des veröffentlichten Bestands von einem lokalen Speicherordner in einen anderen Speicher (local → s3).

`transfer_tree` lädt jede Datei unter ihrem bisherigen Schlüssel hoch, damit die in der Datenbank gespeicherten
Schlüssel (`audio_key`, `cover_key`, `legacy.image_key`) unverändert weiter gelten. Der Lauf ist wiederholbar: Was mit
gleicher Größe schon im Ziel liegt, wird übersprungen. `feed.xml` wird nicht kopiert, weil der Feed die Adressen des
alten Speichers enthält; er wird danach mit dem Zielspeicher neu gebaut (`scripts/transfer_to_s3.py`).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .storage import IMMUTABLE, Storage, StorageError

# Reihenfolge zählt: die längere Endung zuerst
CONTENT_TYPES: tuple[tuple[str, str], ...] = (
    (".chapters.json", "application/json+chapters"),
    (".mp3", "audio/mpeg"),
    (".m4a", "audio/x-m4a"),
    (".vtt", "text/vtt"),
    (".jpg", "image/jpeg"),
    (".jpeg", "image/jpeg"),
    (".png", "image/png"),
)
EXCLUDE = ("feed.xml",)


@dataclass
class TransferResult:
    uploaded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # lagen schon mit gleicher Größe im Ziel
    unknown: list[str] = field(default_factory=list)  # Dateiart nicht bekannt, nicht übertragen
    bytes_uploaded: int = 0
    bytes_total: int = 0


def content_type(key: str) -> str | None:
    low = key.lower()
    for ext, mime in CONTENT_TYPES:
        if low.endswith(ext):
            return mime
    return None


def transfer_tree(root: str | Path, target: Storage, *, exclude: tuple[str, ...] = EXCLUDE, dry_run: bool = False,
                  on_progress: Callable[[int, int, str, str], None] | None = None) -> TransferResult:
    """Überträgt alle Dateien unter `root` nach `target`. `on_progress(nr, anzahl, schlüssel, was)` mit `was` aus
    „hochgeladen“, „schon da“, „unbekannt“. Wirft `StorageError`, wenn eine Datei nach dem Hochladen nicht mit
    gleicher Größe im Ziel liegt."""
    root = Path(root)
    files = sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith(".upload-"))
    todo = [(p.relative_to(root).as_posix(), p) for p in files]
    todo = [(k, p) for k, p in todo if k not in exclude]
    res = TransferResult()
    for i, (key, path) in enumerate(todo, 1):
        size = path.stat().st_size
        mime = content_type(key)
        if mime is None:
            res.unknown.append(key)
            what = "unbekannt"
        elif target.size(key) == size:
            res.skipped.append(key)
            res.bytes_total += size
            what = "schon da"
        else:
            if not dry_run:
                target.put_file(key, path, mime, IMMUTABLE)
                got = target.size(key)
                if got != size:
                    raise StorageError(f"{key} liegt nach dem Hochladen mit {got} statt {size} Bytes im Speicher.")
            res.uploaded.append(key)
            res.bytes_uploaded += size
            res.bytes_total += size
            what = "hochgeladen"
        if on_progress:
            on_progress(i, len(todo), key, what)
    return res
