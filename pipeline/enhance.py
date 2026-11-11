"""Schritte 3 und 4: Stems verbessern (Hochpass, Rauschunterdrückung, De-Esser, Leveler, Kompressor, Pegelangleich,
Begrenzer) und Crossgate.

Aufbau, bewusst konservativ:

1. Hochpass 80 Hz (Butterworth 4. Ordnung, scipy, Zustand über Blöcke).
2. deep-filter (DeepFilterNet 3) mit Dämpfungsgrenze je Stufe, in 60-s-Stücken mit 3 s Vorlauf parallel (je Prozess
   ein Thread), Stücke 100 ms linear überblendet. Vorher statische Vorverstärkung auf etwa −26 LUFS, weil deep-filter
   16 Bit ausgibt (genug Abstand nach oben und unten).
3. Analyse auf 1-ms-Raster: K-gewichtete Energie (Lautheit), Sprachband 300–3400 Hz (Aktivität), Breitband und
   Höhenanteil über 5,5 kHz (De-Esser).
4. Sprachaktivität je Stem, Übersprech-Modell je Paar (Verzögerung per Kreuzkorrelation der Hüllkurven, Dämpfung als
   Median), daraus „eigene Sprache“ = aktiv und deutlich lauter als das vorhergesagte Übersprechen.
5. Alle Pegeländerungen als eine Verstärkungskurve im 1-ms-Raster (vektorisiert, keine Python-Schleife je Sample):
   statischer Angleich auf −20 LUFS Sprach-Lautheit (gemessen nur über eigene Sprache), langsamer Leveler (±8 dB,
   80 % Korrektur, Gauß-Fenster σ = 2 s nur über eigene Sprache, hält in Pausen), Kompressor 2:1 mit 6 dB Knie
   5 dB über Sprachpegel (Attack 5 ms mit Vorausschau, Release 25 dB/s), Crossgate (max. 15 dB, 60 ms Vorausschau,
   150 ms Halten, Schließen mit 50 dB/s).
6. Rendern: De-Esser als dynamische Absenkung des Höhenanteils (max. 6 dB), Verstärkungskurve, Begrenzer −1 dBFS.

Die Kurven sind numpy statt ffmpeg-Filter, weil sie über mehrere Stems hinweg entschieden werden (Crossgate,
Übersprechen) und weil eine einzige Kurve kein Pumpen durch hintereinander geschaltete Regelungen erzeugen kann.
"""
from __future__ import annotations

import math
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.ndimage import gaussian_filter1d

from . import SR
from .dsp import (K_SOS, LoudnessMeter, SosStream, bandpass_sos, fall_max, highpass_sos, limit_file, lowpass_sos,
                  moving_mean, release_min, window_max, window_min)
from .util import THREADS, PipelineError, db, r1

HOP = 48                      # 1 ms
BLOCK = HOP * 10000           # 10 s
TARGET_SPEECH = -20.0         # Sprach-Lautheit der Stems (LUFS, nur über Sprache)
DENOISE_ATTEN = {"off": None, "leicht": 6.0, "normal": 12.0, "stark": 24.0}
DF_CHUNK = SR * 60
DF_PREROLL = SR * 3
DF_XFADE = int(0.1 * SR)
GATE_MAX_DB = 15.0
DEESS_FC = 5500.0


# ---------------------------------------------------------------- Vorbereitung + Rauschunterdrückung

def highpass_file(src: Path, dst: Path, progress=None) -> dict:
    """Hochpass 80 Hz, misst dabei Lautheit (BS.1770), Spitze und K-Energie je ms."""
    hp = SosStream(highpass_sos(80.0, 4))
    meter = LoudnessMeter(1)
    kst = SosStream(K_SOS)
    peak = 0.0
    ek = []
    with sf.SoundFile(str(src)) as f, sf.SoundFile(str(dst), "w", SR, 1, "FLOAT", format="RF64") as out:
        n = f.frames
        for s in range(0, n, BLOCK):
            x = f.read(BLOCK, dtype="float32").astype(np.float64)
            y = hp(x)
            meter.feed(y)
            peak = max(peak, float(np.max(np.abs(y))) if len(y) else 0.0)
            ek.append(_hop_energy(kst(y)))
            out.write(y.astype(np.float32))
            if progress:
                progress(min(1.0, (s + BLOCK) / max(1, n)))
    return {"lufs": meter.integrated(), "peak": peak, "eK": np.concatenate(ek) if ek else np.zeros(0, np.float32)}


