"""Multi-input FIR optimizer for coherent multi-sub summation at MLP.

Given N solo measurements at the same mic position and a desired combined
target curve, design N FIRs such that when their outputs sum acoustically
at the mic, the result approximates the target.

Algorithm (regularized Wiener inverse, per-sub allocation):

For each frequency bin f:
    H_i(f) = complex measured response of sub i  (mag·exp(j·phase))
    T(f)   = complex target response (mag from curve, phase from min-phase
             reconstruction of |T|)

    Per-sub allocation: T_i(f) = T(f) / N
    Filter:             K_i(f) = T_i · conj(H_i) / (|H_i|² + λ²)

Key property: K_i·H_i = T_i · |H_i|² / (|H_i|² + λ²)

When |H_i| >> λ:  K_i·H_i ≈ T_i  (full contribution)
When |H_i| ≈ λ:   K_i·H_i ≈ T_i/2  (regularization kicks in)
When |H_i| << λ:  K_i·H_i ≈ 0  (deep null — don't fight it)

All N contributions land at the same phase (the target phase), so they
sum coherently. The regularization prevents inverting deep nulls.

Phase modes:
- "linear":   IFFT directly, center the impulse. Symmetric, ½·N latency.
- "mixed":    Min-phase magnitude + bounded-window excess phase.
              Configurable pre-ringing ≤ ~25 ms. Latency ≈ pre_ringing.
- "minimum":  Drop phase info, just match magnitude per-sub independently.
              Gives no inter-sub coherence improvement vs single-input
              design_fir — included for completeness.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass
class SubMeasurement:
    """One sub's solo measurement at the mic. All arrays same length."""
    freqs: list[float]       # Hz, monotonically increasing
    spl_db: list[float]      # measured magnitude in dB
    phase_rad: list[float]   # measured phase in radians
    label: str = ""          # human-readable name


