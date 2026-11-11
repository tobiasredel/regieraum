"""WebSocket-Raum `/ws/p`: Signalisierung, Raumzustand, Aufnahmesteuerung, Jingles (SPEC Abschnitt 6)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect

from . import core, i18n, util
from .companion import Companion
from .errors import ApiError

log = logging.getLogger("podcast.room")

MAX_PEERS = 4
START_LEAD_MS = 1500
SOUND_LEAD_MS = 300
STOP_LEAD_MS = 100
METER_MIN_INTERVAL_S = 0.2
HOST_ONLY = {"rec.start", "rec.pause", "rec.resume", "rec.stop", "sound.play", "sound.stop", "marker"}
# Nachrichten `assist.*` (Live-Begleiter, SPEC Abschnitt 15) gehören ebenfalls nur dem Host


@dataclass
class Conn:
    ws: WebSocket
    p: dict
    state: dict = field(default_factory=lambda: {"mic": True, "cam": False, "headphones": False, "device": "desktop"})
    last_meter: float = 0.0
    pending_clip: bool = False


class Room:
    def __init__(self, app):
        self.app = app
        self.conns: dict[str, Conn] = {}
        self.active: dict[str, dict] = {}  # event_id -> Soundeintrag
        self.recorded: dict[str, int] = {}  # track_id -> vom Client gemeldete Frames
        self._gen = 0
        self._current_segment: tuple[str, str] | None = None  # (episode_id, segment_id)
        self.companion = Companion(app, self)

    @property
    def db(self):
        return self.app.state.db

    # --- Senden ----------------------------------------------------------
    async def _send(self, conn: Conn, msg: dict) -> None:
        try:
            await conn.ws.send_text(json.dumps(msg, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - tote Verbindung räumt der Empfangs-Loop auf
            pass

    async def broadcast(self, msg: dict, exclude: str | None = None) -> None:
        await asyncio.gather(*(self._send(c, msg) for pid, c in list(self.conns.items()) if pid != exclude))

    async def send_hosts(self, msg: dict) -> None:
        await asyncio.gather(*(self._send(c, msg) for c in list(self.conns.values()) if c.p["role"] == "host"))

    async def send_room(self) -> None:
        guest, host = self.state(), self.state(host=True)
        await asyncio.gather(*(self._send(c, {"type": "room", "room": host if c.p["role"] == "host" else guest})
                               for c in list(self.conns.values())))

    async def plan_changed(self, episode_id: str) -> None:
        """Sendeplan oder Dossier hat sich geändert: Hosts und Begleiter nachführen, wenn es die aktuelle Folge ist."""
        ep = core.current_episode(self.db)
        if ep is not None and ep["id"] == episode_id:
            self.companion.set_plan(episode_id)
            await self.send_room()

    @property
    def lang(self) -> str:
        return i18n.ui_lang(self.db)

    async def error(self, conn: Conn, code: str, message: str, **params) -> None:
        """Fehlermeldung an eine Verbindung, in der Sprache der Oberfläche (`message` ist der deutsche Schlüssel)."""
        await self._send(conn, {"type": "error", "code": code, "message": i18n.tr(self.lang, message, **params)})

    # --- Zustand ---------------------------------------------------------
    @property
    def current_segment_id(self) -> str | None:
        """Aktueller Abschnitt der aktuellen Folge; ohne Wahl der zuletzt besuchte."""
        ep = core.current_episode(self.db)
        if ep is None:
            return None
        if self._current_segment and self._current_segment[0] == ep["id"]:
            if self.db.one("SELECT 1 AS x FROM segments WHERE id=?", (self._current_segment[1],)):
                return self._current_segment[1]
        row = self.db.one("SELECT id FROM segments WHERE episode_id=? AND visited_at IS NOT NULL "
                          "ORDER BY visited_at DESC, rowid DESC LIMIT 1", (ep["id"],))
        return row["id"] if row else None

    def is_paused(self, ep: dict) -> bool:
        return self._paused(ep)

    def state(self, host: bool = False) -> dict:
        db = self.db
        ep = core.current_episode(db)
        if ep is None:
            episode = {"id": None, "status": "idle", "rec_start_server_ms": None, "paused": False, "pauses": []}
            tracks: dict[str, dict] = {}
        else:
            start = ep["rec_start_server_ms"]
            pauses = []
            if start is not None:
                for a, b in core.pause_intervals(core.events_of(db, ep["id"]), ep["rec_stop_server_ms"]):
                    pauses.append({"from": a - start, "to": None if b is None else b - start})
            paused = ep["status"] == "recording" and bool(pauses) and pauses[-1]["to"] is None
            episode = {"id": ep["id"], "status": ep["status"], "rec_start_server_ms": start, "paused": paused, "pauses": pauses}
            if ep["status"] == "uploading":  # wer rec.stop verpasst hat, kennt sonst das Ende nicht
                episode["rec_stop_server_ms"] = ep["rec_stop_server_ms"]
            tracks = {}
            for t in core.tracks_of(db, ep["id"]):  # später angelegte Spuren überschreiben frühere
                prog = core.track_progress(db, t)
                tracks[t["participant_id"]] = {
                    "id": t["id"], "status": t["status"], "uploaded_frames": prog["uploaded_frames"],
                    "recorded_frames": max(self.recorded.get(t["id"], 0), prog["end_frame"]),
                    "participant_name": t["participant_name"],
                }
        peers = []
        ids = list(self.conns) + [pid for pid in tracks if pid not in self.conns]
        for pid in ids:
            conn = self.conns.get(pid)
            if conn:
                p, st = conn.p, conn.state
            else:
                row = db.one("SELECT id,name,role FROM participants WHERE id=?", (pid,))
                if not row:
                    continue
                p, st = row, {"mic": False, "cam": False, "headphones": False, "device": "desktop"}
            tr = tracks.get(pid)
            if tr:
                tr = {k: v for k, v in tr.items() if k != "participant_name"}
            peers.append({"id": pid, "name": p["name"], "role": p["role"], "connected": conn is not None,
                          "mic": st["mic"], "cam": st["cam"], "headphones": st["headphones"], "device": st["device"],
                          "track": tr})
        sounds = [core.sound_public(s) for s in core.list_sounds(db)]
        active = [{"event_id": eid, "sound_id": a["sound_id"], "at": a["at"], "duration_ms": a["duration_ms"],
                   "preview": a["preview"]} for eid, a in self.active.items()]
        out = {"episode": episode, "peers": peers, "sounds": sounds, "active_sounds": active}
        if host:  # Sendeplan nur für den Host; Gäste sehen nichts davon
            out["plan"] = [{"id": s["id"], "kind": s["kind"], "title": s["title"], "status": s["status"],
                            "visited": s["visited_at"] is not None, "visited_at": s["visited_at"],
                            "good_type": s["good_type"]}
                           for s in (core.plan_of(db, ep["id"]) if ep else [])]
            out["current_segment_id"] = self.current_segment_id
            out["format"] = core.get_format(db)  # Namen der Abschnittsarten für die Oberfläche
        return out

    async def track_progress(self, track_id: str) -> None:
        t = self.db.one("SELECT * FROM tracks WHERE id=?", (track_id,))
        if not t:
            return
        prog = core.track_progress(self.db, t)
        await self.broadcast({
            "type": "track.progress", "track_id": track_id, "participant_id": t["participant_id"],
            "recorded_frames": max(self.recorded.get(track_id, 0), prog["end_frame"]),
            "uploaded_frames": prog["uploaded_frames"], "status": t["status"],
        })

    # --- Verbindung --------------------------------------------------------
    async def serve(self, ws: WebSocket) -> None:
        await ws.accept()
        p = core.participant_by_token(self.db, ws.query_params.get("token"))
        if p is None:
            await ws.send_text(json.dumps({"type": "error", "code": "unauthorized",
                                           "message": i18n.tr(self.lang, "Zugang abgelaufen oder ungültig. Bitte den Einladungslink erneut öffnen")}))
            await ws.close(code=4401)
            return
        if p["id"] not in self.conns and len(self.conns) >= MAX_PEERS:
            await ws.send_text(json.dumps({"type": "error", "code": "room_full",
                                           "message": i18n.tr(self.lang, "Das Studio ist voll. Es sind höchstens vier Teilnehmer gleichzeitig möglich")}))
            await ws.close(code=4429)
            return
        old = self.conns.get(p["id"])
        conn = Conn(ws=ws, p=core.participant_public(p))
        self.conns[p["id"]] = conn
        if old:
            try:
                await old.ws.close(code=4000)
            except Exception:  # noqa: BLE001
                pass
        self.db.run("UPDATE participants SET last_seen_at=? WHERE id=?", (util.now_ms(), p["id"]))
        is_host = conn.p["role"] == "host"
        welcome = {"type": "welcome", "self": conn.p, "room": self.state(host=is_host), "server_ms": util.now_ms()}
        if is_host:
            welcome["assist"] = self.companion.welcome()
        await self._send(conn, welcome)
        await self.send_room()
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
                        raise ValueError
                except ValueError:
                    await self.error(conn, "bad_message", "Nachricht nicht lesbar")
                    continue
                try:
                    await self.handle(conn, msg)
                except ApiError as e:
                    await self.error(conn, e.code, e.template, **e.params)
                except Exception:  # noqa: BLE001
                    log.exception("Fehler bei %s", msg.get("type"))
                    await self.error(conn, "internal", "Interner Fehler")
        except WebSocketDisconnect:
            pass
        except RuntimeError:  # Verbindung bereits geschlossen
            pass
        finally:
            if self.conns.get(p["id"]) is conn:
                del self.conns[p["id"]]
                self.db.run("UPDATE participants SET last_seen_at=? WHERE id=?", (util.now_ms(), p["id"]))
                await self.send_room()  # Host-Trennung beendet nichts: Aufnahme läuft weiter

    # --- Nachrichten -------------------------------------------------------
    async def handle(self, conn: Conn, msg: dict) -> None:
        t = msg["type"]
        if (t in HOST_ONLY or t.startswith("assist.")) and conn.p["role"] != "host":
            raise ApiError(403, "forbidden", "Das darf nur der Host")
        if t == "sync":
            await self._send(conn, {"type": "sync", "t0": msg.get("t0"), "server_ms": util.now_ms()})
        elif t == "state":
            for k in ("mic", "cam", "headphones"):
                if k in msg:
                    conn.state[k] = bool(msg[k])
            if msg.get("device") in ("desktop", "mobile"):
                conn.state["device"] = msg["device"]
            await self.send_room()
        elif t == "rtc":
            target = self.conns.get(str(msg.get("to")))
            data = msg.get("data")
            if target is None or not isinstance(data, dict) or len(json.dumps(data)) > 65536:
                return
            await self._send(target, {"type": "rtc", "from": conn.p["id"], "data": data})
        elif t == "meter":
            await self._meter(conn, msg)
        elif t == "track.recorded":
            tr = self.db.one("SELECT id FROM tracks WHERE id=? AND participant_id=?", (msg.get("track_id"), conn.p["id"]))
            if tr and isinstance(msg.get("recorded_frames"), int):
                self.recorded[tr["id"]] = msg["recorded_frames"]
                await self.track_progress(tr["id"])
        elif t == "rec.start":
            await self.rec_start()
        elif t == "rec.pause":
            await self.rec_pause()
        elif t == "rec.resume":
            await self.rec_resume()
        elif t == "rec.stop":
            await self.rec_stop()
        elif t == "sound.play":
            await self.sound_play(str(msg.get("sound_id")))
        elif t == "sound.stop":
            await self.sound_stop(str(msg.get("event_id")), msg.get("fade_ms"))
        elif t == "marker":
            await self.marker(str(msg.get("label") or "").strip()[:120])
        elif t.startswith("assist."):
            await self.assist(t, msg)
        else:
            await self.error(conn, "unknown_type", "Unbekannter Nachrichtentyp: {type}", type=t)

    async def _meter(self, conn: Conn, msg: dict) -> None:
        clip = bool(msg.get("clip"))
        now = time.monotonic()
        if now - conn.last_meter < METER_MIN_INTERVAL_S:
            conn.pending_clip = conn.pending_clip or clip
            return
        conn.last_meter = now
        try:
            peak, rms = float(msg.get("peak_db", -100)), float(msg.get("rms_db", -100))
        except (TypeError, ValueError):
            return
        clip = clip or conn.pending_clip
        conn.pending_clip = False
        await self.broadcast({"type": "peer.meter", "id": conn.p["id"], "peak_db": peak, "rms_db": rms, "clip": clip},
                             exclude=conn.p["id"])

    # --- Aufnahme ------------------------------------------------------------
    def _recording(self) -> dict:
        ep = core.current_episode(self.db)
        if ep is None or ep["status"] != "recording":
            raise ApiError(409, "not_recording", "Es läuft keine Aufnahme")
        return ep

    def _paused(self, ep: dict) -> bool:
        iv = core.pause_intervals(core.events_of(self.db, ep["id"]))
        return bool(iv) and iv[-1][1] is None

    async def rec_start(self) -> None:
        ep = core.current_episode(self.db, create=True)
        if ep["status"] == "recording":
            raise ApiError(409, "already_recording", "Die Aufnahme läuft bereits")
        if ep["status"] == "uploading":
            raise ApiError(409, "uploading", "Der Upload der letzten Aufnahme läuft noch")
        start_at = util.now_ms() + START_LEAD_MS
        with self.db.tx() as c:
            c.execute("INSERT INTO events(id,episode_id,kind,server_ms,payload) VALUES(?,?,?,?,?)",
                      (util.new_id(), ep["id"], "rec.start", start_at, "{}"))
            c.execute("UPDATE episodes SET status='recording', rec_start_server_ms=?, rec_stop_server_ms=NULL WHERE id=?",
                      (start_at, ep["id"]))
        await self.companion.on_rec_start(ep["id"], start_at)  # vor der Meldung: Chunks beginnen erst ab rec.start
        await self.broadcast({"type": "rec.start", "episode_id": ep["id"], "start_at": start_at})
        await self.send_room()

    async def rec_pause(self) -> None:
        ep = self._recording()
        if self._paused(ep):
            raise ApiError(409, "already_paused", "Die Aufnahme ist bereits pausiert")
        at = max(util.now_ms(), ep["rec_start_server_ms"])
        core.add_event(self.db, ep["id"], "rec.pause", at)
        self.companion.set_paused(True)
        await self.broadcast({"type": "rec.pause", "at": at})
        await self.send_room()

    async def rec_resume(self) -> None:
        ep = self._recording()
        if not self._paused(ep):
            raise ApiError(409, "not_paused", "Die Aufnahme ist nicht pausiert")
        at = util.now_ms()
        core.add_event(self.db, ep["id"], "rec.resume", at)
        self.companion.set_paused(False)
        await self.broadcast({"type": "rec.resume", "at": at})
        await self.send_room()

    async def rec_stop(self) -> None:
        ep = self._recording()
        at = max(util.now_ms(), ep["rec_start_server_ms"])
        core.add_event(self.db, ep["id"], "rec.stop", at)
        start = ep["rec_start_server_ms"]
        # Pausen werden zu Schnitten in der EDL (Sitzungszeit)
        pause_cuts = [{"from_ms": a - start, "to_ms": b - start}
                      for a, b in core.pause_intervals(core.events_of(self.db, ep["id"]), at) if b is not None and b > a]
        edl = core.normalize_edl(ep["edl"], pause_cuts)
        # läuft noch ein aufgezeichneter Sound, endet die Folge erst mit ihm
        end = at
        for a in self.active.values():
            if not a["preview"]:
                end = max(end, a["end_ms"])
        core.update_episode(self.db, ep["id"], status="uploading", rec_stop_server_ms=end, edl=edl)
        await self.broadcast({"type": "rec.stop", "at": at})
        await self.send_room()
        await self.companion.on_rec_stop()
        await self.app.state.runner.on_change()

    # --- Jingles ---------------------------------------------------------------
    async def sound_play(self, sound_id: str) -> None:
        s = self.db.one("SELECT * FROM sounds WHERE id=? AND archived=0", (sound_id,))
        if s is None:
            raise ApiError(404, "sound_not_found", "Sound nicht gefunden")
        ep = core.current_episode(self.db)
        recording = ep is not None and ep["status"] == "recording"
        if recording and self._paused(ep):
            raise ApiError(409, "paused", "In der Pause sind Jingles gesperrt")
        at = util.now_ms() + SOUND_LEAD_MS
        event_id = util.new_id()
        seg_id = None
        if recording:
            if s.get("segment_kind"):  # springt auf den ersten noch nicht besuchten Abschnitt dieser Art
                seg = core.next_unvisited_segment(self.db, ep["id"], s["segment_kind"])
                seg_id = seg["id"] if seg else None
            core.add_event(self.db, ep["id"], "sound.play", at, sound_id=sound_id, event_id=event_id,
                           payload={"segment_id": seg_id} if seg_id else None)
        self._gen += 1
        self.active[event_id] = {"sound_id": sound_id, "at": at, "duration_ms": s["duration_ms"], "preview": not recording,
                                 "end_ms": at + s["duration_ms"], "fade_out_ms": s["fade_out_ms"], "gen": self._gen}
        self._expire_later(event_id, s["duration_ms"] + SOUND_LEAD_MS)
        await self.broadcast({"type": "sound.play", "event_id": event_id, "sound_id": sound_id, "at": at,
                              "gain_db": s["gain_db"], "duration_ms": s["duration_ms"]})
        if seg_id:
            await self.set_segment(ep["id"], seg_id)
        else:
            await self.send_room()

    async def sound_stop(self, event_id: str, fade_ms) -> None:
        a = self.active.get(event_id)
        if a is None:
            raise ApiError(404, "sound_not_active", "Dieser Sound läuft nicht mehr")
        fade = int(fade_ms) if isinstance(fade_ms, (int, float)) and fade_ms >= 0 else a["fade_out_ms"]
        at = util.now_ms() + STOP_LEAD_MS
        if not a["preview"]:
            ep = core.current_episode(self.db)
            if ep is not None:
                core.add_event(self.db, ep["id"], "sound.stop", at, sound_id=a["sound_id"], ref_event_id=event_id,
                               payload={"fade_ms": fade})
        self._gen += 1
        a["gen"], a["end_ms"] = self._gen, at + fade
        self._expire_later(event_id, STOP_LEAD_MS + fade)
        await self.broadcast({"type": "sound.stop", "event_id": event_id, "at": at, "fade_ms": fade})

    def _expire_later(self, event_id: str, delay_ms: int) -> None:
        gen = self.active[event_id]["gen"]
        asyncio.get_running_loop().call_later(delay_ms / 1000, lambda: asyncio.ensure_future(self._expire(event_id, gen)))

    async def _expire(self, event_id: str, gen: int) -> None:
        a = self.active.get(event_id)
        if a is not None and a["gen"] == gen:
            del self.active[event_id]
            await self.send_room()

    async def marker(self, label: str) -> None:
        ep = self._recording()
        at = util.now_ms()
        label = label or i18n.tr(self.lang, "Marker")
        core.add_event(self.db, ep["id"], "marker", at, payload={"label": label})
        await self.broadcast({"type": "marker", "label": label, "at": at - ep["rec_start_server_ms"]})

    # --- Live-Begleiter (nur Host) ---------------------------------------------------------------------------
    async def set_segment(self, episode_id: str, segment_id: str | None) -> None:
        """Aktuellen Abschnitt setzen (Klick oder Jingle), als besucht vermerken, Begleiter und Hosts benachrichtigen."""
        if segment_id:
            self.db.run("UPDATE segments SET visited_at=? WHERE id=? AND visited_at IS NULL", (util.now_ms(), segment_id))
            self._current_segment = (episode_id, segment_id)
        else:
            self._current_segment = None
        self.companion.set_current_segment(segment_id)
        await self.send_hosts({"type": "assist.segment", "segment_id": segment_id})
        await self.send_room()

    async def assist(self, t: str, msg: dict) -> None:
        if t == "assist.toggle":
            await self.companion.set_enabled(bool(msg.get("on")))
        elif t == "assist.segment":
            ep = core.current_episode(self.db)
            sid = msg.get("segment_id")
            if ep is None:
                raise ApiError(409, "no_episode", "Es gibt keine aktuelle Folge")
            if sid is not None and not self.db.one("SELECT 1 AS x FROM segments WHERE id=? AND episode_id=?", (str(sid), ep["id"])):
                raise ApiError(404, "segment_not_found", "Abschnitt nicht gefunden")
            await self.set_segment(ep["id"], str(sid) if sid is not None else None)
        elif t == "assist.pulse":
            await self.companion.pulse()
        elif t == "assist.lookup":
            query = str(msg.get("query") or "").strip()[:200]
            if not query:
                raise ApiError(422, "invalid", "Bitte einen Begriff zum Nachschlagen angeben")
            await self.companion.lookup(query)
        elif t == "assist.card":
            card = self.companion.update_card(str(msg.get("id")), msg.get("pinned"), msg.get("dismissed"))
            if card is None:
                raise ApiError(404, "card_not_found", "Karte nicht gefunden")
            await self.send_hosts({"type": "assist.card", **card})
        else:
            raise ApiError(400, "unknown_type", "Unbekannter Nachrichtentyp: {type}", type=t)
