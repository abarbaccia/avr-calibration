"""Stage-1 probe: read-only Audyssey MultEQ Editor TCP dump.

Connects to a Denon/Marantz AVR on port 1256, sends ENTER_AUDY then GET_AVRINF,
parses framed responses, dumps the JSON state (including current channel
distances), then EXIT_AUDMD. Pure read — no state change.

Wire format (per LaserGuruGuy/ratbuddyssey, MultEqTcp/AudysseyMultEQAvrTcpClient.cs):

  TX/RX frame:
    'T'                 1 byte    header marker
    total_len           2 bytes   big-endian, full frame length incl checksum
    current_packet      1 byte    0 for single-packet payloads
    total_packets       1 byte    0 for single-packet payloads
    command             10 bytes  ASCII, fixed width (e.g. b'GET_AVRINF')
    reserved/null       1 byte    0x00
    data_len            2 bytes   big-endian, length of payload
    data                N bytes   payload (usually JSON or int32 array)
    checksum            1 byte    sum of all preceding bytes mod 256

  All commands ratbuddyssey uses are exactly 10 ASCII chars.
"""

from __future__ import annotations

import json
import socket
import struct
import sys
import time
from dataclasses import dataclass

DEFAULT_HOST = "192.168.1.209"
DEFAULT_PORT = 1256
HEADER_LEN = 9  # 'T' + total_len(2) + cur_pkt + tot_pkt + null(1) + data_len(2)
CMD_LEN = 10


def _checksum(buf: bytes) -> int:
    return sum(buf) & 0xFF


def build_frame(cmd: str, data: bytes = b"", current_packet: int = 0, total_packets: int = 0) -> bytes:
    if len(cmd) != CMD_LEN:
        raise ValueError(f"command must be exactly {CMD_LEN} ASCII chars, got {cmd!r}")
    cmd_bytes = cmd.encode("ascii")
    total_len = HEADER_LEN + CMD_LEN + len(data)
    buf = bytearray()
    buf.append(ord("T"))
    buf += struct.pack(">H", total_len)
    buf.append(current_packet & 0xFF)
    buf.append(total_packets & 0xFF)
    buf += cmd_bytes
    buf.append(0x00)
    buf += struct.pack(">H", len(data))
    buf += data
    buf.append(_checksum(buf))
    return bytes(buf)


@dataclass
class Frame:
    direction: str  # 'T' or 'R'
    command: str
    data: bytes
    current_packet: int
    total_packets: int


def parse_frames(stream: bytearray) -> list[Frame]:
    """Drain complete frames from the front of `stream`, leaving any partial bytes."""
    frames: list[Frame] = []
    while len(stream) >= 19:
        if stream[0] not in (ord("T"), ord("R")):
            # resync: drop one byte, try again
            del stream[0]
            continue
        total_len = struct.unpack(">H", bytes(stream[1:3]))[0]
        frame_total = total_len  # total_len already includes the checksum byte
        if len(stream) < frame_total:
            break  # incomplete
        frame_bytes = bytes(stream[:frame_total])
        body = frame_bytes[:-1]
        ck_actual = frame_bytes[-1]
        ck_expected = _checksum(body)
        if ck_actual != ck_expected:
            print(
                f"[warn] checksum mismatch (got {ck_actual:#x}, want {ck_expected:#x}); skipping",
                file=sys.stderr,
            )
            del stream[0]
            continue
        direction = chr(frame_bytes[0])
        cur = frame_bytes[3]
        tot = frame_bytes[4]
        cmd = frame_bytes[5:15].decode("ascii", errors="replace")
        data_len = struct.unpack(">H", frame_bytes[16:18])[0]
        data = frame_bytes[18 : 18 + data_len]
        frames.append(Frame(direction=direction, command=cmd, data=data, current_packet=cur, total_packets=tot))
        del stream[:frame_total]
    return frames


