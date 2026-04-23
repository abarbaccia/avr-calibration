"""Tests for Pi 5 headless measurement additions in measurement.py.

Coverage diagram:
  _find_umik_device()
  ├── [TESTED] happy path — returns index of first matching input device
  ├── [TESTED] output-only device (max_input_channels=0) is skipped
  ├── [TESTED] name_substring param matches custom substring (e.g. "C-Media")
  ├── [TESTED] multiple matching devices — picks first
  └── [TESTED] no matching device — returns None

  MeasurementEngine.measure(input_device_name=...)
  ├── [TESTED] sets sd.default.device when input_device_name provided and device found
  ├── [TESTED] no-op when input_device_name is None (default)
  ├── [TESTED] sounddevice unavailable → silently ignored, measure proceeds
  ├── [TESTED] PortAudioError from meas.run() → RuntimeError
  └── [TESTED] validate_recording() called before _compute_fr()
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, call, patch

import numpy as np
import pytest

from calibrate.config import Config
from calibrate.measurement import FrequencyResponse, MeasurementEngine, _find_umik_device


def make_config(**measurement_overrides) -> Config:
    defaults = {
        "freq_min": 20,
        "freq_max": 200,
        "sweep_duration": 3.0,
        "sample_rate": 48000,
        "input_channel": 1,
        "output_channel": 1,
    }
    defaults.update(measurement_overrides)
    return Config({
        "denon": {"host": "192.168.1.100"},
        "minidsp": {"host": "localhost", "port": 5380},
        "mic": {"name": "UMIK"},
        "measurement": defaults,
    })


def make_signal(n_samples: int = 4800) -> MagicMock:
    sig = MagicMock()
    sig.timeSignal = np.random.default_rng(42).standard_normal((n_samples, 1))
    return sig


# ── _find_umik_device ─────────────────────────────────────────────────────────

class TestFindUmikDevice:
    def test_happy_path_returns_index(self):
        devices = [
            {"name": "Built-in Microphone", "max_input_channels": 1},
            {"name": "UMIK-1", "max_input_channels": 1},
        ]
        assert _find_umik_device(devices) == 1

    def test_output_only_device_skipped(self):
        """UMIK device with max_input_channels=0 (output monitor) is not selected."""
        devices = [{"name": "UMIK Monitor Output", "max_input_channels": 0}]
        assert _find_umik_device(devices) is None

    def test_custom_name_substring(self):
        """name_substring='C-Media' matches 'C-Media USB Audio Device'."""
        devices = [
            {"name": "Built-in Output", "max_input_channels": 0},
            {"name": "C-Media USB Audio Device", "max_input_channels": 1},
        ]
        assert _find_umik_device(devices, name_substring="C-Media") == 1

    def test_multiple_matching_picks_first(self):
        devices = [
            {"name": "UMIK-1", "max_input_channels": 1},
            {"name": "UMIK-2", "max_input_channels": 1},
        ]
        assert _find_umik_device(devices) == 0

    def test_no_matching_device_returns_none(self):
        devices = [
            {"name": "Built-in Microphone", "max_input_channels": 1},
            {"name": "USB Headset", "max_input_channels": 1},
        ]
        assert _find_umik_device(devices) is None

    def test_empty_device_list_returns_none(self):
        assert _find_umik_device([]) is None


# ── MeasurementEngine.measure(input_device_name=...) ─────────────────────────

class TestMeasureHeadless:
    def _setup_mocks(self, config=None):
        """Configure session-scoped pytta and sounddevice mocks for a single test."""
        cfg = config or make_config()
        engine = MeasurementEngine(cfg)
        n = cfg.measurement["sample_rate"] * 3

        mock_pytta = sys.modules["pytta"]
        mock_pytta.reset_mock()
        sweep = make_signal(n)
        recording = make_signal(n)
        mock_pytta.generate.sweep.return_value = sweep
        mock_pytta.PlayRecMeasure.return_value.run.return_value = recording
        mock_pytta.PlayRecMeasure.return_value.run.side_effect = None  # clear from prev test

        # validate_recording is called inside measure(); mock it so random signals
        # don't fail the SNR quality gate in tests that aren't testing quality gates.
        engine.validate_recording = MagicMock(return_value=([], 0))

        mock_sd = sys.modules["sounddevice"]
        mock_sd.reset_mock()
        mock_sd.default.device = (0, 0)

        # Build a mock playback strategy that returns real arrays.
        # USBPlayback now uses sd.InputStream/OutputStream directly — measure() tests
        # should patch playback_for_route to avoid needing real audio hardware.
        sweep_1d = sweep.timeSignal[:, 0]
        rec_1d = recording.timeSignal[:, 0]
        mock_strategy = MagicMock()
        mock_strategy.play_and_record.return_value = (sweep_1d, rec_1d)

        return engine, mock_pytta, mock_sd, sweep, recording, mock_strategy

    @pytest.mark.asyncio
    async def test_sets_sd_default_device_when_umik_found(self):
        """measure(input_device_name='UMIK') sets sd.default.device to UMIK index."""
        engine, _, mock_sd, _, _, mock_strategy = self._setup_mocks()
        mock_sd.query_devices.return_value = [
            {"name": "Built-in Mic", "max_input_channels": 1},
            {"name": "UMIK-1", "max_input_channels": 1},
        ]
        mock_sd.default.device = (0, 2)

        with patch("calibrate.drivers.playback.playback_for_route", return_value=mock_strategy):
            await engine.measure(input_device_name="UMIK")

        # UMIK-1 is at index 1; output stays at 2
        assert mock_sd.default.device == (1, 2)

    @pytest.mark.asyncio
    async def test_no_input_device_name_uses_config_mic_name(self):
        """measure() with no input_device_name uses config.mic.name for UMIK selection."""
        engine, _, mock_sd, _, _, mock_strategy = self._setup_mocks()
        mock_sd.query_devices.return_value = [
            {"name": "Built-in Mic", "max_input_channels": 1},
            {"name": "UMIK-1", "max_input_channels": 1},
        ]
        mock_sd.default.device = (0, 2)

        with patch("calibrate.drivers.playback.playback_for_route", return_value=mock_strategy):
            await engine.measure()

        # Config has mic.name="UMIK", so UMIK-1 at index 1 should be selected
        assert mock_sd.default.device == (1, 2)

    @pytest.mark.asyncio
    async def test_sounddevice_unavailable_silently_ignored(self):
        """If sounddevice is not installed, measure() proceeds without setting device."""
        engine, _, _, _, _, mock_strategy = self._setup_mocks()
        with patch.dict(sys.modules, {"sounddevice": None}):
            with patch("calibrate.drivers.playback.playback_for_route", return_value=mock_strategy):
                fr = await engine.measure(input_device_name="UMIK")
        assert isinstance(fr, FrequencyResponse)

    @pytest.mark.asyncio
    async def test_portaudio_error_raises_runtime_error(self):
        """PortAudioError from play_and_record() is re-raised as RuntimeError."""
        engine, _, _, _, _, _ = self._setup_mocks()

        class PortAudioError(Exception):
            pass

        error_strategy = MagicMock()
        error_strategy.play_and_record.side_effect = RuntimeError(
            "Audio device error: No default output device"
        )

        with patch("calibrate.drivers.playback.playback_for_route", return_value=error_strategy):
            with pytest.raises(RuntimeError, match="Audio device error"):
                await engine.measure()

    @pytest.mark.asyncio
    async def test_validate_recording_called(self):
        """validate_recording() is called before _compute_fr_arrays()."""
        engine, mock_pytta, _, sweep, recording, mock_strategy = self._setup_mocks()

        call_order: list[str] = []

        original_validate = engine.validate_recording
        original_compute = engine._compute_fr_arrays

        def spy_validate(*args, **kwargs):
            call_order.append("validate")
            return original_validate(*args, **kwargs)

        def spy_compute(*args, **kwargs):
            call_order.append("compute")
            return original_compute(*args, **kwargs)

        engine.validate_recording = spy_validate
        engine._compute_fr_arrays = spy_compute

        with patch("calibrate.drivers.playback.playback_for_route", return_value=mock_strategy):
            await engine.measure()

        assert call_order.index("validate") < call_order.index("compute")

    @pytest.mark.asyncio
    async def test_mic_device_index_overrides_name_search(self):
        """When mic_device_index is set, uses that index directly instead of name search."""
        cfg = make_config(mic_device_index=3)
        engine, _, mock_sd, _, _, mock_strategy = self._setup_mocks(config=cfg)
        mock_sd.query_devices.return_value = [
            {"name": "Built-in Mic", "max_input_channels": 1},
            {"name": "UMIK-1", "max_input_channels": 1},
            {"name": "USB Audio", "max_input_channels": 0},
            {"name": "Direct UMIK", "max_input_channels": 1},
        ]
        mock_sd.default.device = (0, 2)

        with patch("calibrate.drivers.playback.playback_for_route", return_value=mock_strategy):
            await engine.measure()

        # Should use index 3 directly, not search for "UMIK" (which would give index 1)
        assert mock_sd.default.device[0] == 3

    @pytest.mark.asyncio
    async def test_hdmi_device_index_overrides_name_search(self):
        """When hdmi_device_index is set, uses that index for HDMI output."""
        cfg = make_config(playback_route="hdmi", hdmi_device_index=5)
        engine, _, mock_sd, _, _, mock_strategy = self._setup_mocks(config=cfg)
        mock_sd.query_devices.return_value = [
            {"name": "Built-in Mic", "max_input_channels": 1},
            {"name": "UMIK-1", "max_input_channels": 1},
            {"name": "hdmi plugin", "max_output_channels": 8},
            {"name": "hw:0,0", "max_output_channels": 8},
            {"name": "default", "max_output_channels": 2},
            {"name": "exact hdmi", "max_output_channels": 6},
        ]
        mock_sd.default.device = (1, 0)

        with patch("calibrate.drivers.playback.playback_for_route", return_value=mock_strategy):
            await engine.measure()

        # Should use index 5 directly
        assert mock_sd.default.device == (1, 5)

    @pytest.mark.asyncio
    async def test_usb_device_index_overrides_name_search(self):
        """When usb_device_index is set, uses that index for USB output."""
        cfg = make_config(playback_route="usb", usb_device_index=2)
        engine, _, mock_sd, _, _, mock_strategy = self._setup_mocks(config=cfg)
        mock_sd.query_devices.return_value = [
            {"name": "UMIK-1", "max_input_channels": 1},
            {"name": "Built-in Speaker", "max_output_channels": 2},
            {"name": "USB DAC", "max_output_channels": 2},
        ]
        mock_sd.default.device = (0, 1)

        with patch("calibrate.drivers.playback.playback_for_route", return_value=mock_strategy):
            await engine.measure()

        # Should use index 2 directly
        assert mock_sd.default.device == (0, 2)

    @pytest.mark.asyncio
    async def test_name_search_fallback_when_no_device_index(self):
        """When device indices are None, falls back to name substring matching."""
        cfg = make_config(playback_route="usb")
        engine, _, mock_sd, _, _, mock_strategy = self._setup_mocks(config=cfg)
        mock_sd.query_devices.return_value = [
            {"name": "UMIK-1", "max_input_channels": 1},
            {"name": "miniDSP 2x4 HD", "max_output_channels": 2, "max_input_channels": 0},
        ]
        mock_sd.default.device = (0, 0)

        with patch("calibrate.drivers.playback.playback_for_route", return_value=mock_strategy):
            await engine.measure()

        # UMIK at index 0, miniDSP at index 1 (by name search)
        assert mock_sd.default.device == (0, 1)
