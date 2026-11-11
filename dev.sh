#!/bin/sh
# Entwicklung ohne Host-Python: Tests und lokaler Server im Wegwerf-Container.
#   ./dev.sh build              Dev-Image bauen (nach Änderung an requirements*.txt oder Dockerfile)
#   ./dev.sh test [pytest-args] pytest
#   ./dev.sh serve [port]       App auf 127.0.0.1:<port> (Standard 8197), Daten in /tmp/podcast-studio-dev-<port>;
#                               PS_JOBS (Standard 1) sowie gesetzte Schlüssel (ANTHROPIC_API_KEY, OPENAI_API_KEY) und
#                               PS_STT_MODEL, PS_AI_MODEL*, aus der Umgebung werden durchgereicht
#   ./dev.sh serve-e2e [port]   wie serve, aber ohne --reload und mit PS_JOBS (Standard 0) für Browser-Tests
#   ./dev.sh run <befehl ...>   beliebiger Befehl im Dev-Container (z. B. python -m pipeline ...)
# Jeder Port bekommt einen eigenen Container und ein eigenes Datenverzeichnis,
# damit mehrere Entwicklungs-Instanzen nebeneinander laufen können.
set -e
cd "$(dirname "$0")"
IMG=podcast-studio-dev
ENVF=""   # bewusst ohne Env-Datei mit Betriebs-Geheimnissen; Schlüssel bei Bedarf per Umgebung übergeben
LIMITS="--memory 6g --cpus 4"
case "$1" in
  build) exec docker build -q -t $IMG --build-arg DEV=1 . ;;
  test)  shift; exec docker run --rm $LIMITS -u "$(id -u):$(id -g)" -e HOME=/tmp -e PS_DATA=/tmp/ps-test -e PS_HOST_AUTH=none \
           -e PS_JOBS=0 -v "$PWD":/srv -w /srv $IMG python -m pytest -q -p no:cacheprovider "$@" ;;
  serve) P="${2:-8197}"; D="/tmp/podcast-studio-dev-$P"; mkdir -p "$D"
         exec docker run --rm --init --name "podcast-studio-dev-$P" $LIMITS -u "$(id -u):$(id -g)" -p 127.0.0.1:$P:8000 $ENVF \
           -e HOME=/tmp -e PS_DATA=/data -e PS_HOST_AUTH=none -e PS_BASE_URL=http://127.0.0.1:$P \
           -e PS_JOBS="${PS_JOBS:-1}" -e ANTHROPIC_API_KEY -e OPENAI_API_KEY -e PS_STT_MODEL -e PS_AI_MODEL -e PS_AI_MODEL_PREP -e PS_AI_MODEL_LIVE \
           -v "$D":/data -v "$PWD":/srv -w /srv $IMG \
           uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir app --reload-dir pipeline --reload-dir publish ;;
  serve-e2e) P="${2:-8197}"; D="/tmp/podcast-studio-dev-$P"; mkdir -p "$D"   # wie serve, aber ohne --reload, PS_JOBS Standard 0
         exec docker run --rm --init --name "podcast-studio-dev-$P" $LIMITS -u "$(id -u):$(id -g)" -p 127.0.0.1:$P:8000 $ENVF \
           -e HOME=/tmp -e PS_DATA=/data -e PS_HOST_AUTH=none -e PS_BASE_URL=http://127.0.0.1:$P \
           -e PS_JOBS="${PS_JOBS:-0}" -e ANTHROPIC_API_KEY -e OPENAI_API_KEY -e PS_STT_MODEL -e PS_AI_MODEL -e PS_AI_MODEL_PREP -e PS_AI_MODEL_LIVE \
           -v "$D":/data -v "$PWD":/srv -w /srv $IMG \
           uvicorn app.main:app --host 0.0.0.0 --port 8000 ;;
  run)   shift; exec docker run --rm -i $LIMITS -u "$(id -u):$(id -g)" -e HOME=/tmp -e PS_DATA="${PS_DATA:-/tmp/ps-run}" $ENVF \
           -v /tmp:/tmp -v "$PWD":/srv -w /srv $IMG "$@" ;;
  *) echo "usage: $0 build|test [args]|serve [port]|serve-e2e [port]|run <cmd ...>"; exit 1 ;;
esac
