#!/usr/bin/env python3
"""Umzug des veröffentlichten Bestands vom lokalen Speicher nach R2 (einmalig, wiederholbar).

Im laufenden Container, solange `PS_STORAGE` noch `local` ist; die R2-Werte kommen aus der Umgebung
(`PS_S3_ENDPOINT`, `PS_S3_BUCKET`, `PS_S3_ACCESS_KEY`, `PS_S3_SECRET_KEY`, `PS_PUBLIC_BASE_URL`):

    docker exec -e PS_S3_ENDPOINT -e PS_S3_BUCKET -e PS_S3_ACCESS_KEY -e PS_S3_SECRET_KEY -e PS_PUBLIC_BASE_URL \
        podcast-studio python scripts/transfer_to_s3.py [--dry-run] [--no-feed]

1. Alle Dateien aus `$PS_DATA/public` (ohne `feed.xml`) unter gleichem Schlüssel hochladen.
2. Prüfen, dass jede veröffentlichte Folge und das Cover im Ziel liegen.
3. `feed.xml` mit den Adressen des Zielspeichers bauen und hochladen.

Danach `PS_STORAGE='s3'` setzen und den Container neu erstellen. Die lokalen Dateien bleiben liegen und können
gelöscht werden, sobald der neue Feed geprüft ist.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import core, publishing  # noqa: E402
from app.config import Config  # noqa: E402
from app.db import DB  # noqa: E402
from publish.storage import S3Storage, StorageError  # noqa: E402
from publish.transfer import transfer_tree  # noqa: E402

NEED = ("PS_S3_ENDPOINT", "PS_S3_BUCKET", "PS_S3_ACCESS_KEY", "PS_S3_SECRET_KEY", "PS_PUBLIC_BASE_URL")


def missing_keys(db: DB, cfg: Config, target) -> list[str]:
    """Schlüssel, auf die der Feed zeigen wird, die im Ziel aber fehlen oder eine andere Größe haben."""
    bad = []
    for ep in db.all("SELECT audio_key, audio_bytes FROM episodes WHERE status='published' AND audio_key IS NOT NULL"):
        if target.size(ep["audio_key"]) != ep["audio_bytes"]:
            bad.append(ep["audio_key"])
    cover = core.get_settings(db, cfg)["show"].get("cover_key")
    if cover and not target.exists(cover):
        bad.append(cover)
    return bad


def main(argv: list[str]) -> int:
    dry, no_feed = "--dry-run" in argv, "--no-feed" in argv
    env = os.environ
    missing = [k for k in NEED if not (env.get(k) or "").strip()]
    if missing:
        print("Es fehlt in der Umgebung: " + ", ".join(missing))
        return 2
    cfg = Config.from_env()
    target = S3Storage(env["PS_S3_ENDPOINT"], env["PS_S3_BUCKET"], env["PS_S3_ACCESS_KEY"], env["PS_S3_SECRET_KEY"],
                       env["PS_PUBLIC_BASE_URL"])

    def show(i: int, n: int, key: str, what: str) -> None:
        print(f"[{i}/{n}] {what}: {key}", flush=True)

    try:
        res = transfer_tree(cfg.public_dir, target, dry_run=dry, on_progress=show)
        gb = 1e9
        print(f"{'Probelauf: ' if dry else ''}{len(res.uploaded)} hochgeladen ({res.bytes_uploaded / gb:.2f} GB), "
              f"{len(res.skipped)} schon da, {len(res.unknown)} unbekannt {res.unknown or ''}")
        if dry:
            return 0
        db = DB(cfg.db_path)
        bad = missing_keys(db, cfg, target)
        if bad:
            print(f"{len(bad)} Dateien fehlen im Ziel oder haben eine andere Größe, der Feed wird nicht gebaut:")
            for k in bad:
                print("  " + k)
            return 1
        if no_feed:
            return 0
        publishing.rebuild_feed(db, cfg, target)
        print("Feed hochgeladen: " + target.url("feed.xml"))
    except StorageError as exc:
        print(f"Abbruch: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
