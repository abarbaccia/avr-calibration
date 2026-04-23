"""Tests for calibrate.mcp_server — all tools and resources.

All hardware is mocked at the driver level (AVRDriver / DSPDriver).
No real network, hardware, or file access required.

Covers:
  - get_device_state: connected + unreachable for both drivers
  - get_measurement_history: returns sessions, handles empty, handles storage error
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
  - resources: measurements://latest
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
    _downsample_coherence,
    _downsample_group_delay,
    _downsample_to_third_octave,
    _read_resource,
    _tool_analyze_decay,
    _tool_analyze_ir,
    _tool_analyze_phase,
    _tool_apply_eq,
    _tool_apply_fir,
    _tool_avr_set_volume,
    _tool_calibrate_level,
    _tool_check_system,
    _tool_clear_fir,
    _tool_compare_sessions,
    _tool_compare_sub_phase,
    _tool_compute_deviation,
    _tool_configure_matrix,
    _tool_design_fir,
    _tool_fetch_recipe,
    _tool_get_device_state,
    _tool_get_fr_summary,
    _tool_get_measurement_history,
    _tool_get_output_state,
    _tool_mute_output,
    _tool_optimize_q,
    _tool_set_delay,
    _tool_set_master_gain,
    _tool_set_output_gain,
    _tool_set_polarity,
    _tool_simulate_eq,
    _tool_trigger_measurement,
    _tool_unmute_output,
    _tool_evaluate_transfer_function,
    _tool_per_filter_contribution,
    _tool_interpolate_optimal_gain,
    _tool_sensitivity_analysis,
    _tool_fit_correction_filter,
    _tool_predict_rms,
    _tool_recommend_fir_phase,
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
    """Patch _dsp with an AsyncMock DSPDriver."""
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

    async def apply_eq(preset: int, filters: list[dict], output_index: int | None = None, simulation_verified: bool = False) -> None:
        return None

    async def apply_input_eq(preset: int, filters: list[dict], input_index: int | None = None, simulation_verified: bool = False) -> None:
        return None

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
        result = await _tool_get_measurement_history(limit=5, fmt="full")

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
    from calibrate.config import Config, DEFAULT_CONFIG

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

    mock_dsp_local = AsyncMock()
    mock_dsp_local.get_state = AsyncMock(return_value={"source": "Analog", "volume": 0.0})

    # Force playback_route=hdmi so the test exercises the Denon path regardless
    # of what the local ~/.avr-calibration/config.yaml happens to say.
    hdmi_cfg_data = {k: (dict(v) if isinstance(v, dict) else v)
                     for k, v in DEFAULT_CONFIG.items()}
    hdmi_cfg_data["measurement"] = {**hdmi_cfg_data["measurement"],
                                     "playback_route": "hdmi"}

    with (
        patch.dict(sys.modules, {"sounddevice": mock_sd}),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.measurement.compute_session_metadata", return_value={"ir": {}}),
        patch("calibrate.storage.SessionStore", return_value=mock_store),
        patch.object(sut, "DenonSweepContext") as MockCtx,
        patch.object(sut, "_dsp", mock_dsp_local),
        patch.object(sut, "_config", return_value=Config(hdmi_cfg_data)),
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

def _make_fr(spl_db: float) -> MagicMock:
    """Build a mock FrequencyResponse with recording_rms_dbfs set.

    _ir_spl() computes recording_rms_dbfs + mic_offset.
    In tests the mic offset is 0 (no cal file), so SPL = recording_rms_dbfs.
    """
    fr = MagicMock()
    fr.recording_rms_dbfs = spl_db
    fr.recording_peak_dbfs = spl_db  # fallback field
    return fr


def _make_usb_cfg():
    """Return a mock config that reports USB playback route."""
    mock_cfg = MagicMock()
    mock_cfg.measurement.get.side_effect = lambda key, default=None: (
        "usb" if key == "playback_route" else default
    )
    # No mic cal file → sensitivity offset = 0 → SPL = recording_peak_dbfs
    mock_cfg._data = {}
    return mock_cfg


def _make_hdmi_cfg():
    """Return a mock config that reports HDMI playback route."""
    mock_cfg = MagicMock()
    mock_cfg.measurement.get.side_effect = lambda key, default=None: (
        "hdmi" if key == "playback_route" else default
    )
    mock_cfg._data = {}
    return mock_cfg


# ── USB mode tests ──


@pytest.mark.asyncio
async def test_calibrate_level_usb_predict_verify() -> None:
    """USB mode: probe IR peak = 58 dB SPL at -30 dB gain.
    Target 78 dB SPL. Correction = +20 → gain = -10.
    Verify IR peak = 78 dB SPL. 2 sweeps."""
    mock_dsp = AsyncMock()
    mock_dsp.sweep_context = MagicMock(return_value=None)

    probe_fr = _make_fr(58.0)  # 58 dB SPL at -30 gain
    verify_fr = _make_fr(78.0)  # 78 dB SPL — on target

    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(side_effect=[probe_fr, verify_fr])

    with (
        patch.object(sut, "_dsp", mock_dsp),
        patch.object(sut, "_config", return_value=_make_usb_cfg()),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.config.update_config") as mock_update,
        patch("asyncio.sleep"),
    ):
        result = await _tool_calibrate_level(target_spl_db=78.0, start_db=-30.0)

    assert result["ok"]
    # correction = 78 - 58 = +20 → gain = -30 + 20 = -10
    assert result["calibrated_master_gain_db"] == -10.0
    assert result["estimated_spl_db"] == 78.0
    assert mock_engine.measure.call_count == 2
    assert mock_dsp.set_master_gain.call_count == 2
    mock_update.assert_called_once_with({"measurement": {"master_gain_db": -10.0}})


@pytest.mark.asyncio
async def test_calibrate_level_usb_verify_still_hot_backs_off() -> None:
    """USB mode: verify IR peak >3 dB above target → backs off."""
    mock_dsp = AsyncMock()
    mock_dsp.sweep_context = MagicMock(return_value=None)

    probe_fr = _make_fr(90.0)  # 90 dB SPL
    verify_fr = _make_fr(85.0)  # 85 dB > 78+3 → backs off

    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(side_effect=[probe_fr, verify_fr])

    with (
        patch.object(sut, "_dsp", mock_dsp),
        patch.object(sut, "_config", return_value=_make_usb_cfg()),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.config.update_config"),
        patch("asyncio.sleep"),
    ):
        result = await _tool_calibrate_level(target_spl_db=78.0, start_db=-10.0)

    assert result["ok"]
    # probe: correction = 78 - 90 = -12 → computed gain = -10 + (-12) = -22
    # verify: 85 > 78+3 → overshoot = 85 - 78 = 7 → final = -22 - 7 = -29
    assert result["calibrated_master_gain_db"] == -29.0
    # 3 set_master_gain calls: probe(-10), verify(-22), backoff(-29)
    assert mock_dsp.set_master_gain.call_count == 3


@pytest.mark.asyncio
async def test_calibrate_level_usb_gain_clamped_to_zero() -> None:
    """USB mode: computed gain would exceed 0 dB → clamped."""
    mock_dsp = AsyncMock()
    mock_dsp.sweep_context = MagicMock(return_value=None)

    probe_fr = _make_fr(40.0)  # very quiet
    verify_fr = _make_fr(70.0)

    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(side_effect=[probe_fr, verify_fr])

    with (
        patch.object(sut, "_dsp", mock_dsp),
        patch.object(sut, "_config", return_value=_make_usb_cfg()),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.config.update_config"),
        patch("asyncio.sleep"),
    ):
        result = await _tool_calibrate_level(target_spl_db=78.0, start_db=-30.0)

    assert result["ok"]
    # correction = 78 - 40 = +38 → gain = -30 + 38 = +8 → clamped to 0
    assert result["calibrated_master_gain_db"] == 0.0


@pytest.mark.asyncio
async def test_calibrate_level_usb_snr_fail_on_probe() -> None:
    """USB mode: SNR too low on probe → error directing user to physical knob."""
    from calibrate.measurement import MeasurementQualityError

    mock_dsp = AsyncMock()
    mock_dsp.sweep_context = MagicMock(return_value=None)
    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(
        side_effect=MeasurementQualityError("snr", "SNR 8 dB < 20 dB", "increase level"),
    )

    with (
        patch.object(sut, "_dsp", mock_dsp),
        patch.object(sut, "_config", return_value=_make_usb_cfg()),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.config.update_config"),
        patch("asyncio.sleep"),
    ):
        result = await _tool_calibrate_level(start_db=-30.0)

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


@pytest.mark.asyncio
async def test_calibrate_level_usb_solo_gain_hint() -> None:
    """USB mode with 2 subs: suggested_solo_gain_db is higher by ~6 dB."""
    mock_dsp = AsyncMock()
    mock_dsp.sweep_context = MagicMock(return_value=None)
    mock_cfg = _make_usb_cfg()
    mock_cfg.minidsp.get.side_effect = lambda key, default=None: (
        [{"type": "sub"}, {"type": "sub"}, {"type": "shaker"}]
        if key == "output_slots" else default
    )

    probe_fr = _make_fr(68.0)
    verify_fr = _make_fr(78.0)

    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(side_effect=[probe_fr, verify_fr])

    with (
        patch.object(sut, "_dsp", mock_dsp),
        patch.object(sut, "_config", return_value=mock_cfg),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.config.update_config"),
        patch("asyncio.sleep"),
    ):
        result = await _tool_calibrate_level(target_spl_db=78.0, start_db=-20.0)

    assert result["ok"]
    assert result["suggested_solo_gain_db"] > result["calibrated_master_gain_db"]


# ── HDMI mode tests ──


@pytest.mark.asyncio
async def test_calibrate_level_hdmi_predict_verify() -> None:
    """HDMI mode: probe at -30 dB, predict correction to 78 dB SPL, verify."""
    mock_avr = AsyncMock()

    probe_fr = _make_fr(58.0)  # 58 dB SPL
    verify_fr = _make_fr(78.0)  # 78 dB SPL

    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(side_effect=[probe_fr, verify_fr])

    with (
        patch.object(sut, "_avr", mock_avr),
        patch.object(sut, "_config", return_value=_make_hdmi_cfg()),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.config.update_config") as mock_update,
    ):
        result = await _tool_calibrate_level(target_spl_db=78.0, start_db=-30.0)

    assert result["ok"]
    # correction = 78 - 58 = +20 → volume = -30 + 20 = -10
    assert result["calibrated_volume_db"] == -10.0
    assert result["estimated_spl_db"] == 78.0
    assert mock_engine.measure.call_count == 2
    mock_update.assert_called_once_with({"measurement": {"denon_sweep_volume": -10.0}})


@pytest.mark.asyncio
async def test_calibrate_level_hdmi_probe_snr_fail() -> None:
    """HDMI mode: probe sweep SNR too low → error."""
    from calibrate.measurement import MeasurementQualityError

    mock_avr = AsyncMock()
    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(
        side_effect=MeasurementQualityError("snr", "SNR 5 dB", "too quiet"),
    )

    with (
        patch.object(sut, "_avr", mock_avr),
        patch.object(sut, "_config", return_value=_make_hdmi_cfg()),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.config.update_config"),
    ):
        result = await _tool_calibrate_level(start_db=-30.0)

    assert not result["ok"]
    assert "SNR too low" in result["error"]


@pytest.mark.asyncio
async def test_calibrate_level_hdmi_verify_hot_backs_off() -> None:
    """HDMI mode: verify IR peak >3 dB above target → backs off volume."""
    mock_avr = AsyncMock()

    probe_fr = _make_fr(95.0)
    verify_fr = _make_fr(85.0)  # 85 > 78+3 → backs off

    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(side_effect=[probe_fr, verify_fr])

    with (
        patch.object(sut, "_avr", mock_avr),
        patch.object(sut, "_config", return_value=_make_hdmi_cfg()),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.config.update_config"),
    ):
        result = await _tool_calibrate_level(target_spl_db=78.0, start_db=-20.0)

    assert result["ok"]
    # probe: correction = 78 - 95 = -17 → volume = -20 + (-17) = -37
    # verify: 85 > 78+3 → overshoot = 85 - 78 = 7 → final = -37 - 7 = -44
    assert result["calibrated_volume_db"] == -44.0


@pytest.mark.asyncio
async def test_calibrate_level_hdmi_volume_clamped_to_max() -> None:
    """HDMI mode: computed volume exceeds max_volume_db → clamped."""
    mock_avr = AsyncMock()

    probe_fr = _make_fr(30.0)  # very quiet
    verify_fr = _make_fr(73.0)

    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(side_effect=[probe_fr, verify_fr])

    with (
        patch.object(sut, "_avr", mock_avr),
        patch.object(sut, "_config", return_value=_make_hdmi_cfg()),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.config.update_config"),
    ):
        result = await _tool_calibrate_level(
            target_spl_db=78.0, start_db=-40.0, max_volume_db=-5.0
        )

    assert result["ok"]
    # correction = 78 - 30 = +48 → volume = -40 + 48 = +8 → clamped to -5
    assert result["calibrated_volume_db"] == -5.0


@pytest.mark.asyncio
async def test_calibrate_level_no_avr_hdmi_mode() -> None:
    """HDMI mode with no AVR driver loaded → error."""
    with (
        patch.object(sut, "_avr", None),
        patch.object(sut, "_config", return_value=_make_hdmi_cfg()),
    ):
        result = await _tool_calibrate_level()

    assert not result["ok"]
    assert "AVR driver not loaded" in result["error"]


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
async def test_trigger_measurement_sounddevice_unavailable() -> None:
    """sounddevice not available → error."""
    with patch.dict(sys.modules, {"sounddevice": None}):
        result = await _tool_trigger_measurement()
    assert not result["ok"]
    assert "sounddevice" in result["error"] or "audio" in result["error"].lower()


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


@pytest.mark.asyncio
async def test_read_resource_handler_delegates() -> None:
    """read_resource() MCP handler delegates to _read_resource() for unknown URIs."""
    from calibrate.mcp_server import read_resource
    result = await read_resource("unknown://foo")
    data = json.loads(result)
    assert "error" in data


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


# ── end_sweep_session ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_end_sweep_session_no_active_session() -> None:
    """end_sweep_session returns ok when no session is active."""
    import calibrate.mcp_server as srv
    srv._sweep_session = None
    result = await srv._tool_end_sweep_session()
    assert result["ok"]
    assert "No active" in result["message"]


@pytest.mark.asyncio
async def test_end_sweep_session_dispatch() -> None:
    """end_sweep_session routes through call_tool dispatch."""
    from calibrate.mcp_server import call_tool
    import calibrate.mcp_server as srv
    srv._sweep_session = None
    texts = await call_tool("end_sweep_session", {})
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
    mock_fr.xcorr_peak_ms = round(peak_time_s * 1000.0, 3)

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
async def test_apply_fir_via_design_session_id(mock_dsp) -> None:
    """apply_fir accepts a design_session_id that maps to a cached design."""
    from calibrate import mcp_server as _mod
    coeffs = [0.1, 0.2, 0.3, 0.4, 0.5]
    _mod._fir_design_cache[42] = coeffs
    try:
        result = await _tool_apply_fir(output_index=1, design_session_id=42)
    finally:
        _mod._fir_design_cache.pop(42, None)
    assert result["ok"]
    assert result["taps"] == 5
    assert result["source"] == "design_session_id=42"
    mock_dsp.apply_fir.assert_awaited_once_with(1, coeffs)


@pytest.mark.asyncio
async def test_apply_fir_rejects_both_sources(mock_dsp) -> None:
    result = await _tool_apply_fir(
        output_index=0, coefficients=[1.0], design_session_id=1,
    )
    assert not result["ok"]
    assert "not both" in result["error"]


@pytest.mark.asyncio
async def test_apply_fir_rejects_missing_source(mock_dsp) -> None:
    result = await _tool_apply_fir(output_index=0)
    assert not result["ok"]
    assert "provide either" in result["error"]


@pytest.mark.asyncio
async def test_apply_fir_missing_design_in_cache(mock_dsp) -> None:
    from calibrate import mcp_server as _mod
    _mod._fir_design_cache.pop(9999, None)
    result = await _tool_apply_fir(output_index=0, design_session_id=9999)
    assert not result["ok"]
    assert "no cached design" in result["error"]


@pytest.mark.asyncio
async def test_apply_fir_unsafe_boost_rejected(mock_dsp) -> None:
    """apply_fir with a FIR that boosts +10 dB at 60 Hz must return ok=false.

    The driver path raises DriverError when its SafetyValidator.validate_fir
    rejects the coefficients; the tool surfaces that as a structured error.
    """
    import numpy as np
    from calibrate.drivers.base import DriverError

    # Build a FIR that boosts +10 dB in the 63 Hz 1/3-octave band (exceeds
    # the +8 dB thermal ceiling).
    n_taps = 32_768
    rate = 48_000
    freqs = np.fft.rfftfreq(n_taps, d=1.0 / rate)
    mag = np.ones_like(freqs)
    half_step = 2.0 ** (1.0 / 6.0)
    mask = (freqs >= 63.0 / half_step) & (freqs <= 63.0 * half_step)
    mag[mask] = 10.0 ** (10.0 / 20.0)
    taps = np.fft.fftshift(np.fft.irfft(mag, n=n_taps)).tolist()

    # Make the underlying driver raise DriverError when validate_fir rejects
    # — this mirrors what camilladsp/minidsp apply_fir does in production.
    mock_dsp.apply_fir.side_effect = DriverError(
        "SafetyValidator: FIR boost of +10.0 dB at 63 Hz 1/3-octave band "
        "exceeds thermal ceiling of +8 dB (profile 'svs_pb12_nsd')"
    )

    result = await _tool_apply_fir(output_index=1, coefficients=taps)
    assert not result["ok"]
    assert "SafetyValidator" in result["error"]
    assert "dB" in result["error"]


@pytest.mark.asyncio
async def test_apply_fir_safe_coefficients_proceed(mock_dsp) -> None:
    """apply_fir with a flat-magnitude FIR must proceed to the driver cleanly."""
    coeffs = [1.0] + [0.0] * 127  # impulse → flat 0 dB
    result = await _tool_apply_fir(output_index=1, coefficients=coeffs)
    assert result["ok"]
    mock_dsp.apply_fir.assert_awaited_once_with(1, coeffs)


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
    cfg.active_input = 0  # driver-neutral accessor; see Config.active_input
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


# ── recommend_fir_phase (Phase 2.5a decision) ───────────────────────────────

@pytest.mark.asyncio
async def test_recommend_fir_phase_recommends_mixed_for_long_t60() -> None:
    """A 50 Hz mode with long T60 triggers a 'mixed' recommendation with taps
    sized so the FIR impulse covers at least 2× the worst T60."""
    session = _make_session_with_ringing_ir(session_id=1)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_recommend_fir_phase(session_id=1, t60_threshold_ms=300.0)
    assert result["ok"], result
    assert result["recommendation"] == "mixed"
    assert len(result["offending_modes"]) >= 1
    # suggested_num_taps should be a power of two, ≥ 8192, and give an impulse
    # length at least equal to the worst T60 at fir_fs = 48 kHz (fallback).
    taps = result["suggested_num_taps"]
    assert taps & (taps - 1) == 0  # power of two
    assert taps >= 8192
    worst_t60 = max(m["t60_ms"] for m in result["offending_modes"])
    impulse_ms = taps / 48_000 * 1000
    assert impulse_ms >= worst_t60  # 2× was the target, ≥1× is the hard floor


@pytest.mark.asyncio
async def test_recommend_fir_phase_recommends_minimum_when_clean() -> None:
    """A clean IR with no ringing modes → recommendation: minimum."""
    session = _make_session_clean_ir(session_id=5)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_recommend_fir_phase(session_id=5)
    assert result["ok"]
    assert result["recommendation"] == "minimum"
    assert result["offending_modes"] == []


@pytest.mark.asyncio
async def test_recommend_fir_phase_threshold_gate() -> None:
    """Setting a very high t60 threshold rules out all modes → minimum."""
    session = _make_session_with_ringing_ir(session_id=1)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_recommend_fir_phase(
            session_id=1, t60_threshold_ms=10_000.0,
        )
    assert result["ok"]
    assert result["recommendation"] == "minimum"


@pytest.mark.asyncio
async def test_recommend_fir_phase_session_not_found() -> None:
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = []
        result = await _tool_recommend_fir_phase(session_id=9999)
    assert not result["ok"]
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_recommend_fir_phase_missing_ir() -> None:
    session = MagicMock()
    session.id = 1
    session.impulse_response = None
    session.start_fr = MagicMock()
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_recommend_fir_phase(session_id=1)
    assert not result["ok"]
    assert "no impulse response" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_recommend_fir_phase_dispatch() -> None:
    from calibrate.mcp_server import call_tool
    session = _make_session_with_ringing_ir(session_id=1)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        texts = await call_tool(
            "recommend_fir_phase",
            {"session_id": 1, "t60_threshold_ms": 300.0},
        )
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert data["recommendation"] in {"minimum", "mixed"}


@pytest.mark.asyncio
async def test_recommend_fir_phase_suggests_preringing_and_latency() -> None:
    """When recommending mixed, the response includes suggested_preringing_ms,
    estimated_latency_ms, and whether it fits in the AVR audio-delay budget."""
    session = _make_session_with_ringing_ir(session_id=1)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_recommend_fir_phase(
            session_id=1,
            t60_threshold_ms=300.0,
            preringing_ms=25.0,
            audio_delay_budget_ms=200.0,
        )
    assert result["ok"]
    assert result["recommendation"] == "mixed"
    assert result["suggested_preringing_ms"] == 25.0
    assert result["estimated_latency_ms"] == pytest.approx(25.0, abs=1.0)
    assert result["fits_in_budget"] is True
    assert result["audio_delay_budget_ms"] == 200.0


@pytest.mark.asyncio
async def test_recommend_fir_phase_clamps_preringing_to_budget() -> None:
    """If preringing_ms exceeds audio_delay_budget_ms, clamp and flag."""
    session = _make_session_with_ringing_ir(session_id=1)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_recommend_fir_phase(
            session_id=1,
            t60_threshold_ms=300.0,
            preringing_ms=500.0,
            audio_delay_budget_ms=200.0,
        )
    assert result["ok"]
    assert result["recommendation"] == "mixed"
    assert result["suggested_preringing_ms"] == 200.0  # clamped
    assert result["fits_in_budget"] is False
    assert "exceeds" in result["note"].lower() or "warning" in result["note"].lower()


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
        result = await _tool_get_measurement_history(limit=1, min_hz=20.0, fmt="full")
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
        result = await _tool_get_measurement_history(limit=1, max_hz=100.0, fmt="full")
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
        result = await _tool_get_measurement_history(limit=1, min_hz=20.0, max_hz=100.0, fmt="full")
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
        result = await _tool_get_measurement_history(limit=1, decimation=2, fmt="full")
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
        result = await _tool_get_measurement_history(limit=1, fmt="full")
    data = result["sessions"][0]
    assert data["freq_hz"] == [20.14, 40.28]
    assert data["spl_db"] == [-5.12, -3.99]


# ── compute_deviation ────────────────────────────────────────────────────────


def _make_deviation_session(session_id: int, freqs: list[float], spls: list[float]) -> MagicMock:
    """Build a mock session with given FR data for deviation tests."""
    mock_fr = MagicMock()
    mock_fr.frequencies = freqs
    mock_fr.spl = spls
    session = MagicMock()
    session.id = session_id
    session.start_fr = mock_fr
    session.label = f"dev-{session_id}"
    return session


@pytest.mark.asyncio
async def test_compute_deviation_basic() -> None:
    """Simple case: measured matches target within ~1 dB → converged."""
    freqs = [30.0, 40.0, 50.0, 63.0, 80.0]
    # Target: flat at 75 dB
    target = {"points": [{"freq": 20, "spl": 75.0}, {"freq": 100, "spl": 75.0}], "band": [20, 100]}
    # Measured: slightly above target
    spls = [75.5, 75.8, 74.5, 75.2, 74.9]
    session = _make_deviation_session(1, freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_compute_deviation(session_id=1, target_curve=target)
    assert result["ok"]
    assert result["converged"] is True
    assert result["rms_db"] < 2.0
    assert result["included_points"] == 5
    assert result["session_id"] == 1


@pytest.mark.asyncio
async def test_compute_deviation_null_zone_excluded() -> None:
    """Frequencies with SPL far below band average are excluded as null zones."""
    freqs = [30.0, 40.0, 50.0, 63.0, 80.0]
    target = {"points": [{"freq": 20, "spl": 75.0}, {"freq": 100, "spl": 75.0}], "band": [20, 100]}
    # 50 Hz is a deep null — 20 dB below everything else
    spls = [74.0, 75.0, 55.0, 76.0, 75.0]
    session = _make_deviation_session(1, freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_compute_deviation(
            session_id=1, target_curve=target, null_threshold_db=15.0
        )
    assert result["ok"]
    assert result["excluded_null_points"] >= 1
    # The null at 50 Hz should appear in null_zones
    null_freqs = []
    for zone in result["null_zones"]:
        null_freqs.extend([zone["lo_hz"], zone["hi_hz"]])
    assert 50.0 in null_freqs


@pytest.mark.asyncio
async def test_compute_deviation_rolloff_excluded() -> None:
    """Frequencies below port_rolloff_hz are excluded."""
    freqs = [22.0, 25.0, 30.0, 40.0, 50.0, 63.0, 80.0]
    target = {"points": [{"freq": 20, "spl": 75.0}, {"freq": 100, "spl": 75.0}], "band": [20, 100]}
    spls = [65.0, 70.0, 74.0, 75.0, 75.0, 76.0, 75.0]
    session = _make_deviation_session(1, freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_compute_deviation(
            session_id=1, target_curve=target, port_rolloff_hz=28.0
        )
    assert result["ok"]
    assert result["excluded_rolloff_points"] >= 2  # 22 Hz and 25 Hz


@pytest.mark.asyncio
async def test_compute_deviation_not_converged() -> None:
    """RMS > 2.0 → converged is False."""
    freqs = [30.0, 40.0, 50.0, 63.0, 80.0]
    target = {"points": [{"freq": 20, "spl": 75.0}, {"freq": 100, "spl": 75.0}], "band": [20, 100]}
    # Measured: far from target
    spls = [80.0, 70.0, 82.0, 68.0, 81.0]
    session = _make_deviation_session(1, freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_compute_deviation(session_id=1, target_curve=target)
    assert result["ok"]
    assert result["converged"] is False
    assert result["rms_db"] > 2.0


@pytest.mark.asyncio
async def test_compute_deviation_session_not_found() -> None:
    """Session ID not in store → error."""
    target = {"points": [{"freq": 20, "spl": 75.0}, {"freq": 100, "spl": 75.0}], "band": [20, 100]}
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = []
        result = await _tool_compute_deviation(session_id=99, target_curve=target)
    assert not result["ok"]
    assert "session 99 not found" in result["error"]


@pytest.mark.asyncio
async def test_compute_deviation_empty_target_points() -> None:
    """Target curve with no points → error."""
    freqs = [30.0, 40.0, 50.0]
    spls = [75.0, 75.0, 75.0]
    session = _make_deviation_session(1, freqs, spls)
    target = {"points": [], "band": [20, 100]}
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_compute_deviation(session_id=1, target_curve=target)
    assert not result["ok"]
    assert "points" in result["error"]


@pytest.mark.asyncio
async def test_compute_deviation_returns_summary_bands() -> None:
    """Result includes per-1/3-octave summary with error values."""
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(100), 200).tolist()
    target = {"points": [{"freq": 20, "spl": 75.0}, {"freq": 100, "spl": 75.0}], "band": [20, 100]}
    spls = [77.0] * len(freqs)  # 2 dB above target everywhere
    session = _make_deviation_session(1, freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_compute_deviation(session_id=1, target_curve=target)
    assert result["ok"]
    assert len(result["summary"]) > 0
    for band in result["summary"]:
        assert "freq_hz" in band
        assert "error_db" in band
        assert abs(band["error_db"] - 2.0) < 0.5  # roughly +2 dB error


@pytest.mark.asyncio
async def test_call_tool_compute_deviation_dispatch() -> None:
    """call_tool('compute_deviation') dispatches correctly."""
    from calibrate.mcp_server import call_tool
    freqs = [30.0, 40.0, 50.0, 63.0, 80.0]
    spls = [75.0, 75.0, 75.0, 75.0, 75.0]
    target = {"points": [{"freq": 20, "spl": 75.0}, {"freq": 100, "spl": 75.0}], "band": [20, 100]}
    session = _make_deviation_session(1, freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        texts = await call_tool("compute_deviation", {
            "session_id": 1,
            "target_curve": target,
        })
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "rms_db" in data
    assert "converged" in data


@pytest.mark.asyncio
async def test_compute_deviation_exclude_geometry_false_keeps_geometry_bands() -> None:
    """With exclude_geometry=False, geometry-classified bands count against RMS.

    Safety net: confirms the new auto-exclusion is opt-out-able. Uses a
    mock session with no phase data so analyze_phase returns nothing —
    behavior should match pre-change.
    """
    freqs = [30.0, 40.0, 50.0, 63.0, 80.0]
    target = {"points": [{"freq": 20, "spl": 75.0}, {"freq": 100, "spl": 75.0}], "band": [20, 100]}
    spls = [75.5, 75.8, 74.5, 75.2, 74.9]
    session = _make_deviation_session(1, freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_compute_deviation(
            session_id=1, target_curve=target, exclude_geometry=False,
        )
    assert result["ok"]
    # No phase data → no geometry bands exist either way.
    assert result["excluded_geometry_points"] == 0
    assert result["geometry_bands"] == []


@pytest.mark.asyncio
async def test_compute_deviation_exclude_geometry_true_auto_excludes() -> None:
    """With exclude_geometry=True (default) and phase data classifying a
    band as 'geometry', compute_deviation excludes it from RMS automatically.

    Patches _get_geometry_band_ranges directly so we don't need to reproduce
    the entire analyze_phase pipeline just to verify the wiring.
    """
    freqs = [30.0, 40.0, 50.0, 63.0, 80.0]
    target = {"points": [{"freq": 20, "spl": 75.0}, {"freq": 100, "spl": 75.0}], "band": [20, 100]}
    # 50 Hz is 10 dB below target — big error. Without geometry exclusion it
    # would dominate RMS; with geometry exclusion (we'll classify 50 Hz as
    # geometry) it drops out entirely.
    spls = [75.0, 75.0, 65.0, 75.0, 75.0]
    session = _make_deviation_session(1, freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore, \
         patch("calibrate.mcp_server._get_geometry_band_ranges", return_value=[(47.0, 53.0)]):
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_compute_deviation(
            session_id=1, target_curve=target, exclude_geometry=True,
        )
    assert result["ok"]
    assert result["excluded_geometry_points"] >= 1
    assert result["geometry_bands"] == [{"lo_hz": 47.0, "hi_hz": 53.0}]
    # 50 Hz's -10 dB error is excluded — RMS should be ~0.
    assert result["rms_db"] < 0.5


# ── anchor_target ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anchor_target_basic() -> None:
    """Anchor places reference so max boost = max_boost_db at the limiting freq."""
    from calibrate.mcp_server import _tool_anchor_target

    freqs = [25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 120.0]
    # Response tilts down at low freq — 25 Hz needs the most boost
    spls = [-20.0, -16.0, -12.0, -10.0, -9.0, -8.0, -8.0, -8.0]
    session = _make_deviation_session(1, freqs, spls)
    offsets = [
        {"freq_hz": 20, "offset_db": 10},
        {"freq_hz": 25, "offset_db": 9},
        {"freq_hz": 31.5, "offset_db": 7},
        {"freq_hz": 40, "offset_db": 5},
        {"freq_hz": 50, "offset_db": 3},
        {"freq_hz": 63, "offset_db": 1.5},
        {"freq_hz": 80, "offset_db": 0},
        {"freq_hz": 100, "offset_db": 0},
        {"freq_hz": 120, "offset_db": 0},
    ]
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.get_session.return_value = session
        result = await _tool_anchor_target(
            session_id=1, target_offsets=offsets, max_boost_db=6.0,
            port_rolloff_hz=20.0,  # include 25 Hz
        )
    assert result["ok"]
    # 25 Hz: measured=-20, offset=9 → headroom=-29. ref = -29+6 = -23
    assert abs(result["reference_spl"] - (-23.0)) < 0.1
    assert result["max_boost_db"] == 6.0
    assert result["limiting_freq_hz"] == 25.0
    # Anchored points should have absolute SPL values
    assert len(result["anchored_points"]) == len(offsets)
    # 80 Hz anchor = ref + 0 = -23
    p80 = next(p for p in result["anchored_points"] if p["freq"] == 80)
    assert abs(p80["spl"] - (-23.0)) < 0.1
    # 25 Hz anchor = ref + 9 = -14
    p25 = next(p for p in result["anchored_points"] if p["freq"] == 25)
    assert abs(p25["spl"] - (-14.0)) < 0.1


@pytest.mark.asyncio
async def test_anchor_target_excludes_nulls() -> None:
    """Deep nulls are excluded from anchor calculation."""
    from calibrate.mcp_server import _tool_anchor_target

    freqs = [30.0, 40.0, 50.0, 63.0, 80.0, 100.0]
    # 80 Hz is a deep null at -40 dB; everything else around -10 dB
    spls = [-10.0, -10.0, -10.0, -10.0, -40.0, -10.0]
    session = _make_deviation_session(1, freqs, spls)
    offsets = [
        {"freq_hz": 25, "offset_db": 0},
        {"freq_hz": 120, "offset_db": 0},
    ]
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.get_session.return_value = session
        result = await _tool_anchor_target(
            session_id=1, target_offsets=offsets, null_threshold_db=15.0
        )
    assert result["ok"]
    # The null at -40 dB (30 dB below avg ~-15) should be excluded
    assert result["excluded_null_points"] >= 1
    # Reference should NOT be driven by the null
    # Without null: min headroom = -10 - 0 = -10, ref = -10 + 6 = -4
    assert result["reference_spl"] > -10.0


@pytest.mark.asyncio
async def test_anchor_target_excludes_rolloff() -> None:
    """Frequencies below port_rolloff_hz are excluded."""
    from calibrate.mcp_server import _tool_anchor_target

    freqs = [22.0, 25.0, 30.0, 40.0, 50.0, 63.0, 80.0]
    spls = [-30.0, -25.0, -10.0, -10.0, -10.0, -10.0, -10.0]
    session = _make_deviation_session(1, freqs, spls)
    offsets = [
        {"freq_hz": 20, "offset_db": 10},
        {"freq_hz": 80, "offset_db": 0},
    ]
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.get_session.return_value = session
        result = await _tool_anchor_target(
            session_id=1, target_offsets=offsets, port_rolloff_hz=28.0
        )
    assert result["ok"]
    assert result["excluded_rolloff_points"] >= 2  # 22, 25 Hz


@pytest.mark.asyncio
async def test_anchor_target_session_not_found() -> None:
    """Missing session → error."""
    from calibrate.mcp_server import _tool_anchor_target

    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.get_session.return_value = None
        result = await _tool_anchor_target(
            session_id=99,
            target_offsets=[{"freq_hz": 80, "offset_db": 0}],
        )
    assert not result["ok"]
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_anchor_target_error_summary() -> None:
    """Error summary shows boost/cut needed at each target frequency."""
    from calibrate.mcp_server import _tool_anchor_target

    freqs = [25.0, 40.0, 63.0, 80.0, 100.0]
    spls = [-12.0, -8.0, -8.0, -8.0, -8.0]
    session = _make_deviation_session(1, freqs, spls)
    offsets = [
        {"freq_hz": 25, "offset_db": 5},
        {"freq_hz": 80, "offset_db": 0},
        {"freq_hz": 100, "offset_db": 0},
    ]
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.get_session.return_value = session
        result = await _tool_anchor_target(
            session_id=1, target_offsets=offsets, max_boost_db=6.0
        )
    assert result["ok"]
    assert len(result["error_summary"]) == 3
    for entry in result["error_summary"]:
        assert "freq_hz" in entry
        assert "error_db" in entry
        assert "action" in entry
        assert entry["action"] in ("boost", "cut", "ok")


@pytest.mark.asyncio
async def test_call_tool_anchor_target_dispatch() -> None:
    """call_tool('anchor_target') dispatches correctly."""
    from calibrate.mcp_server import call_tool

    freqs = [30.0, 50.0, 80.0, 100.0]
    spls = [-10.0, -10.0, -10.0, -10.0]
    session = _make_deviation_session(1, freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.get_session.return_value = session
        texts = await call_tool("anchor_target", {
            "session_id": 1,
            "target_offsets": [
                {"freq_hz": 25, "offset_db": 5},
                {"freq_hz": 80, "offset_db": 0},
            ],
        })
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "reference_spl" in data
    assert "anchored_points" in data


@pytest.mark.asyncio
async def test_anchor_target_marks_below_port_rolloff_unreachable() -> None:
    """Anchored points below port_rolloff_hz carry reachable=False so Phase 3
    filter design can skip them."""
    from calibrate.mcp_server import _tool_anchor_target

    freqs = [25.0, 31.5, 40.0, 50.0, 63.0, 80.0]
    spls = [-12.0, -14.0, -15.0, -18.0, -16.0, -20.0]
    session = _make_deviation_session(1, freqs, spls)
    offsets = [
        {"freq_hz": 20, "offset_db": 6},   # below port_rolloff
        {"freq_hz": 25, "offset_db": 5},
        {"freq_hz": 40, "offset_db": 3},
        {"freq_hz": 80, "offset_db": 0},
    ]
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.get_session.return_value = session
        result = await _tool_anchor_target(
            session_id=1, target_offsets=offsets, port_rolloff_hz=28.0,
        )
    assert result["ok"]
    pts = {p["freq"]: p for p in result["anchored_points"]}
    assert pts[20].get("reachable") is False
    assert "port-tune rolloff" in pts[20].get("reason", "")
    # In-band points should NOT be flagged.
    assert pts[40].get("reachable", True) is True  # key absent or True
    assert pts[80].get("reachable", True) is True


@pytest.mark.asyncio
async def test_anchor_target_exclude_geometry_wired() -> None:
    """anchor_target forwards exclude_geometry to _get_geometry_band_ranges."""
    from calibrate.mcp_server import _tool_anchor_target

    freqs = [25.0, 31.5, 40.0, 50.0, 63.0, 80.0]
    spls = [-12.0, -14.0, -15.0, -20.0, -16.0, -18.0]  # 50 Hz is a geometry null in our patch
    session = _make_deviation_session(1, freqs, spls)
    offsets = [
        {"freq_hz": 25, "offset_db": 5},
        {"freq_hz": 40, "offset_db": 3},
        {"freq_hz": 50, "offset_db": 2},
        {"freq_hz": 63, "offset_db": 1},
        {"freq_hz": 80, "offset_db": 0},
    ]
    with patch("calibrate.storage.SessionStore") as MockStore, \
         patch("calibrate.mcp_server._get_geometry_band_ranges", return_value=[(47.0, 53.0)]):
        MockStore.return_value.get_session.return_value = session
        result = await _tool_anchor_target(
            session_id=1, target_offsets=offsets, exclude_geometry=True,
        )
    assert result["ok"]
    assert result["excluded_geometry_points"] >= 1
    assert result["geometry_bands"] == [{"lo_hz": 47.0, "hi_hz": 53.0}]


# ── verify_fir_effect ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_fir_effect_within_tolerance() -> None:
    """Measured delta matching predicted within tolerance → within_tolerance=True."""
    from calibrate.mcp_server import _tool_verify_fir_effect

    # 1/3-octave centres near our test band.
    freqs = [25.0, 31.5, 40.0, 50.0, 63.0, 80.0]
    pre_spls = [80.0, 82.0, 85.0, 80.0, 78.0, 76.0]
    # FIR cut 3 dB everywhere in-band
    post_spls = [80.0, 79.0, 82.0, 77.0, 75.0, 73.0]  # roughly -3 dB
    pre_session = _make_deviation_session(1, freqs, pre_spls)
    post_session = _make_deviation_session(2, freqs, post_spls)
    predicted = [
        {"freq_hz": 25.0, "fir_effect_db": 0.0},
        {"freq_hz": 31.5, "fir_effect_db": -3.0},
        {"freq_hz": 40.0, "fir_effect_db": -3.0},
        {"freq_hz": 50.0, "fir_effect_db": -3.0},
        {"freq_hz": 63.0, "fir_effect_db": -3.0},
        {"freq_hz": 80.0, "fir_effect_db": -3.0},
    ]
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [pre_session, post_session]
        result = await _tool_verify_fir_effect(
            pre_session_id=1, post_session_id=2,
            predicted_effect=predicted, tolerance_db=2.0,
        )
    assert result["ok"], result
    assert result["within_tolerance"] is True
    assert result["off_spec_bands"] == []
    assert result["rms_discrepancy_db"] < 1.5


@pytest.mark.asyncio
async def test_verify_fir_effect_flags_off_spec_band() -> None:
    """When measured delta diverges > tolerance, band is flagged."""
    from calibrate.mcp_server import _tool_verify_fir_effect

    freqs = [31.5, 40.0, 50.0, 63.0, 80.0]
    pre_spls = [80.0, 82.0, 85.0, 80.0, 78.0]
    # FIR was supposed to cut 3 dB at 50 Hz but actually didn't move (0 dB).
    post_spls = [77.0, 79.0, 85.0, 77.0, 75.0]
    pre_session = _make_deviation_session(1, freqs, pre_spls)
    post_session = _make_deviation_session(2, freqs, post_spls)
    predicted = [
        {"freq_hz": 31.5, "fir_effect_db": -3.0},
        {"freq_hz": 40.0, "fir_effect_db": -3.0},
        {"freq_hz": 50.0, "fir_effect_db": -3.0},
        {"freq_hz": 63.0, "fir_effect_db": -3.0},
        {"freq_hz": 80.0, "fir_effect_db": -3.0},
    ]
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [pre_session, post_session]
        result = await _tool_verify_fir_effect(
            pre_session_id=1, post_session_id=2,
            predicted_effect=predicted, tolerance_db=2.0,
        )
    assert result["ok"], result
    assert result["within_tolerance"] is False
    # 50 Hz should be in off_spec
    off_spec_freqs = {b["freq_hz"] for b in result["off_spec_bands"]}
    assert 50.0 in off_spec_freqs
    assert "diverges" in result["note"] or "apply" in result["note"]


@pytest.mark.asyncio
async def test_verify_fir_effect_requires_predicted() -> None:
    from calibrate.mcp_server import _tool_verify_fir_effect

    pre_session = _make_deviation_session(1, [50.0], [80.0])
    post_session = _make_deviation_session(2, [50.0], [77.0])
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [pre_session, post_session]
        result = await _tool_verify_fir_effect(
            pre_session_id=1, post_session_id=2, predicted_effect=[],
        )
    assert not result["ok"]
    assert "predicted_effect" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_verify_fir_effect_dispatch() -> None:
    from calibrate.mcp_server import call_tool
    freqs = [31.5, 40.0, 50.0, 63.0, 80.0]
    pre_session = _make_deviation_session(1, freqs, [80.0, 82.0, 85.0, 80.0, 78.0])
    post_session = _make_deviation_session(2, freqs, [77.0, 79.0, 82.0, 77.0, 75.0])
    predicted = [{"freq_hz": f, "fir_effect_db": -3.0} for f in freqs]
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [pre_session, post_session]
        texts = await call_tool("verify_fir_effect", {
            "pre_session_id": 1, "post_session_id": 2,
            "predicted_effect": predicted, "tolerance_db": 2.0,
        })
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "within_tolerance" in data
    assert "rms_discrepancy_db" in data


# ── compare_sessions ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compare_sessions_basic() -> None:
    """Comparing two sessions returns per-band deltas (B minus A)."""
    import numpy as np
    freqs_a = np.logspace(np.log10(20), np.log10(200), 500).tolist()
    spls_a = [70.0] * len(freqs_a)
    session_a = _make_deviation_session(1, freqs_a, spls_a)

    freqs_b = np.logspace(np.log10(20), np.log10(200), 500).tolist()
    spls_b = [73.0] * len(freqs_b)  # 3 dB louder
    session_b = _make_deviation_session(2, freqs_b, spls_b)

    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session_a, session_b]
        result = await _tool_compare_sessions(session_a=1, session_b=2)
    assert result["ok"]
    assert result["session_a"]["id"] == 1
    assert result["session_b"]["id"] == 2
    assert len(result["bands"]) > 0
    for band in result["bands"]:
        assert abs(band["delta_db"] - 3.0) < 0.5  # B is ~3 dB louder
    assert abs(result["avg_delta_db"] - 3.0) < 0.5


@pytest.mark.asyncio
async def test_compare_sessions_session_not_found() -> None:
    """Session A not found → error."""
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(200), 100).tolist()
    spls = [75.0] * len(freqs)
    session = _make_deviation_session(2, freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_compare_sessions(session_a=99, session_b=2)
    assert not result["ok"]
    assert "session 99 not found" in result["error"]


@pytest.mark.asyncio
async def test_compare_sessions_session_b_not_found() -> None:
    """Session B not found → error."""
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(200), 100).tolist()
    spls = [75.0] * len(freqs)
    session = _make_deviation_session(1, freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_compare_sessions(session_a=1, session_b=99)
    assert not result["ok"]
    assert "session 99 not found" in result["error"]


@pytest.mark.asyncio
async def test_compare_sessions_returns_statistics() -> None:
    """Result includes avg, max, and rms delta stats."""
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    spls_a = [72.0] * len(freqs)
    spls_b = [75.0] * len(freqs)
    session_a = _make_deviation_session(1, freqs, spls_a)
    session_b = _make_deviation_session(2, freqs, spls_b)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session_a, session_b]
        result = await _tool_compare_sessions(session_a=1, session_b=2)
    assert result["ok"]
    assert "avg_delta_db" in result
    assert "max_delta_db" in result
    assert "rms_delta_db" in result
    assert result["rms_delta_db"] > 0


@pytest.mark.asyncio
async def test_call_tool_compare_sessions_dispatch() -> None:
    """call_tool('compare_sessions') dispatches correctly."""
    import numpy as np
    from calibrate.mcp_server import call_tool
    freqs = np.logspace(np.log10(20), np.log10(200), 200).tolist()
    session_a = _make_deviation_session(1, freqs, [70.0] * len(freqs))
    session_b = _make_deviation_session(2, freqs, [73.0] * len(freqs))
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session_a, session_b]
        texts = await call_tool("compare_sessions", {
            "session_a": 1,
            "session_b": 2,
        })
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "bands" in data
    assert "avg_delta_db" in data


# ── Label dedup fix ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_measure_label_dedup_no_double_position() -> None:
    """label='foo @ MLP' with position='MLP' → 'foo @ MLP' (not 'foo @ MLP @ MLP')."""
    mock_sd = MagicMock()
    mock_sd.query_devices.return_value = [{"name": "UMIK-1", "max_input_channels": 1}]

    mock_fr = MagicMock()
    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(return_value=mock_fr)

    mock_store = MagicMock()
    mock_store.save_measurement.return_value = 10

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
        result = await _tool_trigger_measurement(label="foo @ MLP", position="MLP")

    assert result["ok"]
    assert result["label"] == "foo @ MLP"
    # Verify the label saved to the store is NOT doubled
    call_kwargs = mock_store.save_measurement.call_args[1]
    assert call_kwargs["label"] == "foo @ MLP"


@pytest.mark.asyncio
async def test_measure_label_appends_position_when_missing() -> None:
    """label='foo' with position='MLP' → 'foo @ MLP'."""
    mock_sd = MagicMock()
    mock_sd.query_devices.return_value = [{"name": "UMIK-1", "max_input_channels": 1}]

    mock_fr = MagicMock()
    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(return_value=mock_fr)

    mock_store = MagicMock()
    mock_store.save_measurement.return_value = 11

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
        result = await _tool_trigger_measurement(label="foo", position="MLP")

    assert result["ok"]
    assert result["label"] == "foo @ MLP"
    call_kwargs = mock_store.save_measurement.call_args[1]
    assert call_kwargs["label"] == "foo @ MLP"


# ── group_delay stripping ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_measure_response_downsamples_group_delay() -> None:
    """The measure tool response includes group_delay downsampled to 1/3-octave."""
    mock_sd = MagicMock()
    mock_sd.query_devices.return_value = [{"name": "UMIK-1", "max_input_channels": 1}]

    mock_fr = MagicMock()
    mock_fr.coherence = None
    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(return_value=mock_fr)

    mock_store = MagicMock()
    mock_store.save_measurement.return_value = 12

    # Simulate compute_session_metadata returning group_delay among other keys
    full_metadata = {
        "ir": {"peak_time_ms": 5.0, "spl_db": 75.0},
        "group_delay": {"freq_hz": [20, 40, 80], "delay_ms": [1.0, 0.5, 0.2]},
    }

    with (
        patch.dict(sys.modules, {"sounddevice": mock_sd}),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.measurement.compute_session_metadata", return_value=full_metadata),
        patch("calibrate.storage.SessionStore", return_value=mock_store),
        patch.object(sut, "DenonSweepContext") as MockCtx,
        patch("calibrate.drivers.minidsp.MinidspSweepContext") as MockMinidspCtx,
    ):
        MockCtx.from_config.return_value = None
        MockMinidspCtx.from_config.return_value = None
        result = await _tool_trigger_measurement()

    assert result["ok"]
    # group_delay should be present but downsampled to 1/3-octave summary
    assert "group_delay" in result["metadata"]
    gd = result["metadata"]["group_delay"]
    assert isinstance(gd, list)  # Downsampled to list of {freq_hz, delay_ms}
    assert "ir" in result["metadata"]

    # Verify full metadata (including group_delay) was saved to the store
    call_kwargs = mock_store.save_measurement.call_args[1]
    assert "group_delay" in call_kwargs["metadata"]


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
async def test_get_measurement_history_compact_downsamples_group_delay() -> None:
    """Compact mode downsamples group_delay to 1/3-octave instead of stripping."""
    freqs = [20.0, 40.0]
    spls  = [1.0, 2.0]
    session = _make_fr_session(freqs, spls)
    session.metadata = {
        "ir": {"peak_time_ms": 5.0, "spl_db": 80.0},
        "group_delay": {"freq_hz": [20.0, 30.0, 40.0, 50.0, 80.0], "delay_ms": [5.0, 4.0, 3.0, 2.0, 1.0]},
        "position": "MLP",
    }
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_get_measurement_history(limit=1, fmt="compact")
    data = result["sessions"][0]
    assert "metadata" in data
    # group_delay should be present but downsampled to 1/3-octave
    assert "group_delay" in data["metadata"]
    gd = data["metadata"]["group_delay"]
    assert isinstance(gd, list)
    assert all("freq_hz" in b and "delay_ms" in b for b in gd)
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


# ── downsample_group_delay / downsample_coherence ───────────────────────────


def test_downsample_group_delay_basic() -> None:
    """Group delay downsampled to 1/3-octave bands."""
    freqs = [20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0]
    delays = [5.0, 4.0, 3.5, 3.0, 2.5, 2.0, 1.5, 1.0]
    result = _downsample_group_delay(freqs, delays)
    assert len(result) > 0
    for band in result:
        assert "freq_hz" in band
        assert "delay_ms" in band
    # Centre frequencies should be from the 1/3-octave list
    centres = {b["freq_hz"] for b in result}
    assert centres.issubset({20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0, 200.0})


def test_downsample_group_delay_empty() -> None:
    """Empty input returns empty output."""
    assert _downsample_group_delay([], []) == []


def test_downsample_coherence_basic() -> None:
    """Coherence downsampled to 1/3-octave bands."""
    freqs = [20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0]
    coh = [0.99, 0.98, 0.97, 0.95, 0.90, 0.85, 0.80, 0.75]
    result = _downsample_coherence(freqs, coh)
    assert len(result) > 0
    for band in result:
        assert "freq_hz" in band
        assert "coherence" in band
        assert 0.0 <= band["coherence"] <= 1.0


def test_downsample_coherence_empty() -> None:
    """Empty input returns empty output."""
    assert _downsample_coherence([], []) == []


# ── Helpers for new analytics tools ─────────────────────────────────────────


def _make_fr_session_with_phase(
    freqs: list[float],
    spls: list[float],
    phase: list[float] | None = None,
    session_id: int = 1,
) -> MagicMock:
    """Build a mock session with FR data including phase."""
    import math

    mock_fr = MagicMock()
    mock_fr.frequencies = freqs
    mock_fr.spl = spls
    mock_fr.phase = phase if phase is not None else [0.0] * len(freqs)
    session = MagicMock()
    session.id = session_id
    session.timestamp = "2026-04-12T00:00:00Z"
    session.label = f"session-{session_id}"
    session.start_fr = mock_fr
    session.metadata = None
    return session


# ── simulate_eq ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_simulate_eq_basic_prediction() -> None:
    """simulate_eq predicts FR after applying a peaking cut."""
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    # Create a 10 dB peak at 50 Hz
    spls = [75.0 + 10.0 * np.exp(-((f - 50) ** 2) / 100) for f in freqs]
    session = _make_fr_session(freqs, spls)

    filters = [
        {"type": "peaking", "freq": 50.0, "gain_db": -8.0, "q": 2.0},
    ]
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_simulate_eq(session_id=1, filters=filters)
    assert result["ok"]
    assert result["num_filters"] == 1
    assert result["point_count"] > 0
    # Parse compact FR and verify the peak is reduced
    pairs = result["predicted_fr"].split(",")
    for pair in pairs:
        freq_str, spl_str = pair.split(":")
        f, s = float(freq_str), float(spl_str)
        if abs(f - 50.0) < 1.0:
            # Original was ~85 dB at 50 Hz, after -8 dB cut should be lower
            assert s < 85.0


@pytest.mark.asyncio
async def test_simulate_eq_hpf_skipped() -> None:
    """simulate_eq skips HPF because the measurement already includes it.

    The miniDSP HPF is always active during measurement, so its effect is
    already baked into the measured FR.  Applying it again would double the
    attenuation, causing predicted bass levels to be far too low.
    """
    freqs = [15.0, 18.0, 20.0, 30.0, 50.0, 80.0, 100.0]
    spls = [75.0] * len(freqs)
    session = _make_fr_session(freqs, spls)

    filters = [{"type": "hpf", "freq": 18.0}]
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_simulate_eq(
            session_id=1, filters=filters, min_hz=15.0, max_hz=100.0,
        )
    assert result["ok"]
    pairs = result["predicted_fr"].split(",")
    # HPF is skipped — all frequencies should remain at ~75.0 dB
    for pair in pairs:
        f, s = pair.split(":")
        f, s = float(f), float(s)
        assert abs(s - 75.0) < 0.1, f"HPF should be skipped, but {f} Hz changed to {s}"


@pytest.mark.asyncio
async def test_simulate_eq_low_shelf_response() -> None:
    """simulate_eq computes correct low_shelf response (not the old peaking approximation)."""
    freqs = [20.0, 25.0, 35.0, 50.0, 80.0, 100.0]
    spls = [70.0] * len(freqs)
    session = _make_fr_session(freqs, spls)

    filters = [{"type": "low_shelf", "freq": 35.0, "gain_db": 5.0, "q": 0.5}]
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_simulate_eq(
            session_id=1, filters=filters, min_hz=20.0, max_hz=100.0,
        )
    assert result["ok"]
    pairs = result["predicted_fr"].split(",")
    predicted = {}
    for pair in pairs:
        f, s = pair.split(":")
        predicted[float(f)] = float(s)
    # Low shelf should boost low frequencies more than high
    assert predicted[25.0] > predicted[80.0], "low shelf must boost 25 Hz more than 80 Hz"
    # Tilt from 25→80 Hz should be ~2.5 dB (verified numerically)
    tilt = predicted[25.0] - predicted[80.0]
    assert tilt > 1.5, f"shelf tilt 25→80 Hz should be >1.5 dB, got {tilt:.1f} dB"


@pytest.mark.asyncio
async def test_simulate_eq_session_not_found() -> None:
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = []
        result = await _tool_simulate_eq(session_id=999, filters=[])
    assert not result["ok"]
    assert "999" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_simulate_eq_dispatch() -> None:
    from calibrate.mcp_server import call_tool

    freqs = [20.0, 40.0, 60.0, 80.0, 100.0]
    spls = [75.0, 78.0, 80.0, 76.0, 74.0]
    session = _make_fr_session(freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        texts = await call_tool("simulate_eq", {
            "session_id": 1,
            "filters": [{"type": "peaking", "freq": 60.0, "gain_db": -3.0, "q": 1.0}],
        })
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "predicted_fr" in data


# ── optimize_q ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_optimize_q_finds_reasonable_q() -> None:
    """optimize_q should find a Q that reduces RMS error in the band."""
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    # Create a narrow 10 dB peak at 50 Hz
    spls = [75.0 + 10.0 * np.exp(-((f - 50) ** 2) / 50) for f in freqs]
    session = _make_fr_session(freqs, spls)

    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_optimize_q(
            session_id=1, freq_hz=50.0, target_gain_db=-10.0,
        )
    assert result["ok"]
    assert 0.5 <= result["optimal_q"] <= 10.0
    assert result["freq_hz"] == 50.0
    assert result["gain_db"] == -10.0
    assert "predicted_rms_in_band" in result
    assert "effect_at_center_db" in result


@pytest.mark.asyncio
async def test_optimize_q_custom_band() -> None:
    """optimize_q respects a custom search band."""
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    spls = [75.0 + 8.0 * np.exp(-((f - 60) ** 2) / 80) for f in freqs]
    session = _make_fr_session(freqs, spls)

    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_optimize_q(
            session_id=1, freq_hz=60.0, target_gain_db=-6.0,
            band_hz=[40.0, 80.0],
        )
    assert result["ok"]
    assert result["band_hz"] == [40.0, 80.0]


@pytest.mark.asyncio
async def test_optimize_q_session_not_found() -> None:
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = []
        result = await _tool_optimize_q(
            session_id=999, freq_hz=50.0, target_gain_db=-5.0,
        )
    assert not result["ok"]
    assert "999" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_optimize_q_dispatch() -> None:
    from calibrate.mcp_server import call_tool
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 200).tolist()
    spls = [75.0 + 5.0 * np.exp(-((f - 50) ** 2) / 100) for f in freqs]
    session = _make_fr_session(freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        texts = await call_tool("optimize_q", {
            "session_id": 1, "freq_hz": 50.0, "target_gain_db": -5.0,
        })
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "optimal_q" in data


# ── analyze_phase ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_analyze_phase_with_phase_data() -> None:
    """analyze_phase returns classification + fixability when phase data is present."""
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    spls = [75.0 + 2.0 * np.sin(2 * np.pi * f / 40) for f in freqs]
    # Gentle phase: roughly minimum-phase-like
    phase = [-0.1 * f for f in freqs]
    session = _make_fr_session_with_phase(freqs, spls, phase)

    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_analyze_phase(session_id=1)
    assert result["ok"]
    assert result["has_phase_data"] is True
    assert len(result["bands"]) > 0
    for band in result["bands"]:
        assert "freq_hz" in band
        assert "spl_db" in band
        assert "min_phase_group_delay_ms" in band
        assert "fixable" in band
        assert "classification" in band
        assert band["classification"] in {"fixable", "partial", "geometry"}


@pytest.mark.asyncio
async def test_analyze_phase_without_phase_data() -> None:
    """analyze_phase reports fixable=None when no phase data is available."""
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    spls = [75.0] * len(freqs)
    session = _make_fr_session_with_phase(freqs, spls, phase=None)
    session.start_fr.phase = None  # Explicitly no phase

    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_analyze_phase(session_id=1)
    assert result["ok"]
    assert result["has_phase_data"] is False
    for band in result["bands"]:
        assert band["fixable"] is None
        assert band["classification"] is None


def test_classify_fixability_tiers() -> None:
    """_classify_fixability uses frequency-scaled thresholds with floors."""
    from calibrate.mcp_server import _classify_fixability

    # At 20 Hz, period = 50 ms → ¼λ = 12.5 ms, ½λ = 25 ms (both clamped at floors)
    # floors are max(10, period/4) and max(25, period/2) → 12.5 and 25 at 20 Hz
    cls, fx = _classify_fixability(20.0, 5.0)
    assert cls == "fixable" and fx is True
    cls, fx = _classify_fixability(20.0, 15.0)
    assert cls == "partial" and fx is True
    cls, fx = _classify_fixability(20.0, 30.0)
    assert cls == "geometry" and fx is False

    # At 80 Hz, period = 12.5 ms → thresholds hit the floors (10 ms, 25 ms)
    cls, fx = _classify_fixability(80.0, 3.0)
    assert cls == "fixable" and fx is True
    cls, fx = _classify_fixability(80.0, 15.0)
    assert cls == "partial" and fx is True
    cls, fx = _classify_fixability(80.0, 40.0)
    assert cls == "geometry" and fx is False

    # Boundary: at 20 Hz, 5 ms excess GD would have been flagged geometry under the
    # old fixed 5 ms threshold, but the new scaled threshold classifies it as fixable.
    cls, fx = _classify_fixability(20.0, 5.0)
    assert cls == "fixable" and fx is True


@pytest.mark.asyncio
async def test_analyze_phase_session_not_found() -> None:
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = []
        result = await _tool_analyze_phase(session_id=999)
    assert not result["ok"]
    assert "999" in result["error"]


@pytest.mark.asyncio
async def test_analyze_phase_insufficient_data() -> None:
    """Too few data points in range → error."""
    session = _make_fr_session_with_phase([30.0], [75.0], [0.0])
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_analyze_phase(session_id=1)
    assert not result["ok"]
    assert "insufficient" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_analyze_phase_dispatch() -> None:
    from calibrate.mcp_server import call_tool
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    spls = [75.0] * len(freqs)
    phase = [-0.05 * f for f in freqs]
    session = _make_fr_session_with_phase(freqs, spls, phase)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        texts = await call_tool("analyze_phase", {"session_id": 1})
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "bands" in data


# ── compare_sub_phase ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compare_sub_phase_reinforcing() -> None:
    """Two subs with similar phase → reinforcing classification."""
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 200).tolist()
    spls_a = [75.0] * len(freqs)
    spls_b = [75.0] * len(freqs)
    # Nearly identical phase → reinforcing
    phase_a = [0.1 * f for f in freqs]
    phase_b = [0.1 * f + 0.05 for f in freqs]  # ~3 deg difference
    session_a = _make_fr_session_with_phase(freqs, spls_a, phase_a, session_id=1)
    session_b = _make_fr_session_with_phase(freqs, spls_b, phase_b, session_id=2)

    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session_a, session_b]
        result = await _tool_compare_sub_phase(session_a=1, session_b=2)
    assert result["ok"]
    assert result["reinforcing_bands"] > 0
    for band in result["bands"]:
        assert "freq_hz" in band
        assert "phase_diff_deg" in band
        assert "predicted_sum_db" in band
        assert "classification" in band


@pytest.mark.asyncio
async def test_compare_sub_phase_cancelling() -> None:
    """Two subs with ~180° phase difference → cancelling."""
    import numpy as np
    import math

    freqs = np.logspace(np.log10(20), np.log10(120), 200).tolist()
    spls_a = [75.0] * len(freqs)
    spls_b = [75.0] * len(freqs)
    phase_a = [0.0] * len(freqs)
    phase_b = [math.pi] * len(freqs)  # 180 degrees out of phase
    session_a = _make_fr_session_with_phase(freqs, spls_a, phase_a, session_id=1)
    session_b = _make_fr_session_with_phase(freqs, spls_b, phase_b, session_id=2)

    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session_a, session_b]
        result = await _tool_compare_sub_phase(session_a=1, session_b=2)
    assert result["ok"]
    assert result["cancelling_bands"] > 0


@pytest.mark.asyncio
async def test_compare_sub_phase_missing_phase() -> None:
    """Session without phase data → error."""
    freqs = [20.0, 50.0, 80.0]
    spls = [75.0, 75.0, 75.0]
    session_a = _make_fr_session(freqs, spls, session_id=1)  # No phase
    session_b = _make_fr_session_with_phase(freqs, spls, session_id=2)

    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session_a, session_b]
        result = await _tool_compare_sub_phase(session_a=1, session_b=2)
    assert not result["ok"]
    assert "phase data" in result["error"]


@pytest.mark.asyncio
async def test_compare_sub_phase_session_not_found() -> None:
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = []
        result = await _tool_compare_sub_phase(session_a=1, session_b=2)
    assert not result["ok"]
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_compare_sub_phase_dispatch() -> None:
    from calibrate.mcp_server import call_tool
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 200).tolist()
    spls = [75.0] * len(freqs)
    phase = [0.0] * len(freqs)
    sa = _make_fr_session_with_phase(freqs, spls, phase, session_id=1)
    sb = _make_fr_session_with_phase(freqs, spls, phase, session_id=2)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [sa, sb]
        texts = await call_tool("compare_sub_phase", {
            "session_a": 1, "session_b": 2,
        })
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "bands" in data


# ── design_fir ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_design_fir_minimum_phase() -> None:
    """design_fir returns coefficients in minimum-phase mode."""
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    spls = [75.0 + 5.0 * np.sin(2 * np.pi * f / 40) for f in freqs]
    session = _make_fr_session(freqs, spls)

    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_design_fir(
            session_id=1, num_taps=256, phase_mode="minimum",
        )
    assert result["ok"]
    assert result["num_taps"] == 256
    assert result["phase_mode"] == "minimum"
    assert result["pre_ringing_ms"] == 0.0
    assert len(result["coefficients"]) == 256
    assert len(result["predicted_effect"]) > 0
    assert result["freq_resolution_hz"] > 0


@pytest.mark.asyncio
async def test_design_fir_linear_phase() -> None:
    """design_fir in linear-phase mode has non-zero pre-ringing."""
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    spls = [75.0] * len(freqs)
    session = _make_fr_session(freqs, spls)

    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_design_fir(
            session_id=1, num_taps=512, phase_mode="linear",
        )
    assert result["ok"]
    assert result["phase_mode"] == "linear"
    assert result["pre_ringing_ms"] > 0  # Linear phase has pre-ringing


# ── mixed-phase: proper homomorphic decomposition ──────────────────────────

@pytest.mark.asyncio
async def test_design_fir_mixed_phase_reports_bounded_latency() -> None:
    """Mixed-phase FIR's latency is bounded above by the preringing window.

    The impulse peak lands at most `preringing_ms` samples into the buffer —
    it can land earlier due to windowing edge effects and truncation, but
    must NEVER exceed the user's pre-ringing budget (that would defeat the
    whole point of the bound).
    """
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    spls = [75.0 + 5.0 * np.sin(2 * np.pi * f / 40) for f in freqs]
    session = _make_fr_session(freqs, spls)

    # Use a small preringing window so the test is rate-agnostic: at the
    # fallback fir_fs=96 kHz, 2 ms = 192 samples (well within num_taps=1024).
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_design_fir(
            session_id=1, num_taps=1024, phase_mode="mixed", preringing_ms=2.0,
        )
    assert result["ok"], result
    assert result["phase_mode"] == "mixed"
    # pre_ringing_ms matches the requested bound (reported value).
    assert result["pre_ringing_ms"] == pytest.approx(2.0, abs=0.5)
    # latency_ms is bounded above by the pre-ringing window (plus small jitter
    # from min-phase core + windowing edge effects).
    assert result["latency_ms"] <= 4.0, result
    # And it's non-negative (peak doesn't land before sample 0).
    assert result["latency_ms"] >= 0.0


@pytest.mark.asyncio
async def test_design_fir_mixed_phase_with_zero_preringing_matches_minimum() -> None:
    """mixed-phase with preringing_ms=0 should degenerate to minimum-phase.

    This is the no-latency escape hatch — choosing mixed but asking for zero
    pre-ringing should produce an impulse whose peak is at sample 0, same as
    phase_mode='minimum'. Critical for the recipe's 'recommend_fir_phase → min'
    branch where we want mixed-phase code path but no pre-ringing.
    """
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    spls = [75.0 + 3.0 * np.sin(2 * np.pi * f / 40) for f in freqs]
    session = _make_fr_session(freqs, spls)

    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_design_fir(
            session_id=1, num_taps=256, phase_mode="mixed", preringing_ms=0.0,
        )
    assert result["ok"], result
    # Latency should be near-zero.
    assert result["latency_ms"] <= 1.0, result


@pytest.mark.asyncio
async def test_design_fir_mixed_phase_magnitude_tracks_min_phase_in_focus_band() -> None:
    """Within the focus band, mixed-phase magnitude should track min-phase.

    Outside the focus band the all-pass is ramped to pass-through, so
    magnitude matches min-phase's natural roll-off there too. The only
    frequencies allowed to drift are near the pass-through transition (ramp
    region). A proper homomorphic mixed-phase shouldn't diverge wildly from
    min-phase anywhere it matters.
    """
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    spls = [75.0 + 5.0 * np.sin(2 * np.pi * f / 40) for f in freqs]
    session = _make_fr_session(freqs, spls)

    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        min_result = await _tool_design_fir(
            session_id=1, num_taps=2048, phase_mode="minimum",
            freq_focus_hz=[25, 90],
        )
        mix_result = await _tool_design_fir(
            session_id=1, num_taps=2048, phase_mode="mixed", preringing_ms=20.0,
            freq_focus_hz=[25, 90],
        )
    assert min_result["ok"] and mix_result["ok"]
    min_effect = {b["freq_hz"]: b["fir_effect_db"] for b in min_result["predicted_effect"]}
    mix_effect = {b["freq_hz"]: b["fir_effect_db"] for b in mix_result["predicted_effect"]}
    # Compare inside the correction band (25 <= f <= 90).
    common = sorted(set(min_effect) & set(mix_effect))
    in_band = [f for f in common if 25 <= f <= 90]
    assert in_band, "no common bands to compare in 25-90 Hz"
    deltas = [mix_effect[f] - min_effect[f] for f in in_band]
    rms = (sum(d * d for d in deltas) / len(deltas)) ** 0.5
    # 6 dB RMS is a loose but fair bound: short pre-ringing windows trade
    # some magnitude accuracy for phase correction. The all-pass band-limit
    # keeps this modest.
    assert rms < 6.0, (
        f"mixed-phase magnitude diverges by {rms:.2f} dB RMS in-band — "
        f"deltas: {list(zip(in_band, deltas))}"
    )


@pytest.mark.asyncio
async def test_design_fir_with_target_curve() -> None:
    """design_fir accepts a custom target curve."""
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    spls = [75.0] * len(freqs)
    session = _make_fr_session(freqs, spls)

    target = {
        "points": [
            {"freq": 25, "spl": 80.0},
            {"freq": 80, "spl": 75.0},
        ]
    }
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_design_fir(
            session_id=1, target_curve=target, num_taps=256,
        )
    assert result["ok"]
    assert len(result["coefficients"]) == 256


@pytest.mark.asyncio
async def test_design_fir_invalid_tap_count() -> None:
    """Tap count outside the driver's [min, max] range → error."""
    freqs = [20.0, 50.0, 100.0]
    spls = [75.0, 75.0, 75.0]
    session = _make_fr_session(freqs, spls)

    # No _dsp attached → falls back to the built-in miniDSP defaults (64-2048).
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_design_fir(session_id=1, num_taps=32)
    assert not result["ok"]
    assert "64" in result["error"] and "2048" in result["error"]


