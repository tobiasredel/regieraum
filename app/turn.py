"""ICE-Server: STUN immer, TURN je nach PS_TURN_MODE (none | static | cloudflare)."""
from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from .config import Config

log = logging.getLogger("podcast.turn")
STUN = {"urls": ["stun:stun.cloudflare.com:3478"]}
CF_URL = "https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers"
CF_TTL_S = 12 * 3600
CACHE_S = 3600  # kürzer als die Gültigkeit, damit ausgelieferte Zugangsdaten noch lange genug halten

_PORT53 = re.compile(r":53(\?|$)")
_cache: dict = {"until": 0.0, "servers": []}
_lock = asyncio.Lock()


async def ice_servers(cfg: Config) -> list[dict]:
    servers = [STUN]
    if cfg.turn_mode == "static" and cfg.turn_urls:
        servers.append({"urls": list(cfg.turn_urls), "username": cfg.turn_username, "credential": cfg.turn_credential})
    elif cfg.turn_mode == "cloudflare" and cfg.cf_turn_key_id and cfg.cf_turn_api_token:
        servers += await _cloudflare(cfg)
    return servers


async def _cloudflare(cfg: Config) -> list[dict]:
    async with _lock:
        if _cache["until"] > time.monotonic() and _cache.get("key") == cfg.cf_turn_key_id:
            return _cache["servers"]
        try:
            async with httpx.AsyncClient(timeout=6) as client:
                r = await client.post(
                    CF_URL.format(key_id=cfg.cf_turn_key_id),
                    headers={"Authorization": f"Bearer {cfg.cf_turn_api_token}", "Content-Type": "application/json"},
                    json={"ttl": CF_TTL_S},
                )
                r.raise_for_status()
                data = r.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("Cloudflare-TURN nicht erreichbar: %s", exc)
            return []
        turn = []
        for entry in data.get("iceServers", []):
            if not entry.get("username"):
                continue  # den STUN-Eintrag liefern wir selbst
            urls = [u for u in entry.get("urls", []) if not _PORT53.search(u)]  # Port 53 sperren Browser
            turn.append({**entry, "urls": urls})
        _cache.update(until=time.monotonic() + CACHE_S, servers=turn, key=cfg.cf_turn_key_id)
        return turn


def reset_cache() -> None:
    _cache.update(until=0.0, servers=[])
