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
from typing import Literal


# ── Constants ──────────────────────────────────────────────────────────────────

MIN_BOOST_FREQ_HZ: float = 25.0
"""Minimum frequency at which a boost is permitted (ported sub port resonance protection)."""

MAX_BOOST_PER_BAND_DB: float = 6.0
"""Maximum boost allowed on any single EQ band."""

MAX_CUMULATIVE_BOOST_DB: float = 9.0
"""Maximum total boost allowed in any single 1/3-octave band."""

MAX_CHANGE_PER_ITER_DB: float = 3.0
"""Maximum increase in gain from the previous iteration on any band."""

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
    """Validates a list of FilterSpec objects against all hard safety limits.

    Instantiate once and reuse::

        validator = SafetyValidator()

    Call ``validate(filters)`` before writing to miniDSP.  Optionally pass
    ``previous_filters`` to enforce the per-iteration change limit.
    """

    def validate(
        self,
        filters: list[FilterSpec],
        previous_filters: list[FilterSpec] | None = None,
    ) -> ValidationResult:
        """Run all safety checks on *filters*.

        Checks (in order):
          1. Per-band boost frequency floor (≥ 25 Hz)
          2. Per-band boost ceiling (≤ +6 dB)
          3. Cumulative 1/3-octave boost ceiling (≤ +9 dB)
          4. Per-iteration change limit (≤ +3 dB increase vs previous_filters)
          5. HPF presence — mandatory 18 Hz 4th-order Butterworth

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
            result = self._check_per_iteration_change(filters, previous_filters)
            if not result.ok:
                return result

        return ValidationResult.passed()

    # ── Individual checks ──────────────────────────────────────────────────────

    def _check_boost_frequency_floor(
        self, filters: list[FilterSpec]
    ) -> ValidationResult:
        """Reject any boost below MIN_BOOST_FREQ_HZ."""
        for f in filters:
            if f.type == "hpf":
                continue
            if _is_boost(f) and f.freq < MIN_BOOST_FREQ_HZ:
                return ValidationResult.failed(
                    f"boost at {f.freq:.1f} Hz is below minimum boost frequency "
                    f"({MIN_BOOST_FREQ_HZ:.0f} Hz) — dangerous for a ported sub"
                )
        return ValidationResult.passed()

    def _check_per_band_boost_ceiling(
        self, filters: list[FilterSpec]
    ) -> ValidationResult:
        """Reject any single band with gain > MAX_BOOST_PER_BAND_DB."""
        for f in filters:
            if f.type == "hpf":
                continue
            if f.gain_db > MAX_BOOST_PER_BAND_DB:
                return ValidationResult.failed(
                    f"band at {f.freq:.1f} Hz requests +{f.gain_db:.1f} dB "
                    f"(max per-band boost is +{MAX_BOOST_PER_BAND_DB:.0f} dB)"
                )
        return ValidationResult.passed()

    def _check_cumulative_boost(
        self, filters: list[FilterSpec]
    ) -> ValidationResult:
        """Reject if any 1/3-octave band accumulates > MAX_CUMULATIVE_BOOST_DB."""
        # Accumulate boost by 1/3-octave centre
        cumulative: dict[float, float] = {}
        for f in filters:
            if f.type == "hpf" or not _is_boost(f):
                continue
            centre = _third_octave_for_freq(f.freq)
            cumulative[centre] = cumulative.get(centre, 0.0) + f.gain_db

        for centre, total in cumulative.items():
            if total > MAX_CUMULATIVE_BOOST_DB:
                return ValidationResult.failed(
                    f"cumulative boost in {centre:.0f} Hz 1/3-octave band is "
                    f"+{total:.1f} dB (max cumulative boost is "
                    f"+{MAX_CUMULATIVE_BOOST_DB:.0f} dB)"
                )
        return ValidationResult.passed()

    def _check_per_iteration_change(
        self,
        filters: list[FilterSpec],
        previous_filters: list[FilterSpec],
    ) -> ValidationResult:
        """Reject if any band increases by more than MAX_CHANGE_PER_ITER_DB vs previous.

        Matches filters by nearest 1/3-octave band (not exact frequency) to
        prevent drift-based bypasses where a correction algorithm shifts a
        filter from 50.0 Hz to 49.9 Hz to dodge the delta check.
        """
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
            if delta > MAX_CHANGE_PER_ITER_DB:
                return ValidationResult.failed(
                    f"band at {f.freq:.1f} Hz (1/3-octave: {centre:.0f} Hz) "
                    f"increases by +{delta:.1f} dB "
                    f"(previous: {prev_gain:+.1f} dB → proposed: {f.gain_db:+.1f} dB; "
                    f"max change per iteration is +{MAX_CHANGE_PER_ITER_DB:.0f} dB)"
                )
        return ValidationResult.passed()

    def _check_hpf_present(self, filters: list[FilterSpec]) -> ValidationResult:
        """Verify a HPF at or below HPF_FREQ_HZ is present in the filter list.

        The infrasonic HPF is mandatory and cannot be omitted.  It protects the
        subwoofer driver from large excursion at very low frequencies.
        """
        for f in filters:
            if f.type == "hpf" and f.freq <= HPF_FREQ_HZ:
                return ValidationResult.passed()
        return ValidationResult.failed(
            f"mandatory infrasonic HPF at ≤{HPF_FREQ_HZ:.0f} Hz is missing — "
            f"all filter sets must include a {HPF_ORDER}-order Butterworth HPF "
            f"at {HPF_FREQ_HZ:.0f} Hz or lower"
        )
