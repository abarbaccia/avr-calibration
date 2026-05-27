"""Tests for hardware pre-flight checks.

Coverage diagram:
  PreflightChecker
  ├── check_hidraw()
  │   ├── [TESTED] /dev/hidraw0 exists → passes with path in detail
  │   └── [TESTED] /dev/hidraw0 missing → fails with OTG adapter hint
  ├── check_mic()
  │   ├── [TESTED] UMIK found by name match → passes with device detail
  │   ├── [TESTED] Name match is case-insensitive
  │   ├── [TESTED] No UMIK but other inputs exist → fails, shows available
  │   ├── [TESTED] No input devices at all → fails, generic message
  │   └── [TESTED] sounddevice raises → fails gracefully with error text
  │   └── [TESTED] Detail includes device index and sample rate
  ├── check_minidsp()
  │   ├── [TESTED] Device found → passes with product name + host:port
  │   ├── [TESTED] Device found — serial not shown (CLI doesn't expose it)
  │   ├── [TESTED] CLI failure (daemon not running / no device) → fails, start-daemon hint
  │   ├── [TESTED] TimeoutError → fails, wait-and-retry hint
  │   ├── [TESTED] Unexpected exception → fails gracefully
  │   └── [TESTED] Custom host and port in detail
  ├── check_minidsp_combined()
  │   ├── [TESTED] Both pass → single pass result with combined detail
  │   ├── [TESTED] Only hidraw fails → propagates hidraw error
  │   ├── [TESTED] Only daemon fails → propagates daemon error
  │   └── [TESTED] Both fail → combined failure, USB error takes precedence
  ├── check_denon()
  │   ├── [TESTED] AVR online → passes with model name
  │   ├── [TESTED] model_name is None → falls back to "Denon AVR"
  │   ├── [TESTED] host not configured → auto-discovers via SSDP
  │   ├── [TESTED] host not configured, nothing found → fails with scan message
  │   ├── [TESTED] Connection fails → fails with host in detail
  │   └── [TESTED] Timeout → fails
  ├── check_denon_and_playback()
  │   ├── [TESTED] Denon passes, HDMI route → adds "HDMI playback ready"
  │   ├── [TESTED] Denon passes, USB route, device found → combined detail
  │   ├── [TESTED] Denon passes, USB route, device missing → fails with playback error
  │   └── [TESTED] Denon fails → propagates failure without running playback check
  └── run_all()
      ├── [TESTED] All pass → 4 passed results (Config, miniDSP, Denon AVR, Signal Path)
      ├── [TESTED] Unhandled exception → captured as failed result
      ├── [TESTED] Results named correctly even when exceptions occur
      └── [TESTED] Partial failure (1 fail, 3 pass)
"""

import asyncio
import sys
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from calibrate.adapters.minidsp import MinidspApiError
from calibrate.preflight import PreflightChecker, CheckResult, HIDRAW_DEVICE
from tests.conftest import make_input_device, make_output_device

_STATUS_CLI = "calibrate.adapters.minidsp._get_status_via_cli"


# ── HID device node checks ───────────────────────────────────────────────────

class TestHidrawCheck:
    async def test_hidraw_present(self, config):
        with patch("calibrate.preflight.os.path.exists", return_value=True):
            result = await PreflightChecker(config).check_hidraw()
        assert result.passed
        assert HIDRAW_DEVICE in result.detail
        assert result.error is None

    async def test_hidraw_missing_gives_otg_hint(self, config):
        with patch("calibrate.preflight.os.path.exists", return_value=False):
            result = await PreflightChecker(config).check_hidraw()
        assert not result.passed
        assert HIDRAW_DEVICE in result.detail
        assert "OTG" in result.error
        assert "micro-USB" in result.error


# ── Microphone checks ────────────────────────────────────────────────────────
# sounddevice is mocked via sys.modules["sounddevice"] (see conftest.py).
# Each test configures query_devices() return value on the session-scoped mock.

class TestMicCheck:
    async def test_umik_found(self, config):
        sys.modules["sounddevice"].query_devices.return_value = [make_input_device("miniDSP UMIK-1")]
        result = await PreflightChecker(config).check_mic()
        assert result.passed
        assert "UMIK-1" in result.detail
        assert result.error is None

    async def test_umik_found_case_insensitive(self, config):
        sys.modules["sounddevice"].query_devices.return_value = [make_input_device("minidsp umik-2")]
        result = await PreflightChecker(config).check_mic()
        assert result.passed

    async def test_no_umik_but_other_inputs_present(self, config):
        sys.modules["sounddevice"].query_devices.return_value = [
            make_input_device("Built-in Microphone"),
            make_output_device("Speakers"),
        ]
        result = await PreflightChecker(config).check_mic()
        assert not result.passed
        assert "Built-in Microphone" in result.detail
        assert result.error is not None

    async def test_no_input_devices_at_all(self, config):
        sys.modules["sounddevice"].query_devices.return_value = [make_output_device("Speakers")]
        result = await PreflightChecker(config).check_mic()
        assert not result.passed
        assert "No audio input" in result.detail

    async def test_sounddevice_raises(self, config):
        sys.modules["sounddevice"].query_devices.side_effect = RuntimeError("portaudio error")
        result = await PreflightChecker(config).check_mic()
        assert not result.passed
        assert "portaudio error" in result.error
        # Reset side_effect for other tests
        sys.modules["sounddevice"].query_devices.side_effect = None

    async def test_detail_includes_device_index_and_sample_rate(self, config):
        sys.modules["sounddevice"].query_devices.return_value = [
            make_input_device("miniDSP UMIK-1", sample_rate=48000.0)
        ]
        result = await PreflightChecker(config).check_mic()
        assert result.passed
        assert "48000" in result.detail
        assert "device 0" in result.detail


# ── miniDSP checks ───────────────────────────────────────────────────────────

_GOOD_STATUS = {
    "master": {"preset": 0, "source": "Analog", "volume": -30.0, "mute": False},
    "input_levels": [],
    "output_levels": [],
}


