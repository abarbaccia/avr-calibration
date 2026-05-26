"""Push original .ady-stored Audyssey correction FIRs back to the AVR.

IMPORTANT — .ady data-source requirements
==========================================
This script feeds ``responseData["2"]`` (the 48 kHz buffer) through
``convert_xt32`` and pushes the result as the AVR's MultEQ FIR.

This only works when ``responseData["2"]`` IS the correction FIR.
Two different things can live in that field depending on how the .ady
was produced:

  CORRECTION FIR (.ady from OCA / oca_transfer.py or similar):
      Peak is near index 0 (minimum-phase) or the centre (linear-phase).
      peak_idx / N < 0.01 for minimum-phase.  Safe to push.

  ROOM MEASUREMENT IR (.ady from Audyssey MultEQ Editor app):
      Peak is at the direct-sound arrival time (typically 10–50 ms,
      i.e. peak_idx > 700 at 48 kHz).  Pushing this as a correction
      FIR doubles the room response and kills HF — do NOT use.

The script rejects inputs where ``peak_idx > PEAK_IDX_LIMIT`` (default
200 ≈ 4 ms at 48 kHz) unless ``--force`` is passed.

For "MultEQ Editor" .ady files with pre-computed coefficients, use
``audyssey_push_full_filters.py`` instead (reads ``coefficient48kHz``).

Usage:
    audyssey_restore_from_ady.py <ady_path> <avr_host> [--force]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from calibrate.audyssey_fir import convert_xt32, FILTER_CONFIGS, is_sub_channel
from calibrate.drivers.denon.audyssey_filter_upload import push_avr_filters


SPEAKER_INPUT_LEN = FILTER_CONFIGS["xt32Speaker"]["input_length"]  # 16321
SUB_INPUT_LEN = FILTER_CONFIGS["xt32Sub"]["input_length"]          # 16055

# Reject responseData peaks beyond this index (≈ 4 ms at 48 kHz).
# Room-measurement IRs have peaks at 700–2400+ samples.
PEAK_IDX_LIMIT = 200


def extract_channel_ir(
    ady_channel: dict,
    samplerate_key: str = "2",
    force: bool = False,
) -> list[float]:
    """Pull 48kHz responseData from one .ady detected channel, validate it
    looks like a correction FIR (not a room measurement), and trim to the
    XT32 polyphase input length."""
    raw = ady_channel.get("responseData", {}).get(samplerate_key, [])
    vals = [float(v) for v in raw]
    cid = ady_channel.get("commandId", "")
    target_len = SUB_INPUT_LEN if is_sub_channel(cid) else SPEAKER_INPUT_LEN
    if len(vals) < target_len:
        raise ValueError(f"{cid} responseData has {len(vals)} samples, expected ≥ {target_len}")

    peak_idx = max(range(len(vals)), key=lambda i: abs(vals[i]))
    if peak_idx > PEAK_IDX_LIMIT and not force:
        raise ValueError(
            f"{cid} responseData peak at index {peak_idx} ({peak_idx/48000*1000:.1f} ms) — "
            f"this is a room-measurement IR, not a correction FIR.\n"
            f"Pushing it would double the room response and destroy HF above 5 kHz.\n"
            f"If you have an OCA-format .ady with a correction FIR in responseData,\n"
            f"pass --force to bypass this check.\n"
            f"For MultEQ Editor .ady files, use audyssey_push_full_filters.py instead."
        )

    return vals[:target_len]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ady_path")
    ap.add_argument("host")
    ap.add_argument("--channels", default="FL,C,FR,SLA,SRA",
                    help="comma-separated channel commandIds to restore")
    ap.add_argument("--inter-packet-delay-ms", type=float, default=50.0)
    ap.add_argument("--force", action="store_true",
                    help="skip peak-position sanity check (use for OCA-format .ady files)")
    args = ap.parse_args()

    with Path(args.ady_path).open() as f:
        ady = json.load(f)

    target_channels = set(args.channels.split(","))
    channel_filters: dict[str, list[float]] = {}
    for ch in ady.get("detectedChannels", []):
        cid = ch.get("commandId")
        if cid not in target_channels:
            continue
        try:
            ir = extract_channel_ir(ch, force=args.force)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        coefs = convert_xt32(ir)
        channel_filters[cid] = coefs
        kind = "sub" if is_sub_channel(cid) else "speaker"
        peak_idx = max(range(len(ir)), key=lambda i: abs(ir[i]))
        print(f"  {cid:>4} ({kind}): IR peak_idx={peak_idx} ({peak_idx/48000*1000:.1f} ms) "
              f"peak={max(abs(x) for x in ir):.4f} "
              f"→ coefs peak={max(abs(x) for x in coefs):.4f} "
              f"sum={sum(coefs):.4f}")

    if not channel_filters:
        print(f"ERROR: no matching channels in {args.ady_path}", file=sys.stderr)
        return 1

    print(f"\nPushing {len(channel_filters)} channel(s) to {args.host}...")
    summary = await push_avr_filters(
        args.host,
        ady=ady,
        channel_filters=channel_filters,
        inter_packet_delay_ms=args.inter_packet_delay_ms,
    )
    print(json.dumps({k: v for k, v in summary.items()
                      if k not in ("mid_wait_frames", "finz_frames",
                                   "fin_frames", "exit_frames",
                                   "pre_coef_frames")}, indent=2, default=str))
    return 0 if summary.get("ok") else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
