"""Sicherheits-Header und Cache-Steuerung als reine ASGI-Middleware."""
from __future__ import annotations

from urllib.parse import urlsplit

from .config import Config


def csp(cfg: Config) -> str:
    pub = ""
    if cfg.public_base_url:
        u = urlsplit(cfg.public_base_url)
        pub = f" {u.scheme}://{u.netloc}"
    ws = " ws:" if cfg.base_url.startswith("http://") else ""
    return "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        f"img-src 'self' data: blob:{pub}",
        "font-src 'self'",
        f"media-src 'self' blob:{pub}",
        f"connect-src 'self' wss:{ws}",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])


class SecurityHeaders:
    def __init__(self, app, cfg: Config):
        self.app = app
        self.csp = csp(cfg).encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]
        is_api = path.startswith("/api/")
        # ES-Module importieren einander ohne Versions-Query: der Browser fragt jedes Mal per ETag nach
        revalidate = path.startswith(("/static/js/", "/static/css/"))

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {k.lower() for k, _ in headers}
                extra = [
                    (b"content-security-policy", self.csp),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"same-origin"),
                    (b"permissions-policy", b"microphone=(self), camera=(self), display-capture=()"),
                ]
                if is_api and b"cache-control" not in present:
                    extra.append((b"cache-control", b"no-store"))
                elif revalidate and b"cache-control" not in present:
                    extra.append((b"cache-control", b"no-cache"))
                headers.extend(h for h in extra if h[0] not in present)
            await send(message)

        await self.app(scope, receive, wrapped)
