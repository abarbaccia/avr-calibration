#!/usr/bin/env bash
# Flash and configure a Raspberry Pi OS 64-bit SD card for headless Pi 5.
# Run as root (sudo bash setup-sdcard.sh) after flashing the image with dd.
#
# What this does:
#   1. Enables SSH on first boot (boot partition)
#   2. Creates the pi user with a default password (boot partition)
#   3. Installs your SSH public key (root partition)
#   4. Writes a NetworkManager WiFi connection (root partition)
#
# Usage:
#   sudo bash setup-sdcard.sh [DEVICE] [SSID] [PASSWORD] [SSH_KEY_FILE]
#
# Defaults:
#   DEVICE       /dev/sdb
#   SSID         (prompted if not given)
#   PASSWORD     (prompted if not given)
#   SSH_KEY_FILE ~/.ssh/id_rsa.pub (or first .pub found in ~/.ssh)
#
# Example:
#   sudo bash setup-sdcard.sh /dev/sdb "MyWiFi" "MyPassword"

set -euo pipefail

DEVICE="${1:-/dev/sdb}"
SSID="${2:-}"
WIFI_PASS="${3:-}"
SSH_KEY_FILE="${4:-}"

BOOT=/mnt/pi-boot
ROOT=/mnt/pi-root

# ── Resolve SSH key ───────────────────────────────────────────────────────
if [ -z "$SSH_KEY_FILE" ]; then
    # Find the invoking user's .pub key (works under sudo)
    REAL_HOME=$(getent passwd "${SUDO_USER:-$USER}" | cut -d: -f6)
    SSH_KEY_FILE=$(ls "$REAL_HOME"/.ssh/*.pub 2>/dev/null | head -1 || true)
fi
if [ -z "$SSH_KEY_FILE" ] || [ ! -f "$SSH_KEY_FILE" ]; then
    echo "ERROR: No SSH public key found. Pass it as the 4th argument." >&2
    exit 1
fi
SSH_KEY=$(cat "$SSH_KEY_FILE")

# ── Prompt for WiFi if not provided ───────────────────────────────────────
if [ -z "$SSID" ]; then
    read -rp "WiFi SSID: " SSID
fi
if [ -z "$WIFI_PASS" ]; then
    read -rsp "WiFi password: " WIFI_PASS
    echo
fi

echo ""
echo "=== Configuring SD card: $DEVICE ==="
echo "    WiFi:    $SSID"
echo "    SSH key: $SSH_KEY_FILE"
echo ""

# ── Boot partition ────────────────────────────────────────────────────────
echo "--- Boot partition ---"
mkdir -p "$BOOT"
mountpoint -q "$BOOT" || mount "${DEVICE}1" "$BOOT"

# Enable SSH on first boot
touch "$BOOT/ssh"

# Create pi user (password: raspberry — change after first login)
ENCRYPTED_PASS=$(openssl passwd -6 "raspberry")
echo "pi:${ENCRYPTED_PASS}" > "$BOOT/userconf.txt"

sync
umount "$BOOT"
echo "SSH enabled, pi user created (password: raspberry)"

# ── Root partition ────────────────────────────────────────────────────────
echo "--- Root partition ---"
mkdir -p "$ROOT"
mountpoint -q "$ROOT" || mount "${DEVICE}2" "$ROOT"

# SSH authorized key
mkdir -p "$ROOT/home/pi/.ssh"
echo "$SSH_KEY" > "$ROOT/home/pi/.ssh/authorized_keys"
chmod 700 "$ROOT/home/pi/.ssh"
chmod 600 "$ROOT/home/pi/.ssh/authorized_keys"
echo "SSH public key installed"

# NetworkManager WiFi connection (Bookworm uses NM, not wpa_supplicant)
CONN_DIR="$ROOT/etc/NetworkManager/system-connections"
mkdir -p "$CONN_DIR"
CONN_FILE="$CONN_DIR/${SSID// /-}.nmconnection"
cat > "$CONN_FILE" << EOF
[connection]
id=${SSID}
type=wifi
autoconnect=true

[wifi]
ssid=${SSID}
mode=infrastructure

[wifi-security]
key-mgmt=wpa-psk
psk=${WIFI_PASS}

[ipv4]
method=auto

[ipv6]
method=auto
addr-gen-mode=stable-privacy
EOF
chmod 600 "$CONN_FILE"
echo "WiFi configured: $SSID"

sync
umount "$ROOT"

echo ""
echo "=== SD card ready ==="
echo "  Eject:  sudo eject $DEVICE"
echo "  Boot the Pi, then after ~60s:"
echo "  SSH in: ssh pi@raspberrypi.local"
echo "  Install: curl -sL https://raw.githubusercontent.com/abarbaccia/avr-calibration/main/deploy/install.sh | bash"
