# Thema

Regieraum liefert ein neutrales Standard-Thema mit: kühle Graustufen, ein Kobaltblau als Akzentfarbe und System-Schriften (keine Schriftdateien). Es liegt in `app/static/design/`. Wer ein eigenes Erscheinungsbild möchte, ersetzt diesen Ordner, ohne Code zu ändern. Das Thema regelt nur Farbe, Schrift und Schatten; Aufbau, Abstände und Raster bleiben Sache der Anwendung.

## Die vier Dateien

Die Vorlage `app/templates/_base.html` lädt genau diese Dateien in dieser Reihenfolge:

| Datei | Inhalt |
|---|---|
| `tokens-unprefixed.css` | Alle Variablen (Farben, Abstände, Radien, Schatten, Zeiten, Schriften), hell in `:root`, dunkel unter `[data-theme="dark"]` |
| `components.css` | Grundkomponenten: Schaltflächen, Karten, Felder, Tags, Balken, Aufzählungen |
| `app-basis.css` | Typo-Skala, Grundstile, App-Rahmen mit Navigation, Listen, Tabellen, Status, Toast, Modal |
| `app-basis.js` | Menü-Knopf für die Seitenleiste auf schmalen Bildschirmen, Schatten der Kopfleiste beim Scrollen |

Die Stylesheets `app/static/css/app.css`, `studio.css` und `manage.css` sowie das JavaScript verwenden diese Variablen und die Klassen `tr-*`. Alle Klassen `tr-*` aus `components.css` und `app-basis.css` gehören zur Schnittstelle: ein eigenes Thema muss sie mit gleichem Namen und gleicher Funktion liefern (Zustände wie `:hover`, `:focus-visible`, `[disabled]`, `.is-error`, `[aria-current="page"]`, `[aria-selected="true"]` eingeschlossen).

## Variablen

Die Namen `--petrol*` stammen aus einer früheren Fassung und bleiben aus Kompatibilität bestehen. Sie bedeuten schlicht „die Akzentfarbe“.

**Farben**

| Variable | Bedeutung |
|---|---|
| `--paper` | Seitenhintergrund |
| `--surface` | Flächen (Karten, Listen, Felder, Modal) |
| `--ink` | Haupttext, Überschriften, Rahmen der sekundären Schaltfläche |
| `--slate` | Sekundärtext, Beschriftungen, Icons |
| `--muted` | Gedämpfte Details: Feldrahmen, neutrale Balken, Platzhalter-nahe Elemente |
| `--line` | Trennlinien und Rahmen |
| `--petrol` | Akzentfarbe: Hauptschaltfläche, Links, Fokusring, Auswahl |
| `--petrol-dark` | Akzentfarbe bei Hover |
| `--petrol-tint` | Sehr heller Ton der Akzentfarbe: Hover der sekundären Schaltfläche, ausgewählte Tabellenzeile |
| `--on-petrol` | Text und Icons auf `--petrol` und `--petrol-dark` |
| `--sand`, `--steel` | Zusatzfarben für Diagramme und Hervorhebungen |
| `--success` | Erfolg (Text) |
| `--warning` | Warnung (Text) |
| `--danger` | Fehler (Text, Fehlerrahmen) |
| `--danger-dark` | Gefährliche Schaltfläche bei Hover (dunkler als `--danger`) |

**Abstände, Form, Bewegung**

| Variable | Bedeutung |
|---|---|
| `--space-1` … `--space-9` | Abstandsskala (4 bis 96 px) |
| `--radius-sm`, `--radius-md`, `--radius-lg` | Eckenradien |
| `--shadow-sm` | Schatten der Kopfleiste beim Scrollen |
| `--shadow-md` | Schatten schwebender Elemente (Toast, Modal) |
| `--dur-fast`, `--dur-base`, `--dur-slow` | Dauer von Übergängen |
| `--ease` | Beschleunigungskurve |

**Schrift**

| Variable | Bedeutung |
|---|---|
| `--font-sans` | Fließtext und Bedienelemente |
| `--font-serif` | Überschriften und große Zahlen |

**Aus `app-basis.css` (Block `:root`)**

| Variable | Bedeutung |
|---|---|
| `--fs-display`, `--fs-h1`, `--fs-h2`, `--fs-h3`, `--fs-body`, `--fs-small`, `--fs-figure` | Schriftgrößen |
| `--lh-tight`, `--lh-body` | Zeilenhöhen |
| `--dark-bg`, `--dark-ink`, `--dark-slate`, `--dark-petrol`, `--on-dark-petrol`, `--dark-petrol-hover` | Farben der dunklen Akzentfläche (`.tr-dark`) |
| `--container` | Maximale Breite des Inhalts |
| `--column` | Breite einspaltiger Seiten |
| `--sidebar` | Breite der Seitenleiste |
| `--focus` | Fokusring (außen) |
| `--focus-inset` | Fokusring (innen, für Listen und Navigation) |

## Kontrastregel

Ein Thema muss mindestens diese Werte nach WCAG 2.x erreichen, jeweils auf `--paper` und auf `--surface`:

- `--ink`, `--slate`, `--success`, `--warning`, `--danger` und `--petrol` (als Linkfarbe): mindestens 4,5 : 1
- `--on-petrol` auf `--petrol` und auf `--petrol-dark`: mindestens 4,5 : 1
- `--muted`: mindestens 3 : 1 (nur Rahmen und Details, nie für Fließtext)

## Eigenes Thema einbinden

1. Einen Ordner mit denselben vier Dateien anlegen (`tokens-unprefixed.css`, `components.css`, `app-basis.css`, `app-basis.js`), am einfachsten als Kopie des Standard-Themas.
2. Eigene Schriften in `fonts/` daneben legen und im eigenen CSS selbst per `@font-face` laden, zum Beispiel `src: url("fonts/meine-schrift-400.woff2")`. Danach `--font-sans` und `--font-serif` darauf setzen.
3. Den Ordner per Docker-Volume über `/srv/app/static/design` legen:

```yaml
services:
  regieraum:
    volumes:
      - ./mein-thema:/srv/app/static/design:ro
```

Das Standard-Thema im Image wird damit vollständig überdeckt. Fehlt eine der vier Dateien, ist die Seite unvollständig; fehlt eine Variable, greift der Browser-Standard.