def _hop_energy(y: np.ndarray) -> np.ndarray:
    n = len(y)
    m = -(-n // HOP) * HOP
    if m != n:
        y = np.pad(y, (0, m - n))
    return np.mean((y * y).reshape(-1, HOP), axis=1).astype(np.float32)


def denoise_prepare(src: Path, atten_db: float, pre_gain_db: float, tmpdir: Path) -> dict:
    """Schneidet die Datei in Stücke (60 s plus 3 s Vorlauf für den Zustand des Netzes, 100 ms Überblendrand)."""
    if shutil.which("deep-filter") is None:
        raise PipelineError("deep-filter ist nicht installiert")
    tmpdir.mkdir(parents=True, exist_ok=True)
    g = 10 ** (pre_gain_db / 20)
    pieces = []
    with sf.SoundFile(str(src)) as f:
        n = f.frames
        for i, s in enumerate(range(0, n, DF_CHUNK)):
            e = min(n, s + DF_CHUNK)
            r0 = max(0, s - DF_PREROLL)
            r1 = min(n, e + DF_XFADE)
            f.seek(r0)
            x = f.read(r1 - r0, dtype="float32") * np.float32(g)
            x = np.concatenate([x, np.zeros(SR // 10, np.float32)])  # Rand für die Laufzeitkorrektur (-D)
            p = tmpdir / f"in_{i:04d}.wav"
            sf.write(str(p), x, SR, subtype="FLOAT")
            pieces.append({"s": s, "e": e, "r0": r0, "r1": r1, "in": p, "out": tmpdir / "out" / p.name,
                           "atten": atten_db, "outdir": tmpdir / "out"})
    return {"pieces": pieces, "n": n, "g": g, "tmpdir": tmpdir}


def denoise_run(jobs: list[dict], progress=None) -> None:
    """Alle Stücke aller Stems gemeinsam, höchstens THREADS deep-filter-Prozesse (je einer läuft einfädig)."""
    pieces = [pc for j in jobs for pc in j["pieces"]]
    done = [0]

    def run(pc):
        cmd = ["deep-filter", "-D", "-a", f"{pc['atten']:g}", "-o", str(pc["outdir"]), str(pc["in"])]
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode != 0 or not pc["out"].exists():
            tail = r.stderr.decode("utf-8", "replace").strip().splitlines()[-2:]
            raise PipelineError(f"Rauschunterdrückung fehlgeschlagen ({' / '.join(tail)})")
        pc["in"].unlink(missing_ok=True)
        done[0] += 1
        if progress:
            progress(done[0] / len(pieces))

    with ThreadPoolExecutor(max_workers=THREADS) as ex:
        list(ex.map(run, pieces))


def denoise_finish(job: dict, dst: Path) -> None:
    """Setzt die Stücke mit 100 ms linearer Überblendung zusammen, zurück auf den Pegel vor der Vorverstärkung."""
    inv = np.float32(1.0 / job["g"])
    n = job["n"]
    with sf.SoundFile(str(dst), "w", SR, 1, "FLOAT", format="RF64") as out:
        tail = None  # Überblendbereich des vorherigen Stücks
        for pc in job["pieces"]:
            y, _ = sf.read(str(pc["out"]), dtype="float32")
            pc["out"].unlink(missing_ok=True)
            need = pc["r1"] - pc["r0"]
            if len(y) < need:
                y = np.pad(y, (0, need - len(y)))
            y = y[:need] * inv
            core = y[pc["s"] - pc["r0"]:]  # ab s bis r1 (inkl. Überblendrand)
            if tail is not None:
                k = len(tail)
                w = (np.arange(k, dtype=np.float32) + 0.5) / k
                core = core.copy()
                core[:k] = tail * (1 - w) + core[:k] * w
            body_len = pc["e"] - pc["s"]
            out.write(core[:body_len])
            if pc["e"] < n:
                tail = core[body_len:body_len + DF_XFADE]
                if len(tail) < DF_XFADE:
                    tail = np.pad(tail, (0, DF_XFADE - len(tail)))
            else:
                tail = None
    shutil.rmtree(job["tmpdir"], ignore_errors=True)


def denoise_file(src: Path, dst: Path, atten_db: float, pre_gain_db: float, tmpdir: Path, progress=None) -> None:
    job = denoise_prepare(src, atten_db, pre_gain_db, tmpdir)
    denoise_run([job], progress)
    denoise_finish(job, dst)


# ---------------------------------------------------------------- Analyse

def analyze_file(path: Path, progress=None, keys: tuple = ("eK", "eS", "eF", "eH")) -> dict:
    """Energien je ms: eK (K-gewichtet), eS (Sprachband), eF (breitbandig), eH (Höhen über 5,5 kHz)."""
    k = SosStream(K_SOS)
    bp = SosStream(bandpass_sos(300.0, 3400.0, 2))
    lp = SosStream(lowpass_sos(DEESS_FC, 2))
    out = {key: [] for key in keys}
    with sf.SoundFile(str(path)) as f:
        n = f.frames
        for s in range(0, n, BLOCK):
            x = f.read(BLOCK, dtype="float32").astype(np.float64)
            if "eK" in out:
                out["eK"].append(_hop_energy(k(x)))
            if "eS" in out:
                out["eS"].append(_hop_energy(bp(x)))
            if "eF" in out:
                out["eF"].append(_hop_energy(x))
            if "eH" in out:
                out["eH"].append(_hop_energy(x - lp(x)))
            if progress:
                progress(min(1.0, (s + BLOCK) / max(1, n)))
    return {key: (np.concatenate(v) if v else np.zeros(0, np.float32)) for key, v in out.items()}


def _db(x):
    return 10.0 * np.log10(np.maximum(x, 1e-20))


def _activity(eS: np.ndarray) -> dict:
    s20 = _db(moving_mean(eS, 10, 9))
    valid = moving_mean(eS, 10, 9) > 1e-14
    if valid.sum() < 100:
        z = np.zeros(len(eS), bool)
        return {"s20": s20, "raw": z, "active": z, "noise_db": None, "thr": None}
    nf = float(np.percentile(s20[valid], 10))
    top = float(np.percentile(s20[valid], 99.5))
    thr = max(nf + 10.0, top - 45.0)
    raw = (s20 > thr) & valid
    active = window_max(raw.astype(np.uint8), 200, 20).astype(bool)
    return {"s20": s20, "raw": raw, "active": active, "noise_db": nf, "thr": thr}


def speech_lufs(eK: np.ndarray, own: np.ndarray) -> float | None:
    """Sprach-Lautheit: Energie über eigene Sprache in 400-ms-Blöcken (mindestens 30 % Sprache), Blöcke mehr als 6 dB
    über oder 10 dB unter dem Median verworfen (Lacher, Rumpler, Übersteuerung verzerren sonst den Mittelwert)."""
    n = min(len(eK), len(own))
    eK, own = eK[:n], own[:n]
    if own.sum() < 1000:
        return None
    B = 400
    m = n // B
    e = np.where(own, eK, 0.0)[: m * B].reshape(m, B).sum(axis=1, dtype=np.float64)
    c = own[: m * B].reshape(m, B).sum(axis=1)
    ok = c >= B * 0.3
    if ok.sum() < 3:
        return float(10 * np.log10(max(float(np.mean(eK[own])), 1e-20)) - 0.691)
    e, c = e[ok], c[ok]
    lb = 10 * np.log10(np.maximum(e / c, 1e-20))
    med = float(np.median(lb))
    sel = (lb > med - 10) & (lb < med + 6)
    return float(10 * np.log10(max(e[sel].sum() / c[sel].sum(), 1e-20)) - 0.691)


def _frames(x: np.ndarray, size: int, how: str = "mean") -> np.ndarray:
    m = len(x) // size
    if m == 0:
        return np.zeros(0)
    v = x[:m * size].reshape(m, size)
    return v.mean(axis=1) if how == "mean" else v.max(axis=1)


def _bleed_model(src: dict, dst: dict) -> dict:
    """Übersprechen von src in dst: Verzögerung (Kreuzkorrelation der hochpassgefilterten Log-Hüllkurven, 10-ms-Raster,
    0–800 ms) und Dämpfung (Median von dst − Vorhersage über die lauteren Sprachstellen von src)."""
    a = _frames(src["s20"], 10)
    b = _frames(dst["s20"], 10)
    act = _frames(src["raw"].astype(np.float32), 10, "max") > 0
    m = min(len(a), len(b))
    res = {"delay_ms": 0, "gain_db": -120.0, "corr": 0.0}
    if m < 300 or act.sum() < 100:
        return res
    a, b, act = a[:m], b[:m], act[:m]
    za = a - moving_mean(a, 50, 50)
    zb = b - moving_mean(b, 50, 50)
    best, best_d = -1.0, 0
    for d in range(0, 81):
        x = za[: m - d]
        y = zb[d:]
        w = act[: m - d]
        if w.sum() < 50:
            continue
        xs, ys = x[w], y[w]
        xs = xs - xs.mean()
        ys = ys - ys.mean()
        den = math.sqrt(float(np.dot(xs, xs)) * float(np.dot(ys, ys)))
        c = float(np.dot(xs, ys)) / den if den > 0 else 0.0
        if c > best:
            best, best_d = c, d
    # Feinsuche im 1-ms-Raster um die grobe Verzögerung
    za1 = src["s20"] - moving_mean(src["s20"], 500, 500)
    zb1 = dst["s20"] - moving_mean(dst["s20"], 500, 500)
    w1 = src["raw"]
    n1 = min(len(za1), len(zb1))
    best1, dh = -2.0, best_d * 10
    for d in range(max(0, best_d * 10 - 12), best_d * 10 + 13):
        w = w1[: n1 - d]
        if w.sum() < 500:
            continue
        xs, ys = za1[: n1 - d][w], zb1[d:n1][w]
        xs = xs - xs.mean()
        ys = ys - ys.mean()
        den = math.sqrt(float(np.dot(xs, xs)) * float(np.dot(ys, ys)))
        c = float(np.dot(xs, ys)) / den if den > 0 else 0.0
        if c > best1:
            best1, dh = c, d
    pred = _shift(src["s20"], dh)
    pred = window_max(pred, 20, 20)
    loud = src["raw"] & (src["s20"] > np.percentile(src["s20"][src["raw"]], 50)) if src["raw"].any() else src["raw"]
    loud = _shift(loud.astype(np.uint8), dh).astype(bool) & dst["valid"]
    if loud.sum() < 200:
        return res
    g = float(np.median(dst["s20"][loud] - pred[loud]))
    return {"delay_ms": int(dh), "gain_db": g, "corr": round(best, 3), "pred": pred}


def _shift(x: np.ndarray, d: int) -> np.ndarray:
    """Verzögert um d Schritte (vorne mit dem ersten Wert aufgefüllt)."""
    if d <= 0:
        return x.copy()
    return np.concatenate([np.full(d, x[0], dtype=x.dtype), x[:-d]])


def plan(stems: list[dict], crossgate: bool, warnings) -> None:
    """Berechnet je Stem die Verstärkungskurven (dB je ms) und Kennzahlen. stems: [{name, an (Analyse), pre (eK vorher)}]."""
    for st in stems:
        st["act"] = _activity(st["an"]["eS"])
        st["act"]["valid"] = moving_mean(st["an"]["eS"], 10, 9) > 1e-14
    H = min(len(st["an"]["eK"]) for st in stems)
    for st in stems:
        for k in ("eK", "eS", "eF", "eH"):
            st["an"][k] = st["an"][k][:H]
        for k in ("s20", "raw", "active", "valid"):
            st["act"][k] = st["act"][k][:H]

    # Übersprechen und eigene Sprache
    for st in stems:
        st["pred"] = np.full(H, -200.0)
        st["bleed"] = []
    if len(stems) > 1:
        for i, di in enumerate(stems):
            for j, sj in enumerate(stems):
                if i == j:
                    continue
                bm = _bleed_model(sj["act"], di["act"])
                if "pred" in bm:
                    di["pred"] = np.maximum(di["pred"], bm.pop("pred") + bm["gain_db"])
                di["bleed"].append({"from": sj["name"], "delay_ms": bm["delay_ms"], "gain_db": r1(bm["gain_db"]),
                                    "corr": bm["corr"]})
                if bm["gain_db"] > -6.0 and bm["corr"] > 0.3:
                    warnings.add(f"Starkes Übersprechen von {sj['name']} in die Spur von {di['name']} "
                                 f"({bm['gain_db']:.0f} dB); Kopfhörer hätten geholfen")
    for st in stems:
        a = st["act"]
        excess = moving_mean(a["s20"] - st["pred"], 15, 15)
        st["excess"] = excess
        st["own"] = a["raw"] & (excess >= 6.0)

    for st in stems:
        an, a = st["an"], st["act"]
        own = st["own"]
        if own.sum() < 1000:  # weniger als 1 s eigene Sprache
            own = a["raw"]
        speech = own.sum() >= 1000
        st["speech_s"] = round(float(own.sum()) / 1000.0, 1)
        if not speech:
            warnings.add(f"In der Spur von {st['name']} ist kaum Sprache zu erkennen")
        L = speech_lufs(an["eK"], own) if speech else TARGET_SPEECH
        st["own_mask"] = own
        st["lufs_speech_in"] = L
        st["lufs_speech_before"] = speech_lufs(st["pre"][:H], own) if speech else None
        G = float(np.clip(TARGET_SPEECH - L, -30.0, 45.0)) if speech else 0.0
        if G > 30:
            warnings.add(f"Die Aufnahme von {st['name']} ist sehr leise ({L:.0f} LUFS); Rauschen kann hörbar sein")
        st["gain_db"] = G

        # Leveler auf 100-ms-Raster: lokaler Mittelwert der dB-Werte über eigene Sprache (robust gegen einzelne laute
        # Stellen), verglichen mit demselben Maß über die ganze Spur
        F = H // 100
        if speech and F > 0:
            ldb = np.where(own, _db(an["eK"]), 0.0)
            e = ldb[:F * 100].reshape(F, 100).sum(axis=1)
            c = own[:F * 100].reshape(F, 100).sum(axis=1).astype(np.float64)
            glob = float(e.sum() / max(c.sum(), 1.0))
            se = gaussian_filter1d(e, 20.0, mode="nearest")
            sc = gaussian_filter1d(c, 20.0, mode="nearest")
            ok = sc > 5.0  # mindestens etwa 5 % Sprache im Fenster
            loc = np.full(F, np.nan)
            loc[ok] = se[ok] / sc[ok]
            loc = _ffill(loc, glob)
            lev_f = np.clip(0.8 * (glob - loc), -8.0, 8.0)
            lev_f = gaussian_filter1d(lev_f, 3.0, mode="nearest")
            lev = np.interp(np.arange(H), np.arange(F) * 100 + 49.5, lev_f)
        else:
            lev = np.zeros(H)
        st["lev"] = lev

        # Kompressor (Detektor nach Angleich und Leveler)
        det = _db(moving_mean(an["eK"], 5, 5)) + G + lev
        T = TARGET_SPEECH + 0.691 + 5.0
        R, W = 2.0, 6.0
        over = det - T
        gr = np.where(over <= -W / 2, 0.0,
                      np.where(over >= W / 2, -(1 - 1 / R) * over, -(1 - 1 / R) * (over + W / 2) ** 2 / (2 * W)))
        h = window_min(gr, 2, 8)
        comp = moving_mean(release_min(h, 25.0 / 1000.0, 0.0), 8, 0)
        st["comp"] = comp

        # De-Esser (Absenkung des Höhenanteils)
        hdb = _db(moving_mean(an["eH"], 2, 2))
        fdb = _db(moving_mean(an["eF"], 2, 2))
        ref = float(10 * np.log10(max(np.mean(an["eF"][own]), 1e-20))) if speech else 0.0
        amount = np.clip((hdb - (ref - 8.0)) * 0.5, 0.0, 6.0) * np.clip((hdb - fdb + 8.0) / 5.0, 0.0, 1.0)
        de = moving_mean(window_min(-amount, 2, 3), 3, 3)
        st["deess"] = de
        st["deess_share"] = round(float(np.mean(de < -1.0)) * 100, 2)

    # Crossgate
    for st in stems:
        st["gate"] = np.zeros(H)
    if crossgate and len(stems) > 1:
        owns = [st["act"]["raw"] & (st["excess"] >= 6.0) for st in stems]
        for i, st in enumerate(stems):
            others = np.zeros(H, bool)
            for j, o in enumerate(owns):
                if j != i:
                    others |= o
            raw = st["act"]["raw"]
            t = np.full(H, np.nan)
            t[raw] = -GATE_MAX_DB * np.clip((10.0 - st["excess"][raw]) / 6.0, 0.0, 1.0)
            t[~raw & others] = -GATE_MAX_DB
            t = _ffill(t, 0.0)
            t = window_max(t, 150, 60)
            g = fall_max(t, GATE_MAX_DB / 300.0, 0.0)
            st["gate"] = moving_mean(g, 3, 3)
        for st in stems:
            st["gate_share"] = round(float(np.mean(st["gate"] < -6.0)) * 100, 1)


def _ffill(x: np.ndarray, first: float) -> np.ndarray:
    """NaN mit dem letzten gültigen Wert füllen (am Anfang mit dem ersten gültigen, sonst `first`)."""
    ok = ~np.isnan(x)
    if not ok.any():
        return np.full(len(x), first)
    idx = np.where(ok, np.arange(len(x)), 0)
    np.maximum.accumulate(idx, out=idx)
    y = x[idx]
    first_ok = int(np.argmax(ok))
    y[:first_ok] = x[first_ok]
    return y


# ---------------------------------------------------------------- Rendern

def render(src: Path, st: dict, tmp: Path, dst: Path, progress=None) -> dict:
    total = st["gain_db"] + st["lev"] + st["comp"] + st["gate"]
    de = st["deess"]
    H = len(total)
    centers = np.arange(H) * HOP + (HOP - 1) / 2
    lp = SosStream(lowpass_sos(DEESS_FC, 2))
    with sf.SoundFile(str(src)) as f, sf.SoundFile(str(tmp), "w", SR, 1, "FLOAT", format="RF64") as out:
        n = f.frames
        for s in range(0, n, BLOCK):
            x = f.read(BLOCK, dtype="float32").astype(np.float64)
            idx = np.arange(s, s + len(x))
            hp = x - lp(x)
            gde = 10 ** (np.interp(idx, centers, de) / 20)
            y = x - (1.0 - gde) * hp
            y *= 10 ** (np.interp(idx, centers, total) / 20)
            out.write(y.astype(np.float32))
            if progress:
                progress(0.6 * min(1.0, (s + BLOCK) / max(1, n)))
    stats = limit_file(tmp, dst, ceiling_db=-1.0, true_peak=False, subtype="PCM_24", fmt="FLAC",
                       release_db_s=60.0, progress=(progress.sub(0.6, 1.0) if progress else None))
    tmp.unlink(missing_ok=True)
    return stats


def speech_intervals(own: np.ndarray, before_ms: int = 150, after_ms: int = 300, min_gap_ms: int = 400) -> list[list[int]]:
    """Eigene Sprache als Bereiche [von_ms, bis_ms] (1-ms-Raster), etwas erweitert und kleine Lücken geschlossen.
    Grundlage für das Transkript, damit Übersprechen nicht doppelt transkribiert wird."""
    if not own.any():
        return []
    m = window_max(own.astype(np.uint8), after_ms, before_ms).astype(bool)
    d = np.diff(np.concatenate([[0], m.astype(np.int8), [0]]))
    starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    out: list[list[int]] = []
    for a, b in zip(starts.tolist(), ends.tolist()):
        if out and a - out[-1][1] < min_gap_ms:
            out[-1][1] = b
        else:
            out.append([a, b])
    return out


def summarize(st: dict, an_after: dict) -> dict:
    after = speech_lufs(an_after["eK"], st["own_mask"])
    a = st["act"]
    noise_in = a["noise_db"]
    s20_after = _db(moving_mean(an_after["eS"], 10, 9))
    valid = moving_mean(an_after["eS"], 10, 9) > 1e-14
    noise_out = float(np.percentile(s20_after[valid], 10)) if valid.sum() > 100 else None
    return {
        "lufs_speech_before": r1(st.get("lufs_speech_before")),
        "lufs_speech_after": r1(after),
        "gain_db": r1(st["gain_db"]),
        "leveler_db": [r1(float(np.min(st["lev"]))), r1(float(np.max(st["lev"])))],
        "compressor_max_db": r1(float(-np.min(st["comp"]))),
        "deess_share_percent": st.get("deess_share"),
        "crossgate_share_percent": st.get("gate_share"),
        "speech_s": st.get("speech_s"),
        "noise_floor_db": {"before": r1(noise_in), "after": r1(noise_out)},
        "bleed": st.get("bleed", []),
    }


def measure_lufs(path: Path) -> float:
    m = LoudnessMeter(sf.info(str(path)).channels)
    with sf.SoundFile(str(path)) as f:
        for blk in f.blocks(BLOCK, dtype="float32", always_2d=True):
            m.feed(blk)
    return m.integrated()


def pre_gain_for(lufs: float, peak: float) -> float:
    """Vorverstärkung vor deep-filter: Ziel −26 LUFS, Spitze höchstens −3 dBFS (16-Bit-Ausgabe)."""
    g = (-26.0 - lufs) if math.isfinite(lufs) else 0.0
    if peak > 0:
        g = min(g, -3.0 - 20 * math.log10(peak))
    return float(np.clip(g, -20.0, 40.0))


__all__ = ["DENOISE_ATTEN", "highpass_file", "denoise_file", "analyze_file", "plan", "render", "summarize",
           "pre_gain_for", "db"]
