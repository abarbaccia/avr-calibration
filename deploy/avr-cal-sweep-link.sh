#!/bin/bash
# avr-cal-sweep PipeWire null sink + persistent links.
#
# Three links required for USB sub calibration:
#
#   avr_cal_sweep:monitor_FL/FR → camilladsp_capture:input_2
#     Sweep enters CamillaDSP's LFE mixer (lfe_input_channel=2, PW port input_2).
#     H = CamillaDSP(HPF+PEQ+FIR) × room_sub × mic ← full DSP chain visible.
#
#   avr_cal_sweep:monitor_FL → loopback_ref:playback_1
#     Pre-CamillaDSP reference for deconvolution.  LoopbackRefPlayback pw-record
#     captures this as the X in H = mic / ref.  Using the pre-DSP signal means
#     FIR and PEQ corrections ARE visible in measurements (not normalised out).
#
# avr_cal_sweep is a permanent PW null sink created by the system at daemon
# startup (/etc/pipewire/pipewire.conf.d/10-avr-cal-sweep.conf).  ensure_sink()
# creates it via pactl only if the daemon conf file is absent (e.g. fresh install).
#
# Idempotent: re-running or restarting the service skips existing links.

set -u

SINK_NAME='avr_cal_sweep'
CAM_CAPTURE='camilladsp_capture'
LOOPBACK_REF='loopback_ref'

ensure_sink() {
    if pw-cli ls Node 2>/dev/null | grep -q "node.name = \"${SINK_NAME}\""; then
        return 0
    fi
    pactl load-module module-null-sink \
        sink_name="${SINK_NAME}" \
        sink_properties="device.description='AVR-Cal-Sweep'" \
        media.class=Audio/Sink \
        channel_map=front-left,front-right \
        >/dev/null || return 1
    sleep 0.3
    return 0
}

# pw-link FROM TO. Treat "File exists" as success (idempotent).
link_one() {
    local src="$1"
    local dst="$2"
    local out
    out=$(pw-link "$src" "$dst" 2>&1) && return 0
    echo "$out" | grep -q 'File exists' && return 0
    echo "pw-link failed: $src -> $dst :: $out" >&2
    return 1
}

up() {
    # Up to 15 attempts (15 s) for PipeWire + CamillaDSP + loopback_ref to appear.
    for _ in $(seq 1 15); do
        if ! ensure_sink; then sleep 1; continue; fi

        # Self-heal: tear down the pre-2e772a0 stale link to input_3 (capture
        # channel 2, unread by the in:2 cal_matrix). It persists on the permanent
        # null sink across restarts and confuses the graph. Harmless but wrong.
        pw-link -d "${SINK_NAME}:monitor_FL" "${CAM_CAPTURE}:input_3" 2>/dev/null || true
        pw-link -d "${SINK_NAME}:monitor_FR" "${CAM_CAPTURE}:input_3" 2>/dev/null || true

        ok=1
        # CamillaDSP LFE input (port input_2 = 0-indexed channel 2 in PW).
        link_one "${SINK_NAME}:monitor_FL" "${CAM_CAPTURE}:input_2" || ok=0
        link_one "${SINK_NAME}:monitor_FR" "${CAM_CAPTURE}:input_2" || ok=0

        # Pre-CamillaDSP loopback reference (measurement deconvolution X).
        link_one "${SINK_NAME}:monitor_FL" "${LOOPBACK_REF}:playback_1" || ok=0

        if [ "$ok" = "1" ]; then
            echo "avr-cal-sweep links active (${SINK_NAME} → ${CAM_CAPTURE}:input_2 + ${LOOPBACK_REF}:playback_1)"
            exit 0
        fi
        sleep 1
    done
    echo "avr-cal-sweep link setup failed after 15 attempts" >&2
    exit 1
}

down() {
    pw-link -d "${SINK_NAME}:monitor_FL" "${CAM_CAPTURE}:input_2" 2>/dev/null || true
    pw-link -d "${SINK_NAME}:monitor_FR" "${CAM_CAPTURE}:input_2" 2>/dev/null || true
    pw-link -d "${SINK_NAME}:monitor_FL" "${LOOPBACK_REF}:playback_1" 2>/dev/null || true
    # Stale pre-2e772a0 links (see up()).
    pw-link -d "${SINK_NAME}:monitor_FL" "${CAM_CAPTURE}:input_3" 2>/dev/null || true
    pw-link -d "${SINK_NAME}:monitor_FR" "${CAM_CAPTURE}:input_3" 2>/dev/null || true
    exit 0
}

case "${1:-up}" in
    up) up ;;
    down) down ;;
    *) echo "usage: $0 up|down" >&2; exit 2 ;;
esac
