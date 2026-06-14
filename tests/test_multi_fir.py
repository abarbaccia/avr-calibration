"""Tests for design_multi_input_fir and design_fir_trinnov."""

from __future__ import annotations

import math

import numpy as np
import pytest

from calibrate.multi_fir import SubMeasurement, design_multi_input_fir, design_fir_trinnov


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


def test_requires_at_least_one_measurement():
    with pytest.raises(ValueError, match="≥ 1"):
        design_multi_input_fir([], [(20, 0.0), (200, 0.0)])


def test_rejects_bad_phase_mode():
    freqs = np.linspace(20, 200, 100).tolist()
    m1 = _synth_measurement(freqs, [0.0] * 100)
    m2 = _synth_measurement(freqs, [0.0] * 100)
    with pytest.raises(ValueError, match="phase_mode"):
        design_multi_input_fir([m1, m2], [(20, 0.0), (200, 0.0)],
                               phase_mode="bogus")


def test_max_correction_db_clamps_fir_gain():
    """max_correction_db bounds the per-band FIR gain deviation (±N dB around the
    in-band median, phase preserved), so the Wiener can't over-correct a tall
    GEOMETRY mode into a deep cut that guts the band (fir-design-reviewer, run 36)."""
    freqs = np.linspace(20, 200, 200).tolist()
    # Flat baseline except a tall +15 dB mode around 50 Hz.
    mag = [15.0 if 45 <= f <= 55 else 0.0 for f in freqs]
    m1 = _synth_measurement(freqs, mag, label="sub1")
    m2 = _synth_measurement(freqs, mag, label="sub2")
    target = [(20, 0.0), (200, 0.0)]
    focus = (30.0, 120.0)
    common = dict(num_taps=4096, sample_rate=48000, phase_mode="mixed",
                  freq_focus_hz=focus, regularization_lambda=0.01)

    unclamped = design_multi_input_fir([m1, m2], target, **common)
    clamped = design_multi_input_fir([m1, m2], target, max_correction_db=6.0, **common)

    def fir_effect_spread(res):
        eff = res["predicted_per_sub"][0]["fir_effect_bands"]
        vals = [b["spl_db"] for b in eff if 30.0 <= b["freq_hz"] <= 120.0]
        return max(vals) - min(vals)

    spread_unclamped = fir_effect_spread(unclamped)
    spread_clamped = fir_effect_spread(clamped)
    assert spread_clamped < spread_unclamped, (
        f"clamp must reduce gain spread: clamped={spread_clamped:.1f} "
        f"unclamped={spread_unclamped:.1f}"
    )
    # ±6 dB around the median ⇒ total spread ≤ ~12 dB (+ band-edge tolerance).
    assert spread_clamped <= 14.0, f"clamped spread {spread_clamped:.1f} dB exceeds ~12 dB bound"


def test_max_correction_db_zero_magnitude_band_does_not_crash():
    """A degenerate sub measurement with zero magnitude across the focus band
    makes |K_i| == 0 everywhere in-band, so the clamp's positive-magnitude set
    `_band_mag = _mag[in_band][_mag>0]` is empty. The `if _band_mag.size:` guard
    must skip the clamp (no median-of-empty / div-by-zero) and the design must
    still return a finite FIR per sub."""
    freqs = np.linspace(20, 200, 120).tolist()
    # spl_db = -inf → linear magnitude exactly 0.0 → |H_i| == 0 → |K_i| == 0 in-band.
    silent = _synth_measurement(freqs, [-np.inf] * len(freqs), label="silent")
    normal = _synth_measurement(freqs, [0.0] * len(freqs), label="normal")
    result = design_multi_input_fir(
        [silent, normal], [(20, 0.0), (200, 0.0)],
        num_taps=2048, sample_rate=48000, phase_mode="mixed",
        freq_focus_hz=(30.0, 120.0), regularization_lambda=0.01,
        max_correction_db=6.0,
    )
    assert result["num_subs"] == 2
    assert len(result["firs"]) == 2
    # No NaN/inf must leak through from the degenerate sub.
    for fir in result["firs"]:
        assert np.all(np.isfinite(fir)), "clamp/degenerate path leaked a non-finite tap"


