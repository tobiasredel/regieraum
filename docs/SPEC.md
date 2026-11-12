# Podcast-Studio – Konzept und technische Spezifikation

Stand: 11.11.2026. Verbindlich für alle, die an diesem Repo bauen. Wer von der Spezifikation abweichen muss,
ändert sie im selben Commit und begründet es dort in einem Satz.

## 1. Zweck

Web-Werkzeug für die Produktion eines Gesprächs-Podcasts mit festem Ablauf: Vorbereitung, Aufnahme, Klang, Veröffentlichung. Entstanden für eine Show mit zwei Hosts; Name der Show, Namen der Hosts und die Abschnittsarten sind Einstellungen (siehe „Show-Profil und Sendeformat“).
Es ersetzt Riverside (Aufnahme), Auphonic (Klang) und den manuellen Upload bei Spotify.

Entscheidungen des Auftraggebers (11.11.2026):

| Thema | Entscheidung |
|---|---|
| Aufnahme | Doppel-Ender: jeder Browser nimmt das eigene Mikro lokal verlustfrei auf und lädt es stückweise hoch. Das Live-Gespräch (WebRTC) ist nur zum Hören und Sehen da und landet nie in der Folge |
| Video | nur live, pro Person an- und abschaltbar, niedrige Auflösung, wird nie aufgezeichnet |
| Jingles | Klick spielt den Jingle für alle hörbar ab; im fertigen Mix sitzt die Originaldatei exakt an dieser Stelle. Kein Ducking. Aus den Klicks entstehen Kapitelmarken |
| Klang | eigene Pipeline auf dem eigenen Server (Vorbild Auphonic), keine fremden Dienste |
| Geräte | Normalfall Laptop mit Kopfhörern. Laptop ohne Kopfhörer und Handy oder Tablet müssen ebenfalls funktionieren |
| Schnitt | Pause-Knopf während der Aufnahme, danach Anfang und Ende trimmen und Bereiche herausschneiden. Kein vollwertiger Editor |
| Veröffentlichung | Prüfen, dann ein Klick (sofort oder geplant). Titel, Shownotes und Kapitel werden aus dem Transkript vorgeschlagen |
| Zugang | Host hinter der Anmeldung (Passwort oder vorgeschalteter Proxy). Der Co-Host über einen festen geheimen Link ohne Login. Gäste über Einmal-Links. Nur der Host steuert Aufnahme und Jingles |
| Teilnehmer | ausgelegt auf zwei, technisch bis vier (WebRTC-Mesh, kein Medienserver) |
| Spotify | eigener RSS-Feed, die Show zieht einmalig von Spotify for Creators um; alle alten Folgen werden mit unveränderter GUID importiert |
| Feed-Hosting | fertige MP3s und Feed liegen in einem S3-kompatiblen Speicher (zum Beispiel Cloudflare R2), nicht auf dem Server |
| Vorbereitung | nur für den Host: Sendeplan aus eigenen Themen (Abschnitte nach dem eingestellten Sendeformat, im Standard drei kurze Themen, ein Hauptthema und eine Empfehlung). Die App schlägt keine Themen vor, sie recherchiert zu den eingegebenen Themen je ein Dossier |
| Live-Begleiter | nur für den Host: hört während der Aufnahme mit (Cloud-Transkription über OpenAI) und spielt Stichworte, Fragen und Wissenswertes zu. Ruhiger Strom (höchstens eine Karte alle 30–45 s) plus Knöpfe „Impuls jetzt“ und „Nachschlagen“ |

## 2. Aufbau

```
Browser (Host, Gast)            Server: Container regieraum                 Cloudflare R2
 ├ WebRTC-Mesh  <───────────>    (nur Signalisierung über WebSocket)
 ├ AudioWorklet-Rekorder ──────> Chunk-Upload ─> /data/episodes/…
 └ Studio-Oberfläche   <──────>  FastAPI + SQLite
                                 Job-Läufer ─> pipeline/ (ffmpeg, deep-filter, whisper)
                                            ─> publish/  ──────────────────────> feed.xml, media/…
```

Ein Container, ein Prozess (uvicorn), SQLite im WAL-Modus, Jobs als Unterprozesse (`python -m pipeline …`) mit `nice`.
Kein Build-Schritt im Frontend: ES-Module, kein Framework, kein npm.

### Verzeichnisse und Zuständigkeiten

| Pfad | Inhalt | Paket |
|---|---|---|
| `app/` | FastAPI-Server: Auth, Räume, Signalisierung, Upload, REST, Job-Läufer | A |
| `app/static/js/studio/`, `app/templates/studio.html`, `app/templates/join.html` | Live-Studio für Host und Gast | B |
| `pipeline/` | Audio-Pipeline, reines Python-Paket mit CLI, kennt weder DB noch HTTP | C |
| `publish/` | Speicher (lokal, R2), Feed, Import, Shownotes, kennt weder DB noch HTTP | D |
| `app/static/js/manage/`, übrige Templates | Folgenliste, Prüfen und Schneiden, Soundboard, Einstellungen | E |
| `assist/` | Vorbereitung (Dossiers) und Live-Begleiter (Mitschrift, Hinweise), kennt weder DB noch HTTP | F |
| `app/static/design/` | Design-System-Kopie, nicht anfassen (kommt aus `uebernehmen.sh`) | – |
| `tests/` | `test_app_*.py`, `test_pipeline_*.py`, `test_publish_*.py`, `e2e/` | je Paket |

Entwicklung ohne Host-Python über `./dev.sh` (build, test, serve, run). Laufzeitdaten unter `$PS_DATA` (Betrieb `/data`):

```
studio.db
sounds/<sound_id>/original.<ext>  playback.wav (48 kHz stereo s16)  meta.json
episodes/<episode_id>/tracks/<track_id>/chunks/<seq 6-stellig>.pcm
episodes/<episode_id>/tracks/<track_id>/raw.flac          nach dem Zusammensetzen (+ raw.json), Chunks löscht die Pipeline
episodes/<episode_id>/work/   Zwischenstände der Pipeline (stems/, manifest.json, report.json)
episodes/<episode_id>/out/    episode.mp3, mix_uncut.m4a, peaks.json, chapters.json, transcript.json, transcript.vtt
public/                       nur Speicher-Backend „local“ (Entwicklung): feed.xml, media/…
models/                       Whisper-Modelle
```

### Konfiguration (Umgebungsvariablen)

| Variable | Bedeutung | Standard |
|---|---|---|
| `PS_DATA` | Datenverzeichnis | `/data` |
| `PS_BASE_URL` | öffentliche Adresse der App | `http://localhost:8000` |
| `PS_HOST_AUTH` | `password` (eingebaute Anmeldung), `proxy` (ein vorgeschalteter Proxy wie tinyauth schützt die Host-Pfade, die App prüft nichts) oder `none` (nur Entwicklung und Tests, warnt beim Start) | `password` |
| `PS_HOST_PASSWORD` | Passwort des Hosts bei `PS_HOST_AUTH=password`, mindestens 8 Zeichen | leer |
| `PS_LANG` | Sprache der Oberfläche, solange die Einstellung `ui.language` fehlt: `de` oder `en` | `de` |
| `PS_JOBS` | `1` startet den Job-Läufer, `0` nicht (Tests) | `1` |
| `PS_STORAGE` | `local` oder `s3` | `local` |
| `PS_PUBLIC_BASE_URL` | Basis-URL der veröffentlichten Dateien | bei `local`: `$PS_BASE_URL/public` |
| `PS_S3_ENDPOINT`, `PS_S3_BUCKET`, `PS_S3_ACCESS_KEY`, `PS_S3_SECRET_KEY` | R2-Zugang | leer |
| `PS_TURN_MODE` | `none`, `static` oder `cloudflare` | `none` |
| `PS_TURN_URLS`, `PS_TURN_USERNAME`, `PS_TURN_CREDENTIAL` | für `static` | leer |
| `PS_CF_TURN_KEY_ID`, `PS_CF_TURN_API_TOKEN` | für `cloudflare` (kurzlebige Zugangsdaten je Sitzung) | leer |
| `ANTHROPIC_API_KEY` | Shownotes-Vorschläge; fehlt der Schlüssel, entfällt nur der Vorschlag | leer |
| `PS_AI_MODEL` | Modell für Shownotes | `claude-sonnet-5-5` |
| `PS_AI_MODEL_PREP` | Modell für Dossiers (mit Websuche) | `claude-opus-5-5` |
| `PS_AI_MODEL_LIVE` | Modell für Live-Hinweise und Nachschlagen | `claude-sonnet-5-5` |
| `OPENAI_API_KEY` | Live-Mitschrift; fehlt der Schlüssel, bleibt der Live-Begleiter aus | leer |
| `PS_STT_MODEL` | Transkriptionsmodell für die Live-Mitschrift (`gpt-transcribe` ist laut Anbieter-Doku 11/2026 das empfohlene Modell mit Begriffsliste; `gpt-4o-transcribe` ist abgekündigt zum 26.02.2027) | `gpt-transcribe` |
| `PS_WHISPER_MODEL` | faster-whisper-Modell | `large-v3-turbo` |
| `PS_NTFY_URL`, `PS_NTFY_USER`, `PS_NTFY_PASS` | Benachrichtigung, Standard-Topic `podcast` | leer = aus |
| `PS_ARCHIVE_DIR` | Ordner im Container für archivierte Rohdaten (`/archive`, als Volume einbinden); leer = kein Archiv, nichts wird verschoben | leer |

### Show-Profil und Sendeformat (Einstellungen, Ergänzung 11.11.2026)

Nichts im Code nennt eine bestimmte Show. Wer spricht und wie eine Folge aufgebaut ist, steht in den Einstellungen
(`GET`/`PUT /api/host/settings`, Seite „Einstellungen“, Abschnitt „Sendung und Format“):

```
show:    {title, author, owner_name, owner_email, description, …}        Angaben für den Feed
profile: {host_name, cohost_name, genre, tone}                            Rufnamen, Thema und Ton der Show
format:  {labels: {blitzlicht, hauptthema, good, frei, read, watch, listen},
          short_minutes, plan: ["blitzlicht", "hauptthema", "good:read", …]}
```

- Abschnittsarten haben feste Schlüssel und festes Verhalten: `blitzlicht` (kurzes Thema, knappes Dossier),
  `hauptthema` (ausführliches Dossier), `good` (Empfehlung eines Werks, mit `read`, `watch` oder `listen`), `frei`.
  Die Schlüssel stammen aus der ersten Show und bleiben aus Kompatibilität; wie die Arten heißen, bestimmt
  `format.labels` (Standard: Kurzthema, Hauptthema, Empfehlung, Freier Abschnitt, Lesetipp, Sehtipp, Hörtipp).
- `format.plan` ist der Plan einer neuen Folge, `short_minutes` die vorgesehene Dauer eines kurzen Themas.
- Die Namen erscheinen in Vorbereitung, Studio, Soundboard, in Kapitelmarken („<Name>: <Titel>“) und in den Prompts.
- Prompts sprechen von Rollen („der Host“, „der Co-Host“). `assist.prompts.show_block` setzt Titel, Hosts, Rufnamen,
  Thema, Ton und den Ablauf einer Folge aus dem Profil ein (`core.assist_show`).
- Der Rufname des Hosts benennt den Host-Teilnehmer im Studio; der Rufname des Co-Hosts den Dauerlink.

### Sprache (Ergänzung 11.11.2026)

Zwei Sprachen sind unabhängig voneinander.

- **Oberfläche** (`ui.language`, `de` oder `en`; sonst `PS_LANG`, sonst `de`): Alles, was ein Mensch in der Oberfläche liest,
  auch was der Server formuliert (Fehlermeldungen, Meldungen und Fortschritt der Jobs, Status des Begleiters,
  Benachrichtigungen, Anmeldung, Standardnamen der Abschnittsarten, Show-Titel „Mein Podcast“, Name des Dauerlinks).
  Deutsch ist die Quellsprache: Der deutsche Text ist der Schlüssel (`app/i18n.py`, `tr(lang, text, **params)`).
  Wörterbücher liegen unter `app/static/i18n/en.<bereich>.json` (deutscher Text → englischer Text), der Server nutzt
  `en.server.json`. `ApiError(status, code, "Folge {id} nicht gefunden", id=…)` trägt den Schlüssel samt Platzhaltern;
  `str(exc)` bleibt der fertige deutsche Text, die Antwort wird in der Sprache der Oberfläche formuliert. Meldungen, die
  in der Datenbank stehen (Jobs, Warnungen der Verarbeitung, Fehler der Recherche), bleiben deutsch gespeichert und
  werden beim Ausliefern übersetzt, damit ein Sprachwechsel sofort wirkt. Meldungen mit Zahlen oder Namen kennen die
  Muster: Jeder Wörterbucheintrag mit Platzhaltern (`{n}`) gilt zugleich als Muster; findet `tr` keinen exakten Treffer,
  passt es den Text gegen diese Muster. Ein Platzhalter `{detail}` steht für eine eingebettete Meldung und wird selbst
  übersetzt. Log-Ausgaben, Protokollwerte und Ausnahmen, die kein Mensch sieht, bleiben deutsch. Gespeicherte Werte
  (Namen der Abschnittsarten, Titel) werden nie übersetzt; Standardwerte gelten in der Sprache der Oberfläche, solange
  nichts gespeichert ist.
