#!/usr/bin/env bash
# Capture phone↔AVR Audyssey traffic on the Pi via ARP-spoof MITM.
#
# Run on the Pi. Requires sudo (raw sockets + sysctl).
#
# Usage:
#   sudo bash audyssey_capture.sh <phone_ip> <avr_ip> [iface] [pcap_path]
#
# Example:
#   sudo bash audyssey_capture.sh 192.168.1.96 192.168.1.209 wlan0 /tmp/multeq.pcap
#
# Then on the phone: open MultEQ Editor → connect to AVR → do upload.
# Ctrl-C this script when done. ARP state is restored on exit.

set -euo pipefail

PHONE="${1:?phone_ip required}"
AVR="${2:?avr_ip required}"
IFACE="${3:-wlan0}"
PCAP="${4:-/tmp/multeq-$(date +%Y%m%d-%H%M%S).pcap}"

echo "[setup] phone=$PHONE avr=$AVR iface=$IFACE pcap=$PCAP"

# Verify tools
for tool in arpspoof tcpdump sysctl; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "[setup] missing $tool — apt-get install -y dsniff tcpdump"
        apt-get install -y dsniff tcpdump
        break
    fi
done

# Enable forwarding so we don't black-hole intercepted traffic
PREV_FWD="$(sysctl -n net.ipv4.ip_forward)"
sysctl -w net.ipv4.ip_forward=1 >/dev/null
echo "[setup] ip_forward 1 (was $PREV_FWD)"

# Cleanup
PIDS=()
cleanup() {
    echo
    echo "[cleanup] restoring..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    sleep 1
    sysctl -w net.ipv4.ip_forward="$PREV_FWD" >/dev/null
    echo "[cleanup] ip_forward restored to $PREV_FWD"
    echo "[cleanup] pcap: $PCAP"
    ls -la "$PCAP" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Start tcpdump first so we don't miss any frames
echo "[setup] starting tcpdump → $PCAP"
tcpdump -i "$IFACE" -w "$PCAP" -U "host $AVR and port 1256" >/tmp/tcpdump.log 2>&1 &
PIDS+=($!)
sleep 1

# arpspoof phone↔AVR (two directions)
echo "[setup] arpspoof phone($PHONE) ← AVR($AVR)"
arpspoof -i "$IFACE" -t "$PHONE" "$AVR" >/tmp/arpspoof-1.log 2>&1 &
PIDS+=($!)

echo "[setup] arpspoof AVR($AVR) ← phone($PHONE)"
arpspoof -i "$IFACE" -t "$AVR" "$PHONE" >/tmp/arpspoof-2.log 2>&1 &
PIDS+=($!)

echo
echo "================================================================"
echo "  CAPTURE ACTIVE — open MultEQ Editor on phone and do an upload."
echo "  When complete, press Ctrl-C here."
echo "================================================================"
echo

# Block until user stops us
while true; do sleep 1; done