class TestMinidspCheck:
    async def test_device_found(self, config):
        """CLI status succeeds → passes with product name and host:port."""
        with patch(_STATUS_CLI, new_callable=AsyncMock, return_value=_GOOD_STATUS):
            result = await PreflightChecker(config).check_minidsp()
        assert result.passed
        assert "miniDSP 2x4 HD" in result.detail
        assert "localhost:5380" in result.detail

    async def test_device_found_no_serial_in_detail(self, config):
        """Serial is empty in CLI response — detail should not contain 'serial'."""
        with patch(_STATUS_CLI, new_callable=AsyncMock, return_value=_GOOD_STATUS):
            result = await PreflightChecker(config).check_minidsp()
        assert result.passed
        assert "serial" not in result.detail

    async def test_cli_failure_daemon_not_running(self, config):
        """CLI failure (any) → fails with start-daemon hint."""
        with patch(_STATUS_CLI, new_callable=AsyncMock,
                   side_effect=MinidspApiError(1, "minidsp status: connection refused")):
            result = await PreflightChecker(config).check_minidsp()
        assert not result.passed
        assert "minidspd" in result.error.lower()

    async def test_timeout(self, config):
        """asyncio.TimeoutError → fails with wait-and-retry hint."""
        with patch(_STATUS_CLI, new_callable=AsyncMock, side_effect=asyncio.TimeoutError()):
            result = await PreflightChecker(config).check_minidsp()
        assert not result.passed
        assert "wait" in result.error.lower()

    async def test_unexpected_exception(self, config):
        """Unexpected exception → fails with error text."""
        with patch(_STATUS_CLI, new_callable=AsyncMock, side_effect=RuntimeError("crash")):
            result = await PreflightChecker(config).check_minidsp()
        assert not result.passed
        assert result.error is not None

    async def test_custom_host_and_port(self):
        """Configured host:port appears in the result detail."""
        from calibrate.config import Config
        cfg = Config({
            "denon": {"host": None},
            "minidsp": {"host": "10.0.0.5", "port": 9999},
            "mic": {"name": "UMIK"},
        })
        with patch(_STATUS_CLI, new_callable=AsyncMock, return_value=_GOOD_STATUS):
            result = await PreflightChecker(cfg).check_minidsp()
        assert result.passed
        assert "10.0.0.5:9999" in result.detail


# ── Denon AVR checks ─────────────────────────────────────────────────────────

class TestDenonCheck:
    async def test_avr_online(self, config):
        state = {"connected": True, "host": "192.168.1.100", "model": "Denon AVR-X3800H", "power": "ON"}
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver, \
             patch(
                 "calibrate.drivers.denon.audyssey_tcp.probe_audyssey_service",
                 return_value={"EQType": "MultEQXT32"},
             ):
            MockDriver.return_value.get_state = AsyncMock(return_value=state)
            result = await PreflightChecker(config).check_denon()
        assert result.passed
        assert "X3800H" in result.detail
        assert "192.168.1.100" in result.detail

    async def test_avr_model_name_none_falls_back(self, config):
        state = {"connected": True, "host": "192.168.1.100", "model": "Denon AVR", "power": "ON"}
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver, \
             patch(
                 "calibrate.drivers.denon.audyssey_tcp.probe_audyssey_service",
                 return_value={"EQType": "MultEQXT32"},
             ):
            MockDriver.return_value.get_state = AsyncMock(return_value=state)
            result = await PreflightChecker(config).check_denon()
        assert result.passed
        assert "Denon AVR" in result.detail

    async def test_avr_in_standby_fails_loudly(self, config):
        """Power=STANDBY should fail with a clear message, not pass silently."""
        state = {"connected": True, "host": "192.168.1.100", "model": "Denon AVR-X3800H", "power": "STANDBY"}
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver, \
             patch(
                 "calibrate.drivers.denon.audyssey_tcp.probe_audyssey_service",
                 return_value={"EQType": "MultEQXT32"},
             ):
            MockDriver.return_value.get_state = AsyncMock(return_value=state)
            result = await PreflightChecker(config).check_denon()
        assert not result.passed
        assert "standby" in result.error.lower()
        assert "STANDBY" in result.detail or "standby" in result.detail.lower()

    async def test_avr_power_none_fails(self, config):
        """power=None (denonavr's default before update) should also fail."""
        state = {"connected": True, "host": "192.168.1.100", "model": "Denon AVR-X3800H", "power": None}
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver, \
             patch(
                 "calibrate.drivers.denon.audyssey_tcp.probe_audyssey_service",
                 return_value={"EQType": "MultEQXT32"},
             ):
            MockDriver.return_value.get_state = AsyncMock(return_value=state)
            result = await PreflightChecker(config).check_denon()
        assert not result.passed

    async def test_host_not_configured_discovery_finds_nothing(self, config):
        config._data["denon"]["host"] = None
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver:
            MockDriver.return_value.discover = AsyncMock(return_value=[])
            result = await PreflightChecker(config).check_denon()
        assert not result.passed
        assert "discovered" in result.detail.lower() or "found" in result.detail.lower()
        assert "config.yaml" in result.error or "network" in result.error.lower()

    async def test_host_auto_discovered_via_ssdp(self, config):
        config._data["denon"]["host"] = None
        state = {"connected": True, "host": "192.168.1.42", "model": "Denon AVR-X3800H", "power": "ON"}
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver, \
             patch(
                 "calibrate.drivers.denon.audyssey_tcp.probe_audyssey_service",
                 return_value={"EQType": "MultEQXT32"},
             ):
            MockDriver.return_value.discover = AsyncMock(return_value=["192.168.1.42"])
            MockDriver.return_value.get_state = AsyncMock(return_value=state)
            result = await PreflightChecker(config).check_denon()
        assert result.passed
        assert "192.168.1.42" in result.detail
        assert "auto-discovered" in result.detail

    async def test_avr_audyssey_tcp_wedged_fails_loudly(self, config):
        """AVR HTTP responding but Audyssey TCP unresponsive → fail with hard-cycle hint."""
        state = {"connected": True, "host": "192.168.1.100", "model": "Denon AVR-X3800H", "power": "ON"}
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver, \
             patch(
                 "calibrate.drivers.denon.audyssey_tcp.probe_audyssey_service",
                 return_value=None,
             ):
            MockDriver.return_value.get_state = AsyncMock(return_value=state)
            result = await PreflightChecker(config).check_denon()
        assert not result.passed
        assert "wedged" in result.error.lower() or "unresponsive" in result.detail.lower()
        assert "power cord" in result.error.lower() or "hard" in result.error.lower()

    async def test_ssdp_discovery_empty(self, config):
        """SSDP discovery returning nothing → fail."""
        config._data["denon"]["host"] = None
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver:
            MockDriver.return_value.discover = AsyncMock(return_value=[])
            result = await PreflightChecker(config).check_denon()
        assert not result.passed

    async def test_avr_unreachable(self, config):
        from calibrate.drivers.base import DriverError
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver:
            MockDriver.return_value.get_state = AsyncMock(side_effect=DriverError("refused"))
            result = await PreflightChecker(config).check_denon()
        assert not result.passed
        assert "192.168.1.100" in result.detail

    async def test_avr_timeout(self, config):
        from calibrate.drivers.base import DriverError
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver:
            MockDriver.return_value.get_state = AsyncMock(side_effect=DriverError("timed out"))
            result = await PreflightChecker(config).check_denon()
        assert not result.passed


