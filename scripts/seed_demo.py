#!/usr/bin/env python3
"""Testdaten für die Verwaltungsoberfläche (Paket E).

    ./dev.sh run python scripts/seed_demo.py /tmp/podcast-studio-dev-8195 [--force]

Legt unter dem Datenverzeichnis an: Show-Daten und Cover, fünf Sounds, Einladungen sowie Folgen in allen Zuständen
(review mit echter Mischung, processing, failed, published, importierte). Alles Angelegte trägt die Kennung `demo`
(IDs, GUIDs); `--force` räumt es zuerst weg. Audio wird synthetisch erzeugt (numpy, ffmpeg), die echte Pipeline
wird nicht gebraucht. Formate nach SPEC Abschnitt 2 und 9.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

SR = 48000
DAY = 24 * 3600 * 1000


# --- Klangsynthese -------------------------------------------------------------------------------------
def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    k = np.hanning(n)
    return np.convolve(x, k / k.sum(), mode="same")


def voice(dur: float, f0: float, seed: int) -> np.ndarray:
    """Sprachähnliches Signal: Obertöne mit Tonhöhenverlauf, Silbenhüllkurve, Satzpausen."""
    rng = np.random.default_rng(seed)
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = f0 * (1 + 0.06 * np.sin(2 * np.pi * 0.7 * t + rng.uniform(0, 6)) + 0.03 * np.sin(2 * np.pi * 3.1 * t))
    phase = 2 * np.pi * np.cumsum(f) / SR
    sig = sum(np.sin(k * phase + k) / k**1.15 for k in range(1, 16))
    syl = (0.5 + 0.5 * np.sin(2 * np.pi * (3.6 + 0.7 * np.sin(2 * np.pi * 0.31 * t)) * t + rng.uniform(0, 6))) ** 1.4
    gate = np.zeros(n)
    pos = 0
    while pos < n:
        talk = int(rng.uniform(1.4, 4.5) * SR)
        gate[pos:pos + talk] = 1.0
        pos += talk + int(rng.uniform(0.25, 0.9) * SR)
    gate = _smooth(gate, int(0.03 * SR))
    breath = _smooth(rng.standard_normal(n), 40) * 0.05
    return (sig * syl * gate * 0.22 + breath * gate)


def place(buf: np.ndarray, sig: np.ndarray, start_s: float) -> None:
    a = int(start_s * SR)
    b = min(len(buf), a + len(sig))
    buf[a:b] += sig[: b - a]


def chord(freqs: list[float], dur: float, decay: float, seed: int = 0) -> np.ndarray:
    n = int(dur * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    for i, f in enumerate(freqs):
        st = i * 0.12
        env = np.exp(-(np.maximum(t - st, 0)) * decay) * (t >= st)
        out += np.sin(2 * np.pi * f * t) * env + 0.4 * np.sin(2 * np.pi * 2 * f * t) * env * 0.5
    out *= np.minimum(1, t * 200) * np.minimum(1, (dur - t) * 40)
    return out * 0.18


def write_wav(path: Path, data: np.ndarray, channels: int = 1) -> None:
    import wave

    pcm = np.clip(data, -1, 1)
    pcm = (pcm * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *args], check=True)


def png_cover(path: Path, size: int = 1400) -> None:
    """Schlichtes Testcover in den Farben des Standard-Themas."""
    y, x = np.mgrid[0:size, 0:size]
    img = np.zeros((size, size, 3), dtype=np.uint8)
    img[:] = (0xF5, 0xF7, 0xFA)
    band = (y > size * 0.62) & (y < size * 0.86)
    img[band] = (0x11, 0x18, 0x27)
    cx, cy, r = size * 0.30, size * 0.34, size * 0.17
    img[(x - cx) ** 2 + (y - cy) ** 2 < r * r] = (0x24, 0x47, 0xC9)
    img[(y > size * 0.90) & (y < size * 0.915) & (x > size * 0.08) & (x < size * 0.92)] = (0xC8, 0xA9, 0x6A)
    raw = b"".join(b"\x00" + img[i].tobytes() for i in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")
    path.write_bytes(png)


# --- Inhalte --------------------------------------------------------------------------------------------
TRANSCRIPT = [
    (8.0, 14.5, "Tobias", "Herzlich willkommen, ihr hört Zwei am Mikro, heute mit einem Thema, das wirklich jeden betrifft."),
    (14.8, 25.5, "Tobias", "Es geht um Zuständigkeiten. Wer ist wofür verantwortlich, und warum weiß das am Ende niemand."),
    (26.5, 34.0, "Max", "Das Schöne ist ja, dass jede Stelle für alles zuständig ist, nur nicht für das Problem."),
    (34.4, 42.0, "Max", "Ich habe diese Woche drei Formulare ausgefüllt, die alle dieselben Fragen stellen."),
    (42.5, 49.5, "Max", "Das vierte wollte dann wissen, ob ich die ersten drei wirklich ausgefüllt habe."),
    (50.5, 56.5, "Tobias", "Das klingt nach einem Fall für die Rubrik Blitzlicht, dazu gleich mehr."),
    (59.5, 68.0, "Max", "Vorher noch ganz kurz zur Bahn, weil das bei mir diese Woche wieder Thema war."),
    (68.4, 77.5, "Max", "Der Zug war nicht ausgefallen, er war nur nie vorgesehen, das ist ein Unterschied."),
    (78.0, 80.0, "Tobias", "Moment, wir machen kurz Pause."),
    (95.0, 99.0, "Tobias", "Wir sind wieder da und ich habe mir einen Kaffee geholt."),
    (100.6, 110.5, "Tobias", "Blitzlicht eins: Die Bahn kündigt an, Verspätungen künftig mit neuen Begriffen zu benennen."),
    (111.0, 123.0, "Max", "Statt Verspätung also Verzögerung im Betriebsablauf, das beruhigt jeden Pendler sofort."),
    (125.8, 133.5, "Tobias", "Blitzlicht zwei, ein Rückblick auf die Woche in drei Sätzen, ohne Hoffnung auf Besserung."),
    (135.5, 146.0, "Max", "Jetzt zum Hauptthema, und ja, es hat mit Formularen zu tun, wie sollte es anders sein."),
    (146.4, 157.0, "Tobias", "Wir fassen zusammen: Zuständigkeit ist ein Zustand, den man nicht erreicht, nur beschreibt."),
]


def build_audio(work: Path) -> dict:
    """Erzeugt Mischung, Spuren und die geschnittene Folge. Liefert Längen und Dateien."""
    total_s = 166.5
    n = int(total_s * SR)
    tob = np.zeros(n)
    max_ = np.zeros(n)
    turns = [("T", 8, 26), ("M", 26.5, 50), ("T", 50.5, 57), ("M", 59.5, 78), ("T", 78, 100), ("T", 100.5, 110.5), ("M", 110.8, 124),
             ("T", 125.5, 134), ("M", 135, 146), ("T", 146.2, 158)]
    for i, (who, a, b) in enumerate(turns):
        sig = voice(b - a, 118 if who == "T" else 176, seed=10 + i)
        place(tob if who == "T" else max_, sig, a)
    # Räuspern von Max bei 57,0 bis 59,5 s: Rauschstoß, später herausgeschnitten
    rng = np.random.default_rng(5)
    cough = _smooth(rng.standard_normal(int(1.2 * SR)), 12) * np.hanning(int(1.2 * SR)) * 0.5
    place(max_, cough, 57.6)
    floor = rng.standard_normal(n) * 0.002
    tob_track = tob + floor
    max_track = max_ + np.roll(floor, 777)

    jingles = {
        "intro": chord([261.6, 329.6, 392.0, 523.3], 7.0, 1.1),
        "blitz": chord([659.3, 880.0, 1318.5], 4.0, 1.8),
        "sting": chord([196.0, 293.7], 1.8, 3.0),
        "lacher": _smooth(rng.standard_normal(int(2.2 * SR)), 6) * np.hanning(int(2.2 * SR)) * 0.25,
        "outro": chord([523.3, 392.0, 329.6, 261.6], 8.0, 0.8),
    }
    music = np.zeros(n)
    place(music, jingles["intro"], 0.5)
    place(music, jingles["blitz"], 100.0)
    place(music, jingles["sting"], 125.0)
    place(music, jingles["outro"], 158.0)
    mix = tob + max_ + music
    mix = mix / max(1.0, float(np.max(np.abs(mix))) / 0.9)

    # Mischung als AAC, Spuren als FLAC
    wav = work / "mix.wav"
    write_wav(wav, np.repeat(mix[:, None], 2, axis=1).reshape(-1), channels=2)
    # Schnittliste: Pause 80 bis 95 s, Räuspern 57 bis 59,5 s
    cuts = [(57.0, 59.5), (80.0, 95.0)]
    keep = np.ones(n, dtype=bool)
    for a, b in cuts:
        keep[int(a * SR):int(b * SR)] = False
    cut_mix = mix[keep]
    cwav = work / "cut.wav"
    write_wav(cwav, np.repeat(cut_mix[:, None], 2, axis=1).reshape(-1), channels=2)
    # Peaks: etwa 50 je Sekunde, 8 Bit
    spp = 960
    m = (n // spp) * spp
    blocks = mix[:m].reshape(-1, spp)
    peaks = np.empty(blocks.shape[0] * 2, dtype=np.int16)
    peaks[0::2] = np.round(blocks.min(axis=1) * 127)
    peaks[1::2] = np.round(blocks.max(axis=1) * 127)
    import soundfile as sf

    flacs = {}
    for name, data in (("tobias", tob_track), ("max", max_track)):
        p = work / f"{name}.flac"
        sf.write(str(p), np.clip(data, -1, 1), SR, format="FLAC", subtype="PCM_24")
        flacs[name] = p
    sounds = {}
    for k, sig in jingles.items():
        p = work / f"{k}.wav"
        write_wav(p, np.repeat(sig[:, None], 2, axis=1).reshape(-1), channels=2)
        sounds[k] = p
    return {"total_ms": int(total_s * 1000), "cuts": cuts, "mix_wav": wav, "cut_wav": cwav, "peaks": peaks.tolist(), "spp": spp,
            "flacs": flacs, "sounds": sounds, "n": n}


def session_to_episode(ms: int, cuts: list[tuple[float, float]]) -> int:
    removed = 0
    for a, b in cuts:
        a, b = int(a * 1000), int(b * 1000)
        if ms >= b:
            removed += b - a
        elif ms > a:
            ms = a
    return ms - removed


# --- Hauptprogramm -------------------------------------------------------------------------------------
def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    if not args:
        sys.exit("Aufruf: seed_demo.py <Datenverzeichnis> [--force]")
    os.environ["PS_DATA"] = args[0]
    os.environ.setdefault("PS_JOBS", "0")
    from app import core, soundproc, util
    from app.config import Config
    from app.db import DB

    cfg = Config.from_env()
    cfg.ensure_dirs()
    db = DB(cfg.db_path)
    core.ensure_permanent_invite(db)

    if db.one("SELECT 1 AS x FROM episodes WHERE guid LIKE 'demo-%' LIMIT 1"):
        if not force:
            print("Testdaten sind schon da (mit --force neu anlegen)")
            return
        for ep in db.all("SELECT id FROM episodes WHERE guid LIKE 'demo-%'"):
            for t in ("tracks", "events", "jobs", "costs"):
                db.run(f"DELETE FROM {t} WHERE episode_id=?", (ep["id"],))
            shutil.rmtree(cfg.episode_dir(ep["id"]), ignore_errors=True)
        db.run("DELETE FROM episodes WHERE guid LIKE 'demo-%'")
        for s in db.all("SELECT id FROM sounds WHERE id LIKE 'demo%'"):
            shutil.rmtree(core.sound_dir(cfg, s["id"]), ignore_errors=True)
        db.run("DELETE FROM sounds WHERE id LIKE 'demo%'")
        db.run("DELETE FROM invites WHERE id LIKE 'demo%'")
        db.run("DELETE FROM participants WHERE id LIKE 'demo%'")
        shutil.rmtree(cfg.public_dir / "media", ignore_errors=True)

    now = util.now_ms()
    work = Path(tempfile.mkdtemp(prefix="seed-demo-"))
    try:
        print("Audio wird erzeugt …")
        a = build_audio(work)

        # Show und Cover
        png_cover(cfg.cover_dir / "cover-demo0001.png")
        show = dict(core.get_settings(db, cfg)["show"])
        show.update(owner_email="kontakt@example.org", cover_key="cover-demo0001.png",
                    description="Politisch, sarkastisch, humorvoll: Anna Beispiel und Ben Muster nehmen die Woche auseinander.",
                    copyright="Anna Beispiel und Ben Muster")
        db.set_setting("show", show)

        # Sounds
        spec = [
            ("demosnd-intro", "Intro", "intro", None, "intro", 0.0, 400, "1"),
            ("demosnd-blitz", "Blitzlicht-Jingle", "segment", "Blitzlicht", "blitz", -1.5, 300, "2"),
            ("demosnd-sting", "Zwischenton", "sting", None, "sting", 0.0, 200, "3"),
            ("demosnd-lacher", "Lacher", "sting", None, "lacher", -3.0, 200, "4"),
            ("demosnd-outro", "Outro", "outro", None, "outro", 0.0, 1200, "5"),
        ]
        top = db.one("SELECT COALESCE(MAX(sort),-1) AS m FROM sounds")["m"]
        for i, (sid, label, kind, chap, key, gain, fade, hotkey) in enumerate(spec):
            d = core.sound_dir(cfg, sid)
            d.mkdir(parents=True, exist_ok=True)
            shutil.copy(a["sounds"][key], d / "original.wav")
            dur = soundproc.process_sound(d / "original.wav", d)
            db.run("UPDATE sounds SET hotkey=NULL WHERE hotkey=?", (hotkey,))
            db.run("INSERT INTO sounds(id,label,kind,chapter_title,ext,duration_ms,gain_db,fade_out_ms,sort,hotkey,archived,created_at)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,0,?)", (sid, label, kind, chap, "wav", dur, gain, fade, top + 1 + i, hotkey, now - 30 * DAY))

        # Einladungen: gültig, abgelaufen, widerrufen, benutzt
        for iid, label, exp, rev in (("demoinv-1", "Lena Hartmann", now + 40 * 3600 * 1000, None), ("demoinv-2", "Jonas", now - 2 * DAY, None),
                                      ("demoinv-3", "Sophie Brandt", now + 20 * 3600 * 1000, now - 3600 * 1000), ("demoinv-4", "Kai", now + 10 * 3600 * 1000, None)):
            db.run("INSERT INTO invites(id,token,label,kind,created_at,expires_at,revoked_at) VALUES(?,?,?,?,?,?,?)",
                   (iid, util.new_token(), label, "single", now - 3 * DAY, exp, rev))
        db.run("INSERT INTO participants(id,token_hash,name,role,invite_id,created_at,last_seen_at) VALUES(?,?,?,?,?,?,?)",
               ("demop-guest", util.sha256("demo-guest"), "Kai", "guest", "demoinv-4", now - 3600 * 1000, now - 1800 * 1000))


        # --- Folge 12: review, echte Mischung -------------------------------------------------------
        eid = "demoep-review"
        out = cfg.episode_dir(eid) / "out"
        work_dir = cfg.episode_dir(eid) / "work"
        out.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        ffmpeg("-i", str(a["mix_wav"]), "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out / "mix_uncut.m4a"))
        ffmpeg("-i", str(a["cut_wav"]), "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "160k", "-id3v2_version", "3", str(out / "episode.mp3"))
        (out / "peaks.json").write_text(json.dumps({"sample_rate": SR, "samples_per_peak": a["spp"], "peaks": a["peaks"]}, separators=(",", ":")))
        rec_start = now - 2 * DAY
        total = a["total_ms"]
        edl = {"trim_start_ms": 0, "trim_end_ms": None, "cuts": [{"from_ms": 57000, "to_ms": 59500}, {"from_ms": 80000, "to_ms": 95000}]}
        duration = total - 2500 - 15000
        ev = {}
        for key, kind, off, sound, payload in (
            ("start", "rec.start", 0, None, {}), ("intro", "sound.play", 500, "demosnd-intro", {}), ("pause", "rec.pause", 80000, None, {}),
            ("resume", "rec.resume", 95000, None, {}), ("blitz", "sound.play", 100000, "demosnd-blitz", {}), ("sting", "sound.play", 125000, "demosnd-sting", {}),
            ("marker", "marker", 135000, None, {"label": "Hauptthema", "kind": "chapter"}),
            ("note1", "marker", 41000, None, {"label": "Aussetzer prüfen", "kind": "note"}),
            ("note2", "marker", 118000, None, {"label": "Zahl nachschlagen", "kind": "note"}), ("outro", "sound.play", 158000, "demosnd-outro", {}), ("stop", "rec.stop", 164000, None, {}),
        ):
            ev[key] = core.add_event(db, eid, kind, rec_start + off, sound, None, payload, event_id=f"demoev-{key}")
        chapters = [(0.5, "Intro"), (100.0, "Blitzlicht: Bahn und Begriffe"), (135.0, "Hauptthema: Zuständigkeit"), (158.0, "Outro")]
        (out / "chapters.json").write_text(json.dumps({"version": "1.2.0", "chapters": [{"startTime": round(session_to_episode(int(t * 1000), a["cuts"]) / 1000, 1), "title": ti} for t, ti in chapters]}, ensure_ascii=False))
        (out / "transcript.json").write_text(json.dumps({"segments": [{"start_ms": int(s * 1000), "end_ms": int(e * 1000), "speaker": sp, "text": tx} for s, e, sp, tx in TRANSCRIPT]}, ensure_ascii=False))
        ts = lambda s: f"{int(s // 60):02d}:{s % 60:06.3f}"  # noqa: E731
        (out / "transcript.vtt").write_text("WEBVTT\n\n" + "\n\n".join(f"{ts(s)} --> {ts(e)}\n<v {sp}>{tx}" for s, e, sp, tx in TRANSCRIPT))
        report = {
            "duration_ms": duration, "target_lufs": -16.0, "final_lufs": -16.1, "true_peak_db": -1.3,
            "tracks": [
                {"participant_name": "Tobias", "lufs_before": -24.6, "lufs_after": -19.8, "gain_db": 4.8, "drift_ppm": 12.4, "start_offset_ms": 143.0,
                 "gaps": [], "clip_events": 0, "peak_db": -4.2},
                {"participant_name": "Max", "lufs_before": -29.8, "lufs_after": -19.7, "gain_db": 10.1, "drift_ppm": -31.9, "start_offset_ms": 212.0,
                 "gaps": [[1920000, 144000], [5760000, 48000]], "sample_rate": SR, "clip_events": 2, "peak_db": -0.4},
            ],
            "warnings": ["Spur von Max: 2 Lücken, zusammen 4 Sekunden, mit Stille gefüllt", "Spur von Max: Übersteuerung bei 12:41, bitte anhören"],
        }
        ai = {
            "titles": ["Zuständigkeit ist ein Zustand", "Der Zug war nie vorgesehen", "Verzögerung im Betriebsablauf"],
            "summary": "Max und Tobias klären, wer wofür zuständig ist, warum Formulare Fragen stellen, die sie selbst beantworten, und wie die Bahn Verspätungen künftig nennt.",
            "description_html": "<p>Zuständigkeiten, Formulare und die Bahn: Max und Tobias nehmen die Woche auseinander.</p><ul><li>Blitzlicht: neue Begriffe für Verspätungen</li><li>Hauptthema: Wer ist eigentlich zuständig?</li></ul><p>Mehr unter <a href=\"https://example.org/uebel\">example.org/uebel</a>.</p>",
            "chapter_titles": {ev["blitz"]: "Blitzlicht: Bahn und Begriffe", ev["marker"]: "Hauptthema: Zuständigkeit"},
        }
        db.run("INSERT INTO episodes(id,status,source,number,season,title,summary,description_html,explicit,episode_type,guid,created_at,rec_start_server_ms,rec_stop_server_ms,duration_ms,"
               "edl,ai,report) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (eid, "review", "recorded", 12, None, None, None, None, 0, "full", "demo-review", rec_start, rec_start, rec_start + total, duration,
                util.dumps(edl), util.dumps(ai), util.dumps(report)))
        for kind, usd in (("research", 0.41), ("research", 0.27), ("companion", 0.92), ("shownotes", 0.06)):
            core.add_cost(db, eid, kind, usd)
        for name, pid, track_id in (("Tobias", "demop-host", "demotr-tobias"), ("Max", "demop-max", "demotr-max")):
            tdir = cfg.episode_dir(eid) / "tracks" / track_id
            tdir.mkdir(parents=True, exist_ok=True)
            shutil.copy(a["flacs"][name.lower()], tdir / "raw.flac")
            db.run("INSERT OR IGNORE INTO participants(id,token_hash,name,role,invite_id,created_at,last_seen_at) VALUES(?,?,?,?,?,?,?)",
                   (pid, util.sha256(pid), name, "host" if name == "Tobias" else "guest", None, now - DAY, now - DAY))
            db.run("INSERT INTO tracks(id,episode_id,participant_id,participant_name,sample_rate,format,start_server_ms,total_frames,total_chunks,status,meta,created_at)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (track_id, eid, pid, name, SR, "s24le", rec_start - 120, a["n"], 84, "assembled",
                                                         util.dumps({"headphones": name == "Tobias", "device_label": "Mikrofon", "echo_cancellation": name != "Tobias"}), rec_start))

        # --- Folge 13: processing ----------------------------------------------------------------------
        db.run("INSERT INTO episodes(id,status,source,number,title,explicit,episode_type,guid,created_at,rec_start_server_ms,rec_stop_server_ms,edl,ai) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               ("demoep-proc", "processing", "recorded", 13, "Wochenrückblick mit Gast", 0, "full", "demo-proc", now - 3600 * 1000, now - 5400 * 1000, now - 1800 * 1000,
                util.dumps({"trim_start_ms": 0, "trim_end_ms": None, "cuts": []}), "{}"))
        db.run("INSERT INTO jobs(id,episode_id,kind,status,progress,message,created_at,started_at) VALUES(?,?,?,?,?,?,?,?)",
               ("demojob-proc", "demoep-proc", "process", "running", 0.42, "Stufe 2: Mischen", now - 600000, now - 590000))

        # --- failed ------------------------------------------------------------------------------------
        db.run("INSERT INTO episodes(id,status,source,number,title,explicit,episode_type,guid,created_at,rec_start_server_ms,rec_stop_server_ms,edl,ai) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               ("demoep-failed", "failed", "recorded", None, "Probeaufnahme Mikrofontest", 0, "full", "demo-failed", now - 6 * DAY, now - 6 * DAY, now - 6 * DAY + 600000,
                util.dumps({"trim_start_ms": 0, "trim_end_ms": None, "cuts": []}), "{}"))
        db.run("INSERT INTO jobs(id,episode_id,kind,status,progress,message,created_at,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?)",
               ("demojob-failed", "demoep-failed", "process", "failed", 0.31, "Die Audio-Verarbeitung ist fehlgeschlagen (Code 1). Spur von Max: Datei beschädigt", now - 6 * DAY, now - 6 * DAY, now - 6 * DAY + 90000))

        # --- published (aufgenommen) -----------------------------------------------------------------------
        pdir = cfg.episode_dir("demoep-pub") / "out"
        pdir.mkdir(parents=True, exist_ok=True)
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=330:duration=20", "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", str(pdir / "episode.mp3"))
        db.run("INSERT INTO episodes(id,status,source,number,title,summary,description_html,explicit,episode_type,guid,created_at,published_at,duration_ms,audio_key,audio_bytes,audio_mime,edl,ai) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               ("demoep-pub", "published", "recorded", 11, "Vom Sinn des Wartens", "Eine Stunde über Warteschleifen.", "<p>Warteschleifen, Hotlines und Geduld.</p>", 0, "full", "demo-pub",
                now - 9 * DAY, now - 7 * DAY, 3_240_000, "media/demo-pub/demo-pub.mp3", (pdir / "episode.mp3").stat().st_size, "audio/mpeg",
                util.dumps({"trim_start_ms": 0, "trim_end_ms": None, "cuts": []}), "{}"))

        # Rohdaten der veröffentlichten Folge (für Speicher und Archiv): eine Rohspur und ein Zwischenstand
        tdir = cfg.episode_dir("demoep-pub") / "tracks" / "demotr-pub"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "raw.flac").write_bytes(b"fLaC" + os.urandom(300_000))
        (cfg.episode_dir("demoep-pub") / "work" / "stems").mkdir(parents=True, exist_ok=True)
        (cfg.episode_dir("demoep-pub") / "work" / "stems" / "tobias.wav").write_bytes(os.urandom(120_000))
        db.run("INSERT INTO tracks(id,episode_id,participant_id,participant_name,sample_rate,format,start_server_ms,total_frames,total_chunks,status,meta,created_at)"
               " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("demotr-pub", "demoep-pub", "demop-host", "Tobias", SR, "s24le", now - 9 * DAY, a["n"], 10, "assembled", "{}", now - 9 * DAY))

        # --- importierte Folgen ------------------------------------------------------------------------------
        titles = ["Auftakt: Wer sind wir überhaupt", "Das Beste kommt zum Schluss", "Politik ist, wenn man trotzdem lacht", "Fünf Gründe gegen Gründe",
                  "Bürokratie in drei Akten", "Neues Jahr, alte Fehler", "Das Sommerloch", "Zwischen Tür und Angel"]
        media = cfg.public_dir / "media"
        for i, title in enumerate(titles):
            num = i + 1
            guid = f"demo-imp-{num}"
            key = f"media/{guid}/{guid}.mp3"
            (media / guid).mkdir(parents=True, exist_ok=True)
            if i == 0:
                ffmpeg("-f", "lavfi", "-i", "sine=frequency=262:duration=12", "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "96k", str(media / guid / f"{guid}.mp3"))
                first = media / guid / f"{guid}.mp3"
            else:
                shutil.copy(first, media / guid / f"{guid}.mp3")
            size = first.stat().st_size
            pub = now - (60 + 28 * (len(titles) - i)) * DAY
            db.run("INSERT INTO episodes(id,status,source,number,title,summary,description_html,explicit,episode_type,guid,created_at,published_at,duration_ms,audio_key,audio_bytes,audio_mime,edl,ai,legacy) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (f"demoep-imp{num}", "published", "imported", num, title, f"Folge {num} aus dem bisherigen Feed.", f"<p>{title}.</p>", 0, "full", guid, pub, pub,
                    (2700 + 140 * i) * 1000, key, size, "audio/mpeg", util.dumps({"trim_start_ms": 0, "trim_end_ms": None, "cuts": []}), "{}",
                    util.dumps({"source_feed": "https://example.org/alter-feed.rss", "pub_date_ms": pub})))
        print("Testdaten angelegt: Folge 12 zur Prüfung (/episodes/demoep-review), dazu processing, failed, published und 8 importierte Folgen")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
