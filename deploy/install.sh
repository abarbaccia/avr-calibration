#!/usr/bin/env bash
# avr-calibration Pi Zero W bootstrap (Docker-based)
# Run as the pi user (not root): bash install.sh
# Tested on Raspberry Pi OS Bookworm Lite (32-bit)
set -euo pipefail

IMAGE="ghcr.io/abarbaccia/avr-calibration:latest"
SERVICE_NAME="avr-calibration"
DATA_DIR="$HOME/.avr-calibration"

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

# ── 3. udev rules for miniDSP HID ─────────────────────────────────────────
#
# Two rules are needed:
#   a) Bind usbhid to the HID interface (interface 4, class 0x03) on hotplug.
#      Without this the hidraw device is not created after a replug.
#   b) Grant group-readable permissions to /dev/hidraw* for the miniDSP.
#
# The Docker container gets access via --device=/dev/hidraw0.
# We do NOT pass --device=/dev/bus/usb — that steals exclusive USB HID access
# from the kernel and breaks hidraw for everyone else.

echo ""
echo "--- Setting up udev rules for miniDSP HID ---"
UDEV_FILE="/etc/udev/rules.d/99-minidsp.rules"
sudo tee "$UDEV_FILE" > /dev/null << 'EOF'
# miniDSP 2x4HD — bind usbhid to HID interface on hotplug so hidraw is created
ACTION=="add", SUBSYSTEM=="usb_interface", \
    ATTRS{idVendor}=="2752", ATTRS{idProduct}=="0011", \
    ATTR{bInterfaceClass}=="03", \
    RUN+="/bin/sh -c 'echo -n %k > /sys/bus/usb/drivers/usbhid/bind'"

# Immediately unbind snd-usb-audio from miniDSP audio interfaces.
# We don't use the USB audio output (playback is via HDMI). The audio driver's
# repeated failed probes generate -71 errors that stress the DWC2 controller
# and accelerate device resets on Pi Zero W.
ACTION=="bind", SUBSYSTEM=="usb_interface", \
    ATTRS{idVendor}=="2752", ATTRS{idProduct}=="0011", \
    ATTR{bInterfaceClass}=="01", \
    ENV{DRIVER}=="snd-usb-audio", \
    RUN+="/bin/sh -c 'echo -n %k > /sys/bus/usb/drivers/snd-usb-audio/unbind 2>/dev/null || true'"

# Grant container-accessible permissions to the hidraw device
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="2752", ATTRS{idProduct}=="0011", \
    MODE="0666", GROUP="plugdev"

# Restart the avr-calibration service when miniDSP hidraw reappears
# (handles device resets — container needs to be restarted to pick up new /dev/hidraw0)
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="2752", ATTRS{idProduct}=="0011", \
    ACTION=="add", \
    RUN+="/bin/systemctl restart avr-calibration"
EOF
sudo udevadm control --reload-rules
echo "udev rules installed — replug miniDSP USB if already connected"

# ── 4. USB power boost ─────────────────────────────────────────────────────
#
# miniDSP 2x4HD draws ~500mA. The Pi Zero W USB OTG port is current-limited
# by default. max_usb_current=1 enables a GPIO switch that raises the USB
# current limit to 1.2A, preventing device resets under sustained load.

echo ""
echo "--- Enabling USB current boost (max_usb_current=1) ---"
CONFIG_FILE="/boot/firmware/config.txt"
[ -f "$CONFIG_FILE" ] || CONFIG_FILE="/boot/config.txt"
if ! grep -q "max_usb_current" "$CONFIG_FILE"; then
    sudo sed -i '/^\[all\]/a max_usb_current=1' "$CONFIG_FILE"
    echo "Added max_usb_current=1 to $CONFIG_FILE (takes effect after reboot)"
else
    echo "max_usb_current already set in $CONFIG_FILE"
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

# ── 6. Pull Docker image ───────────────────────────────────────────────────

echo ""
echo "--- Pulling Docker image ---"
# Use sudo in case the pi user isn't yet in the docker group (first install)
sudo docker pull "$IMAGE"
echo "Image pulled: $IMAGE"

# ── 7. avr-calibration Docker systemd service ─────────────────────────────
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
    -p 8000:8000 \\
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

# ── 8. Done ───────────────────────────────────────────────────────────────

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit $DATA_DIR/config.yaml (set denon.host)"
echo "  2. Plug in miniDSP via USB — hidraw0 will be created automatically"
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
