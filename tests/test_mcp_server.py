"""Tests for calibrate.mcp_server — all tools and resources.

All hardware is mocked at the driver level (AVRDriver / DSPDriver).
No real network, hardware, or file access required.

Covers:
  - get_device_state: connected + unreachable for both drivers
  - get_measurement_history: returns sessions, handles empty, handles storage error
  - read_eq: returns in-memory state (flat on startup, updated after apply_eq)
  - apply_eq: valid filters → driver.apply_eq called
  - apply_eq: DriverError propagated as {ok: false}
  - set_volume: success and failure cases
  - set_volume: legacy aliases (avr_set_volume, set_denon_volume) still dispatch
  - measure: no UMIK found → error
  - measure: success (direct engine call, session saved)
  - measure: engine error propagated
  - measure: DenonSweepContext wraps engine when HDMI configured
  - mute_output / unmute_output: mute/unmute DSP outputs
  - set_delay: set output delay
  - set_polarity: set output polarity
  - check_system: pre-flight hardware checks
  - fetch_recipe: found → returns content; not found → {ok: false}
  - fetch_recipe: path traversal via ".." → rejected
  - fetch_recipe: path traversal via symlink → rejected
  - MCP tool dispatch: unknown tool name → error
  - resources: measurements://latest, eq://current
"""

from __future__ import annotations

import json
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from calibrate import mcp_server as sut
from calibrate.mcp_server import (
    _downsample_to_third_octave,
    _read_resource,
    _tool_analyze_decay,
    _tool_analyze_ir,
    _tool_apply_eq,
    _tool_apply_fir,
    _tool_avr_set_volume,
    _tool_calibrate_level,
    _tool_check_system,
    _tool_clear_fir,
    _tool_configure_matrix,
    _tool_fetch_recipe,
    _tool_get_device_state,
    _tool_get_fr_summary,
    _tool_get_measurement_history,
    _tool_get_output_state,
    _tool_mute_output,
    _tool_read_eq,
    _tool_set_delay,
    _tool_set_master_gain,
    _tool_set_output_gain,
    _tool_set_polarity,
    _tool_trigger_measurement,
    _tool_unmute_output,
)
from calibrate.drivers.base import DriverError


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_avr():
    """Patch _avr with an AsyncMock AVRDriver."""
    avr = AsyncMock()
    avr.get_state.return_value = {
        "connected": True,
        "host": "192.168.1.100",
        "volume": -30.0,
        "input": "CBL/SAT",
        "mute": False,
    }
    avr.set_volume.return_value = -30.0
    with patch("calibrate.mcp_server._avr", avr):
        yield avr


@pytest.fixture
def mock_hdmi_config():
    """Patch _config to simulate HDMI playback route (Denon in signal chain)."""
    mock_cfg = MagicMock()
    mock_cfg.measurement.get.side_effect = lambda key, default=None: (
        "hdmi" if key == "playback_route" else default
    )
    with patch.object(sut, "_config", return_value=mock_cfg):
        yield mock_cfg


@pytest.fixture
def mock_dsp():
    """Patch _dsp with an AsyncMock DSPDriver.

    apply_eq is stateful: it stores filters in _eq_state so read_eq
    reflects what was applied (mirrors MinidspDriver behaviour in tests).
    """
    _eq_state: dict[int, list[dict]] = {}

    dsp = AsyncMock()
    dsp.current_preset.return_value = 0
    dsp.get_state.return_value = {
        "connected": True,
        "host": "localhost",
        "preset": 0,
        "source": "Analog",
        "volume": -30.0,
        "mute": False,
    }

    async def read_eq(preset: int) -> list[dict]:
        return list(_eq_state.get(preset, []))

    async def apply_eq(preset: int, filters: list[dict], output_index: int | None = None) -> None:
        _eq_state[preset] = list(filters)

    async def apply_input_eq(preset: int, filters: list[dict], input_index: int | None = None) -> None:
        _eq_state[("input", preset)] = list(filters)

    dsp.read_eq.side_effect = read_eq
    dsp.apply_eq.side_effect = apply_eq
    dsp.apply_input_eq.side_effect = apply_input_eq

    with patch("calibrate.mcp_server._dsp", dsp):
        yield dsp


@pytest.fixture
def valid_filters():
    """A minimal valid filter set: HPF + one peaking band."""
    return [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 80.0, "gain_db": 3.0, "q": 0.707, "type": "peaking"},
    ]


# ── get_device_state ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_device_state_both_connected(mock_avr, mock_dsp) -> None:
    result = await _tool_get_device_state()
    assert result["ok"]
    assert result["avr"]["connected"]
    assert result["dsp"]["connected"]


@pytest.mark.asyncio
async def test_get_device_state_avr_unreachable(mock_avr, mock_dsp) -> None:
    mock_avr.get_state.side_effect = DriverError("timeout")
    result = await _tool_get_device_state()
    assert result["ok"]  # overall ok — individual errors are in sub-dicts
    assert not result["avr"]["connected"]
    assert "error" in result["avr"]


@pytest.mark.asyncio
async def test_get_device_state_dsp_unreachable(mock_avr, mock_dsp) -> None:
    mock_dsp.get_state.side_effect = DriverError("connection refused")
    result = await _tool_get_device_state()
    assert result["ok"]
    assert not result["dsp"]["connected"]
    assert "error" in result["dsp"]


@pytest.mark.asyncio
async def test_get_device_state_avr_no_host(mock_avr, mock_dsp) -> None:
    mock_avr.get_state.return_value = {"connected": False, "error": "no host configured"}
    result = await _tool_get_device_state()
    assert not result["avr"]["connected"]
    assert "no host" in result["avr"]["error"].lower()


@pytest.mark.asyncio
async def test_get_device_state_avr_timeout(mock_avr, mock_dsp) -> None:
    mock_avr.get_state.side_effect = DriverError("timeout connecting to 192.168.1.100")
    result = await _tool_get_device_state()
    assert result["ok"]
    assert not result["avr"]["connected"]
    assert "timeout" in result["avr"]["error"]


# ── get_measurement_history ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_measurement_history_returns_sessions() -> None:
    mock_fr = MagicMock()
    mock_fr.frequencies = [20.0, 40.0, 80.0]
    mock_fr.spl = [-10.0, -5.0, 0.0]

    mock_session = MagicMock()
    mock_session.id = 1
    mock_session.timestamp = "2026-04-01T00:00:00Z"
    mock_session.label = "test"
    mock_session.start_fr = mock_fr

    with patch("calibrate.storage.SessionStore") as mock_store_cls:
        mock_store = MagicMock()
        mock_store.list_sessions.return_value = [mock_session]
        mock_store_cls.return_value = mock_store
        result = await _tool_get_measurement_history(limit=5)

    assert result["ok"]
    assert result["count"] == 1
    assert result["sessions"][0]["id"] == 1
    assert result["sessions"][0]["freq_hz"] == [20.0, 40.0, 80.0]


@pytest.mark.asyncio
async def test_get_measurement_history_empty() -> None:
    with patch("calibrate.storage.SessionStore") as mock_store_cls:
        mock_store = MagicMock()
        mock_store.list_sessions.return_value = []
        mock_store_cls.return_value = mock_store
        result = await _tool_get_measurement_history()

    assert result["ok"]
    assert result["count"] == 0
    assert result["sessions"] == []


@pytest.mark.asyncio
async def test_get_measurement_history_storage_error() -> None:
    with patch("calibrate.storage.SessionStore") as mock_store_cls:
        mock_store_cls.side_effect = Exception("db error")
        result = await _tool_get_measurement_history()

    assert not result["ok"]
    assert "storage error" in result["error"]


# ── get_fr_summary ────────────────────────────────────────────────────────────


def test_downsample_to_third_octave_basic() -> None:
    """Downsample flat FR to 1/3-octave bands."""
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(200), 500).tolist()
    spl = [75.0] * len(freqs)
    bands = _downsample_to_third_octave(freqs, spl)
    assert len(bands) == 11  # 20 to 200 Hz in 1/3-octave steps
    for b in bands:
        assert b["spl_db"] == 75.0


def test_downsample_to_third_octave_preserves_peak() -> None:
    """A peak in the 80Hz band should show up in the downsampled data."""
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(200), 500).tolist()
    spl = [75.0] * len(freqs)
    # Add a peak near 80Hz
    for i, f in enumerate(freqs):
        if 70 < f < 90:
            spl[i] = 85.0
    bands = _downsample_to_third_octave(freqs, spl)
    band_80 = next(b for b in bands if b["freq_hz"] == 80.0)
    assert band_80["spl_db"] > 80.0


