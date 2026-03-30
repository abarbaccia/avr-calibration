#!/bin/sh
# Generate a self-signed TLS cert on first boot so the browser treats this
# origin as secure — required for getUserMedia (microphone access).
# Stored in the mounted data volume so it persists across container restarts.

# Hard-coded to match the Docker volume mount (-v host_data:/data/.avr-calibration)
# and ENV HOME=/data in the Dockerfile. Do not rely on $HOME to avoid silent breakage.
CERT_DIR=/data/.avr-calibration
CERT="${CERT_DIR}/cert.pem"
KEY="${CERT_DIR}/key.pem"

mkdir -p "$CERT_DIR"

if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
    # Detect the container's IP for the SAN so Chrome shows it correctly
    HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    SAN="DNS:avr-cal.local,DNS:localhost"
    [ -n "$HOST_IP" ] && SAN="${SAN},IP:${HOST_IP}"

    echo "Generating self-signed TLS certificate (SAN: ${SAN})..."
    openssl req -x509 -newkey rsa:2048 \
        -keyout "$KEY" -out "$CERT" \
        -days 3650 -nodes \
        -subj '/CN=avr-calibration' \
        -addext "subjectAltName=${SAN}" \
        2>&1 || { echo "ERROR: TLS cert generation failed — check openssl output above" >&2; exit 1; }
    echo "Certificate generated at ${CERT}"
fi

# ── minidspd ──────────────────────────────────────────────────────────────────
# Start the minidspd HTTP REST daemon so the web server can control the
# miniDSP 2x4HD via localhost:5380.
#
# minidspd (REST daemon) is NOT the same as `minidsp server` (deprecated TCP
# server for the mobile app). minidspd exposes the HTTP REST API that
# MinidspClient uses for gain/delay/PEQ/polarity control.
#
# Requires --privileged (or equivalent device access) on `docker run`.
# Without device access, minidspd starts but reports no devices; DSP control
# operations will fail gracefully via HTTP error in the web server.
MINIDSPD_CONF=/tmp/minidspd.toml
cat > "$MINIDSPD_CONF" << 'TOML'
[http_server]
bind_address = "127.0.0.1:5380"
TOML

echo "Starting minidspd (HTTP REST) on localhost:5380..."
minidspd --config "$MINIDSPD_CONF" >/tmp/minidspd.log 2>&1 &
MINIDSPD_PID=$!
# Give it a moment to bind the port
sleep 2
if kill -0 "$MINIDSPD_PID" 2>/dev/null; then
    echo "minidspd started (pid $MINIDSPD_PID)"
else
    echo "WARNING: minidspd exited — DSP control unavailable. Check /tmp/minidspd.log"
    cat /tmp/minidspd.log >&2
fi

exec python -m uvicorn calibrate.web:app \
    --host 0.0.0.0 --port 8000 \
    --ssl-keyfile "$KEY" \
    --ssl-certfile "$CERT"