@pytest.mark.asyncio
async def test_design_fir_uses_driver_capabilities() -> None:
    """When a driver is attached, tap limits and sample rate come from it."""
    import numpy as np
    from calibrate.drivers.dsp_driver import DSPCapabilities
    import calibrate.mcp_server as srv

    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    spls = [75.0] * len(freqs)
    session = _make_fr_session(freqs, spls)

    class _StubDSP:
        capabilities = DSPCapabilities(
            max_delay_ms=1000.0,
            max_preset_index=-1,
            valid_sources=frozenset(),
            processing_rate=48_000,
            max_peq_slots=32,
            fir_capable=True,
            fir_min_taps=64,
            fir_max_taps_per_output=65536,
            fir_shared_tap_pool=None,
            fir_sample_rate_hz=48_000,
        )

    prev = srv._dsp
    srv._dsp = _StubDSP()  # type: ignore[assignment]
    try:
        with patch("calibrate.storage.SessionStore") as MockStore:
            MockStore.return_value.list_sessions.return_value = [session]
            # 8192 taps would fail the old hardcoded 2048 limit — should pass now.
            result = await _tool_design_fir(session_id=1, num_taps=8192)
        assert result["ok"], result
        # Frequency resolution reflects the driver's FIR sample rate (48 kHz), not 96 kHz.
        assert result["freq_resolution_hz"] == round(48_000 / 8192, 1)
    finally:
        srv._dsp = prev


