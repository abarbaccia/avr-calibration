"""Decay analysis — spectrogram-based T60 estimation for room mode identification.

Identifies ringing modes in a room's impulse response by computing a spectrogram,
applying Schroeder integration per frequency bin, and estimating T60 (time for
energy to decay 60dB). Modes with T60 > 300ms are flagged for correction.

FIR filters shorten these decays (time-domain correction); PEQ cuts the peak
magnitude but cannot shorten the ringing duration. Whether FIR is available
depends on the DSP device (check eq_capabilities.fir_capable from get_config).
The miniDSP 2x4 HD supports FIR (2048 taps/output); use apply_fir for decay
correction. On IIR-only devices, the suggested_q values drive narrow PEQ cuts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class DecayMode:
    """A room mode identified by decay analysis."""
    freq_hz: float       # center frequency of the ringing mode
    t60_ms: float        # estimated T60 in milliseconds
    peak_db: float       # peak magnitude in dB relative to broadband
    suggested_q: float   # longer decay → narrower Q for EQ targeting
    priority: int        # 1 = highest (T60 × peak_amplitude)


def analyze_decay(
    impulse_response: list[float],
    sample_rate: int = 48000,
    t60_threshold_ms: float = 300.0,
    freq_min: float = 20.0,
    freq_max: float = 200.0,
    nperseg: int = 2048,
    noverlap: int = 1536,
) -> list[DecayMode]:
    """Analyze impulse response for ringing modes via spectrogram + Schroeder integration.

    Algorithm:
    1. scipy.signal.spectrogram(ir, fs, nperseg, noverlap)
    2. Per frequency bin: Schroeder integration (cumulative energy sum in reverse)
    3. T60 estimation: linear fit from -5dB to -35dB on Schroeder curve, extrapolate to -60dB
    4. Filter to modes with T60 > threshold in freq_min..freq_max range
    5. Priority scoring: T60_ms × abs(peak_db)
    6. Suggested Q: map T60 to Q (longer decay → narrower Q, range 1.0-10.0)

    Args:
        impulse_response: time-domain IR samples (48K samples = 1 second at 48kHz)
        sample_rate: sample rate in Hz
        t60_threshold_ms: minimum T60 to flag as a ringing mode (default 300ms)
        freq_min: lower frequency bound for mode search
        freq_max: upper frequency bound for mode search
        nperseg: spectrogram window size in samples
        noverlap: spectrogram overlap in samples

    Returns:
        List of DecayMode sorted by priority (highest first).
        Empty list if no modes exceed threshold or IR is too short.

    Raises:
        ValueError: if impulse_response is empty or all zeros
    """
    import numpy as np
    from scipy.signal import spectrogram

    ir = np.array(impulse_response, dtype=np.float64)

    if len(ir) == 0:
        raise ValueError("impulse_response is empty")
    if np.all(ir == 0):
        raise ValueError("impulse_response is all zeros")

    # Guard against IR shorter than spectrogram window
    if len(ir) < nperseg:
        log.warning("IR length %d < nperseg %d, no decay analysis possible", len(ir), nperseg)
        return []

    # Compute spectrogram
    freqs, times, Sxx = spectrogram(
        ir, fs=sample_rate, nperseg=nperseg, noverlap=noverlap,
        scaling='spectrum',
    )

    # Filter to bass frequency range
    freq_mask = (freqs >= freq_min) & (freqs <= freq_max)
    freqs_bass = freqs[freq_mask]
    Sxx_bass = Sxx[freq_mask, :]

    if len(freqs_bass) == 0 or Sxx_bass.shape[1] < 3:
        return []

    # Broadband reference level: mean only over bins with non-negligible energy.
    # Using np.mean(Sxx_bass) directly would include near-zero bins, driving the
    # mean to ~0 and producing astronomically large peak_db values (+300 dB range).
    active_mask = np.max(Sxx_bass, axis=1) >= 1e-20
    active_energy = Sxx_bass[active_mask, :]
    broadband_energy = np.mean(active_energy) if active_energy.size > 0 else 1e-20

    modes: list[DecayMode] = []

    for i, freq in enumerate(freqs_bass):
        energy = Sxx_bass[i, :]

        # Skip bins with negligible energy
        if np.max(energy) < 1e-20:
            continue

        # Schroeder integration: reverse cumulative sum of energy
        schroeder = np.cumsum(energy[::-1])[::-1]
        schroeder_db = 10.0 * np.log10(schroeder / (schroeder[0] + 1e-30) + 1e-30)

        # T60 estimation: linear fit from -5dB to -35dB
        t60_ms = _estimate_t60(schroeder_db, times)

        if t60_ms is None or t60_ms < t60_threshold_ms:
            continue

        # Peak magnitude relative to broadband
        peak_energy = np.max(energy)
        peak_db = 10.0 * np.log10(peak_energy / (broadband_energy + 1e-30) + 1e-30)

        # Suggested Q: longer decay → narrower Q (1.0 to 10.0)
        suggested_q = _t60_to_q(t60_ms)

        modes.append(DecayMode(
            freq_hz=round(float(freq), 1),
            t60_ms=round(float(t60_ms), 1),
            peak_db=round(float(peak_db), 1),
            suggested_q=round(float(suggested_q), 2),
            priority=0,  # assigned below
        ))

    # Assign priority by T60 × |peak_db| (highest score = priority 1)
    modes.sort(key=lambda m: m.t60_ms * abs(m.peak_db), reverse=True)
    for rank, mode in enumerate(modes, 1):
        mode.priority = rank

    return modes


def _estimate_t60(schroeder_db: "np.ndarray", times: "np.ndarray") -> float | None:
    """Estimate T60 from Schroeder decay curve via linear regression on -5 to -35dB range.

    Returns T60 in milliseconds, or None if the decay range is insufficient.
    """
    import numpy as np

    # Find indices where decay is between -5 and -35 dB
    mask = (schroeder_db >= -35.0) & (schroeder_db <= -5.0)

    if np.sum(mask) < 3:
        # Not enough points for reliable linear fit
        return None

    t_fit = times[mask]
    db_fit = schroeder_db[mask]

    try:
        # Linear fit: db = slope * t + intercept
        coeffs = np.polyfit(t_fit, db_fit, 1)
        slope = coeffs[0]

        if slope >= 0:
            # Decay is not actually decaying
            return None

        # T60 = time for 60dB of decay = -60 / slope
        t60_s = -60.0 / slope
        return t60_s * 1000.0  # convert to ms

    except (np.linalg.LinAlgError, ValueError):
        return None


def _t60_to_q(t60_ms: float) -> float:
    """Map T60 to suggested Q value for EQ targeting.

    Longer decay → narrower Q (higher Q value) for more precise targeting.
    Range: 1.0 (very short decay, broad) to 10.0 (very long decay, surgical).
    """
    import numpy as np
    # Log-linear mapping: 300ms → Q=1.0, 3000ms → Q=10.0
    q = 1.0 + 9.0 * np.clip(
        (np.log10(t60_ms) - np.log10(300)) / (np.log10(3000) - np.log10(300)),
        0.0, 1.0,
    )
    return float(q)


def compare_decay(
    before_ir: list[float],
    after_ir: list[float],
    sample_rate: int = 48000,
    freq_min: float = 20.0,
    freq_max: float = 200.0,
) -> list[dict]:
    """Compare decay before and after correction.

    Runs analyze_decay on both IRs and matches modes by frequency (within 1/6 octave).
    Returns per-mode T60 comparison showing improvement.

    Args:
        before_ir: impulse response before correction
        after_ir: impulse response after correction
        sample_rate: sample rate in Hz

    Returns:
        List of dicts: [{freq_hz, t60_before_ms, t60_after_ms, reduction_pct}]
        sorted by reduction_pct descending (biggest improvements first).
    """
    import numpy as np

    before_modes = analyze_decay(before_ir, sample_rate, t60_threshold_ms=0.0,
                                  freq_min=freq_min, freq_max=freq_max)
    after_modes = analyze_decay(after_ir, sample_rate, t60_threshold_ms=0.0,
                                 freq_min=freq_min, freq_max=freq_max)

    comparisons: list[dict] = []

    for bm in before_modes:
        # Find matching after-mode within 1/6 octave
        match = None
        for am in after_modes:
            ratio = am.freq_hz / bm.freq_hz
            if 2 ** (-1/12) <= ratio <= 2 ** (1/12):  # within 1/6 octave
                match = am
                break

        t60_after = match.t60_ms if match else 0.0
        reduction = ((bm.t60_ms - t60_after) / bm.t60_ms * 100.0) if bm.t60_ms > 0 else 0.0

        comparisons.append({
            "freq_hz": bm.freq_hz,
            "t60_before_ms": bm.t60_ms,
            "t60_after_ms": round(t60_after, 1),
            "reduction_pct": round(reduction, 1),
        })

    comparisons.sort(key=lambda c: c["reduction_pct"], reverse=True)
    return comparisons
