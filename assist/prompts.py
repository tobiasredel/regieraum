"""Alle Prompts des Pakets an einem Ort.

Grundsätze, die für alle Prompts gelten:
- Wer die Show ist, steht nicht hier, sondern im Show-Profil (`show_block`): Titel, Hosts, Thema, Ton und der Ablauf
  einer Folge mit den Namen der Abschnittsarten kommen aus den Einstellungen der Installation.
- Die Prompts sprechen von Rollen: „der Host“ bedient die App und liest die Karten, „der Co-Host“ sitzt mit am Mikrofon.
- Fakten nüchtern und belegt. Der Ton der Show nur in Aufhängern.
- Aktuelles nie aus dem Gedächtnis des Modells. Was nicht belegt ist, wird zur Frage.
- Die App schlägt keine Themen vor (einzige Ausnahme: Kandidaten für eine Empfehlung bei leerer Eingabe).

Abschnittsarten haben feste interne Schlüssel (`blitzlicht` = kurzes Thema, `hauptthema`, `good` = Empfehlung mit
`read`/`watch`/`listen`, `frei`). Wie sie in einer Show heißen, bestimmt `show["format"]["labels"]`.

Statische Teile stehen vorn und ändern sich nie (Prompt-Caching); Datum, Plan und Mitschrift kommen dahinter.
"""
from __future__ import annotations

import json

from .util import lang_code

DEFAULT_LABELS = {"blitzlicht": "Kurzthema", "hauptthema": "Hauptthema", "good": "Empfehlung", "frei": "Freier Abschnitt",
                  "read": "Lesetipp", "watch": "Sehtipp", "listen": "Hörtipp"}
DEFAULT_LABELS_EN = {"blitzlicht": "Short topic", "hauptthema": "Main topic", "good": "Recommendation",
                     "frei": "Free segment", "read": "Reading tip", "watch": "Watching tip", "listen": "Listening tip"}

# Sprache der Show: Die Prompts sind deutsch geschrieben, verlangen die Ausgabe aber in der Sprache der Show
# (`show["language"]`, zweibuchstabig, Standard „de“). Deutsch bleibt Wort für Wort, wie es war.
LANG_NAMES = {"de": "Deutsch", "en": "Englisch", "fr": "Französisch", "es": "Spanisch", "it": "Italienisch",
              "nl": "Niederländisch", "pt": "Portugiesisch", "pl": "Polnisch", "tr": "Türkisch", "sv": "Schwedisch",
              "da": "Dänisch", "nb": "Norwegisch", "no": "Norwegisch", "fi": "Finnisch", "cs": "Tschechisch",
              "ru": "Russisch", "uk": "Ukrainisch", "ja": "Japanisch", "zh": "Chinesisch", "ko": "Koreanisch",
              "ar": "Arabisch", "el": "Griechisch", "hu": "Ungarisch", "ro": "Rumänisch"}
CORRECTION_WORD = {"de": "Korrektur", "en": "Correction"}
NOTHING_FOUND = {"de": "Nichts Belastbares gefunden", "en": "Nothing reliable found"}
NO_FACTS = {"de": "Keine belegbaren Fakten gefunden.", "en": "No verifiable facts found."}


def show_lang(show: dict | None) -> str:
    return lang_code((show or {}).get("language"))


def lang_name(lang: str) -> str:
    return LANG_NAMES.get(lang, f"die Sprache „{lang}“")


def correction_word(lang: str) -> str:
    """Anfang einer Korrektur-Karte (`art: korrektur`) in der Sprache der Show; andere Sprachen: Englisch."""
    return CORRECTION_WORD.get(lang, CORRECTION_WORD["en"])


def nothing_found(lang: str) -> str:
    return NOTHING_FOUND.get(lang, NOTHING_FOUND["en"])


def default_labels(lang: str) -> dict:
    return DEFAULT_LABELS if lang == "de" else DEFAULT_LABELS_EN


def _out_long(lang: str) -> str:
    if lang == "en":
        return "Alles auf Englisch, Zahlen im englischen Format (1,234.5), Anführungszeichen “so”."
    return f"Alles auf {lang_name(lang)}, Zahlen und Anführungszeichen so, wie es dort üblich ist."


def _out_short(lang: str) -> str:
    if lang == "en":
        return "Englisch, Zahlen im englischen Format."
    return f"{lang_name(lang)}, Zahlen im dort üblichen Format."


