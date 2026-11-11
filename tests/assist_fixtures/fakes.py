"""Test-Doubles und synthetische Signale für die assist-Tests."""
from __future__ import annotations

import asyncio

import numpy as np

from assist.llm import LLMResult, Source
from assist.stt import Transcript
from assist.util import normalize_url

SR = 48000


def db_to_amp(db: float) -> float:
    return 10 ** (db / 20)


def synth(duration_s: float, bursts: list[tuple[float, float]], *, speech_db: float = -30.0, noise_db: float = -65.0,
          sr: int = SR, seed: int = 1) -> np.ndarray:
    """Rauschen plus „Sprache“: bandbegrenztes Rauschen mit 4-Hz-Silbenmodulation in den angegebenen Bereichen."""
    rng = np.random.default_rng(seed)
    n = int(duration_s * sr)
    t = np.arange(n) / sr
    x = rng.normal(0, db_to_amp(noise_db), n)
    sp = rng.normal(0, 1, n)
    sp = np.convolve(sp, np.ones(6) / 6, mode="same")  # etwas tiefpassen
    sp /= np.sqrt(np.mean(sp ** 2))
    mod = 0.15 + 0.85 * np.abs(np.sin(2 * np.pi * 2.5 * t))
    for a, b in bursts:
        i, j = int(a * sr), int(b * sr)
        x[i:j] += sp[i:j] * mod[i:j] * db_to_amp(speech_db)
    return x.astype(np.float32)


def to_s24le(x: np.ndarray) -> bytes:
    v = np.clip(np.round(x * 8388607), -8388608, 8388607).astype(np.int32)
    b = np.empty((len(v), 3), dtype=np.uint8)
    b[:, 0] = v & 0xFF
    b[:, 1] = (v >> 8) & 0xFF
    b[:, 2] = (v >> 16) & 0xFF
    return b.tobytes()


def chunks(x: np.ndarray, seconds: float = 2.0, sr: int = SR):
    n = int(seconds * sr)
    for i in range(0, len(x), n):
        yield i, x[i : i + n]


class FakeTranscriber:
    """Liefert Texte nach einer Funktion (Startzeit des Audios unbekannt, daher nach Aufrufreihenfolge oder Länge)."""

    def __init__(self, texts=None, *, delay: float = 0.0, fail: Exception | None = None, hang: bool = False):
        self.texts = texts if texts is not None else (lambda audio, n: f"Äußerung Nummer {n} mit genug Inhalt")
        self.delay = delay
        self.fail = fail
        self.hang = hang
        self.calls = []

    async def transcribe(self, audio, sample_rate, *, terms=None, prompt=None):
        n = len(self.calls)
        self.calls.append({"audio": np.array(audio), "samples": len(audio), "terms": terms, "prompt": prompt, "peak": float(np.max(np.abs(audio)) if len(audio) else 0)})
        if self.hang:
            await asyncio.sleep(3600)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        text = self.texts(audio, n) if callable(self.texts) else self.texts[n % len(self.texts)]
        return Transcript(text=text, billed_seconds=len(audio) / sample_rate, cost_usd=0.0001)

    async def aclose(self):
        pass


class FakeLLM:
    """Gibt vorbereitete Antworten zurück; ``responses`` ist eine Liste von dicts oder Ausnahmen oder eine Funktion."""

    def __init__(self, responses=None, *, sources: list[str] | None = None, delay: float = 0.0):
        self.responses = responses if responses is not None else []
        self.calls = []
        self.sources = {normalize_url(u): Source(url=u, title="Titel") for u in (sources or [])}
        self.delay = delay

    async def json_call(self, **kw):
        self.calls.append(kw)
        if self.delay:
            await asyncio.sleep(self.delay)
        if callable(self.responses):
            r = self.responses(kw)
        else:
            r = self.responses.pop(0) if self.responses else {"anlass": "nichts", "karte": None}
        if isinstance(r, BaseException):
            raise r
        return LLMResult(data=r, text="", model=kw["model"], usage={"input_tokens": 100, "output_tokens": 20},
                         cost_usd=0.001, sources=self.sources)


class Clock:
    def __init__(self, start: float = 1_000_000.0):
        self.t = start

    def __call__(self):
        return self.t
