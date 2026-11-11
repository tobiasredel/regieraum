"""Transkription über HTTP, mit gemocktem Transport."""
from __future__ import annotations

import asyncio
import io
import wave

import httpx
import numpy as np
import pytest

from assist.llm import AssistError
from assist.stt import OpenAITranscriber


def parse_multipart(req: httpx.Request) -> tuple[list[tuple[str, str]], bytes]:
    ctype = req.headers["content-type"]
    boundary = ctype.split("boundary=")[1].encode()
    fields, file_bytes = [], b""
    for part in req.content.split(b"--" + boundary):
        if b"Content-Disposition" not in part:
            continue
        head, _, body = part.partition(b"\r\n\r\n")
        body = body.rsplit(b"\r\n", 1)[0]
        name = head.split(b'name="')[1].split(b'"')[0].decode()
        if b"filename=" in head:
            file_bytes = body
        else:
            fields.append((name, body.decode()))
    return fields, file_bytes


def make(model, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAITranscriber(api_key="test", model=model, client=client)


def test_request_shape_gpt_transcribe():
    seen = {}

    def handler(req):
        seen["fields"], seen["file"] = parse_multipart(req)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json={"text": " Hallo Max ", "usage": {"type": "duration", "seconds": 2}})

    t = make("gpt-transcribe", handler)
    audio = np.zeros(16000, np.float32)
    res = asyncio.run(t.transcribe(audio, 16000, terms=["Bärbel Bas", "<böse>", "Bärbel Bas"], prompt="Podcast."))
    f = seen["fields"]
    assert ("model", "gpt-transcribe") in f and ("languages[]", "de") in f
    assert [v for k, v in f if k == "keywords[]"] == ["Bärbel Bas", "böse"]
    assert ("prompt", "Podcast.") in f
    assert seen["auth"] == "Bearer test"
    with wave.open(io.BytesIO(seen["file"])) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getnframes() == 16000
    assert res.text == "Hallo Max" and res.billed_seconds == 2
    assert res.cost_usd == pytest.approx(2 / 60 * 0.0045)


def test_legacy_model_uses_language_and_prompt_terms():
    seen = {}

    def handler(req):
        seen["fields"], _ = parse_multipart(req)
        return httpx.Response(200, json={"text": "ok"})

    t = make("gpt-4o-transcribe", handler)
    asyncio.run(t.transcribe(np.zeros(800, np.float32), 16000, terms=["Klingbeil"], prompt="Podcast."))
    f = dict(seen["fields"])
    assert f["language"] == "de" and "languages[]" not in f
    assert f["prompt"] == "Podcast. Begriffe: Klingbeil"


@pytest.mark.parametrize("status,msg", [(429, "Kontingent"), (401, "Zugangsschlüssel"), (503, "überlastet")])
def test_http_errors_become_german_messages(status, msg):
    t = make("gpt-transcribe", lambda req: httpx.Response(status, json={"error": {"message": "x"}}))
    with pytest.raises(AssistError) as e:
        asyncio.run(t.transcribe(np.zeros(800, np.float32), 16000))
    assert msg in str(e.value)
    assert "openai" not in str(e.value).lower()


def test_timeout_becomes_message():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    t = make("gpt-transcribe", handler)
    with pytest.raises(AssistError, match="Zeitüberschreitung"):
        asyncio.run(t.transcribe(np.zeros(800, np.float32), 16000))


def test_missing_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from assist import stt
    assert stt.from_env() is None
    with pytest.raises(AssistError, match="nicht eingerichtet"):
        OpenAITranscriber()