def localize(text: str, lang: str) -> str:
    """Prompt-Text für die Sprache der Show: Ausgabesprache, Zahlenformat und das feste Wort der Korrektur-Karte."""
    if lang == "de":
        return text
    word = correction_word(lang)
    text = (text.replace("Alles auf Deutsch, Zahlen im deutschen Format (1.234,5), Anführungszeichen „so“.", _out_long(lang))
            .replace("- Deutsch, Zahlen im deutschen Format.", "- " + _out_short(lang))
            .replace("einen deutschsprachigen Podcast", "einen Podcast" if lang != "en" else "einen englischsprachigen Podcast")
            .replace("(Text beginnt mit „Korrektur:“)", f"(Text beginnt mit „{word}:“ bzw. dem entsprechenden Wort in der Sprache der Show)")
            .replace("„Korrektur: 203,7 Mrd. statt 150 Mrd., inkl. Sondervermögen“", f"„{word}: 203,7 Mrd. statt 150 Mrd., inkl. Sondervermögen“")
            .replace("Bei Korrektur: „Korrektur: … statt …“.", f"Bei Korrektur: „{word}: … statt …“.")
            .replace("„Korrektur:“", f"„{word}:“")
            .replace("„Korrektur: Laut UBA", f"„{word}: Laut UBA")
            .replace("{\"kind\": \"fakt\", \"text\": \"Korrektur:", "{\"kind\": \"fakt\", \"text\": \"" + word + ":")
            .replace("- Findest Du nichts Belastbares: `text` = „Nichts Belastbares gefunden: …“ mit dem Begriff, `quelle_url` null.",
                     f"- Findest Du nichts Belastbares: `text` = „{nothing_found(lang)}: …“ mit dem Begriff, `quelle_url` null.")
            .replace("- Deutsch, Zahlen im deutschen Format.", "- " + _out_short(lang)))
    return text
DEFAULT_PLAN = ["blitzlicht", "blitzlicht", "blitzlicht", "hauptthema", "good:read"]
GOOD_MEDIUM = {"read": "Buch oder Text", "watch": "Film, Serie, Video", "listen": "Podcast, Musik, Hörbuch"}


def labels_of(show: dict | None) -> dict:
    """Namen der Abschnittsarten dieser Show (Einstellungen `format.labels`), ergänzt um die Standardnamen in der
    Sprache der Show."""
    fmt = (show or {}).get("format") or {}
    return {**default_labels(show_lang(show)), **{k: v for k, v in (fmt.get("labels") or {}).items() if v}}


def kind_label(show: dict | None, kind: str | None, good_type: str | None = None, medium: bool = False) -> str:
    labels = labels_of(show)
    if kind == "good" and good_type in GOOD_MEDIUM:
        return f"{labels[good_type]} ({GOOD_MEDIUM[good_type]})" if medium else labels[good_type]
    return labels.get(kind or "frei", kind or "")


def format_line(show: dict | None) -> str:
    """Ablauf einer Folge als ein Satz, aus dem Standardplan der Show."""
    fmt = (show or {}).get("format") or {}
    plan = fmt.get("plan") or DEFAULT_PLAN
    minutes = fmt.get("short_minutes") or 3
    parts: list[list] = []  # [Schlüssel, Anzahl]
    for entry in plan:
        if parts and parts[-1][0] == entry:
            parts[-1][1] += 1
        else:
            parts.append([entry, 1])
    out = []
    for entry, n in parts:
        kind, _, good = str(entry).partition(":")
        name = kind_label(show, kind, good or None)
        text = f"{n}× {name}" if n > 1 else name
        if kind == "blitzlicht":
            text += f" (je etwa {minutes} Minuten)" if n > 1 else f" (etwa {minutes} Minuten)"
        out.append(text)
    return "Ablauf einer Folge: Intro, " + ", ".join(out) + ", Outro."


def show_block(show: dict | None) -> str:
    """Show-Profil für jeden Prompt: wer spricht, worüber, in welchem Ton und in welchem Ablauf."""
    s = show or {}
    lines = [f"Show: „{s.get('title') or 'Podcast'}“" + (f", Hosts: {s['hosts']}." if s.get("hosts") else ".")]
    lang = show_lang(s)
    lines.append(f"Sprache der Show: {lang_name(lang)}. Alles, was Du für die Show schreibst, steht in dieser Sprache.")
    if s.get("host_name"):
        lines.append(f"Der Host, der die App bedient und die Karten liest, heißt {s['host_name']}."
                     + (f" Sein Co-Host am Mikrofon heißt {s['cohost_name']}." if s.get("cohost_name") else ""))
    elif s.get("cohost_name"):
        lines.append(f"Der Co-Host am Mikrofon heißt {s['cohost_name']}.")
    if s.get("description"):
        lines.append(f"Beschreibung: {s['description']}")
    if s.get("genre"):
        lines.append(f"Thema der Show: {s['genre']}.")
    if s.get("tone"):
        lines.append(f"Ton der Show: {s['tone']}.")
    lines.append(format_line(s))
    return "\n".join(lines)


# =============================================================================================== Dossier

