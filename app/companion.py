"""Live-Begleiter im Server (SPEC Abschnitt 15): verwaltet die `assist.live.LiveSession` der laufenden Aufnahme.

Grundsatz: Der Begleiter darf die Aufnahme nie stören. Jede Ausnahme aus `assist/` wird hier abgefangen und als
`assist.status` an den Host gemeldet; Aufrufer (Raum, Chunk-Upload) bekommen nie eine Ausnahme zu sehen."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from . import core, i18n, util

log = logging.getLogger("podcast.companion")

FINISH_GRACE_S = 3.0  # nach rec.stop treffen noch die letzten Chunks ein
FINISH_DRAIN_S = 15.0
CARD_FIELDS = ("id", "kind", "text", "detail", "basis", "quelle_url", "segment_id", "server_ms")


def _default_factory(**kw):
    from assist.live import LiveSession

    return LiveSession(**kw)


def card_public(row: dict) -> dict:
    out = {k: row.get(k) for k in CARD_FIELDS}
    out["pinned"], out["dismissed"] = bool(row.get("pinned")), bool(row.get("dismissed"))
    return out


class Companion:
    def __init__(self, app, room):
        self.app, self.room = app, room
        self.factory: Callable = _default_factory  # Tests tauschen sie gegen eine Attrappe
        self.grace_s = FINISH_GRACE_S
        self.drain_s = FINISH_DRAIN_S
        self.session = None
        self.episode_id: str | None = None
        self.rec_start: int | None = None
        self._message = ""
        self._last_status: dict | None = None
        self._finishing: set[asyncio.Task] = set()
        self._starting = False
        self._pending = None  # beendete Sitzung, die noch Chunks annimmt
        self._said: list[dict] = []  # im Gespräch gefallene Dossier-Punkte der laufenden Folge

    # --- Einstellungen und Status ---------------------------------------------------------------
    @property
    def db(self):
        return self.app.state.db

    @property
    def cfg(self):
        return self.app.state.cfg

    def enabled(self) -> bool:
        return bool((self.db.get_setting("assist", {}) or {}).get("enabled"))

    def available(self) -> bool:
        return bool(self.cfg.openai_key)

    def status(self) -> dict:
        st = None
        if self.session is not None:
            try:
                st = dict(self.session.status())
            except Exception:  # noqa: BLE001
                st = None
        if st is None:
            st = {"on": False, "stt": "aus" if not self.available() else "bereit",
                  "ai": "bereit" if self.cfg.anthropic_key else "aus", "lag_ms": None, "message": ""}
            if self.enabled() and not self.available():
                st["message"] = "Der Live-Begleiter ist nicht eingerichtet: Es fehlt der Schlüssel für die Mitschrift"
        st["on"] = self.enabled()
        if self._message:
            st["message"] = self._message
        if st.get("message"):  # Meldungen von Server und Sitzung sind deutsch; die Oberfläche bekommt ihre Sprache
            st["message"] = i18n.tr(i18n.ui_lang(self.db), st["message"])
        return {k: st.get(k) for k in ("on", "stt", "ai", "lag_ms", "message")}

    def status_summary(self) -> dict:
        return {"enabled": self.enabled(), "available": self.available(), "active": self.session is not None}

    def welcome(self) -> dict:
        cards = []
        ep = core.current_episode(self.db)
        if ep is not None:
            cards = [card_public(r) for r in self.db.all(
                "SELECT * FROM cards WHERE episode_id=? AND dismissed=0 ORDER BY server_ms, rowid", (ep["id"],))]
        said = self._said if ep is not None and ep["id"] == self.episode_id else []
        return {"status": self.status(), "cards": cards, "said": list(said)}

    async def _send_status(self, message: str | None = None) -> None:
        if message is not None:
            self._message = message
        msg = {"type": "assist.status", **self.status()}
        self._message = ""
        await self.room.send_hosts(msg)

    async def report_error(self, what: str, exc: BaseException) -> None:
        log.warning("Live-Begleiter: %s: %r", what, exc)
        try:
            await self.room.send_hosts({"type": "assist.status", **{**self.status(), "stt": "fehler",
                                        "message": i18n.tr(i18n.ui_lang(self.db),
                                                           f"Der Live-Begleiter hatte einen Fehler ({what}). Die Aufnahme läuft weiter")}})
        except Exception:  # noqa: BLE001
            pass

    # --- Callbacks der Sitzung ----------------------------------------------------------------------
    async def _on_line(self, line: dict) -> None:
        await self.room.send_hosts({"type": "assist.line", **line})

    async def _on_card(self, card: dict) -> None:
        ep_id = self.episode_id
        if not ep_id:
            return
        c = {k: card.get(k) for k in CARD_FIELDS}
        try:
            self.db.run("INSERT OR REPLACE INTO cards(id,episode_id,server_ms,kind,text,detail,basis,quelle_url,segment_id,"
                        "pinned,dismissed) VALUES(?,?,?,?,?,?,?,?,?,0,0)",
                        (c["id"], ep_id, c["server_ms"] or util.now_ms(), c["kind"], c["text"], c["detail"], c["basis"],
                         c["quelle_url"], c["segment_id"]))
        except Exception as exc:  # noqa: BLE001
            log.warning("Karte nicht gespeichert: %r", exc)
        await self.room.send_hosts({"type": "assist.card", **card_public({**c, "pinned": 0, "dismissed": 0})})

    async def _on_said(self, items: list[dict]) -> None:
        """Dossier-Punkte, die im Gespräch gefallen sind: Der Host sieht sie im Dossier grau."""
        self._said.extend(items)
        await self.room.send_hosts({"type": "assist.said", "items": items})

    async def _on_status(self, st: dict) -> None:
        await self._send_status()

    # --- Lebenszyklus --------------------------------------------------------------------------------
    def _plan(self, episode_id: str) -> list[dict]:
        return [{k: s[k] for k in ("id", "kind", "title", "input", "notes", "dossier", "good_type")}
                for s in core.plan_of(self.db, episode_id)]

    async def _create(self, episode_id: str, rec_start: int) -> None:
        """Sitzung anlegen und starten, wenn der Begleiter an und eingerichtet ist."""
        if self.session is not None or not self.enabled() or not self.available():
            return
        try:
            show = core.assist_show(self.db, self.cfg)
            session = self.factory(
                rec_start_server_ms=rec_start, on_line=self._on_line, on_card=self._on_card, on_status=self._on_status,
                on_said=self._on_said,
                work_dir=self.cfg.episode_dir(episode_id) / "work", show=show, language=show["language"],
                host_name=core.host_first_name(self.db), server_clock=util.now_ms)
            if self.episode_id != episode_id:
                self._said = []
            self.session, self.episode_id, self.rec_start = session, episode_id, rec_start
            await session.start()
            session.set_plan(self._plan(episode_id))
            cur = self.room.current_segment_id
            if cur:
                session.set_current_segment(cur)
        except Exception as exc:  # noqa: BLE001
            self.session = None
            await self.report_error("Start", exc)

    async def on_rec_start(self, episode_id: str, rec_start: int) -> None:
        await self._create(episode_id, rec_start)
        if self.session is not None:
            await self._send_status()

    async def on_rec_stop(self) -> None:
        session, self.session = self.session, None
        if session is None:
            return
        try:
            session.end_of_audio()
        except Exception as exc:  # noqa: BLE001
            await self.report_error("Ende", exc)
        # Chunks, die nach dem Stopp noch eintreffen, werden bis zum Schluss angenommen
        self._pending = session
        task = asyncio.get_running_loop().create_task(self._finish(session))
        self._finishing.add(task)
        task.add_done_callback(self._finishing.discard)

    async def _finish(self, session) -> None:
        try:
            if self.grace_s:
                await asyncio.sleep(self.grace_s)
            session.end_of_audio()
            drain = getattr(session, "drain", None)
            if drain is not None:
                await drain(self.drain_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Live-Begleiter beim Beenden: %r", exc)
        finally:
            try:
                await session.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("Live-Begleiter schließen: %r", exc)
            if self._pending is session:
                self._pending = None

    async def discard_pending(self) -> None:
        """Aufnahme wird verworfen: Nachlauf beendeter Sitzungen abbrechen und sie schließen."""
        tasks = list(self._finishing)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_idle(self) -> None:
        """Wartet, bis beendete Sitzungen geschlossen sind (Tests, Serverstopp)."""
        if self._finishing:
            await asyncio.gather(*list(self._finishing), return_exceptions=True)

    async def close(self) -> None:
        for t in list(self._finishing):
            t.cancel()
        await asyncio.gather(*list(self._finishing), return_exceptions=True)
        session, self.session = self.session, None
        if session is not None:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass

    async def set_enabled(self, on: bool) -> None:
        self.db.set_setting("assist", {**(self.db.get_setting("assist", {}) or {}), "enabled": bool(on)})
        try:
            ep = core.current_episode(self.db)
            recording = ep is not None and ep["status"] == "recording" and ep["rec_start_server_ms"] is not None
            if on and recording:
                await self._create(ep["id"], ep["rec_start_server_ms"])
                if self.session is not None and self.room.is_paused(ep):
                    self._call("set_paused", True)
            elif not on and self.session is not None:
                session, self.session = self.session, None
                await session.close()
        except Exception as exc:  # noqa: BLE001
            await self.report_error("Umschalten", exc)
        msg = None
        if on and not self.available():
            msg = "Der Live-Begleiter ist nicht eingerichtet: Es fehlt der Schlüssel für die Mitschrift"
        await self._send_status(msg)

    # --- Steuerung (nie mit Ausnahme) ---------------------------------------------------------------
    def _call(self, name: str, *args):
        if self.session is None:
            return None
        try:
            res = getattr(self.session, name)(*args)
            if isinstance(res, asyncio.Future):  # Task aus pulse/lookup: Fehler nie unbeachtet lassen
                res.add_done_callback(lambda t: t.cancelled() or t.exception())
            return res
        except Exception as exc:  # noqa: BLE001
            asyncio.get_running_loop().create_task(self.report_error(name, exc))
            return None

    def set_plan_format(self) -> None:
        """Die Namen der Abschnittsarten haben sich geändert: Eine laufende Sitzung rechnet ihren Plan-Text neu."""
        if self.session is not None and self.episode_id:
            try:
                self.session.show = core.assist_show(self.db, self.cfg)
            except Exception:  # noqa: BLE001
                return
            self._call("set_plan", self._plan(self.episode_id))

    def set_plan(self, episode_id: str) -> None:
        if self.session is not None and self.episode_id == episode_id:
            self._call("set_plan", self._plan(episode_id))

    def set_current_segment(self, segment_id: str | None) -> None:
        self._call("set_current_segment", segment_id)

    def set_paused(self, paused: bool) -> None:
        self._call("set_paused", paused)

    async def pulse(self) -> None:
        if self.session is None:
            await self._send_status("Der Begleiter läuft nur während der Aufnahme")
            return
        self._call("pulse")

    async def lookup(self, query: str) -> None:
        if self.session is None:
            await self._send_status("Nachschlagen geht nur, solange der Begleiter läuft")
            return
        self._call("lookup", query)

    def update_card(self, card_id: str, pinned, dismissed) -> dict | None:
        row = self.db.one("SELECT * FROM cards WHERE id=?", (card_id,))
        if row is None:
            return None
        sets, args = [], []
        if pinned is not None:
            sets.append("pinned=?"), args.append(int(bool(pinned)))
        if dismissed is not None:
            sets.append("dismissed=?"), args.append(int(bool(dismissed)))
        if sets:
            self.db.run(f"UPDATE cards SET {', '.join(sets)} WHERE id=?", (*args, card_id))
        return card_public(self.db.one("SELECT * FROM cards WHERE id=?", (card_id,)))

    # --- Audio -----------------------------------------------------------------------------------------
    async def on_chunk(self, track: dict, first_frame: int, pcm: bytes) -> None:
        """Nach dem Speichern eines Chunks aufrufen. Blockiert nicht, wirft nie."""
        try:
            session = self.session or self._pending
            if session is None:
                ep = core.current_episode(self.db)
                if (ep is None or ep["status"] != "recording" or ep["id"] != track["episode_id"] or self._starting
                        or ep["rec_start_server_ms"] is None):
                    return
                self._starting = True  # Neustart des Servers mitten in der Aufnahme: Begleiter nachziehen
                try:
                    await self._create(ep["id"], ep["rec_start_server_ms"])
                finally:
                    self._starting = False
                session = self.session
                if session is None:
                    return
            if track["episode_id"] != self.episode_id or self.rec_start is None:
                return
            rate = int(track["sample_rate"])
            bps = core.BYTES_PER_FRAME[track["format"]]
            start_ms = track["start_server_ms"] - self.rec_start  # Sitzungszeit von Sample 0
            lead = -(start_ms + first_frame * 1000.0 / rate)  # so viel liegt vor rec.start
            if lead > 0:  # nichts weitergeben, was vor rec.start liegt
                skip = int(-(-lead * rate // 1000))
                if skip * bps >= len(pcm):
                    return
                pcm, first_frame = pcm[skip * bps:], first_frame + skip
            session.feed_chunk(track["id"], track["participant_name"], rate, track["format"], first_frame, pcm, start_ms)
        except Exception as exc:  # noqa: BLE001
            await self.report_error("Mitschrift", exc)
