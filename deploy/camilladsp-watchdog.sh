#!/usr/bin/env bash
# CamillaDSP + avr-calibration liveness watchdog.
#
# Self-heals CamillaDSP when its audio thread dies but the process stays
# alive (systemd reports "active" but no PCM flows — the failure mode that
# silenced the subs for two days on 2026-05-13). Also drives the Pi 5's
# onboard ACT/PWR LEDs as a glanceable health indicator and writes a
# status JSON for any future dashboard.
#
# LED states (audio-mode driven, health overlay):
#   listening : ACT solid green, PWR off
#   cal       : ACT heartbeat green (slow blink) — calibration in progress
#   karaoke   : ACT off, PWR solid red
#   camilladsp stalled : ACT off, PWR fast-blink red  (overrides mode; CamillaDSP
#                        now runs in karaoke too, so a stall is healed there as well)
#   avr-calibration container down : ACT heartbeat, PWR on (overlays listening only)
#
# Run as root via camilladsp-watchdog.service.

set -u

POLL_INTERVAL_S="${POLL_INTERVAL_S:-15}"
CAMILLADSP_WS="${CAMILLADSP_WS:-ws://127.0.0.1:1234}"
AVR_HEALTH_URL="${AVR_HEALTH_URL:-http://127.0.0.1:8000/health}"
STATUS_FILE="${STATUS_FILE:-/run/avr-status.json}"
LED_ACT="/sys/class/leds/ACT"
LED_PWR="/sys/class/leds/PWR"
STALL_RESTART_THRESHOLD="${STALL_RESTART_THRESHOLD:-2}"
# Don't restart camilladsp.service for stalls within this many seconds of it
# becoming active — at boot the PW graph + capture wiring take longer than one
# poll cycle to converge, and a premature restart wedges the graph.
BOOT_GRACE_S="${BOOT_GRACE_S:-180}"

stall_count=0

log() { echo "[watchdog] $*" >&2; }

set_led() {
    local led="$1" trigger="$2" brightness="$3"
    [ -d "$led" ] || return 0
    echo "$trigger"    > "$led/trigger"    2>/dev/null || true
    echo "$brightness" > "$led/brightness" 2>/dev/null || true
}

set_led_state() {
    case "$1" in
        listening)
            set_led "$LED_ACT" default-on 1
            set_led "$LED_PWR" none 0
            ;;
        cal)
            set_led "$LED_ACT" heartbeat 1
            set_led "$LED_PWR" none 0
            ;;
        karaoke)
            set_led "$LED_ACT" none 0
            set_led "$LED_PWR" default-on 1
            ;;
        avr_down)
            # Overlays listening: ACT heartbeats, PWR stays off.
            set_led "$LED_ACT" heartbeat 1
            set_led "$LED_PWR" none 0
            ;;
        camilla_stalled)
            # Hard failure indicator: both off + PWR fast-blink.
            set_led "$LED_ACT" none 0
            set_led "$LED_PWR" timer 1
            # Fast blink: 150 ms on / 150 ms off
            echo 150 > "$LED_PWR/delay_on"  2>/dev/null || true
            echo 150 > "$LED_PWR/delay_off" 2>/dev/null || true
            ;;
    esac
}

probe_camilladsp() {
    # Returns 0 if CamillaDSP responds with state=Running, 1 otherwise.
    # Uses python3 + websockets (already a CamillaDSP-stack dependency).
    python3 - "$CAMILLADSP_WS" <<'PY' 2>/dev/null
import asyncio, json, sys
try:
    import websockets
except ImportError:
    sys.exit(2)

async def go(url):
    try:
        async with websockets.connect(url, open_timeout=3, close_timeout=2) as ws:
            await ws.send('"GetState"')
            resp = await asyncio.wait_for(ws.recv(), timeout=3)
            data = json.loads(resp)
            state = data.get("GetState", {}).get("value")
            # Healthy states: Running (audio flowing), Starting (waiting for capture
            # to deliver data — normal when LFE is silent), Paused (intentional).
            # Failure states: Stalled, Inactive, anything unknown.
            sys.exit(0 if state in ("Running", "Starting", "Paused") else 1)
    except Exception:
        sys.exit(1)

asyncio.run(go(sys.argv[1]))
PY
}

probe_avr_calibration() {
    curl --silent --fail --max-time 3 "$AVR_HEALTH_URL" >/dev/null 2>&1
}

