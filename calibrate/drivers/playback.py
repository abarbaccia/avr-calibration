"""Playback strategies for measurement sweep play+record.

Two strategies:
  USBPlayback  — PyTTa PlayRecMeasure (float32 duplex, both devices support it)
  HDMIPlayback — split sd.rec() + sd.play() (HDMI only supports int16 output)

Both return (sweep_1d, rec_1d) numpy arrays for deconvolution.
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class PlaybackStrategy(Protocol):
    """Protocol for sweep play+record strategies."""

    def play_and_record(
        self,
        sweep,  # PyTTa SignalObj
        sample_rate: int,
        in_channel: int,
        out_channel: int,
    ) -> tuple:
        """Play sweep and record response. Returns (sweep_1d, rec_1d) float64 arrays."""
        ...


class USBPlayback:
    """PyTTa PlayRecMeasure — float32 duplex via USB audio device."""

    def play_and_record(self, sweep, sample_rate, in_channel, out_channel):
        import pytta

        measurement = pytta.PlayRecMeasure(
            excitation=sweep,
            inChannels=[in_channel],
            outChannels=[out_channel],
        )
        try:
            recording = measurement.run()
        except Exception as exc:
            if "PortAudioError" in type(exc).__name__ or "PortAudio" in str(exc):
                raise RuntimeError(f"Audio device error during measurement: {exc}") from exc
            raise

        sweep_1d = sweep.timeSignal[:, 0]
        rec_1d = recording.timeSignal[:, 0]
        return sweep_1d, rec_1d


class HDMIPlayback:
    """Split sd.rec() + sd.play() with int16 output for HDMI.

    Outputs a multi-channel buffer so the sweep lands on the correct HDMI
    channel (typically LFE = channel 3 in the FL,FR,LFE,FC mapping).
    ``out_channel`` is 1-based to match PyTTa convention.
    """

    # HDMI requires at least 4 channels to access the LFE slot (FL,FR,LFE,FC).
    HDMI_CHANNELS = 4

    # Extra seconds to record beyond sweep length, to capture HDMI latency
    # (HDMI → Denon processing → sub pre-out → miniDSP → acoustic → UMIK).
    TAIL_SECONDS = 2.0

    def play_and_record(self, sweep, sample_rate, in_channel, out_channel):
        import time

        import numpy as np
        import sounddevice as sd

        sweep_array = sweep.timeSignal[:, 0].astype(np.float32)
        n_samples = len(sweep_array)
        tail_samples = int(self.TAIL_SECONDS * sample_rate)
        rec_samples = n_samples + tail_samples

        # Build multi-channel output with sweep on the target channel only
        ch_idx = out_channel - 1  # convert 1-based → 0-based
        n_out = max(self.HDMI_CHANNELS, ch_idx + 1)
        multi = np.zeros((n_samples, n_out), dtype=np.float32)
        multi[:, ch_idx] = sweep_array
        hdmi_arr = (np.clip(multi, -1.0, 1.0) * 32767).astype(np.int16)

        # Use InputStream callback + sd.play() to avoid device contention.
        # sd.rec() + sd.play() on different ALSA devices can cause the
        # recording stream to drop when the playback stream opens.
        rec_buf = np.zeros((rec_samples, 1), dtype=np.float32)
        write_pos = [0]

        def _input_cb(indata, frames, time_info, status):
            end = write_pos[0] + frames
            if end <= len(rec_buf):
                rec_buf[write_pos[0]:end] = indata
            write_pos[0] = end

        in_dev = sd.default.device[0]
        with sd.InputStream(device=in_dev, samplerate=sample_rate,
                            channels=1, dtype="float32",
                            callback=_input_cb, blocksize=1024):
            sd.play(hdmi_arr, samplerate=sample_rate)
            sd.wait()
            time.sleep(self.TAIL_SECONDS)

        sweep_1d = sweep.timeSignal[:, 0]
        rec_1d = rec_buf[:write_pos[0], 0].astype(np.float64)
        return sweep_1d, rec_1d


def playback_for_route(route: str) -> PlaybackStrategy:
    """Factory: return the right playback strategy for the configured route."""
    if route == "hdmi":
        return HDMIPlayback()
    return USBPlayback()
