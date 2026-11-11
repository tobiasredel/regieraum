"""Generator für synthetische Aufnahmesitzungen (Paket C).

Jeder Sprecher ist ein Signal in Sitzungszeit (Sekunden): Sprache aus tests/assets (zwei synthetische Stimmen, siehe README dort) mit
Sprechabschnitten, optional Übersprechen eines anderen Sprechers, Rauschteppich und Klick-Markern. Die Spuren werden
so abgetastet, wie es ein Browser mit driftender Soundkarte täte: Sample k liegt bei der wahren Serverzeit
T0 + k / (48000 · (1 + ppm·1e-6)). Uhrenpunkte bekommen Netzwerk-Jitter und Ausreißer. Daraus entstehen Chunks
(s24le, 4 s), Manifest und die Wahrheit zum Nachmessen.

Klicks sind bandbegrenzte Impulse (gefensterte sinc), analytisch an beliebiger Zeit ausgewertet, also unabhängig von
jedem Resampler.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

SR = 48000
REC_START = 1_762_000_000_000
ASSETS = Path(__file__).parent / "assets"
CLICK_HALF = 64          # Samples je Seite für die Auswertung der Klick-sinc
CLICK_FC = 0.42          # Grenzfrequenz relativ zur Abtastrate


def load_voice(name: str) -> np.ndarray:
    """Sprach-Asset auf 48 kHz, auf RMS −20 dBFS normiert (über aktive Anteile)."""
    x, sr = sf.read(str(ASSETS / f"speech_{name}.flac"), dtype="float32")
    if sr != SR:
        g = math.gcd(sr, SR)
        x = resample_poly(x, SR // g, sr // g).astype(np.float32)
    env = np.abs(x) > 0.01 * np.max(np.abs(x))
    rms = math.sqrt(float(np.mean(x[env] ** 2)))
    return (x * (10 ** (-20 / 20) / rms)).astype(np.float32)


def click_value(t: np.ndarray, tc: float, amp: float) -> np.ndarray:
    """Bandbegrenzter Impuls bei tc (Sekunden), ausgewertet an t (Sekunden)."""
    u = (t - tc) * SR
    w = np.where(np.abs(u) < CLICK_HALF, 0.5 + 0.5 * np.cos(np.pi * u / CLICK_HALF), 0.0)
    return amp * 2 * CLICK_FC * np.sinc(2 * CLICK_FC * u) * w


def click_template() -> np.ndarray:
    u = np.arange(-CLICK_HALF, CLICK_HALF + 1)
    return click_value(u / SR, 0.0, 1.0).astype(np.float32)


@dataclass
class Speaker:
    pid: str
    name: str
    voice: str                       # "a" oder "b" (Asset)
    level_db: float = 0.0            # Pegel relativ zum Asset
    turns: list = field(default_factory=list)       # [(von_s, bis_s)] Sprechabschnitte in Sitzungszeit
    ppm: float = 0.0                 # Soundkarten-Drift
    noise_db: float = -60.0          # Rauschteppich (dBFS RMS, weiß)
    bleed_from: str | None = None    # pid eines anderen Sprechers
    bleed_db: float = -20.0
    bleed_delay_ms: float = 150.0
    clicks: list = field(default_factory=list)      # [(t_s, amp)]
    tracks: list = field(default_factory=list)      # [(start_s, end_s)] in wahrer Sitzungszeit; leer = ganze Sitzung
    start_error_ms: float = 40.0     # Fehler der Client-Schätzung von start_server_ms
    missing_chunks: list = field(default_factory=list)  # [(track_index, seq)]
    clip_at: list = field(default_factory=list)      # [t_s] kurze Übersteuerung
    clock_jitter_ms: float = 3.0
    clock_outliers: float = 0.1
    with_clock: bool = True
    fmt: str = "s24le"
    sample_rate: int = SR


class Session:
    def __init__(self, root: Path, duration_s: float, speakers: list[Speaker], *, seed: int = 1,
                 sounds: list | None = None, markers: list | None = None, edl: dict | None = None,
                 audio: dict | None = None, tags: dict | None = None):
        self.root = Path(root)
        self.duration_s = duration_s
        self.speakers = speakers
        self.by_pid = {s.pid: s for s in speakers}
        self.rng = np.random.default_rng(seed)
        self.voices = {v: load_voice(v) for v in {s.voice for s in speakers}}
        self.sounds = sounds or []
        self.markers = markers or []
        self.edl = edl or {"trim_start_ms": 0, "trim_end_ms": None, "cuts": []}
        self.audio = {"target_lufs": -16, "true_peak_db": -1.0, "denoise": "normal", "crossgate": True, "mp3_kbps": 160}
        self.audio.update(audio or {})
        self.tags = tags or {"title": "Testfolge", "artist": "Anna Beispiel & Ben Muster", "album": "Zwei am Mikro",
                             "track": 7, "year": 2026, "cover_path": None}

    # --- Signale in Sitzungszeit
    def mask(self, sp: Speaker, t: np.ndarray) -> np.ndarray:
        m = np.zeros(len(t))
        ramp = 0.01
        for a, b in sp.turns:
            m = np.maximum(m, np.clip(np.minimum(t - a, b - t) / ramp, 0, 1))
        return m

    def voice(self, sp: Speaker, t: np.ndarray) -> np.ndarray:
        v = self.voices[sp.voice]
        pos = np.mod(t * SR, len(v) - 1)
        x = np.interp(pos, np.arange(len(v)), v)
        return x * self.mask(sp, t) * 10 ** (sp.level_db / 20)

    def clean(self, sp: Speaker, t: np.ndarray) -> np.ndarray:
        """Eigenes Signal ohne Rauschen und Übersprechen (für Messungen)."""
        return self.voice(sp, t)

    def signal(self, sp: Speaker, t: np.ndarray) -> np.ndarray:
        x = self.voice(sp, t)
        if sp.bleed_from:
            src = self.by_pid[sp.bleed_from]
            x = x + self.voice(src, t - sp.bleed_delay_ms / 1000) * 10 ** (sp.bleed_db / 20)
        for tc, amp in sp.clicks:
            near = np.abs(t - tc) * SR < CLICK_HALF + 2
            if near.any():
                x[near] += click_value(t[near], tc, amp)
        x = x + self.rng.normal(0, 10 ** (sp.noise_db / 20), len(t))
        for tc in sp.clip_at:
            near = np.abs(t - tc) < 0.02
            x[near] = np.sign(np.sin(2 * np.pi * 200 * t[near])) * 1.2
        return x

    # --- Spuren schreiben
    def build(self) -> tuple[Path, dict]:
        ep = self.root / "episode"
        tracks_m = []
        truth = {"tracks": [], "duration_s": self.duration_s}
        for sp in self.speakers:
            spans = sp.tracks or [(-2.0, self.duration_s + 1.0)]
            for ti, (a, b) in enumerate(spans):
                tid = f"{sp.pid}_t{ti}"
                tdir = ep / "tracks" / tid
                (tdir / "chunks").mkdir(parents=True, exist_ok=True)
                fs_true = sp.sample_rate * (1 + sp.ppm * 1e-6)
                t0_server = REC_START + a * 1000.0  # wahre Serverzeit von Sample 0
                total = int((b - a) * fs_true)
                chunk = sp.sample_rate * 4
                chunks = []
                bps = {"s16le": 2, "s24le": 3, "f32le": 4}[sp.fmt]
                for seq, k0 in enumerate(range(0, total, chunk)):
                    k1 = min(total, k0 + chunk)
                    if (ti, seq) in sp.missing_chunks:
                        continue
                    k = np.arange(k0, k1, dtype=np.float64)
                    t = a + k / fs_true
                    x = self.signal(sp, t)
                    (tdir / "chunks" / f"{seq:06d}.pcm").write_bytes(encode(x, sp.fmt))
                    chunks.append({"seq": seq, "first_frame": k0, "frames": k1 - k0})
                points = []
                if sp.with_clock:
                    for k in range(0, total, sp.sample_rate * 10):
                        jit = self.rng.normal(0, sp.clock_jitter_ms)
                        if self.rng.random() < sp.clock_outliers:
                            jit += self.rng.uniform(40, 160)
                        points.append({"sample_index": int(k), "server_ms": int(round(t0_server + k / fs_true * 1000 + jit))})
                tracks_m.append({
                    "id": tid, "participant_id": sp.pid, "participant_name": sp.name, "dir": str(tdir),
                    "sample_rate": sp.sample_rate, "format": sp.fmt, "start_server_ms": int(round(t0_server + sp.start_error_ms)),
                    "total_frames": total, "chunks": chunks, "clock_points": points,
                    "meta": {"headphones": sp.bleed_from is None, "echo_cancellation": False},
                })
                truth["tracks"].append({"id": tid, "pid": sp.pid, "start_s": a, "end_s": a + total / fs_true,
                                        "missing": [(s * 4 * sp.sample_rate / fs_true + a,
                                             min(total, (s + 1) * 4 * sp.sample_rate) / fs_true + a)
                                                    for (i, s) in sp.missing_chunks if i == ti]})
        manifest = {
            "episode_id": "ep_test", "work_dir": str(ep / "work"), "out_dir": str(ep / "out"),
            "rec_start_server_ms": REC_START, "rec_stop_server_ms": REC_START + int(self.duration_s * 1000),
            "tracks": tracks_m, "sounds": self.sounds, "markers": self.markers, "edl": self.edl,
            "audio": self.audio, "tags": self.tags,
        }
        (ep / "work").mkdir(parents=True, exist_ok=True)
        path = ep / "work" / "manifest.json"
        path.write_text(json.dumps(manifest, indent=1))
        return path, truth


def encode(x: np.ndarray, fmt: str) -> bytes:
    if fmt == "f32le":
        return x.astype("<f4").tobytes()
    if fmt == "s16le":
        return np.clip(np.round(x * 32768), -32768, 32767).astype("<i2").tobytes()
    v = np.clip(np.round(x * 8388608), -8388608, 8388607).astype(np.int32)
    b = np.empty((len(v), 3), np.uint8)
    b[:, 0] = v & 0xFF
    b[:, 1] = (v >> 8) & 0xFF
    b[:, 2] = (v >> 16) & 0xFF
    return b.tobytes()


# ---------------------------------------------------------------- Jingles

def make_jingle(path: Path, seconds: float, *, freq: float = 220.0, click_at_s: float = 0.05, level_db: float = -14.0) -> float:
    """Musikähnlicher Jingle (Akkord mit Hüllkurve) mit einem Klick bei click_at_s als Messmarke. → Klickzeit."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    x = np.zeros(n)
    for k, f in enumerate([freq, freq * 1.25, freq * 1.5, freq * 2]):
        x += np.sin(2 * np.pi * f * t + k) * (0.6 ** k)
    x *= 0.5 + 0.5 * np.sin(2 * np.pi * 2.0 * t) ** 2
    env = np.clip(t / 0.2 - 1.0, 0, 1) * np.clip((seconds - t) / 0.05, 0, 1)  # Ton ab 0,2 s
    x = x * env * 10 ** (level_db / 20)
    x += click_value(t, click_at_s, 0.6)
    st = np.stack([x, x * 0.9], axis=1).astype(np.float32)
    sf.write(str(path), st, SR, subtype="PCM_24")
    return click_at_s