- **Inhalte** (`show.language`, z. B. `de`, `de-DE`, `en`, `en-GB`; die ersten zwei Buchstaben zählen, Standard `de`):
  Dossiers, Karten des Begleiters, Shownotes, Live-Mitschrift und Transkript folgen der Sprache der Show. Die Prompts sind
  deutsch geschrieben, verlangen aber Ausgabe in dieser Sprache mit dem dort üblichen Zahlenformat und den üblichen
  Anführungszeichen (`assist.prompts.localize`; `show_block` nennt die Sprache ausdrücklich). Die Karte einer Korrektur
  trägt `art: korrektur`; ihr Text beginnt mit „Korrektur:“ bzw. dem Wort der Sprache („Correction:“). Die Muster für
  Halluzinationen der Mitschrift, Wissenslücken und Zahlwörter gibt es für Deutsch und Englisch, für andere Sprachen
  gelten beide. Der Server gibt die Sprache an die Live-Sitzung (`LiveSession(language=…)`, Mitschrift) und über das
  Manifest (`language`) an die Pipeline (Transkript, `transcript.json` und VTT); fehlt sie, gilt `de`.

## 3. Zugang und Rollen

Öffentlich sind `/join/`, `/api/join`, `/api/p/`, `/ws/p`, `/static/`, `/healthz`, `/public/` (bei Speicher `local`), `/d/` (Download-Zählung und Weiterleitung auf die Audiodatei) sowie
`/login` und `/logout`. Alles andere gehört dem Host: `/`, `/studio`, `/episodes`, `/sounds`, `/settings`, `/api/host/`
und jeder neue Pfad. Wer den Host schützt, steuert `PS_HOST_AUTH`:

- `password` (Standard): Eine Middleware verlangt für alle nicht öffentlichen Pfade, auch statische Mounts, HEAD und
  WebSockets, ein gültiges Sitzungs-Cookie. Ohne Cookie antwortet `/api/…` mit 401 (`unauthorized`, „Bitte anmelden“),
  jeder andere Pfad leitet mit 303 auf `/login?next=<Pfad>`. Ist `PS_HOST_PASSWORD` leer oder kürzer als 8 Zeichen, startet
  die App trotzdem, antwortet auf geschützten Pfaden aber mit 503 (`setup`) und einer Seite, die das Setzen der Variable
  verlangt; öffentliche Pfade laufen weiter.
- `proxy`: Die App prüft nichts. Ein vorgeschalteter Proxy (zum Beispiel Traefik mit einer Forward-Auth) trennt nach Pfad und schützt alles
  außer den öffentlichen Pfaden; `/login` leitet auf `/studio`.
- `none`: wie `proxy`, ohne Schutz; nur für Entwicklung und Tests.

Anmeldung bei `password`: `POST /login` (Formularfeld `password`, optional `next`) vergleicht in konstanter Zeit und
setzt bei Erfolg das Cookie `ps_host`, dann 303 auf `next` (nur lokale Pfade, sonst `/studio`). `POST /logout` löscht das
Cookie. Das Cookie hat den Wert `<ablauf_epoch_s>.<hex hmac-sha256>`, ist 30 Tage gültig und `HttpOnly`,
`SameSite=Lax`, `Path=/`; `Secure` gilt, wenn `PS_BASE_URL` mit `https://` beginnt. Der HMAC-Schlüssel ist
SHA-256 aus einem Zufallsgeheimnis (`$PS_DATA/host_secret`, 32 Byte, Rechte 0600, beim ersten Bedarf angelegt) und dem
Passwort; eine Passwortänderung beendet daher alle Sitzungen. Gegen Durchprobieren zählt die App im Speicher je Client-IP
Fehlversuche: ab 10 in 10 Minuten antwortet `/login` mit 429, ein Erfolg setzt den Zähler zurück. Hinter einem Proxy
sieht die App dessen Adresse als Client-IP, die Bremse gilt dann für alle gemeinsam.

Die App selbst kennt zwei Rollen, `host` und `guest`, und prüft sie bei jeder Steuernachricht.

- Host: `POST /api/host/session` (nur für den angemeldeten Host erreichbar) liefert ein Teilnehmer-Token mit Rolle `host`.
- Gast: `/join/<invite_token>` zeigt die Beitrittsseite. `POST /api/join {invite_token, name}` liefert ein Teilnehmer-Token
  mit Rolle `guest`. Der Browser merkt es sich in `localStorage`; derselbe Link führt später wieder in dieselbe Identität.
- Einladungen: `permanent` (Co-Host, widerrufbar) oder `single` (Gast, verfällt nach 48 Stunden oder nach Widerruf).
- Teilnehmer-Token: 32 Byte zufällig, in der DB nur als SHA-256. Übergabe als `Authorization: Bearer …`, beim WebSocket
  als Query-Parameter `token`.
- Höchstens vier gleichzeitig verbundene Teilnehmer; ein fünfter bekommt eine verständliche Absage.

## 4. Zeitmodell

Alles Gemeinsame wird in **Serverzeit** gemessen (Epoch-Millisekunden der Server-Uhr), Positionen innerhalb einer Folge
in **Sitzungszeit** (`session_ms = server_ms − rec_start_server_ms`).

- Uhrenabgleich: Der Client schickt alle 2 s `sync {t0}` (eigene `performance.timeOrigin + performance.now()`), der Server
  antwortet `sync {t0, server_ms}`. Offset = `server_ms − (t0 + t1) / 2`; verwendet wird die Messung mit der kleinsten
  Laufzeit der letzten 15.
- Jede Spur trägt `start_server_ms`: die Serverzeit, zu der ihr Sample 0 aufgenommen wurde (vom Client geschätzt aus
  `AudioContext.getOutputTimestamp()` bzw. `currentTime` und dem Uhren-Offset).
- Alle 10 s meldet der Client einen Uhrenpunkt `{sample_index, server_ms}`. Die Pipeline legt eine Gerade durch die Punkte
  und gleicht damit Startversatz und Drift der Soundkarten aus.
- Die Rekorder laufen ab Betreten des Studios ohne Unterbrechung. Pause schneidet nichts beim Client, sondern trägt nur
  einen Bereich in die Schnittliste ein. Verbindet sich ein Client neu, beginnt er eine neue Spur; die Pipeline setzt
  mehrere Spuren derselben Person auf der Sitzungszeit zusammen.

Genauigkeit: ±20 ms zwischen den Spuren genügt (das Gespräch selbst hat mehr Verzögerung). Jingles spielt jeder Client
lokal zur vereinbarten Serverzeit ab, nicht über WebRTC.

## 5. Datenmodell (SQLite)

Zeiten als Epoch-Millisekunden (INTEGER), IDs als kurze zufällige Strings (12 Zeichen, URL-sicher), JSON als TEXT.

```
settings(key PK, value JSON)
  show: {title, author, owner_name, owner_email, description, language, category, subcategory, explicit,
         link, copyright, cover_key}
  audio: {target_lufs: -16, true_peak_db: -1.0, denoise: "normal", crossgate: true, mp3_kbps: 160}
  site:  {enabled: true, platforms: [{name, url}]}            öffentliche Seite der Show (Abschnitt 10)
  stats: {mode: "off" | "app" | "prefix", prefix: ""}         Download-Zählung (Abschnitt 10)
invites(id PK, token UNIQUE, label, kind, created_at, expires_at, revoked_at)
participants(id PK, token_hash UNIQUE, name, role, invite_id, created_at, last_seen_at)
sounds(id PK, label, kind, chapter_title, ext, duration_ms, gain_db, fade_out_ms, sort, hotkey, archived, created_at)
  kind: intro | outro | segment | sting.  segment erzeugt eine Kapitelmarke mit chapter_title (sonst label),
  intro und outro ebenfalls („Intro“, „Outro“), sting nie.
episodes(id PK, status, source, number, season, title, summary, description_html, explicit, episode_type,
         guid UNIQUE, created_at, rec_start_server_ms, rec_stop_server_ms, duration_ms,
         publish_at, published_at, audio_key, audio_bytes, audio_mime, edl JSON, ai JSON, report JSON, legacy JSON)
  status: draft | recording | uploading | processing | review | scheduled | published | failed
  source: recorded | imported
  edl:   {trim_start_ms, trim_end_ms, cuts: [{from_ms, to_ms}]}   in Sitzungszeit, Pausen sind bereits als cuts enthalten
  ai:    {titles: [..], summary, description_html, chapter_titles: {event_id: titel}}
tracks(id PK, episode_id, participant_id, participant_name, upload_token_hash, sample_rate, format,
       start_server_ms, total_frames, total_chunks, status, meta JSON, created_at)
  status: recording | finishing | complete | incomplete | assembled
chunks(track_id, seq, first_frame, frames, bytes, received_at, PRIMARY KEY(track_id, seq))
clock_points(track_id, sample_index, server_ms)
events(id PK, episode_id, kind, server_ms, sound_id, ref_event_id, payload JSON)
  kind: rec.start | rec.pause | rec.resume | rec.stop | sound.play | sound.stop | marker
jobs(id PK, episode_id, kind, status, progress, message, log, created_at, started_at, finished_at)
  kind: process | remix | transcribe | suggest | publish | unpublish | import     status: queued | running | done | failed
```

Folgen-Ablauf: `draft` (im Studio angelegt) → `recording` → `uploading` (gestoppt, Chunks fehlen noch) → `processing`
→ `review` → `scheduled` oder `published`. Fehler führen nach `failed` mit Meldung im letzten Job; von dort lässt sich
die Verarbeitung neu starten. Es gibt höchstens eine Folge im Zustand `recording` oder `uploading`; Folgen im Zustand `draft` sind Vorbereitungen,
davon beliebig viele (siehe „Mehrere Vorbereitungen“).

## 6. WebSocket `/ws/p?token=…`

JSON-Nachrichten mit Feld `type`. Ein Raum für das ganze Studio.

Client → Server:

| type | Felder | Wer |
|---|---|---|
| `sync` | `t0` | alle |
| `state` | `mic` (bool, nicht stumm), `cam` (bool), `headphones` (bool), `device` (`desktop` oder `mobile`) | alle |
| `rtc` | `to`, `data` (`{description}` oder `{candidate}`) | alle |
| `meter` | `peak_db`, `rms_db`, `clip` (höchstens 5 pro Sekunde) | alle |
| `rec.start`, `rec.pause`, `rec.resume`, `rec.stop` | – | Host |
| `sound.play` | `sound_id` | Host |
| `sound.stop` | `event_id`, `fade_ms` (Standard: `fade_out_ms` des Sounds) | Host |
| `marker` | `label`, `kind` (`note` Standard, oder `chapter`) | Host |

Server → Client:

| type | Felder |
|---|---|
| `welcome` | `self` (Teilnehmer), `room` (Raumzustand), `server_ms` |
| `sync` | `t0`, `server_ms` |
| `room` | vollständiger Raumzustand, bei jeder Änderung außer Pegeln |
| `rtc` | `from`, `data` |
| `peer.meter` | `id`, `peak_db`, `rms_db`, `clip` |
| `rec.start` | `episode_id`, `start_at` (Serverzeit, 1500 ms in der Zukunft) |
| `rec.pause`, `rec.resume`, `rec.stop` | `at` |
| `sound.play` | `event_id`, `sound_id`, `at` (Serverzeit, 300 ms in der Zukunft), `gain_db`, `duration_ms` |
| `sound.stop` | `event_id`, `at`, `fade_ms` |
| `track.progress` | `track_id`, `participant_id`, `recorded_frames`, `uploaded_frames`, `status` |
| `error` | `code`, `message` (deutsch, für Menschen) |

Raumzustand:

```json
{
  "episode": {"id": "…", "status": "draft", "rec_start_server_ms": null, "paused": false, "pauses": [{"from": 0, "to": 0}]},
  "peers": [{"id": "…", "name": "Max", "role": "guest", "connected": true, "mic": true, "cam": false,
             "headphones": true, "device": "desktop",
             "track": {"id": "…", "recorded_frames": 0, "uploaded_frames": 0, "status": "recording"}}],
  "sounds": [{"id": "…", "label": "Intro", "kind": "intro", "duration_ms": 12000, "gain_db": 0, "fade_out_ms": 400,
              "hotkey": "1", "url": "/api/p/sounds/…/audio"}],
  "active_sounds": [{"event_id": "…", "sound_id": "…", "at": 0, "duration_ms": 12000}]
}
```

Regeln:

- WebRTC nach dem Muster „perfect negotiation“; höflich ist die Seite mit der lexikografisch größeren Teilnehmer-ID.
- `sound.play` außerhalb einer laufenden Aufnahme ist Vorhören: wird an alle verteilt, aber nicht als Ereignis gespeichert.
  Während einer Pause sind Jingles gesperrt.
- `rec.start` legt die Ereignisse an, setzt `rec_start_server_ms = start_at` und den Status `recording`.
  `rec.stop` setzt `uploading`; sind alle Spuren `complete`, startet der Job `process` von selbst. Nach 15 Minuten ohne
  Fortschritt wird mit dem Vorhandenen verarbeitet und die Lücke im Bericht vermerkt.
