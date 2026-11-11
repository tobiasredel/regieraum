"""Job-Läufer: ein Job zur Zeit als asyncio-Task, Jobs aus der Tabelle `jobs` (SPEC Abschnitte 5, 9, 10)."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

from . import archive, core, i18n, manifest, ntfy, publishing, util

log = logging.getLogger("podcast.jobs")
ROOT = Path(__file__).resolve().parent.parent
UPLOAD_TIMEOUT_MS = 15 * 60 * 1000
TICK_S = 30
POLL_S = 2


class JobError(Exception):
    """Meldung ist für Menschen gedacht (deutsch)."""


# --- Einreihen (synchron, aus REST und WebSocket nutzbar) ---------------------------------------
def enqueue(db, episode_id: str | None, kind: str, payload: dict | None = None, dedupe: bool = True) -> str:
    if dedupe:
        row = db.one("SELECT id FROM jobs WHERE kind=? AND status IN ('queued','running') AND episode_id IS ?",
                     (kind, episode_id))
        if row:
            return row["id"]
    jid = util.new_id()
    db.run("INSERT INTO jobs(id,episode_id,kind,status,progress,created_at,payload) VALUES(?,?,?,?,0,?,?)",
           (jid, episode_id, kind, "queued", util.now_ms(), util.dumps(payload) if payload else None))
    return jid


def job_public(j: dict, with_log: bool = True, lang: str = "de") -> dict:
    """Job für die Oberfläche. Die Meldung steht deutsch in der Datenbank und wird erst hier in `lang` übersetzt,
    damit ein Sprachwechsel sofort auch für ältere Jobs wirkt."""
    out = {k: j[k] for k in ("id", "episode_id", "kind", "status", "progress", "message", "created_at", "started_at", "finished_at")}
    out["message"] = i18n.tr(lang, out["message"]) if out["message"] else out["message"]
    if with_log:
        out["log"] = (j["log"] or "")[-4000:]
    return out


def start_processing(db, episode_id: str) -> str:
    """Folge auf `processing` setzen und Job `process` einreihen; unfertige Spuren gelten als unvollständig."""
    db.run("UPDATE tracks SET status='incomplete' WHERE episode_id=? AND status IN ('recording','finishing')", (episode_id,))
    core.update_episode(db, episode_id, status="processing")
    return enqueue(db, episode_id, "process")


def check_uploading(db) -> list[str]:
    """Aufnahme beendet: sind alle Spuren komplett (oder 15 Minuten ohne Fortschritt), Job `process` einreihen."""
    started = []
    now = util.now_ms()
    for ep in db.all("SELECT * FROM episodes WHERE status='uploading'"):
        tracks = db.all("SELECT * FROM tracks WHERE episode_id=?", (ep["id"],))
        all_done = bool(tracks) and all(t["status"] in ("complete", "assembled") for t in tracks)
        last = max([ep["rec_stop_server_ms"] or 0] + [t["created_at"] for t in tracks])
        row = db.one("SELECT MAX(c.received_at) AS m FROM chunks c JOIN tracks t ON t.id=c.track_id WHERE t.episode_id=?", (ep["id"],))
        last = max(last, row["m"] or 0)
        if all_done or now - last >= UPLOAD_TIMEOUT_MS:
            if not all_done:
                note = "Nicht alle Spuren waren vollständig hochgeladen, verarbeitet wurde mit dem Vorhandenen"
                report = core.episode_load(ep)["report"] or {}
                report.setdefault("warnings", []).append(note)
                core.update_episode(db, ep["id"], report=report)
            started.append(start_processing(db, ep["id"]))
    return started


# --- Läufer ------------------------------------------------------------------------------------
class Runner:
    def __init__(self, app):
        self.app = app
        self.task: asyncio.Task | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None  # wird in start() im richtigen Loop angelegt
        self._running: dict | None = None  # {"job_id", "episode_id", "proc"} des laufenden Jobs
        self._cancelled: set[str] = set()  # Folgen, deren Jobs gerade abgebrochen werden (kein Fehleralarm)

    @property
    def db(self):
        return self.app.state.db

    @property
    def cfg(self):
        return self.app.state.cfg

    # Start/Stopp
    def reset_stale(self) -> None:
        self.db.run("UPDATE jobs SET status='queued', started_at=NULL, progress=0 WHERE status='running'")

    def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    def wake(self) -> None:
        if self.loop and self._wake is not None:
            self.loop.call_soon_threadsafe(self._wake.set)

    async def on_change(self) -> None:
        """Nach Upload-Ereignissen: Automatik prüfen und den Läufer wecken."""
        if check_uploading(self.db):
            self.wake()

    async def _loop(self) -> None:
        last_tick = 0.0
        while True:
            try:
                if await self.run_one():
                    continue
                if asyncio.get_running_loop().time() - last_tick >= TICK_S:
                    last_tick = asyncio.get_running_loop().time()
                    await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Fehler im Job-Läufer")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), POLL_S)
            except asyncio.TimeoutError:
                pass

    async def tick(self) -> None:
        """Zeitgeber: Upload-Automatik, geplante Veröffentlichungen und Rohdaten ins Archiv."""
        check_uploading(self.db)
        for ep in self.db.all("SELECT id FROM episodes WHERE status='scheduled' AND publish_at<=?", (util.now_ms(),)):
            enqueue(self.db, ep["id"], "publish")
        for ep in archive.due_episodes(self.db, self.cfg):
            enqueue(self.db, ep["id"], "archive")

    # Ausführung
    async def run_one(self) -> bool:
        job = self.db.one("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at, rowid LIMIT 1")
        if job is None:
            return False
        self.db.run("UPDATE jobs SET status='running', started_at=?, progress=0 WHERE id=?", (util.now_ms(), job["id"]))
        handler = getattr(self, "h_" + job["kind"], None)
        self._running = {"job_id": job["id"], "episode_id": job["episode_id"], "proc": None}
        try:
            if handler is None:
                raise JobError(f"Unbekannte Aufgabe: {job['kind']}")
            message = await handler(job)
            self._finish(job["id"], "done", message or "Fertig", 1.0)
        except (JobError, publishing.PublishError, archive.ArchiveError) as exc:
            if job["episode_id"] in self._cancelled:
                self._finish(job["id"], "failed", "Abgebrochen, die Aufnahme wurde verworfen")
            else:
                self._finish(job["id"], "failed", str(exc))
                await self._failed(job, str(exc))
        except Exception as exc:  # noqa: BLE001
            if job["episode_id"] in self._cancelled:
                self._finish(job["id"], "failed", "Abgebrochen, die Aufnahme wurde verworfen")
            else:
                log.exception("Job %s (%s) abgestürzt", job["id"], job["kind"])
                self._finish(job["id"], "failed", f"Unerwarteter Fehler: {exc}")
                await self._failed(job, f"Unerwarteter Fehler: {exc}")
        finally:
            self._running = None
        return True

    async def cancel_episode(self, episode_id: str, wait_s: float = 20.0) -> bool:
        """Alle Jobs einer Folge stoppen (Aufnahme wird verworfen): wartende streichen, den laufenden abbrechen.
        Ein Unterprozess der Pipeline wird beendet; ein Job ohne Unterprozess (Shownotes) darf zu Ende laufen.
        -> True, wenn danach kein Job dieser Folge mehr läuft. Bis `release_cancel` meldet der Läufer für diese
        Folge keine Fehler."""
        self._cancelled.add(episode_id)
        self.db.run("UPDATE jobs SET status='failed', message='Abgebrochen, die Aufnahme wurde verworfen', finished_at=? "
                    "WHERE episode_id=? AND status='queued'", (util.now_ms(), episode_id))
        deadline = asyncio.get_running_loop().time() + wait_s
        killed = False
        while self._running and self._running["episode_id"] == episode_id:
            proc = self._running.get("proc")
            if proc is not None and proc.returncode is None and not killed:
                killed = True
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            if asyncio.get_running_loop().time() >= deadline:
                if proc is not None and proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                return False
            await asyncio.sleep(0.1)
        return True

    def release_cancel(self, episode_id: str) -> None:
        self._cancelled.discard(episode_id)

    def _finish(self, job_id: str, status: str, message: str, progress: float | None = None) -> None:
        if progress is None:
            self.db.run("UPDATE jobs SET status=?, message=?, finished_at=? WHERE id=?", (status, message, util.now_ms(), job_id))
        else:
            self.db.run("UPDATE jobs SET status=?, message=?, progress=?, finished_at=? WHERE id=?",
                        (status, message, progress, util.now_ms(), job_id))

    def _progress(self, job_id: str, progress: float, message: str = "") -> None:
        self.db.run("UPDATE jobs SET progress=?, message=? WHERE id=?", (max(0.0, min(1.0, progress)), message, job_id))

    @property
    def lang(self) -> str:
        return i18n.ui_lang(self.db)

    async def _notify(self, title: str, message: str, priority: str, tags: str, episode_id: str | None) -> None:
        """Der Titel ist ein deutscher Schlüssel und geht in der Sprache der Oberfläche raus; der Text ist schon fertig
        (Folgentitel und übersetzte Meldung, daher hier nicht noch einmal durchs Wörterbuch)."""
        click = f"{self.cfg.base_url}/episodes/{episode_id}" if episode_id else None
        await asyncio.to_thread(ntfy.send, self.cfg, i18n.tr(self.lang, title), message, priority, tags, click)

    def _label(self, ep: dict) -> str:
        """Name der Folge für Benachrichtigungen; ohne Titel „Folge 3“ bzw. „Neue Folge“ in der Sprache der Oberfläche."""
        if ep["title"]:
            return ep["title"]
        return i18n.tr(self.lang, "Folge {n}", n=ep["number"]) if ep["number"] else i18n.tr(self.lang, "Neue Folge")

    async def _failed(self, job: dict, message: str) -> None:
        ep_id = job["episode_id"]
        if not ep_id:
            return
        ep = core.get_episode(self.db, ep_id)
        if ep and job["kind"] in ("process", "remix") and ep["status"] == "processing":
            core.update_episode(self.db, ep_id, status="failed")
        if job["kind"] == "archive" and ep:
            await self._notify("Archivierung fehlgeschlagen", f"{self._label(ep)}: {i18n.tr(self.lang, message)}", "high",
                               "warning", ep_id)
        if job["kind"] in ("process", "remix", "publish") and ep:
            await self._notify("Verarbeitung fehlgeschlagen", f"{self._label(ep)}: {i18n.tr(self.lang, message)}", "high",
                               "warning", ep_id)

    # Pipeline als Unterprozess
    async def run_pipeline(self, job_id: str, stage: str, manifest_path: Path) -> None:
        cmd = ["nice", "-n", "10", sys.executable, "-m", "pipeline", stage, str(manifest_path)]
        await self.run_cmd(job_id, cmd)

    async def run_cmd(self, job_id: str, cmd: list[str]) -> None:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                    cwd=ROOT, limit=1 << 20)
        if self._running and self._running["job_id"] == job_id:
            self._running["proc"] = proc
        lines: list[str] = []
        last_msg = ""
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            lines.append(line)
            del lines[:-300]
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and "progress" in data:
                last_msg = str(data.get("message") or last_msg)
                self._progress(job_id, float(data["progress"]), last_msg)
        rc = await proc.wait()
        self.db.run("UPDATE jobs SET log=? WHERE id=?", ("\n".join(lines)[-20000:], job_id))
        if rc != 0:
            tail = next((ln for ln in reversed(lines) if ln and not ln.startswith("{")), last_msg)
            raise JobError(f"Die Audio-Verarbeitung ist fehlgeschlagen (Code {rc}). {tail}".strip())

    # --- Handler ---
    def _episode(self, job: dict) -> dict:
        ep = core.get_episode(self.db, job["episode_id"]) if job["episode_id"] else None
        if ep is None:
            raise JobError("Folge nicht gefunden")
        return ep

    def _store_report(self, ep_id: str) -> None:
        path = self.cfg.episode_dir(ep_id) / "work" / "report.json"
        ep = core.get_episode(self.db, ep_id)
        fields: dict = {}
        if path.exists():
            report = util.loads(path.read_text(), None)
            if isinstance(report, dict):
                fields["report"] = report
                if isinstance(report.get("duration_ms"), (int, float)):
                    fields["duration_ms"] = int(report["duration_ms"])
        if "duration_ms" not in fields:
            d = core.edited_duration_ms(ep)
            if d is not None:
                fields["duration_ms"] = d
        if ep["number"] is None and ep["source"] == "recorded":
            n = self.db.one("SELECT MAX(number) AS m FROM episodes")["m"] or 0
            fields["number"] = n + 1
        core.update_episode(self.db, ep_id, **fields)

    async def h_process(self, job: dict) -> str:
        ep = self._episode(job)
        core.update_episode(self.db, ep["id"], status="processing")
        try:
            path = manifest.write(self.db, self.cfg, ep["id"])
        except ValueError as exc:
            raise JobError(str(exc)) from exc
        await self.run_pipeline(job["id"], "process", path)
        for t in self.db.all("SELECT id FROM tracks WHERE episode_id=?", (ep["id"],)):
            if (self.cfg.episode_dir(ep["id"]) / "tracks" / t["id"] / "raw.flac").exists():
                self.db.run("UPDATE tracks SET status='assembled' WHERE id=?", (t["id"],))
        self._store_report(ep["id"])
        enqueue(self.db, ep["id"], "transcribe")
        return "Verarbeitung abgeschlossen"

    async def h_remix(self, job: dict) -> str:
        ep = self._episode(job)
        core.update_episode(self.db, ep["id"], status="processing")
        path = manifest.write(self.db, self.cfg, ep["id"])
        await self.run_pipeline(job["id"], "stage2", path)
        self._store_report(ep["id"])
        core.update_episode(self.db, ep["id"], status="review")
        await self._notify("Folge neu gemischt", self._label(core.get_episode(self.db, ep["id"])), "default",
                           "white_check_mark", ep["id"])
        return "Neu gemischt"

    async def h_transcribe(self, job: dict) -> str:
        ep = self._episode(job)
        try:
            path = self.cfg.episode_dir(ep["id"]) / "work" / "manifest.json"
            if not path.exists():
                path = manifest.write(self.db, self.cfg, ep["id"])
            await self.run_pipeline(job["id"], "transcribe", path)
        except (JobError, ValueError) as exc:
            await self._to_review(ep["id"], "ohne Transkript")
            raise JobError(f"Transkription fehlgeschlagen: {exc}") from exc
        enqueue(self.db, ep["id"], "suggest")
        return "Transkript erstellt"

    async def h_suggest(self, job: dict) -> str:
        ep = self._episode(job)
        try:
            result = await asyncio.to_thread(publishing.suggest, self.db, self.cfg, ep["id"])
        except Exception as exc:  # noqa: BLE001
            await self._to_review(ep["id"], "ohne Vorschlag")
            raise JobError(f"Shownotes-Vorschlag fehlgeschlagen: {exc}") from exc
        if result is None:
            await self._to_review(ep["id"], "")
            return "Kein Vorschlag (Schlüssel oder Transkript fehlt)"
        ep = core.get_episode(self.db, ep["id"])
        ai = {**ep["ai"], **{k: v for k, v in result.items() if k != "chapter_titles"}}
        # eigene Kapitelumbenennungen des Hosts bleiben erhalten
        # Kapitel mit Abschnittstitel aus dem Sendeplan behalten ihren Titel
        planned = core.planned_chapter_events(self.db, ep["id"])
        suggested = {k: v for k, v in (result.get("chapter_titles") or {}).items() if k not in planned}
        ai["chapter_titles"] = {**suggested, **ep["ai"].get("chapter_titles", {})}
        fields: dict = {"ai": ai}
        titles = result.get("titles") or []
        if not ep["title"] and titles:
            fields["title"] = titles[0]
        if not ep["summary"] and result.get("summary"):
            fields["summary"] = result["summary"]
        if not ep["description_html"] and result.get("description_html"):
            fields["description_html"] = result["description_html"]
        core.update_episode(self.db, ep["id"], **fields)
        await self._to_review(ep["id"], "")
        return "Vorschlag erstellt"

    async def _to_review(self, ep_id: str, note: str) -> None:
        ep = core.get_episode(self.db, ep_id)
        if ep and ep["status"] == "processing":
            core.update_episode(self.db, ep_id, status="review")
            msg = self._label(ep) + (f" ({i18n.tr(self.lang, note)})" if note else "")
            await self._notify("Folge zur Prüfung bereit", msg, "default", "microphone", ep_id)

    async def h_publish(self, job: dict) -> str:
        ep = self._episode(job)
        await asyncio.to_thread(publishing.publish_episode, self.db, self.cfg, ep["id"],
                                lambda p, m: self._progress(job["id"], p, m))
        await self._notify("Folge veröffentlicht", self._label(ep), "low", "tada", ep["id"])
        return "Veröffentlicht"

    async def h_unpublish(self, job: dict) -> str:
        ep = self._episode(job)
        await asyncio.to_thread(publishing.unpublish_episode, self.db, self.cfg, ep["id"])
        return "Aus dem Feed genommen"

    async def h_archive(self, job: dict) -> str:
        ep = self._episode(job)
        freed = await asyncio.to_thread(archive.archive_episode, self.db, self.cfg, ep["id"])
        return "Rohdaten ins Archiv verschoben" if freed else "Nichts zu archivieren"

    async def h_restore(self, job: dict) -> str:
        ep = self._episode(job)
        await asyncio.to_thread(archive.restore_episode, self.db, self.cfg, ep["id"])
        return "Rohdaten zurückgeholt"

    async def h_import(self, job: dict) -> str:
        url = (util.loads(job["payload"], {}) or {}).get("feed_url")
        if not url:
            raise JobError("Keine Feed-Adresse angegeben")
        return await asyncio.to_thread(publishing.import_feed, self.db, self.cfg, url,
                                       lambda p, m: self._progress(job["id"], p, m))


def create_runner(app) -> Runner:
    r = Runner(app)
    r.reset_stale()
    return r
