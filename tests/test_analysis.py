"""Tests for analysis module — target curves, convergence, mock corrections."""

import math

import numpy as np
import pytest

from calibrate.analysis import (
    HarmanTarget,
    make_flat_target,
    per_band_deviation,
    per_freq_boost_effectiveness,
    optimal_anchor_reference_spl,
    rms_deviation,
    _propose_mock,
)
from calibrate.measurement import FrequencyResponse
from calibrate.recipe import Recipe, ConvergenceCriteria, MeasurementConfig
from calibrate.safety import FilterSpec, SafetyValidator, _third_octave_for_freq


def _make_fr(
    freq_range: tuple[float, float] = (20.0, 200.0),
    n_points: int = 100,
    spl_value: float = 75.0,
    spl_offsets: dict[float, float] | None = None,
) -> FrequencyResponse:
    """Create a synthetic FrequencyResponse for testing."""
    freqs = np.logspace(
        np.log10(freq_range[0]),
        np.log10(freq_range[1]),
        n_points,
    ).tolist()

    spl = [spl_value] * n_points

    if spl_offsets:
        for target_freq, offset in spl_offsets.items():
            # Find closest bin and apply offset
            idx = int(np.argmin(np.abs(np.array(freqs) - target_freq)))
            spl[idx] = spl_value + offset

    return FrequencyResponse(
        frequencies=freqs,
        spl=spl,
        sample_rate=96000,
        sweep_duration=3.0,
        timestamp="2026-04-05T00:00:00Z",
    )


def _make_recipe(analysis: str = "mock") -> Recipe:
    return Recipe(
        name="test",
        target="harman",
        band=(20.0, 200.0),
        convergence=ConvergenceCriteria(threshold_db=2.0, max_iterations=5),
        analysis=analysis,
        measurement=MeasurementConfig(),
    )


# ── Harman target shape ──────────────────────────────────────────────────────

def test_harman_target_shape() -> None:
    """Harman target has +slope below 80 Hz, flat above."""
    target = HarmanTarget(reference_spl=75.0)

    # Below 80 Hz: should be above reference
    assert target.target_at(25.0) > target.target_at(80.0)
    assert target.target_at(40.0) > target.target_at(80.0)
    assert target.target_at(63.0) > target.target_at(80.0)

    # At 80 Hz: should be at reference
    assert target.target_at(80.0) == 75.0

    # Above 80 Hz: flat or slightly below
    assert target.target_at(100.0) == 75.0
    assert target.target_at(200.0) < 75.0

    # Monotonic decrease from 20 Hz to 200 Hz
    spl_20 = target.target_at(20.0)
    spl_80 = target.target_at(80.0)
    spl_200 = target.target_at(200.0)
    assert spl_20 > spl_80 > spl_200


def test_harman_target_at_20hz() -> None:
    target = HarmanTarget(reference_spl=75.0)
    assert target.target_at(20.0) == pytest.approx(81.0, abs=0.1)


def test_harman_target_array() -> None:
    target = HarmanTarget(reference_spl=75.0)
    freqs = [20.0, 80.0, 200.0]
    arr = target.target_array(freqs)
    assert len(arr) == 3
    assert arr[0] > arr[1] > arr[2]


# ── Flat target ───────────────────────────────────────────────────────────────

def test_flat_target() -> None:
    target = make_flat_target(reference_spl=75.0)
    assert target.target_at(20.0) == 75.0
    assert target.target_at(80.0) == 75.0
    assert target.target_at(200.0) == 75.0


# ── RMS deviation ─────────────────────────────────────────────────────────────

def test_rms_deviation_perfect() -> None:
    """Measurement matching target -> 0.0 deviation."""
    target = HarmanTarget(reference_spl=75.0)
    # Create FR that exactly matches Harman target
    freqs = [20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0, 200.0]
    spl = [target.target_at(f) for f in freqs]
    fr = FrequencyResponse(
        frequencies=freqs,
        spl=spl,
        sample_rate=96000,
        sweep_duration=3.0,
        timestamp="2026-04-05T00:00:00Z",
    )
    rms = rms_deviation(fr, target, (20.0, 200.0))
    assert rms == pytest.approx(0.0, abs=0.01)


