"""SafetyValidator — hard limits for miniDSP EQ writes.

Enforces the safety constraints defined in CLAUDE.md before any filter is
written to the miniDSP 2x4 HD.  All limits are specific to the SVS PB12-NSD
(ported sub, ~22 Hz tuning) but the structure is general.

Limits:
  - Minimum boost frequency: 25 Hz (below this, boost is unsafe for a ported sub)
  - Max boost per EQ band:   +6 dB
  - Max cumulative boost in any 1/3-octave band: +9 dB
  - Max change per iteration: +3 dB/band (protects against large sudden swings)
  - Mandatory infrasonic HPF: 18 Hz, 4th-order Butterworth (always enforced)
  - Cuts: no floor (cuts are always safe)

Usage::

    from calibrate.safety import SafetyValidator, FilterSpec

    validator = SafetyValidator()
    result = validator.validate(new_filters, previous_filters)
    if not result.ok:
        return {"ok": False, "error": result.error}

All methods return a ``ValidationResult`` — they never raise.  The caller
is responsible for acting on ``ok=False``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .graph import TransducerProfile


# ── Constants ──────────────────────────────────────────────────────────────────

MIN_BOOST_FREQ_HZ: float = 25.0
"""Minimum frequency at which a boost is permitted (ported sub port resonance protection)."""

MAX_BOOST_PER_BAND_DB: float = 6.0
"""Maximum boost allowed on any single EQ band (below FREQ_DEPENDENT_BOOST_THRESHOLD_HZ)."""

MAX_BOOST_ABOVE_THRESHOLD_DB: float = 8.0
"""Maximum boost above FREQ_DEPENDENT_BOOST_THRESHOLD_HZ (thermal limit only, SHARC has no saturation)."""

FREQ_DEPENDENT_BOOST_THRESHOLD_HZ: float = 30.0
"""Frequency above which the higher boost limit applies. Below this, port unloading risk is real."""

MAX_CUMULATIVE_BOOST_DB: float = 9.0
"""Maximum total boost allowed in any single 1/3-octave band."""

MAX_CHANGE_PER_ITER_DB: float = 3.0
"""Maximum increase in gain from the previous iteration on any band."""

MAX_CHANGE_SIMULATED_DB: float = 6.0
"""Maximum increase when the filter set has been verified by simulate_eq beforehand."""

HPF_FREQ_HZ: float = 18.0
"""Infrasonic HPF cutoff frequency (Hz). Always injected; cannot be removed."""

HPF_ORDER: int = 4
"""Butterworth HPF order."""

# 1/3-octave band centres from 20 Hz to 200 Hz (ISO 266 series)
THIRD_OCTAVE_CENTRES_HZ: list[float] = [
    20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0, 200.0
]


# ── Data types ─────────────────────────────────────────────────────────────────

FilterType = Literal["peaking", "low_shelf", "high_shelf", "hpf", "lpf", "notch"]


@dataclass
class FilterSpec:
    """Human-readable EQ filter specification (as used in apply_eq MCP tool).

    Attributes:
        freq     — centre/corner frequency in Hz
        gain_db  — gain in dB (positive = boost, negative = cut)
        q        — quality factor (ignored for HPF/LPF)
        type     — filter type
    """

    freq: float
    gain_db: float
    q: float
    type: FilterType


class SafetyValidationError(Exception):
    """Raised by ``SafetyValidator.validate_fir`` on safety violations.

    Carries a human-readable message that names the offending frequency
    band and dB magnitude so callers can surface it to the LLM / user.
    Matches the style of ``ValidationResult.failed``'s error string but
    is raised rather than returned — FIR validation has no "simulation
    verified" relaxation path that needs a soft return.
    """


@dataclass
class ValidationResult:
    """Result of a SafetyValidator check.

    Attributes:
        ok     — True if all checks passed
        error  — Human-readable error message if ok=False, else empty string
    """

    ok: bool
    error: str = ""

    @classmethod
    def passed(cls) -> "ValidationResult":
        return cls(ok=True)

    @classmethod
    def failed(cls, reason: str) -> "ValidationResult":
        return cls(ok=False, error=f"SafetyValidator: {reason}")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _third_octave_for_freq(freq_hz: float) -> float:
    """Return the nearest 1/3-octave centre frequency for *freq_hz*.

    Uses log-distance to find the closest centre from THIRD_OCTAVE_CENTRES_HZ.
    Frequencies outside the 20–200 Hz range snap to the nearest boundary.
    """
    log_freq = math.log(freq_hz)
    best = min(
        THIRD_OCTAVE_CENTRES_HZ,
        key=lambda c: abs(math.log(c) - log_freq),
    )
    return best


def _is_boost(f: FilterSpec) -> bool:
    """Return True if this filter applies positive gain (a boost)."""
    return f.gain_db > 0.0


# ── Validator ──────────────────────────────────────────────────────────────────

class SafetyValidator:
    """Validates a list of FilterSpec objects against hard safety limits.

    Limits come from a ``TransducerProfile`` — the default matches the
    SVS PB12-NSD ported-sub baseline, so existing callers that do
    ``SafetyValidator()`` keep their current behaviour. Pass a different
    profile for a different transducer model::

        validator = SafetyValidator(profile)  # where profile is a TransducerProfile

    Call ``validate(filters)`` before writing to the DSP.  Optionally pass
    ``previous_filters`` to enforce the per-iteration change limit.
    """

    def __init__(self, profile: "TransducerProfile | None" = None) -> None:
        # Late import: avoid circular dependency between safety.py and graph.py.
        # graph.py imports nothing from safety.py; safety.py only needs
        # TransducerProfile for its dataclass shape.
        if profile is None:
            from .graph import SVS_PB12_NSD_PROFILE
            profile = SVS_PB12_NSD_PROFILE
        self._profile = profile

    @property
    def profile(self) -> "TransducerProfile":
        return self._profile

    def validate(
        self,
        filters: list[FilterSpec],
        previous_filters: list[FilterSpec] | None = None,
        simulation_verified: bool = False,
    ) -> ValidationResult:
        """Run all safety checks on *filters*.

        Checks (in order):
          1. Per-band boost frequency floor (≥ 25 Hz)
          2. Per-band boost ceiling (frequency-dependent: +6 dB below 30 Hz, +8 dB above)
          3. Cumulative 1/3-octave boost ceiling (≤ +9 dB)
          4. Per-iteration change limit (≤ +3 dB, or +6 dB if simulation_verified)
          5. HPF presence — mandatory 18 Hz 4th-order Butterworth

        Args:
            simulation_verified: If True, the caller has verified the filter set via
                simulate_eq immediately before applying.  Relaxes the per-iteration
                change limit from +3 dB to +6 dB.

        Returns ValidationResult.passed() if all checks pass, or
        ValidationResult.failed(reason) on the first violation found.
        """
        checks = [
            self._check_boost_frequency_floor,
            self._check_per_band_boost_ceiling,
            self._check_cumulative_boost,
            self._check_hpf_present,
        ]

        for check in checks:
            result = check(filters)
            if not result.ok:
                return result

        if previous_filters is not None:
            result = self._check_per_iteration_change(
                filters, previous_filters, simulation_verified=simulation_verified
            )
            if not result.ok:
                return result

        return ValidationResult.passed()

    # ── Individual checks ──────────────────────────────────────────────────────

    def _check_boost_frequency_floor(
        self, filters: list[FilterSpec]
    ) -> ValidationResult:
        """Reject any boost below the profile's min boost frequency."""
        floor = self._profile.min_boost_freq_hz
        for f in filters:
            if f.type == "hpf":
                continue
            if _is_boost(f) and f.freq < floor:
                return ValidationResult.failed(
                    f"boost at {f.freq:.1f} Hz is below minimum boost frequency "
                    f"({floor:.0f} Hz) for profile {self._profile.name!r}"
                )
        return ValidationResult.passed()

    def _check_per_band_boost_ceiling(
        self, filters: list[FilterSpec]
    ) -> ValidationResult:
        """Reject any single band with gain above the frequency-dependent limit.

        Below the profile's threshold: the stricter ``max_boost_per_band_db``.
        At/above the threshold: the looser ``max_boost_above_threshold_db``.
        For non-ported transducers (``max_boost_above_threshold_db`` equal
        to ``max_boost_per_band_db``) the two limits collapse into one.
        """
        threshold = self._profile.freq_dependent_boost_threshold_hz
        for f in filters:
            if f.type == "hpf":
                continue
            limit = (
                self._profile.max_boost_per_band_db
                if f.freq < threshold
                else self._profile.max_boost_above_threshold_db
            )
            if f.gain_db > limit:
                return ValidationResult.failed(
                    f"band at {f.freq:.1f} Hz requests +{f.gain_db:.1f} dB "
                    f"(profile {self._profile.name!r} max boost at "
                    f"{'<' if f.freq < threshold else '>='}{threshold:.0f} Hz "
                    f"is +{limit:.0f} dB)"
                )
        return ValidationResult.passed()

    def _check_cumulative_boost(
        self, filters: list[FilterSpec]
    ) -> ValidationResult:
        """Reject if any 1/3-octave band accumulates past the profile's ceiling."""
        ceiling = self._profile.max_cumulative_boost_db
        cumulative: dict[float, float] = {}
        for f in filters:
            if f.type == "hpf" or not _is_boost(f):
                continue
            centre = _third_octave_for_freq(f.freq)
            cumulative[centre] = cumulative.get(centre, 0.0) + f.gain_db

        for centre, total in cumulative.items():
            if total > ceiling:
                return ValidationResult.failed(
                    f"cumulative boost in {centre:.0f} Hz 1/3-octave band is "
                    f"+{total:.1f} dB (profile {self._profile.name!r} cumulative "
                    f"ceiling is +{ceiling:.0f} dB)"
                )
        return ValidationResult.passed()

    def _check_per_iteration_change(
        self,
        filters: list[FilterSpec],
        previous_filters: list[FilterSpec],
        simulation_verified: bool = False,
    ) -> ValidationResult:
        """Reject if any band increases by more than the per-iteration limit vs previous.

        Default limit is +3 dB.  If *simulation_verified* is True (the caller ran
        ``simulate_eq`` and confirmed the predicted response), the limit relaxes to
        +6 dB — halving measurement cycles after structural DSP changes like FIR.

        Matches filters by nearest 1/3-octave band (not exact frequency) to
        prevent drift-based bypasses where a correction algorithm shifts a
        filter from 50.0 Hz to 49.9 Hz to dodge the delta check.
        """
        limit = (
            self._profile.max_change_simulated_db
            if simulation_verified
            else self._profile.max_change_per_iter_db
        )

        # Build a lookup: previous gain by 1/3-octave centre
        prev_by_band: dict[float, float] = {}
        for f in previous_filters:
            if f.type == "hpf":
                continue
            centre = _third_octave_for_freq(f.freq)
            # If multiple filters in the same band, use the max gain
            prev_by_band[centre] = max(prev_by_band.get(centre, f.gain_db), f.gain_db)

        for f in filters:
            if f.type == "hpf":
                continue
            centre = _third_octave_for_freq(f.freq)
            prev_gain = prev_by_band.get(centre, 0.0)
            delta = f.gain_db - prev_gain
            if delta > limit:
                return ValidationResult.failed(
                    f"band at {f.freq:.1f} Hz (1/3-octave: {centre:.0f} Hz) "
                    f"increases by +{delta:.1f} dB "
                    f"(previous: {prev_gain:+.1f} dB → proposed: {f.gain_db:+.1f} dB; "
                    f"max change per iteration is +{limit:.0f} dB"
                    f"{' [simulation-verified]' if simulation_verified else ''})"
                )
        return ValidationResult.passed()

    # ── FIR magnitude validation ───────────────────────────────────────────────

    def validate_fir(
        self,
        coefficients: list[float],
        sample_rate: int,
        profile: "TransducerProfile | None" = None,
    ) -> None:
        """Validate a FIR filter's magnitude response against the profile's limits.

        Mirrors the PEQ safety rules, applied in the frequency domain via FFT:

        1. **Below ``profile.min_boost_freq_hz``** — any boost must be
           ≤ ``profile.max_boost_per_band_db`` (port-unloading protection;
           matches the PEQ boost-frequency-floor check).
        2. **From ``min_boost_freq_hz`` up through 200 Hz** — any boost must
           be ≤ ``profile.max_boost_above_threshold_db`` (thermal ceiling,
           matches the recipe's Phase 2.2 manual-check and PEQ
           ``_check_per_band_boost_ceiling`` for the above-threshold band).
        3. **Cuts are always safe** — attenuation is not constrained.

        Magnitude is computed via ``numpy.fft.rfft`` and binned to the same
        1/3-octave centres used by the PEQ validator, so an FIR that
        aggregates +4 dB across two adjacent bins still reads as "+4 dB at
        this band", not as an invisible piecewise-legal stack.

        Args:
            coefficients: FIR taps (float array).
            sample_rate: rate the FIR will run at, in Hz — sets the FFT's
                frequency axis. Required because the same tap sequence has
                a different magnitude response at 48 kHz vs 96 kHz.
            profile: transducer profile to validate against. Defaults to
                the validator's own profile (``SafetyValidator(profile)``),
                so the driver-level call can stay zero-arg.

        Raises:
            SafetyValidationError: if any 1/3-octave band's peak magnitude
                exceeds the applicable limit. Message names the offending
                frequency band and the observed dB level.
        """
        import numpy as np

        prof = profile if profile is not None else self._profile

        if not coefficients:
            # Empty FIR is a no-op; driver layer will reject before us but
            # guard anyway so validate_fir is independently safe to call.
            return

        taps = np.asarray(coefficients, dtype=np.float64)
        # Zero-pad to fine frequency resolution. At 96 kHz we need ~2 Hz bin
        # width to reliably cover the 20 Hz 1/3-octave band (17.8–22.4 Hz),
        # so demand sample_rate/n_fft ≤ 2 Hz → n_fft ≥ sample_rate/2. The
        # power-of-two rounding above that keeps the FFT path fast.
        min_n_fft = max(4096, int(2 ** np.ceil(np.log2(max(int(sample_rate) // 2, 4096)))))
        n_fft = max(min_n_fft, int(2 ** np.ceil(np.log2(max(len(taps), 2)))))
        spectrum = np.fft.rfft(taps, n=n_fft)
        freqs = np.fft.rfftfreq(n_fft, d=1.0 / float(sample_rate))
        # 20·log10(|H|). Guard against log(0) at notches; cuts aren't a safety
        # concern so we can clip the floor without affecting any check.
        mag = np.abs(spectrum)
        mag_db = 20.0 * np.log10(np.maximum(mag, 1e-9))

        # Bin peak magnitude per 1/3-octave band (20 Hz … 200 Hz).
        # Bin edges: ±1/6-octave around each centre (log-spaced).
        half_step = 2.0 ** (1.0 / 6.0)
        per_band: dict[float, float] = {}
        for centre in THIRD_OCTAVE_CENTRES_HZ:
            lo = centre / half_step
            hi = centre * half_step
            mask = (freqs >= lo) & (freqs < hi)
            if not np.any(mask):
                continue
            per_band[centre] = float(np.max(mag_db[mask]))

        floor = prof.min_boost_freq_hz
        below_floor_limit = prof.max_boost_per_band_db
        thermal_limit = prof.max_boost_above_threshold_db
        transient_limit = prof.transient_max_boost_db
        transient_max_ms = prof.transient_max_pulse_ms

        # Transient-aware classification. A FIR's boost at a 1/3-octave band
        # can come from a sustained gain (e.g., +8 dB PEQ at 50 Hz applied
        # in FIR form: long-tailed bandpassed envelope) or a brief modal-
        # cancellation anti-pulse (Gaussian burst, ~5–15 ms). The thermal
        # cap protects the driver against sustained level; transient pulses
        # contribute to excursion only for their duration. Compute, per
        # band, the bandpassed envelope's width-above-half-amplitude as
        # the discriminator.
        def _pulse_width_ms(lo_hz: float, hi_hz: float) -> float:
            from scipy.signal import butter as _butter
            from scipy.signal import sosfiltfilt as _sosfiltfilt
            from scipy.signal import hilbert as _hilbert

            nyq = 0.5 * float(sample_rate)
            lo_n = max(1.0, lo_hz) / nyq
            hi_n = min(0.999, hi_hz / nyq)
            if hi_n <= lo_n:
                return float("inf")
            sos = _butter(4, [lo_n, hi_n], btype="band", output="sos")
            band = _sosfiltfilt(sos, taps)
            env = np.abs(_hilbert(band))
            peak = float(env.max())
            if peak <= 0.0:
                return 0.0
            above = env >= 0.5 * peak
            # Longest contiguous run of True in `above` (samples).
            longest = 0
            cur = 0
            for v in above:
                if v:
                    cur += 1
                    if cur > longest:
                        longest = cur
                else:
                    cur = 0
            return 1000.0 * longest / float(sample_rate)

        for centre, peak_db in per_band.items():
            if peak_db <= 0.0:
                # Cut — always safe.
                continue
            if centre < floor:
                if peak_db > below_floor_limit:
                    raise SafetyValidationError(
                        f"SafetyValidator: FIR boost of +{peak_db:.1f} dB "
                        f"in {centre:.0f} Hz 1/3-octave band exceeds "
                        f"below-port-tune limit of +{below_floor_limit:.0f} dB "
                        f"(profile {prof.name!r}, floor {floor:.0f} Hz). "
                        f"Boost below the port-tuning frequency risks "
                        f"port unloading and driver damage."
                    )
            else:
                if peak_db <= thermal_limit:
                    continue  # Under sustained cap; never need transient logic.
                lo = centre / half_step
                hi = centre * half_step
                pulse_ms = _pulse_width_ms(lo, hi)
                effective_limit = (
                    transient_limit if pulse_ms < transient_max_ms else thermal_limit
                )
                if peak_db > effective_limit:
                    if pulse_ms < transient_max_ms:
                        kind = "transient"
                        cap_label = (
                            f"transient ceiling of +{transient_limit:.0f} dB "
                            f"(pulse width {pulse_ms:.1f} ms < "
                            f"{transient_max_ms:.0f} ms threshold)"
                        )
                    else:
                        kind = "sustained"
                        cap_label = (
                            f"thermal ceiling of +{thermal_limit:.0f} dB "
                            f"(pulse width {pulse_ms:.1f} ms ≥ "
                            f"{transient_max_ms:.0f} ms — sustained gain)"
                        )
                    raise SafetyValidationError(
                        f"SafetyValidator: FIR {kind} boost of "
                        f"+{peak_db:.1f} dB at {centre:.0f} Hz "
                        f"1/3-octave band exceeds {cap_label} "
                        f"(profile {prof.name!r}). Reduce the FIR's boost "
                        f"or shorten the pulse."
                    )

    def _check_hpf_present(self, filters: list[FilterSpec]) -> ValidationResult:
        """Verify a HPF at or below the profile's HPF frequency is present.

        Skipped entirely when the profile's ``hpf_freq_hz`` is None — mains,
        tweeters, and other non-ported transducers don't need an infrasonic
        HPF. Ported subs (SVS default) always do.
        """
        if self._profile.hpf_freq_hz is None:
            return ValidationResult.passed()
        ceiling = self._profile.hpf_freq_hz
        order = self._profile.hpf_order
        for f in filters:
            if f.type == "hpf" and f.freq <= ceiling:
                return ValidationResult.passed()
        return ValidationResult.failed(
            f"mandatory infrasonic HPF at ≤{ceiling:.0f} Hz is missing — "
            f"profile {self._profile.name!r} requires a {order}-order "
            f"Butterworth HPF at {ceiling:.0f} Hz or lower"
        )
