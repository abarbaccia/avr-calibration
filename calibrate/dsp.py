"""DSP utilities — human-readable filter spec to biquad coefficients.

Converts human-readable filter specifications (frequency, gain, Q, type) to
Audio EQ Cookbook biquad coefficients (b0, b1, b2, a1, a2) normalised to a0=1.
Callers pass the DSP's processing sample rate (see ``DSPCapabilities``); the
default matches miniDSP 2x4 HD.

Supported filter types:
  - ``peaking``    — parametric EQ peak/notch
  - ``low_shelf``  — low-frequency shelving filter
  - ``high_shelf`` — high-frequency shelving filter
  - ``hpf``        — high-pass filter (Butterworth, variable order)
  - ``allpass``    — unity-magnitude phase-only filter (RBJ APF)

Usage::

    from calibrate.dsp import freq_gain_q_to_biquad

    biquad = freq_gain_q_to_biquad(
        freq=80.0, gain_db=-3.0, q=0.7, filter_type="peaking",
        sample_rate=driver.capabilities.processing_rate,
    )
    # → {"b0": ..., "b1": ..., "b2": ..., "a1": ..., "a2": ...}

References:
  Audio EQ Cookbook — Robert Bristow-Johnson
  scipy.signal.iirfilter / sosfilt for multi-order HPF
"""

from __future__ import annotations

import math
from typing import Literal

import numpy as np
from scipy import signal as _signal

# ── Types ──────────────────────────────────────────────────────────────────────

FilterType = Literal["peaking", "low_shelf", "high_shelf", "hpf", "allpass"]

BiquadCoeffs = dict[str, float]
"""Biquad coefficients in minidspd form: {b0, b1, b2, a1, a2}."""

# ── Constants ──────────────────────────────────────────────────────────────────

SAMPLE_RATE_HZ: int = 96_000
"""Default DSP processing sample rate in Hz.

Used only when the caller does not pass an explicit ``sample_rate``. Drivers
should query their own rate via ``DSPDriver.capabilities.processing_rate`` and
pass it through (miniDSP 2x4 HD: 96_000 Hz; CamillaDSP: whatever the YAML
pipeline declares).
"""

DEFAULT_HPF_ORDER: int = 4
"""Default Butterworth HPF order (matches CLAUDE.md mandatory infrasonic HPF)."""


# ── Public API ─────────────────────────────────────────────────────────────────

def freq_gain_q_to_biquad(
    freq: float,
    gain_db: float,
    q: float,
    filter_type: FilterType,
    sample_rate: int = SAMPLE_RATE_HZ,
    hpf_order: int = DEFAULT_HPF_ORDER,
) -> BiquadCoeffs:
    """Convert a human-readable filter spec to Audio EQ Cookbook biquad coeffs.

    Args:
        freq:        Centre / corner frequency in Hz.
        gain_db:     Gain in dB.  Ignored for HPF/LPF.
        q:           Quality factor.  Ignored for HPF/LPF.
        filter_type: One of 'peaking', 'low_shelf', 'high_shelf', 'hpf'.
        sample_rate: DSP processing sample rate in Hz. Prefer passing
                     ``driver.capabilities.processing_rate`` rather than
                     relying on the default.
        hpf_order:   Butterworth order for HPF (default: 4).

    Returns:
        Dict with keys b0, b1, b2, a1, a2 (normalised: a0 = 1).

    Raises:
        ValueError: if filter_type is not supported.
    """
    if filter_type == "peaking":
        return _peaking(freq, gain_db, q, sample_rate)
    elif filter_type == "low_shelf":
        return _low_shelf(freq, gain_db, q, sample_rate)
    elif filter_type == "high_shelf":
        return _high_shelf(freq, gain_db, q, sample_rate)
    elif filter_type == "hpf":
        return _hpf(freq, sample_rate, order=hpf_order)
    elif filter_type == "allpass":
        return _allpass(freq, q, sample_rate)
    else:
        raise ValueError(
            f"Unsupported filter type: {filter_type!r}. "
            f"Must be one of: peaking, low_shelf, high_shelf, hpf, allpass"
        )


def mandatory_hpf_biquads(
    freq: float = 18.0,
    order: int = DEFAULT_HPF_ORDER,
    sample_rate: int = SAMPLE_RATE_HZ,
) -> list[BiquadCoeffs]:
    """Return the mandatory infrasonic HPF as a list of biquad sections.

    A 4th-order Butterworth HPF is two cascaded 2nd-order sections. For PEQ
    backends that hold one biquad per slot (e.g. miniDSP 2x4 HD), a 4th-order
    HPF occupies 2 slots.

    Returns a list of BiquadCoeffs dicts, one per biquad section.
    """
    # Design as second-order sections (sos) then convert each section
    sos = _signal.butter(order, freq, btype="high", fs=sample_rate, output="sos")
    result = []
    for section in sos:
        b0, b1, b2 = float(section[0]), float(section[1]), float(section[2])
        a0, a1, a2 = float(section[3]), float(section[4]), float(section[5])
        # Normalise by a0 (should be 1.0 from scipy but be explicit)
        result.append({
            "b0": b0 / a0,
            "b1": b1 / a0,
            "b2": b2 / a0,
            "a1": a1 / a0,
            "a2": a2 / a0,
        })
    return result


# ── Private implementations ────────────────────────────────────────────────────

def _omega_and_alpha(freq: float, q: float, sample_rate: int) -> tuple[float, float]:
    """Pre-compute the omega and alpha values shared by peaking and shelf filters."""
    w0 = 2.0 * math.pi * freq / sample_rate
    alpha = math.sin(w0) / (2.0 * q)
    return w0, alpha