write_status() {
    local camilla="$1" avr="$2" led="$3"
    cat > "$STATUS_FILE" <<EOF
{
  "ts": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "camilladsp": "$camilla",
  "avr_calibration": "$avr",
  "led_state": "$led",
  "stall_count": $stall_count
}
EOF
}

# audio-mode state file. /var/lib survives reboots — the watchdog previously
# read /run/audio-mode, which audio-mode stopped writing when its state moved
# to /var/lib (the stale path silently disabled karaoke-awareness).
AUDIO_MODE_FILE="${AUDIO_MODE_FILE:-/var/lib/audio-mode}"

read_audio_mode() {
    [ -r "$AUDIO_MODE_FILE" ] && cat "$AUDIO_MODE_FILE" 2>/dev/null || echo "unknown"
}

log "starting (poll=${POLL_INTERVAL_S}s, stall-restart=${STALL_RESTART_THRESHOLD})"

while true; do
    # CamillaDSP runs in ALL modes now — karaoke included (PipeWire mixes the kiosk
    # audio into the Scarlett alongside CamillaDSP, so the daemon no longer has to be
    # stopped to free the device). So karaoke falls through the normal probe + wire
    # self-heal path: `audio-mode wire` (no arg) re-derives the karaoke links from the
    # state file. The karaoke LED state is still selected below by audio_mode.
    if probe_camilladsp; then
        camilla="running"
        stall_count=0
        # PW-graph self-heal: camilladsp_capture links die whenever the capture
        # node's ports are recreated (CamillaDSP or WirePlumber restart), and
        # WirePlumber never relinks them (node.autoconnect=false by design).
        # audio-mode wire is idempotent and fast when links already exist.
        # WIRE_RETRIES=1: in this healthy path the ports must already exist.
        WIRE_RETRIES=1 /usr/local/sbin/audio-mode wire >/dev/null 2>&1 \
            || log "audio-mode wire failed (will retry next poll)"
    else
        camilla="stalled"
        stall_count=$((stall_count + 1))
        log "CamillaDSP not Running (consecutive: $stall_count)"
        # A "not Running" daemon at boot is usually just UNWIRED: its capture
        # (input_3 ← avr_cal_sweep:monitor) hasn't been linked yet, so it never
        # leaves Starting. Restarting it here only wedges the graph (verified
        # 2026-06-13: a mid-init restart left the null sink a detached driver and
        # hung the daemon in 'deactivating'). So: try to WIRE it first — that is
        # what actually lets it reach Running — and only restart as a last resort,
        # and never during the boot grace window while it is still converging.
        WIRE_RETRIES=2 /usr/local/sbin/audio-mode wire >/dev/null 2>&1 || true
        svc_started=$(date -d "$(systemctl show camilladsp.service -p ActiveEnterTimestamp --value 2>/dev/null)" +%s 2>/dev/null || echo 0)
        svc_uptime=$(( $(date +%s) - svc_started ))
        if [ "$stall_count" -ge "$STALL_RESTART_THRESHOLD" ] && [ "$svc_started" -gt 0 ] && [ "$svc_uptime" -ge "$BOOT_GRACE_S" ]; then
            log "restarting camilladsp.service after $stall_count consecutive stalls (uptime ${svc_uptime}s)"
            systemctl restart camilladsp || log "systemctl restart failed"
            # Give it a moment before next probe; don't reset stall_count here,
            # the next successful probe will. If restart didn't help, we stay red.
            sleep 5
        elif [ "$stall_count" -ge "$STALL_RESTART_THRESHOLD" ]; then
            log "stalled but within boot grace (uptime ${svc_uptime}s < ${BOOT_GRACE_S}s) — wiring, not restarting"
        fi
    fi

    if probe_avr_calibration; then
        avr="up"
    else
        avr="down"
    fi

    audio_mode="$(read_audio_mode)"

    if [ "$camilla" = "stalled" ]; then
        led_state="camilla_stalled"
    elif [ "$audio_mode" = "karaoke" ]; then
        led_state="karaoke"
    elif [ "$audio_mode" = "cal" ]; then
        led_state="cal"
    elif [ "$avr" = "down" ]; then
        led_state="avr_down"
    else
        led_state="listening"
    fi

    set_led_state "$led_state"
    write_status "$camilla" "$avr" "$led_state"

    sleep "$POLL_INTERVAL_S"
done
