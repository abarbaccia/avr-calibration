"""Unit tests for the sub-alignment algorithm.

All tests use synthetic numpy arrays — no hardware, no network, no audio I/O.
MeasurementEngine is patched where needed to bypass PyTTa / PortAudio.
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from calibrate.alignment import (
    SubIRResult,
    compute_delay_offsets,
    detect_and_correct_polarity,
    level_match_subs,
    measure_sub_ir,
    MUTE_GAIN_DB,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_sweep(duration_s: float = 0.5, sample_rate: int = 48000) -> list[float]:
    """Minimal log sweep for testing (same formula as MeasurementEngine)."""
    n = int(sample_rate * duration_s)
    t = np.linspace(0.0, duration_s, n, endpoint=False)
    k = duration_s / np.log(200 / 20)
    return np.sin(2.0 * np.pi * 20 * k * (np.exp(t / k) - 1.0)).astype(np.float32).tolist()


def _make_delayed_recording(
    sweep: list[float],
    delay_samples: int,
    sample_rate: int = 48000,
    amplitude: float = 0.8,
    invert: bool = False,
) -> list[float]:
    """Return a recording that looks like 'sweep delayed by delay_samples'."""
    n = len(sweep) + delay_samples + sample_rate  # extra second of zeros
    rec = np.zeros(n, dtype=np.float32)
    sig = np.array(sweep) * amplitude * (-1 if invert else 1)
    rec[delay_samples : delay_samples + len(sweep)] = sig
    return rec.tolist()


def _make_engine_mock() -> MagicMock:
    """Stub MeasurementEngine that passes validate_recording without raising."""
    engine = MagicMock()
    engine.validate_recording.return_value = []  # no warnings
    return engine


# ── compute_delay_offsets ──────────────────────────────────────────────────────

def test_compute_delay_offsets_two_subs_different() -> None:
    """Sub 1 peaks at 12 ms, sub 0 peaks at 10 ms → delays = [2.0, 0.0]."""
    results = [
        SubIRResult(sub_index=0, peak_time_s=0.010, peak_sign=1, polarity_inverted=False, spl_db=-20.0),
        SubIRResult(sub_index=1, peak_time_s=0.012, peak_sign=1, polarity_inverted=False, spl_db=-20.0),
    ]
    offsets = compute_delay_offsets(results)
    assert len(offsets) == 2
    assert abs(offsets[0] - 2.0) < 0.01   # sub 0 needs +2 ms
    assert abs(offsets[1] - 0.0) < 0.01   # sub 1 is the reference


def test_compute_delay_offsets_all_same() -> None:
    """Both subs peak at 10 ms → delays = [0.0, 0.0]."""
    results = [
        SubIRResult(sub_index=0, peak_time_s=0.010, peak_sign=1, polarity_inverted=False, spl_db=-20.0),
        SubIRResult(sub_index=1, peak_time_s=0.010, peak_sign=1, polarity_inverted=False, spl_db=-20.0),
    ]
    offsets = compute_delay_offsets(results)
    assert all(abs(d) < 1e-9 for d in offsets)


def test_compute_delay_offsets_single_sub() -> None:
    """Single sub → delay = [0.0]."""
    results = [
        SubIRResult(sub_index=0, peak_time_s=0.015, peak_sign=1, polarity_inverted=False, spl_db=-25.0),
    ]
    offsets = compute_delay_offsets(results)
    assert offsets == [0.0]


def test_compute_delay_offsets_empty() -> None:
    assert compute_delay_offsets([]) == []


# ── measure_sub_ir — IR peak detection ────────────────────────────────────────

@pytest.mark.asyncio
async def test_compute_ir_peak_positive() -> None:
    """Positive-polarity recording → peak_sign = +1, peak_time close to delay."""
    sr = 48000
    delay_s = 0.010
    delay_samples = int(delay_s * sr)
    sweep = _make_sweep(sample_rate=sr)
    rec = _make_delayed_recording(sweep, delay_samples, sr, amplitude=0.8, invert=False)

    engine = _make_engine_mock()
    result = await measure_sub_ir(engine, rec, sweep, sr, sub_index=0)

    assert result.peak_sign == 1
    assert abs(result.peak_time_s - delay_s) < 0.002  # within 2 ms


@pytest.mark.asyncio
async def test_compute_ir_peak_negative() -> None:
    """Inverted recording → peak_sign = -1."""
    sr = 48000
    delay_samples = int(0.010 * sr)
    sweep = _make_sweep(sample_rate=sr)
    rec = _make_delayed_recording(sweep, delay_samples, sr, amplitude=0.8, invert=True)

    engine = _make_engine_mock()
    result = await measure_sub_ir(engine, rec, sweep, sr, sub_index=0)

    assert result.peak_sign == -1


# ── detect_and_correct_polarity ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_polarity_same_sign_no_inversion() -> None:
    """Both subs positive → no set_output_polarity calls."""
    client = AsyncMock()
    results = [
        SubIRResult(sub_index=0, peak_time_s=0.010, peak_sign=1, polarity_inverted=False, spl_db=-20.0),
        SubIRResult(sub_index=1, peak_time_s=0.012, peak_sign=1, polarity_inverted=False, spl_db=-20.0),
    ]
    updated = await detect_and_correct_polarity(results, [0, 1], client)

    client.set_output_polarity.assert_not_called()
    assert not any(r.polarity_inverted for r in updated)


@pytest.mark.asyncio
async def test_polarity_opposite_sign_inverts() -> None:
    """Sub 1 is negative vs sub 0 positive → polarity_inverted[1]=True, set_output_polarity called."""
    client = AsyncMock()
    results = [
        SubIRResult(sub_index=0, peak_time_s=0.010, peak_sign=1,  polarity_inverted=False, spl_db=-20.0),
        SubIRResult(sub_index=1, peak_time_s=0.012, peak_sign=-1, polarity_inverted=False, spl_db=-20.0),
    ]
    updated = await detect_and_correct_polarity(results, [0, 1], client)

    client.set_output_polarity.assert_called_once_with(1, inverted=True)
    assert not updated[0].polarity_inverted
    assert updated[1].polarity_inverted


# ── measure_sub_ir — gain restore safety ──────────────────────────────────────

@pytest.mark.asyncio
async def test_measure_sub_ir_propagates_quality_error() -> None:
    """MeasurementQualityError from validate_recording propagates to caller."""
    from calibrate.measurement import MeasurementQualityError

    engine = MagicMock()
    engine.validate_recording.side_effect = MeasurementQualityError(
        check="sweep_capture",
        detail="Sweep not captured",
        suggestion="Turn on your amp",
    )

    sweep = _make_sweep()
    rec = [0.0] * len(sweep)

    with pytest.raises(MeasurementQualityError) as exc_info:
        await measure_sub_ir(engine, rec, sweep, 48000, sub_index=0)

    assert exc_info.value.check == "sweep_capture"


# ── level_match_subs ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_level_match_sub_quieter() -> None:
    """Sub 1 is -3 dB relative to sub 0 → gain_trim = +3.0 written to output 1."""
    client = AsyncMock()
    results = [
        SubIRResult(sub_index=0, peak_time_s=0.010, peak_sign=1, polarity_inverted=False, spl_db=-20.0),
        SubIRResult(sub_index=1, peak_time_s=0.012, peak_sign=1, polarity_inverted=False, spl_db=-23.0),
    ]
    trims = await level_match_subs(results, [0, 1], client)

    assert abs(trims[0]) < 0.01      # reference: 0 dB trim
    assert abs(trims[1] - 3.0) < 0.01  # +3 dB to match

    # set_output_gain was called for each sub
    calls = client.set_output_gain.call_args_list
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_level_match_already_matched() -> None:
    """Both subs at same SPL → gain_trims ≈ [0.0, 0.0]."""
    client = AsyncMock()
    results = [
        SubIRResult(sub_index=0, peak_time_s=0.010, peak_sign=1, polarity_inverted=False, spl_db=-20.0),
        SubIRResult(sub_index=1, peak_time_s=0.012, peak_sign=1, polarity_inverted=False, spl_db=-20.0),
    ]
    trims = await level_match_subs(results, [0, 1], client)

    assert all(abs(t) < 0.01 for t in trims)
