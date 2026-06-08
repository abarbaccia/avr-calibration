"""Decay analysis — spectrogram-based T60 estimation for room mode identification.

Identifies ringing modes in a room's impulse response by computing a spectrogram,
applying Schroeder integration per frequency bin, and estimating T60 (time for
energy to decay 60dB). Modes with T60 > 300ms are flagged for correction.

FIR filters shorten these decays (time-domain correction); PEQ cuts the peak
magnitude but cannot shorten the ringing duration. Whether FIR is available —
and how many taps are on offer — depends on the DSP device. Read fir_capable
and fir_max_taps_per_output from eq_capabilities (get_config) before designing
a FIR. On IIR-only devices, the suggested_q values drive narrow PEQ cuts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

# Pre-import scipy at module load so the first analyze_decay call isn't slow.
# Without this, `from scipy.signal import spectrogram` inside the function
# triggers a full scipy + scipy.stats cold-start (~500 ms on the Pi).
try:
    from scipy.signal import spectrogram as _scipy_spectrogram  # noqa: F401
    from scipy.signal import decimate as _scipy_decimate        # noqa: F401
except ImportError:
    pass  # runtime import in functions will surface the error with context

log = logging.getLogger(__name__)


@dataclass
class DecayMode:
    """A room mode identified by decay analysis."""
    freq_hz: float       # center frequency of the ringing mode
    t60_ms: float        # estimated T60 in milliseconds
    peak_db: float       # peak magnitude in dB relative to broadband
    suggested_q: float   # longer decay → narrower Q for EQ targeting
    priority: int        # 1 = highest (T60 × peak_amplitude)


def analyze_decay(
    impulse_response: list[float],
    sample_rate: int = 48000,
    t60_threshold_ms: float = 300.0,
    freq_min: float = 20.0,
    freq_max: float = 200.0,
    nperseg: int = 2048,
    noverlap: int = 1536,
    bands_per_octave: int | None = None,
) -> list[DecayMode]:
    """Analyze impulse response for ringing modes via Schroeder integration.

    Two modes controlled by bands_per_octave:

    bands_per_octave=None (default): fixed-FFT spectrogram. Resolution is
    sample_rate/nperseg (23.4 Hz at 48kHz/2048). Fast, coarse.

    bands_per_octave=N: bandpass filter bank at 1/N-octave spacing. Resolution
    scales with frequency (e.g. ~2.4 Hz at 20 Hz with N=6). Use this when you
    need to resolve modes closer than ~1 octave apart, especially below 50 Hz.
    N=6 (1/6-octave) is a good default; N=12 is surgical but slow.

    Algorithm (bandpass mode):
    1. Generate center frequencies at 1/N-octave steps from freq_min to freq_max
    2. Per frequency: 4th-order Butterworth bandpass → Schroeder integration
    3. T60 estimation: linear fit from -5dB to -35dB, extrapolate to -60dB
    4. Filter to modes with T60 > threshold
    5. Priority scoring: T60_ms × abs(peak_db)

    Args:
        impulse_response: time-domain IR samples (48K samples = 1 second at 48kHz)
        sample_rate: sample rate in Hz
        t60_threshold_ms: minimum T60 to flag as a ringing mode (default 300ms)
        freq_min: lower frequency bound for mode search
        freq_max: upper frequency bound for mode search
        nperseg: spectrogram window size (only used when bands_per_octave is None)
        noverlap: spectrogram overlap (only used when bands_per_octave is None)
        bands_per_octave: if set, use bandpass filter bank instead of spectrogram

    Returns:
        List of DecayMode sorted by priority (highest first).
        Empty list if no modes exceed threshold or IR is too short.

    Raises:
        ValueError: if impulse_response is empty or all zeros
    """
    if bands_per_octave is not None:
        return _analyze_decay_bandpass(
            impulse_response, sample_rate, t60_threshold_ms,
            freq_min, freq_max, bands_per_octave,
        )
    return _analyze_decay_spectrogram(
        impulse_response, sample_rate, t60_threshold_ms,
        freq_min, freq_max, nperseg, noverlap,
    )


def _analyze_decay_spectrogram(
    impulse_response: list[float],
    sample_rate: int,
    t60_threshold_ms: float,
    freq_min: float,
    freq_max: float,
    nperseg: int,
    noverlap: int,
) -> list[DecayMode]:
    """Spectrogram-based decay analysis with automatic downsampling for speed.

    Downsamples the IR to the minimum sample rate needed for freq_max before
    computing the spectrogram. For 20-200 Hz analysis at 48 kHz this gives a
    ~24× speedup with no loss of accuracy.
    """
    import numpy as np
    from scipy.signal import spectrogram, decimate  # already imported at module level; this is a no-op cache hit

    ir = np.array(impulse_response, dtype=np.float64)

    if len(ir) == 0:
        raise ValueError("impulse_response is empty")
    if np.all(ir == 0):
        raise ValueError("impulse_response is all zeros")

    # Downsample to minimum rate needed: Nyquist must exceed freq_max by 4×.
    # Decimation factor is chosen to keep the effective sample rate ≥ 8 × freq_max
    # (generous anti-alias margin). Integer-only factors; skip if sample_rate is
    # already low enough.
    target_rate = max(int(freq_max * 8), 1600)   # ≥ 1.6 kHz even at freq_max=200
    decim_factor = max(1, sample_rate // target_rate)
    if decim_factor > 1:
        try:
            ir = decimate(ir, decim_factor, zero_phase=True).astype(np.float64)
            sample_rate = sample_rate // decim_factor
            # Adjust window params proportionally so time resolution is preserved.
            nperseg = max(64, nperseg // decim_factor)
            noverlap = max(nperseg // 2, noverlap // decim_factor)
        except Exception:
            pass  # fall back to full-rate analysis on any error

    if len(ir) < nperseg:
        log.warning("IR length %d < nperseg %d, no decay analysis possible", len(ir), nperseg)
        return []

    freqs, times, Sxx = spectrogram(
        ir, fs=sample_rate, nperseg=nperseg, noverlap=noverlap,
        scaling='spectrum',
    )

    freq_mask = (freqs >= freq_min) & (freqs <= freq_max)
    freqs_bass = freqs[freq_mask]
    Sxx_bass = Sxx[freq_mask, :]

    if len(freqs_bass) == 0 or Sxx_bass.shape[1] < 3:
        return []

    active_mask = np.max(Sxx_bass, axis=1) >= 1e-20
    active_energy = Sxx_bass[active_mask, :]
    broadband_energy = np.mean(active_energy) if active_energy.size > 0 else 1e-20

    modes: list[DecayMode] = []

    for i, freq in enumerate(freqs_bass):
        energy = Sxx_bass[i, :]
        if np.max(energy) < 1e-20:
            continue

        schroeder = np.cumsum(energy[::-1])[::-1]
        schroeder_db = 10.0 * np.log10(schroeder / (schroeder[0] + 1e-30) + 1e-30)
        t60_ms = _estimate_t60(schroeder_db, times)

        if t60_ms is None or t60_ms < t60_threshold_ms:
            continue

        peak_energy = np.max(energy)
        peak_db = 10.0 * np.log10(peak_energy / (broadband_energy + 1e-30) + 1e-30)
        suggested_q = _t60_to_q(t60_ms)

        modes.append(DecayMode(
            freq_hz=round(float(freq), 1),
            t60_ms=round(float(t60_ms), 1),
            peak_db=round(float(peak_db), 1),
            suggested_q=round(float(suggested_q), 2),
            priority=0,
        ))

    modes.sort(key=lambda m: m.t60_ms * abs(m.peak_db), reverse=True)
    for rank, mode in enumerate(modes, 1):
        mode.priority = rank
    return modes


def _analyze_decay_bandpass(
    impulse_response: list[float],
    sample_rate: int,
    t60_threshold_ms: float,
    freq_min: float,
    freq_max: float,
    bands_per_octave: int,
    min_peak_db: float = -6.0,
) -> list[DecayMode]:
    """High-resolution T60 analysis using a bandpass filter bank.

    Gives constant relative frequency resolution at all frequencies.
    At 20 Hz with bands_per_octave=6: ~2.4 Hz resolution vs 23.4 Hz for spectrogram.
    Uses 4th-order Butterworth bandpass + Schroeder integration per band.
    """
    import numpy as np
    from scipy.signal import butter, sosfiltfilt, decimate

    ir = np.array(impulse_response, dtype=np.float64)

    if len(ir) == 0:
        raise ValueError("impulse_response is empty")
    if np.all(ir == 0):
        raise ValueError("impulse_response is all zeros")

    # Downsample so freq_min sits at ≥5 % of Nyquist — near-DC Butterworth is
    # numerically degenerate and sosfiltfilt initial-conditions grow enormous,
    # making it ~100× slower or hang.  Mirror the spectrogram path's strategy.
    target_rate = max(int(freq_max * 10), 1600)
    decim_factor = max(1, sample_rate // target_rate)
    if decim_factor > 1:
        try:
            ir = decimate(ir, decim_factor, zero_phase=True).astype(np.float64)
            sample_rate = sample_rate // decim_factor
        except Exception:
            pass  # fall back to full-rate on any error

    nyquist = sample_rate / 2.0
    times = np.arange(len(ir)) / sample_rate
    half_step = 0.5 / bands_per_octave

    # Broadband RMS for peak_db reference
    broadband_rms = float(np.sqrt(np.mean(ir ** 2))) + 1e-30

    # Generate 1/N-octave center frequencies
    center_freqs: list[float] = []
    f = float(freq_min)
    while f <= freq_max * 1.001:
        center_freqs.append(f)
        f *= 2 ** (1.0 / bands_per_octave)

    # Pass 1: filter all bands, collect (fc, filtered_signal, band_rms) tuples.
    # We need all band RMS values before computing peak_db so we can normalise
    # each band against the mean-band RMS rather than broadband RMS.
    # Broadband-relative peak_db fails for dominant single-mode IRs (all energy
    # at one frequency → band_rms ≈ broadband_rms → peak_db ≈ 0 dB).
    band_results: list[tuple[float, np.ndarray, float]] = []

    for fc in center_freqs:
        f_low = fc * 2 ** (-half_step)
        f_high = fc * 2 ** half_step

        if f_low < 1.0 or f_high >= nyquist * 0.95:
            continue

        try:
            sos = butter(4, [f_low / nyquist, f_high / nyquist],
                         btype='bandpass', output='sos')
            filtered = sosfiltfilt(sos, ir)
        except Exception:
            continue

        if np.max(np.abs(filtered)) < 1e-20:
            continue

        band_rms = float(np.sqrt(np.mean(filtered ** 2))) + 1e-30
        band_results.append((fc, filtered, band_rms))

    if not band_results:
        return []

    # Mean band RMS as reference: a real mode band will be significantly above
    # the mean; filter ringing artifacts cluster near the mean (all bands similar).
    mean_band_rms = float(np.mean([r[2] for r in band_results])) + 1e-30

    modes: list[DecayMode] = []

    for fc, filtered, band_rms in band_results:
        peak_db = 20.0 * np.log10(band_rms / mean_band_rms)

        # Reject bands far below the mean — guards against pure noise bands.
        # Default -6 dB is permissive: sub sweeps energize the entire passband
        # uniformly so modes don't stand out >3 dB above the mean.  T60 is the
        # primary criterion; this gate only removes total-noise outliers.
        if peak_db < min_peak_db:
            continue

        # Direct envelope-based T20 estimation. Schroeder backward integration
        # is contaminated by the noise floor in the IR tail — for a 500ms IR
        # at -45 dB SNR it inflates T60 by 4-15× vs ground truth (validated
        # against session 262, 2026-05-25). See _estimate_t60_envelope.
        t60_ms = _estimate_t60_envelope(filtered, sample_rate)
        if t60_ms is None or t60_ms < t60_threshold_ms:
            continue

        suggested_q = _t60_to_q(t60_ms)

        modes.append(DecayMode(
            freq_hz=round(fc, 1),
            t60_ms=round(float(t60_ms), 1),
            peak_db=round(float(peak_db), 1),
            suggested_q=round(float(suggested_q), 2),
            priority=0,
        ))

    modes.sort(key=lambda m: m.t60_ms * abs(m.peak_db), reverse=True)
    for rank, mode in enumerate(modes, 1):
        mode.priority = rank
    return modes


def _estimate_t60_envelope(
    filtered: "np.ndarray",
    sample_rate: int,
    noise_floor_margin_db: float = 0.0,
) -> float | None:
    """Direct envelope-based T60 estimation from a bandpassed IR.

    Replaces Schroeder backward integration for the bandpass path. Schroeder
    integrates the IR tail as if it were modal energy — for a 500 ms IR at
    -45 dB SNR, the noise integral inflates apparent T60 by 4-15× (validated
    against session 262, 2026-05-25: 47 Hz read 1905 ms vs manual T20×3 of
    117-308 ms).

    Algorithm:
    1. Hilbert envelope of the bandpassed signal.
    2. Convert to dB rel peak (peak found within first ~10% of IR).
    3. Estimate noise floor: median of last 10% of envelope, in dB rel peak.
    4. Reject bands where the -25 dB threshold isn't at least
       ``noise_floor_margin_db`` above the noise floor.
    5. Measure time-to-first-cross of -5 dB and -25 dB post-peak.
    6. T60 = (t_minus25 - t_minus5) × 3.

    Returns T60 in ms, or None if any sanity gate fails (indeterminate —
    do NOT extrapolate past the end of the IR).
    """
    import numpy as np
    from scipy.signal import hilbert

    if len(filtered) < 64:
        return None

    envelope = np.abs(hilbert(filtered))
    if not np.any(envelope > 0):
        return None

    # Confine peak search to first ~10% of IR — modes ring out immediately
    # after the impulse; a late peak would be noise or a wraparound artifact.
    search_end = max(int(len(envelope) * 0.10), 32)
    peak_idx = int(np.argmax(envelope[:search_end]))
    peak_val = float(envelope[peak_idx])
    if peak_val <= 0:
        return None

    env_db = 20.0 * np.log10(envelope / peak_val + 1e-30)

    post_peak = env_db[peak_idx:]
    below_5 = np.where(post_peak <= -5.0)[0]
    below_25 = np.where(post_peak <= -25.0)[0]
    if len(below_5) == 0 or len(below_25) == 0:
        return None  # envelope never decays enough — indeterminate

    t5_idx = int(below_5[0])
    t25_idx = int(below_25[0])
    if t25_idx <= t5_idx:
        return None

    # Noise-floor sanity gate. After the -25 dB crossing, the envelope should
    # KEEP descending (signal still decaying) or plateau at noise floor that
    # is itself below -25 dB. If the envelope rebounds substantially above
    # -25 dB after the crossing, the -25 dB crossing was a noise excursion,
    # not real signal decay.
    abs_t25_idx = peak_idx + t25_idx
    if abs_t25_idx < len(env_db) - 64:
        # Take a stable window starting after the crossing. Use median so a
        # single noise spike doesn't mask a true plateau.
        post_tail = env_db[abs_t25_idx:]
        tail_median_db = float(np.median(post_tail))
        # If the median post-crossing envelope sits well ABOVE -25 dB, the
        # crossing was noise modulation — reject.
        if tail_median_db > -25.0 + noise_floor_margin_db:
            return None

    t20_s = (t25_idx - t5_idx) / sample_rate
    return float(t20_s * 3.0 * 1000.0)


def _estimate_t60(schroeder_db: "np.ndarray", times: "np.ndarray") -> float | None:
    """Estimate T60 from Schroeder decay curve via linear regression.

    Used by the spectrogram path. Includes two sanity gates to prevent the
    noise-floor inflation that caused 4-15× T60 over-estimation on 500 ms IRs
    (validated against session 262, 2026-05-25):

    1. Noise-floor gate: estimate noise floor from the last 10% of the
       Schroeder tail; stop fitting 3 dB above it so the regression doesn't
       run into the noise-dominated region.
    2. IR-window cap: if the extrapolated T60 exceeds 1.5× the observable IR
       duration, the decay was not completed within the measurement window —
       return None (indeterminate) instead of extrapolating.

    Returns T60 in milliseconds, or None if any gate fails.
    """
    import numpy as np

    if len(times) < 3:
        return None

    ir_duration_ms = float(times[-1]) * 1000.0

    # Gate 1: noise-floor-aware fit ceiling.
    n_tail = max(1, len(schroeder_db) // 10)
    noise_floor_db = float(np.median(schroeder_db[-n_tail:]))
    fit_lower = max(noise_floor_db + 3.0, -35.0)

    mask = (schroeder_db >= fit_lower) & (schroeder_db <= -5.0)
    if np.sum(mask) < 3:
        return None

    t_fit = times[mask]
    db_fit = schroeder_db[mask]

    try:
        coeffs = np.polyfit(t_fit, db_fit, 1)
        slope = coeffs[0]
        if slope >= 0:
            return None
        t60_ms = (-60.0 / slope) * 1000.0

        # Gate 2: can't reliably observe T60 > 1.5× the IR window.
        if t60_ms > ir_duration_ms * 1.5:
            return None

        return t60_ms

    except (np.linalg.LinAlgError, ValueError):
        return None


def _t60_to_q(t60_ms: float) -> float:
    """Map T60 to suggested Q value for EQ targeting.

    Longer decay → narrower Q (higher Q value) for more precise targeting.
    Range: 1.0 (very short decay, broad) to 10.0 (very long decay, surgical).
    """
    import numpy as np
    # Log-linear mapping: 300ms → Q=1.0, 3000ms → Q=10.0
    q = 1.0 + 9.0 * np.clip(
        (np.log10(t60_ms) - np.log10(300)) / (np.log10(3000) - np.log10(300)),
        0.0, 1.0,
    )
    return float(q)


def compare_decay(
    before_ir: list[float],
    after_ir: list[float],
    sample_rate: int = 48000,
    freq_min: float = 20.0,
    freq_max: float = 200.0,
) -> list[dict]:
    """Compare decay before and after correction.

    Runs analyze_decay on both IRs and matches modes by frequency (within 1/6 octave).
    Returns per-mode T60 comparison showing improvement.

    Args:
        before_ir: impulse response before correction
        after_ir: impulse response after correction
        sample_rate: sample rate in Hz

    Returns:
        List of dicts: [{freq_hz, t60_before_ms, t60_after_ms, reduction_pct}]
        sorted by reduction_pct descending (biggest improvements first).
    """
    import numpy as np

    before_modes = analyze_decay(before_ir, sample_rate, t60_threshold_ms=0.0,
                                  freq_min=freq_min, freq_max=freq_max)
    after_modes = analyze_decay(after_ir, sample_rate, t60_threshold_ms=0.0,
                                 freq_min=freq_min, freq_max=freq_max)

    comparisons: list[dict] = []

    for bm in before_modes:
        # Find matching after-mode within 1/6 octave
        match = None
        for am in after_modes:
            ratio = am.freq_hz / bm.freq_hz
            if 2 ** (-1/12) <= ratio <= 2 ** (1/12):  # within 1/6 octave
                match = am
                break

        t60_after = match.t60_ms if match else 0.0
        reduction = ((bm.t60_ms - t60_after) / bm.t60_ms * 100.0) if bm.t60_ms > 0 else 0.0

        comparisons.append({
            "freq_hz": bm.freq_hz,
            "t60_before_ms": bm.t60_ms,
            "t60_after_ms": round(t60_after, 1),
            "reduction_pct": round(reduction, 1),
        })

    comparisons.sort(key=lambda c: c["reduction_pct"], reverse=True)
    return comparisons
