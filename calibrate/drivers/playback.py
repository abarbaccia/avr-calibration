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
    """Split sd.rec() + sd.play() with int16 output for HDMI."""

    def play_and_record(self, sweep, sample_rate, in_channel, out_channel):
        import numpy as np
        import sounddevice as sd

        sweep_array = sweep.timeSignal[:, 0].astype(np.float32)
        n_samples = len(sweep_array)

        rec_buf = sd.rec(n_samples, samplerate=sample_rate, channels=1, dtype="float32")
        hdmi_arr = (np.clip(sweep_array, -1.0, 1.0) * 32767).astype(np.int16).reshape(-1, 1)
        sd.play(hdmi_arr, samplerate=sample_rate)
        sd.wait()

        sweep_1d = sweep.timeSignal[:, 0]
        rec_1d = rec_buf[:, 0].astype(np.float64)
        return sweep_1d, rec_1d


def playback_for_route(route: str) -> PlaybackStrategy:
    """Factory: return the right playback strategy for the configured route."""
    if route == "hdmi":
        return HDMIPlayback()
    return USBPlayback()