# ── check_loopback_reference() ────────────────────────────────────────────────

class TestCheckLoopbackReference:
    async def test_fails_when_no_loopback_configured(self, config):
        config._data.setdefault("measurement", {})["loopback_ref_pipewire_node"] = None
        config._data.setdefault("measurement", {})["loopback_ref_device"] = None
        result = await PreflightChecker(config).check_loopback_reference()
        assert not result.passed
        assert "LOOPBACK REFERENCE NOT CONFIGURED" in result.error

    async def test_passes_when_pipewire_node_configured(self, config):
        config._data.setdefault("measurement", {})["loopback_ref_pipewire_node"] = "scarlett_input"
        config._data.setdefault("measurement", {})["loopback_ref_device"] = None
        config._data.setdefault("measurement", {})["loopback_ref_channel_index"] = 4
        config._data.setdefault("measurement", {})["loopback_ref_channels"] = 2
        result = await PreflightChecker(config).check_loopback_reference()
        assert result.passed
        assert "scarlett_input" in result.detail
        assert "4..5" in result.detail

    async def test_passes_when_alsa_device_configured(self, config):
        config._data.setdefault("measurement", {})["loopback_ref_pipewire_node"] = None
        config._data.setdefault("measurement", {})["loopback_ref_device"] = "hw:1,0"
        result = await PreflightChecker(config).check_loopback_reference()
        assert result.passed
        assert "hw:1,0" in result.detail


# ── run_all() ────────────────────────────────────────────────────────────────

class TestRunAll:
    async def test_all_pass(self, config):
        checker = PreflightChecker(config)
        with (
            patch.object(checker, "check_config", return_value=CheckResult("Config", True, "Required fields present")),
            patch.object(checker, "check_mic", return_value=CheckResult("Microphone", True, "UMIK-1 (device 0, 48000Hz)")),
            patch.object(checker, "check_minidsp_combined", return_value=CheckResult("miniDSP", True, "2x4HD; /dev/hidraw0 present")),
            patch.object(checker, "check_denon_and_playback", return_value=CheckResult("Denon AVR", True, "X3800H; USB: miniDSP")),
            patch.object(checker, "check_signal_path_sync", return_value=CheckResult("Signal Path", True, "not configured (skipped)")),
            patch.object(checker, "check_output_routing_safety", return_value=CheckResult("Output routing", True, "not applicable")),
            patch.object(checker, "check_audio_stack_clean", return_value=CheckResult("Audio stack", True, "no PipeWire holders")),
            patch.object(checker, "check_dsp_persisted_state", return_value=CheckResult("DSP persisted state", True, "all defaults")),
            patch.object(checker, "check_loopback_reference", return_value=CheckResult("Loopback reference", True, "enabled")),
        ):
            results = await checker.run_all()
        assert all(r.passed for r in results)
        assert len(results) == 9

    async def test_unhandled_exception_becomes_failed_result(self, config):
        checker = PreflightChecker(config)
        with (
            patch.object(checker, "check_config", return_value=CheckResult("Config", True, "Required fields present")),
            patch.object(checker, "check_mic", return_value=CheckResult("Microphone", True, "UMIK-1")),
            patch.object(checker, "check_minidsp_combined", side_effect=RuntimeError("boom")),
            patch.object(checker, "check_denon_and_playback", return_value=CheckResult("Denon AVR", True, "X3800H")),
            patch.object(checker, "check_signal_path_sync", return_value=CheckResult("Signal Path", True, "not configured (skipped)")),
            patch.object(checker, "check_output_routing_safety", return_value=CheckResult("Output routing", True, "not applicable")),
        ):
            results = await checker.run_all()
        minidsp = next(r for r in results if r.name == "miniDSP 2x4 HD")
        assert not minidsp.passed
        assert "boom" in minidsp.error

    async def test_result_names_match_expected(self, config):
        checker = PreflightChecker(config)
        with (
            patch.object(checker, "check_config", side_effect=RuntimeError("err")),
            patch.object(checker, "check_mic", side_effect=RuntimeError("err")),
            patch.object(checker, "check_minidsp_combined", side_effect=RuntimeError("err")),
            patch.object(checker, "check_denon_and_playback", side_effect=RuntimeError("err")),
            patch.object(checker, "check_signal_path_sync", side_effect=RuntimeError("err")),
            patch.object(checker, "check_output_routing_safety", side_effect=RuntimeError("err")),
            patch.object(checker, "check_audio_stack_clean", side_effect=RuntimeError("err")),
            patch.object(checker, "check_dsp_persisted_state", side_effect=RuntimeError("err")),
            patch.object(checker, "check_loopback_reference", side_effect=RuntimeError("err")),
        ):
            results = await checker.run_all()
        # DSP label now comes from the configured driver — "miniDSP 2x4 HD" for
        # dsp_driver: minidsp (the test fixture default).
        assert [r.name for r in results] == [
            "Config", "Microphone", "miniDSP 2x4 HD", "Denon AVR", "Signal Path",
            "Output routing", "Audio stack", "DSP persisted state",
            "Loopback reference",
        ]

    async def test_result_names_camilladsp_label(self, config):
        """dsp_driver: camilladsp → label reads 'CamillaDSP' in the preflight output."""
        config._data["dsp_driver"] = "camilladsp"
        checker = PreflightChecker(config)
        with (
            patch.object(checker, "check_config", side_effect=RuntimeError("err")),
            patch.object(checker, "check_mic", side_effect=RuntimeError("err")),
            patch.object(checker, "check_minidsp_combined", side_effect=RuntimeError("err")),
            patch.object(checker, "check_denon_and_playback", side_effect=RuntimeError("err")),
            patch.object(checker, "check_signal_path_sync", side_effect=RuntimeError("err")),
        ):
            results = await checker.run_all()
        assert results[2].name == "CamillaDSP"

    async def test_partial_failure(self, config):
        checker = PreflightChecker(config)
        with (
            patch.object(checker, "check_config", return_value=CheckResult("Config", True, "Required fields present")),
            patch.object(checker, "check_mic", return_value=CheckResult("Microphone", True, "UMIK-1")),
            patch.object(checker, "check_minidsp_combined", return_value=CheckResult("miniDSP", False, "", "start minidspd")),
            patch.object(checker, "check_denon_and_playback", return_value=CheckResult("Denon AVR", True, "X3800H")),
            patch.object(checker, "check_signal_path_sync", return_value=CheckResult("Signal Path", True, "not configured (skipped)")),
        ):
            results = await checker.run_all()
        assert results[0].passed       # Config
        assert results[1].passed       # Microphone
        assert not results[2].passed   # miniDSP
        assert results[3].passed       # Denon AVR
        assert results[4].passed       # Signal Path


