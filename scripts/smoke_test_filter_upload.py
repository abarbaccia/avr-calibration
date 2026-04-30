"""Smoke-test the full Audyssey filter upload pipeline against a live AVR.

Does NOT play audio sweeps. Exercises:
  1. ENTER_AUDY + GET_AVRINF + GET_AVRSTS handshake (introspect)
  2. SET_SETDAT envelope construction from a .ady file
  3. Envelope chunking under the 510-byte threshold
  4. SET_COEFDT packet stream construction (no actual transmit by default)
  5. Optional --transmit: real upload of passthrough FIRs (zero-EQ)

The default mode (--dry-run) does NOT touch the AVR's calibration state —
it only queries GET_AVRINF / GET_AVRSTS. Safe to run any time.

The --transmit mode pushes passthrough (identity) FIR coefficients to all
channels. This DOES rewrite the AVR's MultEQ filters with our zero-EQ
identity IRs — losing the original Audyssey calibration in the process.
Recovery: re-push the original .ady via the MultEQ Editor app, or run
this script again with --restore-from <original.ady>.

Usage:
    smoke_test_filter_upload.py /path/to/state.ady 192.168.1.209
    smoke_test_filter_upload.py /path/to/state.ady 192.168.1.209 --transmit --commit
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from calibrate.audyssey_fir import (
    convert_xt32,
    design_passthrough_ir,
    is_sub_channel,
)
from calibrate.drivers.denon.audyssey_coef_transfer import (
    XT32_SAMPLE_RATES_HZ,
    all_streams_for_channel,
)
from calibrate.drivers.denon.audyssey_filter_upload import (
    build_set_dat_envelope,
    channels_in_ady,
    chunk_setdat_payload,
    envelope_packet_size,
    push_avr_filters,
    query_avr_status,
)


def _print_avr_state(state: dict) -> None:
    print(f"  AmpAssign:      {state.get('AmpAssign')!r}")
    print(f"  EQType:         {state.get('EQType')!r}")
    print(f"  DType:          {state.get('DType')!r}")
    print(f"  CoefWaitTime:   {state.get('CoefWaitTime')!r}")
    print(f"  SWSetup:        {state.get('SWSetup')!r}")
    bin_str = state.get("AssignBin", "")
    print(f"  AssignBin:      {bin_str[:40]}{'...' if len(bin_str) > 40 else ''}")


def _summarize_envelope(envelope: list[tuple[str, object]]) -> None:
    for k, v in envelope:
        if isinstance(v, list):
            print(f"  {k}: list[{len(v)}], first[0]={list(v[0].items())[:2] if v else '...'}")
        elif isinstance(v, dict):
            sample = list(v.items())[:2]
            print(f"  {k}: dict[{len(v)}] sample={sample}")
        else:
            print(f"  {k}: {v!r}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ady_path", help=".ady file with the AVR's stored calibration state")
    ap.add_argument("host", help="AVR IP / hostname")
    ap.add_argument("--port", type=int, default=1256)
    ap.add_argument(
        "--transmit", action="store_true",
        help="actually transmit passthrough FIRs to the AVR. "
             "Overwrites the current MultEQ calibration. "
             "Default is dry-run (no writes).",
    )
    ap.add_argument(
        "--override", nargs=2, action="append", default=[],
        metavar=("CH", "METERS"),
        help="override one channel's distance "
             "(repeatable, e.g. --override SW1 20.0)",
    )
    ap.add_argument(
        "--target-curves", default="00,01",
        help="comma-separated target curves to write (00=Flat, 01=Reference). "
             "Default: both (so user can toggle at runtime).",
    )
    ap.add_argument(
        "--samplerates", default=",".join(str(r) for r in XT32_SAMPLE_RATES_HZ),
        help="comma-separated sample rates in Hz to ship per channel",
    )
    args = ap.parse_args()

    ady_path = Path(args.ady_path)
    if not ady_path.exists():
        print(f"ERROR: .ady not found at {ady_path}", file=sys.stderr)
        return 1
    with ady_path.open() as f:
        ady = json.load(f)

    print(f"=== .ady summary: {ady_path.name} ===")
    print(f"  targetModelName: {ady.get('targetModelName')}")
    print(f"  channels:        {channels_in_ady(ady)}")

    print(f"\n=== Querying AVR state at {args.host}:{args.port} ===")
    avr_state = query_avr_status(args.host, port=args.port)
    if not avr_state:
        print("ERROR: no AVR response — is the receiver on, MultEQ Editor port "
              "open, and not in Pure Direct mode?", file=sys.stderr)
        return 2
    _print_avr_state(avr_state)

    overrides = {ch: float(m) for ch, m in args.override}

    print(f"\n=== Building SET_SETDAT envelope ===")
    envelope = build_set_dat_envelope(
        ady, avr_state, distances_override_m=overrides,
    )
    _summarize_envelope(envelope)

    print(f"\n=== Envelope chunking (threshold 510B) ===")
    chunks = chunk_setdat_payload(envelope)
    for i, chunk in enumerate(chunks):
        size = envelope_packet_size(chunk)
        keys = list(chunk.keys())
        print(f"  chunk {i + 1}/{len(chunks)}: {size}B, fields={keys}")

    print(f"\n=== Building passthrough FIR coefficient streams ===")
    target_curves = tuple(args.target_curves.split(","))
    samplerates = tuple(int(r) for r in args.samplerates.split(","))
    channel_filters: dict[str, list[float]] = {}
    total_packets = 0
    for cid in channels_in_ady(ady):
        ir = design_passthrough_ir(is_sub=is_sub_channel(cid))
        coefs = convert_xt32(ir)
        channel_filters[cid] = coefs
        streams = all_streams_for_channel(
            coefs, channel_id=cid,
            target_curves=target_curves, samplerates_hz=samplerates,
        )
        total_packets += len(streams)
        kind = "sub" if is_sub_channel(cid) else "speaker"
        print(f"  {cid:>4} ({kind}): {len(coefs)} coefs → {len(streams)} packets")

    total_bytes_setdat = sum(envelope_packet_size(c) for c in chunks)
    print(
        f"\n=== Wire-traffic estimate ===\n"
        f"  SET_SETDAT chunks:   {len(chunks)} ({total_bytes_setdat} bytes total)\n"
        f"  SET_COEFDT packets:  {total_packets} (coefficient streams)"
    )

    if not args.transmit:
        print(
            "\n--dry-run mode: no AVR writes performed. "
            "Re-run with --transmit to actually push the passthrough FIRs."
        )
        return 0

    # Confirmation guard for the destructive path.
    print(
        "\n!!! TRANSMIT MODE — about to overwrite the AVR's MultEQ filters with "
        "passthrough (zero-EQ) FIRs. Type 'yes' to proceed."
    )
    answer = input("> ").strip()
    if answer.lower() != "yes":
        print("Aborted.")
        return 0

    print("\n=== Pushing to AVR ===")
    summary = await push_avr_filters(
        args.host, ady=ady, channel_filters=channel_filters,
        avr_status=avr_state, distances_override_m=overrides,
        target_curves=target_curves, samplerates_hz=samplerates,
        port=args.port,
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary.get("ok") else 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