@pytest.mark.asyncio
async def test_get_fr_summary_returns_bands() -> None:
    """get_fr_summary returns 1/3-octave downsampled FR data."""
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(200), 500).tolist()
    spl = [75.0] * len(freqs)

    mock_fr = MagicMock()
    mock_fr.frequencies = freqs
    mock_fr.spl = spl
    mock_fr.peak_spl = 75.0
    mock_fr.freq_at_peak = 80.0

    mock_session = MagicMock()
    mock_session.id = 42
    mock_session.timestamp = "2026-04-07T00:00:00Z"
    mock_session.label = "test-summary"
    mock_session.start_fr = mock_fr
    mock_session.metadata = {"ir": {"peak_time_ms": 5.0, "spl_db": 75.0}}

    with patch("calibrate.storage.SessionStore") as mock_store_cls:
        mock_store = MagicMock()
        mock_store.list_sessions.return_value = [mock_session]
        mock_store_cls.return_value = mock_store
        result = await _tool_get_fr_summary(limit=5)

    assert result["ok"]
    assert result["count"] == 1
    session = result["sessions"][0]
    assert session["id"] == 42
    assert len(session["bands"]) == 11
    assert session["peak_spl"] == 75.0
    assert session["ir_summary"] == {"peak_time_ms": 5.0, "spl_db": 75.0}


@pytest.mark.asyncio
async def test_get_fr_summary_by_session_ids() -> None:
    """get_fr_summary fetches specific sessions by ID."""
    mock_fr = MagicMock()
    mock_fr.frequencies = [20.0, 80.0, 200.0]
    mock_fr.spl = [75.0, 75.0, 75.0]
    mock_fr.peak_spl = 75.0
    mock_fr.freq_at_peak = 80.0

    mock_session = MagicMock()
    mock_session.id = 10
    mock_session.timestamp = "2026-04-07T00:00:00Z"
    mock_session.label = "specific"
    mock_session.start_fr = mock_fr
    mock_session.metadata = None

    with patch("calibrate.storage.SessionStore") as mock_store_cls:
        mock_store = MagicMock()
        mock_store.get_session.return_value = mock_session
        mock_store_cls.return_value = mock_store
        result = await _tool_get_fr_summary(session_ids=[10])

    assert result["ok"]
    assert result["count"] == 1
    assert result["sessions"][0]["id"] == 10
    assert "ir_summary" not in result["sessions"][0]  # no metadata


@pytest.mark.asyncio
async def test_get_fr_summary_empty() -> None:
    with patch("calibrate.storage.SessionStore") as mock_store_cls:
        mock_store = MagicMock()
        mock_store.list_sessions.return_value = []
        mock_store_cls.return_value = mock_store
        result = await _tool_get_fr_summary()

    assert result["ok"]
    assert result["count"] == 0


# ── read_eq ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_eq_starts_flat(mock_dsp) -> None:
    result = await _tool_read_eq()
    assert result["ok"]
    assert result["filters"] == []


@pytest.mark.asyncio
async def test_read_eq_reflects_applied_state(mock_dsp, valid_filters) -> None:
    """After apply_eq, read_eq should return the applied filters."""
    apply_result = await _tool_apply_eq(valid_filters)
    assert apply_result["ok"], apply_result

    read_result = await _tool_read_eq()
    assert read_result["ok"]
    assert len(read_result["filters"]) == len(valid_filters)


# ── apply_eq ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_eq_valid_calls_driver(mock_dsp, valid_filters) -> None:
    result = await _tool_apply_eq(valid_filters)
    assert result["ok"], result
    mock_dsp.apply_eq.assert_called_once()


@pytest.mark.asyncio
async def test_apply_eq_missing_hpf_rejected(mock_dsp) -> None:
    mock_dsp.apply_eq.side_effect = DriverError("SafetyValidator: mandatory HPF missing")
    filters = [{"freq": 80.0, "gain_db": 3.0, "q": 0.707, "type": "peaking"}]
    result = await _tool_apply_eq(filters)
    assert not result["ok"]
    assert "SafetyValidator" in result["error"]


@pytest.mark.asyncio
async def test_apply_eq_boost_below_25hz_rejected(mock_dsp) -> None:
    mock_dsp.apply_eq.side_effect = DriverError("SafetyValidator: boost below 25 Hz")
    filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 20.0, "gain_db": 3.0, "q": 0.707, "type": "peaking"},
    ]
    result = await _tool_apply_eq(filters)
    assert not result["ok"]
    assert "SafetyValidator" in result["error"]


@pytest.mark.asyncio
async def test_apply_eq_above_6db_rejected(mock_dsp) -> None:
    mock_dsp.apply_eq.side_effect = DriverError("SafetyValidator: gain exceeds +6 dB")
    filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 80.0, "gain_db": 7.0, "q": 0.707, "type": "peaking"},
    ]
    result = await _tool_apply_eq(filters)
    assert not result["ok"]
    assert "SafetyValidator" in result["error"]


@pytest.mark.asyncio
async def test_apply_eq_too_many_filters(mock_dsp) -> None:
    mock_dsp.apply_eq.side_effect = DriverError(
        "too many filters: 10 requested, 8 PEQ slots available (slots 2-9)"
    )
    filters = [{"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"}]
    filters += [{"freq": float(f), "gain_db": -1.0, "q": 0.707, "type": "peaking"}
                for f in [30, 40, 50, 63, 80, 100, 125, 160, 200]]
    result = await _tool_apply_eq(filters)
    assert not result["ok"]
    assert "too many filters" in result["error"]


@pytest.mark.asyncio
async def test_apply_eq_minidsp_error_returns_structured_error(mock_dsp, valid_filters) -> None:
    mock_dsp.apply_eq.side_effect = DriverError("minidsp write failed: minidspd 500 on /devices/0/config")
    result = await _tool_apply_eq(valid_filters)
    assert not result["ok"]
    assert "minidsp write failed" in result["error"]


@pytest.mark.asyncio
async def test_apply_eq_generic_exception_returns_error(mock_dsp, valid_filters) -> None:
    mock_dsp.apply_eq.side_effect = DriverError("apply_eq error: unexpected error")
    result = await _tool_apply_eq(valid_filters)
    assert not result["ok"]
    assert "apply_eq error" in result["error"]


@pytest.mark.asyncio
async def test_apply_eq_updates_reflected_in_read_eq(mock_dsp, valid_filters) -> None:
    """State from apply_eq is reflected by read_eq (driver is stateful)."""
    await _tool_apply_eq(valid_filters)
    read_result = await _tool_read_eq()
    assert read_result["ok"]
    assert len(read_result["filters"]) == len(valid_filters)


# ── avr_set_volume / set_denon_volume ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_avr_set_volume_success(mock_avr, mock_hdmi_config) -> None:
    mock_avr.set_volume.return_value = -25.0
    result = await _tool_avr_set_volume(-25.0)
    assert result["ok"]
    assert result["level_db"] == -25.0
    mock_avr.set_volume.assert_called_once_with(-25.0)


@pytest.mark.asyncio
async def test_avr_set_volume_no_host(mock_avr, mock_hdmi_config) -> None:
    mock_avr.set_volume.side_effect = DriverError("no host configured")
    result = await _tool_avr_set_volume(-30.0)
    assert not result["ok"]
    assert "avr unreachable" in result["error"]


@pytest.mark.asyncio
async def test_avr_set_volume_connection_error(mock_avr, mock_hdmi_config) -> None:
    mock_avr.set_volume.side_effect = DriverError("connection refused")
    result = await _tool_avr_set_volume(-30.0)
    assert not result["ok"]
    assert "avr unreachable" in result["error"]


@pytest.mark.asyncio
async def test_avr_set_volume_usb_mode_no_op() -> None:
    """In USB mode, set_volume is a no-op — Denon is not in the signal chain."""
    mock_avr = AsyncMock()
    mock_cfg = MagicMock()
    mock_cfg.measurement.get.side_effect = lambda key, default=None: (
        "usb" if key == "playback_route" else default
    )
    with patch.object(sut, "_avr", mock_avr), patch.object(sut, "_config", return_value=mock_cfg):
        result = await _tool_avr_set_volume(-25.0)
    assert result["ok"]
    assert result["level_db"] is None
    assert "USB mode" in result["message"]
    mock_avr.set_volume.assert_not_called()


@pytest.mark.asyncio
async def test_set_volume_legacy_alias_avr_set_volume(mock_avr, mock_hdmi_config) -> None:
    """Legacy avr_set_volume alias still dispatches to set_volume."""
    mock_avr.set_volume.return_value = -25.0
    from calibrate.mcp_server import call_tool
    texts = await call_tool("avr_set_volume", {"level_db": -25.0})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert data["level_db"] == -25.0


@pytest.mark.asyncio
async def test_set_volume_legacy_alias_set_denon_volume(mock_avr, mock_hdmi_config) -> None:
    """Legacy set_denon_volume alias still dispatches to set_volume."""
    mock_avr.set_volume.return_value = -25.0
    from calibrate.mcp_server import call_tool
    texts = await call_tool("set_denon_volume", {"level_db": -25.0})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert data["level_db"] == -25.0


# ── trigger_measurement ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trigger_measurement_no_umik() -> None:
    """No UMIK found → error."""
    mock_sd = sys.modules.get("sounddevice")
    if mock_sd:
        mock_sd.query_devices.return_value = [
            {"name": "USB Audio", "max_input_channels": 2}
        ]
    result = await _tool_trigger_measurement()
    assert not result["ok"]
    assert "UMIK" in result["error"]