def test_rms_deviation_flat() -> None:
    """Flat measurement vs Harman target -> known non-zero deviation."""
    target = HarmanTarget(reference_spl=75.0)
    fr = _make_fr(spl_value=75.0)  # flat at 75 dB
    rms = rms_deviation(fr, target, (20.0, 200.0))
    # Harman has ~6 dB range (from +6 at 20 Hz to -2 at 200 Hz)
    # RMS of a slope should be > 0
    assert rms > 1.0
    assert rms < 10.0  # sanity check


def test_rms_deviation_empty_band() -> None:
    """Band outside measurement range -> 0.0."""
    target = HarmanTarget(reference_spl=75.0)
    fr = _make_fr(freq_range=(20.0, 200.0))
    rms = rms_deviation(fr, target, (500.0, 1000.0))
    assert rms == 0.0


# ── Convergence reference pinned to baseline ──────────────────────────────────

def test_convergence_reference_pinned_to_baseline() -> None:
    """Reference SPL stays constant regardless of measurement changes."""
    baseline_spl = 75.0
    target = HarmanTarget(reference_spl=baseline_spl)

    # Iteration 1: measurement at 75 dB
    fr1 = _make_fr(spl_value=75.0)
    rms1 = rms_deviation(fr1, target, (20.0, 200.0))

    # Iteration 2: measurement at 80 dB (louder)
    fr2 = _make_fr(spl_value=80.0)
    rms2 = rms_deviation(fr2, target, (20.0, 200.0))

    # Both use the same target (reference_spl=75), so the 80 dB measurement
    # should have a different (higher) RMS because it's offset from target
    assert rms2 > rms1


# ── Mock backend proposals ────────────────────────────────────────────────────

def test_deterministic_proposes_cuts_for_peaks() -> None:
    """A +6 dB peak at 80 Hz -> negative gain filter."""
    fr = _make_fr(spl_value=75.0, spl_offsets={80.0: 6.0})
    target = HarmanTarget(reference_spl=75.0)
    recipe = _make_recipe()
    hw = {"available_peq_slots": 8}

    filters = _propose_mock(fr, target, [], hw, recipe)
    # Should have at least one cut near 80 Hz
    cuts = [f for f in filters if f.gain_db < 0]
    assert len(cuts) >= 1
    # The biggest cut should be near 80 Hz
    biggest_cut = min(filters, key=lambda f: f.gain_db)
    assert biggest_cut.gain_db < 0


def test_deterministic_proposes_boosts_for_dips() -> None:
    """A -6 dB dip at 50 Hz -> positive gain filter (capped by safety)."""
    fr = _make_fr(spl_value=75.0, spl_offsets={50.0: -6.0})
    target = HarmanTarget(reference_spl=75.0)
    recipe = _make_recipe()
    hw = {"available_peq_slots": 8}

    filters = _propose_mock(fr, target, [], hw, recipe)
    boosts = [f for f in filters if f.gain_db > 0]
    # Should have at least one boost
    assert len(boosts) >= 1
    # All boosts should be within safety limits
    for f in boosts:
        assert f.gain_db <= 6.0
        assert f.gain_db <= 3.0  # per-iteration limit for mock


def test_deterministic_respects_slot_limit() -> None:
    """More peaks than PEQ slots -> limited to available slots."""
    # Create FR with deviations at every 1/3-octave centre
    offsets = {f: 4.0 for f in [25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0, 200.0]}
    fr = _make_fr(spl_value=75.0, spl_offsets=offsets)
    target = HarmanTarget(reference_spl=75.0)
    recipe = _make_recipe()
    hw = {"available_peq_slots": 3}  # only 3 slots

    filters = _propose_mock(fr, target, [], hw, recipe)
    assert len(filters) <= 3


# ── Frequency drift safety check ─────────────────────────────────────────────

