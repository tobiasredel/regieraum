"""ID3v2.3-Tags mit Kapiteln (CHAP/CTOC) über mutagen."""
from __future__ import annotations

from pathlib import Path

from mutagen.id3 import APIC, CHAP, CTOC, ID3, TALB, TDRC, TIT2, TPE1, TRCK, TSSE, CTOCFlags
from mutagen.id3._tags import save_frame

ENC = 1  # UTF-16 mit BOM: in ID3v2.3 die einzige Unicode-Kodierung
TEXT_ORDER = ["TIT2", "TPE1", "TALB", "TRCK", "TDRC", "TYER"]


class _OrderedID3(ID3):
    """mutagen sortiert Frames nach Größe; ffmpeg und manche Player lesen CHAP aber in Dateireihenfolge. Hier: Texte,
    Bild, Inhaltsverzeichnis, dann Kapitel chronologisch (mutagen ist in requirements.txt fest auf 1.47 gepinnt)."""

    def _write(self, config):
        def key(f):
            if f.FrameID == "CHAP":
                return (3, f.start_time, f.HashKey)
            if f.FrameID == "CTOC":
                return (2, 0, f.HashKey)
            if f.FrameID in TEXT_ORDER:
                return (0, TEXT_ORDER.index(f.FrameID), f.HashKey)
            return (1, 0, f.HashKey)
        return bytearray().join(save_frame(f, config=config) for f in sorted(self.values(), key=key))


def write_tags(mp3: Path, tags: dict, chapters: list[dict], duration_ms: int) -> None:
    id3 = _OrderedID3()
    if tags.get("title"):
        id3.add(TIT2(encoding=ENC, text=[str(tags["title"])]))
    if tags.get("artist"):
        id3.add(TPE1(encoding=ENC, text=[str(tags["artist"])]))
    if tags.get("album"):
        id3.add(TALB(encoding=ENC, text=[str(tags["album"])]))
    if tags.get("track") not in (None, ""):
        id3.add(TRCK(encoding=ENC, text=[str(tags["track"])]))
    if tags.get("year"):
        id3.add(TDRC(encoding=ENC, text=[str(tags["year"])]))  # wird beim Speichern als v2.3 zu TYER
    id3.add(TSSE(encoding=ENC, text=["podcast-studio"]))
    cover = tags.get("cover_path")
    if cover and Path(cover).is_file():
        data = Path(cover).read_bytes()
        mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        id3.add(APIC(encoding=0, mime=mime, type=3, desc="Cover", data=data))
    if chapters:
        ids = []
        for i, c in enumerate(chapters):
            start = int(round(c["startTime"] * 1000))
            end = int(round(chapters[i + 1]["startTime"] * 1000)) if i + 1 < len(chapters) else int(duration_ms)
            eid = f"chp{i}"
            ids.append(eid)
            id3.add(CHAP(element_id=eid, start_time=start, end_time=max(start, end),
                         start_offset=0xFFFFFFFF, end_offset=0xFFFFFFFF,
                         sub_frames=[TIT2(encoding=ENC, text=[str(c["title"])])]))
        id3.add(CTOC(element_id="toc", flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED, child_element_ids=ids,
                     sub_frames=[TIT2(encoding=ENC, text=["Kapitel"])]))
    id3.save(str(mp3), v1=0, v2_version=3)
