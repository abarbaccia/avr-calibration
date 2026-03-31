# ── Stage 1: builder ─────────────────────────────────────────────────────────
#
# arm/v7  (Pi Zero 2 W):
#   Rust wheels available; uv works. But pytta → sounddevice C extension uses
#   old distutils spawn(dry_run=) API removed in setuptools 60+ → build fails.
#   Fix: uv sync without --extra measurement (pytta not needed; web UI uses browser audio).
#
# amd64: full deps including pytta via uv sync (dev use; CLI measure command).
#
FROM python:3.11-slim-bookworm AS builder

ARG TARGETARCH
ARG TARGETVARIANT

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    portaudio19-dev \
    libatlas-base-dev \
    libffi-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Step 1: install external deps only (no project source needed).
# This layer only rebuilds when pyproject.toml or uv.lock changes — not on every
# calibrate/ source edit. On the Pi, this is the slow extraction step; keep it stable.
COPY pyproject.toml uv.lock ./

RUN if [ "$TARGETARCH" = "arm" ] && [ "$TARGETVARIANT" = "v7" ]; then \
        echo "ARMv7 (Pi Zero 2 W): pre-install numpy from piwheels, then deps" && \
        pip install --no-cache-dir uv && \
        uv venv /opt/venv && \
        NUMPY_VER=$(grep -A1 '^name = "numpy"$' uv.lock | grep version | grep -o '[0-9][0-9.]*') && \
        VIRTUAL_ENV=/opt/venv uv pip install \
            --extra-index-url https://www.piwheels.org/simple \
            "numpy==${NUMPY_VER}" && \
        UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --no-dev --no-install-project; \
    else \
        echo "amd64: full deps including pytta (measurement extra)" && \
        pip install --no-cache-dir uv && \
        uv venv /opt/venv && \
        UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --no-dev --extra measurement --no-install-project; \
    fi

# Step 2: copy source and install the project itself.
# This layer re-runs on every calibrate/ change but is fast (no third-party downloads).
COPY calibrate/ ./calibrate/
RUN UV_PROJECT_ENVIRONMENT=/opt/venv uv sync --no-dev --no-editable

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS runtime

ARG TARGETARCH
ARG TARGETVARIANT

# minidsp-rs: talks HID to the miniDSP 2x4HD.
# Install from .deb so we get BOTH binaries:
#   minidsp   — CLI (used for one-off setup commands like routing)
#   minidspd  — HTTP REST daemon (used by MinidspClient for gain/delay/PEQ)
# The container needs --privileged (or equivalent device access) at runtime.
# Use Python urllib to avoid apt-get curl/tar (causes held-package resolver failures under QEMU).
ARG MINIDSP_VERSION=0.1.12
RUN set -e; \
    ARCH="${TARGETARCH}${TARGETVARIANT}"; \
    if [ "$ARCH" = "amd64" ]; then DEB_ARCH="amd64"; else DEB_ARCH="armhf"; fi; \
    DEB_ARCH_EXPORT="$DEB_ARCH" MINIDSP_VERSION_EXPORT="$MINIDSP_VERSION" python3 -c "import urllib.request, subprocess, os; ver=os.environ['MINIDSP_VERSION_EXPORT']; arch=os.environ['DEB_ARCH_EXPORT']; url=f'https://github.com/mrene/minidsp-rs/releases/download/v{ver}/minidsp_{ver}-1_{arch}.deb'; urllib.request.urlretrieve(url, '/tmp/minidsp.deb'); subprocess.run(['dpkg','-x','/tmp/minidsp.deb','/tmp/minidsp-pkg'],check=True); os.rename('/tmp/minidsp-pkg/usr/bin/minidsp','/usr/local/bin/minidsp'); os.rename('/tmp/minidsp-pkg/usr/bin/minidspd','/usr/local/bin/minidspd'); os.chmod('/usr/local/bin/minidsp',0o755); os.chmod('/usr/local/bin/minidspd',0o755); os.remove('/tmp/minidsp.deb')"

RUN apt-get update && apt-get install -y --no-install-recommends \
    libportaudio2 \
    libatlas3-base \
    openssl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# VERSION file is read by _read_semantic_version() to populate the UI version chip.
COPY VERSION /app/VERSION
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
# Set HOME to /data so config.py finds ~/.avr-calibration at /data/.avr-calibration
ENV HOME=/data
# Git SHA baked at build time by CI — used by /api/version to report current version.
# Local builds without --build-arg BUILD_SHA=... will show "unknown".
ARG BUILD_SHA
ENV BUILD_SHA=${BUILD_SHA:-unknown}

EXPOSE 8000

# /data holds config.yaml, TLS cert, and the SQLite measurement DB — mount as a volume
VOLUME ["/data"]

COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Generates a self-signed TLS cert on first boot (stored in /data volume),
# then starts uvicorn over HTTPS — required for browser getUserMedia access.
CMD ["/entrypoint.sh"]