# ── Playback route checks ─────────────────────────────────────────────────────

class TestPlaybackRouteCheck:
    async def test_usb_device_found(self, config):
        config._data.setdefault("measurement", {})["playback_route"] = "usb"
        config._data["measurement"]["playback_device"] = "miniDSP"
        from tests.conftest import make_output_device
        sys.modules["sounddevice"].query_devices.return_value = [
            make_output_device("miniDSP USB"),
        ]
        result = await PreflightChecker(config).check_playback_route()
        assert result.passed
        assert "USB" in result.detail

    async def test_usb_device_not_found(self, config):
        config._data.setdefault("measurement", {})["playback_route"] = "usb"
        config._data["measurement"]["playback_device"] = "miniDSP"
        from tests.conftest import make_input_device
        sys.modules["sounddevice"].query_devices.return_value = [
            make_input_device("Built-in Mic"),
        ]
        result = await PreflightChecker(config).check_playback_route()
        assert not result.passed
        assert "miniDSP" in result.detail

    async def test_hdmi_route_denon_reachable(self, config):
        config._data.setdefault("measurement", {})["playback_route"] = "hdmi"
        config._data["denon"]["host"] = "192.168.1.100"
        state = {"connected": True, "host": "192.168.1.100", "model": "Denon AVR-X3800H", "power": "ON"}
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver, \
             patch(
                 "calibrate.drivers.denon.audyssey_tcp.probe_audyssey_service",
                 return_value={"EQType": "MultEQXT32"},
             ):
            MockDriver.return_value.get_state = AsyncMock(return_value=state)
            result = await PreflightChecker(config).check_playback_route()
        assert result.passed
        assert "HDMI" in result.detail
        assert "192.168.1.100" in result.detail

    async def test_hdmi_route_no_denon_host(self, config):
        config._data.setdefault("measurement", {})["playback_route"] = "hdmi"
        config._data["denon"]["host"] = None
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver:
            MockDriver.return_value.discover = AsyncMock(return_value=[])
            result = await PreflightChecker(config).check_playback_route()
        assert not result.passed
        assert "denon.host" in result.error or "network" in result.error.lower()

    async def test_hdmi_route_denon_unreachable(self, config):
        config._data.setdefault("measurement", {})["playback_route"] = "hdmi"
        config._data["denon"]["host"] = "192.168.1.100"
        from calibrate.drivers.base import DriverError
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver:
            MockDriver.return_value.get_state = AsyncMock(side_effect=DriverError("refused"))
            result = await PreflightChecker(config).check_playback_route()
        assert not result.passed

    async def test_usb_sounddevice_raises_captured(self, config):
        config._data.setdefault("measurement", {})["playback_route"] = "usb"
        sys.modules["sounddevice"].query_devices.side_effect = RuntimeError("portaudio error")
        result = await PreflightChecker(config).check_playback_route()
        assert not result.passed
        assert "portaudio error" in result.error
        sys.modules["sounddevice"].query_devices.side_effect = None


# ── Signal path sync check ────────────────────────────────────────────────────

