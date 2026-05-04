"""Non-destructive endianness verification probe for SET_COEFDT wire format.

Pushes a passthrough (zero-EQ identity) FIR to ONE channel (SW1 by default),
sends FINZ_COEFS, and observes whether the AVR ACKs. Does NOT send the Fin
commit, so the AVR's persisted Audyssey calibration is unchanged after
EXIT_AUDMD.

Why one channel + sub: even if the AVR loads the new coefs into active DSP
until power-cycle, the sub chain has CamillaDSP modal correction in front of
Audyssey, so the listening experience isn't broken. Mains Audyssey untouched.

Pass condition: `finz_coefs_ack == True` confirms the BE float32 wire format
is accepted. Fail condition: missing ACK + NACK frames in the response stream
indicate a packet-validation problem.

Usage:
    audyssey_endian_probe.py <ady_path> <avr_host> [--channel SW1]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import time
from pathlib import Path

from calibrate.audyssey_fir import (
    convert_xt32,
    design_passthrough_ir,
    is_sub_channel,
)
from calibrate.drivers.denon.audyssey_coef_transfer import (
    XT32_SAMPLE_RATES_HZ,
    TARGET_CURVE_FLAT,
    TARGET_CURVE_REFERENCE,
    build_coef_packets,
)
from calibrate.drivers.denon.audyssey_filter_upload import (
    build_set_dat_envelope,
    chunk_setdat_payload,
    parse_frames,
    query_avr_status,
)
from calibrate.drivers.denon.audyssey_tcp import DEFAULT_PORT, build_frame


def _drain(sock: socket.socket, rxbuf: bytearray, seconds: float) -> list[dict]:
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


def _ack_in(frames: list[dict], cmd: str) -> bool:
    for f in frames:
        if f["cmd"] != cmd:
            continue
        try:
            if json.loads(f["data"]).get("Comm") == "ACK":
                return True
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return False


def _nack_in(frames: list[dict]) -> int:
    n = 0
    for f in frames:
        try:
            if json.loads(f["data"]).get("Comm") == "NACK":
                n += 1
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ady_path")
    ap.add_argument("host")
    ap.add_argument("--channel", default="SW1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    ady_path = Path(args.ady_path)
    if not ady_path.exists():
        print(f"ERROR: .ady not found at {ady_path}", file=sys.stderr)
        return 1
    with ady_path.open() as f:
        ady = json.load(f)

    print(f"=== Probe target: {args.channel} on {args.host} ===")
    print(f"  ady: {ady_path.name}")

    avr_state = query_avr_status(args.host, port=args.port)
    if not avr_state:
        print("ERROR: no AVR response (powered on? port 1256 reachable?)", file=sys.stderr)
        return 2
    print(f"  DType: {avr_state.get('DType')!r}  CoefWaitTime: {avr_state.get('CoefWaitTime')!r}")

    # Build envelope (NotFin) — required so AVR has channel context for SET_COEFDT.
    envelope = build_set_dat_envelope(ady, dict(avr_state))
    chunks = chunk_setdat_payload(envelope)
    print(f"  envelope: {len(chunks)} SET_SETDAT chunk(s)")

    # Build passthrough FIR + packets for one channel × 2 TC × 3 SR.
    is_sub = is_sub_channel(args.channel)
    ir = design_passthrough_ir(is_sub=is_sub)
    coefs = convert_xt32(ir)
    print(f"  channel={args.channel}  kind={'sub' if is_sub else 'speaker'}  "
          f"coefs={len(coefs)} floats")

    coef_streams: list[bytes] = []
    for tc in (TARGET_CURVE_FLAT, TARGET_CURVE_REFERENCE):
        for sr in XT32_SAMPLE_RATES_HZ:
            pkts = build_coef_packets(
                coefs, channel_id=args.channel, target_curve=tc, samplerate_hz=sr,
            )
            coef_streams.extend(pkts)
    print(f"  coef packets: {len(coef_streams)} total")

    # Wire sequence — same as push_avr_filters but NO Fin commit.
    coef_wait_final_ms = float(avr_state.get("CoefWaitTime", {}).get("Final", 15000))
    inter_packet_delay_ms = 5.0

    sock = socket.create_connection((args.host, args.port), timeout=30.0)
    sock.settimeout(30.0)
    rx = bytearray()
    summary: dict = {}

    try:
        sock.sendall(build_frame("ENTER_AUDY"))
        summary["enter_audy_ack"] = _ack_in(_drain(sock, rx, 1.0), "ENTER_AUDY")
        print(f"  ENTER_AUDY ack: {summary['enter_audy_ack']}")
        rx.clear()

        for i, chunk in enumerate(chunks):
            body = json.dumps(chunk, separators=(",", ":")).encode("ascii")
            sock.sendall(build_frame("SET_SETDAT", body))
            ack = _ack_in(_drain(sock, rx, 2.5), "SET_SETDAT")
            print(f"  SET_SETDAT[{i + 1}/{len(chunks)}] ack: {ack}")
            rx.clear()
            if not ack:
                print("  ✗ envelope NACK — aborting probe")
                return 3

        d_type = str(avr_state.get("DType", "")).lower()
        if d_type.startswith("fixed"):
            time.sleep(0.02)
            sock.sendall(build_frame("INIT_COEFS"))
            init_ack = _ack_in(_drain(sock, rx, 1.5), "INIT_COEFS")
            print(f"  INIT_COEFS ack: {init_ack}")
            rx.clear()

        # SET_COEFDT stream — fire-and-forget, watch for NACK frames.
        nack_count = 0
        for pkt in coef_streams:
            sock.sendall(pkt)
            time.sleep(inter_packet_delay_ms / 1000.0)
        # Pause after stream (between-channel pacing analogue).
        time.sleep(0.1)
        # Drain anything queued during processing.
        time.sleep(coef_wait_final_ms / 1000.0)
        mid_frames = _drain(sock, rx, 0.5)
        nack_count += _nack_in(mid_frames)
        if mid_frames:
            print(f"  mid-stream frames: {[f['cmd'] for f in mid_frames]}  "
                  f"NACKs: {nack_count}")

        rx.clear()
        sock.sendall(build_frame("FINZ_COEFS"))
        finz_frames = _drain(sock, rx, 20.0)
        finz_ack = _ack_in(finz_frames, "FINZ_COEFS")
        finz_nack = _nack_in(finz_frames)
        summary["finz_coefs_ack"] = finz_ack
        summary["finz_nack_count"] = finz_nack
        summary["mid_nack_count"] = nack_count
        print(f"  FINZ_COEFS ack: {finz_ack}  NACKs: {finz_nack}  "
              f"frames: {[f['cmd'] for f in finz_frames]}")

        # Skip Fin commit — non-destructive probe.
        print("  (Fin commit SKIPPED — probe is non-destructive)")
        rx.clear()
        time.sleep(0.02)
        sock.sendall(build_frame("EXIT_AUDMD"))
        exit_frames = _drain(sock, rx, 2.0)
        summary["exit_audmd_ack"] = _ack_in(exit_frames, "EXIT_AUDMD")
        print(f"  EXIT_AUDMD ack: {summary['exit_audmd_ack']}")
    finally:
        sock.close()

    print(f"\n=== RESULT ===\n{json.dumps(summary, indent=2)}")
    if summary.get("finz_coefs_ack"):
        print("\n✓ PASS — FINZ_COEFS ACKed. BE float32 wire format is accepted.")
        return 0
    else:
        total_nacks = summary.get("mid_nack_count", 0) + summary.get("finz_nack_count", 0)
        print(f"\n✗ FAIL — FINZ_COEFS did not ACK. Total NACK frames: {total_nacks}.")
        return 4


if __name__ == "__main__":
    sys.exit(main())
