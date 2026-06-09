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
    inotify-tools \
    pipewire \
    pipewire-alsa \
    wireplumber

# Enable PipeWire / WirePlumber as user services for `pi`. PipeWire runs
# under uid 1000 (lingering enables it on boot before login).
sudo loginctl enable-linger pi 2>/dev/null || true
for svc in pipewire.socket pipewire.service wireplumber.service; do
    sudo -u pi XDG_RUNTIME_DIR=/run/user/1000 systemctl --user enable --now "$svc" 2>/dev/null || true
done

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
  # On the PipeWire stack (v0.2.0+), this is a PipeWire node name. The
  # avr-cal-sweep-link.service host service creates this null sink and
  # links it to camilladsp_capture so sweeps reach CamillaDSP -> subs.
  # Legacy direct-ALSA setups: set to a PortAudio device substring like
  # "miniDSP" instead.
  playback_device: "avr_cal_sweep"

  # Loopback reference (Scarlett input ch3 ← Denon LFE pre-out).
  # PipeWire bridges the Scarlett AUX2 capture to snd-aloop subdevice 1
  # via loopback-ref-link.service. The container reads it as hw:Loopback,1,0
  # (= hw:2,1 — the unique substring used here for sounddevice lookup).
  # Cross-correlating the UMIK capture against this ref isolates pure
  # acoustic delay from CamillaDSP / USB / Denon processing latency.
  loopback_ref_device: hw:2,1
  loopback_ref_channels: 2
  loopback_ref_channel_index: 1
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
    --ipc=host \\
    --privileged \\
    -v ${DATA_DIR}:/data/.avr-calibration \\
    -v /run/user/1000:/run/user/1000 \\
    -e XDG_RUNTIME_DIR=/run/user/1000 \\
    -e PIPEWIRE_RUNTIME_DIR=/run/user/1000 \\
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

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

# ── 7. Audio stack (audio-mode + watchdog + pikaraoke bridge) ─────────────
#
# Installs the host-side audio orchestration: mode switcher (listening/cal/
# karaoke), the CamillaDSP watchdog, and the pikaraoke idle bridge. These
# run on the Pi (not in a container) because they manage host services
# (camilladsp.service, the pikaraoke Docker container) and need privileges.

echo ""
echo "--- Installing audio-stack scripts and services ---"

# Root python needs websockets for the watchdog probe + audio-mode probe.
# Without it the watchdog falls into a thrash loop restarting CamillaDSP
# every ~20s. Use --break-system-packages on Debian 12+ (system pip is
# externally-managed). Skip if already installed.
if ! sudo python3 -c 'import websockets' 2>/dev/null; then
    echo "Installing python3-websockets for root (watchdog/audio-mode probes)"
    sudo pip3 install --break-system-packages websockets >/dev/null
fi

for f in audio-mode denon-watch.sh camilladsp-watchdog.sh; do
    if [ -f "$SCRIPT_DIR/$f" ]; then
        sudo install -m 0755 "$SCRIPT_DIR/$f" "/usr/local/sbin/$f"
        echo "Installed: /usr/local/sbin/$f"
    fi
done

# audio-mode.conf — only install if absent (don't clobber user edits).
if [ -f "$SCRIPT_DIR/audio-mode.conf" ] && [ ! -f /etc/audio-mode.conf ]; then
    sudo install -m 0644 "$SCRIPT_DIR/audio-mode.conf" /etc/audio-mode.conf
    echo "Installed: /etc/audio-mode.conf  (edit to set KARAOKE_TRIGGER_INPUT)"
fi

# ALSA configs — asound.conf defines karaoke_out plug (stereo → Scarlett PCM 1/2,
# fed into Scarlett HW Mix A/B which sum mic preamps 1/2 + PCM 1/2 → Line 1/2).
# Per-user asoundrc sets pi user's ALSA default to karaoke_out so Chromium kiosk
# audio flows there during karaoke mode.
if [ -f "$SCRIPT_DIR/asound.conf" ]; then
    sudo install -m 0644 "$SCRIPT_DIR/asound.conf" /etc/asound.conf
    echo "Installed: /etc/asound.conf"
fi
if [ -f "$SCRIPT_DIR/home-pi.asoundrc" ] && [ -d /home/pi ]; then
    sudo install -m 0644 -o pi -g pi "$SCRIPT_DIR/home-pi.asoundrc" /home/pi/.asoundrc
    echo "Installed: /home/pi/.asoundrc"
fi

# PipeWire configs: Scarlett pro-audio profile + 48kHz/256-frame clock lock.
# These live alongside CamillaDSP's native PipeWire backend (camilladsp-linux-
# pipewire-aarch64 binary at /usr/local/bin/camilladsp). CamillaDSP is a PW
# client — it captures + plays back all 20 Scarlett channels via the PW graph,
# letting the measurement engine attach a concurrent capture stream for the
# loopback reference (Scarlett input ch3 → snd-aloop, bridged by the
# loopback-ref-link service installed below).
if [ -f "$SCRIPT_DIR/wireplumber-scarlett.lua" ] && [ -d /home/pi ]; then
    sudo install -d -o pi -g pi /home/pi/.config/wireplumber/main.lua.d
    sudo install -m 0644 -o pi -g pi "$SCRIPT_DIR/wireplumber-scarlett.lua" \
        /home/pi/.config/wireplumber/main.lua.d/50-scarlett.lua
    echo "Installed: /home/pi/.config/wireplumber/main.lua.d/50-scarlett.lua"
