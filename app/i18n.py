"""Sprache der Oberfläche auf der Serverseite (Fehlermeldungen, Vorlagen, Statustexte).

Deutsch ist die Quellsprache: Der deutsche Text ist der Schlüssel. Für andere Sprachen liegen Wörterbücher unter
`app/static/i18n/<sprache>.<bereich>.json` (deutscher Text → Übersetzung); dieselben Dateien lädt der Browser
(`app/static/js/i18n.js`). Fehlt ein Eintrag, bleibt der deutsche Text stehen. Platzhalter in geschweiften Klammern:
`tr("en", "Folge {n} nicht gefunden", n=3)`.

Welche Sprache gilt, bestimmt die Installation: Einstellung `ui.language` (Seite „Einstellungen“), sonst die
Umgebungsvariable `PS_LANG`, sonst Deutsch.

Meldungen mit Zahlen oder Namen („3 Folgen übernommen, 2 schon vorhanden“) brauchen keinen eigenen Mechanismus: Ein
Wörterbucheintrag, dessen deutscher Text Platzhalter enthält, gilt zugleich als Muster. Findet `tr` keinen exakten
Treffer, passt es den Text gegen diese Muster (`{n}` steht für beliebigen Text) und setzt das Gefundene in die
Übersetzung ein. So lassen sich auch fertig formatierte deutsche Meldungen aus der Datenbank übersetzen. Ein Platzhalter
namens `{detail}` steht für eine eingebettete Meldung und wird selbst übersetzt.

Die Sprache der Oberfläche (`ui_lang`) ist nicht die Sprache der Inhalte: Dossiers, Karten, Shownotes und Mitschrift
folgen der Einstellung `show.language` (`content_lang`, `show_lang`).
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

LANGUAGES = ("de", "en")
I18N_DIR = Path(__file__).parent / "static" / "i18n"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def default_lang() -> str:
    lang = (os.environ.get("PS_LANG") or "de").strip().lower()[:2]
    return lang if lang in LANGUAGES else "de"


def ui_lang(db) -> str:
    """Sprache der Oberfläche dieser Installation."""
    try:
        lang = (db.get_setting("ui", {}) or {}).get("language")
    except Exception:  # noqa: BLE001 - ohne Datenbank gilt die Voreinstellung
        lang = None
    return lang if lang in LANGUAGES else default_lang()


@lru_cache(maxsize=8)
def _dict(lang: str, stamp: float) -> dict:
    out: dict = {}
    for path in sorted(I18N_DIR.glob(f"{lang}.*.json")):
        try:
            out.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def dictionary(lang: str) -> dict:
    """Alle Wörterbücher einer Sprache, zusammengeführt. Geänderte Dateien werden ohne Neustart neu gelesen."""
    if lang == "de":
        return {}
    try:
        stamp = max((p.stat().st_mtime for p in I18N_DIR.glob(f"{lang}.*.json")), default=0.0)
    except OSError:
        stamp = 0.0
    return _dict(lang, stamp)


def content_lang(value) -> str:
    """Zweibuchstabiger Sprachcode aus `show.language` („de“, „de-DE“, „en_GB“ …); sonst „de“."""
    m = re.match(r"\s*([A-Za-z]{2})(?![A-Za-z])", str(value or ""))
    return m.group(1).lower() if m else "de"


def show_lang(db) -> str:
    """Sprache der Inhalte dieser Show (Einstellung `show.language`)."""
    try:
        return content_lang((db.get_setting("show", {}) or {}).get("language"))
    except Exception:  # noqa: BLE001
        return "de"


@lru_cache(maxsize=8)
def _patterns(lang: str, stamp: float) -> tuple:
    """Einträge mit Platzhaltern als Muster: (Regex, Übersetzung), die mit dem meisten festen Text zuerst."""
    out = []
    for de, en in _dict(lang, stamp).items():
        names = _PLACEHOLDER.findall(de)
        if not names or len(set(names)) != len(names):
            continue
        parts = _PLACEHOLDER.split(de)  # Text, Name, Text, Name, …, Text
        fixed = sum(len(p) for p in parts[0::2])
        if fixed < 3:
            continue
        rx = "".join(re.escape(p) if i % 2 == 0 else f"(?P<{p}>.+?)" for i, p in enumerate(parts))
        out.append((fixed, re.compile(rx, re.S), en))
    out.sort(key=lambda x: -x[0])
    return tuple((rx, en) for _, rx, en in out)


def _stamp(lang: str) -> float:
    try:
        return max((p.stat().st_mtime for p in I18N_DIR.glob(f"{lang}.*.json")), default=0.0)
    except OSError:
        return 0.0


def _fill(lang: str, template: str, values: dict) -> str:
    def one(m):
        name = m.group(1)
        if name not in values:
            return m.group(0)
        v = str(values[name])
        return tr(lang, v) if name == "detail" else v

    return _PLACEHOLDER.sub(one, template)


def tr(lang: str, text: str, **params) -> str:
    """Text in der Sprache `lang`; `text` ist der deutsche Text (mit Platzhaltern oder schon fertig formatiert).
    Unbekanntes bleibt deutsch."""
    if lang == "de":
        return _fill(lang, text, params) if params else text
    entries = dictionary(lang)
    if text in entries:
        return _fill(lang, entries[text], params) if params else entries[text]
    for rx, en in _patterns(lang, _stamp(lang)):
        m = rx.fullmatch(text)
        if m:
            return _fill(lang, en, {**params, **m.groupdict()})
    return _fill(lang, text, params) if params else text
