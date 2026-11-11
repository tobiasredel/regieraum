import json
from types import SimpleNamespace

import pytest

from publish import shownotes
from publish.shownotes import ShownotesUnavailable, build_prompt, suggest, transcript_text
from tests.publish_fixtures.helpers import show

TRANSCRIPT = {"segments": [
    {"start_ms": 0, "end_ms": 5000, "speaker": "Anna", "text": "Heute reden wir über Parkgebühren."},
    {"start_ms": 65000, "end_ms": 70000, "speaker": "Ben", "text": "Das steht auf kommune-beispiel.de und ist absurd."},
    {"start_ms": 3_725_000, "end_ms": 3_730_000, "speaker": "Anna", "text": "Ende."},
]}
CHAPTERS = [{"event_id": "e0", "start_ms": 0, "title": "Intro"}, {"event_id": "e1", "start_ms": 65000, "title": "Kapitel 1"},
            {"event_id": "e2", "startTime": 100, "title": "Kapitel 2"}, {"event_id": "e3", "at_ms": 3_000_000, "title": "Outro"}]


def reply(payload, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=json.dumps(payload) if not isinstance(payload, str) else payload)])


class Fake:
    def __init__(self, response):
        self.response = response
        self.calls = []
        outer = self

        class M:
            def create(self, **kw):
                outer.calls.append(("messages", kw))
                return outer.response

        class B:
            messages = type("BM", (), {"create": lambda self, **kw: (outer.calls.append(("beta", kw)), outer.response)[1]})()

        self.messages = M()
        self.beta = B()


