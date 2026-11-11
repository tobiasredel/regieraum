"""Live-Begleiter (SPEC Abschnitt 15): Mitschrift je Sprecher und ruhiger Strom von Hinweiskarten.

Einbau in den Server (asyncio, im Event-Loop des Servers)::

    live = LiveSession(rec_start_server_ms=..., on_line=..., on_card=..., on_status=..., work_dir=...)
    await live.start()
    live.set_plan(segments)                 # [{id, kind, title, input, notes, dossier}]
    live.set_current_segment(segment_id)
    live.feed_chunk(track_id, speaker, sample_rate, fmt, first_frame, pcm_bytes, track_start_session_ms)
    live.set_paused(True/False)
    live.pulse(); live.lookup("Begriff")
    await live.close()

``feed_chunk`` blockiert nicht und ist auch aus anderen Threads aufrufbar. Callbacks dürfen synchron oder
asynchron sein; Ausnahmen darin werden geschluckt. Kein Fehler eines Dienstes verlässt die Sitzung: Er wird zu
``on_status`` mit deutschem Klartext.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np

from . import prompts
from .llm import LLM, WEB_SEARCH_TOOL, AssistError, describe_error
from .schemas import LIVE_CARD_SCHEMA, LOOKUP_SCHEMA, SUMMARY_SCHEMA
from .util import (card_overlap, clip_text, content_words, lang_code, near_duplicate_text, new_id, normalize_url, words)
from .vad import Utterance, UtteranceCutter

log = logging.getLogger("assist.live")

DEFAULT_LIVE_MODEL = "claude-sonnet-5-5"
TEXT_MAX = 140
DETAIL_MAX = 300

# Typische Halluzinationen von Transkriptionsmodellen auf Geräuschen oder Stille
_HALLU = re.compile(r"(untertitel|amara\.org|vielen dank für(s| das)? zuschauen|copyright|swr \d|zdf|ard text|"
                    r"tschüss und bis zum nächsten mal|danke fürs zuhören|abonniert)", re.I)


@dataclass
class LiveConfig:
    hint_interval_ms: int = 35_000  # Takt der Hinweise (Sitzungszeit)
    min_card_gap_ms: int = 30_000  # höchstens eine Takt-Karte je 30 s
    min_new_chars: int = 160  # so viel Neues muss seit dem letzten Versuch gesagt worden sein
    window_ms: int = 240_000  # Mitschrift-Fenster für Hinweise
    pulse_window_ms: int = 60_000
    summary_min_chars: int = 600  # ab so viel herausgefallener Mitschrift wird die Kurzfassung fortgeschrieben
    stt_timeout_s: float = 10.0
    llm_timeout_s: float = 20.0
    lookup_timeout_s: float = 25.0
    stt_concurrency: int = 3
    stt_max_pending: int = 6  # mehr wartende Äußerungen: älteste verwerfen
    stt_max_age_ms: int = 15_000  # Äußerungen, die so lange auf die Mitschrift warten, werden übersprungen
    bleed_db: float = 15.0  # Rahmen, in denen eine andere Spur so viel lauter ist, gelten als Übersprechen
    bleed_share: float = 0.7  # so viel Übersprechen unter den Sprachrahmen: Äußerung gar nicht transkribieren
    bleed_wait_ms: int = 2_500  # so lange auf die Daten der anderen Spuren warten
    line_hold_ms: int = 3_000  # Zeilen warten höchstens so lange auf überlappende Äußerungen anderer Spuren
    hint_effort: str = "low"
    check_min_gap_ms: int = 12_000  # Zahlen-Prüfung und Wissenslücke (Korrekturen, Antworten) höchstens so oft
    auto_lookup_min_gap_ms: int = 45_000  # selbst ausgelöstes Nachschlagen höchstens so oft
    auto_lookup_max: int = 40  # und höchstens so viele je Aufnahme (jede Suche kostet)
    check_window_ms: int = 90_000  # Mitschrift-Fenster der Zahlen-Prüfung
    chunk_queue_max: int = 200
    poll_s: float = 0.25


@dataclass
class _Pending:
    utt: Utterance
    text: str | None = None
    ready_at: float = 0.0  # Serverzeit, zu der die Mitschrift vorlag
    done: bool = False
    dropped: bool = False


@dataclass
class Stats:
    stt_requests: int = 0
    stt_seconds: float = 0.0
    stt_cost_usd: float = 0.0
    stt_errors: int = 0
    stt_skipped: int = 0
    llm_requests: int = 0
    llm_cost_usd: float = 0.0
    llm_errors: int = 0
    llm_tokens: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0,
                                                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
    searches: int = 0
    cost_by_kind: dict = field(default_factory=dict)  # tick, check, pulse, summary, lookup → [Aufrufe, $]
    lines: int = 0
    lines_deduped: int = 0
    bleed_skipped: int = 0
    cards: int = 0
    cards_rejected: int = 0
    hint_calls: int = 0
    hint_silent: int = 0
    lag_ms: list = field(default_factory=list)
    hint_latency_ms: list = field(default_factory=list)


class LiveSession:
    def __init__(self, *, rec_start_server_ms: float | None = None, on_line: Callable | None = None,
                 on_card: Callable | None = None, on_status: Callable | None = None,
                 on_said: Callable | None = None, transcriber: Any = "env",
                 llm: LLM | None | str = "env", model: str | None = None, work_dir: str | Path | None = None,
                 server_clock: Callable[[], float] | None = None, show: dict | None = None,
                 host_name: str = "der Host", config: LiveConfig | None = None, language: str | None = None):
        self.cfg = config or LiveConfig()
        self.server_clock = server_clock or (lambda: time.time() * 1000.0)
        self.rec_start = rec_start_server_ms if rec_start_server_ms is not None else self.server_clock()
        self.on_line, self.on_card, self.on_status, self.on_said = on_line, on_card, on_status, on_said
        self.said: set[tuple[str, str, int]] = set()  # (Abschnitt, Art, Nr.) der im Gespräch gefallenen Dossier-Punkte
        self.model = model or os.environ.get("PS_AI_MODEL_LIVE") or DEFAULT_LIVE_MODEL
        self.show = show
        self.language = lang_code(language or (show or {}).get("language"))  # Sprache der Show: Mitschrift, Prompts, Karten
        self.host_name = host_name
        self.work_dir = Path(work_dir) if work_dir else None
        if transcriber == "env":
            from .stt import from_env
            try:
                transcriber = from_env(language=self.language)
            except AssistError:
                transcriber = None
        self.transcriber = transcriber
        if llm == "env":
            llm = LLM() if os.environ.get("ANTHROPIC_API_KEY") else None
        self.llm = llm
        self.stats = Stats()
        # Zustand
        self.cutters: dict[str, UtteranceCutter] = {}
        self.plan: list[dict] = []
        self.plan_text = prompts.render_plan([], self.show)
        self.terms: list[str] = []
        self.dossier_urls: dict[str, str] = {}
        self.current_id: str | None = None
        self.segment_since_ms: float | None = None
        self.paused = False
        self.lines: list[dict] = []  # ausgegebene Zeilen
        self.summary = ""
        self.summarized_upto = 0  # Index in self.lines
        self.cards: list[dict] = []
        self.chars_since_hint = 0
        self.last_hint_try_ms = -1e18
        self.last_card_ms = -1e18
        self._pending: list[_Pending] = []  # in Mitschrift oder wartend auf Ausgabe, nach Beginn sortiert
        self._stt_queue: deque[_Pending] = deque()
        self._deferred: list[tuple[Utterance, float]] = []  # warten auf andere Spuren (Übersprech-Prüfung)
        self._chunks: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task] = []
        self._bg: set[asyncio.Task] = set()
        self._stt_sem: asyncio.Semaphore | None = None
        self._hint_busy = False
        self._hint_lock: asyncio.Lock | None = None
        self.number_pending = False  # seit dem letzten Hinweis-Aufruf wurde eine Zahl genannt
        self.question_pending = False  # … oder nach einer Tatsache gefragt bzw. Unsicherheit geäußert
        self.auto_queries: list[str] = []  # selbst ausgelöste Suchbegriffe dieser Aufnahme
        self.last_auto_lookup_ms = -1e18
        self._summary_busy = False
        self._closed = False
        self._status = {"on": True, "stt": "aus" if transcriber is None else "bereit",
                        "ai": "aus" if llm is None else "bereit", "lag_ms": None, "message": ""}
        self._stt_fail_run = 0
        self._stt_block_until = 0.0
        self._ai_fail_run = 0
        self._jsonl = None

    # ================================================================== Lebenszyklus
    async def start(self) -> None:
        if self._loop is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._chunks = asyncio.Queue(maxsize=self.cfg.chunk_queue_max)
        self._stt_sem = asyncio.Semaphore(self.cfg.stt_concurrency)
        self.last_hint_try_ms = self.now_ms()  # erster Hinweis frühestens nach einem Takt
        self._hint_lock = asyncio.Lock()
        if self.work_dir:
            try:
                self.work_dir.mkdir(parents=True, exist_ok=True)
                self._jsonl = open(self.work_dir / "live_transcript.jsonl", "a", encoding="utf-8")
            except OSError:
                self._jsonl = None
        self._tasks = [asyncio.create_task(self._chunk_worker(), name="assist-chunks"),
                       asyncio.create_task(self._ticker(), name="assist-ticker")]
        msg = ""
        if self.transcriber is None:
            msg = "Mitschrift nicht eingerichtet, Begleiter bleibt aus"
        elif self.llm is None:
            msg = "Hinweise nicht eingerichtet, nur Mitschrift"
        self._set_status(message=msg, force=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for t in self._tasks:
            t.cancel()
        for t in list(self._bg):
            t.cancel()
        await asyncio.gather(*self._tasks, *self._bg, return_exceptions=True)
        if self.transcriber is not None and hasattr(self.transcriber, "aclose"):
            try:
                await self.transcriber.aclose()
            except Exception:
                pass
        llm_client = getattr(self.llm, "_client", None)
        if llm_client is not None and hasattr(llm_client, "close"):
            try:
                await llm_client.close()
            except Exception:
                pass
        if self._jsonl:
            self._jsonl.close()
            self._jsonl = None

    # ================================================================== Steuerung
    def now_ms(self) -> float:
        """Sitzungszeit jetzt."""
        return self.server_clock() - self.rec_start

    def feed_chunk(self, track_id: str, speaker: str, sample_rate: int, fmt: str, first_frame: int,
                   pcm_bytes: bytes, track_start_session_ms: float) -> None:
        """Nimmt einen hochgeladenen Chunk an (blockiert nicht, threadsicher)."""
        if self._closed or self._loop is None or self.transcriber is None:
            return
        item = (track_id, speaker, int(sample_rate), fmt, int(first_frame), bytes(pcm_bytes), float(track_start_session_ms))
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            self._put_chunk(item)
        else:
            self._loop.call_soon_threadsafe(self._put_chunk, item)

    def _put_chunk(self, item):
        try:
            self._chunks.put_nowait(item)
        except asyncio.QueueFull:
            self.stats.stt_skipped += 1
            self._set_status(stt="hinkt", message="Mitschrift überlastet, Audio wird übersprungen")

    def set_plan(self, segments: list[dict]) -> None:
        self.plan = [dict(s) for s in segments or []]
        self.plan_text = prompts.render_plan(self.plan, self._pshow)
        terms, urls = [], {}
        for s in self.plan:
            d = s.get("dossier") or {}
            terms += d.get("begriffe") or []
            for f in (d.get("fakten") or []) + (d.get("zahlen") or []) + (d.get("quellen") or []):
                u = f.get("quelle_url") or f.get("url")
                if normalize_url(u):
                    urls[normalize_url(u)] = u
            w = d.get("werk") or {}
            if normalize_url(w.get("url")):
                urls[normalize_url(w["url"])] = w["url"]
        self.terms = list(dict.fromkeys(t for t in terms if t))[:100]
        self.dossier_urls = urls
        if self.current_id and not self._segment(self.current_id):
            self.current_id = None

    def set_current_segment(self, segment_id: str | None) -> None:
        if segment_id != self.current_id:
            self.current_id = segment_id
            self.segment_since_ms = self.now_ms()

    def set_paused(self, paused: bool) -> None:
        paused = bool(paused)
        if paused == self.paused:
            return
        self.paused = paused
        if paused:  # laufende Äußerungen verwerfen, in der Pause wird nichts transkribiert
            for c in self.cutters.values():
                c.flush()
        self._set_status(message="Pause: Begleiter hört nicht mit" if paused else "", force=True)

    def pulse(self) -> asyncio.Task | None:
        """„Impuls jetzt“: sofort eine Karte zum zuletzt Gesagten."""
        return self._spawn(self._hint("pulse"))

    def lookup(self, query: str) -> asyncio.Task | None:
        """„Nachschlagen“: kurze Netzrecherche, Antwort als Karte ``recherche``."""
        q = " ".join((query or "").split())[:200]
        if not q:
            return None
        return self._spawn(self._lookup(q))

    def end_of_audio(self) -> None:
        """Alle Spuren zu Ende: laufende Äußerungen abschließen und noch transkribieren (z. B. bei ``rec.stop``)."""
        if self._loop is None or self._closed:
            return

        def _do():
            try:
                self._chunks.put_nowait(_FLUSH)
            except asyncio.QueueFull:
                pass
        self._loop.call_soon_threadsafe(_do)

    async def drain(self, timeout: float = 15.0) -> None:
        """Wartet, bis alle Chunks verarbeitet und alle Zeilen ausgegeben sind (für CLI und Tests)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            busy = ((self._chunks is not None and not self._chunks.empty()) or bool(self._deferred)
                    or any(not p.done for p in self._pending))
            if not busy:
                self._flush_lines()
                if not self._pending:
                    return
            await asyncio.sleep(0.05)

    def status(self) -> dict:
        return dict(self._status)

    def summary_stats(self) -> dict:
        s = self.stats
        lag = sorted(s.lag_ms)
        hl = sorted(s.hint_latency_ms)

        def pct(a, p):
            return round(a[min(len(a) - 1, int(p * (len(a) - 1)))]) if a else None
        return {"stt_requests": s.stt_requests, "stt_seconds": round(s.stt_seconds, 1),
                "stt_cost_usd": round(s.stt_cost_usd, 4), "stt_errors": s.stt_errors, "stt_skipped": s.stt_skipped,
                "llm_requests": s.llm_requests, "llm_cost_usd": round(s.llm_cost_usd, 4), "llm_errors": s.llm_errors,
                "llm_tokens": s.llm_tokens, "searches": s.searches,
                "cost_by_kind": {k: [v[0], round(v[1], 4)] for k, v in s.cost_by_kind.items()}, "lines": s.lines,
                "lines_deduped": s.lines_deduped, "bleed_skipped": s.bleed_skipped, "cards": s.cards, "cards_rejected": s.cards_rejected,
                "hint_calls": s.hint_calls, "hint_silent": s.hint_silent,
                "lag_ms_median": pct(lag, 0.5), "lag_ms_p90": pct(lag, 0.9),
                "hint_latency_ms_median": pct(hl, 0.5), "hint_latency_ms_p90": pct(hl, 0.9)}

    # ================================================================== Hilfen
    def _spawn(self, coro) -> asyncio.Task | None:
        if self._closed or self._loop is None:
            coro.close()
            return None
        t = self._loop.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    def _emit(self, cb, payload):
        if not cb:
            return
        try:
            r = cb(payload)
            if inspect.isawaitable(r):
                self._spawn(_swallow(r))
        except Exception:
            log.exception("Callback-Fehler")

    def _set_status(self, force: bool = False, **kw):
        new = {**self._status, **kw}
        if force or new != self._status:
            self._status = new
            self._emit(self.on_status, dict(new))

    def _segment(self, sid):
        return next((s for s in self.plan if s.get("id") == sid), None)

    # ================================================================== Mitschrift
    async def _chunk_worker(self):
        while True:
            item = await self._chunks.get()
            try:
                if item is _FLUSH:
                    flushed = [u for c in self.cutters.values() for u in c.flush()]
                    if not self.paused:
                        for u in flushed:
                            self._gate(u)
                        for i, (u, _) in enumerate(self._deferred):
                            self._deferred[i] = (u, 0.0)  # alle Spuren sind zu Ende: sofort entscheiden
                        self._retry_deferred()
                    continue
                self._process_chunk(*item)
            except Exception:
                log.exception("Chunk-Verarbeitung")
                self._set_status(stt="fehler", message="Mitschrift: Audio nicht lesbar")

    def _process_chunk(self, track_id, speaker, sr, fmt, first_frame, pcm, track_start_ms):
        cut = self.cutters.get(track_id)
        if cut is None or cut.sr != sr or cut.fmt != fmt:
            cut = UtteranceCutter(track_id, speaker, sr, fmt, track_start_ms)
            self.cutters[track_id] = cut
        cut.speaker = speaker
        utts = cut.push(first_frame, pcm)
        if self.paused:
            if cut.in_utt:
                cut.flush()
            return
        for u in utts:
            self._gate(u)
        self._retry_deferred()

    # ---------------------------------------------------------------- Übersprechen vor der Mitschrift
    def _bleed_check(self, u: Utterance, force: bool = False):
        """Vergleicht die Rahmenpegel mit den anderen Spuren derselben Zeit.

        Liefert ``None`` (noch nicht entscheidbar), ``"drop"`` (fast nur Übersprechen) oder die Äußerung, in der
        Übersprech-Rahmen stark abgesenkt sind (damit die Mitschrift sie nicht mitschreibt)."""
        others = [c for tid, c in self.cutters.items() if tid != u.track_id and c.next_src_frame is not None]
        if not others or u.frame_db is None or not len(u.frame_db):
            return u
        if not force and any(c.position_ms < u.end_ms - 40 for c in others):
            return None
        times = u.start_ms + np.arange(len(u.frame_db)) * 20.0 + 10.0
        other = np.full(len(times), -200.0)
        for c in others:
            other = np.fmax(other, np.nan_to_num(c.db_at(times), nan=-200.0))
        bleed = u.frame_db < other - self.cfg.bleed_db
        mask = u.speech_mask if u.speech_mask is not None else np.ones(len(bleed), bool)
        n_speech = int(mask.sum())
        if n_speech and (bleed & mask).sum() / n_speech >= self.cfg.bleed_share:
            return "drop"
        if bleed.any():
            # Übersprechen am Rand abschneiden (Zeiten stimmen dann), in der Mitte stark absenken
            good = ~bleed & mask
            if good.any():
                n = len(bleed)
                i, j = int(np.argmax(good)), n - 1 - int(np.argmax(good[::-1]))
                lo = max(0, i - 5)
                while lo < i and bleed[lo]:
                    lo += 1
                hi = min(n, j + 7)
                while hi > j + 1 and bleed[hi - 1]:
                    hi -= 1
                if lo > 0 or hi < n:
                    start0 = u.start_ms
                    u.audio = u.audio[lo * 320 : hi * 320]
                    u.start_ms, u.end_ms = start0 + lo * 20.0, start0 + hi * 20.0
                    u.frame_db, u.speech_mask, bleed = u.frame_db[lo:hi], mask[lo:hi], bleed[lo:hi]
            gain = np.repeat(np.where(bleed, 0.05, 1.0), 320)[: len(u.audio)]
            if len(gain) < len(u.audio):
                gain = np.pad(gain, (0, len(u.audio) - len(gain)), constant_values=1.0)
            u.audio = (u.audio * gain).astype(np.float32)
        return u

    def _gate(self, u: Utterance):
        res = self._bleed_check(u)
        if res is None:
            self._deferred.append((u, self.server_clock() + self.cfg.bleed_wait_ms))
        elif res == "drop":
            self.stats.bleed_skipped += 1
        else:
            self._enqueue_utterance(res)

    def _retry_deferred(self):
        if not self._deferred:
            return
        now = self.server_clock()
        waiting = []
        for u, deadline in sorted(self._deferred, key=lambda x: x[0].start_ms):
            res = self._bleed_check(u, force=now >= deadline)
            if res is None:
                waiting.append((u, deadline))
            elif res == "drop":
                self.stats.bleed_skipped += 1
            elif not self.paused:
                self._enqueue_utterance(res)
        self._deferred = waiting

    def _enqueue_utterance(self, u: Utterance):
        p = _Pending(u)
        self._pending.append(p)
        self._pending.sort(key=lambda x: x.utt.start_ms)
        self._stt_queue.append(p)
        self._drop_stale()
        self._spawn(self._stt_one())

    def _drop_stale(self):
        """Überlast-Schutz: alte Äußerungen überspringen statt aufstauen."""
        now = self.now_ms()
        dropped = 0
        while self._stt_queue and (len(self._stt_queue) > self.cfg.stt_max_pending
                                   or now - self._stt_queue[0].utt.end_ms > self.cfg.stt_max_age_ms):
            p = self._stt_queue.popleft()
            p.dropped = p.done = True
            dropped += 1
        if dropped:
            self.stats.stt_skipped += dropped
            self._set_status(stt="hinkt", message="Mitschrift hinkt hinterher, ältere Äußerungen übersprungen")

    async def _stt_one(self):
        async with self._stt_sem:
            self._drop_stale()
            if not self._stt_queue:
                return
            p = self._stt_queue.popleft()
            if time.monotonic() < self._stt_block_until:  # nach mehreren Fehlern kurz aussetzen
                p.dropped = p.done = True
                self.stats.stt_skipped += 1
                self._flush_lines()
                return
            prompt = self._stt_prompt(p.utt.speaker)
            try:
                res = await asyncio.wait_for(
                    self.transcriber.transcribe(p.utt.audio, 16000, terms=self.terms, prompt=prompt),
                    timeout=self.cfg.stt_timeout_s + 1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                p.dropped = p.done = True
                self.stats.stt_errors += 1
                self._stt_fail_run += 1
                if self._stt_fail_run >= 3:
                    self._stt_block_until = time.monotonic() + 10
                self._set_status(stt="fehler", message=f"Mitschrift gestört: {describe_error(exc)}")
                self._flush_lines()
                return
            self._stt_fail_run = 0
            self.stats.stt_requests += 1
            self.stats.stt_seconds += res.billed_seconds
            self.stats.stt_cost_usd += res.cost_usd
            p.text = self._clean_text(res.text, p.utt)
            p.done = True
            p.ready_at = self.server_clock()
            if self._status.get("stt") != "ok" and not self._stt_queue:
                self._set_status(stt="ok", message="")
            self._flush_lines()

    @property
    def _pshow(self) -> dict:
        """Show-Profil für die Prompts, mit der Sprache der Sitzung."""
        return {**(self.show or {}), "language": self.language}

    def _stt_prompt(self, speaker: str) -> str:
        last = [l["text"] for l in self.lines[-3:]]
        show = self.show or {}
        title = show.get("title") or "Podcast"
        if self.language == "de":
            base = f"Deutschsprachiger Podcast „{title}“" + (f" mit {show['hosts']}." if show.get("hosts") else ".")
        elif self.language == "en":
            base = f"English-language podcast “{title}”" + (f" with {show['hosts']}." if show.get("hosts") else ".")
        else:
            base = f"Podcast “{title}” ({prompts.lang_name(self.language)})" + (f" – {show['hosts']}." if show.get("hosts") else ".")
        return (base + " " + " ".join(last))[-800:]

    def _clean_text(self, text: str, u: Utterance) -> str | None:
        t = " ".join((text or "").split())
        if not t or not re.search(r"[A-Za-zÄÖÜäöüß0-9]", t):
            return None
        if patterns_for(self.language)[0].search(t) and len(words(t)) <= 12:
            return None
        # sehr kurze Äußerung mit langem Text ist fast immer erfunden
        if u.speech_ms < 600 and len(words(t)) > 8:
            return None
        return t

    def _blocked(self, p: _Pending, now_server: float) -> bool:
        """Wartet die Zeile noch auf eine überlappende Äußerung einer anderen Spur?"""
        if now_server - p.ready_at >= self.cfg.line_hold_ms:
            return False
        u = p.utt
        for q in self._pending:
            if q is p or q.utt.track_id == u.track_id or q.done:
                continue
            if q.utt.start_ms < u.end_ms and q.utt.end_ms > u.start_ms:
                return True
        for v, _ in self._deferred:
            if v.track_id != u.track_id and v.start_ms < u.end_ms and v.end_ms > u.start_ms:
                return True
        for tid, c in self.cutters.items():
            if tid == u.track_id:
                continue
            since = c.active_since_ms
            if since is not None and since < u.end_ms:
                return True
        return False

    def _flush_lines(self):
        """Fertige Zeilen in Reihenfolge ausgeben; Übersprech-Dubletten: die lautere Spur gewinnt."""
        now_server = self.server_clock()
        while self._pending:
            p = self._pending[0]
            if not p.done:
                # eine frühere Äußerung ist noch in Arbeit: spätere nur durchlassen, wenn sie zu lange warten
                later_ready = [q for q in self._pending[1:] if q.done and not q.dropped and q.text
                               and now_server - q.ready_at >= self.cfg.line_hold_ms]
                if not later_ready:
                    return
                self._pending.remove(later_ready[0])
                self._output(later_ready[0])
                continue
            if p.dropped or not p.text:
                self._pending.pop(0)
                continue
            if self._blocked(p, now_server):
                return
            self._pending.pop(0)
            self._output(p)

    def _is_duplicate(self, p: _Pending) -> bool:
        u = p.utt
        cands = [(q.utt, q.text) for q in self._pending if q is not p and q.done and q.text and not q.dropped]
        cands += [(l["_utt"], l["text"]) for l in self.lines[-12:]]
        for v, text in cands:
            if v.track_id == u.track_id:
                continue
            ov = min(u.end_ms, v.end_ms) - max(u.start_ms, v.start_ms)
            if ov <= 0 or ov < 0.4 * min(u.duration_ms, v.duration_ms):
                continue
            if not near_duplicate_text(p.text, text):
                continue
            if v.snr_db >= u.snr_db:
                return True  # andere Spur ist lauter: diese Zeile ist Übersprechen
            for q in self._pending:  # diese ist lauter: die wartende leisere fliegt
                if q.utt is v:
                    q.dropped = True
                    self.stats.lines_deduped += 1
        return False

    def _output(self, p: _Pending):
        if self._is_duplicate(p):
            self.stats.lines_deduped += 1
            return
        u = p.utt
        line = {"speaker": u.speaker, "start_ms": int(round(u.start_ms)), "end_ms": int(round(u.end_ms)), "text": p.text}
        lag = self.server_clock() - (self.rec_start + u.end_ms)
        self.stats.lag_ms.append(lag)
        self.stats.lines += 1
        self.lines.append({**line, "_utt": _UttRef(u)})
        self.chars_since_hint += len(p.text)
        _, unsure, number = patterns_for(self.language)
        if number.search(p.text):
            self.number_pending = True
        if unsure.search(p.text):
            self.question_pending = True
        self._set_status(lag_ms=int(lag))
        if self._jsonl:
            try:
                self._jsonl.write(json.dumps({**line, "track_id": u.track_id}, ensure_ascii=False) + "\n")
                self._jsonl.flush()
            except OSError:
                pass
        self._emit(self.on_line, dict(line))

    # ================================================================== Takt
    async def _ticker(self):
        while True:
            await asyncio.sleep(self.cfg.poll_s)
            try:
                self._retry_deferred()
                self._flush_lines()
                if self.llm is None or self.paused:
                    continue
                now = self.now_ms()
                if (not self._hint_busy and now - self.last_hint_try_ms >= self.cfg.hint_interval_ms
                        and now - self.last_card_ms >= self.cfg.min_card_gap_ms
                        and self.chars_since_hint >= self.cfg.min_new_chars):
                    self.last_hint_try_ms = now
                    self._spawn(self._hint("tick"))
                elif (not self._hint_busy and ((self.number_pending and self.plan) or self.question_pending)
                      and now - self.last_hint_try_ms >= self.cfg.check_min_gap_ms):
                    # eine Zahl fiel oder jemand suchte nach einer Tatsache: kurz prüfen (Korrektur oder Antwort)
                    self.last_hint_try_ms = now
                    self._spawn(self._hint("check"))
                if not self._summary_busy and self._summary_backlog_chars() >= self.cfg.summary_min_chars:
                    self._spawn(self._update_summary())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Takt")

    def _window(self, ms: int) -> list[dict]:
        cut = self.now_ms() - ms
        return [_public(l) for l in self.lines if l["end_ms"] >= cut]

    def _summary_backlog(self) -> list[dict]:
        cut = self.now_ms() - self.cfg.window_ms
        out = []
        for l in self.lines[self.summarized_upto :]:
            if l["end_ms"] < cut:
                out.append(l)
            else:
                break
        return out

    def _summary_backlog_chars(self) -> int:
        return sum(len(l["text"]) for l in self._summary_backlog())

    def _system_blocks(self, task_system: str) -> list:
        # stabiler Präfix: Regeln + Show (immer gleich), dann Plan mit Dossiers (gleich bis set_plan)
        return [
            {"type": "text", "text": task_system + "\n\n" + prompts.show_block(self._pshow)},
            {"type": "text", "text": self.plan_text, "cache_control": {"type": "ephemeral"}},
        ]

    def _account(self, res, kind: str = ""):
        entry = self.stats.cost_by_kind.setdefault(kind or "sonst", [0, 0.0])
        entry[0] += 1
        entry[1] += res.cost_usd
        log.debug("Aufruf %s: %s Token, %.4f $", kind, res.usage, res.cost_usd)
        self.stats.llm_requests += 1
        self.stats.llm_cost_usd += res.cost_usd
        self.stats.searches += res.searches
        for k in self.stats.llm_tokens:
            self.stats.llm_tokens[k] += res.usage.get(k, 0) or 0

    def _ai_failed(self, exc, what: str):
        self.stats.llm_errors += 1
        self._set_status(ai="fehler", message=f"{what} gestört: {describe_error(exc)}")

    async def _hint(self, mode: str):
        """``tick``: Takt-Hinweis; ``check``: Korrektur einer gerade genannten Zahl oder Antwort auf eine Wissenslücke;
        ``pulse``: „Impuls jetzt“."""
        if self.llm is None:
            self._set_status(ai="aus", message="Hinweise nicht eingerichtet")
            return
        if mode != "pulse" and (self._hint_busy or self._hint_lock.locked()):
            return
        async with self._hint_lock:  # „Impuls jetzt“ wartet auf einen laufenden Takt-Hinweis
            self._hint_busy = True
            try:
                await self._hint_call(mode)
            finally:
                self._hint_busy = False

    async def _hint_call(self, mode: str):
        t0 = time.monotonic()
        chars_at_start = self.chars_since_hint
        self.number_pending = False
        self.question_pending = False
        try:
            now = self.now_ms()
            current = self._segment(self.current_id)
            elapsed = now - self.segment_since_ms if current and self.segment_since_ms is not None else None
            window = {"pulse": self.cfg.pulse_window_ms, "check": self.cfg.check_window_ms}.get(mode, self.cfg.window_ms)
            lines = self._window(window)
            if mode == "pulse" and not lines:
                lines = [_public(l) for l in self.lines[-6:]]
            user = prompts.live_user_message(mode=mode, current=current, segment_elapsed_ms=elapsed, now_ms=now,
                                             summary=self.summary, lines=lines, cards=self.cards,
                                             host_name=self.host_name, show=self._pshow)
            self.stats.hint_calls += 1
            res = await self.llm.json_call(model=self.model, system=self._system_blocks(prompts.live_system(self._pshow)),
                                           messages=[{"role": "user", "content": user}], schema=LIVE_CARD_SCHEMA,
                                           effort=self.cfg.hint_effort, max_tokens=3000,
                                           timeout=self.cfg.llm_timeout_s)
            self._account(res, mode)
            self.stats.hint_latency_ms.append((time.monotonic() - t0) * 1000)
            if mode != "check":
                self.chars_since_hint = max(0, self.chars_since_hint - chars_at_start)
            if self._status.get("ai") != "ok":
                self._set_status(ai="ok", message="")
            data = res.data or {}
            raw = data.get("karte")
            ask = data.get("nachschlagen")
            log.info("Hinweis %s: %s | %s | nachschlagen=%s", mode, data.get("anlass"), raw, ask)
            self._note_said(data.get("genannt"))
            if not raw:
                self.stats.hint_silent += 1
                if ask and self._auto_lookup(ask):
                    return
                if mode == "pulse":
                    self._set_status(message="Kein Impuls zum Gesagten gefunden", force=True)
                return
            card = self._validate_card(raw, mode=mode, recent=self._window(self.cfg.pulse_window_ms),
                                       art=data.get("art"))
            if card:
                self._publish_card(card, user_triggered=(mode == "pulse"))
            elif mode == "pulse":
                self._set_status(message="Kein neuer Impuls zum Gesagten gefunden", force=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._ai_failed(exc, "Hinweise")

    _SAID_LISTS = {"frage": "fragen", "fakt": "fakten", "zahl": "zahlen", "aufhaenger": "aufhaenger"}

    def _note_said(self, items) -> None:
        """Vom Modell gemeldete Dossier-Punkte, die im Gespräch gefallen sind: prüfen, merken, neue weitergeben."""
        new = []
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            seg = self._segment(it.get("abschnitt"))
            key = self._SAID_LISTS.get(it.get("art"))
            nr = it.get("nr")
            if not seg or not key or not isinstance(nr, int):
                continue
            if not 1 <= nr <= len((seg.get("dossier") or {}).get(key) or []):
                continue
            ref = (seg["id"], it["art"], nr)
            if ref not in self.said:
                self.said.add(ref)
                new.append({"segment_id": seg["id"], "art": it["art"], "nr": nr})
        if new:
            self._emit(self.on_said, new)

    def _auto_lookup(self, query: str) -> bool:
        """Das Modell kennt die Antwort auf eine Wissenslücke nicht sicher: selbst nachschlagen. Gedrosselt, und kein
        Begriff zweimal. -> ob eine Suche gestartet wurde."""
        q = " ".join((query or "").split())[:120]
        now = self.now_ms()
        if (len(q) < 4 or len(self.auto_queries) >= self.cfg.auto_lookup_max
                or now - self.last_auto_lookup_ms < self.cfg.auto_lookup_min_gap_ms
                or any(card_overlap(q, old) >= 0.6 for old in self.auto_queries)):
            return False
        self.auto_queries.append(q)
        self.last_auto_lookup_ms = now
        self._set_status(message=f"Schlägt nach: {q}", force=True)
        return self._spawn(self._lookup(q, auto=True)) is not None

    def _validate_card(self, raw: dict, *, mode: str, recent: list[dict], art: str | None = None) -> dict | None:
        """Form, Grenzen, Belege und Wiederholungsschutz hart durchsetzen. ``None`` = verwerfen."""
        kind = raw.get("kind")
        allowed = {"tick": {"stichwort", "frage", "fakt"}, "pulse": {"stichwort", "frage", "fakt"},
                   "check": {"fakt"}}.get(mode, {"recherche"})
        if kind not in allowed:
            self.stats.cards_rejected += 1
            return None
        answer = art == "antwort" and kind == "fakt"  # Antwort auf eine Wissenslücke
        starts = ("korrektur", prompts.correction_word(self.language).lower())  # „Korrektur“ oder das Wort der Show-Sprache
        if mode == "check" and not (answer or art == "korrektur"
                                    or (raw.get("text") or "").lstrip().lower().startswith(starts)):
            self.stats.cards_rejected += 1
            return None
        text = clip_text(raw.get("text") or "", TEXT_MAX)
        detail = clip_text(raw.get("detail") or "", DETAIL_MAX)
        if len(text) < 8:
            self.stats.cards_rejected += 1
            return None
        basis = raw.get("basis")
        url = raw.get("quelle_url") or None
        if mode == "lookup":
            basis = "netz"
        elif basis not in ("dossier", "allgemein"):
            basis = "allgemein"
        if basis == "dossier":
            key = normalize_url(url)
            url = self.dossier_urls.get(key) if key else None
            if kind == "fakt" and not self.dossier_urls:
                basis = "allgemein"
        elif basis == "allgemein":
            url = None
        # Wiederholung früherer Karten
        for c in self.cards[-30:]:
            if card_overlap(text, c["text"]) >= 0.6:
                self.stats.cards_rejected += 1
                return None
        # nichts, was gerade schon gesagt wurde (nur Stichworte und Fakten; Fragen nehmen das Thema bewusst auf,
        # und eine Antwort wiederholt zwangsläufig die Wörter der Frage)
        if kind in ("stichwort", "fakt") and recent and not answer:
            cw = content_words(text)
            said = set()
            for l in recent:
                said |= content_words(l["text"])
            if len(cw) >= 3 and len(cw & said) / len(cw) >= 0.8:
                self.stats.cards_rejected += 1
                return None
        return {"id": new_id(), "kind": kind, "text": text, "detail": detail or None, "basis": basis,
                "quelle_url": url, "segment_id": self.current_id, "server_ms": int(self.server_clock())}

    def _publish_card(self, card: dict, *, user_triggered: bool):
        now = self.now_ms()
        self.cards.append({**card, "session_ms": now})
        self.stats.cards += 1
        self.last_card_ms = now
        self.last_hint_try_ms = now  # Takt beginnt nach jeder Karte neu
        self._emit(self.on_card, dict(card))

    async def _update_summary(self):
        if self.llm is None or self._summary_busy:
            return
        backlog = self._summary_backlog()
        if not backlog:
            return
        self._summary_busy = True
        try:
            res = await self.llm.json_call(model=self.model, system=[{"type": "text", "text": prompts.summary_system(self._pshow)}],
                                           messages=[{"role": "user", "content": prompts.summary_user_message(
                                               self.summary, [_public(l) for l in backlog])}],
                                           schema=SUMMARY_SCHEMA, effort="low", max_tokens=3000,
                                           timeout=self.cfg.llm_timeout_s)
            self._account(res, "summary")
            text = (res.data or {}).get("kurzfassung") or ""
            if text.strip():
                self.summary = clip_text(text, 2000)
                self.summarized_upto += len(backlog)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._ai_failed(exc, "Kurzfassung")
            # Rückfall: Mitschrift-Rohtext anhängen, damit nichts verloren geht und der Takt nicht hängt
            raw = " ".join(f"{l['speaker']}: {l['text']}" for l in backlog)
            self.summary = (self.summary + " " + raw)[-2000:]
            self.summarized_upto += len(backlog)
        finally:
            self._summary_busy = False

    async def _lookup(self, query: str, auto: bool = False):
        """``auto``: vom Begleiter selbst ausgelöst. Dann erscheint nur eine Karte, wenn die Suche etwas Belegtes
        ergibt, das nicht schon auf einer Karte stand."""
        if self.llm is None:
            self._set_status(ai="aus", message="Nachschlagen nicht eingerichtet")
            return
        try:
            today = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%d")
            user = prompts.lookup_user_message(query, today, self._window(self.cfg.pulse_window_ms)[-6:],
                                               self._segment(self.current_id), self._pshow)
            res = await self.llm.json_call(
                model=self.model, system=[{"type": "text", "text": prompts.lookup_system(self._pshow)}],
                messages=[{"role": "user", "content": user}], schema=LOOKUP_SCHEMA, effort="low", max_tokens=4000,
                tools=[{"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": 1,
                        "user_location": {"type": "approximate", "country": "DE", "timezone": "Europe/Berlin"}}],
                timeout=self.cfg.lookup_timeout_s)
            self._account(res, "lookup")
            data = res.data or {}
            key = normalize_url(data.get("quelle_url"))
            src = res.sources.get(key) if key else None
            text = data.get("text") or ""
            if not src and not text.startswith(tuple(prompts.NOTHING_FOUND.values()) + (prompts.nothing_found(self.language),)):
                text = f"{prompts.nothing_found(self.language)}: {query}"
                data["detail"] = ""
            card = {"kind": "recherche", "text": text, "detail": data.get("detail") or "",
                    "quelle_url": src.url if src else None}
            out = self._validate_lookup(card)
            if auto and (not src or any(card_overlap(out["text"], c["text"]) >= 0.6 for c in self.cards[-30:])):
                self._set_status(ai="ok", message="" if src else f"Nichts Belastbares gefunden: {query}", force=True)
                return
            self._publish_card(out, user_triggered=not auto)
            if auto or self._status.get("ai") != "ok":
                self._set_status(ai="ok", message="", force=auto)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._ai_failed(exc, "Nachschlagen")

    def _validate_lookup(self, raw: dict) -> dict:
        return {"id": new_id(), "kind": "recherche", "text": clip_text(raw["text"], TEXT_MAX),
                "detail": clip_text(raw.get("detail") or "", DETAIL_MAX) or None, "basis": "netz",
                "quelle_url": raw.get("quelle_url"), "segment_id": self.current_id,
                "server_ms": int(self.server_clock())}


_FLUSH = object()
_HALLU_EN = re.compile(r"(subtitles? (by|in)|amara\.org|thank(s| you) for (watching|listening)|please subscribe|"
                       r"subscribe to|like and subscribe|copyright|see you (in the )?next (time|episode|video)|"
                       r"transcribed by|captions? by|www\.)", re.I)
_UNSURE_EN = re.compile(
    r"\b(how (many|much|high|big|large|long|old|far|tall|expensive)|since when|"
    r"when (was|is|did|were|has|have|will)|who (was|is|has|did|were)|what (was|is) (it|that|the name)|"
    r"what('s| is) it called|where (is|was|are|were)|what does .{1,30} (cost|mean)|"
    r"i (don'?t|do not) (really )?know|no idea|not (really )?(sure|certain|exactly)|unsure|"
    r"if i (remember|recall) (correctly|right)|off the top of (my|the) head|"
    r"(look|check) (it|that|this) up|google|need to (check|look)|"
    r"roughly|approximately|around about|something like|"
    r"i (think|guess|believe|reckon|suppose),? (it|that|they|there|the)? ?(was|were|is|are|has|had)\b)", re.I)
_NUMBER_EN = re.compile(r"\d|\b(hundred|thousand|million|billion|trillion|percent|per cent|euros?|dollars?|pounds?|"
                        r"half|third|quarter|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty|"
                        r"forty|fifty|sixty|seventy|eighty|ninety)\b", re.I)
_PATTERN_CACHE: dict[str, tuple] = {}


def patterns_for(lang: str) -> tuple:
    """(Halluzinationen der Mitschrift, Wissenslücke, Zahl) für die Sprache der Show. Deutsch und Englisch haben je ein
    Muster; für andere Sprachen gelten beide zusammen."""
    if lang not in _PATTERN_CACHE:
        de, en = (_HALLU, _UNSURE, _NUMBER), (_HALLU_EN, _UNSURE_EN, _NUMBER_EN)
        if lang == "de":
            _PATTERN_CACHE[lang] = de
        elif lang == "en":
            _PATTERN_CACHE[lang] = en
        else:
            _PATTERN_CACHE[lang] = tuple(re.compile(f"(?:{a.pattern})|(?:{b.pattern})", re.I) for a, b in zip(de, en))
    return _PATTERN_CACHE[lang]
# Wissenslücke im Gesagten: Frage nach einer Tatsache oder hörbare Unsicherheit. Großzügig, denn das Modell entscheidet;
# ein bloßes vorangestelltes „ich glaube, dass …“ (Meinung) zählt nicht.
_UNSURE = re.compile(
    r"\b(wie ?viele?|wie hoch|wie groß|wie lange?|wie alt|wie weit|wie teuer|seit wann|"
    r"wann (war|ist|wurde|wurden|hat|haben|kam|gab)|wer (war|ist|hat|wurde)|wie hei(ß|ss)t|wie hie(ß|ss)|"
    r"wo (liegt|ist|war)|was (kostet|hei(ß|ss)t|bedeutet)|"
    r"wei(ß|ss) (ich|man|es) (\w+ ){0,3}nicht|keine ahnung|nicht (\w+ )?(sicher|genau)\b|unsicher|"
    r"wenn ich mich (recht|richtig|nicht)|aus dem kopf|nach(gucken|schauen|schlagen|sehen|lesen)|googeln|"
    r"m(ü|ue)sste(n|st)? (man|ich|wir) (\w+ ){0,3}(gucken|schauen|pr(ü|ue)fen)|"
    r"ungef(ä|ae)hr|circa|zirka|sch(ä|ae)tzungsweise|so um die|irgendwas (um|bei|mit|zwischen|in)|"
    r"(glaube?|meine|sch(ä|ae)tze|denke) ich\b|ich (glaube?|meine|sch(ä|ae)tze),? (es |das |die |der )?(sind|waren|ist|war|liegt|lag|hat|hatte)\b)",
    re.I)
# Zahl im Gesagten: Ziffern oder Zahlwörter mit Größenordnung, Prozent, Währung
_NUMBER = re.compile(r"\d|\b(hundert|tausend|million|milliard|billion|prozent|euro|dollar|hälfte|drittel|viertel|"
                     r"zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn|elf|zwölf|zwanzig|dreißig|vierzig|fünfzig|"
                     r"sechzig|siebzig|achtzig|neunzig)", re.I)


class _UttRef:
    """Nur die Zeitdaten einer Äußerung (ohne Audio), für den Dubletten-Abgleich."""
    __slots__ = ("track_id", "start_ms", "end_ms", "snr_db")

    def __init__(self, u: Utterance):
        self.track_id, self.start_ms, self.end_ms, self.snr_db = u.track_id, u.start_ms, u.end_ms, u.snr_db

    @property
    def duration_ms(self):
        return self.end_ms - self.start_ms


def _public(line: dict) -> dict:
    return {k: v for k, v in line.items() if not k.startswith("_")}


async def _swallow(aw):
    try:
        await aw
    except Exception:
        log.exception("Callback-Fehler")