@pytest.mark.asyncio
async def test_trigger_measurement_success() -> None:
    """UMIK found → engine.measure() called directly → session saved."""
    mock_sd = MagicMock()
    mock_sd.query_devices.return_value = [{"name": "UMIK-1", "max_input_channels": 1}]

    mock_fr = MagicMock()
    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(return_value=mock_fr)

    mock_store = MagicMock()
    mock_store.save_measurement.return_value = 7

    with (
        patch.dict(sys.modules, {"sounddevice": mock_sd}),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.measurement.compute_session_metadata", return_value={"ir": {}}),
        patch("calibrate.storage.SessionStore", return_value=mock_store),
        patch.object(sut, "DenonSweepContext") as MockCtx,
        patch("calibrate.drivers.minidsp.MinidspSweepContext") as MockMinidspCtx,
    ):
        MockCtx.from_config.return_value = None  # no HDMI context
        MockMinidspCtx.from_config.return_value = None  # no miniDSP context → direct call
        result = await _tool_trigger_measurement()

    assert result["ok"]
    assert result["session_id"] == 7
    mock_engine.measure.assert_called_once()


@pytest.mark.asyncio
async def test_trigger_measurement_engine_error() -> None:
    """engine.measure() raises → error returned."""
    mock_sd = MagicMock()
    mock_sd.query_devices.return_value = [{"name": "UMIK-1", "max_input_channels": 1}]

    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(side_effect=RuntimeError("Audio device error"))

    with (
        patch.dict(sys.modules, {"sounddevice": mock_sd}),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch.object(sut, "DenonSweepContext") as MockCtx,
        patch("calibrate.drivers.minidsp.MinidspSweepContext") as MockMinidspCtx,
    ):
        MockCtx.from_config.return_value = None
        MockMinidspCtx.from_config.return_value = None
        result = await _tool_trigger_measurement()

    assert not result["ok"]
    assert "measurement failed" in result["error"]


@pytest.mark.asyncio
async def test_trigger_measurement_with_denon_context() -> None:
    """HDMI route → DenonSweepContext wraps engine.measure()."""
    mock_sd = MagicMock()
    mock_sd.query_devices.return_value = [{"name": "UMIK-1", "max_input_channels": 1}]

    mock_fr = MagicMock()
    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(return_value=mock_fr)

    mock_store = MagicMock()
    mock_store.save_measurement.return_value = 3

    mock_ctx_instance = AsyncMock()
    mock_ctx_instance.__aenter__ = AsyncMock(return_value=mock_ctx_instance)
    mock_ctx_instance.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.dict(sys.modules, {"sounddevice": mock_sd}),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.measurement.compute_session_metadata", return_value={"ir": {}}),
        patch("calibrate.storage.SessionStore", return_value=mock_store),
        patch.object(sut, "DenonSweepContext") as MockCtx,
    ):
        MockCtx.from_config.return_value = mock_ctx_instance
        result = await _tool_trigger_measurement()

    assert result["ok"]
    assert result["session_id"] == 3
    mock_ctx_instance.__aenter__.assert_called_once()
    mock_ctx_instance.__aexit__.assert_called_once()


@pytest.mark.asyncio
async def test_trigger_measurement_stores_explicit_target_curve() -> None:
    """target_curve passed explicitly by calibration engine is stored with the session."""
    mock_sd = MagicMock()
    mock_sd.query_devices.return_value = [{"name": "UMIK-1", "max_input_channels": 1}]

    mock_fr = MagicMock()
    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(return_value=mock_fr)

    tc = {"type": "harman", "reference_spl": 72.5, "band": [20, 200]}
    mock_store = MagicMock()
    mock_store.save_measurement.return_value = 5

    with (
        patch.dict(sys.modules, {"sounddevice": mock_sd}),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.measurement.compute_session_metadata", return_value={"ir": {}}),
        patch("calibrate.storage.SessionStore", return_value=mock_store),
        patch.object(sut, "DenonSweepContext") as MockCtx,
        patch("calibrate.drivers.minidsp.MinidspSweepContext") as MockMinidspCtx,
    ):
        MockCtx.from_config.return_value = None
        MockMinidspCtx.from_config.return_value = None
        result = await _tool_trigger_measurement(target_curve=tc)

    assert result["ok"]
    call_kwargs = mock_store.save_measurement.call_args[1]
    assert call_kwargs["target_curve"] == tc


@pytest.mark.asyncio
async def test_trigger_measurement_no_target_for_raw_capture() -> None:
    """Standalone/diagnostic measurement stores target_curve=None — no delta shown."""
    mock_sd = MagicMock()
    mock_sd.query_devices.return_value = [{"name": "UMIK-1", "max_input_channels": 1}]

    mock_fr = MagicMock()
    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(return_value=mock_fr)

    mock_store = MagicMock()
    mock_store.save_measurement.return_value = 6

    with (
        patch.dict(sys.modules, {"sounddevice": mock_sd}),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.measurement.compute_session_metadata", return_value={"ir": {}}),
        patch("calibrate.storage.SessionStore", return_value=mock_store),
        patch.object(sut, "DenonSweepContext") as MockCtx,
        patch("calibrate.drivers.minidsp.MinidspSweepContext") as MockMinidspCtx,
    ):
        MockCtx.from_config.return_value = None
        MockMinidspCtx.from_config.return_value = None
        result = await _tool_trigger_measurement()  # no target_curve

    assert result["ok"]
    call_kwargs = mock_store.save_measurement.call_args[1]
    assert call_kwargs["target_curve"] is None


# ── calibrate_level ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_calibrate_level_succeeds_first_try() -> None:
    """If SNR is good at starting volume, returns immediately."""
    mock_avr = AsyncMock()
    mock_avr.set_volume = AsyncMock(return_value=-10.0)

    mock_fr = MagicMock()
    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(return_value=mock_fr)

    with (
        patch.object(sut, "_avr", mock_avr),
        patch.object(sut, "_config"),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.config.update_config") as mock_update,
    ):
        result = await _tool_calibrate_level(start_db=-10.0)

    assert result["ok"]
    assert result["calibrated_volume_db"] == -10.0
    mock_update.assert_called_once_with({"measurement": {"denon_sweep_volume": -10.0}})


@pytest.mark.asyncio
async def test_calibrate_level_ramps_on_low_snr() -> None:
    """SNR too low at start → ramps up until good."""
    from calibrate.measurement import MeasurementQualityError

    mock_avr = AsyncMock()
    mock_avr.set_volume = AsyncMock(return_value=-10.0)

    mock_fr = MagicMock()
    mock_engine = MagicMock()
    # Fail twice with low SNR, then succeed
    mock_engine.measure = AsyncMock(side_effect=[
        MeasurementQualityError("snr", "SNR 12 dB < 20 dB", "increase volume"),
        MeasurementQualityError("snr", "SNR 16 dB < 20 dB", "increase volume"),
        mock_fr,
    ])

    with (
        patch.object(sut, "_avr", mock_avr),
        patch.object(sut, "_config"),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.config.update_config"),
    ):
        result = await _tool_calibrate_level(start_db=-10.0, step_db=3.0)

    assert result["ok"]
    assert result["calibrated_volume_db"] == -4.0  # -10 + 3 + 3


@pytest.mark.asyncio
async def test_calibrate_level_hits_ceiling() -> None:
    """SNR never good enough → returns error at ceiling."""
    from calibrate.measurement import MeasurementQualityError

    mock_avr = AsyncMock()
    mock_avr.set_volume = AsyncMock(return_value=-10.0)

    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(
        side_effect=MeasurementQualityError("snr", "SNR 10 dB < 20 dB", "check subs"),
    )

    with (
        patch.object(sut, "_avr", mock_avr),
        patch.object(sut, "_config"),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.config.update_config"),
    ):
        result = await _tool_calibrate_level(start_db=-4.0, max_volume_db=0.0, step_db=3.0)

    assert not result["ok"]
    assert "Could not achieve SNR" in result["error"]


@pytest.mark.asyncio
async def test_calibrate_level_no_avr_hdmi_mode() -> None:
    """HDMI mode with no AVR driver loaded → error."""
    mock_cfg = MagicMock()
    mock_cfg.measurement.get.side_effect = lambda key, default=None: "hdmi" if key == "playback_route" else default

    with patch.object(sut, "_avr", None), \
         patch.object(sut, "_config", return_value=mock_cfg):
        result = await _tool_calibrate_level()

    assert not result["ok"]
    assert "AVR driver not loaded" in result["error"]


@pytest.mark.asyncio
async def test_calibrate_level_ramps_on_sweep_not_detected() -> None:
    """Sweep not captured → ramps up (same as low SNR)."""
    from calibrate.measurement import MeasurementQualityError

    mock_avr = AsyncMock()
    mock_avr.set_volume = AsyncMock(return_value=-10.0)

    mock_fr = MagicMock()
    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(side_effect=[
        MeasurementQualityError("sweep_capture", "Sweep not detected", "check input"),
        mock_fr,
    ])

    with (
        patch.object(sut, "_avr", mock_avr),
        patch.object(sut, "_config"),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.config.update_config"),
    ):
        result = await _tool_calibrate_level(start_db=-10.0, step_db=3.0)

    assert result["ok"]
    assert result["calibrated_volume_db"] == -7.0


def _make_usb_cfg():
    """Return a mock config that reports USB playback route."""
    mock_cfg = MagicMock()
    mock_cfg.measurement.get.side_effect = lambda key, default=None: (
        "usb" if key == "playback_route" else default
    )
    return mock_cfg


