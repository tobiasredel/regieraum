"""Transkription kurzer Äußerungen für die Live-Mitschrift.

``Transcriber`` ist die Schnittstelle; ``OpenAITranscriber`` spricht direkt per ``httpx`` mit
``POST /v1/audio/transcriptions``. Standardmodell ``gpt-transcribe`` (laut Anbieter-Doku 11/2026 das empfohlene
Modell für aufgezeichnete Sprache, mit ``languages``, ``keywords`` und ``prompt``; 0,0045 $/min). Für die älteren
Modelle (``gpt-4o-transcribe`` u. a., abgekündigt zum 26.02.2027) wird ``language`` statt ``languages`` geschickt und
die Begriffsliste nur über den Prompt.
"""
from __future__ import annotations

import io
import os
import wave
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .llm import AssistError, describe_error
from .util import STT_PRICES_USD_PER_MIN, lang_code

DEFAULT_MODEL = "gpt-transcribe"
API_URL = "https://api.openai.com/v1/audio/transcriptions"


@dataclass
class Transcript:
    text: str
    billed_seconds: float
    cost_usd: float


class Transcriber(Protocol):
    async def transcribe(self, audio: np.ndarray, sample_rate: int, *, terms: list[str] | None = None,
                         prompt: str | None = None) -> Transcript:
        """``audio``: float32 mono in [-1, 1]. Wirft ``AssistError`` mit deutschem Klartext."""
        ...

    async def aclose(self) -> None: ...


def wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    pcm = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16)
    return buf.getvalue()


def _clean_terms(terms: list[str] | None, limit: int = 100) -> list[str]:
    out, seen = [], set()
    for t in terms or []:
        t = " ".join(str(t).replace("<", " ").replace(">", " ").split())
        if t and len(t) <= 60 and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
        if len(out) >= limit:
            break
    return out


class _StatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(status_code)
        self.status_code = status_code


class OpenAITranscriber:
    def __init__(self, api_key: str | None = None, model: str | None = None, *, timeout: float = 10.0,
                 language: str = "de", client=None):
        import httpx

        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise AssistError("Mitschrift nicht eingerichtet")
        self.model = model or os.environ.get("PS_STT_MODEL") or DEFAULT_MODEL
        self.language = lang_code(language)
        self.timeout = timeout
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0),
                                                   limits=httpx.Limits(max_connections=8, max_keepalive_connections=4))

    def _form(self, terms: list[str], prompt: str | None) -> dict:
        data: dict = {"model": self.model, "response_format": "json"}
        hint = (prompt or "").strip()
        if self.model.startswith("gpt-transcribe"):
            data["languages[]"] = [self.language]
            if terms:
                data["keywords[]"] = terms
        else:
            data["language"] = self.language
            if terms:
                hint = (hint + " Begriffe: " + ", ".join(terms)).strip()
        if hint:
            data["prompt"] = hint[:1500]
        return data

    async def transcribe(self, audio, sample_rate, *, terms=None, prompt=None) -> Transcript:
        terms = _clean_terms(terms)
        files = {"file": ("utterance.wav", wav_bytes(audio, sample_rate), "audio/wav")}
        try:
            r = await self._client.post(API_URL, headers={"Authorization": f"Bearer {self.api_key}"},
                                        data=self._form(terms, prompt), files=files, timeout=self.timeout)
        except Exception as exc:
            raise AssistError(describe_error(exc)) from exc
        if r.status_code != 200:
            raise AssistError(describe_error(_StatusError(r.status_code)))
        try:
            body = r.json()
        except ValueError as exc:
            raise AssistError("Antwort des Dienstes nicht lesbar") from exc
        seconds = len(audio) / sample_rate
        usage = body.get("usage") or {}
        if usage.get("type") == "duration" and usage.get("seconds"):
            seconds = float(usage["seconds"])
        price = STT_PRICES_USD_PER_MIN.get(self.model, 0.006)
        return Transcript(text=(body.get("text") or "").strip(), billed_seconds=seconds, cost_usd=seconds / 60 * price)

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass


def from_env(language: str = "de") -> Transcriber | None:
    """``OpenAITranscriber`` bei gesetztem ``OPENAI_API_KEY``, sonst ``None`` (Live-Begleiter bleibt aus).
    ``language``: Sprache der Show (zwei Buchstaben)."""
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    return OpenAITranscriber(language=language)
