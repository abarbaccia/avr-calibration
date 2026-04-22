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

    Limits come from a ``TransducerProfile`` — the default matches the legacy
    SVS PB12-NSD ported-sub constants, so existing callers that do
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
