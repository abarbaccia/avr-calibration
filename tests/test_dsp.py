"""Tests for calibrate.dsp — biquad filter coefficient generation.

Validates that freq_gain_q_to_biquad produces correctly shaped output for all
supported filter types.  Checks coefficient symmetry and pass-through identity
rather than hardcoding exact floating-point values (which are numerically brittle).

Covers:
  - peaking: flat at 0 dB gain (identity coefficients)
  - peaking: non-zero gain produces asymmetric a/b coefficients
  - low_shelf: correct structure
  - high_shelf: correct structure
  - hpf: DC attenuation (b0+b1+b2 near zero at DC)
  - mandatory_hpf_biquads: returns correct number of sections
  - unsupported filter type raises ValueError
"""

import math
import pytest
from calibrate.dsp import (
    freq_gain_q_to_biquad,
    mandatory_hpf_biquads,
    SAMPLE_RATE_HZ,
    DEFAULT_HPF_ORDER,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _has_biquad_keys(d: dict) -> bool:
    return set(d.keys()) == {"b0", "b1", "b2", "a1", "a2"}


def _db_to_linear(db: float) -> float:
    return 10.0 ** (db / 20.0)


# ── peaking ────────────────────────────────────────────────────────────────────

def test_peaking_returns_biquad_keys() -> None:
    b = freq_gain_q_to_biquad(80.0, 3.0, 0.707, "peaking")
    assert _has_biquad_keys(b)


def test_peaking_zero_gain_is_identity() -> None:
    """0 dB peaking filter: normalised b0 should be 1.0 (A=1 means 1+alpha*A / 1+alpha/A = 1).
    The filter is a pass-through in gain, but b1/b2 are not all 1.0 after normalisation."""
    b = freq_gain_q_to_biquad(80.0, 0.0, 0.707, "peaking")
    # At 0 dB, A = 1.0 so alpha*A = alpha/A, therefore b0 normalises to exactly 1.0
    assert math.isclose(b["b0"], 1.0, rel_tol=1e-6)
    # Gain at Nyquist (z=-1): (b0 - b1 + b2) / (1 - a1 + a2) should be 1.0 (pass-through)
    num = b["b0"] - b["b1"] + b["b2"]
    den = 1.0 - b["a1"] + b["a2"]
    assert math.isclose(abs(num / den), 1.0, rel_tol=1e-3)


def test_peaking_boost_b0_larger_than_cut() -> None:
    """A boost should produce b0 > 1; a cut should produce b0 < 1."""
    boost = freq_gain_q_to_biquad(80.0, 6.0, 0.707, "peaking")
    cut = freq_gain_q_to_biquad(80.0, -6.0, 0.707, "peaking")
    assert boost["b0"] > 1.0
    assert cut["b0"] < 1.0


def test_peaking_boost_is_symmetric_about_centre() -> None:
    """Peaking filter boost and cut at equal magnitude: cut's b-coeffs = boost's a-coeffs.
    This is the EQ Cookbook inverse relationship: H_cut(z) = 1 / H_boost(z)."""
    boost = freq_gain_q_to_biquad(80.0, 3.0, 0.707, "peaking")
    cut = freq_gain_q_to_biquad(80.0, -3.0, 0.707, "peaking")
    # For the cut: b_cut = a_boost (normalised coefficients swap)
    # b0_cut should approximately equal 1/b0_boost (reciprocal relationship)
    assert math.isclose(boost["b0"] * cut["b0"], 1.0, rel_tol=1e-3)


def test_peaking_different_frequencies_different_b1() -> None:
    low = freq_gain_q_to_biquad(40.0, 3.0, 0.707, "peaking")
    high = freq_gain_q_to_biquad(160.0, 3.0, 0.707, "peaking")
    assert not math.isclose(low["b1"], high["b1"], rel_tol=1e-3)


# ── low_shelf ──────────────────────────────────────────────────────────────────

def test_low_shelf_returns_biquad_keys() -> None:
    b = freq_gain_q_to_biquad(40.0, 3.0, 0.707, "low_shelf")
    assert _has_biquad_keys(b)


def test_low_shelf_boost_has_b0_gt_1() -> None:
    b = freq_gain_q_to_biquad(40.0, 6.0, 0.707, "low_shelf")
    assert b["b0"] > 1.0


def test_low_shelf_cut_has_b0_lt_1() -> None:
    b = freq_gain_q_to_biquad(40.0, -6.0, 0.707, "low_shelf")
    assert b["b0"] < 1.0


# ── high_shelf ─────────────────────────────────────────────────────────────────

def test_high_shelf_returns_biquad_keys() -> None:
    b = freq_gain_q_to_biquad(160.0, 3.0, 0.707, "high_shelf")
    assert _has_biquad_keys(b)


def test_high_shelf_boost_has_b0_gt_1() -> None:
    b = freq_gain_q_to_biquad(160.0, 6.0, 0.707, "high_shelf")
    assert b["b0"] > 1.0


# ── hpf ───────────────────────────────────────────────────────────────────────

def test_hpf_returns_biquad_keys() -> None:
    b = freq_gain_q_to_biquad(18.0, 0.0, 0.707, "hpf")
    assert _has_biquad_keys(b)


def test_hpf_attenuates_dc() -> None:
    """For a high-pass filter, the DC gain (z=1 → b0+b1+b2) / (1+a1+a2) should be ~0."""
    b = freq_gain_q_to_biquad(18.0, 0.0, 0.707, "hpf")
    dc_num = b["b0"] + b["b1"] + b["b2"]
    # DC numerator should be near zero (high-pass attenuates DC)
    assert abs(dc_num) < 0.1


def test_hpf_passes_high_frequencies() -> None:
    """Above the cutoff, the gain should be close to 0 dB (gain ≈ 1.0)."""
    b = freq_gain_q_to_biquad(18.0, 0.0, 0.707, "hpf")
    # Evaluate at Nyquist (z = -1): (b0 - b1 + b2) / (1 - a1 + a2)
    num = b["b0"] - b["b1"] + b["b2"]
    den = 1.0 - b["a1"] + b["a2"]
    gain_at_nyquist = abs(num / den)
    # Should be close to 1.0 at Nyquist for an 18 Hz HPF at 96 kHz
    assert math.isclose(gain_at_nyquist, 1.0, rel_tol=0.01)


# ── mandatory_hpf_biquads ──────────────────────────────────────────────────────

def test_mandatory_hpf_returns_correct_section_count() -> None:
    """4th-order Butterworth HPF = 2 second-order sections."""
    sections = mandatory_hpf_biquads(freq=18.0, order=4)
    assert len(sections) == 2


def test_mandatory_hpf_second_order_returns_one_section() -> None:
    sections = mandatory_hpf_biquads(freq=18.0, order=2)
    assert len(sections) == 1


def test_mandatory_hpf_all_sections_have_biquad_keys() -> None:
    sections = mandatory_hpf_biquads(freq=18.0, order=4)
    for section in sections:
        assert _has_biquad_keys(section)


def test_mandatory_hpf_matches_hpf_tool_first_section() -> None:
    """freq_gain_q_to_biquad('hpf') should return same first section as mandatory_hpf_biquads."""
    sections = mandatory_hpf_biquads(freq=18.0, order=4)
    single = freq_gain_q_to_biquad(18.0, 0.0, 0.707, "hpf")
    for key in ["b0", "b1", "b2", "a1", "a2"]:
        assert math.isclose(sections[0][key], single[key], rel_tol=1e-9)


# ── Custom sample rate ─────────────────────────────────────────────────────────

def test_different_sample_rate_changes_coefficients() -> None:
    b_96k = freq_gain_q_to_biquad(80.0, 3.0, 0.707, "peaking", sample_rate=96000)
    b_48k = freq_gain_q_to_biquad(80.0, 3.0, 0.707, "peaking", sample_rate=48000)
    # Different sample rates should produce different b1/a1 coefficients
    assert not math.isclose(b_96k["b1"], b_48k["b1"], rel_tol=1e-3)


# ── Unsupported filter type ────────────────────────────────────────────────────

def test_unsupported_filter_type_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unsupported filter type"):
        freq_gain_q_to_biquad(80.0, 3.0, 0.707, "notch")  # type: ignore


def test_unsupported_filter_type_names_the_type() -> None:
    with pytest.raises(ValueError, match="badtype"):
        freq_gain_q_to_biquad(80.0, 3.0, 0.707, "badtype")  # type: ignore