def find_clicks(x: np.ndarray, expected_s: list[float], sr: int = SR, search_ms: float = 60.0) -> list[float]:
    """Lage der Klicks (Sekunden) per Kreuzkorrelation mit der Vorlage, nahe den erwarteten Zeiten (parabelgenau)."""
    return find_clicks_sr(x, expected_s, sr, search_ms)


def decode(path: Path, sr: int = SR, channels: int = 2) -> np.ndarray:
    import subprocess
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", str(channels),
                        "-ar", str(sr), "pipe:1"], capture_output=True, check=True)
    return np.frombuffer(r.stdout, dtype="<f4").reshape(-1, channels)


# ---------------------------------------------------------------- Aufruf und Messung

ROOT = Path(__file__).resolve().parent.parent


def run_cli(*args: str, timeout: float = 900) -> tuple[int, list[dict], str]:
    """Startet `python -m pipeline …` wie der Server. → (Exit-Code, Fortschrittszeilen, stderr)."""
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-m", "pipeline", *args], cwd=ROOT, capture_output=True, text=True,
                       timeout=timeout)
    lines = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip()]
    return r.returncode, lines, r.stderr


def click_template_at(sr: int) -> np.ndarray:
    u = np.arange(-CLICK_HALF, CLICK_HALF + 1)
    return click_value(u / sr, 0.0, 1.0).astype(np.float32)


