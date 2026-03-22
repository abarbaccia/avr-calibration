"""Tests for hardware pre-flight checks.

Coverage diagram:
  PreflightChecker
  ├── check_mic()
  │   ├── [TESTED] UMIK found by name match → passes with device detail
  │   ├── [TESTED] Name match is case-insensitive
  │   ├── [TESTED] No UMIK but other inputs exist → fails, shows available
  │   ├── [TESTED] No input devices at all → fails, generic message
  │   └── [TESTED] sounddevice raises → fails gracefully with error text
  │   └── [TESTED] Detail includes device index and sample rate
  ├── check_minidsp()
  │   ├── [TESTED] Device found → passes with product name + serial
  │   ├── [TESTED] Device found but no serial → passes, no serial in detail
  │   ├── [TESTED] Daemon running but no USB device → fails, actionable hint
  │   ├── [TESTED] ConnectError (daemon not running) → fails, start-daemon hint
  │   ├── [TESTED] TimeoutException → fails, wait-and-retry hint
  │   ├── [TESTED] Unexpected exception → fails gracefully
  │   └── [TESTED] Custom host and port respected
  ├── check_denon()
  │   ├── [TESTED] AVR online → passes with model name
  │   ├── [TESTED] model_name is None → falls back to "Denon AVR"
  │   ├── [TESTED] host not configured → fails, edit-config hint
  │   ├── [TESTED] Connection fails → fails with host in detail
  │   └── [TESTED] Timeout → fails
  └── run_all()
      ├── [TESTED] All pass → 3 passed results
      ├── [TESTED] Unhandled exception → captured as failed result
      ├── [TESTED] Results named correctly even when exceptions occur
      └── [TESTED] Partial failure (2 pass, 1 fail)
"""

import sys
import pytest
import httpx
import respx
from unittest.mock import patch, MagicMock, AsyncMock

from calibrate.preflight import PreflightChecker, CheckResult
from tests.conftest import make_input_device, make_output_device


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

class TestMinidspCheck:
    @respx.mock
    async def test_device_found_with_serial(self, config):
        respx.get("http://localhost:5380/devices").mock(return_value=httpx.Response(
            200, json=[{"product_name": "2x4HD", "version": {"serial": 965535}}]
        ))
        result = await PreflightChecker(config).check_minidsp()
        assert result.passed
        assert "2x4HD" in result.detail
        assert "965535" in result.detail

    @respx.mock
    async def test_device_found_without_serial(self, config):
        respx.get("http://localhost:5380/devices").mock(return_value=httpx.Response(
            200, json=[{"product_name": "2x4HD", "version": {}}]
        ))
        result = await PreflightChecker(config).check_minidsp()
        assert result.passed
        assert "serial" not in result.detail

    @respx.mock
    async def test_daemon_running_no_usb_device(self, config):
        respx.get("http://localhost:5380/devices").mock(return_value=httpx.Response(200, json=[]))
        result = await PreflightChecker(config).check_minidsp()
        assert not result.passed
        assert "no devices found" in result.detail.lower()
        assert "USB" in result.error

    @respx.mock
    async def test_connect_error_daemon_not_running(self, config):
        respx.get("http://localhost:5380/devices").mock(side_effect=httpx.ConnectError("refused"))
        result = await PreflightChecker(config).check_minidsp()
        assert not result.passed
        assert "minidspd" in result.error.lower()

    @respx.mock
    async def test_timeout(self, config):
        route = respx.get("http://localhost:5380/devices")
        route.mock(side_effect=httpx.ConnectTimeout("timed out"))
        result = await PreflightChecker(config).check_minidsp()
        assert not result.passed
        assert "wait" in result.error.lower()  # "wait a moment and retry"

    @respx.mock
    async def test_unexpected_exception(self, config):
        respx.get("http://localhost:5380/devices").mock(side_effect=ValueError("bad json"))
        result = await PreflightChecker(config).check_minidsp()
        assert not result.passed
        assert result.error is not None

    async def test_custom_host_and_port(self):
        from calibrate.config import Config
        cfg = Config({
            "denon": {"host": None},
            "minidsp": {"host": "10.0.0.5", "port": 9999},
            "mic": {"name": "UMIK"},
        })
        with respx.mock:
            respx.get("http://10.0.0.5:9999/devices").mock(return_value=httpx.Response(
                200, json=[{"product_name": "2x4HD", "version": {}}]
            ))
            result = await PreflightChecker(cfg).check_minidsp()
        assert result.passed
        assert "10.0.0.5:9999" in result.detail


# ── Denon AVR checks ─────────────────────────────────────────────────────────