@pytest.mark.asyncio
async def test_calibrate_level_usb_good_level() -> None:
    """USB mode: sets master gain to start_db, peak_spl in range → success."""
    mock_dsp = AsyncMock()
    mock_fr = MagicMock()
    mock_fr.peak_spl = -12.0  # within [-30, -6] window
    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(return_value=mock_fr)

    with (
        patch.object(sut, "_dsp", mock_dsp),
        patch.object(sut, "_config", return_value=_make_usb_cfg()),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.drivers.minidsp.MinidspSweepContext.from_config", return_value=None),
        patch("calibrate.config.update_config") as mock_update,
        patch("asyncio.sleep"),
    ):
        result = await _tool_calibrate_level(start_db=-10.0)

    assert result["ok"]
    assert result["calibrated_master_gain_db"] == -10.0
    mock_dsp.set_master_gain.assert_called_once_with(-10.0)
    mock_update.assert_called_once_with({"measurement": {"master_gain_db": -10.0}})


@pytest.mark.asyncio
async def test_calibrate_level_usb_steps_down_on_hot_signal() -> None:
    """USB mode: peak_spl too hot → steps master gain down until in range."""
    mock_dsp = AsyncMock()

    hot_fr = MagicMock()
    hot_fr.peak_spl = 1.5  # above 0 dBFS ceiling (clipping)

    good_fr = MagicMock()
    good_fr.peak_spl = -4.0  # within range

    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(side_effect=[hot_fr, good_fr])

    with (
        patch.object(sut, "_dsp", mock_dsp),
        patch.object(sut, "_config", return_value=_make_usb_cfg()),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.drivers.minidsp.MinidspSweepContext.from_config", return_value=None),
        patch("calibrate.config.update_config") as mock_update,
        patch("asyncio.sleep"),
    ):
        result = await _tool_calibrate_level(start_db=-10.0, step_db=3.0, max_spl_dbfs=0.0)

    assert result["ok"]
    assert result["calibrated_master_gain_db"] == -13.0  # -10 - 3
    assert mock_dsp.set_master_gain.call_count == 2
    mock_update.assert_called_once_with({"measurement": {"master_gain_db": -13.0}})


@pytest.mark.asyncio
async def test_calibrate_level_usb_snr_fail() -> None:
    """USB mode: SNR too low → error directing user to physical knob."""
    from calibrate.measurement import MeasurementQualityError

    mock_dsp = AsyncMock()
    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(
        side_effect=MeasurementQualityError("snr", "SNR 8 dB < 20 dB", "increase level"),
    )

    with (
        patch.object(sut, "_dsp", mock_dsp),
        patch.object(sut, "_config", return_value=_make_usb_cfg()),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.drivers.minidsp.MinidspSweepContext.from_config", return_value=None),
        patch("calibrate.config.update_config"),
        patch("asyncio.sleep"),
    ):
        result = await _tool_calibrate_level(start_db=-10.0)

    assert not result["ok"]
    assert "physical gain knob" in result["error"]


@pytest.mark.asyncio
async def test_calibrate_level_usb_no_dsp() -> None:
    """USB mode with no DSP driver loaded → error."""
    with (
        patch.object(sut, "_dsp", None),
        patch.object(sut, "_config", return_value=_make_usb_cfg()),
    ):
        result = await _tool_calibrate_level()

    assert not result["ok"]
    assert "DSP driver not loaded" in result["error"]


# ── fetch_recipe ───────────────────────────────────────────────────────────────

@pytest.fixture
def recipe_dir(tmp_path) -> Path:
    """Create a temporary recipes directory with one recipe."""
    core_dir = tmp_path / "core"
    core_dir.mkdir(parents=True)
    (core_dir / "harman-bass.md").write_text("# Recipe: Harman Bass\nversion: 1.0\n")
    return tmp_path


@pytest.mark.asyncio
async def test_fetch_recipe_found(recipe_dir) -> None:
    with patch.object(sut, "RECIPES_DIR", recipe_dir):
        result = await _tool_fetch_recipe("core/harman-bass")
    assert result["ok"]
    assert "Harman Bass" in result["content"]
    assert result["name"] == "core/harman-bass"


@pytest.mark.asyncio
async def test_fetch_recipe_not_found(recipe_dir) -> None:
    with patch.object(sut, "RECIPES_DIR", recipe_dir):
        result = await _tool_fetch_recipe("core/nonexistent")
    assert not result["ok"]
    assert "recipe not found" in result["error"]
    assert "core/nonexistent" in result["error"]


@pytest.mark.asyncio
async def test_fetch_recipe_path_traversal_rejected(recipe_dir) -> None:
    with patch.object(sut, "RECIPES_DIR", recipe_dir):
        result = await _tool_fetch_recipe("../../etc/passwd")
    assert not result["ok"]
    assert "invalid" in result["error"]


@pytest.mark.asyncio
async def test_fetch_recipe_symlink_traversal_rejected(tmp_path) -> None:
    """Symlink inside recipes/ pointing outside → rejected (P0 security fix)."""
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    target = tmp_path / "secret.txt"
    target.write_text("sensitive data")
    (recipes / "evil.md").symlink_to(target)
    with patch.object(sut, "RECIPES_DIR", recipes):
        result = await _tool_fetch_recipe("evil")
    assert not result["ok"]
    assert "invalid" in result["error"]


@pytest.mark.asyncio
async def test_fetch_recipe_leading_slash_sanitised(recipe_dir) -> None:
    with patch.object(sut, "RECIPES_DIR", recipe_dir):
        result = await _tool_fetch_recipe("/core/harman-bass")
    assert result["ok"]


@pytest.mark.asyncio
async def test_fetch_recipe_read_error(tmp_path) -> None:
    recipe_file = tmp_path / "bad.md"
    recipe_file.write_text("content")
    with patch.object(sut, "RECIPES_DIR", tmp_path):
        with patch("pathlib.Path.read_text", side_effect=PermissionError("denied")):
            result = await _tool_fetch_recipe("bad")
    assert not result["ok"]
    assert "failed to read recipe" in result["error"]


# ── MCP tool dispatch ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_call_tool_unknown_name() -> None:
    result = await _tool_fetch_recipe("")  # empty name → not found
    assert not result["ok"]


# ── Resources ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resource_measurements_latest_empty() -> None:
    with patch("calibrate.storage.SessionStore") as mock_cls:
        mock_store = MagicMock()
        mock_store.list_sessions.return_value = []
        mock_cls.return_value = mock_store
        result = await _read_resource("measurements://latest")
    data = json.loads(result)
    assert "error" in data
    assert "no measurements" in data["error"]


@pytest.mark.asyncio
async def test_resource_measurements_latest_returns_first() -> None:
    mock_fr = MagicMock()
    mock_fr.frequencies = [20.0, 80.0]
    mock_fr.spl = [-5.0, 0.0]

    mock_session = MagicMock()
    mock_session.id = 42
    mock_session.timestamp = "2026-04-01T00:00:00Z"
    mock_session.label = "test run"
    mock_session.start_fr = mock_fr
    mock_session.notes = None

    with patch("calibrate.storage.SessionStore") as mock_cls:
        mock_store = MagicMock()
        mock_store.list_sessions.return_value = [mock_session]
        mock_cls.return_value = mock_store
        result = await _read_resource("measurements://latest")

    data = json.loads(result)
    assert data["id"] == 42
    assert data["freq_hz"] == [20.0, 80.0]


@pytest.mark.asyncio
async def test_resource_eq_current(mock_dsp) -> None:
    result = await _read_resource("eq://current")
    data = json.loads(result)
    assert "preset" in data
    assert "filters" in data


@pytest.mark.asyncio
async def test_resource_unknown_uri() -> None:
    result = await _read_resource("unknown://foo")
    data = json.loads(result)
    assert "error" in data
    assert "unknown resource" in data["error"]


@pytest.mark.asyncio
async def test_resource_measurements_latest_storage_exception() -> None:
    with patch("calibrate.storage.SessionStore") as mock_cls:
        mock_cls.side_effect = Exception("disk error")
        result = await _read_resource("measurements://latest")
    data = json.loads(result)
    assert "error" in data
    assert "disk error" in data["error"]


# ── Missing error paths ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_device_state_avr_generic_exception(mock_avr, mock_dsp) -> None:
    """Generic (non-DriverError) exception from avr.get_state → error in avr sub-dict."""
    mock_avr.get_state.side_effect = RuntimeError("unexpected avr failure")
    result = await _tool_get_device_state()
    assert result["ok"]
    assert not result["avr"]["connected"]
    assert "unexpected avr failure" in result["avr"]["error"]


@pytest.mark.asyncio
async def test_get_device_state_dsp_generic_exception(mock_avr, mock_dsp) -> None:
    """Generic (non-DriverError) exception from dsp.get_state → error in dsp sub-dict."""
    mock_dsp.get_state.side_effect = RuntimeError("unexpected dsp failure")
    result = await _tool_get_device_state()
    assert result["ok"]
    assert not result["dsp"]["connected"]
    assert "unexpected dsp failure" in result["dsp"]["error"]


