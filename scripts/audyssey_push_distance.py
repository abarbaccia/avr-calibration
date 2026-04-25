"""Direct Audyssey TCP write of speaker distance — bypasses MultEQ Editor app.

Reads a .ady source file, builds a SET_SETDAT IAmp payload using the
customDistance values, optionally overrides one channel, and pushes to the AVR.
Commits with {"AudyFinFlg":"Fin"}.

Schema basis: ratbuddyssey C# IAmp interface
(MultEqAvr/AudysseyMultEQAvrAmp.cs). Distance is List<Dict<channel,int_cm>>,
one dict per measurement position.

Usage:
    audyssey_push_distance.py SRC_ADY HOST [--override CHANNEL METERS]

If no override, pushes the .ady customDistance values verbatim.
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time

HEADER_LEN = 9
CMD_LEN = 10


def build_frame(cmd: str, data: bytes = b"") -> bytes:
    cb = cmd.encode("ascii")
    if len(cb) != CMD_LEN:
        raise ValueError(f"command must be {CMD_LEN} chars, got {cmd!r}")
    total = HEADER_LEN + CMD_LEN + len(data)
    buf = bytearray()
    buf.append(ord("T"))
    buf += struct.pack(">H", total)
    buf += b"\x00\x00"
    buf += cb
    buf.append(0x00)
    buf += struct.pack(">H", len(data))
    buf += data
    buf.append(sum(buf) & 0xFF)
    return bytes(buf)


def parse_frames(stream: bytearray) -> list[dict]:
    out = []
    while len(stream) >= 19:
        if stream[0] not in (ord("T"), ord("R")):
            del stream[0]
            continue
        total_len = struct.unpack(">H", bytes(stream[1:3]))[0]
        if len(stream) < total_len:
            break
        frame = bytes(stream[:total_len])
        ck_actual = frame[-1]
        ck_expected = sum(frame[:-1]) & 0xFF
        if ck_actual != ck_expected:
            del stream[0]
            continue
        cmd = frame[5:15].decode("ascii", errors="replace").rstrip("\x00")
        data_len = struct.unpack(">H", frame[16:18])[0]
        data = frame[18 : 18 + data_len]
        out.append({"dir": chr(frame[0]), "cmd": cmd, "data": data,
                    "cur": frame[3], "tot": frame[4]})
        del stream[:total_len]
    return out


def build_distance_payload(
    ady: dict,
    override_channel: str | None,
    override_meters: float | None,
    shift_others_m: float = 0.0,
    shift_skip: tuple[str, ...] = (),
    floor_m: float = 0.0,
) -> dict:
    """Build IAmp.Distance: list of dicts, one per measurement position. Values in cm.

    `shift_others_m`: subtracted from every channel except those in `shift_skip`
    (and except `override_channel`). Result floored at `floor_m`.
    """
    channels = []
    for ch in ady.get("detectedChannels", []):
        cmd_id = ch.get("commandId")
        if cmd_id is None:
            continue
        meters = float(ch.get("customDistance", 0.0))
        if override_channel and cmd_id == override_channel and override_meters is not None:
            meters = override_meters
        elif cmd_id not in shift_skip:
            meters = max(floor_m, meters - shift_others_m)
        cm = round(meters * 100)
        channels.append((cmd_id, cm))

    # number of measurement positions = number of keys in any channel's responseData
    n_pos = 1
    for ch in ady.get("detectedChannels", []):
        rd = ch.get("responseData")
        if isinstance(rd, dict) and rd:
            n_pos = max(n_pos, len(rd))
            break

    # same dict per position (customDistance is per-channel, not per-position)
    pos_dict = {cmd_id: cm for cmd_id, cm in channels}
    distance_list = [dict(pos_dict) for _ in range(n_pos)]
    return {"Distance": distance_list}


def push(host: str, port: int, payload: dict, commit: bool, timeout: float = 6.0) -> None:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    rxbuf = bytearray()

    def drain(seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                sock.settimeout(max(0.05, end - time.monotonic()))
                c = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return
            if not c:
                return
            rxbuf.extend(c)
            for f in parse_frames(rxbuf):
                snippet = f["data"][:200].decode("ascii", errors="replace")
                print(f"  [rx] {f['cmd']} pkt={f['cur']}/{f['tot']} datalen={len(f['data'])} body={snippet!r}")

    try:
        for cmd, body in (
            ("ENTER_AUDY", b""),
        ):
            print(f"[tx] {cmd}")
            sock.sendall(build_frame(cmd, body))
            drain(1.0)

        body_json = json.dumps(payload, separators=(",", ":")).encode("ascii")
        print(f"[tx] SET_SETDAT (Distance, {len(body_json)} bytes):")
        # truncate large body for log
        s = body_json.decode("ascii", errors="replace")
        print(f"     {s if len(s) < 600 else s[:600] + '...'}")
        sock.sendall(build_frame("SET_SETDAT", body_json))
        drain(2.5)

        if commit:
            commit_body = b'{"AudyFinFlg":"Fin"}'
            print(f"[tx] SET_SETDAT (commit) body={commit_body!r}")
            sock.sendall(build_frame("SET_SETDAT", commit_body))
            drain(1.5)

        print("[tx] EXIT_AUDMD")
        sock.sendall(build_frame("EXIT_AUDMD", b""))
        drain(1.0)
    finally:
        sock.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="source .ady file (provides per-channel current customDistance)")
    ap.add_argument("host", help="AVR IP/hostname")
    ap.add_argument("--port", type=int, default=1256)
    ap.add_argument("--override", nargs=2, metavar=("CHANNEL", "METERS"),
                    help="override one channel's distance (e.g. --override SW1 30.72)")
    ap.add_argument("--shift-others", type=float, default=0.0, metavar="METERS",
                    help="subtract this many meters from every channel except --override (and --shift-skip)")
    ap.add_argument("--shift-skip", action="append", default=[], metavar="CHANNEL",
                    help="channel(s) to exclude from --shift-others (repeatable)")
    ap.add_argument("--floor", type=float, default=0.0, metavar="METERS",
                    help="minimum allowed distance after shift (default 0.0)")
    ap.add_argument("--commit", action="store_true", help="send AudyFinFlg=Fin after the data write")
    ap.add_argument("--dry-run", action="store_true", help="print the payload but don't connect")
    args = ap.parse_args()

    with open(args.src) as f:
        ady = json.load(f)

    override_ch = args.override[0] if args.override else None
    override_m = float(args.override[1]) if args.override else None

    skip = tuple(args.shift_skip) + ((override_ch,) if override_ch else ())
    payload = build_distance_payload(
        ady, override_ch, override_m,
        shift_others_m=args.shift_others,
        shift_skip=skip,
        floor_m=args.floor,
    )
    print(f"=== Distance payload ({len(payload['Distance'])} positions) ===")
    print(json.dumps(payload, indent=2))
    print()

    if args.dry_run:
        return 0

    push(args.host, args.port, payload, commit=args.commit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
