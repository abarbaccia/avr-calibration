"""Tests for calibrate.drivers — unit tests for DenonDriver, MinidspDriver, registry.

All network calls are mocked:
  - DenonDriver: denonavr module patched in sys.modules
  - MinidspDriver: CLI subprocess mocked via _run_minidsp_cli and _get_status_via_cli
  - Registry: Config mock
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from calibrate.adapters.minidsp import MinidspApiError
from calibrate.drivers.base import DriverError
from calibrate.drivers.denon import DenonDriver
from calibrate.drivers.minidsp import MinidspDriver, MinidspSweepContext
from calibrate.drivers.registry import load_avr_driver, load_dsp_driver

# Patch target for CLI status reads (used by get_state, current_preset, check_for_dsp_hang)
_ADAPTER_STATUS_CLI = "calibrate.adapters.minidsp._get_status_via_cli"

_GOOD_STATUS = {
    "master": {"preset": 0, "source": "Analog", "volume": -30.0, "mute": False},
    "input_levels": [-120.0, -120.0],
    "output_levels": [-120.0, -120.0, -120.0, -120.0],
}

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


def _make_denon_mock_with_audyssey(
    sound_mode: str = "Multi Ch Stereo",
    multi_eq: str = "Reference",
    volume: float = -30.0,
    power: str = "ON",
):
    """Mock factory that adds audyssey + soundmode + power surfaces."""
    mock_mod, mock_receiver = _make_denonavr_mock(volume=volume)
    mock_receiver.audyssey = MagicMock()
    mock_receiver.audyssey.async_update = AsyncMock()
    mock_receiver.audyssey.multi_eq = multi_eq
    mock_receiver.soundmode = MagicMock()
    mock_receiver.soundmode.sound_mode = sound_mode
    mock_receiver.power = power
    return mock_mod, mock_receiver


@pytest.mark.asyncio
async def test_audyssey_state_full_calibration_ready() -> None:
    """All settings in calibration-ready posture → calibration_ready=True."""
    mock_mod, _ = _make_denon_mock_with_audyssey(
        sound_mode="Multi Ch Stereo", multi_eq="Flat", power="ON"
    )
    with patch.dict(sys.modules, {"denonavr": mock_mod}):
        driver = DenonDriver(host="192.168.1.100")
        async def _telnet_stub(self, commands, **_):
            return {
                "PSDYNEQ ?": "PSDYNEQ OFF",
                "PSDYNVOL ?": "PSDYNVOL OFF",
                "PSMULTEQ: ?": "PSMULTEQ:FLAT",
                "PW?": "PWON",
            }
        with patch.object(DenonDriver, "telnet_query", _telnet_stub):
            state = await driver.audyssey_state_full()
    assert state["calibration_ready"] is True
    assert state["recommendations"] == []
    assert state["dynamic_eq"] == "OFF"
    assert state["dynamic_volume"] == "OFF"
    assert state["multi_eq"] == "FLAT"
    assert state["power"] == "ON"


@pytest.mark.asyncio
async def test_audyssey_state_full_dyneq_on_blocks_cal() -> None:
    """DYNEQ=ON → not calibration-ready; recommendation surfaced."""
    mock_mod, _ = _make_denon_mock_with_audyssey(
        sound_mode="Multi Ch Stereo", multi_eq="Flat", power="ON"
    )
    with patch.dict(sys.modules, {"denonavr": mock_mod}):
        driver = DenonDriver(host="192.168.1.100")
        async def _telnet_stub(self, commands, **_):
            return {
                "PSDYNEQ ?": "PSDYNEQ ON",
                "PSDYNVOL ?": "PSDYNVOL OFF",
                "PSMULTEQ: ?": "PSMULTEQ:FLAT",
                "PW?": "PWON",
            }
        with patch.object(DenonDriver, "telnet_query", _telnet_stub):
            state = await driver.audyssey_state_full()
    assert state["calibration_ready"] is False
    assert any("Dynamic EQ" in r for r in state["recommendations"])
    assert state["dynamic_eq"] == "ON"


@pytest.mark.asyncio
async def test_audyssey_state_full_direct_mode_bypasses_firs() -> None:
    """DIRECT sound mode bypasses Audyssey + pushed FIRs → not cal-ready."""
    mock_mod, _ = _make_denon_mock_with_audyssey(
        sound_mode="DIRECT", multi_eq="Flat", power="ON"
    )
    with patch.dict(sys.modules, {"denonavr": mock_mod}):
        driver = DenonDriver(host="192.168.1.100")
        async def _telnet_stub(self, commands, **_):
            return {
                "PSDYNEQ ?": "PSDYNEQ OFF",
                "PSDYNVOL ?": "PSDYNVOL OFF",
                "PSMULTEQ: ?": "PSMULTEQ:FLAT",
                "PW?": "PWON",
            }
        with patch.object(DenonDriver, "telnet_query", _telnet_stub):
            state = await driver.audyssey_state_full()
    assert state["calibration_ready"] is False
    assert any("Sound mode" in r and "bypass" in r.lower() for r in state["recommendations"])


@pytest.mark.asyncio
async def test_audyssey_state_full_multeq_off_blocks() -> None:
    """PSMULTEQ:OFF disables our pushed FIRs → not cal-ready."""
    mock_mod, _ = _make_denon_mock_with_audyssey(
        sound_mode="Multi Ch Stereo", multi_eq="Flat", power="ON"
    )
    with patch.dict(sys.modules, {"denonavr": mock_mod}):
        driver = DenonDriver(host="192.168.1.100")
        async def _telnet_stub(self, commands, **_):
            return {
                "PSDYNEQ ?": "PSDYNEQ OFF",
                "PSDYNVOL ?": "PSDYNVOL OFF",
                "PSMULTEQ: ?": "PSMULTEQ:OFF",
                "PW?": "PWON",
            }
        with patch.object(DenonDriver, "telnet_query", _telnet_stub):
            state = await driver.audyssey_state_full()
    assert state["calibration_ready"] is False
    assert any("MultEQ" in r for r in state["recommendations"])
    assert state["multi_eq"] == "OFF"


@pytest.mark.asyncio
async def test_audyssey_state_full_no_host_raises() -> None:
    """No host configured → DriverError."""
    driver = DenonDriver(host=None)
    with pytest.raises(DriverError, match="no host"):
        await driver.audyssey_state_full()


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

@pytest.mark.asyncio
async def test_minidsp_get_state_connected() -> None:
    with patch(_ADAPTER_STATUS_CLI, new_callable=AsyncMock, return_value=_GOOD_STATUS):
        driver = MinidspDriver(host="localhost", port=5380)
        state = await driver.get_state()
    assert state["connected"]
    assert state["preset"] == 0
    assert state["source"] == "Analog"


@pytest.mark.asyncio
async def test_minidsp_get_state_timeout() -> None:
    with patch(_ADAPTER_STATUS_CLI, new_callable=AsyncMock, side_effect=asyncio.TimeoutError()):
        driver = MinidspDriver(host="localhost", port=5380)
        with pytest.raises(DriverError):
            await driver.get_state()


@pytest.mark.asyncio
async def test_minidsp_current_preset_returns_preset() -> None:
    status = {"master": {"preset": 2, "source": "Analog", "volume": -30.0, "mute": False}}
    with patch(_ADAPTER_STATUS_CLI, new_callable=AsyncMock, return_value=status):
        driver = MinidspDriver(host="localhost", port=5380)
        assert await driver.current_preset() == 2


@pytest.mark.asyncio
async def test_minidsp_current_preset_defaults_to_zero_on_failure() -> None:
    with patch(_ADAPTER_STATUS_CLI, new_callable=AsyncMock,
               side_effect=MinidspApiError(1, "connection refused")):
        driver = MinidspDriver(host="localhost", port=5380)
        assert await driver.current_preset() == 0


@pytest.mark.asyncio
async def test_minidsp_eq_state_starts_empty() -> None:
    driver = MinidspDriver(host="localhost", port=5380)
    assert driver._eq_state.get(0, []) == []


def test_minidsp_rehydrate_empty_store_leaves_shadow_empty() -> None:
    driver = MinidspDriver(host="localhost", port=5380)
    driver.rehydrate_from_active_state({})
    assert driver._eq_state == {}
    assert driver._output_gain == {}
    assert driver._output_delay == {}
    assert driver._output_polarity == {}


def test_minidsp_rehydrate_populates_all_shadow_state() -> None:
    driver = MinidspDriver(
        host="localhost", port=5380,
        active_input=0, usb_input=1,
    )
    output_eq_filters = [
        {"freq": 33.0, "gain_db": -3.5, "q": 9.2, "type": "peaking"},
    ]
    input_eq_filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 45.0, "gain_db": 3.0, "q": 0.8, "type": "low_shelf"},
    ]
    active_state = {
        "output_eq_0": {"filters": output_eq_filters, "preset": 0, "timestamp": "x"},
        "output_eq_2": {"filters": output_eq_filters, "preset": 0, "timestamp": "x"},
        "input_eq": {"filters": input_eq_filters, "preset": 0, "timestamp": "x"},
        "gain_0": {"gain_db": -2.5, "timestamp": "x"},
        "delay_2": {"delay_ms": 6.15, "timestamp": "x"},
        "polarity_1": {"inverted": True, "timestamp": "x"},
        "target_curve": {"type": "harman", "timestamp": "x"},  # ignored
    }

    driver.rehydrate_from_active_state(active_state)

    assert driver._eq_state[(0, 0)] == output_eq_filters
    assert driver._eq_state[(0, 2)] == output_eq_filters
    # input EQ written to both active_input and usb_input
    assert driver._eq_state[("input", 0, 0)] == input_eq_filters
    assert driver._eq_state[("input", 1, 0)] == input_eq_filters
    assert driver._output_gain[0] == -2.5
    assert driver._output_delay[2] == 6.15
    assert driver._output_polarity[1] is True


def test_minidsp_rehydrate_skips_malformed_entries() -> None:
    driver = MinidspDriver(host="localhost", port=5380)
    active_state = {
        "gain_0": {"missing_gain_db_field": True},
        "delay_1": {"delay_ms": "not-a-float"},
        "output_eq_bad": {"filters": [], "preset": 0},  # bad index
        "gain_1": {"gain_db": 1.5},  # this one is valid
    }
    driver.rehydrate_from_active_state(active_state)
    assert driver._output_gain == {1: 1.5}
    assert driver._output_delay == {}


def test_minidsp_rehydrate_shadow_survives_restart_round_trip(tmp_path: Path) -> None:
    """Full round-trip: persist state via SessionStore, rehydrate fresh driver."""
    from calibrate.storage import SessionStore

    store = SessionStore(tmp_path / "round_trip.db")
    store.set_active_dsp("gain_0", {"gain_db": -3.0})
    store.set_active_dsp("delay_2", {"delay_ms": 4.25})
    store.set_active_dsp("polarity_2", {"inverted": True})
    store.set_active_dsp("output_eq_1", {
        "filters": [{"freq": 50.0, "gain_db": -2.0, "q": 10.0, "type": "peaking"}],
        "preset": 0,
    })

    # Simulate process restart — brand-new driver, load from store
    driver = MinidspDriver(host="localhost", port=5380)
    driver.rehydrate_from_active_state(store.get_active_dsp())

    state = driver.get_output_state()
    assert state[0]["gain_db"] == -3.0
    assert state[2]["delay_ms"] == 4.25
    assert state[2]["polarity_inverted"] is True
    assert driver._eq_state[(0, 1)][0]["freq"] == 50.0


@pytest.mark.asyncio
async def test_minidsp_apply_eq_valid_writes_hardware() -> None:
    driver = MinidspDriver(host="localhost", port=5380)
    filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 80.0, "gain_db": 3.0, "q": 0.707, "type": "peaking"},
    ]
    with patch(_ADAPTER_STATUS_CLI, new_callable=AsyncMock, return_value=_GOOD_STATUS):
        with patch("calibrate.adapters.minidsp._run_minidsp_cli", new_callable=AsyncMock) as mock_cli:
            await driver.apply_eq(0, filters)
            # CLI called at least once per output (default sub_outputs=[0,1])
            assert mock_cli.call_count > 0
    # State should be updated after successful write
    assert len(driver._eq_state.get(0, [])) == len(filters)


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


@pytest.mark.asyncio
async def test_minidsp_apply_eq_hardware_failure_no_state_update() -> None:
    """P0: if hardware write fails, _eq_state must NOT be updated."""
    from calibrate.adapters.minidsp import MinidspApiError
    driver = MinidspDriver(host="localhost", port=5380)
    filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 80.0, "gain_db": 3.0, "q": 0.707, "type": "peaking"},
    ]
    with patch("calibrate.adapters.minidsp._run_minidsp_cli", new_callable=AsyncMock,
               side_effect=MinidspApiError(1, "cli: minidsp error")):
        with pytest.raises(DriverError, match="minidsp write failed"):
            await driver.apply_eq(0, filters)

    # State must be unchanged after failed write
    assert driver._eq_state.get(0, []) == []


@pytest.mark.asyncio
async def test_minidsp_apply_eq_updates_state_only_on_success() -> None:
    """State update and hardware write are atomic: state updates iff all writes pass."""
    driver = MinidspDriver(host="localhost", port=5380)
    filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 80.0, "gain_db": 3.0, "q": 0.707, "type": "peaking"},
    ]
    with patch("calibrate.adapters.minidsp._run_minidsp_cli", new_callable=AsyncMock):
        await driver.apply_eq(0, filters)
    state = driver._eq_state.get(0, [])
    assert len(state) == len(filters)
    assert state[0]["freq"] == 18.0
    assert state[1]["freq"] == 80.0


@pytest.mark.asyncio
async def test_minidsp_apply_eq_per_output() -> None:
    """Per-output EQ targets only the specified output index."""
    driver = MinidspDriver(host="localhost", port=5380, sub_outputs=[1])
    filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 80.0, "gain_db": -3.0, "q": 1.0, "type": "peaking"},
    ]
    with patch("calibrate.adapters.minidsp._run_minidsp_cli", new_callable=AsyncMock) as mock_cli:
        await driver.apply_eq(0, filters, output_index=1)
        # Output-targeted CLI calls must reference output 1; mute calls are excluded
        output_calls = [c for c in mock_cli.call_args_list if c.args[0] == "output"]
        for call in output_calls:
            args = call.args
            assert args[1] == "1", f"unexpected output index in CLI call: {args}"


@pytest.mark.asyncio
async def test_minidsp_apply_eq_negates_a1_a2_for_hardware() -> None:
    """CLI peq set must send negated a1/a2 (scipy→miniDSP positive sign convention)."""
    from calibrate.dsp import freq_gain_q_to_biquad
    driver = MinidspDriver(host="localhost", port=5380, sub_outputs=[1])
    filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 80.0, "gain_db": -3.0, "q": 1.0, "type": "peaking"},
    ]
    biquad = freq_gain_q_to_biquad(freq=80.0, gain_db=-3.0, q=1.0, filter_type="peaking")
    with patch("calibrate.adapters.minidsp._run_minidsp_cli", new_callable=AsyncMock) as mock_cli:
        await driver.apply_eq(0, filters, output_index=1)
        # Find the peq set call for the peaking filter (slot 3, the second filter)
        set_calls = [c for c in mock_cli.call_args_list if "set" in c.args]
        set_call = set_calls[1]  # second set call is the peaking filter
        args = set_call.args
        # args: ("output", "1", "peq", slot, "set", "--", b0, b1, b2, a1_hw, a2_hw)
        a1_hw = float(args[9])
        a2_hw = float(args[10])
        assert abs(a1_hw - (-biquad["a1"])) < 1e-9, f"a1_hw {a1_hw} != -a1_scipy {-biquad['a1']}"
        assert abs(a2_hw - (-biquad["a2"])) < 1e-9, f"a2_hw {a2_hw} != -a2_scipy {-biquad['a2']}"
        # No mute calls — mute guard was removed (it doesn't stop the DSP pipeline)
        all_args = [c.args for c in mock_cli.call_args_list]
        assert ("mute", "on") not in all_args, "unexpected mute on — mute guard was removed"


@pytest.mark.asyncio
async def test_minidsp_apply_eq_detects_dsp_hang() -> None:
    """apply_eq must raise DriverError if output level is frozen at 0.0 dBFS post-write."""
    hang_status = {
        "master": {"preset": 0, "source": "Analog", "volume": -30.0, "mute": False},
        "output_levels": [-120.0, 0.0, 0.0, -120.0],  # outputs 1,2 frozen → hang
    }
    driver = MinidspDriver(host="localhost", port=5380, sub_outputs=[1, 2])
    filters = [{"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"}]
    with patch(_ADAPTER_STATUS_CLI, new_callable=AsyncMock, return_value=hang_status):
        with patch("calibrate.adapters.minidsp._run_minidsp_cli", new_callable=AsyncMock):
            with pytest.raises(DriverError, match="DSP hang detected"):
                await driver.apply_eq(0, filters)


@pytest.mark.asyncio
async def test_minidsp_apply_input_eq_writes_via_cli() -> None:
    """Input PEQ writes to ALL active signal paths (USB input + analog input)."""
    # active_input=1 (Denon LFE on analog), usb_input=0 (USB sweep path)
    driver = MinidspDriver(host="localhost", port=5380, sub_outputs=[1, 2],
                           active_input=1, usb_input=0)
    filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 50.0, "gain_db": -2.0, "q": 1.0, "type": "peaking"},
    ]
    with patch("calibrate.adapters.minidsp._run_minidsp_cli", new_callable=AsyncMock) as mock_cli:
        await driver.apply_input_eq(0, filters)
        # Must write to BOTH input 0 (USB sweep path) AND input 1 (analog listening path)
        input_calls = [c for c in mock_cli.call_args_list if c.args[0] == "input"]
        written_inputs = {c.args[1] for c in input_calls}
        assert "0" in written_inputs, "USB sweep input (0) must receive input PEQ"
        assert "1" in written_inputs, "analog input (1) must receive input PEQ"

@pytest.mark.asyncio
async def test_minidsp_apply_input_eq_same_input_writes_once() -> None:
    """When usb_input == active_input, input 1 is not written (no duplicate)."""
    driver = MinidspDriver(host="localhost", port=5380, sub_outputs=[1, 2],
                           active_input=0, usb_input=0)
    filters = [{"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"}]
    with patch("calibrate.adapters.minidsp._run_minidsp_cli", new_callable=AsyncMock) as mock_cli:
        await driver.apply_input_eq(0, filters)
        input_calls = [c for c in mock_cli.call_args_list if c.args[0] == "input"]
        written_inputs = {c.args[1] for c in input_calls}
        assert "0" in written_inputs, "input 0 must be written"
        assert "1" not in written_inputs, "input 1 must not be written when usb_input==active_input"


@pytest.mark.asyncio
async def test_reapply_volatile_output_state_skips_input_peq() -> None:
    """reapply_volatile_output_state() must NOT re-send input PEQ.

    Input PEQ survives source switches on the miniDSP 2x4 HD — it is written
    to both USB and Analog inputs at apply time and does not need restore.
    """
    driver = MinidspDriver(host="localhost", port=5380, sub_outputs=[1, 2],
                           active_input=1, usb_input=0)
    filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 40.0, "gain_db": 3.0, "q": 1.5, "type": "peaking"},
    ]

    with patch(_ADAPTER_STATUS_CLI, new_callable=AsyncMock, return_value=_GOOD_STATUS):
        with patch("calibrate.adapters.minidsp._run_minidsp_cli", new_callable=AsyncMock) as mock_cli:
            await driver.apply_input_eq(0, filters)
            mock_cli.reset_mock()

            await driver.reapply_volatile_output_state()

            all_args = [c.args for c in mock_cli.call_args_list]
            input_set_calls = [a for a in all_args if a[0] == "input" and "set" in a]
            assert len(input_set_calls) == 0, (
                "Input PEQ must NOT be re-sent during volatile state restore — "
                "it survives source switches"
            )


@pytest.mark.asyncio
async def test_reapply_volatile_output_state_restores_gain_and_peq() -> None:
    """reapply_volatile_output_state() must re-send non-zero gains and per-output PEQ via CLI."""
    driver = MinidspDriver(host="localhost", port=5380, sub_outputs=[1, 2])
    filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 63.0, "gain_db": -6.0, "q": 4.0, "type": "peaking"},
    ]

    with patch(_ADAPTER_STATUS_CLI, new_callable=AsyncMock, return_value=_GOOD_STATUS):
        with patch("calibrate.adapters.minidsp._run_minidsp_cli", new_callable=AsyncMock) as mock_cli:
            # Simulate apply_eq for output 1 (stores state) and a gain trim on output 2
            await driver.apply_eq(0, filters, output_index=1)
            mock_cli.reset_mock()

            # Simulate gain set on output 2 (-4.3 dB trim)
            await driver.set_output_gain(2, -4.3)
            mock_cli.reset_mock()

            # Now simulate source-switch restore
            await driver.reapply_volatile_output_state()

            all_args = [c.args for c in mock_cli.call_args_list]
            # Gain restore: output 2 gain must be re-sent (-4.3 dB)
            gain_calls = [a for a in all_args if a[:2] == ("output", "2") and "gain" in a]
            assert gain_calls, "expected gain restore call for output 2"
            assert "--" in gain_calls[0] and str(-4.3) in gain_calls[0], \
                f"gain call args wrong: {gain_calls[0]}"

            # PEQ restore: output 1 peq set calls must be re-sent
            peq_set_calls = [a for a in all_args if a[:2] == ("output", "1") and "set" in a]
            assert peq_set_calls, "expected PEQ set restore calls for output 1"


@pytest.mark.asyncio
async def test_reapply_volatile_output_state_skips_zero_gain() -> None:
    """reapply_volatile_output_state() must skip gain restore if gain is 0.0 (hardware default)."""
    driver = MinidspDriver(host="localhost", port=5380, sub_outputs=[1])

    with patch(_ADAPTER_STATUS_CLI, new_callable=AsyncMock, return_value=_GOOD_STATUS):
        with patch("calibrate.adapters.minidsp._run_minidsp_cli", new_callable=AsyncMock) as mock_cli:
            await driver.set_output_gain(1, 0.0)  # explicitly set to 0 — should be skipped
            mock_cli.reset_mock()

            await driver.reapply_volatile_output_state()

            gain_calls = [a for a in [c.args for c in mock_cli.call_args_list]
                          if "gain" in a]
            assert not gain_calls, f"gain=0.0 should be skipped but got: {gain_calls}"


@pytest.mark.asyncio
async def test_minidsp_set_preset() -> None:
    driver = MinidspDriver(host="localhost", port=5380)
    with patch("calibrate.adapters.minidsp._run_minidsp_cli", new_callable=AsyncMock) as mock_cli:
        await driver.set_preset(1)
    mock_cli.assert_called_once_with("preset", "1")


@pytest.mark.asyncio
async def test_minidsp_setup_and_close_are_noop() -> None:
    driver = MinidspDriver(host="localhost", port=5380)
    await driver.setup()
    await driver.close()


# ── MinidspSweepContext ─────────────────────────────────────────────────────────

# MinidspSweepContext calls _run_minidsp_cli, _get_source_via_cli, and
# _configure_routing_via_cli — all module-level in calibrate.drivers.minidsp.
# Patch there (not in the adapter) since the name was imported at load time.
_CTX_CLI  = "calibrate.drivers.minidsp._run_minidsp_cli"
_GET_SRC  = "calibrate.drivers.minidsp._get_source_via_cli"
_CFG_RT   = "calibrate.drivers.minidsp._configure_routing_via_cli"

# MinidspDriver methods (apply_eq, set_output_gain, etc.) call _run_minidsp_cli
# through MinidspClient, which lives in the adapter module — patch there.
_ADAPTER_CLI = "calibrate.adapters.minidsp._run_minidsp_cli"


def _usb_sweep_config(
    route: str = "usb",
    output_channel: int = 1,
    active_input: int = 0,
    slots: list | None = None,
):
    cfg = MagicMock()
    cfg.measurement = {
        "playback_route": route,
        "output_channel": output_channel,
    }
    cfg.minidsp = {
        "active_input": active_input,
        "output_slots": slots or [
            {"index": 0, "type": "sub"},
            {"index": 1, "type": "sub"},
            {"index": 2, "type": "unused"},
            {"index": 3, "type": "unused"},
        ],
    }
    return cfg


def test_sweep_context_from_config_returns_none_for_hdmi():
    """from_config() returns None when playback_route != 'usb'."""
    cfg = _usb_sweep_config(route="hdmi")
    assert MinidspSweepContext.from_config(cfg) is None


def test_sweep_context_from_config_returns_context_for_usb():
    """from_config() returns a MinidspSweepContext when route == 'usb'."""
    cfg = _usb_sweep_config(route="usb")
    ctx = MinidspSweepContext.from_config(cfg)
    assert isinstance(ctx, MinidspSweepContext)


def test_sweep_context_from_config_maps_output_channel_to_usb_input():
    """output_channel=1 → usb_input=0 (USB left), output_channel=2 → usb_input=1."""
    cfg = _usb_sweep_config(output_channel=2)
    ctx = MinidspSweepContext.from_config(cfg)
    assert ctx._usb_input == 1  # 2 - 1 = 1


def test_sweep_context_from_config_excludes_unused_outputs():
    """Outputs marked 'unused' in config are excluded from enabled_outputs."""
    cfg = _usb_sweep_config()
    ctx = MinidspSweepContext.from_config(cfg)
    assert ctx._enabled_outputs == {0, 1}


@pytest.mark.asyncio
async def test_sweep_context_enter_switches_source_and_configures_routing():
    """__aenter__: if source != usb, switch source, sleep 1s, configure routing."""
    with (
        patch(_GET_SRC, new_callable=AsyncMock, return_value="Analog") as mock_src,
        patch(_CTX_CLI, new_callable=AsyncMock) as mock_cli,
        patch(_CFG_RT, new_callable=AsyncMock) as mock_rt,
        patch("calibrate.drivers.minidsp.asyncio.sleep", new_callable=AsyncMock),
    ):
        ctx = MinidspSweepContext(usb_input=0, normal_input=0, enabled_outputs={0, 1})
        await ctx.__aenter__()

    # source switch must be sent
    cli_sources = [c.args for c in mock_cli.call_args_list if c.args[:2] == ("source", "usb")]
    assert cli_sources, "expected 'source usb' CLI call"

    # routing must be configured
    mock_rt.assert_called_once_with(0, {0, 1})


@pytest.mark.asyncio
async def test_sweep_context_enter_skips_switch_when_already_usb():
    """__aenter__: if source == usb, skip switch+sleep but still configure routing."""
    with (
        patch(_GET_SRC, new_callable=AsyncMock, return_value="Usb"),
        patch(_CTX_CLI, new_callable=AsyncMock) as mock_cli,
        patch(_CFG_RT, new_callable=AsyncMock) as mock_rt,
        patch("calibrate.drivers.minidsp.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        ctx = MinidspSweepContext(usb_input=0, normal_input=0, enabled_outputs={0, 1})
        await ctx.__aenter__()

    # no source switch CLI call
    cli_sources = [c.args for c in mock_cli.call_args_list if "source" in c.args]
    assert not cli_sources, f"source switch should be skipped but got: {cli_sources}"

    # no sleep
    mock_sleep.assert_not_called()

    # routing still configured
    mock_rt.assert_called_once_with(0, {0, 1})


@pytest.mark.asyncio
async def test_sweep_context_enter_restores_mutes_after_source_switch():
    """__aenter__ restores all 4 output mutes via CLI after switching source."""
    driver = MinidspDriver(host="localhost", port=5380, sub_outputs=[0, 1])
    driver._output_muted = {1: True}  # output 1 was muted

    with (
        patch(_GET_SRC, new_callable=AsyncMock, return_value="Analog"),
        patch(_CTX_CLI, new_callable=AsyncMock) as mock_cli,
        patch(_CFG_RT, new_callable=AsyncMock),
        patch("calibrate.drivers.minidsp.asyncio.sleep", new_callable=AsyncMock),
    ):
        ctx = MinidspSweepContext(usb_input=0, normal_input=0, enabled_outputs={0, 1, 2, 3}, driver=driver)
        await ctx.__aenter__()

    all_args = [c.args for c in mock_cli.call_args_list]
    mute_calls = {(a[1], a[3]) for a in all_args if len(a) == 4 and a[0] == "output" and a[2] == "mute"}
    assert ("1", "on") in mute_calls, "output 1 should be muted (was tracked as muted)"
    assert ("0", "off") in mute_calls, "output 0 should be unmuted (not tracked)"


@pytest.mark.asyncio
async def test_sweep_context_enter_does_not_restore_mutes_when_already_usb():
    """__aenter__: no mute restore when source was already USB (no reset happened)."""
    driver = MinidspDriver(host="localhost", port=5380, sub_outputs=[0, 1])
    driver._output_muted = {1: True}

    with (
        patch(_GET_SRC, new_callable=AsyncMock, return_value="Usb"),
        patch(_CTX_CLI, new_callable=AsyncMock) as mock_cli,
        patch(_CFG_RT, new_callable=AsyncMock),
        patch("calibrate.drivers.minidsp.asyncio.sleep", new_callable=AsyncMock),
    ):
        ctx = MinidspSweepContext(usb_input=0, normal_input=0, enabled_outputs={0, 1}, driver=driver)
        await ctx.__aenter__()

    all_args = [c.args for c in mock_cli.call_args_list]
    mute_calls = [a for a in all_args if len(a) >= 3 and a[0] == "output" and a[2] == "mute"]
    assert not mute_calls, f"mute restore should be skipped when already USB, got: {mute_calls}"


@pytest.mark.asyncio
async def test_sweep_context_exit_restores_original_source():
    """__aexit__: restores original non-USB source, reconfigures routing."""
    with (
        patch(_GET_SRC, new_callable=AsyncMock, return_value="Analog"),
        patch(_CTX_CLI, new_callable=AsyncMock) as mock_cli,
        patch(_CFG_RT, new_callable=AsyncMock) as mock_rt,
        patch("calibrate.drivers.minidsp.asyncio.sleep", new_callable=AsyncMock),
    ):
        ctx = MinidspSweepContext(usb_input=0, normal_input=0, enabled_outputs={0, 1})
        await ctx.__aenter__()
        mock_cli.reset_mock()
        mock_rt.reset_mock()
        await ctx.__aexit__(None, None, None)

    # must restore original source
    restore_src_calls = [c.args for c in mock_cli.call_args_list if c.args[:1] == ("source",)]
    assert restore_src_calls, "expected source restore CLI call"
    assert restore_src_calls[0][1] == "analog"

    # routing must be reconfigured to normal_input
    mock_rt.assert_called_once_with(0, {0, 1})


@pytest.mark.asyncio
async def test_sweep_context_exit_skips_source_restore_when_already_usb():
    """__aexit__: no source restore when original was already USB."""
    with (
        patch(_GET_SRC, new_callable=AsyncMock, return_value="Usb"),
        patch(_CTX_CLI, new_callable=AsyncMock) as mock_cli,
        patch(_CFG_RT, new_callable=AsyncMock) as mock_rt,
        patch("calibrate.drivers.minidsp.asyncio.sleep", new_callable=AsyncMock),
    ):
        ctx = MinidspSweepContext(usb_input=0, normal_input=0, enabled_outputs={0, 1})
        await ctx.__aenter__()
        mock_cli.reset_mock()
        mock_rt.reset_mock()
        await ctx.__aexit__(None, None, None)

    # no source switch
    restore_src_calls = [c.args for c in mock_cli.call_args_list if c.args[:1] == ("source",)]
    assert not restore_src_calls, f"source restore should be skipped, got: {restore_src_calls}"

    # routing still reconfigured
    mock_rt.assert_called_once_with(0, {0, 1})


@pytest.mark.asyncio
async def test_sweep_context_exit_swallows_exceptions():
    """__aexit__ catches exceptions and logs a warning — does not raise."""
    with (
        patch(_GET_SRC, new_callable=AsyncMock, return_value="Analog"),
        patch(_CTX_CLI, new_callable=AsyncMock, side_effect=Exception("CLI error")),
        patch(_CFG_RT, new_callable=AsyncMock),
        patch("calibrate.drivers.minidsp.asyncio.sleep", new_callable=AsyncMock),
    ):
        ctx = MinidspSweepContext(usb_input=0, normal_input=0, enabled_outputs={0, 1})
        ctx._original_source = "Analog"  # bypass __aenter__
        # must not raise
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_sweep_context_enter_is_idempotent():
    """Calling enter() twice is a no-op on the second call (persistent session)."""
    with (
        patch(_GET_SRC, new_callable=AsyncMock, return_value="Analog"),
        patch(_CTX_CLI, new_callable=AsyncMock) as mock_cli,
        patch(_CFG_RT, new_callable=AsyncMock) as mock_rt,
        patch("calibrate.drivers.minidsp.asyncio.sleep", new_callable=AsyncMock),
    ):
        ctx = MinidspSweepContext(usb_input=0, normal_input=0, enabled_outputs={0, 1})
        await ctx.enter()
        assert ctx.active

        # Reset mocks and enter again
        mock_cli.reset_mock()
        mock_rt.reset_mock()
        await ctx.enter()

    # Second enter should not make any CLI calls
    assert not mock_cli.call_args_list, "Second enter() should be a no-op"
    assert not mock_rt.call_args_list, "Second enter() should not reconfigure routing"


@pytest.mark.asyncio
async def test_sweep_context_exit_sets_active_false():
    """exit() sets active=False so a subsequent enter() re-enters."""
    with (
        patch(_GET_SRC, new_callable=AsyncMock, return_value="Analog"),
        patch(_CTX_CLI, new_callable=AsyncMock),
        patch(_CFG_RT, new_callable=AsyncMock),
        patch("calibrate.drivers.minidsp.asyncio.sleep", new_callable=AsyncMock),
    ):
        ctx = MinidspSweepContext(usb_input=0, normal_input=0, enabled_outputs={0, 1})
        await ctx.enter()
        assert ctx.active
        await ctx.exit()
        assert not ctx.active


@pytest.mark.asyncio
async def test_sweep_context_exit_without_enter_is_noop():
    """exit() is safe to call without enter()."""
    ctx = MinidspSweepContext(usb_input=0, normal_input=0, enabled_outputs={0, 1})
    assert not ctx.active
    await ctx.exit()  # must not raise
    assert not ctx.active


@pytest.mark.asyncio
async def test_sweep_context_enter_does_not_restore_volatile_peq():
    """enter() must NOT call reapply_volatile_output_state — only mute restore."""
    driver = MinidspDriver(host="localhost", port=5380, sub_outputs=[0, 1])
    with (
        patch(_GET_SRC, new_callable=AsyncMock, return_value="Analog"),
        patch(_CTX_CLI, new_callable=AsyncMock),
        patch(_CFG_RT, new_callable=AsyncMock),
        patch("calibrate.drivers.minidsp.asyncio.sleep", new_callable=AsyncMock),
        patch.object(driver, "reapply_volatile_output_state", new_callable=AsyncMock) as mock_reapply,
    ):
        ctx = MinidspSweepContext(usb_input=0, normal_input=0, enabled_outputs={0, 1}, driver=driver)
        await ctx.enter()

    mock_reapply.assert_not_called()


@pytest.mark.asyncio
async def test_reapply_volatile_output_state_holds_lock():
    """reapply_volatile_output_state() acquires self._lock before any CLI write."""
    driver = MinidspDriver(host="localhost", port=5380, sub_outputs=[1])
    lock_was_held = []

    with patch(_ADAPTER_STATUS_CLI, new_callable=AsyncMock, return_value=_GOOD_STATUS):
        with patch(_ADAPTER_CLI, new_callable=AsyncMock) as mock_cli:
            # Pre-populate gain state without the lock-check side effect
            await driver.set_output_gain(1, -5.0)

            # Now install the lock-check side effect and observe only reapply calls
            async def check_lock(*args, **kwargs):
                lock_was_held.append(driver._lock.locked())

            mock_cli.side_effect = check_lock

            await driver.reapply_volatile_output_state()

    assert lock_was_held, "reapply must make at least one CLI call"
    assert all(lock_was_held), "lock must be held during every CLI call in reapply"


# ── DenonSweepContext ──────────────────────────────────────────────────────────

from calibrate.drivers.denon import DenonSweepContext


def _denon_sweep_config(settle_ms: int = 5000):
    """Build a minimal Config mock for DenonSweepContext.from_config()."""
    cfg = MagicMock()
    cfg.measurement = {
        "playback_route": "hdmi",
        "denon_sweep_input": "Videocore",
        "denon_sweep_volume": -20.0,
        "denon_settle_ms": settle_ms,
    }
    cfg.denon = {"host": "192.168.1.209"}
    return cfg


def test_denon_sweep_from_config_returns_none_for_usb():
    """from_config() returns None when playback_route == 'usb'."""
    cfg = _denon_sweep_config()
    cfg.measurement["playback_route"] = "usb"
    assert DenonSweepContext.from_config(cfg) is None


def test_denon_sweep_from_config_returns_context_for_hdmi():
    """from_config() returns a DenonSweepContext when HDMI configured."""
    cfg = _denon_sweep_config()
    ctx = DenonSweepContext.from_config(cfg)
    assert isinstance(ctx, DenonSweepContext)


def test_denon_sweep_from_config_settle_ms_default():
    """from_config() uses 5000ms default when denon_settle_ms not set."""
    cfg = _denon_sweep_config()
    del cfg.measurement["denon_settle_ms"]
    ctx = DenonSweepContext.from_config(cfg)
    assert ctx._settle_ms == 5000


def test_denon_sweep_from_config_returns_none_no_host():
    """from_config() returns None when denon.host is not set."""
    cfg = _denon_sweep_config()
    cfg.denon = {"host": None}
    assert DenonSweepContext.from_config(cfg) is None


def test_denon_sweep_from_config_returns_none_no_sweep_input():
    """from_config() returns None when denon_sweep_input is not set."""
    cfg = _denon_sweep_config()
    cfg.measurement["denon_sweep_input"] = None
    assert DenonSweepContext.from_config(cfg) is None


def test_denon_sweep_volume_ceiling_protects_against_corrupt_audio_path() -> None:
    """The MAX_SWEEP_VOLUME_DB cap is a hardware safety limit.

    Pinned at -15 dB as of 2026-05-04 — a corrupted FIR push (SET_DISFIL
    with empty FilData/DispData on X3800H) produced loud distorted output
    through MultEQ at sweep_volume=0 dB, audible from another room. The
    ceiling protects users + speakers from audio-path corruption modes
    the safety validator can't see at the wire-protocol level.

    This test pins the constant so it can't be silently raised. Any
    intentional change to it should require a code review."""
    assert DenonSweepContext.MAX_SWEEP_VOLUME_DB <= -15.0, (
        f"sweep_volume ceiling raised to {DenonSweepContext.MAX_SWEEP_VOLUME_DB}; "
        f"this is a safety regression — see 2026-05-04 incident notes."
    )


def test_denon_sweep_init_rejects_volume_above_ceiling() -> None:
    """Constructing a DenonSweepContext with a sweep_volume above the
    ceiling MUST raise. This is the wire that prevents calibrate.measure
    from accidentally sending a hot signal to the speakers."""
    import pytest as _pytest
    with _pytest.raises(ValueError, match="sweep_volume must be"):
        DenonSweepContext(host="x", sweep_input="AUX1", sweep_volume=0.0)
    with _pytest.raises(ValueError, match="sweep_volume must be"):
        DenonSweepContext(host="x", sweep_input="AUX1", sweep_volume=-10.0)
    # -15 (the ceiling) is allowed.
    DenonSweepContext(host="x", sweep_input="AUX1", sweep_volume=-15.0)
    DenonSweepContext(host="x", sweep_input="AUX1", sweep_volume=-30.0)


def _make_sweep_receiver(volume=-28.0, sound_mode="DTS SURROUND", power="ON"):
    """Build a denonavr mock receiver with all async methods needed by DenonSweepContext."""
    mock_mod, mock_receiver = _make_denonavr_mock(volume=volume)
    mock_receiver.input_func = "SHIELD"
    mock_receiver.power = power
    mock_receiver.async_set_input_func = AsyncMock()
    mock_receiver.async_set_volume = AsyncMock()
    mock_receiver.async_power_on = AsyncMock()
    mock_receiver.soundmode = MagicMock()
    mock_receiver.soundmode.sound_mode = sound_mode
    mock_receiver.soundmode.async_set_sound_mode = AsyncMock()
    return mock_mod, mock_receiver


@pytest.mark.asyncio
async def test_denon_sweep_enter_does_not_switch_input():
    """__aenter__ does NOT switch input — it checks and proceeds if correct."""
    mock_mod, mock_receiver = _make_sweep_receiver()
    mock_receiver.input_func = "Videocore"  # already on the right input

    with patch.dict(sys.modules, {"denonavr": mock_mod}):
        with patch("calibrate.drivers.denon.asyncio.sleep", new_callable=AsyncMock):
            ctx = DenonSweepContext(
                host="192.168.1.209", sweep_input="Videocore",
                sweep_volume=-20.0, settle_ms=100,
            )
            await ctx.__aenter__()

    mock_receiver.async_set_input_func.assert_not_called()
    mock_receiver.soundmode.async_set_sound_mode.assert_not_called()


@pytest.mark.asyncio
async def test_denon_sweep_enter_raises_on_wrong_input():
    """__aenter__ raises DriverError if AVR is on the wrong input."""
    from calibrate.drivers.denon import DriverError
    mock_mod, mock_receiver = _make_sweep_receiver()
    mock_receiver.input_func = "SHIELD"  # wrong input

    with patch.dict(sys.modules, {"denonavr": mock_mod}):
        with patch("calibrate.drivers.denon.asyncio.sleep", new_callable=AsyncMock):
            ctx = DenonSweepContext(
                host="192.168.1.209", sweep_input="CAL",
                sweep_volume=-20.0, settle_ms=100,
            )
            with pytest.raises(DriverError, match="AVR is on input 'SHIELD', expected 'CAL'"):
                await ctx.__aenter__()

    mock_receiver.async_set_input_func.assert_not_called()


@pytest.mark.asyncio
async def test_denon_sweep_exit_does_not_restore_input():
    """__aexit__ restores volume but never restores or touches the AVR input."""
    mock_mod, mock_receiver = _make_sweep_receiver()
    mock_receiver.input_func = "Videocore"

    with patch.dict(sys.modules, {"denonavr": mock_mod}):
        with patch("calibrate.drivers.denon.asyncio.sleep", new_callable=AsyncMock):
            ctx = DenonSweepContext(
                host="192.168.1.209", sweep_input="Videocore",
                sweep_volume=-20.0, settle_ms=100,
            )
            await ctx.__aenter__()
            mock_receiver.async_set_input_func.reset_mock()
            mock_receiver.soundmode.async_set_sound_mode.reset_mock()
            await ctx.__aexit__(None, None, None)

    mock_receiver.async_set_input_func.assert_not_called()
    mock_receiver.soundmode.async_set_sound_mode.assert_not_called()


# ── Registry ───────────────────────────────────────────────────────────────────

def _mock_config(avr_driver: str = "denon", dsp_driver: str = "minidsp",
                  processing_rate: int = 96_000):
    """Build a mock Config whose `signal_graph` synthesises the expected processors.

    The registry-based load_*_driver functions walk the graph to find which
    drivers to instantiate, so the mock has to expose a real SignalGraph with
    the correct processor nodes. We build one inline rather than stubbing it.
    """
    from calibrate.graph import (
        Processor, SignalGraph, SVS_PB12_NSD_PROFILE,
    )

    cfg = MagicMock()
    cfg.avr_driver_name = avr_driver
    cfg.dsp_driver_name = dsp_driver
    cfg.denon = {"host": "192.168.1.100"}
    cfg.minidsp = {"host": "localhost", "port": 5380}
    cfg.minidsp_host_port = ("localhost", 5380)
    cfg.camilladsp = {
        "host": "127.0.0.1", "port": 1234,
        "samplerate": 48_000, "chunksize": 1024,
        "input_channels": 2, "output_channels": 10,
    }
    cfg.sub_outputs = [0, 1]
    cfg.measurement = {"output_channel": 1}
    cfg.eq_capabilities = {"processing_rate": processing_rate}

    processors = []
    if avr_driver in {"denon"}:
        processors.append(Processor(name=avr_driver, driver_ref=avr_driver, kind="avr"))
    if dsp_driver in {"minidsp", "camilladsp"}:
        processors.append(Processor(name=dsp_driver, driver_ref=dsp_driver, kind="dsp"))
    cfg.signal_graph = SignalGraph(
        processors=tuple(processors),
        profiles=(SVS_PB12_NSD_PROFILE,),
    )
    return cfg


def test_load_avr_driver_denon() -> None:
    cfg = _mock_config(avr_driver="denon")
    driver = load_avr_driver(cfg)
    assert isinstance(driver, DenonDriver)


def test_load_dsp_driver_minidsp() -> None:
    cfg = _mock_config(dsp_driver="minidsp")
    driver = load_dsp_driver(cfg)
    assert isinstance(driver, MinidspDriver)


def test_load_dsp_driver_passes_processing_rate() -> None:
    """Registry passes processing_rate from config.eq_capabilities to the driver."""
    cfg = _mock_config(dsp_driver="minidsp", processing_rate=48_000)
    driver = load_dsp_driver(cfg)
    assert driver._processing_rate == 48_000


def test_load_dsp_driver_default_processing_rate() -> None:
    """Default processing_rate is 96000 (miniDSP 2x4 HD)."""
    cfg = _mock_config(dsp_driver="minidsp")
    cfg.eq_capabilities = {}  # no explicit processing_rate
    driver = load_dsp_driver(cfg)
    assert driver._processing_rate == 96_000


def test_load_avr_driver_unknown_raises() -> None:
    cfg = _mock_config(avr_driver="yamaha")
    with pytest.raises(ValueError, match="Unknown AVR driver"):
        load_avr_driver(cfg)


def test_load_dsp_driver_unknown_raises() -> None:
    cfg = _mock_config(dsp_driver="yamahadsp")
    with pytest.raises(ValueError, match="Unknown DSP driver"):
        load_dsp_driver(cfg)


def test_load_dsp_driver_camilladsp() -> None:
    """dsp_driver: camilladsp returns a CamillaDSPDriver wired from config.camilladsp."""
    cfg = _mock_config(dsp_driver="camilladsp")
    driver = load_dsp_driver(cfg)
    assert isinstance(driver, CamillaDSPDriver)
    assert driver._host == "127.0.0.1"
    assert driver._port == 1234
    assert driver._processing_rate == 48_000
    assert driver._chunksize == 1024
    assert driver._input_channels == 2
    assert driver._output_channels == 10
    assert driver._sub_outputs == [0, 1]


# ── set_master_gain ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_minidsp_set_master_gain_calls_cli() -> None:
    driver = MinidspDriver(host="localhost", port=5380)
    with patch("calibrate.adapters.minidsp._run_minidsp_cli", new_callable=AsyncMock) as mock_cli:
        await driver.set_master_gain(-30.0)
    mock_cli.assert_awaited_once_with("gain", "--", "-30.0")


@pytest.mark.asyncio
async def test_minidsp_set_master_gain_clamps_positive() -> None:
    driver = MinidspDriver(host="localhost", port=5380)
    with patch("calibrate.adapters.minidsp._run_minidsp_cli", new_callable=AsyncMock) as mock_cli:
        await driver.set_master_gain(5.0)
    # Positive values must be clamped to 0
    mock_cli.assert_awaited_once_with("gain", "--", "0.0")


@pytest.mark.asyncio
async def test_minidsp_set_master_gain_clamps_below_minus127() -> None:
    driver = MinidspDriver(host="localhost", port=5380)
    with patch("calibrate.adapters.minidsp._run_minidsp_cli", new_callable=AsyncMock) as mock_cli:
        await driver.set_master_gain(-200.0)
    mock_cli.assert_awaited_once_with("gain", "--", "-127.0")


# ── CamillaDSPDriver ───────────────────────────────────────────────────────────
#
# Partial driver — setup/close/get_state/set_master_gain are wired to the real
# websocket protocol; pipeline-editor-dependent methods still raise
# NotImplementedError until the 18i20 arrives and the edit layer lands. Tests
# mock _CamillaWSClient.call to assert protocol shape without a live daemon.

from calibrate.drivers.camilladsp import CamillaDSPDriver, _CamillaWSClient
from calibrate.drivers.base import DriverError
from calibrate.drivers.dsp_driver import DSPDriver as _DSPDriverBase


def test_camilladsp_is_a_dsp_driver() -> None:
    driver = CamillaDSPDriver()
    assert isinstance(driver, _DSPDriverBase)


def test_camilladsp_init_routes_all_routed_outputs_not_just_subs() -> None:
    """Regression for 2026-05-02: shaker (output 7) was silent on every
    container restart because initial routing was keyed off sub_outputs
    only. Driver must accept a wider routed_outputs list and apply it.
    """
    driver = CamillaDSPDriver(
        sub_outputs=[5, 6],
        routed_outputs=[5, 6, 7],   # subs + shaker
        output_channels=10,
    )
    # input 0 → 5, 6, 7 enabled; everything else disabled.
    row = driver._routing[0]
    assert row[5] is True
    assert row[6] is True
    assert row[7] is True
    assert row[0] is False
    assert row[8] is False
    # _sub_outputs preserved for sub-only operations (default FIR-clear
    # target etc.) — the shaker is NOT a sub.
    assert driver._sub_outputs == [5, 6]


def test_camilladsp_init_routed_outputs_defaults_to_sub_outputs() -> None:
    """Back-compat: omitting routed_outputs falls back to sub_outputs so
    legacy callers (tests, older configs) still work."""
    driver = CamillaDSPDriver(sub_outputs=[5, 6], output_channels=8)
    row = driver._routing[0]
    assert row[5] is True
    assert row[6] is True
    assert row[7] is False  # shaker would be silent here — back-compat


def test_make_camilladsp_routes_shaker_from_signal_graph(tmp_path) -> None:
    """The registry must walk the signal_graph and pass every transducer
    output (subs + shaker) into routed_outputs — closes the loop end-to-end."""
    import yaml
    from calibrate.config import Config
    from calibrate.drivers.registry import _make_camilladsp

    cfg_data = {
        "avr_driver": "denon",
        "dsp_driver": "camilladsp",
        "denon": {"host": "127.0.0.1"},
        "camilladsp": {
            "host": "127.0.0.1", "port": 1234,
            "input_channels": 2, "output_channels": 10,
            "samplerate": 48_000, "chunksize": 1024,
        },
        "signal_graph": {
            "sources": [{"name": "lfe", "type": "analog"}],
            "processors": [
                {"name": "denon", "driver_ref": "denon", "kind": "avr"},
                {"name": "camilla", "driver_ref": "camilladsp", "kind": "dsp",
                 "outputs": [str(i) for i in range(10)]},
            ],
            "transducers": [
                {"name": "sub_a", "role": "sub", "processor_ref": "camilla",
                 "output_index": 5, "safety_profile_ref": "svs_pb12_nsd"},
                {"name": "sub_b", "role": "sub", "processor_ref": "camilla",
                 "output_index": 6, "safety_profile_ref": "svs_pb12_nsd"},
                {"name": "shaker_a", "role": "shaker", "processor_ref": "camilla",
                 "output_index": 7, "safety_profile_ref": "svs_pb12_nsd"},
            ],
            "profiles": [
                {"name": "svs_pb12_nsd", "min_boost_freq_hz": 25,
                 "max_boost_per_band_db": 6, "max_cumulative_boost_db": 9,
                 "hpf_freq_hz": 18, "hpf_order": 4},
            ],
        },
    }
    cfg = Config(cfg_data)
    proc = next(p for p in cfg.signal_graph.processors if p.kind == "dsp")
    driver = _make_camilladsp(cfg, proc)
    row = driver._routing[0]
    # Subs AND shaker routed at init — no post-startup tool call needed.
    assert row[5] is True, "sub_a not routed"
    assert row[6] is True, "sub_b not routed"
    assert row[7] is True, "shaker not routed — would silently drop on restart"


def test_camilladsp_capabilities_reflect_single_pipeline_model() -> None:
    driver = CamillaDSPDriver(processing_rate=48_000)
    caps = driver.capabilities
    assert caps.processing_rate == 48_000
    assert caps.max_preset_index == -1       # no preset slots
    assert caps.valid_sources == frozenset()  # no source switching
    assert caps.max_delay_ms >= 100.0         # generous, not 30ms-cap
    assert caps.max_peq_slots >= 8
    # FIR: first-class on CamillaDSP, no shared pool, long taps supported.
    assert caps.fir_capable is True
    assert caps.fir_shared_tap_pool is None
    assert caps.fir_max_taps_per_output >= 8192
    assert caps.fir_sample_rate_hz == 48_000


def test_minidsp_capabilities_pin_hardware_limits() -> None:
    driver = MinidspDriver(host="localhost", port=5380)
    caps = driver.capabilities
    assert caps.max_delay_ms == 30.0
    assert caps.max_preset_index == 3
    assert "Analog" in caps.valid_sources
    assert "Usb" in caps.valid_sources
    assert caps.processing_rate == 96_000
    assert caps.max_peq_slots == 8   # slots 2-9
    # FIR: pinned to miniDSP 2x4 HD limits.
    assert caps.fir_capable is True
    assert caps.fir_max_taps_per_output == 2048
    assert caps.fir_shared_tap_pool == 4096
    assert caps.fir_sample_rate_hz == 96_000


@pytest.mark.asyncio
async def test_camilladsp_preset_semantics_are_single_pipeline() -> None:
    driver = CamillaDSPDriver()
    assert await driver.current_preset() == 0
    # set_preset is a documented no-op — calling any preset index must not raise
    await driver.set_preset(0)
    await driver.set_preset(3)


def test_camilladsp_output_state_shape_matches_minidsp_contract() -> None:
    driver = CamillaDSPDriver(output_channels=4)
    state = driver.get_output_state()
    assert set(state.keys()) == {0, 1, 2, 3}
    for per_out in state.values():
        assert set(per_out.keys()) == {
            "gain_db", "delay_ms", "polarity_inverted", "fir_taps",
        }
        assert per_out == {
            "gain_db": 0.0, "delay_ms": 0.0,
            "polarity_inverted": False, "fir_taps": 0,
        }


# ── CamillaDSPDriver — wired methods (setup/close/get_state/set_master_gain) ──


@pytest.mark.asyncio
async def test_camilladsp_setup_opens_ws_probes_version_but_does_not_push() -> None:
    """setup connects and probes version; it does NOT push a default pipeline.

    A fresh install keeps whatever config the daemon was started with (e.g.
    ``initial.yml``) until the first state mutation triggers a push. On
    restart, ``rehydrate_from_active_state`` reconciles the daemon with the
    persisted shadow — so a mid-session MCP restart doesn't wipe the running
    calibration by pushing an empty pipeline out from under it.
    """
    driver = CamillaDSPDriver()
    driver._client.connect = AsyncMock()
    driver._client.close = AsyncMock()
    driver._client.call = AsyncMock(return_value="2.0.3")

    await driver.setup()

    driver._client.connect.assert_awaited_once()
    # Only GetVersion — no SetConfig, no push of any kind.
    call_cmds = [c.args[0] for c in driver._client.call.await_args_list]
    assert call_cmds == ["GetVersion"]


@pytest.mark.asyncio
async def test_camilladsp_setup_closes_ws_on_probe_failure() -> None:
    """If GetVersion fails after connect, the socket must be closed before raising."""
    driver = CamillaDSPDriver()
    driver._client.connect = AsyncMock()
    driver._client.close = AsyncMock()
    driver._client.call = AsyncMock(side_effect=DriverError("protocol error"))

    with pytest.raises(DriverError):
        await driver.setup()

    driver._client.close.assert_awaited_once()




@pytest.mark.asyncio
async def test_camilladsp_close_closes_ws() -> None:
    driver = CamillaDSPDriver()
    driver._client.close = AsyncMock()
    await driver.close()
    driver._client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_camilladsp_get_state_disconnected_returns_minimal_dict() -> None:
    """get_state before setup must not raise — returns {connected: False}."""
    driver = CamillaDSPDriver(host="example.invalid")
    # _client.connected is False by default (no connect yet)
    state = await driver.get_state()
    assert state == {"connected": False, "host": "example.invalid"}


@pytest.mark.asyncio
async def test_camilladsp_get_state_returns_full_shape_when_connected() -> None:
    """Connected get_state calls GetState+GetVolume+GetMute+GetProcessingLoad."""
    driver = CamillaDSPDriver()

    # Fake a connected client with canned responses per command.
    responses = {
        "GetState": "Running",
        "GetVolume": -12.0,
        "GetMute": False,
        "GetProcessingLoad": 0.08,
    }
    driver._client._ws = object()  # mark as connected without a real ws
    driver._client.call = AsyncMock(side_effect=lambda cmd, *a, **kw: responses[cmd])

    state = await driver.get_state()

    assert state["connected"] is True
    assert state["state"] == "Running"
    assert state["volume"] == -12.0
    assert state["mute"] is False
    assert state["cpu_load"] == 0.08
    # Protocol-compatibility fields are present but stubbed.
    assert state["source"] is None
    assert state["preset"] == 0


@pytest.mark.asyncio
async def test_camilladsp_get_state_tolerates_missing_processing_load() -> None:
    """Old daemons may not support GetProcessingLoad; cpu_load becomes None."""
    driver = CamillaDSPDriver()

    def _call(cmd, *a, **kw):
        if cmd == "GetProcessingLoad":
            raise DriverError("unknown command")
        return {"GetState": "Running", "GetVolume": 0.0, "GetMute": False}[cmd]

    driver._client._ws = object()
    driver._client.call = AsyncMock(side_effect=_call)

    state = await driver.get_state()
    assert state["cpu_load"] is None
    assert state["connected"] is True


@pytest.mark.asyncio
async def test_camilladsp_set_master_gain_dispatches_setvolume() -> None:
    driver = CamillaDSPDriver()
    driver._client.call = AsyncMock(return_value=None)
    await driver.set_master_gain(-9.5)
    driver._client.call.assert_awaited_once_with("SetVolume", -9.5)


# ── _USBSweepContext ──────────────────────────────────────────────────────────

from calibrate.drivers.camilladsp import _USBSweepContext, _NoOpSweepContext


def _make_config_with_gain(gain_db):
    from unittest.mock import MagicMock
    cfg = MagicMock()
    cfg.measurement.get.side_effect = lambda k, default=None: (
        gain_db if k == "master_gain_db" else default
    )
    return cfg


def _make_camilla_stub(gains_set, initial_volume=-20.0):
    """Return a side_effect callable that stubs the CamillaDSP WS protocol."""
    def _call(cmd, *args):
        if cmd == "GetState":
            return "Running"
        if cmd == "GetVolume":
            return initial_volume
        if cmd == "GetMute":
            return False
        if cmd == "GetProcessingLoad":
            return 0.0
        if cmd == "SetVolume":
            gains_set.append(args[0])
            return None
        return None
    return _call


@pytest.mark.asyncio
async def test_usb_sweep_context_sets_and_restores_master_gain() -> None:
    driver = CamillaDSPDriver()
    gains_set = []

    driver._client._ws = object()  # mark as connected
    driver._client.call = AsyncMock(side_effect=_make_camilla_stub(gains_set))
    cfg = _make_config_with_gain(-50.0)

    ctx = _USBSweepContext(driver, cfg)
    async with ctx:
        assert ctx.active is True

    # Should have set -50.0 on enter, then restored -20.0 on exit
    assert gains_set == [-50.0, -20.0]
    assert ctx.active is False


@pytest.mark.asyncio
async def test_usb_sweep_context_restores_on_exception() -> None:
    driver = CamillaDSPDriver()
    gains_set = []

    driver._client._ws = object()
    driver._client.call = AsyncMock(side_effect=_make_camilla_stub(gains_set))
    cfg = _make_config_with_gain(-50.0)

    ctx = _USBSweepContext(driver, cfg)
    try:
        async with ctx:
            raise RuntimeError("simulated sweep failure")
    except RuntimeError:
        pass

    assert gains_set == [-50.0, -20.0]


def test_camilladsp_sweep_context_returns_usb_context_when_gain_configured() -> None:
    from calibrate.drivers.camilladsp import _USBSweepContext
    driver = CamillaDSPDriver()
    cfg = _make_config_with_gain(-50.0)
    ctx = driver.sweep_context(cfg)
    assert isinstance(ctx, _USBSweepContext)


def test_camilladsp_sweep_context_returns_noop_when_no_gain_configured() -> None:
    driver = CamillaDSPDriver()
    from unittest.mock import MagicMock
    cfg = MagicMock()
    cfg.measurement.get.side_effect = lambda k, default=None: default
    ctx = driver.sweep_context(cfg)
    assert isinstance(ctx, _NoOpSweepContext)


# ── _CamillaWSClient ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_camilla_ws_client_call_returns_unwrapped_value() -> None:
    """Ok response → the inner 'value' field is returned."""
    client = _CamillaWSClient("127.0.0.1", 1234)
    fake_ws = AsyncMock()
    fake_ws.send = AsyncMock()
    fake_ws.recv = AsyncMock(
        return_value='{"GetVersion": {"result": "Ok", "value": "2.0.3"}}'
    )
    client._ws = fake_ws

    result = await client.call("GetVersion")

    assert result == "2.0.3"
    # Request without args must serialise as the bare string command.
    sent_payload = fake_ws.send.await_args.args[0]
    assert sent_payload == '"GetVersion"'


@pytest.mark.asyncio
async def test_camilla_ws_client_call_sends_args_as_single_key_object() -> None:
    client = _CamillaWSClient("127.0.0.1", 1234)
    fake_ws = AsyncMock()
    fake_ws.send = AsyncMock()
    fake_ws.recv = AsyncMock(
        return_value='{"SetVolume": {"result": "Ok"}}'
    )
    client._ws = fake_ws

    await client.call("SetVolume", -10.0)

    import json as _json
    sent = _json.loads(fake_ws.send.await_args.args[0])
    assert sent == {"SetVolume": -10.0}


@pytest.mark.asyncio
async def test_camilla_ws_client_call_raises_on_error_result() -> None:
    client = _CamillaWSClient("127.0.0.1", 1234)
    fake_ws = AsyncMock()
    fake_ws.send = AsyncMock()
    fake_ws.recv = AsyncMock(
        return_value='{"SetVolume": {"result": "Error", "value": "out of range"}}'
    )
    client._ws = fake_ws

    with pytest.raises(DriverError, match="out of range"):
        await client.call("SetVolume", 999.0)


@pytest.mark.asyncio
async def test_camilla_ws_client_call_without_connect_raises() -> None:
    client = _CamillaWSClient("127.0.0.1", 1234)
    with pytest.raises(DriverError, match="not connected"):
        await client.call("GetVersion")


@pytest.mark.asyncio
async def test_camilla_ws_client_call_rejects_wrong_response_key() -> None:
    """Daemon bug / protocol drift — response for a different command is a hard fail."""
    client = _CamillaWSClient("127.0.0.1", 1234)
    fake_ws = AsyncMock()
    fake_ws.send = AsyncMock()
    fake_ws.recv = AsyncMock(
        return_value='{"GetVolume": {"result": "Ok", "value": 0.0}}'
    )
    client._ws = fake_ws

    with pytest.raises(DriverError, match="unexpected response shape"):
        await client.call("GetVersion")


# ── CamillaDSPDriver — pipeline editor (apply_eq / gain / delay / mute / FIR) ──
#
# All of these share the same test pattern: stub _client.call as an AsyncMock,
# invoke the driver method, assert (1) the shadow state updated correctly and
# (2) a SetConfig call was issued with a YAML payload that contains the
# expected named blocks. We parse the YAML back to a dict to make assertions
# structural rather than string-matching-dependent.


def _stub_client(driver: "CamillaDSPDriver") -> AsyncMock:
    """Replace the driver's ws client with an AsyncMock that swallows all calls."""
    driver._client._ws = object()  # mark "connected" so get_state-style paths work
    driver._client.call = AsyncMock(return_value=None)
    return driver._client.call


