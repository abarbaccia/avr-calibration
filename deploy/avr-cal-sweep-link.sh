#!/bin/bash
# avr-cal-sweep PipeWire null sink + persistent links to camilladsp_capture + snd-aloop.
#
# Design principle — unified loopback reference:
#   The snd-aloop (hw:2,1 capture = what PipeWire writes to hw:2,0 playback) is the
#   ONLY loopback reference. It carries the raw pre-processing sweep from avr_cal_sweep
#   monitor. This single reference works for BOTH sub and mains calibration:
#
#   Sub cal:   avr_cal_sweep → camilladsp_capture:input_3 (LFE path)
#              H = CamillaDSP(HPF+shelf) × room_sub × mic  ← full chain visible
#
#   Mains cal: avr_cal_sweep → HDMI → Denon → mains  (separate HDMI sink, future)
#              H = Denon_processing × room_mains × mic
#
#   Sub-vs-mains delay: same aloop reference for both → IR peak comparison gives
#   exact relative delay including all DSP latencies.
#
#   NOTE: loopback-ref-link.service (Scarlett input ch3 → snd-aloop) is DISABLED.
#   It was contaminating the reference with the Denon sub pre-out signal. The
#   reference is now purely the raw avr_cal_sweep monitor.
#
# Idempotent: re-running the script (or restarting the service) skips
# existing links and a pre-existing null sink without erroring.

set -u

SINK_NAME='avr_cal_sweep'
CAM_CAPTURE='camilladsp_capture'
ALOOP_SINK='alsa_output.platform-snd_aloop.0.analog-stereo'

ensure_sink() {
    # If a node named avr_cal_sweep already exists, skip the load-module.
    if pw-cli ls Node 2>/dev/null | grep -q "node.name = \"${SINK_NAME}\""; then
        return 0
    fi
    pactl load-module module-null-sink \
        sink_name="${SINK_NAME}" \
        sink_properties="device.description='AVR-Cal-Sweep'" \
        media.class=Audio/Sink \
        channel_map=front-left,front-right \
        >/dev/null || return 1
    # Small settle so the monitor ports register before we link them.
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
    # Up to 15 attempts (15s) for PipeWire + camilladsp_capture to be present.
    for _ in $(seq 1 15); do
        if ! ensure_sink; then sleep 1; continue; fi

        # Sub calibration path: monitor_FL/FR → input_3 (CamillaDSP LFE channel).
        # Both channels sum into input_3 (lfe_source mixer reads channel 2, 0-indexed).
        ok=1
        link_one "${SINK_NAME}:monitor_FL" "${CAM_CAPTURE}:input_3" || ok=0
        link_one "${SINK_NAME}:monitor_FR" "${CAM_CAPTURE}:input_3" || ok=0

        # Fan the raw sweep into snd-aloop as the loopback reference.
        # LoopbackRefPlayback reads hw:2,1 (the cross-loopback of what PipeWire
        # writes to hw:2,0 = alsa_output.platform-snd_aloop.0).
        # Reference = raw pre-CamillaDSP sweep → H includes full DSP chain.
        link_one "${SINK_NAME}:monitor_FL" "${ALOOP_SINK}:playback_FL" || true
        link_one "${SINK_NAME}:monitor_FR" "${ALOOP_SINK}:playback_FR" || true

        if [ "$ok" = "1" ]; then
            echo "avr-cal-sweep link active (${SINK_NAME} → ${CAM_CAPTURE}:input_3 + ${ALOOP_SINK})"
            exit 0
        fi
        sleep 1
    done
    echo "avr-cal-sweep link failed after 15 attempts" >&2
    exit 1
}

down() {
    pw-link -d "${SINK_NAME}:monitor_FL" "${CAM_CAPTURE}:input_3" 2>/dev/null || true
    pw-link -d "${SINK_NAME}:monitor_FR" "${CAM_CAPTURE}:input_3" 2>/dev/null || true
    pw-link -d "${SINK_NAME}:monitor_FL" "${ALOOP_SINK}:playback_FL" 2>/dev/null || true
    pw-link -d "${SINK_NAME}:monitor_FR" "${ALOOP_SINK}:playback_FR" 2>/dev/null || true
    # Leave the null sink loaded; pactl module IDs aren't stable across
    # restarts and tearing it down would break in-flight cal sessions.
    exit 0
}

case "${1:-up}" in
    up) up ;;
    down) down ;;
    *) echo "usage: $0 up|down" >&2; exit 2 ;;
esac
