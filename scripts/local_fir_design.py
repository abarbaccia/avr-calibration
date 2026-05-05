"""Run FIR design locally against the copied SQLite history.db.

For each channel (FL, C, FR, SLA, SRA), reads the corresponding fresh DIRECT-
mode measurement (sessions 975–979), computes a correction toward
harman-plus-4, designs the AVR-format polyphase FIR, and writes the
coefficients out to JSON files that can be uploaded with apply_avr_fir.

Run from repo root:
    uv run python scripts/local_fir_design.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calibrate.audyssey_fir import (
    convert_xt32,
    design_correction_ir,
    is_sub_channel,
)
from calibrate.measurement import FrequencyResponse

REPO = Path(__file__).resolve().parent.parent
DB_PATH = REPO / ".local-db" / "history.db"
CURVE_PATH = REPO / "recipes" / "curves" / "harman-plus-4.json"
OUT_DIR = REPO / ".local-db" / "fir-cache"
OUT_DIR.mkdir(parents=True, exist_ok=True)

CACHE_KEY = "mains-cal-run28"

# Channel → (session_id, AVR commandId, correction passband)
# We restrict the correction band to 200 Hz – 12 kHz: below 200 Hz the
# bass shelf is the sub bus's job (and DIRECT-mode mains measurements
# include strong room modes the FIR can't fix); above 12 kHz mic/position
# bias and tweeter rolloff produce saturating corrections that mostly
# trade one room-position artifact for another.
CHANNELS = [
    ("FL",  975, "FL",  (200.0, 12000.0)),
    ("C",   976, "C",   (200.0, 12000.0)),
    ("FR",  977, "FR",  (200.0, 12000.0)),
    ("SLA", 978, "SLA", (200.0, 12000.0)),
    ("SRA", 979, "SRA", (200.0, 12000.0)),
]

# Conservative cap. Audyssey's hard limit is ±6 dB. ±3 here gives the FIR
# room to do real shaping while keeping headroom and minimizing the risk
# of overcorrecting transient measurement artifacts.
GAIN_CAP_DB = 3.0

# 1/3 octave — smooth enough that we're targeting overall tonal balance,
# not chasing narrow modes that move with room position.
SMOOTH_FRAC_OCTAVE = 3


def load_target_curve(path: Path) -> tuple[np.ndarray, np.ndarray]:
    data = json.loads(path.read_text())
    pts = data["points"]
    freqs = np.array([p["freq_hz"] for p in pts], dtype=np.float64)
    gains = np.array([p["offset_db"] for p in pts], dtype=np.float64)
    return freqs, gains


def fractional_octave_smooth(
    freqs: np.ndarray, spl_db: np.ndarray, frac: int
) -> np.ndarray:
    """Smooth an SPL curve in 1/frac-octave windows."""
    log_f = np.log2(np.maximum(freqs, 1e-3))
    half_width = 1.0 / (2.0 * frac)
    out = np.empty_like(spl_db)
    # The freq grid is dense, so a simple boxcar in log-space is fine.
    for i, lf in enumerate(log_f):
        mask = (log_f >= lf - half_width) & (log_f <= lf + half_width)
        out[i] = float(np.mean(spl_db[mask]))
    return out


def design_for_channel(
    db: sqlite3.Connection,
    label: str,
    session_id: int,
    command_id: str,
    passband: tuple[float, float],
    target_freqs: np.ndarray,
    target_gains: np.ndarray,
) -> dict:
    row = db.execute("SELECT start_fr FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None or row[0] is None:
        raise RuntimeError(f"session {session_id} not found or missing start_fr")
    fr = FrequencyResponse.from_json(row[0])
    freqs = np.asarray(fr.frequencies, dtype=np.float64)
    spl = np.asarray(fr.spl, dtype=np.float64)

    # Smooth the measured SPL.
    smoothed = fractional_octave_smooth(freqs, spl, SMOOTH_FRAC_OCTAVE)

    # Anchor at 1 kHz: shift measured so spl(1kHz) → 0 dB. Correction then
    # operates on relative shape only — Audyssey level trim handles absolute
    # SPL.
    f_ref = 1000.0
    idx_ref = int(np.argmin(np.abs(freqs - f_ref)))
    anchor = smoothed[idx_ref]
    measured_rel = smoothed - anchor

    # Interpolate harman-plus-4 onto the measurement grid (log-axis).
    log_meas = np.log10(np.maximum(freqs, 1e-3))
    log_tgt = np.log10(np.maximum(target_freqs, 1e-3))
    target_at_meas = np.interp(log_meas, log_tgt, target_gains)

    # Correction: drive measured_rel to target_at_meas.
    correction = target_at_meas - measured_rel

    # Outside the speaker's passband, taper to 0 dB so the FIR doesn't try
    # to boost what the speaker can't reproduce.
    f_lo, f_hi = passband
    correction = np.where(freqs < f_lo, 0.0, correction)
    correction = np.where(freqs > f_hi, 0.0, correction)

    # Cap.
    correction = np.clip(correction, -GAIN_CAP_DB, GAIN_CAP_DB)

    # Build a sparse target-curve list (one point per ~1/12 octave) for
    # design_correction_ir. The function does its own log-axis interpolation.
    n_target = 96  # ~10 octaves * 12/oct ≈ 120; 96 is plenty
    log_lo = np.log10(max(freqs[0], 20.0))
    log_hi = np.log10(min(freqs[-1], 20000.0))
    log_grid = np.linspace(log_lo, log_hi, n_target)
    sparse_freqs = 10 ** log_grid
    sparse_gains = np.interp(log_grid, log_meas, correction)

    # Design the FIR.
    is_sub = is_sub_channel(command_id)
    ir = design_correction_ir(
        sparse_freqs.tolist(),
        sparse_gains.tolist(),
        is_sub=is_sub,
        samplerate_hz=48000.0,
    )

    # Polyphase-decimate to AVR format (1024 for speaker, 704 for sub).
    avr_coeffs = convert_xt32(ir)

    return {
        "label": label,
        "session_id": session_id,
        "command_id": command_id,
        "is_sub": is_sub,
        "passband_hz": list(passband),
        "anchor_freq_hz": f_ref,
        "anchor_spl_db": float(anchor),
        "correction_summary": {
            "min_db": float(correction.min()),
            "max_db": float(correction.max()),
            "rms_db": float(np.sqrt(np.mean(correction**2))),
            "p95_abs_db": float(np.percentile(np.abs(correction), 95)),
        },
        "sparse_target_curve": [
            {"freq_hz": float(f), "gain_db": float(g)}
            for f, g in zip(sparse_freqs, sparse_gains)
        ],
        "avr_coefficients": avr_coeffs,
    }


def main() -> int:
    target_freqs, target_gains = load_target_curve(CURVE_PATH)
    db = sqlite3.connect(DB_PATH)
    print(f"DB:      {DB_PATH}")
    print(f"Curve:   {CURVE_PATH}  (anchored at 1 kHz)")
    print(f"Cache:   {CACHE_KEY}")
    print(f"Outdir:  {OUT_DIR}")
    print()
    print(f"{'ch':4s} {'sess':>4s} {'cmd':4s} {'min':>6s} {'max':>6s} {'rms':>5s} {'p95':>5s} {'taps':>5s}")
    for label, sid, cid, passband in CHANNELS:
        result = design_for_channel(
            db, label, sid, cid, passband, target_freqs, target_gains,
        )
        s = result["correction_summary"]
        n = len(result["avr_coefficients"])
        print(
            f"{label:4s} {sid:>4d} {cid:4s} "
            f"{s['min_db']:+6.2f} {s['max_db']:+6.2f} "
            f"{s['rms_db']:>5.2f} {s['p95_abs_db']:>5.2f} {n:>5d}"
        )
        out_path = OUT_DIR / f"{CACHE_KEY}__{cid}.json"
        out_path.write_text(json.dumps(result, separators=(",", ":")))

    print()
    print(f"Wrote {len(CHANNELS)} FIR coefficient files to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