def test_flat_measurement_flat_target_produces_near_identity():
    """Two flat subs at unit gain summing to flat target — combined should
    hit target in the mid-band (away from focus edges where bandpass
    truncation produces droop).
    """
    freqs = np.linspace(20, 200, 50).tolist()
    m1 = _synth_measurement(freqs, [0.0] * 50)
    m2 = _synth_measurement(freqs, [0.0] * 50)
    target = [(20, 0.0), (200, 0.0)]
    result = design_multi_input_fir(
        [m1, m2], target,
        num_taps=1024, sample_rate=48000,
        phase_mode="linear",  # coherent-sum mode
        regularization_lambda=0.01,
        freq_focus_hz=(20, 200),
    )
    # Mid-band hits target within a few dB (40-100 Hz is the sweet spot;
    # outside that the bandpass impulse truncation produces edge droop).
    bands = result["predicted_combined"]
    mid_band = [b for b in bands if 40 <= b["freq_hz"] <= 125]
    avg = sum(b["spl_db"] for b in mid_band) / len(mid_band)
    assert abs(avg) < 5.0, f"mid-band combined avg {avg} dB, expected near 0"


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
        phase_mode="linear",  # coherent-sum requires phase preservation
        freq_focus_hz=(20, 200),
        regularization_lambda=0.05,
    )
    # Mid-band hits target within realization tolerances
    bands = result["predicted_combined"]
    mid_band = [b for b in bands if 50 <= b["freq_hz"] <= 125]
    assert len(mid_band) > 0
    for b in mid_band:
        assert -7 < b["spl_db"] < 7, f"mid-band combined out of range at {b['freq_hz']}Hz: {b['spl_db']}dB"


def test_linear_phase_adds_more_latency_than_mixed():
    """Linear phase puts the impulse at num_taps/2 (most pre-ringing budget).
    Mixed phase limits to preringing_ms (smaller pre-ring window).
    So linear latency > mixed latency.
    """
    freqs = np.linspace(20, 200, 50).tolist()
    m1 = _synth_measurement(freqs, [0.0] * 50)
    m2 = _synth_measurement(freqs, [0.0] * 50)
    target = [(20, 0.0), (200, 0.0)]
    res_linear = design_multi_input_fir(
        [m1, m2], target, num_taps=1024, phase_mode="linear",
        freq_focus_hz=(20, 200),
    )
    res_mixed = design_multi_input_fir(
        [m1, m2], target, num_taps=1024, phase_mode="mixed",
        preringing_ms=5, freq_focus_hz=(20, 200),
    )
    assert res_linear["latency_ms"] > res_mixed["latency_ms"]


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


# ---------------------------------------------------------------------------
# design_fir_trinnov tests
# ---------------------------------------------------------------------------

def _synth_room_ir_with_mode(
    mode_freq_hz: float = 47.0,
    t60_ms: float = 600.0,
    n_samples: int = 24576,
    sample_rate: int = 48000,
    peak_at: int = 100,
) -> list[float]:
    """Synthesize a room IR: delta at peak_at, then damped sinusoidal mode."""
    ir = np.zeros(n_samples)
    ir[peak_at] = 1.0
    decay_rate = math.log(1000) / (t60_ms / 1000.0)
    mode_t = np.arange(n_samples - peak_at) / sample_rate
    mode = np.exp(-decay_rate * mode_t) * np.sin(2 * np.pi * mode_freq_hz * mode_t)
    ir[peak_at:] += mode * 0.5
    return ir.tolist()


def _two_flat_measurements(freqs=None) -> list[SubMeasurement]:
    if freqs is None:
        freqs = np.linspace(20, 120, 60).tolist()
    m1 = SubMeasurement(freqs=freqs, spl_db=[0.0] * len(freqs),
                        phase_rad=[0.0] * len(freqs), label="sub1")
    m2 = SubMeasurement(freqs=freqs, spl_db=[0.0] * len(freqs),
                        phase_rad=[0.0] * len(freqs), label="sub2")
    return [m1, m2]


