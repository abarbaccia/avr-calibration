"""Tests for SafetyValidator — all limits, both pass and reject cases.

Covers:
  - Boost frequency floor (< 25 Hz rejected)
  - Per-band boost ceiling (+6 dB max)
  - Cumulative 1/3-octave boost ceiling (+9 dB)
  - Per-iteration change limit (+3 dB max increase)
  - Mandatory HPF presence
  - Cuts always pass (no floor)
  - Multiple filters in one call
"""

import pytest
from calibrate.safety import FilterSpec, SafetyValidator, ValidationResult


@pytest.fixture
def validator() -> SafetyValidator:
    return SafetyValidator()


def make_filter(
    freq: float = 80.0,
    gain_db: float = 0.0,
    q: float = 0.707,
    type: str = "peaking",
) -> FilterSpec:
    return FilterSpec(freq=freq, gain_db=gain_db, q=q, type=type)


def hpf(freq: float = 18.0) -> FilterSpec:
    return FilterSpec(freq=freq, gain_db=0.0, q=0.707, type="hpf")


# ── Mandatory HPF ──────────────────────────────────────────────────────────────

def test_missing_hpf_fails(validator: SafetyValidator) -> None:
    filters = [make_filter(80.0, 0.0)]  # no HPF
    result = validator.validate(filters)
    assert not result.ok
    assert "mandatory" in result.error.lower() or "hpf" in result.error.lower()


def test_hpf_present_passes(validator: SafetyValidator) -> None:
    filters = [hpf(18.0), make_filter(80.0, 0.0)]
    result = validator.validate(filters)
    assert result.ok


def test_hpf_below_limit_passes(validator: SafetyValidator) -> None:
    """HPF at 15 Hz is also acceptable (≤ 18 Hz)."""
    filters = [hpf(15.0), make_filter(80.0, 0.0)]
    result = validator.validate(filters)
    assert result.ok


def test_hpf_above_limit_fails(validator: SafetyValidator) -> None:
    """HPF at 25 Hz does not satisfy the infrasonic protection requirement."""
    filters = [hpf(25.0), make_filter(80.0, 0.0)]
    result = validator.validate(filters)
    assert not result.ok
    assert "hpf" in result.error.lower() or "mandatory" in result.error.lower()


# ── Boost frequency floor ──────────────────────────────────────────────────────

def test_boost_below_25hz_fails(validator: SafetyValidator) -> None:
    filters = [hpf(), make_filter(freq=20.0, gain_db=3.0)]
    result = validator.validate(filters)
    assert not result.ok
    assert "25" in result.error or "minimum" in result.error.lower()


def test_boost_at_25hz_passes(validator: SafetyValidator) -> None:
    filters = [hpf(), make_filter(freq=25.0, gain_db=3.0)]
    result = validator.validate(filters)
    assert result.ok


def test_boost_above_25hz_passes(validator: SafetyValidator) -> None:
    filters = [hpf(), make_filter(freq=80.0, gain_db=3.0)]
    result = validator.validate(filters)
    assert result.ok


def test_cut_below_25hz_passes(validator: SafetyValidator) -> None:
    """Cuts at any frequency are always safe."""
    filters = [hpf(), make_filter(freq=20.0, gain_db=-6.0)]
    result = validator.validate(filters)
    assert result.ok


# ── Per-band boost ceiling ─────────────────────────────────────────────────────

def test_boost_at_6db_passes(validator: SafetyValidator) -> None:
    filters = [hpf(), make_filter(80.0, 6.0)]
    result = validator.validate(filters)
    assert result.ok


def test_boost_above_limit_at_80hz_fails(validator: SafetyValidator) -> None:
    """80 Hz is above 30 Hz → limit is +8 dB. 8.1 dB should fail."""
    filters = [hpf(), make_filter(80.0, 8.1)]
    result = validator.validate(filters)
    assert not result.ok
    assert "8" in result.error


def test_boost_7db_at_80hz_passes(validator: SafetyValidator) -> None:
    """80 Hz is above 30 Hz → +7 dB is under the +8 dB limit."""
    filters = [hpf(), make_filter(80.0, 7.0)]
    result = validator.validate(filters)
    assert result.ok


def test_boost_8db_at_80hz_passes(validator: SafetyValidator) -> None:
    """80 Hz is above 30 Hz → +8 dB is exactly at the limit."""
    filters = [hpf(), make_filter(80.0, 8.0)]
    result = validator.validate(filters)
    assert result.ok


def test_boost_above_6db_below_30hz_fails(validator: SafetyValidator) -> None:
    """25 Hz is below 30 Hz → limit is still +6 dB. 6.1 dB should fail."""
    filters = [hpf(), make_filter(25.0, 6.1)]
    result = validator.validate(filters)
    assert not result.ok


def test_cut_unlimited(validator: SafetyValidator) -> None:
    """Cuts have no floor — very deep cuts are fine."""
    filters = [hpf(), make_filter(80.0, -20.0)]
    result = validator.validate(filters)
    assert result.ok


# ── Cumulative 1/3-octave boost ────────────────────────────────────────────────

