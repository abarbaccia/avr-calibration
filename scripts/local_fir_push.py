"""Push locally-designed FIR coefficients to the AVR via Audyssey TCP.

Audyssey TCP only needs network access to the AVR (no Pi/MCP required) so
the design + push loop can run entirely from the dev host once the DB has
been copied over.

Reads coefficient JSON files from .local-db/fir-cache/<cache_key>__<ch>.json
written by scripts/local_fir_design.py.

Run from repo root:
    uv run python scripts/local_fir_push.py --commit
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calibrate.drivers.denon.audyssey_filter_upload import (
    channels_in_ady,
    push_avr_filters,
)

REPO = Path(__file__).resolve().parent.parent
ADY_PATH = REPO / "1_BACKUP_20260427-160223.ady"
CACHE_DIR = REPO / ".local-db" / "fir-cache"
CACHE_KEY = "mains-cal-run28"

AVR_HOST = "192.168.1.209"

# Preserve current SW1=20m alignment from 2026-05-02.
DISTANCE_OVERRIDES_M = {"SW1": 20.0}


def load_channel_filters(cache_key: str, only: list[str] | None = None) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for f in sorted(CACHE_DIR.glob(f"{cache_key}__*.json")):
        if only is not None:
            cid = f.stem.split("__")[1]
            if cid not in only:
                continue
        data = json.loads(f.read_text())
        cid = data["command_id"]
        coefs = data["avr_coefficients"]
        out[cid] = coefs
        s = data["correction_summary"]
        print(
            f"  {cid:4s} ({len(coefs)} taps) — correction "
            f"min {s['min_db']:+.2f} max {s['max_db']:+.2f} "
            f"rms {s['rms_db']:.2f} p95 {s['p95_abs_db']:.2f} dB"
        )
    return out


async def main_async(commit: bool, channels: list[str] | None, target_curves: list[str] | None) -> int:
    if not ADY_PATH.exists():
        print(f"ERROR: .ady not found: {ADY_PATH}", file=sys.stderr)
        return 1
    ady = json.loads(ADY_PATH.read_text())
    available = channels_in_ady(ady)
    print(f"AVR:     {AVR_HOST}")
    print(f".ady:    {ADY_PATH}  ({len(available)} channels: {available})")
    print(f"Cache:   {CACHE_KEY}  ({CACHE_DIR})")
    print(f"Commit:  {commit}")
    print()
    print(f"Channels:   {channels if channels else 'all'}")
    print(f"Curves:     {target_curves if target_curves else 'both (Flat + Reference)'}")
    print()
    print("Channel filters to upload:")
    channel_filters = load_channel_filters(CACHE_KEY, only=channels)
    if not channel_filters:
        print(
            f"ERROR: no FIR cache files found at {CACHE_DIR}/{CACHE_KEY}__*.json — "
            "run scripts/local_fir_design.py first",
            file=sys.stderr,
        )
        return 1
    print()
    if not commit:
        print("Dry run — pass --commit to actually push.")
        return 0

    # X3800H crashed our last attempt with default 30s timeout +
    # 5ms inter-packet delay. Two changes:
    #   - timeout: 30s → 120s (X3800H's CoefWaitTime.Final is 15s alone,
    #     plus 30 streams × ~packets/stream + per-packet pacing)
    #   - inter_packet_delay_ms: 5 → 25 (slow the firehose; reduces
    #     receive-buffer pressure that may have triggered the crash)
    #   - coef_wait_init_ms / coef_wait_final_ms: long enough for the
    #     X3800H's documented CoefWaitTime values
    print("Pushing to AVR via Audyssey TCP/1256 (120s timeout, 25ms inter-packet)...")
    push_kwargs = dict(
        ady=ady,
        channel_filters=channel_filters,
        distances_override_m=DISTANCE_OVERRIDES_M,
        inter_packet_delay_ms=25.0,
        coef_wait_init_ms=2500.0,
        coef_wait_final_ms=20000.0,
        timeout=120.0,
        # Force INIT_COEFS — auto-detect from DType returns False on X3800H
        # but the FINZ_COEFS ACK never lands without it on this AVR.
        init_coefs_required=True,
    )
    if target_curves:
        push_kwargs["target_curves"] = tuple(target_curves)
    summary = await push_avr_filters(AVR_HOST, **push_kwargs)
    print()
    print("Push summary:")
    for k, v in summary.items():
        if isinstance(v, (str, int, bool)):
            print(f"  {k}: {v}")
        elif isinstance(v, list) and v and isinstance(v[0], (str, bool, int)):
            print(f"  {k}: {v}")
        else:
            print(f"  {k}: ({type(v).__name__})")
    print()
    if summary.get("ok"):
        print("✅ Upload OK. Re-measure to verify.")
        print(
            "⚠️  DO NOT enter Manual Setup > Distances on the AVR remote — "
            "that triggers firmware re-validation that snaps Distance back "
            "to the variance cap."
        )
        return 0
    else:
        print("❌ Upload reported failure. AVR may be in a partial state.")
        return 2


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true", help="Actually push to the AVR.")
    p.add_argument(
        "--channels", nargs="*",
        help="Subset of channel commandIds to push (e.g. C FL FR). Default = all in cache.",
    )
    p.add_argument(
        "--curves", nargs="*", choices=["00", "01"],
        help="Target curve banks: 00=Flat, 01=Reference. Default = both.",
    )
    args = p.parse_args()
    return asyncio.run(main_async(args.commit, args.channels, args.curves))


if __name__ == "__main__":
    sys.exit(main())
