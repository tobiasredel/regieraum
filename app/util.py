"""Kleine Helfer: zentrale Serverzeit, IDs, Hashes, JSON."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable
from typing import Any


def _real_clock() -> int:
    return int(time.time() * 1000)


_clock: Callable[[], int] = _real_clock


def now_ms() -> int:
    """Serverzeit in Epoch-Millisekunden. Einzige Zeitquelle der App; Tests tauschen sie über set_clock aus."""
    return _clock()


def set_clock(fn: Callable[[], int] | None = None) -> None:
    global _clock
    _clock = fn or _real_clock


def new_id() -> str:
    """12 Zeichen, URL-sicher."""
    return secrets.token_urlsafe(9)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, maxlen: int = 60) -> str:
    table = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "ae", "Ö": "oe", "Ü": "ue"})
    s = _SLUG_RE.sub("-", text.translate(table).lower()).strip("-")
    return s[:maxlen].strip("-") or "folge"