- Ein Soundereignis endet von selbst nach `duration_ms`. Spielt bei `rec.stop` noch ein Sound (Outro), läuft er in der
  Mischung bis zu seinem Ende weiter; die Folge endet mit dem späteren von Stop und Sound-Ende.
- Trennt der Host die Verbindung, läuft die Aufnahme weiter. Nur `rec.stop` beendet sie.

## 7. REST

Teilnehmer-API, `Authorization: Bearer <Teilnehmer-Token>`:

```
POST /api/join {invite_token, name}                → {participant_token, participant}              (öffentlich)
GET  /api/p/me                                     → participant
GET  /api/p/ice                                    → {iceServers: [...]}        STUN immer, TURN je nach PS_TURN_MODE
GET  /api/p/sounds/{id}/audio                      → playback.wav (Range, Cache 1 Tag)
POST /api/p/tracks {sample_rate, format, start_server_ms, meta}
                                                   → {track_id}                 hängt an der aktuellen Folge
PUT  /api/p/tracks/{id}/chunks/{seq}               Body roh, Header X-First-Frame, X-Frames → {received: n}
POST /api/p/tracks/{id}/clock {points: [{sample_index, server_ms}]}
POST /api/p/tracks/{id}/finish {total_frames, total_chunks} → {complete, missing: [seq]}
GET  /api/p/tracks/{id}                            → {status, received: [seq…] als Bereiche, missing}
```

- `format`: `s24le` (Standard), `s16le` oder `f32le`, immer mono. Chunks sind etwa 2 s lang (kurz, damit der Live-Begleiter zeitnah mithören kann), höchstens 8 MB.
- Chunk-Upload ist idempotent (gleiche `seq` überschreibt). Eine Spur gehört dem Teilnehmer, der sie angelegt hat.
- `meta`: `{user_agent, device_label, echo_cancellation, noise_suppression, auto_gain, headphones, context_sample_rate}`.
- Spuren dürfen schon im Zustand `draft` angelegt werden (Rekorder laufen ab Betreten). Was vor `rec.start` oder nach dem
  Ende liegt, schneidet die Pipeline weg. Der Client hebt höchstens 60 s Vorlauf auf und lädt erst ab `rec.start` hoch.

Host-API unter `/api/host/` (JSON, sofern nicht anders angegeben):

```
POST   /session                                    → {participant_token, participant}
GET    /sounds            POST /sounds (multipart: file, label, kind, …)
PATCH  /sounds/{id}       DELETE /sounds/{id}      POST /sounds/order {ids: [...]}
GET    /sounds/{id}/audio
GET    /invites           POST /invites {label, kind}        DELETE /invites/{id}
GET    /settings          PUT /settings {show?, audio?, site?, stats?}      POST /settings/cover (multipart, JPEG/PNG 1400–3000 px quadratisch)
GET    /episodes          → Liste (ohne große Felder)
GET    /episodes/{id}     → Folge mit tracks, events, jobs, report, ai, Kapitelvorschau
PATCH  /episodes/{id}     {title?, number?, season?, summary?, description_html?, explicit?, episode_type?, edl?, chapter_titles?}
DELETE /episodes/{id}     nur draft, review, failed
POST   /episodes/{id}/discard                     Aufnahme verwerfen, die Folge wird wieder Vorbereitung (siehe unten)
GET    /preparations                             Vorbereitungen und Wahl des Studios {preparations, selected, busy}
POST   /preparations {label?}                    neue Vorbereitung mit Standardplan
PUT    /studio/preparation {episode_id|null}     worauf die nächste Aufnahme aufbaut (null: ohne Vorbereitung)
POST   /episodes/{id}/process          Verarbeitung komplett neu
POST   /episodes/{id}/remix            nur Mischung und Encoding neu (nach Schnitt oder Titeländerung)
POST   /episodes/{id}/suggest          Shownotes-Vorschlag neu
POST   /episodes/{id}/publish {publish_at?}       ohne publish_at sofort
POST   /episodes/{id}/unpublish
GET    /episodes/{id}/audio/{which}    which: mix (ungeschnitten, zum Schneiden) | episode (fertig) ; Range-fähig
GET    /episodes/{id}/peaks            → peaks.json der ungeschnittenen Mischung
GET    /episodes/{id}/tracks/{track_id}/raw       → FLAC-Download der Rohspur
GET    /episodes/{id}/transcript       → transcript.json
POST   /import {feed_url}              → {job_id}             alle alten Folgen übernehmen
GET    /jobs/{id}                      → Job
GET    /feed/preview                   → feed.xml, wie er jetzt veröffentlicht würde
GET    /status                         → {storage, turn, ai, whisper, disk_free_bytes}   für die Einstellungsseite
```

Fehler immer als `{"error": {"code": "…", "message": "deutsch, für Menschen"}}` mit passendem HTTP-Status.

## 8. Studio im Browser (Paket B)

Zwei Seiten, ein gemeinsamer Kern: `/studio` (Host) und `/join/<token>` (Gast, zuerst Name und Gerätecheck).

- Gerätecheck vor dem Betreten: Mikrofon wählen, Pegelanzeige, Kopfhörer ja oder nein, Kamera optional, Testton für den
  Ausgang. Auf dem Handy ist „Kopfhörer“ anfangs aus.
- Aufnahmequalität: `getUserMedia` mit `channelCount 1`, `sampleRate 48000`. Mit Kopfhörern sind `echoCancellation`,
  `noiseSuppression` und `autoGainControl` aus (unbearbeitetes Signal). Ohne Kopfhörer ist `echoCancellation` an,
  die anderen beiden bleiben aus. Umschalten startet eine neue Spur.
- Rekorder: `AudioWorklet` liefert Float32-Blöcke, der Hauptthread packt sie zu `s24le`, schneidet Chunks von 2 s, legt
  sie zuerst in OPFS (Ausweich: IndexedDB) ab und lädt sie mit Wiederholung hoch. Nach Bestätigung wird der Chunk gelöscht.
  Beim Neuladen der Seite werden liegengebliebene Chunks nachgeladen, bevor etwas Neues beginnt.
- Live-Ton: Opus mit 96 kbit/s mono, FEC an, DTX aus. Video 640×360 bei 24 Bildern, je Gegenüber höchstens 700 kbit/s
  (zu zweit), 450 (zu dritt) oder 300 (zu viert). Wird die Leitung eng, sinkt zuerst die Auflösung; reicht es neben dem
  Ton nicht einmal für 150 kbit/s, geht das Video aus.
- Bühne (Ergänzung 11.11.2026): Sobald eine Kamera läuft, stehen die Kamerabilder groß unter der Aufnahmeleiste. Je
  verbundenem Gegenüber eine Kachel im Format 16:9 mit Namensschild („stumm“ steht dort mit), ohne Kamera zeigt die Kachel
  den Namen. Ist die Bühne schmaler als 720 px (Host neben dem Sendeplan, Handy), liegt das eigene Bild klein in der
  Ecke; ist sie breiter und sind es zwei Personen, stehen beide Bilder gleich groß nebeneinander. Das eigene Bild ist
  gespiegelt. Ohne Kamera bleibt die Bühne verborgen. Die Kacheln bleiben im Dokument stehen und werden nur angepasst,
  damit laufende Videos bei Raum-Updates nicht neu starten. Prüfung: `tests/e2e/stage.mjs`.
- Jingles: Dateien beim Betreten vorladen und dekodieren, zur vereinbarten Serverzeit über Web Audio starten. Der Host
  hat das Soundboard mit Tasten 1–9, Restzeit je laufendem Sound und „Stopp mit Ausblenden“.
- Sendemodus (Ergänzung 11.11.2026, nur Host, solange die Aufnahme läuft, ab 1100 × 600 px; Tablet hochkant siehe unten): Alles passt auf einen
  Bildschirm, die Seite scrollt nicht. Kopf und Navigation der Seite sind ausgeblendet. Kopfzeile: Laufzeit, die
  Abschnitte des Sendeplans als Leiste (aktueller mit „seit m:ss“), Pause und Beenden. Darunter drei Spalten: links
  Bühne, Pegelzeile und Soundboard; in der Mitte der Begleiter mit festem Platz für Karten; rechts das Dossier des
  aktuellen Abschnitts. Die Pegelzeile zeigt je Teilnehmer einen Pegel und eine Zustandszeile, die nur bei Problemen
  laut wird (nicht verbunden, stumm, Verbindung schwach, übersteuert, Upload hängt mehr als 30 s nach);
  Teilnehmerliste und Gerätewahl liegen im Dialog „Technik“. Karten: Die neueste steht zwei Minuten groß, ältere
  klein, nach zehn Minuten fallen sie heraus; nie ist eine Karte angeschnitten, Angepinntes steht darunter. Dossier
  ohne Klappen (Notiz, Fragen, Fakten und Zahlen, Aufhänger; Lage, Positionen, Gegenargumente und Begriffe hinter
  einer Zeile am Ende). Was im Gespräch gefallen ist, wird durchgestrichen und trägt „gestellt“, „genannt“ oder
  „gebracht“: Der Begleiter meldet es mit `assist.said` (Abschnitt 15), ein Klick auf den Punkt schaltet von Hand um.
  Im Soundboard ist der Jingle markiert, dessen `segment_kind` zum nächsten unbesuchten Abschnitt passt. Nach dem
  Beenden steht alles wieder in der gewohnten Ansicht. Sonst bleibt es unter 1100 px Breite bei der gewohnten Ansicht.
  Prüfung: `tests/e2e/live.mjs`.
- Tasten beim Host (nicht in Eingabefeldern oder offenen Dialogen): `1` bis `9` Jingle, `←` und `→` Abschnitt
  wechseln, `I` Impuls jetzt, `/` ins Nachschlagen-Feld, `M` Merkpunkt, `K` Kapitel (beide nur während der Aufnahme). Jede Taste steht als
  Tastenkappe neben ihrer Funktion.
- Host-Leiste: Aufnahme starten, Pause, Weiter, Stopp (mit Rückfrage), Laufzeit; im Soundboard-Bereich „Merkpunkt setzen“ und „Kapitel setzen“ mit gemeinsamem Feld für die Bezeichnung. Je Teilnehmer: Pegel
  (vom Client gemessen, nicht aus dem WebRTC-Ton), Übersteuerungswarnung, Kopfhörer-Status, Upload-Stand, Verbindungsgüte.
- Nach Stopp: deutlicher Hinweis „Upload läuft, Fenster offen lassen“ mit Fortschritt, danach „Fertig, Fenster kann zu“.
- Merkpunkt und Kapitel (Ergänzung 11.11.2026): Zwei Arten von Marken. Ein **Merkpunkt** („hier war ein Versprecher“) ist
  nur für den Schnitt und kommt nie in die fertige Folge; ein **Kapitel** wird in der Folge zum Kapitel. Ereignis
  `kind: "marker"` mit `payload {label, kind}`: `payload.kind == "note"` ist ein Merkpunkt, alles andere (auch alte
  Ereignisse ohne `payload.kind`) ein Kapitel. Ohne `kind` in der Nachricht gilt `note`; ein unbekannter Wert wird zu
  `note` (der Zeitpunkt geht nicht verloren, und ein Merkpunkt ist die folgenlose Art). Die Rückmeldung nennt Art und
  Zeit („Merkpunkt bei 12:34“, „Kapitel bei 12:34“).
- Hörprobe im Gerätecheck (Host und Gast): fünf Sekunden vom gewählten Mikrofon mit den Einstellungen der Aufnahme
  (Kopfhörer, Echounterdrückung), danach „Abspielen“ und „Noch einmal“. Rein lokal per MediaRecorder, nichts geht an
  den Server. Mit dem Testsignal geht sie ebenso; ohne MediaRecorder ist sie ausgeblendet. Ein Satz nennt, worauf zu
  achten ist (Hall, Rauschen, Abstand). Prüfung: `tests/e2e/probe.mjs`.
- Ausgang (Ergänzung 12.11.2026): Im Gerätecheck und im Studio (Feld „Ausgang“ bei „Eigenes Gerät“, im Sendemodus im
  Fenster „Technik“) lässt sich das Ausgabegerät wählen, „Systemstandard“ zuerst. Stimmen der anderen und die Hörprobe
  laufen über `<audio>` mit `HTMLMediaElement.setSinkId` (Chrome, Edge, Firefox), Jingles und Testton über
  `AudioContext.setSinkId` (Chrome, Edge; in Firefox bleiben sie beim Standardausgang, ein Satz sagt das). Firefox zeigt
  Ausgänge erst nach seiner eigenen Auswahl: Knopf „Anderen Ausgang wählen“ (`selectAudioOutput`). Ohne `setSinkId`
  (Safari, iPhone) ist das Feld verborgen, ein Satz verweist auf den Systemausgang. Der AudioContext wechselt nie während
  einer Aufnahme, weil der Rekorder an ihm hängt (ein Gerätewechsel kann eine Lücke reißen); der Wechsel folgt nach dem
  Ende, ein Hinweis sagt das. Gespeichert werden ID und Name des Geräts (`ps.prefs`), weil sich Geräte-IDs ändern können
  (Safari, gelöschte Website-Daten); fehlt die ID, wird das Gerät über den Namen wiedergefunden, sonst gilt der
  Systemstandard. Code: `app/static/js/studio/output.js`. Prüfung: `tests/e2e/output.mjs`.
