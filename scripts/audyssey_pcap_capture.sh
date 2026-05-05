#!/usr/bin/env bash
# Capture Audyssey TCP traffic between the Pi/workstation and AVR while
# the official MultEQ Editor mobile app pushes a calibration. The
# resulting pcap is the ground truth for SET_SETDAT / SET_DISFIL /
# SET_COEFDT / FINZ_COEFS / Fin commit byte sequences.
#
# Usage:
#   On Pi:  ./audyssey_pcap_capture.sh [output.pcap] [duration_seconds]
#   Then on phone: open MultEQ Editor → connect to AVR → "Send to AVR"
#   Wait for confirmation in app → capture stops automatically (or Ctrl+C).
#
# Decode via:
#   git show ea8fd76:scripts/audyssey_pcap_decode.py > /tmp/decode.py
#   python /tmp/decode.py output.pcap 192.168.1.209
set -e
OUT="${1:-/tmp/audyssey-multeq-editor.pcap}"
DURATION="${2:-300}"
AVR_IP="${AVR_IP:-192.168.1.209}"
echo "Capturing Audyssey traffic to/from $AVR_IP:1256 for ${DURATION}s..."
echo "  Output: $OUT"
echo "  Operate the MultEQ Editor mobile app NOW: connect → 'Send to AVR'."
echo ""
sudo timeout "$DURATION" tcpdump -i any "host $AVR_IP and port 1256" -w "$OUT" -s 0 -nn || true
echo ""
echo "Capture complete: $OUT"
sudo chmod a+r "$OUT"
ls -la "$OUT"
echo ""
echo "Decode with:"
echo "  git show ea8fd76:scripts/audyssey_pcap_decode.py > /tmp/decode.py"
echo "  python3 /tmp/decode.py $OUT $AVR_IP"