def _delayed_sub(delay_ms: float, label: str, freqs=None) -> SubMeasurement:
    """A flat-magnitude sub with a pure propagation delay (linear phase)."""
    if freqs is None:
        freqs = np.linspace(20, 120, 80)
    freqs = np.asarray(freqs, dtype=float)
    phase = (-2 * np.pi * freqs * delay_ms / 1000.0).tolist()
    return SubMeasurement(freqs=list(freqs), spl_db=[0.0] * len(freqs),
                          phase_rad=phase, label=label)


def _realized_combined_db(result, measurements, band_hz, sample_rate=48000):
    """Independently realize Σ FIR_i · H_i and return mean dB in a 1/6-oct band."""
    n_fft = 32768
    fo = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    H_sum = np.zeros(len(fo), dtype=complex)
    for fir, m in zip(result["firs"], measurements):
        f = np.asarray(fir)
        F = np.zeros(n_fft)
        F[: len(f)] = f
        K = np.fft.rfft(F)
        mag = 10 ** (np.interp(fo, m.freqs, m.spl_db) / 20.0)
        ph = np.interp(fo, m.freqs, np.unwrap(m.phase_rad))
        H_sum += K * mag * np.exp(1j * ph)
    db = 20 * np.log10(np.abs(H_sum) + 1e-12)
    lo, hi = band_hz / 2 ** (1 / 12), band_hz * 2 ** (1 / 12)
    mask = (fo >= lo) & (fo < hi)
    return float(np.mean(db[mask]))


def test_trinnov_fir_shape_and_length():
    """Each returned FIR has exactly num_taps coefficients."""
    room_ir = _synth_room_ir_with_mode()
    measurements = _two_flat_measurements()
    target = [(20, 0.0), (120, 0.0)]
    result = design_fir_trinnov(
        room_ir, measurements, target,
        sample_rate=48000, num_taps=4096,
        freq_min=20.0, freq_max=120.0,
    )
    assert result["num_subs"] == 2
    assert result["num_taps"] == 4096
    assert len(result["firs"]) == 2
    for fir in result["firs"]:
        assert len(fir) == 4096, f"expected 4096 taps, got {len(fir)}"


def test_trinnov_default_phase_mode_is_mixed():
    """Default phase_mode must be 'mixed' for coherent multi-sub summation."""
    room_ir = _synth_room_ir_with_mode()
    measurements = _two_flat_measurements()
    result = design_fir_trinnov(
        room_ir, measurements, [(20, 0.0), (120, 0.0)],
        sample_rate=48000, num_taps=4096,
    )
    assert result["phase_mode"] == "mixed"
    # Pure magnitude/phase correction → strict 'correction' safety intent,
    # NOT 'modal_cancel' (which would relax the FIR boost cap).
    assert result["apply_intent"] == "correction"


def test_trinnov_mixed_beats_minimum_for_coherent_sum():
    """The core property: with subs at different arrival times, 'mixed' phase
    sums coherently near the target while 'minimum' phase produces a deep null.

    This is the regression guard for the phase_mode='minimum' default bug:
    minimum-phase discards inter-sub phase, so delayed subs cancel acoustically.
    """
    room_ir = _synth_room_ir_with_mode()
    # 6 ms relative delay → near anti-phase around 80 Hz when summed.
    subs = [_delayed_sub(3.0, "sub5"), _delayed_sub(9.0, "sub6")]
    target = [(20, 0.0), (120, 0.0)]  # flat 0 dB target

    res_min = design_fir_trinnov(room_ir, subs, target, sample_rate=48000,
                                 num_taps=8192, phase_mode="minimum",
                                 regularization_lambda=0.01, freq_focus_hz=(20, 120))
    res_mix = design_fir_trinnov(room_ir, subs, target, sample_rate=48000,
                                 num_taps=8192, phase_mode="mixed",
                                 regularization_lambda=0.01, freq_focus_hz=(20, 120))

    min_80 = _realized_combined_db(res_min, subs, 80.0)
    mix_80 = _realized_combined_db(res_mix, subs, 80.0)

    # minimum-phase falls into a deep null; mixed stays near the 0 dB target.
    assert min_80 < -8.0, f"expected minimum-phase null at 80 Hz, got {min_80:.1f} dB"
    assert abs(mix_80) < 4.0, f"expected mixed near 0 dB at 80 Hz, got {mix_80:.1f} dB"
    assert mix_80 - min_80 > 8.0, (
        f"mixed should beat minimum by >8 dB at the null; "
        f"mixed={mix_80:.1f} minimum={min_80:.1f}"
    )


