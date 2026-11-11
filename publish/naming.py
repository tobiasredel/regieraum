"""Schlüsselnamen im Speicher: nur ASCII (Apple verlangt a-zA-Z0-9 in Datei- und URL-Namen)."""
from __future__ import annotations

import hashlib
import re
import unicodedata

_UML = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "ae", "Ö": "oe", "Ü": "ue", "ß": "ss"}


def slugify(text: str, max_len: int = 50, fallback: str = "episode") -> str:
    for k, v in _UML.items():
        text = text.replace(k, v)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:max_len].strip("-")
    return text or fallback


def short_hash(data: str | bytes, n: int = 8) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:n]


def guid_slug(guid: str) -> str:
    """Verzeichnisname je Folge. Ein GUID aus [A-Za-z0-9-] (bis 64 Zeichen) bleibt unverändert,
    alles andere wird bereinigt und bekommt einen Kurz-Hash, damit nichts kollidiert."""
    if re.fullmatch(r"[A-Za-z0-9-]{1,64}", guid):
        return guid
    base = re.sub(r"[^A-Za-z0-9]+", "-", unicodedata.normalize("NFKD", guid).encode("ascii", "ignore").decode()).strip("-")[:40]
    return f"{base or 'guid'}-{short_hash(guid, 6)}"


EXT_BY_MIME = {
    "audio/mpeg": "mp3",
    "audio/x-m4a": "m4a",
    "audio/mp4": "m4a",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/flac": "flac",
    "video/mp4": "mp4",
    "image/jpeg": "jpg",
    "image/png": "png",
    "text/vtt": "vtt",
    "application/json": "json",
}


def media_key(guid: str, stem: str, content_hash: str, ext: str) -> str:
    return f"media/{guid_slug(guid)}/{stem}-{content_hash}.{ext}"


def sibling_keys(audio_key: str) -> tuple[str, str]:
    """Kapitel- und Transkript-Schlüssel zu einem Audio-Schlüssel: gleicher Namensstamm, andere Endung."""
    stem = audio_key.rsplit(".", 1)[0]
    return f"{stem}.chapters.json", f"{stem}.vtt"
