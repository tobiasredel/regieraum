# Running Regieraum

This guide is for people who install Regieraum on their own server. For what the app does, see the
[README](../README.md); every setting is listed in `docs/SPEC.md` (German, section 2).

## Requirements

- A Linux server with Docker and the Compose plugin. The image exists for `linux/amd64` and `linux/arm64`.
- About 6 GB of RAM for audio processing and transcription (the example Compose file sets `mem_limit: 6g`).
  A recording of an hour takes about 12 minutes to process on a small six-core server, plus the transcript.
- A domain name with HTTPS. Browsers only allow microphone and camera on `https://` pages (or `localhost`), so a
  recording studio without TLS does not work for anyone but you on the same machine.
- Disk space for the data directory (see [Backups](#backups)): raw tracks are stored losslessly until you clean them up,
  and the first transcript downloads the Whisper model (`large-v3-turbo`, roughly 1.5 GB) into `PS_DATA/models`.

## Install with the ready-made image

```sh
mkdir regieraum && cd regieraum
curl -fsSLO https://raw.githubusercontent.com/tobiasredel/regieraum/main/docker-compose.example.yml
curl -fsSLO https://raw.githubusercontent.com/tobiasredel/regieraum/main/.env.example
mv docker-compose.example.yml docker-compose.yml
mv .env.example .env
mkdir data && sudo chown 1000:1000 data     # the container runs as user 1000
```

Edit `.env`: at least `PS_BASE_URL` (the public address, for example `https://studio.example.org`) and
`PS_HOST_PASSWORD` (8 characters or more). Then:

```sh
docker compose up -d
docker compose logs -f regieraum
```

The app listens on port 8000 inside the container. The example publishes it as `8000:8000`; if a reverse proxy runs on
the same host, publish it on loopback only (`127.0.0.1:8000:8000`) or put both on one Docker network and drop the
`ports:` entry.

Images are published to `ghcr.io/tobiasredel/regieraum` with the tags `latest`, `<major>.<minor>` (for example `0.1`)
and the full version (`0.1.0`). To build from a clone instead, replace `image:` by `build: .` in the Compose file.

## Reverse proxy

The proxy terminates TLS and has to

1. pass **WebSocket** connections through (`/ws/p`, used for the live conversation and the studio control channel),
2. **not** send a `Permissions-Policy` header that blocks `microphone` or `camera` (some hardened proxy presets add
   `microphone=()` by default; remove it or set `microphone=(self), camera=(self)`),
3. let large uploads through (the browsers upload lossless tracks; do not set a small request size limit).

Set `PS_BASE_URL` to the public address exactly as users type it, including `https://`. The session cookie is only
marked `Secure` when it starts with `https://`.

### Caddy

Caddy handles WebSockets and TLS certificates by itself and adds no restrictive `Permissions-Policy`:

```caddyfile
studio.example.org {
    reverse_proxy 127.0.0.1:8000
}
```

### Traefik

Traefik passes WebSockets through without extra configuration. Labels for the Compose service (the router and
entrypoint names are examples, use your own):

```yaml
services:
  regieraum:
    # ... as in docker-compose.example.yml, but without "ports:"
    networks: [proxy]
    labels:
      - traefik.enable=true
      - traefik.docker.network=proxy
      - traefik.http.routers.regieraum.rule=Host(`studio.example.org`)
      - traefik.http.routers.regieraum.entrypoints=websecure
      - traefik.http.routers.regieraum.tls.certresolver=letsencrypt
      - traefik.http.services.regieraum.loadbalancer.server.port=8000

networks:
  proxy:
    external: true
```

Do not attach a headers middleware that sets `Permissions-Policy` (or `customResponseHeaders` with it) to this router.

### Who protects what

With `PS_HOST_AUTH=password` (the default) the app protects everything itself except the paths guests need, so a plain
proxy is enough. With `PS_HOST_AUTH=proxy` the app checks nothing and your proxy has to protect every path **except**
these public ones:

`/join/`, `/api/join`, `/api/p/`, `/ws/p`, `/static/`, `/public/`, `/d/` and `/healthz`

(`/public/` only matters with `PS_STORAGE=local`, `/d/` only with download counting in the app.) Guests need these to join through an invitation link without an
account. Everything else, including `/studio`, `/settings` and `/api/host/`, belongs to the host. `PS_HOST_AUTH=none`
is for development and tests only.

## Storage for published episodes

`PS_STORAGE=local` (default) serves episodes and the feed from the app under `/public`. Simple, but every download of
an episode goes through your server.

`PS_STORAGE=s3` uploads to any S3 compatible storage (tested with Cloudflare R2). Create a bucket that is publicly
readable (for R2: a custom domain or the public development URL), an access key that can write to it, and set:

```sh
PS_STORAGE=s3
PS_PUBLIC_BASE_URL=https://podcast-files.example.org
PS_S3_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
PS_S3_BUCKET=my-show
PS_S3_ACCESS_KEY=...
PS_S3_SECRET_KEY=...
```

`PS_PUBLIC_BASE_URL` is the address under which the bucket is reachable from the internet; it ends up in the feed.
`scripts/transfer_to_s3.py` (see its header) moves already published files from local storage to a bucket.
To take over an existing show (all episodes with unchanged GUIDs), use **Settings → Import**.

## TURN relay

Participants behind strict firewalls or on mobile networks may not connect directly. Set `PS_TURN_MODE`:

```sh
# your own TURN server (for example coturn)
PS_TURN_MODE=static
PS_TURN_URLS=turn:turn.example.org:3478
PS_TURN_USERNAME=...
PS_TURN_CREDENTIAL=...

# or a Cloudflare Realtime TURN key (short-lived credentials per session)
PS_TURN_MODE=cloudflare
PS_CF_TURN_KEY_ID=...
PS_CF_TURN_API_TOKEN=...
```

The relay is only used when a direct connection fails and only for the live conversation. Recordings are always
uploaded directly to your server.

## AI keys and costs

AI is optional. Without keys the studio, the audio pipeline and the feed work as usual.

| Variable | Used for | Without it |
|---|---|---|
| `ANTHROPIC_API_KEY` | research dossiers (with web search), live hints, show notes | no dossiers, hints or show note suggestions |
| `OPENAI_API_KEY` | live transcript for the companion | the live companion stays off |
| `OPENAI_BASE_URL` | OpenAI-compatible gateway for the live transcript | `https://api.openai.com/v1` |

Models can be changed with `PS_AI_MODEL` (show notes), `PS_AI_MODEL_PREP` (dossiers), `PS_AI_MODEL_LIVE` (live hints)
and `PS_STT_MODEL` (live transcript). The transcript of the finished episode does not use an API: it runs locally with
faster-whisper (`PS_WHISPER_MODEL`, default `large-v3-turbo`).

Rough costs from real use, which depend on models and prices at the time: a dossier about 0.30 to 1 USD per segment,
the live companion about 1 to 1.30 USD per hour of recording, each automatic web lookup a few cents. Check the
providers' current prices before you rely on these numbers.

Optional notifications (episode ready, processing failed, published) go to an [ntfy](https://ntfy.sh) topic when you set
`PS_NTFY_URL` (and `PS_NTFY_USER`, `PS_NTFY_PASS` if it needs a login).

## Languages

Two languages are independent of each other.

- **Interface**: German or English. A new installation starts with `PS_LANG` (`de` or `en`, default `de`); afterwards
  the setting under Settings → Interface language wins.
- **Show**: the language of the content the app writes (dossiers, companion cards, show notes, transcript) is the
  `Language` field in the show data, for example `de` or `en`. German and English are tested. The prompts themselves
  are written in German.

## Backups

Everything that matters lives in the data directory, `PS_DATA` (`/data` in the container, `./data` in the example):
the SQLite database `studio.db`, raw tracks, episodes, jingles, the login secret `host_secret` and downloaded models.
Back up that directory. For a consistent copy, stop the container briefly (`docker compose stop`), copy, and start it
again, or take a snapshot of the volume. Models under `PS_DATA/models` can be downloaded again and may be skipped.
With `PS_STORAGE=s3` the published files live in the bucket and need their own backup.

Do not put `.env` in a public repository: it contains your password and API keys.

## Updating

```sh
docker compose pull
docker compose up -d
```

Database migrations run when the app starts, so there is nothing else to do. With `latest` you follow every release;
pin `0.1` (minor releases of that line) or `0.1.0` to update on your own schedule. Read the
[changelog](../CHANGELOG.md) first and back up `PS_DATA` before a larger update. Do not update while an episode is being
recorded or processed.

## Resources and troubleshooting

- **Memory**: processing and transcription need about 6 GB. If the container is killed during processing (exit 137), give
  it more memory or move other services away.
- **Time**: about 12 minutes per hour of recording on a six-core server for the audio pipeline, plus the transcript
  (longer on slow CPUs; the transcript runs on the CPU).
- **No microphone prompt in the browser**: the page is not served over `https://`, or a `Permissions-Policy` header blocks
  it. Check the response headers of `/studio`.
- **Guests cannot connect or the studio shows no other participants**: the WebSocket is not passed through, or a TURN
  relay is needed.
- **Health check**: `GET /healthz` answers `{"ok":true}`.
- **A setup page asks you to set the password** (HTTP 503 on protected paths): `PS_HOST_PASSWORD` is empty or shorter than
  8 characters.
