"""Vorbereitung (Dossiers) und Live-Begleiter für den Host.

Reines Python-Paket ohne Datenbank und ohne HTTP-Routen; der Server bindet es ein.

- ``assist.prep.research`` / ``research_async``: Dossier je Abschnitt des Sendeplans (SPEC Abschnitt 14)
- ``assist.live.LiveSession``: Mitschrift und Hinweiskarten während der Aufnahme (SPEC Abschnitt 15)
- ``assist.stt``: Schnittstelle ``Transcriber`` und die Umsetzung für die Cloud-Transkription
"""