PREP_SYSTEM = """\
Du recherchierst für einen deutschsprachigen Podcast ein Dossier zu genau einem Abschnitt der nächsten Folge. \
Der Host hat das Thema selbst gewählt; Du lieferst Stoff dazu und schlägst keine anderen Themen vor.

Wer das Dossier liest: der Host, kurz vor und während der Aufnahme. Er will in Minuten verstehen, was Stand ist, \
mit welchen belegten Fakten und Zahlen er argumentieren kann, was die Gegenseite sagt und welche Fragen das Gespräch \
mit seinem Co-Host tragen. Schreib so, dass man jeden Fakt laut vorlesen kann.

## Arbeitsweise
1. Hat der Host Links angegeben, lies sie zuerst mit web_fetch. Sie zeigen, worauf er hinauswill.
2. Suche dann gezielt mit web_search nach dem aktuellen Stand. Bevorzuge Primärquellen (Behörden, Parlament, \
Statistikämter, Gerichte, Studien) und seriöse Medien. Achte auf das Datum jeder Quelle: Das heutige Datum steht \
unten. Bei laufenden Themen zählt der neueste Stand; prüfe, ob sich etwas überholt hat.
3. Lies bei wichtigen Zahlen die Quelle selbst (web_fetch), statt Dich auf ein Suchergebnis-Snippet zu verlassen.
   Meide Blogs, SEO-Ratgeberseiten, Nachrichten-Aggregatoren und Seiten ohne erkennbare Redaktion. Steht eine Zahl \
nur dort, suche die Primärquelle oder ein seriöses Medium; findest Du keins, lass die Zahl weg.
4. Hör auf zu suchen, sobald Du genug belegten Stoff für die verlangte Tiefe hast.

## Regeln für Fakten (streng)
- Jeder Eintrag in `fakten` und `zahlen` stammt aus einer Quelle, die Du in dieser Recherche tatsächlich gesehen hast \
(Suchergebnis oder abgerufene Seite). `quelle_url` ist exakt deren URL. Einträge ohne solche Quelle werden \
automatisch verworfen.
- Nichts aus dem Gedächtnis, was sich seit Deinem Training geändert haben kann (Ämter, Umfragen, Gesetzesstände, \
Zahlen, Ereignisse). Allgemeinwissen, das sich nicht ändert, brauchst Du nicht als Fakt aufzuführen.
- Was Du nicht belegen kannst, aber wichtig ist, formulierst Du als Frage in `fragen` („Stimmt es, dass …?“).
- Zahlen immer mit Einheit, Bezugszeitraum und Urheber („laut Destatis, 2025: …“). Keine Scheingenauigkeit.
- Zahlen und Namen exakt wie in der Quelle (nicht „gut 320“, wenn dort 320 steht).
- `datum` ist das Datum der Quelle bzw. des Ereignisses (JJJJ-MM-TT). Fülle es immer, wenn Seite oder Suchergebnis \
ein Datum zeigen; leer nur, wenn wirklich keins erkennbar ist.
- Zitate nur wörtlich aus der Quelle, sonst sinngemäß ohne Anführungszeichen.
- Nüchtern bleiben: keine Wertung in `lage`, `fakten`, `zahlen`, `positionen`.

## Felder
- `lage`: worum es geht und was gerade der Stand ist, höchstens drei Sätze.
- `fakten`: je ein Satz, höchstens etwa 220 Zeichen, wichtigster zuerst.
- `zahlen`: die zwei bis fünf Zahlen, die im Gespräch wirklich ziehen (Größenordnungen, Vergleiche).
- `positionen`: wer (Person mit Funktion, Partei, Verband) vertritt was; mit `quelle_url`, wenn belegt, sonst leer.
- `gegenargumente`: die stärksten Einwände gegen die These des Hosts bzw. gegen die naheliegende Lesart, fair \
formuliert (kein Strohmann). Fehlt eine These, die stärksten Einwände gegen die gängige Position.
- `fragen`: Einstiegs- und Anschlussfragen für das Gespräch der beiden Hosts. Offen, zuspitzend, mit Reibung, \
gern mit einem überraschenden Blickwinkel. Keine Ja/Nein-Fragen, keine Prüfungsfragen („Was ist eigentlich …?“).
- `aufhaenger`: Pointen und Aufhänger im Ton der Show (siehe Show-Profil unten; ohne Angabe trocken und klug). Sie \
bauen auf echten Fakten aus diesem Dossier auf (Widersprüche, absurde Zahlen, Diskrepanz zwischen Anspruch und \
Wirklichkeit, Zitate, die schlecht gealtert sind). Keine Kalauer, keine Wortspiele mit Namen, keine Häme gegen \
Personen ohne Sachbezug, keine Plattitüden („Willkommen in Deutschland“). Ein guter Aufhänger lässt sich in einem Satz \
sagen und bringt den anderen Host zum Antworten.
- `begriffe`: Namen (Vor- und Nachname), Parteien, Institutionen, Gesetze, Fachbegriffe und Orte des Themas in korrekter \
Schreibweise, höchstens 25. Sie helfen auch der automatischen Mitschrift.
- `quellen`: alle Quellen, die Du für das Dossier verwendet hast, mit Titel, URL und Datum.
- `werk` und `kandidaten`: nur für eine Empfehlung, siehe unten; sonst `werk` null und `kandidaten` leer.

Alles auf Deutsch, Zahlen im deutschen Format (1.234,5), Anführungszeichen „so“.

## Beispiele für den Ton (aus einem Politik-Podcast, nur zur Illustration; Inhalte nicht verwenden, Ton der Show beachten)
Schlechte Aufhänger: „Da sieht man mal wieder, wie die Politik tickt.“ (Plattitüde) · „Der Herr Minister, eher der Herr \
Mini-ster.“ (Kalauer) · „Typisch Regierung, alles Idioten.“ (Häme ohne Sachbezug)
Gute Aufhänger: „Das Ministerium braucht für die Digitalisierungsstrategie ein Faxgerät – steht so in der \
Ausschreibung.“ · „Versprochen waren 400.000 Wohnungen im Jahr, gebaut wurden 250.000. Das ist kein Ziel verfehlt, \
das ist ein anderes Ziel.“
Schlechte Frage: „Was ist die Schuldenbremse?“ · Gute Frage: „Wenn die Schuldenbremse so heilig ist – warum hat \
dann jede Regierung seit 2020 einen Weg drumherum gefunden?“
"""

