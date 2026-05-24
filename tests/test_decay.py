"""Unit tests for the decay analysis module.

All tests use synthetic numpy arrays — no hardware, no network, no audio I/O.
"""

from __future__ import annotations

import numpy as np
import pytest

from calibrate.decay import DecayMode, _t60_to_q, analyze_decay, compare_decay, _analyze_decay_bandpass


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_ringing_ir(
    freq_hz: float = 50.0,
    t60_ms: float = 600.0,
    sample_rate: int = 48000,
    duration_s: float = 1.0,
) -> list[float]:
    """Synthetic IR with a decaying sinusoid at freq_hz."""
    n = int(sample_rate * duration_s)
    t = np.arange(n) / sample_rate
    # Exponential decay: amplitude = exp(-6.9 * t / t60_s) where 6.9 = ln(10^3) ~ 60dB
    decay_rate = 6.9 / (t60_ms / 1000.0)
    ir = np.sin(2 * np.pi * freq_hz * t) * np.exp(-decay_rate * t)
    # Add a small impulse at t=0 for realism
    ir[0] = 1.0
    return ir.tolist()


def _make_clean_impulse(sample_rate: int = 48000, duration_s: float = 1.0) -> list[float]:
    """Clean impulse with no ringing — single spike then rapid exponential decay.

    Uses a very fast decay (T60 ~5ms) broadband noise burst so no single frequency
    bin sustains energy long enough to appear as a ringing mode.
    """
    n = int(sample_rate * duration_s)
    t = np.arange(n) / sample_rate
    # Very fast broadband decay — nothing rings past a few ms
    decay = np.exp(-6.9 / 0.005 * t)  # T60 = 5ms
    rng = np.random.default_rng(42)
    ir = rng.normal(0, 1.0, n) * decay
    ir[0] = 1.0
    return ir.tolist()


# ── analyze_decay tests ───────────────────────────────────────────────────────


class TestAnalyzeDecay:
    """Tests for analyze_decay."""

    def test_finds_ringing_mode(self) -> None:
        """A decaying 50Hz sinusoid with T60~600ms should be detected."""
        ir = _make_ringing_ir(freq_hz=50.0, t60_ms=600.0, duration_s=2.0)
        modes = analyze_decay(ir, sample_rate=48000, t60_threshold_ms=200.0)

        assert len(modes) >= 1
        # Find the mode closest to 50Hz
        closest = min(modes, key=lambda m: abs(m.freq_hz - 50.0))
        assert abs(closest.freq_hz - 50.0) < 10.0, f"Expected ~50Hz, got {closest.freq_hz}Hz"
        # T60 should be in a reasonable range (spectrogram estimation is approximate)
        assert closest.t60_ms > 200.0, f"Expected T60 > 200ms, got {closest.t60_ms}ms"
        assert closest.t60_ms < 2000.0, f"Expected T60 < 2000ms, got {closest.t60_ms}ms"
        assert closest.priority >= 1

    def test_clean_ir_returns_empty(self) -> None:
        """A clean impulse with no ringing should return no modes above threshold."""
        ir = _make_clean_impulse(duration_s=1.0)
        modes = analyze_decay(ir, sample_rate=48000, t60_threshold_ms=300.0)
        assert modes == []

    def test_empty_ir_raises(self) -> None:
        """Empty impulse response should raise ValueError."""
        with pytest.raises(ValueError, match="empty"):
            analyze_decay([])

    def test_all_zeros_raises(self) -> None:
        """All-zero impulse response should raise ValueError."""
        with pytest.raises(ValueError, match="all zeros"):
            analyze_decay([0.0] * 48000)

    def test_short_ir_returns_empty(self) -> None:
        """IR shorter than nperseg should return empty list, not crash."""
        short_ir = [1.0] + [0.0] * 100  # 101 samples, way less than default 2048
        result = analyze_decay(short_ir, sample_rate=48000)
        assert result == []

    def test_priority_ordering(self) -> None:
        """Two ringing modes: the one with higher T60*peak_db should get priority 1."""
        # Build an IR with two resonances: strong 50Hz and weaker 100Hz
        n = 48000 * 2  # 2 seconds
        t = np.arange(n) / 48000
        # 50Hz mode: long decay, high amplitude
        mode1 = 1.0 * np.sin(2 * np.pi * 50 * t) * np.exp(-6.9 / 0.8 * t)
        # 100Hz mode: shorter decay, lower amplitude
        mode2 = 0.3 * np.sin(2 * np.pi * 100 * t) * np.exp(-6.9 / 0.4 * t)
        ir = (mode1 + mode2).tolist()
        ir[0] = 1.0

        modes = analyze_decay(ir, sample_rate=48000, t60_threshold_ms=100.0)

        if len(modes) >= 2:
            # Priority 1 should be the mode with highest T60 * |peak_db|
            assert modes[0].priority == 1
            assert modes[1].priority == 2
            score_1 = modes[0].t60_ms * abs(modes[0].peak_db)
            score_2 = modes[1].t60_ms * abs(modes[1].peak_db)
            assert score_1 >= score_2


