"""Tests for calibrate.drivers.playback — USB and HDMI playback strategies.

Coverage diagram:
  playback_for_route()
  ├── [TESTED] "usb" → returns USBPlayback instance
  ├── [TESTED] "hdmi" → returns HDMIPlayback instance
  └── [TESTED] unknown route → defaults to USBPlayback

  USBPlayback.play_and_record()
  ├── [TESTED] happy path — calls PyTTa PlayRecMeasure, returns (sweep_1d, rec_1d)
  └── [TESTED] PortAudioError → re-raised as RuntimeError

  HDMIPlayback.play_and_record()
  └── [TESTED] happy path — calls sd.rec + sd.play + sd.wait, returns arrays
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch, call

import numpy as np
import pytest

from calibrate.drivers.playback import (
    HDMIPlayback,
    USBPlayback,
    playback_for_route,
)


class TestPlaybackForRoute:
    def test_usb_route(self):
        assert isinstance(playback_for_route("usb"), USBPlayback)

    def test_hdmi_route(self):
        assert isinstance(playback_for_route("hdmi"), HDMIPlayback)

    def test_unknown_defaults_to_usb(self):
        assert isinstance(playback_for_route("toslink"), USBPlayback)


class TestUSBPlayback:
    def test_happy_path(self):
        mock_pytta = sys.modules["pytta"]
        mock_pytta.reset_mock()

        sweep = MagicMock()
        sweep.timeSignal = np.random.default_rng(42).standard_normal((4800, 1))

        recording = MagicMock()
        recording.timeSignal = np.random.default_rng(99).standard_normal((4800, 1))

        mock_pytta.PlayRecMeasure.return_value.run.return_value = recording

        strategy = USBPlayback()
        sweep_1d, rec_1d = strategy.play_and_record(sweep, 48000, 1, 1)

        mock_pytta.PlayRecMeasure.assert_called_once()
        assert sweep_1d.shape == (4800,)
        assert rec_1d.shape == (4800,)
        np.testing.assert_array_equal(sweep_1d, sweep.timeSignal[:, 0])
        np.testing.assert_array_equal(rec_1d, recording.timeSignal[:, 0])

    def test_portaudio_error_raises_runtime_error(self):
        mock_pytta = sys.modules["pytta"]
        mock_pytta.reset_mock()

        sweep = MagicMock()
        sweep.timeSignal = np.zeros((4800, 1))

        class PortAudioError(Exception):
            pass

        mock_pytta.PlayRecMeasure.return_value.run.side_effect = PortAudioError("no device")

        strategy = USBPlayback()
        with pytest.raises(RuntimeError, match="Audio device error"):
            strategy.play_and_record(sweep, 48000, 1, 1)


class TestHDMIPlayback:
    def test_happy_path(self):
        mock_sd = sys.modules["sounddevice"]
        mock_sd.reset_mock()

        sweep = MagicMock()
        sweep.timeSignal = np.random.default_rng(42).standard_normal((4800, 1))

        rec_result = np.random.default_rng(99).standard_normal((4800, 1)).astype(np.float32)
        mock_sd.rec.return_value = rec_result

        strategy = HDMIPlayback()
        sweep_1d, rec_1d = strategy.play_and_record(sweep, 48000, 1, 1)

        mock_sd.rec.assert_called_once()
        mock_sd.play.assert_called_once()
        mock_sd.wait.assert_called_once()

        assert sweep_1d.shape == (4800,)
        assert rec_1d.shape == (4800,)
        assert rec_1d.dtype == np.float64

        # Verify sd.play was called with int16 data
        play_args = mock_sd.play.call_args
        played_arr = play_args[0][0]
        assert played_arr.dtype == np.int16