class TestSignalPathSync:
    async def test_skipped_when_not_configured(self, config):
        """No signal_path in config → skipped (passes)."""
        result = await PreflightChecker(config).check_signal_path_sync()
        assert result.passed
        assert "skipped" in result.detail

    async def test_skipped_when_no_source_or_preset(self, config):
        config._data["minidsp"]["signal_path"] = {}
        result = await PreflightChecker(config).check_signal_path_sync()
        assert result.passed
        assert "skipped" in result.detail

    async def test_passes_when_device_matches_config(self, config):
        config._data["minidsp"]["signal_path"] = {"source": "Analog", "preset": 0}
        from unittest.mock import AsyncMock, patch
        status = {"master": {"preset": 0, "source": "Analog", "volume": -30.0, "mute": False}}
        with patch("calibrate.adapters.minidsp._get_status_via_cli", new_callable=AsyncMock, return_value=status):
            result = await PreflightChecker(config).check_signal_path_sync()
        assert result.passed
        assert "source=Analog" in result.detail
        assert "preset=0" in result.detail

    async def test_fails_on_source_mismatch(self, config):
        config._data["minidsp"]["signal_path"] = {"source": "Toslink"}
        from unittest.mock import AsyncMock, patch
        status = {"master": {"preset": 0, "source": "Analog", "volume": -30.0, "mute": False}}
        with patch("calibrate.adapters.minidsp._get_status_via_cli", new_callable=AsyncMock, return_value=status):
            result = await PreflightChecker(config).check_signal_path_sync()
        assert not result.passed
        assert "source" in result.detail
        assert "signal-path apply" in result.error

    async def test_fails_on_preset_mismatch(self, config):
        config._data["minidsp"]["signal_path"] = {"preset": 2}
        from unittest.mock import AsyncMock, patch
        status = {"master": {"preset": 0, "source": "Analog", "volume": -30.0, "mute": False}}
        with patch("calibrate.adapters.minidsp._get_status_via_cli", new_callable=AsyncMock, return_value=status):
            result = await PreflightChecker(config).check_signal_path_sync()
        assert not result.passed
        assert "preset" in result.detail

    async def test_api_error_becomes_failed_result(self, config):
        config._data["minidsp"]["signal_path"] = {"source": "Analog"}
        from unittest.mock import AsyncMock, patch
        from calibrate.adapters.minidsp import MinidspApiError
        with patch("calibrate.adapters.minidsp._get_status_via_cli",
                   new_callable=AsyncMock,
                   side_effect=MinidspApiError(503, "/devices/0")):
            result = await PreflightChecker(config).check_signal_path_sync()
        assert not result.passed
        assert result.error


# ── Combined miniDSP check ────────────────────────────────────────────────────

class TestMinidspCombined:
    async def test_daemon_passes_is_sufficient(self, config):
        # Normal operating case: minidspd has claimed the device via libusb, so
        # /dev/hidraw0 does not exist. Daemon passing is the authoritative check.
        checker = PreflightChecker(config)
        with patch.object(checker, "check_minidsp", return_value=CheckResult("miniDSP", True, "2x4HD at localhost:5380 (serial 965535)")):
            result = await checker.check_minidsp_combined()
        assert result.passed
        assert result.name == "miniDSP"
        assert "2x4HD" in result.detail

    async def test_daemon_passes_hidraw_not_called(self, config):
        # check_hidraw should not be called when the daemon passes.
        checker = PreflightChecker(config)
        with (
            patch.object(checker, "check_minidsp", return_value=CheckResult("miniDSP", True, "2x4HD at localhost:5380")),
            patch.object(checker, "check_hidraw", side_effect=AssertionError("check_hidraw should not be called")),
        ):
            result = await checker.check_minidsp_combined()
        assert result.passed

    async def test_daemon_fails_hidraw_passes_shows_daemon_error(self, config):
        # Daemon down but USB device physically present — surface the daemon error.
        checker = PreflightChecker(config)
        with (
            patch.object(checker, "check_hidraw", return_value=CheckResult("miniDSP USB", True, "/dev/hidraw0 present")),
            patch.object(checker, "check_minidsp", return_value=CheckResult("miniDSP", False, "Cannot reach minidspd", "Start the daemon")),
        ):
            result = await checker.check_minidsp_combined()
        assert not result.passed
        assert result.name == "miniDSP"
        assert "daemon" in result.error.lower() or "start" in result.error.lower()
        assert "USB device found" in result.detail

    async def test_both_fail_usb_error_takes_precedence(self, config):
        checker = PreflightChecker(config)
        with (
            patch.object(checker, "check_hidraw", return_value=CheckResult("miniDSP USB", False, "/dev/hidraw0 not found", "OTG adapter required")),
            patch.object(checker, "check_minidsp", return_value=CheckResult("miniDSP", False, "Cannot reach minidspd", "Start the daemon")),
        ):
            result = await checker.check_minidsp_combined()
        assert not result.passed
        assert result.name == "miniDSP"
        assert "OTG" in result.error  # USB issue is more fundamental


# ── Combined Denon + Playback check ──────────────────────────────────────────

class TestDenonAndPlayback:
    async def test_hdmi_route_denon_passes_adds_playback_detail(self, config):
        config._data.setdefault("measurement", {})["playback_route"] = "hdmi"
        checker = PreflightChecker(config)
        with patch.object(checker, "check_denon", return_value=CheckResult("Denon AVR", True, "X3800H online at 192.168.1.100")):
            result = await checker.check_denon_and_playback()
        assert result.passed
        assert result.name == "Denon AVR"
        assert "HDMI playback ready" in result.detail
        assert "X3800H" in result.detail

    async def test_usb_route_both_pass_combined_detail(self, config):
        config._data.setdefault("measurement", {})["playback_route"] = "usb"
        checker = PreflightChecker(config)
        with (
            patch.object(checker, "check_denon", return_value=CheckResult("Denon AVR", True, "X3800H online at 192.168.1.100")),
            patch.object(checker, "check_playback_route", return_value=CheckResult("Playback Route", True, "USB: miniDSP (device 1)")),
        ):
            result = await checker.check_denon_and_playback()
        assert result.passed
        assert result.name == "Denon AVR"
        assert "X3800H" in result.detail
        assert "USB" in result.detail

    async def test_usb_route_playback_fails(self, config):
        config._data.setdefault("measurement", {})["playback_route"] = "usb"
        checker = PreflightChecker(config)
        with (
            patch.object(checker, "check_denon", return_value=CheckResult("Denon AVR", True, "X3800H online")),
            patch.object(checker, "check_playback_route", return_value=CheckResult("Playback Route", False, 'USB: no "miniDSP" found', "Connect miniDSP via USB")),
        ):
            result = await checker.check_denon_and_playback()
        assert not result.passed
        assert result.name == "Denon AVR"
        assert "Connect miniDSP" in result.error

    async def test_denon_fails_skips_playback_check(self, config):
        config._data.setdefault("measurement", {})["playback_route"] = "hdmi"
        checker = PreflightChecker(config)
        denon_fail = CheckResult("Denon AVR", False, "Cannot connect", "AVR unreachable")
        with (
            patch.object(checker, "check_denon", return_value=denon_fail),
            patch.object(checker, "check_playback_route") as mock_playback,
        ):
            result = await checker.check_denon_and_playback()
        assert not result.passed
        assert result.name == "Denon AVR"
        assert "AVR unreachable" in result.error
        mock_playback.assert_not_called()


