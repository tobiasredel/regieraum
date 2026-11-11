"""Downloads zählen (SPEC Abschnitt 15): öffentliche Weiterleitung `/d/<audio_key>` und Auswertung für die Verwaltung.

Datensparsam: Es wird keine IP gespeichert, nur `sha256(salt + ip + user_agent + tag)[:16]`, ein Besucher zählt je Folge
und Tag einmal. Das Salz entsteht einmalig zufällig in `$PS_DATA/stats_salt`."""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from . import core, util
from .auth import host_guard
from .errors import ApiError

public = APIRouter()
router = APIRouter(prefix="/api/host", dependencies=[Depends(host_guard)])

BOTS = ("bot", "crawl", "spider", "slurp", "curl/", "wget/", "python-requests", "python-urllib", "aiohttp", "httpx",
        "go-http-client", "headlesschrome", "facebookexternalhit", "preview", "monitor", "uptime")
_RANGE_START = re.compile(r"^\s*bytes\s*=\s*0\s*-")


def _salt(cfg) -> bytes:
    path = cfg.data / "stats_salt"
    for _ in range(3):
        try:
            data = path.read_bytes()
            if len(data) >= 16:
                return data
        except FileNotFoundError:
            pass
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(32))
    return path.read_bytes()


def is_bot(user_agent: str) -> bool:
    ua = (user_agent or "").lower().strip()
    return not ua or any(b in ua for b in BOTS)


def counts(method: str, range_header: str | None, user_agent: str) -> bool:
    """Zählt dieser Abruf? Nur GET, ohne Range oder ab Byte 0, kein bekannter Bot."""
    if method != "GET" or is_bot(user_agent):
        return False
    return not range_header or bool(_RANGE_START.match(range_header))


def _client_ip(request: Request) -> str:
    h = request.headers
    fwd = (h.get("cf-connecting-ip") or h.get("x-forwarded-for") or "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "")


def today(ms: int | None = None) -> str:
    return dt.datetime.fromtimestamp((ms if ms is not None else util.now_ms()) / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d")


def visitor_hash(salt: bytes, ip: str, user_agent: str, day: str) -> str:
    return hashlib.sha256(salt + ip.encode() + b"\0" + user_agent.encode() + b"\0" + day.encode()).hexdigest()[:16]


@public.api_route("/d/{key:path}", methods=["GET", "HEAD"], include_in_schema=False)
def download(key: str, request: Request) -> Response:
    """Öffentlich: zählt den Abruf und leitet mit 302 auf die Datei im Speicher weiter."""
    st = request.app.state
    ep = st.db.one("SELECT id FROM episodes WHERE audio_key=? AND status='published'", (key,))
    if ep is None or not st.cfg.public_base_url:
        raise ApiError(404, "not_found", "Datei nicht gefunden")
    ua = request.headers.get("user-agent", "")
    if counts(request.method, request.headers.get("range"), ua):
        day = today()
        st.db.run("INSERT OR IGNORE INTO downloads(day,episode_id,visitor) VALUES(?,?,?)",
                  (day, ep["id"], visitor_hash(_salt(st.cfg), _client_ip(request), ua, day)))
    return Response(status_code=302, headers={"Location": f"{st.cfg.public_base_url}/{quote(key)}", "Cache-Control": "no-store"})


def totals(db) -> dict[str, int]:
    return {r["episode_id"]: r["n"] for r in db.all("SELECT episode_id, COUNT(*) AS n FROM downloads GROUP BY episode_id")}


def history(db, episode_id: str, days: int = 30) -> dict:
    now = util.now_ms()
    day_list = [today(now - i * 86_400_000) for i in range(days - 1, -1, -1)]
    per = {r["day"]: r["n"] for r in db.all(
        "SELECT day, COUNT(*) AS n FROM downloads WHERE episode_id=? AND day>=? GROUP BY day", (episode_id, day_list[0]))}
    series = [{"day": d, "count": per.get(d, 0)} for d in day_list]
    total = db.one("SELECT COUNT(*) AS n FROM downloads WHERE episode_id=?", (episode_id,))["n"]
    return {"total": total, "last7": sum(x["count"] for x in series[-7:]), "last30": sum(x["count"] for x in series), "days": series}


@router.get("/stats/downloads")
def stats_totals(request: Request) -> dict:
    """Downloads je Folge (Gesamtzahl) und der eingestellte Zählmodus."""
    db = request.app.state.db
    return {"mode": core.get_stats(db)["mode"], "totals": totals(db)}


@router.get("/episodes/{episode_id}/downloads")
def episode_downloads(episode_id: str, request: Request) -> dict:
    db = request.app.state.db
    if db.one("SELECT id FROM episodes WHERE id=?", (episode_id,)) is None:
        raise ApiError(404, "episode_not_found", "Folge nicht gefunden")
    return {"mode": core.get_stats(db)["mode"], **history(db, episode_id)}
