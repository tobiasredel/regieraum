"""Dossier je Abschnitt des Sendeplans (SPEC Abschnitt 14).

``research_async(segment, show, previous_titles, plan_context=None, on_progress=None) -> dict``
recherchiert mit Websuche und liefert ein Dossier mit belegten Fakten. Jede URL in ``fakten``, ``zahlen``,
``quellen``, ``werk`` und ``kandidaten`` muss aus einem Suchergebnis oder einer abgerufenen Seite dieser Recherche
stammen; alles andere verwirft der Code (Zählung in ``meta.verworfen``).
"""
from __future__ import annotations

import asyncio
import inspect
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import prompts
from .llm import (FALLBACK_BETA, WEB_FETCH_TOOL, WEB_SEARCH_TOOL, AssistError, add_usage, collect_sources,
                  describe_error, final_text, make_async_client, parse_json_text, usage_to_dict)
from .schemas import DOSSIER_SCHEMA
from .util import clip_text, extract_urls, normalize_url, usage_cost_usd

TZ = ZoneInfo("Europe/Berlin")
DEFAULT_MODEL = "claude-opus-5-5"

# Tiefe je Art: (max. Suchen, max. Abrufe, Effort, Grenzen je Liste)
DEPTH = {
    "blitzlicht": dict(searches=6, fetches=4, effort="medium",
                       limits=dict(fakten=5, zahlen=3, positionen=3, gegenargumente=2, fragen=3, aufhaenger=2, begriffe=12)),
    "hauptthema": dict(searches=15, fetches=10, effort="high",
                       limits=dict(fakten=12, zahlen=5, positionen=6, gegenargumente=5, fragen=8, aufhaenger=4, begriffe=25)),
    "good": dict(searches=6, fetches=5, effort="medium",
                 limits=dict(fakten=4, zahlen=3, positionen=3, gegenargumente=3, fragen=3, aufhaenger=2, begriffe=15)),
    "frei": dict(searches=8, fetches=6, effort="medium",
                 limits=dict(fakten=6, zahlen=3, positionen=4, gegenargumente=3, fragen=4, aufhaenger=2, begriffe=15)),
}
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MAX_CONTINUATIONS = 4
ROUND_TIMEOUT_S = 600.0


def _call_progress(cb, progress: float, message: str):
    if not cb:
        return
    try:
        r = cb(round(max(0.0, min(1.0, progress)), 2), message)
        if inspect.isawaitable(r):
            asyncio.ensure_future(r)
    except Exception:
        pass


def _tools(depth: dict) -> list:
    return [
        {"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": depth["searches"],
         "user_location": {"type": "approximate", "country": "DE", "timezone": "Europe/Berlin"}},
        {"type": WEB_FETCH_TOOL, "name": "web_fetch", "max_uses": depth["fetches"], "max_content_tokens": 12000},
    ]


def _block_label(block) -> str | None:
    """Fortschrittstext für einen Server-Werkzeugaufruf."""
    if getattr(block, "type", None) != "server_tool_use":
        return None
    inp = getattr(block, "input", None) or {}
    name = getattr(block, "name", "")
    if name == "web_search" and inp.get("query"):
        return f"Sucht: {inp['query']}"
    if name == "web_fetch" and inp.get("url"):
        return f"Liest: {inp['url']}"
    return None


async def _run_round(client, kwargs: dict, on_progress, state: dict):
    """Eine Anfrage als Stream; meldet Suchen und Abrufe als Fortschritt."""
    async with client.beta.messages.stream(**kwargs) as stream:
        async for event in stream:
            et = getattr(event, "type", None)
            if et == "content_block_stop":
                try:
                    block = stream.current_message_snapshot.content[event.index]
                except Exception:
                    continue
                label = _block_label(block)
                if label and label != state.get("last"):
                    state["last"] = label
                    state["steps"] += 1
                    frac = state["steps"] / max(1, state["budget"])
                    _call_progress(on_progress, 0.1 + 0.75 * min(1.0, frac), label)
            elif et == "content_block_start":
                cb = getattr(event, "content_block", None)
                if getattr(cb, "type", None) == "text" and not state.get("writing"):
                    state["writing"] = True
                    _call_progress(on_progress, 0.88, "Dossier wird geschrieben")
        return await stream.get_final_message()


