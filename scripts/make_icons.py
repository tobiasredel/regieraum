"""Erzeugt die App-Symbole für den Home-Bildschirm (einmalig; die PNGs liegen unter app/static/icons/).

Ein Mikrofon-Glyph in der Akzentfarbe des Standard-Themas auf heller Fläche. Die Symbole sind bewusst neutral:
Wer ein eigenes Erscheinungsbild möchte, ersetzt die Dateien im Ordner. Aufruf im Dev-Container:

    ./dev.sh run python scripts/make_icons.py
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
BG = (0xF5, 0xF7, 0xFA)
FG = (0x24, 0x47, 0xC9)
SS = 4  # Überabtastung gegen Treppenstufen


def _seg(px, py, ax, ay, bx, by):
    """Abstand jedes Punkts zur Strecke a–b."""
    dx, dy = bx - ax, by - ay
    t = np.clip(((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy or 1.0), 0, 1)
    return np.hypot(px - (ax + t * dx), py - (ay + t * dy))


def glyph_mask(size: int) -> np.ndarray:
    n = size * SS
    y, x = (np.mgrid[0:n, 0:n] + 0.5) / n
    m = _seg(x, y, 0.5, 0.31, 0.5, 0.43) <= 0.09                       # Kapsel
    ring = np.abs(np.hypot(x - 0.5, y - 0.43) - 0.17) <= 0.0165          # Bügel (untere Hälfte)
    m |= ring & (y >= 0.43)
    m |= _seg(x, y, 0.5, 0.60, 0.5, 0.72) <= 0.0165                     # Fuß
    m |= _seg(x, y, 0.40, 0.72, 0.60, 0.72) <= 0.0165                   # Standplatte
    m |= _seg(x, y, 0.33, 0.43, 0.33, 0.44) <= 0.0165                   # linkes Bügelende, rund
    m |= _seg(x, y, 0.67, 0.43, 0.67, 0.44) <= 0.0165                   # rechtes Bügelende, rund
    return m.reshape(size, SS, size, SS).mean(axis=(1, 3))


def render(size: int) -> np.ndarray:
    a = glyph_mask(size)[..., None]
    bg = np.array(BG, dtype=np.float64)
    fg = np.array(FG, dtype=np.float64)
    return np.rint(bg * (1 - a) + fg * a).astype(np.uint8)


def write_png(path: Path, img: np.ndarray) -> None:
    h, w, _ = img.shape
    raw = b"".join(b"\x00" + img[i].tobytes() for i in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    path.write_bytes(png)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size in (("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)):
        write_png(OUT / name, render(size))
        print("geschrieben:", OUT / name)


if __name__ == "__main__":
    main()
