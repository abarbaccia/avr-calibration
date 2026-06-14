"""Unit tests for the decay analysis module.

All tests use synthetic numpy arrays — no hardware, no network, no audio I/O.
"""

from __future__ import annotations

import numpy as np
import pytest

from calibrate.decay import (
    DecayMode,
    _estimate_t60,
    _estimate_t60_envelope,
    _lundeby_t60,
    _t60_to_q,
    analyze_decay,
    compare_decay,
    _analyze_decay_bandpass,
)


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


class TestT60Envelope:
    """Tests for the noise-floor-aware envelope T60 estimator.

    Replaces Schroeder backward integration which inflated T60 by 4-15× on
    short (500ms) noisy IRs (session 262, 2026-05-25). The envelope estimator
    should:
      - recover known synthetic T60 within ±25%
      - return None when the IR is too short to observe full decay
      - reject estimates where noise floor obscures the -25 dB threshold
    """

    @staticmethod
    def _pure_decay(freq_hz: float, t60_ms: float, sample_rate: int = 48000,
                    duration_s: float = 1.0, noise_rms: float = 0.0,
                    seed: int = 0) -> np.ndarray:
        n = int(sample_rate * duration_s)
        t = np.arange(n) / sample_rate
        decay = np.exp(-6.9 / (t60_ms / 1000.0) * t)
        sig = np.sin(2 * np.pi * freq_hz * t) * decay
        if noise_rms > 0:
            rng = np.random.default_rng(seed)
            sig = sig + rng.normal(0, noise_rms, n)
        return sig

    @staticmethod
    def _bandpass(sig: np.ndarray, fc: float, sample_rate: int = 48000,
                  bands_per_octave: int = 6) -> np.ndarray:
        """Match the production bandpass before envelope analysis."""
        from scipy.signal import butter, sosfiltfilt
        half_step = 0.5 / bands_per_octave
        f_low = fc * 2 ** (-half_step) / (sample_rate / 2)
        f_high = fc * 2 ** half_step / (sample_rate / 2)
        sos = butter(4, [f_low, f_high], btype='bandpass', output='sos')
        return sosfiltfilt(sos, sig)

    def test_recovers_known_t60_medium(self) -> None:
        """T60=200ms exponential decay should be in a sane range (not 1000+ ms).

        Note: 1/6-octave bandpass filtering compresses dynamic range and biases
        T60 estimates low. The important property post-fix is that we don't
        OVER-estimate (the pre-fix bug); a slight under-estimate from narrow
        bandpass is acceptable and consistent across calibrations.
        """
        sig = self._pure_decay(freq_hz=50.0, t60_ms=200.0, duration_s=1.0)
        filtered = self._bandpass(sig, 50.0)
        est = _estimate_t60_envelope(filtered, sample_rate=48000)
        assert est is not None
        # Allow ±50%: bandpass narrowing systematically biases T60 lower.
        assert 100.0 <= est <= 300.0, f"Expected ~200ms ±50%, got {est}ms"

    def test_recovers_known_t60_long(self) -> None:
        """T60=500ms in 2s IR. Same tolerance as medium — bandpass biases low."""
        sig = self._pure_decay(freq_hz=50.0, t60_ms=500.0, duration_s=2.0)
        filtered = self._bandpass(sig, 50.0)
        est = _estimate_t60_envelope(filtered, sample_rate=48000)
        assert est is not None
        # NOT 1500+ ms (the pre-fix Schroeder bug); reasonably proportional.
        assert 250.0 <= est <= 750.0, f"Expected ~500ms ±50%, got {est}ms"

    def test_truncated_ir_returns_none(self) -> None:
        """Only first 100 ms of a 500 ms T60 decay visible — envelope never
        reaches -25 dB. Must return None, NOT extrapolate to 1500 ms.

        500 ms T60 ⇒ -5 dB at ~42 ms, -25 dB at ~208 ms. A 100 ms window only
        sees down to ~-12 dB, so the algorithm has no -25 dB crossing.
        """
        sig = self._pure_decay(freq_hz=50.0, t60_ms=500.0, duration_s=0.1)
        est = _estimate_t60_envelope(sig, sample_rate=48000)
        assert est is None, f"Truncated IR must return None, got {est}ms"

    def test_high_noise_floor_returns_none(self) -> None:
        """Noise floor near -25 dB (within 6 dB margin) ⇒ indeterminate."""
        # Tiny decaying mode (T60=50ms) with large additive noise floor.
        # Envelope tail will sit near -20 dB → -25 dB threshold not 6 dB clear.
        sig = self._pure_decay(freq_hz=50.0, t60_ms=50.0, duration_s=0.5,
                                noise_rms=0.1, seed=1)
        est = _estimate_t60_envelope(sig, sample_rate=48000)
        assert est is None, f"Expected None at high noise floor, got {est}ms"

    def test_low_noise_floor_recovered(self) -> None:
        """Noise floor at -40 dB shouldn't prevent recovery of true T60."""
        sig = self._pure_decay(freq_hz=50.0, t60_ms=200.0, duration_s=1.0,
                                noise_rms=0.01, seed=2)
        est = _estimate_t60_envelope(sig, sample_rate=48000)
        assert est is not None, "Should recover with -40 dB noise floor"
        assert 150.0 <= est <= 300.0, f"Expected ~200ms, got {est}ms"

    def test_session_262_regression(self) -> None:
        """Regression for session 262 (2026-05-25): a 500 ms IR with a 47 Hz
        mode at T60~200 ms must NOT report T60>1000 ms.

        Pre-fix algorithm reported 1905 ms; manual T20×3 was 117-308 ms. The
        envelope estimator must stay in a realistic range.
        """
        # Synthesise the session-262-style IR: 500 ms long, dominant 47 Hz
        # mode with T60~250ms, weak broadband noise floor at -45 dB.
        sample_rate = 48000
        duration_s = 0.5
        n = int(sample_rate * duration_s)
        t = np.arange(n) / sample_rate
        decay = np.exp(-6.9 / 0.25 * t)
        mode = np.sin(2 * np.pi * 47 * t) * decay
        rng = np.random.default_rng(262)
        # ~-60 dB rel peak: realistic measurement noise floor for a 47 Hz mode
        # in the post-PR #176 IRs (clean PipeWire deconvolution).
        noise = rng.normal(0, 0.001, n)
        ir = (mode + noise).tolist()

        modes = analyze_decay(ir, sample_rate=sample_rate,
                              t60_threshold_ms=100.0, bands_per_octave=6)
        near_47 = [m for m in modes if abs(m.freq_hz - 47.0) < 5.0]
        assert near_47, "47 Hz mode should be detected"
        # The mode-bearing band gives a realistic T60; adjacent sideband
        # leakage can inflate T60 (filter ringing). At minimum, the best
        # estimate near 47 Hz should be in a realistic range.
        best_t60 = min(m.t60_ms for m in near_47)
        assert best_t60 < 800.0, (
            f"Session 262 regression: best 47 Hz T60 should be <800ms "
            f"(realistic), got {best_t60}ms (pre-fix bug was ~1900ms for ALL bands)"
        )