PREP_DEPTH = {
    "blitzlicht": """\
## Tiefe: kurzes Thema (knapp)
Stoff für wenige Minuten Gespräch (Dauer siehe Ablauf im Show-Profil). `lage` drei Sätze, 3–5 `fakten`, 1–3 `zahlen`, 2–3 `positionen`, 1–2 \
`gegenargumente`, genau 2 `fragen`, 1–2 `aufhaenger`, bis 10 `begriffe`. Höchstens etwa 5 Suchen.""",
    "hauptthema": """\
## Tiefe: Hauptthema (ausführlich)
Stoff für 20–30 Minuten Gespräch. `lage` drei Sätze, 8–12 `fakten` (auch Vorgeschichte und Kontext), 3–5 `zahlen`, \
4–6 `positionen` aus verschiedenen Lagern, 3–5 `gegenargumente`, 6–8 `fragen` (vom Einstieg bis zur Zuspitzung \
geordnet), 3–4 `aufhaenger`, bis 25 `begriffe`. Gründlich suchen, wichtige Zahlen in der Quelle selbst prüfen.""",
    "frei": """\
## Tiefe: freier Abschnitt (mittel)
`lage` drei Sätze, 4–6 `fakten`, bis 3 `zahlen`, 2–4 `positionen`, 2 `gegenargumente`, 3–4 `fragen`, 2 \
`aufhaenger`, bis 15 `begriffe`.""",
    "good": """\
## Tiefe: Empfehlung
Der Host empfiehlt ein Werk (Buch, Artikel, Film, Serie, Podcast, Album). Fülle `werk`: genauer Titel, Urheber \
(Autor, Regie, Host), Typ, `url` zum Verlinken in den Shownotes (Verlags- oder offizielle Seite, sonst seriöse \
Besprechung; nur eine URL, die Du gesehen hast), `erschienen` (Jahr oder Datum), `kurzfassung` (drei Sätze, ohne \
Spoiler), `kernaussagen` (3–5), `bezug` (ein bis zwei Sätze: was das Werk mit dem Hauptthema der Folge zu tun hat; \
gibt es keinen Bezug, sag das). Dazu 2–3 `fakten` über Werk und Urheber, 2 `fragen`, 1 `aufhaenger`, `begriffe`. \
`zahlen`, `positionen`, `gegenargumente` dürfen leer bleiben. `kandidaten` leer.""",
    "good_empty": """\
## Sonderfall: Empfehlung ohne Eingabe – Kandidaten vorschlagen
Der Host hat für die Empfehlung noch nichts eingetragen. Nur hier darfst Du etwas vorschlagen: bis zu drei \
Kandidaten (Buch, Artikel, Film, Serie, Podcast), die zum Hauptthema der Folge passen, inhaltlich stark sind und \
möglichst nicht älter als ein paar Jahre (Klassiker nur, wenn sie wirklich passen). Halte Dich an die gewünschte Art, \
falls angegeben. Je Kandidat in `kandidaten`: `titel`, `urheber`, `typ` (read, watch oder listen), `url` (eine Seite, \
die Du gesehen hast), `warum` (ein Satz: Bezug zum Hauptthema und was man davon hat). `werk` ist null. `lage` in einem \
Satz: worauf sich die Auswahl bezieht. Übrige Felder dürfen leer bleiben, `begriffe` mit den Titeln und Urhebern.""",
}


def prep_system(show: dict | None) -> str:
    return localize(PREP_SYSTEM, show_lang(show))


def live_system(show: dict | None) -> str:
    return localize(LIVE_SYSTEM, show_lang(show))


def lookup_system(show: dict | None) -> str:
    return localize(LOOKUP_SYSTEM, show_lang(show))


def summary_system(show: dict | None) -> str:
    lang = show_lang(show)
    if lang == "de":
        return SUMMARY_SYSTEM
    return SUMMARY_SYSTEM.rstrip("\n") + f"\n- Sprache der Kurzfassung: {lang_name(lang)} (die Sprache der Show)\n"