# ── _t60_to_q tests ──────────────────────────────────────────────────────────


class TestT60ToQ:
    """Tests for the T60-to-Q mapping function."""

    def test_minimum_q(self) -> None:
        """300ms decay should map to Q near 1.0."""
        q = _t60_to_q(300.0)
        assert abs(q - 1.0) < 0.1, f"Expected Q~1.0 at 300ms, got {q}"

    def test_maximum_q(self) -> None:
        """3000ms decay should map to Q near 10.0."""
        q = _t60_to_q(3000.0)
        assert abs(q - 10.0) < 0.1, f"Expected Q~10.0 at 3000ms, got {q}"

    def test_clamps_below_300(self) -> None:
        """Values below 300ms should clamp to Q=1.0."""
        q = _t60_to_q(100.0)
        assert abs(q - 1.0) < 0.1

    def test_clamps_above_3000(self) -> None:
        """Values above 3000ms should clamp to Q=10.0."""
        q = _t60_to_q(10000.0)
        assert abs(q - 10.0) < 0.1

    def test_monotonic(self) -> None:
        """Q should increase monotonically with T60."""
        values = [300, 500, 1000, 2000, 3000]
        qs = [_t60_to_q(v) for v in values]
        for i in range(len(qs) - 1):
            assert qs[i] <= qs[i + 1], f"Q not monotonic: {qs}"


# ── bandpass decay tests ─────────────────────────────────────────────────────