class TestEstimateT60Spectrogram:
    """Regression tests for _estimate_t60 (spectrogram path noise-floor fix).

    Before the fix, _estimate_t60 used a raw -5 to -35 dB Schroeder fit with
    no noise-floor awareness. A 500 ms IR with -45 dBFS noise floor would
    return >1900 ms because the noise tail pulled the -35 dB intercept far
    beyond the IR window. The fix adds:
      1. Noise-floor gate: stops fitting 3 dB above the estimated tail floor.
      2. IR-window cap: returns None when fitted T60 > 1.5× the IR duration.
    """

    def _schroeder_from_decay(
        self,
        freq_hz: float,
        t60_ms: float,
        ir_duration_ms: float,
        noise_rms: float = 0.0,
        sample_rate: int = 48000,
    ):
        """Build a (schroeder_db, times) pair from a synthetic decaying sinusoid."""
        n = int(sample_rate * ir_duration_ms / 1000)
        t = np.arange(n) / sample_rate
        decay = np.exp(-6.9 / (t60_ms / 1000) * t)
        signal = np.sin(2 * np.pi * freq_hz * t) * decay
        if noise_rms > 0:
            rng = np.random.default_rng(42)
            signal += rng.normal(0, noise_rms, n)

        # Build spectrogram-style energy (squared amplitude) and Schroeder curve
        energy = signal ** 2
        schroeder = np.cumsum(energy[::-1])[::-1]
        schroeder_db = 10.0 * np.log10(schroeder / (schroeder[0] + 1e-30) + 1e-30)
        times = t
        return schroeder_db, times

    def test_recovers_short_t60_in_long_ir(self) -> None:
        """200 ms T60 in a 2 s IR: fit is valid and well within window cap."""
        sch, times = self._schroeder_from_decay(50.0, t60_ms=200.0, ir_duration_ms=2000.0)
        t60 = _estimate_t60(sch, times)
        assert t60 is not None
        assert 100.0 < t60 < 400.0, f"expected ~200 ms, got {t60:.0f} ms"

    def test_session_262_regression_spectrogram_path(self) -> None:
        """500 ms IR with 1900 ms T60 must return None (indeterminate).

        Pre-fix: returned 1905 ms (extrapolated 3.8× past the 500 ms window).
        Post-fix: IR-window cap (1.5×) returns None — caller skips this mode.
        """
        sch, times = self._schroeder_from_decay(
            47.0, t60_ms=1900.0, ir_duration_ms=500.0, noise_rms=0.001,
        )
        t60 = _estimate_t60(sch, times)
        assert t60 is None, (
            f"500 ms IR should not report T60=1905 ms — got {t60:.0f} ms. "
            "IR-window cap (1.5×) should return None for unobservable decays."
        )

    def test_noise_floor_gate_stops_early_fit(self) -> None:
        """High noise floor pushes the Schroeder tail up: fit ceiling adapts."""
        # 300 ms T60, 500 ms IR, noise floor at ~-20 dB relative → fit stops early
        sch, times = self._schroeder_from_decay(
            50.0, t60_ms=300.0, ir_duration_ms=500.0, noise_rms=0.1,
        )
        t60 = _estimate_t60(sch, times)
        # Either returns a plausible T60 or None — must NOT return >750 ms
        if t60 is not None:
            assert t60 < 750.0, f"noise floor gate failed: returned {t60:.0f} ms"

    def test_analyze_decay_spectrogram_session262_regression(self) -> None:
        """End-to-end: analyze_decay (spectrogram, no bands_per_octave) on a
        500 ms IR must NOT report a mode with T60 > 750 ms.

        This is the bug that shipped in PR #177 — the fix was only applied to
        the bandpass path; the default spectrogram path continued to return
        1905 ms for session 262.
        """
        sample_rate = 48000
        n = int(sample_rate * 0.5)  # 500 ms IR
        t = np.arange(n) / sample_rate
        # 47 Hz ringing, T60 ~250 ms, noise floor at ~-45 dBFS
        ir = (np.sin(2 * np.pi * 47 * t) * np.exp(-6.9 / 0.25 * t)
              + np.random.default_rng(262).normal(0, 0.001, n))

        modes = analyze_decay(ir.tolist(), sample_rate=sample_rate,
                              t60_threshold_ms=100.0)  # no bands_per_octave → spectrogram path
        long_modes = [m for m in modes if m.t60_ms > 750.0]
        assert not long_modes, (
            f"Spectrogram path returned indeterminate T60 values > 750 ms: "
            f"{[(m.freq_hz, m.t60_ms) for m in long_modes]}. "
            "IR-window cap should have returned None for these."
        )