GOOD = {
    "titles": ["Parken, bis der Arzt kommt", "Zahl, was du willst", "parken, bis der arzt kommt", "Bisheriger Titel", "Haltverbot für den Verstand", ""],
    "summary": "  Es geht um\nParkgebühren.  ",
    "description_html": '<p onclick="x">Einstieg <script>x()</script></p><ul><li>Parken</li></ul>'
                        '<p><a href="https://kommune-beispiel.de/x">Seite</a> <a href="https://erfunden.example/y">Link</a></p>',
    "chapter_titles": [{"event_id": "e1", "title": "Parkscheine"}, {"event_id": "e0", "title": "Eingeschmuggelt"},
                       {"event_id": "unbekannt", "title": "x"}, {"event_id": "e2", "title": "  "}],
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("PS_AI_MODEL", raising=False)
    monkeypatch.delenv("PS_AI_FALLBACKS", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


def test_result_shape_and_normalisation():
    fake = Fake(reply(GOOD))
    r = suggest(TRANSCRIPT, show(), CHAPTERS, ["Bisheriger Titel"], client=fake)
    assert r["titles"] == ["Parken, bis der Arzt kommt", "Zahl, was du willst", "Haltverbot für den Verstand"]  # Dubletten, leere, bisherige raus
    assert r["summary"] == "Es geht um Parkgebühren."
    assert "<script" not in r["description_html"] and "onclick" not in r["description_html"]
    assert 'href="https://kommune-beispiel.de/x"' in r["description_html"]  # im Transkript genannt: bleibt
    assert "erfunden.example" not in r["description_html"] and "Link" in r["description_html"]  # nicht genannt: nur Text
    assert r["chapter_titles"] == {"e1": "Parkscheine"}  # Intro/Outro, unbekannte IDs, leere Titel entfallen
    assert set(r) == {"titles", "summary", "description_html", "chapter_titles"}


def test_request_is_structured_uses_default_model_and_fallback():
    fake = Fake(reply(GOOD))
    suggest(TRANSCRIPT, show(), CHAPTERS, [], client=fake)
    kind, kw = fake.calls[0]
    assert kind == "beta" and kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["fallbacks"] == "default"
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert kw["output_config"]["effort"] == "medium"
    assert kw["max_tokens"] >= 8000
    assert "thinking" not in kw and "temperature" not in kw and "tool_choice" not in kw  # laut Modellvorgaben


def test_model_from_env_and_fallback_switch(monkeypatch):
    monkeypatch.setenv("PS_AI_MODEL", "claude-opus-5-5")
    fake = Fake(reply(GOOD))
    suggest(TRANSCRIPT, show(), CHAPTERS, [], client=fake)
    kind, kw = fake.calls[0]
    assert kind == "messages" and kw["model"] == "claude-opus-5-5" and "fallbacks" not in kw
    monkeypatch.setenv("PS_AI_MODEL", "claude-sonnet-4-5")
    fake = Fake(reply(GOOD))
    suggest(TRANSCRIPT, show(), CHAPTERS, [], client=fake)
    assert "effort" not in fake.calls[0][1]["output_config"]
    monkeypatch.delenv("PS_AI_MODEL")
    monkeypatch.setenv("PS_AI_FALLBACKS", "0")
    fake = Fake(reply(GOOD))
    suggest(TRANSCRIPT, show(), CHAPTERS, [], client=fake)
    assert fake.calls[0][0] == "messages"


def test_prompt_contains_full_transcript_titles_and_chapters():
    system, user = build_prompt(TRANSCRIPT, show(), CHAPTERS, ["Alt eins", "Alt zwei"])
    assert "Zwei am Mikro" in system and "sarkastisch" in system and "Erfinde nichts" in system
    assert "- Alt eins" in user and "- Alt zwei" in user
    assert "event_id=e1 | bei 00:01:05" in user and "event_id=e2 | bei 00:01:40" in user
    assert "event_id=e0" not in user and "event_id=e3" not in user  # Intro/Outro nicht anfragen
    assert "[00:00:00] Anna: Heute reden wir über Parkgebühren." in user and "[01:02:05] Anna: Ende." in user
    assert "owner" not in user.lower() and "@" not in system  # keine Kontaktdaten an den Dienst


def test_long_transcript_is_never_truncated():
    segs = [{"start_ms": i * 1000, "end_ms": i * 1000 + 900, "speaker": "A", "text": f"Satz Nummer {i} mit etwas Text."} for i in range(20000)]
    fake = Fake(reply(GOOD))
    suggest({"segments": segs}, show(), [], [], client=fake)
    content = fake.calls[0][1]["messages"][0]["content"]
    assert "Satz Nummer 0 mit" in content and "Satz Nummer 19999 mit" in content and content.count("Satz Nummer") == 20000


def test_transcript_formats():
    assert transcript_text("reiner Text\n") == "reiner Text"
    assert transcript_text(TRANSCRIPT["segments"]).startswith("[00:00:00] Anna:")
    assert transcript_text({"segments": [{"start_ms": 1000, "text": "  "}]}) == ""


def test_missing_key_gives_clear_exception(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(ShownotesUnavailable, match="Schlüssel"):
        suggest(TRANSCRIPT, show(), CHAPTERS, [])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  ")
    with pytest.raises(ShownotesUnavailable, match="Schlüssel"):
        suggest(TRANSCRIPT, show(), CHAPTERS, [])


def test_empty_transcript():
    with pytest.raises(ShownotesUnavailable, match="leer"):
        suggest({"segments": []}, show(), [], [], client=Fake(reply(GOOD)))


@pytest.mark.parametrize("resp,msg", [
    (reply(GOOD, stop="refusal"), "abgelehnt"),
    (reply(GOOD, stop="max_tokens"), "abgeschnitten"),
    (reply("das ist kein json"), "nicht lesbar"),
    (SimpleNamespace(stop_reason="end_turn", content=[]), "nicht lesbar"),
])
def test_unusable_responses(resp, msg):
    with pytest.raises(ShownotesUnavailable, match=msg):
        suggest(TRANSCRIPT, show(), CHAPTERS, [], client=Fake(resp))


def test_api_errors_become_unavailable():
    import anthropic
    import httpx

    class Boom:
        def __init__(self, exc):
            self.exc = exc
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._raise))

        def _raise(self, **kw):
            raise self.exc

    req = httpx.Request("POST", "https://api.example/v1/messages")
    for exc, msg in [
        (anthropic.AuthenticationError("x", response=httpx.Response(401, request=req), body=None), "Schlüssel"),
        (anthropic.RateLimitError("x", response=httpx.Response(429, request=req), body=None), "überlastet"),
        (anthropic.APIConnectionError(request=req), "nicht erreichbar"),
        (anthropic.InternalServerError("x", response=httpx.Response(500, request=req), body=None), "HTTP 500"),
    ]:
        with pytest.raises(ShownotesUnavailable, match=msg):
            suggest(TRANSCRIPT, show(), CHAPTERS, [], client=Boom(exc))


def test_messages_name_no_provider():
    import inspect
    src = inspect.getsource(shownotes)
    texts = [l for l in src.splitlines() if "ShownotesUnavailable(" in l]
    assert texts and not any("Anthropic" in l or "Claude" in l for l in texts)


def test_show_may_be_dict():
    system, _ = build_prompt(TRANSCRIPT, {"title": "X", "description": "Y"}, [], [])
    assert "„X“" in system and "Y" in system
