"""Push original .ady-stored Audyssey coefs back to the AVR.

Uses the existing convert_xt32 + push_avr_filters pipeline, just sourcing
the IR from the .ady's responseData instead of design_correction_ir.

Result: AVR's MultEQ filter banks restored to whatever was in the .ady.
For our use case (1_BACKUP_20260427-160223.ady), that's the working
post-Audyssey calibration state.

Use to:
  - Recover from a botched apply_avr_fir push that silences MultEQ
  - Restore original Audyssey before re-running calibration
  - Diagnose whether convert_xt32 is correct (push original → expect
    original behavior; if silent, the polyphase pipeline is wrong)

Usage:
    audyssey_restore_from_ady.py <ady_path> <avr_host>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from calibrate.audyssey_fir import convert_xt32, FILTER_CONFIGS, is_sub_channel
from calibrate.drivers.denon.audyssey_filter_upload import push_avr_filters


# .ady stores 16384 samples per coefficient bank; XT32 polyphase expects
# 16,321 (speaker) or 16,055 (sub). The .ady padding is at the END of
# the buffer (last 63/329 samples are zero) — drop those to feed the
# decimator.
SPEAKER_INPUT_LEN = FILTER_CONFIGS["xt32Speaker"]["input_length"]  # 16321
SUB_INPUT_LEN = FILTER_CONFIGS["xt32Sub"]["input_length"]          # 16055


def extract_channel_ir(ady_channel: dict, samplerate_key: str = "2") -> list[float]:
    """Pull 48kHz responseData from one .ady detected channel and trim it
    to the XT32 polyphase input length."""
    raw = ady_channel.get("responseData", {}).get(samplerate_key, [])
    vals = [float(v) for v in raw]
    cid = ady_channel.get("commandId", "")
    target_len = SUB_INPUT_LEN if is_sub_channel(cid) else SPEAKER_INPUT_LEN
    if len(vals) >= target_len:
        # Trim trailing zeros / padding.
        return vals[:target_len]
    raise ValueError(f"{cid} responseData has {len(vals)} samples, expected ≥ {target_len}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ady_path")
    ap.add_argument("host")
    ap.add_argument("--channels", default="FL,C,FR,SLA,SRA",
                    help="comma-separated channel commandIds to restore")
    ap.add_argument("--inter-packet-delay-ms", type=float, default=50.0)
    args = ap.parse_args()

    with Path(args.ady_path).open() as f:
        ady = json.load(f)

    target_channels = set(args.channels.split(","))
    channel_filters: dict[str, list[float]] = {}
    for ch in ady.get("detectedChannels", []):
        cid = ch.get("commandId")
        if cid not in target_channels:
            continue
        ir = extract_channel_ir(ch)
        coefs = convert_xt32(ir)
        channel_filters[cid] = coefs
        kind = "sub" if is_sub_channel(cid) else "speaker"
        print(f"  {cid:>4} ({kind}): IR peak={max(abs(x) for x in ir):.4f} "
              f"sum={sum(ir):.4f} → coefs peak={max(abs(x) for x in coefs):.4f} "
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
