#!/usr/bin/env bash
# avr-calibration Raspberry Pi bootstrap (Docker-based)
# Run as the pi user (not root): bash install.sh
# Tested on Raspberry Pi OS Bookworm 64-bit (Pi 5)
set -euo pipefail

IMAGE="ghcr.io/abarbaccia/avr-calibration:latest"
SERVICE_NAME="avr-calibration"
DATA_DIR="$HOME/.avr-calibration"

echo ""
echo "=== avr-calibration Raspberry Pi setup ==="
echo ""

ARCH=$(uname -m)
echo "Arch: $ARCH"

# ── 1. System packages ─────────────────────────────────────────────────────

echo ""
echo "--- Installing system packages ---"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    curl \
    udev \
    ca-certificates \
    gnupg \
    inotify-tools

# ── 2. Docker ─────────────────────────────────────────────────────────────

echo ""
echo "--- Installing Docker ---"
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    echo "Docker installed. NOTE: you may need to log out and back in for"
    echo "docker group membership to take effect. The service will still start."
else
    echo "Docker already installed: $(docker --version)"
fi

# ── 3. Config ─────────────────────────────────────────────────────────────

echo ""
echo "--- Generating config ---"
mkdir -p "$DATA_DIR"
if [ ! -f "$DATA_DIR/config.yaml" ]; then
    cat > "$DATA_DIR/config.yaml" << 'EOF'
# AVR Calibration Configuration
# Run 'calibrate check' after editing to verify everything is reachable.

denon:
  host: "192.168.1.100"  # IP address of your Denon X3800H

minidsp:
  host: "localhost"       # minidspd runs inside the Docker container
  port: 5380              # default minidspd port

mic:
  name: "UMIK"           # substring matched against audio device names

measurement:
  freq_min: 20               # Hz — lower bound of calibration band
  freq_max: 200              # Hz — upper bound (bass calibration only)
  sweep_duration: 3.0        # seconds
  sample_rate: 48000         # Hz

  # Sweep playback route: hdmi (recommended) or usb
  # hdmi: Pi HDMI → Denon AUX1 → full signal chain (crossover, bass mgmt, miniDSP)
  # usb:  Pi USB  → miniDSP direct (bypasses Denon — sub only, for testing)
  playback_route: hdmi
  denon_sweep_input: "AUX1"  # Denon input your Pi HDMI is connected to
  denon_sweep_volume: -25.0  # dB master volume during sweep (restored after)
  sweep_channel: lfe          # lfe | fl | fr | c | sl | sr

  # USB route only (playback_route: usb)
  playback_device: "miniDSP" # substring matched against ALSA device names
EOF
    echo ""
    echo "IMPORTANT: Edit $DATA_DIR/config.yaml with your Denon IP:"
    echo "  denon:"
    echo "    host: \"192.168.x.x\""
else
    echo "Config already exists at $DATA_DIR/config.yaml"
fi

# ── 4. Pull Docker image ───────────────────────────────────────────────────

echo ""
echo "--- Pulling Docker image ---"
# Use sudo in case the pi user isn't yet in the docker group (first install)
sudo docker pull "$IMAGE"
echo "Image pulled: $IMAGE"

# ── 5. avr-calibration Docker systemd service ─────────────────────────────
#
# minidspd runs INSIDE the container (not on the Pi host).
# We use --privileged to give the container full device access, which avoids
# the HID driver conflict where libusb steals the interface from hid-generic,
# causing /dev/hidraw0 to disappear mid-session.
#
# --privileged is appropriate here because:
#   - This is a single-purpose appliance (home theater calibration)
#   - The Pi is on a trusted home LAN, not exposed to the internet
#   - It's the only reliable way to use HID devices in Docker containers
#
# Power note: miniDSP 2x4HD requires 12V 1A minimum. 0.8A causes a boot loop.

echo ""
echo "--- Installing avr-calibration systemd service ---"
SYSTEMD_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

sudo tee "$SYSTEMD_FILE" > /dev/null << EOF
[Unit]
Description=AVR Calibration — web server (Docker)
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=$USER
ExecStartPre=-/usr/bin/docker rm -f ${SERVICE_NAME}
ExecStart=/usr/bin/docker run --rm \\
    --name ${SERVICE_NAME} \\
    --network=host \\
    --privileged \\
    -v ${DATA_DIR}:/data/.avr-calibration \\
    ${IMAGE}
ExecStop=/usr/bin/docker stop ${SERVICE_NAME}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl start "$SERVICE_NAME"
echo "Service enabled and started"

# ── 6. Auto-update timer ──────────────────────────────────────────────────
#
# Registers avr-calibration-update.service (one-shot updater) and
# avr-calibration-update.timer (fires daily at 3am) on the host.
# The update service also watches for a trigger file written by the
# in-container POST /api/upgrade endpoint.

echo ""
echo "--- Installing auto-update service and timer ---"

UPDATE_SERVICE="/etc/systemd/system/avr-calibration-update.service"
UPDATE_TIMER="/etc/systemd/system/avr-calibration-update.timer"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "$SCRIPT_DIR/avr-calibration-update.service" ]; then
    sudo cp "$SCRIPT_DIR/avr-calibration-update.service" "$UPDATE_SERVICE"
    echo "Installed: $UPDATE_SERVICE"
else
    echo "WARNING: avr-calibration-update.service not found in $SCRIPT_DIR — skipping"
fi

if [ -f "$SCRIPT_DIR/avr-calibration-update.timer" ]; then
    sudo cp "$SCRIPT_DIR/avr-calibration-update.timer" "$UPDATE_TIMER"
    echo "Installed: $UPDATE_TIMER"
else
    echo "WARNING: avr-calibration-update.timer not found in $SCRIPT_DIR — skipping"
fi

sudo systemctl daemon-reload
if [ -f "$UPDATE_TIMER" ]; then
    sudo systemctl enable avr-calibration-update.timer
    sudo systemctl start avr-calibration-update.timer
    echo "Auto-update timer enabled and started"
    echo "  Next run: $(systemctl show avr-calibration-update.timer --property=NextElapseUSecRealtime --value 2>/dev/null || echo 'unknown')"
fi

# ── 7. Done ───────────────────────────────────────────────────────────────

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit $DATA_DIR/config.yaml (set denon.host)"
echo "  2. Plug in miniDSP and UMIK-1 via USB"
echo "  3. Run: docker exec ${SERVICE_NAME} calibrate check"
echo "  4. Service URL: https://$(hostname -I | awk '{print $1}'):8000"
echo "     (self-signed cert — click Advanced → Proceed in your browser)"
echo ""
echo "Service commands:"
echo "  sudo systemctl status $SERVICE_NAME"
echo "  sudo systemctl restart $SERVICE_NAME"
echo "  sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "Upgrade to latest image:"
echo "  sudo docker pull $IMAGE && sudo systemctl restart $SERVICE_NAME"
echo ""
