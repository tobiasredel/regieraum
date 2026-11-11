"""Sprache der Oberfläche: Wörterbücher sind vollständig, die Einstellung wirkt."""
from __future__ import annotations

import json
import re
from pathlib import Path

from app import i18n

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "app" / "static" / "js"
TEMPLATES = ROOT / "app" / "templates"
# t("…"), t('…') oder t(`…`) mit festem ersten Argument; \\b verhindert Treffer wie format("…")
CALL = re.compile(r"""(?<![\w.$])t\(\s*(["'`])((?:\\.|(?!\1).)+?)\1""", re.S)


def _keys(text: str) -> set[str]:
    out = set()
    for quote, body in CALL.findall(text):
        if quote == "`" and "${" in body:
            continue  # zusammengesetzte Texte sind kein Schlüssel; dafür gibt es Platzhalter
        out.add(bytes(body, "utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8") if "\\" in body else body)
    return out


def used_keys() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in list(JS.rglob("*.js")) + list(TEMPLATES.glob("*.html")):
        if path.name == "i18n.js":
            continue  # nur Beispiele im Kommentar
        keys = _keys(path.read_text(encoding="utf-8"))
        if keys:
            found[str(path.relative_to(ROOT))] = keys
    return found


def test_every_ui_text_has_an_english_entry():
    en = i18n.dictionary("en")
    missing = {f: sorted(k for k in keys if k not in en) for f, keys in used_keys().items()}
    missing = {f: ks for f, ks in missing.items() if ks}
    assert not missing, json.dumps(missing, ensure_ascii=False, indent=1)[:3000]


def test_dictionaries_are_valid_and_keep_placeholders():
    for path in sorted((ROOT / "app" / "static" / "i18n").glob("en.*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), path.name
        for de, en in data.items():
            assert isinstance(en, str) and en.strip(), f"{path.name}: leere Übersetzung für {de!r}"
            assert set(re.findall(r"\{(\w+)\}", de)) == set(re.findall(r"\{(\w+)\}", en)), f"{path.name}: Platzhalter weichen ab bei {de!r}"


def test_tr_falls_back_and_fills_placeholders():
    assert i18n.tr("de", "Hallo {name}", name="Anna") == "Hallo Anna"
    assert i18n.tr("en", "Diesen Text gibt es in keinem Wörterbuch {n}", n=3) == "Diesen Text gibt es in keinem Wörterbuch 3"


def test_ui_language_setting(client, monkeypatch):
    assert client.get("/api/host/settings").json()["ui"] == {"language": "de"}
    assert 'lang="de"' in client.get("/studio").text
    assert client.put("/api/host/settings", json={"ui": {"language": "en"}}).json()["ui"] == {"language": "en"}
    assert 'lang="en"' in client.get("/studio").text
    assert client.put("/api/host/settings", json={"ui": {"language": "fr"}}).status_code == 422


def test_same_key_has_same_translation_in_every_dictionary():
    """Die Bereichsdateien werden zusammengeführt; ein Schlüssel darf nicht zwei verschiedene Übersetzungen haben."""
    seen: dict[str, tuple[str, str]] = {}
    clashes = []
    for path in sorted((ROOT / "app" / "static" / "i18n").glob("en.*.json")):
        for de, en in json.loads(path.read_text(encoding="utf-8")).items():
            if de in seen and seen[de][1] != en:
                clashes.append(f"{de!r}: {seen[de][0]} sagt {seen[de][1]!r}, {path.name} sagt {en!r}")
            seen.setdefault(de, (path.name, en))
    assert not clashes, "\n".join(clashes)
