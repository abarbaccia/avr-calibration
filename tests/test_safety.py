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

import math

import numpy as np
import pytest
from calibrate.safety import (
    FilterSpec,
    SafetyValidationError,
    SafetyValidator,
    ValidationResult,
)


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
    """80 Hz is above 30 Hz → limit is +12 dB. 12.1 dB should fail."""
    filters = [hpf(), make_filter(80.0, 12.1)]
    result = validator.validate(filters)
    assert not result.ok
    assert "12" in result.error


def test_boost_7db_at_80hz_passes(validator: SafetyValidator) -> None:
    """80 Hz is above 30 Hz → +7 dB is under the +12 dB limit."""
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


# ── Shelf-aware per-band magnitude (regression for shelf mis-binning) ─────────

def test_shelf_magnitude_high_shelf_below_corner_is_near_zero(
    validator: SafetyValidator,
) -> None:
    """high_shelf with negative gain attenuates ABOVE corner, not below.

    Pre-fix the validator attributed the filter's full ``gain_db`` to the
    1/3-octave centre nearest the corner — so a high_shelf at 35 Hz with
    -10 dB gain showed up as -10 dB at the 31.5 Hz centre. That was
    physically wrong; below the corner the filter's magnitude approaches
    0 dB. Regression test for the bug that produced spurious "+10 dB
    per-iteration increase" rejections when the filter was removed.
    """
    from calibrate.safety import _per_band_magnitudes
    filters = [
        hpf(18.0),
        FilterSpec(freq=35.0, gain_db=-10.0, q=0.5, type="high_shelf"),
        FilterSpec(freq=45.0, gain_db=-3.0, q=2.0, type="peaking"),
        FilterSpec(freq=55.0, gain_db=-2.0, q=2.0, type="peaking"),
        FilterSpec(freq=70.0, gain_db=-2.0, q=2.0, type="peaking"),
        FilterSpec(freq=90.0, gain_db=-2.0, q=2.0, type="peaking"),
    ]
    bands = _per_band_magnitudes(filters)
    # Pre-fix bug: 31.5 Hz band would read -10 dB. New math: shelf is well
    # below its full attenuation here, so ≥ -6 dB (i.e. closer to 0 than
    # to the shelf gain). The exact value depends on Q; just assert we're
    # not pinned to the gain magnitude.
    assert bands[31.5] > -7.0, f"31.5 Hz band={bands[31.5]:.2f} should not read full shelf gain"
    # Far above the corner, the high_shelf approaches its full gain.
    assert bands[125.0] < -6.0


def test_shelf_magnitude_low_shelf_above_corner_is_near_zero() -> None:
    """low_shelf with negative gain attenuates BELOW corner, not above."""
    from calibrate.safety import _per_band_magnitudes
    filters = [
        hpf(18.0),
        FilterSpec(freq=50.0, gain_db=-6.0, q=0.7, type="low_shelf"),
    ]
    bands = _per_band_magnitudes(filters)
    # Below the corner: close to the shelf gain.
    assert bands[25.0] < -3.0
    # Above the corner: close to 0.
    assert bands[100.0] > -2.0
    assert bands[200.0] > -1.0


def test_shelf_per_band_magnitude_not_pinned_to_filter_gain(
    validator: SafetyValidator,
) -> None:
    """Regression for the shelf mis-binning bug.

    Pre-fix, a high_shelf with -10 dB gain at corner 35 Hz reported -10 dB
    at the 31.5 Hz 1/3-octave centre — same magnitude as the filter's
    declared gain. Physically wrong: a high_shelf attenuates ABOVE its
    corner, so at 31.5 Hz (below the 35 Hz corner) magnitude is much
    closer to 0 dB than to the shelf gain. With the buggy math, removing
    or replacing this shelf produced a spurious "+10 dB at 31.5 Hz"
    per-iteration delta. With correct biquad math, the residual at 31.5
    Hz is only a few dB.
    """
    from calibrate.safety import _per_band_magnitudes
    filters = [
        hpf(18.0),
        FilterSpec(freq=35.0, gain_db=-10.0, q=0.5, type="high_shelf"),
        FilterSpec(freq=45.0, gain_db=-3.0, q=2.0, type="peaking"),
        FilterSpec(freq=55.0, gain_db=-2.0, q=2.0, type="peaking"),
        FilterSpec(freq=70.0, gain_db=-2.0, q=2.0, type="peaking"),
        FilterSpec(freq=90.0, gain_db=-2.0, q=2.0, type="peaking"),
    ]
    bands = _per_band_magnitudes(filters)
    # Pre-fix bug: bands[31.5] would be -10 dB. Correct math: nowhere near.
    assert bands[31.5] > -7.0, (
        f"31.5 Hz band={bands[31.5]:.2f} should be much closer to 0 than to "
        "the shelf gain of -10 dB (pre-fix bug attributed -10 dB here)"
    )
    # And well above the corner the shelf does reach near its full gain.
    assert bands[200.0] < -8.0