@pytest.mark.asyncio
async def test_design_fir_session_not_found() -> None:
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = []
        result = await _tool_design_fir(session_id=999)
    assert not result["ok"]
    assert "999" in result["error"]


@pytest.mark.asyncio
async def test_design_fir_coefficients_normalized() -> None:
    """FIR coefficients should be normalized so peak <= 1.0."""
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    # Large deviation to ensure non-trivial FIR
    spls = [75.0 + 10.0 * np.sin(2 * np.pi * f / 30) for f in freqs]
    session = _make_fr_session(freqs, spls)

    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_design_fir(
            session_id=1, num_taps=256, phase_mode="minimum",
        )
    assert result["ok"]
    peak = max(abs(c) for c in result["coefficients"])
    assert peak <= 1.0 + 1e-8  # Normalized


@pytest.mark.asyncio
async def test_call_tool_design_fir_dispatch() -> None:
    from calibrate.mcp_server import call_tool
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 200).tolist()
    spls = [75.0] * len(freqs)
    session = _make_fr_session(freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        texts = await call_tool("design_fir", {
            "session_id": 1, "num_taps": 128,
        })
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "coefficients" in data


@pytest.mark.asyncio
async def test_call_tool_design_fir_dispatch_return_coefficients_false() -> None:
    """The dispatcher must forward return_coefficients=False — otherwise the
    caller's opt-out is silently ignored and the large array comes back anyway.
    Regression test for the Apr-23 verification fix.
    """
    from calibrate.mcp_server import call_tool
    from calibrate import mcp_server as _mod
    import numpy as np

    freqs = np.logspace(np.log10(20), np.log10(120), 200).tolist()
    spls = [75.0 + 3.0 * np.sin(2 * np.pi * f / 40) for f in freqs]
    session = _make_fr_session(freqs, spls)
    _mod._fir_design_cache.pop(1, None)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        texts = await call_tool("design_fir", {
            "session_id": 1,
            "num_taps": 128,
            "return_coefficients": False,
        })
    try:
        data = json.loads(texts[0].text)
        assert data["ok"], data
        assert data["design_cached"] is True
        assert "coefficients" not in data, (
            "dispatcher dropped return_coefficients=False — "
            "array would blow the token budget on 8k+ taps"
        )
    finally:
        _mod._fir_design_cache.pop(1, None)


@pytest.mark.asyncio
async def test_design_fir_return_coefficients_false_caches_only() -> None:
    """design_fir with return_coefficients=False omits the array and caches it."""
    import numpy as np
    from calibrate import mcp_server as _mod

    freqs = np.logspace(np.log10(20), np.log10(120), 200).tolist()
    spls = [75.0 + 3.0 * np.sin(2 * np.pi * f / 40) for f in freqs]
    session = _make_fr_session(freqs, spls)

    _mod._fir_design_cache.pop(1, None)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        design = await _tool_design_fir(
            session_id=1, num_taps=128, return_coefficients=False,
        )
    try:
        assert design["ok"], design
        assert design["design_cached"] is True
        assert "coefficients" not in design
        assert design["peak_abs"] <= 1.0 + 1e-8
        cached = _mod._fir_design_cache.get(1)
        assert cached is not None and len(cached) == 128
    finally:
        _mod._fir_design_cache.pop(1, None)


def test_camilladsp_rehydrate_restores_fir_from_active_state() -> None:
    """rehydrate_from_active_state restores _fir_state for the 'fir' field."""
    import asyncio
    from calibrate.drivers.camilladsp import CamillaDSPDriver
    from calibrate.storage import dsp_output_key

    driver = CamillaDSPDriver(
        host="127.0.0.1", port=1234, processing_rate=48_000,
        input_channels=2, output_channels=4,
        capture_device={"type": "Alsa", "device": "hw:Loopback,1,0", "channels": 2, "format": "S32_LE"},
        playback_device={"type": "Alsa", "device": "hw:USB,0,0", "channels": 4, "format": "S32_LE"},
    )
    coeffs = [0.1, -0.2, 0.3, -0.4]
    active_state = {
        dsp_output_key("camilla", 1, "fir"): {"coefficients": coeffs, "num_taps": 4},
    }
    # Rehydrate without pushing to a live daemon — patch the push call to a no-op.
    async def _no_push():
        pass
    driver._push_config_locked = _no_push  # type: ignore[assignment]

    asyncio.run(driver.rehydrate_from_active_state(active_state))
    assert driver._fir_state.get(1) == [float(c) for c in coeffs]


# ── LLM filter-design math tools ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluate_transfer_function_basic() -> None:
    """Peaking filter has max effect at centre frequency, less at edges."""
    filters = [{"type": "peaking", "freq": 80.0, "gain_db": -6.0, "q": 2.0}]
    result = await _tool_evaluate_transfer_function(
        filters=filters, query_freqs=[40.0, 80.0, 120.0],
    )
    assert result["ok"]
    assert result["num_filters"] == 1
    assert result["num_freqs"] == 3
    # Centre frequency gets the strongest cut
    centre = result["results"][1]
    assert centre["freq_hz"] == 80.0
    assert centre["total_db"] < -5.0  # close to -6
    # Edges get less
    assert abs(result["results"][0]["total_db"]) < abs(centre["total_db"])
    assert abs(result["results"][2]["total_db"]) < abs(centre["total_db"])


@pytest.mark.asyncio
async def test_evaluate_transfer_function_hpf_skipped() -> None:
    """HPF contributes 0 dB (measurement already includes it)."""
    filters = [
        {"type": "hpf", "freq": 18.0, "gain_db": 0, "q": 0.707},
        {"type": "peaking", "freq": 50.0, "gain_db": -3.0, "q": 2.0},
    ]
    result = await _tool_evaluate_transfer_function(
        filters=filters, query_freqs=[50.0],
    )
    assert result["ok"]
    pf = result["results"][0]["per_filter"]
    assert pf[0]["contribution_db"] == 0.0  # HPF
    assert pf[1]["contribution_db"] < -2.0  # peaking


@pytest.mark.asyncio
async def test_evaluate_transfer_function_multiple_filters_stack() -> None:
    """Two overlapping cuts should sum."""
    filters = [
        {"type": "peaking", "freq": 60.0, "gain_db": -4.0, "q": 1.0},
        {"type": "peaking", "freq": 70.0, "gain_db": -4.0, "q": 1.0},
    ]
    result = await _tool_evaluate_transfer_function(
        filters=filters, query_freqs=[65.0],
    )
    assert result["ok"]
    total = result["results"][0]["total_db"]
    # Both filters contribute at 65 Hz — total should be more negative than either alone
    assert total < -4.0


@pytest.mark.asyncio
async def test_per_filter_contribution_basic() -> None:
    """Per-filter contribution shows baseline, correction, and predicted."""
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    spls = [75.0] * len(freqs)
    session = _make_fr_session(freqs, spls)

    filters = [{"type": "peaking", "freq": 50.0, "gain_db": -5.0, "q": 2.0}]
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_per_filter_contribution(
            filters=filters, session_id=1, query_freqs=[50.0, 80.0],
        )
    assert result["ok"]
    assert result["num_freqs"] == 2
    # At 50 Hz, the peaking filter should have strong contribution
    r50 = result["results"][0]
    assert r50["baseline_db"] == 75.0
    assert r50["per_filter"][0]["contribution_db"] < -4.0
    assert r50["predicted_db"] < 71.0
    # At 80 Hz, less effect
    r80 = result["results"][1]
    assert abs(r80["per_filter"][0]["contribution_db"]) < abs(r50["per_filter"][0]["contribution_db"])


@pytest.mark.asyncio
async def test_per_filter_contribution_default_freqs() -> None:
    """Omitting query_freqs uses sixth-octave centres."""
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    spls = [75.0] * len(freqs)
    session = _make_fr_session(freqs, spls)

    filters = [{"type": "peaking", "freq": 50.0, "gain_db": -3.0, "q": 2.0}]
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_per_filter_contribution(
            filters=filters, session_id=1,
        )
    assert result["ok"]
    assert result["num_freqs"] > 5  # sixth-octave gives ~12 bands


@pytest.mark.asyncio
async def test_per_filter_contribution_session_not_found() -> None:
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = []
        result = await _tool_per_filter_contribution(
            filters=[], session_id=999,
        )
    assert not result["ok"]


@pytest.mark.asyncio
async def test_interpolate_optimal_gain_two_points() -> None:
    """Two-point linear interpolation finds exact zero crossing."""
    result = await _tool_interpolate_optimal_gain(
        freq=80.0, q=4.0, filter_type="peaking",
        measured_errors=[
            {"gain_applied": -6.0, "error_measured": -2.8},
            {"gain_applied": -3.0, "error_measured": 4.5},
        ],
    )
    assert result["ok"]
    # With -6 → -2.8 and -3 → +4.5, the zero crossing is between -6 and -3
    optimal = result["optimal_gain_db"]
    assert -6.0 < optimal < -3.0
    assert abs(result["predicted_error_db"]) < 0.01


@pytest.mark.asyncio
async def test_interpolate_optimal_gain_three_points() -> None:
    """Three points with least-squares fit."""
    result = await _tool_interpolate_optimal_gain(
        freq=50.0, q=2.0, filter_type="peaking",
        measured_errors=[
            {"gain_applied": -5.0, "error_measured": -4.6},
            {"gain_applied": -2.5, "error_measured": 1.2},
            {"gain_applied": 0.0, "error_measured": 4.4},
        ],
    )
    assert result["ok"]
    assert -5.0 < result["optimal_gain_db"] < 0.0
    assert result["n_points"] == 3


@pytest.mark.asyncio
async def test_interpolate_optimal_gain_insufficient_data() -> None:
    """Fewer than 2 points returns error."""
    result = await _tool_interpolate_optimal_gain(
        freq=80.0, q=4.0, filter_type="peaking",
        measured_errors=[{"gain_applied": -5.0, "error_measured": -2.0}],
    )
    assert not result["ok"]


@pytest.mark.asyncio
async def test_sensitivity_analysis_basic() -> None:
    """Sensitivity analysis returns gradients for non-HPF filters."""
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(120), 200).tolist()
    # Create a response that's 5 dB above flat target
    spls = [80.0] * len(freqs)
    session = _make_fr_session(freqs, spls)

    filters = [
        {"type": "hpf", "freq": 18, "gain_db": 0, "q": 0.707},
        {"type": "peaking", "freq": 65.0, "gain_db": -5.0, "q": 1.0},
    ]
    target = {
        "points": [{"freq": 20, "spl": 75.0}, {"freq": 120, "spl": 75.0}],
        "band": [20, 120],
    }
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_sensitivity_analysis(
            filters=filters, session_id=1, target_curve=target,
        )
    assert result["ok"]
    assert result["baseline_rms"] > 0
    assert len(result["sensitivities"]) == 2
    # HPF should be skipped
    assert result["sensitivities"][0]["skipped"] is True
    # Peaking should have gradient values
    peaking = result["sensitivities"][1]
    assert "d_rms_d_gain" in peaking
    assert "d_rms_d_freq" in peaking
    assert "d_rms_d_q" in peaking