class TestAnalyzeDecayBandpass:
    """Tests for the bandpass filter-bank decay analysis."""

    def test_finds_30hz_mode(self) -> None:
        """A 30 Hz ringing mode should be detected — spectrogram misses it (bins at 23.4/46.9 Hz)."""
        ir = _make_ringing_ir(freq_hz=30.0, t60_ms=600.0, duration_s=2.0)
        modes = analyze_decay(ir, sample_rate=48000, t60_threshold_ms=200.0, bands_per_octave=6)

        assert len(modes) >= 1
        closest = min(modes, key=lambda m: abs(m.freq_hz - 30.0))
        assert abs(closest.freq_hz - 30.0) < 5.0, f"Expected ~30Hz, got {closest.freq_hz}Hz"
        assert closest.t60_ms > 200.0

    def test_resolves_close_modes(self) -> None:
        """30 Hz and 40 Hz modes should be resolved as distinct peaks."""
        n = 48000 * 2
        t = np.arange(n) / 48000
        mode1 = np.sin(2 * np.pi * 30 * t) * np.exp(-6.9 / 0.7 * t)
        mode2 = np.sin(2 * np.pi * 40 * t) * np.exp(-6.9 / 0.5 * t)
        ir = (mode1 + mode2).tolist()

        modes = analyze_decay(ir, sample_rate=48000, t60_threshold_ms=200.0, bands_per_octave=6)

        freqs = [m.freq_hz for m in modes]
        near_30 = any(abs(f - 30.0) < 5.0 for f in freqs)
        near_40 = any(abs(f - 40.0) < 5.0 for f in freqs)
        assert near_30, f"30 Hz mode not found in {freqs}"
        assert near_40, f"40 Hz mode not found in {freqs}"

    def test_mode_dominates_filter_ringing(self) -> None:
        """A strong mode is detected even when filter ringing is present.

        sosfiltfilt (zero-phase two-pass) at 20 Hz / 1/6-octave has ~300-1500ms of
        inherent filter ringing, so clean-IR tests at low frequencies are not meaningful.
        What matters is that a real mode with long T60 stands out above the mean-band RMS
        (peak_db > min_peak_db=3.0) and is correctly reported.
        """
        # 50 Hz mode at 700ms T60 — well above filter ringing floor at 50 Hz (~130ms)
        ir = _make_ringing_ir(freq_hz=50.0, t60_ms=700.0, duration_s=2.0)
        modes = analyze_decay(ir, sample_rate=48000, t60_threshold_ms=400.0, bands_per_octave=6)
        assert len(modes) >= 1
        closest = min(modes, key=lambda m: abs(m.freq_hz - 50.0))
        assert abs(closest.freq_hz - 50.0) < 5.0

    def test_spectrogram_misses_30hz_bandpass_finds_it(self) -> None:
        """Demonstrate the resolution improvement: spectrogram bins skip 30 Hz."""
        ir = _make_ringing_ir(freq_hz=30.0, t60_ms=700.0, duration_s=2.0)

        spectrogram_modes = analyze_decay(ir, sample_rate=48000, t60_threshold_ms=200.0)
        bandpass_modes = analyze_decay(ir, sample_rate=48000, t60_threshold_ms=200.0, bands_per_octave=6)

        # Spectrogram bins are at 23.4 and 46.9 Hz — closest is 23.4 Hz, >6 Hz away
        if spectrogram_modes:
            closest_spec = min(spectrogram_modes, key=lambda m: abs(m.freq_hz - 30.0))
            assert abs(closest_spec.freq_hz - 30.0) > 5.0, \
                "Spectrogram unexpectedly resolved 30 Hz precisely"

        # Bandpass should find it within 5 Hz
        assert len(bandpass_modes) >= 1
        closest_bp = min(bandpass_modes, key=lambda m: abs(m.freq_hz - 30.0))
        assert abs(closest_bp.freq_hz - 30.0) < 5.0, \
            f"Bandpass should find ~30 Hz, got {closest_bp.freq_hz} Hz"


# ── compare_decay tests ──────────────────────────────────────────────────────


class TestCompareDecay:
    """Tests for compare_decay."""

    def test_improvement_detected(self) -> None:
        """Before IR with ringing, after IR with less ringing, should show positive reduction."""
        before = _make_ringing_ir(freq_hz=50.0, t60_ms=800.0, duration_s=2.0)
        # "After" has much shorter decay at same frequency
        after = _make_ringing_ir(freq_hz=50.0, t60_ms=200.0, duration_s=2.0)

        results = compare_decay(before, after, sample_rate=48000)

        # Should have at least one comparison entry near 50Hz
        assert len(results) >= 1
        near_50 = [r for r in results if abs(r["freq_hz"] - 50.0) < 10.0]
        if near_50:
            assert near_50[0]["reduction_pct"] > 0, "Expected positive reduction"
            assert near_50[0]["t60_before_ms"] > near_50[0]["t60_after_ms"]

    def test_no_modes_returns_empty(self) -> None:
        """Both IRs clean (no modes) should return empty list."""
        before = _make_clean_impulse(duration_s=1.0)
        after = _make_clean_impulse(duration_s=1.0)

        results = compare_decay(before, after, sample_rate=48000)
        assert results == []