def test_shelf_unchanged_no_iteration_violation(
    validator: SafetyValidator,
) -> None:
    """When the shelf is unchanged across iterations, no spurious delta fires.

    Regression: with the buggy magnitude math, even a tweak to a single
    peaking filter could trigger a "+10 dB at 31.5 Hz" rejection because
    the shelf was being mis-binned to that band. With correct math, an
    unchanged shelf contributes zero delta at every band, and the only
    delta comes from the actual peaking change.
    """
    shelf = FilterSpec(freq=35.0, gain_db=-10.0, q=0.5, type="high_shelf")
    prev = [
        hpf(18.0), shelf,
        FilterSpec(freq=45.0, gain_db=-3.0, q=2.0, type="peaking"),
    ]
    curr = [
        hpf(18.0), shelf,
        # Move the peaking cut deeper — a *cut*, no boost anywhere.
        FilterSpec(freq=45.0, gain_db=-5.0, q=2.0, type="peaking"),
    ]
    result = validator.validate(curr, prev)
    assert result.ok, f"shelf-unchanged + peaking deeper-cut: {result.error}"


def test_peaking_filter_per_iteration_unchanged(validator: SafetyValidator) -> None:
    """Peaking filters were correct before the shelf-math fix; verify still correct.

    A +3 dB peaking at 80 Hz from a 0 dB baseline must still pass the
    +3 dB-per-iteration cap exactly the same as before.
    """
    prev = [hpf(), FilterSpec(freq=80.0, gain_db=0.0, q=2.0, type="peaking")]
    curr = [hpf(), FilterSpec(freq=80.0, gain_db=3.0, q=2.0, type="peaking")]
    result = validator.validate(curr, prev)
    assert result.ok
    # And +4 dB still rejected.
    curr2 = [hpf(), FilterSpec(freq=80.0, gain_db=4.0, q=2.0, type="peaking")]
    result2 = validator.validate(curr2, prev)
    assert not result2.ok


# ── bypass_iteration_limit ────────────────────────────────────────────────────

def test_bypass_iteration_limit_allows_large_change(
    validator: SafetyValidator,
) -> None:
    """bypass_iteration_limit=True skips the +3 dB delta cap.

    The proposed change still has to pass absolute boost caps; +5 dB at
    80 Hz is well within the +8 dB above-threshold cap.
    """
    prev = [hpf(), FilterSpec(freq=80.0, gain_db=0.0, q=2.0, type="peaking")]
    curr = [hpf(), FilterSpec(freq=80.0, gain_db=5.0, q=2.0, type="peaking")]
    # Default: rejected.
    assert not validator.validate(curr, prev).ok
    # With bypass: accepted.
    assert validator.validate(curr, prev, bypass_iteration_limit=True).ok


def test_bypass_iteration_limit_still_enforces_absolute_cap(
    validator: SafetyValidator,
) -> None:
    """bypass_iteration_limit does NOT relax the absolute boost cap.

    +20 dB at 80 Hz still violates the +8 dB above-threshold ceiling.
    """
    prev = [hpf(), FilterSpec(freq=80.0, gain_db=0.0, q=2.0, type="peaking")]
    curr = [hpf(), FilterSpec(freq=80.0, gain_db=20.0, q=2.0, type="peaking")]
    result = validator.validate(curr, prev, bypass_iteration_limit=True)
    assert not result.ok
    # Error must reference the per-band cap, not the iteration cap.
    assert "iteration" not in result.error.lower()


def test_bypass_iteration_limit_default_preserves_behavior(
    validator: SafetyValidator,
) -> None:
    """Regression: bypass_iteration_limit=False (default) keeps the +3 dB cap.

    Mirrors test_iteration_increase_exceeds_limit so we can be sure adding
    the new param didn't accidentally weaken the default check.
    """
    prev = [hpf(), FilterSpec(freq=80.0, gain_db=0.0, q=2.0, type="peaking")]
    curr = [hpf(), FilterSpec(freq=80.0, gain_db=4.0, q=2.0, type="peaking")]
    result = validator.validate(curr, prev, bypass_iteration_limit=False)
    assert not result.ok
    assert "iteration" in result.error.lower() or "3" in result.error


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


