"""Shownotes-Vorschlag mit Sendeplan: Quellen und Good Read fließen ein, bestehende Aufrufe bleiben unverändert."""
import inspect

import pytest

from publish.shownotes import build_prompt, suggest
from tests.publish_fixtures.helpers import show

from .test_publish_shownotes import CHAPTERS, GOOD, TRANSCRIPT, Fake, reply

PLAN = [
    {"kind": "blitzlicht", "title": "Parkgebühren", "input": "Stichworte zum Thema",
     "dossier": {"quellen": [{"titel": "Stadtblatt", "url": "https://stadtblatt.example/parken"}]}},
    {"kind": "good", "title": "Die Akte Amtsstube", "good_type": "read", "input": "",
     "dossier": {"werk": {"titel": "Die Akte Amtsstube", "urheber": "Lena Falk", "typ": "Buch", "url": "https://verlag.example/akte"},
                 "quellen": []}},
    {"kind": "frei", "title": "", "input": "", "dossier": None},
]


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("PS_AI_MODEL", raising=False)


def test_plan_is_optional_keyword():
    assert "plan" in inspect.signature(suggest).parameters
    system, user = build_prompt(TRANSCRIPT, show(), CHAPTERS, [])
    assert "<sendeplan>" not in user and "Sendeplan" not in system  # ohne Plan unverändert


def test_prompt_contains_sources_and_good_read():
    system, user = build_prompt(TRANSCRIPT, show(), CHAPTERS, [], PLAN)
    assert "<sendeplan>" in user and "Blitzlicht: Parkgebühren" in user
    assert "Quelle: Stadtblatt | https://stadtblatt.example/parken" in user
    assert "Good Read: Die Akte Amtsstube" in user and "Lena Falk" in user and "https://verlag.example/akte" in user
    assert "Freier Abschnitt" not in user  # leerer Abschnitt entfällt
    assert "Sendeplan" in system


def test_links_from_plan_survive_others_do_not():
    data = dict(GOOD, description_html='<p><a href="https://stadtblatt.example/parken">Stadtblatt</a> '
                                       '<a href="https://erfunden.example/y">Link</a></p>')
    r = suggest(TRANSCRIPT, show(), CHAPTERS, [], client=Fake(reply(data)), plan=PLAN)
    assert 'href="https://stadtblatt.example/parken"' in r["description_html"]
    assert "erfunden.example" not in r["description_html"]
    r = suggest(TRANSCRIPT, show(), CHAPTERS, [], client=Fake(reply(data)))  # ohne Plan nicht erlaubt
    assert "stadtblatt.example" not in r["description_html"]


def test_plan_reaches_the_request():
    fake = Fake(reply(GOOD))
    suggest(TRANSCRIPT, show(), CHAPTERS, [], client=fake, plan=PLAN)
    assert "stadtblatt.example/parken" in fake.calls[0][1]["messages"][0]["content"]