def verify_dossier(data: dict, sources: dict, kind: str, *, good_empty: bool = False) -> tuple[dict, int]:
    """Nur Belegtes behalten. Liefert (Dossier, Zahl der verworfenen Einträge)."""
    limits = DEPTH.get(kind, DEPTH["frei"])["limits"]
    dropped = 0

    def ok(url):
        key = normalize_url(url)
        return sources.get(key) if key else None

    def strs(name):
        return [clip_text(s, 400) for s in (data.get(name) or []) if isinstance(s, str) and s.strip()][: limits[name]]

    fakten = []
    for f in data.get("fakten") or []:
        src = ok(f.get("quelle_url"))
        if not src or not (f.get("text") or "").strip():
            dropped += 1
            continue
        fakten.append({"text": clip_text(f["text"], 300), "quelle_titel": (f.get("quelle_titel") or src.title or "").strip(),
                       "quelle_url": src.url, "datum": (f.get("datum") or "").strip()})
    zahlen = []
    for z in data.get("zahlen") or []:
        src = ok(z.get("quelle_url"))
        if not src or not (z.get("text") or "").strip():
            dropped += 1
            continue
        zahlen.append({"text": clip_text(z["text"], 300), "quelle_url": src.url})
    positionen = []
    for p in data.get("positionen") or []:
        url = (p.get("quelle_url") or "").strip()
        src = ok(url) if url else None
        if url and not src:  # erfundene URL: Eintrag ist nicht vertrauenswürdig
            dropped += 1
            continue
        if (p.get("wer") or "").strip() and (p.get("position") or "").strip():
            positionen.append({"wer": p["wer"].strip(), "position": clip_text(p["position"], 400),
                               "quelle_url": src.url if src else ""})
    quellen, seen = [], set()

    def add_source(url, titel="", datum=""):
        src = ok(url)
        if not src:
            return False
        key = normalize_url(src.url)
        if key in seen:
            return True
        seen.add(key)
        quellen.append({"titel": (titel or src.title or "").strip(), "url": src.url, "datum": (datum or "").strip()})
        return True

    for q in data.get("quellen") or []:
        if not add_source(q.get("url"), q.get("titel"), q.get("datum")):
            dropped += 1
    for f in fakten:
        add_source(f["quelle_url"], f["quelle_titel"], f["datum"])
    for z in zahlen + positionen:
        if z.get("quelle_url"):
            add_source(z["quelle_url"])

    werk = None
    w = data.get("werk")
    if kind == "good" and isinstance(w, dict) and (w.get("titel") or "").strip():
        src = ok(w.get("url"))
        if w.get("url") and not src:
            dropped += 1
        werk = {"titel": w["titel"].strip(), "urheber": (w.get("urheber") or "").strip(), "typ": (w.get("typ") or "").strip(),
                "url": src.url if src else "", "erschienen": (w.get("erschienen") or "").strip(),
                "kurzfassung": clip_text(w.get("kurzfassung") or "", 700),
                "kernaussagen": [clip_text(k, 300) for k in (w.get("kernaussagen") or []) if k][:5],
                "bezug": clip_text(w.get("bezug") or "", 400)}
        if src:
            add_source(src.url, werk["titel"])
    kandidaten = []
    if kind == "good" and good_empty:
        for k in data.get("kandidaten") or []:
            src = ok(k.get("url"))
            if not src or not (k.get("titel") or "").strip():
                dropped += 1
                continue
            kandidaten.append({"titel": k["titel"].strip(), "urheber": (k.get("urheber") or "").strip(),
                               "typ": k.get("typ") if k.get("typ") in ("read", "watch", "listen") else "read",
                               "url": src.url, "warum": clip_text(k.get("warum") or "", 300)})
            if len(kandidaten) == 3:
                break

    begriffe, seen_b = [], set()
    for b in data.get("begriffe") or []:
        b = (b or "").strip()
        if b and b.lower() not in seen_b and len(b) <= 80:
            seen_b.add(b.lower())
            begriffe.append(b)
    out = {
        "lage": clip_text(data.get("lage") or "", 1000),
        "fakten": fakten[: limits["fakten"]],
        "zahlen": zahlen[: limits["zahlen"]],
        "positionen": positionen[: limits["positionen"]],
        "gegenargumente": strs("gegenargumente"),
        "fragen": strs("fragen"),
        "aufhaenger": strs("aufhaenger"),
        "begriffe": begriffe[: limits["begriffe"]],
        "quellen": quellen,
    }
    if kind == "good":
        out["werk"] = werk
        out["kandidaten"] = kandidaten
    return out, dropped