# ── FIR magnitude validation ──────────────────────────────────────────────────

_FIR_RATE = 48_000


def _fir_boost_at(freq_hz: float, gain_db: float, n_taps: int = 32768) -> list[float]:
    """Construct a FIR whose magnitude at *freq_hz* equals *gain_db*.

    Uses frequency-sampling design with no windowing: the full-length IR's
    FFT exactly reproduces the target magnitude spectrum we asked for, so
    the validator's FFT check sees the gain we intended.
    """
    freqs = np.fft.rfftfreq(n_taps, d=1.0 / _FIR_RATE)
    mag = np.ones_like(freqs)
    half_step = 2.0 ** (1.0 / 6.0)
    mask = (freqs >= freq_hz / half_step) & (freqs <= freq_hz * half_step)
    mag[mask] = 10.0 ** (gain_db / 20.0)
    # Zero-phase IR: real, symmetric.
    ir = np.fft.irfft(mag, n=n_taps)
    ir = np.fft.fftshift(ir)
    return ir.tolist()


def test_validate_fir_flat_passes(validator: SafetyValidator) -> None:
    """A pure impulse (flat magnitude = 0 dB) must not raise."""
    taps = [1.0] + [0.0] * 255
    validator.validate_fir(taps, sample_rate=_FIR_RATE)


def test_validate_fir_boost_below_port_tune_fails(validator: SafetyValidator) -> None:
    """SVS PB12-NSD profile: +10 dB at 20 Hz (below 25 Hz floor) must raise."""
    taps = _fir_boost_at(freq_hz=20.0, gain_db=10.0)
    with pytest.raises(SafetyValidationError) as excinfo:
        validator.validate_fir(taps, sample_rate=_FIR_RATE)
    msg = str(excinfo.value)
    # Error must name a frequency and dB value so operators know what failed.
    assert "Hz" in msg
    assert "dB" in msg
    # Either "port" (below-tune check) or "20" / "25" (offending band / limit).
    assert "port" in msg.lower() or "below" in msg.lower()


def test_validate_fir_boost_thermal_ceiling_fails(validator: SafetyValidator) -> None:
    """+14 dB at 60 Hz exceeds the +12 dB thermal ceiling (SVS profile)."""
    taps = _fir_boost_at(freq_hz=63.0, gain_db=14.0)
    with pytest.raises(SafetyValidationError) as excinfo:
        validator.validate_fir(taps, sample_rate=_FIR_RATE)
    msg = str(excinfo.value)
    assert "Hz" in msg
    assert "dB" in msg
    assert "thermal" in msg.lower() or "ceiling" in msg.lower()


def test_validate_fir_deep_cut_passes(validator: SafetyValidator) -> None:
    """-15 dB cut at 60 Hz must pass — cuts are always safe."""
    taps = _fir_boost_at(freq_hz=63.0, gain_db=-15.0)
    validator.validate_fir(taps, sample_rate=_FIR_RATE)


def test_validate_fir_error_names_frequency_and_db(
    validator: SafetyValidator,
) -> None:
    """The error message must surface both the offending band (Hz) and dB level.

    Used so the LLM / operator sees *what* was unsafe, not just that it was.
    """
    taps = _fir_boost_at(freq_hz=63.0, gain_db=12.0)
    with pytest.raises(SafetyValidationError) as excinfo:
        validator.validate_fir(taps, sample_rate=_FIR_RATE)
    msg = str(excinfo.value)
    # Must contain a digit-Hz token and a +dB number.
    import re
    assert re.search(r"\d+\s*Hz", msg)
    assert re.search(r"\+\d+(?:\.\d+)?\s*dB", msg)


def test_validate_fir_empty_is_noop(validator: SafetyValidator) -> None:
    """Empty coefficients list must not raise (driver layer handles the error)."""
    validator.validate_fir([], sample_rate=_FIR_RATE)


def test_validate_fir_uses_passed_profile_over_validator_default() -> None:
    """Explicit profile argument overrides the validator's bound profile."""
    from calibrate.graph import TransducerProfile

    loose = TransducerProfile(
        name="loose_test",
        min_boost_freq_hz=10.0,
        max_boost_per_band_db=20.0,
        max_boost_above_threshold_db=20.0,
        freq_dependent_boost_threshold_hz=10.0,
        max_cumulative_boost_db=30.0,
        hpf_freq_hz=None,
    )
    taps = _fir_boost_at(freq_hz=63.0, gain_db=10.0)
    # Default (strict) validator would reject; passing the loose profile accepts.
    SafetyValidator().validate_fir(
        taps, sample_rate=_FIR_RATE, profile=loose,
    )