def _normalise(b0: float, b1: float, b2: float,
               a0: float, a1: float, a2: float) -> BiquadCoeffs:
    """Return coefficients normalised by a0."""
    return {
        "b0": b0 / a0,
        "b1": b1 / a0,
        "b2": b2 / a0,
        "a1": a1 / a0,
        "a2": a2 / a0,
    }


def _peaking(freq: float, gain_db: float, q: float, sample_rate: int) -> BiquadCoeffs:
    """Peaking EQ — Audio EQ Cookbook peakingEQ."""
    w0, alpha = _omega_and_alpha(freq, q, sample_rate)
    A = 10.0 ** (gain_db / 40.0)
    cos_w0 = math.cos(w0)

    b0 = 1.0 + alpha * A
    b1 = -2.0 * cos_w0
    b2 = 1.0 - alpha * A
    a0 = 1.0 + alpha / A
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha / A
    return _normalise(b0, b1, b2, a0, a1, a2)


def _low_shelf(freq: float, gain_db: float, q: float, sample_rate: int) -> BiquadCoeffs:
    """Low-shelf filter — RBJ Q-based lowShelf, matching CamillaDSP's formula.

    Uses alpha = sin(w0)/2 * sqrt((A + 1/A)*(1/q - 1) + 2), which is the
    shelf-specific Q formula from the Audio EQ Cookbook. This differs from the
    peaking EQ alpha (sin(w0)/(2*q)) — mixing them produces group-delay changes
    without the expected amplitude effect, the bug observed 2026-05-25.

    Q is clamped to [0.1, 3.0]: values above 3.0 make the inner sqrt term go
    negative at typical boost levels (> 3 dB). Practical shelf Q is 0.5-1.5.
    """
    q = max(0.1, min(q, 3.0))
    w0 = 2.0 * math.pi * freq / sample_rate
    A = 10.0 ** (gain_db / 40.0)
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = sin_w0 / 2.0 * math.sqrt((A + 1.0 / A) * (1.0 / q - 1.0) + 2.0)
    sqrt_A = math.sqrt(A)

    b0 = A * ((A + 1.0) - (A - 1.0) * cos_w0 + 2.0 * sqrt_A * alpha)
    b1 = 2.0 * A * ((A - 1.0) - (A + 1.0) * cos_w0)
    b2 = A * ((A + 1.0) - (A - 1.0) * cos_w0 - 2.0 * sqrt_A * alpha)
    a0 = (A + 1.0) + (A - 1.0) * cos_w0 + 2.0 * sqrt_A * alpha
    a1 = -2.0 * ((A - 1.0) + (A + 1.0) * cos_w0)
    a2 = (A + 1.0) + (A - 1.0) * cos_w0 - 2.0 * sqrt_A * alpha
    return _normalise(b0, b1, b2, a0, a1, a2)


def _high_shelf(freq: float, gain_db: float, q: float, sample_rate: int) -> BiquadCoeffs:
    """High-shelf filter — RBJ Q-based highShelf, matching CamillaDSP's formula.

    Q is clamped to [0.1, 3.0] for the same reason as _low_shelf.
    """
    q = max(0.1, min(q, 3.0))
    w0 = 2.0 * math.pi * freq / sample_rate
    A = 10.0 ** (gain_db / 40.0)
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = sin_w0 / 2.0 * math.sqrt((A + 1.0 / A) * (1.0 / q - 1.0) + 2.0)
    sqrt_A = math.sqrt(A)

    b0 = A * ((A + 1.0) + (A - 1.0) * cos_w0 + 2.0 * sqrt_A * alpha)
    b1 = -2.0 * A * ((A - 1.0) + (A + 1.0) * cos_w0)
    b2 = A * ((A + 1.0) + (A - 1.0) * cos_w0 - 2.0 * sqrt_A * alpha)
    a0 = (A + 1.0) - (A - 1.0) * cos_w0 + 2.0 * sqrt_A * alpha
    a1 = 2.0 * ((A - 1.0) - (A + 1.0) * cos_w0)
    a2 = (A + 1.0) - (A - 1.0) * cos_w0 - 2.0 * sqrt_A * alpha
    return _normalise(b0, b1, b2, a0, a1, a2)


def _allpass(freq: float, q: float, sample_rate: int) -> BiquadCoeffs:
    """All-pass filter — Audio EQ Cookbook APF.

    Unity magnitude at all frequencies; phase wraps from 0 at low f
    through -180 at f0 to -360 (= 0) at high f. Useful for frequency-
    specific phase manipulation (e.g., aligning two subs at one mode
    without affecting the magnitude response).
    """
    w0, alpha = _omega_and_alpha(freq, q, sample_rate)
    cos_w0 = math.cos(w0)

    b0 = 1.0 - alpha
    b1 = -2.0 * cos_w0
    b2 = 1.0 + alpha
    a0 = 1.0 + alpha
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha
    return _normalise(b0, b1, b2, a0, a1, a2)


def _hpf(freq: float, sample_rate: int, order: int = DEFAULT_HPF_ORDER) -> BiquadCoeffs:
    """High-pass Butterworth filter — returns the first biquad section.

    For a single-section HPF (order=2), returns the complete filter.
    For higher-order filters, use mandatory_hpf_biquads() which returns
    all cascaded sections.

    This function returns only the first section — suitable for single-slot
    writes.  Use mandatory_hpf_biquads() for the full 4th-order HPF.
    """
    sections = mandatory_hpf_biquads(freq=freq, order=order, sample_rate=sample_rate)
    return sections[0]