@pytest.mark.asyncio
async def test_sensitivity_analysis_session_not_found() -> None:
    target = {"points": [{"freq": 20, "spl": 75}, {"freq": 120, "spl": 75}], "band": [20, 120]}
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = []
        result = await _tool_sensitivity_analysis(
            filters=[], session_id=999, target_curve=target,
        )
    assert not result["ok"]


@pytest.mark.asyncio
async def test_fit_correction_filter_finds_cut() -> None:
    """Fit finds a cut filter for a response that's above target."""
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(120), 200).tolist()
    # 5 dB peak at 60 Hz
    spls = [75.0 + 5.0 * np.exp(-((f - 60) ** 2) / 200) for f in freqs]
    session = _make_fr_session(freqs, spls)

    target = {
        "points": [{"freq": 20, "spl": 75.0}, {"freq": 120, "spl": 75.0}],
        "band": [20, 120],
    }
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_fit_correction_filter(
            session_id=1, target_curve=target,
            freq_range=[40.0, 80.0],
        )
    assert result["ok"]
    assert result["rms_after"] < result["rms_before"]
    bf = result["best_filter"]
    assert bf["type"] == "peaking"
    # Filter should be near 60 Hz and a cut
    assert 50.0 < bf["freq"] < 75.0
    assert bf["gain_db"] < 0


