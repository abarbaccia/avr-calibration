#!/usr/bin/env bash
# avr-calibration Pi Zero W bootstrap (Docker-based)
# Run as the pi user (not root): bash install.sh
# Tested on Raspberry Pi OS Bookworm Lite (32-bit)
set -euo pipefail

IMAGE="ghcr.io/abarbaccia/avr-calibration:latest"
SERVICE_NAME="avr-calibration"
DATA_DIR="$HOME/.avr-calibration"
MINIDSP_VERSION="0.1.12"
MINIDSP_URL="https://github.com/mrene/minidsp-rs/releases/download/v${MINIDSP_VERSION}/minidsp.arm-linux-gnueabihf-rpi.tar.gz"

echo ""
echo "=== avr-calibration Pi Zero W setup ==="
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
    gnupg

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

# ── 3. minidsp-rs ─────────────────────────────────────────────────────────

echo ""
echo "--- Installing minidsp ---"
if ! command -v minidsp &>/dev/null; then
    TMP=$(mktemp -d)
    echo "Downloading minidsp v${MINIDSP_VERSION} for ARM..."
    if ! curl -fsSL "$MINIDSP_URL" -o "$TMP/minidsp.tar.gz"; then
        echo "ERROR: Failed to download minidsp from:"
        echo "  $MINIDSP_URL"
        echo "Check https://github.com/mrene/minidsp-rs/releases for available assets."
        rm -rf "$TMP"
        exit 1
    fi
    tar -xzf "$TMP/minidsp.tar.gz" -C "$TMP"
    sudo install -m 755 "$TMP/minidsp" /usr/local/bin/minidsp
    rm -rf "$TMP"
    echo "minidsp installed to /usr/local/bin/minidsp"
    # Note: the -rpi binary targets gnueabihf (ARMv7). On armv6l (Pi Zero W),
    # run 'minidsp --version' to confirm it works; if you see "Illegal instruction",
    # you'll need to cross-compile from source for ARMv6.
else
    echo "minidsp already installed: $(minidsp --version 2>/dev/null || echo 'version unknown')"
fi

# ── 4. udev rule for miniDSP USB ──────────────────────────────────────────

echo ""
echo "--- Setting up udev rule for miniDSP ---"
UDEV_RULE='SUBSYSTEM=="usb", ATTR{idVendor}=="2752", ATTR{idProduct}=="0011", MODE="0666", GROUP="plugdev"'
UDEV_FILE="/etc/udev/rules.d/99-minidsp.rules"
if [ ! -f "$UDEV_FILE" ]; then
    echo "$UDEV_RULE" | sudo tee "$UDEV_FILE" > /dev/null
    sudo udevadm control --reload-rules
    echo "udev rule installed"
else
    echo "udev rule already exists"
fi

# ── 5. Config ─────────────────────────────────────────────────────────────

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
  host: "172.17.0.1"    # Docker bridge gateway — minidspd runs on Pi host, not in container
  port: 5380             # default minidspd port

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

# ── 6. Pull Docker image ───────────────────────────────────────────────────

echo ""
echo "--- Pulling Docker image ---"
# Use sudo in case the pi user isn't yet in the docker group (first install)
sudo docker pull "$IMAGE"
echo "Image pulled: $IMAGE"

# ── 7. minidspd systemd service ───────────────────────────────────────────
#
# minidspd runs on the Pi HOST (not inside Docker) so it has exclusive,
# stable access to the miniDSP USB HID interface. The Docker container
# talks to it over HTTP via the Docker bridge gateway (172.17.0.1:5380).
#
# Passing --device=/dev/bus/usb to Docker steals the hidraw interface from
# the host, breaking minidsp probe after any container restart. Keep Docker
# off the USB bus entirely.

echo ""
echo "--- Installing minidspd systemd service ---"
MINIDSPD_FILE="/etc/systemd/system/minidspd.service"

sudo tee "$MINIDSPD_FILE" > /dev/null << 'EOF'
[Unit]
Description=miniDSP HTTP daemon
After=local-fs.target
Wants=local-fs.target

[Service]
Type=simple
ExecStart=/usr/local/bin/minidsp server 0.0.0.0:5380
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable minidspd
sudo systemctl start minidspd
echo "minidspd service enabled and started"

# ── 8. avr-calibration Docker systemd service ─────────────────────────────

echo ""
echo "--- Installing avr-calibration systemd service ---"
SYSTEMD_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

sudo tee "$SYSTEMD_FILE" > /dev/null << EOF
[Unit]
Description=AVR Calibration — web server (Docker)
After=network.target docker.service minidspd.service
Requires=docker.service minidspd.service

[Service]
Type=simple
User=$USER
ExecStartPre=-/usr/bin/docker rm -f ${SERVICE_NAME}
ExecStart=/usr/bin/docker run --rm \\
    --name ${SERVICE_NAME} \\
    -p 8000:8000 \\
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

# ── 8. Done ───────────────────────────────────────────────────────────────

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit $DATA_DIR/config.yaml (set denon.host)"
echo "  2. Plug in miniDSP via USB"
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
