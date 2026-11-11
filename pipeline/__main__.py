"""CLI: python -m pipeline {process|stage1|stage2|transcribe} manifest.json | sound <eingabe> <zielordner>

Fortschritt als JSON-Zeilen auf stdout. Bei einem Fehler: Exit-Code 1, die letzte Zeile auf stderr ist die Meldung
für Menschen (deutsch).
"""
from __future__ import annotations

import os
import sys

# höchstens vier Threads, bevor numpy, scipy oder ctranslate2 geladen werden
_T = str(max(1, min(4, int(os.environ.get("PS_THREADS", "4") or 4))))
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "RAYON_NUM_THREADS"):
    os.environ.setdefault(_v, _T)

USAGE = ("Aufruf: python -m pipeline process|stage1|stage2|transcribe <manifest.json>\n"
         "        python -m pipeline sound <eingabe> <zielordner>")


def main(argv: list[str]) -> int:
    import json
    from pathlib import Path

    from .util import PipelineError, Progress

    if len(argv) < 2:
        print(USAGE, file=sys.stderr)
        return 2
    cmd = argv[0]
    prog = Progress()
    try:
        if cmd == "sound":
            if len(argv) != 3:
                print(USAGE, file=sys.stderr)
                return 2
            from . import sound
            sound.run(Path(argv[1]), Path(argv[2]), prog)
            return 0
        if cmd not in ("process", "stage1", "stage2", "transcribe"):
            print(USAGE, file=sys.stderr)
            return 2
        try:
            m = json.loads(Path(argv[1]).read_text())
        except (OSError, ValueError) as exc:
            raise PipelineError(f"Manifest nicht lesbar ({exc})") from exc
        for key in ("work_dir", "out_dir", "rec_start_server_ms"):
            if m.get(key) is None:
                raise PipelineError(f"Manifest unvollständig: „{key}“ fehlt")
        if cmd in ("process", "stage1"):
            from . import stage1
            stage1.run(m, prog.sub(0.0, 0.7) if cmd == "process" else prog)
        if cmd in ("process", "stage2"):
            from . import stage2
            stage2.run(m, prog.sub(0.7, 1.0) if cmd == "process" else prog)
        if cmd == "transcribe":
            from . import transcribe
            transcribe.run(m, prog)
        return 0
    except PipelineError as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    except MemoryError:
        print("Zu wenig Arbeitsspeicher für die Verarbeitung", file=sys.stderr, flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"Unerwarteter Fehler in der Audio-Verarbeitung: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