@pytest.mark.asyncio
async def test_fit_correction_filter_session_not_found() -> None:
    target = {"points": [{"freq": 20, "spl": 75}, {"freq": 120, "spl": 75}], "band": [20, 120]}
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = []
        result = await _tool_fit_correction_filter(
            session_id=999, target_curve=target, freq_range=[20, 120],
        )
    assert not result["ok"]


@pytest.mark.asyncio
async def test_fit_correction_filter_respects_constraints() -> None:
    """max_boost_db constraint prevents the optimizer from boosting above limit."""
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(120), 200).tolist()
    # 5 dB dip at 60 Hz — would need a boost to fix
    spls = [75.0 - 5.0 * np.exp(-((f - 60) ** 2) / 200) for f in freqs]
    session = _make_fr_session(freqs, spls)

    target = {
        "points": [{"freq": 20, "spl": 75.0}, {"freq": 120, "spl": 75.0}],
        "band": [20, 120],
    }
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_fit_correction_filter(
            session_id=1, target_curve=target,
            freq_range=[40.0, 80.0],
            constraints={"max_boost_db": 3.0},
        )
    assert result["ok"]
    if "best_filter" in result:
        assert result["best_filter"]["gain_db"] <= 3.0


# ── multi-filter joint optimization (num_filters > 1) ──────────────────────