def test_frequency_drift_caught_by_validator() -> None:
    """49.9 Hz and 50.0 Hz should be treated as the same 1/3-octave band."""
    # Both should map to 50 Hz 1/3-octave centre
    assert _third_octave_for_freq(49.9) == _third_octave_for_freq(50.0)
    assert _third_octave_for_freq(50.0) == 50.0

    # SafetyValidator should catch the drift
    validator = SafetyValidator()
    hpf = FilterSpec(freq=18.0, gain_db=0.0, q=0.707, type="hpf")

    prev = [hpf, FilterSpec(freq=50.0, gain_db=1.0, q=1.0, type="peaking")]
    # New filter at 49.9 Hz with +5.0 dB -> delta is 4.0 dB (> 3.0 limit)
    # Under per-band ceiling (+6 dB) so it hits the per-iteration check
    new = [hpf, FilterSpec(freq=49.9, gain_db=5.0, q=1.0, type="peaking")]

    result = validator.validate(new, prev)
    assert not result.ok
    assert "change per iteration" in result.error.lower()


# ── harman_rms helper ────────────────────────────────────────────────────────

def test_harman_rms_returns_positive_float():
    """harman_rms() computes a single RMS deviation number from Harman target."""
    from calibrate.analysis import harman_rms

    # Flat measurement at 75 dB — Harman has bass rise, so deviation > 0
    fr = _make_fr(spl_value=75.0)
    result = harman_rms(fr)
    assert isinstance(result, float)
    assert result > 0  # flat measurement != Harman target


def test_harman_rms_near_zero_for_matching_shape():
    """Measurement shaped like Harman target -> low deviation."""
    from calibrate.analysis import harman_rms, HarmanTarget

    # Create a measurement with Harman-like shape
    ref = 75.0
    target = HarmanTarget(reference_spl=ref)
    # Use offsets to match Harman curve shape
    offsets = {
        20.0: +6.0, 25.0: +5.0, 31.5: +4.0, 40.0: +3.0,
        50.0: +2.0, 63.0: +1.0, 80.0: 0.0, 100.0: 0.0,
        125.0: 0.0, 160.0: -1.0, 200.0: -2.0,
    }
    fr = _make_fr(spl_value=ref, spl_offsets=offsets)
    result = harman_rms(fr)
    assert result < 3.0  # close to Harman shape (interpolation causes some deviation)


def test_min_rms_reference_spl_lower_than_max_safe():
    """min_rms ref should be at or below max_safe ref (trades level for fit)."""
    from calibrate.analysis import min_rms_reference_spl, max_safe_reference_spl

    # Flat measurement — Harman needs bass boost, so min_rms will drop the ref
    # to reduce the boost needed at 20-25 Hz (sub rolloff region)
    fr = _make_fr(spl_value=75.0)
    max_safe = max_safe_reference_spl(fr)
    min_rms = min_rms_reference_spl(fr)
    assert min_rms <= max_safe


def test_min_rms_gives_lower_rms_than_max_safe():
    """min_rms anchoring should produce equal or lower RMS than max_safe."""
    from calibrate.analysis import harman_rms

    # Flat measurement — max_safe will max out boost at 20Hz, min_rms will drop ref
    fr = _make_fr(spl_value=75.0)
    rms_max_safe = harman_rms(fr, anchor="max_safe")
    rms_min = harman_rms(fr, anchor="min_rms")
    assert rms_min <= rms_max_safe + 0.1  # allow small float tolerance


def test_min_rms_equals_max_safe_for_harman_shaped():
    """If measurement already matches Harman shape, both anchors converge."""
    from calibrate.analysis import min_rms_reference_spl, max_safe_reference_spl

    offsets = {
        20.0: +6.0, 25.0: +5.0, 31.5: +4.0, 40.0: +3.0,
        50.0: +2.0, 63.0: +1.0, 80.0: 0.0, 100.0: 0.0,
        125.0: 0.0, 160.0: -1.0, 200.0: -2.0,
    }
    fr = _make_fr(spl_value=75.0, spl_offsets=offsets)
    max_safe = max_safe_reference_spl(fr)
    min_rms = min_rms_reference_spl(fr)
    # Should be very close — Harman-shaped measurement needs no correction
    assert abs(max_safe - min_rms) < 2.0


# ── per_freq_boost_effectiveness ─────────────────────────────────────────────

