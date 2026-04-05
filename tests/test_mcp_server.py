"""Tests for calibrate.mcp_server — all tools and resources.

All hardware is mocked at the driver level (AVRDriver / DSPDriver).
No real network, hardware, or file access required.

Covers:
  - get_device_state: connected + unreachable for both drivers
  - get_measurement_history: returns sessions, handles empty, handles storage error
  - read_eq: returns in-memory state (flat on startup, updated after apply_eq)
  - apply_eq: valid filters → driver.apply_eq called
  - apply_eq: DriverError propagated as {ok: false}
  - avr_set_volume: success and failure cases
  - set_denon_volume: deprecated alias → same behaviour as avr_set_volume
  - trigger_measurement: no UMIK found → error
  - trigger_measurement: success (direct engine call, session saved)
  - trigger_measurement: engine error propagated
  - trigger_measurement: DenonSweepContext wraps engine when HDMI configured
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
    _read_resource,
    _tool_apply_eq,
    _tool_avr_set_volume,
    _tool_fetch_recipe,
    _tool_get_device_state,
    _tool_get_measurement_history,
    _tool_read_eq,
    _tool_trigger_measurement,
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

    async def apply_eq(preset: int, filters: list[dict]) -> None:
        _eq_state[preset] = list(filters)

    dsp.read_eq.side_effect = read_eq
    dsp.apply_eq.side_effect = apply_eq

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
async def test_avr_set_volume_success(mock_avr) -> None:
    mock_avr.set_volume.return_value = -25.0
    result = await _tool_avr_set_volume(-25.0)
    assert result["ok"]
    assert result["level_db"] == -25.0
    mock_avr.set_volume.assert_called_once_with(-25.0)


@pytest.mark.asyncio
async def test_avr_set_volume_no_host(mock_avr) -> None:
    mock_avr.set_volume.side_effect = DriverError("no host configured")
    result = await _tool_avr_set_volume(-30.0)
    assert not result["ok"]
    assert "avr unreachable" in result["error"]


@pytest.mark.asyncio
async def test_avr_set_volume_connection_error(mock_avr) -> None:
    mock_avr.set_volume.side_effect = DriverError("connection refused")
    result = await _tool_avr_set_volume(-30.0)
    assert not result["ok"]
    assert "avr unreachable" in result["error"]


@pytest.mark.asyncio
async def test_set_denon_volume_alias_dispatches(mock_avr) -> None:
    """set_denon_volume deprecated alias calls avr_set_volume behaviour."""
    mock_avr.set_volume.return_value = -25.0
    # Call via the MCP dispatch to verify the alias is wired up
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
        patch("calibrate.storage.SessionStore", return_value=mock_store),
        patch.object(sut, "DenonSweepContext") as MockCtx,
    ):
        MockCtx.from_config.return_value = None  # USB route
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
    ):
        MockCtx.from_config.return_value = None
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
        patch("calibrate.storage.SessionStore", return_value=mock_store),
        patch.object(sut, "DenonSweepContext") as MockCtx,
    ):
        MockCtx.from_config.return_value = mock_ctx_instance
        result = await _tool_trigger_measurement()

    assert result["ok"]
    assert result["session_id"] == 3
    mock_ctx_instance.__aenter__.assert_called_once()
    mock_ctx_instance.__aexit__.assert_called_once()


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
async def test_call_tool_trigger_measurement_dispatch() -> None:
    from calibrate.mcp_server import call_tool
    with patch.dict(sys.modules, {"sounddevice": None}):
        texts = await call_tool("trigger_measurement", {})
    data = json.loads(texts[0].text)
    assert not data["ok"]  # no UMIK on CI → degraded mode


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
    assert "trigger_measurement" in names
    assert "fetch_recipe" in names


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


# ── run_calibration_loop ────────────────────────────────────────────────────