- Handy und Tablet: Screen Wake Lock während der Aufnahme. Der Gerätecheck rät vorab, das Studio im Vordergrund zu
  lassen, den Bildschirm nicht zu sperren und ein Ladekabel zu nutzen. Kommt die Seite aus dem Hintergrund zurück,
  steht bei Host und Gast eine Warnung mit derselben Anweisung; auf Handy und Tablet bleibt sie stehen, bis sie
  geschlossen wird. Layout ab 360 px.
- Home-Bildschirm (iPhone, iPad, Android): `/app.webmanifest` (hinter der Host-Anmeldung, Name und Kurzname aus dem
  Titel der Show, `display: standalone`, `start_url: /studio`, Symbole 192 und 512 px) und `apple-touch-icon`
  (180 px) unter `app/static/icons/` (erzeugt von `scripts/make_icons.py`). Eingebunden nur auf Host-Seiten
  (`crossorigin="use-credentials"`); die Gastseite `/join/…` und die Anmeldung bekommen kein Manifest.
- Sendemodus auf dem Tablet hochkant (760 bis 1099 px Breite, mindestens 900 px Höhe, z. B. iPad 820 × 1180):
  Kopfzeile, darunter zwei Spalten (links Gespräch und Soundboard, rechts Karten), darunter über die volle Breite das
  Dossier; die Seite scrollt nicht. Darunter bleibt es bei der gewohnten Ansicht.
- Browser: aktuelle Chrome, Edge, Firefox, Safari (macOS und iOS). Fehlt etwas Nötiges, sagt die Seite klar, was.

## 9. Audio-Pipeline (Paket C)

Reines Paket, arbeitet auf einem **Manifest** und Verzeichnissen:

```json
{
  "episode_id": "…", "work_dir": "…", "out_dir": "…",
  "rec_start_server_ms": 0, "rec_stop_server_ms": 0,
  "tracks": [{"id": "…", "participant_id": "…", "participant_name": "Max", "dir": "…/tracks/<id>",
              "sample_rate": 48000, "format": "s24le", "start_server_ms": 0, "total_frames": 0,
              "chunks": [{"seq": 0, "first_frame": 0, "frames": 192000}],
              "clock_points": [{"sample_index": 0, "server_ms": 0}],
              "meta": {"headphones": true, "echo_cancellation": false}}],
  "sounds": [{"event_id": "…", "sound_id": "…", "path": "…/original.wav", "at_ms": 0, "stop_ms": null,
              "fade_ms": 400, "gain_db": 0, "kind": "intro", "chapter_title": "Intro"}],
  "markers": [{"at_ms": 0, "label": "…"}],
  "edl": {"trim_start_ms": 0, "trim_end_ms": null, "cuts": [{"from_ms": 0, "to_ms": 0}]},
  "audio": {"target_lufs": -16, "true_peak_db": -1.0, "denoise": "normal", "crossgate": true, "mp3_kbps": 160},
  "tags": {"title": "…", "artist": "…", "album": "…", "track": 12, "year": 2026, "cover_path": null}
}
```

Zeiten im Manifest sind Sitzungszeit in Millisekunden (`at_ms`, `stop_ms`, EDL), außer den ausdrücklich als Serverzeit
benannten Feldern.

CLI (jede Stufe schreibt Fortschritt als JSON-Zeilen `{"progress": 0.42, "message": "…"}` auf stdout):

```
python -m pipeline process    manifest.json   Stufe 1 und 2
python -m pipeline stage1     manifest.json   zusammensetzen, ausrichten, verbessern → work/stems/<participant_id>.flac
python -m pipeline stage2     manifest.json   mischen, schneiden, mastern, kodieren → out/
python -m pipeline transcribe manifest.json   → out/transcript.json, out/transcript.vtt
python -m pipeline sound      <eingabe> <zielordner>   Jingle prüfen → playback.wav, meta.json {duration_ms, lufs, peak_db}
```

Stufe 1, je Spur:

1. Chunks zu einer Datei zusammensetzen, Lücken mit Stille füllen und im Bericht nennen, als `raw.flac` ablegen.
2. Auf die Sitzungszeit legen: Startversatz und Drift aus den Uhrenpunkten (robuste Gerade), hochwertiges Resampling
   auf 48 kHz. Mehrere Spuren einer Person zu einem Stem zusammenfügen. Vor `rec.start` und nach dem Ende abschneiden.
3. Verbessern: Hochpass 80 Hz, Rauschunterdrückung mit `deep-filter` (Stufen `off`, `leicht`, `normal`, `stark` über die
   Dämpfungsgrenze), De-Esser, sanfte Kompression, Pegelangleich je Sprecher (gleiche Sprach-Lautheit für alle Stems,
   gemessen nur über Sprachanteile), Begrenzer.
4. Crossgate (wenn `audio.crossgate`): Spricht nur eine Person, wird das Übersprechen in den anderen Stems abgesenkt
   (höchstens 15 dB, weiche Flanken, kein hörbares Pumpen). Das ist der Schutz für Aufnahmen ohne Kopfhörer.

Stufe 2:

5. Mischen: Stems mittig, Jingles in Stereo an ihrer Sitzungszeit, Jingles auf einheitliche Lautheit relativ zur Sprache
   (Musik liegt 2 LU unter der Sprache, `gain_db` des Sounds kommt dazu), `stop_ms` mit Ausblenden über `fade_ms`.
6. `out/mix_uncut.m4a` (AAC 96 kbit/s, ganze Sitzung) und `out/peaks.json` (`{sample_rate, samples_per_peak, peaks: [min, max, …]}`,
   etwa 50 Werte je Sekunde, 8 Bit) für die Schnittansicht.
7. Schneiden nach EDL mit 15 ms Überblendung an jeder Schnittkante.
8. Mastern: Lautheit zweistufig auf `target_lufs` (Standard −16 LUFS, einstellbar), True Peak höchstens `true_peak_db`.
9. `out/episode.mp3`: 44,1 kHz Stereo, CBR `mp3_kbps`, ID3v2.3 mit Titel, Künstler, Album, Jahr, Cover und Kapiteln (CHAP/CTOC).
10. `out/chapters.json` im Podcasting-2.0-Format, Zeiten in Folgenzeit (nach dem Schnitt). Kapitel, die in einen
    herausgeschnittenen Bereich fallen, rücken an die Schnittkante; doppelte entfallen.
11. `work/report.json`: Dauer, Lautheit vorher und nachher je Spur, angewandte Verstärkung, Drift in ppm, Startversatz,
    Lücken, Übersteuerungen, Warnungen in deutschem Klartext.

`pipeline.timeline` stellt bereit: `session_to_episode(ms, edl) -> ms | None`, `episode_to_session(ms, edl) -> ms`,
`chapters(manifest) -> [{startTime, title}]`. Der Server nutzt genau diese Funktionen.

Transkript: faster-whisper je Stem (dadurch ist der Sprecher bekannt), Sprache Deutsch, VAD an, Segmente nach Zeit
zusammengeführt. `transcript.json`: `{segments: [{start_ms, end_ms, speaker, text}]}` in Sitzungszeit. `transcript.vtt`
in Folgenzeit mit Sprecher (`<v Max>`).

Ressourcen: Ausgelegt auf einen kleinen Server: sechs schwache Kerne (i5-8400T) und wenig freier Speicher. Höchstens vier Threads, nie mehr als
ein Job gleichzeitig, Spitzenbedarf unter 5 GB, lange Dateien blockweise verarbeiten.

### Festlegungen der Umsetzung (Paket C, 11.11.2026)

- Fehler: Exit-Code 1, die **letzte Zeile auf stderr** ist die Meldung für Menschen (deutsch). Warnungen erscheinen
  zusätzlich als `Warnung: …` auf stderr, maßgeblich sind die in `report.json`. Exit-Code 2 bei falschem Aufruf.
- Schritt 1 legt neben `raw.flac` ein `raw.json` (abgedeckte Bereiche, Lücken, Übersteuerungen) ab und löscht die
  Chunk-Dateien erst nach geprüftem Schreiben. Ein erneuter Lauf nimmt `raw.flac` als Grundlage und legt später
  eingetroffene Chunks darüber; der Server muss nichts löschen.
- Ausrichten: robuste Gerade per RANSAC über Punktpaare (Toleranz 12 ms, Steigung höchstens 1000 ppm), danach kleinste
  Quadrate; die Steigung wird bei kurzen Spuren zur Nennrate hin geschrumpft (Vorwissen ±100 ppm). Ohne Uhrenpunkte gilt
  `start_server_ms` mit Nennrate (Warnung im Bericht). Resampling mit soxr (VHQ, beliebiges Verhältnis), daher
  `soxr` in `requirements.txt`.
- Neben jedem Stem liegt `work/stems/<participant_id>.speech.json` `{hop_ms: 1, speech_ms: [[von, bis], …]}`: Bereiche
  eigener Sprache (ohne Übersprechen). `transcribe` nutzt sie, damit Übersprechen nicht doppelt im Transkript landet,
  und transkribiert je Redebeitrag.
- Sprach-Lautheit der Stems: −20 LUFS (nur über eigene Sprache gemessen). Jingles werden relativ dazu gemischt.
- `meta.json` von `sound`: `peak_db` ist der True Peak (dBTP) der Wiedergabedatei; Dateien über 30 Minuten oder stumme
  Dateien werden abgelehnt.
- `pipeline.timeline.chapters(manifest)` liefert `[{startTime, title, event_id}]`, `startTime` in Sekunden
  (Podcasting 2.0). Marker (`markers`) werden wie im Server zu Kapiteln; bei gleicher Startzeit bleibt das zuerst
  geklickte. `session_to_episode` bildet das Ende des letzten behaltenen Bereichs auf das Ende der Folge ab; zusätzlich
  gibt es `snap_to_episode(ms, edl)` (Zeitpunkte im Schnitt rücken an die Kante) und `episode_duration_ms(edl, session_ms)`.
- `report.json` (oberste Ebene): `duration_ms` (Folge nach Schnitt), `session_duration_ms`, `tracks` (je Spur Drift,
  Startversatz, Lücken, Übersteuerungen, Lautheit vorher und nachher, Verstärkung), `stems` (Kette je Person),
  `mix`, `master` (Lautheit vorher, Verstärkung, Ergebnis, unabhängige Nachmessung mit ffmpeg `ebur128` am fertigen
  MP3 unter `verified`), `chapters`, `warnings` (Stufe 1 und 2 zusammen), Laufzeiten je Schritt.
- Umgebungsvariablen nur für die Pipeline: `PS_THREADS` (Standard und Obergrenze 4), `PS_KEEP_WORK=1` behält
  Zwischenstände (Fehlersuche).
- Laufzeit auf dem Referenzserver (Lasttest 60 min, zwei Personen, `normal`): etwa 12 Minuten, davon gut 7 für deep-filter;
  Spitzenspeicher etwa 1,1 GB.

## 10. Veröffentlichung (Paket D)

```python
class Storage(Protocol):
    def put_file(self, key: str, path: Path, content_type: str, cache_control: str | None = None) -> None
    def put_bytes(self, key: str, data: bytes, content_type: str, cache_control: str | None = None) -> None
    def delete(self, key: str) -> None
    def exists(self, key: str) -> bool
    def size(self, key: str) -> int | None
    def url(self, key: str) -> str
```

`LocalStorage(root, base_url)` und `S3Storage(endpoint, bucket, access_key, secret_key, base_url)` (boto3, R2-tauglich:
Region `auto`, große Dateien mehrteilig). `publish.storage.from_env()` wählt nach `PS_STORAGE`.

Schlüssel (nur ASCII, Apple verlangt es): `feed.xml`, `cover-<hash>.<jpg|png>` (Endung wie das Bild, Abweichung von „.jpg“),
`media/<guid-slug>/<titel-slug>-<hash>.mp3` und daneben `<titel-slug>-<hash>.chapters.json` und `<titel-slug>-<hash>.vtt`
(`publish.naming.sibling_keys(audio_key)` leitet die beiden letzten aus dem Audio-Schlüssel ab; deshalb speichert
`episodes` nur `audio_key`, und `FeedEpisode.from_row` setzt bei `source = recorded` Kapitel und Transkript daraus).
Mediendateien sind unveränderlich (`Cache-Control: public, max-age=31536000, immutable`); ändert sich eine Folge, bekommt
die Datei einen neuen Namen (Kurz-Hash, bei `release.upload_episode_assets` über alle drei Dateien) und die alte wird
gelöscht. `feed.xml` mit `max-age=300`. Ein Verweis auf eine fehlende Kapitel- oder Transkriptdatei entfällt im Feed.