def _make_fr_with_coherence(
    spl_value: float = 75.0,
    coherence_value: float = 0.9,
    n_points: int = 200,
) -> FrequencyResponse:
    freqs = np.logspace(np.log10(20.0), np.log10(200.0), n_points).tolist()
    return FrequencyResponse(
        frequencies=freqs,
        spl=[spl_value] * n_points,
        coherence=[coherence_value] * n_points,
        sample_rate=48000,
        sweep_duration=3.0,
        timestamp="2026-06-01T00:00:00Z",
    )


def test_per_freq_effectiveness_below_port_is_zero():
    fr = _make_fr_with_coherence()
    result = per_freq_boost_effectiveness(fr, port_tune_hz=30.0, band=(20.0, 200.0))
    for freq, eff in result.items():
        if freq < 30.0:
            assert eff == 0.0, f"expected 0 below port at {freq} Hz, got {eff}"


def test_per_freq_effectiveness_geometry_null_is_zero():
    fr = _make_fr_with_coherence()
    geometry_ranges = [(45.0, 55.0)]
    result = per_freq_boost_effectiveness(
        fr, geometry_ranges=geometry_ranges, port_tune_hz=25.0, band=(20.0, 200.0)
    )
    for freq, eff in result.items():
        if 45.0 <= freq < 55.0:
            assert eff == 0.0, f"expected 0 in geometry null at {freq} Hz, got {eff}"


def test_per_freq_effectiveness_long_t60_reduces_base():
    fr = _make_fr_with_coherence(coherence_value=1.0)
    # Single mode at 47 Hz with T60 700 ms
    t60_data = [(47.0, 700.0)]
    result = per_freq_boost_effectiveness(
        fr, t60_data=t60_data, port_tune_hz=25.0, band=(20.0, 200.0)
    )
    # 47 Hz should have low effectiveness (long T60, base=0.20)
    eff_at_47 = result.get(min(result, key=lambda f: abs(f - 47.0)), 1.0)
    assert eff_at_47 <= 0.25, f"expected low effectiveness at 47 Hz, got {eff_at_47}"
    # 100 Hz (far from mode, no T60 data) should have high effectiveness
    eff_at_100 = result.get(min(result, key=lambda f: abs(f - 100.0)), 0.0)
    assert eff_at_100 >= 0.6, f"expected high effectiveness at 100 Hz, got {eff_at_100}"


def test_per_freq_effectiveness_short_t60_high_coherence():
    fr = _make_fr_with_coherence(coherence_value=0.95)
    t60_data = [(80.0, 150.0)]
    result = per_freq_boost_effectiveness(
        fr, t60_data=t60_data, port_tune_hz=25.0, band=(20.0, 200.0)
    )
    eff_at_80 = result.get(min(result, key=lambda f: abs(f - 80.0)), 0.0)
    assert eff_at_80 >= 0.75, f"expected high effectiveness at 80 Hz, got {eff_at_80}"


def test_per_freq_effectiveness_keyed_at_native_resolution():
    """Result keys should be at measurement frequencies, not 1/3-oct centres."""
    fr = _make_fr_with_coherence(n_points=200)
    result = per_freq_boost_effectiveness(fr, port_tune_hz=25.0, band=(20.0, 200.0))
    # Should have many more entries than 1/3-oct would give (~10 bands 20-200 Hz)
    assert len(result) > 30


# ── optimal_anchor_reference_spl ─────────────────────────────────────────────

_HARMAN_OFFSETS = [
    {"freq_hz": 25.0, "offset_db": 5.0},
    {"freq_hz": 31.5, "offset_db": 4.0},
    {"freq_hz": 40.0, "offset_db": 3.0},
    {"freq_hz": 50.0, "offset_db": 2.0},
    {"freq_hz": 63.0, "offset_db": 1.0},
    {"freq_hz": 80.0, "offset_db": 0.0},
    {"freq_hz": 100.0, "offset_db": 0.0},
    {"freq_hz": 125.0, "offset_db": 0.0},
]


