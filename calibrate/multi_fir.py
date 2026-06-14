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


def _build_anti_pulse_fir(
    modal_intents: list[dict],
    sample_rate: int,
    n_taps: int,
    gabor_n_cycles: int = 1,
) -> "np.ndarray":
    """Build a shared anti-pulse FIR: [anti-pulses ... main-delta ... zeros].

    Each intent dict: {freq_hz, treatment, cancel_strength?, bp_q?, peak_db?}
    Only 'anti_pulse' treatment entries are processed.

    Returns n_taps coefficients with the main delta at pre_samples and
    anti-pulses placed half-cycle earlier. Convolving any per-sub FIR with
    this produces a combined FIR that corrects both magnitude and modal T60.
    """
    import numpy as np
    from calibrate.modal_fir import design_anti_pulse

    anti_intents = [i for i in modal_intents if i.get("treatment") == "anti_pulse"]
    if not anti_intents:
        fir = np.zeros(n_taps, dtype=np.float64)
        fir[0] = 1.0
        return fir

    # Pre-ring budget: must be >= T/2 + Gabor_half for the lowest-freq mode.
    # With n_cycles=1 this is exactly T, so pre_samples >= T.
    min_needed_ms = max(
        (0.5 + 0.5 * gabor_n_cycles) * 1000.0 / float(i["freq_hz"])
        for i in anti_intents
    )
    pre_samples = min(int(math.ceil(min_needed_ms * sample_rate / 1000)), n_taps - 1)

    fir = np.zeros(n_taps, dtype=np.float64)

    for intent in anti_intents:
        freq = float(intent["freq_hz"])
        cancel_strength = float(intent.get("cancel_strength", 0.5))
        bp_q = float(intent.get("bp_q", 1.5))
        peak_db = float(intent.get("peak_db", 6.0))

        anti = design_anti_pulse(
            freq_hz=freq,
            peak_db=peak_db,
            cancel_strength=cancel_strength,
            sample_rate=sample_rate,
            bp_q=bp_q,
            envelope="gabor",
            n_cycles=gabor_n_cycles,
        )
        half_cycle = int(0.5 * sample_rate / freq)
        anti_center = pre_samples - half_cycle
        start_unclamped = anti_center - len(anti) // 2
        anti_offset = max(0, -start_unclamped)
        start = max(0, start_unclamped)
        end = min(start + len(anti) - anti_offset, pre_samples)
        segment = anti[anti_offset: anti_offset + (end - start)]
        fir[start:end] += segment

    # Main impulse (delta) at pre_samples
    fir[pre_samples] += 1.0
    return fir


