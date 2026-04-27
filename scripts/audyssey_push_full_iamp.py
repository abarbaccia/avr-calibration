"""Push full IAmp payload (Distance + ChLevel + Crossover + SpConfig + flags) to AVR.

Implements the IAmp portion of the MultEQ Editor app's TCP upload sequence
(see ratbuddyssey C# source: SetAvrSetAmp). Tested ACK on Denon X3800H.

Findings from 2026-04-27 protocol discovery session
(scripts/audyssey_iamp_variant_tester.py):
  - Per-channel arrays (ChLevel, Crossover, SpConfig) MUST have n_pos=1.
    Sending n_pos=2 or n_pos=3 → NACK from the AVR.
    Distance alone tolerates any n_pos, but combined with the per-channel
    fields all positions must be 1.
  - Crossover all "F" (full range) breaks bass management; use "80" for
    mains (HPF at 80 Hz) and " " for sub.
  - SpConfig: 1/2 → "S" (small), 3 → "E" (sub enabled). Live AVR readback
    showed all non-sub channels as "S" regardless of .ady's
    enSpeakerConnect=1 vs 2.

CAVEAT: this script's IAmp push WIPES Audyssey MultEQ filters even when
ACK'd. To fully replicate the app's upload sequence (which preserves
filters), additionally implement SET_DISFIL + INIT_COEFS + SET_COEFDT
(filter coefficient binary chunks). Until those exist, after running this
script you must re-import the .ady via the MultEQ Editor app to restore
filters. This script is useful for protocol debugging, not yet production
distance-only push.
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time

DEFAULT_PORT = 1256
HEADER_LEN = 9
CMD_LEN = 10


def build_frame(cmd: str, data: bytes = b"") -> bytes:
    if len(cmd) != CMD_LEN:
        raise ValueError(f"command must be {CMD_LEN} ascii chars, got {cmd!r}")
    cmd_bytes = cmd.encode("ascii")
    total_len = HEADER_LEN + CMD_LEN + len(data)
    buf = bytearray()
    buf.append(ord("T"))
    buf += struct.pack(">H", total_len)
    buf += b"\x00\x00"
    buf += cmd_bytes
    buf.append(0)
    buf += struct.pack(">H", len(data))
    buf += data
    buf.append(sum(buf) & 0xFF)
    return bytes(buf)


def parse_frames(stream: bytearray) -> list[dict]:
    frames: list[dict] = []
    while len(stream) >= 19:
        if stream[0] not in (ord("T"), ord("R")):
            del stream[0]
            continue
        total_len = struct.unpack(">H", bytes(stream[1:3]))[0]
        if len(stream) < total_len:
            break
        cmd = bytes(stream[5:15]).decode("ascii", errors="replace")
        data_len = struct.unpack(">H", bytes(stream[16:18]))[0]
        data = bytes(stream[18:18 + data_len])
        frames.append({"cmd": cmd.strip(), "data": data, "dir": chr(stream[0])})
        del stream[:total_len]
    return frames


def crossover_string(rolloff_hz: int) -> str:
    """Map .ady frequencyRangeRolloff Hz to IAmp Crossover string.

    Crossover list per ratbuddyssey: " ", "40", "60", "80", "90", "100",
    "110", "120", "150", "180", "200", "250", "F"

    NOTE: setting Crossover to "F" (full-range) on mains DISABLES bass
    management — sub stops receiving LFE redirected from mains. For most
    home theater setups with sub-managed mains, use "80" not "F".
    """
    if rolloff_hz >= 1000:
        # Mains marked full-range in .ady. For HT setups WITH a sub, use "80"
        # so the AVR keeps mains crossed to the sub. If you genuinely run
        # mains full-range (no sub), pass override_crossover_mains="F".
        return "80"
    if rolloff_hz <= 30:
        return " "
    standards = [40, 60, 80, 90, 100, 110, 120, 150, 180, 200, 250]
    closest = min(standards, key=lambda x: abs(x - rolloff_hz))
    return str(closest)


def spconfig_string(en_speaker_connect: int | None) -> str:
    """Map channelReport.enSpeakerConnect int to IAmp SpConfig string.

    Channel setup list: "L", "N", "S", "E"
    Best-guess mapping based on common Audyssey conventions:
      1 → "S" (small)
      2 → "L" (large)
      3 → "E" (enabled, used for sub channels)
      0/null → "N" (not present)
    """
    if en_speaker_connect is None:
        return "N"
    return {0: "N", 1: "S", 2: "S", 3: "E"}.get(en_speaker_connect, "S")


def build_iamp_payload(ady: dict, sw1_meters_override: float | None = None) -> dict:
    """Build the IAmp JSON payload from a parsed .ady dict.

    Returns: {
      "Distance":  [{ch: cm, ...}, ...],
      "ChLevel":   [{ch: int_dbx2, ...}, ...],
      "Crossover": [{ch: str, ...}, ...],
      "SpConfig":  [{ch: str, ...}, ...],
      "AudyDynEq": bool,
      "AudyEqRef": int,
    }

    All per-channel arrays have n_pos=1; the AVR rejects n_pos > 1 when
    Distance is sent alongside the per-channel fields. Distance also uses
    n_pos=1 for the same reason (must match the others when combined).
    """
    channels = ady.get("detectedChannels", [])
    n_positions = 1  # AVR rejects > 1 in combined IAmp payload

    distance: dict[str, int] = {}
    chlevel: dict[str, int] = {}
    crossover: dict[str, str] = {}
    spconfig: dict[str, str] = {}

    for ch in channels:
        cid = ch.get("commandId")
        if not cid:
            continue
        meters = float(ch.get("customDistance") or 0.0)
        if cid == "SW1" and sw1_meters_override is not None:
            meters = sw1_meters_override
        distance[cid] = round(meters * 100)

        trim_db = float(ch.get("trimAdjustment") or 0.0)
        chlevel[cid] = round(trim_db * 2)  # int in 0.5 dB steps

        rolloff = int(ch.get("frequencyRangeRolloff") or 80)
        crossover[cid] = crossover_string(rolloff)

        cr = ch.get("channelReport") or {}
        spconfig[cid] = spconfig_string(cr.get("enSpeakerConnect"))

    return {
        "Distance":  [dict(distance) for _ in range(n_positions)],
        "ChLevel":   [dict(chlevel) for _ in range(n_positions)],
        "Crossover": [dict(crossover) for _ in range(n_positions)],
        "SpConfig":  [dict(spconfig) for _ in range(n_positions)],
        "AudyDynEq": bool(ady.get("dynamicEq", False)),
        "AudyEqRef": 0,  # 0 = "Audy" reference (curve mode)
    }


def push(host: str, port: int, payload: dict, commit: bool, timeout: float = 6.0) -> None:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(2.0)
    rxbuf = bytearray()

    def drain(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                sock.settimeout(max(0.05, end - time.monotonic()))
                chunk = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                break
            if not chunk:
                break
            rxbuf.extend(chunk)

    try:
        sock.sendall(build_frame("ENTER_AUDY"))
        drain(1.5)
        for f in parse_frames(rxbuf):
            print(f"  [rx] {f['cmd']!r} dir={f['dir']} datalen={len(f['data'])} body={f['data'][:120]!r}")
        rxbuf.clear()

        body = json.dumps(payload, separators=(",", ":")).encode()
        print(f"\n[tx] SET_SETDAT (full IAmp, {len(body)} bytes):")
        print(f"     {body[:500].decode()}{'...' if len(body) > 500 else ''}")
        sock.sendall(build_frame("SET_SETDAT", body))
        drain(3.0)
        for f in parse_frames(rxbuf):
            print(f"  [rx] {f['cmd']!r} dir={f['dir']} datalen={len(f['data'])} body={f['data'][:120]!r}")
        rxbuf.clear()

        if commit:
            commit_body = b'{"AudyFinFlg":"Fin"}'
            print(f"\n[tx] SET_SETDAT (commit) body={commit_body!r}")
            sock.sendall(build_frame("SET_SETDAT", commit_body))
            drain(3.0)
            for f in parse_frames(rxbuf):
                print(f"  [rx] {f['cmd']!r} dir={f['dir']} datalen={len(f['data'])} body={f['data'][:120]!r}")
            rxbuf.clear()

        sock.sendall(build_frame("EXIT_AUDMD"))
        drain(1.0)
        for f in parse_frames(rxbuf):
            print(f"  [rx] {f['cmd']!r} dir={f['dir']} datalen={len(f['data'])} body={f['data'][:120]!r}")
    finally:
        sock.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help=".ady source file")
    ap.add_argument("host", help="AVR IP/hostname")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--sw1-meters", type=float, default=None,
                    help="override SW1 customDistance (meters)")
    ap.add_argument("--commit", action="store_true", help="send AudyFinFlg=Fin commit")
    ap.add_argument("--dry-run", action="store_true", help="print payload, don't connect")
    args = ap.parse_args()

    with open(args.src) as f:
        ady = json.load(f)

    payload = build_iamp_payload(ady, sw1_meters_override=args.sw1_meters)
    print(f"=== IAmp payload ({len(payload['Distance'])} positions) ===")
    print(json.dumps(payload, indent=2))
    print()

    if args.dry_run:
        return 0

    push(args.host, args.port, payload, commit=args.commit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