@pytest.mark.asyncio
async def test_read_eq_driver_error(mock_dsp) -> None:
    """DriverError from dsp.current_preset → {ok: false}."""
    mock_dsp.current_preset.side_effect = DriverError("dsp unreachable")
    result = await _tool_read_eq()
    assert not result["ok"]
    assert "dsp unreachable" in result["error"]


@pytest.mark.asyncio
async def test_trigger_measurement_sounddevice_unavailable() -> None:
    """sounddevice not available → error."""
    with patch.dict(sys.modules, {"sounddevice": None}):
        result = await _tool_trigger_measurement()
    assert not result["ok"]
    assert "sounddevice" in result["error"] or "audio" in result["error"].lower()


@pytest.mark.asyncio
async def test_resource_eq_current_driver_error(mock_dsp) -> None:
    """DriverError from dsp in eq://current resource → JSON error."""
    mock_dsp.current_preset.side_effect = DriverError("preset unavailable")
    result = await _read_resource("eq://current")
    data = json.loads(result)
    assert "error" in data
    assert "preset unavailable" in data["error"]


# ── MCP handler dispatch (call_tool for all branches) ─────────────────────────

@pytest.mark.asyncio
async def test_call_tool_get_device_state(mock_avr, mock_dsp) -> None:
    from calibrate.mcp_server import call_tool
    texts = await call_tool("get_device_state", {})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "avr" in data
    assert "dsp" in data


@pytest.mark.asyncio
async def test_call_tool_get_measurement_history() -> None:
    from calibrate.mcp_server import call_tool
    with patch("calibrate.storage.SessionStore") as mock_cls:
        mock_store = MagicMock()
        mock_store.list_sessions.return_value = []
        mock_cls.return_value = mock_store
        texts = await call_tool("get_measurement_history", {"limit": 3})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert data["count"] == 0


@pytest.mark.asyncio
async def test_call_tool_read_eq(mock_dsp) -> None:
    from calibrate.mcp_server import call_tool
    texts = await call_tool("read_eq", {})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "filters" in data


@pytest.mark.asyncio
async def test_call_tool_apply_eq(mock_dsp, valid_filters) -> None:
    from calibrate.mcp_server import call_tool
    texts = await call_tool("apply_eq", {"filters": valid_filters})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert data["filters_applied"] == len(valid_filters)


@pytest.mark.asyncio
async def test_call_tool_measure_dispatch() -> None:
    from calibrate.mcp_server import call_tool
    with patch.dict(sys.modules, {"sounddevice": None}):
        texts = await call_tool("measure", {})
    data = json.loads(texts[0].text)
    assert not data["ok"]  # no UMIK on CI → degraded mode


@pytest.mark.asyncio
async def test_call_tool_trigger_measurement_legacy_alias() -> None:
    """Legacy trigger_measurement name still dispatches."""
    from calibrate.mcp_server import call_tool
    with patch.dict(sys.modules, {"sounddevice": None}):
        texts = await call_tool("trigger_measurement", {})
    data = json.loads(texts[0].text)
    assert not data["ok"]


@pytest.mark.asyncio
async def test_call_tool_fetch_recipe_dispatch(tmp_path) -> None:
    from calibrate.mcp_server import call_tool
    recipe = tmp_path / "core" / "test.md"
    recipe.parent.mkdir()
    recipe.write_text("# test recipe")
    with patch.object(sut, "RECIPES_DIR", tmp_path):
        texts = await call_tool("fetch_recipe", {"name": "core/test"})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "test recipe" in data["content"]


@pytest.mark.asyncio
async def test_call_tool_unknown_dispatches_error() -> None:
    from calibrate.mcp_server import call_tool
    texts = await call_tool("no_such_tool", {})
    data = json.loads(texts[0].text)
    assert not data["ok"]
    assert "unknown tool" in data["error"]


# ── MCP list_tools / list_resources / read_resource handlers ──────────────────

@pytest.mark.asyncio
async def test_list_tools_returns_all_tools() -> None:
    from calibrate.mcp_server import list_tools
    tools = await list_tools()
    names = {t.name for t in tools}
    assert "get_device_state" in names
    assert "apply_eq" in names
    assert "measure" in names
    assert "set_volume" in names
    assert "mute_output" in names
    assert "unmute_output" in names
    assert "set_delay" in names
    assert "set_polarity" in names
    assert "get_output_state" in names
    assert "set_output_gain" in names
    assert "apply_fir" in names
    assert "clear_fir" in names
    assert "analyze_ir" in names
    assert "configure_matrix" in names
    assert "analyze_decay" in names
    assert "check_system" in names
    assert "fetch_recipe" in names
    # Deprecated names should NOT be in tool list
    assert "trigger_measurement" not in names
    assert "set_denon_volume" not in names
    assert "avr_set_volume" not in names
    assert "mute_sub_outputs" not in names


@pytest.mark.asyncio
async def test_list_resources_returns_known_resources() -> None:
    from calibrate.mcp_server import list_resources
    resources = await list_resources()
    uris = {str(r.uri) for r in resources}
    assert "measurements://latest" in uris
    assert "eq://current" in uris


@pytest.mark.asyncio
async def test_read_resource_handler_delegates(mock_dsp) -> None:
    """read_resource() MCP handler delegates to _read_resource()."""
    from calibrate.mcp_server import read_resource
    result = await read_resource("eq://current")
    data = json.loads(result)
    assert "preset" in data
    assert "filters" in data


# ── create_app ────────────────────────────────────────────────────────────────

def test_create_app_returns_starlette_app() -> None:
    """create_app() constructs a Starlette ASGI app with SSE and messages routes."""
    from starlette.applications import Starlette
    from calibrate.mcp_server import create_app
    app = create_app()
    assert isinstance(app, Starlette)
    route_paths = [r.path for r in app.routes]
    assert "/sse" in route_paths


# ── get_config ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_config() -> None:
    from calibrate.mcp_server import _tool_get_config
    fake_data = {"denon": {"host": "192.168.1.100"}, "minidsp": {"port": 5380}}
    mock_cfg = MagicMock()
    mock_cfg._data = fake_data
    with patch("calibrate.mcp_server._config", return_value=mock_cfg):
        result = await _tool_get_config()
    assert result["ok"]
    assert result["config"]["denon"]["host"] == "192.168.1.100"


@pytest.mark.asyncio
async def test_get_config_error() -> None:
    from calibrate.mcp_server import _tool_get_config
    with patch("calibrate.mcp_server._config", side_effect=FileNotFoundError("missing")):
        result = await _tool_get_config()
    assert not result["ok"]
    assert "config error" in result["error"]


# ── set_config ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_config() -> None:
    from calibrate.mcp_server import _tool_set_config
    updated_data = {"denon": {"host": "10.0.0.1"}, "minidsp": {"port": 5380}}
    mock_cfg = MagicMock()
    mock_cfg._data = updated_data
    with (
        patch("calibrate.mcp_server.update_config") as mock_update,
        patch("calibrate.mcp_server._config", return_value=mock_cfg),
    ):
        result = await _tool_set_config({"denon": {"host": "10.0.0.1"}})
    assert result["ok"]
    assert result["config"]["denon"]["host"] == "10.0.0.1"
    mock_update.assert_called_once_with({"denon": {"host": "10.0.0.1"}})


@pytest.mark.asyncio
async def test_set_config_error() -> None:
    from calibrate.mcp_server import _tool_set_config
    with patch("calibrate.mcp_server.update_config", side_effect=OSError("read-only fs")):
        result = await _tool_set_config({"denon": {"host": "x"}})
    assert not result["ok"]
    assert "config write error" in result["error"]


# ── discover_avr ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discover_avr_found(mock_avr) -> None:
    from calibrate.mcp_server import _tool_discover_avr
    mock_avr.discover.return_value = ["192.168.1.209", "192.168.1.210"]
    result = await _tool_discover_avr()
    assert result["ok"]
    assert result["receivers"] == ["192.168.1.209", "192.168.1.210"]


@pytest.mark.asyncio
async def test_discover_avr_empty(mock_avr) -> None:
    from calibrate.mcp_server import _tool_discover_avr
    mock_avr.discover.return_value = []
    result = await _tool_discover_avr()
    assert result["ok"]
    assert result["receivers"] == []


@pytest.mark.asyncio
async def test_discover_avr_error(mock_avr) -> None:
    from calibrate.mcp_server import _tool_discover_avr
    mock_avr.discover.side_effect = asyncio.TimeoutError("scan timed out")
    result = await _tool_discover_avr()
    assert not result["ok"]
    assert "discovery error" in result["error"]


# ── get_calibration_runs bug fix ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_calibration_runs_no_crash() -> None:
    """Verify the _load_config() -> _config() bug is fixed (no NameError)."""
    from calibrate.mcp_server import _tool_get_calibration_runs
    mock_store = MagicMock()
    mock_store.get_runs.return_value = []
    with patch("calibrate.storage.SessionStore", return_value=mock_store):
        result = await _tool_get_calibration_runs()
    assert result["ok"]
    assert result["runs"] == []


# ── mute_output / unmute_output ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mute_output_success(mock_dsp) -> None:
    result = await _tool_mute_output([0, 1])
    assert result["ok"]
    assert result["muted"] == [0, 1]
    mock_dsp.mute_outputs.assert_called_once_with([0, 1])


