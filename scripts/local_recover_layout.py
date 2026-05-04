"""Recovery: re-push the full .ady envelope to restore wiped speaker channels.

Use after an Audyssey FIR upload that crashed the X3800H and left ChSetup
with channels marked "N" (not present). Pushes the full A1Evo-format
envelope from the .ady, with our committed distance + level overrides
preserved.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calibrate.drivers.denon.audyssey_tcp import push_full_envelope_from_ady

REPO = Path(__file__).resolve().parent.parent
ADY_PATH = REPO / "1_BACKUP_20260427-160223.ady"
AVR_HOST = "192.168.1.209"

# PROPER values — replaces the run-28 values that were derived from
# Dolby-upmix-corrupted measurements (pre-multichannel-fix).
#
# Distances: keep .ady values (Audyssey-calibrated baseline) untouched.
# Direct-mode peak times produced opposite-signed deltas vs the .ady,
# suggesting deconvolution-window artifacts rather than real acoustic
# differences. Only override SW1=20m for sub-chain FIR-latency
# compensation (verified working 2026-05-02).
#
# Levels: from sessions 975-979 mid-band SPL (500 Hz – 2 kHz),
# Audyssey-OFF measurements through the multichannel-fixed HDMI path.
# Reference channel = FL.
DISTANCE_OVERRIDES_M = {
    # Computed from sessions 975-979 IR peak times (post-fix DIRECT-mode
    # measurements). Reference (slowest) = SLA at 277.15 ms. Each channel's
    # stored distance is REDUCED by (peak_delta × 343 / 1000) m to add the
    # equivalent Audyssey delay, bringing all arrivals to the SLA peak time.
    # Caveat: assumes DIRECT mode does not apply Audyssey distance comp.
    # If that assumption is wrong, these will over-compensate — iterate
    # against fresh measurements after the push.
    "FL":  2.53,
    "C":   2.57,
    "FR":  4.16,
    "SLA": 2.63,
    "SRA": 0.60,
    "SW1": 20.0,
}
LEVEL_OVERRIDES_DB = {
    "FL": 0.0,
    "C": -1.9,
    "FR": -1.4,
    "SLA": -5.5,
    "SRA": -6.3,
}


async def main() -> int:
    ady = json.loads(ADY_PATH.read_text())
    print(f"Pushing full envelope from {ADY_PATH.name} to {AVR_HOST}...")
    print(f"Distance overrides: {DISTANCE_OVERRIDES_M}")
    print(f"Level overrides:    {LEVEL_OVERRIDES_DB}")
    ok = await push_full_envelope_from_ady(
        host=AVR_HOST,
        ady=ady,
        distance_overrides_m=DISTANCE_OVERRIDES_M,
        level_overrides_db=LEVEL_OVERRIDES_DB,
        commit=True,
    )
    print(f"\nResult: {'OK' if ok else 'PARTIAL — some SET_SETDAT NACKd'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
