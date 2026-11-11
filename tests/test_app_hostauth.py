"""Eingebaute Host-Anmeldung (PS_HOST_AUTH=password)."""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import stat

import pytest
from fastapi.testclient import TestClient

PW = "geheim-1234"


def make_client(cfg, **changes):
    from app.main import create_app

    app = create_app(dataclasses.replace(cfg, **changes))
    return TestClient(app, follow_redirects=False)


@pytest.fixture()
def pcfg(cfg):
    return dataclasses.replace(cfg, host_auth="password", host_password=PW)


@pytest.fixture()
def pclient(pcfg):
    from app.main import create_app

    with TestClient(create_app(pcfg), follow_redirects=False) as c:
        yield c


def login(c, password=PW, **form):
    return c.post("/login", data={"password": password, **form})


def test_config_modes(monkeypatch, cfg):
    from app.config import Config

    monkeypatch.delenv("PS_HOST_AUTH")
    assert Config.from_env().host_auth == "password"
    monkeypatch.setenv("PS_HOST_AUTH", "proxy")
    assert Config.from_env().host_auth == "proxy"
    monkeypatch.setenv("PS_HOST_AUTH", "quatsch")
    with pytest.raises(ValueError, match="PS_HOST_AUTH"):
        Config.from_env()


def test_protected_page_redirects_to_login(pclient):
    r = pclient.get("/studio")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Fstudio"
    r = pclient.get("/episodes/abc?x=1&y=2")
    assert r.headers["location"] == "/login?next=%2Fepisodes%2Fabc%3Fx%3D1%26y%3D2"
    assert pclient.head("/settings").status_code == 303
    assert pclient.get("/").status_code == 303


def test_protected_api_is_401_json(pclient):
    r = pclient.get("/api/host/settings")
    assert r.status_code == 401
    assert r.json() == {"error": {"code": "unauthorized", "message": "Bitte anmelden"}}
    assert pclient.post("/api/host/session").status_code == 401


def test_unknown_paths_are_protected(pclient):
    assert pclient.get("/irgendwas-neues").status_code == 303
    assert pclient.get("/api/neu").status_code == 401
    assert pclient.get("/joinx").status_code == 303
    assert pclient.get("/api/joiner").status_code == 401
    assert pclient.get("/api/p").status_code == 401


def test_public_paths_reachable(pclient):
    assert pclient.get("/healthz").json() == {"ok": True}
    assert pclient.get("/join/abc").status_code == 200
    assert pclient.get("/static/css/app.css").status_code == 200
    assert pclient.get("/api/join/gibtsnicht").status_code == 404
    assert pclient.post("/api/join", json={"name": ""}).status_code == 422
    assert pclient.get("/api/p/me").json()["error"]["code"] == "unauthorized"
    assert pclient.get("/login").status_code == 200
    assert pclient.get("/public/feed.xml").status_code in (200, 404)


def test_login_page_fields(pclient):
    html = pclient.get("/login").text
    assert 'type="password"' in html and 'autocomplete="current-password"' in html
    assert ">Passwort</label>" in html and ">Anmelden</button>" in html


def test_wrong_password_401(pclient):
    r = login(pclient, "falsch")
    assert r.status_code == 401 and "Das Passwort stimmt nicht" in r.text
    assert "set-cookie" not in r.headers


def test_login_sets_cookie_and_redirects(pclient):
    r = login(pclient, next="/episodes?a=1")
    assert r.status_code == 303 and r.headers["location"] == "/episodes?a=1"
    sc = r.headers["set-cookie"]
    assert sc.startswith("ps_host=")
    low = sc.lower()
    assert "httponly" in low and "samesite=lax" in low and "path=/" in low and "secure" not in low
    assert "max-age=2592000" in low
    value = sc.split("=", 1)[1].split(";", 1)[0]
    exp, mac = value.split(".")
    assert exp.isdigit() and len(mac) == 64 and int(mac, 16) >= 0
    assert pclient.get("/api/host/settings").status_code == 200
    assert pclient.get("/studio").status_code == 200


def test_cookie_secure_on_https(cfg):
    c = make_client(cfg, host_auth="password", host_password=PW, base_url="https://studio.example")
    r = login(c)
    assert "secure" in r.headers["set-cookie"].lower()


@pytest.mark.parametrize("nxt", ["https://evil.example", "//evil.example", "/\\evil", "evil", "", "/a\nb", "/\\\\x"])
def test_external_next_becomes_studio(pclient, nxt):
    r = login(pclient, next=nxt)
    assert r.status_code == 303 and r.headers["location"] == "/studio"


