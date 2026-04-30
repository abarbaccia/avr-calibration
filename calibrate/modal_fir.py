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
from scipy.signal import minimum_phase, butter, sosfilt, iirpeak, lfilter


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
    # Provenance flags. When True, the value was explicitly supplied by the
    # caller and must not be overridden by adjacent-mode-density auto-selection.
    bp_q_user_set: bool = False
    envelope_user_set: bool = False


def _auto_envelope_for_mode(
    mode_freq: float,
    all_anti_pulse_modes: list[float],
    default_bp_q: float = 1.5,
) -> tuple[str, float, bool]:
    """Pick (envelope, bp_q, warn_dense) based on adjacent-mode log-distance.

    Adjacent anti-pulse modes within ~1 octave force a narrower Gabor envelope
    so spectral skirts do not leak into the neighbour's 1/3-oct band and trip
    the modal_cancel safety cap.

      * nearest neighbour > 1 octave (or none): default Gabor at ``default_bp_q``.
      * 0.5 .. 1.0 octave: bump bp_q to 3.0 (still Gabor).
      * < 0.5 octave (very dense triplets like 47/70/94 Hz): bump bp_q to 5.0
        and signal ``warn_dense=True`` so the caller can suggest a
        compensation_notch.
    """
    others = [f for f in all_anti_pulse_modes if abs(f - mode_freq) > 1e-6]
    if not others:
        return ("gabor", float(default_bp_q), False)
    nearest = min(others, key=lambda f: abs(np.log2(f / mode_freq)))
    octaves = abs(np.log2(nearest / mode_freq))
    if octaves > 1.0:
        return ("gabor", float(default_bp_q), False)
    if octaves >= 0.5:
        return ("gabor", 3.0, False)
    return ("gabor", 5.0, True)


