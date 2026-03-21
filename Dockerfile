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

COPY pyproject.toml uv.lock ./
COPY calibrate/ ./calibrate/

# arm/v6: uv has no pre-built wheel for armv6 and building it from source
# requires cargo/Rust (fails under QEMU). Use plain pip + venv instead.
# Other arches: install uv and use uv sync for a locked, reproducible build.
RUN if [ "$TARGETARCH" = "arm" ] && [ "$TARGETVARIANT" = "v6" ]; then \
        echo "ARMv6: using pip (uv has no armv6 wheel); numpy from source, pytta skipped" && \
        python -m venv /opt/venv && \
        /opt/venv/bin/pip install --no-cache-dir "numpy>=1.24.4,<1.25" --no-binary numpy && \
        /opt/venv/bin/pip install --no-cache-dir ".[dev]"; \
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
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
# Set HOME to /data so config.py finds ~/.avr-calibration at /data/.avr-calibration
ENV HOME=/data

EXPOSE 8000

# /data holds config.yaml and the SQLite measurement DB — mount as a volume
VOLUME ["/data"]

CMD ["python", "-m", "uvicorn", "calibrate.web:app", "--host", "0.0.0.0", "--port", "8000"]
