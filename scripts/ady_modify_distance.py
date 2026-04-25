"""Edit a single channel's customDistance in a Denon .ady file.

Reads SRC, sets the named channel's customDistance to METERS, writes DST.
Pure JSON edit — no other fields touched.
"""
from __future__ import annotations
import json
import sys


def main() -> int:
    if len(sys.argv) != 5:
        print("usage: ady_modify_distance.py SRC DST CHANNEL METERS", file=sys.stderr)
        return 2
    src, dst, channel, meters_s = sys.argv[1:]
    meters = float(meters_s)
    with open(src) as f:
        d = json.load(f)
    found = False
    for ch in d.get("detectedChannels", []):
        if ch.get("commandId") == channel:
            old = ch.get("customDistance")
            ch["customDistance"] = meters
            print(f"{channel}: customDistance {old} -> {meters}")
            found = True
            break
    if not found:
        print(f"channel {channel!r} not found", file=sys.stderr)
        return 1
    with open(dst, "w") as f:
        json.dump(d, f, separators=(",", ":"))
    print(f"wrote {dst} ({len(json.dumps(d))} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