@pytest.mark.asyncio
async def test_mute_output_error(mock_dsp) -> None:
    mock_dsp.mute_outputs.side_effect = Exception("hw error")
    result = await _tool_mute_output([0])
    assert not result["ok"]
    assert "mute failed" in result["error"]


@pytest.mark.asyncio
async def test_unmute_output_success(mock_dsp) -> None:
    result = await _tool_unmute_output([0, 1])
    assert result["ok"]
    assert result["unmuted"] == [0, 1]
    mock_dsp.unmute_outputs.assert_called_once_with([0, 1])


@pytest.mark.asyncio
async def test_unmute_output_error(mock_dsp) -> None:
    mock_dsp.unmute_outputs.side_effect = Exception("hw error")
    result = await _tool_unmute_output([0])
    assert not result["ok"]
    assert "unmute failed" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_mute_output_dispatch(mock_dsp) -> None:
    from calibrate.mcp_server import call_tool
    texts = await call_tool("mute_output", {"output_indices": [1]})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert data["muted"] == [1]


@pytest.mark.asyncio
async def test_call_tool_unmute_output_dispatch(mock_dsp) -> None:
    from calibrate.mcp_server import call_tool
    texts = await call_tool("unmute_output", {"output_indices": [0, 1]})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert data["unmuted"] == [0, 1]


@pytest.mark.asyncio
async def test_call_tool_mute_sub_outputs_legacy(mock_dsp) -> None:
    """Legacy mute_sub_outputs name still works via dispatch."""
    from calibrate.mcp_server import call_tool
    texts = await call_tool("mute_sub_outputs", {"mute": [1], "unmute": [0]})
    data = json.loads(texts[0].text)
    assert data["ok"]


# ── set_delay ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_delay_success(mock_dsp) -> None:
    result = await _tool_set_delay(output_index=0, delay_ms=2.5)
    assert result["ok"]
    assert result["output_index"] == 0
    assert result["delay_ms"] == 2.5
    mock_dsp.set_output_delay.assert_called_once_with(0, 2.5)


@pytest.mark.asyncio
async def test_set_delay_driver_error(mock_dsp) -> None:
    mock_dsp.set_output_delay.side_effect = DriverError("invalid output")
    result = await _tool_set_delay(output_index=5, delay_ms=1.0)
    assert not result["ok"]
    assert "invalid output" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_set_delay_dispatch(mock_dsp) -> None:
    from calibrate.mcp_server import call_tool
    texts = await call_tool("set_delay", {"output_index": 1, "delay_ms": 3.0})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert data["delay_ms"] == 3.0


# ── set_polarity ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_polarity_success(mock_dsp) -> None:
    result = await _tool_set_polarity(output_index=1, inverted=True)
    assert result["ok"]
    assert result["output_index"] == 1
    assert result["inverted"] is True
    mock_dsp.set_output_polarity.assert_called_once_with(1, True)


@pytest.mark.asyncio
async def test_set_polarity_driver_error(mock_dsp) -> None:
    mock_dsp.set_output_polarity.side_effect = DriverError("hw failure")
    result = await _tool_set_polarity(output_index=0, inverted=False)
    assert not result["ok"]
    assert "hw failure" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_set_polarity_dispatch(mock_dsp) -> None:
    from calibrate.mcp_server import call_tool
    texts = await call_tool("set_polarity", {"output_index": 0, "inverted": True})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert data["inverted"] is True


# ── check_system ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_system_all_pass() -> None:
    from calibrate.preflight import CheckResult
    mock_results = [
        CheckResult(name="Config", passed=True, detail="ok"),
        CheckResult(name="miniDSP", passed=True, detail="connected"),
        CheckResult(name="Denon AVR", passed=True, detail="online"),
        CheckResult(name="Signal Path", passed=True, detail="matches"),
    ]
    with patch("calibrate.preflight.PreflightChecker") as MockChecker:
        instance = AsyncMock()
        instance.run_all.return_value = mock_results
        MockChecker.return_value = instance
        result = await _tool_check_system()
    assert result["ok"]
    assert result["all_passed"] is True
    assert len(result["checks"]) == 4


@pytest.mark.asyncio
async def test_check_system_some_fail() -> None:
    from calibrate.preflight import CheckResult
    mock_results = [
        CheckResult(name="Config", passed=True, detail="ok"),
        CheckResult(name="miniDSP", passed=False, detail="not found", error="USB disconnected"),
    ]
    with patch("calibrate.preflight.PreflightChecker") as MockChecker:
        instance = AsyncMock()
        instance.run_all.return_value = mock_results
        MockChecker.return_value = instance
        result = await _tool_check_system()
    assert result["ok"]
    assert result["all_passed"] is False
    failed = [c for c in result["checks"] if not c["passed"]]
    assert len(failed) == 1
    assert failed[0]["name"] == "miniDSP"


@pytest.mark.asyncio
async def test_check_system_error() -> None:
    with patch("calibrate.mcp_server._config", side_effect=Exception("boom")):
        result = await _tool_check_system()
    assert not result["ok"]
    assert "check_system error" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_check_system_dispatch() -> None:
    from calibrate.mcp_server import call_tool
    from calibrate.preflight import CheckResult
    mock_results = [
        CheckResult(name="Config", passed=True, detail="ok"),
    ]
    with patch("calibrate.preflight.PreflightChecker") as MockChecker:
        instance = AsyncMock()
        instance.run_all.return_value = mock_results
        MockChecker.return_value = instance
        texts = await call_tool("check_system", {})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert data["all_passed"] is True


# ── set_volume dispatch ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_call_tool_set_volume_dispatch(mock_avr, mock_hdmi_config) -> None:
    from calibrate.mcp_server import call_tool
    mock_avr.set_volume.return_value = -30.0
    texts = await call_tool("set_volume", {"level_db": -30.0})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert data["level_db"] == -30.0


# ── get_output_state ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_output_state_fresh_returns_defaults(mock_dsp) -> None:
    """Fresh driver returns defaults: 0dB gain, 0ms delay, not inverted."""
    defaults = {i: {"gain_db": 0.0, "delay_ms": 0.0, "polarity_inverted": False} for i in range(4)}
    mock_dsp.get_output_state = MagicMock(return_value=defaults)
    result = await _tool_get_output_state()
    assert result["ok"]
    assert len(result["outputs"]) == 4
    assert result["outputs"][0]["gain_db"] == 0.0
    assert result["outputs"][1]["delay_ms"] == 0.0
    assert result["outputs"][2]["polarity_inverted"] is False


@pytest.mark.asyncio
async def test_get_output_state_reflects_applied_values(mock_dsp) -> None:
    """After set_delay/set_polarity/set_output_gain, state should reflect them."""
    mock_dsp.get_output_state = MagicMock(return_value={
        0: {"gain_db": 0.0, "delay_ms": 0.0, "polarity_inverted": False},
        1: {"gain_db": -3.5, "delay_ms": 4.2, "polarity_inverted": True},
        2: {"gain_db": 0.0, "delay_ms": 0.0, "polarity_inverted": False},
        3: {"gain_db": 0.0, "delay_ms": 0.0, "polarity_inverted": False},
    })
    result = await _tool_get_output_state()
    assert result["ok"]
    assert result["outputs"][1]["gain_db"] == -3.5
    assert result["outputs"][1]["delay_ms"] == 4.2
    assert result["outputs"][1]["polarity_inverted"] is True


@pytest.mark.asyncio
async def test_get_output_state_error(mock_dsp) -> None:
    mock_dsp.get_output_state = MagicMock(side_effect=Exception("driver gone"))
    result = await _tool_get_output_state()
    assert not result["ok"]
    assert "get_output_state error" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_get_output_state_dispatch(mock_dsp) -> None:
    from calibrate.mcp_server import call_tool
    defaults = {i: {"gain_db": 0.0, "delay_ms": 0.0, "polarity_inverted": False} for i in range(4)}
    mock_dsp.get_output_state = MagicMock(return_value=defaults)
    texts = await call_tool("get_output_state", {})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "outputs" in data


# ── analyze_ir ───────────────────────────────────────────────────────────────

def _make_session_with_ir(session_id: int, peak_time_s: float = 0.005,
                           positive_peak: bool = True, spl_db: float = -20.0) -> MagicMock:
    """Build a mock Session with a synthetic IR having a known peak."""
    import numpy as np
    sample_rate = 48000
    n = int(sample_rate * 0.5)  # 500ms
    ir = np.zeros(n)
    peak_idx = int(peak_time_s * sample_rate)
    peak_idx = min(peak_idx, n - 1)
    amplitude = 10 ** (spl_db / 20.0)
    ir[peak_idx] = amplitude if positive_peak else -amplitude

    mock_fr = MagicMock()
    mock_fr.sample_rate = sample_rate

    session = MagicMock()
    session.id = session_id
    session.start_fr = mock_fr
    session.impulse_response = ir.tolist()
    return session


