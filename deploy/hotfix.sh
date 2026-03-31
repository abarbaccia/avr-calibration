#!/usr/bin/env bash
# deploy/hotfix.sh — SSH hotfix: overlay changed source files into the running container
#
# Usage:
#   ./deploy/hotfix.sh                          # auto-detects git-modified calibrate/ files
#   ./deploy/hotfix.sh calibrate/web.py         # specific files
#   PI_HOST=192.168.1.50 ./deploy/hotfix.sh     # override Pi address
#
# How it works:
#   Changed calibrate/*.py files are SCP'd to the Pi, then the container is
#   started with each file bind-mounted over the installed package path inside
#   the image. No rebuild required — takes effect in seconds.
#
# To revert to the real image (after pipeline confirms the fix):
#   ssh pi@avr-cal.local "sudo systemctl start avr-calibration"

set -euo pipefail

PI_HOST="${PI_HOST:-192.168.1.126}"
PI_USER="${PI_USER:-pi}"
CONTAINER="avr-calibration"
IMAGE="${IMAGE:-ghcr.io/abarbaccia/avr-calibration:latest}"
PKG_IN_CONTAINER="/opt/venv/lib/python3.11/site-packages/calibrate"
SERVICE="avr-calibration"

SSH="ssh ${PI_USER}@${PI_HOST}"

# ── Collect files ─────────────────────────────────────────────────────────────

if [ $# -gt 0 ]; then
    FILES=("$@")
else
    mapfile -t FILES < <(
        git diff --name-only HEAD -- 'calibrate/*.py' 'calibrate/**/*.py' \
        | grep '^calibrate/' || true
    )
    if [ ${#FILES[@]} -eq 0 ]; then
        echo "ERROR: No modified calibrate/ files found."
        echo "  Pass files explicitly: $0 calibrate/web.py"
        echo "  Or commit your changes first and try again."
        exit 1
    fi
fi

echo "=== avr-calibration SSH hotfix ==="
echo "Target : ${PI_USER}@${PI_HOST}"
echo "Files  :"
for f in "${FILES[@]}"; do echo "  $f"; done
echo ""

# ── SCP files to Pi ───────────────────────────────────────────────────────────

PI_TMP="/tmp/avr-hotfix-$$"
$SSH "mkdir -p ${PI_TMP}"

for f in "${FILES[@]}"; do
    if [ ! -f "$f" ]; then
        echo "ERROR: File not found: $f"
        exit 1
    fi
    echo "Copying $f → ${PI_USER}@${PI_HOST}:${PI_TMP}/$(basename "$f")"
    scp "$f" "${PI_USER}@${PI_HOST}:${PI_TMP}/$(basename "$f")"
done

# ── Build bind-mount args for docker run ──────────────────────────────────────

MOUNT_ARGS=""
for f in "${FILES[@]}"; do
    basename_f="$(basename "$f")"
    # calibrate/foo/bar.py → foo/bar.py (strip leading calibrate/)
    rel="${f#calibrate/}"
    MOUNT_ARGS="${MOUNT_ARGS} -v ${PI_TMP}/${basename_f}:${PKG_IN_CONTAINER}/${rel}:ro"
done

# ── Stop service, start hotfixed container ────────────────────────────────────

echo ""
echo "Stopping ${SERVICE} service on Pi..."
$SSH "sudo systemctl stop ${SERVICE} 2>/dev/null || true
      sudo docker rm -f ${CONTAINER} 2>/dev/null || true"

echo "Starting hotfixed container..."
# shellcheck disable=SC2029  # MOUNT_ARGS intentionally expands on local side
$SSH "sudo docker run -d \
    --name ${CONTAINER} \
    -p 8000:8000 \
    --device=/dev/bus/usb \
    --device=/dev/snd \
    -v \$HOME/.avr-calibration:/data/.avr-calibration \
    ${MOUNT_ARGS} \
    ${IMAGE} && echo 'Container started.'"

# ── Follow logs ───────────────────────────────────────────────────────────────

echo ""
echo "================================================================"
echo " Hotfix active — following logs (Ctrl+C to stop tailing)"
echo ""
echo " Hotfixed files:"
for f in "${FILES[@]}"; do echo "  $f"; done
echo ""
echo " To revert to the stable image:"
echo "   ssh ${PI_USER}@${PI_HOST} 'sudo systemctl start ${SERVICE}'"
echo "================================================================"
echo ""

$SSH "sudo docker logs -f ${CONTAINER}" || true

# ── Prompt to restore on exit ─────────────────────────────────────────────────

echo ""
read -rp "Restore stable image now? [Y/n] " yn
case "${yn:-Y}" in
    [Yy]*)
        $SSH "sudo docker stop ${CONTAINER} 2>/dev/null || true
              sudo docker rm ${CONTAINER} 2>/dev/null || true
              sudo systemctl start ${SERVICE}"
        echo "Stable image restored."
        ;;
    *)
        echo "Left hotfix running. Restore manually with:"
        echo "  ssh ${PI_USER}@${PI_HOST} 'sudo systemctl start ${SERVICE}'"
        ;;
esac

# Clean up temp files on Pi
$SSH "rm -rf ${PI_TMP}" 2>/dev/null || true