Feed: `publish.feed.build_feed(show, episodes, *, url_for=None, feed_url=None) -> str` (`url_for` = `storage.url`; ohne sie
müssen die Schlüssel schon URLs sein), RSS 2.0 mit `itunes:`, `podcast:` (guid, chapters, transcript), `atom:link rel="self"`
und `content:encoded`. Neueste zuerst, deterministisch (kein „jetzt“). GUIDs importierter Folgen bleiben unverändert und
haben `isPermaLink="false"`. `itunes:duration` in Sekunden, `pubDate` RFC 2822. Pflichtfelder nach den Vorgaben von Spotify
und Apple (Quellen im Kopf von `publish/feed.py`); `itunes:owner` mit E-Mail (Spotify prüft darüber die Inhaberschaft).
Beschreibung als bereinigtes HTML (`publish.sanitize.clean_html`, nh3; erlaubt: p, br, a, ul, ol, li, strong, em).
Datenklassen `publish.models.Show` und `FeedEpisode` (Felder dort dokumentiert), `Show.from_dict`, `FeedEpisode.from_row`.

Import: `publish.importer.import_feed(feed_url, storage, on_progress=None, skip_guids=()) -> ImportResult` liest den
bisherigen Feed (Spotify for Creators), übernimmt Show-Daten und Cover, lädt jede Audiodatei streamend, prüft Typ und Größe
anhand des Inhalts, legt sie im Speicher ab (auch Folgenbilder, `legacy.image_key`) und liefert je Folge `{guid, title,
description_html, summary, pub_date_ms, duration_ms, number, season, episode_type, explicit, audio_key, audio_bytes,
audio_mime, legacy}`. `ImportResult`: `show` (Felder von `settings.show`), `episodes`, `skipped_guids`, `errors`
(`{guid, title, message}`; ein erneuter Lauf mit `skip_guids` holt sie nach), `warnings`, `feed_episode_count`, `.ok`,
`.to_dict()`. `on_progress(anteil, meldung)`. Wiederholbar: Folgen in `skip_guids` werden übersprungen. Ist der Feed selbst
nicht lesbar, wirft die Funktion `FeedImportError`. Fehlt die Dauer im Feed, wird sie aus der Datei gemessen.

Shownotes: `publish.shownotes.suggest(transcript, show, chapters, previous_titles) -> {titles, summary,
description_html, chapter_titles}` über die Anthropic-API (strukturierte Ausgabe, Modell `PS_AI_MODEL`). `chapters` sind
`[{event_id, start_ms | at_ms | startTime, title}]`; `chapter_titles` ist `{event_id: titel}`. Ton der Show: politisch,
sarkastisch, humorvoll; die Vorschläge dürfen das aufnehmen, ohne zu erfinden, was nicht gesagt wurde. Ohne Schlüssel oder
bei Dienstfehlern: `ShownotesUnavailable` mit deutscher Meldung.

Hilfen für den Server: `publish.release.upload_episode_assets(storage, episode, mp3, chapters, vtt) -> {audio_key,
audio_bytes, audio_mime, chapters_key, transcript_key}`, `remove_episode_assets`, `upload_cover` (prüft 1400 bis 3000 px
quadratisch), `render_feed` (Vorschau) und `publish_feed(storage, show, episodes) -> feed_url`. Prüfung für die
Einstellungsseite: `publish.validate.validate_feed(xml, check_network=False) -> list[str]` („Fehler: …“, „Hinweis: …“).

Der Server ruft beim Veröffentlichen: Dateien hochladen → Folge als `published` (oder `scheduled`) markieren → Feed neu
bauen und hochladen. Geplante Folgen prüft ein Zeitgeber jede Minute. `unpublish` nimmt die Folge aus dem Feed und
löscht ihre Dateien.

Aufnahme verwerfen und neu aufnehmen (Ergänzung 11.11.2026): `POST /episodes/{id}/discard` geht bei `uploading`,
`processing`, `review` und `failed` (nicht während `recording`, nicht bei importierten Folgen). Wartende Jobs der Folge
werden gestrichen, ein laufender Pipeline-Unterprozess beendet (`Runner.cancel_episode`, ohne Fehleralarm), dann
verschwinden Spuren, Ereignisse, Jobs, Karten, Dateien sowie Schnitt, Angaben und Bericht. Die Folge selbst wird wieder
zur Vorbereitung (`draft`) mit Sendeplan, Dossiers, Notizen und Arbeitstitel; Besuchsmarken werden zurückgesetzt, und
das Studio wählt sie für die nächste Aufnahme. Antwort `{discarded, episode_id}`. Der Raum bekommt
`{type: "rec.discard", episode_id}`: Die Rekorder lassen ihre Spuren dieser Folge fallen und das Studio ist wieder
„Bereit“. Bedienung: Knopf „Aufnahme verwerfen“ beim Host nach dem Beenden (im Studio) und Abschnitt „Neu aufnehmen“
auf der Folgenseite. Prüfung: `tests/e2e/discard.mjs`.

Mehrere Vorbereitungen (Ergänzung 11.11.2026): Eine Vorbereitung ist eine Folge im Zustand `draft` mit Sendeplan und
Arbeitstitel (`episodes.label`, Migration 3); davon kann es beliebig viele geben. Die Seite „Vorbereitung“ wählt,
legt an, benennt und löscht sie (`?id=` merkt sich die offene). Das Studio legt vor dem Start fest, worauf die
Aufnahme aufbaut („Aufbauen auf“): eine Vorbereitung oder „Ohne Vorbereitung“. Die Wahl liegt in der Einstellung
`studio = {episode_id}` (`null` = ohne); fehlt sie, gilt die neueste Vorbereitung. `core.current_episode` liefert eine
laufende Aufnahme oder einen laufenden Upload, sonst die gewählte Vorbereitung; ohne Vorbereitung gibt es vor dem
Start keine Folge, `rec.start` legt dann eine leere an. Jeder Start setzt die Wahl zurück. Der Raumzustand des Hosts
trägt `preparations` und `prep_id`. Die Folgenliste zeigt Vorbereitungen nicht, sondern nennt nur ihre Zahl.
Prüfung: `tests/e2e/preps.mjs`.

Abschnitt in eine andere Vorbereitung (Ergänzung 11.11.2026): `POST /api/host/segments/{id}/transfer {target_episode_id,
mode}` verschiebt (`move`) oder kopiert (`copy`) einen Abschnitt samt Eingabe, Notizen, Status und Dossier. Das Ziel muss
eine andere Vorbereitung (`draft`) sein (422 `same_preparation`, `not_a_preparation`). Die Kopie bekommt eine neue ID und
hängt hinten an, Verschieben hängt den Abschnitt hinten an und setzt `visited_at` zurück; die Quelle wird neu
durchnummeriert. Läuft für den Abschnitt gerade eine Recherche, antwortet der Server mit 409 `researching`. Der Raum
wird für Quelle und Ziel nachgeführt (`plan_changed`), sofern sie die Folge des Studios sind. Oberfläche: je Abschnitt der
Knopf „In andere Vorbereitung“ (nur mit mindestens zwei Vorbereitungen) mit Dialog „Verschieben“ oder „Kopieren“.

Show-Daten und Cover stehen im Feed. Deshalb bauen `PUT /settings` (mit `show`) und `POST /settings/cover` den Feed sofort
neu, sobald mindestens eine Folge veröffentlicht ist (`publishing.refresh_feed`, lädt auch ein neues Cover hoch). Scheitert
der Speicher, sind die Einstellungen gespeichert und die Antwort ist `502 storage`. Der Import legt das Cover zusätzlich
unter `$PS_DATA/cover/` ab, weil Einstellungsseite und Verarbeitung es von dort lesen.

Nummerierung (Ergänzung 11.11.2026): Einstellung `numbering = {season, prefix}`. `season`: `keep` (Standard, Staffel von
Hand), `year` (Staffel = Jahr der Veröffentlichung in Ortszeit des Servers, die Nummer läuft über alle Jahre weiter) oder
`none` (keine Staffel). `prefix`: Vorlage vor dem Titel im Feed mit den Platzhaltern `{number}` und `{season}` (andere
Platzhalter oder einzelne Klammern: 422), leer = aus; Beispiel `#{number} ` ergibt „#12 Titel“. Gespeichert bleibt der
Titel ohne Präfix; `<title>` im Feed trägt es, `<itunes:title>` nennt dann den Titel ohne (Apple zeigt die Nummer selbst),
die öffentliche Seite zeigt den Titel ohne Präfix und darunter „2026 · Folge 12“ (bei `year`) bzw. „Staffel 2, Folge 12“.
Neue Folgen bekommen beim Start der Aufnahme (`rec.start`) die nächste Nummer (`MAX(number)+1` über alle Folgen außer
Vorbereitungen) und die Staffel nach Modus; Verwerfen nimmt beides zurück. Beim Veröffentlichen setzt `year` die Staffel
auf das Jahr der Veröffentlichung, eine fehlende Nummer wird ergänzt. Bei `year` gilt die Nummer über alle Staffeln als
vergeben (409 `number_taken`). `GET /episodes/renumber` zeigt, was „Neu nummerieren“ ändern würde (alle Folgen außer
Vorbereitungen nach Datum: Veröffentlichung, Planung, Aufnahme, Anlage; ab 1; Staffel nach Modus), `POST /episodes/renumber
{count}` übernimmt es (409 `changed`, wenn die Zahl der Änderungen nicht mehr stimmt, 409 `busy` während einer Aufnahme) und
baut den Feed neu. GUIDs bleiben, Podcast-Apps laden nichts neu. Oberfläche: Einstellungen → Nummerierung, auf der
Folgenseite „Im Feed: …“ unter dem Titel. Prüfung: `tests/test_app_numbering.py`.

Öffentliche Seite (Ergänzung 11.11.2026): Bei jedem Neubau des Feeds (`publishing.rebuild_feed`) entstehen im Speicher
zusätzlich statische Seiten ohne JavaScript, CSS inline: `index.html` (Cover, Titel, Beschreibung, Links zu Podcast-Apps,
Folgenliste neueste zuerst mit Datum, Dauer, Kurzbeschreibung und `<audio controls preload="none">`) und je Folge
`folgen/<guid-slug>.html` (Titel, Datum, Player, Shownotes als bereinigtes HTML, Kapitel mit Zeiten aus `chapters.json`).
Code: `publish/site.py`, Aufruf aus `release.publish_feed(…, site=SiteOptions)`. Farben: `app/static/design/tokens-unprefixed.css`
wird zur Laufzeit gelesen, `--paper --surface --ink --slate --line --petrol` (hell aus `:root`, dunkel aus
`[data-theme="dark"]` als `prefers-color-scheme`) landen als Variablen im Stylesheet der Seite; so trägt jede Installation
ihr Thema. Sprache der Seite und `<html lang>` = `show.language`, Texte über `i18n.tr` (nicht übersetzbare Sprachen:
Englisch). Unveränderte Seiten werden nicht erneut hochgeladen (Prüfsummen in `settings.site_pages`, gebunden an die
Adresse des Speichers). Einstellungen `site = {enabled (Standard true), platforms}` (Name und http(s)-Adresse, höchstens 6;
sonst 422); Abschalten löscht `index.html` und die Folgenseiten, Zurückziehen einer Folge ihre Seite. Der Feed-`link` (Show
und Folgen): Hat der Nutzer unter „Show“ eine Webseite gespeichert (anders als `PS_BASE_URL`, das die
Einstellungsseite als Vorgabe zeigt), gilt sie. Sonst zeigt `link` auf `storage.url("index.html")` und der `link` je Folge
auf ihre Seite, solange die Seite an ist, auch wenn eine importierte Folge noch den Link ihres alten Hosters hat;
ist sie aus, gilt dieser importierte Link oder `PS_BASE_URL`. Die Einstellungen nennen die Adresse
immer mit `index.html`, weil manche S3-Speicher (R2) unter `/` nichts ausliefern.

Downloads zählen (Ergänzung 11.11.2026, Migration 5, Tabelle `downloads(day, episode_id, visitor)`, Primärschlüssel über
alle drei): Einstellung `stats = {mode, prefix}`. `off` (Standard): Enclosures zeigen auf den Speicher. `app`: Enclosure-URLs
in Feed und Seite lauten `<PS_BASE_URL>/d/<audio_key>`; die öffentliche Route `GET|HEAD /d/{key}` (`app/downloads.py`) zählt
und leitet mit 302 (`Cache-Control: no-store`) auf `<PS_PUBLIC_BASE_URL>/<key>` weiter, unbekannte oder nicht
veröffentlichte Schlüssel ergeben 404. `prefix`: vor die Speicheradresse der Audiodatei kommt das Präfix eines
Zähldienstes (`https://op3.dev/e/`), gezählt wird dort. Betroffen sind nur Audiodateien (`release.enclosure_url_fn`,
`build_feed(enclosure_url=…)`), nicht Cover, Kapitel oder Transkript. Datensparsam: keine IP gespeichert, nur
`visitor = sha256(salt + ip + user_agent + tag)[:16]` mit zufälligem Salz in `$PS_DATA/stats_salt`; je Tag und Folge zählt ein
Besucher einmal. Gezählt wird nur `GET` (nicht `HEAD`), ohne `Range` oder mit `Range` ab Byte 0, und nicht bei bekannten
Bots (einfache Liste im User-Agent). Tag = UTC. Auswertung: `GET /api/host/stats/downloads` → `{mode, totals}`,
`GET /api/host/episodes/{id}/downloads` → `{mode, total, last7, last30, days: [30 × {day, count}]}`; Folgenliste zeigt
„n Downloads“, Folgenseite Gesamt, 7 und 30 Tage und einen Balkenverlauf.

