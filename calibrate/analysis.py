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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _median_spl(fr: FrequencyResponse) -> float:
    """Return the median SPL value from a FrequencyResponse."""
    if not fr.spl:
        return 0.0
    sorted_spl = sorted(fr.spl)
    n = len(sorted_spl)
    if n % 2 == 0:
        return (sorted_spl[n // 2 - 1] + sorted_spl[n // 2]) / 2
    return sorted_spl[n // 2]


def optimal_reference_spl(
    fr: FrequencyResponse,
    band: tuple[float, float] = (20.0, 200.0),
) -> float:
    """Find the reference SPL that minimizes boost needed to reach the Harman target.

    Instead of anchoring to the median (which often places the target above the
    room's low-frequency capability, requiring large boosts), this finds the
    reference level where the target curve sits at or below the measured response
    as much as possible — so corrections are mostly cuts.

    Strategy: sweep candidate reference levels and pick the one that minimizes
    total positive (boost) deviation while keeping total RMS reasonable.
    """
    if not fr.frequencies or not fr.spl:
        return 0.0

    freqs = np.array(fr.frequencies)
    spl = np.array(fr.spl)
    mask = (freqs >= band[0]) & (freqs <= band[1])
    freqs_band = freqs[mask]
    spl_band = spl[mask]

    if len(spl_band) == 0:
        return _median_spl(fr)

    # Build the Harman offset array for the band frequencies
    offsets = np.array([
        HarmanTarget(reference_spl=0.0, band=band).target_at(f)
        for f in freqs_band
    ])

    # The target at each freq = ref + offset.
    # Deviation = measured - target = spl_band - (ref + offsets)
    # Boost needed where deviation < 0: boost = max(0, target - measured)
    #
    # We want ref such that sum of boosts is minimized.
    # Optimal: ref = min(spl_band - offsets), which places the target at/below
    # the lowest point of the measured curve. But that may leave too much to cut.
    #
    # Compromise: use the 25th percentile of (spl - offsets). This keeps the
    # target mostly below the measured curve, with only small boosts needed
    # at the weakest frequencies.
    adjusted = spl_band - offsets
    return float(np.percentile(adjusted, 25))


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


def max_safe_reference_spl(
    fr: FrequencyResponse,
    band: tuple[float, float] = (20.0, 200.0),
    max_boost_db: float = 6.0,
) -> float:
    """Find the highest Harman reference level where no band needs more than max_boost_db.

    This maximizes bass extension while staying within the safety limit.
    For each frequency: boost_needed = (ref + harman_offset) - measured.
    Constraint: boost_needed <= max_boost_db for all frequencies.
    So: ref <= measured(f) - harman_offset(f) + max_boost_db for all f.
    Take the minimum across all frequencies.
    """
    if not fr.frequencies or not fr.spl:
        return 0.0

    freqs = np.array(fr.frequencies)
    spl = np.array(fr.spl)
    mask = (freqs >= band[0]) & (freqs <= band[1])
    freqs_band = freqs[mask]
    spl_band = spl[mask]

    if len(spl_band) == 0:
        return _median_spl(fr)

    offsets = np.array([
        HarmanTarget(reference_spl=0.0, band=band).target_at(f)
        for f in freqs_band
    ])

    # ref <= spl(f) - offset(f) + max_boost for all f
    # So ref = min(spl - offset + max_boost)
    return float(np.min(spl_band - offsets + max_boost_db))


def min_rms_reference_spl(
    fr: FrequencyResponse,
    band: tuple[float, float] = (20.0, 200.0),
    max_boost_db: float = 6.0,
    step_db: float = 0.5,
    max_drop_db: float = 10.0,
) -> float:
    """Find the reference SPL that minimizes RMS deviation from the Harman target.

    Starts at max_safe_reference_spl and steps downward. Lowering the reference
    trades absolute bass level for better curve shape tracking — bands that
    were maxed out on boost become reachable, and the overall RMS drops.

    Stops when RMS starts increasing again (diminishing returns — just cutting
    everything more without improving shape) or max_drop_db is reached.
    """
    ceiling = max_safe_reference_spl(fr, band, max_boost_db)

    best_ref = ceiling
    best_rms = rms_deviation(fr, HarmanTarget(reference_spl=ceiling, band=band), band)

    ref = ceiling - step_db
    floor = ceiling - max_drop_db
    while ref >= floor:
        target = HarmanTarget(reference_spl=ref, band=band)
        rms = rms_deviation(fr, target, band)
        if rms < best_rms:
            best_rms = rms
            best_ref = ref
        elif rms > best_rms + 0.5:
            # RMS rising — past the sweet spot
            break
        ref -= step_db

    return float(best_ref)


def per_freq_boost_effectiveness(
    fr: FrequencyResponse,
    t60_data: list[tuple[float, float]] | None = None,
    geometry_ranges: list[tuple[float, float]] | None = None,
    port_tune_hz: float = 28.0,
    band: tuple[float, float] = (20.0, 200.0),
) -> dict[float, float]:
    """Estimate boost correction effectiveness at each measurement frequency bin.

    Returns a dict mapping freq_hz → effectiveness in [0.0, 1.0] at the
    measurement's native resolution — not snapped to 1/3-octave centres.
    Modal peaks are a few Hz wide (Q > 5); 1/3-octave binning dilutes them.

    Effectiveness is the fraction of a requested boost that manifests at the
    listening position:

      - 0.0: physically impossible (geometry null, below port rolloff)
      - 0.2: deeply modal (T60 > 600 ms) — room ringing masks the added energy
      - 0.85: short T60 + high coherence — boost lands as expected

    t60_data: list of (freq_hz, t60_ms) pairs from analyze_decay. The decay
    analysis covers only modes above the detection threshold; frequencies with
    no nearby data default to moderate effectiveness (0.75).

    T60 is interpolated with inverse-log-distance weighting across all data
    points within ±half-octave (frequency ratio ≤ √2). This lets a sharp mode
    at 47 Hz suppress effectiveness at 46–48 Hz without polluting 40 Hz.
    """
    geometry_ranges = geometry_ranges or []
    t60_data = t60_data or []

    if not fr.frequencies:
        return {}

    freq_arr = np.array(fr.frequencies)
    coh_arr = (
        np.array(fr.coherence)
        if fr.coherence and len(fr.coherence) == len(fr.frequencies)
        else np.full(len(fr.frequencies), 0.8)
    )

    # Pre-sort T60 data by frequency for fast windowed lookup
    t60_sorted: list[tuple[float, float]] = sorted(t60_data, key=lambda x: x[0])
    t60_freqs = np.array([f for f, _ in t60_sorted]) if t60_sorted else np.array([])
    t60_vals = np.array([t for _, t in t60_sorted]) if t60_sorted else np.array([])

    result: dict[float, float] = {}
    for i, freq in enumerate(freq_arr):
        if not (band[0] <= freq <= band[1]):
            continue

        if freq < port_tune_hz:
            result[float(freq)] = 0.0
            continue
        if any(lo <= freq < hi for lo, hi in geometry_ranges):
            result[float(freq)] = 0.0
            continue

        # T60 estimate: inverse-log-distance weighted average of data points
        # within ±half-octave (ratio ≤ √2 ≈ 1.414).
        t60_ms: float | None = None
        if len(t60_freqs) > 0:
            log_dists = np.abs(np.log(t60_freqs / max(freq, 1e-6)))
            half_oct = math.log(2.0) / 2.0  # log(√2)
            mask = log_dists <= half_oct
            if mask.any():
                weights = 1.0 / (log_dists[mask] + 1e-6)
                t60_ms = float(np.average(t60_vals[mask], weights=weights))

        if t60_ms is None:
            base = 0.75
        elif t60_ms > 600.0:
            base = 0.20
        elif t60_ms > 400.0:
            base = 0.35
        elif t60_ms > 200.0:
            base = 0.55
        else:
            base = 0.85

        # Coherence scaling: low coherence = measurement unreliable = boost
        # may not be real. Clamp to [0.2, 1.0] so noisy bins don't zero out.
        coh = float(coh_arr[i])
        coherence_scale = max(0.2, min(1.0, coh))

        result[float(freq)] = round(base * coherence_scale, 3)

    return result


def optimal_anchor_reference_spl(
    fr: FrequencyResponse,
    target_offsets: list[dict],
    effectiveness: dict[float, float],
    band: tuple[float, float] = (20.0, 200.0),
    max_boost_db: float = 6.0,
    null_threshold_db: float = 15.0,
    headroom_lambda: float = 0.3,
    sweep_step_db: float = 0.25,
) -> tuple[float, float, list[dict]]:
    """Find the reference_spl that maximises achievable compliance across the band.

    Operates at the measurement's native frequency resolution (dense grid from
    the FR object), not snapped to 1/3-octave. The effectiveness map must match
    — use per_freq_boost_effectiveness() to build it.

    For each candidate reference_spl, scores the expected correction quality
    per frequency bin:

      - Cut needed: residual = 0 (always achievable)
      - Boost needed B with effectiveness e: residual = B × (1 − e)
      - Boost exceeding max_boost_db: capped correction + uncapped residual

    Minimises mean squared residual with a λ-weighted penalty for the gain
    increase implied by setting reference_spl above the balanced (all-boost) floor.

    headroom_lambda: penalty per dB above balanced anchor. Higher = prefer cuts.

    Returns:
        (reference_spl, score, per_band_breakdown)
        per_band_breakdown is at 1/3-octave resolution for readability — the
        scoring uses the full grid internally.
    """
    if not fr.frequencies or not fr.spl:
        return 0.0, 0.0, []

    offsets_sorted = sorted(target_offsets, key=lambda p: p["freq_hz"])
    off_freqs = [p["freq_hz"] for p in offsets_sorted]
    off_vals = [p["offset_db"] for p in offsets_sorted]

    def _interp_offset(freq_hz: float) -> float | None:
        if freq_hz < off_freqs[0] or freq_hz > off_freqs[-1]:
            return None
        for i in range(len(off_freqs) - 1):
            f0, v0 = off_freqs[i], off_vals[i]
            f1, v1 = off_freqs[i + 1], off_vals[i + 1]
            if f0 <= freq_hz <= f1:
                t = math.log(freq_hz / f0) / math.log(f1 / f0) if f1 != f0 else 0.0
                return v0 + t * (v1 - v0)
        return off_vals[-1]

    freqs_arr = np.array(fr.frequencies)
    spl_arr = np.array(fr.spl)
    band_mask = (freqs_arr >= band[0]) & (freqs_arr <= band[1])
    band_avg = float(np.mean(spl_arr[band_mask])) if band_mask.any() else 0.0

    # Build dense valid points from native measurement grid
    valid_points: list[tuple[float, float, float, float]] = []
    for i, freq in enumerate(freqs_arr):
        if not (band[0] <= freq <= band[1]):
            continue
        offset = _interp_offset(float(freq))
        if offset is None:
            continue
        measured = float(spl_arr[i])
        if measured < band_avg - null_threshold_db:
            continue
        eff = effectiveness.get(float(freq), 0.75)
        if eff <= 0.0:
            continue
        valid_points.append((float(freq), measured, offset, eff))

    if not valid_points:
        return 0.0, 0.0, []

    headrooms = [m - o for _, m, o, _ in valid_points]
    ref_min = min(headrooms) + max_boost_db
    ref_max = max(headrooms)

    # Vectorised scoring over the full dense grid
    vf = np.array([p[0] for p in valid_points])
    vm = np.array([p[1] for p in valid_points])
    vo = np.array([p[2] for p in valid_points])
    ve = np.array([p[3] for p in valid_points])

    def _score_candidate(ref: float) -> float:
        correction = (ref + vo) - vm        # positive = boost, negative = cut
        boost = np.clip(correction, 0.0, max_boost_db)
        residual = boost * (1.0 - ve)
        # Uncorrectable portion beyond safety cap
        over_cap = np.maximum(correction - max_boost_db, 0.0)
        residual += over_cap
        mean_sq = float(np.mean(residual ** 2))
        gain_penalty = headroom_lambda * max(0.0, ref - ref_min)
        return -(mean_sq + gain_penalty)

    best_ref = ref_min
    best_score = _score_candidate(ref_min)
    ref = ref_min + sweep_step_db
    while ref <= ref_max + sweep_step_db:
        s = _score_candidate(ref)
        if s > best_score:
            best_score = s
            best_ref = ref
        ref += sweep_step_db

    # Build 1/3-octave breakdown at the chosen anchor for readability
    breakdown: list[dict] = []
    for centre in THIRD_OCTAVE_CENTRES_HZ:
        if not (band[0] <= centre <= band[1]):
            continue
        offset = _interp_offset(centre)
        if offset is None:
            continue
        idx = int(np.argmin(np.abs(freqs_arr - centre)))
        measured = float(spl_arr[idx])
        eff = effectiveness.get(float(freqs_arr[idx]), 0.75)
        correction = (best_ref + offset) - measured
        if correction <= 0.0:
            strategy = "cut"
            residual = 0.0
        else:
            boost = min(correction, max_boost_db)
            residual = boost * (1.0 - eff)
            if correction > max_boost_db:
                residual += correction - max_boost_db
            strategy = "boost"
        breakdown.append({
            "freq_hz": centre,
            "correction_db": round(correction, 2),
            "strategy": strategy,
            "effectiveness": round(eff, 3),
            "residual_db": round(residual, 2),
        })

    return round(best_ref, 2), round(best_score, 4), breakdown

    best_ref = ref_min
    best_score, best_breakdown = _score_candidate(ref_min)

    ref = ref_min + sweep_step_db
    while ref <= ref_max + sweep_step_db:
        score, breakdown = _score_candidate(ref)
        if score > best_score:
            best_score = score
            best_ref = ref
            best_breakdown = breakdown
        ref += sweep_step_db

    return round(best_ref, 2), round(best_score, 4), best_breakdown


def harman_rms(
    fr: FrequencyResponse,
    band: tuple[float, float] = (20.0, 200.0),
    anchor: str = "min_rms",
) -> float:
    """Compute RMS deviation from Harman target for a measurement.

    Standalone helper — creates a HarmanTarget and returns the RMS deviation
    within *band*. Useful for computing a single "how close to Harman" number
    for any stored measurement.

    anchor:
        "min_rms"  — minimize RMS deviation (best curve fit). Default.
        "max_safe" — max safe extension (highest ref where no band needs >6dB boost).
        "median"   — anchor to median SPL. Legacy behavior.
    """
    if anchor == "min_rms":
        ref = min_rms_reference_spl(fr, band)
    elif anchor == "max_safe":
        ref = max_safe_reference_spl(fr, band)
    else:
        ref = _median_spl(fr)
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