def test_trinnov_ringing_modes_reported_for_long_t60():
    """A long-decay IR mode must appear in the informational ringing_modes report."""
    room_ir = _synth_room_ir_with_mode(mode_freq_hz=47.0, t60_ms=800.0)
    measurements = _two_flat_measurements()
    result = design_fir_trinnov(
        room_ir, measurements, [(20, 0.0), (120, 0.0)],
        sample_rate=48000, num_taps=4096,
        freq_min=30.0, freq_max=80.0, t60_threshold_ms=300.0, bands_per_octave=6,
    )
    assert result["n_ringing_modes"] > 0, "expected ≥1 ringing mode for T60=800ms"
    freqs = [m["freq_hz"] for m in result["ringing_modes"]]
    assert any(35 <= f <= 65 for f in freqs), (
        f"no ringing mode near 47 Hz; got {sorted(freqs)}"
    )


def test_trinnov_no_ringing_modes_when_threshold_high():
    """When t60_threshold_ms exceeds the IR's decay, ringing_modes is empty —
    and the design still succeeds (the FIRs do not depend on the report)."""
    room_ir = _synth_room_ir_with_mode(mode_freq_hz=47.0, t60_ms=400.0)
    measurements = _two_flat_measurements()
    result = design_fir_trinnov(
        room_ir, measurements, [(20, 0.0), (120, 0.0)],
        sample_rate=48000, num_taps=4096,
        freq_min=20.0, freq_max=120.0, t60_threshold_ms=2000.0, bands_per_octave=6,
    )
    assert result["n_ringing_modes"] == 0
    assert len(result["firs"]) == 2


def test_trinnov_peak_normalized():
    """All FIR coefficients must satisfy |c| ≤ 1.0."""
    room_ir = _synth_room_ir_with_mode(t60_ms=600.0)
    measurements = _two_flat_measurements()
    result = design_fir_trinnov(
        room_ir, measurements, [(20, 0.0), (120, 0.0)],
        sample_rate=48000, num_taps=4096,
    )
    for i, fir in enumerate(result["firs"]):
        peak = max(abs(c) for c in fir)
        assert peak <= 1.0 + 1e-9, f"sub{i} FIR peak={peak:.4f} exceeds 1.0"


def test_trinnov_bad_ir_does_not_fail_design():
    """An empty/garbage IR must not break the design — the FIRs stand alone
    and the ringing-mode report is best-effort."""
    measurements = _two_flat_measurements()
    result = design_fir_trinnov(
        [0.0] * 4096, measurements, [(20, 0.0), (120, 0.0)],
        sample_rate=48000, num_taps=4096,
    )
    assert len(result["firs"]) == 2
    assert result["n_ringing_modes"] == 0


def test_single_sub_wiener():
    """design_multi_input_fir must work with exactly one measurement.
    The FIR should make the single sub hit the full target (T/1, not T/2).
    """
    freqs = np.linspace(20, 200, 60).tolist()
    m1 = SubMeasurement(freqs=freqs, spl_db=[0.0]*60, phase_rad=[0.0]*60, label="sub1")
    result = design_multi_input_fir([m1], [(20, 0.0), (200, 0.0)],
                                    num_taps=512, sample_rate=48000,
                                    phase_mode="minimum",
                                    regularization_lambda=0.01,
                                    freq_focus_hz=(20, 200))
    assert result["num_subs"] == 1
    assert len(result["firs"]) == 1
    assert len(result["firs"][0]) == 512
    assert len(result["per_sub_peak_boost_db"]) == 1