Umzug `local` → `s3` (Ergänzung 11.11.2026): `publish.transfer.transfer_tree(root, target, *, dry_run=False,
on_progress=None) -> TransferResult` lädt alles aus `$PS_DATA/public` unter gleichem Schlüssel in den Zielspeicher
(ohne `feed.xml`, unveränderlich, Inhaltstyp nach Endung), überspringt, was mit gleicher Größe schon dort liegt, und prüft
nach jedem Hochladen die Größe. `scripts/transfer_to_s3.py` ruft das im laufenden Container auf, prüft, dass jede
veröffentlichte Folge und das Cover im Ziel liegen, und baut den Feed mit den Adressen des Zielspeichers. Danach
`PS_STORAGE='s3'` setzen und den Container neu erstellen. Die Schlüssel in der Datenbank bleiben, wie sie sind.

## 11. Verwaltung im Browser (Paket E)

Rahmen `tr-shell` mit Seitenleiste: Studio, Folgen, Soundboard, Einstellungen.

- Folgen: Liste mit Status als Text, Dauer, Datum. Laufende Verarbeitung mit Fortschritt.
- Folge prüfen: Player der fertigen Folge, Wellenform der ungeschnittenen Mischung mit Kapitelmarken, Trimmen an beiden
  Enden, Bereiche markieren und herausschneiden (rückgängig machbar), „Neu mischen“. Titelvorschläge zum Anklicken,
  Shownotes-Editor (einfaches HTML), Kapitel umbenennen, Folgennummer. Klangbericht in Klartext. Veröffentlichen sofort
  oder zu Datum und Uhrzeit. Rohspuren zum Herunterladen.
- Soundboard: Dateien hochladen, Bezeichnung, Art, Kapiteltitel, Lautstärke, Ausblendzeit, Taste, Reihenfolge, Vorhören.
- Einstellungen: Show-Daten und Cover, Klang (Ziel-Lautheit, Rauschunterdrückung, Crossgate), Einladungslinks
  (Dauerlink für den Co-Host kopieren oder erneuern, Einmal-Link erzeugen), Import des alten Feeds, Systemstatus.

## 12. Gestaltung

Farben, Schriften und Grundbausteine kommen aus dem Thema in `app/static/design/` (Schnittstelle in `docs/THEME.md`).
Mitgeliefert ist ein neutrales Standard-Thema; ein eigenes Thema wird über diesen Ordner gelegt. Alle übrigen
Stylesheets nutzen nur die Variablen und Klassen des Themas.

- Hell, kein Dark Mode. Die Akzentfarbe nur für den einen Primary-Button je Ansicht, Links und die eine Kennzahl.
- Status als Text, keine Punkte, keine farbigen Flächen. „Aufnahme läuft“ ist Text in `danger` mit Laufzeit.
- Lucide-Icons als Inline-SVG, 20 px, Strich 1,5 px. Keine Emojis, keine Endlos-Animationen (Pegelanzeigen sind Daten,
  keine Animation).
- Keine externen Schriften oder CDNs, CSP mit `font-src 'self'`, Versions-Query an allen Assets.
- Texte deutsch und ohne direkte Anrede formuliert („Mikrofon wählen“, „Aufnahme starten“). Überschriften, Labels und
  Buttons ohne Schlusspunkt. Der Produktname des KI-Anbieters erscheint nirgends in der Oberfläche.
- Prüfung: Breiten-Sweep 360 bis 1920 px ohne Überlauf, Screenshots 390×844 und 1280×800, Kontrast mindestens 4,5:1.
- Ganze Seite mit unslop durchgesehen (11.11.2026, Damir Sokolovsky, @heydamir, CC BY 4.0): Studio und alle
  Verwaltungsseiten. In der Verwaltung gilt seitdem: Ein einzelner Satz als Hinweis, Leerzustand oder Rückfrage endet
  ohne Punkt (mehrere Sätze behalten ihre Punkte). „Veröffentlicht“ ist der Normalzustand und bleibt neutral, Farbe
  tragen nur „Zur Prüfung“, „Upload läuft“, „Aufnahme läuft“ und „Fehlgeschlagen“. „importiert“ steht als Text in der
  Meta-Zeile statt als Plakette. Tasten stehen als Tastenkappen (`.app-kbd` in `app.css`): die Taste eines Sounds und
  die Tasten des Schnitt-Editors. Klapp-Zeilen (`.m-details`) tragen den Pfeil aus dem Icon-Satz. Das Sound-Formular
  hat eine, zwei, drei oder sechs Spalten, nie ein einzelnes Feld in einer Zeile. Schriftgrößen ohne 13 und 15 px.
  Für das Studio: Richtung: ruhig, genau,
  sendebereit; Welt ist der Regieraum (Ablauf, Pegel, Tasten). Farbe und Größe gehören zwei Stellen, dem Kamerabild
  des Gegenübers und der einen großen Karte; alles andere bleibt neutral, die Akzentfarbe nur auf der einen Hauptaktion.
  Kennzeichen: Tastenkappen an allem, was sich per Taste auslösen lässt. Abstände im 4er-Raster, im Sendemodus
  haben alle Flächen 16 px Innenabstand, Schriftgrößen 12 (Meta), 14, 16 und 24.

## 13. Betrieb

- Ein Container, ein Prozess; Jobs laufen als Unterprozesse. Beispiel in `docker-compose.example.yml`, Variablen in
  `.env.example` und Abschnitt 2. `init: true` (Unterprozesse), ein Speicherlimit um 6 GB schützt den Server während
  der Verarbeitung.
- Daten liegen unter `PS_DATA` (SQLite `studio.db`, Rohspuren, Folgen, Modelle). Sichern heißt: dieses Verzeichnis.
- Hinter einem Reverse Proxy braucht die App WebSocket-Durchleitung und für Mikrofon und Kamera eine
  `Permissions-Policy`, die beides erlaubt.
- Benachrichtigungen über ntfy (optional): Folge fertig zur Prüfung `default` mit Klick auf die Folge, Verarbeitung
  fehlgeschlagen `high`, veröffentlicht `low`.

## 14. Vorbereitung: Sendeplan und Dossiers (Paket F, Einbau in den Server nach Paket A)

Nur für den Host. Der Sendeplan gehört zur aktuellen Folge (`draft`) und bleibt nach der Aufnahme an ihr hängen.

```
segments(id PK, episode_id, sort, kind, title, input, good_type, status, dossier JSON, notes, visited_at, updated_at)
  kind:      blitzlicht | hauptthema | good | frei
  good_type: read | watch | listen | NULL
  input:     was der Host eingibt: Stichworte, These, Links (Freitext)
  status:    empty | researching | ready | failed
  notes:     eigene Notizen des Hosts, werden nie überschrieben
sounds.segment_kind   neu, NULL | blitzlicht | hauptthema | good
```

Standardplan beim Anlegen: nach dem Sendeformat (`format.plan`), im Standard drei kurze Themen, ein Hauptthema und eine Empfehlung zum Lesen. Abschnitte lassen sich hinzufügen, löschen, umsortieren.

Dossier je Abschnitt (`assist.prep.research_async(segment, show, previous_titles, plan_context=None, on_progress=None)
-> dict`, synchron `assist.prep.research(...)`; Anthropic-API mit Websuche und Seitenabruf, strukturierte Ausgabe, jede
Tatsache mit Quelle). `segment` ist `{id, kind, title, input, good_type}`, `plan_context` die übrigen Abschnitte des
Plans (`{id, kind, title, input}`, für den Bezug zum Hauptthema), `on_progress(anteil 0..1, meldung)` darf synchron
oder asynchron sein. Fehler kommen als `assist.llm.AssistError` mit deutschem Klartext (für `status: failed`).

```json
{
  "lage": "worum es geht, in drei Sätzen",
  "fakten": [{"text": "…", "quelle_titel": "…", "quelle_url": "…", "datum": "2026-11-08"}],
  "zahlen": [{"text": "…", "quelle_url": "…"}],
  "positionen": [{"wer": "…", "position": "…", "quelle_url": "… oder leer"}],
  "gegenargumente": ["…"],
  "fragen": ["Einstiegs- und Anschlussfragen"],
  "aufhaenger": ["Pointen und Aufhänger im Ton der Show"],
  "begriffe": ["Namen und Fachbegriffe in korrekter Schreibweise (auch als Hilfe für die Mitschrift)"],
  "quellen": [{"titel": "…", "url": "…", "datum": "…"}],
  "stand": "ISO-Zeitpunkt der Recherche",
  "meta": {"model": "…", "kosten_usd": 0.37, "suchen": 5, "abrufe": 4, "verworfen": 0, "quellen_gesehen": 28, "dauer_s": 83}
}
```

Nur bei `kind: good` zusätzlich `werk: {titel, urheber, typ, url, erschienen, kurzfassung, kernaussagen, bezug} | null`
und `kandidaten: [...]`. Fehlen belegbare Fakten ganz, steht `hinweis: "Keine belegbaren Fakten gefunden."` dabei.

Quellenprüfung im Code: Jede URL in `fakten`, `zahlen`, `positionen`, `quellen`, `werk` und `kandidaten` muss aus einem
Suchergebnis oder einer abgerufenen Seite genau dieser Recherche stammen (Vergleich ohne Schema, `www.`, Fragment,
Tracking-Parameter, Schluss-Slash). Fakten, Zahlen und Kandidaten ohne solche Quelle werden verworfen, ebenso Positionen
mit erfundener URL; `quellen` wird aus den belegten Einträgen ergänzt. `meta.verworfen` zählt das. Vom Host
angegebene Links liest die Recherche zuerst.

- Tiefe nach Art: kurzes Thema (`blitzlicht`) knapp (Stoff für 2–3 Minuten: Lage, 3–5 Fakten, 2 Fragen, 1–2 Aufhänger; bis 6 Suchen,
  Effort `medium`), Hauptthema ausführlich (8–12 Fakten, 6–8 Fragen; bis 15 Suchen, Effort `high`), `good` mit Kurzfassung des Werks, Kernaussagen, Bezug zum Hauptthema und Angaben zum Verlinken.
  Bleibt die Eingabe bei `good` leer, liefert das Dossier stattdessen bis zu drei Kandidaten passend zum Hauptthema
  (`kandidaten: [{titel, urheber, typ, url, warum}]`); das ist der einzige Fall, in dem die App etwas vorschlägt.
- Ton der Show (politisch, sarkastisch, humorvoll) nur in `aufhaenger`; Fakten bleiben nüchtern und belegt. Nichts
  erfinden; was nicht belegbar ist, steht als Frage da.
- Die Recherche läuft neben dem Job-Läufer (reine Netzarbeit), mehrere Abschnitte gleichzeitig. Gemessen (11/2026):
  kurzes Thema etwa 80 s und 0,37 $, Hauptthema etwa 4 min und 1,00 $.
- Quellen und die Empfehlung aus dem Plan fließen später in den Shownotes-Vorschlag ein.

Host-API:

```
GET    /api/host/episodes/{id}/segments
POST   /api/host/episodes/{id}/segments {kind, title?, input?, good_type?}
PATCH  /api/host/segments/{id} {title?, input?, good_type?, notes?, sort?}
DELETE /api/host/segments/{id}
POST   /api/host/segments/{id}/research           startet oder wiederholt die Recherche
POST   /api/host/segments/{id}/transfer {target_episode_id, mode: "move" | "copy"}   → Abschnitt (siehe „Abschnitt in eine andere Vorbereitung“)
POST   /api/host/episodes/{id}/segments/order {ids: [...]}
POST   /api/host/episodes/draft                   liefert die aktuelle Folge im Zustand draft und legt sie bei Bedarf
                                                  samt Standardplan an → {episode}
```

`PATCH /api/host/sounds/{id}` und der Upload nehmen zusätzlich `segment_kind` an; die Sound-Liste liefert es mit.

Oberfläche: Seite „Vorbereitung“ in der Verwaltung (Abschnitte eingeben, Recherche starten, Dossier lesen, eigene
Notizen, Druckansicht) und im Studio die Spalte „Sendeplan“ (nur Host) mit dem Dossier des aktuellen Abschnitts.

Aktueller Abschnitt: Der Host wählt ihn per Klick. Zusätzlich springt er von selbst weiter, wenn ein Sound mit
`segment_kind` abgefeuert wird, und zwar auf den ersten noch nicht besuchten Abschnitt dieser Art (der Jingle der kurzen Themen
und der Ton dazwischen tragen beide `blitzlicht`). Kapitelmarken nehmen dann den Titel des Abschnitts
(„<Name der Art>: …“) statt des Sound-Namens.

## 15. Live-Begleiter (Paket F, Einbau in den Server nach Paket A)

Nur für den Host; Gäste bekommen keine dieser Nachrichten und sehen keinen Hinweis darauf in ihrer Oberfläche.

Ablauf: Die hochgeladenen Chunks jeder Spur laufen während der Aufnahme zusätzlich in `assist.live.LiveSession`.

