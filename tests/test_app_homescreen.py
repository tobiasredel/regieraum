"""Home-Bildschirm: Manifest nur für Host-Seiten, hinter der Anmeldung; Gäste installieren nichts."""
from __future__ import annotations

from .test_app_hostauth import login, pcfg, pclient  # noqa: F401  (Fixtures)


def test_manifest_content(client):
    client.put("/api/host/settings", json={"show": {"title": "Uebel und Veredelt"}})
    r = client.get("/app.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    m = r.json()
    assert m["name"] == "Uebel und Veredelt" and m["short_name"] == "Uebel und V…"
    assert m["display"] == "standalone" and m["start_url"] == "/studio"
    sizes = {i["sizes"] for i in m["icons"]}
    assert sizes == {"192x192", "512x512"}
    for icon in m["icons"]:
        assert client.get(icon["src"]).status_code == 200
    assert client.get("/static/icons/apple-touch-icon.png").status_code == 200


def test_manifest_only_on_host_pages(client):
    for path in ("/studio", "/plan", "/episodes", "/settings"):
        html = client.get(path).text
        assert 'rel="manifest"' in html and 'crossorigin="use-credentials"' in html, path
        assert 'rel="apple-touch-icon"' in html and 'name="apple-mobile-web-app-capable"' in html, path
    guest = client.get("/join/irgendein-token").text
    assert "manifest" not in guest and "apple-touch-icon" not in guest and "apple-mobile-web-app" not in guest


def test_manifest_behind_host_login(pclient):  # noqa: F811
    assert pclient.get("/app.webmanifest").status_code in (302, 303, 307, 401)
    assert 'rel="manifest"' not in pclient.get("/login").text
    login(pclient)
    assert pclient.get("/app.webmanifest").status_code == 200