@pytest.mark.asyncio
async def test_fit_correction_filter_joint_beats_single_on_3_peaks() -> None:
    """A response with 3 distinct peaks at 35 / 55 / 80 Hz should be better
    corrected by a 3-filter joint fit than by a single filter.

    Regression test for the manual iteration loop that burned 4 iterations
    in run 16 hand-tuning filter freq/gain/Q one at a time.
    """
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(120), 400).tolist()
    # Three peaks: +4 dB at 35, +5 dB at 55, +4 dB at 80
    def peak(fc, height, width):
        return lambda f: height * np.exp(-((f - fc) ** 2) / (2 * width ** 2))
    bumps = [peak(35, 4.0, 3.0), peak(55, 5.0, 4.0), peak(80, 4.0, 4.0)]
    spls = [75.0 + sum(b(f) for b in bumps) for f in freqs]
    session = _make_fr_session(freqs, spls)

    target = {
        "points": [{"freq": 20, "spl": 75.0}, {"freq": 120, "spl": 75.0}],
        "band": [20, 120],
    }
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        single = await _tool_fit_correction_filter(
            session_id=1, target_curve=target,
            freq_range=[25.0, 100.0], num_filters=1,
        )
        joint = await _tool_fit_correction_filter(
            session_id=1, target_curve=target,
            freq_range=[25.0, 100.0], num_filters=3,
        )
    assert single["ok"] and joint["ok"], (single, joint)
    # Joint fit's RMS should be at least modestly better than single.
    assert joint["rms_after"] < single["rms_after"]
    # Three filters requested, three (or fewer after zero-gain pruning) returned.
    assert len(joint["filters"]) <= 3
    assert joint["num_filters_requested"] == 3
    # Each returned filter should be inside the freq_range we asked for.
    for f in joint["filters"]:
        assert 25.0 <= f["freq"] <= 100.0
        assert f["type"] == "peaking"


