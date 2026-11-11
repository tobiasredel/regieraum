"""Konfiguration aus Umgebungsvariablen (SPEC Abschnitt 2)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Config:
    data: Path
    base_url: str
    host_auth: str
    host_password: str
    jobs: bool
    storage: str
    public_base_url: str
    s3_configured: bool
    turn_mode: str
    turn_urls: tuple[str, ...]
    turn_username: str
    turn_credential: str
    cf_turn_key_id: str
    cf_turn_api_token: str
    anthropic_key: str
    ai_model: str
    ai_model_prep: str
    ai_model_live: str
    openai_key: str
    stt_model: str
    whisper_model: str
    ntfy_url: str
    ntfy_user: str
    ntfy_pass: str

    @classmethod
    def from_env(cls) -> "Config":
        base = _env("PS_BASE_URL", "http://localhost:8000").rstrip("/")
        storage = _env("PS_STORAGE", "local")
        host_auth = _env("PS_HOST_AUTH", "password")
        if host_auth not in ("password", "proxy", "none"):
            raise ValueError("PS_HOST_AUTH muss 'password', 'proxy' oder 'none' sein")
        return cls(
            data=Path(_env("PS_DATA", "/data")),
            base_url=base,
            host_auth=host_auth,
            host_password=os.environ.get("PS_HOST_PASSWORD", ""),
            jobs=_env("PS_JOBS", "1") == "1",
            storage=storage,
            public_base_url=(_env("PS_PUBLIC_BASE_URL") or (base + "/public" if storage == "local" else "")).rstrip("/"),
            s3_configured=all(_env(k) for k in ("PS_S3_ENDPOINT", "PS_S3_BUCKET", "PS_S3_ACCESS_KEY", "PS_S3_SECRET_KEY")),
            turn_mode=_env("PS_TURN_MODE", "none"),
            turn_urls=tuple(u.strip() for u in _env("PS_TURN_URLS").split(",") if u.strip()),
            turn_username=_env("PS_TURN_USERNAME"),
            turn_credential=_env("PS_TURN_CREDENTIAL"),
            cf_turn_key_id=_env("PS_CF_TURN_KEY_ID"),
            cf_turn_api_token=_env("PS_CF_TURN_API_TOKEN"),
            anthropic_key=_env("ANTHROPIC_API_KEY"),
            ai_model=_env("PS_AI_MODEL", "claude-sonnet-5-5"),
            ai_model_prep=_env("PS_AI_MODEL_PREP", "claude-opus-5-5"),
            ai_model_live=_env("PS_AI_MODEL_LIVE", "claude-sonnet-5-5"),
            openai_key=_env("OPENAI_API_KEY"),
            stt_model=_env("PS_STT_MODEL", "gpt-transcribe"),
            whisper_model=_env("PS_WHISPER_MODEL", "large-v3-turbo"),
            ntfy_url=_env("PS_NTFY_URL"),
            ntfy_user=_env("PS_NTFY_USER"),
            ntfy_pass=_env("PS_NTFY_PASS"),
        )

    @property
    def db_path(self) -> Path:
        return self.data / "studio.db"

    @property
    def sounds_dir(self) -> Path:
        return self.data / "sounds"

    @property
    def episodes_dir(self) -> Path:
        return self.data / "episodes"

    @property
    def cover_dir(self) -> Path:
        return self.data / "cover"

    @property
    def public_dir(self) -> Path:
        return self.data / "public"

    def episode_dir(self, episode_id: str) -> Path:
        return self.episodes_dir / episode_id

    def ensure_dirs(self) -> None:
        for d in (self.data, self.sounds_dir, self.episodes_dir, self.cover_dir, self.data / "models"):
            d.mkdir(parents=True, exist_ok=True)
        if self.storage == "local":
            self.public_dir.mkdir(parents=True, exist_ok=True)
