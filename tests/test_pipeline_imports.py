"""Die Pipeline-Abhängigkeiten lassen sich importieren (Transkription scheiterte 2026-11-12 im Betrieb an einem fehlenden
Paket, das eine nicht festgelegte Abhängigkeit nicht mehr mitbrachte)."""


def test_transcription_dependencies_import():
    import faster_whisper  # noqa: F401
    import requests  # noqa: F401
