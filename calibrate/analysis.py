"""Analysis module — correction proposals, target curves, and convergence math.

The core intelligence layer between measurement and EQ application.
``propose_corrections()`` dispatches to the configured backend:
  - ``claude`` — Claude API with structured JSON output (default)
  - ``mock`` — deterministic peak/dip finder for CI testing

The Harman target curve and RMS deviation math are backend-independent.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .measurement import FrequencyResponse
from .recipe import Recipe
from .safety import FilterSpec, THIRD_OCTAVE_CENTRES_HZ, _third_octave_for_freq

log = logging.getLogger(__name__)


# ── Harman target curve ───────────────────────────────────────────────────────

# Harman bass target: relative dB offsets at 1/3-octave centres.
# +3 dB/octave slope below 80 Hz, flat above 80 Hz, slight rolloff above 125 Hz.
# Source: Harman research (Sean Olive et al.)
_HARMAN_BASS_TABLE: dict[float, float] = {
    20.0: +6.0,
    25.0: +5.0,
    31.5: +4.0,
    40.0: +3.0,
    50.0: +2.0,
    63.0: +1.0,
    80.0: 0.0,
    100.0: 0.0,
    125.0: 0.0,
    160.0: -1.0,
    200.0: -2.0,
}


@dataclass
class HarmanTarget:
    """Harman bass target curve, anchored to a reference SPL.

    The reference SPL is the median SPL of the baseline measurement, pinned
    once at loop start and reused across all iterations.
    """

    reference_spl: float
    band: tuple[float, float] = (20.0, 200.0)

    def target_at(self, freq_hz: float) -> float:
        """Return the target SPL at a given frequency.

        Uses linear interpolation in log-frequency space between the
        Harman bass table entries.
        """
        freqs = sorted(_HARMAN_BASS_TABLE.keys())
        offsets = [_HARMAN_BASS_TABLE[f] for f in freqs]

        if freq_hz <= freqs[0]:
            return self.reference_spl + offsets[0]
        if freq_hz >= freqs[-1]:
            return self.reference_spl + offsets[-1]

        # Log-space interpolation
        log_freq = math.log(freq_hz)
        for i in range(len(freqs) - 1):
            log_lo = math.log(freqs[i])
            log_hi = math.log(freqs[i + 1])
            if log_lo <= log_freq <= log_hi:
                t = (log_freq - log_lo) / (log_hi - log_lo)
                offset = offsets[i] + t * (offsets[i + 1] - offsets[i])
                return self.reference_spl + offset

        return self.reference_spl  # fallback

    def target_array(self, frequencies: list[float]) -> np.ndarray:
        """Return target SPL values for a list of frequencies."""
        return np.array([self.target_at(f) for f in frequencies])


def make_flat_target(reference_spl: float, band: tuple[float, float] = (20.0, 200.0)) -> HarmanTarget:
    """Create a flat target (0 dB offset everywhere). Uses HarmanTarget with zeroed table."""
    # For flat, we just return reference_spl at every frequency.
    # We reuse HarmanTarget but override target_at to return reference_spl.
    target = HarmanTarget(reference_spl=reference_spl, band=band)
    # Monkey-patch for flat — cleaner to subclass but this is simpler for now
    target.target_at = lambda freq_hz: reference_spl  # type: ignore[assignment]
    target.target_array = lambda frequencies: np.full(len(frequencies), reference_spl)  # type: ignore[assignment]
    return target


# ── Convergence math ──────────────────────────────────────────────────────────

def rms_deviation(
    fr: FrequencyResponse,
    target: HarmanTarget,
    band: tuple[float, float],
) -> float:
    """Compute RMS deviation between measurement and target within *band*.

    Only considers frequency bins within [band[0], band[1]] Hz.
    Returns the RMS of (measured_spl - target_spl) in dB.
    """
    freqs = np.array(fr.frequencies)
    spl = np.array(fr.spl)

    mask = (freqs >= band[0]) & (freqs <= band[1])
    freqs_in_band = freqs[mask]
    spl_in_band = spl[mask]

    if len(freqs_in_band) == 0:
        return 0.0

    target_spl = target.target_array(freqs_in_band.tolist())
    deviation = spl_in_band - target_spl

    return float(np.sqrt(np.mean(deviation ** 2)))


def per_band_deviation(
    fr: FrequencyResponse,
    target: HarmanTarget,
    band: tuple[float, float],
) -> list[dict[str, float]]:
    """Compute per-frequency deviation for the LLM context.

    Returns a list of {freq_hz, measured_db, target_db, deviation_db} dicts
    at 1/3-octave centres within the band.
    """
    result = []
    for centre in THIRD_OCTAVE_CENTRES_HZ:
        if centre < band[0] or centre > band[1]:
            continue

        # Find closest measurement bin
        freqs = np.array(fr.frequencies)
        idx = int(np.argmin(np.abs(freqs - centre)))
        measured = float(fr.spl[idx])
        target_db = target.target_at(centre)

        result.append({
            "freq_hz": centre,
            "measured_db": round(measured, 1),
            "target_db": round(target_db, 1),
            "deviation_db": round(measured - target_db, 1),
        })

    return result


def harman_rms(
    fr: FrequencyResponse,
    band: tuple[float, float] = (20.0, 200.0),
) -> float:
    """Compute RMS deviation from Harman target for a measurement.

    Standalone helper — creates a HarmanTarget anchored to the FR's median SPL
    and returns the RMS deviation within *band*. Useful for computing a single
    "how close to Harman" number for any stored measurement.
    """
    from .loop import median_spl

    ref = median_spl(fr)
    target = HarmanTarget(reference_spl=ref, band=band)
    return rms_deviation(fr, target, band)


# ── Correction proposals ──────────────────────────────────────────────────────

async def propose_corrections(
    measurement: FrequencyResponse,
    target: HarmanTarget,
    current_eq: list[FilterSpec],
    hardware_profile: dict[str, Any],
    recipe: Recipe,
    iteration: int = 1,
) -> list[FilterSpec]:
    """Propose EQ corrections based on measurement vs target.

    Dispatches to the backend specified in the recipe:
      - ``claude`` — Claude API (default)
      - ``mock`` — deterministic peak/dip algorithm (CI)
    """
    if recipe.analysis == "mock":
        return _propose_mock(measurement, target, current_eq, hardware_profile, recipe)

    if recipe.analysis == "claude":
        try:
            return await _propose_claude(
                measurement, target, current_eq, hardware_profile, recipe, iteration
            )
        except Exception as exc:
            log.warning("Claude API failed (%s), falling back to mock backend", exc)
            return _propose_mock(measurement, target, current_eq, hardware_profile, recipe)

    raise ValueError(f"unknown analysis backend: {recipe.analysis!r}")


# ── Mock backend (deterministic, for CI) ──────────────────────────────────────

def _propose_mock(
    measurement: FrequencyResponse,
    target: HarmanTarget,
    current_eq: list[FilterSpec],
    hardware_profile: dict[str, Any],
    recipe: Recipe,
) -> list[FilterSpec]:
    """Deterministic peak/dip correction algorithm.

    Finds the largest deviations from target at 1/3-octave centres within
    the recipe band, and proposes opposing PEQ filters. Caps boosts at the
    safety limits. Prioritises by deviation magnitude.
    """
    from .safety import MAX_BOOST_PER_BAND_DB, MAX_CHANGE_PER_ITER_DB, MIN_BOOST_FREQ_HZ

    available_slots = hardware_profile.get("available_peq_slots", 8)
    deviations = per_band_deviation(measurement, target, recipe.band)

    # Sort by absolute deviation, largest first
    deviations.sort(key=lambda d: abs(d["deviation_db"]), reverse=True)

    # Threshold: only correct deviations > 1 dB
    threshold_db = 1.0

    filters: list[FilterSpec] = []
    for dev in deviations:
        if len(filters) >= available_slots:
            break

        deviation = dev["deviation_db"]  # positive = too loud, negative = too quiet
        if abs(deviation) < threshold_db:
            continue

        # Correction is the opposite of deviation
        correction = -deviation

        # Cap boosts
        if correction > 0:
            if dev["freq_hz"] < MIN_BOOST_FREQ_HZ:
                continue  # don't boost below minimum frequency
            correction = min(correction, MAX_BOOST_PER_BAND_DB)
            correction = min(correction, MAX_CHANGE_PER_ITER_DB)

        # Q: wider for low frequencies, narrower for high
        q = 1.0 if dev["freq_hz"] < 60 else 1.5

        filters.append(FilterSpec(
            freq=dev["freq_hz"],
            gain_db=round(correction, 1),
            q=q,
            type="peaking",
        ))

    return filters


# ── Claude API backend ────────────────────────────────────────────────────────

async def _propose_claude(
    measurement: FrequencyResponse,
    target: HarmanTarget,
    current_eq: list[FilterSpec],
    hardware_profile: dict[str, Any],
    recipe: Recipe,
    iteration: int,
) -> list[FilterSpec]:
    """Call Claude API to propose EQ corrections.

    Uses structured JSON output via tool_use. Falls back to mock on any error
    (caller handles the fallback).
    """
    import anthropic

    deviations = per_band_deviation(measurement, target, recipe.band)
    available_slots = hardware_profile.get("available_peq_slots", 8)
    safety_limits = hardware_profile.get("safety", {})

    system_prompt = (
        "You are an expert audio EQ engineer specializing in subwoofer calibration. "
        "You are given a frequency response measurement, its deviation from a target curve, "
        "the current EQ state, and hardware constraints. Propose parametric EQ filter corrections "
        "to bring the measurement closer to the target.\n\n"
        "Rules:\n"
        "- Only propose peaking filters (type: 'peaking')\n"
        "- Prefer cuts over boosts (cuts are always safe)\n"
        "- Respect the safety limits provided\n"
        "- Use Q values between 0.5 and 8.0\n"
        "- Focus corrections on the largest deviations first\n"
        "- Consider that deep nulls (room modes) cannot be fixed with EQ boost\n"
        f"- This is iteration {iteration} — be conservative, max +3 dB change per band\n"
        "- Return ONLY a JSON array of filter objects, no explanation"
    )

    user_message = json.dumps({
        "deviation_from_target": deviations,
        "current_eq": [
            {"freq": f.freq, "gain_db": f.gain_db, "q": f.q, "type": f.type}
            for f in current_eq
        ],
        "hardware": {
            "available_peq_slots": available_slots,
            "safety_limits": safety_limits,
            "sub_tuning_hz": hardware_profile.get("sub_tuning_hz", 22),
        },
        "recipe": {
            "target": recipe.target,
            "band": list(recipe.band),
            "iteration": iteration,
            "max_iterations": recipe.convergence.max_iterations,
        },
    }, indent=2)

    client = anthropic.AsyncAnthropic()

    response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    # Parse the response text as JSON
    text = response.content[0].text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if lines[-1].startswith("```") else "\n".join(lines[1:])

    filters_raw = json.loads(text)
    if not isinstance(filters_raw, list):
        raise ValueError(f"expected JSON array, got {type(filters_raw).__name__}")

    filters = []
    for f in filters_raw:
        filters.append(FilterSpec(
            freq=float(f["freq"]),
            gain_db=float(f["gain_db"]),
            q=float(f.get("q", 1.0)),
            type=f.get("type", "peaking"),
        ))

    log.info(
        "Claude proposed %d filters (iteration %d): %s",
        len(filters), iteration,
        ", ".join(f"{f.freq:.0f}Hz/{f.gain_db:+.1f}dB" for f in filters),
    )

    return filters
