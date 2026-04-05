"""Loop orchestrator — headless calibration state machine.

Runs the full calibration loop: measure -> propose corrections -> validate ->
apply EQ -> re-measure -> check convergence. Designed to run headless
("plug in the Pi, calibrate overnight, wake up to results").

The LLM (or mock backend) is one function call with a clean interface.
SafetyValidator enforces hard limits regardless of what the backend proposes.
The orchestrator always injects the mandatory HPF (defense in depth).

Usage::

    from calibrate.loop import LoopOrchestrator

    orchestrator = LoopOrchestrator(minidsp_driver, measurement_engine, config)
    result = await orchestrator.run(recipe, preset=0)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .analysis import HarmanTarget, make_flat_target, propose_corrections, rms_deviation
from .drivers.minidsp import MinidspDriver
from .measurement import FrequencyResponse, MeasurementEngine
from .recipe import Recipe
from .safety import (
    HPF_FREQ_HZ,
    FilterSpec,
    SafetyValidator,
    _third_octave_for_freq,
)
from .storage import SessionStore

log = logging.getLogger(__name__)

# Mandatory HPF — always injected by orchestrator, even if the correction
# backend omits it. Belt (orchestrator injects) and suspenders (SafetyValidator checks).
_MANDATORY_HPF = FilterSpec(freq=HPF_FREQ_HZ, gain_db=0.0, q=0.707, type="hpf")


class LoopError(RuntimeError):
    """Raised when the calibration loop cannot proceed."""


@dataclass
class IterationResult:
    """Result of a single loop iteration."""

    iteration: int
    rms_before: float
    rms_after: float
    filters_proposed: list[FilterSpec]
    filters_applied: list[FilterSpec]
    safety_ok: bool
    safety_error: str = ""


@dataclass
class LoopResult:
    """Result of the full calibration loop."""

    converged: bool
    iterations_run: int
    final_rms: float
    baseline_rms: float
    iteration_results: list[IterationResult] = field(default_factory=list)
    rollback_snapshot: list[dict] | None = None
    error: str = ""


class LoopOrchestrator:
    """Headless calibration loop state machine.

    Coordinates measurement, analysis, safety validation, and EQ application.
    Designed to be fully testable with mocked drivers and measurement engine.
    """

    def __init__(
        self,
        minidsp: MinidspDriver,
        measurement_engine: MeasurementEngine | None = None,
        hardware_profile: dict[str, Any] | None = None,
        store: SessionStore | None = None,
    ) -> None:
        self._minidsp = minidsp
        self._measurement = measurement_engine
        self._hardware_profile = hardware_profile or _default_hardware_profile()
        self._validator = SafetyValidator()
        self._store = store
        self._lock = asyncio.Lock()
        self._running = False

    async def run(
        self,
        recipe: Recipe,
        preset: int = 0,
        fresh: bool = False,
        measure_fn: Any | None = None,
    ) -> LoopResult:
        """Run the calibration loop.

        Args:
            recipe: Calibration strategy (target, convergence, analysis backend).
            preset: miniDSP preset index to operate on.
            fresh: If True, allow starting from empty EQ state.
            measure_fn: Optional override for measurement (for testing).
                        Async callable returning FrequencyResponse.
        """
        if not self._lock.locked():
            async with self._lock:
                return await self._run_locked(recipe, preset, fresh, measure_fn)
        else:
            raise LoopError("calibration loop is already running")

    async def _run_locked(
        self,
        recipe: Recipe,
        preset: int,
        fresh: bool,
        measure_fn: Any | None,
    ) -> LoopResult:
        self._running = True
        try:
            return await self._execute_loop(recipe, preset, fresh, measure_fn)
        finally:
            self._running = False

    async def _execute_loop(
        self,
        recipe: Recipe,
        preset: int,
        fresh: bool,
        measure_fn: Any | None,
    ) -> LoopResult:
        # Step 1: Snapshot current EQ state for rollback
        snapshot = await self._minidsp.read_eq(preset)
        if not snapshot and not fresh:
            raise LoopError(
                "Cannot snapshot empty EQ state — miniDSP driver has no EQ history. "
                "Either apply EQ at least once, or use fresh=True to start from scratch."
            )

        log.info("Loop starting: recipe=%s, preset=%d, fresh=%s", recipe.name, preset, fresh)

        # Persist run start
        run_id = self._persist_run_start(recipe)

        # Step 2: Baseline measurement
        baseline = await self._measure_with_retry(recipe, measure_fn)
        reference_spl = float(median_spl(baseline))

        # Step 3: Build target curve
        if recipe.target == "harman":
            target = HarmanTarget(reference_spl=reference_spl, band=recipe.band)
        elif recipe.target == "flat":
            target = make_flat_target(reference_spl, band=recipe.band)
        else:
            raise LoopError(f"unknown target: {recipe.target!r}")

        baseline_rms = rms_deviation(baseline, target, recipe.band)
        log.info("Baseline RMS deviation: %.1f dB (threshold: %.1f dB)",
                 baseline_rms, recipe.convergence.threshold_db)

        # Check if already converged
        if baseline_rms <= recipe.convergence.threshold_db:
            log.info("Already converged at baseline, no corrections needed")
            self._persist_run_end(run_id, True, 0, baseline_rms, baseline_rms)
            return LoopResult(
                converged=True,
                iterations_run=0,
                final_rms=baseline_rms,
                baseline_rms=baseline_rms,
                rollback_snapshot=snapshot,
            )

        # Step 4: EQ iteration loop
        current_eq: list[FilterSpec] = _snapshot_to_specs(snapshot)
        current_rms = baseline_rms
        iteration_results: list[IterationResult] = []

        for iteration in range(1, recipe.convergence.max_iterations + 1):
            log.info("=== Iteration %d/%d (current RMS: %.1f dB) ===",
                     iteration, recipe.convergence.max_iterations, current_rms)

            # Measure (or use baseline for iteration 1)
            if iteration == 1:
                fr = baseline
            else:
                fr = await self._measure_with_retry(recipe, measure_fn)

            rms_before = rms_deviation(fr, target, recipe.band)

            # Propose corrections
            try:
                proposed = await propose_corrections(
                    measurement=fr,
                    target=target,
                    current_eq=current_eq,
                    hardware_profile=self._hardware_profile,
                    recipe=recipe,
                    iteration=iteration,
                )
            except Exception as exc:
                log.error("Correction proposal failed: %s", exc)
                return await self._rollback_and_return(
                    preset, snapshot, iteration, current_rms, baseline_rms,
                    iteration_results, f"proposal failed: {exc}",
                    run_id=run_id,
                )

            # Inject mandatory HPF (defense in depth)
            filters_with_hpf = _inject_hpf(proposed)

            # Validate
            result = self._validator.validate(filters_with_hpf, current_eq or None)
            if not result.ok:
                log.warning("SafetyValidator rejected iteration %d: %s", iteration, result.error)
                iter_result = IterationResult(
                    iteration=iteration,
                    rms_before=rms_before,
                    rms_after=rms_before,
                    filters_proposed=proposed,
                    filters_applied=[],
                    safety_ok=False,
                    safety_error=result.error,
                )
                iteration_results.append(iter_result)
                self._persist_iteration(run_id, iter_result)
                # Don't rollback on safety rejection — just skip this iteration
                # and try again with the same measurement
                continue

            # Apply EQ
            try:
                filter_dicts = [
                    {"freq": f.freq, "gain_db": f.gain_db, "q": f.q, "type": f.type}
                    for f in filters_with_hpf
                ]
                await self._minidsp.apply_eq(preset, filter_dicts)
            except Exception as exc:
                log.error("apply_eq failed: %s", exc)
                return await self._rollback_and_return(
                    preset, snapshot, iteration, current_rms, baseline_rms,
                    iteration_results, f"apply_eq failed: {exc}",
                    run_id=run_id,
                )

            current_eq = filters_with_hpf

            # Re-measure to check convergence
            try:
                post_measure = await self._measure_with_retry(recipe, measure_fn)
            except LoopError:
                return await self._rollback_and_return(
                    preset, snapshot, iteration, current_rms, baseline_rms,
                    iteration_results, "measurement failed after retries",
                    run_id=run_id,
                )

            rms_after = rms_deviation(post_measure, target, recipe.band)
            current_rms = rms_after

            iter_result = IterationResult(
                iteration=iteration,
                rms_before=rms_before,
                rms_after=rms_after,
                filters_proposed=proposed,
                filters_applied=filters_with_hpf,
                safety_ok=True,
            )
            iteration_results.append(iter_result)
            self._persist_iteration(run_id, iter_result)

            log.info("Iteration %d: RMS %.1f -> %.1f dB", iteration, rms_before, rms_after)

            if rms_after <= recipe.convergence.threshold_db:
                log.info("Converged after %d iterations (RMS: %.1f dB)", iteration, rms_after)
                self._persist_run_end(run_id, True, iteration, baseline_rms, rms_after)
                return LoopResult(
                    converged=True,
                    iterations_run=iteration,
                    final_rms=rms_after,
                    baseline_rms=baseline_rms,
                    iteration_results=iteration_results,
                    rollback_snapshot=snapshot,
                )

        # Max iterations reached
        log.info("Max iterations reached. Final RMS: %.1f dB", current_rms)
        self._persist_run_end(
            run_id, False, recipe.convergence.max_iterations,
            baseline_rms, current_rms,
        )
        return LoopResult(
            converged=False,
            iterations_run=recipe.convergence.max_iterations,
            final_rms=current_rms,
            baseline_rms=baseline_rms,
            iteration_results=iteration_results,
            rollback_snapshot=snapshot,
        )

    async def _measure_with_retry(
        self,
        recipe: Recipe,
        measure_fn: Any | None,
    ) -> FrequencyResponse:
        """Measure with circuit breaker: retry_count retries with delay."""
        retries = recipe.measurement.retry_count
        delay = recipe.measurement.retry_delay_s

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                if measure_fn is not None:
                    return await measure_fn()
                if self._measurement is not None:
                    return await self._measurement.measure()
                raise LoopError("no measurement engine or measure_fn provided")
            except LoopError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < retries:
                    log.warning("Measurement attempt %d failed: %s (retrying in %.0fs)",
                                attempt + 1, exc, delay)
                    await asyncio.sleep(delay)

        raise LoopError(
            f"measurement failed after {retries + 1} attempts: {last_error}"
        )

    async def _rollback_and_return(
        self,
        preset: int,
        snapshot: list[dict],
        iteration: int,
        current_rms: float,
        baseline_rms: float,
        iteration_results: list[IterationResult],
        error: str,
        run_id: int | None = None,
    ) -> LoopResult:
        """Rollback to snapshot and return error result."""
        if snapshot:
            log.info("Rolling back to pre-loop EQ state")
            try:
                await self._minidsp.apply_eq(preset, snapshot)
            except Exception as rollback_exc:
                log.error("Rollback failed: %s", rollback_exc)
                error += f" (rollback also failed: {rollback_exc})"
        self._persist_run_end(run_id, False, iteration, baseline_rms, current_rms, error)
        return LoopResult(
            converged=False,
            iterations_run=iteration,
            final_rms=current_rms,
            baseline_rms=baseline_rms,
            iteration_results=iteration_results,
            rollback_snapshot=snapshot,
            error=error,
        )

    # ── Persistence helpers (non-critical, never abort calibration) ──────────

    def _persist_run_start(self, recipe: Recipe) -> int | None:
        """Save a new calibration run to the store. Returns run_id or None."""
        if self._store is None:
            return None
        try:
            return self._store.save_run(recipe.name, recipe.target)
        except Exception as exc:
            log.warning("Failed to persist run start: %s", exc)
            return None

    def _persist_run_end(
        self,
        run_id: int | None,
        converged: bool,
        iterations_run: int,
        baseline_rms: float,
        final_rms: float,
        error: str = "",
    ) -> None:
        """Update a calibration run with final results."""
        if self._store is None or run_id is None:
            return
        try:
            self._store.update_run(
                run_id, converged=converged, iterations_run=iterations_run,
                baseline_rms=baseline_rms, final_rms=final_rms, error=error,
            )
        except Exception as exc:
            log.warning("Failed to persist run end: %s", exc)

    def _persist_iteration(self, run_id: int | None, ir: IterationResult) -> None:
        """Save one iteration result to the store."""
        if self._store is None or run_id is None:
            return
        try:
            self._store.save_iteration(
                run_id=run_id,
                iteration=ir.iteration,
                rms_before=ir.rms_before,
                rms_after=ir.rms_after,
                filters_proposed=[
                    {"freq": f.freq, "gain_db": f.gain_db, "q": f.q, "type": f.type}
                    for f in ir.filters_proposed
                ],
                filters_applied=[
                    {"freq": f.freq, "gain_db": f.gain_db, "q": f.q, "type": f.type}
                    for f in ir.filters_applied
                ],
                safety_ok=ir.safety_ok,
                safety_error=ir.safety_error,
            )
        except Exception as exc:
            log.warning("Failed to persist iteration %d: %s", ir.iteration, exc)

    async def rollback(self, preset: int, snapshot: list[dict]) -> None:
        """Manually rollback to a saved EQ snapshot."""
        if not snapshot:
            raise LoopError("empty snapshot, nothing to rollback to")
        await self._minidsp.apply_eq(preset, snapshot)
        log.info("Rolled back to snapshot (%d filters)", len(snapshot))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _inject_hpf(filters: list[FilterSpec]) -> list[FilterSpec]:
    """Ensure the mandatory HPF is present in the filter list.

    If a HPF at or below HPF_FREQ_HZ already exists, keep it.
    Otherwise, prepend the mandatory HPF.
    """
    has_hpf = any(f.type == "hpf" and f.freq <= HPF_FREQ_HZ for f in filters)
    if has_hpf:
        return filters
    return [_MANDATORY_HPF] + filters


def _snapshot_to_specs(snapshot: list[dict]) -> list[FilterSpec]:
    """Convert a snapshot (list of dicts) to list of FilterSpec."""
    return [
        FilterSpec(
            freq=float(f["freq"]),
            gain_db=float(f["gain_db"]),
            q=float(f.get("q", 0.707)),
            type=f["type"],
        )
        for f in snapshot
    ]


def median_spl(fr: FrequencyResponse) -> float:
    """Return the median SPL value from a FrequencyResponse."""
    if not fr.spl:
        return 0.0
    sorted_spl = sorted(fr.spl)
    n = len(sorted_spl)
    if n % 2 == 0:
        return (sorted_spl[n // 2 - 1] + sorted_spl[n // 2]) / 2
    return sorted_spl[n // 2]


def _default_hardware_profile() -> dict[str, Any]:
    """Default hardware profile for SVS PB12-NSD + miniDSP 2x4 HD."""
    return {
        "device": "miniDSP 2x4 HD",
        "available_peq_slots": 7,  # 8 hardware slots, 1 reserved for mandatory HPF
        "sub_tuning_hz": 22,
        "safety": {
            "min_boost_freq_hz": 25,
            "max_boost_per_band_db": 6,
            "max_cumulative_boost_db": 9,
            "max_change_per_iteration_db": 3,
            "mandatory_hpf": "18 Hz 4th-order Butterworth",
        },
    }