def test_single_sub_trinnov():
    """design_fir_trinnov must work with one measurement (single-sub calibration)."""
    room_ir = _synth_room_ir_with_mode(mode_freq_hz=47.0, t60_ms=600.0)
    measurements = [SubMeasurement(
        freqs=np.linspace(20, 120, 60).tolist(),
        spl_db=[0.0]*60, phase_rad=[0.0]*60, label="sub_only"
    )]
    result = design_fir_trinnov(
        room_ir, measurements, [(20, 0.0), (120, 0.0)],
        sample_rate=48000, num_taps=4096,
        freq_min=30.0, freq_max=80.0,
    )
    assert result["num_subs"] == 1
    assert len(result["firs"]) == 1
    assert len(result["firs"][0]) == 4096


# ---------------------------------------------------------------------------
# Self-cancellation guard: detect realised multi-sub FIRs that cancel at the
# mic (the mixed-phase truncation notch) before they ship.
# ---------------------------------------------------------------------------


def test_self_cancellation_margin_helper_inphase_vs_opposing():
    from calibrate.multi_fir import _self_cancellation_margin
    freqs = np.linspace(20.0, 200.0, 100)
    a = np.full(100, 0.5 + 0.0j)
    # Two in-phase contributions: |a+a| == |a|+|a| → margin 0 dB (coherent).
    margin_coherent = _self_cancellation_margin(a + a, np.abs(a) + np.abs(a), freqs, None)
    assert abs(margin_coherent) < 0.1, margin_coherent
    # Two opposing contributions: |a-a| == 0 → margin deeply negative (cancelling).
    margin_cancel = _self_cancellation_margin(a - a, np.abs(a) + np.abs(a), freqs, None)
    assert margin_cancel < -50.0, margin_cancel


def test_self_cancellation_margin_focus_band_restriction():
    from calibrate.multi_fir import _self_cancellation_margin
    freqs = np.linspace(20.0, 200.0, 181)  # 1 Hz grid
    a = np.full(len(freqs), 0.5 + 0.0j)
    # Cancellation only OUTSIDE the focus band → in-band margin stays ~0.
    coherent = a + a
    incoh = np.abs(a) + np.abs(a)
    out = freqs > 120.0
    coherent[out] = 0.0  # force cancellation above 120 Hz
    margin_inband = _self_cancellation_margin(coherent, incoh, freqs, (40.0, 100.0))
    assert abs(margin_inband) < 0.1, margin_inband


def test_design_reports_self_cancellation_margin_field():
    """design_multi_input_fir always returns self_cancellation_margin_db; for two
    well-behaved coherent subs it is ~0 dB (no cancellation)."""
    freqs = np.linspace(20, 200, 200).tolist()
    m1 = _synth_measurement(freqs, [0.0] * 200, label="sub1")
    m2 = _synth_measurement(freqs, [0.0] * 200, label="sub2")
    result = design_multi_input_fir(
        [m1, m2], [(20, 0.0), (200, 0.0)],
        num_taps=4096, sample_rate=48000, phase_mode="minimum",
        freq_focus_hz=(40.0, 100.0), regularization_lambda=0.01)
    assert "self_cancellation_margin_db" in result
    assert result["self_cancellation_margin_db"] <= 0.01
    assert result["self_cancellation_margin_db"] > -3.0  # coherent, no notch


def test_self_cancellation_margin_focus_band_entirely_outside_grid():
    """If freq_focus_hz falls entirely outside the FFT grid the in-band mask is
    empty; the helper must fall back to the whole band (np.min on an empty slice
    would otherwise crash). Returns the whole-band margin, no exception."""
    from calibrate.multi_fir import _self_cancellation_margin
    freqs = np.linspace(20.0, 200.0, 100)
    a = np.full(100, 0.5 + 0.0j)
    whole = _self_cancellation_margin(a + a, np.abs(a) + np.abs(a), freqs, None)
    outside = _self_cancellation_margin(
        a + a, np.abs(a) + np.abs(a), freqs, (500.0, 1000.0))
    assert outside == whole  # fell back to whole-band, did not crash


# ---------------------------------------------------------------------------
# Bounded-pre-ring windowed-target mixed-phase (decay_correction_ms)
#
# Invert only the EARLY part of modal decay with a bounded pre-ring, so the
# realised correction stays CAUSAL-dominant (zeros on the poles → energy
# decays FORWARD) and does NOT become the time-reversed matched filter that
# re-excites the mode (+40 dB, T60 unchanged). See multi_fir.py:736 and the
# acoustician's spec: f_pre = E(t<peak)/E_total > 0.5 ⇒ anti-causal/matched.
# ---------------------------------------------------------------------------


