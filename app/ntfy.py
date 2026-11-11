"""ntfy-Benachrichtigungen (SPEC Abschnitt 13). Ohne PS_NTFY_URL passiert nichts."""
from __future__ import annotations

import base64
import logging

import httpx

from .config import Config

log = logging.getLogger("podcast.ntfy")
TOPIC = "podcast"  # Standard-Topic, wenn PS_NTFY_URL nur den Server nennt


def _post(url: str, headers: dict, body: str, auth: tuple[str, str] | None) -> None:
    httpx.post(url, content=body.encode(), headers=headers, auth=auth, timeout=10).raise_for_status()


def _header(value: str) -> str:
    """HTTP-Header sind ASCII; Umlaute gehen nach RFC 2047 kodiert durch, ntfy dekodiert das."""
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


def send(cfg: Config, title: str, message: str, priority: str = "default", tags: str = "", click: str | None = None) -> None:
    """Blockierend; aus asyncio-Code per to_thread aufrufen. Fehler werden nur protokolliert."""
    if not cfg.ntfy_url:
        return
    url = cfg.ntfy_url.rstrip("/")
    if url.count("/") <= 2:  # nur Host, kein Topic
        url += "/" + TOPIC
    headers = {"Title": _header(title), "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if click:
        headers["Click"] = click
    auth = (cfg.ntfy_user, cfg.ntfy_pass) if cfg.ntfy_user else None
    try:
        _post(url, headers, message, auth)
    except Exception as exc:  # noqa: BLE001 - Benachrichtigung darf nie etwas kaputt machen
        log.warning("ntfy fehlgeschlagen: %s", exc)