def reassemble(frames: list[Frame]) -> dict[str, bytes]:
    """Group multi-packet responses by command; return cmd -> concatenated data."""
    pending: dict[str, dict[int, bytes]] = {}
    expected_totals: dict[str, int] = {}
    completed: dict[str, bytes] = {}
    for f in frames:
        if f.total_packets == 0:
            completed[f.command] = f.data
            continue
        slot = pending.setdefault(f.command, {})
        slot[f.current_packet] = f.data
        expected_totals[f.command] = f.total_packets
        # ratbuddyssey expects total_packets+1 segments (0..total_packets inclusive)
        if len(slot) == f.total_packets + 1:
            ordered = b"".join(slot[i] for i in sorted(slot))
            completed[f.command] = ordered
            del pending[f.command]
    # also include any partials so we don't silently drop them
    for cmd, slot in pending.items():
        ordered = b"".join(slot[i] for i in sorted(slot))
        completed[cmd + "(partial)"] = ordered
    return completed


def probe(host: str, port: int, timeout: float = 8.0) -> int:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    rxbuf = bytearray()
    all_frames: list[Frame] = []

    def drain(deadline: float) -> None:
        while time.monotonic() < deadline:
            try:
                sock.settimeout(max(0.1, deadline - time.monotonic()))
                chunk = sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return
            if not chunk:
                return
            rxbuf.extend(chunk)
            new = parse_frames(rxbuf)
            for f in new:
                print(
                    f"[rx] dir={f.direction} cmd={f.command.strip()!r:14} "
                    f"pkt={f.current_packet}/{f.total_packets} datalen={len(f.data)}"
                )
            all_frames.extend(new)

    # IAmp query — every value replaced with "?" per ratbuddyssey MakeQuery pattern
    iamp_query = (
        '{"ChLevel":"?","Crossover":"?","Distance":"?","SpConfig":"?",'
        '"AudyFinFlg":"?","AudyDynEq":"?","AudyEqRef":"?"}'
    ).encode("ascii")
    # IAudy query — covers Audyssey-mode runtime fields
    iaudy_query = b'{"EnMultEQ":"?","EnDynEq":"?","DynEqOff":"?","EnDynVol":"?","DynVolSet":"?"}'

    try:
        for cmd, body in (
            ("ENTER_AUDY", b""),
            ("GET_AVRINF", b""),
            ("GET_AVRSTS", b""),
            ("SET_SETDAT", iamp_query),
            ("SET_SETDAT", iaudy_query),
        ):
            print(f"[tx] {cmd} body={body!r}")
            sock.sendall(build_frame(cmd, body))
            drain(time.monotonic() + 3.0)
        drain(time.monotonic() + 2.0)  # final settle
        print("[tx] EXIT_AUDMD")
        sock.sendall(build_frame("EXIT_AUDMD"))
        drain(time.monotonic() + 1.5)
    finally:
        try:
            sock.close()
        except OSError:
            pass

    # group by (command, transmit/receive) and reassemble in arrival order per cmd
    print(f"\n=== {len(all_frames)} frames received ===")
    # Show every distinct response in order
    seen_count: dict[str, int] = {}
    multi: dict[str, list[Frame]] = {}
    for f in all_frames:
        if f.total_packets == 0:
            seen_count[f.command] = seen_count.get(f.command, 0) + 1
            tag = f"{f.command.strip()}#{seen_count[f.command]}"
            print(f"\n--- {tag} ({len(f.data)} bytes) ---")
            _dump(f.data)
        else:
            multi.setdefault(f.command, []).append(f)
    for cmd, frames in multi.items():
        frames.sort(key=lambda x: x.current_packet)
        joined = b"".join(x.data for x in frames)
        print(f"\n--- {cmd.strip()} (multi-packet, {len(frames)} segments, {len(joined)} bytes) ---")
        _dump(joined)


def _dump(data: bytes) -> None:
    try:
        obj = json.loads(data)
        text = json.dumps(obj, indent=2)
        print(text[:6000])
        if len(text) > 6000:
            print(f"... [truncated, full {len(text)} chars]")
        distances = _hunt_distances(obj)
        if distances:
            print("\n  >>> DISTANCE FIELDS <<<")
            for path, val in distances:
                print(f"  {path} = {val}")
    except json.JSONDecodeError:
        print(f"(not JSON; first 400 bytes hex) {data[:400].hex()}")
    return 0


def _hunt_distances(obj, path: str = "$") -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            sub = f"{path}.{k}"
            if isinstance(k, str) and "distance" in k.lower():
                out.append((sub, v))
            out.extend(_hunt_distances(v, sub))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_hunt_distances(v, f"{path}[{i}]"))
    return out


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HOST
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORT
    sys.exit(probe(host, port))
