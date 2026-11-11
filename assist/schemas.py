"""JSON-Schemas für strukturierte Ausgaben (alle Felder Pflicht, Längen setzt der Code durch)."""
from __future__ import annotations


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_S = {"type": "string"}
_SN = {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _arr(item) -> dict:
    return {"type": "array", "items": item}


DOSSIER_SCHEMA = _obj({
    "lage": _S,
    "fakten": _arr(_obj({"text": _S, "quelle_titel": _S, "quelle_url": _S, "datum": _S})),
    "zahlen": _arr(_obj({"text": _S, "quelle_url": _S})),
    "positionen": _arr(_obj({"wer": _S, "position": _S, "quelle_url": _S})),
    "gegenargumente": _arr(_S),
    "fragen": _arr(_S),
    "aufhaenger": _arr(_S),
    "begriffe": _arr(_S),
    "quellen": _arr(_obj({"titel": _S, "url": _S, "datum": _S})),
    "werk": {"anyOf": [_obj({"titel": _S, "urheber": _S, "typ": _S, "url": _S, "erschienen": _S,
                             "kurzfassung": _S, "kernaussagen": _arr(_S), "bezug": _S}), {"type": "null"}]},
    "kandidaten": _arr(_obj({"titel": _S, "urheber": _S, "typ": {"type": "string", "enum": ["read", "watch", "listen"]},
                             "url": _S, "warum": _S})),
})

CARD_KINDS_LIVE = ["stichwort", "frage", "fakt"]

LIVE_CARD_SCHEMA = _obj({
    "anlass": _S,
    "art": {"type": "string", "enum": ["korrektur", "antwort", "sonstiges"]},
    "nachschlagen": _SN,
    "genannt": _arr(_obj({"abschnitt": _S, "art": {"type": "string", "enum": ["frage", "fakt", "zahl", "aufhaenger"]},
                          "nr": {"type": "integer"}})),
    "karte": {"anyOf": [_obj({
        "kind": {"type": "string", "enum": CARD_KINDS_LIVE},
        "text": _S,
        "detail": _S,
        "basis": {"type": "string", "enum": ["dossier", "allgemein"]},
        "quelle_url": _SN,
    }), {"type": "null"}]},
})

LOOKUP_SCHEMA = _obj({"text": _S, "detail": _S, "quelle_url": _SN})

SUMMARY_SCHEMA = _obj({"kurzfassung": _S})