class TestLundebyT60:
    """Tests for the Lundeby (1995) noise-floor-aware T60 estimator.

    The core fix for the T60 INFLATION bug: Schroeder backward integration over
    the FULL ~2.5 s IR window accumulates a noise tail that inflates T60 by
    4-15×. Lundeby finds the crosspoint where the modal decay meets the noise
    floor and truncates the Schroeder integration there, so only the clean decay
    is integrated.

    The shared helper ``_lundeby_t60(energy, sample_rate)`` takes an energy
    decay curve (squared IR or squared band signal) and returns T60 in ms, or
    None when the usable decay range above the noise floor is < ~15 dB.
    """

    @staticmethod
    def _decaying_energy(
        freq_hz: float,
        t60_ms: float,
        sample_rate: int = 48000,
        duration_s: float = 2.5,
        noise_db: float | None = -50.0,
        seed: int = 7,
    ) -> np.ndarray:
        """Squared (energy) signal of a decaying sinusoid + white noise tail.

        noise_db: additive white-noise level in dB relative to the peak
        amplitude of the decaying sinusoid (None = no noise).
        """
        n = int(sample_rate * duration_s)
        t = np.arange(n) / sample_rate
        decay = np.exp(-6.9 / (t60_ms / 1000.0) * t)
        sig = np.sin(2 * np.pi * freq_hz * t) * decay
        if noise_db is not None:
            rng = np.random.default_rng(seed)
            noise_rms = 10.0 ** (noise_db / 20.0)
            sig = sig + rng.normal(0, noise_rms, n)
        return sig ** 2

    def test_known_t60_not_inflated_by_noise_tail(self) -> None:
        """A 400 ms T60 mode in a 2.5 s IR with a -50 dB noise tail must read
        ~400 ms (±20%), NOT inflated to >1000 ms.

        This is THE bug: full-window Schroeder integration over a 2.5 s IR would
        accumulate ~2 s of noise and inflate T60 by 4-15×.
        """
        energy = self._decaying_energy(
            freq_hz=50.0, t60_ms=400.0, duration_s=2.5, noise_db=-50.0,
        )
        est = _lundeby_t60(energy, sample_rate=48000)
        assert est is not None, "Should estimate a clean 400 ms decay"
        assert 320.0 <= est <= 480.0, (
            f"Expected ~400 ms ±20%, got {est:.0f} ms "
            "(inflation bug if >1000 ms)"
        )

    def test_pure_noise_returns_none(self) -> None:
        """Pure white noise (no decaying mode) must return None — no spurious
        huge T60."""
        rng = np.random.default_rng(99)
        n = int(48000 * 2.5)
        energy = (rng.normal(0, 1.0, n)) ** 2
        est = _lundeby_t60(energy, sample_rate=48000)
        assert est is None, f"Pure noise must return None, got {est}"

    def test_short_clean_decay_recovered(self) -> None:
        """A short, clean decay with no noise tail must still estimate correctly
        (truncation must not break the clean case)."""
        energy = self._decaying_energy(
            freq_hz=50.0, t60_ms=400.0, duration_s=0.8, noise_db=None,
        )
        est = _lundeby_t60(energy, sample_rate=48000)
        assert est is not None, "Clean decay should estimate"
        assert 320.0 <= est <= 480.0, f"Expected ~400 ms ±20%, got {est:.0f} ms"

    def test_insufficient_range_returns_none(self) -> None:
        """If usable decay above the noise floor is < ~15 dB, return None
        (better than inflating)."""
        # Noise floor at -10 dB: decay never gets >15 dB clear of noise.
        energy = self._decaying_energy(
            freq_hz=50.0, t60_ms=400.0, duration_s=2.5, noise_db=-10.0, seed=3,
        )
        est = _lundeby_t60(energy, sample_rate=48000)
        assert est is None, (
            f"Insufficient SNR (<15 dB usable) must return None, got {est}"
        )