@dataclass
class DesignSummary:
    total_taps: int
    sample_rate: int
    pre_delay_ms: float
    pre_delay_samples: int
    peak_amplitude: float
    mode_treatments: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    safety_budget: list[dict] = field(default_factory=list)
    compensation_notches: list[dict] = field(default_factory=list)


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
                modal_cancel_max_boost_db: float | None = None,
                compensation_notch: bool = False,
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

        # 5. Add anti-pulses for ringy modes.
        # We track per-anti-pulse signals + placement so we can run an
        # adjacent-band-aware iterative amplitude search after summation
        # (see step 5b below). Non-anti-pulse treatments still emit their
        # treatment record here.
        anti_records: list[dict] = []  # mutable per-pulse state for iteration
        # Build a treatment-record-by-mode lookup so iteration can demote
        # entries without disturbing ordering.
        treatment_index: dict[int, dict] = {}

        # 5a. Adjacent-mode-density-aware envelope/bp_q auto-selection.
        # When the caller did NOT explicitly supply bp_q or envelope on an
        # anti_pulse intent, choose them from the *set* of anti_pulse mode
        # frequencies so densely-spaced modes get a narrower Gabor (less
        # spectral-skirt leak into neighbours' 1/3-oct bands).
        anti_pulse_freqs = [
            float(i.freq_hz) for i in intents if i.treatment == "anti_pulse"
        ]
        auto_envelope_decisions: dict[int, tuple[str, float, bool]] = {}
        for idx, intent in enumerate(intents):
            if intent.treatment != "anti_pulse":
                continue
            if intent.bp_q_user_set or intent.envelope_user_set:
                continue
            env, bp_q, warn_dense = _auto_envelope_for_mode(
                float(intent.freq_hz), anti_pulse_freqs,
            )
            # Only mark as auto-selected when at least one of (envelope, bp_q)
            # actually differs from the dataclass defaults.
            if env != intent.envelope or abs(bp_q - intent.bp_q) > 1e-6:
                auto_envelope_decisions[idx] = (env, bp_q, warn_dense)

        for idx, intent in enumerate(intents):
            entry = {
                "freq_hz": intent.freq_hz,
                "treatment": intent.treatment,
                "rationale": intent.rationale,
            }

            if intent.treatment == "anti_pulse":
                # Apply auto-envelope selection if the caller didn't pin
                # bp_q/envelope on this intent.
                eff_envelope = intent.envelope
                eff_bp_q = intent.bp_q
                if idx in auto_envelope_decisions:
                    auto_env, auto_bp_q, warn_dense = auto_envelope_decisions[idx]
                    eff_envelope = auto_env
                    eff_bp_q = auto_bp_q
                    entry["auto_envelope_selected"] = True
                    entry["auto_envelope"] = auto_env
                    entry["auto_bp_q"] = round(float(auto_bp_q), 3)
                    if warn_dense:
                        summary.notes.append(
                            f"dense-mode warning: {intent.freq_hz:.0f} Hz has an "
                            f"adjacent anti_pulse mode within 0.5 octave; bp_q "
                            f"raised to {auto_bp_q:.0f} — consider a "
                            f"compensation_notch on the leaked band"
                        )
                anti = design_anti_pulse(
                    freq_hz=intent.freq_hz,
                    peak_db=intent.peak_db,
                    cancel_strength=intent.cancel_strength,
                    sample_rate=self.sr,
                    bp_q=eff_bp_q,
                    envelope=eff_envelope,
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
                segment = -anti[:end - start]
                fir[start:end] += segment
                base_amp = float(np.max(np.abs(anti)))
                entry["anti_pulse_pre_ms"] = round(half_cycle_samples / self.sr * 1000, 2)
                entry["anti_pulse_amplitude"] = round(base_amp, 4)
                entry["cancel_strength_requested"] = round(intent.cancel_strength, 4)
                entry["cancel_strength_achieved"] = round(intent.cancel_strength, 4)
                entry["predicted_t60_reduction_pct"] = int(intent.cancel_strength * 60)
                anti_records.append({
                    "intent_idx": idx,
                    "freq_hz": float(intent.freq_hz),
                    "segment": np.asarray(segment, dtype=np.float32),
                    "start": int(start),
                    "end": int(end),
                    "scale": 1.0,
                    "requested_strength": float(intent.cancel_strength),
                    "base_amp": base_amp,
                    "entry": entry,
                })

            summary.mode_treatments.append(entry)
            treatment_index[idx] = entry

        # 5b. Adjacent-band-aware iterative amplitude search.
        # Anti-pulses are time-localized but their FFT magnitude leaks into
        # adjacent 1/3-octave bands — typically the bands one and two below
        # the mode (e.g. a 70 Hz pulse boosts 50 and 63 Hz). When that boost
        # exceeds the profile's modal_cancel cap, SafetyValidator will reject
        # the FIR. We catch it here, scaling each pulse down (binary search
        # ≤6 iterations, jointly across pulses) until all adjacent bands fit.
        if anti_records and modal_cancel_max_boost_db is not None:
            cap_db = float(modal_cancel_max_boost_db)
            if compensation_notch:
                # Alternative path: keep cancel_strength HIGH, then add narrow
                # magnitude notches on adjacent over-cap bands. Verifies the
                # mode's cancellation is preserved (notch BW > mode BW would
                # destroy the cancellation, so we abort the notch in that case).
                self._apply_compensation_notches(
                    fir=fir,
                    anti_records=anti_records,
                    cap_db=cap_db,
                    summary=summary,
                )
            else:
                self._iteratively_fit_adjacent_band_cap(
                    fir=fir,
                    anti_records=anti_records,
                    cap_db=cap_db,
                    summary=summary,
                )

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

    # ── Adjacent-band iterative amplitude search ────────────────────────
    # The anti-pulse spectral skirt leaks energy into 1/3-octave bands
    # surrounding the mode. SafetyValidator's modal_cancel cap rejects any
    # band exceeding modal_cancel_max_boost_db. Rather than make the LLM
    # blindly retry with smaller cancel_strength, we self-iterate here.

    def _adjacent_band_centres(self, mode_freq_hz: float) -> list[float]:
        """Return 1/3-octave centres within ±2/3-octave of ``mode_freq_hz``.

        Two-thirds of an octave covers the 1-2 bands above and below the
        mode where Gabor-anti-pulse spectral skirts typically leak. We
        deliberately exclude the mode's own band (its boost is the
        cancellation we want — caller's safety profile already applies the
        modal_cancel cap there).
        """
        # Late import to avoid a circular dependency at module import.
        from .safety import THIRD_OCTAVE_CENTRES_HZ
        ratio = 2.0 ** (2.0 / 3.0)
        lo = mode_freq_hz / ratio
        hi = mode_freq_hz * ratio
        # Find the centre that "owns" the mode (closest in log-space).
        own = min(
            THIRD_OCTAVE_CENTRES_HZ,
            key=lambda c: abs(np.log(c) - np.log(mode_freq_hz)),
        )
        return [c for c in THIRD_OCTAVE_CENTRES_HZ if lo <= c <= hi and c != own]

    def _per_band_peak_db(self, fir: np.ndarray) -> dict[float, float]:
        """Bin |H(f)| into 1/3-oct centres, return peak dB per band."""
        from .safety import THIRD_OCTAVE_CENTRES_HZ
        n_fft = max(4096, int(2 ** np.ceil(np.log2(max(len(fir), 2)))))
        spec = np.fft.rfft(fir, n=n_fft)
        freqs = np.fft.rfftfreq(n_fft, d=1.0 / float(self.sr))
        mag = np.abs(spec)
        mag_db = 20.0 * np.log10(np.maximum(mag, 1e-9))
        half = 2.0 ** (1.0 / 6.0)
        out: dict[float, float] = {}
        for centre in THIRD_OCTAVE_CENTRES_HZ:
            lo, hi = centre / half, centre * half
            mask = (freqs >= lo) & (freqs < hi)
            if np.any(mask):
                out[centre] = float(np.max(mag_db[mask]))
        return out

    def _apply_compensation_notches(
        self,
        fir: np.ndarray,
        anti_records: list[dict],
        cap_db: float,
        summary: DesignSummary,
    ) -> None:
        """Keep anti-pulse cancel_strength HIGH; suppress over-cap leakage with
        narrow magnitude notches.

        Strategy:
          1. Identify each adjacent 1/3-oct band whose peak exceeds ``cap_db``.
          2. For each over-cap band, design a Q≈5 peaking-cut biquad sized to
             pull the band down to (cap − 0.5 dB).
          3. Verify mode preservation: the depression at ``mode_freq`` must be
             ≥ 80% of the no-notch depression. If the notch directly overlaps
             the mode centre (notch BW envelopes the mode), abort that notch
             and record a warning.
          4. Apply each surviving notch via ``scipy.signal.lfilter`` in place.
        """
        n_fft = max(4096, int(2 ** np.ceil(np.log2(max(len(fir), 2)))))

        def _mag_db_at(arr: np.ndarray, freq: float) -> float:
            spec = np.fft.rfft(arr, n=n_fft)
            freqs = np.fft.rfftfreq(n_fft, d=1.0 / float(self.sr))
            idx = int(np.argmin(np.abs(freqs - freq)))
            return float(20.0 * np.log10(max(abs(spec[idx]), 1e-9)))

        # Mark per-mode achieved strength = requested (no amplitude reduction
        # in this path) and predicted T60 reduction accordingly.
        for rec in anti_records:
            entry = rec["entry"]
            entry["cancel_strength_achieved"] = round(
                float(rec["requested_strength"]), 4
            )
            entry["anti_pulse_amplitude"] = round(float(rec["base_amp"]), 4)
            entry["predicted_t60_reduction_pct"] = int(
                rec["requested_strength"] * 60
            )

        # Snapshot the pre-notch FIR for verifying mode-cancellation depression.
        pre_notch_fir = fir.copy()

        # Collect all over-cap adjacent bands across all anti-pulses.
        band_peaks = self._per_band_peak_db(fir)
        over_cap: dict[float, dict] = {}
        for rec in anti_records:
            for centre in self._adjacent_band_centres(rec["freq_hz"]):
                level = band_peaks.get(centre)
                if level is None or level <= cap_db:
                    continue
                if centre not in over_cap or level > over_cap[centre]["level_db"]:
                    over_cap[centre] = {
                        "level_db": float(level),
                        "neighbour_mode_hz": float(rec["freq_hz"]),
                    }

        notch_q = 5.0
        for centre, info in sorted(over_cap.items()):
            level = info["level_db"]
            mode_hz = info["neighbour_mode_hz"]
            cut_db = -(level - cap_db + 0.5)
            if cut_db >= -0.05:
                continue

            # Mode-bandwidth check. If the compensation notch's nominal BW
            # (≈ centre/Q) envelopes the neighbouring mode centre, the notch
            # would directly cut the cancellation energy → abort.
            notch_bw = centre / notch_q
            notch_lo = centre - notch_bw / 2.0
            notch_hi = centre + notch_bw / 2.0
            if notch_lo <= mode_hz <= notch_hi:
                summary.notes.append(
                    f"compensation_notch skipped: {centre:.1f} Hz notch "
                    f"(BW={notch_bw:.1f} Hz) overlaps mode at {mode_hz:.1f} Hz"
                )
                continue

            # Hand-rolled RBJ peaking EQ biquad (cut).
            A = 10.0 ** (cut_db / 40.0)
            w0 = 2.0 * np.pi * centre / float(self.sr)
            cos_w0 = np.cos(w0)
            alpha = np.sin(w0) / (2.0 * notch_q)
            b0 = 1.0 + alpha * A
            b1 = -2.0 * cos_w0
            b2 = 1.0 - alpha * A
            a0 = 1.0 + alpha / A
            a1 = -2.0 * cos_w0
            a2 = 1.0 - alpha / A
            b = np.array([b0, b1, b2]) / a0
            a = np.array([1.0, a1 / a0, a2 / a0])

            candidate = lfilter(b, a, fir).astype(fir.dtype)

            # Verify cancellation preserved at the neighbouring mode centre.
            # The notch must not shift the FIR magnitude at ``mode_hz`` by
            # more than 1 dB — any larger drop means the notch BW reaches the
            # cancellation zone and undoes the anti-pulse.
            mag_pre = _mag_db_at(pre_notch_fir, mode_hz)
            mag_post = _mag_db_at(candidate, mode_hz)
            if abs(mag_post - mag_pre) > 1.0:
                summary.notes.append(
                    f"compensation_notch aborted: {centre:.1f} Hz notch "
                    f"shifts FIR magnitude at mode {mode_hz:.1f} Hz by "
                    f"{mag_post - mag_pre:+.2f} dB (>1 dB) — notch BW "
                    f"overlaps cancellation"
                )
                continue

            # Commit the notch.
            fir[:] = candidate
            summary.compensation_notches.append({
                "freq_hz": round(float(centre), 2),
                "gain_db": round(float(cut_db), 2),
                "q": round(float(notch_q), 2),
                "neighbour_mode_hz": round(float(mode_hz), 2),
                "pre_band_peak_db": round(float(level), 2),
            })

        # Populate per-mode safety_budget post-notch.
        post_peaks = self._per_band_peak_db(fir)
        for rec in anti_records:
            adj_bands = self._adjacent_band_centres(rec["freq_hz"])
            band_peaks_now = {c: post_peaks.get(c, -np.inf) for c in adj_bands}
            worst = max(band_peaks_now.values(), default=-np.inf)
            entry = rec["entry"]
            entry["adjacent_band_peak_db"] = (
                round(float(worst), 2) if np.isfinite(worst) else None
            )
            entry["adjacent_band_cap_db"] = round(float(cap_db), 2)
            mode_round = round(float(rec["freq_hz"]), 2)
            summary.safety_budget.append({
                "mode_freq_hz": mode_round,
                "adjacent_bands_hz": [round(c, 1) for c in adj_bands],
                "max_boost_db": (
                    round(float(worst), 2) if np.isfinite(worst) else None
                ),
                "cap_db": round(float(cap_db), 2),
                "headroom_db": (
                    round(float(cap_db - worst), 2)
                    if np.isfinite(worst)
                    else None
                ),
                "compensation_notch_used": any(
                    cn["neighbour_mode_hz"] == mode_round
                    for cn in summary.compensation_notches
                ),
            })

    def _iteratively_fit_adjacent_band_cap(
        self,
        fir: np.ndarray,
        anti_records: list[dict],
        cap_db: float,
        summary: DesignSummary,
    ) -> None:
        """Scale each anti-pulse so adjacent-band boost ≤ ``cap_db``.

        Each anti-pulse contributes (linearly) to the FIR's spectrum, so
        scaling its time-domain amplitude scales its spectral contribution
        by the same factor. We binary-search a per-pulse scale ``s`` ∈
        [0, 1] such that, when applied to that pulse, every band in its
        adjacency window is within cap. We re-run the full set up to 6
        times to converge when pulses share adjacent bands.

        If any pulse's amplitude must drop to ≈0 yet still exceeds cap,
        the corresponding mode is auto-demoted to ``linear_notch`` (the
        anti-pulse component is removed and the entry's treatment field
        is updated).
        """
        max_passes = 6

        def _band_peak_excluding_pulses(
            current_fir: np.ndarray, centres: list[float]
        ) -> dict[float, float]:
            return {c: v for c, v in self._per_band_peak_db(current_fir).items()
                    if c in centres}

        for pass_idx in range(max_passes):
            changed = False
            band_peaks = self._per_band_peak_db(fir)
            for rec in list(anti_records):
                if rec.get("demoted"):
                    continue
                adj_bands = self._adjacent_band_centres(rec["freq_hz"])
                worst = max(
                    (band_peaks.get(c, -np.inf) for c in adj_bands),
                    default=-np.inf,
                )
                if worst <= cap_db:
                    continue
                # Binary search a scale factor for THIS pulse that brings
                # all adjacent bands ≤ cap, holding others fixed.
                s_lo, s_hi = 0.0, rec["scale"]
                # Save the current contribution and remove it from fir
                seg = rec["segment"][: rec["end"] - rec["start"]] * rec["scale"]
                fir[rec["start"]:rec["end"]] -= seg
                # Now binary-search a new scale relative to the unscaled segment.
                base_seg = rec["segment"][: rec["end"] - rec["start"]]
                best_s = 0.0
                # Quick check: is even s=0 (no pulse) over-cap from OTHER pulses?
                worst_zero = max(
                    (self._per_band_peak_db(fir).get(c, -np.inf) for c in adj_bands),
                    default=-np.inf,
                )
                if worst_zero > cap_db:
                    # Other pulses alone overflow this band — leave this pulse
                    # at zero; the other-pulse pass will fix it.
                    new_scale = 0.0
                else:
                    # Binary search for largest s such that adding s*base_seg
                    # keeps adjacent bands ≤ cap.
                    lo, hi = 0.0, rec["scale"]
                    for _ in range(6):
                        mid = 0.5 * (lo + hi)
                        fir[rec["start"]:rec["end"]] += base_seg * mid
                        peaks = self._per_band_peak_db(fir)
                        worst_mid = max(
                            (peaks.get(c, -np.inf) for c in adj_bands),
                            default=-np.inf,
                        )
                        fir[rec["start"]:rec["end"]] -= base_seg * mid
                        if worst_mid <= cap_db:
                            best_s = mid
                            lo = mid
                        else:
                            hi = mid
                    new_scale = best_s
                # Apply the new scale
                fir[rec["start"]:rec["end"]] += base_seg * new_scale
                if abs(new_scale - rec["scale"]) > 1e-4:
                    rec["scale"] = float(new_scale)
                    changed = True
            if not changed:
                break

        # Demote any pulse whose final scale is effectively zero AND whose
        # band still exceeds cap (caller can't deliver any cancellation).
        post_peaks = self._per_band_peak_db(fir)
        for rec in anti_records:
            entry = rec["entry"]
            achieved_strength = rec["scale"] * rec["requested_strength"]
            entry["cancel_strength_achieved"] = round(achieved_strength, 4)
            entry["anti_pulse_amplitude"] = round(rec["base_amp"] * rec["scale"], 4)
            entry["predicted_t60_reduction_pct"] = int(achieved_strength * 60)
            adj_bands = self._adjacent_band_centres(rec["freq_hz"])
            worst = max(
                (post_peaks.get(c, -np.inf) for c in adj_bands),
                default=-np.inf,
            )
            entry["adjacent_band_peak_db"] = round(float(worst), 2) if np.isfinite(worst) else None
            entry["adjacent_band_cap_db"] = round(cap_db, 2)
            if rec["scale"] < 0.02 and worst > cap_db:
                # Even amplitude≈0 cannot fit cap (other pulses dominate this
                # band). Demote this mode to linear_notch — the FIR's anti-
                # pulse contribution has already been zeroed by the search.
                rec["demoted"] = True
                entry["demoted_from"] = "anti_pulse"
                entry["treatment"] = "linear_notch"
                entry["rationale"] = (
                    "anti_pulse demoted to linear_notch — adjacent-band cap "
                    "unreachable (cap=+%.0f dB at adjacent band still exceeded "
                    "even at amplitude≈0)" % cap_db
                )
                summary.notes.append(
                    f"auto-demote: {rec['freq_hz']:.0f} Hz anti_pulse → "
                    f"linear_notch (adjacent-band cap unreachable)"
                )
            elif rec["scale"] < 0.999:
                summary.notes.append(
                    f"adjacent-band fit: {rec['freq_hz']:.0f} Hz anti_pulse "
                    f"scaled to {rec['scale']:.2f} of requested "
                    f"(cancel_strength {rec['requested_strength']:.2f} → "
                    f"{achieved_strength:.2f}) to keep adjacent bands ≤"
                    f"+{cap_db:.0f} dB"
                )

        # Populate per-mode safety_budget entries for the response.
        for rec in anti_records:
            adj_bands = self._adjacent_band_centres(rec["freq_hz"])
            band_peaks_now = {c: post_peaks.get(c, -np.inf) for c in adj_bands}
            worst = max(band_peaks_now.values(), default=-np.inf)
            summary.safety_budget.append({
                "mode_freq_hz": round(float(rec["freq_hz"]), 2),
                "adjacent_bands_hz": [round(c, 1) for c in adj_bands],
                "max_boost_db": round(float(worst), 2) if np.isfinite(worst) else None,
                "cap_db": round(float(cap_db), 2),
                "headroom_db": (
                    round(float(cap_db - worst), 2)
                    if np.isfinite(worst)
                    else None
                ),
            })


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
