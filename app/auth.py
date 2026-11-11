"""Teilnehmer-Token und Rollenprüfung für REST."""
from __future__ import annotations

from fastapi import Request

from . import core, util
from .errors import ApiError


def bearer(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else None


def participant(request: Request) -> dict:
    """Abhängigkeit für /api/p/…: gültiges Teilnehmer-Token erforderlich."""
    db = request.app.state.db
    p = core.participant_by_token(db, bearer(request))
    if p is None:
        raise ApiError(401, "unauthorized", "Zugang abgelaufen oder ungültig. Bitte den Einladungslink erneut öffnen")
    if (util.now_ms() - (p["last_seen_at"] or 0)) > 30_000:
        db.run("UPDATE participants SET last_seen_at=? WHERE id=?", (util.now_ms(), p["id"]))
    return p


def host_guard(request: Request) -> None:
    """Abhängigkeit für /api/host/…: Der Pfad liegt hinter der Host-Anmeldung (oder einem Proxy, siehe PS_HOST_AUTH). Ein mitgeschicktes Teilnehmer-Token ohne
    Host-Rolle bekommt nie Host-Rechte, auch wenn jemand den Proxy umgeht."""
    token = bearer(request)
    if token:
        p = core.participant_by_token(request.app.state.db, token)
        if p is not None and p["role"] != "host":
            raise ApiError(403, "forbidden", "Dieser Bereich ist nur für den Host")
