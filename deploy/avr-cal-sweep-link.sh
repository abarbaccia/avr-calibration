#!/bin/bash
# avr-cal-sweep PipeWire null sink + persistent link to camilladsp_capture.
#
# Why this exists:
#   After PipeWire migration (v0.2.0) CamillaDSP captures via the PipeWire
#   node `camilladsp_capture` (autoconnected to Scarlett multichannel-input).
#   The avr-calibration container has no way to "play to CamillaDSP" directly
#   — PortAudio inside Docker only sees raw ALSA, and the snd-aloop subdev
#   used as the loopback ref is owned exclusively by PipeWire.
#
#   This service creates a persistent PipeWire null sink named `avr_cal_sweep`
#   and links its monitor ports to camilladsp_capture's input ports. The
#   container plays the calibration sweep to `avr_cal_sweep` (via pipewire-alsa
#   + PIPEWIRE_NODE env), PipeWire pumps the audio into camilladsp_capture,
#   CamillaDSP processes + routes it to the subs.
#
#   It also links the same monitor to the snd-aloop sink so the loopback ref
#   path (Scarlett input ch3 → snd-aloop, bridged by loopback-ref-link.service)
#   captures the sweep electrically as a timing reference during USB-route cal.
#
# Idempotent: re-running the script (or restarting the service) skips
# existing links and a pre-existing null sink without erroring.
#
# Sister service: loopback-ref-link.service (Scarlett ch3 → snd-aloop). That
# bridge is untouched — this service is additive.

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

        # Link the null sink's monitor to CamillaDSP capture (FL, FR -> input_1, input_2).
        ok=1
        link_one "${SINK_NAME}:monitor_FL" "${CAM_CAPTURE}:input_1" || ok=0
        link_one "${SINK_NAME}:monitor_FR" "${CAM_CAPTURE}:input_2" || ok=0

        # Also fan the same monitor into the snd-aloop sink so the loopback
        # ref capture sees the sweep electrically. Failure here is non-fatal:
        # the loopback ref is an optional timing aid, not a hard dependency
        # for sweep delivery.
        link_one "${SINK_NAME}:monitor_FL" "${ALOOP_SINK}:playback_FL" || true
        link_one "${SINK_NAME}:monitor_FR" "${ALOOP_SINK}:playback_FR" || true

        if [ "$ok" = "1" ]; then
            echo "avr-cal-sweep link active (${SINK_NAME} -> ${CAM_CAPTURE})"
            exit 0
        fi
        sleep 1
    done
    echo "avr-cal-sweep link failed after 15 attempts" >&2
    exit 1
}

down() {
    pw-link -d "${SINK_NAME}:monitor_FL" "${CAM_CAPTURE}:input_1" 2>/dev/null || true
    pw-link -d "${SINK_NAME}:monitor_FR" "${CAM_CAPTURE}:input_2" 2>/dev/null || true
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