def find_clicks_sr(x: np.ndarray, expected_s: list[float], sr: int, search_ms: float = 60.0) -> list[float]:
    tpl = click_template_at(sr).astype(np.float64)
    half = len(tpl) // 2
    out = []
    for e in expected_s:
        c = int(round(e * sr))
        r = int(search_ms / 1000 * sr)
        a, b = max(0, c - r - half), min(len(x), c + r + half)
        seg = x[a:b].astype(np.float64)
        cc = np.correlate(seg, tpl, mode="valid")
        i = int(np.argmax(np.abs(cc)))
        frac = 0.0
        if 0 < i < len(cc) - 1:
            y0, y1, y2 = abs(cc[i - 1]), abs(cc[i]), abs(cc[i + 1])
            den = y0 - 2 * y1 + y2
            frac = 0.5 * (y0 - y2) / den if den != 0 else 0.0
        out.append((a + i + frac + half) / sr)
    return out


def rms_db(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=np.float64)
    return 10 * math.log10(max(float(np.mean(x * x)), 1e-20))


def intervals_minus(a: list, b: list) -> list:
    """Zeitbereiche a ohne die Bereiche b (Sekunden)."""
    out = []
    for x0, x1 in a:
        pieces = [(x0, x1)]
        for y0, y1 in b:
            nxt = []
            for p0, p1 in pieces:
                if y1 <= p0 or y0 >= p1:
                    nxt.append((p0, p1))
                else:
                    if y0 > p0:
                        nxt.append((p0, y0))
                    if y1 < p1:
                        nxt.append((y1, p1))
            pieces = nxt
        out += pieces
    return out


def take(x: np.ndarray, spans: list, sr: int = SR, pad_s: float = 0.0) -> np.ndarray:
    parts = [x[int((a + pad_s) * sr):int((b - pad_s) * sr)] for a, b in spans if b - a > 2 * pad_s]
    return np.concatenate(parts) if parts else np.zeros(0, x.dtype)