@pytest.mark.asyncio
async def test_fit_correction_filter_joint_respects_bounds() -> None:
    """Multi-filter mode respects max_boost_db, min_q, max_q bounds."""
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(120), 200).tolist()
    spls = [75.0 + 8.0 * np.exp(-((f - 50) ** 2) / 20) for f in freqs]  # 8 dB peak
    session = _make_fr_session(freqs, spls)

    target = {
        "points": [{"freq": 20, "spl": 75.0}, {"freq": 120, "spl": 75.0}],
        "band": [20, 120],
    }
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_fit_correction_filter(
            session_id=1, target_curve=target,
            freq_range=[25.0, 100.0], num_filters=2,
            constraints={"max_boost_db": 3.0, "min_q": 1.0, "max_q": 5.0},
        )
    assert result["ok"]
    for f in result["filters"]:
        assert f["gain_db"] <= 3.0, "exceeds max_boost_db"
        assert 1.0 <= f["q"] <= 5.0, f"Q {f['q']} out of [1,5] bounds"


@pytest.mark.asyncio
async def test_fit_correction_filter_joint_rejects_too_many_filters() -> None:
    """num_filters > 8 is rejected (SafetyValidator slot budget)."""
    target = {"points": [{"freq": 20, "spl": 75}, {"freq": 120, "spl": 75}], "band": [20, 120]}
    session = _make_fr_session([20.0, 120.0], [75.0, 75.0])
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_fit_correction_filter(
            session_id=1, target_curve=target,
            freq_range=[25.0, 100.0], num_filters=9,
        )
    assert not result["ok"]
    assert "slot budget" in result["error"].lower() or "8" in result["error"]


@pytest.mark.asyncio
async def test_call_tool_fit_correction_filter_joint_dispatch() -> None:
    """The dispatcher forwards num_filters to the multi-filter path."""
    from calibrate.mcp_server import call_tool
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(120), 200).tolist()
    spls = [75.0 + 4.0 * np.exp(-((f - 50) ** 2) / 20) for f in freqs]
    session = _make_fr_session(freqs, spls)

    target = {"points": [{"freq": 20, "spl": 75.0}, {"freq": 120, "spl": 75.0}], "band": [20, 120]}
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        texts = await call_tool("fit_correction_filter", {
            "session_id": 1, "target_curve": target,
            "freq_range": [25.0, 100.0], "num_filters": 2,
        })
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "filters" in data
    assert data["num_filters_requested"] == 2


@pytest.mark.asyncio
async def test_predict_rms_basic() -> None:
    """predict_rms returns predicted deviation for proposed filters."""
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    # Flat at 80 dB
    spls = [80.0] * len(freqs)
    session = _make_fr_session(freqs, spls)

    # Target: flat at 75 dB → need 5 dB of cut
    target = {
        "points": [{"freq": 20, "spl": 75.0}, {"freq": 120, "spl": 75.0}],
        "band": [20, 120],
    }
    # No filters → RMS should be ~5 dB
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_predict_rms(
            filters=[], session_id=1, target_curve=target,
        )
    assert result["ok"]
    assert 4.5 < result["predicted_rms"] < 5.5


@pytest.mark.asyncio
async def test_predict_rms_with_correction() -> None:
    """Applying a broadband cut should reduce predicted RMS."""
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    spls = [80.0] * len(freqs)
    session = _make_fr_session(freqs, spls)

    target = {
        "points": [{"freq": 20, "spl": 75.0}, {"freq": 120, "spl": 75.0}],
        "band": [20, 120],
    }
    # Broadband cut at 65 Hz — should reduce the 5 dB overshoot
    filters = [{"type": "peaking", "freq": 65.0, "gain_db": -5.0, "q": 0.5}]
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_predict_rms(
            filters=filters, session_id=1, target_curve=target,
        )
    assert result["ok"]
    assert result["predicted_rms"] < 5.0  # improved from ~5 dB


@pytest.mark.asyncio
async def test_predict_rms_convergence() -> None:
    """Perfect correction converges."""
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    spls = [75.5] * len(freqs)  # 0.5 dB above target
    session = _make_fr_session(freqs, spls)

    target = {
        "points": [{"freq": 20, "spl": 75.0}, {"freq": 120, "spl": 75.0}],
        "band": [20, 120],
    }
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_predict_rms(
            filters=[], session_id=1, target_curve=target,
            convergence_threshold=1.0,
        )
    assert result["ok"]
    assert result["converged"] is True
    assert result["predicted_rms"] < 1.0


@pytest.mark.asyncio
async def test_predict_rms_session_not_found() -> None:
    target = {"points": [{"freq": 20, "spl": 75}, {"freq": 120, "spl": 75}], "band": [20, 120]}
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = []
        result = await _tool_predict_rms(
            filters=[], session_id=999, target_curve=target,
        )
    assert not result["ok"]


@pytest.mark.asyncio
async def test_predict_rms_returns_summary() -> None:
    """predict_rms returns per-band summary."""
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(120), 300).tolist()
    spls = [80.0] * len(freqs)
    session = _make_fr_session(freqs, spls)

    target = {
        "points": [{"freq": 20, "spl": 75.0}, {"freq": 120, "spl": 75.0}],
        "band": [20, 120],
    }
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        result = await _tool_predict_rms(
            filters=[], session_id=1, target_curve=target,
        )
    assert result["ok"]
    assert len(result["summary"]) > 5
    for band in result["summary"]:
        assert "freq_hz" in band
        assert "predicted_db" in band
        assert "target_db" in band
        assert "error_db" in band


# ── Dispatcher tests for new tools ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_call_tool_evaluate_transfer_function_dispatch() -> None:
    from calibrate.mcp_server import call_tool
    texts = await call_tool("evaluate_transfer_function", {
        "filters": [{"type": "peaking", "freq": 50, "gain_db": -3, "q": 2}],
        "query_freqs": [50.0],
    })
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert data["num_freqs"] == 1


@pytest.mark.asyncio
async def test_call_tool_per_filter_contribution_dispatch() -> None:
    from calibrate.mcp_server import call_tool
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(120), 200).tolist()
    spls = [75.0] * len(freqs)
    session = _make_fr_session(freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        texts = await call_tool("per_filter_contribution", {
            "filters": [{"type": "peaking", "freq": 50, "gain_db": -3, "q": 2}],
            "session_id": 1,
            "query_freqs": [50.0],
        })
    data = json.loads(texts[0].text)
    assert data["ok"]


@pytest.mark.asyncio
async def test_call_tool_interpolate_optimal_gain_dispatch() -> None:
    from calibrate.mcp_server import call_tool
    texts = await call_tool("interpolate_optimal_gain", {
        "freq": 80.0, "q": 4.0, "filter_type": "peaking",
        "measured_errors": [
            {"gain_applied": -6, "error_measured": -3},
            {"gain_applied": -3, "error_measured": 4},
        ],
    })
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "optimal_gain_db" in data


@pytest.mark.asyncio
async def test_call_tool_sensitivity_analysis_dispatch() -> None:
    from calibrate.mcp_server import call_tool
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(120), 200).tolist()
    spls = [80.0] * len(freqs)
    session = _make_fr_session(freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        texts = await call_tool("sensitivity_analysis", {
            "filters": [{"type": "peaking", "freq": 65, "gain_db": -5, "q": 1}],
            "session_id": 1,
            "target_curve": {
                "points": [{"freq": 20, "spl": 75}, {"freq": 120, "spl": 75}],
                "band": [20, 120],
            },
        })
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "sensitivities" in data


@pytest.mark.asyncio
async def test_call_tool_fit_correction_filter_dispatch() -> None:
    from calibrate.mcp_server import call_tool
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(120), 200).tolist()
    spls = [80.0] * len(freqs)
    session = _make_fr_session(freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        texts = await call_tool("fit_correction_filter", {
            "session_id": 1,
            "target_curve": {
                "points": [{"freq": 20, "spl": 75}, {"freq": 120, "spl": 75}],
                "band": [20, 120],
            },
            "freq_range": [40, 80],
        })
    data = json.loads(texts[0].text)
    assert data["ok"]


@pytest.mark.asyncio
async def test_call_tool_predict_rms_dispatch() -> None:
    from calibrate.mcp_server import call_tool
    import numpy as np
    freqs = np.logspace(np.log10(20), np.log10(120), 200).tolist()
    spls = [80.0] * len(freqs)
    session = _make_fr_session(freqs, spls)
    with patch("calibrate.storage.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [session]
        texts = await call_tool("predict_rms", {
            "session_id": 1,
            "filters": [{"type": "peaking", "freq": 65, "gain_db": -5, "q": 0.7}],
            "target_curve": {
                "points": [{"freq": 20, "spl": 75}, {"freq": 120, "spl": 75}],
                "band": [20, 120],
            },
        })
    data = json.loads(texts[0].text)
    assert data["ok"]
    assert "predicted_rms" in data


# ── Signal graph MCP tools ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_signal_graph_returns_summary_from_legacy_shim() -> None:
    """get_signal_graph always returns a graph — even on legacy configs."""
    from calibrate.mcp_server import _tool_get_signal_graph

    result = await _tool_get_signal_graph()
    assert result["ok"]
    g = result["graph"]
    assert "processors" in g
    assert "transducers" in g
    assert "groups" in g


@pytest.mark.asyncio
async def test_resolve_target_returns_transducers_for_group() -> None:
    from calibrate.mcp_server import _tool_resolve_target

    # Default legacy config synthesises a "bass" group with two subs.
    result = await _tool_resolve_target("bass")
    assert result["ok"]
    assert len(result["resolved"]) == 2
    assert all(r["role"] == "sub" for r in result["resolved"])


@pytest.mark.asyncio
async def test_resolve_target_unknown_returns_empty_list() -> None:
    from calibrate.mcp_server import _tool_resolve_target

    result = await _tool_resolve_target("no_such_thing")
    assert result["ok"]
    assert result["resolved"] == []


@pytest.mark.asyncio
async def test_set_routing_parses_string_keys_and_dispatches_to_default_dsp(
    mock_dsp,
) -> None:
    """set_routing accepts the JSON string-key shape and forwards ints to the driver."""
    from calibrate.mcp_server import _tool_set_routing

    # JSON object keys arrive as strings; the tool must coerce to int before
    # calling the driver so shadow indices match.
    result = await _tool_set_routing(
        {"2": {"1": True, "2": True, "3": True}}
    )
    assert result["ok"], result
    mock_dsp.set_routing.assert_awaited_once()
    (call_arg,) = mock_dsp.set_routing.await_args.args
    assert call_arg == {2: {1: True, 2: True, 3: True}}
    # Response echoes the normalised (int-keyed) routing for confirmation.
    assert result["routing"] == {2: {1: True, 2: True, 3: True}}


@pytest.mark.asyncio
async def test_set_routing_rejects_non_object_row(mock_dsp) -> None:
    from calibrate.mcp_server import _tool_set_routing

    result = await _tool_set_routing({"0": "not a dict"})
    assert not result["ok"]
    assert "must be an object" in result["error"]


@pytest.mark.asyncio
async def test_set_routing_rejects_non_integer_keys(mock_dsp) -> None:
    from calibrate.mcp_server import _tool_set_routing

    result = await _tool_set_routing({"left": {"0": True}})
    assert not result["ok"]
    assert "invalid routing shape" in result["error"]


@pytest.mark.asyncio
async def test_apply_eq_target_resolves_via_graph(
    mock_dsp, valid_filters,
) -> None:
    """apply_eq(target='bass') dispatches through the graph to the target's driver."""
    from calibrate.drivers.registry import DriverRegistry
    from calibrate.mcp_server import _tool_apply_eq

    # Legacy config synthesises subs on the 'minidsp' processor. Route the
    # mocked driver through the registry so the dispatch picks it up.
    registry = DriverRegistry(drivers={"minidsp": mock_dsp})
    with patch("calibrate.mcp_server._default_dsp_name", return_value="minidsp"), \
         patch("calibrate.mcp_server._drivers", registry):
        result = await _tool_apply_eq(valid_filters, target="bass")

    assert result["ok"], result
    # bass resolves to two subs → apply_eq called once per transducer
    assert mock_dsp.apply_eq.call_count == 2


@pytest.mark.asyncio
async def test_apply_eq_target_and_output_index_conflict_rejected(
    mock_dsp, valid_filters,
) -> None:
    from calibrate.mcp_server import _tool_apply_eq

    result = await _tool_apply_eq(valid_filters, output_index=0, target="bass")
    assert not result["ok"]
    assert "either target or output_index" in result["error"]


@pytest.mark.asyncio
async def test_apply_eq_unknown_target_rejected(mock_dsp, valid_filters) -> None:
    from calibrate.mcp_server import _tool_apply_eq

    with patch("calibrate.mcp_server._default_dsp_name", return_value="minidsp"):
        result = await _tool_apply_eq(valid_filters, target="no_such_group")
    assert not result["ok"]
    assert "unknown target" in result["error"]


@pytest.mark.asyncio
async def test_apply_eq_target_dispatches_across_multiple_dsps(
    valid_filters,
) -> None:
    """A group spanning two processors dispatches to each processor's driver."""
    from calibrate.drivers.registry import DriverRegistry
    from calibrate.mcp_server import _tool_apply_eq
    from calibrate.graph import (
        Processor, SignalGraph, SVS_PB12_NSD_PROFILE, Transducer, TransducerGroup,
    )

    cross_graph = SignalGraph(
        processors=(
            Processor(name="mini", driver_ref="minidsp", kind="dsp"),
            Processor(name="camilla", driver_ref="camilladsp", kind="dsp"),
        ),
        profiles=(SVS_PB12_NSD_PROFILE,),
        transducers=(
            Transducer(name="sub_l", role="sub", processor_ref="mini",
                       output_index=0, safety_profile_ref="svs_pb12_nsd"),
            Transducer(name="sub_r", role="sub", processor_ref="camilla",
                       output_index=1, safety_profile_ref="svs_pb12_nsd"),
        ),
        groups=(TransducerGroup(name="bass", members=("sub_l", "sub_r")),),
    )

    class _FakeCfg:
        signal_graph = cross_graph

    mini = AsyncMock()
    mini.current_preset.return_value = 0
    camilla = AsyncMock()
    camilla.current_preset.return_value = 0
    registry = DriverRegistry(drivers={"mini": mini, "camilla": camilla})

    with patch("calibrate.mcp_server._config", return_value=_FakeCfg()), \
         patch("calibrate.mcp_server._drivers", registry):
        result = await _tool_apply_eq(valid_filters, target="bass")

    assert result["ok"], result
    # Each DSP's apply_eq is called exactly once, with its transducer's output_index.
    mini.apply_eq.assert_awaited_once()
    camilla.apply_eq.assert_awaited_once()
    mini_kwargs = mini.apply_eq.await_args.kwargs
    camilla_kwargs = camilla.apply_eq.await_args.kwargs
    assert mini_kwargs["output_index"] == 0
    assert camilla_kwargs["output_index"] == 1
    # Response payload names the dispatch.
    applied_procs = {a["processor"] for a in result["applied"]}
    assert applied_procs == {"mini", "camilla"}


@pytest.mark.asyncio
async def test_apply_input_eq_target_validates_against_strictest_profile(
    mock_dsp,
) -> None:
    """Input EQ with a named target uses the strictest profile in the scope."""
    from calibrate.drivers.registry import DriverRegistry
    from calibrate.mcp_server import _tool_apply_input_eq
    from calibrate.graph import (
        Processor, SignalGraph, Transducer, TransducerGroup, TransducerProfile,
        SVS_PB12_NSD_PROFILE,
    )

    tight_main = TransducerProfile(
        name="tight_main", max_boost_per_band_db=2.0, min_boost_freq_hz=50.0,
        hpf_freq_hz=None, max_cumulative_boost_db=4.0,
    )
    mixed_graph = SignalGraph(
        processors=(Processor(name="dsp", driver_ref="minidsp", kind="dsp"),),
        profiles=(SVS_PB12_NSD_PROFILE, tight_main),
        transducers=(
            Transducer(name="sub", role="sub", processor_ref="dsp",
                       output_index=0, safety_profile_ref="svs_pb12_nsd"),
            Transducer(name="main", role="main", processor_ref="dsp",
                       output_index=1, safety_profile_ref="tight_main"),
        ),
        groups=(TransducerGroup(name="all", members=("sub", "main")),),
    )

    class _FakeCfg:
        signal_graph = mixed_graph

    registry = DriverRegistry(drivers={"dsp": mock_dsp})

    # +5 dB would be fine for SVS (6 dB limit) but violates tight_main (2 dB).
    # Strictest wins → rejected.
    bad = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 60.0, "gain_db": 5.0, "q": 1.0, "type": "peaking"},
    ]
    with patch("calibrate.mcp_server._config", return_value=_FakeCfg()), \
         patch("calibrate.mcp_server._drivers", registry):
        result = await _tool_apply_input_eq(bad, target="all")
    assert not result["ok"]
    assert "SafetyValidator" in result["error"]
    assert "tight_main" in result["error"]


