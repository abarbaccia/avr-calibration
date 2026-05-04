"""Decode Audyssey TCP frames from a tcpdump pcap file.

Reassembles each TCP direction (phone→AVR and AVR→phone) for port-1256
flows, runs the Audyssey frame parser over the bytes, and prints every
framed message in chronological order. For binary SET_COEFDT bodies,
dumps the first few values under three candidate interpretations
(float32, Q31, Q36) so we can resolve the encoding question.

Pure-Python — no scapy/dpkt dependency. Parses pcap-format files
directly (https://wiki.wireshark.org/Development/LibpcapFileFormat).

USAGE
    python audyssey_pcap_decode.py /tmp/multeq-NNN.pcap [--avr 192.168.1.209]
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from collections import defaultdict

PCAP_GLOBAL_HDR = 24
PCAP_REC_HDR = 16
LINKTYPE_ETHERNET = 1
LINKTYPE_RAW = 101
LINKTYPE_LINUX_SLL = 113
LINKTYPE_LINUX_SLL2 = 276


# --- pcap reader -----------------------------------------------------------


def read_pcap(path: str):
    """Yield (ts, packet_bytes) for every record in a pcap file."""
    with open(path, "rb") as f:
        gh = f.read(PCAP_GLOBAL_HDR)
        if len(gh) < PCAP_GLOBAL_HDR:
            raise RuntimeError("pcap too small")
        magic = struct.unpack("<I", gh[:4])[0]
        if magic == 0xA1B2C3D4:
            endian = "<"
        elif magic == 0xD4C3B2A1:
            endian = ">"
        else:
            raise RuntimeError(f"bad pcap magic 0x{magic:08x}")
        linktype = struct.unpack(endian + "I", gh[20:24])[0]
        while True:
            rh = f.read(PCAP_REC_HDR)
            if len(rh) < PCAP_REC_HDR:
                return
            ts_sec, ts_usec, caplen, _origlen = struct.unpack(endian + "IIII", rh)
            data = f.read(caplen)
            if len(data) < caplen:
                return
            ts = ts_sec + ts_usec / 1_000_000
            yield ts, linktype, data


# --- L2/L3 parsing ---------------------------------------------------------


def strip_link(linktype: int, pkt: bytes) -> bytes:
    """Return the IP datagram from a link-layer packet."""
    if linktype == LINKTYPE_ETHERNET:
        if len(pkt) < 14:
            return b""
        et = struct.unpack("!H", pkt[12:14])[0]
        if et == 0x8100:  # 802.1Q VLAN
            et = struct.unpack("!H", pkt[16:18])[0]
            return pkt[18:] if et == 0x0800 else b""
        return pkt[14:] if et == 0x0800 else b""
    if linktype == LINKTYPE_LINUX_SLL:
        if len(pkt) < 16:
            return b""
        et = struct.unpack("!H", pkt[14:16])[0]
        return pkt[16:] if et == 0x0800 else b""
    if linktype == LINKTYPE_LINUX_SLL2:
        if len(pkt) < 20:
            return b""
        et = struct.unpack("!H", pkt[0:2])[0]
        return pkt[20:] if et == 0x0800 else b""
    if linktype == LINKTYPE_RAW:
        return pkt
    return b""


def parse_ip_tcp(ip_pkt: bytes):
    """Return (src_ip, dst_ip, src_port, dst_port, seq, payload) or None."""
    if len(ip_pkt) < 20:
        return None
    vhl = ip_pkt[0]
    if (vhl >> 4) != 4:
        return None
    ihl = (vhl & 0x0F) * 4
    if ip_pkt[9] != 6:  # TCP
        return None
    total_len = struct.unpack("!H", ip_pkt[2:4])[0]
    if total_len < ihl or len(ip_pkt) < total_len:
        return None
    src = ".".join(str(b) for b in ip_pkt[12:16])
    dst = ".".join(str(b) for b in ip_pkt[16:20])
    tcp = ip_pkt[ihl:total_len]
    if len(tcp) < 20:
        return None
    sp, dp = struct.unpack("!HH", tcp[:4])
    seq = struct.unpack("!I", tcp[4:8])[0]
    doff = (tcp[12] >> 4) * 4
    payload = tcp[doff:]
    return src, dst, sp, dp, seq, payload


# --- Audyssey frame parser -------------------------------------------------


def drain_audy_frames(buf: bytearray) -> tuple[list[dict], int]:
    """Parse complete Audyssey frames from front of buf.

    Returns (frames, bytes_consumed). Bytes consumed include any garbage
    skipped during resync. Caller is expected to del buf[:bytes_consumed].
    """
    out: list[dict] = []
    cursor = 0
    n = len(buf)
    while n - cursor >= 19:
        if buf[cursor] not in (ord("T"), ord("R")):
            cursor += 1
            continue
        total_len = struct.unpack(">H", bytes(buf[cursor + 1:cursor + 3]))[0]
        if total_len < 19 or total_len > 0xFFFF:
            cursor += 1
            continue
        if n - cursor < total_len:
            break
        end = cursor + total_len
        cur = buf[cursor + 3]
        tot = buf[cursor + 4]
        cmd = bytes(buf[cursor + 5:cursor + 15]).decode("ascii", errors="replace").strip()
        dlen = struct.unpack(">H", bytes(buf[cursor + 16:cursor + 18]))[0]
        data = bytes(buf[cursor + 18:cursor + 18 + dlen])
        ck_actual = buf[end - 1]
        ck_expected = sum(buf[cursor:end - 1]) & 0xFF
        out.append({
            "dir_byte": chr(buf[cursor]),
            "cmd": cmd,
            "cur": cur,
            "tot": tot,
            "data": data,
            "ck_ok": ck_actual == ck_expected,
        })
        cursor = end
    return out, cursor


# --- Body decoding ---------------------------------------------------------


def decode_coefdt_body(data: bytes, max_show: int = 12) -> str:
    """Three-way decode for binary SET_COEFDT body. First int is header word."""
    if len(data) < 4:
        return f"(too short, {len(data)}B)"
    n = len(data) // 4
    ints = struct.unpack(f">{n}i", data[:n * 4])
    floats = struct.unpack(f">{n}f", data[:n * 4])
    header = ints[0] & 0xFFFFFFFF
    ch_bits = header & 0x00000F00
    rate_bits = header & 0x00030000
    curve_bit = header & 0x01000000
    ch_map = {0x000: "FL", 0x100: "C", 0x200: "FR", 0x300: "SRA",
              0xC00: "SLA", 0xD00: "SW1"}
    rate_map = {0x00000: "32k", 0x10000: "44.1k", 0x20000: "48k"}
    ch = ch_map.get(ch_bits, f"?0x{ch_bits:x}")
    rate = rate_map.get(rate_bits, f"?0x{rate_bits:x}")
    curve = "Flat" if curve_bit else "Audy"

    # Three interpretations of the payload (skip header):
    show = min(max_show, n - 1)
    sample_floats = floats[1:1 + show]
    sample_q31 = [v / 0x7FFFFFFF for v in ints[1:1 + show]]
    sample_q36 = [v / 0xFFFFFFFFF for v in ints[1:1 + show]]

    lines = [
        f"  HEADER 0x{header:08x} → ch={ch} rate={rate} curve={curve}; total {n} ints ({len(data)}B)",
        f"  as float32:  [{', '.join(f'{v:+.4g}' for v in sample_floats)}]",
        f"  as Q31:      [{', '.join(f'{v:+.4g}' for v in sample_q31)}]",
        f"  as Q36:      [{', '.join(f'{v:+.4g}' for v in sample_q36)}]",
    ]
    return "\n".join(lines)


def decode_body(cmd: str, data: bytes) -> str:
    if not data:
        return "(empty)"
    if cmd == "SET_COEFDT":
        return decode_coefdt_body(data)
    # Try JSON
    try:
        text = data.decode("ascii")
        try:
            obj = json.loads(text)
            return "  json: " + json.dumps(obj, indent=2)[:1200]
        except json.JSONDecodeError:
            return "  ascii: " + repr(text[:600])
    except UnicodeDecodeError:
        pass
    return f"  binary {len(data)}B head: {data[:32].hex()}"


# --- TCP reassembly --------------------------------------------------------


def reassemble(pcap_path: str, avr_ip: str | None) -> dict[tuple, list[tuple[float, bytes]]]:
    """Group TCP payloads by (client, server, server_port) flow.

    Returns {flow_key: [(ts, payload), ...]} where flow_key is
    (client_ip, server_ip, "phone→avr"|"avr→phone").
    Payloads are sorted by sequence number to handle out-of-order packets.
    """
    by_flow: dict[tuple, list[tuple[int, float, bytes]]] = defaultdict(list)
    for ts, linktype, pkt in read_pcap(pcap_path):
        ip_pkt = strip_link(linktype, pkt)
        if not ip_pkt:
            continue
        parsed = parse_ip_tcp(ip_pkt)
        if not parsed:
            continue
        src, dst, sp, dp, seq, payload = parsed
        if not payload:
            continue
        if dp == 1256:
            # phone → avr
            key = (src, dst, "phone→avr")
            if avr_ip and dst != avr_ip:
                continue
        elif sp == 1256:
            key = (dst, src, "avr→phone")
            if avr_ip and src != avr_ip:
                continue
        else:
            continue
        by_flow[key].append((seq, ts, payload))

    # Sort each flow by seq
    out: dict[tuple, list[tuple[float, bytes]]] = {}
    for key, segs in by_flow.items():
        segs.sort(key=lambda x: x[0])
        # naive dedup on (seq, len)
        seen = set()
        dedup = []
        for seq, ts, p in segs:
            k = (seq, len(p))
            if k in seen:
                continue
            seen.add(k)
            dedup.append((ts, p))
        out[key] = dedup
    return out


# --- Main ------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pcap")
    ap.add_argument("--avr", help="filter to this AVR IP only (e.g. 192.168.1.209)")
    args = ap.parse_args()

    flows = reassemble(args.pcap, args.avr)
    if not flows:
        print("no port-1256 flows found")
        return 1

    # Group both directions for each phone↔avr pair
    pairs: dict[tuple, dict[str, list[tuple[float, bytes]]]] = defaultdict(dict)
    for (client, server, direction), segs in flows.items():
        pairs[(client, server)][direction] = segs

    for (client, server), dirs in pairs.items():
        print(f"\n{'='*70}")
        print(f"FLOW  client={client}  server={server} (port 1256)")
        print(f"{'='*70}")

        # Interleave by timestamp
        events: list[tuple[float, str, bytes]] = []
        for direction, segs in dirs.items():
            for ts, payload in segs:
                events.append((ts, direction, payload))
        events.sort(key=lambda x: x[0])

        # Per-direction byte streams (Audyssey frames may straddle TCP segments)
        bufs: dict[str, bytearray] = {"phone→avr": bytearray(), "avr→phone": bytearray()}
        t0 = events[0][0] if events else 0
        for ts, direction, payload in events:
            bufs[direction].extend(payload)
            new_frames, consumed = drain_audy_frames(bufs[direction])
            for f in new_frames:
                ck = "" if f["ck_ok"] else "  ✗CK"
                tag = f"{f['dir_byte']}/{direction}"
                rel = ts - t0
                print(f"\n[{rel:7.3f}s] {tag} cmd={f['cmd']:12} cur={f['cur']} "
                      f"tot={f['tot']} dlen={len(f['data'])}{ck}")
                print(decode_body(f["cmd"], f["data"]))
            del bufs[direction][:consumed]

    return 0


if __name__ == "__main__":
    sys.exit(main())