# ── Config check ──────────────────────────────────────────────────────────────

class TestPreflightConfigCheck:
    async def test_check_config_all_fields_present(self, config):
        result = await PreflightChecker(config).check_config()
        assert result.passed
        assert result.error is None

    async def test_check_config_denon_host_none_still_passes(self):
        """denon.host is optional — SSDP auto-discovery covers missing host."""
        from calibrate.config import Config
        cfg = Config({"denon": {"host": None}, "minidsp": {}, "mic": {}})
        result = await PreflightChecker(cfg).check_config()
        assert result.passed
        assert "SSDP" in result.detail

    async def test_check_config_with_valid_host(self):
        from calibrate.config import Config
        cfg = Config({"denon": {"host": "192.168.1.100"}, "minidsp": {}, "mic": {}})
        result = await PreflightChecker(cfg).check_config()
        assert result.passed
        assert "All fields present" in result.detail


# ── HDMI deduplication ────────────────────────────────────────────────────────

class TestHdmiDeduplication:
    async def test_hdmi_route_delegates_to_check_denon(self, config):
        """check_playback_route(hdmi) must call self.check_denon(), not create its own DenonAVR."""
        config._data.setdefault("measurement", {})["playback_route"] = "hdmi"
        config._data["denon"]["host"] = "192.168.1.100"
        checker = PreflightChecker(config)
        denon_result = CheckResult("Denon AVR", True, "Denon AVR-X3800H online at 192.168.1.100")
        with patch.object(checker, "check_denon", return_value=denon_result) as mock_denon:
            result = await checker.check_playback_route()
        mock_denon.assert_awaited_once()
        assert result.passed
        assert "HDMI" in result.detail
        assert "X3800H" in result.detail

    async def test_hdmi_route_uses_check_denon_not_raw_denonavr(self, config):
        """When playback_route=hdmi, check_playback_route delegates to check_denon."""
        config._data.setdefault("measurement", {})["playback_route"] = "hdmi"
        config._data["denon"]["host"] = "192.168.1.100"
        checker = PreflightChecker(config)
        denon_result = CheckResult("Denon AVR", True, "X3800H online at 192.168.1.100")
        with patch.object(checker, "check_denon", return_value=denon_result) as mock_denon:
            await checker.check_playback_route()
        mock_denon.assert_awaited_once()

    async def test_hdmi_route_propagates_denon_failure(self, config):
        """HDMI route failure reflects check_denon's failure."""
        config._data.setdefault("measurement", {})["playback_route"] = "hdmi"
        config._data["denon"]["host"] = "192.168.1.100"
        checker = PreflightChecker(config)
        denon_result = CheckResult("Denon AVR", False, "Cannot connect to Denon AVR at 192.168.1.100", "refused")
        with patch.object(checker, "check_denon", return_value=denon_result):
            result = await checker.check_playback_route()
        assert not result.passed
        assert result.error == "refused"


# ── SSDP generic exception (lines 235-236) ───────────────────────────────────

class TestSsdpGenericException:
    async def test_ssdp_generic_exception_returns_failed_result(self, config):
        """Generic (non-TimeoutError) exception during SSDP → failed CheckResult."""
        from unittest.mock import AsyncMock, patch
        config._data["denon"]["host"] = None
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver:
            MockDriver.return_value.discover = AsyncMock(side_effect=OSError("network unreachable"))
            result = await PreflightChecker(config).check_denon()
        assert not result.passed
        assert "SSDP discovery failed" in result.error


# ── check_signal_path_sync — source/preset both None (line 328) ──────────────

class TestSignalPathSyncEdgeCases:
    async def test_skipped_when_signal_path_has_no_source_or_preset(self, config):
        """signal_path set but both source and preset are None → skipped (line 328)."""
        # signal_path must be truthy (non-empty) to bypass the `if not sp` check,
        # but must have no source or preset.
        config._data["minidsp"]["signal_path"] = {"routing": [{"input": 0, "outputs": [0, 1]}]}
        result = await PreflightChecker(config).check_signal_path_sync()
        assert result.passed
        assert "skipped" in result.detail

    async def test_generic_exception_returns_failed_result(self, config):
        """DriverError during device state fetch → failed CheckResult."""
        from unittest.mock import AsyncMock, patch
        from calibrate.drivers.base import DriverError
        config._data["minidsp"]["signal_path"] = {"source": "Analog"}
        with patch("calibrate.drivers.minidsp.MinidspDriver.get_state",
                   new_callable=AsyncMock,
                   side_effect=DriverError("network unreachable")):
            result = await PreflightChecker(config).check_signal_path_sync()
        assert not result.passed
        assert "Cannot read device state" in result.error


# ── Audio stack health (PipeWire/PulseAudio interception) ──────────────────

