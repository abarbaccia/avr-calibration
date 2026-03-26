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

COPY pyproject.toml uv.lock ./
COPY calibrate/ ./calibrate/

RUN if [ "$TARGETARCH" = "arm" ] && [ "$TARGETVARIANT" = "v7" ]; then \
        echo "ARMv7 (Pi Zero 2 W): uv build, skip measurement extra (pytta build fails on arm/v7)" && \
        pip install --no-cache-dir uv && \
        uv venv /opt/venv && \
        uv sync --extra dev --no-editable; \
    else \
        echo "amd64: full deps including pytta (measurement extra)" && \
        pip install --no-cache-dir uv && \
        uv venv /opt/venv && \
        uv sync --extra dev --extra measurement --no-editable; \
    fi

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS runtime

ARG TARGETARCH
ARG TARGETVARIANT

# minidsp-rs: talks HID to the miniDSP 2x4HD — serves HTTP on localhost:5380
# Runs inside the container (container gets --device=/dev/hidraw0 from Docker run).
# arm/v7 uses the ARMv7-hf RPi binary; amd64 uses the Linux x86_64 build.
# Use Python urllib to avoid apt-get curl/tar (causes held-package resolver failures under QEMU).
ARG MINIDSP_VERSION=0.1.12
RUN set -e; \
    ARCH="${TARGETARCH}${TARGETVARIANT}"; \
    if [ "$ARCH" = "amd64" ]; then \
        URL="https://github.com/mrene/minidsp-rs/releases/download/v${MINIDSP_VERSION}/minidsp.x86_64-unknown-linux-gnu.tar.gz"; \
    else \
        URL="https://github.com/mrene/minidsp-rs/releases/download/v${MINIDSP_VERSION}/minidsp.arm-linux-gnueabihf-rpi.tar.gz"; \
    fi; \
    URL_EXPORT="$URL" python3 -c "import urllib.request, tarfile, os; url = os.environ['URL_EXPORT']; urllib.request.urlretrieve(url, '/tmp/minidsp.tar.gz'); tf = tarfile.open('/tmp/minidsp.tar.gz'); tf.extract('minidsp', '/tmp/'); tf.close(); os.rename('/tmp/minidsp', '/usr/local/bin/minidsp'); os.chmod('/usr/local/bin/minidsp', 0o755)"

RUN apt-get update && apt-get install -y --no-install-recommends \
    libportaudio2 \
    libatlas3-base \
    openssl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
# Set HOME to /data so config.py finds ~/.avr-calibration at /data/.avr-calibration
ENV HOME=/data

EXPOSE 8000

# /data holds config.yaml, TLS cert, and the SQLite measurement DB — mount as a volume
VOLUME ["/data"]

COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Generates a self-signed TLS cert on first boot (stored in /data volume),
# then starts uvicorn over HTTPS — required for browser getUserMedia access.
CMD ["/entrypoint.sh"]