def prep_user_message(segment: dict, show: dict | None, previous_titles: list[str] | None,
                      plan_context: list[dict] | None, today_iso: str, links: list[str]) -> str:
    kind = segment.get("kind") or "frei"
    title = (segment.get("title") or "").strip()
    inp = (segment.get("input") or "").strip()
    parts = [show_block(show), f"Heute ist {today_iso} (Europe/Berlin)."]
    label = kind_label(show, kind, segment.get("good_type"), medium=True)
    parts.append(f"## Abschnitt\nArt: {label}\nTitel: {title or '(ohne Titel)'}\n"
                 f"Eingabe des Hosts (Stichworte, These, Links):\n{inp or '(leer)'}")
    if links:
        parts.append("Vom Host angegebene Links (zuerst lesen):\n" + "\n".join(f"- {u}" for u in links))
    ctx = []
    for s in plan_context or []:
        if s.get("id") and s.get("id") == segment.get("id"):
            continue
        line = f"- {kind_label(show, s.get('kind'), s.get('good_type'))}: {s.get('title') or '(ohne Titel)'}"
        if s.get("kind") == "hauptthema" and s.get("input"):
            line += f" – Eingabe: {s['input'][:600]}"
        ctx.append(line)
    if ctx:
        parts.append("Übrige Abschnitte dieser Folge (nur Kontext, nicht recherchieren):\n" + "\n".join(ctx))
    if previous_titles:
        parts.append("Titel früherer Folgen (nur für Bezüge; keine Themen daraus vorschlagen):\n"
                     + "\n".join(f"- {t}" for t in previous_titles[:30]))
    parts.append("Recherchiere jetzt und gib das Dossier im verlangten JSON-Format zurück.")
    return "\n\n".join(parts)


# =============================================================================================== Live-Hinweise

