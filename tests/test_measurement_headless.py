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
        engine.validate_recording = MagicMock(return_value=[])

        mock_sd = sys.modules["sounddevice"]
        mock_sd.reset_mock()
        mock_sd.default.device = (0, 0)

        return engine, mock_pytta, mock_sd, sweep, recording

    def test_sets_sd_default_device_when_umik_found(self):
        """measure(input_device_name='UMIK') sets sd.default.device to UMIK index."""
        engine, _, mock_sd, _, _ = self._setup_mocks()
        mock_sd.query_devices.return_value = [
            {"name": "Built-in Mic", "max_input_channels": 1},
            {"name": "UMIK-1", "max_input_channels": 1},
        ]
        mock_sd.default.device = (0, 2)

        engine.measure(input_device_name="UMIK")

        # UMIK-1 is at index 1; output stays at 2
        assert mock_sd.default.device == (1, 2)

    def test_no_input_device_name_leaves_default_unchanged(self):
        """measure() with no input_device_name does not touch sd.default.device."""
        engine, _, mock_sd, _, _ = self._setup_mocks()

        engine.measure()

        mock_sd.query_devices.assert_not_called()

    def test_sounddevice_unavailable_silently_ignored(self):
        """If sounddevice is not installed, measure() proceeds without setting device."""
        engine, _, _, _, _ = self._setup_mocks()
        with patch.dict(sys.modules, {"sounddevice": None}):
            fr = engine.measure(input_device_name="UMIK")
        assert isinstance(fr, FrequencyResponse)

    def test_portaudio_error_raises_runtime_error(self):
        """PortAudioError from meas.run() is re-raised as RuntimeError."""
        engine, mock_pytta, _, _, _ = self._setup_mocks()

        class PortAudioError(Exception):
            pass

        mock_pytta.PlayRecMeasure.return_value.run.side_effect = PortAudioError(
            "No default output device"
        )

        with pytest.raises(RuntimeError, match="Audio device error"):
            engine.measure()

    def test_validate_recording_called(self):
        """validate_recording() is called before _compute_fr()."""
        engine, mock_pytta, _, sweep, recording = self._setup_mocks()

        call_order: list[str] = []

        original_validate = engine.validate_recording
        original_compute = engine._compute_fr

        def spy_validate(*args, **kwargs):
            call_order.append("validate")
            return original_validate(*args, **kwargs)

        def spy_compute(*args, **kwargs):
            call_order.append("compute")
            return original_compute(*args, **kwargs)

        engine.validate_recording = spy_validate
        engine._compute_fr = spy_compute

        engine.measure()

        assert call_order.index("validate") < call_order.index("compute")
