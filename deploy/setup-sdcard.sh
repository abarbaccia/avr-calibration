#!/usr/bin/env bash
# Configure a freshly-flashed Raspberry Pi OS 64-bit SD card for headless Pi 5.
#
# Uses the same firstrun.sh mechanism as rpi-imager:
#   - imager_custom set_wlan  (WiFi + country, PBKDF2 PSK)
#   - imager_custom enable_ssh (SSH + authorized key)
#   - userconf-pi userconf     (pi user creation)
#   - rfkill unblock wifi      (unblock radio)
#
# Usage:
#   sudo bash setup-sdcard.sh [DEVICE] [SSID] [WIFI_PASSWORD] [SSH_KEY_FILE]
#
# Defaults:
#   DEVICE        /dev/sdb
#   SSID          (prompted)
#   WIFI_PASSWORD (prompted)
#   SSH_KEY_FILE  first ~/.ssh/*.pub found
#
# Example:
#   sudo bash setup-sdcard.sh /dev/sdb "MyWiFi" "MyPassword"

set -euo pipefail

DEVICE="${1:-/dev/sdb}"
SSID="${2:-}"
WIFI_PASS="${3:-}"
SSH_KEY_FILE="${4:-}"
COUNTRY="${5:-US}"
HOSTNAME="raspberrypi"
USERNAME="pi"
DEFAULT_PASSWORD="raspberry"

BOOT=/mnt/pi-boot

# ── Resolve SSH key ───────────────────────────────────────────────────────
if [ -z "$SSH_KEY_FILE" ]; then
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

# ── Hash WiFi password (PBKDF2-SHA1, same as wpa_passphrase) ─────────────
WIFI_PSK=$(python3 -c "
import hashlib, sys
psk = hashlib.pbkdf2_hmac('sha1', '$WIFI_PASS'.encode(), '$SSID'.encode(), 4096, 32).hex()
print(psk)
")

# ── Encrypt default password ──────────────────────────────────────────────
ENCRYPTED_PASS=$(openssl passwd -6 "$DEFAULT_PASSWORD")

echo ""
echo "=== Configuring SD card: $DEVICE ==="
echo "    Hostname: $HOSTNAME"
echo "    WiFi:     $SSID ($COUNTRY)"
echo "    SSH key:  $SSH_KEY_FILE"
echo ""

# ── Mount boot partition ──────────────────────────────────────────────────
mkdir -p "$BOOT"
mountpoint -q "$BOOT" || mount "${DEVICE}1" "$BOOT"

# ── Write firstrun.sh (injected by rpi-imager --first-run-script) ─────────
cat > "$BOOT/firstrun.sh" << FIRSTRUN
#!/bin/sh
set +e

FIRSTUSER=\$(getent passwd 1000 | cut -d: -f1)
FIRSTUSERHOME=\$(getent passwd 1000 | cut -d: -f6)

# Hostname
if [ -f /usr/lib/raspberrypi-sys-mods/imager_custom ]; then
   /usr/lib/raspberrypi-sys-mods/imager_custom set_hostname ${HOSTNAME}
else
   echo ${HOSTNAME} > /etc/hostname
   sed -i "s/127.0.1.1.*\$/127.0.1.1\t${HOSTNAME}/g" /etc/hosts
fi

# User
if [ -f /usr/lib/userconf-pi/userconf ]; then
   /usr/lib/userconf-pi/userconf '${USERNAME}' '${ENCRYPTED_PASS}'
else
   echo "\$FIRSTUSER:${ENCRYPTED_PASS}" | chpasswd -e
fi
echo '${USERNAME} ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/010_${USERNAME}-nopasswd
chmod 0440 /etc/sudoers.d/010_${USERNAME}-nopasswd

# SSH + authorized key
if [ -f /usr/lib/raspberrypi-sys-mods/imager_custom ]; then
   /usr/lib/raspberrypi-sys-mods/imager_custom enable_ssh -k '${SSH_KEY}'
else
   install -o "\$FIRSTUSER" -m 700 -d "\$FIRSTUSERHOME/.ssh"
   echo '${SSH_KEY}' >> "\$FIRSTUSERHOME/.ssh/authorized_keys"
   chown "\$FIRSTUSER:\$FIRSTUSER" "\$FIRSTUSERHOME/.ssh/authorized_keys"
   chmod 600 "\$FIRSTUSERHOME/.ssh/authorized_keys"
   systemctl enable ssh
fi

# WiFi — imager_custom uses pre-hashed PSK + country code
if [ -f /usr/lib/raspberrypi-sys-mods/imager_custom ]; then
   /usr/lib/raspberrypi-sys-mods/imager_custom set_wlan '${SSID}' '${WIFI_PSK}' '${COUNTRY}'
else
   cat > /etc/wpa_supplicant/wpa_supplicant.conf << 'WPAEOF'
country=${COUNTRY}
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
ap_scan=1
update_config=1
network={
	scan_ssid=1
	ssid="${SSID}"
	key_mgmt=WPA-PSK SAE
	psk=${WIFI_PSK}
	ieee80211w=1
}
WPAEOF
   chmod 600 /etc/wpa_supplicant/wpa_supplicant.conf
fi

# Unblock WiFi radio
rfkill unblock wifi
for filename in /var/lib/systemd/rfkill/*:wlan; do
   echo 0 > \$filename
done

# Cleanup — remove firstrun from cmdline so it doesn't run again
rm -f /boot/firmware/firstrun.sh
sed -i 's| systemd.run.*||g' /boot/firmware/cmdline.txt
exit 0
FIRSTRUN

chmod +x "$BOOT/firstrun.sh"

# ── Inject firstrun into cmdline.txt ─────────────────────────────────────
CMDLINE="$BOOT/cmdline.txt"
if ! grep -q "firstrun.sh" "$CMDLINE"; then
    sed -i 's/$/ systemd.run=\/boot\/firmware\/firstrun.sh systemd.run_success_action=reboot systemd.unit=kernel-command-line.target/' "$CMDLINE"
fi

sync
umount "$BOOT"

echo ""
echo "=== SD card ready ==="
echo "  Eject:   sudo eject $DEVICE"
echo "  Boot the Pi — it will configure itself and reboot once (~60s)"
echo "  Then SSH: ssh ${USERNAME}@${HOSTNAME}.local"
echo "  Fallback password: $DEFAULT_PASSWORD"
echo ""
echo "  After SSH, run the install script:"
echo "  curl -sL https://raw.githubusercontent.com/abarbaccia/avr-calibration/main/deploy/install.sh | bash"
