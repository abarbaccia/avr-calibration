"""Empirical probe for SET_DISFIL / INIT_COEFS / SET_COEFDT acceptance rules.

We don't have ground-truth bytes from a packet capture, so probe the AVR
directly: send synthetic payloads with varying shapes and log ACK/NACK.
NACK is information; the AVR can be power-cycled if it locks up.

Variants tested (in one TCP session):

  A. SET_DISFIL with empty FilData/DispData arrays
  B. SET_DISFIL with arrays of varying lengths (256, 512, 1024, 2048)
  C. INIT_COEFS with empty body, with simple JSON, with channel selector
  D. SET_COEFDT with N float32 zeros for FL/44.1k/Audy at varying N
     (128, 256, 512, 768, 1024, 2048, 4096)
  E. SET_COEFDT with same payload but Q31-scaled and Q36-scaled — see if
     amplitude meaningfully different gives different ACK behavior (some
     firmwares range-check)
  F. Multi-packet SET_COEFDT (split a 1024-int payload into 8x128-chunks
     with cur_pkt/tot_pkts populated) — verify packet ordering rules

USAGE
    python audyssey_coefdt_probe.py 192.168.1.209
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
CHUNK_INTS = 128


def build_frame(cmd: str, data: bytes = b"", cur: int = 0, tot: int = 0) -> bytes:
    if len(cmd) != CMD_LEN:
        raise ValueError(f"command must be {CMD_LEN} ascii chars, got {cmd!r}")
    cmd_bytes = cmd.encode("ascii")
    total_len = HEADER_LEN + CMD_LEN + len(data)
    buf = bytearray()
    buf.append(ord("T"))
    buf += struct.pack(">H", total_len)
    buf.append(cur & 0xFF)
    buf.append(tot & 0xFF)
    buf += cmd_bytes
    buf.append(0x00)
    buf += struct.pack(">H", len(data))
    buf += data
    buf.append(sum(buf) & 0xFF)
    return bytes(buf)


def parse_frames(stream: bytearray) -> list[dict]:
    out: list[dict] = []
    while len(stream) >= 19:
        if stream[0] not in (ord("T"), ord("R")):
            del stream[0]
            continue
        total_len = struct.unpack(">H", bytes(stream[1:3]))[0]
        if total_len < 19 or total_len > 0xFFFF:
            del stream[0]
            continue
        if len(stream) < total_len:
            break
        cur = stream[3]
        tot = stream[4]
        cmd = bytes(stream[5:15]).decode("ascii", errors="replace").strip()
        dlen = struct.unpack(">H", bytes(stream[16:18]))[0]
        data = bytes(stream[18:18 + dlen])
        out.append({"cmd": cmd, "data": data, "cur": cur, "tot": tot, "dir": chr(stream[0])})
        del stream[:total_len]
    return out


# Bitfield
HEADER_FL_441_AUDY = 0x000 | 0x10000 | 0x00000000


def f32_bits(values: list[float]) -> bytes:
    """Pack as float32, then read back as big-endian Int32 array bytes."""
    if not values:
        return b""
    return struct.pack(f">{len(values)}f", *values)


def q31_ints(values: list[float]) -> bytes:
    """Pack as scaled Int32 (value * 0x7FFFFFFF), big-endian."""
    if not values:
        return b""
    out = []
    for v in values:
        s = round(v * 0x7FFFFFFF)
        s = max(-0x80000000, min(0x7FFFFFFF, s))
        out.append(s)
    return struct.pack(f">{len(out)}i", *out)


def q36_ints(values: list[float]) -> bytes:
    """Pack as scaled Int32 (value * 0xFFFFFFFFF, ratbuddyssey-literal), clipped."""
    if not values:
        return b""
    out = []
    for v in values:
        s = round(v * 0xFFFFFFFFF)
        s = max(-0x80000000, min(0x7FFFFFFF, s))
        out.append(s)
    return struct.pack(f">{len(out)}i", *out)


class Probe:
    def __init__(self, host: str, port: int, timeout: float = 6.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.rxbuf = bytearray()
        self.results: list[dict] = []

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(2.0)

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def _drain(self, seconds: float) -> None:
        assert self.sock is not None
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                self.sock.settimeout(max(0.05, end - time.monotonic()))
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return
            if not chunk:
                return
            self.rxbuf.extend(chunk)

    def send_and_wait(self, label: str, cmd: str, data: bytes = b"", cur: int = 0, tot: int = 0,
                       wait: float = 1.5) -> dict:
        assert self.sock is not None
        self.sock.sendall(build_frame(cmd, data, cur, tot))
        self._drain(wait)
        # Find first reply for this cmd after we sent
        comm = "TIMEOUT"
        reply_data = b""
        for f in parse_frames(self.rxbuf):
            if f["dir"] == "R":
                # Try to parse {"Comm": ...}
                try:
                    obj = json.loads(f["data"].decode("ascii", errors="replace"))
                    c = obj.get("Comm") if isinstance(obj, dict) else None
                    if c:
                        comm = c
                        reply_data = f["data"]
                        break
                except (json.JSONDecodeError, UnicodeDecodeError):
                    reply_data = f["data"]
                    comm = "NON_JSON"
                    break
        self.rxbuf.clear()  # reset between probes for clarity
        result = {
            "label": label, "cmd": cmd.strip(),
            "tx_bytes": len(data), "cur": cur, "tot": tot,
            "comm": comm, "rx_body": reply_data[:120],
        }
        self.results.append(result)
        marker = "✓" if comm == "ACK" else ("✗" if comm == "NACK" else "?")
        print(f"  [{marker}] {label:55s} → {comm:8s} (sent {cmd.strip()} {len(data)}B "
              f"cur={cur} tot={tot}, rx={reply_data[:60]!r})")
        return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    p = Probe(args.host, args.port)
    print(f"=== probing {args.host}:{args.port} ===\n")
    p.connect()

    # Open Audyssey session
    p.send_and_wait("00 ENTER_AUDY", "ENTER_AUDY", wait=2.0)

    # ========== SET_DISFIL variants ==========
    print("\n--- SET_DISFIL variants ---")

    # A. Empty FilData/DispData
    body = json.dumps({"EqType": "Audy", "ChData": "FL",
                       "FilData": [], "DispData": []},
                      separators=(",", ":")).encode("ascii")
    p.send_and_wait("A1 DisFil FL/Audy empty arrays", "SET_DISFIL", body)

    # B. Various lengths of zero-filled arrays
    for n in (256, 512, 1024, 2048):
        body = json.dumps({"EqType": "Audy", "ChData": "FL",
                           "FilData": [0] * n, "DispData": [0] * n},
                          separators=(",", ":")).encode("ascii")
        p.send_and_wait(f"B{n} DisFil FL/Audy len={n}", "SET_DISFIL", body)

    # ========== INIT_COEFS variants ==========
    print("\n--- INIT_COEFS variants ---")

    # C1. Empty
    p.send_and_wait("C1 INIT_COEFS empty", "INIT_COEFS")

    # ========== SET_COEFDT length scan, single-packet ==========
    print("\n--- SET_COEFDT length scan (single-packet, float32) ---")

    for n in (32, 63, 64, 127, 128):  # one packet only — must fit in 128 ints (header+127 coeffs)
        coeffs = [0.0] * (n - 1)
        body = struct.pack(">i", HEADER_FL_441_AUDY) + f32_bits(coeffs)
        p.send_and_wait(f"D{n} CoefDt N={n} float32 single-pkt",
                        "SET_COEFDT", body, cur=0, tot=0)

    # ========== Multi-packet SET_COEFDT scan ==========
    print("\n--- SET_COEFDT multi-packet scans (float32 zeros) ---")

    def push_multi(label: str, n_total_ints: int, encoding_fn) -> str:
        """Push N total ints split into 128-int chunks. Return last comm."""
        coeffs = [0.0] * (n_total_ints - 1)
        full_body_bytes = struct.pack(">i", HEADER_FL_441_AUDY) + encoding_fn(coeffs)
        # split into 512-byte chunks
        chunks = [full_body_bytes[i:i + 512] for i in range(0, len(full_body_bytes), 512)]
        tot = len(chunks) - 1
        last_comm = "?"
        for i, chunk in enumerate(chunks):
            r = p.send_and_wait(f"{label} pkt {i}/{tot} ({len(chunk)}B)",
                                "SET_COEFDT", chunk, cur=i, tot=tot)
            last_comm = r["comm"]
            if last_comm == "NACK":
                break  # don't keep flooding
        return last_comm

    # E. Common Audyssey sizes
    for n in (256, 512, 1024, 2048, 4096):
        push_multi(f"E{n} CoefDt N={n} f32", n, f32_bits)

    # F. Same N=1024 with different encodings
    print("\n--- SET_COEFDT encoding scan at N=1024 ---")
    push_multi("F-q31 CoefDt N=1024 Q31", 1024, q31_ints)
    push_multi("F-q36 CoefDt N=1024 Q36", 1024, q36_ints)

    # Close session cleanly
    print("\n--- closing ---")
    p.send_and_wait("99 EXIT_AUDMD", "EXIT_AUDMD")
    p.close()

    # Summary
    print("\n=== SUMMARY ===")
    accepts = [r for r in p.results if r["comm"] == "ACK"]
    rejects = [r for r in p.results if r["comm"] == "NACK"]
    timeouts = [r for r in p.results if r["comm"] == "TIMEOUT"]
    print(f"ACK: {len(accepts)}  NACK: {len(rejects)}  TIMEOUT: {len(timeouts)}")
    print("\nACCEPTED:")
    for r in accepts:
        print(f"  {r['label']}")
    print("\nREJECTED:")
    for r in rejects:
        print(f"  {r['label']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