def test_secret_file_created_0600(pclient, pcfg):
    login(pclient)
    p = pcfg.data / "host_secret"
    assert p.is_file() and len(p.read_bytes()) == 32
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_forged_and_expired_cookies_rejected(pclient, pcfg, clock):
    exp = clock.now() // 1000 + 3600
    login(pclient)
    pclient.cookies.clear()
    key = hashlib.sha256((pcfg.data / "host_secret").read_bytes() + PW.encode()).digest()

    def signed(e):
        return f"{e}.{hmac.new(key, str(e).encode(), hashlib.sha256).hexdigest()}"

    pclient.cookies.set("ps_host", signed(exp))
    assert pclient.get("/api/host/settings").status_code == 200  # Gegenprobe: so sieht ein gültiges Cookie aus
    for bad in (signed(exp)[:-1] + "0" if not signed(exp).endswith("0") else signed(exp)[:-1] + "1",
                f"{exp + 99999}.{signed(exp).split('.')[1]}", "x.y", "", signed(clock.now() // 1000 - 1)):
        pclient.cookies.clear()
        pclient.cookies.set("ps_host", bad)
        assert pclient.get("/api/host/settings").status_code == 401, bad


def test_cookie_expires_after_30_days(pclient, clock):
    login(pclient)
    assert pclient.get("/api/host/settings").status_code == 200
    clock.advance(30 * 24 * 3600 * 1000 + 2000)
    assert pclient.get("/api/host/settings").status_code == 401


def test_changed_password_invalidates_cookies(pclient, pcfg):
    login(pclient)
    cookie = pclient.cookies.get("ps_host")
    other = make_client(pcfg, host_password="ein-anderes-passwort")
    other.cookies.set("ps_host", cookie)
    assert other.get("/api/host/settings").status_code == 401
    same = make_client(pcfg)
    same.cookies.set("ps_host", cookie)
    assert same.get("/api/host/settings").status_code == 200


def test_logout(pclient):
    login(pclient)
    r = pclient.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert 'ps_host=""' in r.headers["set-cookie"] or "max-age=0" in r.headers["set-cookie"].lower()
    assert pclient.get("/api/host/settings").status_code == 401


def test_logout_button_only_in_password_mode(pclient, cfg):
    login(pclient)
    assert 'action="/logout"' in pclient.get("/studio").text
    assert "Abmelden" in pclient.get("/studio").text
    none = make_client(cfg)
    assert "/logout" not in none.get("/studio").text


def test_brute_force_brake(pclient, clock):
    for _ in range(10):
        assert login(pclient, "falsch").status_code == 401
    r = login(pclient, "falsch")
    assert r.status_code == 429 and "Zu viele Versuche" in r.text
    assert login(pclient).status_code == 429  # auch das richtige Passwort bleibt gesperrt
    clock.advance(601_000)
    assert login(pclient).status_code == 303


def test_success_resets_counter(pclient):
    for _ in range(9):
        login(pclient, "falsch")
    assert login(pclient).status_code == 303
    for _ in range(9):
        assert login(pclient, "falsch").status_code == 401


@pytest.mark.parametrize("pw", ["", "kurz", "1234567"])
def test_short_password_gives_503(cfg, pw):
    c = make_client(cfg, host_auth="password", host_password=pw)
    r = c.get("/api/host/settings")
    assert r.status_code == 503 and r.json()["error"]["code"] == "setup"
    assert "PS_HOST_PASSWORD" in r.json()["error"]["message"] and "8" in r.json()["error"]["message"]
    r = c.get("/studio")
    assert r.status_code == 503 and "PS_HOST_PASSWORD" in r.text
    assert c.get("/login").status_code == 503
    assert login(c, pw).status_code == 503
    assert c.get("/healthz").status_code == 200
    assert c.get("/join/abc").status_code == 200
    assert c.get("/static/css/app.css").status_code == 200


def test_websocket_needs_cookie_except_public(pclient):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with pclient.websocket_connect("/ws/host"):
            pass
    login(pclient)
    # mit Cookie greift die Anmeldung nicht mehr (die App kennt den Pfad nicht, das Ergebnis ist ein Abbruch ohne 1008)
    with pytest.raises(WebSocketDisconnect) as e:
        with pclient.websocket_connect("/ws/host"):
            pass
    assert e.value.code != 1008


def test_public_websocket_path_not_blocked_by_login(pclient):
    # /ws/p ist ohne Cookie erreichbar: die Raum-Logik lehnt selbst ab (kein Token), nicht die Anmeldung
    with pclient.websocket_connect("/ws/p") as ws:
        assert ws.receive_json()["code"] == "unauthorized"


def test_proxy_mode_passes_everything(cfg):
    c = make_client(cfg, host_auth="proxy", host_password="")
    assert c.get("/api/host/settings").status_code == 200
    assert c.get("/studio").status_code == 200
    r = c.get("/login")
    assert r.status_code == 303 and r.headers["location"] == "/studio"
    assert "/logout" not in c.get("/studio").text


def test_none_mode_passes_everything_and_warns(cfg, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="podcast"):
        c = make_client(cfg, host_auth="none")
    assert any("PS_HOST_AUTH=none" in m for m in caplog.messages)
    assert c.get("/api/host/settings").status_code == 200
