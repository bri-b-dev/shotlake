# syntax=docker/dockerfile:1
# Shotlake: ein Image, ein Datenvolumen (/data), läuft ohne root.

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.13.5

# --- Build: Abhängigkeiten und Paket in eine virtuelle Umgebung --------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS build
ARG UV_VERSION
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PIP_NO_CACHE_DIR=1
RUN pip install "uv==${UV_VERSION}"
WORKDIR /app
# Zuerst nur die Abhängigkeiten (bleibt im Cache, solange pyproject.toml und uv.lock gleich sind)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra garmin
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable --extra garmin

# --- Laufzeit ---------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm
ARG VERSION=dev
LABEL org.opencontainers.image.title="Shotlake" \
      org.opencontainers.image.description="Persönliche Golf-Datenplattform" \
      org.opencontainers.image.source="https://github.com/bri-b-dev/shotlake" \
      org.opencontainers.image.version="${VERSION}"
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 shotlake \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin shotlake \
 && mkdir -p /data && chown shotlake:shotlake /data
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# Standardkonfiguration; wird beim ersten Start nach /data/config kopiert und dort gepflegt
COPY config /app/config
COPY baselines /app/baselines
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    SHOTLAKE_LOG_FORMAT=json \
    HOME=/tmp \
    SHOTLAKE_CLUB_MAP=/data/config/club_map.toml \
    SHOTLAKE_DEDUP_CONFIG=/data/config/dedup.toml
USER shotlake
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
ENTRYPOINT ["entrypoint.sh"]
# Im Container an alle Adressen binden; den Zugriff von außen regelt der Port (Compose: nur lokal)
CMD ["shotlake", "--data-dir", "/data", "serve", "--host", "0.0.0.0", "--port", "8000"]