1. Mitschrift: je Sprecher auf 16 kHz herunterrechnen, mit Pegel-VAD in Äußerungen schneiden (Abschluss nach 600 ms
   Pause, spätestens nach 12 s an der leisesten Stelle der letzten 1,5 s), an die Transkription schicken (OpenAI,
   Sprache Deutsch, `begriffe` aller Dossiers als Begriffsliste für Namen). Ergebnis: Zeilen
   `{speaker, start_ms, end_ms, text}` in Sitzungszeit, zusätzlich in `work/live_transcript.jsonl` (mit `track_id`).
   - VAD mit relativer Schwelle: Sprache liegt 10 dB (Einstieg) bzw. 6 dB (Halten) über dem Grundrauschen der Spur
     (10-%-Perzentil der letzten 8 s; springt das Geräusch, folgt die Schätzung nach einer Sekunde gleichmäßigen
     Pegels). Gleichmäßiges Geräusch ohne Pegelschwankung wird nicht transkribiert.
   - Übersprechen ohne Kopfhörer: Vor der Transkription vergleicht der Begleiter die 20-ms-Pegel mit den anderen Spuren
     derselben Zeit. Ist eine andere Spur dort mindestens 15 dB lauter, ist das Übersprechen: besteht eine Äußerung zu
     70 % daraus, wird sie gar nicht transkribiert; sonst werden solche Rahmen am Rand abgeschnitten und in der Mitte
     abgesenkt. Bleibt trotzdem eine Dublette (gleichzeitig, nahezu gleicher Text), gewinnt die Spur mit dem größeren
     Abstand zum Grundrauschen.
   - In einer Pause wird nichts transkribiert. Bekannte Halluzinationen auf Geräuschen („Untertitel …“) fallen weg.
2. Hinweise: Etwa alle 35 s, wenn seitdem genug Neues gesagt wurde (160 Zeichen) und keine Pause läuft, entsteht
   höchstens eine Karte, frühestens 30 s nach der letzten. Eingabe an das Modell: Regeln, Dossiers des Plans (als
   zwischengespeicherter Präfix), aktueller Abschnitt mit Laufzeit, Mitschrift der letzten vier Minuten, Kurzfassung
   des Früheren, bisherige Karten. Das Modell darf und soll schweigen, wenn es nichts Nützliches gibt. Die Kurzfassung
   schreibt ein eigener kleiner Aufruf fort, sobald 600 Zeichen Mitschrift aus dem Vier-Minuten-Fenster gefallen sind.
   Zusätzlich die schnelle Prüfung: Fällt im Gespräch eine Zahl (nur mit Sendeplan) oder steht eine Wissenslücke im Raum
   (Frage nach einer Tatsache oder hörbare Unsicherheit wie „wie viele …“, „weiß ich gerade nicht“, „…, glaube ich“;
   Muster `_UNSURE`, ein vorangestelltes „ich glaube, dass …“ zählt nicht) und liegt der letzte Aufruf mindestens 12 s
   zurück, prüft ein kurzer Aufruf die letzten 90 s. Er darf nur zweierlei liefern, auch kurz nach einer anderen Karte:
   eine Karte „Korrektur: …“ (Widerspruch zum Dossier) oder die Antwort auf die Wissenslücke (`art: antwort`). So kommt
   beides nach 5–10 s statt erst im nächsten Takt.
   Freier als das Dossier (Ergänzung 11.11.2026, Leitsatz: Fokus auf das Dossier, aber nie ins Leere fragen): Das
   Dossier ist der Schwerpunkt, nicht die Grenze. Antworten und Hintergrund dürfen aus gesichertem, langsam
   veränderlichem Wissen kommen (`allgemein`, in der Oberfläche als ungeprüft markiert, Größen gerundet mit Jahr). Was
   sich schnell ändert oder was das Modell nicht sicher weiß, gibt es als `nachschlagen: "<Suchbegriff>"` zurück; dann
   schlägt der Begleiter selbst im Netz nach (wie „Nachschlagen“, Karte `recherche` mit Quelle). Gedrosselt: höchstens
   eine solche Suche je 45 s, 40 je Aufnahme, kein Begriff zweimal; ohne belegten Treffer erscheint keine Karte, nur die
   Statuszeile. Eine Suche kostet rund 0,07–0,10 $ und braucht etwa 15 s.
3. „Impuls jetzt“: sofort eine Anschlussfrage oder ein Stichwort zum zuletzt Gesagten (letzte 60 s). Läuft gerade ein
   Takt-Aufruf, wartet der Impuls darauf und kennt dessen Karte.
4. „Nachschlagen“: kurze Netzrecherche (eine Suche) zu einem eingegebenen oder angeklickten Begriff, Antwort mit
   Quelle. Die `quelle_url` muss aus den Suchergebnissen stammen, sonst lautet die Karte „Nichts Belastbares gefunden:
   …“. Zeitlimit 25 s statt 20 s, weil die Suche selbst Zeit braucht.

Karte:

```json
{"id": "…", "kind": "stichwort | frage | fakt | recherche", "text": "höchstens 140 Zeichen",
 "detail": "optional, höchstens 300 Zeichen", "basis": "dossier | allgemein | netz",
 "quelle_url": null, "segment_id": "…", "server_ms": 0}
```

Abhaken: Jeder Hinweis-Aufruf im Takt liefert zusätzlich `genannt: [{abschnitt, art, nr}]` mit den nummerierten
Dossier-Punkten (`frage`, `fakt`, `zahl`, `aufhaenger`), die in der Mitschrift inhaltlich vorkamen. Die Sitzung prüft
Abschnitt, Art und Nummer, meldet jeden Punkt nur einmal (`on_said`), und der Server gibt ihn als
`{type: "assist.said", items: [{segment_id, art, nr}]}` an den Host weiter. Beim Betreten kommt der bisherige Stand in
`welcome.assist.said` mit (zusammen mit `status` und `cards`). Er liegt nur im Speicher des Servers.

Regeln für Karten: deutsch, knapp, auf einen Blick lesbar. `fakt` nur aus dem Dossier (`basis: dossier`), aus einer
Netzrecherche (`netz`, mit `quelle_url`) oder aus gesichertem Allgemeinwissen (`allgemein`, in der Oberfläche als
ungeprüft gekennzeichnet). Aktuelle Ereignisse nie aus dem Gedächtnis des Modells. Keine Wiederholung früherer Karten,
nichts, was gerade schon gesagt wurde.

Im Code durchgesetzt: `text` und `detail` hart auf 140 bzw. 300 Zeichen gekürzt (an Satz- oder Wortgrenze, mit „…“);
`recherche` nur aus „Nachschlagen“ (von Hand oder selbst ausgelöst), `netz` nur mit Quelle aus der Suche; `dossier`-Karten tragen nur eine `quelle_url`,
die im Dossier steht, `allgemein`-Karten nie eine; Karten, die einer früheren ähneln (Inhaltswörter oder Wortfolge zu
60 %), und Stichworte oder Fakten, deren Inhaltswörter zu 80 % in der letzten Minute gesagt wurden, werden verworfen
(außer Antworten auf eine Wissenslücke: Sie wiederholen zwangsläufig die Wörter der Frage).

```
cards(id PK, episode_id, server_ms, kind, text, detail, basis, quelle_url, segment_id, pinned, dismissed)
```

Schnittstelle für den Server (asyncio, im Event-Loop des Servers):

```python
live = LiveSession(rec_start_server_ms=…, on_line=cb, on_card=cb, on_status=cb, work_dir=…/work, show=show)
await live.start()
live.set_plan(segments)            # [{id, kind, title, input, notes, dossier}] – bei jeder Plan- oder Dossieränderung
live.set_current_segment(id)       # Klick oder Jingle mit segment_kind
live.feed_chunk(track_id, speaker, sample_rate, fmt, first_frame, pcm_bytes, track_start_session_ms)
live.set_paused(True | False)      # rec.pause / rec.resume
live.pulse(); live.lookup(query)   # liefern einen Task; das Ergebnis kommt über on_card
live.end_of_audio()                # rec.stop: laufende Äußerungen abschließen
await live.close()
```

`feed_chunk` blockiert nicht und darf auch aus einem anderen Thread kommen (`track_start_session_ms =
start_server_ms − rec_start_server_ms` der Spur). Callbacks dürfen synchron oder asynchron sein; Ausnahmen darin bleiben
folgenlos. Ohne `OPENAI_API_KEY` bleibt der Begleiter aus (`stt: "aus"`), ohne `ANTHROPIC_API_KEY` gibt es nur die
Mitschrift. `on_status` bekommt `{on, stt, ai, lag_ms, message}` mit `stt`/`ai` aus `aus | bereit | ok | hinkt |
fehler`; `live.summary_stats()` liefert Zähler, Verzögerung und Kosten.

WebSocket, nur Host. Client → Server: `assist.toggle {on}`, `assist.segment {segment_id}`, `assist.pulse {}`,
`assist.lookup {query}`, `assist.card {id, pinned?, dismissed?}`. Server → Host: `assist.status {on, stt, ai, lag_ms,
message}`, `assist.line {speaker, start_ms, end_ms, text}` (Mitschrift, für die Anzeige „hört mit“), `assist.card {…}`,
`assist.segment {segment_id}`.

Der Raumzustand des Hosts enthält zusätzlich `plan: [{id, kind, title, status, visited}]` und `current_segment_id`.

Grenzen: Der Begleiter darf die Aufnahme nie stören. Jeder Fehler (Netz, Kontingent, Zeitüberschreitung) führt nur zu
`assist.status` mit Klartext; Upload und Steuerung laufen unberührt weiter. Transkriptions- und Modellaufrufe mit
Zeitlimit (10 s bzw. 20 s) und ohne Warteschlangen-Stau: Kommt die Mitschrift nicht hinterher, werden alte Äußerungen
übersprungen statt aufgestaut.

Kostenrahmen je Folge (60 Minuten): Mitschrift unter 0,50 €, Hinweise unter 2 €. Gemessen (11/2026, 4,5 Minuten
zahlenreiches Zwiegespräch ohne Kopfhörer, hochgerechnet): Mitschrift 0,28 $ je Stunde, automatische Hinweise
(Takt, Zahlen-Prüfung, Kurzfassung) etwa 0,8–1,0 $ je Stunde; dazu je „Nachschlagen“ etwa 0,04–0,08 $ und je
„Impuls jetzt“ etwa 0,01 $. Verzögerung vom Ende einer Äußerung bis zur Zeile im Median 2,6 s (p90 3,4 s), davon
etwa 1 s Warten auf den 2-s-Chunk; ein Hinweis-Aufruf dauert etwa 2–3 s.

## 16. Ergänzungen aus dem Server (Paket A)

Festlegungen, die beim Bau des Servers nötig waren. Sie ergänzen die Abschnitte 3 bis 7, ohne sie zu widersprechen.

- Tabelle `jobs` hat zusätzlich `payload` (JSON, z. B. `{"feed_url": …}` für `import`). Importe haben `episode_id` NULL.
- Raumzustand ohne aktuelle Folge: `episode = {id: null, status: "idle", rec_start_server_ms: null, paused: false, pauses: []}`.
  `pauses[].to` ist `null`, solange die Pause läuft. `peers` enthält auch getrennte Teilnehmer, die in der aktuellen
  Folge eine Spur haben (`connected: false`). `active_sounds[]` hat zusätzlich `preview` (bool, Vorhören).
- Zusätzliche WebSocket-Nachrichten: Client → Server `track.recorded {track_id, recorded_frames}` (aufgenommene Frames
  der eigenen Spur, für die Fortschrittsanzeige); Server → Client `marker {label, kind, at}` (`at` in Sitzungszeit).
  `track.progress` kommt nach jedem Chunk, `finish` und `track.recorded`. Unbekannte Typen liefern `error`.
  Beim Verbindungsaufbau mit ungültigem Token oder vollem Raum kommt zuerst eine `error`-Nachricht
  (`unauthorized` bzw. `room_full`), danach schließt der Server mit Code 4401 bzw. 4429. Verbindet sich dieselbe Identität
  erneut, wird die alte Verbindung mit Code 4000 geschlossen.
- `POST /api/join` akzeptiert zusätzlich `participant_token`: gehört es zu derselben Einladung, kommt es unverändert
  zurück (gleiche Identität nach Neuladen). Ein Einmal-Link kann nur einmal zu einer neuen Identität führen
  (danach 410 `invite_used`). `GET /api/join/{invite_token}` (öffentlich) liefert `{valid, label, kind}` für die
  Beitrittsseite; ungültige Links liefern 404 `invite_unknown`, 410 `invite_revoked` oder 410 `invite_expired`.
- `POST /api/host/session` akzeptiert optional `{participant_token}` und gibt ein gültiges Host-Token unverändert zurück,
  sonst entsteht eine neue Host-Identität. Mit einem Gast-Token im `Authorization`-Header antwortet jeder Pfad unter
  `/api/host/` mit 403.
- `POST /api/p/tracks` und `rec.start` scheitern mit 409 `uploading`, solange die letzte Aufnahme noch hochlädt
  (es gibt höchstens eine aktuelle Folge). `POST /api/host/episodes/{id}/process` ist auch im Zustand `uploading`
  erlaubt und verarbeitet dann mit dem Vorhandenen. Chunks: 400 bei falscher Länge (`X-Frames` mal Bytes je Frame
  muss die Körpergröße ergeben), 413 über 8 MB, 409 `track_closed` nach dem Zusammensetzen.