def _second_order_allpass(f0_hz, r, sample_rate):
    """2nd-order all-pass impulse response (pole r∠θ, zero at conj-reciprocal).

    Localised excess phase around f0 — models a room cancellation, |A|=1 so
    it preserves the cascaded mode's magnitude/T60 but makes H non-min-phase.
    Returns (b, a) biquad coefficients.
    """
    theta = 2 * np.pi * f0_hz / sample_rate
    a1 = -2 * r * np.cos(theta)
    a2 = r * r
    # All-pass: b = reverse(a)
    b = np.array([a2, a1, 1.0])
    a = np.array([1.0, a1, a2])
    return b, a


def _synth_excess_phase_mode(
    mode_freq_hz=60.0, t60_ms=400.0, n_samples=48000, sample_rate=48000,
    peak_at=100, allpass_r=0.988, noise_db=-60.0, seed=0,
):
    """Single-sub IR: decaying mode cascaded with a 2nd-order all-pass →
    a NON-minimum-phase (excess-phase) response. Small noise floor added so
    the decay estimators' noise gates are exercised like on real data."""
    from scipy.signal import lfilter
    ir = np.zeros(n_samples)
    ir[peak_at] = 1.0
    decay_rate = math.log(1000) / (t60_ms / 1000.0)
    t = np.arange(n_samples - peak_at) / sample_rate
    mode = np.exp(-decay_rate * t) * np.sin(2 * np.pi * mode_freq_hz * t)
    ir[peak_at:] += mode * 0.5
    # Cascade the all-pass to add excess phase (keeps |H| / T60 unchanged).
    b, a = _second_order_allpass(mode_freq_hz, allpass_r, sample_rate)
    ir = lfilter(b, a, ir)
    # Small white noise floor, relative to peak.
    rng = np.random.default_rng(seed)
    peak = float(np.max(np.abs(ir))) + 1e-30
    ir = ir + rng.standard_normal(n_samples) * peak * 10 ** (noise_db / 20.0)
    return ir


def _measurement_from_ir(ir, sample_rate, freqs, label=""):
    """Derive a SubMeasurement (mag dB + phase) from a time-domain IR."""
    ir = np.asarray(ir, dtype=float)
    n_fft = 1
    while n_fft < len(ir):
        n_fft *= 2
    H = np.fft.rfft(ir, n=n_fft)
    fo = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    freqs = np.asarray(freqs, dtype=float)
    mag_db = np.interp(freqs, fo, 20 * np.log10(np.abs(H) + 1e-12))
    phase = np.interp(freqs, fo, np.unwrap(np.angle(H)))
    return SubMeasurement(freqs=list(freqs), spl_db=list(mag_db),
                          phase_rad=list(phase), label=label)


def _t20_at(ir, freq_hz, sample_rate=48000):
    """T20 (≈ early-decay) of a single mode band, via the clean-decay estimator."""
    from scipy.signal import butter, sosfiltfilt
    from calibrate.decay import _estimate_t60_clean_decay, _lundeby_t60
    ir = np.asarray(ir, dtype=float)
    nyq = sample_rate / 2.0
    lo = freq_hz * 2 ** (-1.0 / 6) / nyq
    hi = freq_hz * 2 ** (1.0 / 6) / nyq
    sos = butter(4, [lo, hi], btype="bandpass", output="sos")
    band = sosfiltfilt(sos, ir)
    t = _estimate_t60_clean_decay(band, sample_rate)
    if t is None:
        t = _lundeby_t60(band ** 2, sample_rate)
    return t


