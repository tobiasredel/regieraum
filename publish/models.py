"""Datenklassen für Feed und Veröffentlichung.

Sie ergeben sich direkt aus den DB-Feldern der SPEC (Abschnitt 5) und kennen weder Datenbank noch HTTP.
Der Server baut sie mit `Show.from_dict(settings["show"])` und `FeedEpisode.from_row(row)`.

Schlüssel (`cover_key`, `audio_key`, `chapters_key`, `transcript_key`) sind Speicherschlüssel wie
`media/<guid>/<datei>.mp3`. `build_feed` macht daraus absolute URLs über `Storage.url`.
Ein Wert, der schon mit http:// oder https:// beginnt, bleibt unverändert.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from typing import Any, Mapping

from .naming import sibling_keys


@dataclass(frozen=True)
class Show:
    """`settings.show` aus der SPEC.

    title, author, description: Pflicht für den Feed. owner_name/owner_email: `itunes:owner`; Spotify prüft über
    die E-Mail die Inhaberschaft. language: ISO-Code, z. B. "de". category/subcategory: Apple-Kategorien auf
    Englisch (z. B. "News" / "News Commentary"). explicit: bool. link: Webseite der Show. copyright: Freitext.
    cover_key: Speicherschlüssel des Covers (JPEG oder PNG, quadratisch, 1400 bis 3000 px).
    feed_url: eigene Feed-Adresse (für atom:link und podcast:guid); sonst leitet build_feed sie aus dem Speicher ab.
    show_type: "episodic" (Standard) oder "serial".
    """

    title: str = ""
    author: str = ""
    description: str = ""
    owner_name: str = ""
    owner_email: str = ""
    language: str = "de"
    category: str = ""
    subcategory: str = ""
    explicit: bool = False
    link: str = ""
    copyright: str = ""
    cover_key: str = ""
    feed_url: str = ""
    show_type: str = "episodic"

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> "Show":
        d = d or {}
        names = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in names and v is not None}
        if "explicit" in kw:
            kw["explicit"] = bool(kw["explicit"])
        return cls(**kw)

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class FeedEpisode:
    """Eine veröffentlichte Folge (Zeile aus `episodes`, nur die Felder, die der Feed braucht).

    guid: unveränderlich, auch bei importierten Folgen. pub_date_ms: Veröffentlichungsdatum in Epoch-Millisekunden
    (`published_at`, bei geplanten Folgen `publish_at`). duration_ms: Länge der fertigen Datei.
    number/season: positive ganze Zahlen oder None. episode_type: "full", "trailer" oder "bonus".
    explicit: None heißt „wie die Show“. summary: Klartext. description_html: bereits bereinigtes HTML.
    audio_bytes/audio_mime: exakt für `enclosure length` und `type`. chapters_key/transcript_key: optional.
    legacy: Zusatzfelder importierter Folgen (`link`, `image_url`, `image_key`, `guid_is_permalink`, `title_itunes`).
    itunes_title: Titel ohne Nummer für `<itunes:title>`, wenn `title` ein Präfix der Nummerierung trägt (sonst leer).
    imported: True bei Folgen aus dem Import (GUID-Attribut isPermaLink="false" gilt für alle, siehe SPEC).
    """

    guid: str
    title: str
    audio_key: str
    audio_bytes: int
    audio_mime: str = "audio/mpeg"
    pub_date_ms: int | None = None
    duration_ms: int | None = None
    summary: str = ""
    description_html: str = ""
    number: int | None = None
    season: int | None = None
    episode_type: str = "full"
    explicit: bool | None = None
    chapters_key: str | None = None
    transcript_key: str | None = None
    legacy: Mapping[str, Any] = field(default_factory=dict)
    itunes_title: str = ""

    @classmethod
    def from_row(cls, row: Any) -> "FeedEpisode":
        """Aus einer DB-Zeile (sqlite3.Row oder dict). `legacy` darf als JSON-Text kommen.
        `chapters_key` und `transcript_key` stehen nicht in der Tabelle `episodes`. Bei selbst aufgenommenen Folgen
        (`source == "recorded"`) werden sie aus `audio_key` abgeleitet (`publish.naming.sibling_keys`), weil
        `upload_episode_assets` alle drei Dateien unter demselben Namensstamm ablegt. `publish_feed` lässt Verweise
        auf Dateien weg, die im Speicher nicht existieren. Stehen die Schlüssel in der Zeile, gelten sie."""
        d = dict(row) if not isinstance(row, dict) else row
        legacy = d.get("legacy") or {}
        if isinstance(legacy, (str, bytes)):
            legacy = json.loads(legacy or "{}")
        pub = d.get("published_at") or d.get("publish_at") or d.get("pub_date_ms")
        explicit = d.get("explicit")
        chapters_key, transcript_key = d.get("chapters_key"), d.get("transcript_key")
        if d.get("source") == "recorded" and not (chapters_key or transcript_key):
            chapters_key, transcript_key = sibling_keys(d["audio_key"])
        return cls(
            guid=d["guid"],
            title=d.get("title") or "",
            audio_key=d["audio_key"],
            audio_bytes=int(d["audio_bytes"]),
            audio_mime=d.get("audio_mime") or "audio/mpeg",
            pub_date_ms=int(pub) if pub is not None else None,
            duration_ms=int(d["duration_ms"]) if d.get("duration_ms") is not None else None,
            summary=d.get("summary") or "",
            description_html=d.get("description_html") or "",
            number=d.get("number") or None,
            season=d.get("season") or None,
            episode_type=d.get("episode_type") or "full",
            explicit=None if explicit is None else bool(explicit),
            chapters_key=chapters_key,
            transcript_key=transcript_key,
            legacy=legacy,
            itunes_title=d.get("itunes_title") or "",
        )


@dataclass
class ImportedEpisode:
    """Ergebnis je Folge des Imports; `to_dict()` liefert genau die Felder aus SPEC Abschnitt 10."""

    guid: str
    title: str
    description_html: str
    summary: str
    pub_date_ms: int | None
    duration_ms: int | None
    number: int | None
    season: int | None
    episode_type: str
    explicit: bool
    audio_key: str
    audio_bytes: int
    audio_mime: str
    legacy: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass
class ImportResult:
    """Ergebnis von `import_feed`.

    show: Felder von `settings.show` (inkl. `cover_key`). episodes: neu übernommene Folgen (Liste von Dicts, Schlüssel
    siehe ImportedEpisode). skipped_guids: übersprungen, weil schon vorhanden. errors: Folgen, die nicht geladen werden
    konnten (`{guid, title, message}`); ein erneuter Lauf holt sie nach. warnings: deutsche Hinweise.
    feed_episode_count: Anzahl der Folgen im Quell-Feed.
    """

    show: dict[str, Any] = field(default_factory=dict)
    episodes: list[dict[str, Any]] = field(default_factory=list)
    skipped_guids: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    feed_episode_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "show": self.show,
            "episodes": self.episodes,
            "skipped_guids": self.skipped_guids,
            "errors": self.errors,
            "warnings": self.warnings,
            "feed_episode_count": self.feed_episode_count,
            "ok": self.ok,
        }
