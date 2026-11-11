"""Dünne Hülle um die Anthropic-API: strukturierte Ausgabe, Zeitlimit, Quellen sammeln, Kosten, Fehlertexte."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field

from .util import normalize_url, usage_cost_usd

FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_SEARCH_TOOL = "web_search_20260209"
WEB_FETCH_TOOL = "web_fetch_20260209"


class AssistError(Exception):
    """Fehler mit deutschem Klartext für Menschen (ohne Anbieter- oder Modellnamen)."""


def describe_error(exc: BaseException) -> str:
    """Deutscher Klartext zu einem Fehler eines KI- oder Transkriptionsdienstes."""
    if isinstance(exc, AssistError):
        return str(exc)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "Zeitüberschreitung"
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    resp = getattr(exc, "response", None)
    if status is None and resp is not None:
        status = getattr(resp, "status_code", None)
    if "Timeout" in name:
        return "Zeitüberschreitung"
    if "Connect" in name or "Network" in name or name in ("ReadError", "WriteError", "RemoteProtocolError"):
        return "keine Verbindung zum Dienst"
    if status == 429:
        return "Kontingent erschöpft oder zu viele Anfragen"
    if status in (401, 403):
        return "Zugangsschlüssel ungültig"
    if status == 400:
        return "Anfrage abgelehnt (400)"
    if status is not None and status >= 500 or status == 529:
        return "Dienst überlastet oder gestört"
    return "unerwarteter Fehler"


@dataclass
class Source:
    url: str
    title: str = ""
    page_age: str = ""


@dataclass
class LLMResult:
    data: dict | None
    text: str
    model: str
    usage: dict
    cost_usd: float
    sources: dict = field(default_factory=dict)  # normalisierte URL -> Source
    stop_reason: str | None = None
    searches: int = 0
    fetches: int = 0


def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def collect_sources(content, into: dict | None = None) -> dict:
    """URLs aus Suchergebnissen und abgerufenen Seiten der Antwort (nur diese gelten als Belege)."""
    out = into if into is not None else {}
    for block in content or []:
        btype = _get(block, "type")
        if btype == "web_search_tool_result":
            items = _get(block, "content")
            if isinstance(items, list):
                for it in items:
                    url = _get(it, "url")
                    key = normalize_url(url)
                    if key and key not in out:
                        out[key] = Source(url=url, title=_get(it, "title") or "", page_age=_get(it, "page_age") or "")
        elif btype == "web_fetch_tool_result":
            res = _get(block, "content")
            if _get(res, "type") == "web_fetch_result":
                url = _get(res, "url")
                key = normalize_url(url)
                if key:
                    doc = _get(res, "content")
                    title = _get(doc, "title") or ""
                    prev = out.get(key)
                    out[key] = Source(url=url, title=title or (prev.title if prev else ""),
                                      page_age=(prev.page_age if prev else "") or (_get(res, "retrieved_at") or "")[:10])
    return out


def usage_to_dict(usage) -> dict:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    d = {}
    for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        d[k] = getattr(usage, k, 0) or 0
    stu = getattr(usage, "server_tool_use", None)
    d["server_tool_use"] = {"web_search_requests": getattr(stu, "web_search_requests", 0) or 0,
                            "web_fetch_requests": getattr(stu, "web_fetch_requests", 0) or 0} if stu else {}
    return d


def add_usage(a: dict, b: dict) -> dict:
    out = dict(a)
    for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        out[k] = (out.get(k) or 0) + (b.get(k) or 0)
    s = dict(out.get("server_tool_use") or {})
    for k, v in (b.get("server_tool_use") or {}).items():
        s[k] = (s.get(k) or 0) + (v or 0)
    out["server_tool_use"] = s
    return out


def final_text(content) -> str:
    texts = [_get(b, "text") or "" for b in content or [] if _get(b, "type") == "text"]
    return texts[-1] if texts else ""


def parse_json_text(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        i, j = text.find("{"), text.rfind("}")
        if 0 <= i < j:
            try:
                return json.loads(text[i : j + 1])
            except json.JSONDecodeError:
                return None
    return None


def make_async_client(api_key: str | None = None, *, max_retries: int = 0, timeout: float = 20.0):
    import anthropic

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise AssistError("KI-Zugang nicht eingerichtet")
    return anthropic.AsyncAnthropic(api_key=key, max_retries=max_retries, timeout=timeout)


class LLM:
    """Strukturierte Aufrufe mit hartem Zeitlimit. ``client`` ist ein ``AsyncAnthropic`` oder ein Test-Double."""

    def __init__(self, client=None, *, api_key: str | None = None, use_fallbacks: bool = True):
        self._client = client
        self._api_key = api_key
        self.use_fallbacks = use_fallbacks

    @property
    def client(self):
        if self._client is None:
            self._client = make_async_client(self._api_key)
        return self._client

    async def json_call(self, *, model: str, system, messages: list, schema: dict, effort: str = "low",
                        max_tokens: int = 2000, tools: list | None = None, timeout: float = 20.0) -> LLMResult:
        kwargs = dict(model=model, max_tokens=max_tokens, system=system, messages=messages,
                      output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}})
        if tools:
            kwargs["tools"] = tools
        if self.use_fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        resp = await asyncio.wait_for(self.client.beta.messages.create(**kwargs, timeout=timeout), timeout=timeout)
        content = _get(resp, "content") or []
        stop = _get(resp, "stop_reason")
        usage = usage_to_dict(_get(resp, "usage"))
        if stop == "refusal":
            raise AssistError("Anfrage wurde vom Dienst abgelehnt")
        text = final_text(content)
        data = parse_json_text(text)
        if data is None:
            raise AssistError("Antwort des Dienstes nicht lesbar")
        stu = usage.get("server_tool_use") or {}
        return LLMResult(data=data, text=text, model=model, usage=usage, cost_usd=usage_cost_usd(model, usage),
                         sources=collect_sources(content), stop_reason=stop,
                         searches=stu.get("web_search_requests", 0), fetches=stu.get("web_fetch_requests", 0))
