"""Dossier: Quellenprüfung, Format, Ablauf mit gemocktem Client."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from assist import prep, prompts
from assist.llm import AssistError, collect_sources
from assist.prep import research_async, verify_dossier
from assist.util import clip_text, normalize_url

SPEC_KEYS = {"lage", "fakten", "zahlen", "positionen", "gegenargumente", "fragen", "aufhaenger", "begriffe", "quellen",
             "stand"}

SEARCH = NS(type="web_search_tool_result", content=[
    NS(type="web_search_result", url="https://www.tagesschau.de/inland/rente-100.html?utm_source=x", title="Rente – tagesschau", page_age="2026-11-09"),
    NS(type="web_search_result", url="https://www.destatis.de/DE/rente/", title="Destatis Rente", page_age=None),
])
FETCH = NS(type="web_fetch_tool_result", content=NS(type="web_fetch_result", url="https://www.bundestag.de/rente",
                                                     retrieved_at="2026-11-10T10:00:00Z", content=NS(title="Bundestag Rente")))


def model_output(**over):
    d = {
        "lage": "Lage in drei Sätzen.",
        "fakten": [
            {"text": "Fakt belegt", "quelle_titel": "", "quelle_url": "https://tagesschau.de/inland/rente-100.html", "datum": "2026-11-09"},
            {"text": "Fakt erfunden", "quelle_titel": "X", "quelle_url": "https://www.erfunden.example/a", "datum": ""},
            {"text": "Fakt ohne Quelle", "quelle_titel": "", "quelle_url": "", "datum": ""},
            {"text": "Fakt aus Abruf", "quelle_titel": "", "quelle_url": "http://bundestag.de/rente/", "datum": ""},
        ],
        "zahlen": [{"text": "48 %", "quelle_url": "https://www.destatis.de/DE/rente"},
                   {"text": "99 %", "quelle_url": "https://nirgends.example"}],
        "positionen": [{"wer": "SPD", "position": "dafür", "quelle_url": ""},
                       {"wer": "Fake", "position": "erfunden", "quelle_url": "https://fake.example"}],
        "gegenargumente": ["Gegen"], "fragen": ["Frage?"], "aufhaenger": ["Pointe"],
        "begriffe": ["Bärbel Bas", "bärbel bas", "Haltelinie"],
        "quellen": [{"titel": "Erfunden", "url": "https://erfunden.example/b", "datum": ""}],
        "werk": None, "kandidaten": [],
    }
    d.update(over)
    return d


def test_collect_sources_and_url_normalisation():
    src = collect_sources([SEARCH, FETCH, NS(type="text", text="x")])
    assert normalize_url("https://www.tagesschau.de/inland/rente-100.html/#a") in src
    assert normalize_url("http://tagesschau.de/inland/rente-100.html?utm_medium=y") in src
    assert src[normalize_url("https://bundestag.de/rente")].title == "Bundestag Rente"
    assert normalize_url("ftp://x") is None and normalize_url("kein link") is None


def test_verify_drops_unbelegtes_and_rebuilds_sources():
    src = collect_sources([SEARCH, FETCH])
    d, dropped = verify_dossier(model_output(), src, "blitzlicht")
    assert [f["text"] for f in d["fakten"]] == ["Fakt belegt", "Fakt aus Abruf"]
    assert d["fakten"][0]["quelle_titel"] == "Rente – tagesschau"  # aus dem Suchergebnis ergänzt
    assert d["fakten"][1]["quelle_url"] == "https://www.bundestag.de/rente"  # URL so, wie gesehen
    assert [z["text"] for z in d["zahlen"]] == ["48 %"]
    assert [p["wer"] for p in d["positionen"]] == ["SPD"]
    assert {q["url"] for q in d["quellen"]} == {"https://www.tagesschau.de/inland/rente-100.html?utm_source=x",
                                               "https://www.bundestag.de/rente", "https://www.destatis.de/DE/rente/"}
    assert d["begriffe"] == ["Bärbel Bas", "Haltelinie"]
    assert dropped == 2 + 1 + 1 + 1  # zwei Fakten, eine Zahl, eine Position, eine Quelle
    assert "werk" not in d and "kandidaten" not in d


def test_verify_good_candidates_need_seen_url():
    src = collect_sources([SEARCH])
    out = model_output(kandidaten=[
        {"titel": "Buch A", "urheber": "X", "typ": "read", "url": "https://destatis.de/DE/rente", "warum": "passt"},
        {"titel": "Buch B", "urheber": "Y", "typ": "read", "url": "https://erfunden.example/buch", "warum": "passt"},
    ])
    d, _ = verify_dossier(out, src, "good", good_empty=True)
    assert [k["titel"] for k in d["kandidaten"]] == ["Buch A"]
    assert d["werk"] is None


def test_limits_per_kind():
    src = collect_sources([SEARCH])
    many = [{"text": f"F{i}", "quelle_titel": "", "quelle_url": "https://destatis.de/DE/rente", "datum": ""} for i in range(20)]
    d, _ = verify_dossier(model_output(fakten=many, fragen=[f"Q{i}?" for i in range(10)]), src, "blitzlicht")
    assert len(d["fakten"]) == 5 and len(d["fragen"]) == 3
    d, _ = verify_dossier(model_output(fakten=many), src, "hauptthema")
    assert len(d["fakten"]) == 12


def test_clip_text_hard_limit():
    t = clip_text("Eins zwei drei. " * 30, 140)
    assert len(t) <= 140
    assert clip_text("kurz", 140) == "kurz"
    assert len(clip_text("x" * 500, 140)) == 140


class FakeStream:
    def __init__(self, message):
        self.message = message
        blocks = message.content
        self.current_message_snapshot = NS(content=blocks)
        self.events = []
        for i, b in enumerate(blocks):
            self.events.append(NS(type="content_block_start", index=i, content_block=b))
            self.events.append(NS(type="content_block_stop", index=i))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            for e in self.events:
                yield e
        return gen()

    async def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self, messages):
        self.messages_out = list(messages)
        self.calls = []
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **kw):
        self.calls.append(kw)
        m = self.messages_out.pop(0)
        if isinstance(m, BaseException):
            raise m
        return FakeStream(m)


def msg(content, stop="end_turn"):
    usage = NS(input_tokens=1000, output_tokens=2000, cache_read_input_tokens=0, cache_creation_input_tokens=0,
               server_tool_use=NS(web_search_requests=2, web_fetch_requests=1))
    return NS(content=content, stop_reason=stop, usage=usage)


def test_research_flow_with_pause_turn_and_progress():
    search_use = NS(type="server_tool_use", name="web_search", input={"query": "Rente 2026"})
    fetch_use = NS(type="server_tool_use", name="web_fetch", input={"url": "https://bundestag.de/rente"})
    m1 = msg([search_use, SEARCH], stop="pause_turn")
    m2 = msg([fetch_use, FETCH, NS(type="text", text=json.dumps(model_output()))])
    client = FakeClient([m1, m2])
    progress = []
    seg = {"id": "s1", "kind": "blitzlicht", "title": "Rente", "input": "These: alles Show. https://www.bundestag.de/rente"}
    now = datetime(2026, 11, 11, 9, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    d = asyncio.run(research_async(seg, {"title": "Zwei am Mikro"}, ["Folge 12: Wahlrecht"],
                                   [{"id": "s2", "kind": "hauptthema", "title": "Haushalt", "input": "Schuldenbremse"}],
                                   on_progress=lambda p, m: progress.append((p, m)), client=client, now=now))
    assert SPEC_KEYS <= set(d)
    assert d["stand"] == "2026-11-11T09:00:00+01:00"
    assert d["meta"]["suchen"] == 4 and d["meta"]["abrufe"] == 2 and d["meta"]["verworfen"] == 5
    assert d["meta"]["kosten_usd"] == pytest.approx(2 * (1000 * 4 + 2000 * 20) / 1e6 + 4 * 0.01)
    # zweiter Aufruf setzt nach pause_turn fort
    assert len(client.calls) == 2 and client.calls[1]["messages"][-1]["role"] == "assistant"
    kw = client.calls[0]
    user = kw["messages"][0]["content"]
    assert "Heute ist 2026-11-11, Mittwoch" in user and "https://www.bundestag.de/rente" in user
    assert "Haushalt" in user and "Folge 12: Wahlrecht" in user
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_config"]["format"]["type"] == "json_schema" and kw["output_config"]["effort"] == "medium"
    assert {t["name"] for t in kw["tools"]} == {"web_search", "web_fetch"}
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    msgs = [m for _, m in progress]
    assert msgs[0] == "Recherche startet" and "Sucht: Rente 2026" in msgs and msgs[-1] == "Dossier fertig"
    assert [p for p, _ in progress] == sorted(p for p, _ in progress)


def test_research_good_empty_uses_candidates_prompt():
    client = FakeClient([msg([SEARCH, NS(type="text", text=json.dumps(model_output(kandidaten=[
        {"titel": "Doku", "urheber": "ARD", "typ": "watch", "url": "https://destatis.de/DE/rente", "warum": "passt"}])))])])
    d = asyncio.run(research_async({"kind": "good", "title": "Good Read", "input": ""}, None, [], [], client=client))
    assert d["kandidaten"][0]["titel"] == "Doku"
    assert "Kandidaten vorschlagen" in client.calls[0]["system"][1]["text"]


@pytest.mark.parametrize("exc_or_msg,expect", [
    (TimeoutError(), "Recherche fehlgeschlagen: Zeitüberschreitung"),
    ("refusal", "abgelehnt"),
    ("kaputt", "kein lesbares Dossier"),
])
def test_research_errors_are_german(exc_or_msg, expect):
    if exc_or_msg == "refusal":
        out = msg([NS(type="text", text="")], stop="refusal")
    elif exc_or_msg == "kaputt":
        out = msg([NS(type="text", text="kein json")])
    else:
        out = exc_or_msg
    with pytest.raises(AssistError, match=expect):
        asyncio.run(research_async({"kind": "blitzlicht", "title": "x", "input": "y"}, client=FakeClient([out])))


def test_live_plan_rendering_contains_dossier_and_ids():
    text = prompts.render_plan([{"id": "s1", "kind": "blitzlicht", "title": "Rente", "input": "These",
                                 "dossier": {"lage": "L", "fakten": [{"text": "F", "quelle_url": "https://u", "datum": "2026-11-01"}],
                                             "fragen": ["Q?"], "aufhaenger": ["A"], "begriffe": ["B"]}}])
    for s in ("id=s1", "Kurzthema", "Fakt 1: F [2026-11-01; https://u]", "Frage 1: Q?", "Aufhänger 1: A", "Begriffe: B"):
        assert s in text


def test_no_provider_names_in_user_visible_errors():
    from assist.llm import describe_error
    for status in (400, 401, 429, 500, 529):
        e = Exception()
        e.status_code = status
        m = describe_error(e)
        assert not any(p in m.lower() for p in ("anthropic", "claude", "openai", "sonnet", "opus"))