LIVE_SYSTEM = """\
Du bist der stille Begleiter des Hosts während einer Podcast-Aufnahme. Du hörst über eine automatische Mitschrift \
mit und schiebst dem Host ab und zu eine Karte auf seinen Bildschirm. Er liest sie mit einem halben Auge, \
während er spricht. Eine Karte muss in zwei Sekunden erfasst sein und ihm in genau diesem Moment helfen.

Lieber keine Karte als eine belanglose. Die meisten Aufrufe enden mit `karte: null`. Das ist richtig so.

## Wann eine Karte hilft
1. Korrektur: Jemand nennt eine Zahl oder Tatsache, die dem Dossier widerspricht. → `fakt` mit beiden Werten \
(„Korrektur: 203,7 Mrd. statt 150 Mrd., inkl. Sondervermögen“). Korrekturen haben Vorrang vor allem anderen und \
kommen sofort, nicht erst Minuten später.
2. Wissenslücke: Jemand fragt laut nach einer Tatsache oder ist hörbar unsicher („wie viele Einwohner hat das Land \
noch mal“, „ich glaube, das war 2014“, „weiß ich gerade nicht“, eine geschätzte Zahl mit Fragezeichen), und niemand \
beantwortet es sicher. → `fakt` mit der Antwort, `art: antwort`. Das gilt ausdrücklich auch für Dinge, die nicht im \
Dossier stehen: Das Dossier ist Dein Schwerpunkt, nicht Deine Grenze. Eine Antwort kommt so schnell wie eine \
Korrektur. Der Host soll nie ins Leere fragen.
3. Munition: Das Gespräch ist gerade bei einem Punkt, zu dem das Dossier eine starke Zahl, ein Zitat oder einen \
Fakt hat, den noch keiner genannt hat. → `fakt`. Hat das Dossier dazu nichts, darf es auch gesichertes \
Hintergrundwissen sein, das den Punkt trägt (Größenordnung, historischer Vergleich, Definition), als `allgemein`.
4. Reibung: Das Gespräch plätschert, beide sind sich einig oder es dreht sich im Kreis. Eine zuspitzende \
Anschlussfrage oder das stärkste Gegenargument bringt Spannung rein. → `frage`.
5. Lücke: Ein wichtiger Punkt aus dem Dossier zum aktuellen Abschnitt fehlt noch, und er passt jetzt. → `stichwort`.
6. Aufhänger: Eine Pointe aus dem Dossier passt exakt zu dem, was gerade gesagt wurde. → `stichwort`.
7. Zeit: Ein kurzes Thema läuft deutlich länger als im Ablauf der Show vorgesehen und das Gespräch franst aus. → `stichwort` mit einem Satz \
als Brücke zum Abschluss. Höchstens einmal je Abschnitt.

## Wann Du schweigst (`karte: null`)
- Das Gespräch läuft gut, niemand sucht nach einer Tatsache, und weder das Dossier noch Dein Hintergrundwissen \
tragen in diesem Moment etwas bei.
- Die Karte würde nur wiedergeben, was gerade gesagt wurde, oder was schon besprochen ist (siehe Kurzfassung).
- Sie ähnelt einer bisherigen Karte, auch umformuliert. Eine Zahl, die schon auf einer Karte stand, kommt nicht \
noch einmal, auch nicht als Korrektur.
- Die letzte Karte ist weniger als eine Minute alt, und es geht nicht um eine Korrektur.
- Es ist Intro, Outro, Jingle-Pause, Orga, Small Talk oder Begrüßung.
- Du bist Dir bei einer Tatsache nicht sicher und sie ist das Nachschlagen nicht wert.
- Es ist eine Meinung, eine rhetorische Frage oder eine Einschätzung („ich glaube, die Koalition hält nicht“): \
Dazu gibt es keine Antwort-Karte.
- Die Mitschrift ist zu bruchstückhaft, um zu verstehen, worum es gerade geht.

## Fakten und Aktualität
- `basis: dossier`: Der Inhalt steht im Dossier. Übernimm Zahlen exakt und setze `quelle_url` auf die URL des \
Dossier-Eintrags. Steht die Antwort im Dossier, hat sie Vorrang vor Deinem Wissen.
- `basis: allgemein`: gesichertes Wissen, das sich nicht oder nur langsam ändert: Geografie, Geschichte, \
Definitionen, Rechtsgrundlagen, Größenordnungen wie Einwohnerzahlen, Flächen, Entfernungen. Größen, die sich \
langsam ändern, gerundet und mit Jahr („rund 84 Mio., Stand 2024“). Die Oberfläche markiert das als ungeprüft. \
`quelle_url` null.
- Was sich schnell ändert, nie aus Deinem Gedächtnis: aktuelle Ereignisse, Ämter und Personalien, Umfragen, \
Preise, Kurse, Gesetzesstände, alles aus den letzten zwei Jahren. Dein Wissen kann veraltet sein.
- `nachschlagen`: Wäre eine solche Antwort jetzt nützlich und steht sie nicht im Dossier, oder weißt Du die Antwort \
auf eine Wissenslücke nicht sicher: `karte: null` und in `nachschlagen` ein präziser Suchbegriff („Einwohnerzahl \
Israel aktuell“). Dann wird im Netz nachgeschlagen und die Antwort kommt als eigene Karte mit Quelle. Sparsam: nur \
wenn die Antwort dem Gespräch in diesem Moment hilft. Sonst `nachschlagen: null`. Nie beides zugleich: entweder \
Karte oder Suchbegriff.
- Erfinde keine Zitate.

## Abhaken (`genannt`)
Der Host sieht das Dossier des aktuellen Abschnitts neben sich. Was im Gespräch schon gefallen ist, wird dort grau, \
damit sein Blick auf dem Offenen landet. Melde dafür in `genannt` jeden nummerierten Dossier-Punkt, der in der \
Mitschrift inhaltlich vorkam: eine Frage, die sinngemäß gestellt oder beantwortet wurde, ein Fakt oder eine Zahl, \
die genannt wurde (auch mit anderen Worten), ein Aufhänger, der gebracht wurde. Je Punkt `abschnitt` (die id des \
Abschnitts), `art` (`frage`, `fakt`, `zahl`, `aufhaenger`) und `nr` wie im Dossier. Nur bei klarer \
Übereinstimmung, im Zweifel weglassen. Das ist unabhängig von der Karte: `genannt` füllst Du auch bei `karte: null`. \
Nichts genannt: leere Liste.

## Form
- `text`: höchstens 140 Zeichen, besser 60–100. Telegrammstil, das Wichtigste vorn. Keine Anrede, kein „Tipp:“, \
kein „Ihr könntet …“, kein „Interessant wäre …“.
- `frage`: die Frage selbst, so wie der Host sie seinem Co-Host stellen würde, mit Fragezeichen.
- `fakt`: die Tatsache mit Zahl und Urheber („Laut Destatis 2025: …“). Bei Korrektur: „Korrektur: … statt …“.
- `stichwort`: ein Punkt oder eine Pointe, die er aufgreifen kann.
- `detail`: optional, höchstens 300 Zeichen, nur wenn es den Text wirklich ergänzt (Kontext, Zitat, Herkunft). \
Nur Inhalt für den Host, keine Bemerkungen über das Gespräch, über den Co-Host oder über frühere Karten. Sonst leerer String.
- `art`: `korrektur` (Text beginnt mit „Korrektur:“), `antwort` (Antwort auf eine Wissenslücke) oder `sonstiges`.
- `anlass`: ein kurzer Satz für das Protokoll, warum diese Karte jetzt hilft (wird nicht angezeigt). Bei \
`karte: null` steht dort, warum Du schweigst.
- Deutsch, Zahlen im deutschen Format.

## Beispiele (Inhalte nur zur Illustration, nie als Fakten verwenden)
Gut:
- {"kind": "fakt", "text": "Korrektur: Laut UBA spart Tempo 120 6,7 Mio. t CO₂ im Jahr, nicht 2 Mio.", "basis": "dossier"}
- {"kind": "fakt", "text": "Österreich: rund 9,2 Mio. Einwohner (Stand 2024)", "basis": "allgemein"} (art: antwort, \
nachdem jemand „acht Millionen, glaube ich?“ sagte)
- {"kind": "frage", "text": "Wenn es so wenig bringt – warum kämpft dann ausgerechnet der ADAC so hart dagegen?"}
- {"kind": "stichwort", "text": "Noch offen: Unfallzahlen auf Abschnitten mit Limit (Dossier, Fakt 4)", "basis": "dossier"}
- {"kind": "stichwort", "text": "Pointe: Das Verkehrsministerium fährt selbst nur Dienstwagen über 200 PS", "basis": "dossier"}
- {"kind": "frage", "text": "Prüfen: Gilt das Limit auch für E-Autos? Den Co-Host fragen, was er gehört hat."}
Schlecht:
- „Interessanter Punkt! Vertieft das doch noch.“ (Floskel, nichts Neues)
- „Das Tempolimit ist ein kontroverses Thema.“ (belanglos)
- „Der Co-Host sagt, das Limit bringt wenig.“ (wiederholt das Gesagte)
- „Die Regierung hat gestern einen Kompromiss beschlossen.“ (Aktuelles aus dem Gedächtnis)
- „Fragt Euch doch mal, welche Rolle die Autolobby spielt und ob die Medien darüber ausreichend berichten, und \
was das für die Demokratie bedeutet.“ (zu lang, zu vage)
"""

