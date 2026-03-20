#!/usr/bin/env bash
# avr-calibration Pi Zero W bootstrap
# Run as the pi user (not root): bash install.sh
# Tested on Raspberry Pi OS Bookworm Lite (32-bit, Python 3.11)
set -euo pipefail

REPO_URL="https://github.com/abarbaccia/avr-calibration"
INSTALL_DIR="$HOME/avr-calibration"
SERVICE_NAME="avr-calibration"
MINIDSP_VERSION="0.1.5"
MINIDSP_URL="https://github.com/mrene/minidsp-rs/releases/download/v${MINIDSP_VERSION}/minidspd-arm-unknown-linux-gnueabihf.tar.gz"

echo ""
echo "=== avr-calibration Pi Zero W setup ==="
echo ""

# ── 1. System check ────────────────────────────────────────────────────────

PYTHON=$(python3 --version 2>&1)
echo "Python: $PYTHON"
if ! python3 -c "import sys; assert sys.version_info >= (3,11)" 2>/dev/null; then
    echo "ERROR: Python 3.11+ required. Run: sudo apt install python3.11"
    exit 1
fi

ARCH=$(uname -m)
echo "Arch:   $ARCH"
if [[ "$ARCH" != "armv6l" && "$ARCH" != "armv7l" && "$ARCH" != "aarch64" ]]; then
    echo "WARNING: unexpected architecture $ARCH — continuing anyway"
fi

# ── 2. System packages ─────────────────────────────────────────────────────

echo ""
echo "--- Installing system packages ---"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    git \
    portaudio19-dev \
    libatlas-base-dev \
    python3-dev \
    python3-pip \
    curl \
    udev

# ── 3. uv ──────────────────────────────────────────────────────────────────

echo ""
echo "--- Installing uv ---"
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# uv installs to ~/.local/bin; make it available for the rest of this script
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
echo 'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"' >> "$HOME/.bashrc"
echo "uv: $(uv --version)"

# ── 4. Clone / update repo ─────────────────────────────────────────────────

echo ""
echo "--- Setting up avr-calibration ---"
if [ -d "$INSTALL_DIR" ]; then
    echo "Updating existing install..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ── 5. Python deps (ARMv6 numpy pin) ──────────────────────────────────────

echo ""
echo "--- Installing Python dependencies ---"
# On ARMv6 (Pi Zero W), numpy 1.26+ has no wheel — pin to last ARMv6 release.
# numpy 1.24.x requires distutils (available in Python 3.11, removed in 3.12).
# Pi Zero W ships with Python 3.11 on Bookworm so this is safe.
if [[ "$ARCH" == "armv6l" ]]; then
    echo "ARMv6 detected — pinning numpy to 1.24.x (compiling from source, ~20 min)"
    uv venv .venv --clear
    # Install numpy 1.24.x into the venv before uv sync so uv reuses it.
    # uv pip uses uv's own resolver — no system pip needed, no PEP 668 issue.
    uv pip install "numpy>=1.24.4,<1.25" --no-binary numpy --python .venv/bin/python
    uv sync --extra dev --no-build-isolation
else
    uv sync --extra dev
fi

# ── 6. minidsp-rs ─────────────────────────────────────────────────────────

echo ""
echo "--- Installing minidsp-rs ---"
if ! command -v minidspd &>/dev/null; then
    TMP=$(mktemp -d)
    echo "Downloading minidspd v${MINIDSP_VERSION} for ARM..."
    curl -sL "$MINIDSP_URL" -o "$TMP/minidspd.tar.gz"
    tar -xzf "$TMP/minidspd.tar.gz" -C "$TMP"
    sudo install -m 755 "$TMP/minidspd" /usr/local/bin/minidspd
    rm -rf "$TMP"
    echo "minidspd installed to /usr/local/bin/minidspd"
else
    echo "minidspd already installed: $(minidspd --version 2>/dev/null || echo 'version unknown')"
fi

# ── 7. udev rule for miniDSP USB ──────────────────────────────────────────

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

# ── 8. Config ─────────────────────────────────────────────────────────────

echo ""
echo "--- Generating config ---"
mkdir -p "$HOME/.avr-calibration"
if [ ! -f "$HOME/.avr-calibration/config.yaml" ]; then
    uv run calibrate check 2>/dev/null || true  # creates template
    echo ""
    echo "IMPORTANT: Edit ~/.avr-calibration/config.yaml with your Denon IP:"
    echo "  denon:"
    echo "    host: \"192.168.x.x\""
else
    echo "Config already exists at ~/.avr-calibration/config.yaml"
fi

# ── 9. systemd service ────────────────────────────────────────────────────

echo ""
echo "--- Installing systemd service ---"
SERVICE_FILE="$INSTALL_DIR/deploy/avr-calibration.service"
SYSTEMD_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# Substitute actual paths into service file
sed "s|__INSTALL_DIR__|$INSTALL_DIR|g; s|__USER__|$USER|g" \
    "$SERVICE_FILE" | sudo tee "$SYSTEMD_FILE" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl start "$SERVICE_NAME"
echo "Service enabled and started"

# ── 10. Done ──────────────────────────────────────────────────────────────

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit ~/.avr-calibration/config.yaml (set denon.host)"
echo "  2. Plug in miniDSP via USB"
echo "  3. Run: uv run calibrate check"
echo "  4. Service URL: http://$(hostname -I | awk '{print $1}'):8000"
echo ""
echo "Service commands:"
echo "  sudo systemctl status $SERVICE_NAME"
echo "  sudo systemctl restart $SERVICE_NAME"
echo "  sudo journalctl -u $SERVICE_NAME -f"
echo ""
