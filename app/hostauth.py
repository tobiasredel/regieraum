"""Eingebaute Host-Anmeldung (PS_HOST_AUTH=password): Passwort, signiertes Sitzungs-Cookie, Schutz-Middleware."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from starlette.requests import Request as StarletteRequest

from . import util
from .config import Config
from .errors import error_body

COOKIE = "ps_host"
SESSION_S = 30 * 24 * 3600
MIN_PASSWORD = 8
MAX_FAILS = 10
WINDOW_S = 600
SETUP_MESSAGE = "PS_HOST_PASSWORD ist nicht gesetzt oder zu kurz. Es braucht mindestens 8 Zeichen"

router = APIRouter()


class HostAuth:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._key: bytes | None = None
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return len(self.cfg.host_password) >= MIN_PASSWORD

    @property
    def secure(self) -> bool:
        return self.cfg.base_url.startswith("https://")

    def _secret(self) -> bytes:
        path = self.cfg.data / "host_secret"
        for _ in range(3):
            try:
                data = path.read_bytes()
                if len(data) >= 32:
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

    def key(self) -> bytes:
        if self._key is None:
            self._key = hashlib.sha256(self._secret() + self.cfg.host_password.encode()).digest()
        return self._key

    def _sign(self, expires: int) -> str:
        return hmac.new(self.key(), str(expires).encode(), hashlib.sha256).hexdigest()

    def make_cookie(self) -> str:
        exp = util.now_ms() // 1000 + SESSION_S
        return f"{exp}.{self._sign(exp)}"

    def valid_cookie(self, value: str | None) -> bool:
        if not value or not self.configured:
            return False
        exp_s, _, mac = value.partition(".")
        if not exp_s.isdigit() or len(exp_s) > 12:
            return False
        if not hmac.compare_digest(mac.encode(), self._sign(int(exp_s)).encode()):
            return False
        return int(exp_s) > util.now_ms() // 1000

    def check_password(self, given: str) -> bool:
        return hmac.compare_digest(given.encode(), self.cfg.host_password.encode())

    # Bremse: im Speicher, je Client-IP
    def locked(self, ip: str) -> bool:
        with self._lock:
            now = util.now_ms() / 1000
            recent = [t for t in self._fails.get(ip, []) if now - t < WINDOW_S]
            if recent:
                self._fails[ip] = recent
            else:
                self._fails.pop(ip, None)
            return len(recent) >= MAX_FAILS

    def fail(self, ip: str) -> None:
        with self._lock:
            if len(self._fails) > 5000:  # nicht unbegrenzt wachsen, wenn sehr viele Adressen anklopfen
                self._fails.clear()
            self._fails.setdefault(ip, []).append(util.now_ms() / 1000)

    def reset(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


def is_public(path: str) -> bool:
    return (
        path in ("/healthz", "/login", "/logout", "/ws/p", "/api/join")
        or path.startswith(("/join/", "/api/join/", "/api/p/", "/static/", "/public/"))
    )


def safe_next(value: str | None) -> str:
    v = value or ""
    if v.startswith("/") and not v.startswith("//") and "\\" not in v and all(c >= " " and c != "\x7f" for c in v):
        return v
    return "/studio"


def _cookie_from_scope(scope) -> str | None:
    for k, v in scope["headers"]:
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                name, _, val = part.strip().partition("=")
                if name == COOKIE:
                    return val
    return None


class HostAuthMiddleware:
    """Reine ASGI-Middleware: greift für Seiten, API, statische Mounts, HEAD und WebSocket."""

    def __init__(self, app, auth: HostAuth):
        self.app = app
        self.auth = auth

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or is_public(scope["path"]):
            return await self.app(scope, receive, send)
        if self.auth.valid_cookie(_cookie_from_scope(scope)):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await receive()
            return await send({"type": "websocket.close", "code": 1008})
        path = scope["path"]
        is_api = path.startswith("/api/")
        if not self.auth.configured:
            if is_api:
                resp = _json(503, error_body("setup", SETUP_MESSAGE))
            else:
                resp = _login_page(StarletteRequest(scope), setup=True)
        elif is_api:
            resp = _json(401, error_body("unauthorized", "Bitte anmelden"))
        else:
            qs = scope.get("query_string", b"").decode("latin-1")
            target = path + ("?" + qs if qs else "")
            resp = RedirectResponse("/login?next=" + quote(target, safe=""), status_code=303)
        await resp(scope, receive, send)


def _json(status: int, body: dict) -> Response:
    return Response(json.dumps(body, ensure_ascii=False).encode(), status, media_type="application/json",
                    headers={"cache-control": "no-store"})


def _login_page(request: Request, *, setup: bool = False, error: str = "", next_: str = "", status: int = 200):
    from .pages import render
    resp = render(request, "login.html", "Anmelden", setup=setup, error=error, next=next_)
    resp.status_code = 503 if setup else status
    resp.headers["cache-control"] = "no-store"
    return resp


def _auth(request: Request) -> HostAuth | None:
    return request.app.state.hostauth if request.app.state.cfg.host_auth == "password" else None


@router.get("/login", include_in_schema=False)
def login_page(request: Request, next: str = ""):
    auth = _auth(request)
    if auth is None:
        return RedirectResponse("/studio", status_code=303)
    if auth.valid_cookie(request.cookies.get(COOKIE)):
        return RedirectResponse(safe_next(next), status_code=303)
    return _login_page(request, setup=not auth.configured, next_=safe_next(next) if next else "")


@router.post("/login", include_in_schema=False)
async def login_submit(request: Request):
    auth = _auth(request)
    if auth is None:
        return RedirectResponse("/studio", status_code=303)
    if not auth.configured:
        return _login_page(request, setup=True)
    form = parse_qs((await request.body())[:8192].decode("utf-8", "replace"))
    given = (form.get("password") or [""])[0]
    nxt = (form.get("next") or [""])[0]
    keep = safe_next(nxt) if nxt else ""
    ip = request.client.host if request.client else "unbekannt"
    if auth.locked(ip):
        return _login_page(request, error="Zu viele Versuche. Bitte in einigen Minuten erneut versuchen",
                           next_=keep, status=429)
    if not auth.check_password(given):
        auth.fail(ip)
        return _login_page(request, error="Das Passwort stimmt nicht", next_=keep, status=401)
    auth.reset(ip)
    resp = RedirectResponse(safe_next(nxt), status_code=303)
    resp.set_cookie(COOKIE, auth.make_cookie(), max_age=SESSION_S, path="/", httponly=True, samesite="lax",
                    secure=auth.secure)
    return resp


@router.post("/logout", include_in_schema=False)
def logout(request: Request):
    auth = _auth(request)
    resp = RedirectResponse("/login" if auth else "/studio", status_code=303)
    if auth:
        resp.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax", secure=auth.secure)
    return resp