def _default_config_cfg():
    """Return a Config using DEFAULT_CONFIG — bypasses the user's on-disk config."""
    from calibrate.config import Config, DEFAULT_CONFIG
    # Deep-copy via dict constructor so tests can mutate without touching DEFAULT_CONFIG.
    return Config({k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_CONFIG.items()})


@pytest.mark.asyncio
async def test_set_delay_target_dispatches_per_transducer() -> None:
    """set_delay(target='bass') applies the same delay to every sub via its driver."""
    import calibrate.mcp_server as sut
    from calibrate.drivers.registry import DriverRegistry

    drv = AsyncMock()
    registry = DriverRegistry(drivers={"minidsp": drv})

    with patch.object(sut, "_drivers", registry), \
         patch.object(sut, "_config", return_value=_default_config_cfg()):
        result = await sut._tool_set_delay(delay_ms=2.5, target="bass")

    assert result["ok"], result
    # Two subs synthesised by legacy shim (DEFAULT_CONFIG: outputs 0, 1 typed sub).
    assert drv.set_output_delay.await_count == 2
    call_outputs = {c.args[0] for c in drv.set_output_delay.await_args_list}
    assert call_outputs == {0, 1}


@pytest.mark.asyncio
async def test_set_polarity_target_dispatches_per_transducer() -> None:
    import calibrate.mcp_server as sut
    from calibrate.drivers.registry import DriverRegistry

    drv = AsyncMock()
    registry = DriverRegistry(drivers={"minidsp": drv})

    with patch.object(sut, "_drivers", registry), \
         patch.object(sut, "_config", return_value=_default_config_cfg()):
        result = await sut._tool_set_polarity(inverted=True, target="bass")

    assert result["ok"]
    assert drv.set_output_polarity.await_count == 2


@pytest.mark.asyncio
async def test_set_output_gain_target_dispatches_per_transducer() -> None:
    import calibrate.mcp_server as sut
    from calibrate.drivers.registry import DriverRegistry

    drv = AsyncMock()
    registry = DriverRegistry(drivers={"minidsp": drv})

    with patch.object(sut, "_drivers", registry), \
         patch.object(sut, "_config", return_value=_default_config_cfg()):
        result = await sut._tool_set_output_gain(gain_db=-3.0, target="bass")

    assert result["ok"]
    assert drv.set_output_gain.await_count == 2


@pytest.mark.asyncio
async def test_mute_output_target_dispatches_per_driver() -> None:
    """mute_output(target='bass') groups transducers by driver and calls once per driver."""
    import calibrate.mcp_server as sut
    from calibrate.drivers.registry import DriverRegistry

    drv = AsyncMock()
    registry = DriverRegistry(drivers={"minidsp": drv})

    with patch.object(sut, "_drivers", registry), \
         patch.object(sut, "_config", return_value=_default_config_cfg()):
        result = await sut._tool_mute_output(target="bass")

    assert result["ok"]
    # Both subs are on the same driver → one batched mute_outputs call.
    drv.mute_outputs.assert_awaited_once()
    assert set(drv.mute_outputs.await_args.args[0]) == {0, 1}


@pytest.mark.asyncio
async def test_set_delay_target_and_output_index_conflict_rejected() -> None:
    import calibrate.mcp_server as sut
    from calibrate.drivers.registry import DriverRegistry

    with patch.object(sut, "_drivers", DriverRegistry()):
        result = await sut._tool_set_delay(delay_ms=1.0, output_index=0, target="bass")
    assert not result["ok"]
    assert "either target or output_index" in result["error"]


@pytest.mark.asyncio
async def test_trigger_measurement_hdmi_uses_graph_sweep_context() -> None:
    """HDMI route with _drivers populated composes AVR + DSP contexts via the graph."""
    import calibrate.mcp_server as sut
    from calibrate.drivers.registry import DriverRegistry
    from calibrate.drivers.dsp_driver import DSPCapabilities

    # Mock Denon and DSP drivers; each returns a recording context.
    denon_events: list[str] = []
    dsp_events: list[str] = []

    class _RecCtx:
        def __init__(self, name: str, events: list[str]) -> None:
            self.name = name
            self._events = events
        async def __aenter__(self):
            self._events.append(f"enter:{self.name}")
            return self
        async def __aexit__(self, *_):
            self._events.append(f"exit:{self.name}")

    denon_drv = MagicMock()
    denon_drv.sweep_context = MagicMock(return_value=_RecCtx("denon", denon_events))
    dsp_drv = MagicMock()
    dsp_drv.sweep_context = MagicMock(return_value=_RecCtx("dsp", dsp_events))
    dsp_drv.capabilities = DSPCapabilities(
        max_delay_ms=30, max_preset_index=3,
        valid_sources=frozenset({"Analog", "Usb"}),
        processing_rate=96000, max_peq_slots=8, fir_capable=True,
        fir_min_taps=64, fir_max_taps_per_output=2048,
        fir_shared_tap_pool=4096, fir_sample_rate_hz=96000,
    )

    registry = DriverRegistry(drivers={"denon": denon_drv, "minidsp": dsp_drv})

    # Legacy shim synthesises denon+minidsp processor names, so the registry
    # keyed on those names is what the graph will look up.
    mock_sd = MagicMock()
    mock_sd.query_devices.return_value = [{"name": "UMIK-1", "max_input_channels": 1}]
    mock_fr = MagicMock()
    mock_engine = MagicMock()
    mock_engine.measure = AsyncMock(return_value=mock_fr)
    mock_store = MagicMock()
    mock_store.save_measurement.return_value = 42

    # Force the route to HDMI for this test (user's dev config may have it
    # set already; patch the Config.load to return a predictable state).
    from calibrate.config import Config, DEFAULT_CONFIG
    test_data = {**DEFAULT_CONFIG}
    test_data["measurement"] = {**DEFAULT_CONFIG["measurement"], "playback_route": "hdmi"}

    with (
        patch.dict(sys.modules, {"sounddevice": mock_sd}),
        patch("calibrate.measurement.MeasurementEngine", return_value=mock_engine),
        patch("calibrate.measurement.compute_session_metadata", return_value={"ir": {}}),
        patch("calibrate.storage.SessionStore", return_value=mock_store),
        patch.object(sut, "_drivers", registry),
        patch.object(sut, "_config", return_value=Config(test_data)),
    ):
        result = await sut._tool_trigger_measurement()

    assert result["ok"], result
    # Graph composer entered AVR first, then DSP; exited in reverse order.
    assert denon_events == ["enter:denon", "exit:denon"]
    assert dsp_events == ["enter:dsp", "exit:dsp"]


@pytest.mark.asyncio
async def test_apply_input_eq_target_dispatches_per_processor(valid_filters) -> None:
    """A group spanning two processors calls apply_input_eq on each driver."""
    from calibrate.drivers.registry import DriverRegistry
    from calibrate.mcp_server import _tool_apply_input_eq
    from calibrate.graph import (
        Processor, SignalGraph, Transducer, TransducerGroup,
        SVS_PB12_NSD_PROFILE,
    )

    cross_graph = SignalGraph(
        processors=(
            Processor(name="mini", driver_ref="minidsp", kind="dsp"),
            Processor(name="camilla", driver_ref="camilladsp", kind="dsp"),
        ),
        profiles=(SVS_PB12_NSD_PROFILE,),
        transducers=(
            Transducer(name="sub_l", role="sub", processor_ref="mini",
                       output_index=0, safety_profile_ref="svs_pb12_nsd"),
            Transducer(name="sub_r", role="sub", processor_ref="camilla",
                       output_index=1, safety_profile_ref="svs_pb12_nsd"),
        ),
        groups=(TransducerGroup(name="bass", members=("sub_l", "sub_r")),),
    )

    class _FakeCfg:
        signal_graph = cross_graph

    mini = AsyncMock()
    mini.current_preset.return_value = 0
    camilla = AsyncMock()
    camilla.current_preset.return_value = 0
    registry = DriverRegistry(drivers={"mini": mini, "camilla": camilla})

    with patch("calibrate.mcp_server._config", return_value=_FakeCfg()), \
         patch("calibrate.mcp_server._drivers", registry):
        result = await _tool_apply_input_eq(valid_filters, target="bass")

    assert result["ok"], result
    mini.apply_input_eq.assert_awaited_once()
    camilla.apply_input_eq.assert_awaited_once()
    procs = {a["processor"] for a in result["applied"]}
    assert procs == {"mini", "camilla"}


# ── Storage key migration ─────────────────────────────────────────────────────


def test_storage_legacy_dsp_keys_migrate_to_namespaced() -> None:
    """Flat legacy active_dsp_state keys are rewritten to processor-namespaced form."""
    import tempfile
    from pathlib import Path
    from calibrate.storage import SessionStore

    # Pre-seed a database with legacy flat keys using the raw sqlite API.
    import sqlite3
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        store = SessionStore(db_path=db)

        # Seed legacy keys directly (bypasses dsp_key helper).
        store.set_active_dsp("output_eq_0", {"filters": [], "preset": 0})
        store.set_active_dsp("delay_1", {"delay_ms": 2.5})
        store.set_active_dsp("input_eq", {"filters": [], "preset": 0})
        store.set_active_dsp("target_curve", {"type": "harman"})

        # Mutate directly to undo the namespacing the writer would have applied,
        # simulating a pre-migration database.
        with sqlite3.connect(db) as conn:
            conn.execute("DELETE FROM active_dsp_state")
            for k, d in [
                ("output_eq_0", '{"filters":[],"preset":0}'),
                ("delay_1", '{"delay_ms":2.5}'),
                ("input_eq", '{"filters":[],"preset":0}'),
                ("target_curve", '{"type":"harman"}'),
            ]:
                conn.execute(
                    "INSERT INTO active_dsp_state (key, timestamp, data) VALUES (?,?,?)",
                    (k, "2026-04-22T00:00:00Z", d),
                )

        # Open a fresh SessionStore on the same DB — migration runs in _migrate_schema.
        store2 = SessionStore(db_path=db)
        state = store2.get_active_dsp()
        keys = set(state.keys())

        # DSP-scoped keys are now namespaced. target_curve (non-DSP) is left flat.
        assert any(k.startswith("processor:") and k.endswith(":output:0:eq") for k in keys)
        assert any(k.startswith("processor:") and k.endswith(":output:1:delay") for k in keys)
        assert any(k.startswith("processor:") and k.endswith(":input:eq") for k in keys)
        assert "target_curve" in keys
        # No legacy-shaped DSP keys should remain.
        assert "output_eq_0" not in keys
        assert "delay_1" not in keys
        assert "input_eq" not in keys


def test_parse_dsp_key_handles_both_shapes() -> None:
    """parse_dsp_key tolerates namespaced and legacy keys during transient states."""
    from calibrate.storage import parse_dsp_key

    # Namespaced
    assert parse_dsp_key("processor:minidsp:output:2:eq") == {
        "processor": "minidsp", "kind": "output", "output_index": 2, "field": "eq",
    }
    assert parse_dsp_key("processor:camilla:input:eq") == {
        "processor": "camilla", "kind": "input", "field": "eq",
    }
    # Legacy (processor=None)
    assert parse_dsp_key("output_eq_3") == {
        "processor": None, "kind": "output", "output_index": 3, "field": "eq",
    }
    assert parse_dsp_key("input_eq") == {
        "processor": None, "kind": "input", "field": "eq",
    }
    # Non-DSP keys yield None
    assert parse_dsp_key("target_curve") is None
    assert parse_dsp_key("random_garbage") is None
