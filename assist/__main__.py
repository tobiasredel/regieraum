"""CLI zum Ausprobieren.

    python -m assist prep --kind hauptthema --title "…" --input "…" [--plan plan.json] [--out dossier.json]
    python -m assist live <wav-datei-oder-ordner> [--plan plan.json] [--speed 4] [--segment ID]
                          [--pulse-at 120] [--lookup-at "150:Begriff"] [--segment-at "200:ID"] [--json-out f.json]

``live`` spielt eine Aufnahme wie in Echtzeit (optional beschleunigt) in eine ``LiveSession``: jede WAV-Datei ist
eine Spur, der Dateiname (ohne Endung) der Sprecher. Alle Dateien beginnen bei Sitzungszeit 0. Ausgabe: Zeilen und
Karten mit Zeitstempel, am Ende Messwerte (Verzögerung, Kosten).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np

from .prompts import fmt_ms


def _load_plan(path: str | None) -> list[dict]:
    if not path:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("segments") or [data]
    return data


def cmd_prep(a) -> int:
    from .prep import research

    seg = {"id": "cli", "kind": a.kind, "title": a.title or "", "input": a.input or "", "good_type": a.good_type}
    plan = _load_plan(a.plan)

    def progress(p, msg):
        print(f"  {int(p * 100):3d} %  {msg}", file=sys.stderr, flush=True)

    t0 = time.monotonic()
    try:
        dossier = research(seg, None, [], plan, on_progress=progress, model=a.model)
    except Exception as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    out = json.dumps({**seg, "dossier": dossier}, ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).write_text(out, encoding="utf-8")
    else:
        print(out)
    m = dossier.get("meta", {})
    print(f"Fertig in {time.monotonic() - t0:.0f} s · Kosten {m.get('kosten_usd')} $ · Suchen {m.get('suchen')} · "
          f"Abrufe {m.get('abrufe')} · verworfen {m.get('verworfen')}", file=sys.stderr)
    return 0


def _read_tracks(path: Path) -> list[tuple[str, int, np.ndarray]]:
    import soundfile as sf

    files = sorted(path.glob("*.wav")) if path.is_dir() else [path]
    tracks = []
    for f in files:
        data, sr = sf.read(str(f), dtype="float32", always_2d=True)
        tracks.append((f.stem.capitalize(), sr, data.mean(axis=1).astype(np.float32)))
    return tracks


async def _run_live(a) -> int:
    from .live import LiveConfig, LiveSession

    tracks = _read_tracks(Path(a.audio))
    if not tracks:
        print("Keine WAV-Dateien gefunden", file=sys.stderr)
        return 1
    plan = _load_plan(a.plan)
    speed = max(0.1, a.speed)
    t_real0 = time.monotonic()
    rec_start = 1_700_000_000_000.0

    def clock():
        return rec_start + (time.monotonic() - t_real0) * 1000.0 * speed

    events = []

    def on_line(l):
        lag = (clock() - rec_start - l["end_ms"]) / speed
        events.append({"type": "line", "at_ms": clock() - rec_start, "lag_real_ms": lag, **l})
        print(f"[{fmt_ms(l['start_ms'])}–{fmt_ms(l['end_ms'])}] (+{lag / 1000:.1f} s) {l['speaker']}: {l['text']}", flush=True)

    def on_card(c):
        at = clock() - rec_start
        events.append({"type": "card", "at_ms": at, **c})
        src = f" <{c['quelle_url']}>" if c.get("quelle_url") else ""
        det = f"\n          {c['detail']}" if c.get("detail") else ""
        print(f"\n  >>> [{fmt_ms(at)}] KARTE {c['kind']} ({c['basis']}): {c['text']}{src}{det}\n", flush=True)

    def on_status(s):
        if s.get("message") or a.verbose:
            print(f"  ··· Status: stt={s['stt']} ai={s['ai']} {s.get('message') or ''}", file=sys.stderr, flush=True)

    cfg = LiveConfig()
    live = LiveSession(rec_start_server_ms=rec_start, on_line=on_line, on_card=on_card, on_status=on_status,
                       server_clock=clock, work_dir=a.work_dir, config=cfg)
    await live.start()
    live.set_plan(plan)
    seg = a.segment or (next((s["id"] for s in plan if s.get("kind") == "hauptthema"), None) if plan else None)
    if seg:
        live.set_current_segment(seg)
    actions = []
    for t in a.pulse_at or []:
        actions.append((float(t) * 1000, "pulse", None))
    for spec in a.lookup_at or []:
        t, q = spec.split(":", 1)
        actions.append((float(t) * 1000, "lookup", q))
    for spec in a.segment_at or []:
        t, sid = spec.split(":", 1)
        actions.append((float(t) * 1000, "segment", sid))
    actions.sort(key=lambda x: x[0])
    chunk_s = 2.0
    upload_delay_ms = 300.0
    total_ms = max(len(x) / sr for _, sr, x in tracks) * 1000
    pos = [0] * len(tracks)
    while True:
        now = clock() - rec_start
        done = True
        for i, (name, sr, x) in enumerate(tracks):
            n = int(sr * chunk_s)
            while pos[i] < len(x):
                end_ms = min(len(x), pos[i] + n) / sr * 1000
                if end_ms + upload_delay_ms > now:
                    break
                chunk = x[pos[i] : pos[i] + n]
                live.feed_chunk(f"t-{name}", name, sr, "f32le", pos[i], chunk.astype("<f4").tobytes(), 0.0)
                pos[i] += n
            if pos[i] < len(x):
                done = False
        while actions and actions[0][0] <= now:
            _, kind, arg = actions.pop(0)
            if kind == "pulse":
                print(f"  ··· [{fmt_ms(now)}] Impuls jetzt", flush=True)
                live.pulse()
            elif kind == "lookup":
                print(f"  ··· [{fmt_ms(now)}] Nachschlagen: {arg}", flush=True)
                live.lookup(arg)
            else:
                live.set_current_segment(arg)
        if done and now > total_ms + upload_delay_ms:
            break
        await asyncio.sleep(0.05)
    live.end_of_audio()
    await live.drain(timeout=20)
    # laufende Hinweise oder Nachschlagen abwarten
    for _ in range(100):
        if not live._bg:
            break
        await asyncio.sleep(0.2)
    stats = live.summary_stats()
    await live.close()
    minutes = total_ms / 60000
    stt_h = stats["stt_cost_usd"] / minutes * 60 if minutes else 0
    llm_h = stats["llm_cost_usd"] / minutes * 60 if minutes else 0
    lag_med = stats["lag_ms_median"] / speed if stats["lag_ms_median"] is not None else None
    lag_p90 = stats["lag_ms_p90"] / speed if stats["lag_ms_p90"] is not None else None
    print("\n=== Messwerte ===")
    print(f"Dauer {minutes:.1f} min, Tempo {speed}x, Spuren {len(tracks)}")
    print(f"Zeilen {stats['lines']} (Dubletten entfernt {stats['lines_deduped']}), Karten {stats['cards']} "
          f"(verworfen {stats['cards_rejected']}), Hinweis-Aufrufe {stats['hint_calls']} (geschwiegen {stats['hint_silent']})")
    if lag_med is not None:
        print(f"Verzögerung Wortende → Zeile (Echtzeit): Median {lag_med / 1000:.2f} s, p90 {lag_p90 / 1000:.2f} s")
    if stats["hint_latency_ms_median"] is not None:
        print(f"Dauer eines Hinweis-Aufrufs: Median {stats['hint_latency_ms_median'] / 1000:.1f} s, "
              f"p90 {stats['hint_latency_ms_p90'] / 1000:.1f} s")
    print(f"Mitschrift: {stats['stt_requests']} Anfragen, {stats['stt_seconds']:.0f} s abgerechnet, "
          f"{stats['stt_cost_usd']:.4f} $ → hochgerechnet {stt_h:.2f} $ / 60 min")
    print(f"Hinweise: {stats['llm_requests']} Aufrufe, {stats['llm_cost_usd']:.4f} $ → hochgerechnet {llm_h:.2f} $ / 60 min; "
          f"Token {stats['llm_tokens']}, Suchen {stats['searches']}")
    cbk = stats.get("cost_by_kind") or {}
    if cbk:
        print("Hinweis-Kosten nach Art: " + ", ".join(f"{k} {n}× {c:.4f} $" for k, (n, c) in sorted(cbk.items())))
        auto = sum(c for k, (n, c) in cbk.items() if k in ("tick", "check", "summary"))
        print(f"  davon automatisch (Takt, Zahlen-Prüfung, Kurzfassung): {auto:.4f} $ → hochgerechnet "
              f"{auto / minutes * 60 if minutes else 0:.2f} $ / 60 min (Impuls und Nachschlagen je Druck extra)")
    print(f"Fehler: Mitschrift {stats['stt_errors']}, übersprungen {stats['stt_skipped']}, KI {stats['llm_errors']}")
    if a.json_out:
        Path(a.json_out).write_text(json.dumps({"events": events, "stats": stats, "minutes": minutes, "speed": speed},
                                               ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m assist")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep", help="Dossier zu einem Abschnitt recherchieren")
    p.add_argument("--kind", choices=["blitzlicht", "hauptthema", "good", "frei"], required=True)
    p.add_argument("--title", default="")
    p.add_argument("--input", default="")
    p.add_argument("--good-type", choices=["read", "watch", "listen"])
    p.add_argument("--plan", help="übrige Abschnitte als JSON (Kontext, z. B. Hauptthema für Good)")
    p.add_argument("--model")
    p.add_argument("--out")
    l = sub.add_parser("live", help="Aufnahme durch den Live-Begleiter schicken")
    l.add_argument("audio")
    l.add_argument("--plan")
    l.add_argument("--speed", type=float, default=1.0)
    l.add_argument("--segment")
    l.add_argument("--pulse-at", action="append")
    l.add_argument("--lookup-at", action="append")
    l.add_argument("--segment-at", action="append")
    l.add_argument("--work-dir")
    l.add_argument("--json-out")
    l.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if getattr(a, "verbose", False) else logging.WARNING,
                        format="  ··· %(message)s", stream=sys.stderr)
    for noisy in ("httpx", "anthropic", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if a.cmd == "prep":
        return cmd_prep(a)
    return asyncio.run(_run_live(a))


if __name__ == "__main__":
    sys.exit(main())
