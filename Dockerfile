# ── Stage 1: builder ─────────────────────────────────────────────────────────
#
# arm/v6  (Pi Zero W — current):
#   • uv has no armv6 wheel; building from source needs cargo → fails under QEMU
#   • pydantic-core (FastAPI dep) has no armv6 wheel; same cargo problem
#   • numpy 1.26+ has no armv6 wheel → compile 1.24.x from source via pip
#   • pytta → llvmlite → LLVM impractical on armv6 → skipped
#   Fix: plain pip + venv, pin pydantic v1 + fastapi 0.99.x (pure Python, no Rust)
#
# arm/v7  (Pi Zero 2 W — TODO: uncomment platforms line in docker.yml):
#   All Rust wheels available for armv7. Just falls through to the else branch.
#   No Dockerfile changes needed; uv sync handles everything.
#
# amd64: full deps including pytta via uv sync.
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

RUN if [ "$TARGETARCH" = "arm" ] && [ "$TARGETVARIANT" = "v6" ]; then \
        echo "ARMv6 (Pi Zero W): pip-only build, pydantic v1, numpy from source" && \
        python -m venv /opt/venv && \
        /opt/venv/bin/pip install --no-cache-dir "numpy>=1.24.4,<1.25" --no-binary numpy && \
        /opt/venv/bin/pip install --no-cache-dir ".[dev]" \
            "pydantic>=1.10,<2" \
            "fastapi>=0.99,<0.100"; \
    else \
        echo "Building full deps including pytta (measurement extra)" && \
        pip install --no-cache-dir uv && \
        uv venv /opt/venv && \
        uv sync --extra dev --extra measurement --no-editable; \
    fi

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS runtime

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
