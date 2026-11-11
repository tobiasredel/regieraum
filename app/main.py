"""App-Fabrik. `app` ist die Modul-Variable für uvicorn (`uvicorn app.main:app`)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

from . import api_host, api_p, core, downloads, errors, hostauth, jobs, pages, plan
from .config import Config
from .db import DB
from .room import Room
from .security import SecurityHeaders

log = logging.getLogger("podcast")


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    cfg.ensure_dirs()
    db = DB(cfg.db_path)
    core.ensure_permanent_invite(db)
    plan.mark_stale_research(db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if cfg.jobs:
            app.state.runner.start()
        yield
        await app.state.researcher.stop()
        await app.state.room.companion.close()
        await app.state.runner.stop()

    app = FastAPI(title="Podcast-Studio", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.cfg, app.state.db = cfg, db
    app.state.room = Room(app)
    app.state.runner = jobs.create_runner(app)
    app.state.researcher = plan.Researcher(app)
    errors.install(app)
    app.state.hostauth = hostauth.HostAuth(cfg)
    if cfg.host_auth == "password":
        app.add_middleware(hostauth.HostAuthMiddleware, auth=app.state.hostauth)
    elif cfg.host_auth == "none":
        log.warning("PS_HOST_AUTH=none: Die Host-Seiten sind ohne Anmeldung erreichbar. Nur für Entwicklung und Tests")
    app.add_middleware(SecurityHeaders, cfg=cfg)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        db.one("SELECT 1 AS ok")
        return {"ok": True}

    @app.websocket("/ws/p")
    async def ws_p(ws: WebSocket) -> None:
        await app.state.room.serve(ws)

    app.include_router(api_p.router)
    app.include_router(api_host.router)
    app.include_router(plan.router)
    app.include_router(downloads.router)
    app.include_router(downloads.public)
    app.include_router(hostauth.router)
    app.include_router(pages.router)
    app.mount("/static", StaticFiles(directory=str(pages.STATIC_DIR), check_dir=False), name="static")
    if cfg.storage == "local":
        app.mount("/public", StaticFiles(directory=str(cfg.public_dir)), name="public")
    return app


app = create_app()
