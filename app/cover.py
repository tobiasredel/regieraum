"""Cover-Prüfung ohne Bildbibliothek: JPEG/PNG, quadratisch, 1400 bis 3000 px."""
from __future__ import annotations

import struct

MIN_PX, MAX_PX = 1400, 3000


class CoverError(Exception):
    pass


def image_info(data: bytes) -> tuple[str, int, int]:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return "png", w, h
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            (length,) = struct.unpack(">H", data[i + 2:i + 4])
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return "jpg", w, h
            i += 2 + length
    raise CoverError("Das Cover muss ein JPEG oder PNG sein")


def validate(data: bytes) -> str:
    """Liefert die Dateiendung oder wirft CoverError mit verständlicher Meldung."""
    ext, w, h = image_info(data)
    if w != h:
        raise CoverError(f"Das Cover muss quadratisch sein (hochgeladen: {w} × {h} px)")
    if not MIN_PX <= w <= MAX_PX:
        raise CoverError(f"Das Cover muss zwischen {MIN_PX} und {MAX_PX} px breit sein (hochgeladen: {w} px)")
    return ext