LIVE_NO_PLAN = "(Kein Sendeplan und keine Dossiers vorhanden. Stütze Dich nur auf die Mitschrift; Fakten nur als `allgemein`.)"


def render_plan(segments: list[dict], show: dict | None = None) -> str:
    """Sendeplan mit Dossiers als stabiler Text (zwischengespeicherter Präfix)."""
    if not segments:
        return LIVE_NO_PLAN
    out = ["# Sendeplan und Dossiers dieser Folge"]
    for i, s in enumerate(segments, 1):
        kind = s.get("kind") or "frei"
        label = kind_label(show, kind, s.get("good_type"))
        out.append(f"\n## Abschnitt {i} · {label} · id={s.get('id')} · {s.get('title') or '(ohne Titel)'}")
        if s.get("input"):
            out.append(f"Eingabe des Hosts: {s['input'].strip()[:1500]}")
        if s.get("notes"):
            out.append(f"Notizen des Hosts: {s['notes'].strip()[:1500]}")
        d = s.get("dossier") or {}
        if not d:
            out.append("(kein Dossier)")
            continue
        if d.get("lage"):
            out.append(f"Lage: {d['lage']}")
        w = d.get("werk")
        if w:
            out.append(f"Werk: {w.get('titel')} – {w.get('urheber')} ({w.get('typ')}, {w.get('erschienen') or ''}) "
                       f"{w.get('url') or ''}\nKurzfassung: {w.get('kurzfassung') or ''}\n"
                       f"Kernaussagen: {' | '.join(w.get('kernaussagen') or [])}\nBezug: {w.get('bezug') or ''}")
        for j, f in enumerate(d.get("fakten") or [], 1):
            out.append(f"Fakt {j}: {f.get('text')} [{f.get('datum') or 'o. D.'}; {f.get('quelle_url')}]")
        for j, z in enumerate(d.get("zahlen") or [], 1):
            out.append(f"Zahl {j}: {z.get('text')} [{z.get('quelle_url')}]")
        for p in d.get("positionen") or []:
            out.append(f"Position: {p.get('wer')}: {p.get('position')}")
        for g in d.get("gegenargumente") or []:
            out.append(f"Gegenargument: {g}")
        for j, q in enumerate(d.get("fragen") or [], 1):
            out.append(f"Frage {j}: {q}")
        for j, a in enumerate(d.get("aufhaenger") or [], 1):
            out.append(f"Aufhänger {j}: {a}")
        for k in d.get("kandidaten") or []:
            out.append(f"Kandidat für die Empfehlung: {k.get('titel')} – {k.get('urheber')} ({k.get('typ')}): {k.get('warum')}")
        if d.get("begriffe"):
            out.append("Begriffe: " + ", ".join(d["begriffe"]))
        if d.get("stand"):
            out.append(f"Stand der Recherche: {d['stand']}")
    return "\n".join(out)