fi
if [ -f "$SCRIPT_DIR/pipewire-scarlett-clock.conf" ]; then
    sudo install -d /etc/pipewire/pipewire.conf.d
    sudo install -m 0644 "$SCRIPT_DIR/pipewire-scarlett-clock.conf" \
        /etc/pipewire/pipewire.conf.d/10-scarlett-clock.conf
    echo "Installed: /etc/pipewire/pipewire.conf.d/10-scarlett-clock.conf"
fi

# avr-cal-sweep PipeWire null sink + persistent link to camilladsp_capture.
# Lets the container play the USB-route sweep into the PipeWire graph and
# from there into CamillaDSP. Installed as a user systemd unit (same
# pattern as loopback-ref-link below).
if [ -f "$SCRIPT_DIR/avr-cal-sweep-link.sh" ]; then
    sudo install -m 0755 "$SCRIPT_DIR/avr-cal-sweep-link.sh" \
        /usr/local/sbin/avr-cal-sweep-link.sh
    echo "Installed: /usr/local/sbin/avr-cal-sweep-link.sh"
fi
if [ -f "$SCRIPT_DIR/avr-cal-sweep-link.service" ] && [ -d /home/pi ]; then
    sudo install -d -o pi -g pi /home/pi/.config/systemd/user
    sudo install -m 0644 -o pi -g pi "$SCRIPT_DIR/avr-cal-sweep-link.service" \
        /home/pi/.config/systemd/user/avr-cal-sweep-link.service
    sudo -u pi XDG_RUNTIME_DIR=/run/user/1000 \
        systemctl --user daemon-reload 2>/dev/null || true
    sudo -u pi XDG_RUNTIME_DIR=/run/user/1000 \
        systemctl --user enable --now avr-cal-sweep-link.service 2>/dev/null || true
    echo "Installed + enabled: avr-cal-sweep-link.service (user)"
fi

# Loopback ref bridge: Scarlett input ch3 (AUX2) → snd-aloop sink. The
# avr-calibration measurement engine reads hw:Loopback,1,0 as the
# loopback reference. systemd user service so it follows pipewire.service.
if [ -f "$SCRIPT_DIR/loopback-ref-link.sh" ]; then
    sudo install -m 0755 "$SCRIPT_DIR/loopback-ref-link.sh" \
        /usr/local/sbin/loopback-ref-link.sh
    echo "Installed: /usr/local/sbin/loopback-ref-link.sh"
fi
if [ -f "$SCRIPT_DIR/loopback-ref-link.service" ] && [ -d /home/pi ]; then
    sudo install -d -o pi -g pi /home/pi/.config/systemd/user
    sudo install -m 0644 -o pi -g pi "$SCRIPT_DIR/loopback-ref-link.service" \
        /home/pi/.config/systemd/user/loopback-ref-link.service
    sudo -u pi XDG_RUNTIME_DIR=/run/user/1000 \
        systemctl --user daemon-reload 2>/dev/null || true
    sudo -u pi XDG_RUNTIME_DIR=/run/user/1000 \
        systemctl --user enable --now loopback-ref-link.service 2>/dev/null || true
    echo "Installed + enabled: loopback-ref-link.service (user)"
fi

for svc in camilladsp-watchdog.service denon-watch.service; do
    if [ -f "$SCRIPT_DIR/$svc" ]; then
        sudo cp "$SCRIPT_DIR/$svc" "/etc/systemd/system/$svc"
        echo "Installed: /etc/systemd/system/$svc"
    fi
done

# Retire the old dmix keepalive if it's still present from a previous install.
if systemctl list-unit-files scarlett-keepalive.service >/dev/null 2>&1; then
    sudo systemctl disable --now scarlett-keepalive.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/scarlett-keepalive.service
fi

sudo systemctl daemon-reload
for svc in camilladsp-watchdog.service denon-watch.service; do
    if [ -f "/etc/systemd/system/$svc" ]; then
        sudo systemctl enable --now "$svc"
    fi
done

# Land in listening mode (sets /run/audio-mode so the watchdog behaves).
if [ -x /usr/local/sbin/audio-mode ]; then
    sudo /usr/local/sbin/audio-mode set listening || true
fi

# ── 8. Bare-metal measurement service ─────────────────────────────────────
#
# Installs the avr-measurement systemd service which runs MeasurementEngine
# natively (with host PipeWire access), eliminating the need for the Docker
# container to mount /run/user/1000.

echo ""
echo "--- Installing avr-measurement service ---"

# Install uv if not present
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    source "$HOME/.local/bin/env" 2>/dev/null || export PATH="$HOME/.local/bin:$PATH"
fi

MEAS_DIR=/opt/avr-measurement
sudo mkdir -p "$MEAS_DIR/src"
sudo chown pi:pi "$MEAS_DIR"

# Sync the calibrate package source and project metadata
rsync -a --delete calibrate/ "$MEAS_DIR/src/calibrate/"
cp pyproject.toml uv.lock "$MEAS_DIR/src/"

# Create venv and install
cd "$MEAS_DIR/src"
uv venv "$MEAS_DIR/venv"
UV_PROJECT_ENVIRONMENT="$MEAS_DIR/venv" uv sync --no-dev --extra measurement --no-editable
cd -

# Install systemd service
if [ -f "$SCRIPT_DIR/avr-measurement.service" ]; then
    sudo cp "$SCRIPT_DIR/avr-measurement.service" /etc/systemd/system/avr-measurement.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now avr-measurement.service
    echo "Installed + enabled: avr-measurement.service"
fi

# ── 9. Done ───────────────────────────────────────────────────────────────

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
