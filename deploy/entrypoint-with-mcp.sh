#!/bin/sh
# Extended entrypoint: starts minidspd + FastAPI + MCP server.
# Used by docker-compose.yml (see compose profile notes).
#
# For Pi Zero 2 W / Pi 4 deployment via hotfix.sh + systemd, the single
# existing entrypoint.sh is still used — it only starts FastAPI + minidspd.
# This script is for docker compose up on development machines.

CERT_DIR=/data/.avr-calibration
CERT="${CERT_DIR}/cert.pem"
KEY="${CERT_DIR}/key.pem"

mkdir -p "$CERT_DIR"

# Generate self-signed TLS cert for browser getUserMedia access
if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
    HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    SAN="DNS:avr-cal.local,DNS:localhost"
    [ -n "$HOST_IP" ] && SAN="${SAN},IP:${HOST_IP}"

    echo "Generating self-signed TLS certificate (SAN: ${SAN})..."
    openssl req -x509 -newkey rsa:2048 \
        -keyout "$KEY" -out "$CERT" \
        -days 3650 -nodes \
        -subj '/CN=avr-calibration' \
        -addext "subjectAltName=${SAN}" \
        2>&1 || { echo "ERROR: TLS cert generation failed" >&2; exit 1; }
    echo "Certificate generated at ${CERT}"
fi

# ── Scarlett 18i20 PCM routing fix ────────────────────────────────────────────
# Re-assert PCM 05–10 → "PCM N" so CamillaDSP's USB output reaches the line
# outs (instead of the 18i20 falling back to Analogue passthrough).
# Idempotent + silent skip when the Scarlett isn't present.
if [ -x /fix-scarlett-routing.sh ]; then
    /fix-scarlett-routing.sh || true
fi

# Start minidspd — HTTP REST daemon for miniDSP 2x4 HD
# Bind to 0.0.0.0 so both FastAPI and MCP server can reach it within the container
MINIDSPD_CONF=/tmp/minidspd.toml
cat > "$MINIDSPD_CONF" << 'TOML'
[http_server]
bind_address = "0.0.0.0:5380"
TOML

echo "Starting minidspd on 0.0.0.0:5380..."
minidspd --config "$MINIDSPD_CONF" >/tmp/minidspd.log 2>&1 &
MINIDSPD_PID=$!
sleep 2
if kill -0 "$MINIDSPD_PID" 2>/dev/null; then
    echo "minidspd started (pid $MINIDSPD_PID)"
else
    echo "WARNING: minidspd exited — DSP control unavailable"
    cat /tmp/minidspd.log >&2
fi

# Start MCP server in background
echo "Starting MCP server on 0.0.0.0:${MCP_PORT:-8765}..."
python -m calibrate.mcp_server >/tmp/mcp.log 2>&1 &
MCP_PID=$!
sleep 1
if kill -0 "$MCP_PID" 2>/dev/null; then
    echo "MCP server started (pid $MCP_PID)"
else
    echo "WARNING: MCP server exited — check /tmp/mcp.log"
    cat /tmp/mcp.log >&2
fi

# Start FastAPI (foreground — keeps container alive)
exec python -m uvicorn calibrate.web:app \
    --host 0.0.0.0 --port 8000 \
    --ssl-keyfile "$KEY" \
    --ssl-certfile "$CERT"