def test_cumulative_boost_within_limit(validator: SafetyValidator) -> None:
    """Two boosts in the same 1/3-oct band that sum to 9 dB — passes."""
    # Both 80 Hz and 90 Hz fall in the 80 Hz 1/3-octave band
    filters = [hpf(), make_filter(80.0, 4.5), make_filter(90.0, 4.5)]
    result = validator.validate(filters)
    assert result.ok


def test_cumulative_boost_exceeds_limit(validator: SafetyValidator) -> None:
    """Two 5 dB boosts near 80 Hz → 10 dB cumulative → rejected."""
    filters = [hpf(), make_filter(80.0, 5.0), make_filter(85.0, 5.0)]
    result = validator.validate(filters)
    assert not result.ok
    assert "9" in result.error or "cumulative" in result.error.lower()


def test_cumulative_boost_different_bands_passes(validator: SafetyValidator) -> None:
    """6 dB boost at 40 Hz and 6 dB boost at 160 Hz — different bands, both pass."""
    filters = [hpf(), make_filter(40.0, 6.0), make_filter(160.0, 6.0)]
    result = validator.validate(filters)
    assert result.ok


# ── Per-iteration change limit ─────────────────────────────────────────────────

def test_iteration_increase_within_limit(validator: SafetyValidator) -> None:
    """Increase of 3 dB from previous is within the per-iteration limit."""
    prev = [make_filter(80.0, 0.0), hpf()]
    curr = [hpf(), make_filter(80.0, 3.0)]
    result = validator.validate(curr, prev)
    assert result.ok


def test_iteration_increase_exceeds_limit(validator: SafetyValidator) -> None:
    """Increase of 4 dB — rejected."""
    prev = [make_filter(80.0, 0.0), hpf()]
    curr = [hpf(), make_filter(80.0, 4.0)]
    result = validator.validate(curr, prev)
    assert not result.ok
    assert "3" in result.error or "iteration" in result.error.lower()


def test_iteration_decrease_no_limit(validator: SafetyValidator) -> None:
    """Decreasing gain by any amount is allowed."""
    prev = [make_filter(80.0, 5.0), hpf()]
    curr = [hpf(), make_filter(80.0, -6.0)]
    result = validator.validate(curr, prev)
    assert result.ok


def test_no_previous_filters_skips_iteration_check(validator: SafetyValidator) -> None:
    """When previous_filters is None, iteration check is skipped."""
    curr = [hpf(), make_filter(80.0, 6.0)]
    result = validator.validate(curr, previous_filters=None)
    assert result.ok


def test_new_band_no_previous_starts_from_zero(validator: SafetyValidator) -> None:
    """A new band not present in previous — treated as increase from 0 dB."""
    prev = [hpf()]  # no 80 Hz band
    curr = [hpf(), make_filter(80.0, 3.0)]  # 3 dB increase from 0 — within limit
    result = validator.validate(curr, prev)
    assert result.ok


def test_new_band_too_large_increase_from_zero(validator: SafetyValidator) -> None:
    """4 dB boost on a new band (not in previous) — exceeds +3 dB limit from 0."""
    prev = [hpf()]
    curr = [hpf(), make_filter(80.0, 4.0)]
    result = validator.validate(curr, prev)
    assert not result.ok


# ── Simulation-verified iteration limit ────────────────────────────────────────

def test_simulation_verified_allows_6db_change(validator: SafetyValidator) -> None:
    """With simulation_verified=True, +6 dB change per iteration is allowed."""
    prev = [hpf(), make_filter(80.0, 0.0)]
    curr = [hpf(), make_filter(80.0, 6.0)]
    result = validator.validate(curr, prev, simulation_verified=True)
    assert result.ok


def test_simulation_verified_rejects_above_6db(validator: SafetyValidator) -> None:
    """Even with simulation_verified, > +6 dB change is rejected."""
    prev = [hpf(), make_filter(80.0, 0.0)]
    curr = [hpf(), make_filter(80.0, 7.0)]
    result = validator.validate(curr, prev, simulation_verified=True)
    assert not result.ok
    assert "6" in result.error
    assert "simulation-verified" in result.error


def test_without_simulation_verified_4db_rejected(validator: SafetyValidator) -> None:
    """Without simulation_verified, +4 dB change exceeds +3 dB limit."""
    prev = [hpf(), make_filter(80.0, 0.0)]
    curr = [hpf(), make_filter(80.0, 4.0)]
    result = validator.validate(curr, prev, simulation_verified=False)
    assert not result.ok


# ── ValidationResult ───────────────────────────────────────────────────────────

def test_validation_result_passed() -> None:
    r = ValidationResult.passed()
    assert r.ok is True
    assert r.error == ""


def test_validation_result_failed() -> None:
    r = ValidationResult.failed("too much boost")
    assert r.ok is False
    assert "SafetyValidator" in r.error
    assert "too much boost" in r.error


# ── HPF is exempt from boost checks ───────────────────────────────────────────

def test_hpf_not_subject_to_gain_checks(validator: SafetyValidator) -> None:
    """HPF filter type bypasses all gain-related checks."""
    # HPF gain_db is meaningless but shouldn't trigger boost-floor check
    filters = [FilterSpec(freq=18.0, gain_db=0.0, q=0.707, type="hpf")]
    # This should fail only on HPF frequency check (18 Hz HPF satisfies the requirement)
    result = validator.validate(filters)
    assert result.ok