@pytest.mark.asyncio
async def test_analyze_ir_latest_session() -> None:
    session = _make_session_with_ir(session_id=3, peak_time_s=0.008, positive_peak=True)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_analyze_ir()
    assert result["ok"]
    assert result["session_id"] == 3
    assert abs(result["peak_time_s"] - 0.008) < 0.001
    assert result["peak_time_ms"] == pytest.approx(result["peak_time_s"] * 1000, abs=0.1)
    assert result["peak_sign"] == 1
    assert "spl_db" in result


@pytest.mark.asyncio
async def test_analyze_ir_negative_peak_sign() -> None:
    """Inverted polarity should report peak_sign = -1."""
    session = _make_session_with_ir(session_id=1, positive_peak=False)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_analyze_ir()
    assert result["ok"]
    assert result["peak_sign"] == -1


@pytest.mark.asyncio
async def test_analyze_ir_by_session_id() -> None:
    older = _make_session_with_ir(session_id=2, peak_time_s=0.010)
    newer = _make_session_with_ir(session_id=5, peak_time_s=0.005)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [newer, older]
        result = await _tool_analyze_ir(session_id=2)
    assert result["ok"]
    assert result["session_id"] == 2
    assert abs(result["peak_time_s"] - 0.010) < 0.001


@pytest.mark.asyncio
async def test_analyze_ir_session_not_found() -> None:
    session = _make_session_with_ir(session_id=1)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_analyze_ir(session_id=99)
    assert not result["ok"]
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_analyze_ir_no_sessions() -> None:
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = []
        result = await _tool_analyze_ir()
    assert not result["ok"]
    assert "no measurements found" in result["error"]


@pytest.mark.asyncio
async def test_analyze_ir_missing_ir() -> None:
    session = MagicMock()
    session.id = 1
    session.start_fr = MagicMock()
    session.impulse_response = None
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_analyze_ir()
    assert not result["ok"]
    assert "no impulse response" in result["error"]


@pytest.mark.asyncio
async def test_analyze_ir_delay_computation() -> None:
    """Validate that peak_time_s differences give correct delay offsets."""
    sub1 = _make_session_with_ir(session_id=1, peak_time_s=0.005)
    sub2 = _make_session_with_ir(session_id=2, peak_time_s=0.012)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [sub1]
        r1 = await _tool_analyze_ir(session_id=1)
        MockStore.return_value.list_sessions.return_value = [sub2]
        r2 = await _tool_analyze_ir(session_id=2)
    delay_offset_ms = (r2["peak_time_s"] - r1["peak_time_s"]) * 1000.0
    assert abs(delay_offset_ms - 7.0) < 1.0, f"Expected ~7ms offset, got {delay_offset_ms:.2f}ms"


@pytest.mark.asyncio
async def test_call_tool_analyze_ir_dispatch() -> None:
    from calibrate.mcp_server import call_tool
    session = _make_session_with_ir(session_id=1)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        texts = await call_tool("analyze_ir", {})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "peak_time_s" in data
    assert "peak_sign" in data
    assert "spl_db" in data


# ── set_output_gain ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_output_gain_success(mock_dsp) -> None:
    result = await _tool_set_output_gain(output_index=1, gain_db=-6.0)
    assert result["ok"]
    assert result["output_index"] == 1
    assert result["gain_db"] == -6.0
    mock_dsp.set_output_gain.assert_awaited_once_with(1, -6.0)


@pytest.mark.asyncio
async def test_set_output_gain_driver_error(mock_dsp) -> None:
    mock_dsp.set_output_gain.side_effect = DriverError("hw failure")
    result = await _tool_set_output_gain(output_index=2, gain_db=0.0)
    assert not result["ok"]
    assert "hw failure" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_set_output_gain_dispatch(mock_dsp) -> None:
    from calibrate.mcp_server import call_tool
    texts = await call_tool("set_output_gain", {"output_index": 1, "gain_db": -3.0})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert data["output_index"] == 1
    assert data["gain_db"] == -3.0


# ── set_master_gain ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_master_gain_success(mock_dsp) -> None:
    result = await _tool_set_master_gain(gain_db=-30.0)
    assert result["ok"]
    assert result["gain_db"] == -30.0
    mock_dsp.set_master_gain.assert_awaited_once_with(-30.0)


@pytest.mark.asyncio
async def test_set_master_gain_clamps_to_zero(mock_dsp) -> None:
    """Positive values are clamped to 0 by the driver."""
    result = await _tool_set_master_gain(gain_db=5.0)
    assert result["ok"]
    assert result["gain_db"] == 0.0


@pytest.mark.asyncio
async def test_set_master_gain_driver_error(mock_dsp) -> None:
    mock_dsp.set_master_gain.side_effect = DriverError("usb error")
    result = await _tool_set_master_gain(gain_db=-20.0)
    assert not result["ok"]
    assert "usb error" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_set_master_gain_dispatch(mock_dsp) -> None:
    from calibrate.mcp_server import call_tool
    texts = await call_tool("set_master_gain", {"gain_db": -30.0})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert data["gain_db"] == -30.0


# ── apply_fir / clear_fir ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_fir_success(mock_dsp) -> None:
    coeffs = [0.0] * 127 + [1.0]  # 128 taps, impulse at end
    result = await _tool_apply_fir(output_index=1, coefficients=coeffs)
    assert result["ok"]
    assert result["output_index"] == 1
    assert result["taps"] == 128
    mock_dsp.apply_fir.assert_awaited_once_with(1, coeffs)


@pytest.mark.asyncio
async def test_apply_fir_too_many_taps(mock_dsp) -> None:
    mock_dsp.apply_fir.side_effect = DriverError("too many FIR taps: 2049 > 2048")
    result = await _tool_apply_fir(output_index=0, coefficients=[0.0] * 2049)
    assert not result["ok"]
    assert "too many FIR taps" in result["error"]


@pytest.mark.asyncio
async def test_apply_fir_driver_error(mock_dsp) -> None:
    mock_dsp.apply_fir.side_effect = DriverError("hw failure")
    result = await _tool_apply_fir(output_index=1, coefficients=[1.0])
    assert not result["ok"]
    assert "hw failure" in result["error"]


@pytest.mark.asyncio
async def test_clear_fir_success(mock_dsp) -> None:
    result = await _tool_clear_fir(output_index=2)
    assert result["ok"]
    assert result["output_index"] == 2
    mock_dsp.clear_fir.assert_awaited_once_with(2)


@pytest.mark.asyncio
async def test_clear_fir_driver_error(mock_dsp) -> None:
    mock_dsp.clear_fir.side_effect = DriverError("clear failed")
    result = await _tool_clear_fir(output_index=0)
    assert not result["ok"]
    assert "clear failed" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_apply_fir_dispatch(mock_dsp) -> None:
    from calibrate.mcp_server import call_tool
    coeffs = [0.5, 0.5]
    texts = await call_tool("apply_fir", {"output_index": 1, "coefficients": coeffs})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert data["taps"] == 2


@pytest.mark.asyncio
async def test_call_tool_clear_fir_dispatch(mock_dsp) -> None:
    from calibrate.mcp_server import call_tool
    texts = await call_tool("clear_fir", {"output_index": 0})
    data = json.loads(texts[0].text)
    assert data["ok"]


# ── configure_matrix ─────────────────────────────────────────────────────────

@pytest.fixture
def mock_config_with_slots():
    """Mock _config() returning a config with two active output slots (indices 1, 2)."""
    cfg = MagicMock()
    cfg.minidsp.get.side_effect = lambda key, default=None: {
        "active_input": 0,
        "output_slots": [
            {"index": 1, "type": "sub"},
            {"index": 2, "type": "sub"},
            {"index": 3, "type": "unused"},
        ],
    }.get(key, default)
    return cfg


@pytest.mark.asyncio
async def test_configure_matrix_default_input(mock_dsp, mock_config_with_slots) -> None:
    with patch("calibrate.mcp_server._config", return_value=mock_config_with_slots):
        result = await _tool_configure_matrix()
    assert result["ok"]
    assert result["active_input"] == 0
    assert result["routed_outputs"] == [1, 2]
    mock_dsp.set_routing.assert_awaited_once()


@pytest.mark.asyncio
async def test_configure_matrix_override_input(mock_dsp, mock_config_with_slots) -> None:
    with patch("calibrate.mcp_server._config", return_value=mock_config_with_slots):
        result = await _tool_configure_matrix(active_input=1)
    assert result["ok"]
    assert result["active_input"] == 1
    # Other input (0) should be fully muted in the routing call
    call_args = mock_dsp.set_routing.call_args[0][0]
    assert all(not v for v in call_args[0].values()), "input 0 should be fully muted"


@pytest.mark.asyncio
async def test_configure_matrix_driver_error(mock_dsp, mock_config_with_slots) -> None:
    mock_dsp.set_routing.side_effect = DriverError("routing hw failure")
    with patch("calibrate.mcp_server._config", return_value=mock_config_with_slots):
        result = await _tool_configure_matrix()
    assert not result["ok"]
    assert "routing hw failure" in result["error"]


# ── analyze_decay ─────────────────────────────────────────────────────────────

def _make_session_with_ringing_ir(session_id: int = 1) -> MagicMock:
    """Build a mock Session with a ringing 50Hz IR."""
    import numpy as np
    n = 48000 * 2
    t = np.arange(n) / 48000
    ir = (np.sin(2 * np.pi * 50 * t) * np.exp(-6.9 / 0.6 * t)).tolist()
    ir[0] = 1.0

    mock_fr = MagicMock()
    mock_fr.sample_rate = 48000

    session = MagicMock()
    session.id = session_id
    session.start_fr = mock_fr
    session.impulse_response = ir
    return session