def _self_cancellation_margin(h_combined, incoherent_mag, freqs_out, freq_focus_hz):
    """Worst (most negative) coherent-vs-incoherent summation margin, in dB.

    ``h_combined`` is the complex coherent acoustic sum Σ K_i·H_i at the mic;
    ``incoherent_mag`` is Σ|K_i·H_i|. Their ratio in dB is ≤ 0: it is 0 dB when
    every sub's realised contribution lands at the SAME phase (ideal coherent
    summation) and goes deeply negative where the realised FIRs CANCEL at the
    mic — the mixed-phase truncation notch (a non-causal Wiener inverse whose
    anti-causal tail was truncated to the pre-ring window). Restricted to the
    focus band. This is the metric that detects a self-cancelling design before
    it ships; ~0 dB means the multi-sub FIR sums coherently as intended.
    """
    import numpy as np
    margin = (20.0 * np.log10(np.abs(h_combined) + 1e-12)
              - 20.0 * np.log10(incoherent_mag + 1e-12))
    if freq_focus_hz:
        mask = (freqs_out >= freq_focus_hz[0]) & (freqs_out <= freq_focus_hz[1])
    else:
        mask = np.ones_like(freqs_out, dtype=bool)
    if not np.any(mask):
        mask = np.ones_like(freqs_out, dtype=bool)
    return round(float(np.min(margin[mask])), 2)


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
    max_correction_db: float | None = None,
    modal_intents: list[dict] | None = None,
    modal_taps: int | None = None,
    gabor_n_cycles: int = 1,
) -> dict:
    """Design FIRs for N subs to achieve coherent target sum at MLP.

    Optionally includes modal anti-pulse correction: if modal_intents is
    provided, a shared anti-pulse FIR is built (one half-wavelength before
    the main impulse per mode) and convolved with each per-sub FIR.  The
    total tap budget is split as: magnitude_taps = num_taps - modal_taps + 1,
    modal_taps = modal_taps (default auto-sized from mode frequencies).

    Returns dict with:
        firs:                list[list[float]]  — one FIR per sub, peak-normalized
        predicted_per_sub:   list of 1/3-octave band SPL each sub contributes
        predicted_combined:  1/3-octave band SPL of coherent sum
        per_sub_peak_boost:  max dB boost per sub (for safety check by caller)
        latency_ms:          effective latency of the design
        modal_pre_delay_ms:  pre-ring from anti-pulses (0 if no modal_intents)
    """
    import numpy as np

    n_subs = len(measurements)
    if n_subs < 1:
        raise ValueError("multi-input FIR requires ≥ 1 measurement")
    if phase_mode not in ("minimum", "linear", "mixed"):
        raise ValueError(f"phase_mode must be minimum/linear/mixed, got {phase_mode!r}")

    # Modal anti-pulse + Wiener FIR combination — ARCHITECTURAL LIMITATION.
    #
    # A Gabor anti-pulse has Fourier energy across its entire bandwidth
    # (~±15 Hz for Q=1.5 at 23 Hz). Convolving it with the Wiener FIR
    # produces a combined FIR whose frequency response is DOMINATED by the
    # anti-pulse's large Fourier components (+50 dB at the mode bandwidth),
    # completely swamping the Wiener magnitude correction. The result is a
    # FIR that AMPLIFIES 20-80 Hz by 30-50 dB instead of attenuating it.
    #
    # Anti-pulse correction is a TIME-DOMAIN technique: the Gabor must be
    # placed in the same FIR as the main impulse, T/2 before it, and the
    # correction only works because of destructive interference in the ROOM
    # at the mode frequency. It cannot be combined with a frequency-domain
    # Wiener filter via simple convolution.
    #
    # To apply modal T60 correction together with multi-sub magnitude
    # correction, use ModalAwareFIRDesigner with the combined per-sub
    # response as the base_correction — the anti-pulses are then placed in
    # the same time-domain FIR buffer, before the main impulse.
    if modal_intents and any(i.get("treatment") == "anti_pulse" for i in modal_intents):
        raise ValueError(
            "modal_intents with anti_pulse treatment cannot be combined with the "
            "Wiener multi-sub FIR via convolution — the anti-pulse's broad Fourier "
            "spectrum swamps the magnitude correction (+40-50 dB at target bands). "
            "Use ModalAwareFIRDesigner with the per-sub corrected response as "
            "base_correction to place anti-pulses in the same time-domain buffer "
            "as the main impulse."
        )

    modal_pre_delay_ms = 0.0
    _modal_fir_arr = None
    if modal_intents:
        anti_intents = [i for i in modal_intents if i.get("treatment") == "anti_pulse"]
        if anti_intents:
            if modal_taps is None:
                # Auto-size: need at least pre_samples + a few hundred samples tail.
                min_needed_ms = max(
                    (0.5 + 0.5 * gabor_n_cycles) * 1000.0 / float(i["freq_hz"])
                    for i in anti_intents
                )
                pre_s = int(math.ceil(min_needed_ms * sample_rate / 1000))
                modal_taps = min(max(pre_s + 512, 2048), num_taps // 2)
            mag_taps = num_taps - modal_taps + 1
            if mag_taps < 256:
                raise ValueError(
                    f"modal_taps={modal_taps} leaves only {mag_taps} taps for "
                    f"magnitude FIR (num_taps={num_taps}). Increase num_taps or reduce modal_taps."
                )
            _modal_fir_arr = _build_anti_pulse_fir(
                modal_intents, sample_rate, modal_taps, gabor_n_cycles
            )
            # Pre-delay from the anti-pulse FIR = position of the main delta
            anti_only = [i for i in modal_intents if i.get("treatment") == "anti_pulse"]
            _modal_pre_ms = max(
                (0.5 + 0.5 * gabor_n_cycles) * 1000.0 / float(i["freq_hz"])
                for i in anti_only
            )
            modal_pre_delay_ms = round(_modal_pre_ms, 2)
        else:
            mag_taps = num_taps
    else:
        mag_taps = num_taps

    n_fft = max(mag_taps * 4, 16384)
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

    # Normalize T to the average measured H at the reference frequency.
    # target_points are relative (e.g. Harman +5/+4/.../0 dB); the last point
    # is the 0 dB reference (80 Hz). Without normalization, T is in absolute
    # dBFS (0 dB = 1.0) while H is at the actual sweep measurement level
    # (typically -50 to -55 dBFS at calibrated gain). The Wiener would then
    # compute +50 dB boosts to reach 0 dBFS — rejected by SafetyValidator.
    # Anchoring T to avg(H) at the reference frequency makes corrections
    # relative: the FIR shapes the frequency response to match the target curve;
    # absolute level is set by master gain, not the FIR.
    ref_freq = float(tgt_f[-1])
    ref_idx = int(np.argmin(np.abs(freqs_out - ref_freq)))
    H_mag_at_ref = float(np.mean([np.abs(H_i[ref_idx]) for H_i in H_complex])) if H_complex else 1.0
    T_scale = max(H_mag_at_ref, 1e-30)
    tgt_mag_db = tgt_mag_db + 20.0 * np.log10(T_scale)

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

    # ── Phase 1: compute ideal K_i and pre-IFFT each one ──
    # We need a UNIFIED time-domain rotation across all subs so they
    # remain phase-coherent after realization. Per-sub rotation would
    # introduce per-sub linear phase shifts that break the coherent-sum
    # property we designed into the complex K_i.
    K_full_list = []
    k_full_list = []
    for H_i in H_complex:
        H_mag_sq = np.abs(H_i) ** 2
        K_i = T_per_sub * np.conj(H_i) / (H_mag_sq + regularization_lambda ** 2)
        # Optional correction clamp: bound the FIR's frequency-dependent gain to
        # ±max_correction_db around its in-band median (the broadband level it
        # applies), preserving the complex phase that delivers coherent summation.
        # Without this, the Wiener inverse aimed at a near-converged baseline with
        # +12–15 dB GEOMETRY modes (T60-dominated, not flat-magnitude excess)
        # produced 20–37 dB cuts that gut the band (fir-design-reviewer, run 36).
        # The clamp caps how hard the inverse drives without abandoning the
        # coherent-sum design. None = no clamp (legacy behavior).
        if max_correction_db is not None:
            _mag = np.abs(K_i)
            _band_mag = _mag[in_band]
            _band_mag = _band_mag[_band_mag > 0]
            if _band_mag.size:
                _ref = float(np.median(_band_mag))
                _lo = _ref * 10.0 ** (-abs(max_correction_db) / 20.0)
                _hi = _ref * 10.0 ** (abs(max_correction_db) / 20.0)
                _clamped = np.clip(_mag, _lo, _hi)
                K_i = K_i / (_mag + 1e-20) * _clamped
        # Outside the focus band: use passthrough (identity per sub, K = 1/n_subs)
        # rather than zero. Setting K=0 outside the band gives the minimum-phase
        # reconstruction of a bandpass, which creates steep high-pass/low-pass
        # roll-offs that apply huge unintended attenuation at adjacent frequencies.
        # Passthrough outside the band ensures the filter only corrects within
        # the focus band and is flat everywhere else.
        if freq_focus_hz:
            K_passthrough = (T_per_sub / (T_per_sub + 1e-30)).real * 0 + 1.0 / n_subs
            K_i_full = np.where(in_band, K_i, K_passthrough + 0j)
        else:
            K_i_full = K_i
        K_full_list.append(K_i_full)
        k_full_list.append(np.fft.irfft(K_i_full, n=n_fft))

    # Choose a single rotation amount: the maximum peak across all subs.
    # This guarantees every sub's impulse fits within the FIR window after
    # the same rotation, AND keeps the relative phase alignment intact.
    peak_indices = [int(np.argmax(np.abs(k))) for k in k_full_list]
    # If any peak is in the "wrap" region (high index = negative time),
    # interpret it as a negative shift; pick rotation that puts the
    # maximum-positive-time peak at the target center.
    target_center = n_fft // 2
    common_rotation = target_center - max(peak_indices)

    # ── Phase 2: realize each FIR via shared rotation, window, truncate ──
    fir_list: list[np.ndarray] = []
    contribution_db_list: list[np.ndarray] = []
    for K_i_full, k_full, H_i in zip(K_full_list, k_full_list, H_complex):
        # Track ideal per-sub acoustic contribution magnitude for predicted output
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
            k_td = np.fft.irfft(K_min, n=n_fft)[:mag_taps]
            # Taper the tail to prevent abrupt-truncation spectral artifacts.
            # Minimum-phase energy is front-loaded, so only taper the end.
            # Without this, FIRs for strongly-resonant rooms (T60 >> num_taps/sr)
            # develop deep (~30-40 dB) spectral notches at resonance frequencies.
            taper_len = max(1, mag_taps // 16)
            tail_window = np.ones(mag_taps)
            tail_edge = 0.5 * (1 - np.cos(np.pi * np.arange(taper_len) / taper_len))
            tail_window[-taper_len:] = tail_edge[::-1]
            k_td = k_td * tail_window
        else:
            # linear / mixed: preserve K_i's complex phase.
            # Apply the SAME rotation across all subs (common_rotation,
            # chosen above to put the largest peak at target_center). This
            # preserves the inter-sub phase relationship that the complex
            # K_i was designed to produce coherent summing at MLP.
            k_centered = np.roll(k_full, common_rotation)

            if phase_mode == "linear":
                half = mag_taps // 2
            else:
                # mixed: keep only preringing_ms before the peak, rest after
                pre_samples = int(preringing_ms / 1000 * sample_rate)
                half = min(pre_samples, mag_taps // 2)

            start = target_center - half
            end = start + mag_taps
            k_td = k_centered[start:end].copy()

            # Soft cosine taper at the edges only (preserve central impulse).
            # Short taper (1/32 of FIR length) to avoid attenuating the
            # impulse tails — those tails carry LOW-frequency response
            # information, so an aggressive taper kills the bottom octave.
            taper_len = max(1, mag_taps // 32)
            window = np.ones(mag_taps)
            edge = 0.5 * (1 - np.cos(np.pi * np.arange(taper_len) / taper_len))
            window[:taper_len] = edge
            window[-taper_len:] = edge[::-1]
            k_td = k_td * window

        fir_list.append(k_td)

    # ── Modal convolution: convolve each per-sub FIR with the shared anti-pulse FIR ──
    if _modal_fir_arr is not None:
        convolved = []
        for fir_td in fir_list:
            conv = np.convolve(fir_td, _modal_fir_arr)[:num_taps]
            if len(conv) < num_taps:
                conv = np.pad(conv, (0, num_taps - len(conv)))
            convolved.append(conv)
        fir_list = convolved

    # Conditional unified peak normalization.
    # CamillaDSP requires each coefficient |c| ≤ 1.0 — but only normalize
    # if peak EXCEEDS 1.0. Unconditional scaling (peak → 1) when peak < 1
    # inflates the gain to bizarre levels (in earlier iterations we saw
    # combined output at +50 dB above target because the bandpass impulse's
    # natural peak ~0.005 was being scaled up 200x). The natural magnitude
    # of K_i already gives the correct frequency response for coherent sum;
    # don't change it unless we have to.
    #
    # When normalizing, use a UNIFIED factor across all FIRs to preserve
    # inter-sub relative magnitudes (per-sub normalization would break the
    # coherent-sum math).
    global_peak = max(float(np.max(np.abs(fir))) for fir in fir_list)
    if global_peak > 1.0:
        fir_list = [fir / global_peak for fir in fir_list]
        output_gain_db = round(-20 * math.log10(global_peak), 2)
    else:
        output_gain_db = 0.0

    # Predicted combined response = sum of K_actual_i · H_i across freq bins,
    # computed from the post-normalization, post-truncation FIRs so the
    # prediction reflects what will actually be loaded into CamillaDSP
    # (not the idealized K we computed pre-realization).
    #
    # IMPORTANT: after modal convolution fir_list entries have num_taps taps
    # (not mag_taps), so the prediction FFT must be sized to num_taps, not
    # the pre-convolution mag_taps. Recompute freqs_out for the prediction loop.
    # Predicted combined response. Use the ORIGINAL n_fft / H_complex grid for
    # consistency: the combined FIR (up to num_taps ≤ n_fft) is zero-padded to
    # n_fft and evaluated at the same frequencies as H_complex. Using a different
    # pred_n_fft would cause the irfft/rfft normalisations to differ, giving 30-50 dB
    # amplitude errors in fir_effect_bands (the "inverse gain" bug).
    H_combined = np.zeros_like(H_complex[0])
    incoherent_mag = np.zeros_like(np.abs(H_complex[0]))
    for fir_td, H_i in zip(fir_list, H_complex):
        fir_full = np.zeros(n_fft)
        fir_full[:len(fir_td)] = fir_td
        K_actual = np.fft.rfft(fir_full)
        contrib = K_actual * H_i
        H_combined += contrib
        incoherent_mag += np.abs(contrib)
    combined_db = 20 * np.log10(np.abs(H_combined) + 1e-12)

    # ── Self-cancellation guard ──────────────────────────────────────────
    # The coherent acoustic sum |Σ K_i·H_i| compared against the incoherent
    # magnitude sum Σ|K_i·H_i|. The ratio (margin, in dB, ≤ 0) is 0 when every
    # sub's realised contribution lands at the SAME phase at the mic (perfect
    # coherent summation), and goes deeply negative where the realised FIRs
    # CANCEL at the mic. That cancellation is the mixed-phase truncation notch:
    # the complex Wiener inverse of an excess-phase response is non-causal, and
    # truncating its anti-causal tail to the pre-ring window leaves each sub at
    # a different residual phase. This metric DETECTS a self-cancelling design
    # before it is applied to a sub (see design_fir_trinnov / fir-design-reviewer).
    self_cancellation_margin_db = _self_cancellation_margin(
        H_combined, incoherent_mag, freqs_out, freq_focus_hz
    ) if n_subs >= 2 else 0.0

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
        "modal_pre_delay_ms": modal_pre_delay_ms,
        "self_cancellation_margin_db": self_cancellation_margin_db,
    }


def design_fir_multi_modal(
    measurements: Sequence[SubMeasurement],
    target_points: Sequence[tuple[float, float]],
    modal_intents: list[dict],
    *,
    sample_rate: int = 48000,
    num_taps: int = 24576,
    phase_mode: str = "minimum",
    regularization_lambda: float = 0.1,
    freq_focus_hz: tuple[float, float] | None = None,
    gabor_n_cycles: int = 1,
) -> dict:
    """Design per-sub FIRs combining Wiener magnitude correction with modal
    anti-pulse T60 correction — the correct Trinnov-style architecture.

    Architecture (anti-pulse BEFORE Wiener main impulse in same buffer):
      fir[0..pre_samples-1]   = Gabor anti-pulses (T60 cancellation)
      fir[pre_samples..]      = min-phase Wiener magnitude correction

    This is correct because anti-pulses are a TIME-DOMAIN technique: the
    pulse arrives T/2 before the room excitation and creates destructive
    interference at the mode frequency via the room's own T60 decay.
    Convolving them as separate FIRs breaks this (see block in design_fir_multi).

    The tap budget is split: mag_taps = num_taps - pre_samples, where
    pre_samples is auto-sized from the lowest-frequency anti_pulse mode.
    """
    import math
    import numpy as np
    from calibrate.modal_fir import ModalAwareFIRDesigner, ModeIntent

    # Validate modal_intents
    anti_intents_raw = [i for i in modal_intents if i.get("treatment") == "anti_pulse"]
    if not anti_intents_raw:
        # No anti-pulses — fall back to plain multi-sub Wiener design
        return design_multi_input_fir(
            measurements, target_points,
            sample_rate=sample_rate, num_taps=num_taps,
            phase_mode=phase_mode, regularization_lambda=regularization_lambda,
            freq_focus_hz=freq_focus_hz,
        )

    # Compute pre-ring budget from the lowest-frequency anti-pulse mode
    min_needed_ms = max(
        (0.5 + 0.5 * gabor_n_cycles) * 1000.0 / float(i["freq_hz"])
        for i in anti_intents_raw
    )
    pre_samples = math.ceil(min_needed_ms * sample_rate / 1000)
    mag_taps = num_taps - pre_samples
    if mag_taps < 512:
        raise ValueError(
            f"pre_samples={pre_samples} leaves only {mag_taps} taps for Wiener "
            f"magnitude correction (num_taps={num_taps}). Increase num_taps."
        )

    # Step 1 — design per-sub Wiener magnitude FIRs with reduced tap budget
    wiener = design_multi_input_fir(
        measurements, target_points,
        sample_rate=sample_rate, num_taps=mag_taps,
        phase_mode=phase_mode, regularization_lambda=regularization_lambda,
        freq_focus_hz=freq_focus_hz,
    )

    # Convert modal_intents dicts → ModeIntent objects
    mode_intents = [
        ModeIntent(
            freq_hz=float(d["freq_hz"]),
            t60_ms=float(d.get("t60_ms", 1000.0)),
            peak_db=float(d.get("peak_db", 6.0)),
            treatment=d["treatment"],
            cancel_strength=float(d.get("cancel_strength", 0.5)),
            bp_q=float(d.get("bp_q", 1.5)),
        )
        for d in modal_intents
    ]

    # Step 2 — for each sub, use ModalAwareFIRDesigner to place anti-pulses
    # BEFORE the Wiener FIR's main impulse in the same time-domain buffer.
    # ModalAwareFIRDesigner.design() converts base_correction to min-phase
    # and places it at fir[pre_samples:]; anti-pulses go at fir[0:pre_samples].
    combined_firs: list[list[float]] = []
    modal_notes: list[str] = []
    designer = ModalAwareFIRDesigner(
        sample_rate=sample_rate,
        n_taps=num_taps,
        max_pre_ring_ms=min_needed_ms * 1.05,
    )
    for wiener_taps in wiener["firs"]:
        fir_taps, summary = designer.design(
            decay_modes=[],          # explicit intents provided
            base_correction=wiener_taps,
            intents=mode_intents,
            gabor_n_cycles=gabor_n_cycles,
            # skip_freq_domain_norm: the Wiener FIR is already an attenuation
            # filter (gain < 1). The Gabor's Fourier integral (+52 dB at mode
            # freq) is a transient artifact — normalizing by it kills the
            # Wiener correction 400×. SafetyValidator's modal_cancel cap (60 dB
            # for SVS PB12-NSD) correctly permits transient Gabor content.
            skip_freq_domain_norm=True,
        )
        combined_firs.append(fir_taps)
        modal_notes.extend(summary.notes)

    return {
        "firs": combined_firs,
        "num_subs": wiener["num_subs"],
        "num_taps": num_taps,
        "sample_rate": sample_rate,
        "phase_mode": phase_mode,
        "regularization_lambda": regularization_lambda,
        "predicted_combined": wiener["predicted_combined"],
        "predicted_per_sub": wiener["predicted_per_sub"],
        "per_sub_peak_boost_db": wiener["per_sub_peak_boost_db"],
        "output_gain_db": wiener.get("output_gain_db", 0.0),
        "latency_ms": round(pre_samples / sample_rate * 1000, 2),
        "modal_pre_delay_ms": round(min_needed_ms, 2),
        "self_cancellation_margin_db": wiener.get("self_cancellation_margin_db", 0.0),
        "modal_notes": modal_notes,
        # FIRs contain Gabor anti-pulse pre-ring; apply_fir must use
        # intent="modal_cancel" so SafetyValidator uses the 60 dB cap.
        "apply_intent": "modal_cancel",
    }


# ---------------------------------------------------------------------------
# Trinnov-style coherent multi-sub correction
# ---------------------------------------------------------------------------

def design_fir_trinnov(
    room_ir: list[float],
    measurements: "Sequence[SubMeasurement]",
    target_points: "Sequence[tuple[float, float]]",
    *,
    sample_rate: int = 48000,
    num_taps: int = 24576,
    phase_mode: str = "mixed",
    regularization_lambda: float = 0.01,
    freq_focus_hz: tuple[float, float] | None = None,
    max_correction_db: float | None = None,
    preringing_ms: float = 20.0,
    freq_min: float = 20.0,
    freq_max: float = 120.0,
    bands_per_octave: int = 6,
    t60_threshold_ms: float = 300.0,
) -> dict:
    """Trinnov-style coherent multi-sub correction via the complex Wiener inverse.

    This is a thin wrapper over :func:`design_multi_input_fir` in ``mixed``
    phase mode, plus a baseline T60 report derived from the measured room IR.

    Why no separate "anti-ringing" section any more
    -----------------------------------------------
    Earlier revisions added a pre-causal section built from the time-reversed
    room ringing, intended to shorten modal decay.  That is mathematically
    unsound: the corrected response at the mic is ``C * h`` (the correction
    FIR convolved with the sub→mic path).  Time-reversed mode ringing is a
    *matched filter* for the mode — convolving it back through ``h`` re-excites
    the mode, boosting its steady-state level by tens of dB while leaving T60
    unchanged (verified empirically: +40 dB at the mode, T60 578→585 ms).

    The ONLY correction that reduces a room mode in both magnitude and decay
    is the regularised complex inverse, ``K_i = T_i·conj(H_i)/(|H_i|²+λ²)``,
    realised with its phase preserved (``mixed``/``linear``).  ``minimum`` mode
    inverts each sub's magnitude but discards the inter-sub phase, so subs with
    different arrival times cancel acoustically at the listener (deep nulls).
    ``mixed`` rotates every sub to a common target phase so they sum coherently
    — which is exactly the multi-sub property this design exists to deliver.

    Parameters
    ----------
    room_ir          : averaged impulse response from measure_impulse_ir().
                       Used ONLY for the informational baseline T60 report;
                       it does not shape the correction.
    measurements     : per-sub solo FR sessions (magnitude + phase) — the
                       inputs to the coherent Wiener design.
    target_points    : target magnitude curve [(freq_hz, spl_db), ...]
    phase_mode       : 'mixed' (default, coherent multi-sub), 'linear'
                       (symmetric, highest latency), or 'minimum' (per-sub
                       magnitude only — no inter-sub coherence; single-sub use).
    preringing_ms    : pre-ring budget for mixed phase (≈ latency). Default 20.
    freq_focus_hz    : optional (lo, hi) band to confine the correction to.
    freq_min/max     : frequency range for the baseline T60 report.
    bands_per_octave : filter-bank resolution for the baseline T60 report.
    t60_threshold_ms : modes with baseline T60 above this are listed in the
                       returned ``ringing_modes`` report (informational).
    """
    wiener = design_multi_input_fir(
        measurements, target_points,
        sample_rate=sample_rate, num_taps=num_taps,
        phase_mode=phase_mode, regularization_lambda=regularization_lambda,
        freq_focus_hz=freq_focus_hz, max_correction_db=max_correction_db,
        preringing_ms=preringing_ms,
    )

    # Informational baseline decay report from the measured room IR. This is a
    # report only — the correction is the coherent Wiener inverse above. We
    # reuse calibrate.decay.analyze_decay (the canonical Schroeder estimator)
    # rather than re-implementing band T60 here.
    ringing_modes: list[dict] = []
    try:
        from .decay import analyze_decay
        modes = analyze_decay(
            list(room_ir), sample_rate=sample_rate,
            t60_threshold_ms=t60_threshold_ms,
            freq_min=freq_min, freq_max=freq_max,
            bands_per_octave=bands_per_octave,
        )
        ringing_modes = [
            {
                "freq_hz": round(m.freq_hz, 1),
                "t60_ms": round(m.t60_ms),
                "peak_db": round(m.peak_db, 1),
            }
            for m in modes
        ]
    except Exception:
        # A bad/empty IR must never fail the design — the FIRs stand on their
        # own; the T60 report is best-effort.
        ringing_modes = []

    return {
        "firs": wiener["firs"],
        "num_subs": wiener["num_subs"],
        "num_taps": num_taps,
        "sample_rate": sample_rate,
        "phase_mode": phase_mode,
        "regularization_lambda": regularization_lambda,
        "predicted_combined": wiener["predicted_combined"],
        "predicted_per_sub": wiener["predicted_per_sub"],
        "per_sub_peak_boost_db": wiener["per_sub_peak_boost_db"],
        "output_gain_db": wiener.get("output_gain_db", 0.0),
        "latency_ms": wiener["latency_ms"],
        "self_cancellation_margin_db": wiener.get("self_cancellation_margin_db", 0.0),
        "ringing_modes": ringing_modes,
        "n_ringing_modes": len(ringing_modes),
        # Pure magnitude/phase correction — strict thermal cap applies, and the
        # FIR is safe to sweep through (no anti-pulse Gabor content).
        "apply_intent": "correction",
    }
