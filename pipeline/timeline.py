"""Zeitrechnung zwischen Sitzungszeit und Folgenzeit (nach dem Schnitt) und Kapitel.

Die Schnittliste (EDL) ist `{trim_start_ms, trim_end_ms, cuts: [{from_ms, to_ms}]}` in Sitzungszeit. Behalten wird
`[trim_start_ms, trim_end_ms)` ohne die Schnitte. `trim_end_ms = None` heißt „bis zum Ende der Sitzung“.
Diese Funktionen nutzt auch der Server; sie dürfen nichts außer der Standardbibliothek brauchen.
"""
from __future__ import annotations

import math

CHAPTER_DEFAULTS = {"intro": "Intro", "outro": "Outro"}
CHAPTER_KINDS = ("intro", "outro", "segment")


def _num(v, default):
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def normalize_edl(edl: dict | None, total_ms: float | None = None) -> tuple[float, float | None, list[tuple[float, float]]]:
    """→ (trim_start, trim_end oder None, Schnitte sortiert, verschmolzen und auf den Trimmbereich begrenzt)."""
    edl = edl or {}
    ts = max(0.0, _num(edl.get("trim_start_ms"), 0.0))
    te = _num(edl.get("trim_end_ms"), None)
    if total_ms is not None:
        te = total_ms if te is None else min(te, total_ms)
    if te is not None and te < ts:
        te = ts
    cuts = []
    for c in edl.get("cuts") or []:
        a, b = _num(c.get("from_ms"), None), _num(c.get("to_ms"), None)
        if a is None or b is None:
            continue
        a, b = max(a, ts), (b if te is None else min(b, te))
        if b > a:
            cuts.append([a, b])
    cuts.sort()
    merged: list[list[float]] = []
    for a, b in cuts:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return ts, te, [(a, b) for a, b in merged]


def segments(edl: dict | None, total_ms: float | None = None) -> list[tuple[float, float | None]]:
    """Behaltene Bereiche in Sitzungszeit, aufsteigend. Das Ende des letzten ist None, wenn offen."""
    ts, te, cuts = normalize_edl(edl, total_ms)
    out = []
    pos = ts
    for a, b in cuts:
        if a > pos:
            out.append((pos, a))
        pos = b
    if te is None or te > pos:
        out.append((pos, te))
    return out


def _ret(x: float, like) -> float | int:
    return int(round(x)) if isinstance(like, int) else x


def session_to_episode(ms, edl: dict | None):
    """Sitzungszeit → Folgenzeit. None, wenn der Zeitpunkt herausgeschnitten ist oder außerhalb liegt.

    Das Ende des letzten behaltenen Bereichs bildet auf das Ende der Folge ab.
    """
    t = float(ms)
    off = 0.0
    segs = segments(edl)
    for i, (a, b) in enumerate(segs):
        if t < a:
            return None
        if b is None or t < b or (i == len(segs) - 1 and t == b):
            return _ret(off + t - a, ms)
        off += b - a
    return None


def episode_to_session(ms, edl: dict | None):
    """Folgenzeit → Sitzungszeit. Eine Schnittkante bildet auf den Anfang des folgenden Bereichs ab; Werte über das Ende
    hinaus werden auf das Ende begrenzt, negative auf den Anfang."""
    t = max(0.0, float(ms))
    off = 0.0
    segs = segments(edl)
    for a, b in segs:
        if b is None or t < off + (b - a):
            return _ret(a + (t - off), ms)
        off += b - a
    a, b = segs[-1] if segs else (0.0, 0.0)
    return _ret(b if b is not None else a, ms)


def episode_duration_ms(edl: dict | None, session_ms: float):
    """Länge der Folge nach dem Schnitt (ganzzahlig, wenn session_ms ganzzahlig ist)."""
    return _ret(sum((b - a) for a, b in segments(edl, session_ms)), session_ms)


def snap_to_episode(ms, edl: dict | None, session_ms: float | None = None):
    """Wie session_to_episode, aber Zeitpunkte in Schnitten rücken an die Schnittkante (Anfang des nächsten Bereichs),
    Zeitpunkte vor dem Anfang auf 0. None nur, wenn der Zeitpunkt hinter dem Ende liegt."""
    t = float(ms)
    off = 0.0
    for a, b in segments(edl, session_ms):
        if t < a:
            return _ret(off, ms)
        if b is None or t < b:
            return _ret(off + t - a, ms)
        off += b - a
    return None


def session_duration_ms(manifest: dict) -> float:
    """Länge der Sitzung: von rec.start bis zum späteren von Stop und Ende des letzten Sounds."""
    end = float(manifest.get("rec_stop_server_ms") or 0) - float(manifest.get("rec_start_server_ms") or 0)
    for s in manifest.get("sounds") or []:
        e = sound_end_ms(s)
        if e is not None:
            end = max(end, e)
    return max(0.0, end)


def sound_end_ms(s: dict) -> float | None:
    at = _num(s.get("at_ms"), None)
    if at is None:
        return None
    dur = _num(s.get("duration_ms"), None)
    end = at + dur if dur is not None else None
    if s.get("stop_ms") is not None:
        stop = _num(s.get("stop_ms"), None)
        if stop is not None:
            fade_end = stop + max(0.0, _num(s.get("fade_ms"), 0.0))
            end = fade_end if end is None else min(end, fade_end)
    return end


def chapters(manifest: dict) -> list[dict]:
    """Kapitel in Folgenzeit: [{startTime (Sekunden), title, event_id}], aufsteigend.

    Quellen: Sounds der Art intro, outro, segment (Titel aus `chapter_title`, sonst Vorgabe je Art) und Kapitel-Marker
    (Titel `label`; Merkpunkte zählen nicht). Kapitel in herausgeschnittenen Bereichen rücken an die Schnittkante, solche hinter dem Ende
    entfallen; bei gleicher Startzeit bleibt das zuerst geklickte.
    """
    edl = manifest.get("edl") or {}
    total = session_duration_ms(manifest) if manifest.get("rec_stop_server_ms") is not None else None
    items = []
    for s in manifest.get("sounds") or []:
        # Ein Zwischenton (sting) bekommt nur dann ein Kapitel, wenn der Server ihm einen Titel mitgibt
        # (er hat dann den Sendeplan auf einen Abschnitt weitergeschaltet).
        if s.get("kind") not in CHAPTER_KINDS and not s.get("chapter_title"):
            continue
        title = s.get("chapter_title") or CHAPTER_DEFAULTS.get(s.get("kind")) or s.get("label") or "Kapitel"
        items.append((_num(s.get("at_ms"), 0.0), str(title), s.get("event_id")))
    for m in manifest.get("markers") or []:
        if m.get("kind") == "note":  # Merkpunkte sind keine Kapitel (der Server liefert sie getrennt unter `notes`)
            continue
        items.append((_num(m.get("at_ms"), 0.0), str(m.get("label") or "Marker"), m.get("event_id")))
    items.sort(key=lambda x: x[0])
    dur = episode_duration_ms(edl, total) if total is not None else None
    out, seen = [], set()
    for at, title, event_id in items:
        t = snap_to_episode(at, edl, total)
        if t is None or (dur is not None and t >= dur and dur > 0):
            continue
        t = int(round(t))
        if t in seen:
            continue
        seen.add(t)
        out.append({"startTime": t / 1000, "title": title, "event_id": event_id})
    out.sort(key=lambda c: c["startTime"])
    return out
