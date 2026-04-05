"""Tests for the calibration loop orchestrator.

All tests use the mock analysis backend and synthetic FrequencyResponse data.
No real hardware, no Claude API calls, no PortAudio.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from calibrate.analysis import HarmanTarget
from calibrate.loop import LoopError, LoopOrchestrator, _inject_hpf, median_spl
from calibrate.measurement import FrequencyResponse
from calibrate.recipe import ConvergenceCriteria, MeasurementConfig, Recipe
from calibrate.safety import FilterSpec, HPF_FREQ_HZ


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_recipe(max_iterations: int = 5, analysis: str = "mock") -> Recipe:
    return Recipe(
        name="test-recipe",
        target="harman",
        band=(20.0, 200.0),
        convergence=ConvergenceCriteria(
            threshold_db=2.0,
            max_iterations=max_iterations,
        ),
        analysis=analysis,
        measurement=MeasurementConfig(retry_count=1, retry_delay_s=0.01),
    )


def _make_fr(spl_value: float = 75.0, n_points: int = 50) -> FrequencyResponse:
    """Synthetic flat FR at spl_value."""
    freqs = np.logspace(np.log10(20), np.log10(200), n_points).tolist()
    return FrequencyResponse(
        frequencies=freqs,
        spl=[spl_value] * n_points,
        sample_rate=96000,
        sweep_duration=3.0,
        timestamp="2026-04-05T00:00:00Z",
    )


def _make_harman_fr(reference_spl: float = 75.0, n_points: int = 50) -> FrequencyResponse:
    """Synthetic FR that matches the Harman target exactly."""
    target = HarmanTarget(reference_spl=reference_spl)
    freqs = np.logspace(np.log10(20), np.log10(200), n_points).tolist()
    spl = [target.target_at(f) for f in freqs]
    return FrequencyResponse(
        frequencies=freqs,
        spl=spl,
        sample_rate=96000,
        sweep_duration=3.0,
        timestamp="2026-04-05T00:00:00Z",
    )


def _make_mock_driver(eq_state: list[dict] | None = None) -> AsyncMock:
    """Create a mock MinidspDriver."""
    driver = AsyncMock()
    driver.read_eq = AsyncMock(return_value=eq_state or [])
    driver.apply_eq = AsyncMock(return_value=None)
    return driver


def _make_initial_eq() -> list[dict]:
    """A valid initial EQ state (HPF + one peaking filter)."""
    return [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 80.0, "gain_db": -2.0, "q": 1.0, "type": "peaking"},
    ]


# ── Convergence tests ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_converges_in_n_iterations() -> None:
    """Loop with mock backend converges on synthetic data."""
    driver = _make_mock_driver(eq_state=_make_initial_eq())
    orchestrator = LoopOrchestrator(minidsp=driver)
    recipe = _make_recipe(max_iterations=5)

    # Start with flat FR, then return progressively closer to Harman
    call_count = 0

    async def improving_measurement() -> FrequencyResponse:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return _make_fr(spl_value=75.0)  # flat (far from Harman)
        else:
            return _make_harman_fr(reference_spl=75.0)  # perfect match

    result = await orchestrator.run(recipe, preset=0, measure_fn=improving_measurement)

    assert result.converged
    assert result.iterations_run > 0
    assert result.final_rms <= recipe.convergence.threshold_db


@pytest.mark.asyncio
async def test_loop_stops_at_max_iterations() -> None:
    """Non-converging measurement stops at max iterations."""
    driver = _make_mock_driver(eq_state=_make_initial_eq())
    orchestrator = LoopOrchestrator(minidsp=driver)
    recipe = _make_recipe(max_iterations=2)

    # Always return flat FR (never converges to Harman)
    measure_fn = AsyncMock(return_value=_make_fr(spl_value=75.0))

    result = await orchestrator.run(recipe, preset=0, measure_fn=measure_fn)

    assert not result.converged
    assert result.iterations_run == 2
    assert result.final_rms > recipe.convergence.threshold_db


@pytest.mark.asyncio
async def test_loop_already_converged() -> None:
    """If baseline already matches target, loop exits immediately."""
    driver = _make_mock_driver(eq_state=_make_initial_eq())
    orchestrator = LoopOrchestrator(minidsp=driver)
    recipe = _make_recipe()

    measure_fn = AsyncMock(return_value=_make_harman_fr(75.0))

    result = await orchestrator.run(recipe, preset=0, measure_fn=measure_fn)

    assert result.converged
    assert result.iterations_run == 0
    assert driver.apply_eq.call_count == 0


# ── Empty state guard ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_refuses_empty_eq_state() -> None:
    """read_eq returns [] without fresh flag -> LoopError."""
    driver = _make_mock_driver(eq_state=[])
    orchestrator = LoopOrchestrator(minidsp=driver)
    recipe = _make_recipe()

    with pytest.raises(LoopError, match="Cannot snapshot empty EQ state"):
        await orchestrator.run(recipe, preset=0, measure_fn=AsyncMock())


@pytest.mark.asyncio
async def test_loop_allows_empty_eq_with_fresh_flag() -> None:
    """read_eq returns [] + fresh=True -> proceeds normally."""
    driver = _make_mock_driver(eq_state=[])
    orchestrator = LoopOrchestrator(minidsp=driver)
    recipe = _make_recipe(max_iterations=1)

    measure_fn = AsyncMock(return_value=_make_harman_fr(75.0))

    result = await orchestrator.run(recipe, preset=0, fresh=True, measure_fn=measure_fn)
    # Should proceed (converges at baseline since FR matches target)
    assert result.converged


# ── Measurement failure / rollback ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_rollback_on_measurement_failure() -> None:
    """Measurement error after retries triggers rollback."""
    initial_eq = _make_initial_eq()
    driver = _make_mock_driver(eq_state=initial_eq)
    orchestrator = LoopOrchestrator(minidsp=driver)
    recipe = _make_recipe(max_iterations=3)

    call_count = 0

    async def failing_measurement() -> FrequencyResponse:
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            return _make_fr(75.0)  # baseline succeeds
        raise RuntimeError("USB mic disconnected")

    result = await orchestrator.run(recipe, preset=0, measure_fn=failing_measurement)

    assert not result.converged
    assert "measurement failed" in result.error.lower() or "USB mic" in result.error
    # Rollback should have been called
    assert driver.apply_eq.call_count >= 1


# ── Concurrent start prevention ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_concurrent_start_prevented() -> None:
    """Lock prevents two loops from running simultaneously."""
    driver = _make_mock_driver(eq_state=_make_initial_eq())
    orchestrator = LoopOrchestrator(minidsp=driver)
    recipe = _make_recipe(max_iterations=1)

    # Create a slow measurement function
    async def slow_measurement() -> FrequencyResponse:
        await asyncio.sleep(0.5)
        return _make_harman_fr(75.0)

    # Start first loop
    task1 = asyncio.create_task(orchestrator.run(recipe, preset=0, measure_fn=slow_measurement))
    await asyncio.sleep(0.05)  # Let it acquire the lock

    # Second loop should fail
    with pytest.raises(LoopError, match="already running"):
        await orchestrator.run(recipe, preset=0, measure_fn=slow_measurement)

    # Clean up first task
    await task1


# ── HPF injection ─────────────────────────────────────────────────────────────

def test_hpf_injection_when_missing() -> None:
    """Proposed filters missing HPF -> HPF prepended."""
    filters = [FilterSpec(freq=80.0, gain_db=-2.0, q=1.0, type="peaking")]
    result = _inject_hpf(filters)
    assert result[0].type == "hpf"
    assert result[0].freq == HPF_FREQ_HZ
    assert len(result) == 2


def test_hpf_injection_when_present() -> None:
    """Proposed filters already have HPF -> no duplicate."""
    hpf = FilterSpec(freq=18.0, gain_db=0.0, q=0.707, type="hpf")
    filters = [hpf, FilterSpec(freq=80.0, gain_db=-2.0, q=1.0, type="peaking")]
    result = _inject_hpf(filters)
    assert len(result) == 2
    assert result[0].type == "hpf"


@pytest.mark.asyncio
async def test_loop_always_injects_hpf() -> None:
    """Even if mock backend omits HPF, applied filters include it."""
    driver = _make_mock_driver(eq_state=_make_initial_eq())
    orchestrator = LoopOrchestrator(minidsp=driver)
    recipe = _make_recipe(max_iterations=1)

    measure_fn = AsyncMock(return_value=_make_fr(75.0))

    result = await orchestrator.run(recipe, preset=0, measure_fn=measure_fn)

    # Check that every apply_eq call includes an HPF
    for call in driver.apply_eq.call_args_list:
        filters = call[1].get("filters") or call[0][1]
        hpf_present = any(f.get("type") == "hpf" or (hasattr(f, "type") and f.type == "hpf") for f in filters)
        assert hpf_present, f"apply_eq called without HPF: {filters}"


# ── Snapshot ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_saves_snapshot_before_start() -> None:
    """Snapshot (rollback point) is saved in the result."""
    initial_eq = _make_initial_eq()
    driver = _make_mock_driver(eq_state=initial_eq)
    orchestrator = LoopOrchestrator(minidsp=driver)
    recipe = _make_recipe(max_iterations=1)

    measure_fn = AsyncMock(return_value=_make_harman_fr(75.0))

    result = await orchestrator.run(recipe, preset=0, measure_fn=measure_fn)
    assert result.rollback_snapshot == initial_eq


# ── Helpers ───────────────────────────────────────────────────────────────────

def test_median_spl() -> None:
    fr = FrequencyResponse(
        frequencies=[20.0, 40.0, 80.0],
        spl=[70.0, 75.0, 80.0],
        sample_rate=96000,
        sweep_duration=3.0,
        timestamp="2026-04-05T00:00:00Z",
    )
    assert median_spl(fr) == 75.0


def test_median_spl_empty() -> None:
    fr = FrequencyResponse(
        frequencies=[],
        spl=[],
        sample_rate=96000,
        sweep_duration=3.0,
        timestamp="2026-04-05T00:00:00Z",
    )
    assert median_spl(fr) == 0.0


# ── Persistence tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_persists_run_and_iterations(tmp_path) -> None:
    """When a SessionStore is provided, the loop persists run + iterations."""
    from calibrate.storage import SessionStore

    store = SessionStore(db_path=tmp_path / "test.db")
    driver = _make_mock_driver(eq_state=_make_initial_eq())
    recipe = _make_recipe(max_iterations=3)

    call_count = 0

    async def measure_fn():
        nonlocal call_count
        call_count += 1
        return _make_fr(spl_value=75.0)

    orch = LoopOrchestrator(minidsp=driver, store=store)
    result = await orch.run(recipe, preset=0, measure_fn=measure_fn)

    # Should have run some iterations
    assert result.iterations_run > 0

    # Check persistence
    runs = store.get_runs()
    assert len(runs) == 1
    assert runs[0]["recipe_name"] == "test-recipe"
    assert runs[0]["target"] == "harman"

    detail = store.get_run_detail(runs[0]["id"])
    assert detail is not None
    assert len(detail["iterations"]) == result.iterations_run
    # Each iteration has filter data
    for it in detail["iterations"]:
        assert isinstance(it["filters_proposed"], list)
        assert isinstance(it["filters_applied"], list)


@pytest.mark.asyncio
async def test_loop_persists_without_store() -> None:
    """Loop works fine without a store (backward compatibility)."""
    driver = _make_mock_driver(eq_state=_make_initial_eq())
    recipe = _make_recipe(max_iterations=1)

    async def measure_fn():
        return _make_fr()

    orch = LoopOrchestrator(minidsp=driver)  # no store
    result = await orch.run(recipe, preset=0, measure_fn=measure_fn)
    assert result.iterations_run >= 0  # just didn't crash