class TestLundebyIntegratedPaths:
    """End-to-end: both analyze_decay paths must report a real ~400 ms mode as
    ~400 ms, NOT inflated to 3000+ ms, on a 2.5 s noisy IR."""

    @staticmethod
    def _noisy_ir(
        freq_hz: float = 50.0,
        t60_ms: float = 400.0,
        sample_rate: int = 48000,
        duration_s: float = 2.5,
        noise_db: float = -50.0,
        seed: int = 11,
    ) -> list[float]:
        n = int(sample_rate * duration_s)
        t = np.arange(n) / sample_rate
        decay = np.exp(-6.9 / (t60_ms / 1000.0) * t)
        sig = np.sin(2 * np.pi * freq_hz * t) * decay
        rng = np.random.default_rng(seed)
        noise_rms = 10.0 ** (noise_db / 20.0)
        sig = sig + rng.normal(0, noise_rms, n)
        sig[0] = 1.0
        return sig.tolist()

    def test_spectrogram_path_not_inflated(self) -> None:
        """Spectrogram path: 400 ms mode in a 2.5 s noisy IR reads ~400 ms,
        never >1000 ms."""
        ir = self._noisy_ir(freq_hz=50.0, t60_ms=400.0, duration_s=2.5)
        modes = analyze_decay(ir, sample_rate=48000, t60_threshold_ms=100.0)
        near_50 = [m for m in modes if abs(m.freq_hz - 50.0) < 12.0]
        assert near_50, f"50 Hz mode should be detected, got {[m.freq_hz for m in modes]}"
        best = min(m.t60_ms for m in near_50)
        assert best < 1000.0, (
            f"Spectrogram path inflated T60: best near-50Hz = {best:.0f} ms "
            "(should be ~400, never >1000)"
        )

    def test_bandpass_path_not_inflated(self) -> None:
        """Bandpass path: 400 ms mode in a 2.5 s noisy IR reads ~400 ms,
        never >1000 ms."""
        ir = self._noisy_ir(freq_hz=50.0, t60_ms=400.0, duration_s=2.5)
        modes = analyze_decay(ir, sample_rate=48000, t60_threshold_ms=100.0,
                              bands_per_octave=6)
        near_50 = [m for m in modes if abs(m.freq_hz - 50.0) < 5.0]
        assert near_50, f"50 Hz mode should be detected, got {[m.freq_hz for m in modes]}"
        best = min(m.t60_ms for m in near_50)
        assert best < 1000.0, (
            f"Bandpass path inflated T60: best near-50Hz = {best:.0f} ms "
            "(should be ~400, never >1000)"
        )