# ── Transient-aware FIR check ──────────────────────────────────────────────────


def _realistic_gabor_anti_pulse_fir(
    freq_hz: float,
    sample_rate: int,
    target_peak_db: float,
    bp_q: float = 3.0,
) -> list[float]:
    """Build a realistic anti-pulse FIR with peak amplitude normalized to <=1.

    Returns a 4096-sample FIR with a main impulse + a Gabor pulse, scaled so
    its FFT magnitude at ``freq_hz`` equals approximately ``target_peak_db``.
    Matches the production normalization in ``calibrate.modal_fir``.
    """
    sigma = bp_q * sample_rate / (math.pi * freq_hz)
    n_pulse = max(64, int(8 * sigma))
    if n_pulse % 2 == 1:
        n_pulse += 1
    t = np.arange(n_pulse) - (n_pulse - 1) / 2.0
    env = np.exp(-0.5 * (t / sigma) ** 2)
    carrier = np.cos(2.0 * math.pi * freq_hz * t / sample_rate)
    pulse_template = env * carrier
    fir = np.zeros(4096)
    main_idx = 2048
    fir[main_idx] = 1.0
    pulse_centre = main_idx - int(0.5 * sample_rate / freq_hz)
    start = pulse_centre - n_pulse // 2
    n_fft = 8192
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)
    bin_idx = int(np.argmin(np.abs(freqs - freq_hz)))
    base_at_bin = float(np.abs(np.fft.rfft(fir, n_fft))[bin_idx])
    pad = np.zeros_like(fir)
    pad[start : start + n_pulse] = pulse_template
    pulse_at_bin = float(np.abs(np.fft.rfft(pad, n_fft))[bin_idx])
    target_mag = 10.0 ** (target_peak_db / 20.0)
    scale = (target_mag - base_at_bin) / max(pulse_at_bin, 1e-12)
    fir = fir + scale * pad
    peak = float(np.max(np.abs(fir)))
    if peak > 1.0:
        fir /= peak
    return fir.tolist()


def test_modal_cancel_intent_admits_above_thermal() -> None:
    """+12 dB Gabor anti-pulse passes when validator is told it's modal-cancel.

    The same FIR fails under the default ``intent="general"`` cap (+8 dB).
    The looser ``modal_cancel`` cap (+14 dB on PB12-NSD) is justified by the
    physical fact that anti-pulse boosts cancel the room's mode at the
    listener — net SPL at the listening position is unchanged; only the
    driver sees the boosted input.
    """
    # Use a frequency-sampling FIR that hits exactly the validator's metric.
    fir = _fir_boost_at(freq_hz=70.0, gain_db=12.0, n_taps=8192)
    with pytest.raises(SafetyValidationError):
        SafetyValidator().validate_fir(fir, sample_rate=_FIR_RATE)
    SafetyValidator().validate_fir(fir, sample_rate=_FIR_RATE, intent="modal_cancel")


def test_modal_cancel_intent_still_rejects_above_modal_cap() -> None:
    """Even the modal_cancel cap rejects FIRs over its limit (currently 60 dB
    for SVS PB12-NSD — see profile YAML for tuning rationale)."""
    fir = _fir_boost_at(freq_hz=70.0, gain_db=65.0, n_taps=8192)
    with pytest.raises(SafetyValidationError):
        SafetyValidator().validate_fir(fir, sample_rate=_FIR_RATE, intent="modal_cancel")


def test_general_intent_keeps_strict_thermal_cap() -> None:
    """Generic FIRs (PEQ-equivalent) at +14 dB sustained must still reject."""
    taps = _fir_boost_at(freq_hz=63.0, gain_db=14.0)
    with pytest.raises(SafetyValidationError) as excinfo:
        SafetyValidator().validate_fir(taps, sample_rate=_FIR_RATE)
    assert "thermal" in str(excinfo.value).lower()


# ── Effective-boost (excursion/port-compression) ceiling ─────────────────────
# Distinct from the safety caps: predicts how much boost actually translates to
# acoustic output vs is swallowed by driver excursion near the port tune.
# See memory/feedback_cant_eq_boost_past_excursion.md.

from calibrate.safety import (  # noqa: E402
    interp_effective_boost_ceiling,
    effective_boost_ceiling_db,
    boost_translation_warnings,
)
from calibrate.graph import (  # noqa: E402
    SVS_PB12_NSD_PROFILE,
    TransducerProfile,
    _parse_effective_boost_ceiling,
)

_PB12_CURVE = ((25.0, 1.0), (31.5, 1.5), (40.0, 3.0), (50.0, 6.0))


