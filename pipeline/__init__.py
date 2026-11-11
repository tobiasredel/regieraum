"""Audio-Pipeline für Podcast-Studio (SPEC Abschnitt 9).

Reines Paket ohne Datenbank und HTTP. Aufruf über die CLI (`python -m pipeline …`) oder für die Zeitrechnung über
`pipeline.timeline`.
"""

SR = 48000  # interne Abtastrate aller Stems und Mischungen