def _make_session_clean_ir(session_id: int = 1) -> MagicMock:
    """Build a mock Session with a clean (non-ringing) IR."""
    import numpy as np
    n = 48000
    t = np.arange(n) / 48000
    rng = np.random.default_rng(42)
    ir = (rng.normal(0, 1.0, n) * np.exp(-6.9 / 0.005 * t)).tolist()
    ir[0] = 1.0

    mock_fr = MagicMock()
    mock_fr.sample_rate = 48000

    session = MagicMock()
    session.id = session_id
    session.start_fr = mock_fr
    session.impulse_response = ir
    return session


@pytest.mark.asyncio
async def test_analyze_decay_latest_session() -> None:
    session = _make_session_with_ringing_ir(session_id=5)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_analyze_decay()
    assert result["ok"]
    assert result["session_id"] == 5
    assert result["mode_count"] >= 1
    first = result["modes"][0]
    assert "freq_hz" in first
    assert "t60_ms" in first
    assert "suggested_q" in first
    assert first["priority"] == 1


@pytest.mark.asyncio
async def test_analyze_decay_by_session_id() -> None:
    older = _make_session_with_ringing_ir(session_id=3)
    newer = _make_session_clean_ir(session_id=7)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [newer, older]
        result = await _tool_analyze_decay(session_id=3)
    assert result["ok"]
    assert result["session_id"] == 3


@pytest.mark.asyncio
async def test_analyze_decay_session_not_found() -> None:
    session = _make_session_with_ringing_ir(session_id=1)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_analyze_decay(session_id=9999)
    assert not result["ok"]
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_analyze_decay_no_sessions() -> None:
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = []
        result = await _tool_analyze_decay()
    assert not result["ok"]
    assert "no measurements found" in result["error"]


@pytest.mark.asyncio
async def test_analyze_decay_session_missing_ir() -> None:
    session = MagicMock()
    session.id = 1
    session.start_fr = MagicMock()
    session.impulse_response = None
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_analyze_decay()
    assert not result["ok"]
    assert "no impulse response" in result["error"]


@pytest.mark.asyncio
async def test_analyze_decay_clean_ir() -> None:
    session = _make_session_clean_ir(session_id=2)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_analyze_decay(t60_threshold_ms=300.0)
    assert result["ok"]
    assert result["mode_count"] == 0
    assert result["modes"] == []


@pytest.mark.asyncio
async def test_analyze_decay_threshold_param() -> None:
    """A mode with T60~600ms should be filtered out when threshold is 800ms."""
    session = _make_session_with_ringing_ir(session_id=1)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_analyze_decay(t60_threshold_ms=2000.0)
    assert result["ok"]
    assert result["mode_count"] == 0


@pytest.mark.asyncio
async def test_analyze_decay_freq_range_param() -> None:
    """A 50Hz mode should not appear when freq_min=80."""
    session = _make_session_with_ringing_ir(session_id=1)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_analyze_decay(freq_min=80.0, freq_max=200.0)
    assert result["ok"]
    # 50Hz mode should be excluded from the 80-200Hz range
    for mode in result["modes"]:
        assert mode["freq_hz"] >= 80.0


@pytest.mark.asyncio
async def test_call_tool_analyze_decay_dispatch() -> None:
    from calibrate.mcp_server import call_tool
    session = _make_session_with_ringing_ir(session_id=1)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        texts = await call_tool("analyze_decay", {})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "mode_count" in data
    assert "modes" in data


# ── get_measurement_history — frequency filtering ─────────────────────────────

def _make_fr_session(freqs: list[float], spls: list[float], session_id: int = 1) -> MagicMock:
    mock_fr = MagicMock()
    mock_fr.frequencies = freqs
    mock_fr.spl = spls
    mock_fr.phase = None
    session = MagicMock()
    session.id = session_id
    session.timestamp = "2026-04-10T00:00:00Z"
    session.label = f"session-{session_id}"
    session.start_fr = mock_fr
    session.metadata = None
    return session


@pytest.mark.asyncio
async def test_get_measurement_history_min_hz_filters_low_freqs() -> None:
    freqs = [10.0, 20.0, 50.0, 100.0, 200.0]
    spls  = [ 1.0,  2.0,  3.0,   4.0,   5.0]
    session = _make_fr_session(freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_get_measurement_history(limit=1, min_hz=20.0)
    assert result["ok"]
    data = result["sessions"][0]
    assert data["freq_hz"] == [20.0, 50.0, 100.0, 200.0]
    assert data["spl_db"] == [2.0, 3.0, 4.0, 5.0]


@pytest.mark.asyncio
async def test_get_measurement_history_max_hz_filters_high_freqs() -> None:
    freqs = [10.0, 20.0, 50.0, 100.0, 200.0]
    spls  = [ 1.0,  2.0,  3.0,   4.0,   5.0]
    session = _make_fr_session(freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_get_measurement_history(limit=1, max_hz=100.0)
    data = result["sessions"][0]
    assert data["freq_hz"] == [10.0, 20.0, 50.0, 100.0]
    assert data["spl_db"] == [1.0, 2.0, 3.0, 4.0]


@pytest.mark.asyncio
async def test_get_measurement_history_min_max_hz_combined() -> None:
    freqs = [10.0, 20.0, 50.0, 100.0, 200.0]
    spls  = [ 1.0,  2.0,  3.0,   4.0,   5.0]
    session = _make_fr_session(freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_get_measurement_history(limit=1, min_hz=20.0, max_hz=100.0)
    data = result["sessions"][0]
    assert data["freq_hz"] == [20.0, 50.0, 100.0]
    assert data["spl_db"] == [2.0, 3.0, 4.0]


@pytest.mark.asyncio
async def test_get_measurement_history_decimation() -> None:
    freqs = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]
    spls  = [ 1.0,  2.0,  3.0,  4.0,  5.0,  6.0]
    session = _make_fr_session(freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_get_measurement_history(limit=1, decimation=2)
    data = result["sessions"][0]
    assert data["freq_hz"] == [10.0, 30.0, 50.0]
    assert data["spl_db"] == [1.0, 3.0, 5.0]


@pytest.mark.asyncio
async def test_get_measurement_history_rounds_floats() -> None:
    freqs = [20.1416015625, 40.2832031250]
    spls  = [-5.12345678,   -3.98765432]
    session = _make_fr_session(freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_get_measurement_history(limit=1)
    data = result["sessions"][0]
    assert data["freq_hz"] == [20.14, 40.28]
    assert data["spl_db"] == [-5.12, -3.99]


@pytest.mark.asyncio
async def test_get_measurement_history_compact_format() -> None:
    freqs = [20.0, 40.0, 80.0]
    spls  = [-1.5,  2.3,  0.0]
    session = _make_fr_session(freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_get_measurement_history(limit=1, fmt="compact")
    data = result["sessions"][0]
    assert "fr" in data
    assert "freq_hz" not in data
    assert "spl_db" not in data
    assert data["fr"] == "20.00:-1.5,40.00:2.3,80.00:0.0"
    assert data["point_count"] == 3


@pytest.mark.asyncio
async def test_get_measurement_history_compact_with_range() -> None:
    """Compact format + range filter together — the primary bass calibration mode."""
    freqs = [10.0, 20.0, 50.0, 100.0, 200.0]
    spls  = [ 1.0,  2.0,  3.0,   4.0,   5.0]
    session = _make_fr_session(freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_get_measurement_history(
            limit=1, min_hz=20.0, max_hz=100.0, fmt="compact"
        )
    data = result["sessions"][0]
    assert data["fr"] == "20.00:2.0,50.00:3.0,100.00:4.0"
    assert data["point_count"] == 3


@pytest.mark.asyncio
async def test_get_measurement_history_compact_strips_group_delay() -> None:
    """group_delay is ~17KB per session; compact mode must exclude it."""
    freqs = [20.0, 40.0]
    spls  = [1.0, 2.0]
    session = _make_fr_session(freqs, spls)
    session.metadata = {
        "ir": {"peak_time_ms": 5.0, "spl_db": 80.0},
        "group_delay": {"freq_hz": list(range(500)), "gd_ms": list(range(500))},
        "position": "MLP",
    }
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_get_measurement_history(limit=1, fmt="compact")
    data = result["sessions"][0]
    assert "metadata" in data
    assert "group_delay" not in data["metadata"]
    assert "ir" in data["metadata"]
    assert "position" in data["metadata"]


@pytest.mark.asyncio
async def test_get_measurement_history_full_keeps_group_delay() -> None:
    """Full mode returns group_delay unchanged."""
    freqs = [20.0, 40.0]
    spls  = [1.0, 2.0]
    session = _make_fr_session(freqs, spls)
    session.metadata = {
        "group_delay": {"freq_hz": [20.0], "gd_ms": [1.0]},
    }
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_get_measurement_history(limit=1, fmt="full")
    data = result["sessions"][0]
    assert "group_delay" in data["metadata"]
