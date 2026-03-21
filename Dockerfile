# ── Stage 1: builder ─────────────────────────────────────────────────────────
# Builds the venv with compiled C extensions.
# On arm/v6 (Pi Zero W): numpy 1.24.x is compiled from source (no wheel for
# 1.26+) and pytta is skipped (requires llvmlite/LLVM which is impractical).
# On all other arches: full deps including pytta via normal uv sync.
FROM python:3.11-slim-bookworm AS builder

ARG TARGETARCH
ARG TARGETVARIANT

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    portaudio19-dev \
    libatlas-base-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install uv into the builder
RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
COPY calibrate/ ./calibrate/

RUN if [ "$TARGETARCH" = "arm" ] && [ "$TARGETVARIANT" = "v6" ]; then \
        echo "ARMv6: building numpy 1.24.x from source, skipping pytta" && \
        uv venv /opt/venv && \
        uv pip install "numpy>=1.24.4,<1.25" --no-binary numpy \
            --python /opt/venv/bin/python && \
        uv pip install -e ".[dev]" --python /opt/venv/bin/python; \
    else \
        echo "Building full deps including pytta (measurement extra)" && \
        uv venv /opt/venv && \
        uv sync --extra dev --extra measurement --no-editable; \
    fi

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    libportaudio2 \
    libatlas3-base \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY calibrate/ /app/calibrate/

WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
# Set HOME to /data so config.py finds ~/.avr-calibration at /data/.avr-calibration
ENV HOME=/data

EXPOSE 8000

# /data holds config.yaml and the SQLite measurement DB — mount as a volume
VOLUME ["/data"]

CMD ["python", "-m", "uvicorn", "calibrate.web:app", "--host", "0.0.0.0", "--port", "8000"]