@pytest.mark.asyncio
class TestAudioStackClean:
    """A userspace audio manager (PipeWire, PulseAudio, wireplumber) holding
    /dev/snd handles can intercept sweep playback and redirect it to unexpected
    sinks (HDMI, Scarlett monitor outputs). Observed where sweeps were reaching
    the AVR via an untraceable path until PipeWire was disabled on the host.
    """

    async def test_passes_when_only_camilladsp_holds_snd_devices(self, config, monkeypatch):
        from calibrate.preflight import PreflightChecker
        import subprocess

        clean_output = (
            "                     USER        PID ACCESS COMMAND\n"
            "/dev/snd/controlC2:  pi        12345 F.... camilladsp\n"
            "/dev/snd/controlC3:  pi        12345 F.... camilladsp\n"
        )

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(args=cmd, returncode=0,
                                                stdout=clean_output, stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        checker = PreflightChecker(config)
        result = await checker.check_audio_stack_clean()
        assert result.passed
        assert "No PipeWire" in result.detail

    async def test_fails_when_pipewire_holds_snd_device(self, config, monkeypatch):
        from calibrate.preflight import PreflightChecker
        import subprocess

        dirty_output = (
            "                     USER        PID ACCESS COMMAND\n"
            "/dev/snd/controlC2:  pi          805 F.... wireplumber\n"
            "                     pi        60728 F.... camilladsp\n"
            "/dev/snd/controlC3:  pi        60728 F.... camilladsp\n"
            "/dev/snd/controlC4:  pi          805 F.... wireplumber\n"
        )

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(args=cmd, returncode=0,
                                                stdout=dirty_output, stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        checker = PreflightChecker(config)
        result = await checker.check_audio_stack_clean()
        assert not result.passed
        assert "wireplumber" in result.detail.lower()
        # Error message contains an actionable systemctl recipe.
        assert "systemctl" in (result.error or "")

    async def test_skips_gracefully_when_fuser_unavailable(self, config, monkeypatch):
        from calibrate.preflight import PreflightChecker
        import subprocess

        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("fuser not installed")

        monkeypatch.setattr(subprocess, "run", fake_run)
        checker = PreflightChecker(config)
        result = await checker.check_audio_stack_clean()
        # When we can't inspect, we don't block — pass with explanatory detail.
        assert result.passed
        assert "skipping" in result.detail.lower()


# ── Output routing safety (Focusrite Monitor-bus detection) ─────────────────

@pytest.mark.asyncio
class TestOutputRoutingSafety:
    """Scarlett 18i20 Lines 01-04 are slaved to the Monitor 1/2 hardware knobs
    (read-only in software). Warn when any active transducer lands on those
    lines. Non-fatal, but catches the silent-attenuation footgun from run 14.
    """

    def _config_with_graph(self, dsp_driver: str, transducer_indices: list[tuple[str, str, int]]) -> "Config":
        """transducer_indices: list of (name, role, output_index)."""
        from calibrate.config import Config
        return Config({
            "dsp_driver": dsp_driver,
            "denon": {"host": "192.168.1.100"},
            "minidsp": {"host": "localhost", "port": 5380},
            "mic": {"name": "UMIK"},
            "signal_graph": {
                "transducers": [
                    {
                        "name": n,
                        "role": r,
                        "processor_ref": "camilla" if dsp_driver == "camilladsp" else "minidsp",
                        "output_index": idx,
                        "safety_profile_ref": "svs_pb12_nsd",
                    }
                    for (n, r, idx) in transducer_indices
                ],
            },
        })

    async def test_non_camilladsp_driver_is_skipped(self):
        config = self._config_with_graph("minidsp", [("sub_a", "sub", 1)])
        result = await PreflightChecker(config).check_output_routing_safety()
        assert result.passed
        assert "non-CamillaDSP" in result.detail

    async def test_all_outputs_on_direct_pcm_lines(self):
        config = self._config_with_graph("camilladsp", [
            ("sub_fr", "sub", 5),
            ("sub_nf", "sub", 6),
            ("shaker", "shaker", 7),
        ])
        result = await PreflightChecker(config).check_output_routing_safety()
        assert result.passed
        assert "direct-PCM" in result.detail

    async def test_monitor_bus_offenders_are_flagged(self):
        config = self._config_with_graph("camilladsp", [
            ("sub_fr", "sub", 1),     # Line 02 — Monitor 1 R
            ("sub_nf", "sub", 2),     # Line 03 — Monitor 2 L
            ("shaker", "shaker", 7),  # safe
        ])
        result = await PreflightChecker(config).check_output_routing_safety()
        assert not result.passed
        assert "Line 02" in result.detail
        assert "Line 03" in result.detail
        assert "Line 08" not in result.detail  # shaker at 7 → safe, not listed
        assert "Monitor" in result.error

    async def test_unused_transducers_are_skipped(self):
        config = self._config_with_graph("camilladsp", [
            ("front_left_unused", "unused", 0),  # Line 01 BUT unused — ignore
            ("sub_fr", "sub", 5),                # safe
        ])
        result = await PreflightChecker(config).check_output_routing_safety()
        assert result.passed


# ── Bug 4: pipeline_state — warn when CamillaDSP pipeline is Inactive ─────────

@pytest.mark.asyncio
class TestCamillaDSPPipelineStateInPreflight:
    """check_minidsp warns (but passes) when CamillaDSP pipeline is Inactive.

    Pipeline restarts itself before measurement when needed, so an Inactive
    state at preflight time is non-fatal — just informational.
    """

    def _camilladsp_config(self) -> "Config":
        from calibrate.config import Config, DEFAULT_CONFIG
        data = {
            **DEFAULT_CONFIG,
            "dsp_driver": "camilladsp",
            "camilladsp": {
                **DEFAULT_CONFIG["camilladsp"],
                "host": "127.0.0.1",
                "port": 1234,
            },
        }
        return Config(data)

    async def test_running_pipeline_passes_cleanly(self) -> None:
        """check_minidsp passes with clean detail when pipeline is Running."""
        from calibrate.drivers.camilladsp import CamillaDSPDriver
        cfg = self._camilladsp_config()
        driver = CamillaDSPDriver()
        responses = {"GetState": "Running", "GetVolume": 0.0, "GetMute": False, "GetProcessingLoad": 0.0}
        driver._client._ws = object()
        driver._client.call = AsyncMock(side_effect=lambda cmd, *a, **kw: responses[cmd])

        import calibrate.drivers.registry as _reg
        with patch.object(_reg, "load_dsp_driver", return_value=driver):
            result = await PreflightChecker(cfg).check_minidsp()

        assert result.passed
        assert "Inactive" not in result.detail

    async def test_inactive_pipeline_warns_but_passes(self) -> None:
        """check_minidsp passes (warning) when pipeline is Inactive."""
        from calibrate.drivers.camilladsp import CamillaDSPDriver
        cfg = self._camilladsp_config()
        driver = CamillaDSPDriver()

        responses = {"GetState": "Inactive", "GetVolume": 0.0, "GetMute": False}
        driver._client._ws = object()
        driver._client.call = AsyncMock(side_effect=lambda cmd, *a, **kw: responses.get(cmd, "Inactive"))

        import calibrate.drivers.registry as _reg
        with patch.object(_reg, "load_dsp_driver", return_value=driver):
            result = await PreflightChecker(cfg).check_minidsp()

        assert result.passed  # warning, not hard failure
        assert "Inactive" in result.detail
        assert "not Running" in result.detail

    async def test_unknown_pipeline_state_does_not_warn(self) -> None:
        """check_minidsp does not add a warning when state is Unknown (websocket error).

        pipeline_state() returns "Unknown" on DriverError; check_minidsp should
        still pass and not include an Inactive warning.
        """
        from calibrate.drivers.camilladsp import CamillaDSPDriver
        from calibrate.drivers.base import DriverError
        cfg = self._camilladsp_config()
        driver = CamillaDSPDriver()

        # get_state succeeds for all commands; pipeline_state is separately mocked
        # to return "Unknown" so we don't fight with the get_state call ordering.
        responses = {"GetState": "Running", "GetVolume": 0.0, "GetMute": False, "GetProcessingLoad": 0.0}
        driver._client._ws = object()
        driver._client.call = AsyncMock(side_effect=lambda cmd, *a, **kw: responses[cmd])

        # Override pipeline_state directly to return Unknown without a DriverError.
        driver.pipeline_state = AsyncMock(return_value="Unknown")

        import calibrate.drivers.registry as _reg
        with patch.object(_reg, "load_dsp_driver", return_value=driver):
            result = await PreflightChecker(cfg).check_minidsp()

        assert result.passed
        # Unknown state should not trigger the Inactive warning
        assert "Inactive" not in result.detail
        assert "not Running" not in result.detail


# ── check_dsp_persisted_state ────────────────────────────────────────────────

class TestDspPersistedState:
    """check_dsp_persisted_state surfaces non-default per-output DSP state."""

    async def test_all_defaults_passes(self, config) -> None:
        """No persisted overrides → passes cleanly."""
        checker = PreflightChecker(config)
        with patch("calibrate.storage.SessionStore") as MockStore:
            MockStore.return_value.get_active_dsp.return_value = {
                "processor:camilla:output:5:polarity": {"inverted": False, "timestamp": "t0"},
                "processor:camilla:output:5:gain": {"gain_db": 0.0, "timestamp": "t0"},
                "processor:camilla:output:5:delay": {"delay_ms": 0.0, "timestamp": "t0"},
            }
            result = await checker.check_dsp_persisted_state()
        assert result.passed
        assert "defaults" in result.detail

    async def test_polarity_flip_warns(self, config) -> None:
        checker = PreflightChecker(config)
        with patch("calibrate.storage.SessionStore") as MockStore:
            MockStore.return_value.get_active_dsp.return_value = {
                "processor:camilla:output:5:polarity": {"inverted": True, "timestamp": "2026-04-24T02:59"},
            }
            result = await checker.check_dsp_persisted_state()
        assert not result.passed
        assert "polarity=inverted" in result.detail
        assert "camilla:output:5" in result.detail

    async def test_nonzero_gain_warns(self, config) -> None:
        checker = PreflightChecker(config)
        with patch("calibrate.storage.SessionStore") as MockStore:
            MockStore.return_value.get_active_dsp.return_value = {
                "processor:camilla:output:6:gain": {"gain_db": 6.0, "timestamp": "t0"},
            }
            result = await checker.check_dsp_persisted_state()
        assert not result.passed
        assert "gain_db=+6.00" in result.detail

    async def test_nonzero_delay_warns(self, config) -> None:
        checker = PreflightChecker(config)
        with patch("calibrate.storage.SessionStore") as MockStore:
            MockStore.return_value.get_active_dsp.return_value = {
                "processor:camilla:output:2:delay": {"delay_ms": 6.15, "timestamp": "t0"},
            }
            result = await checker.check_dsp_persisted_state()
        assert not result.passed
        assert "delay_ms=6.150" in result.detail

    async def test_eq_and_mute_are_ignored(self, config) -> None:
        """EQ state is expected post-cal; mute is transient — neither triggers a warning."""
        checker = PreflightChecker(config)
        with patch("calibrate.storage.SessionStore") as MockStore:
            MockStore.return_value.get_active_dsp.return_value = {
                "processor:camilla:output:5:eq": {"filters": [{"type": "hpf", "freq": 18}], "timestamp": "t0"},
                "processor:camilla:output:5:mute": {"muted": True, "timestamp": "t0"},
            }
            result = await checker.check_dsp_persisted_state()
        assert result.passed

    async def test_storage_error_skips_gracefully(self, config) -> None:
        """SessionStore failure → graceful skip, not a hard fail."""
        checker = PreflightChecker(config)
        with patch("calibrate.storage.SessionStore", side_effect=RuntimeError("db gone")):
            result = await checker.check_dsp_persisted_state()
        assert result.passed  # skipped, not failed
        assert "skipped" in result.detail.lower()


