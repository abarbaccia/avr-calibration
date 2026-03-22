#!/bin/sh
# Generate a self-signed TLS cert on first boot so the browser treats this
# origin as secure — required for getUserMedia (microphone access).
# Stored in the mounted data volume so it persists across container restarts.

CERT_DIR="${HOME}/.avr-calibration"
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
        2>/dev/null
    echo "Certificate generated at ${CERT}"
fi

exec python -m uvicorn calibrate.web:app \
    --host 0.0.0.0 --port 8000 \
    --ssl-keyfile "$KEY" \
    --ssl-certfile "$CERT"
