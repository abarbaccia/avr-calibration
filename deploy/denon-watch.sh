#!/usr/bin/env bash
# denon-watch — flip audio-mode based on Denon AVR state.
#
# Polls the Denon's current input source (SI?) via telnet on port 23.
# When the input matches KARAOKE_TRIGGER_INPUT, set audio-mode=karaoke.
# Otherwise set audio-mode=listening.
#
# AVR power state is honored: if PW=STANDBY, treated as "not karaoke"
# (input value at standby is whatever was last selected and not meaningful).
#
# Config: /etc/audio-mode.conf
#   DENON_HOST=192.168.1.209           # AVR IP
#   KARAOKE_TRIGGER_INPUT=MPLAY        # Denon SI code that means karaoke
#                                      # Empty/unset = feature disabled
#   POLL_INTERVAL_S=10
#
# Manual override: `audio-mode set <mode>` is always respected; the bridge
# only flips when the Denon state DIFFERS from the current audio-mode AND
# the trigger config is set.

set -u

CONFIG_FILE="${CONFIG_FILE:-/etc/audio-mode.conf}"
# /var/lib (persists across reboots) is the single source of truth written by
# audio-mode + read by camilladsp-watchdog. The old /run path was tmpfs and
# audio-mode stopped writing it, so denon-watch saw "unknown" and fought the mode.
STATE_FILE="${STATE_FILE:-/var/lib/audio-mode}"
AUDIO_MODE_BIN="${AUDIO_MODE_BIN:-/usr/local/sbin/audio-mode}"

# Defaults — overridden by /etc/audio-mode.conf if present.
DENON_HOST="${DENON_HOST:-192.168.1.209}"
KARAOKE_TRIGGER_INPUT="${KARAOKE_TRIGGER_INPUT:-}"
POLL_INTERVAL_S="${POLL_INTERVAL_S:-10}"

# shellcheck source=/dev/null
[ -r "$CONFIG_FILE" ] && . "$CONFIG_FILE"

log() { echo "[denon-watch] $*" >&2; }

if [ -z "$KARAOKE_TRIGGER_INPUT" ]; then
    log "KARAOKE_TRIGGER_INPUT not set in $CONFIG_FILE — bridge disabled"
    log "Set KARAOKE_TRIGGER_INPUT=<SI-code> (e.g. MPLAY) to enable."
    # Stay alive so systemd doesn't restart-loop us. Sleep long.
    while true; do sleep 3600; done
fi

log "starting (host=$DENON_HOST trigger=SI$KARAOKE_TRIGGER_INPUT poll=${POLL_INTERVAL_S}s)"

# Returns (via stdout) "POWER:<ON|STANDBY|UNKNOWN> INPUT:<code>"
probe_denon() {
    python3 - "$DENON_HOST" <<'PY' 2>/dev/null
import socket, sys, time
host = sys.argv[1]
try:
    s = socket.create_connection((host, 23), timeout=3)
    s.settimeout(2)
    for q in ("PW?", "SI?"):
        s.sendall((q + "\r").encode())
        time.sleep(0.4)
    buf = b""
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk: break
            buf += chunk
    except Exception:
        pass
    s.close()
    text = buf.decode(errors="ignore")
    power = "UNKNOWN"
    inp = ""
    for line in text.split("\r"):
        line = line.strip()
        if line.startswith("PW"):
            power = line[2:] or "UNKNOWN"
        elif line.startswith("SI"):
            inp = line[2:]
    print(f"POWER:{power} INPUT:{inp}")
except Exception:
    sys.exit(1)
PY
}

current_audio_mode() {
    [ -r "$STATE_FILE" ] && cat "$STATE_FILE" 2>/dev/null || echo "unknown"
}

while true; do
    state="$(probe_denon)"
    if [ -z "$state" ]; then
        # AVR unreachable. Don't change anything — could be a transient blip.
        sleep "$POLL_INTERVAL_S"
        continue
    fi

    power="$(printf '%s' "$state" | sed -n 's/.*POWER:\([A-Z]*\).*/\1/p')"
    input="$(printf '%s' "$state" | sed -n 's/.*INPUT:\([A-Z0-9]*\).*/\1/p')"
    cur="$(current_audio_mode)"

    want="listening"
    if [ "$power" = "ON" ] && [ "$input" = "$KARAOKE_TRIGGER_INPUT" ]; then
        want="karaoke"
    fi

    # Don't touch cal / cal-hdmi — those are manual/recipe territory and a flip
    # mid-sweep would corrupt a calibration run.
    if [ "$cur" = "cal" ] || [ "$cur" = "cal-hdmi" ]; then
        sleep "$POLL_INTERVAL_S"
        continue
    fi

    if [ "$cur" != "$want" ]; then
        log "Denon PW=$power SI=$input → switching $cur → $want"
        "$AUDIO_MODE_BIN" set "$want" || log "audio-mode set $want failed (rc=$?)"
    fi

    sleep "$POLL_INTERVAL_S"
done
