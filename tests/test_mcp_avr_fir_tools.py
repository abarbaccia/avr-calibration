"""Tests for ``design_avr_fir`` + ``apply_avr_fir`` MCP tools.

Covers the no-hardware paths (validation, caching, error handling).
The actual TCP transmission of ``apply_avr_fir`` is exercised by
``scripts/smoke_test_filter_upload.py`` against the live AVR.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from calibrate.mcp_server import (
    _AVR_FIR_CACHE,
    _tool_apply_avr_fir,
    _tool_design_avr_fir,
)


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    _AVR_FIR_CACHE.clear()


# ── design_avr_fir ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_design_avr_fir_speaker_caches_1024_taps() -> None:
    res = await _tool_design_avr_fir(
        channel_id="FL",
        target_curve_db=[
            {"freq_hz": 20, "gain_db": 0},
            {"freq_hz": 1000, "gain_db": +3},
            {"freq_hz": 20000, "gain_db": 0},
        ],
        cache_key="session-42",
    )
    assert res["ok"]
    assert res["channel_id"] == "FL"
    assert res["fir_taps"] == 1024
    assert res["is_sub"] is False
    assert ("session-42", "FL") in _AVR_FIR_CACHE
    assert len(_AVR_FIR_CACHE[("session-42", "FL")]) == 1024


@pytest.mark.asyncio
async def test_design_avr_fir_sub_caches_704_taps() -> None:
    res = await _tool_design_avr_fir(
        channel_id="SW1",
        target_curve_db=[
            {"freq_hz": 20, "gain_db": +5},
            {"freq_hz": 80, "gain_db": 0},
            {"freq_hz": 200, "gain_db": -3},
        ],
        cache_key="iter-3",
    )
    assert res["ok"]
    assert res["fir_taps"] == 704
    assert res["is_sub"] is True


@pytest.mark.asyncio
async def test_design_avr_fir_lfe_treated_as_sub() -> None:
    res = await _tool_design_avr_fir(
        channel_id="LFE",
        target_curve_db=[{"freq_hz": 50, "gain_db": 0}],
        cache_key="x",
    )
    assert res["ok"]
    assert res["fir_taps"] == 704
    assert res["is_sub"] is True


@pytest.mark.asyncio
async def test_design_avr_fir_unknown_channel_returns_error() -> None:
    res = await _tool_design_avr_fir(
        channel_id="BOGUS",
        target_curve_db=[{"freq_hz": 100, "gain_db": 0}],
        cache_key="x",
    )
    assert not res["ok"]
    assert "Unknown" in res["error"]


@pytest.mark.asyncio
async def test_design_avr_fir_empty_curve_returns_error() -> None:
    res = await _tool_design_avr_fir(
        channel_id="FL",
        target_curve_db=[],
        cache_key="x",
    )
    assert not res["ok"]
    assert "empty" in res["error"]


@pytest.mark.asyncio
async def test_design_avr_fir_malformed_point_returns_error() -> None:
    res = await _tool_design_avr_fir(
        channel_id="FL",
        target_curve_db=[{"freq": 100}],  # missing gain_db, wrong key name
        cache_key="x",
    )
    assert not res["ok"]
    assert "bad target_curve_db entry" in res["error"]


@pytest.mark.asyncio
async def test_design_avr_fir_sorts_unsorted_input() -> None:
    """Out-of-order frequency points should still produce a valid IR."""
    res = await _tool_design_avr_fir(
        channel_id="FL",
        target_curve_db=[
            {"freq_hz": 20000, "gain_db": 0},
            {"freq_hz": 20, "gain_db": 0},
            {"freq_hz": 1000, "gain_db": +3},
        ],
        cache_key="x",
    )
    assert res["ok"]
    assert res["fir_taps"] == 1024


@pytest.mark.asyncio
async def test_design_avr_fir_peak_under_unity() -> None:
    """Polyphase output should not clip — peak well under 1.0."""
    res = await _tool_design_avr_fir(
        channel_id="FL",
        target_curve_db=[
            {"freq_hz": 30, "gain_db": +6},
            {"freq_hz": 100, "gain_db": +6},
            {"freq_hz": 1000, "gain_db": 0},
            {"freq_hz": 20000, "gain_db": -6},
        ],
        cache_key="x",
    )
    assert res["ok"]
    assert res["peak_amplitude"] < 1.0


# ── apply_avr_fir ─────────────────────────────────────────────────────


@pytest.fixture()
def small_ady_file(tmp_path: Path) -> Path:
    ady = {
        "ampAssignInfo": "00" * 48,
        "enAmpAssignType": 0,
        "subwooferNum": 1,
        "targetModelName": "Denon AVR-X3800H",
        "detectedChannels": [
            {"commandId": "FL", "customSpeakerType": "S",
             "customDistance": 4.0, "customCrossover": 80,
             "trimAdjustment": 0.0,
             "responseData": {"0": [0.0]}},
            {"commandId": "SW1", "customSpeakerType": "E",
             "customDistance": 3.0, "customCrossover": 0,
             "trimAdjustment": 0.0,
             "responseData": {"0": [0.0]}},
        ],
    }
    p = tmp_path / "test.ady"
    p.write_text(json.dumps(ady))
    return p


@pytest.mark.asyncio
async def test_apply_avr_fir_missing_ady_returns_error() -> None:
    res = await _tool_apply_avr_fir(
        host="192.0.2.1",
        ady_path="/no/such/file.ady",
        cache_key="x",
    )
    assert not res["ok"]
    assert "not found" in res["error"]


@pytest.mark.asyncio
async def test_apply_avr_fir_no_cached_filters_returns_error(
    small_ady_file: Path,
) -> None:
    res = await _tool_apply_avr_fir(
        host="192.0.2.1",
        ady_path=str(small_ady_file),
        cache_key="missing-key",
    )
    assert not res["ok"]
    assert "no cached FIR" in res["error"]


@pytest.mark.asyncio
async def test_apply_avr_fir_unknown_channel_returns_error(
    small_ady_file: Path,
) -> None:
    """Channel not in .ady raises before any TCP attempt."""
    # Pre-populate cache with a channel that DOES exist.
    _AVR_FIR_CACHE[("k", "FL")] = [0.0] * 1024
    res = await _tool_apply_avr_fir(
        host="192.0.2.1",
        ady_path=str(small_ady_file),
        cache_key="k",
        channel_ids=["FL", "BOGUS"],
        allow_partial=True,
    )
    assert not res["ok"]
    assert "not in .ady" in res["error"]


@pytest.mark.asyncio
async def test_apply_avr_fir_calls_push_with_resolved_filters(
    small_ady_file: Path,
) -> None:
    """Happy path: cached coefs + valid .ady + mocked TCP push."""
    _AVR_FIR_CACHE[("k", "FL")] = [0.0] * 1024
    _AVR_FIR_CACHE[("k", "SW1")] = [0.0] * 704

    fake_summary = {
        "ok": True,
        "enter_audy_ack": True,
        "setdat_acks": [True, True, True],
        "init_coefs_ack": None,
        "coef_packets_sent": 90,
        "finz_coefs_ack": True,
        "fin_commit_ack": True,
        "exit_audmd_ack": True,
        "channel_count": 2,
    }
    with patch(
        "calibrate.drivers.denon.audyssey_filter_upload.push_avr_filters",
        new=AsyncMock(return_value=fake_summary),
    ) as mock_push:
        res = await _tool_apply_avr_fir(
            host="192.0.2.1",
            ady_path=str(small_ady_file),
            cache_key="k",
            distances_override_m={"SW1": 20.0},
        )
    assert res["ok"]
    assert sorted(res["channels_uploaded"]) == ["FL", "SW1"]
    mock_push.assert_called_once()
    call_kwargs = mock_push.call_args.kwargs
    assert "FL" in call_kwargs["channel_filters"]
    assert "SW1" in call_kwargs["channel_filters"]
    assert call_kwargs["distances_override_m"]["SW1"] == 20.0


@pytest.mark.asyncio
async def test_apply_avr_fir_subset_refused_by_default(
    small_ady_file: Path,
) -> None:
    """A subset push leaves un-selected channels with stale FIRs that can
    silently attenuate under MultEQ:FLAT/REFERENCE. Default-refuse so the
    caller has to opt in explicitly."""
    _AVR_FIR_CACHE[("k", "FL")] = [0.0] * 1024
    _AVR_FIR_CACHE[("k", "SW1")] = [0.0] * 704

    res = await _tool_apply_avr_fir(
        host="192.0.2.1",
        ady_path=str(small_ady_file),
        cache_key="k",
        channel_ids=["FL"],
    )
    assert not res["ok"]
    assert "partial push refused" in res["error"]
    assert "allow_partial=True" in res["error"]


@pytest.mark.asyncio
async def test_apply_avr_fir_subset_channels_with_allow_partial(
    small_ady_file: Path,
) -> None:
    """Caller can restrict the upload to a subset of channels by passing
    allow_partial=True, intentionally bypassing the safety gate."""
    _AVR_FIR_CACHE[("k", "FL")] = [0.0] * 1024
    _AVR_FIR_CACHE[("k", "SW1")] = [0.0] * 704

    with patch(
        "calibrate.drivers.denon.audyssey_filter_upload.push_avr_filters",
        new=AsyncMock(return_value={"ok": True, "channel_count": 1}),
    ) as mock_push:
        res = await _tool_apply_avr_fir(
            host="192.0.2.1",
            ady_path=str(small_ady_file),
            cache_key="k",
            channel_ids=["FL"],
            allow_partial=True,
        )
    assert res["ok"]
    assert res["channels_uploaded"] == ["FL"]
    pushed = mock_push.call_args.kwargs["channel_filters"]
    assert list(pushed.keys()) == ["FL"]
    assert "SW1" not in pushed


@pytest.mark.asyncio
async def test_apply_avr_fir_propagates_target_curves_and_rates(
    small_ady_file: Path,
) -> None:
    _AVR_FIR_CACHE[("k", "FL")] = [0.0] * 1024
    _AVR_FIR_CACHE[("k", "SW1")] = [0.0] * 704
    with patch(
        "calibrate.drivers.denon.audyssey_filter_upload.push_avr_filters",
        new=AsyncMock(return_value={"ok": True}),
    ) as mock_push:
        await _tool_apply_avr_fir(
            host="192.0.2.1",
            ady_path=str(small_ady_file),
            cache_key="k",
            target_curves=["01"],  # Reference only
            samplerates_hz=[48000],  # 48 kHz only
        )
    kwargs = mock_push.call_args.kwargs
    assert kwargs["target_curves"] == ("01",)
    assert kwargs["samplerates_hz"] == (48000,)
