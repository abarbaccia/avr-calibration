#!/usr/bin/env bash
# deploy/hotfix.sh — SSH hotfix: overlay changed source files into the running container
#
# Usage:
#   ./deploy/hotfix.sh                          # auto-detects git-modified calibrate/ files
#   ./deploy/hotfix.sh calibrate/web.py         # specific files
#   ./deploy/hotfix.sh --clean                  # wipe accumulated hotfix files and restore image
#   PI_HOST=192.168.1.50 ./deploy/hotfix.sh     # override Pi address
#
# How it works:
#   Files are accumulated in a stable directory on the Pi (/tmp/avr-hotfix/).
#   Each new hotfix merges its files into that directory — so redeploying a
#   second file does NOT lose the first file's changes. The container is started
#   with bind-mounts for ALL files currently in the accumulation directory.
#
# To revert to the real image (after pipeline confirms the fix):
#   ssh pi@192.168.1.117 "sudo systemctl start avr-calibration"
#   ./deploy/hotfix.sh --clean   (also wipes accumulated files)

set -euo pipefail

PI_HOST="${PI_HOST:-192.168.1.117}"
PI_USER="${PI_USER:-pi}"
CONTAINER="avr-calibration"
IMAGE="${IMAGE:-ghcr.io/abarbaccia/avr-calibration:latest}"
PKG_IN_CONTAINER="/opt/venv/lib/python3.11/site-packages/calibrate"
SERVICE="avr-calibration"
PI_HOTFIX_DIR="/tmp/avr-hotfix"

# ── Bare-metal measurement service (a SEPARATE deploy target) ──────────────────
# The avr-measurement.service runs on the Pi host (NOT in the container) and
# executes the measurement-path modules below. It loads them from TWO locations:
#   - the installed venv site-packages
#   - the editable source checkout
# Restarting the container alone does NOT update this service's code, so a
# measurement-path hotfix must also be scp'd to both bare-metal paths.
MEAS_SERVICE="avr-measurement.service"
MEAS_VENV_PKG="/opt/avr-measurement/venv/lib/python3.11/site-packages/calibrate"
MEAS_SRC_PKG="/opt/avr-measurement/src/calibrate"
# calibrate/ files (relative to calibrate/) that the bare-metal service runs.
MEAS_PATH_FILES=(
    "measurement.py"
    "measurement_service.py"
    "measurement_client.py"
    "drivers/playback.py"
)

SSH="ssh ${PI_USER}@${PI_HOST}"

# ── --clean: wipe and restore ─────────────────────────────────────────────────

if [ "${1:-}" = "--clean" ]; then
    echo "Wiping hotfix dir and restoring stable image..."
    $SSH "sudo docker stop ${CONTAINER} 2>/dev/null || true
          sudo docker rm -f ${CONTAINER} 2>/dev/null || true
          rm -rf ${PI_HOTFIX_DIR}
          sudo systemctl start ${SERVICE}"
    echo "Done. Stable image restored."
    exit 0
fi

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
echo "New files:"
for f in "${FILES[@]}"; do echo "  $f"; done
echo ""

# ── Merge new files into stable accumulation dir ──────────────────────────────

# Is `rel` (path relative to calibrate/) one the bare-metal service runs?
is_measurement_path_file() {
    local rel="$1"
    local m
    for m in "${MEAS_PATH_FILES[@]}"; do
        [ "$rel" = "$m" ] && return 0
    done
    return 1
}

MEAS_DEPLOYED=0  # set to 1 if any measurement-path file was pushed to bare metal

for f in "${FILES[@]}"; do
    if [ ! -f "$f" ]; then
        echo "ERROR: File not found: $f"
        exit 1
    fi
    rel="${f#calibrate/}"
    subdir=$(dirname "$rel")
    $SSH "mkdir -p ${PI_HOTFIX_DIR}/${subdir}"
    echo "Copying $f → ${PI_USER}@${PI_HOST}:${PI_HOTFIX_DIR}/${rel}"
    scp "$f" "${PI_USER}@${PI_HOST}:${PI_HOTFIX_DIR}/${rel}"

    # ── Bare-metal measurement service: also overlay measurement-path files ────
    # into BOTH the venv site-packages and the editable source checkout so the
    # host-side avr-measurement.service runs the hotfixed code, not stale code.
    if is_measurement_path_file "$rel"; then
        echo "  ↳ measurement-path file — also deploying to bare-metal ${MEAS_SERVICE}"
        for dest_pkg in "${MEAS_VENV_PKG}" "${MEAS_SRC_PKG}"; do
            # Only deploy to a location that actually exists on the Pi (the
            # editable src checkout may be absent on some installs). Idempotent.
            if $SSH "[ -d ${dest_pkg} ]"; then
                echo "    → ${dest_pkg}/${rel}"
                # scp into a user-writable temp path, then sudo-install it
                # (the dest dirs are root-owned). Clean up the temp file after.
                tmp="/tmp/avr-meas-hotfix-$$-$(echo "$rel" | tr '/' '_')"
                scp "$f" "${PI_USER}@${PI_HOST}:${tmp}"
                $SSH "sudo mkdir -p ${dest_pkg}/${subdir} \
                      && sudo cp ${tmp} ${dest_pkg}/${rel} \
                      && rm -f ${tmp} \
                      && sudo find ${dest_pkg} -name __pycache__ -exec rm -rf {} + 2>/dev/null || true"
            else
                echo "    (skipped ${dest_pkg} — not present on Pi)"
            fi
        done
        MEAS_DEPLOYED=1
    fi
