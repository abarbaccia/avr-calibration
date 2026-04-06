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
    """Explicit InputStream + OutputStream for HDMI play + mic record.

    Uses separate streams to avoid a sounddevice bug where sd.rec(float32)
    + sd.play(int16) corrupts the recording buffer with playback data.

    Places the sweep on the specified out_channel (1-based, e.g. 4 = LFE in
    5.1 layout) within a multi-channel HDMI buffer. Other channels are silent.
    """

    def play_and_record(self, sweep, sample_rate, in_channel, out_channel):
        import numpy as np
        import sounddevice as sd

        sweep_array = sweep.timeSignal[:, 0].astype(np.float32)
        n_samples = len(sweep_array)

        # Build multi-channel buffer with sweep on the target channel.
        # HDMI requires standard channel counts (2, 6, or 8).
        # 5.1 layout: 1=FL, 2=FR, 3=LFE, 4=C, 5=RL, 6=RR (varies by sink).
        standard_counts = [2, 6, 8]
        n_channels = next(c for c in standard_counts if c >= out_channel)
        hdmi_buf = np.zeros((n_samples, n_channels), dtype=np.int16)
        ch_idx = out_channel - 1  # convert 1-based to 0-based
        hdmi_buf[:, ch_idx] = (np.clip(sweep_array, -1.0, 1.0) * 32767).astype(np.int16)

        in_dev = int(sd.default.device[0])
        out_dev = int(sd.default.device[1])

        rec_data = np.zeros((n_samples, 1), dtype=np.float32)
        rec_pos = [0]

        def _rec_callback(indata, frames, time_info, status):
            end = min(rec_pos[0] + frames, n_samples)
            count = end - rec_pos[0]
            rec_data[rec_pos[0]:end] = indata[:count]
            rec_pos[0] = end

        in_stream = sd.InputStream(
            device=in_dev, samplerate=sample_rate,
            channels=1, dtype="float32", callback=_rec_callback,
        )
        out_stream = sd.OutputStream(
            device=out_dev, samplerate=sample_rate,
            channels=n_channels, dtype="int16",
        )

        in_stream.start()
        out_stream.start()
        out_stream.write(hdmi_buf)
        out_stream.stop()
        # Drain remaining mic samples after playback ends
        import time
        time.sleep(0.5)
        in_stream.stop()
        in_stream.close()
        out_stream.close()

        sweep_1d = sweep.timeSignal[:, 0]
        rec_1d = rec_data[:rec_pos[0], 0].astype(np.float64)
        return sweep_1d, rec_1d


def playback_for_route(route: str) -> PlaybackStrategy:
    """Factory: return the right playback strategy for the configured route."""
    if route == "hdmi":
        return HDMIPlayback()
    return USBPlayback()
