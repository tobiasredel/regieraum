# Regieraum – Aufnahmestudio, Audio-Pipeline, Feed.
# Laufzeit, Abhängigkeiten und Code. Für die Entwicklung lässt sich der Arbeitsordner über /srv mounten (siehe dev.sh).
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# DeepFilterNet als eigenständiges Binary (Modell eingebettet, kein PyTorch nötig); amd64 und arm64 (aarch64, glibc)
ARG DEEPFILTER_VERSION=0.5.6
ARG TARGETARCH
RUN case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      amd64) DF=x86_64-unknown-linux-musl ;; \
      arm64) DF=aarch64-unknown-linux-gnu ;; \
      *) echo "Architektur ${TARGETARCH} wird nicht unterstützt" >&2; exit 1 ;; \
    esac \
 && curl -fsSL -o /usr/local/bin/deep-filter \
      "https://github.com/Rikorose/DeepFilterNet/releases/download/v${DEEPFILTER_VERSION}/deep-filter-${DEEPFILTER_VERSION}-${DF}" \
 && chmod +x /usr/local/bin/deep-filter

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

ARG DEV=0
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN if [ "$DEV" = "1" ]; then pip install --no-cache-dir -r /tmp/requirements-dev.txt; fi

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PS_DATA=/data HF_HOME=/data/models
WORKDIR /srv
COPY . /srv
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
