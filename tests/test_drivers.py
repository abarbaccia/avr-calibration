"""Tests for calibrate.drivers — unit tests for DenonDriver, MinidspDriver, registry.

All network calls are mocked:
  - DenonDriver: denonavr module patched in sys.modules
  - MinidspDriver: CLI subprocess mocked via _run_minidsp_cli and _get_status_via_cli
  - Registry: Config mock
"""

from __future__ import annotations

import asyncio
import sys
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
async def test_minidsp_read_eq_starts_empty() -> None:
    driver = MinidspDriver(host="localhost", port=5380)
    assert await driver.read_eq(0) == []


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
    assert await driver.read_eq(0) == []


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
    state = await driver.read_eq(0)
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


def _denon_sweep_config(pure_direct: bool = True, settle_ms: int = 5000):
    """Build a minimal Config mock for DenonSweepContext.from_config()."""
    cfg = MagicMock()
    cfg.measurement = {
        "playback_route": "hdmi",
        "denon_sweep_input": "Videocore",
        "denon_sweep_volume": -10.0,
        "denon_settle_ms": settle_ms,
        "denon_pure_direct": pure_direct,
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


def test_denon_sweep_from_config_pure_direct_default_true():
    """from_config() defaults to pure_direct=True."""
    cfg = _denon_sweep_config()
    del cfg.measurement["denon_pure_direct"]  # not in config → default
    ctx = DenonSweepContext.from_config(cfg)
    assert ctx._pure_direct is True


def test_denon_sweep_from_config_pure_direct_false():
    """from_config() respects denon_pure_direct=False."""
    cfg = _denon_sweep_config(pure_direct=False)
    ctx = DenonSweepContext.from_config(cfg)
    assert ctx._pure_direct is False


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


def _make_sweep_receiver(volume=-28.0, sound_mode="DTS SURROUND"):
    """Build a denonavr mock receiver with all async methods needed by DenonSweepContext."""
    mock_mod, mock_receiver = _make_denonavr_mock(volume=volume)
    mock_receiver.input_func = "SHIELD"
    mock_receiver.async_set_input_func = AsyncMock()
    mock_receiver.async_set_volume = AsyncMock()
    mock_receiver.soundmode = MagicMock()
    mock_receiver.soundmode.sound_mode = sound_mode
    mock_receiver.soundmode.async_set_sound_mode = AsyncMock()
    return mock_mod, mock_receiver


@pytest.mark.asyncio
async def test_denon_sweep_enter_sets_pure_direct():
    """With pure_direct=True, __aenter__ calls async_set_sound_mode(PURE DIRECT)."""
    mock_mod, mock_receiver = _make_sweep_receiver()

    with patch.dict(sys.modules, {"denonavr": mock_mod}):
        with patch("calibrate.drivers.denon.asyncio.sleep", new_callable=AsyncMock):
            ctx = DenonSweepContext(
                host="192.168.1.209", sweep_input="Videocore",
                sweep_volume=-10.0, settle_ms=100, pure_direct=True,
            )
            await ctx.__aenter__()

    mock_receiver.soundmode.async_set_sound_mode.assert_called_once_with("PURE DIRECT")


@pytest.mark.asyncio
async def test_denon_sweep_enter_skips_pure_direct():
    """With pure_direct=False, __aenter__ does NOT call async_set_sound_mode."""
    mock_mod, mock_receiver = _make_sweep_receiver()

    with patch.dict(sys.modules, {"denonavr": mock_mod}):
        with patch("calibrate.drivers.denon.asyncio.sleep", new_callable=AsyncMock):
            ctx = DenonSweepContext(
                host="192.168.1.209", sweep_input="Videocore",
                sweep_volume=-10.0, settle_ms=100, pure_direct=False,
            )
            await ctx.__aenter__()

    mock_receiver.soundmode.async_set_sound_mode.assert_not_called()


@pytest.mark.asyncio
async def test_denon_sweep_exit_restores_sound_mode_when_pure_direct():
    """With pure_direct=True, __aexit__ restores the saved sound mode."""
    mock_mod, mock_receiver = _make_sweep_receiver()

    with patch.dict(sys.modules, {"denonavr": mock_mod}):
        with patch("calibrate.drivers.denon.asyncio.sleep", new_callable=AsyncMock):
            ctx = DenonSweepContext(
                host="192.168.1.209", sweep_input="Videocore",
                sweep_volume=-10.0, settle_ms=100, pure_direct=True,
            )
            await ctx.__aenter__()
            mock_receiver.soundmode.async_set_sound_mode.reset_mock()

            await ctx.__aexit__(None, None, None)

    mock_receiver.soundmode.async_set_sound_mode.assert_called_once_with("DTS SURROUND")


@pytest.mark.asyncio
async def test_denon_sweep_exit_skips_sound_mode_restore_when_not_pure_direct():
    """With pure_direct=False, __aexit__ does NOT restore sound mode."""
    mock_mod, mock_receiver = _make_sweep_receiver()

    with patch.dict(sys.modules, {"denonavr": mock_mod}):
        with patch("calibrate.drivers.denon.asyncio.sleep", new_callable=AsyncMock):
            ctx = DenonSweepContext(
                host="192.168.1.209", sweep_input="Videocore",
                sweep_volume=-10.0, settle_ms=100, pure_direct=False,
            )
            await ctx.__aenter__()
            mock_receiver.soundmode.async_set_sound_mode.reset_mock()

            await ctx.__aexit__(None, None, None)

    mock_receiver.soundmode.async_set_sound_mode.assert_not_called()


# ── Registry ───────────────────────────────────────────────────────────────────

def _mock_config(avr_driver: str = "denon", dsp_driver: str = "minidsp",
                  processing_rate: int = 96_000):
    cfg = MagicMock()
    cfg.avr_driver_name = avr_driver
    cfg.dsp_driver_name = dsp_driver
    cfg.denon = {"host": "192.168.1.100"}
    cfg.minidsp = {"host": "localhost", "port": 5380}
    cfg.minidsp_host_port = ("localhost", 5380)
    cfg.eq_capabilities = {"processing_rate": processing_rate}
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
    cfg = _mock_config(dsp_driver="camilla")
    with pytest.raises(ValueError, match="Unknown DSP driver"):
        load_dsp_driver(cfg)


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