def test_decay_correction_ms_realizes_causal_dominant_correction():
    """SYNTHETIC EXCESS-PHASE MODE: with decay_correction_ms set, the realised
    correction must be CAUSAL-dominant (most energy AFTER the peak), shorten the
    mode's early decay (T20 drops), and NOT amplify the steady-state mode (no
    matched-filter +40 dB trap)."""
    sr = 48000
    ir = _synth_excess_phase_mode(mode_freq_hz=60.0, t60_ms=400.0,
                                  n_samples=sr, sample_rate=sr, allpass_r=0.988)
    freqs = np.linspace(20, 200, 400).tolist()
    m = _measurement_from_ir(ir, sr, freqs, label="sub")
    target = [(20, 0.0), (200, 0.0)]
    res = design_multi_input_fir(
        [m], target, num_taps=8192, sample_rate=sr, phase_mode="mixed",
        preringing_ms=20.0, decay_correction_ms=80.0,
        regularization_lambda=0.05, freq_focus_hz=(40.0, 120.0),
    )

    # (a) causal-dominant: the design must report the pre-peak energy fraction
    # and it must be ≤ 0.5 (more than half pre-peak energy = matched filter).
    assert "pre_ring_energy_fraction" in res
    assert "matched_filter_unsafe" in res
    f_pre = res["pre_ring_energy_fraction"][0]
    assert f_pre <= 0.5, f"correction is anti-causal dominant (f_pre={f_pre:.3f})"
    assert res["matched_filter_unsafe"] is False

    # (b) shortens early decay: convolve correction with the mode IR; T20 drops.
    corr = np.asarray(res["firs"][0])
    corrected = np.convolve(ir, corr)[: len(ir)]
    t20_before = _t20_at(ir, 60.0, sr)
    t20_after = _t20_at(corrected, 60.0, sr)
    assert t20_before is not None and t20_after is not None
    assert t20_after < t20_before * 0.9, (
        f"early decay must drop materially: before={t20_before:.0f}ms "
        f"after={t20_after:.0f}ms"
    )

    # (c) steady-state mode magnitude NOT amplified > 3 dB (matched-filter guard).
    n_fft = 65536
    fo = np.fft.rfftfreq(n_fft, d=1.0 / sr)
    bidx = int(np.argmin(np.abs(fo - 60.0)))
    H_before = np.abs(np.fft.rfft(ir, n=n_fft))[bidx]
    H_after = np.abs(np.fft.rfft(corrected, n=n_fft))[bidx]
    gain_db = 20 * np.log10((H_after + 1e-12) / (H_before + 1e-12))
    assert gain_db < 3.0, f"steady-state mode amplified {gain_db:.1f} dB (matched-filter trap)"


def test_decay_correction_flags_matched_filter_as_unsafe():
    """CONTROL — matched-filter rejection: a deliberately time-reversed-ringing
    correction (anti-causal energy dominant) must be FLAGGED unsafe by the new
    pre-ring-energy check."""
    from calibrate.multi_fir import _matched_filter_unsafe, _pre_ring_energy_fraction
    sr = 48000
    n = 4096
    # A causal decaying mode (energy after the peak) — SAFE.
    causal = np.zeros(n)
    peak = 200
    causal[peak] = 1.0
    t = np.arange(n - peak) / sr
    causal[peak:] += 0.6 * np.exp(-t / 0.05) * np.sin(2 * np.pi * 60 * t)
    f_causal = _pre_ring_energy_fraction(causal)
    assert f_causal < 0.5
    assert _matched_filter_unsafe(causal) is False

    # Its time-reversal = the matched filter (energy GROWS toward the peak) — UNSAFE.
    matched = causal[::-1].copy()
    f_matched = _pre_ring_energy_fraction(matched)
    assert f_matched > 0.5, f_matched
    assert _matched_filter_unsafe(matched) is True