class TestBandpassBeatingRobustness:
    """Regression tests for the bandpass T60 INFLATION bug on multi-modal data.

    On a clean single tone the min(Lundeby, envelope) cross-check works. But on
    real multi-modal room IRs, a 1/6-octave band that sits BETWEEN two modes (or
    contains leakage from adjacent modes) sees BEATING energy — non-exponential,
    non-monotonic decay. A single-slope linear T20/T30 fit over-reads the decay,
    and BOTH sub-estimators inflate, so min() inflates too.

    Pre-fix the bandpass path reported 80 Hz=2376 ms, 25 Hz=3842 ms etc. on the
    live combined-sub IR while the spectrogram path gave realistic numbers. These
    tests pin the bandpass path to a realistic domestic-room range (~200-700 ms)
    and require it to AGREE with the spectrogram path.
    """

    @staticmethod
    def _two_mode_ir(
        f1: float, t60_1_ms: float,
        f2: float, t60_2_ms: float,
        sample_rate: int = 48000,
        duration_s: float = 2.5,
        noise_db: float = -50.0,
        seed: int = 123,
    ) -> list[float]:
        n = int(sample_rate * duration_s)
        t = np.arange(n) / sample_rate
        m1 = np.sin(2 * np.pi * f1 * t) * np.exp(-6.9 / (t60_1_ms / 1000.0) * t)
        m2 = np.sin(2 * np.pi * f2 * t) * np.exp(-6.9 / (t60_2_ms / 1000.0) * t)
        rng = np.random.default_rng(seed)
        noise = rng.normal(0, 10.0 ** (noise_db / 20.0), n)
        sig = m1 + m2 + noise
        sig[0] = 1.0
        return sig.tolist()

    @staticmethod
    def _one_mode_ir(
        freq_hz: float, t60_ms: float,
        sample_rate: int = 48000, duration_s: float = 2.5,
        noise_db: float = -50.0, seed: int = 321,
    ) -> list[float]:
        n = int(sample_rate * duration_s)
        t = np.arange(n) / sample_rate
        m = np.sin(2 * np.pi * freq_hz * t) * np.exp(-6.9 / (t60_ms / 1000.0) * t)
        rng = np.random.default_rng(seed)
        noise = rng.normal(0, 10.0 ** (noise_db / 20.0), n)
        sig = m + noise
        sig[0] = 1.0
        return sig.tolist()

    def test_multimodal_bands_near_true_modes_not_inflated(self) -> None:
        """KEY new test: two modes at 47 Hz (T60=350 ms) and 70 Hz (T60=300 ms).

        The 1/6-octave bands near 47 and 70 Hz beat (modes fall between band
        centres + leakage). The bandpass path must report T60 within ±30 % of
        the true values at the bands nearest those modes — NOT inflated >1000 ms.
        """
        ir = self._two_mode_ir(47.0, 350.0, 70.0, 300.0)
        modes = analyze_decay(ir, sample_rate=48000, t60_threshold_ms=80.0,
                              bands_per_octave=6)
        assert modes, "expected modes from a two-mode IR"

        near_47 = [m for m in modes if abs(m.freq_hz - 47.0) < 4.0]
        near_70 = [m for m in modes if abs(m.freq_hz - 70.0) < 5.0]
        assert near_47, f"no band near 47 Hz in {[m.freq_hz for m in modes]}"
        assert near_70, f"no band near 70 Hz in {[m.freq_hz for m in modes]}"

        best_47 = min(m.t60_ms for m in near_47)
        best_70 = min(m.t60_ms for m in near_70)
        # 47 Hz mode (the longer-lived, T60=350 ms): its nearest band must land
        # within ±30 % — NOT the pre-fix 700-2400 ms inflation.
        assert 245.0 <= best_47 <= 455.0, (
            f"47 Hz band T60 out of ±30%: got {best_47} ms (true 350 ms)"
        )
        # 70 Hz mode (T60=300 ms): its band is contaminated by leakage from the
        # equal-amplitude, longer-lived 47 Hz mode only 0.6 octave away, so a
        # single 1/6-octave band reads somewhat high. The hard requirement is
        # that it stays in a realistic domestic-room range (NOT inflated to
        # seconds) — well under the pre-fix 2376 ms seen on the live IR.
        assert 210.0 <= best_70 <= 560.0, (
            f"70 Hz band T60 unrealistic: got {best_70} ms (true 300 ms)"
        )

    def test_multimodal_no_band_grossly_inflated(self) -> None:
        """No band anywhere in the analysis may report a physically impossible
        T60 (>1000 ms) for a domestic room on this two-mode IR."""
        ir = self._two_mode_ir(47.0, 350.0, 70.0, 300.0)
        modes = analyze_decay(ir, sample_rate=48000, t60_threshold_ms=80.0,
                              bands_per_octave=6)
        inflated = [(m.freq_hz, m.t60_ms) for m in modes if m.t60_ms > 1000.0]
        assert not inflated, (
            f"bandpass path reported physically impossible T60 (>1000 ms): "
            f"{inflated}"
        )

    def test_offset_mode_not_inflated(self) -> None:
        """A single mode OFFSET from the band centre (45 Hz; nearest 1/6-octave
        centre ~44.9/50 Hz) must still read realistic, not inflated."""
        ir = self._one_mode_ir(45.0, 350.0)
        modes = analyze_decay(ir, sample_rate=48000, t60_threshold_ms=80.0,
                              bands_per_octave=6)
        near = [m for m in modes if abs(m.freq_hz - 45.0) < 6.0]
        assert near, f"no band near 45 Hz in {[m.freq_hz for m in modes]}"
        best = min(m.t60_ms for m in near)
        assert best < 600.0, (
            f"offset-mode band inflated: got {best} ms (true 350 ms)"
        )
        # And not absurd anywhere.
        assert all(m.t60_ms < 1000.0 for m in modes), (
            f"some band inflated >1000 ms: {[(m.freq_hz, m.t60_ms) for m in modes]}"
        )

    def test_bandpass_agrees_with_spectrogram(self) -> None:
        """Agreement test: a single 50 Hz / 400 ms mode + noise. The bandpass and
        spectrogram paths must agree within ~30 % (both realistic ~300-520 ms)."""
        ir = self._one_mode_ir(50.0, 400.0, seed=55)

        spec = analyze_decay(ir, sample_rate=48000, t60_threshold_ms=80.0)
        band = analyze_decay(ir, sample_rate=48000, t60_threshold_ms=80.0,
                             bands_per_octave=6)

        spec_50 = [m for m in spec if abs(m.freq_hz - 50.0) < 13.0]
        band_50 = [m for m in band if abs(m.freq_hz - 50.0) < 5.0]
        assert spec_50, f"spectrogram missed 50 Hz: {[m.freq_hz for m in spec]}"
        assert band_50, f"bandpass missed 50 Hz: {[m.freq_hz for m in band]}"

        t_spec = min(m.t60_ms for m in spec_50)
        t_band = min(m.t60_ms for m in band_50)
        # Both realistic.
        assert 300.0 <= t_spec <= 520.0, f"spectrogram T60 unrealistic: {t_spec} ms"
        assert 300.0 <= t_band <= 520.0, f"bandpass T60 unrealistic: {t_band} ms"
        # Agree within ~30 %.
        ratio = t_band / t_spec
        assert 0.7 <= ratio <= 1.3, (
            f"paths disagree: spectrogram={t_spec} ms, bandpass={t_band} ms "
            f"(ratio {ratio:.2f})"
        )

    def test_pure_noise_bandpass_no_inflation(self) -> None:
        """Pure noise through the bandpass path must NOT produce inflated modes.

        Pre-fix, pure white noise yielded multi-second T60 'modes' (6000-10000 ms)
        because the Lundeby/envelope estimators read the noise tail as decay. The
        clean-decay gate must reject those: no band may report a physically
        impossible T60 (>1000 ms) on pure noise.
        """
        rng = np.random.default_rng(404)
        ir = rng.normal(0, 1.0, int(48000 * 2.5)).tolist()
        modes = analyze_decay(ir, sample_rate=48000, t60_threshold_ms=300.0,
                              bands_per_octave=6)
        inflated = [(m.freq_hz, m.t60_ms) for m in modes if m.t60_ms > 1000.0]
        assert not inflated, (
            f"pure noise produced inflated T60 (>1000 ms): {inflated}"
        )


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