def test_optimal_anchor_flat_response_returns_balanced():
    """Flat measurement + uniform high effectiveness → balanced anchor."""
    fr = _make_fr_with_coherence(spl_value=75.0)
    effectiveness = {f: 0.85 for f in fr.frequencies}
    ref, score, breakdown = optimal_anchor_reference_spl(
        fr, _HARMAN_OFFSETS, effectiveness, band=(25.0, 125.0), max_boost_db=6.0
    )
    assert 60.0 <= ref <= 82.0, f"unexpected reference_spl {ref}"
    assert len(breakdown) > 0


def test_optimal_anchor_hump_avoids_anchor_at_hump():
    """Modal hump with low effectiveness → anchor should NOT sit at hump peak."""
    # Flat at 75 dB except +8 dB hump at 50 Hz with very low effectiveness
    fr = _make_fr_with_coherence(spl_value=75.0, coherence_value=0.9)
    # Override SPL near 50 Hz manually
    freqs = list(fr.frequencies)
    spl = list(fr.spl)
    for i, f in enumerate(freqs):
        if 45.0 <= f <= 55.0:
            spl[i] = 83.0  # +8 dB hump
    fr_hump = FrequencyResponse(
        frequencies=freqs, spl=spl, coherence=fr.coherence,
        sample_rate=48000, sweep_duration=3.0, timestamp="2026-06-01T00:00:00Z",
    )
    # Low effectiveness at hump frequencies
    effectiveness = {f: 0.85 for f in freqs}
    for f in freqs:
        if 45.0 <= f <= 55.0:
            effectiveness[f] = 0.15  # nearly ineffective — deep mode
    ref, score, breakdown = optimal_anchor_reference_spl(
        fr_hump, _HARMAN_OFFSETS, effectiveness, band=(25.0, 125.0), max_boost_db=6.0
    )
    # If anchored at hump (~83 dB), everything else needs large boosts.
    # Optimal should prefer lower reference where hump is a cut, not anchor.
    assert ref < 82.0, f"anchor sat on hump: ref={ref}"


def test_optimal_anchor_prefers_boost_when_effective():
    """When effectiveness is high, a boost is better than sacrificing headroom."""
    fr = _make_fr_with_coherence(spl_value=75.0, coherence_value=0.95)
    # All high effectiveness → balanced anchor (boosts are fine)
    effectiveness = {f: 0.90 for f in fr.frequencies}
    ref_high_eff, _, _ = optimal_anchor_reference_spl(
        fr, _HARMAN_OFFSETS, effectiveness, band=(25.0, 125.0),
        max_boost_db=6.0, headroom_lambda=0.3,
    )
    # All low effectiveness → prefers cuts (higher reference_spl)
    effectiveness_low = {f: 0.15 for f in fr.frequencies}
    ref_low_eff, _, _ = optimal_anchor_reference_spl(
        fr, _HARMAN_OFFSETS, effectiveness_low, band=(25.0, 125.0),
        max_boost_db=6.0, headroom_lambda=0.3,
    )
    # Low effectiveness → anchor pushed higher to prefer cuts
    assert ref_low_eff >= ref_high_eff, (
        f"expected low-eff anchor higher, got high_eff={ref_high_eff} low_eff={ref_low_eff}"
    )


def test_optimal_anchor_excludes_nulls():
    """Deep null bands are excluded; anchor ignores them."""
    fr = _make_fr_with_coherence(spl_value=75.0)
    freqs = list(fr.frequencies)
    spl = list(fr.spl)
    # Deep null at 63 Hz: -20 dB
    for i, f in enumerate(freqs):
        if 60.0 <= f <= 67.0:
            spl[i] = 55.0
    fr_null = FrequencyResponse(
        frequencies=freqs, spl=spl, coherence=fr.coherence,
        sample_rate=48000, sweep_duration=3.0, timestamp="2026-06-01T00:00:00Z",
    )
    effectiveness = {f: 0.85 for f in freqs}
    # Should not raise and should return a sane reference
    ref, score, breakdown = optimal_anchor_reference_spl(
        fr_null, _HARMAN_OFFSETS, effectiveness, band=(25.0, 125.0), max_boost_db=6.0
    )
    assert 55.0 <= ref <= 90.0
