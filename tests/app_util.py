"""Hilfen für die App-Tests (kein Test, kein pytest-Name)."""
from __future__ import annotations

import io
import math
import struct
import wave


def wav_bytes(seconds: float = 1.0, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(rate * seconds)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(n)))
    return buf.getvalue()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def host_login(client) -> tuple[str, dict]:
    r = client.post("/api/host/session")
    assert r.status_code == 200, r.text
    d = r.json()
    return d["participant_token"], d["participant"]


def join_guest(client, name: str = "Max", kind: str = "permanent") -> tuple[str, dict]:
    if kind == "permanent":
        inv = next(i for i in client.get("/api/host/invites").json() if i["kind"] == "permanent")
    else:
        inv = client.post("/api/host/invites", json={"label": name, "kind": "single"}).json()
    r = client.post("/api/join", json={"invite_token": inv["token"], "name": name})
    assert r.status_code == 200, r.text
    d = r.json()
    return d["participant_token"], d["participant"]


def upload_sound(client, label="Intro", kind="intro", seconds=1.0, **extra):
    files = {"file": ("jingle.wav", wav_bytes(seconds), "audio/wav")}
    data = {"label": label, "kind": kind, **{k: str(v) for k, v in extra.items()}}
    r = client.post("/api/host/sounds", files=files, data=data)
    assert r.status_code == 201, r.text
    return r.json()


def recv_until(ws, type_: str, limit: int = 30) -> dict:
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["type"] == type_:
            return msg
    raise AssertionError(f"Nachricht {type_} kam nicht")


def drain_until_room(ws, pred, limit: int = 40) -> dict:
    """Liest `room`-Nachrichten, bis pred(room) wahr ist."""
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["type"] == "room" and pred(msg["room"]):
            return msg["room"]
    raise AssertionError("passender Raumzustand kam nicht")


def create_track(client, token: str, start_ms: int, **meta) -> str:
    r = client.post("/api/p/tracks", headers=auth(token),
                    json={"sample_rate": 48000, "format": "s24le", "start_server_ms": start_ms, "meta": {"headphones": True, **meta}})
    assert r.status_code == 200, r.text
    return r.json()["track_id"]


def put_chunk(client, token: str, track_id: str, seq: int, frames: int = 100, first_frame: int | None = None, fmt_bytes: int = 3):
    return client.put(
        f"/api/p/tracks/{track_id}/chunks/{seq}",
        headers={**auth(token), "X-First-Frame": str(seq * frames if first_frame is None else first_frame), "X-Frames": str(frames)},
        content=b"\x01" * (frames * fmt_bytes),
    )