- `GET /api/p/tracks/{id}` liefert zusätzlich `total_chunks`, `total_frames`; `received` sind Bereiche `[[von, bis], …]`.
- Pausen werden beim `rec.stop` als Schnitte in `episodes.edl` (Sitzungszeit) eingetragen; `PATCH` mit `edl`
  normalisiert (sortiert, überlappende Schnitte verschmolzen, leere verworfen).
- `GET /api/host/episodes` liefert je Folge zusätzlich `job` (laufender oder wartender Job mit `progress`/`message`).
  `GET /api/host/episodes/{id}` liefert zusätzlich `chapters_preview: [{event_id, kind, sound_id, session_ms,
  start_ms (Folgenzeit), title}]` (Jingles der Arten intro/outro/segment und Marker; Kapitelnamen aus `ai.chapter_titles`),
  `files: {mix, episode, peaks, transcript, chapters}` (bool) und je Spur `uploaded_frames`, `chunks`, `has_raw`.
  `PATCH` mit `chapter_titles: {event_id: titel}` schreibt nach `ai.chapter_titles` (leerer Titel setzt zurück);
  ein anschließendes Neu-Mischen übernimmt sie.
- Zusätzlich: `GET /api/host/settings/cover` (Bild), Cover-Schlüssel `cover-<hash>.<jpg|png>`, lokal unter `$PS_DATA/cover/`.
  `DELETE /api/host/sounds/{id}` blendet einen Sound nur aus (`archived`), wenn Aufnahmen darauf verweisen
  (Antwort `{deleted, archived}`). `POST /api/host/invites` mit `kind: permanent` legt einen weiteren Dauerlink an
  (Erneuern = neuen anlegen, alten widerrufen). `POST /publish` antwortet `{status: "scheduled"|"publishing", publish_at, job_id}`.
  Importierte Folgen lassen sich nicht über `unpublish` zurückziehen.
- Seiten: Platzhalter für `/`, `/studio`, `/episodes`, `/episodes/{id}`, `/sounds`, `/settings`, `/join/{token}`. Vorlagen
  (`index.html`, `studio.html`, `episodes.html`, `episode.html`, `sounds.html`, `settings.html`, `join.html` in
  `app/templates/`) bekommen `request`, `v` (Asset-Version), `base_url`, bei `join.html` `token`, bei `episode.html`
  `episode_id`. `/` leitet auf `/studio` um, solange `index.html` fehlt.
- Server-Zeit: einzige Quelle ist `app.util.now_ms()`; Tests tauschen sie mit `app.util.set_clock(fn)`.
- CSP: `style-src 'self' 'unsafe-inline'` (für Pegelbalken per `style`-Attribut), Host der `PS_PUBLIC_BASE_URL` zusätzlich in
  `img-src` und `media-src` (importierte Folgen werden von dort abgespielt), bei `http://`-Basisadresse zusätzlich `ws:`.

### Einbau von Vorbereitung und Live-Begleiter (Pakete F und A)

- Datenbank: Migration 2 (`PRAGMA user_version`) legt `segments` und `cards` an und ergänzt `sounds.segment_kind`; eine bereits
  vorhandene Entwurfsfolge bekommt dabei den Standardplan. `segments` hat zusätzlich `error` (deutsche Meldung bei `failed`).
  `cards.pinned` und `cards.dismissed` sind 0 oder 1. Beim Löschen einer Folge entfallen Abschnitte und Karten.
- Standardplan (aus `format.plan`) entsteht, sobald eine Folge angelegt wird,
  egal ob über `POST /api/host/episodes/draft` oder über das Studio (erste Spur, `rec.start`). Gelöschte Abschnitte kommen nicht
  zurück. `POST /episodes/draft` liefert `{episode}` (Felder wie in der Folgenliste); läuft schon eine Aufnahme oder ein Upload,
  kommt diese Folge zurück, es entsteht nie eine zweite.
- `GET /episodes/{id}/segments` liefert eine Liste (nach `sort`), je Abschnitt `{id, episode_id, sort, kind, title, input,
  good_type, status, dossier (Objekt oder null), notes, visited_at, updated_at, error}`, bei laufender Recherche zusätzlich
  `progress` und `message`. `good_type` gilt nur bei `kind: good` (sonst `null`; Standard `read`). `PATCH` mit `sort` setzt den
  Abschnitt an diese Stelle; `POST …/segments/order` mit einer Teilliste hängt die nicht genannten in alter Reihenfolge an.
- Recherche: `POST /segments/{id}/research` antwortet 202 mit dem Abschnitt (Status `researching`); läuft schon eine, kommt
  derselbe Zustand zurück, ohne Doppelstart. Ohne `ANTHROPIC_API_KEY` 503 `no_ai`, ohne Thema und Eingabe (außer bei `good`) 422
  `empty_segment`. Höchstens vier Recherchen laufen zugleich, weitere warten mit Status `researching`. Beim Serverstart und beim
  Herunterfahren werden hängende Recherchen `failed` mit Hinweis. Eine Recherche schreibt nur Status, Dossier und Fehler, nie
  Titel, Eingabe oder Notizen.
- Sendeplan-Art und Kapitel: Der Titel eines Sendeplan-Abschnitts ersetzt den Kapitelnamen nur bei Sounds der Art `segment`
  (Intro und Outro bleiben „Intro“ und „Outro“, ein Zwischenton (`sting`) setzt nie eine Kapitelmarke, auch mit `segment_kind`).
  Die Abschnitts-ID steht im `payload` des Ereignisses `sound.play` (`{"segment_id": …}`). Ein vom Host gesetzter Kapiteltitel
  (`ai.chapter_titles`) gewinnt; der Shownotes-Vorschlag überschreibt keine Titel, die vom Abschnitt kommen. Sind alle Abschnitte
  der Art schon besucht, bleibt der Abschnitt unverändert und der Kapitelname ist der des Sounds. Vorhören springt nie.
  Der Titel für `good` richtet sich nach `good_type` (Namen aus `format.labels`).
- Raumzustand: `plan` enthält zusätzlich `good_type`. `current_segment_id` ist die Wahl des Hosts (Klick oder Jingle), sonst der
  zuletzt besuchte Abschnitt. Bei Status `uploading` enthält `episode` zusätzlich `rec_stop_server_ms`, damit ein Client, der
  `rec.stop` verpasst hat, das Ende kennt. Das `welcome` des Hosts hat zusätzlich `assist: {status, cards}` (Status wie
  `assist.status`, Karten der aktuellen Folge ohne weggeklickte, `pinned` als Wahrheitswert).
- `assist.*`-Nachrichten von Gästen beantwortet der Server mit `error` `forbidden`. `assist.segment` mit `segment_id: null`
  setzt die Wahl zurück; unbekannte Abschnitte liefern `segment_not_found`. `assist.card` ohne Treffer `card_not_found`.
- Live-Begleiter im Server: Die Einstellung heißt `settings.assist = {enabled}`, sichtbar in `GET /settings` und setzbar mit
  `PUT /settings {assist: {enabled}}` (wie `assist.toggle`). `assist.status.on` ist diese Einstellung, nicht „Sitzung läuft“. Die
  Sitzung entsteht bei `rec.start` (vor der Meldung `rec.start`), bei `assist.toggle` während der Aufnahme oder, falls der Server
  mitten in der Aufnahme neu gestartet wurde, beim nächsten Chunk. Gibt es keinen `OPENAI_API_KEY`, bleibt sie aus und
  `assist.status` erklärt es. Chunks vor `rec.start` gehen nicht an die Sitzung, ein Chunk über `rec.start` hinweg wird vorn
  gekürzt; schaltet der Host erst während der Aufnahme ein, hört der Begleiter ab da mit. Bei `rec.stop` ruft der Server sofort
  `end_of_audio()`, nimmt noch 3 s Chunks an, ruft erneut `end_of_audio()`, wartet höchstens 15 s auf die Mitschrift
  (`drain`) und schließt dann. `assist.pulse` und `assist.lookup` ohne laufende Sitzung antworten mit `assist.status` und Hinweis.
  Jede Ausnahme aus `assist/` wird abgefangen und als `assist.status` (`stt: "fehler"`, deutsche Meldung ohne Technikdetails)
  gemeldet; der Chunk-Upload antwortet unabhängig davon.
- `GET /status` hat zusätzlich `stt: {configured, model}` und `assist: {enabled, available, active}`.
- Konfiguration: `OPENAI_API_KEY`, `PS_STT_MODEL`, `PS_AI_MODEL_PREP`, `PS_AI_MODEL_LIVE` stehen in `app/config.py`; die Pakete lesen
  ihre Variablen selbst, der Server reicht keine Modellnamen durch.
- Veröffentlichen: neue Dateien hochladen → Folge und Feed aktualisieren → erst dann alte Dateien löschen
  (`release.upload_episode_assets(..., delete_old=False)` liefert `obsolete_keys`, `release.delete_keys` räumt auf). `unpublish`
  nimmt die Folge zuerst aus dem Feed und löscht dann die Dateien. Der Shownotes-Vorschlag bekommt den Sendeplan über
  `shownotes.suggest(..., plan=[{kind, title, input, good_type, dossier}])`; Links aus den Quellen und dem Werk des Plans
  bleiben in den Shownotes erlaubt.
- `/static/js/` und `/static/css/` kommen mit `Cache-Control: no-cache` (der Browser prüft per ETag), `/static/design/` unverändert.
- `dev.sh serve` reicht `PS_JOBS` (Standard 1) und, wenn gesetzt, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `PS_STT_MODEL`, `PS_AI_MODEL`,
  `PS_AI_MODEL_PREP`, `PS_AI_MODEL_LIVE` aus der Umgebung durch. `dev.sh serve-e2e [port]` ist `serve` ohne `--reload` und mit
  `PS_JOBS` Standard 0 für Browser-Tests.

### Merkpunkte, Archiv und Kosten (Nachbearbeitung und Betrieb, 11.11.2026)

- Merkpunkte statt Kapitel: Ein Ereignis `marker` mit `payload.kind == "note"` ist ein Merkpunkt (Taste `M` im Studio), jedes
  andere (`payload.kind == "chapter"`, Taste `K`, und alte Ereignisse ohne `kind`) ist ein Kapitel. Kapitelvorschau, `chapters.json`
  und Feed enthalten nur Kapitel. Das Manifest hat `markers` (nur Kapitel) und `notes: [{at_ms, label}]` (Sitzungszeit; die
  Pipeline ignoriert sie, auch ein verirrter Merkpunkt in `markers` setzt kein Kapitel). `GET /episodes/{id}` liefert
  `notes: [{event_id, session_ms, label}]` (leere Bezeichnung: „Merkpunkt“). Der Schnitt-Editor zeigt sie als Fähnchen in
  Sand und als Liste „Merkpunkte“; ein Klick springt 2 Sekunden vor den Punkt.
- Archiv: Mit `PS_ARCHIVE_DIR` und der Einstellung `archive = {after_days}` (Standard 14, 0 = sofort; `GET/PUT /settings` führt
  zusätzlich `archive.available`) reiht der Zeitgeber (`tick`) für veröffentlichte, selbst aufgenommene Folgen nach Ablauf der
  Frist den Job `archive` ein. Der Job kopiert das Folgenverzeichnis bis auf `out/` nach `<PS_ARCHIVE_DIR>/<guid>/`
  (zuerst in `<guid>.partial`), prüft Dateien und Größen, setzt `episodes.archived_at` und `archive_path` und löscht erst dann
  lokal. Nach einem Fehlschlag gibt es sechs Stunden Ruhe. Folgen in der Prüfung, Importe und Entwürfe werden nie archiviert;
  ohne Archiv passiert nichts. Solange `archived_at` gesetzt ist, antworten `remix`, `process` und der Rohspur-Download mit
  409 `archived`. `POST /episodes/{id}/restore` (Job `restore`) kopiert zurück, löscht `archived_at` und die Archivkopie.
  Löschen und Verwerfen entfernen auch die Archivkopie. `GET /episodes/{id}` hat `storage: {local_bytes, archived,
  archive_available}`, `GET /status` hat `archive: {configured, episodes_bytes, archive_bytes, archive_free_bytes}`.
- Kosten: Tabelle `costs(id, episode_id, segment_id, kind, usd, created_at)`, `kind` = `research | companion | shownotes`
  (Migration 4). Die Recherche schreibt nach jedem Dossier (`dossier.meta.kosten_usd`, mit `segment_id`), der Live-Begleiter
  beim Schließen der Sitzung (Mitschrift plus Hinweise aus `summary_stats()`), der Shownotes-Vorschlag nach jedem Aufruf
  (`shownotes.suggest` liefert dazu `cost_usd`, das der Server herauslöst). Anzeige: `GET /episodes/{id}` hat `costs: {total,
  by_kind}`, Abschnitte haben `cost_usd`, `GET /status` hat `costs: {current, previous}` (je `{month, total, by_kind}`, UTC).
  Beträge sind Größenordnungen aus den Preislisten, auf Cent gerundet angezeigt.