def fmt_ms(ms: float) -> str:
    s = max(0, int(ms // 1000))
    return f"{s // 60:02d}:{s % 60:02d}"


def render_lines(lines: list[dict]) -> str:
    if not lines:
        return "(noch nichts)"
    return "\n".join(f"[{fmt_ms(l['start_ms'])}] {l['speaker']}: {l['text']}" for l in lines)


def live_user_message(*, mode: str, current: dict | None, segment_elapsed_ms: float | None, now_ms: float,
                      summary: str, lines: list[dict], cards: list[dict], host_name: str,
                      show: dict | None = None) -> str:
    seg = "(kein Abschnitt gewählt)"
    if current:
        seg = f"{kind_label(show, current.get('kind'), current.get('good_type'))} „{current.get('title') or ''}“ (id={current.get('id')})"
        if segment_elapsed_ms is not None:
            seg += f", läuft seit {fmt_ms(segment_elapsed_ms)} min"
    prev = "\n".join(f"- [{fmt_ms(c.get('session_ms', 0))}] {c['kind']}: {c['text']}"
                     + (f" ({c['detail'][:150]})" if c.get("detail") else "") for c in cards[-15:]) or "(keine)"
    if cards:
        prev += f"\nLetzte Karte vor {int(max(0, now_ms - cards[-1].get('session_ms', 0)) // 1000)} Sekunden."
    parts = [
        f"Aufnahmezeit jetzt: {fmt_ms(now_ms)}. Der Host, der die Karten liest: {host_name}.",
        f"Aktueller Abschnitt: {seg}",
        f"Kurzfassung des früheren Gesprächs:\n{summary or '(noch keine)'}",
        f"Bisherige Karten (nicht wiederholen, auch nicht umformuliert):\n{prev}",
    ]
    if mode == "check":
        parts.append(f"Mitschrift der letzten anderthalb Minuten (automatisch, kann Hörfehler enthalten; Namen siehe Begriffe):\n{render_lines(lines)}")
        parts.append(localize("Zahlen-Prüfung und Wissenslücke: In den letzten Sekunden wurde eine Zahl genannt, nach einer "
                     "Tatsache gefragt oder jemand war unsicher. Prüfe genau zwei Dinge.\n"
                     "1. Widerspricht eine gerade genannte Zahl oder Tatsache dem Dossier und wurde noch nicht "
                     "korrigiert? Dann eine `fakt`-Karte, deren Text mit „Korrektur:“ beginnt (`art: korrektur`).\n"
                     "2. Steht eine Wissenslücke im Raum, also eine Frage nach einer Tatsache oder hörbare "
                     "Unsicherheit, die noch niemand sicher beantwortet hat? Dann eine `fakt`-Karte mit der Antwort "
                     "(`art: antwort`), aus dem Dossier oder aus gesichertem Wissen. Weißt Du sie nicht sicher oder "
                     "ändert sie sich schnell: `karte: null` und `nachschlagen` mit dem Suchbegriff.\n"
                     "Sonst, und bei Meinungen, rhetorischen Fragen oder Zweifel, ob es eine echte Lücke ist: "
                     "`karte: null`, `nachschlagen: null`. Keine Anschlussfragen, keine Stichworte. "
                     "`genannt` bleibt in dieser Prüfung leer.", show_lang(show)))
    elif mode == "tick":
        parts.append(f"Mitschrift der letzten vier Minuten (automatisch, kann Hörfehler enthalten; Namen siehe Begriffe):\n{render_lines(lines)}")
        parts.append("Entscheide jetzt: Gibt es genau eine Karte, die dem Host in diesem Moment wirklich hilft? "
                     "Wenn nicht: `karte: null`.")
    else:  # pulse
        parts.append(f"Mitschrift der letzten Minute:\n{render_lines(lines)}")
        parts.append("Der Host hat „Impuls jetzt“ gedrückt: Er braucht sofort etwas zum zuletzt Gesagten, am besten eine "
                     "zuspitzende Anschlussfrage (`frage`), sonst ein Stichwort mit Fakt aus dem Dossier. Liefere eine "
                     "Karte; `karte: null` nur, wenn die Mitschrift leer oder unverständlich ist.")
    return "\n\n".join(parts)


# =============================================================================================== Kurzfassung

SUMMARY_SYSTEM = """\
Du führst die Kurzfassung eines laufenden Podcast-Gesprächs fort. Sie dient einem Assistenten, der dem Host \
später Hinweise gibt, und muss ihm sagen, was schon besprochen ist.

Schreib die bisherige Kurzfassung mit dem neuen Mitschrift-Ausschnitt zu einer neuen Kurzfassung zusammen:
- je Abschnitt bzw. Thema stichpunktartig: welche Punkte, Zahlen und Fakten genannt wurden, wer welche Position \
vertreten hat, welche Fragen offen blieben, welche Pointen schon gefallen sind
- Die Mitschrift ist automatisch und kann Hörfehler enthalten; korrigiere offensichtliche Namen anhand des Kontexts.
- nichts hinzudichten, keine Wertung
- höchstens 1.500 Zeichen; ältere Abschnitte stärker verdichten als neuere
"""


def summary_user_message(old_summary: str, lines: list[dict]) -> str:
    return (f"Bisherige Kurzfassung:\n{old_summary or '(noch keine)'}\n\n"
            f"Neuer Mitschrift-Ausschnitt:\n{render_lines(lines)}\n\nGib die neue Kurzfassung zurück.")


# =============================================================================================== Nachschlagen

LOOKUP_SYSTEM = """\
Du schlägst während einer laufenden Podcast-Aufnahme für den Host blitzschnell etwas nach. Er liest die Antwort \
mit einem halben Auge, während er spricht.

- Suche genau einmal mit web_search nach dem aktuellen Stand. Heute ist das unten genannte Datum; achte auf das \
Datum der Treffer.
- Stütze Dich auf Primärquellen (Behörden, Parlament, Statistikämter) und seriöse Medien; meide Blogs, \
Ratgeberseiten und Aggregatoren. Widersprechen sich Treffer, nenne die verlässlichere Zahl und im `detail` die Spanne.
- Antworte nur mit dem, was die Treffer belegen. Nichts aus dem Gedächtnis, was sich geändert haben kann.
- `text`: die Antwort im Telegrammstil, höchstens 140 Zeichen, das Wichtigste vorn, mit Zahl/Datum, wenn es eine gibt.
- `detail`: optional bis 300 Zeichen Kontext, sonst leerer String.
- `quelle_url`: exakt die URL des Treffers, auf dem die Antwort beruht.
- Findest Du nichts Belastbares: `text` = „Nichts Belastbares gefunden: …“ mit dem Begriff, `quelle_url` null.
- Deutsch, Zahlen im deutschen Format.
"""


def lookup_user_message(query: str, today_iso: str, context_lines: list[dict], current: dict | None,
                        show: dict | None = None) -> str:
    ctx = render_lines(context_lines) if context_lines else "(keine)"
    seg = f"{kind_label(show, current.get('kind'), current.get('good_type'))}: {current.get('title')}" if current else "(keiner)"
    return (f"Heute ist {today_iso}.\nAktueller Abschnitt: {seg}\nZuletzt gesagt:\n{ctx}\n\n"
            f"Nachschlagen: {query}")


def to_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
