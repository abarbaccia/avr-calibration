"""Probe: push raw 16,384-float .ady IRs to the X3800H without polyphase
decimation, mimicking the deleted scripts/audyssey_push_full_filters.py
approach (commit ea8fd76).

Hypothesis: X3800H MultEQ XT32 expects 16,384 raw floats per stream, NOT
the 1,024 polyphase-decimated coefs that calibrate.audyssey_fir.convert_xt32
produces. The polyphase decimator is ported from audyssey-rew-tuner /
oca_transfer.py which targets a different MultEQ generation.

If running THIS probe restores audible MultEQ playback (where the
calibrate/audyssey_fir.py path silenced the channel even when restoring
.ady's own coefs through it), the polyphase pipeline is wrong for this
hardware and we should retire it.

Wire format (per the deleted reference):
  Per stream: header_int32 + 16,384 coef int32s = 16,385 int32s
  Chunked at 128 int32s per SET_COEFDT packet.
  Each packet uses the standard SET_COEFDT frame (marker T, total_len BE,
  current_packet, total_packets, cmd, sep, param_len BE, data, checksum).

Usage:
    audyssey_raw_push.py <ady_path> <avr_host> [--commit] [--inter-packet-delay-ms 50]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import struct
import sys
import time
from pathlib import Path

from calibrate.drivers.denon.audyssey_filter_upload import (
    build_set_dat_envelope,
    chunk_setdat_payload,
    parse_frames,
    query_avr_status,
)
from calibrate.drivers.denon.audyssey_tcp import DEFAULT_PORT, build_frame


# Per the deleted reference (and ratbuddyssey FloatInt32 union):
# coefficient header word encodes channel, rate, curve in the FIRST
# Int32 of each blob. We pack as BE float32 → BE Int32 throughout.
CHANNEL_BITS = {
    "FL":  0x000, "C":  0x100, "FR": 0x200,
    "SRA": 0x300, "SLA": 0xC00,
    "SBR": 0x700, "SBL": 0x800,
    "TFL": 0xB00, "TFR": 0x400, "TRL": 0x900, "TRR": 0x600,
    "SW1": 0xD00, "SW2": 0xE00,
}
RATE_BITS = {32000: 0x00000, 44100: 0x10000, 48000: 0x20000}
CURVE_BITS = {"00": 0x01000000, "01": 0x00000000}  # 00=Flat, 01=Reference

CHUNK_INTS = 128  # 512 bytes per packet


def encode_header_word(channel: str, rate_hz: int, curve: str) -> int:
    return CHANNEL_BITS[channel] | RATE_BITS[rate_hz] | CURVE_BITS[curve]


def encode_floats_be(values: list[float]) -> list[int]:
    """Pack floats as BE Int32 bit-patterns (= BE float32 on the wire)."""
    return [struct.unpack(">i", struct.pack(">f", float(v)))[0] for v in values]


def chunk_int32s(ints: list[int], chunk_size: int = CHUNK_INTS) -> list[list[int]]:
    return [ints[i:i + chunk_size] for i in range(0, len(ints), chunk_size)]


def build_coefdt_frame(payload_ints: list[int], cur_pkt: int, tot_pkts: int) -> bytes:
    """Build a SET_COEFDT frame carrying ``payload_ints`` BE-packed.

    Frame: 'T' + total_len(2 BE) + cur_pkt(1) + tot_pkts(1) + 'SET_COEFDT'(10) +
           0x00 + param_len(2 BE) + payload + checksum(1)
    """
    body = struct.pack(f">{len(payload_ints)}i", *payload_ints)
    cmd = b"SET_COEFDT"
    total_len = 9 + len(cmd) + len(body)
    buf = bytearray()
    buf.append(ord("T"))
    buf += struct.pack(">H", total_len)
    buf.append(cur_pkt & 0xFF)
    buf.append(tot_pkts & 0xFF)
    buf += cmd
    buf.append(0x00)
    buf += struct.pack(">H", len(body))
    buf += body
    buf.append(sum(buf) & 0xFF)
    return bytes(buf)


def extract_response_data(ady_channel: dict) -> dict[int, list[float]]:
    """Pull responseData per sample-rate. Returns {samplerate_hz: [floats]}."""
    rate_keys = {"0": 32000, "1": 44100, "2": 48000}
    raw = ady_channel.get("responseData", {})
    out: dict[int, list[float]] = {}
    for k, sr in rate_keys.items():
        v = raw.get(k, raw.get(int(k), []))
        if v:
            out[sr] = [float(x) for x in v]
    return out


def drain(sock: socket.socket, rxbuf: bytearray, seconds: float) -> list[dict]:
    end = time.monotonic() + seconds
    frames: list[dict] = []
    while time.monotonic() < end:
        try:
            sock.settimeout(max(0.05, end - time.monotonic()))
            chunk = sock.recv(65536)
        except (socket.timeout, TimeoutError):
            break
        if not chunk:
            break
        rxbuf.extend(chunk)
        for f in parse_frames(rxbuf):
            frames.append({"cmd": f["cmd"].strip(), "data": f["data"]})
    return frames


def ack_in(frames: list[dict], cmd: str) -> bool:
    for f in frames:
        if f["cmd"] != cmd:
            continue
        try:
            if json.loads(f["data"]).get("Comm") == "ACK":
                return True
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return False


def nack_count(frames: list[dict]) -> int:
    n = 0
    for f in frames:
        try:
            if json.loads(f["data"]).get("Comm") == "NACK":
                n += 1
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return n


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ady_path")
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--channels", default="FL,C,FR,SLA,SRA")
    ap.add_argument("--curves", default="00,01")
    ap.add_argument("--rates", default="48000")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--inter-packet-delay-ms", type=float, default=50.0)
    args = ap.parse_args()

    with Path(args.ady_path).open() as f:
        ady = json.load(f)
    avr_state = query_avr_status(args.host, port=args.port)
    if not avr_state:
        print("ERROR: AVR Audyssey TCP unresponsive", file=sys.stderr)
        return 1

    target_channels = set(args.channels.split(","))
    target_rates = [int(r) for r in args.rates.split(",")]
    target_curves = args.curves.split(",")

    # Build coefficient blobs per (channel, rate, curve).
    blobs: list[tuple[str, int, str, list[int]]] = []
    for ch in ady.get("detectedChannels", []):
        cid = ch.get("commandId")
        if cid not in target_channels:
            continue
        rd = extract_response_data(ch)
        for sr in target_rates:
            if sr not in rd:
                print(f"  WARN: {cid} has no {sr}Hz responseData; skipping")
                continue
            ir = rd[sr]
            for curve in target_curves:
                header = encode_header_word(cid, sr, curve)
                ints = [header] + encode_floats_be(ir)
                blobs.append((cid, sr, curve, ints))
                print(f"  {cid} {sr}Hz tc={curve}: {len(ir)} floats → {len(ints)} ints "
                      f"(peak={max(abs(x) for x in ir):.4f})")

    if not blobs:
        print("ERROR: no blobs to push", file=sys.stderr)
        return 2

    # Send envelope + raw COEFDT streams.
    envelope = build_set_dat_envelope(ady, dict(avr_state))
    setdat_chunks = chunk_setdat_payload(envelope)
    print(f"\nEnvelope: {len(setdat_chunks)} SET_SETDAT chunk(s)")
    print(f"COEFDT blobs: {len(blobs)} streams, total chunks: "
          f"{sum(len(chunk_int32s(b[3])) for b in blobs)}")

    sock = socket.create_connection((args.host, args.port), timeout=30.0)
    sock.settimeout(30.0)
    rx = bytearray()
    summary: dict = {"setdat_acks": [], "coef_packets_sent": 0, "coef_nack_count": 0}

    try:
        sock.sendall(build_frame("ENTER_AUDY"))
        summary["enter_audy_ack"] = ack_in(drain(sock, rx, 1.0), "ENTER_AUDY")
        rx.clear()
        for ch in setdat_chunks:
            body = json.dumps(ch, separators=(",", ":")).encode("ascii")
            sock.sendall(build_frame("SET_SETDAT", body))
            ok = ack_in(drain(sock, rx, 2.5), "SET_SETDAT")
            summary["setdat_acks"].append(ok)
            rx.clear()
            if not ok:
                print(f"SET_SETDAT NACK — aborting")
                return 3

        # Pre-coef settle
        time.sleep(0.5)
        rx.clear()

        # Stream blobs.
        for blob_idx, (cid, sr, curve, ints) in enumerate(blobs):
            chunks = chunk_int32s(ints)
            tot_pkts = len(chunks) - 1
            blob_nacks = 0
            for i, chunk in enumerate(chunks):
                pkt = build_coefdt_frame(chunk, cur_pkt=i, tot_pkts=tot_pkts)
                sock.sendall(pkt)
                summary["coef_packets_sent"] += 1
                time.sleep(args.inter_packet_delay_ms / 1000.0)
            # Drain after each blob (per-stream NACK accounting)
            blob_frames = drain(sock, rx, 0.1)
            blob_nacks = nack_count(blob_frames)
            summary["coef_nack_count"] += blob_nacks
            print(f"  [{blob_idx + 1}/{len(blobs)}] {cid} {sr}Hz tc={curve}: "
                  f"{len(chunks)} pkts, NACKs={blob_nacks}")
            rx.clear()

        # CoefWaitTime.Final wait
        wait_ms = float(avr_state.get("CoefWaitTime", {}).get("Final", 15000))
        print(f"\nCoefWait final {wait_ms}ms...")
        wait_frames = drain(sock, rx, wait_ms / 1000.0)
        wait_nacks = nack_count(wait_frames)
        summary["coef_nack_count"] += wait_nacks
        print(f"  wait NACKs: {wait_nacks}")
        rx.clear()

        sock.sendall(build_frame("FINZ_COEFS"))
        finz_frames = drain(sock, rx, 20.0)
        summary["finz_coefs_ack"] = ack_in(finz_frames, "FINZ_COEFS")
        print(f"FINZ_COEFS ack: {summary['finz_coefs_ack']}")
        rx.clear()

        if args.commit and summary["coef_nack_count"] == 0:
            time.sleep(0.05)
            sock.sendall(build_frame("SET_SETDAT", b'{"AudyFinFlg":"Fin"}'))
            fin_frames = drain(sock, rx, 5.0)
            summary["fin_commit_ack"] = ack_in(fin_frames, "SET_SETDAT")
            print(f"Fin commit ack: {summary['fin_commit_ack']}")
            rx.clear()
        elif args.commit:
            print(f"Fin commit SKIPPED — {summary['coef_nack_count']} NACKs")

        time.sleep(0.05)
        sock.sendall(build_frame("EXIT_AUDMD"))
        exit_frames = drain(sock, rx, 2.0)
        summary["exit_audmd_ack"] = ack_in(exit_frames, "EXIT_AUDMD")
        print(f"EXIT_AUDMD ack: {summary['exit_audmd_ack']}")
    finally:
        sock.close()

    print("\n" + json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
