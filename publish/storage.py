"""Speicher für veröffentlichte Dateien: lokal (Entwicklung) oder S3-kompatibel (Cloudflare R2)."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

IMMUTABLE = "public, max-age=31536000, immutable"
FEED_CACHE = "public, max-age=300"


class StorageError(Exception):
    """Speicher nicht erreichbar oder falsch eingerichtet (deutsche Meldung für Menschen)."""


class Storage(Protocol):
    def put_file(self, key: str, path: Path, content_type: str, cache_control: str | None = None) -> None: ...
    def put_bytes(self, key: str, data: bytes, content_type: str, cache_control: str | None = None) -> None: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def size(self, key: str) -> int | None: ...
    def url(self, key: str) -> str: ...


def _check_key(key: str) -> str:
    if not key or key.startswith("/") or ".." in key.split("/") or "\\" in key or "\0" in key:
        raise ValueError(f"Ungültiger Speicherschlüssel: {key!r}")
    return key


class LocalStorage:
    """Dateien unter `root`, öffentlich erreichbar unter `base_url` (die App liefert `/public/` aus)."""

    def __init__(self, root: str | Path, base_url: str):
        self.root = Path(root)
        self.base_url = base_url.rstrip("/")
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / _check_key(key)

    def put_file(self, key, path, content_type, cache_control=None):
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as out, open(path, "rb") as src:
                shutil.copyfileobj(src, out, 1024 * 1024)
            os.replace(tmp, dest)
            os.chmod(dest, 0o644)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def put_bytes(self, key, data, content_type, cache_control=None):
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            os.replace(tmp, dest)
            os.chmod(dest, 0o644)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def delete(self, key):
        p = self._path(key)
        try:
            p.unlink()
        except FileNotFoundError:
            return
        d = p.parent
        while d != self.root:  # leere Unterordner aufräumen
            try:
                d.rmdir()
            except OSError:
                break
            d = d.parent

    def exists(self, key):
        return self._path(key).is_file()

    def size(self, key):
        p = self._path(key)
        return p.stat().st_size if p.is_file() else None

    def url(self, key):
        return f"{self.base_url}/{quote(_check_key(key))}"


class S3Storage:
    """S3-kompatibel, ausgelegt auf Cloudflare R2: Region `auto`, Signatur v4, Pfad-Adressierung,
    große Dateien mehrteilig (Teile gleicher Größe, wie R2 es verlangt)."""

    PART_SIZE = 16 * 1024 * 1024
    MULTIPART_THRESHOLD = 64 * 1024 * 1024

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, base_url: str,
                 *, client=None, region: str = "auto"):
        import boto3
        from boto3.s3.transfer import TransferConfig
        from botocore.config import Config

        self.bucket = bucket
        self.base_url = base_url.rstrip("/")
        cfg = dict(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=15,
            read_timeout=120,
        )
        # Neuere botocore-Versionen schicken sonst Prüfsummen-Header, die R2 nicht kennt.
        for opt in ("request_checksum_calculation", "response_checksum_validation"):
            if opt in getattr(Config, "OPTION_DEFAULTS", {}):
                cfg[opt] = "when_required"
        self.client = client or boto3.client(
            "s3",
            endpoint_url=endpoint or None,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(**cfg),
        )
        self._transfer = TransferConfig(
            multipart_threshold=self.MULTIPART_THRESHOLD,
            multipart_chunksize=self.PART_SIZE,
            max_concurrency=4,
            use_threads=True,
        )

    @staticmethod
    def _extra(content_type: str, cache_control: str | None) -> dict:
        extra = {"ContentType": content_type}
        if cache_control:
            extra["CacheControl"] = cache_control
        return extra

    def put_file(self, key, path, content_type, cache_control=None):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            self.client.upload_file(str(path), self.bucket, _check_key(key),
                                    ExtraArgs=self._extra(content_type, cache_control), Config=self._transfer)
        except (BotoCoreError, ClientError) as e:
            raise StorageError(f"Hochladen von {key} in den Speicher ist fehlgeschlagen: {e}") from e

    def put_bytes(self, key, data, content_type, cache_control=None):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            self.client.put_object(Bucket=self.bucket, Key=_check_key(key), Body=data,
                                   **self._extra(content_type, cache_control))
        except (BotoCoreError, ClientError) as e:
            raise StorageError(f"Hochladen von {key} in den Speicher ist fehlgeschlagen: {e}") from e

    def delete(self, key):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            self.client.delete_object(Bucket=self.bucket, Key=_check_key(key))
        except (BotoCoreError, ClientError) as e:
            raise StorageError(f"Löschen von {key} im Speicher ist fehlgeschlagen: {e}") from e

    def _head(self, key):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            return self.client.head_object(Bucket=self.bucket, Key=_check_key(key))
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise StorageError(f"Speicher antwortet auf {key} mit Fehler {code}: {e}") from e
        except BotoCoreError as e:
            raise StorageError(f"Speicher nicht erreichbar: {e}") from e

    def exists(self, key):
        return self._head(key) is not None

    def size(self, key):
        h = self._head(key)
        return None if h is None else int(h["ContentLength"])

    def url(self, key):
        return f"{self.base_url}/{quote(_check_key(key))}"


def from_env(env=None) -> Storage:
    """Speicher nach `PS_STORAGE` (`local` oder `s3`), Variablen laut SPEC Abschnitt 2."""
    env = os.environ if env is None else env
    kind = (env.get("PS_STORAGE") or "local").strip().lower()
    if kind == "local":
        data = env.get("PS_DATA") or "/data"
        base = env.get("PS_PUBLIC_BASE_URL") or (env.get("PS_BASE_URL") or "http://localhost:8000").rstrip("/") + "/public"
        return LocalStorage(Path(data) / "public", base)
    if kind == "s3":
        need = {
            "PS_S3_ENDPOINT": "Endpunkt",
            "PS_S3_BUCKET": "Bucket",
            "PS_S3_ACCESS_KEY": "Zugangsschlüssel",
            "PS_S3_SECRET_KEY": "Geheimer Schlüssel",
            "PS_PUBLIC_BASE_URL": "öffentliche Basis-Adresse",
        }
        missing = [f"{k} ({v})" for k, v in need.items() if not (env.get(k) or "").strip()]
        if missing:
            raise StorageError("Der Speicher „s3“ ist nicht vollständig eingerichtet, es fehlt: " + ", ".join(missing))
        return S3Storage(env["PS_S3_ENDPOINT"], env["PS_S3_BUCKET"], env["PS_S3_ACCESS_KEY"],
                         env["PS_S3_SECRET_KEY"], env["PS_PUBLIC_BASE_URL"])
    raise StorageError(f"Unbekannter Speicher PS_STORAGE={kind!r}; erlaubt sind „local“ und „s3“.")
