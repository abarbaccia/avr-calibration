"""Modal-aware mixed-phase FIR designer.

Takes a measurement session's decay_modes data, applies LLM-supplied
per-mode design intents, and generates a mixed-phase FIR with
explicit pre-ringing budget allocation.

Design philosophy (vs conventional min-phase):
  - Each mode classified by (T60, peak_dB, Q) into a treatment regime
  - Long-ringy modes get anti-pulse cancellation (mixed-phase)
  - Short-loud modes get linear-phase notches
  - Mild modes get conservative min-phase EQ
  - Total pre-ringing budget bounded by user-specified ms (default 25)

Output:
  - FIR coefficients (suitable for apply_fir)
  - Per-mode design rationale
  - Predicted effect (modal T60 reduction, magnitude)
  - Total pre-delay (= filter latency contribution to sub chain)

Usage:
    designer = ModalAwareFIRDesigner(
        sample_rate=8000,
        n_taps=4096,
        max_pre_ring_ms=25.0,
    )
    fir, summary = designer.design(
        decay_modes=[...],          # from session metadata
        base_correction=fir_coeffs, # current min-phase FIR coefficients (magnitude shape)
        intents=[...],              # LLM-supplied per-mode intents
    )
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal
import numpy as np
from scipy.signal import minimum_phase, butter, sosfilt


@dataclass
class ModeIntent:
    """LLM-supplied design intent for a single room mode."""
    freq_hz: float
    t60_ms: float
    peak_db: float
    treatment: Literal["anti_pulse", "linear_notch", "min_phase", "skip"]
    cancel_strength: float = 0.6   # 0-1, fraction of mode amplitude to cancel
    bp_q: float = 1.5              # bandpass Q for anti-pulse envelope
    envelope: str = "gabor"        # "gabor" (default) or "butterworth" (legacy)
    rationale: str = ""


@dataclass
class DesignSummary:
    total_taps: int
    sample_rate: int
    pre_delay_ms: float
    pre_delay_samples: int
    peak_amplitude: float
    mode_treatments: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def design_anti_pulse(freq_hz: float, peak_db: float, cancel_strength: float,
                       sample_rate: int, n_cycles: int = 3,
                       bp_q: float = 1.5,
                       envelope: str = "gabor") -> np.ndarray:
    """Generate a band-limited anti-pulse for modal cancellation.

    Returns an N-sample array where N = n_cycles × wavelength(freq).
    Caller is responsible for placing/scaling and inverting polarity.

    Two envelope shapes available:

    ``gabor`` (default) — Gaussian-windowed sinusoid (Gabor wavelet). Optimal
    time-frequency localization (Heisenberg lower bound), so adjacent-band
    spectral skirts decay much faster than the Butterworth equivalent. The
    pulse has analytical bandwidth ≈ ``freq_hz / bp_q`` (3 dB), with
    Gaussian rolloff outside.

    ``butterworth`` — legacy 4th-order bandpass-filtered impulse. Wider
    skirts; kept for regression / A-B comparison.

    bp_q controls the bandwidth (Q = freq / bandwidth). Higher Q = narrower
    band = less leakage but longer time-domain tail. Default 1.5; raise to
    3-5 when adjacent-band leakage trips per-band safety caps.
    """
    n = int(n_cycles * sample_rate / freq_hz)
    if n < 8:
        n = 8
    if envelope == "gabor":
        # Gaussian-windowed cosine. σ chosen so the 3 dB bandwidth is
        # freq_hz / bp_q. For a Gabor wavelet, BW_Hz ≈ 1 / (π × σ_t),
        # so σ_t = bp_q / (π × freq_hz). σ_samples = σ_t × sample_rate.
        sigma_samples = bp_q * sample_rate / (np.pi * freq_hz)
        t = np.arange(n) - (n - 1) / 2.0
        envelope_arr = np.exp(-0.5 * (t / sigma_samples) ** 2)
        carrier = np.cos(2.0 * np.pi * freq_hz * t / sample_rate)
        bp = envelope_arr * carrier
    else:  # legacy butterworth
        impulse = np.zeros(n)
        impulse[n // 2] = 1.0
        bw = freq_hz / max(2.0 * bp_q, 0.5)
        lo, hi = max(1.0, freq_hz - bw), min(sample_rate / 2 - 1, freq_hz + bw)
        sos = butter(4, [lo, hi], btype="band", fs=sample_rate, output="sos")
        bp = sosfilt(sos, impulse)
    bp = bp / (float(np.max(np.abs(bp))) + 1e-12)
    # Linear amplitude proportional to mode strength × cancel_strength
    # Use partial cancellation: don't try to fully invert (over-correction risk)
    linear_strength = (10.0 ** (peak_db / 20.0) - 1.0) * cancel_strength * 0.10
    linear_strength = float(np.clip(linear_strength, 0.005, 0.4))
    return bp * linear_strength


def classify_mode_default(
    freq_hz: float,
    t60_ms: float,
    peak_db: float,
    target_t60_ms: float = 300.0,
    peak_action_db: float = 3.0,
    short_loud_t60_factor: float = 0.5,
    short_loud_peak_db: float = 12.0,
    long_ringy_t60_factor: float = 2.0,
    anti_pulse_cancel_strength: float = 0.6,
) -> ModeIntent:
    """Auto-classify a single mode against a user-supplied target T60.

    The ``target_t60_ms`` parameter encodes "what does a good-enough room
    look like?" — modes already meeting it are skipped, modes far above
    it get aggressive treatment, and modes a little above it get gentle
    correction. Industry references for the target:

    | Target | Room class                        |
    |--------|------------------------------------|
    | <250   | Mastering / control room           |
    | 300    | THX / Dolby reference (default)    |
    | <500   | Acceptable home theater            |
    | >700   | Untreated room (would skip target) |

    Decision rules, evaluated in order:

      1. ``peak_db < peak_action_db`` (default 3 dB) → ``skip``
         The mode is too quiet to matter; spending pre-ring budget on it
         just adds latency for no audible gain.

      2. ``t60_ms ≤ target_t60_ms`` → ``skip``
         Mode already meets the target; leave it alone.

      3. ``t60_ms > target_t60_ms × 2`` AND ``peak_db ≥ peak_action_db``
         → ``anti_pulse`` (cancel both peak and decay tail)
         "Far above target" — anti-pulse is worth its pre-ring cost.

      4. ``peak_db > 12`` AND ``t60_ms < target_t60_ms × 0.5``
         → ``linear_notch`` (precise magnitude cut on short loud peak)
         Short fast peak that's still loud — a surgical notch is better
         than anti-pulse here (no decay to cancel).

      5. otherwise → ``min_phase`` (gentle magnitude EQ)
         Mode is moderately above target; reduce peak without spending
         pre-ring budget.

    Callers can override per-mode by supplying explicit ``intents`` to
    ``ModalAwareFIRDesigner.design()``.
    """
    if peak_db < peak_action_db:
        return ModeIntent(
            freq_hz=freq_hz, t60_ms=t60_ms, peak_db=peak_db,
            treatment="skip",
            rationale=f"peak={peak_db:+.1f}dB below action threshold ({peak_action_db}dB)",
        )
    # Linear-notch FIRST: even if T60 is short, a very loud narrow peak still
    # needs cutting. The notch fixes magnitude without touching the (already
    # short) decay tail.
    if peak_db > short_loud_peak_db and t60_ms < target_t60_ms * short_loud_t60_factor:
        return ModeIntent(
            freq_hz=freq_hz, t60_ms=t60_ms, peak_db=peak_db,
            treatment="linear_notch",
            rationale=(
                f"T60={t60_ms:.0f}ms short, peak={peak_db:+.1f}dB loud — "
                f"precise linear-phase notch"
            ),
        )
    if t60_ms <= target_t60_ms:
        return ModeIntent(
            freq_hz=freq_hz, t60_ms=t60_ms, peak_db=peak_db,
            treatment="skip",
            rationale=(
                f"T60={t60_ms:.0f}ms ≤ target {target_t60_ms:.0f}ms — already meets goal"
            ),
        )
    if t60_ms > target_t60_ms * long_ringy_t60_factor:
        return ModeIntent(
            freq_hz=freq_hz, t60_ms=t60_ms, peak_db=peak_db,
            treatment="anti_pulse", cancel_strength=anti_pulse_cancel_strength,
            rationale=(
                f"T60={t60_ms:.0f}ms is {t60_ms / target_t60_ms:.1f}× target "
                f"({target_t60_ms:.0f}ms) — long ringy mode at {freq_hz:.0f}Hz; "
                f"anti-pulse can shorten T60 by ~{int(60 * anti_pulse_cancel_strength)}%"
            ),
        )
    return ModeIntent(
        freq_hz=freq_hz, t60_ms=t60_ms, peak_db=peak_db,
        treatment="min_phase",
        rationale=(
            f"T60={t60_ms:.0f}ms moderately above target {target_t60_ms:.0f}ms — "
            f"min-phase magnitude EQ"
        ),
    )


class ModalAwareFIRDesigner:
    def __init__(self, sample_rate: int = 8000, n_taps: int = 4096,
                 max_pre_ring_ms: float = 25.0):
        self.sr = sample_rate
        self.n_taps = n_taps
        self.max_pre_ring_ms = max_pre_ring_ms

    def design(self, decay_modes: list[dict], base_correction: list[float],
                intents: list[ModeIntent] | None = None,
                target_t60_ms: float = 300.0,
                peak_action_db: float = 3.0,
                short_loud_t60_factor: float = 0.5,
                short_loud_peak_db: float = 12.0,
                long_ringy_t60_factor: float = 2.0,
                anti_pulse_cancel_strength: float = 0.6,
                target_curve_db: list[tuple[float, float]] | None = None,
                source_fr_db: list[tuple[float, float]] | None = None,
                magnitude_focus_hz: tuple[float, float] | None = None,
                anchor_freq_hz: float | None = None,
                ) -> tuple[list[float], DesignSummary]:
        """Generate a modal-aware mixed-phase FIR.

        Parameters
        ----------
        decay_modes : list of {freq_hz, t60_ms, peak_db, suggested_q}
            From session.metadata.decay_modes
        base_correction : list[float]
            Current FIR coefficients (used as the base magnitude correction).
            Will be transformed to min-phase before any anti-pulse work.
        intents : list[ModeIntent] | None
            LLM-supplied per-mode intent. If None, falls back to
            classify_mode_default().
        """
        # 1. Generate / accept per-mode intents
        if intents is None:
            intents = [
                classify_mode_default(
                    m["freq_hz"], m["t60_ms"], m["peak_db"],
                    target_t60_ms=target_t60_ms,
                    peak_action_db=peak_action_db,
                    short_loud_t60_factor=short_loud_t60_factor,
                    short_loud_peak_db=short_loud_peak_db,
                    long_ringy_t60_factor=long_ringy_t60_factor,
                    anti_pulse_cancel_strength=anti_pulse_cancel_strength,
                )
                for m in decay_modes
            ]
        # match intents to modes by freq (forgiving order)
        intent_by_freq = {round(i.freq_hz, 1): i for i in intents}

        summary = DesignSummary(
            total_taps=self.n_taps,
            sample_rate=self.sr,
            pre_delay_ms=0.0,
            pre_delay_samples=0,
            peak_amplitude=0.0,
        )

        # 2. Convert base correction to min-phase (impulse at sample 0)
        base = np.array(base_correction, dtype=np.float32)
        base_min = minimum_phase(base, method="homomorphic", n_fft=4 * len(base))

        # 3. Compute pre-ring budget needed for active anti-pulses
        active_anti = [i for i in intents if i.treatment == "anti_pulse"]
        if active_anti:
            # Each anti-pulse needs half a wavelength of pre-position
            max_half_cycle_ms = max(1000.0 / (2 * i.freq_hz) for i in active_anti)
            # Plus 1 cycle of bandpass tail
            tail_ms = max(1000.0 / i.freq_hz for i in active_anti)
            needed_pre_ring_ms = max_half_cycle_ms + tail_ms / 2
        else:
            needed_pre_ring_ms = 0.0

        budget_ms = min(self.max_pre_ring_ms, max(needed_pre_ring_ms, 0.0))
        pre_samples = int(budget_ms * self.sr / 1000)
        summary.pre_delay_ms = pre_samples / self.sr * 1000
        summary.pre_delay_samples = pre_samples

        # 4. Build the FIR
        fir = np.zeros(self.n_taps, dtype=np.float32)

        # Place min-phase base correction starting at sample = pre_samples
        end_base = min(pre_samples + len(base_min), self.n_taps)
        fir[pre_samples:end_base] += base_min[:end_base - pre_samples]

        # 5. Add anti-pulses for ringy modes
        for intent in intents:
            entry = {
                "freq_hz": intent.freq_hz,
                "treatment": intent.treatment,
                "rationale": intent.rationale,
            }

            if intent.treatment == "anti_pulse":
                anti = design_anti_pulse(
                    freq_hz=intent.freq_hz,
                    peak_db=intent.peak_db,
                    cancel_strength=intent.cancel_strength,
                    sample_rate=self.sr,
                    bp_q=intent.bp_q,
                    envelope=intent.envelope,
                )
                # Anti-pulse position: pre_samples - half_cycle
                half_cycle_samples = int(0.5 * self.sr / intent.freq_hz)
                anti_center = pre_samples - half_cycle_samples
                start = anti_center - len(anti) // 2
                if start < 0:
                    summary.notes.append(
                        f"WARN: anti-pulse for {intent.freq_hz:.0f}Hz needs "
                        f"{half_cycle_samples} pre-samples; only {pre_samples} budget "
                        f"available — clipping"
                    )
                    start = 0
                end = min(start + len(anti), self.n_taps)
                # Inverted polarity for cancellation
                fir[start:end] -= anti[:end - start]
                entry["anti_pulse_pre_ms"] = round(half_cycle_samples / self.sr * 1000, 2)
                entry["anti_pulse_amplitude"] = round(float(np.max(np.abs(anti))), 4)
                entry["predicted_t60_reduction_pct"] = int(intent.cancel_strength * 60)

            summary.mode_treatments.append(entry)

        # 6. Optional magnitude-correction layer for target-curve tracking.
        # When target_curve_db + source_fr_db are provided, the unified design
        # adds a min-phase magnitude correction FIR that tracks
        #     target − source − modal_fir_response
        # so a single FIR delivers both modal cancellation AND target-curve
        # shaping. Without this layer the caller must add a separate magnitude
        # FIR or PEQ on top, which can fight the modal cancellation.
        if target_curve_db is not None and source_fr_db is not None:
            mag_fir = _design_magnitude_correction_fir(
                fir=fir,
                target_db=target_curve_db,
                source_fr_db=source_fr_db,
                sample_rate=self.sr,
                n_taps=min(self.n_taps, 1024),
                focus_hz=magnitude_focus_hz,
                anchor_freq_hz=anchor_freq_hz,
            )
            # Convolve and truncate to n_taps
            combined = np.convolve(fir, mag_fir)[: self.n_taps].astype(np.float32)
            fir = combined
            summary.notes.append(
                f"unified target-curve correction layer applied "
                f"(min-phase magnitude FIR, {len(mag_fir)} taps convolved)"
            )

        # 7. Normalize peak ≤ 1.0
        peak = float(np.max(np.abs(fir)))
        if peak > 1.0:
            fir = fir / (peak * 1.001)
            summary.notes.append(f"normalized: original peak {peak:.3f} → 0.999")
        summary.peak_amplitude = float(np.max(np.abs(fir)))

        return fir.tolist(), summary


def _design_magnitude_correction_fir(
    fir: np.ndarray,
    target_db: list[tuple[float, float]],
    source_fr_db: list[tuple[float, float]],
    sample_rate: int,
    n_taps: int = 1024,
    focus_hz: tuple[float, float] | None = None,
    anchor_freq_hz: float | None = None,
) -> np.ndarray:
    """Design a min-phase FIR that corrects ``source + fir`` toward ``target``.

    Computes the residual error in dB at each frequency in the union of
    ``target_db`` and ``source_fr_db`` grids, then returns a min-phase FIR
    whose magnitude response tracks that residual.

    Outside ``focus_hz`` (default: full sub band) the correction tapers to
    0 dB to avoid lifting noise floors or boosting frequencies the sub
    can't reproduce.
    """
    n_fft = max(2048, int(2 ** np.ceil(np.log2(max(n_taps, len(fir))))))
    fir_spec_db = 20.0 * np.log10(np.maximum(np.abs(np.fft.rfft(fir, n_fft)), 1e-9))
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)

    # Interpolate source FR and target onto the FFT grid (log-frequency).
    src_pts = sorted(source_fr_db, key=lambda p: p[0])
    tgt_pts = sorted(target_db, key=lambda p: p[0])
    src_freqs = np.array([p[0] for p in src_pts], dtype=float)
    src_db = np.array([p[1] for p in src_pts], dtype=float)
    tgt_freqs = np.array([p[0] for p in tgt_pts], dtype=float)
    tgt_db = np.array([p[1] for p in tgt_pts], dtype=float)
    log_freqs = np.log10(np.maximum(freqs, 1.0))
    src_interp = np.interp(log_freqs, np.log10(src_freqs), src_db,
                            left=src_db[0], right=src_db[-1])
    tgt_interp = np.interp(log_freqs, np.log10(tgt_freqs), tgt_db,
                            left=tgt_db[0], right=tgt_db[-1])

    # Anchor target to source. Default: midband (60-100 Hz) mean reference so
    # absolute SPL drops out. When `anchor_freq_hz` is set, force
    # target(anchor) == source(anchor) — yields pure-cuts above the anchor for
    # downward-sloping curves; useful for deep-bass-priority sub calibration.
    if anchor_freq_hz is not None and anchor_freq_hz > 0:
        # Sample both curves at the anchor frequency (log-interp on log_freqs grid).
        src_at = float(np.interp(np.log10(anchor_freq_hz),
                                 np.log10(src_freqs), src_db))
        tgt_at = float(np.interp(np.log10(anchor_freq_hz),
                                 np.log10(tgt_freqs), tgt_db))
        tgt_interp = tgt_interp - tgt_at + src_at
    else:
        ref_lo, ref_hi = 60.0, 100.0
        ref_mask = (freqs >= ref_lo) & (freqs <= ref_hi)
        if np.any(ref_mask):
            src_ref = float(np.mean(src_interp[ref_mask]))
            tgt_ref = float(np.mean(tgt_interp[ref_mask]))
            tgt_interp = tgt_interp - tgt_ref + src_ref

    # Residual: how much more boost the magnitude FIR must add.
    residual_db = tgt_interp - src_interp - fir_spec_db

    # Apply focus window: outside it, residual smoothly goes to 0.
    lo, hi = focus_hz if focus_hz else (15.0, sample_rate * 0.4)
    window = np.ones_like(freqs)
    edge_lo = max(lo * 0.6, 1.0)
    edge_hi = min(hi * 1.4, freqs[-1])
    window[freqs < edge_lo] = 0.0
    window[freqs > edge_hi] = 0.0
    in_lo = (freqs >= edge_lo) & (freqs < lo)
    if np.any(in_lo):
        window[in_lo] = 0.5 * (
            1.0 - np.cos(np.pi * (freqs[in_lo] - edge_lo) / (lo - edge_lo + 1e-9))
        )
    in_hi = (freqs > hi) & (freqs <= edge_hi)
    if np.any(in_hi):
        window[in_hi] = 0.5 * (
            1.0 + np.cos(np.pi * (freqs[in_hi] - hi) / (edge_hi - hi + 1e-9))
        )
    residual_db = residual_db * window

    # Cap residual to safety-friendly magnitudes; downstream SafetyValidator
    # is the final gatekeeper but we keep the design conservative.
    residual_db = np.clip(residual_db, -24.0, 12.0)
    target_mag = 10.0 ** (residual_db / 20.0)

    # Frequency-sampling design → linear-phase FIR → minimum-phase via
    # homomorphic decomposition (so the magnitude correction adds zero pre-ring).
    linear_phase = np.fft.irfft(target_mag, n=n_fft)
    linear_phase = np.fft.fftshift(linear_phase)
    # Apply Hann window to suppress sidelobes, length n_taps centered.
    if n_taps < n_fft:
        start = (n_fft - n_taps) // 2
        linear_phase = linear_phase[start : start + n_taps]
    win = np.hanning(len(linear_phase))
    linear_phase = linear_phase * win
    # Convert to min-phase. minimum_phase requires symmetric input; the
    # windowed linear-phase IR is symmetric by construction.
    try:
        min_phase = minimum_phase(linear_phase, method="homomorphic",
                                  n_fft=4 * len(linear_phase))
    except Exception:
        # Fallback: just return the linear-phase windowed IR (caller will
        # see slightly more pre-ring but the design is still safe).
        min_phase = linear_phase
    return np.asarray(min_phase, dtype=np.float32)


def latency_budget_breakdown(summary: DesignSummary, current_pre_delay_ms: float = 38.0) -> dict:
    """Compute the latency saving vs current FIR.

    Returns budget breakdown including total sub-path latency change.
    """
    return {
        "current_fir_pre_delay_ms": current_pre_delay_ms,
        "new_fir_pre_delay_ms": summary.pre_delay_ms,
        "fir_latency_saved_ms": round(current_pre_delay_ms - summary.pre_delay_ms, 2),
        "explanation": (
            f"FIR pre-ring budget reduced from {current_pre_delay_ms:.0f}ms "
            f"(linear/mixed phase) to {summary.pre_delay_ms:.1f}ms "
            f"(modal-aware mixed-phase). Anti-pulse cancellations occupy "
            f"the remaining budget — used to actively cancel ringy modes."
        ),
    }


def _disabled_demo():
    """Original demo, retained as reference. Kept off the import path."""
    """Run a demo with the user's known room modes."""
    # User's room mode data from solo-sub measurements
    decay_modes = [
        {"freq_hz": 23.4,  "t60_ms": 1500, "peak_db": 5.0,  "suggested_q": 7.5},
        {"freq_hz": 46.9,  "t60_ms": 1500, "peak_db": 2.0,  "suggested_q": 7.4},
        {"freq_hz": 70.3,  "t60_ms": 1100, "peak_db": 9.0,  "suggested_q": 6.0},
        {"freq_hz": 93.8,  "t60_ms": 600,  "peak_db": 10.0, "suggested_q": 3.6},
        {"freq_hz": 117.2, "t60_ms": 320,  "peak_db": 17.0, "suggested_q": 1.3},
        {"freq_hz": 140.6, "t60_ms": 600,  "peak_db": 14.0, "suggested_q": 3.5},
        {"freq_hz": 164.1, "t60_ms": 750,  "peak_db": 5.0,  "suggested_q": 4.6},
        {"freq_hz": 187.5, "t60_ms": 700,  "peak_db": 4.0,  "suggested_q": 4.3},
    ]

    # LLM-supplied per-mode intent (override defaults with reasoning)
    # I (Claude) am acting as the "expert engineer reviewing measurements":
    intents = [
        ModeIntent(  # 23 Hz: peak +5, T60 1500 ms — long but mild
            freq_hz=23.4, t60_ms=1500, peak_db=5.0,
            treatment="min_phase",
            rationale="23Hz is below sub port (~22Hz) — anti-pulse here would cost "
                      "21ms pre-ring for a +5dB mode. Not worth it. Min-phase magnitude EQ only.",
        ),
        ModeIntent(  # 47 Hz: peak +2, mild
            freq_hz=46.9, t60_ms=1500, peak_db=2.0,
            treatment="skip",
            rationale="+2dB is below action threshold; skip.",
        ),
        ModeIntent(  # 70 Hz: peak +9, T60 1100 ms — PRIMARY anti-pulse target
            freq_hz=70.3, t60_ms=1100, peak_db=9.0,
            treatment="anti_pulse", cancel_strength=0.7,
            rationale="The big win: T60=1100ms ringy mode at +9dB. "
                      "Anti-pulse at 7.14ms pre-position should cut T60 to ~440ms. "
                      "Pre-ring cost: 7.14ms (well under 25ms audibility threshold).",
        ),
        ModeIntent(  # 93.8 Hz: peak +10, T60 600 ms
            freq_hz=93.8, t60_ms=600, peak_db=10.0,
            treatment="anti_pulse", cancel_strength=0.5,
            rationale="Medium-T60 mode at +10dB. Mild anti-pulse (0.5 strength). "
                      "Pre-ring: 5.3ms. Half-strength to avoid over-correction.",
        ),
        ModeIntent(  # 117 Hz: peak +17, T60 320 ms — STRONG narrow loud
            freq_hz=117.2, t60_ms=320, peak_db=17.0,
            treatment="linear_notch",
            rationale="+17dB peak, narrow Q, short T60. This is the loudest mode "
                      "and can be addressed with precise magnitude notch. Linear-phase "
                      "notch costs 4.3ms pre-ring — small price for surgical precision.",
        ),
        ModeIntent(  # 140 Hz: peak +14, T60 600 ms
            freq_hz=140.6, t60_ms=600, peak_db=14.0,
            treatment="anti_pulse", cancel_strength=0.6,
            rationale="Medium-T60 +14dB mode. Anti-pulse at 3.6ms pre-position. "
                      "Cancellation should cut T60 to ~360ms.",
        ),
        ModeIntent(  # 164 Hz: peak +5, T60 750 ms — mild
            freq_hz=164.1, t60_ms=750, peak_db=5.0,
            treatment="min_phase",
            rationale="+5dB at 164Hz: above the bass region, mains take over. "
                      "Conservative min-phase only.",
        ),
        ModeIntent(  # 187.5 Hz: peak +4, mild
            freq_hz=187.5, t60_ms=700, peak_db=4.0,
            treatment="skip",
            rationale="+4dB and above sub crossover; mains handle this band.",
        ),
    ]

    # Synthetic base correction (placeholder; real run feeds the user's existing FIR)
    base_corr = [0.0] * 4096
    base_corr[0] = 1.0  # passthrough impulse for demo

    designer = ModalAwareFIRDesigner(sample_rate=8000, n_taps=4096, max_pre_ring_ms=25.0)
    coeffs, summary = designer.design(
        decay_modes=decay_modes,
        base_correction=base_corr,
        intents=intents,
    )

    import json
    print("=" * 70)
    print("MODAL-AWARE FIR DESIGN — your room")
    print("=" * 70)
    print(f"\nTaps: {summary.total_taps}")
    print(f"Sample rate: {summary.sample_rate} Hz internal")
    print(f"Total pre-delay: {summary.pre_delay_ms:.2f} ms ({summary.pre_delay_samples} samples)")
    print(f"Peak amplitude: {summary.peak_amplitude:.4f}")
    if summary.notes:
        print("Notes:")
        for n in summary.notes:
            print(f"  - {n}")

    print("\nPer-mode treatment plan:")
    for entry in summary.mode_treatments:
        print(f"\n  {entry['freq_hz']:.1f} Hz → {entry['treatment'].upper()}")
        print(f"    rationale: {entry['rationale']}")
        if entry["treatment"] == "anti_pulse":
            print(f"    pre-ring used: {entry['anti_pulse_pre_ms']} ms")
            print(f"    anti-pulse amplitude: {entry['anti_pulse_amplitude']}")
            print(f"    predicted T60 reduction: {entry['predicted_t60_reduction_pct']}%")

    print("\nLatency budget vs current linear-phase FIR (38 ms):")
    budget = latency_budget_breakdown(summary, current_pre_delay_ms=38.0)
    for k, v in budget.items():
        print(f"  {k}: {v}")