def _last_pushed_config(call_mock: AsyncMock) -> dict:
    """Return the parsed config dict from the most recent SetConfig call."""
    import yaml as _yaml
    for mock_call in reversed(call_mock.await_args_list):
        if mock_call.args and mock_call.args[0] == "SetConfig":
            return _yaml.safe_load(mock_call.args[1])
    raise AssertionError("no SetConfig call was recorded")


# SafetyValidator requires a mandatory 18 Hz HPF in every filter set — include
# it in test fixtures so apply_eq() exercises the happy path.
_HPF_18HZ = {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"}


@pytest.mark.asyncio
async def test_camilladsp_apply_eq_updates_shadow_and_pushes_pipeline() -> None:
    driver = CamillaDSPDriver(sub_outputs=[0, 1], output_channels=4, input_channels=2)
    call = _stub_client(driver)

    filters = [
        _HPF_18HZ,
        {"freq": 45.0, "gain_db": -4.0, "q": 3.0, "type": "peaking"},
        {"freq": 55.0, "gain_db": -2.5, "q": 2.0, "type": "peaking"},
    ]
    await driver.apply_eq(preset=0, filters=filters)

    # Shadow state updated for every sub output.
    assert [(f["freq"], f["type"]) for f in driver._output_eq[0]] == [
        (18.0, "hpf"), (45.0, "peaking"), (55.0, "peaking"),
    ]
    assert driver._output_eq[1] == driver._output_eq[0]

    cfg = _last_pushed_config(call)
    # Filter blocks exist for both sub outputs, named slotwise. HPF is slot 0.
    assert "cal_out0_peq_0" in cfg["filters"]   # HPF
    assert "cal_out0_peq_1" in cfg["filters"]   # 45 Hz peak
    assert "cal_out0_peq_2" in cfg["filters"]
    assert "cal_out1_peq_0" in cfg["filters"]
    # HPF maps to BiquadCombo Butterworth so it cascades as one filter.
    hpf_block = cfg["filters"]["cal_out0_peq_0"]
    assert hpf_block["type"] == "BiquadCombo"
    assert hpf_block["parameters"]["type"] == "ButterworthHighpass"
    assert hpf_block["parameters"]["order"] == 4
    # Peaking: Biquad with freq/q/gain.
    peq1 = cfg["filters"]["cal_out0_peq_1"]
    assert peq1["type"] == "Biquad"
    assert peq1["parameters"]["type"] == "Peaking"
    assert peq1["parameters"]["freq"] == 45.0
    assert peq1["parameters"]["gain"] == -4.0
    assert peq1["parameters"]["q"] == 3.0

    # Pipeline references the filter names on the right output channels.
    out0_step = next(
        s for s in cfg["pipeline"]
        if s.get("type") == "Filter" and s.get("channels") == [0]
    )
    assert "cal_out0_peq_0" in out0_step["names"]
    assert "cal_out0_peq_2" in out0_step["names"]


@pytest.mark.asyncio
async def test_camilladsp_apply_eq_targets_single_output() -> None:
    driver = CamillaDSPDriver(sub_outputs=[0, 1], output_channels=4)
    _stub_client(driver)

    await driver.apply_eq(
        preset=0,
        filters=[_HPF_18HZ, {"freq": 50.0, "gain_db": -3.0, "q": 2.0, "type": "peaking"}],
        output_index=1,
    )

    # Only output 1's shadow was touched; output 0 stays empty.
    assert driver._output_eq.get(0) in (None, [])
    assert len(driver._output_eq[1]) == 2


@pytest.mark.asyncio
async def test_camilladsp_apply_eq_runs_safety_validator() -> None:
    """+12 dB boost must be rejected by SafetyValidator — well above +6 dB cap."""
    driver = CamillaDSPDriver()
    _stub_client(driver)

    with pytest.raises(DriverError, match="SafetyValidator"):
        await driver.apply_eq(
            preset=0,
            filters=[_HPF_18HZ, {"freq": 40.0, "gain_db": 12.0, "q": 3.0, "type": "peaking"}],
            output_index=0,
        )
    # Shadow must remain clean on safety rejection.
    assert driver._output_eq.get(0) in (None, [])


@pytest.mark.asyncio
async def test_camilladsp_apply_eq_missing_hpf_rejected() -> None:
    """SafetyValidator requires the mandatory 18 Hz HPF in every filter set."""
    driver = CamillaDSPDriver()
    _stub_client(driver)
    with pytest.raises(DriverError, match="HPF"):
        await driver.apply_eq(
            preset=0,
            filters=[{"freq": 40.0, "gain_db": -3.0, "q": 2.0, "type": "peaking"}],
            output_index=0,
        )


@pytest.mark.asyncio
async def test_camilladsp_apply_eq_rejects_too_many_filters() -> None:
    driver = CamillaDSPDriver(max_peq_slots=2)
    _stub_client(driver)
    many = [_HPF_18HZ] + [
        {"freq": 40.0 + i, "gain_db": -1.0, "q": 2.0, "type": "peaking"} for i in range(3)
    ]
    with pytest.raises(DriverError, match="too many"):
        await driver.apply_eq(preset=0, filters=many, output_index=0)


@pytest.mark.asyncio
async def test_camilladsp_apply_eq_rolls_back_shadow_on_push_failure() -> None:
    driver = CamillaDSPDriver(sub_outputs=[0], output_channels=4)
    driver._client._ws = object()
    driver._client.call = AsyncMock(side_effect=DriverError("daemon reload failed"))

    with pytest.raises(DriverError, match="reload failed"):
        await driver.apply_eq(
            preset=0,
            filters=[_HPF_18HZ, {"freq": 50.0, "gain_db": -3.0, "q": 2.0, "type": "peaking"}],
            output_index=0,
        )
    assert driver._output_eq.get(0) in (None, [])


@pytest.mark.asyncio
async def test_camilladsp_apply_input_eq_writes_to_all_inputs_by_default() -> None:
    driver = CamillaDSPDriver(input_channels=2, output_channels=4)
    call = _stub_client(driver)

    await driver.apply_input_eq(
        preset=0,
        filters=[_HPF_18HZ, {"freq": 60.0, "gain_db": -2.0, "q": 1.4, "type": "peaking"}],
    )
    assert 0 in driver._input_eq and 1 in driver._input_eq

    cfg = _last_pushed_config(call)
    assert "cal_in0_peq_0" in cfg["filters"]
    assert "cal_in1_peq_0" in cfg["filters"]
    # Per-input Filter steps appear before the Mixer.
    step_types = [s.get("type") for s in cfg["pipeline"]]
    mixer_idx = step_types.index("Mixer")
    assert any(
        s.get("type") == "Filter" and s.get("channels") in ([0], [1])
        and "cal_in0_peq_0" in s.get("names", [])
        for s in cfg["pipeline"][:mixer_idx]
    )


@pytest.mark.asyncio
async def test_camilladsp_apply_input_eq_honours_explicit_input_index() -> None:
    driver = CamillaDSPDriver(input_channels=2)
    _stub_client(driver)
    await driver.apply_input_eq(
        preset=0,
        filters=[_HPF_18HZ, {"freq": 60.0, "gain_db": -2.0, "q": 1.4, "type": "peaking"}],
        input_index=1,
    )
    assert 0 not in driver._input_eq
    assert 1 in driver._input_eq


@pytest.mark.asyncio
async def test_camilladsp_set_output_gain_updates_shadow_and_pushes() -> None:
    driver = CamillaDSPDriver(output_channels=4)
    call = _stub_client(driver)

    await driver.set_output_gain(2, -3.5)
    assert driver._output_gain[2] == -3.5

    cfg = _last_pushed_config(call)
    gain_block = cfg["filters"]["cal_out2_gain"]
    assert gain_block["type"] == "Gain"
    assert gain_block["parameters"]["gain"] == -3.5
    assert gain_block["parameters"]["inverted"] is False
    assert gain_block["parameters"]["mute"] is False


@pytest.mark.asyncio
async def test_camilladsp_set_output_delay_updates_shadow_and_pushes() -> None:
    driver = CamillaDSPDriver(output_channels=4)
    call = _stub_client(driver)

    await driver.set_output_delay(0, 2.25)
    assert driver._output_delay[0] == 2.25

    cfg = _last_pushed_config(call)
    delay_block = cfg["filters"]["cal_out0_delay"]
    assert delay_block["type"] == "Delay"
    assert delay_block["parameters"]["delay"] == 2.25
    assert delay_block["parameters"]["unit"] == "ms"
    assert delay_block["parameters"]["subsample"] is True


@pytest.mark.asyncio
async def test_camilladsp_set_output_polarity_flows_into_gain_block() -> None:
    driver = CamillaDSPDriver(output_channels=4)
    call = _stub_client(driver)

    await driver.set_output_polarity(1, True)
    assert driver._output_polarity[1] is True

    cfg = _last_pushed_config(call)
    # Polarity is implemented as `inverted` on the Gain block, not a separate filter.
    assert cfg["filters"]["cal_out1_gain"]["parameters"]["inverted"] is True


@pytest.mark.asyncio
async def test_camilladsp_mute_then_unmute_toggles_gain_mute_flag() -> None:
    driver = CamillaDSPDriver(output_channels=4)
    call = _stub_client(driver)

    await driver.mute_outputs([0, 1])
    assert driver._output_muted[0] is True
    assert driver._output_muted[1] is True
    cfg = _last_pushed_config(call)
    assert cfg["filters"]["cal_out0_gain"]["parameters"]["mute"] is True
    assert cfg["filters"]["cal_out1_gain"]["parameters"]["mute"] is True

    await driver.unmute_outputs([0])
    assert driver._output_muted[0] is False
    assert driver._output_muted[1] is True  # untouched
    cfg = _last_pushed_config(call)
    assert cfg["filters"]["cal_out0_gain"]["parameters"]["mute"] is False
    assert cfg["filters"]["cal_out1_gain"]["parameters"]["mute"] is True


@pytest.mark.asyncio
async def test_camilladsp_set_routing_builds_mixer_mapping() -> None:
    driver = CamillaDSPDriver(input_channels=2, output_channels=4, sub_outputs=[0, 1])
    call = _stub_client(driver)

    # Route input 0 → outputs 0,1 only; input 1 silent.
    await driver.set_routing({
        0: {0: True, 1: True, 2: False, 3: False},
        1: {0: False, 1: False, 2: False, 3: False},
    })
    cfg = _last_pushed_config(call)
    mapping = cfg["mixers"]["output_router"]["mapping"]
    by_dest = {m["dest"]: m["sources"] for m in mapping}
    # outs 0/1 have exactly one source (input 0); outs 2/3 have none.
    assert len(by_dest[0]) == 1 and by_dest[0][0]["channel"] == 0
    assert len(by_dest[1]) == 1 and by_dest[1][0]["channel"] == 0
    assert by_dest[2] == []
    assert by_dest[3] == []


def test_camilladsp_default_routing_is_subs_only_not_broadcast() -> None:
    """Default routing must send input 0 only to sub_outputs, not to every channel.

    Regression guard: on a 10-output 18i20 where outputs 2-9 are wired to
    mains/tweeters, a default of "input 0 → all outputs" would blast the LFE
    sweep through every driver at full gain. The driver starts in the
    minimum safe state; any broader routing has to come from set_routing().
    """
    driver = CamillaDSPDriver(input_channels=2, output_channels=10, sub_outputs=[0, 1])
    # input 0 → subs only
    for out in range(10):
        expected = out in {0, 1}
        assert driver._routing[0][out] is expected, f"output {out} default mismatch"
    # input 1 → silent everywhere
    for out in range(10):
        assert driver._routing[1][out] is False


@pytest.mark.asyncio
async def test_camilladsp_set_master_gain_does_not_push_pipeline() -> None:
    """SetVolume bypasses the pipeline — no SetConfig call should be issued."""
    driver = CamillaDSPDriver()
    call = _stub_client(driver)
    await driver.set_master_gain(-7.5)
    cmds = [c.args[0] for c in call.await_args_list]
    assert "SetVolume" in cmds
    assert "SetConfig" not in cmds


@pytest.mark.asyncio
async def test_camilladsp_apply_fir_writes_conv_filter_with_inline_values() -> None:
    driver = CamillaDSPDriver(output_channels=4)
    call = _stub_client(driver)
    # Normalized tent so DC gain = 1.0 (0 dB) — avoids the SafetyValidator
    # tripping on the unnormalized +6 dB DC boost.
    coeffs = [0.0, 0.25, 0.5, 0.25, 0.0]
    await driver.apply_fir(0, coeffs)
    assert driver._fir_state[0] == coeffs

    cfg = _last_pushed_config(call)
    fir = cfg["filters"]["cal_out0_fir"]
    assert fir["type"] == "Conv"
    assert fir["parameters"]["type"] == "Values"
    assert fir["parameters"]["values"] == coeffs

    # FIR name should appear in the per-output Filter step.
    out0_step = next(
        s for s in cfg["pipeline"]
        if s.get("type") == "Filter" and s.get("channels") == [0]
    )
    assert "cal_out0_fir" in out0_step["names"]


@pytest.mark.asyncio
async def test_camilladsp_clear_fir_removes_conv_filter_from_pipeline() -> None:
    driver = CamillaDSPDriver(output_channels=4)
    _stub_client(driver)
    await driver.apply_fir(0, [0.0, 1.0, 0.0])
    call = driver._client.call  # retain the same mock

    await driver.clear_fir(0)
    cfg = _last_pushed_config(call)
    assert "cal_out0_fir" not in cfg["filters"]
    out0_step = next(
        s for s in cfg["pipeline"]
        if s.get("type") == "Filter" and s.get("channels") == [0]
    )
    assert "cal_out0_fir" not in out0_step["names"]


@pytest.mark.asyncio
async def test_camilladsp_apply_fir_rejects_too_many_taps() -> None:
    driver = CamillaDSPDriver()
    _stub_client(driver)
    too_long = [0.0] * (driver.capabilities.fir_max_taps_per_output + 1)
    with pytest.raises(DriverError, match="too many FIR taps"):
        await driver.apply_fir(0, too_long)


@pytest.mark.asyncio
async def test_camilladsp_apply_fir_rejects_empty_coefficients() -> None:
    driver = CamillaDSPDriver()
    _stub_client(driver)
    with pytest.raises(DriverError, match="empty"):
        await driver.apply_fir(0, [])


@pytest.mark.asyncio
async def test_camilladsp_apply_fir_rejects_wrong_sample_rate() -> None:
    driver = CamillaDSPDriver(processing_rate=48_000)
    _stub_client(driver)
    with pytest.raises(DriverError, match="sample rate"):
        await driver.apply_fir(0, [0.0, 1.0, 0.0], sample_rate=96_000)


def test_camilladsp_config_samples_devices_block_from_constructor_args() -> None:
    """The emitted `devices` section reflects the capture/playback/rate/chunksize args."""
    driver = CamillaDSPDriver(
        output_channels=10,
        input_channels=2,
        processing_rate=96_000,
        chunksize=2048,
    )
    cfg = driver._build_config()
    devices = cfg["devices"]
    assert devices["samplerate"] == 96_000
    assert devices["chunksize"] == 2048
    assert devices["capture"]["channels"] == 2
    assert devices["playback"]["channels"] == 10


def test_camilladsp_default_devices_are_pipewire_shaped() -> None:
    """Post-v0.2.0 the Pi runs PipeWire as the single audio orchestrator;
    CamillaDSP attaches as a native PipeWire client. Driver defaults must
    therefore emit PipeWire device dicts (node_name + autoconnect_to), not
    ALSA hw: device strings.

    The earlier ALSA-shaped defaults caused a watchdog-restart cascade when
    `start_calibration` reset DSP state: SetConfig pushed an ALSA-shaped
    device into the running PipeWire daemon, the audio thread died, the
    watchdog issued `systemctl restart camilladsp`, the restart hung in
    `deactivating`, and subsequent SetConfig calls failed with
    RateLimitExceededError. Regression guard.

    PipeWire negotiates format with WirePlumber (pinned to S32_LE / 48 kHz on
    the Scarlett); the device dict must NOT carry a `format` key.
    """
    driver = CamillaDSPDriver()
    cfg = driver._build_config()
    cap = cfg["devices"]["capture"]
    pb = cfg["devices"]["playback"]
    assert cap["type"] == "PipeWire"
    assert pb["type"] == "PipeWire"
    assert "node_name" in cap and cap["node_name"]
    assert "node_name" in pb and pb["node_name"]
    assert "autoconnect_to" in cap and "Scarlett" in cap["autoconnect_to"]
    assert "autoconnect_to" in pb and "Scarlett" in pb["autoconnect_to"]
    # PipeWire negotiates format; no explicit format key on the device dict.
    assert "format" not in cap
    assert "format" not in pb


def test_camilladsp_pipeline_filter_steps_use_channels_list() -> None:
    """CamillaDSP 2.x+ pipeline Filter steps use `channels: [N]` (plural list).

    Shipped once as `channel: N` (scalar, singular) and was rejected by the
    daemon with `pipeline: unknown field 'channel'`. Regression guard against
    reverting to the pre-2.x schema.
    """
    driver = CamillaDSPDriver(output_channels=4, input_channels=2)
    cfg = driver._build_config()
    filter_steps = [s for s in cfg["pipeline"] if s.get("type") == "Filter"]
    # Default shadow has no input-side PEQ, so only output-side filter steps.
    assert filter_steps, "at least one Filter step expected (per-output processing)"
    for step in filter_steps:
        assert "channels" in step, f"step {step!r} missing `channels` key"
        assert "channel" not in step, f"step {step!r} still uses singular `channel`"
        assert isinstance(step["channels"], list), (
            f"step {step!r}: channels must be a list, got {type(step['channels']).__name__}"
        )
        assert all(isinstance(c, int) for c in step["channels"]), (
            f"step {step!r}: channels entries must be int, got {step['channels']!r}"
        )


@pytest.mark.asyncio
async def test_camilladsp_preset_semantics_are_still_single_pipeline() -> None:
    """set_preset on CamillaDSP is documented as a no-op and must not push config."""
    driver = CamillaDSPDriver()
    call = _stub_client(driver)
    await driver.set_preset(0)
    await driver.set_preset(3)
    # No SetConfig should have been emitted by set_preset.
    assert not any(
        c.args and c.args[0] == "SetConfig" for c in call.await_args_list
    )


# ── CamillaDSPDriver — rehydrate_from_active_state ───────────────────────────


@pytest.mark.asyncio
async def test_camilladsp_rehydrate_populates_shadow_from_namespaced_keys() -> None:
    """rehydrate reads the namespaced active_dsp_state keys into shadow state."""
    driver = CamillaDSPDriver(output_channels=4, input_channels=2, sub_outputs=[0, 1])
    _stub_client(driver)

    active_state = {
        # Processor-namespaced keys — the shape that storage.dsp_output_key produces.
        "processor:camilla:output:0:eq": {
            "filters": [
                {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
                {"freq": 50.0, "gain_db": -3.0, "q": 2.0, "type": "peaking"},
            ],
            "preset": 0,
        },
        "processor:camilla:output:1:gain": {"gain_db": -2.5},
        "processor:camilla:output:1:delay": {"delay_ms": 1.5},
        "processor:camilla:output:0:polarity": {"inverted": True},
        "processor:camilla:input:eq": {
            "filters": [
                {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
            ],
            "preset": 0,
        },
        # Non-DSP keys are ignored.
        "target_curve": {"type": "harman"},
    }

    await driver.rehydrate_from_active_state(active_state)

    # Output EQ slot-0 picked up both filters.
    assert len(driver._output_eq[0]) == 2
    # Gain, delay, polarity threaded through.
    assert driver._output_gain[1] == -2.5
    assert driver._output_delay[1] == 1.5
    assert driver._output_polarity[0] is True
    # Input EQ replicated onto every input channel (matches apply_input_eq shape).
    assert 0 in driver._input_eq and 1 in driver._input_eq


@pytest.mark.asyncio
async def test_camilladsp_rehydrate_pushes_when_shadow_has_content() -> None:
    """rehydrate with persisted filters pushes the reconstructed config once."""
    driver = CamillaDSPDriver(output_channels=4)
    call = _stub_client(driver)

    active_state = {
        "processor:camilla:output:0:eq": {
            "filters": [{"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"}],
            "preset": 0,
        },
    }
    await driver.rehydrate_from_active_state(active_state)

    set_config_calls = [c for c in call.await_args_list
                        if c.args and c.args[0] == "SetConfig"]
    assert len(set_config_calls) == 1


@pytest.mark.asyncio
async def test_camilladsp_rehydrate_empty_state_does_not_push() -> None:
    """Fresh install: empty active_dsp_state → no push → daemon keeps initial.yml."""
    driver = CamillaDSPDriver()
    call = _stub_client(driver)

    await driver.rehydrate_from_active_state({})

    assert not any(
        c.args and c.args[0] == "SetConfig" for c in call.await_args_list
    )


@pytest.mark.asyncio
async def test_camilladsp_rehydrate_tolerates_legacy_flat_keys() -> None:
    """Defensive: legacy flat keys (pre-migration) parse cleanly too."""
    driver = CamillaDSPDriver(output_channels=4)
    _stub_client(driver)

    active_state = {
        "output_eq_0": {
            "filters": [{"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"}],
            "preset": 0,
        },
        "gain_1": {"gain_db": -1.5},
    }
    await driver.rehydrate_from_active_state(active_state)
    assert len(driver._output_eq[0]) == 1
    assert driver._output_gain[1] == -1.5


@pytest.mark.asyncio
async def test_camilladsp_mute_flips_source_mute_flag() -> None:
    """Muting flips ``mute: true`` on every routed source for that output;
    routing structure is preserved.

    Why source-mute and not source-removal: the original fix (PR #96) silenced
    by dropping mixer sources entirely, which silently rewrote routing on
    every mute call and confused any consumer that read the live config to
    verify state. Per-mixer-source ``mute`` fires upstream of the per-output
    FIR/Delay/PEQ chain, so the run-15 leak (Gain mute downstream of Conv/Delay
    not honored) does not apply, and routing is preserved as inspectable data.
    """
    driver = CamillaDSPDriver(output_channels=4, input_channels=2, sub_outputs=[0, 1])
    _stub_client(driver)

    await driver.set_routing({0: {0: True, 1: True}, 1: {}})
    await driver.mute_outputs([1])

    mixer = driver._build_mixer()["output_router"]
    mapping = {entry["dest"]: entry["sources"] for entry in mixer["mapping"]}
    # Output 0 unaffected — source still present and unmuted.
    assert len(mapping[0]) == 1
    assert mapping[0][0]["channel"] == 0
    assert mapping[0][0]["mute"] is False
    # Output 1's source is preserved (routing inspectable) but muted.
    assert len(mapping[1]) == 1
    assert mapping[1][0]["channel"] == 0
    assert mapping[1][0]["mute"] is True


@pytest.mark.asyncio
async def test_camilladsp_unmute_clears_source_mute_flag() -> None:
    """Unmute clears the mute flag on the source; routing is unchanged throughout."""
    driver = CamillaDSPDriver(output_channels=4, input_channels=2, sub_outputs=[0, 1])
    _stub_client(driver)

    await driver.set_routing({0: {0: True, 1: True}, 1: {}})
    await driver.mute_outputs([1])
    assert driver._build_mixer()["output_router"]["mapping"][1]["sources"][0]["mute"] is True

    await driver.unmute_outputs([1])
    mapping = driver._build_mixer()["output_router"]["mapping"]
    assert len(mapping[1]["sources"]) == 1
    assert mapping[1]["sources"][0]["channel"] == 0
    assert mapping[1]["sources"][0]["mute"] is False


@pytest.mark.asyncio
async def test_camilladsp_mute_does_not_mutate_routing() -> None:
    """The set of routed (channel, dest) pairs is invariant under mute/unmute.

    This is the structural property PR #96's fix violated: muting changed the
    routing structure (sources disappeared), so any consumer of GetConfigJson
    saw a different routing for muted outputs. Pin the property mechanically
    so future refactors can't reintroduce that behavior.
    """
    driver = CamillaDSPDriver(output_channels=4, input_channels=2, sub_outputs=[0, 1])
    _stub_client(driver)
    await driver.set_routing({0: {0: True, 1: True}, 1: {2: True}})

    def routing_pairs() -> set[tuple[int, int]]:
        mapping = driver._build_mixer()["output_router"]["mapping"]
        return {
            (src["channel"], entry["dest"])
            for entry in mapping
            for src in entry["sources"]
        }

    baseline = routing_pairs()
    await driver.mute_outputs([0, 1])
    assert routing_pairs() == baseline
    await driver.unmute_outputs([0])
    assert routing_pairs() == baseline
    await driver.unmute_outputs([1])
    assert routing_pairs() == baseline


@pytest.mark.asyncio
async def test_camilladsp_rehydrate_restores_mute_state() -> None:
    """active_dsp_state carries a 'mute' field now; rehydrate picks it up.

    Matches the eq/gain/delay/polarity/fir pattern; before this change mute
    was the only state field that evaporated on MCP restart.
    """
    driver = CamillaDSPDriver(output_channels=4, input_channels=2, sub_outputs=[0, 1])
    _stub_client(driver)
    await driver.set_routing({0: {0: True, 1: True}, 1: {}})

    active_state = {
        "processor:camilla:output:1:mute": {"muted": True},
    }
    await driver.rehydrate_from_active_state(active_state)
    assert driver._output_muted.get(1) is True
    # The source for output 1 stays in the mixer (routing inspectable) but
    # carries mute: true so the per-output chain receives silence.
    mapping = driver._build_mixer()["output_router"]["mapping"]
    assert len(mapping[1]["sources"]) == 1
    assert mapping[1]["sources"][0]["mute"] is True

# ── Bug 4: pipeline_state — detect Inactive CamillaDSP pipeline ───────────────


class TestCamillaDSPPipelineState:
    """Unit tests for CamillaDSPDriver.pipeline_state (Bug 4)."""

    @pytest.mark.asyncio
    async def test_pipeline_state_returns_running_when_active(self) -> None:
        driver = CamillaDSPDriver()
        driver._client._ws = object()
        driver._client.call = AsyncMock(return_value="Running")
        state = await driver.pipeline_state()
        assert state == "Running"
        driver._client.call.assert_awaited_once_with("GetState")

    @pytest.mark.asyncio
    async def test_pipeline_state_returns_inactive_when_pipeline_down(self) -> None:
        driver = CamillaDSPDriver()
        driver._client._ws = object()
        driver._client.call = AsyncMock(return_value="Inactive")
        state = await driver.pipeline_state()
        assert state == "Inactive"

    @pytest.mark.asyncio
    async def test_pipeline_state_returns_unknown_when_not_connected(self) -> None:
        driver = CamillaDSPDriver()
        # _client._ws is None → not connected
        state = await driver.pipeline_state()
        assert state == "Unknown"

    @pytest.mark.asyncio
    async def test_pipeline_state_returns_unknown_on_driver_error(self) -> None:
        from calibrate.drivers.base import DriverError
        driver = CamillaDSPDriver()
        driver._client._ws = object()
        driver._client.call = AsyncMock(side_effect=DriverError("timeout"))
        state = await driver.pipeline_state()
        assert state == "Unknown"


# ── Direct Focusrite capture: physical vs logical channel disambiguation ──────


class TestCamillaDSPDirectCapture:
    """Tests for capture_channels / lfe_input_channel — direct multichannel capture
    without the ffmpeg bridge. The pre-output_router capture_mixer fans out one
    physical channel to every logical input."""

    def test_default_capture_channels_match_input_channels(self) -> None:
        """Backward compat: omitting capture_channels keeps the legacy 1:1 path."""
        driver = CamillaDSPDriver(input_channels=2)
        assert driver._capture_channels == 2
        assert driver._lfe_input_channel is None
        assert driver._build_capture_mixer() is None

    def test_capture_channels_without_lfe_channel_raises(self) -> None:
        """A multichannel capture with no fan-out target is ambiguous; reject early."""
        with pytest.raises(ValueError, match="lfe_input_channel"):
            CamillaDSPDriver(input_channels=2, capture_channels=20)

    def test_lfe_input_channel_out_of_range_raises(self) -> None:
        """lfe_input_channel must be a valid index into the capture stream."""
        with pytest.raises(ValueError, match="out of range"):
            CamillaDSPDriver(input_channels=2, capture_channels=20, lfe_input_channel=20)
        with pytest.raises(ValueError, match="out of range"):
            CamillaDSPDriver(input_channels=2, capture_channels=20, lfe_input_channel=-1)

    def test_capture_device_channel_count_uses_physical_count(self) -> None:
        """The CamillaDSP capture device declares physical channels, not logical."""
        driver = CamillaDSPDriver(
            input_channels=2,
            capture_channels=20,
            lfe_input_channel=2,
        )
        assert driver._capture_device["channels"] == 20

    def test_capture_mixer_fans_out_lfe_to_all_logical_inputs(self) -> None:
        """Each logical input reads from the configured physical LFE channel."""
        driver = CamillaDSPDriver(
            input_channels=2,
            capture_channels=20,
            lfe_input_channel=2,
        )
        mixer = driver._build_capture_mixer()
        assert mixer is not None
        assert mixer["channels"] == {"in": 20, "out": 2}
        assert len(mixer["mapping"]) == 2
        for entry in mixer["mapping"]:
            assert len(entry["sources"]) == 1
            src = entry["sources"][0]
            assert src["channel"] == 2
            assert src["mute"] is False
            assert src["inverted"] is False

    def test_pipeline_includes_capture_mixer_step_first(self) -> None:
        """When physical != logical, lfe_source is the first pipeline step."""
        driver = CamillaDSPDriver(
            input_channels=2, output_channels=4,
            capture_channels=20, lfe_input_channel=2,
        )
        pipeline = driver._build_pipeline()
        assert pipeline[0] == {"type": "Mixer", "name": "lfe_source"}
        # output_router still present, after any input PEQ.
        assert {"type": "Mixer", "name": "output_router"} in pipeline

    def test_pipeline_omits_capture_mixer_on_legacy_path(self) -> None:
        """Legacy 2:2 Loopback path emits no capture_mixer step."""
        driver = CamillaDSPDriver(input_channels=2, output_channels=4)
        pipeline = driver._build_pipeline()
        assert {"type": "Mixer", "name": "lfe_source"} not in pipeline
        # output_router is still the routing mixer.
        names = [s.get("name") for s in pipeline if s.get("type") == "Mixer"]
        assert names == ["output_router"]

    def test_full_config_contains_both_mixers_on_direct_path(self) -> None:
        """_build_config emits lfe_source alongside output_router when needed."""
        driver = CamillaDSPDriver(
            input_channels=2, output_channels=4,
            capture_channels=20, lfe_input_channel=2,
        )
        cfg = driver._build_config()
        assert "lfe_source" in cfg["mixers"]
        assert "output_router" in cfg["mixers"]
        assert cfg["devices"]["capture"]["channels"] == 20

    def test_registry_passes_through_new_keys(self) -> None:
        """_make_camilladsp wires capture_channels + lfe_input_channel from config."""
        from calibrate.config import Config, DEFAULT_CONFIG
        from calibrate.drivers.registry import load_drivers_from_graph
        cfg = Config({
            **DEFAULT_CONFIG,
            "dsp_driver": "camilladsp",
            "camilladsp": {
                **DEFAULT_CONFIG["camilladsp"],
                "capture_channels": 20,
                "lfe_input_channel": 2,
                "capture": {
                    "type": "Alsa", "device": "plughw:USB,0",
                    "channels": 20, "format": "S32LE",
                },
            },
        })
        registry = load_drivers_from_graph(cfg)
        driver = registry.default_dsp()
        assert isinstance(driver, CamillaDSPDriver)
        assert driver._capture_channels == 20
        assert driver._lfe_input_channel == 2



# ── Multi-rate processing: low-rate sub band, device at 48 kHz ────────────────


class TestCamillaDSPMultirate:
    """capture_samplerate + resampler: enable a low-rate sub-band processing
    pipeline (e.g. samplerate=8000 with the device running at 48000) so long
    FIRs can correct deep room modes without the per-chunk FFT cost of running
    65k+ taps at the full device rate."""

    def test_default_emits_no_multirate_fields(self) -> None:
        """Backward compat: omitting both keys leaves the device config minimal."""
        driver = CamillaDSPDriver()
        cfg = driver._build_config()
        assert "capture_samplerate" not in cfg["devices"]
        assert "resampler" not in cfg["devices"]

    def test_capture_samplerate_emitted_when_set(self) -> None:
        """capture_samplerate at the devices level instructs CamillaDSP to
        resample from device rate to processing rate at capture."""
        driver = CamillaDSPDriver(
            processing_rate=8000,
            capture_samplerate=48000,
        )
        cfg = driver._build_config()
        assert cfg["devices"]["samplerate"] == 8000
        assert cfg["devices"]["capture_samplerate"] == 48000

    def test_resampler_emitted_when_set(self) -> None:
        """resampler config is passed straight through to CamillaDSP."""
        driver = CamillaDSPDriver(
            processing_rate=8000,
            capture_samplerate=48000,
            resampler={"type": "AsyncSinc", "profile": "Balanced"},
        )
        cfg = driver._build_config()
        assert cfg["devices"]["resampler"] == {"type": "AsyncSinc", "profile": "Balanced"}

    def test_capture_rate_equal_to_processing_rate_drops_resampler(self) -> None:
        """When the device rate matches the processing rate, the resampler is
        a no-op — drop both fields so CamillaDSP doesn't insert a useless stage."""
        driver = CamillaDSPDriver(
            processing_rate=48000,
            capture_samplerate=48000,
            resampler={"type": "AsyncSinc", "profile": "Balanced"},
        )
        cfg = driver._build_config()
        assert "capture_samplerate" not in cfg["devices"]
        assert "resampler" not in cfg["devices"]

    def test_resampler_dict_is_copied_not_aliased(self) -> None:
        """Mutating the caller's dict after construction must not mutate the driver."""
        resampler = {"type": "AsyncSinc", "profile": "Balanced"}
        driver = CamillaDSPDriver(
            processing_rate=8000,
            capture_samplerate=48000,
            resampler=resampler,
        )
        resampler["profile"] = "Fast"
        cfg = driver._build_config()
        assert cfg["devices"]["resampler"]["profile"] == "Balanced"

    def test_registry_passes_through_multirate_keys(self) -> None:
        """_make_camilladsp wires capture_samplerate + resampler from config."""
        from calibrate.config import Config, DEFAULT_CONFIG
        from calibrate.drivers.registry import load_drivers_from_graph
        cfg = Config({
            **DEFAULT_CONFIG,
            "dsp_driver": "camilladsp",
            "camilladsp": {
                **DEFAULT_CONFIG["camilladsp"],
                "samplerate": 8000,
                "capture_samplerate": 48000,
                "resampler": {"type": "AsyncSinc", "profile": "Balanced"},
            },
        })
        registry = load_drivers_from_graph(cfg)
        driver = registry.default_dsp()
        assert isinstance(driver, CamillaDSPDriver)
        assert driver._processing_rate == 8000
        assert driver._capture_samplerate == 48000
        assert driver._resampler == {"type": "AsyncSinc", "profile": "Balanced"}




# ── LoopbackRefPlayback (task #50 skeleton) ─────────────────────────────────────


def test_loopback_ref_playback_construction() -> None:
    """LoopbackRefPlayback wraps a base strategy and stores ref params."""
    from calibrate.drivers.playback import LoopbackRefPlayback, USBPlayback

    base = USBPlayback()
    wrapper = LoopbackRefPlayback(
        base=base, ref_device="hw:Loopback,1,0",
        ref_channels=2, ref_channel_index=1,
    )
    assert wrapper.base is base
    assert wrapper.ref_device == "hw:Loopback,1,0"
    assert wrapper.ref_channels == 2
    assert wrapper.ref_channel_index == 1


def test_loopback_ref_playback_validates_channel_index() -> None:
    """ref_channel_index out of [1, ref_channels] raises."""
    from calibrate.drivers.playback import LoopbackRefPlayback, USBPlayback

    base = USBPlayback()
    with pytest.raises(ValueError, match="ref_channel_index"):
        LoopbackRefPlayback(
            base=base, ref_device="hw:dummy",
            ref_channels=2, ref_channel_index=3,  # out of range
        )
    with pytest.raises(ValueError, match="ref_channel_index"):
        LoopbackRefPlayback(
            base=base, ref_device="hw:dummy",
            ref_channels=2, ref_channel_index=0,  # 0-based bug guard
        )


def test_loopback_ref_playback_validates_channel_count() -> None:
    """ref_channels < 1 raises."""
    from calibrate.drivers.playback import LoopbackRefPlayback, USBPlayback

    base = USBPlayback()
    with pytest.raises(ValueError, match="ref_channels"):
        LoopbackRefPlayback(
            base=base, ref_device="hw:dummy",
            ref_channels=0, ref_channel_index=1,
        )


def test_playback_for_route_no_ref_returns_base() -> None:
    """Without loopback_ref_device, factory returns the base strategy unchanged."""
    from calibrate.drivers.playback import (
        USBPlayback, HDMIPwCatPlayback, LoopbackRefPlayback, playback_for_route,
    )

    p_usb = playback_for_route("usb")
    assert isinstance(p_usb, USBPlayback)
    assert not isinstance(p_usb, LoopbackRefPlayback)

    p_hdmi = playback_for_route(
        "hdmi", hdmi_pipewire_node="alsa_output.platform-107c701400.hdmi.hdmi-stereo", hdmi_channels=6,
    )
    assert isinstance(p_hdmi, HDMIPwCatPlayback)
    assert not isinstance(p_hdmi, LoopbackRefPlayback)


def test_playback_for_route_with_ref_wraps_in_loopback() -> None:
    """With loopback_ref_device set, factory wraps base in LoopbackRefPlayback."""
    from calibrate.drivers.playback import (
        LoopbackRefPlayback, USBPlayback, playback_for_route,
    )

    p = playback_for_route(
        "usb",
        loopback_ref_device="hw:Loopback,1,0",
        loopback_ref_channels=2, loopback_ref_channel_index=1,
    )
    assert isinstance(p, LoopbackRefPlayback)
    assert isinstance(p.base, USBPlayback)
    assert p.ref_device == "hw:Loopback,1,0"


def test_loopback_ref_playback_returns_three_tuple() -> None:
    """play_and_record returns (sweep, mic, ref) triple even when ref is stubbed."""
    from unittest.mock import MagicMock
    from calibrate.drivers.playback import LoopbackRefPlayback
    import numpy as np

    # Mock base strategy that returns a known 2-tuple.
    fake_sweep = np.array([0.1, 0.2, 0.3])
    fake_mic = np.array([0.0, 0.5, 0.0])
    base = MagicMock()
    base.play_and_record.return_value = (fake_sweep, fake_mic)

    sweep_obj = MagicMock()
    sweep_obj.timeSignal = MagicMock()

    wrapper = LoopbackRefPlayback(
        base=base, ref_device="hw:Loopback,1,0",
    )
    result = wrapper.play_and_record(sweep_obj, 48000, 1, 1)
    assert len(result) == 3, f"expected 3-tuple, got {len(result)}"
    sweep_1d, mic_1d, ref_1d = result
    np.testing.assert_array_equal(sweep_1d, fake_sweep)
    np.testing.assert_array_equal(mic_1d, fake_mic)
    # Ref is stubbed as zeros until full capture wiring lands.
    assert len(ref_1d) == len(fake_mic)
    assert (ref_1d == 0).all(), "stub should return all-zero ref"
