#!/bin/bash
# Bridge Scarlett input ch3 (AUX2) -> snd-aloop sink (FL & FR) via PipeWire.
# The measurement engine in the avr-calibration container reads
# hw:Loopback,1,0 as the loopback reference (= the Denon LFE pre-out signal
# tapped electrically at Scarlett input 3, so cross-correlation of the UMIK
# capture against this reference isolates pure acoustic delay from any
# CamillaDSP/USB/Denon processing latency).
#
# This script is invoked by the loopback-ref-link.service systemd user
# unit at PipeWire start. CamillaDSP and the measurement engine can now
# capture the Scarlett concurrently because PipeWire is the single ALSA
# owner — both reach it through the PW graph.
#
# Idempotent: "File exists" from pw-link is treated as success.

set -u
SRC='alsa_input.usb-Focusrite_Scarlett_18i20_USB_P9W3FNX378D03D-00.multichannel-input:capture_AUX2'
SINK='alsa_output.platform-snd_aloop.0.analog-stereo'

link_one() {
  local dst="$1"
  local out
  out=$(pw-link "$SRC" "$dst" 2>&1) && return 0
  echo "$out" | grep -q 'File exists' && return 0
  echo "pw-link failed: $out" >&2
  return 1
}

case "${1:-up}" in
  up)
    for i in $(seq 1 15); do
      if link_one "${SINK}:playback_FL" && link_one "${SINK}:playback_FR"; then
        echo "loopback ref link active"
        exit 0
      fi
      sleep 1
    done
    echo "loopback ref link failed after 15 attempts" >&2
    exit 1
    ;;
  down)
    pw-link -d "$SRC" "${SINK}:playback_FL" 2>/dev/null || true
    pw-link -d "$SRC" "${SINK}:playback_FR" 2>/dev/null || true
    exit 0
    ;;
  *)
    echo "usage: $0 up|down" >&2
    exit 2
    ;;
esac