def _min_phase_from_magnitude(mag_db, np_):
    """Reconstruct minimum-phase response from log magnitude.

    Uses the Hilbert transform of log|H(f)| to compute minimum phase.
    Returns phase array same length as input.
    """
    log_mag = mag_db * (math.log(10) / 20.0)  # dB → ln
    # Construct symmetric log-magnitude for Hilbert (rfft form)
    n = len(log_mag)
    # Build full FFT spectrum (mirror)
    full = np_.concatenate([log_mag, log_mag[-2:0:-1]])
    cep = np_.fft.ifft(full).real
    # Fold: keep n_full/2 of cepstrum centered, double positive lags
    n_full = len(cep)
    h = np_.zeros(n_full)
    h[0] = cep[0]
    h[1:n_full // 2] = 2 * cep[1:n_full // 2]
    h[n_full // 2] = cep[n_full // 2]
    # Min-phase = imag part of FFT of folded cepstrum (analytical signal)
    log_h_min = np_.fft.fft(h)
    min_phase_full = log_h_min.imag
    return min_phase_full[:n]


def design_multi_input_fir(
    measurements: Sequence[SubMeasurement],
    target_points: Sequence[tuple[float, float]],
    *,
    sample_rate: int = 48000,
    num_taps: int = 4096,
    phase_mode: str = "mixed",
    preringing_ms: float = 20.0,
    regularization_lambda: float = 0.1,
    freq_focus_hz: tuple[float, float] | None = None,
) -> dict:
    """Design FIRs for N subs to achieve coherent target sum at MLP.

    Returns dict with:
        firs:                list[list[float]]  — one FIR per sub, peak-normalized
        predicted_per_sub:   list of 1/3-octave band SPL each sub contributes
        predicted_combined:  1/3-octave band SPL of coherent sum
        per_sub_peak_boost:  max dB boost per sub (for safety check by caller)
        latency_ms:          effective latency of the design
    """
    import numpy as np

    n_subs = len(measurements)
    if n_subs < 2:
        raise ValueError("multi-input FIR requires ≥ 2 measurements")
    if phase_mode not in ("minimum", "linear", "mixed"):
        raise ValueError(f"phase_mode must be minimum/linear/mixed, got {phase_mode!r}")

    n_fft = max(num_taps * 4, 16384)
    freqs_out = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)

    # Interpolate each measurement onto the FFT grid
    H_complex = []
    for m in measurements:
        meas_f = np.array(m.freqs)
        meas_mag = np.array(m.spl_db)
        meas_ph = np.array(m.phase_rad)
        # Log-linear magnitude in dB → linear amplitude
        mag_lin = 10 ** (meas_mag / 20.0)
        # Interpolate magnitude in dB then convert, to preserve features
        mag_db_full = np.interp(freqs_out, meas_f, meas_mag,
                                left=meas_mag[0], right=meas_mag[-1])
        mag_lin_full = 10 ** (mag_db_full / 20.0)
        # Interpolate phase (unwrap first to avoid 2π jumps)
        ph_unwrapped = np.unwrap(meas_ph)
        ph_full = np.interp(freqs_out, meas_f, ph_unwrapped,
                            left=ph_unwrapped[0], right=ph_unwrapped[-1])
        H_complex.append(mag_lin_full * np.exp(1j * ph_full))

    # Build target magnitude on FFT grid
    tgt_f = np.array([p[0] for p in target_points])
    tgt_spl = np.array([p[1] for p in target_points])
    tgt_mag_db = np.interp(freqs_out, tgt_f, tgt_spl,
                           left=tgt_spl[0], right=tgt_spl[-1])
    T_mag = 10 ** (tgt_mag_db / 20.0)

    # Target phase: minimum-phase reconstruction of |T|
    # This is the phase that all subs should land at when summed.
    tgt_phase = _min_phase_from_magnitude(tgt_mag_db, np)
    T_complex = T_mag * np.exp(1j * tgt_phase)

    # Apply focus mask
    if freq_focus_hz:
        lo, hi = freq_focus_hz
        in_band = (freqs_out >= lo) & (freqs_out <= hi)
    else:
        in_band = np.ones_like(freqs_out, dtype=bool)

    # Per-sub allocation
    T_per_sub = T_complex / n_subs

    # Compute K_i for each sub
    fir_list: list[np.ndarray] = []
    contribution_db_list: list[np.ndarray] = []
    for H_i in H_complex:
        H_mag_sq = np.abs(H_i) ** 2
        # Regularized Wiener inverse — note this is COMPLEX
        # K_i = T_per_sub · conj(H_i) / (|H_i|² + λ²)
        # When applied: K_i · H_i = T_per_sub · |H_i|² / (|H_i|² + λ²)
        # The phase of K_i·H_i = phase of T_per_sub (= target phase).
        K_i = T_per_sub * np.conj(H_i) / (H_mag_sq + regularization_lambda ** 2)

        # Outside focus band: unity passthrough (no correction applied).
        K_i_full = np.where(in_band, K_i, 1.0 + 0j)

        # Track per-sub acoustic contribution magnitude for predicted output
        contribution_at_mic = K_i_full * H_i
        contribution_db_list.append(
            20 * np.log10(np.abs(contribution_at_mic) + 1e-12)
        )

        # ── Time-domain conversion per phase_mode ──
        # The complex K_i has the phase response that delivers coherent sum
        # at MLP. To preserve that coherence, we MUST keep the complex K_i
        # phase in the realized FIR. The challenge: K_i may be non-causal
        # (require time advance) for subs that arrive earlier than the target
        # phase. We add a constant time delay to make every K_i causal,
        # then window/truncate.

        if phase_mode == "minimum":
            # Discard K_i's phase entirely; build min-phase FIR from |K_i|.
            # This loses inter-sub coherence — included as a baseline
            # equivalent to running design_fir per-sub independently.
            mag_db = 20 * np.log10(np.abs(K_i_full) + 1e-12)
            mp_phase = _min_phase_from_magnitude(mag_db, np)
            K_min = np.abs(K_i_full) * np.exp(1j * mp_phase)
            k_td = np.fft.irfft(K_min, n=n_fft)[:num_taps]
        else:
            # linear or mixed: preserve K_i's complex phase, add bulk delay
            # to make it causal, window in time domain.
            if phase_mode == "linear":
                # Place impulse at the midpoint of the FIR window.
                # Maximum allowable pre-ringing = num_taps/2 samples.
                bulk_delay_samples = num_taps // 2
            else:
                # mixed: limit pre-ringing to preringing_ms.
                bulk_delay_samples = int(preringing_ms / 1000 * sample_rate)

            # Apply linear-phase delay to K_i (shift impulse forward in time).
            phase_shift = -2 * math.pi * freqs_out * bulk_delay_samples / sample_rate
            K_shifted = K_i_full * np.exp(1j * phase_shift)

            # IFFT to time domain
            k_full = np.fft.irfft(K_shifted, n=n_fft)

            # Extract num_taps samples centered on bulk_delay_samples
            # (where the impulse should now be).
            half = num_taps // 2
            start = bulk_delay_samples - half
            end = start + num_taps
            # Clamp to FFT bounds
            if start < 0:
                k_td = np.concatenate([
                    np.zeros(-start),
                    k_full[0:end],
                ])
            elif end > n_fft:
                k_td = np.concatenate([
                    k_full[start:n_fft],
                    np.zeros(end - n_fft),
                ])
            else:
                k_td = k_full[start:end]

            # Apply a soft window to suppress edge truncation ringing.
            # Tukey window (cosine taper) preserves the central impulse
            # better than full Hanning, which would attenuate the peak.
            try:
                from scipy.signal.windows import tukey
                window = tukey(num_taps, alpha=0.25)
            except Exception:
                # Fall back to a simple cosine taper on edges only
                taper_len = max(1, num_taps // 8)
                window = np.ones(num_taps)
                edge = 0.5 * (1 - np.cos(np.pi * np.arange(taper_len) / taper_len))
                window[:taper_len] = edge
                window[-taper_len:] = edge[::-1]
            k_td = k_td * window

        fir_list.append(k_td)

    # Unified peak normalization across ALL FIRs.
    # CamillaDSP requires each FIR's peak coefficient ≤ 1.0, but per-sub
    # normalization would scale each FIR independently — breaking the
    # relative magnitudes that produce coherent sum at MLP. Use a single
    # scale factor across all subs so the inter-sub relationship is
    # preserved; the caller can compensate with master gain if needed.
    global_peak = max(float(np.max(np.abs(fir))) for fir in fir_list)
    if global_peak > 0:
        fir_list = [fir / global_peak for fir in fir_list]
    output_gain_db = round(-20 * math.log10(global_peak) if global_peak > 0 else 0.0, 2)

    # Predicted combined response = sum of K_actual_i · H_i across freq bins,
    # computed from the post-normalization, post-truncation FIRs so the
    # prediction reflects what will actually be loaded into CamillaDSP
    # (not the idealized K we computed pre-realization).
    H_combined = np.zeros_like(H_complex[0])
    for fir_td, H_i in zip(fir_list, H_complex):
        # FIR's frequency response
        fir_full = np.zeros(n_fft)
        fir_full[:len(fir_td)] = fir_td
        K_actual = np.fft.rfft(fir_full)
        H_combined += K_actual * H_i
    combined_db = 20 * np.log10(np.abs(H_combined) + 1e-12)

    # Downsample to 1/3-octave for compact response
    third_oct_centres = [
        20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500,
        630, 800, 1000, 1250, 1600, 2000,
    ]
    factor_third = 2 ** (1 / 6)

    def _bandavg(values_db):
        bands = []
        for centre in third_oct_centres:
            lo = centre / factor_third
            hi = centre * factor_third
            mask = (freqs_out >= lo) & (freqs_out < hi)
            if np.any(mask):
                avg = float(np.mean(values_db[mask]))
                bands.append({"freq_hz": centre, "spl_db": round(avg, 2)})
        return bands

    predicted_combined = _bandavg(combined_db)

    # Per-sub contribution at mic (post-normalization)
    predicted_per_sub = []
    per_sub_peak_boost = []
    for i, (fir_td, H_i) in enumerate(zip(fir_list, H_complex)):
        fir_full = np.zeros(n_fft)
        fir_full[:len(fir_td)] = fir_td
        K_actual = np.fft.rfft(fir_full)
        contrib = K_actual * H_i
        contrib_db = 20 * np.log10(np.abs(contrib) + 1e-12)
        fir_db = 20 * np.log10(np.abs(K_actual) + 1e-12)
        meas_db_full = np.interp(freqs_out,
                                 np.array(measurements[i].freqs),
                                 np.array(measurements[i].spl_db),
                                 left=measurements[i].spl_db[0],
                                 right=measurements[i].spl_db[-1])
        fir_only_effect_db = contrib_db - meas_db_full  # what the FIR adds
        predicted_per_sub.append({
            "label": measurements[i].label or f"sub_{i}",
            "bands": _bandavg(contrib_db),
            "fir_effect_bands": _bandavg(fir_only_effect_db),
        })
        # Peak boost in the focus band, for safety check
        if freq_focus_hz:
            lo, hi = freq_focus_hz
            band_mask = (freqs_out >= lo) & (freqs_out <= hi)
        else:
            band_mask = np.ones_like(freqs_out, dtype=bool)
        per_sub_peak_boost.append(round(float(np.max(fir_only_effect_db[band_mask])), 2))

    # Latency: position of peak in the design (the "effective delay")
    latency_samples = max(int(np.argmax(np.abs(f))) for f in fir_list)
    latency_ms = round(latency_samples / sample_rate * 1000, 2)

    return {
        "firs": [fir.tolist() for fir in fir_list],
        "num_subs": n_subs,
        "num_taps": num_taps,
        "sample_rate": sample_rate,
        "phase_mode": phase_mode,
        "regularization_lambda": regularization_lambda,
        "predicted_combined": predicted_combined,
        "predicted_per_sub": predicted_per_sub,
        "per_sub_peak_boost_db": per_sub_peak_boost,
        "output_gain_db": output_gain_db,
        "latency_ms": latency_ms,
    }
