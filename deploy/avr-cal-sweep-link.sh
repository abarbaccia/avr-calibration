#!/bin/bash
# avr-cal-sweep PipeWire null sink + persistent links.
#
# Two links required for USB sub calibration:
#
#   avr_cal_sweep:monitor_FL → camilladsp_capture:input_3
#     Sweep enters CamillaDSP's LFE mixer channel 2 (0-indexed) = PipeWire port
#     input_3 (1-indexed). The lfe_source mixer fans this mono feed to all sub
#     outputs. Only FL is needed — the lfe_source mixer distributes it.
#     WRONG: input_2 (0-indexed channel 1) is NOT the LFE channel.
#
#   avr_cal_sweep:monitor_FL → loopback_ref:playback_1
#     Pre-CamillaDSP reference for deconvolution.  H = mic / ref captures the
#     full DSP chain (HPF + PEQ + FIR) in every measurement.
#
# avr_cal_sweep is a permanent PW null sink created by the system at daemon
# startup (/etc/pipewire/pipewire.conf.d/10-avr-cal-sweep.conf).  ensure_sink()
# creates it via pactl only if the daemon conf file is absent (e.g. fresh install).
#
# Idempotent: re-running or restarting the service skips existing links.
# Stale cleanup: any UMIK→camilladsp_capture auto-links from WirePlumber are
# removed at startup — the UMIK is captured directly by the measurement service
# (PortAudio), not via CamillaDSP.

set -u

SINK_NAME='avr_cal_sweep'
CAM_CAPTURE='camilladsp_capture'
LOOPBACK_REF='loopback_ref'
UMIK_NODE='alsa_input.usb-miniDSP_Umik-1_Gain__18dB_00002-00.analog-stereo'

ensure_sink() {
    if pw-cli ls Node 2>/dev/null | grep -q "node.name = \"${SINK_NAME}\""; then
        return 0
    fi
    # session.suspend-timeout-seconds=0 + node.pause-on-idle=false: a null sink has
    # no hardware clock; if WirePlumber suspends it on idle its monitor stops passing
    # audio (pw-cat plays in, monitor stays silent). Pin it hot. Matches 10-avr-cal-sweep.conf.
    pactl load-module module-null-sink \
        sink_name="${SINK_NAME}" \
        sink_properties="device.description='AVR-Cal-Sweep' session.suspend-timeout-seconds=0 node.pause-on-idle=false" \
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

remove_stale() {
    # Remove WirePlumber auto-links from UMIK into camilladsp_capture.
    # The UMIK is captured directly by the bare-metal measurement service
    # (PortAudio); linking it into CamillaDSP contaminates the sweep path.
    for port in input_1 input_2 input_3 input_4; do
        pw-link -d "${UMIK_NODE}:capture_FL" "${CAM_CAPTURE}:${port}" 2>/dev/null || true
        pw-link -d "${UMIK_NODE}:capture_FR" "${CAM_CAPTURE}:${port}" 2>/dev/null || true
    done
    # Remove any stale avr_cal_sweep→input_2 links (old wrong port).
    pw-link -d "${SINK_NAME}:monitor_FL" "${CAM_CAPTURE}:input_2" 2>/dev/null || true
    pw-link -d "${SINK_NAME}:monitor_FR" "${CAM_CAPTURE}:input_2" 2>/dev/null || true
}

up() {
    # Up to 15 attempts (15 s) for PipeWire + CamillaDSP + loopback_ref to appear.
    for _ in $(seq 1 15); do
        if ! ensure_sink; then sleep 1; continue; fi

        # Remove any stale links before establishing correct ones.
        remove_stale

        ok=1
        # CamillaDSP LFE input: input_3 = 0-indexed channel 2 in PipeWire (1-indexed ports).
        # This is the load-bearing link that drives the subs through the lfe_source mixer.
        link_one "${SINK_NAME}:monitor_FL" "${CAM_CAPTURE}:input_3" || ok=0

        # Pre-CamillaDSP loopback reference (measurement deconvolution X).
        link_one "${SINK_NAME}:monitor_FL" "${LOOPBACK_REF}:playback_1" || ok=0

        if [ "$ok" = "1" ]; then
            echo "avr-cal-sweep links active (${SINK_NAME} → ${CAM_CAPTURE}:input_3 + ${LOOPBACK_REF}:playback_1)"
            exit 0
        fi
        sleep 1
    done
    echo "avr-cal-sweep link setup failed after 15 attempts" >&2
    exit 1
}

down() {
    pw-link -d "${SINK_NAME}:monitor_FL" "${CAM_CAPTURE}:input_3" 2>/dev/null || true
    pw-link -d "${SINK_NAME}:monitor_FL" "${LOOPBACK_REF}:playback_1" 2>/dev/null || true
    exit 0
}

case "${1:-up}" in
    up) up ;;
    down) down ;;
    *) echo "usage: $0 up|down" >&2; exit 2 ;;
esac