def test_interp_ceiling_empty_curve_returns_none() -> None:
    assert interp_effective_boost_ceiling((), 30.0) is None


def test_interp_ceiling_holds_lowest_below_first_breakpoint() -> None:
    assert interp_effective_boost_ceiling(_PB12_CURVE, 20.0) == 1.0


def test_interp_ceiling_none_above_top_breakpoint() -> None:
    # Above the curve the safety cap governs — no extra effective limit.
    assert interp_effective_boost_ceiling(_PB12_CURVE, 80.0) is None


def test_interp_ceiling_log_interpolates_between_breakpoints() -> None:
    val = interp_effective_boost_ceiling(_PB12_CURVE, 40.0)
    assert val == pytest.approx(3.0, abs=0.01)
    mid = interp_effective_boost_ceiling(_PB12_CURVE, 35.0)
    assert 1.5 < mid < 3.0  # between the 31.5 and 40 Hz breakpoints


def test_pb12_profile_carries_effective_boost_ceiling() -> None:
    assert SVS_PB12_NSD_PROFILE.effective_boost_ceiling == _PB12_CURVE
    assert effective_boost_ceiling_db(SVS_PB12_NSD_PROFILE, 31.5) == pytest.approx(1.5)


def test_boost_translation_warns_on_deep_bass_boost() -> None:
    """A +5 dB boost at 31 Hz exceeds the ~1.5 dB effective ceiling → warned."""
    filters = [{"type": "peaking", "freq": 31.5, "gain_db": 5.0, "q": 2.0}]
    warns = boost_translation_warnings(SVS_PB12_NSD_PROFILE, filters)
    assert len(warns) == 1
    assert warns[0]["freq"] == 31.5
    assert warns[0]["effective_ceiling_db"] == pytest.approx(1.5)
    assert warns[0]["excess_db"] == pytest.approx(3.5, abs=0.01)


def test_boost_translation_ignores_high_freq_and_cuts() -> None:
    """A boost above the curve (80 Hz) and any cut never warn."""
    filters = [
        {"type": "peaking", "freq": 80.0, "gain_db": 5.0, "q": 2.0},   # above curve
        {"type": "peaking", "freq": 31.5, "gain_db": -5.0, "q": 2.0},  # cut
        {"type": "low_shelf", "freq": 50.0, "gain_db": 4.0, "q": 0.7},  # at top breakpoint → None (unconstrained)
    ]
    assert boost_translation_warnings(SVS_PB12_NSD_PROFILE, filters) == []


def test_parse_effective_boost_ceiling_rejects_malformed_dict() -> None:
    """A dict entry missing the db key raises a clear error (not a silent drop)."""
    with pytest.raises(ValueError):
        _parse_effective_boost_ceiling([{"freq": 40}])
    with pytest.raises(ValueError):
        _parse_effective_boost_ceiling([{"max_effective_boost_db": 3.0}])


def test_parse_effective_boost_ceiling_sorts_and_accepts_dict_form() -> None:
    parsed = _parse_effective_boost_ceiling(
        [{"freq": 40, "max_effective_boost_db": 3}, [25, 1.0]]
    )
    assert parsed == ((25.0, 1.0), (40.0, 3.0))
    assert _parse_effective_boost_ceiling(None) == ()


def test_strictest_of_merges_effective_boost_ceiling() -> None:
    """The strictest-of merge keeps the most restrictive ceiling across drivers."""
    from calibrate.graph import SignalGraph, Transducer, Processor

    weak = TransducerProfile(
        name="weak", effective_boost_ceiling=((25.0, 0.5), (50.0, 4.0)),
    )
    strong = TransducerProfile(
        name="strong", effective_boost_ceiling=((25.0, 2.0), (50.0, 8.0)),
    )
    graph = SignalGraph(
        processors=(Processor(name="dsp", driver_ref="camilladsp", kind="dsp"),),
        transducers=(
            Transducer(name="a", role="sub", processor_ref="dsp",
                       output_index=0, safety_profile_ref="weak"),
            Transducer(name="b", role="sub", processor_ref="dsp",
                       output_index=1, safety_profile_ref="strong"),
        ),
        profiles=(weak, strong),
    )
    merged = graph.strictest_profile(graph.transducers)
    # Min at each breakpoint → the weak driver's ceiling.
    assert effective_boost_ceiling_db(merged, 25.0) == pytest.approx(0.5)
    assert effective_boost_ceiling_db(merged, 50.0) is None or \
        interp_effective_boost_ceiling(merged.effective_boost_ceiling, 49.9) == pytest.approx(4.0, abs=0.2)
