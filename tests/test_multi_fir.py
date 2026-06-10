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
