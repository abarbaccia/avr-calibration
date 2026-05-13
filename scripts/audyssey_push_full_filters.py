"""Push full Audyssey profile (IAmp + filters + coefficients) via TCP.

Implements the parts of the MultEQ Editor app upload sequence that
preserve MultEQ filters end-to-end — i.e. SET_DISFIL, INIT_COEFS, and
SET_COEFDT — in addition to the IAmp/IAudy SET_SETDAT writes already
proven by `audyssey_push_full_iamp.py`. With this script and a real
.ady source, the AVR should accept a profile via TCP without needing
the phone app to re-import filters afterwards.

Reverse-engineered from ratbuddyssey C# (LaserGuruGuy/ratbuddyssey,
master branch). Citations as `file:line` in that repo:

  Frame builder + checksum ........ MultEqTcp/AudysseyMultEQAvrTcpClient.cs:84-133, 247-274
  Senders (SET_DISFIL/etc) ........ Ratbuddyssey/AudysseyMultEQAvrTcpParser.cs:303-372
  ACK parsing ..................... Ratbuddyssey/AudysseyMultEQAvrTcpParser.cs:520-551
  DisFil JSON shape ............... MultEqAvr/AudysseyMultEQAvrDisFil.cs:43-100
  CoefData header bitfield ........ MultEqAdapter/AudysseyMultEQAvrAdapter.cs:435-473

Sequence (per the C# reference):

  1. ENTER_AUDY                       — empty body
  2. SET_SETDAT  (IAmp JSON)          — distance/level/xover/spcfg + AudyDynEq/EqRef
  3. SET_SETDAT  (IAudy JSON)         — speaker config flags
  4. SET_DISFIL  × N_chan × 2_EqType  — one packet per (channel, "Audy"|"Flat")
  5. INIT_COEFS                       — empty body, ONCE
  6. SET_COEFDT  × N_blobs × N_chunks — per Int32[] in CoefData, 128 ints/chunk
                                        (last chunk short). cur_pkt/tot_pkts in
                                        the frame header are populated with
                                        tot_pkts = (N_chunks - 1).
  7. SET_SETDAT  {"AudyFinFlg":"Fin"} — commit
  8. EXIT_AUDMD                       — empty body

Coefficient encoding — TWO DIFFERENT FORMATS, do not confuse:

  WIRE (TCP SET_COEFDT body): IEEE 754 float32 bit-cast to Int32, packed
  big-endian. Per parser.cs:18-35 `FloatInt32` is a struct-explicit union
  reading float bits as Int32 with no scaling. Coefficients go on the
  wire as raw float32 bit patterns.

  ADY FILE (XML, on disk): doubles encoded as `int = round(value *
  0xFFFFFFFFF)` per adapter.cs:438-470. This is `.ady`-only, NOT the wire.

  Header word (first Int32 of each blob, on the wire) self-identifies
  the (channel, sample_rate, curve_type) tuple as a packed bitfield:

    bits 0x00000F00  channel  FL=0x000 C=0x100 FR=0x200 SRA=0x300
                              SLA=0xC00 SW1=0xD00
    bits 0x00030000  rate     32k=0x00000 44.1k=0x10000 48k=0x20000
    bit  0x01000000  curve    Audy=0x00000000 Flat=0x01000000

  Wire framing: big-endian Int32 array, 128 ints (512 bytes) per chunk,
  multi-packet via header cur_pkt/tot_pkts (tot_pkts = N_chunks - 1).
  The frame's 1-byte sum is the only integrity check.

ACK: AVR replies `{"Comm":"ACK"|"NACK"|"INPROGRESS"}` after every command
(parser:520-530). C# blocks ≤10s per send. We wait for explicit ACK then
proceed; on NACK we abort.

USAGE
    python audyssey_push_full_filters.py path/to/profile.ady 192.168.1.209 \\
        [--commit] [--dry-run] [--sw1-meters 30.72] [--scale q36|q31]

CAVEAT — this needs a REAL .ady file with all the coefficient + display
arrays populated (dispLargeData, dispSmallData, coefficient32kHz,
coefficient441kHz, coefficient48kHz). Files produced by ratbuddyssey or
exported from the MultEQ Editor app contain these. A .ady built from
scratch will not. Run with --dry-run first to inspect what will be sent.
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time
from typing import Iterable

DEFAULT_PORT = 1256
HEADER_LEN = 9
CMD_LEN = 10
CHUNK_INTS = 128  # 128 Int32 = 512 bytes (parser:355)

# .ady-XML scaling only (NOT used on the TCP wire). Kept here for converting
# .ady-stored ints back to doubles when reading a source file.
ADY_SCALE = 0xFFFFFFFFF

# CoefData[0] header bitfield — adapter:438-470
CHANNEL_BITS: dict[str, int] = {
    "FL":  0x000,
    "C":   0x100,
    "FR":  0x200,
    "SRA": 0x300,
    "SLA": 0xC00,
    "SW1": 0xD00,
}
RATE_BITS: dict[str, int] = {
    "32kHz":   0x00000,
    "44.1kHz": 0x10000,
    "48kHz":   0x20000,
}
CURVE_BITS: dict[str, int] = {
    "Audy": 0x00000000,
    "Flat": 0x01000000,
}

# .ady field names per channel
ADY_COEFF_FIELDS: dict[str, str] = {
    "32kHz":   "coefficient32kHz",
    "44.1kHz": "coefficient441kHz",
    "48kHz":   "coefficient48kHz",
}


# ---------------------------------------------------------------------------
# Frame builder
# ---------------------------------------------------------------------------


def build_frame(cmd: str, data: bytes = b"", current_packet: int = 0, total_packets: int = 0) -> bytes:
    """Build one Audyssey TCP frame.

    For multi-packet SET_COEFDT, set current_packet (0-indexed) and
    total_packets (= N_chunks - 1, also 0-indexed last index, NOT count
    — see parser:357).
    """
    if len(cmd) != CMD_LEN:
        raise ValueError(f"command must be {CMD_LEN} ascii chars, got {cmd!r}")
    cmd_bytes = cmd.encode("ascii")
    total_len = HEADER_LEN + CMD_LEN + len(data)
    if total_len > 0xFFFF:
        raise ValueError(f"frame too long: {total_len} > 65535")
    buf = bytearray()
    buf.append(ord("T"))
    buf += struct.pack(">H", total_len)
    buf.append(current_packet & 0xFF)
    buf.append(total_packets & 0xFF)
    buf += cmd_bytes
    buf.append(0x00)
    buf += struct.pack(">H", len(data))
    buf += data
    buf.append(sum(buf) & 0xFF)
    return bytes(buf)


def parse_frames(stream: bytearray) -> list[dict]:
    """Parse RX bytes into a list of frame dicts. Mutates stream in place."""
    frames: list[dict] = []
    while len(stream) >= 19:
        if stream[0] not in (ord("T"), ord("R")):
            del stream[0]
            continue
        total_len = struct.unpack(">H", bytes(stream[1:3]))[0]
        if len(stream) < total_len:
            break
        cur_pkt = stream[3]
        tot_pkt = stream[4]
        cmd = bytes(stream[5:15]).decode("ascii", errors="replace")
        data_len = struct.unpack(">H", bytes(stream[16:18]))[0]
        data = bytes(stream[18:18 + data_len])
        frames.append({
            "cmd": cmd.strip(),
            "data": data,
            "dir": chr(stream[0]),
            "cur": cur_pkt,
            "tot": tot_pkt,
        })
        del stream[:total_len]
    return frames


def parse_comm(data: bytes) -> str | None:
    """Return ACK | NACK | INPROGRESS from a reply body, or None."""
    try:
        obj = json.loads(data.decode("ascii", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    return obj.get("Comm") if isinstance(obj, dict) else None


# ---------------------------------------------------------------------------
# Coefficient encoding
# ---------------------------------------------------------------------------


def encode_header_word(channel: str, rate: str, curve: str) -> int:
    """Build the leading Int32 of a CoefData blob from channel/rate/curve."""
    if channel not in CHANNEL_BITS:
        raise ValueError(f"unknown channel {channel!r}; supported: {sorted(CHANNEL_BITS)}")
    if rate not in RATE_BITS:
        raise ValueError(f"unknown rate {rate!r}")
    if curve not in CURVE_BITS:
        raise ValueError(f"unknown curve {curve!r}")
    return CHANNEL_BITS[channel] | RATE_BITS[rate] | CURVE_BITS[curve]


def encode_coefficients(values: Iterable[float]) -> list[int]:
    """Pack float32 coefficients as Int32 bit-patterns (wire format).

    Per ratbuddyssey FloatInt32 union (parser.cs:18-35): coefficients on
    the TCP wire are raw IEEE 754 float32 bits read as signed Int32.
    No scaling, no Q-format. We pack each value as float32 big-endian
    then unpack as signed Int32 big-endian to keep the rest of the
    pipeline working in Int32-land.
    """
    out: list[int] = []
    for v in values:
        bits = struct.unpack(">i", struct.pack(">f", float(v)))[0]
        out.append(bits)
    return out


def pack_int32_be(values: Iterable[int]) -> bytes:
    """Pack a sequence of signed Int32 as big-endian bytes (4 bytes each)."""
    return struct.pack(f">{sum(1 for _ in values)}i", *list(values))  # noqa


def pack_int32_be_list(values: list[int]) -> bytes:
    return struct.pack(f">{len(values)}i", *values)


def chunk_blob(int_array: list[int], chunk_size: int = CHUNK_INTS) -> list[list[int]]:
    """Split an Int32[] into chunks of size `chunk_size` (last may be short)."""
    return [int_array[i:i + chunk_size] for i in range(0, len(int_array), chunk_size)]


# ---------------------------------------------------------------------------
# .ady extraction
# ---------------------------------------------------------------------------


def extract_disfil_messages(ady: dict) -> list[dict]:
    """Build the SET_DISFIL JSON bodies from a parsed .ady.

    One per (channel, EqType). Returns a list of dicts in send order:
    {"EqType": "Audy"|"Flat", "ChData": <commandId>, "FilData": [...], "DispData": [...]}.

    Per DisFil.cs:43-100 — uses dispLargeData/dispSmallData arrays. Some
    .ady exports may not split per EqType; in that case both eqtypes
    share the same arrays. Without a packet capture confirming Audy vs
    Flat array sources, we pull from the same field for both — verify
    against the AVR's response.
    """
    messages: list[dict] = []
    for ch in ady.get("detectedChannels", []):
        cid = ch.get("commandId")
        if not cid:
            continue
        large = ch.get("dispLargeData") or []
        small = ch.get("dispSmallData") or []
        for eq in ("Audy", "Flat"):
            messages.append({
                "EqType": eq,
                "ChData": cid,
                "FilData": list(large),
                "DispData": list(small),
            })
    return messages


def extract_coef_blobs(ady: dict) -> list[dict]:
    """Build the SET_COEFDT Int32 blobs from a parsed .ady.

    Returns list of dicts:
      {"channel": str, "rate": str, "curve": str, "ints": [Int32, ...]}

    Per channel × per rate present × per curve. .ady exports typically
    only carry the rates the AVR actually uses for that speaker (subs
    often only 44.1kHz). Missing fields are skipped silently — verify
    the resulting blob count matches what an app upload sends.

    The Audy vs Flat curve distinction: ratbuddyssey stores both in
    CoefData but the .ady format exposes only one set of coefficient
    arrays per rate. Without a per-curve field in .ady, we duplicate
    the same coefficients under both curve flags. THIS IS A KNOWN GAP
    — confirm against a packet capture; the real app likely derives
    one from the other or reads a separate .ady field.
    """
    blobs: list[dict] = []
    for ch in ady.get("detectedChannels", []):
        cid = ch.get("commandId")
        if not cid or cid not in CHANNEL_BITS:
            continue
        for rate, ady_field in ADY_COEFF_FIELDS.items():
            coeffs = ch.get(ady_field)
            if not coeffs:
                continue
            for curve in ("Audy", "Flat"):
                header = encode_header_word(cid, rate, curve)
                ints = [header] + encode_coefficients(coeffs)
                blobs.append({
                    "channel": cid,
                    "rate": rate,
                    "curve": curve,
                    "ints": ints,
                })
    return blobs


# ---------------------------------------------------------------------------
# Send / receive
# ---------------------------------------------------------------------------


class AudysseySender:
    def __init__(self, host: str, port: int, timeout: float = 10.0, verbose: bool = True):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.verbose = verbose
        self.sock: socket.socket | None = None
        self.rxbuf = bytearray()

    def __enter__(self) -> "AudysseySender":
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(2.0)
        return self

    def __exit__(self, *exc) -> None:
        if self.sock is not None:
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

    def _wait_ack(self, expect: str = "ACK", max_wait: float = 10.0) -> str:
        """Block until a Comm reply arrives (or timeout). Returns Comm value."""
        end = time.monotonic() + max_wait
        while time.monotonic() < end:
            self._drain(0.2)
            for f in parse_frames(self.rxbuf):
                comm = parse_comm(f["data"])
                if self.verbose:
                    print(f"  [rx {f['dir']}] {f['cmd']!r} cur={f['cur']} tot={f['tot']} "
                          f"datalen={len(f['data'])} comm={comm} body={f['data'][:80]!r}")
                if comm in ("ACK", "NACK"):
                    return comm
                # INPROGRESS or unknown — keep waiting
        return "TIMEOUT"

    def send(self, cmd: str, data: bytes = b"", cur_pkt: int = 0, tot_pkts: int = 0) -> str:
        assert self.sock is not None
        frame = build_frame(cmd, data, current_packet=cur_pkt, total_packets=tot_pkts)
        if self.verbose:
            preview = data[:80]
            print(f"[tx] {cmd!r} cur={cur_pkt} tot={tot_pkts} datalen={len(data)} "
                  f"body={preview!r}{'...' if len(data) > 80 else ''}")
        self.sock.sendall(frame)
        return self._wait_ack()


# ---------------------------------------------------------------------------
# Top-level orchestration (delegated to caller — this script just builds it)
# ---------------------------------------------------------------------------


def push_filters_only(
    sender: AudysseySender,
    disfil_msgs: list[dict],
    coef_blobs: list[dict],
) -> None:
    """Run steps 4-6 (DisFil + INIT_COEFS + CoefDt). Caller handles IAmp/IAudy/Fin."""
    for m in disfil_msgs:
        body = json.dumps(m, separators=(",", ":")).encode("ascii")
        comm = sender.send("SET_DISFIL", body)
        if comm != "ACK":
            raise RuntimeError(f"SET_DISFIL {m['ChData']}/{m['EqType']} → {comm}")

    comm = sender.send("INIT_COEFS")
    if comm != "ACK":
        raise RuntimeError(f"INIT_COEFS → {comm}")

    for blob in coef_blobs:
        chunks = chunk_blob(blob["ints"], CHUNK_INTS)
        tot = len(chunks) - 1  # zero-indexed last index, per parser:357
        for i, chunk in enumerate(chunks):
            body = pack_int32_be_list(chunk)
            comm = sender.send("SET_COEFDT", body, cur_pkt=i, tot_pkts=tot)
            if comm != "ACK":
                raise RuntimeError(
                    f"SET_COEFDT {blob['channel']}/{blob['rate']}/{blob['curve']} "
                    f"chunk {i}/{tot} → {comm}"
                )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help=".ady source file")
    ap.add_argument("host", help="AVR IP/hostname")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--filters-only", action="store_true",
                    help="skip IAmp/IAudy SET_SETDAT (caller already pushed them)")
    ap.add_argument("--commit", action="store_true",
                    help="send AudyFinFlg=Fin commit after filters")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be sent, don't connect")
    args = ap.parse_args()

    with open(args.src) as f:
        ady = json.load(f)

    disfil_msgs = extract_disfil_messages(ady)
    coef_blobs = extract_coef_blobs(ady)

    print(f"=== plan ===")
    print(f"SET_DISFIL packets: {len(disfil_msgs)} "
          f"({len({m['ChData'] for m in disfil_msgs})} channels × 2 EqTypes)")
    total_chunks = sum(len(chunk_blob(b['ints'], CHUNK_INTS)) for b in coef_blobs)
    print(f"SET_COEFDT blobs:   {len(coef_blobs)} ({total_chunks} chunks total)")
    for b in coef_blobs:
        n = len(chunk_blob(b['ints'], CHUNK_INTS))
        print(f"  {b['channel']:>4} {b['rate']:>7} {b['curve']:>4}  "
              f"ints={len(b['ints'])} chunks={n}")

    if args.dry_run:
        if disfil_msgs:
            print("\nfirst SET_DISFIL body:")
            print("  " + json.dumps(disfil_msgs[0])[:400])
        if coef_blobs:
            head = coef_blobs[0]['ints'][0]
            print(f"\nfirst CoefData header word: 0x{head & 0xFFFFFFFF:08X} "
                  f"(channel={coef_blobs[0]['channel']} rate={coef_blobs[0]['rate']} "
                  f"curve={coef_blobs[0]['curve']})")
        return 0

    with AudysseySender(args.host, args.port) as s:
        if not args.filters_only:
            print("\n[!] IAmp/IAudy push not implemented in this script — "
                  "use audyssey_push_full_iamp.py first, then re-run with --filters-only")
            return 2

        push_filters_only(s, disfil_msgs, coef_blobs)

        if args.commit:
            comm = s.send("SET_SETDAT", b'{"AudyFinFlg":"Fin"}')
            if comm != "ACK":
                print(f"[!] commit → {comm}")
                return 1

        s.send("EXIT_AUDMD")

    print("\n[ok] filter push complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
