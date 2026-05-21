#!/bin/sh
# fix-scarlett-routing.sh
#
# Idempotent ALSA routing fix for the Focusrite Scarlett 18i20.
#
# Bug: after avr-calibration container restart, the 18i20's PCM-to-Line
# output routing for channels 5–10 can revert from "PCM N" (USB direct from
# CamillaDSP) to "Analogue N" (hardware passthrough of the corresponding
# analog input) — silently breaking the calibration signal path. Symptoms:
# measure() returns SNR ~0 dB even at master_gain=0, FIR/PEQ filters appear
# inert, cuts deliver near-zero effect at the listener.
#
# Fix: re-set PCM 0N → "PCM N" on each container start. No-op when:
#   - the Scarlett isn't present (no `USB` card)
#   - the routing is already correct (amixer sset is idempotent)
#
# Reference: memory/feedback_scarlett_pcm_resets_on_restart.md
#
# Exit code: always 0 (we never want to take down the entrypoint over this).

set -u

# Channels that carry calibration / playback traffic on this rig.
# (Channels 1–4 on the 18i20 are slaved to the Monitor 1/2 hardware knobs;
# see memory/project_focusrite_monitor_gotcha.md. The 18i20 simple-mixer
# only exposes PCM 01–09; channel 10 is not a switchable output here.)
CHANNELS="5 6 7 8 9"

# Wait up to ~10s for the USB card to enumerate.
ATTEMPTS=20
SLEEP=0.5

scarlett_present() {
    amixer -c USB scontrols 2>/dev/null | grep -q "PCM 0"
}

i=0
while [ "$i" -lt "$ATTEMPTS" ]; do
    if scarlett_present; then
        break
    fi
    i=$((i + 1))
    sleep "$SLEEP"
done

if ! scarlett_present; then
    echo "[fix-scarlett-routing] Scarlett (USB card) not present after ${ATTEMPTS} attempts — skipping." >&2
    exit 0
fi

echo "[fix-scarlett-routing] Scarlett detected — re-asserting PCM 0N -> PCM N..." >&2

OK=0
FAIL=0
for ch in $CHANNELS; do
    src="PCM 0${ch}"
    target="PCM ${ch}"

    # Skip silently if the control doesn't exist on this device variant.
    if ! amixer -c USB sget "$src" >/dev/null 2>&1; then
        echo "[fix-scarlett-routing]   ${src} not present — skipping" >&2
        continue
    fi

    if amixer -c USB sset "$src" "$target" >/dev/null 2>&1; then
        cur=$(amixer -c USB sget "$src" 2>/dev/null | awk -F\' '/Item0/ {print $2}')
        if [ "$cur" = "$target" ]; then
            echo "[fix-scarlett-routing]   ${src} -> ${target} OK" >&2
            OK=$((OK + 1))
        else
            echo "[fix-scarlett-routing]   ${src} -> ${target} FAILED (now: '${cur}')" >&2
            FAIL=$((FAIL + 1))
        fi
    else
        echo "[fix-scarlett-routing]   ${src} -> ${target} sset failed" >&2
        FAIL=$((FAIL + 1))
    fi
done

echo "[fix-scarlett-routing] Done — ${OK} ok, ${FAIL} failed." >&2

# Karaoke summing matrix: Mix A → Line 1 (Denon L), Mix B → Line 2 (Denon R).
# Each Mix bus sums Mic 1 (Mixer In 01) + Mic 2 (Mixer In 02) + Pi karaoke L/R
# (Mixer In 03 = PCM 1, Mixer In 04 = PCM 2). Slot 25's source enum is forced
# to Off (was sticking on "Analogue 9", dumping unwanted signal into karaoke
# output and surfacing as the input-3 buzz reported 2026-05-18).
#
# Mix A: 01,02,03 hot (160 = 0 dB); 04 + 05..25 muted (0 = -80 dB)
# Mix B: 01,02,04 hot; 03 + 05..25 muted
echo "[fix-scarlett-routing] Asserting Mix A/B karaoke summing matrix..." >&2
# Mixer Input 25 source -> Off  (numid=103 on Scarlett 18i20 3rd Gen driver)
amixer -c USB cset numid=103 0 >/dev/null 2>&1 || true
for slot in 01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25; do
    case "$slot" in
        01|02|03) a_val=160 ;;
        *)        a_val=0   ;;
    esac
    case "$slot" in
        01|02|04) b_val=160 ;;
        *)        b_val=0   ;;
    esac
    amixer -c USB sset "Mix A Input $slot" "$a_val" >/dev/null 2>&1 || true
    amixer -c USB sset "Mix B Input $slot" "$b_val" >/dev/null 2>&1 || true
done
echo "[fix-scarlett-routing] Mix A/B asserted." >&2

exit 0
