#!/bin/bash
# DEPRECATED / DISABLED — do not re-enable without understanding the consequences.
#
# Original intent: Bridge Scarlett input ch3 (AUX2 = Denon sub pre-out) to
# snd-aloop as the measurement loopback reference.
#
# Why it's disabled:
#   This contaminated the loopback reference with the Denon-processed signal.
#   When the measurement deconvolves mic vs reference, Denon processing cancels
#   out — making CamillaDSP input EQ (HPF, Harman shelf) invisible to measurements.
#   Root cause analysis 2026-05-25: avr_cal_sweep:monitor is the correct reference
#   (raw pre-processing sweep). The snd-aloop ONLY carries avr_cal_sweep:monitor
#   now, set up by avr-cal-sweep-link.sh.
#
# Loopback reference architecture (current):
#   avr_cal_sweep:monitor_FL/FR → snd-aloop → hw:2,1 (LoopbackRefPlayback)
#   H = CamillaDSP(HPF+shelf) × room × mic  [full DSP chain visible]
#
# The loopback-ref-link.service is disabled. This script is kept for the
# `down` path (to remove stale links if service was re-enabled manually).
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
