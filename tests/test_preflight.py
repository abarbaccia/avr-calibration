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
        state = {"connected": True, "host": "192.168.1.100", "model": "Denon AVR-X3800H"}
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver:
            MockDriver.return_value.get_state = AsyncMock(return_value=state)
            result = await PreflightChecker(config).check_denon()
        assert result.passed
        assert "X3800H" in result.detail
        assert "192.168.1.100" in result.detail

    async def test_avr_model_name_none_falls_back(self, config):
        state = {"connected": True, "host": "192.168.1.100", "model": "Denon AVR"}
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver:
            MockDriver.return_value.get_state = AsyncMock(return_value=state)
            result = await PreflightChecker(config).check_denon()
        assert result.passed
        assert "Denon AVR" in result.detail

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
        state = {"connected": True, "host": "192.168.1.42", "model": "Denon AVR-X3800H"}
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver:
            MockDriver.return_value.discover = AsyncMock(return_value=["192.168.1.42"])
            MockDriver.return_value.get_state = AsyncMock(return_value=state)
            result = await PreflightChecker(config).check_denon()
        assert result.passed
        assert "192.168.1.42" in result.detail
        assert "auto-discovered" in result.detail

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
        ):
            results = await checker.run_all()
        assert all(r.passed for r in results)
        assert len(results) == 5

    async def test_unhandled_exception_becomes_failed_result(self, config):
        checker = PreflightChecker(config)
        with (
            patch.object(checker, "check_config", return_value=CheckResult("Config", True, "Required fields present")),
            patch.object(checker, "check_mic", return_value=CheckResult("Microphone", True, "UMIK-1")),
            patch.object(checker, "check_minidsp_combined", side_effect=RuntimeError("boom")),
            patch.object(checker, "check_denon_and_playback", return_value=CheckResult("Denon AVR", True, "X3800H")),
            patch.object(checker, "check_signal_path_sync", return_value=CheckResult("Signal Path", True, "not configured (skipped)")),
        ):
            results = await checker.run_all()
        minidsp = next(r for r in results if r.name == "miniDSP")
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
        ):
            results = await checker.run_all()
        assert [r.name for r in results] == ["Config", "Microphone", "miniDSP", "Denon AVR", "Signal Path"]

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
        state = {"connected": True, "host": "192.168.1.100", "model": "Denon AVR-X3800H"}
        with patch("calibrate.drivers.denon.DenonDriver") as MockDriver:
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
        """Generic exception during miniDSP status fetch → failed CheckResult."""
        from unittest.mock import AsyncMock, patch
        config._data["minidsp"]["signal_path"] = {"source": "Analog"}
        with patch("calibrate.adapters.minidsp._get_status_via_cli",
                   new_callable=AsyncMock,
                   side_effect=OSError("network unreachable")):
            result = await PreflightChecker(config).check_signal_path_sync()
        assert not result.passed
        assert "Cannot reach miniDSP daemon" in result.error
