"""Gemeinsame Hilfen für die Tests von publish/: künstliche Audio- und Bilddateien, Beispiel-Folgen."""
from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

from publish.models import FeedEpisode, Show

FIXTURES = Path(__file__).parent


def fake_mp3(size: int = 4096, seed: str = "a") -> bytes:
    """Beginnt mit einem ID3-Kopf, danach reproduzierbare Füllbytes (kein echtes Audio)."""
    block = b"".join(hashlib.sha256(f"{seed}{i}".encode()).digest() for i in range(1024))  # 32 KB
    body = (block * (size // len(block) + 1))[: max(0, size - 10)]
    return b"ID3\x03\x00\x00\x00\x00\x00\x00" + body


def fake_m4a(size: int = 4096) -> bytes:
    return b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A mp42isom" + b"\x00" * (size - 28)


def png_bytes(w: int, h: int) -> bytes:
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0")) + chunk(b"IEND", b"")


def jpeg_bytes(w: int, h: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def show(**kw) -> Show:
    base = dict(
        title="Zwei am Mikro", author="Anna Beispiel & Ben Muster",
        description="Politisch, sarkastisch & humorvoll: „Fünf Minuten Wahrheit“.",
        owner_name="Anna Beispiel", owner_email="podcast@example.org", language="de",
        category="News", subcategory="News Commentary", explicit=True, link="https://example.org/",
        copyright="© 2026 Beispiel & Muster", cover_key="cover-abcd1234.jpg",
    )
    base.update(kw)
    return Show(**base)


def episode(n: int = 1, **kw) -> FeedEpisode:
    base = dict(
        guid=f"guid-{n}", title=f"Folge {n}: Öl & „Gas“", audio_key=f"media/guid-{n}/folge-{n}-aaaa1111.mp3",
        audio_bytes=1234567 + n, audio_mime="audio/mpeg", pub_date_ms=1_700_000_000_000 + n * 86_400_000,
        duration_ms=3_723_000, summary=f"Kurz {n}.", description_html=f"<p>Beschreibung {n}</p>", number=n,
    )
    base.update(kw)
    return FeedEpisode(**base)
