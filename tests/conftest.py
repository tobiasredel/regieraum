"""Gemeinsame Test-Fixtures für test_app_*.py."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("PS_JOBS", "0")
os.environ.setdefault("PS_HOST_AUTH", "none")


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    from app.config import Config

    monkeypatch.setenv("PS_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("PS_JOBS", "0")
    monkeypatch.setenv("PS_HOST_AUTH", "none")
    monkeypatch.setenv("PS_STORAGE", "local")
    monkeypatch.setenv("PS_BASE_URL", "http://testserver")
    monkeypatch.delenv("PS_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("PS_NTFY_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PS_TURN_MODE", raising=False)
    return Config.from_env()


@pytest.fixture()
def clock():
    """Steuerbare Serverzeit."""
    from app import util

    state = {"t": 1_800_000_000_000}
    util.set_clock(lambda: state["t"])

    class C:
        def now(self) -> int:
            return state["t"]

        def advance(self, ms: int) -> int:
            state["t"] += ms
            return state["t"]

    yield C()
    util.set_clock(None)


@pytest.fixture()
def app(cfg):
    from app.main import create_app

    return create_app(cfg)


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c