done

# ── Build bind-mount args from ALL accumulated files ──────────────────────────
# Every file in PI_HOTFIX_DIR gets mounted over the installed package path.
# A second hotfix automatically picks up files from the first.

MOUNT_ARGS=$($SSH "find ${PI_HOTFIX_DIR} -type f 2>/dev/null | sort | while read -r p; do
    rel=\${p#${PI_HOTFIX_DIR}/}
    echo \" -v \${p}:${PKG_IN_CONTAINER}/\${rel}:ro\"
done" | tr -d '\n')

echo ""
echo "All hotfixed files (accumulated):"
$SSH "find ${PI_HOTFIX_DIR} -type f 2>/dev/null | sort | sed \"s|${PI_HOTFIX_DIR}/|  |\"" || true
echo ""

# ── Stop service, start hotfixed container ────────────────────────────────────

echo "Stopping ${SERVICE} service on Pi..."
$SSH "sudo systemctl stop ${SERVICE} 2>/dev/null || true
      sudo docker rm -f ${CONTAINER} 2>/dev/null || true"

echo "Starting hotfixed container..."
# shellcheck disable=SC2029  # MOUNT_ARGS intentionally expands on local side
# Host networking matches deploy/avr-calibration.service — lets the container
# reach services on the Pi's loopback (CamillaDSP on 127.0.0.1:1234, minidspd,
# etc.) without having to rebind those daemons to 0.0.0.0. Ports 8000 (web)
# and 8765 (MCP SSE) are still reachable on the Pi's LAN IP via host net.
$SSH "sudo docker run -d \
    --name ${CONTAINER} \
    --network=host \
    --ipc=host \
    --privileged \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e OPENBLAS_NUM_THREADS=4 \
    -e OMP_NUM_THREADS=4 \
    -v \$HOME/.avr-calibration:/data/.avr-calibration \
    -v \$HOME/.avr-calibration/entrypoint.sh:/entrypoint.sh:ro \
    -v /etc/asound.conf:/etc/asound.conf:ro \
    ${MOUNT_ARGS} \
    --entrypoint sh \
    ${IMAGE} \
    -c 'find /opt/venv/lib/python3.11/site-packages/calibrate -name __pycache__ -exec rm -rf {} + 2>/dev/null; exec /entrypoint.sh' \
    && echo 'Container started.'"

# Restart the bare-metal measurement service so it picks up the freshly
# deployed measurement-path code. Only when a measurement-path file was
# actually deployed to bare metal — otherwise the restart is a no-op churn.
if [ "${MEAS_DEPLOYED}" = "1" ]; then
    echo "Restarting bare-metal ${MEAS_SERVICE} (measurement-path code changed)..."
    $SSH "sudo systemctl restart ${MEAS_SERVICE} || true"
else
    echo "No measurement-path files changed — leaving ${MEAS_SERVICE} running."
fi

# ── Follow logs ───────────────────────────────────────────────────────────────

echo ""
echo "================================================================"
echo " Hotfix active — following logs (Ctrl+C to stop tailing)"
echo ""
echo " Accumulated hotfix files in ${PI_HOTFIX_DIR}:"
$SSH "find ${PI_HOTFIX_DIR} -type f 2>/dev/null | sort | sed \"s|${PI_HOTFIX_DIR}/|  |\"" || true
echo ""
echo " To revert to the stable image:"
echo "   ./deploy/hotfix.sh --clean"
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
              rm -rf ${PI_HOTFIX_DIR}
              sudo systemctl start ${SERVICE}"
        echo "Stable image restored."
        ;;
    *)
        echo "Left hotfix running. Hotfix files persist in ${PI_HOTFIX_DIR}."
        echo "Next hotfix will accumulate on top. To wipe: ./deploy/hotfix.sh --clean"
        ;;
esac
