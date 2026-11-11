"""Zugang, Rollen, Einladungen, Beitritt."""
from __future__ import annotations

import pytest

from .app_util import auth, host_login, join_guest


def test_healthz_and_default_invite(client):
    assert client.get("/healthz").json() == {"ok": True}
    invites = client.get("/api/host/invites").json()
    assert [i["label"] for i in invites if i["kind"] == "permanent"] == ["Co-Host"]
    assert invites[0]["url"].startswith("http://testserver/join/")


def test_join_roles_and_token_hashed(client, app):
    token, p = join_guest(client, "Max")
    assert p["role"] == "guest"
    assert client.get("/api/p/me", headers=auth(token)).json()["name"] == "Max"
    row = app.state.db.one("SELECT token_hash FROM participants WHERE id=?", (p["id"],))
    assert row["token_hash"] != token and len(row["token_hash"]) == 64
    assert client.get("/api/p/me").status_code == 401
    assert client.get("/api/p/me", headers=auth("falsch")).json()["error"]["code"] == "unauthorized"


def test_host_session_reuse(client):
    t1, p1 = host_login(client)
    assert p1["role"] == "host"
    r = client.post("/api/host/session", json={"participant_token": t1}).json()
    assert r["participant_token"] == t1


def test_guest_token_gets_no_host_rights(client):
    token, _ = join_guest(client)
    for method, path in [("get", "/api/host/settings"), ("get", "/api/host/episodes"), ("post", "/api/host/invites")]:
        r = getattr(client, method)(path, headers=auth(token))
        assert r.status_code == 403, path
        assert r.json()["error"]["code"] == "forbidden"
    # auch kein Host-Token über die Gast-Sitzung erschleichen
    r = client.post("/api/host/session", headers=auth(token))
    assert r.status_code == 403


def test_single_invite_used_once_and_identity_kept(client):
    inv = client.post("/api/host/invites", json={"label": "Gast", "kind": "single"}).json()
    assert client.get(f"/api/join/{inv['token']}").json()["valid"] is True
    r = client.post("/api/join", json={"invite_token": inv["token"], "name": "Anna"})
    token = r.json()["participant_token"]
    again = client.post("/api/join", json={"invite_token": inv["token"], "name": "Anna", "participant_token": token})
    assert again.json()["participant_token"] == token
    other = client.post("/api/join", json={"invite_token": inv["token"], "name": "Berta"})
    assert other.status_code == 410 and other.json()["error"]["code"] == "invite_used"


def test_single_invite_expires_after_48h(client, clock):
    inv = client.post("/api/host/invites", json={"label": "Gast", "kind": "single"}).json()
    assert inv["expires_at"] == clock.now() + 48 * 3600 * 1000
    clock.advance(48 * 3600 * 1000 + 1)
    r = client.post("/api/join", json={"invite_token": inv["token"], "name": "Anna"})
    assert r.status_code == 410 and r.json()["error"]["code"] == "invite_expired"


def test_revoked_invite(client):
    inv = client.post("/api/host/invites", json={"label": "Gast", "kind": "single"}).json()
    assert client.delete(f"/api/host/invites/{inv['id']}").json()["revoked_at"]
    r = client.post("/api/join", json={"invite_token": inv["token"], "name": "Anna"})
    assert r.status_code == 410 and r.json()["error"]["code"] == "invite_revoked"
    assert client.post("/api/join", json={"invite_token": "nix", "name": "A"}).status_code == 404


def test_error_shape_and_security_headers(client):
    r = client.get("/api/host/episodes/gibtsnicht")
    assert r.status_code == 404 and set(r.json()["error"]) == {"code", "message"}
    assert r.headers["cache-control"] == "no-store"
    csp = r.headers["content-security-policy"]
    for part in ("font-src 'self'", "media-src 'self' blob:", "worker-src 'self' blob:", "connect-src 'self' wss:"):
        assert part in csp
    assert client.post("/api/join", json={"name": ""}).status_code == 422


def test_pages_placeholders(client):
    assert client.get("/", follow_redirects=False).status_code == 307
    for path in ("/studio", "/episodes", "/sounds", "/settings", "/join/abc"):
        r = client.get(path)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]


@pytest.mark.parametrize("mode", ["none", "static"])
def test_ice(client, app, mode, monkeypatch):
    import dataclasses

    app.state.cfg = dataclasses.replace(app.state.cfg, turn_mode=mode, turn_urls=("turn:t.example:3478",),
                                        turn_username="u", turn_credential="c")
    token, _ = join_guest(client)
    servers = client.get("/api/p/ice", headers=auth(token)).json()["iceServers"]
    assert servers[0]["urls"] == ["stun:stun.cloudflare.com:3478"]
    assert (len(servers) == 2 and servers[1]["username"] == "u") == (mode == "static")


def test_ice_cloudflare(client, app, monkeypatch):
    import dataclasses

    import httpx

    from app import turn

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json={"iceServers": [
            {"urls": ["stun:stun.cloudflare.com:3478", "stun:stun.cloudflare.com:53"]},
            {"urls": ["turn:turn.cloudflare.com:3478?transport=udp", "turn:turn.cloudflare.com:53?transport=udp",
                      "turns:turn.cloudflare.com:5349?transport=tcp"], "username": "U", "credential": "C"}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(turn.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    turn.reset_cache()
    app.state.cfg = dataclasses.replace(app.state.cfg, turn_mode="cloudflare", cf_turn_key_id="KEY", cf_turn_api_token="TOK")
    token, _ = join_guest(client)
    for _ in range(2):
        servers = client.get("/api/p/ice", headers=auth(token)).json()["iceServers"]
    assert len(calls) == 1  # zwischengespeichert
    assert calls[0].url.path.endswith("/turn/keys/KEY/credentials/generate-ice-servers")
    assert calls[0].headers["authorization"] == "Bearer TOK"
    assert b'"ttl":43200' in calls[0].content.replace(b" ", b"")
    assert servers[1]["username"] == "U"
    assert all(":53?" not in u for u in servers[1]["urls"]) and len(servers[1]["urls"]) == 2
    turn.reset_cache()