def test_decay_correction_ms_improves_self_cancellation_two_sub():
    """TWO-SUB: with decay_correction_ms set, the self-cancellation margin is
    materially better (less negative) than the same design with
    decay_correction_ms=None, because the bounded window reduces the truncation
    cancellation that leaves each sub at a different residual phase."""
    sr = 48000
    # Two subs, same mode but DIFFERENT excess phase (different all-pass r) so
    # the full-inversion truncation leaves different residual phases → they
    # cancel; the bounded window keeps them causal-dominant and coherent.
    ir5 = _synth_excess_phase_mode(mode_freq_hz=60.0, t60_ms=400.0, n_samples=sr,
                                   sample_rate=sr, allpass_r=0.985, seed=1)
    ir6 = _synth_excess_phase_mode(mode_freq_hz=60.0, t60_ms=400.0, n_samples=sr,
                                   sample_rate=sr, allpass_r=0.992, seed=2)
    freqs = np.linspace(20, 200, 400).tolist()
    m5 = _measurement_from_ir(ir5, sr, freqs, label="sub5")
    m6 = _measurement_from_ir(ir6, sr, freqs, label="sub6")
    target = [(20, 0.0), (200, 0.0)]
    common = dict(num_taps=8192, sample_rate=sr, phase_mode="mixed",
                  preringing_ms=20.0, regularization_lambda=0.05,
                  freq_focus_hz=(40.0, 120.0))

    res_none = design_multi_input_fir([m5, m6], target, **common)
    res_bounded = design_multi_input_fir([m5, m6], target,
                                         decay_correction_ms=80.0, **common)

    assert res_bounded["self_cancellation_margin_db"] > res_none["self_cancellation_margin_db"], (
        f"bounded must improve margin: bounded="
        f"{res_bounded['self_cancellation_margin_db']:.2f} "
        f"none={res_none['self_cancellation_margin_db']:.2f}"
    )


def test_decay_correction_ms_none_is_backward_compatible():
    """Backward-compat: decay_correction_ms=None reproduces existing mixed-phase
    behavior EXACTLY (identical FIR taps)."""
    freqs = np.linspace(20, 200, 120).tolist()
    mag = [12.0 if 45 <= f <= 55 else 0.0 for f in freqs]
    m1 = _synth_measurement(freqs, mag, label="sub1")
    m2 = _synth_measurement(freqs, mag, label="sub2")
    target = [(20, 0.0), (200, 0.0)]
    common = dict(num_taps=4096, sample_rate=48000, phase_mode="mixed",
                  preringing_ms=20.0, freq_focus_hz=(30.0, 120.0),
                  regularization_lambda=0.01)
    res_default = design_multi_input_fir([m1, m2], target, **common)
    res_none = design_multi_input_fir([m1, m2], target,
                                      decay_correction_ms=None, **common)
    for a, b in zip(res_default["firs"], res_none["firs"]):
        assert np.allclose(np.asarray(a), np.asarray(b), atol=0, rtol=0), (
            "decay_correction_ms=None must reproduce existing taps exactly"
        )


def test_decay_correction_ms_rejects_bad_preringing_budget():
    """preringing_ms must be ≤ decay_correction_ms / 2 (causal-dominant balance);
    a larger pre-ring inverts the causal/anti-causal balance into a matched
    filter and must be rejected at the API boundary."""
    freqs = np.linspace(20, 200, 60).tolist()
    m1 = _synth_measurement(freqs, [0.0] * 60, label="sub1")
    m2 = _synth_measurement(freqs, [0.0] * 60, label="sub2")
    target = [(20, 0.0), (200, 0.0)]
    with pytest.raises(ValueError, match="preringing_ms"):
        design_multi_input_fir(
            [m1, m2], target, num_taps=2048, sample_rate=48000,
            phase_mode="mixed", preringing_ms=60.0, decay_correction_ms=80.0,
            freq_focus_hz=(40.0, 120.0),
        )


def test_trinnov_threads_decay_correction_ms():
    """design_fir_trinnov must thread decay_correction_ms through to the Wiener
    design and surface the matched-filter safety fields."""
    sr = 48000
    ir = _synth_excess_phase_mode(mode_freq_hz=60.0, t60_ms=400.0,
                                  n_samples=sr, sample_rate=sr, allpass_r=0.988)
    freqs = np.linspace(20, 120, 200).tolist()
    m = _measurement_from_ir(ir, sr, freqs, label="sub")
    result = design_fir_trinnov(
        ir.tolist(), [m], [(20, 0.0), (120, 0.0)],
        sample_rate=sr, num_taps=8192, phase_mode="mixed",
        preringing_ms=20.0, decay_correction_ms=80.0,
        regularization_lambda=0.05, freq_focus_hz=(40.0, 120.0),
    )
    assert "matched_filter_unsafe" in result
    assert "pre_ring_energy_fraction" in result
    assert result["matched_filter_unsafe"] is False
