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
  └── [TESTED] happy path — opens explicit InputStream/OutputStream, returns arrays
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch, call

import numpy as np
import pytest

from calibrate.drivers.playback import (
    HDMIAplayPlayback,
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
        """USBPlayback records via sd.InputStream callback, plays via sd.OutputStream."""
        mock_sd = sys.modules["sounddevice"]
        mock_sd.reset_mock()

        n_samples = 4800
        sweep = MagicMock()
        sweep.timeSignal = np.random.default_rng(42).standard_normal((n_samples, 1))
        mock_sd.default.device = [0, 1]

        in_stream = MagicMock()
        out_stream = MagicMock()
        captured_callback = [None]

        def fake_input_stream(**kwargs):
            captured_callback[0] = kwargs.get("callback")
            return in_stream

        mock_sd.InputStream = MagicMock(side_effect=fake_input_stream)
        mock_sd.OutputStream = MagicMock(return_value=out_stream)

        # Simulate mic data arriving when out_stream.write() plays the sweep.
        pre_s = int(USBPlayback.PRE_DELAY_S * 48000)
        post_s = int(USBPlayback.POST_DELAY_S * 48000)
        rec_n = pre_s + n_samples + post_s
        fake_mic = np.random.default_rng(99).standard_normal((rec_n, 1)).astype(np.float32)

        def fake_write(buf):
            if captured_callback[0]:
                captured_callback[0](fake_mic, rec_n, None, None)

        out_stream.write = MagicMock(side_effect=fake_write)

        strategy = USBPlayback()
        with patch("time.sleep"):
            sweep_1d, rec_1d = strategy.play_and_record(sweep, 48000, 1, 1)

        mock_sd.InputStream.assert_called_once()
        mock_sd.OutputStream.assert_called_once()
        in_stream.start.assert_called_once()
        out_stream.start.assert_called_once()
        out_stream.write.assert_called_once()

        assert sweep_1d.shape == (n_samples,)
        assert len(rec_1d) > 0
        assert rec_1d.dtype == np.float64
        np.testing.assert_array_equal(sweep_1d, sweep.timeSignal[:, 0])

    def test_portaudio_error_raises_runtime_error(self):
        """Any audio device error from stream operations is re-raised as RuntimeError."""
        mock_sd = sys.modules["sounddevice"]
        mock_sd.reset_mock()

        sweep = MagicMock()
        sweep.timeSignal = np.zeros((4800, 1))
        mock_sd.default.device = [0, 1]

        in_stream = MagicMock()
        in_stream.start.side_effect = Exception("no device")
        mock_sd.InputStream = MagicMock(return_value=in_stream)
        mock_sd.OutputStream = MagicMock(return_value=MagicMock())

        strategy = USBPlayback()
        with patch("time.sleep"), pytest.raises(RuntimeError, match="Audio device error"):
            strategy.play_and_record(sweep, 48000, 1, 1)


class TestHDMIPlayback:
    def test_happy_path(self):
        mock_sd = sys.modules["sounddevice"]
        mock_sd.reset_mock()

        n_samples = 4800
        sweep = MagicMock()
        sweep.timeSignal = np.random.default_rng(42).standard_normal((n_samples, 1))

        # Mock the default device tuple
        mock_sd.default.device = [0, 1]

        # Capture the InputStream callback so we can feed it fake mic data
        in_stream = MagicMock()
        out_stream = MagicMock()
        captured_callback = [None]

        def fake_input_stream(**kwargs):
            captured_callback[0] = kwargs.get("callback")
            return in_stream

        def fake_output_stream(**kwargs):
            return out_stream

        mock_sd.InputStream = MagicMock(side_effect=fake_input_stream)
        mock_sd.OutputStream = MagicMock(side_effect=fake_output_stream)

        # Simulate the OutputStream.write triggering the callback with mic data
        fake_mic = np.random.default_rng(99).standard_normal((n_samples, 1)).astype(np.float32)

        def fake_write(buf):
            # Feed fake mic data via the captured callback
            if captured_callback[0]:
                captured_callback[0](fake_mic, n_samples, None, None)

        out_stream.write = MagicMock(side_effect=fake_write)

        strategy = HDMIPlayback()
        sweep_1d, rec_1d = strategy.play_and_record(sweep, 48000, 1, 1)

        mock_sd.InputStream.assert_called_once()
        mock_sd.OutputStream.assert_called_once()
        in_stream.start.assert_called_once()
        out_stream.start.assert_called_once()
        out_stream.write.assert_called_once()

        assert sweep_1d.shape == (n_samples,)
        assert rec_1d.shape == (n_samples,)
        assert rec_1d.dtype == np.float64

        # Verify OutputStream was opened with int16 dtype
        out_kwargs = mock_sd.OutputStream.call_args[1]
        assert out_kwargs["dtype"] == "int16"

        # Verify the write buffer is int16
        write_args = out_stream.write.call_args[0]
        assert write_args[0].dtype == np.int16


class TestHDMIAplayPlayback:
    """Direct-ALSA HDMI playback via the aplay subprocess."""

    def _make_sweep(self, n_samples: int = 4800) -> MagicMock:
        sweep_data = np.linspace(-0.5, 0.5, n_samples, dtype=np.float32).reshape(-1, 1)
        sweep = MagicMock()
        sweep.timeSignal = sweep_data
        return sweep

    def _patch_sd(self):
        """Stub out sd.InputStream so callbacks don't actually run."""
        mock_sd = sys.modules["sounddevice"]
        mock_sd.reset_mock()
        in_stream = MagicMock()
        mock_sd.InputStream = MagicMock(return_value=in_stream)
        mock_sd.default.device = (1, 0)
        return mock_sd, in_stream

    def test_factory_returns_aplay_when_device_provided(self):
        s = playback_for_route("hdmi", hdmi_alsa_device="hdmi:CARD=vc4hdmi0,DEV=0", hdmi_channels=8)
        assert isinstance(s, HDMIAplayPlayback)
        assert s.alsa_device == "hdmi:CARD=vc4hdmi0,DEV=0"
        assert s.channels == 8

    def test_factory_returns_legacy_when_no_device(self):
        # Backward-compat: callers that don't pass a device still get HDMIPlayback.
        assert isinstance(playback_for_route("hdmi"), HDMIPlayback)

    def test_constructor_validates_device(self):
        with pytest.raises(ValueError, match="non-empty"):
            HDMIAplayPlayback(alsa_device="")
        with pytest.raises(ValueError, match="channels"):
            HDMIAplayPlayback(alsa_device="hdmi:CARD=x,DEV=0", channels=0)

    def test_play_and_record_invokes_aplay_with_correct_args(self):
        """aplay is called with the configured device, S16_LE, channel count, sample rate."""
        mock_sd, in_stream = self._patch_sd()
        sweep = self._make_sweep(n_samples=4800)

        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = MagicMock(return_value=(b"", b""))
        proc.poll = MagicMock(return_value=0)

        strategy = HDMIAplayPlayback(alsa_device="hdmi:CARD=vc4hdmi0,DEV=0", channels=8)
        with (
            patch("subprocess.Popen", return_value=proc) as MockPopen,
            patch("time.sleep"),
        ):
            sweep_1d, rec_1d = strategy.play_and_record(sweep, 48000, 1, 1)

        # aplay command shape
        cmd = MockPopen.call_args[0][0]
        assert cmd[0] == "aplay"
        assert "-D" in cmd
        assert cmd[cmd.index("-D") + 1] == "hdmi:CARD=vc4hdmi0,DEV=0"
        assert cmd[cmd.index("-c") + 1] == "8"
        assert cmd[cmd.index("-r") + 1] == "48000"
        assert cmd[cmd.index("-f") + 1] == "S16_LE"
        assert cmd[cmd.index("-t") + 1] == "raw"
        assert cmd[-1] == "-"  # stdin

        # The sweep bytes were written to aplay's stdin via communicate(input=...)
        kwargs = proc.communicate.call_args.kwargs
        assert "input" in kwargs
        pcm_bytes = kwargs["input"]
        # 4800 frames × 8 channels × 2 bytes/sample
        assert len(pcm_bytes) == 4800 * 8 * 2

        # Capture path still ran via PortAudio
        mock_sd.InputStream.assert_called_once()
        in_stream.start.assert_called_once()

        assert sweep_1d.shape == (4800,)
        assert rec_1d.dtype == np.float64

    def test_multichannel_interleave_only_target_channel_has_signal(self):
        """sweep on channel 3 of 8 puts non-zero only on channel 3, zeros elsewhere."""
        mock_sd, in_stream = self._patch_sd()
        sweep = self._make_sweep(n_samples=480)

        captured: dict = {}
        def _fake_communicate(input=None, timeout=None):
            captured["input"] = input
            return (b"", b"")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = MagicMock(side_effect=_fake_communicate)
        proc.poll = MagicMock(return_value=0)

        strategy = HDMIAplayPlayback(alsa_device="hdmi:CARD=vc4hdmi0,DEV=0", channels=8)
        with (
            patch("subprocess.Popen", return_value=proc),
            patch("time.sleep"),
        ):
            strategy.play_and_record(sweep, 48000, 1, 3)

        pcm = np.frombuffer(captured["input"], dtype=np.int16).reshape(-1, 8)
        # Channel 3 (1-based) → column index 2 has signal; all others zero.
        for ch in range(8):
            col = pcm[:, ch]
            if ch == 2:
                assert np.any(col != 0), "target channel must have signal"
            else:
                assert np.all(col == 0), f"channel {ch} must be silent (got {col[col != 0][:4]})"

    def test_aplay_nonzero_returncode_raises(self):
        mock_sd, _ = self._patch_sd()
        sweep = self._make_sweep(n_samples=480)

        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = MagicMock(return_value=(b"", b"aplay: device busy"))
        proc.poll = MagicMock(return_value=1)

        strategy = HDMIAplayPlayback(alsa_device="hdmi:CARD=vc4hdmi0,DEV=0", channels=2)
        with (
            patch("subprocess.Popen", return_value=proc),
            patch("time.sleep"),
            pytest.raises(RuntimeError, match="aplay.*device busy"),
        ):
            strategy.play_and_record(sweep, 48000, 1, 1)