async def research_async(segment: dict, show: dict | None = None, previous_titles: list[str] | None = None,
                         plan_context: list[dict] | None = None, *, on_progress=None, client=None,
                         model: str | None = None, now: datetime | None = None) -> dict:
    """Dossier für einen Abschnitt. ``segment``: {id?, kind, title, input, good_type?}.

    ``plan_context``: die übrigen Abschnitte des Plans ({id, kind, title, input}), damit Good-Abschnitte den Bezug
    zum Hauptthema kennen. ``on_progress(progress 0..1, message)`` darf synchron oder asynchron sein.
    Fehler: ``AssistError`` mit deutschem Klartext.
    """
    t0 = time.monotonic()
    kind = segment.get("kind") if segment.get("kind") in DEPTH else "frei"
    inp = (segment.get("input") or "").strip()
    # Good mit leerer Eingabe: Kandidaten vorschlagen (SPEC 14); der Titel ist dann nur die Abschnittsbezeichnung
    good_empty = kind == "good" and not inp
    depth = DEPTH[kind]
    model = model or os.environ.get("PS_AI_MODEL_PREP") or DEFAULT_MODEL
    now = now or datetime.now(TZ)
    links = extract_urls(inp)
    depth_text = prompts.PREP_DEPTH["good_empty" if good_empty else kind]
    system = [
        {"type": "text", "text": prompts.PREP_SYSTEM, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": depth_text},
    ]
    today = f"{now.strftime('%Y-%m-%d')}, {WEEKDAYS[now.weekday()]}"
    user = prompts.prep_user_message(segment, show, previous_titles, plan_context, today, links)
    messages = [{"role": "user", "content": user}]
    if client is None:
        client = make_async_client(max_retries=2, timeout=ROUND_TIMEOUT_S)
    kwargs = dict(model=model, max_tokens=32000, system=system, tools=_tools(depth),
                  output_config={"effort": depth["effort"], "format": {"type": "json_schema", "schema": DOSSIER_SCHEMA}},
                  betas=[FALLBACK_BETA], fallbacks="default")
    _call_progress(on_progress, 0.05, "Recherche startet")
    state = {"steps": 0, "budget": depth["searches"] + min(depth["fetches"], 3), "writing": False}
    usage_total: dict = {}
    sources: dict = {}
    cost = 0.0
    msg = None
    try:
        for _ in range(MAX_CONTINUATIONS + 1):
            msg = await asyncio.wait_for(_run_round(client, dict(kwargs, messages=messages), on_progress, state),
                                         timeout=ROUND_TIMEOUT_S)
            u = usage_to_dict(msg.usage)
            usage_total = add_usage(usage_total, u)
            cost += usage_cost_usd(model, u)
            collect_sources(msg.content, sources)
            if msg.stop_reason != "pause_turn":
                break
            messages = messages + [{"role": "assistant", "content": msg.content}]
    except AssistError:
        raise
    except Exception as exc:  # Netz, Kontingent, Zeitlimit
        raise AssistError(f"Recherche fehlgeschlagen: {describe_error(exc)}") from exc
    if msg is None or msg.stop_reason == "refusal":
        raise AssistError("Recherche wurde vom Dienst abgelehnt")
    if msg.stop_reason == "max_tokens":
        raise AssistError("Recherche abgebrochen: Antwort zu lang")
    data = parse_json_text(final_text(msg.content))
    if not isinstance(data, dict):
        raise AssistError("Recherche lieferte kein lesbares Dossier")
    _call_progress(on_progress, 0.95, "Quellen werden geprüft")
    dossier, dropped = verify_dossier(data, sources, kind, good_empty=good_empty)
    stu = usage_total.get("server_tool_use") or {}
    dossier["stand"] = now.isoformat(timespec="seconds")
    dossier["meta"] = {
        "model": model, "kosten_usd": round(cost, 4), "suchen": stu.get("web_search_requests", 0),
        "abrufe": stu.get("web_fetch_requests", 0), "verworfen": dropped,
        "quellen_gesehen": len(sources), "dauer_s": round(time.monotonic() - t0, 1),
        "tokens": {k: usage_total.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                       "cache_creation_input_tokens")},
    }
    if not dossier["fakten"] and not dossier.get("werk") and not dossier.get("kandidaten"):
        dossier["hinweis"] = "Keine belegbaren Fakten gefunden."
    _call_progress(on_progress, 1.0, "Dossier fertig")
    return dossier


def research(segment: dict, show: dict | None = None, previous_titles: list[str] | None = None,
             plan_context: list[dict] | None = None, **kw) -> dict:
    """Synchrone Variante (CLI, Threads). Im laufenden Event-Loop ``research_async`` verwenden."""
    return asyncio.run(research_async(segment, show, previous_titles, plan_context, **kw))
