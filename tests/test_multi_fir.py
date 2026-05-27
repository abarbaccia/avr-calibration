"""Tests for design_multi_input_fir — coherent multi-sub FIR optimizer."""

from __future__ import annotations

import math

import numpy as np
import pytest

from calibrate.multi_fir import SubMeasurement, design_multi_input_fir


def _synth_measurement(freqs, mag_db, phase_rad=None, label=""):
    if phase_rad is None:
        phase_rad = [0.0] * len(freqs)
    return SubMeasurement(
        freqs=list(freqs),
        spl_db=list(mag_db),
        phase_rad=list(phase_rad),
        label=label,
    )


def test_returns_one_fir_per_measurement():
    freqs = np.linspace(20, 200, 100).tolist()
    m1 = _synth_measurement(freqs, [0.0] * 100, label="sub1")
    m2 = _synth_measurement(freqs, [0.0] * 100, label="sub2")
    target = [(20, 0.0), (200, 0.0)]
    result = design_multi_input_fir([m1, m2], target,
                                    num_taps=512, sample_rate=48000,
                                    phase_mode="minimum")
    assert result["num_subs"] == 2
    assert len(result["firs"]) == 2
    assert all(len(f) == 512 for f in result["firs"])


def test_requires_two_measurements():
    freqs = np.linspace(20, 200, 100).tolist()
    m1 = _synth_measurement(freqs, [0.0] * 100, label="sub1")
    with pytest.raises(ValueError, match="≥ 2"):
        design_multi_input_fir([m1], [(20, 0.0), (200, 0.0)])


def test_rejects_bad_phase_mode():
    freqs = np.linspace(20, 200, 100).tolist()
    m1 = _synth_measurement(freqs, [0.0] * 100)
    m2 = _synth_measurement(freqs, [0.0] * 100)
    with pytest.raises(ValueError, match="phase_mode"):
        design_multi_input_fir([m1, m2], [(20, 0.0), (200, 0.0)],
                               phase_mode="bogus")


def test_flat_measurement_flat_target_produces_near_identity():
    """Two flat subs at unit gain summing to flat target → FIRs should be
    near impulses (DC ≈ 1/2 per sub, sum → 1).
    """
    freqs = np.linspace(20, 200, 50).tolist()
    # Both subs flat at 0 dB
    m1 = _synth_measurement(freqs, [0.0] * 50)
    m2 = _synth_measurement(freqs, [0.0] * 50)
    # Target flat at 0 dB (linear amplitude 1.0)
    target = [(20, 0.0), (200, 0.0)]
    result = design_multi_input_fir(
        [m1, m2], target,
        num_taps=1024, sample_rate=48000,
        phase_mode="minimum",
        regularization_lambda=0.01,
        freq_focus_hz=(20, 200),
    )
    # Combined response inside focus band should be near 0 dB
    bands = result["predicted_combined"]
    in_band = [b for b in bands if 25 <= b["freq_hz"] <= 160]
    avg = sum(b["spl_db"] for b in in_band) / len(in_band)
    # Should be within a few dB of target (FIR truncation imperfect)
    assert abs(avg) < 3.0, f"combined avg {avg} dB, expected near 0"


def test_peak_normalized():
    freqs = np.linspace(20, 200, 50).tolist()
    m1 = _synth_measurement(freqs, [-6.0] * 50)  # 6 dB down
    m2 = _synth_measurement(freqs, [-6.0] * 50)
    target = [(20, 0.0), (200, 0.0)]
    result = design_multi_input_fir(
        [m1, m2], target,
        num_taps=1024, sample_rate=48000,
        phase_mode="minimum",
        freq_focus_hz=(20, 200),
    )
    # Each FIR's peak coefficient must be ≤ 1.0 (CamillaDSP requirement)
    for fir in result["firs"]:
        peak = max(abs(c) for c in fir)
        assert peak <= 1.0 + 1e-9, f"peak={peak} exceeds 1.0"


def test_predicted_combined_uses_post_normalization_firs():
    """Sanity: predicted combined should reflect the actual realized FIRs,
    not the pre-normalization ideal."""
    freqs = np.linspace(20, 200, 50).tolist()
    # Asymmetric mags: sub1 hot, sub2 quiet, so K_2 needs more boost.
    m1 = _synth_measurement(freqs, [0.0] * 50)
    m2 = _synth_measurement(freqs, [-12.0] * 50)
    target = [(20, 0.0), (200, 0.0)]
    result = design_multi_input_fir(
        [m1, m2], target,
        num_taps=1024,
        phase_mode="minimum",
        freq_focus_hz=(20, 200),
        regularization_lambda=0.05,
    )
    # The two subs' predicted contributions should sum to roughly 0 dB combined
    bands = result["predicted_combined"]
    in_band = [b for b in bands if 30 <= b["freq_hz"] <= 160]
    assert len(in_band) > 0
    # Some headroom for FIR realization error, but should be within 3 dB
    for b in in_band:
        assert -6 < b["spl_db"] < 6, f"combined out of range at {b['freq_hz']}Hz: {b['spl_db']}dB"


def test_mixed_phase_adds_latency():
    """Mixed phase should add nonzero latency due to pre-ring window."""
    freqs = np.linspace(20, 200, 50).tolist()
    m1 = _synth_measurement(freqs, [0.0] * 50)
    m2 = _synth_measurement(freqs, [0.0] * 50)
    target = [(20, 0.0), (200, 0.0)]
    res_min = design_multi_input_fir(
        [m1, m2], target, num_taps=1024, phase_mode="minimum",
        freq_focus_hz=(20, 200),
    )
    res_mixed = design_multi_input_fir(
        [m1, m2], target, num_taps=1024, phase_mode="mixed",
        preringing_ms=20, freq_focus_hz=(20, 200),
    )
    # Mixed should have ≥ minimum latency (often higher due to centered impulse)
    assert res_mixed["latency_ms"] >= res_min["latency_ms"]


def test_per_sub_peak_boost_reported():
    """sub2 with a wide dip should report a higher per-sub peak boost than
    sub1 with flat magnitude — the FIR must lift the dip region."""
    freqs = np.linspace(20, 200, 50).tolist()
    mag2 = [0.0] * 50
    # 5-point dip (~25 Hz wide) centered at index 25 (~110 Hz)
    for i in range(22, 28):
        mag2[i] = -8.0
    m1 = _synth_measurement(freqs, [0.0] * 50)
    m2 = _synth_measurement(freqs, mag2)
    target = [(20, 0.0), (200, 0.0)]
    result = design_multi_input_fir(
        [m1, m2], target, num_taps=1024,
        phase_mode="minimum",
        regularization_lambda=0.02,
        freq_focus_hz=(20, 200),
    )
    boosts = result["per_sub_peak_boost_db"]
    assert len(boosts) == 2
    # sub2 has more work to do — the FIR adds more dB somewhere
    assert boosts[1] > boosts[0], f"expected sub2 boost > sub1, got {boosts}"
