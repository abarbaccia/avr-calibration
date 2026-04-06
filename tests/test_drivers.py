"""Tests for calibrate.drivers — unit tests for DenonDriver, MinidspDriver, registry.

All network calls are mocked:
  - DenonDriver: denonavr module patched in sys.modules
  - MinidspDriver: respx mocks for minidspd HTTP API
  - Registry: Config mock
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from calibrate.drivers.base import DriverError
from calibrate.drivers.denon import DenonDriver
from calibrate.drivers.minidsp import MinidspDriver
from calibrate.drivers.registry import load_avr_driver, load_dsp_driver

MINIDSP_BASE = "http://localhost:5380"
DEVICE_URL = f"{MINIDSP_BASE}/devices/0"
CONFIG_URL = f"{MINIDSP_BASE}/devices/0/config"

# ── denonavr mock helpers ──────────────────────────────────────────────────────

def _make_denonavr_mock(volume: float = -30.0, connected: bool = True):
    receiver = MagicMock()
    receiver.async_setup = AsyncMock(
        side_effect=None if connected else Exception("connection refused")
    )
    receiver.async_update = AsyncMock()
    receiver.volume = volume
    receiver.input_func = "CBL/SAT"
    receiver.muted = False
    receiver.async_set_volume = AsyncMock()
    mod = MagicMock()
    mod.DenonAVR = MagicMock(return_value=receiver)
    return mod, receiver


# ── DenonDriver ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_denon_get_state_connected() -> None:
    mock_mod, mock_receiver = _make_denonavr_mock(volume=-30.0)
    with patch.dict(sys.modules, {"denonavr": mock_mod}):
        driver = DenonDriver(host="192.168.1.100")
        state = await driver.get_state()
    assert state["connected"]
    assert state["volume"] == -30.0
    assert state["host"] == "192.168.1.100"


@pytest.mark.asyncio
async def test_denon_get_state_no_host() -> None:
    driver = DenonDriver(host=None)
    state = await driver.get_state()
    assert not state["connected"]
    assert "no host" in state["error"]


@pytest.mark.asyncio
async def test_denon_get_state_timeout() -> None:
    mock_mod, mock_receiver = _make_denonavr_mock()
    mock_receiver.async_setup.side_effect = __import__("asyncio").TimeoutError()
    with patch.dict(sys.modules, {"denonavr": mock_mod}):
        driver = DenonDriver(host="192.168.1.100")
        with pytest.raises(DriverError, match="timeout"):
            await driver.get_state()


@pytest.mark.asyncio
async def test_denon_get_state_connection_error() -> None:
    mock_mod, mock_receiver = _make_denonavr_mock()
    mock_receiver.async_setup.side_effect = Exception("connection refused")
    with patch.dict(sys.modules, {"denonavr": mock_mod}):
        driver = DenonDriver(host="192.168.1.100")
        with pytest.raises(DriverError):
            await driver.get_state()


@pytest.mark.asyncio
async def test_denon_set_volume_success() -> None:
    mock_mod, mock_receiver = _make_denonavr_mock(volume=-25.0)
    with patch.dict(sys.modules, {"denonavr": mock_mod}):
        driver = DenonDriver(host="192.168.1.100")
        confirmed = await driver.set_volume(-25.0)
    assert confirmed == -25.0
    mock_receiver.async_set_volume.assert_called_once_with(-25.0)


@pytest.mark.asyncio
async def test_denon_set_volume_no_host() -> None:
    driver = DenonDriver(host=None)
    with pytest.raises(DriverError, match="no host"):
        await driver.set_volume(-30.0)


@pytest.mark.asyncio
async def test_denon_set_volume_clamps_below_min() -> None:
    mock_mod, mock_receiver = _make_denonavr_mock(volume=-80.0)
    with patch.dict(sys.modules, {"denonavr": mock_mod}):
        driver = DenonDriver(host="192.168.1.100")
        await driver.set_volume(-999.0)  # should clamp to -80
    mock_receiver.async_set_volume.assert_called_once_with(-80.0)


@pytest.mark.asyncio
async def test_denon_set_volume_clamps_above_max() -> None:
    mock_mod, mock_receiver = _make_denonavr_mock(volume=18.0)
    with patch.dict(sys.modules, {"denonavr": mock_mod}):
        driver = DenonDriver(host="192.168.1.100")
        await driver.set_volume(999.0)  # should clamp to +18
    mock_receiver.async_set_volume.assert_called_once_with(18.0)


@pytest.mark.asyncio
async def test_denon_setup_and_close_are_noop() -> None:
    driver = DenonDriver(host=None)
    await driver.setup()  # should not raise
    await driver.close()  # should not raise


@pytest.mark.asyncio
async def test_denon_discover_returns_empty() -> None:
    """discover() returns [] when SSDP finds no devices."""
    denonavr_mock = MagicMock()
    denonavr_mock.async_discover = AsyncMock(return_value=[])
    with patch.dict(sys.modules, {"denonavr": denonavr_mock}):
        driver = DenonDriver(host=None)
        assert await driver.discover() == []


@pytest.mark.asyncio
async def test_denon_discover_found() -> None:
    """discover() returns host list from SSDP scan."""
    denonavr_mock = MagicMock()
    denonavr_mock.async_discover = AsyncMock(return_value=[
        {"host": "192.168.1.209"},
        {"host": "192.168.1.210"},
    ])
    with patch.dict(sys.modules, {"denonavr": denonavr_mock}):
        driver = DenonDriver(host=None)
        result = await driver.discover()
    assert result == ["192.168.1.209", "192.168.1.210"]


@pytest.mark.asyncio
async def test_denon_discover_timeout() -> None:
    """discover() returns [] on timeout."""
    import asyncio
    denonavr_mock = MagicMock()
    denonavr_mock.async_discover = AsyncMock(side_effect=asyncio.TimeoutError())
    with patch.dict(sys.modules, {"denonavr": denonavr_mock}):
        driver = DenonDriver(host=None)
        assert await driver.discover() == []


# ── MinidspDriver ──────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_minidsp_get_state_connected() -> None:
    respx.get(DEVICE_URL).mock(return_value=httpx.Response(200, json={
        "master": {"preset": 0, "source": "Analog", "volume": -30.0, "mute": False}
    }))
    driver = MinidspDriver(host="localhost", port=5380)
    state = await driver.get_state()
    assert state["connected"]
    assert state["preset"] == 0
    assert state["source"] == "Analog"


@respx.mock
@pytest.mark.asyncio
async def test_minidsp_get_state_timeout() -> None:
    respx.get(DEVICE_URL).mock(side_effect=httpx.ConnectTimeout("timeout"))
    driver = MinidspDriver(host="localhost", port=5380)
    with pytest.raises(DriverError):
        await driver.get_state()


@respx.mock
@pytest.mark.asyncio
async def test_minidsp_current_preset_returns_preset() -> None:
    respx.get(DEVICE_URL).mock(return_value=httpx.Response(200, json={
        "master": {"preset": 2, "source": "Analog", "volume": -30.0, "mute": False}
    }))
    driver = MinidspDriver(host="localhost", port=5380)
    assert await driver.current_preset() == 2


@respx.mock
@pytest.mark.asyncio
async def test_minidsp_current_preset_defaults_to_zero_on_failure() -> None:
    respx.get(DEVICE_URL).mock(side_effect=Exception("connection refused"))
    driver = MinidspDriver(host="localhost", port=5380)
    assert await driver.current_preset() == 0


@pytest.mark.asyncio
async def test_minidsp_read_eq_starts_empty() -> None:
    driver = MinidspDriver(host="localhost", port=5380)
    assert await driver.read_eq(0) == []


@respx.mock
@pytest.mark.asyncio
async def test_minidsp_apply_eq_valid_writes_hardware() -> None:
    config_route = respx.post(CONFIG_URL).mock(return_value=httpx.Response(200))
    respx.get(DEVICE_URL).mock(return_value=httpx.Response(200, json={
        "master": {"preset": 0, "source": "Analog", "volume": -30.0, "mute": False}
    }))
    driver = MinidspDriver(host="localhost", port=5380)
    filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 80.0, "gain_db": 3.0, "q": 0.707, "type": "peaking"},
    ]
    await driver.apply_eq(0, filters)
    assert config_route.called
    # Mute-before-write pattern: mute gain + PEQ batch + unmute gain = 3 POSTs per output
    # Default sub_outputs = [0, 1], so 6 POSTs total for broadcast mode
    assert config_route.call_count == 6
    # State should be updated after successful write
    assert len(await driver.read_eq(0)) == len(filters)


@pytest.mark.asyncio
async def test_minidsp_apply_eq_missing_hpf_raises_driver_error() -> None:
    driver = MinidspDriver(host="localhost", port=5380)
    filters = [{"freq": 80.0, "gain_db": 3.0, "q": 0.707, "type": "peaking"}]
    with pytest.raises(DriverError, match="SafetyValidator"):
        await driver.apply_eq(0, filters)


@pytest.mark.asyncio
async def test_minidsp_apply_eq_boost_below_25hz_raises() -> None:
    driver = MinidspDriver(host="localhost", port=5380)
    filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 20.0, "gain_db": 3.0, "q": 0.707, "type": "peaking"},
    ]
    with pytest.raises(DriverError, match="SafetyValidator"):
        await driver.apply_eq(0, filters)


@pytest.mark.asyncio
async def test_minidsp_apply_eq_invalid_spec_raises() -> None:
    driver = MinidspDriver(host="localhost", port=5380)
    filters = [{"freq": "bad", "gain_db": 0.0, "q": 0.707, "type": "hpf"}]
    with pytest.raises(DriverError, match="invalid filter spec"):
        await driver.apply_eq(0, filters)


@pytest.mark.asyncio
async def test_minidsp_apply_eq_too_many_filters_raises() -> None:
    driver = MinidspDriver(host="localhost", port=5380)
    # 9 PEQ slots available; provide 10
    filters = [{"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"}] * 10
    with pytest.raises(DriverError, match="too many filters"):
        await driver.apply_eq(0, filters)


@respx.mock
@pytest.mark.asyncio
async def test_minidsp_apply_eq_hardware_failure_no_state_update() -> None:
    """P0: if hardware write fails, _eq_state must NOT be updated."""
    respx.post(CONFIG_URL).mock(return_value=httpx.Response(500))
    driver = MinidspDriver(host="localhost", port=5380)
    filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 80.0, "gain_db": 3.0, "q": 0.707, "type": "peaking"},
    ]
    with pytest.raises(DriverError, match="minidsp write failed"):
        await driver.apply_eq(0, filters)

    # State must be unchanged after failed write
    assert await driver.read_eq(0) == []


@respx.mock
@pytest.mark.asyncio
async def test_minidsp_apply_eq_updates_state_only_on_success() -> None:
    """State update and hardware write are atomic: state updates iff all writes pass."""
    respx.post(CONFIG_URL).mock(return_value=httpx.Response(200))
    driver = MinidspDriver(host="localhost", port=5380)
    filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 80.0, "gain_db": 3.0, "q": 0.707, "type": "peaking"},
    ]
    await driver.apply_eq(0, filters)
    state = await driver.read_eq(0)
    assert len(state) == len(filters)
    assert state[0]["freq"] == 18.0
    assert state[1]["freq"] == 80.0


@respx.mock
@pytest.mark.asyncio
async def test_minidsp_apply_eq_mutes_before_peq_write() -> None:
    """Mute-before-write: output is muted before PEQ, unmuted after."""
    call_log: list[dict] = []

    def _capture(request):
        import json
        call_log.append(json.loads(request.content))
        return httpx.Response(200)

    respx.post(CONFIG_URL).mock(side_effect=_capture)
    driver = MinidspDriver(host="localhost", port=5380, sub_outputs=[0])
    filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 80.0, "gain_db": -3.0, "q": 1.0, "type": "peaking"},
    ]
    await driver.apply_eq(0, filters, output_index=0)
    # Expect: mute(gain=-127) → PEQ batch → unmute(gain=0)
    assert len(call_log) == 3
    # First call: mute
    assert call_log[0]["outputs"][0]["gain"] == -127.0
    # Second call: PEQ write
    assert "peq" in call_log[1]["outputs"][0]
    # Third call: unmute
    assert call_log[2]["outputs"][0]["gain"] == 0.0


@respx.mock
@pytest.mark.asyncio
async def test_minidsp_apply_input_eq_mutes_outputs_during_write() -> None:
    """Input PEQ write mutes all sub outputs, writes, then unmutes."""
    call_log: list[dict] = []

    def _capture(request):
        import json
        call_log.append(json.loads(request.content))
        return httpx.Response(200)

    respx.post(CONFIG_URL).mock(side_effect=_capture)
    driver = MinidspDriver(host="localhost", port=5380, sub_outputs=[0, 2])
    filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 50.0, "gain_db": -2.0, "q": 1.0, "type": "peaking"},
    ]
    await driver.apply_input_eq(0, filters)
    # Expect: mute out0 + mute out2 → PEQ batch → unmute out0 + unmute out2
    assert len(call_log) == 5
    # First two: mute each output
    assert call_log[0]["outputs"][0]["gain"] == -127.0
    assert call_log[1]["outputs"][0]["gain"] == -127.0
    # Third: input PEQ write
    assert "inputs" in call_log[2]
    # Last two: unmute each output
    assert call_log[3]["outputs"][0]["gain"] == 0.0
    assert call_log[4]["outputs"][0]["gain"] == 0.0


@respx.mock
@pytest.mark.asyncio
async def test_minidsp_set_preset() -> None:
    respx.post(DEVICE_URL).mock(return_value=httpx.Response(200))
    driver = MinidspDriver(host="localhost", port=5380)
    await driver.set_preset(1)  # should not raise


@pytest.mark.asyncio
async def test_minidsp_setup_and_close_are_noop() -> None:
    driver = MinidspDriver(host="localhost", port=5380)
    await driver.setup()
    await driver.close()


# ── Registry ───────────────────────────────────────────────────────────────────

def _mock_config(avr_driver: str = "denon", dsp_driver: str = "minidsp"):
    cfg = MagicMock()
    cfg.avr_driver_name = avr_driver
    cfg.dsp_driver_name = dsp_driver
    cfg.denon = {"host": "192.168.1.100"}
    cfg.minidsp = {"host": "localhost", "port": 5380}
    cfg.minidsp_host_port = ("localhost", 5380)
    return cfg


def test_load_avr_driver_denon() -> None:
    cfg = _mock_config(avr_driver="denon")
    driver = load_avr_driver(cfg)
    assert isinstance(driver, DenonDriver)


def test_load_dsp_driver_minidsp() -> None:
    cfg = _mock_config(dsp_driver="minidsp")
    driver = load_dsp_driver(cfg)
    assert isinstance(driver, MinidspDriver)


def test_load_avr_driver_unknown_raises() -> None:
    cfg = _mock_config(avr_driver="yamaha")
    with pytest.raises(ValueError, match="Unknown AVR driver"):
        load_avr_driver(cfg)


def test_load_dsp_driver_unknown_raises() -> None:
    cfg = _mock_config(dsp_driver="camilla")
    with pytest.raises(ValueError, match="Unknown DSP driver"):
        load_dsp_driver(cfg)