class TestDenonCheck:
    async def test_avr_online(self, config):
        mock_receiver = MagicMock()
        mock_receiver.model_name = "Denon AVR-X3800H"
        mock_receiver.async_setup = AsyncMock()
        with patch("denonavr.DenonAVR", return_value=mock_receiver):
            result = await PreflightChecker(config).check_denon()
        assert result.passed
        assert "X3800H" in result.detail
        assert "192.168.1.100" in result.detail

    async def test_avr_model_name_none_falls_back(self, config):
        mock_receiver = MagicMock()
        mock_receiver.model_name = None
        mock_receiver.async_setup = AsyncMock()
        with patch("denonavr.DenonAVR", return_value=mock_receiver):
            result = await PreflightChecker(config).check_denon()
        assert result.passed
        assert "Denon AVR" in result.detail

    async def test_host_not_configured(self, config):
        config._data["denon"]["host"] = None
        result = await PreflightChecker(config).check_denon()
        assert not result.passed
        assert "not set" in result.error.lower()
        assert "config.yaml" in result.error

    async def test_avr_unreachable(self, config):
        mock_receiver = MagicMock()
        mock_receiver.async_setup = AsyncMock(side_effect=ConnectionRefusedError("refused"))
        with patch("denonavr.DenonAVR", return_value=mock_receiver):
            result = await PreflightChecker(config).check_denon()
        assert not result.passed
        assert "192.168.1.100" in result.detail

    async def test_avr_timeout(self, config):
        mock_receiver = MagicMock()
        mock_receiver.async_setup = AsyncMock(side_effect=TimeoutError("timed out"))
        with patch("denonavr.DenonAVR", return_value=mock_receiver):
            result = await PreflightChecker(config).check_denon()
        assert not result.passed


# ── run_all() ────────────────────────────────────────────────────────────────

class TestRunAll:
    async def test_all_pass(self, config):
        checker = PreflightChecker(config)
        with (
            patch.object(checker, "check_mic", return_value=CheckResult("Microphone", True, "UMIK-1")),
            patch.object(checker, "check_minidsp", return_value=CheckResult("miniDSP", True, "2x4HD")),
            patch.object(checker, "check_denon", return_value=CheckResult("Denon AVR", True, "X3800H")),
            patch.object(checker, "check_playback_route", return_value=CheckResult("Playback Route", True, "USB: miniDSP")),
        ):
            results = await checker.run_all()
        assert all(r.passed for r in results)
        assert len(results) == 4

    async def test_unhandled_exception_becomes_failed_result(self, config):
        checker = PreflightChecker(config)
        with (
            patch.object(checker, "check_mic", side_effect=RuntimeError("boom")),
            patch.object(checker, "check_minidsp", return_value=CheckResult("miniDSP", True, "2x4HD")),
            patch.object(checker, "check_denon", return_value=CheckResult("Denon AVR", True, "X3800H")),
            patch.object(checker, "check_playback_route", return_value=CheckResult("Playback Route", True, "USB")),
        ):
            results = await checker.run_all()
        mic = next(r for r in results if r.name == "Microphone")
        assert not mic.passed
        assert "boom" in mic.error

    async def test_result_names_match_expected(self, config):
        checker = PreflightChecker(config)
        with (
            patch.object(checker, "check_mic", side_effect=RuntimeError("err")),
            patch.object(checker, "check_minidsp", side_effect=RuntimeError("err")),
            patch.object(checker, "check_denon", side_effect=RuntimeError("err")),
            patch.object(checker, "check_playback_route", side_effect=RuntimeError("err")),
        ):
            results = await checker.run_all()
        assert [r.name for r in results] == ["Microphone", "miniDSP", "Denon AVR", "Playback Route"]

    async def test_partial_failure(self, config):
        checker = PreflightChecker(config)
        with (
            patch.object(checker, "check_mic", return_value=CheckResult("Microphone", True, "UMIK-1")),
            patch.object(checker, "check_minidsp", return_value=CheckResult("miniDSP", False, "", "start minidspd")),
            patch.object(checker, "check_denon", return_value=CheckResult("Denon AVR", True, "X3800H")),
            patch.object(checker, "check_playback_route", return_value=CheckResult("Playback Route", True, "USB")),
        ):
            results = await checker.run_all()
        assert results[0].passed
        assert not results[1].passed
        assert results[2].passed
        assert results[3].passed


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
        mock_receiver = MagicMock()
        mock_receiver.model_name = "Denon AVR-X3800H"
        mock_receiver.async_setup = AsyncMock()
        with patch("denonavr.DenonAVR", return_value=mock_receiver):
            result = await PreflightChecker(config).check_playback_route()
        assert result.passed
        assert "HDMI" in result.detail
        assert "192.168.1.100" in result.detail

    async def test_hdmi_route_no_denon_host(self, config):
        config._data.setdefault("measurement", {})["playback_route"] = "hdmi"
        config._data["denon"]["host"] = None
        result = await PreflightChecker(config).check_playback_route()
        assert not result.passed
        assert "denon.host" in result.detail

    async def test_hdmi_route_denon_unreachable(self, config):
        config._data.setdefault("measurement", {})["playback_route"] = "hdmi"
        config._data["denon"]["host"] = "192.168.1.100"
        mock_receiver = MagicMock()
        mock_receiver.async_setup = AsyncMock(side_effect=ConnectionRefusedError("refused"))
        with patch("denonavr.DenonAVR", return_value=mock_receiver):
            result = await PreflightChecker(config).check_playback_route()
        assert not result.passed

    async def test_usb_sounddevice_raises_captured(self, config):
        config._data.setdefault("measurement", {})["playback_route"] = "usb"
        sys.modules["sounddevice"].query_devices.side_effect = RuntimeError("portaudio error")
        result = await PreflightChecker(config).check_playback_route()
        assert not result.passed
        assert "portaudio error" in result.error
        sys.modules["sounddevice"].query_devices.side_effect = None
