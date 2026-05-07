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
    """Explicit InputStream + OutputStream for USB sweep play + mic record.

    Uses separate streams (same pattern as HDMIPlayback) to guarantee that
    recording starts BEFORE playback. This ensures the first 500 ms of the
    recording captures the pre-sweep noise floor rather than mid-sweep signal
    — critical for the SNR validation gate.

    sd.playrec() with two different USB devices (UMIK on hw:3, miniDSP on
    hw:2) uses non-synchronized streams; if recording starts late, the floor
    window captures mid-sweep energy and SNR collapses to ~0 dB.
    """

    # Pre-delay: recording starts this many seconds before playback.
    # Must exceed the floor window (500ms) by a comfortable margin so no
    # sweep energy contaminates the noise floor estimate.
    # 1.0s > 500ms floor window, with 500ms headroom for USB latency variance.
    PRE_DELAY_S: float = 1.0

    # Post-silence appended to the output buffer so the full sweep response
    # (including room reverb tail) is captured before the input stream stops.
    POST_DELAY_S: float = 0.5

    def play_and_record(self, sweep, sample_rate, in_channel, out_channel):
        import time as _time

        import numpy as np
        import sounddevice as sd

        sweep_array = sweep.timeSignal[:, 0].astype(np.float32)
        n_samples = len(sweep_array)

        in_dev = int(sd.default.device[0])
        out_dev = int(sd.default.device[1])

        pre_samples = int(self.PRE_DELAY_S * sample_rate)
        post_samples = int(self.POST_DELAY_S * sample_rate)

        # Output buffer: sweep only (pre-delay handled by sleeping before start).
        # Append post-silence so the output stream stays open long enough for
        # the full room response to be captured by the input stream.
        n_out_ch = max(2, out_channel)
        sweep_buf = np.zeros((n_samples, n_out_ch), dtype=np.float32)
        sweep_buf[:, out_channel - 1] = sweep_array  # 1-based → 0-based
        post_silence = np.zeros((post_samples, n_out_ch), dtype=np.float32)
        out_buf = np.vstack([sweep_buf, post_silence])

        # Recording buffer: pre_delay + sweep + post_delay
        # CRITICAL: must be larger than n_samples. With a 1s pre-delay, the
        # sweep starts 48000 samples into the recording. A buffer sized to only
        # n_samples would fill up before the sweep finishes, truncating the
        # recording and corrupting the cross-correlation / SNR calculation.
        rec_n = pre_samples + n_samples + post_samples
        rec_buf = np.zeros((rec_n, 1), dtype=np.float32)
        rec_pos = [0]

        def _rec_callback(indata, frames, time_info, status):
            end = min(rec_pos[0] + frames, rec_n)
            count = end - rec_pos[0]
            if count > 0:
                rec_buf[rec_pos[0]:end] = indata[:count, in_channel - 1 : in_channel]
            rec_pos[0] = end

        in_stream = sd.InputStream(
            device=in_dev,
            samplerate=sample_rate,
            channels=in_channel,
            dtype="float32",
            callback=_rec_callback,
        )
        out_stream = sd.OutputStream(
            device=out_dev,
            samplerate=sample_rate,
            channels=n_out_ch,
            dtype="float32",
        )

        # Sequence: recording FIRST, then sleep PRE_DELAY_S, then play.
        # The sleep guarantees the first 500ms of recording (the noise floor
        # window) contains only pre-sweep silence regardless of USB playback
        # buffer start latency (~50-200ms). out_stream.write() is blocking and
        # returns when all samples have been consumed by the OS audio buffer.
        try:
            in_stream.start()
            _time.sleep(self.PRE_DELAY_S)
            out_stream.start()
            out_stream.write(out_buf)   # sweep + post_silence — blocking
            out_stream.stop()
            out_stream.close()

            # Give the OS and PortAudio a moment to flush the last mic callback
            # frames before stopping the input stream.
            _time.sleep(0.1)
            in_stream.stop()
            in_stream.close()
        except Exception as exc:
            # Clean up streams on any audio device error, then surface as
            # RuntimeError so callers get a consistent exception type.
            for s in (in_stream, out_stream):
                try:
                    s.stop()
                except Exception:
                    pass
                try:
                    s.close()
                except Exception:
                    pass
            raise RuntimeError(f"Audio device error: {exc}") from exc

        n_recorded = rec_pos[0]
        sweep_1d = sweep.timeSignal[:, 0]
        rec_1d = rec_buf[:n_recorded, 0].astype(np.float64)

        # Debug: log recording stats to diagnose SNR failures.
        # floor = first 500ms; sig = everything after (includes pre-delay gap).
        if len(rec_1d) > 0:
            peak = float(np.max(np.abs(rec_1d)))
            floor_n = min(int(0.5 * sample_rate), len(rec_1d) // 2)
            floor_rms = float(np.sqrt(np.mean(rec_1d[:floor_n] ** 2)))
            sig_rms = float(np.sqrt(np.mean(rec_1d[floor_n:] ** 2)))
            log.info(
                "USBPlayback: pre=%.0fms n_sweep=%d rec_n=%d n_recorded=%d "
                "in_dev=%d out_dev=%d peak=%.1f dBFS floor=%.1f dBFS "
                "sig=%.1f dBFS SNR=%.1f dB",
                self.PRE_DELAY_S * 1000, n_samples, rec_n, n_recorded,
                in_dev, out_dev,
                20 * np.log10(peak + 1e-12),
                20 * np.log10(floor_rms + 1e-12),
                20 * np.log10(sig_rms + 1e-12),
                20 * np.log10(sig_rms / (floor_rms + 1e-12)),
            )
        else:
            log.warning("USBPlayback: recording is empty (0 samples captured)")

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
        # Denon X3800H layout: 1=FL, 2=FR, 3=LFE, 4=C, 5=SL, 6=SR.
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

        try:
            out_stream = sd.OutputStream(
                device=out_dev, samplerate=sample_rate,
                channels=n_channels, dtype="int16",
            )
        except Exception:
            in_stream.close()
            raise

        try:
            in_stream.start()
            out_stream.start()
            out_stream.write(hdmi_buf)
            out_stream.stop()
            # Drain remaining mic samples after playback ends
            import time
            time.sleep(0.5)
            in_stream.stop()
        finally:
            in_stream.close()
            out_stream.close()

        sweep_1d = sweep.timeSignal[:, 0]
        rec_1d = rec_data[:rec_pos[0], 0].astype(np.float64)
        return sweep_1d, rec_1d


class MultichannelPlayback:
    """Play pre-built numpy multichannel buffers via HDMI + record from UMIK.

    Unlike HDMIPlayback (which accepts PyTTa SignalObj for sweep deconvolution),
    this class accepts pre-built int16 numpy arrays for steady-state multitone
    playback.  Used by the headroom / amp clipping test.
    """

    PRE_DELAY_S: float = 0.5
    POST_DELAY_S: float = 0.5

    def play_and_record(
        self,
        output_buffer,  # np.ndarray int16, shape (n_samples, n_channels)
        sample_rate: int,
        in_device: int | None = None,
        out_device: int | None = None,
    ) -> tuple:
        """Play multichannel buffer via HDMI and record from UMIK.

        Returns (recording, n_recorded) where recording is a float64 1D array.
        """
        import time as _time

        import numpy as np
        import sounddevice as sd

        n_samples, n_channels = output_buffer.shape

        if in_device is None:
            in_device = int(sd.default.device[0])
        if out_device is None:
            out_device = int(sd.default.device[1])

        pre_samples = int(self.PRE_DELAY_S * sample_rate)
        post_samples = int(self.POST_DELAY_S * sample_rate)
        rec_n = pre_samples + n_samples + post_samples
        rec_buf = np.zeros((rec_n, 1), dtype=np.float32)
        rec_pos = [0]

        def _rec_callback(indata, frames, time_info, status):
            end = min(rec_pos[0] + frames, rec_n)
            count = end - rec_pos[0]
            if count > 0:
                rec_buf[rec_pos[0]:end] = indata[:count, :1]
            rec_pos[0] = end

        in_stream = sd.InputStream(
            device=in_device, samplerate=sample_rate,
            channels=1, dtype="float32", callback=_rec_callback,
        )
        out_stream = sd.OutputStream(
            device=out_device, samplerate=sample_rate,
            channels=n_channels, dtype="int16",
        )

        try:
            in_stream.start()
            _time.sleep(self.PRE_DELAY_S)
            out_stream.start()
            out_stream.write(output_buffer)
            out_stream.stop()
            out_stream.close()
            _time.sleep(self.POST_DELAY_S)
            in_stream.stop()
            in_stream.close()
        except Exception as exc:
            for s in (in_stream, out_stream):
                try:
                    s.stop()
                except Exception:
                    pass
                try:
                    s.close()
                except Exception:
                    pass
            raise RuntimeError(f"Audio device error: {exc}") from exc

        n_recorded = rec_pos[0]
        # Trim to playback-aligned region (skip pre-delay, keep up to n_samples)
        start = pre_samples
        end = min(start + n_samples, n_recorded)
        recording = rec_buf[start:end, 0].astype(np.float64)

        if len(recording) > 0:
            peak = float(np.max(np.abs(recording)))
            log.info(
                "MultichannelPlayback: n_samples=%d n_ch=%d rec_n=%d "
                "n_recorded=%d peak=%.1f dBFS",
                n_samples, n_channels, rec_n, n_recorded,
                20 * np.log10(peak + 1e-12),
            )
        else:
            log.warning("MultichannelPlayback: recording is empty")

        return recording, n_recorded


class HDMIAplayPlayback:
    """HDMI sweep playback via the ``aplay`` subprocess (direct ALSA), capture via PortAudio.

    Why this exists:
        Inside the avr-calibration container on the Pi 5 (Docker, ``--privileged``,
        ``--network=host``) PortAudio enumerates only the ALSA Loopback PCMs and
        does NOT see ``vc4hdmi0`` even though ``aplay -L`` and ``speaker-test
        -D hdmi:CARD=vc4hdmi0,DEV=0`` work cleanly. The result is that the prior
        ``HDMIPlayback`` route silently fell back to a Loopback device, the sweep
        never reached the AVR, and cross-correlation reported "Sweep not detected."

        ``aplay`` invokes the ALSA hw device directly, bypassing PortAudio's
        broken enumeration. Capture stays on PortAudio/sounddevice (the UMIK
        path works fine).

    Sequence (mirrors ``USBPlayback``):
        recording-first → sleep PRE_DELAY_S → spawn aplay → wait for aplay to
        exit → small POST_DELAY_S settle → stop recording. The 1 s pre-delay
        guarantees the noise-floor window in ``validate_recording`` lands on
        pre-sweep silence.

    Multi-channel layout:
        Builds an N-channel S16_LE buffer and writes the sweep onto channel
        ``out_channel - 1`` (1-based input). Other channels are silent. So
        ``out_channel=1, channels=8`` puts the sweep on FL only and zero-fills
        the rest of the 8-ch HDMI stream.
    """

    PRE_DELAY_S: float = 1.0
    """Seconds of recording before playback starts. Mirrors ``USBPlayback`` so
    the deconvolution alignment math (sweep-pad shared anchor) is identical."""

    POST_DELAY_S: float = 0.5
    """Seconds of trailing capture after aplay exits, to capture the room reverb tail."""

    def __init__(self, alsa_device: str, channels: int = 8) -> None:
        if not alsa_device:
            raise ValueError("HDMIAplayPlayback requires a non-empty ALSA device string")
        if channels < 1:
            raise ValueError(f"channels must be >= 1, got {channels}")
        self.alsa_device = alsa_device
        self.channels = int(channels)

    def play_and_record(self, sweep, sample_rate, in_channel, out_channel):
        import subprocess
        import time as _time

        import numpy as np
        import sounddevice as sd

        sweep_array = sweep.timeSignal[:, 0].astype(np.float32)
        n_samples = len(sweep_array)
        n_channels = max(self.channels, out_channel)

        # Build N-channel int16 PCM with sweep on out_channel-1, others silent.
        sweep_int16 = (np.clip(sweep_array, -1.0, 1.0) * 32767).astype(np.int16)
        out_buf = np.zeros((n_samples, n_channels), dtype=np.int16)
        out_buf[:, out_channel - 1] = sweep_int16
        pcm_bytes = out_buf.tobytes()

        in_dev = int(sd.default.device[0])

        pre_samples = int(self.PRE_DELAY_S * sample_rate)
        post_samples = int(self.POST_DELAY_S * sample_rate)
        rec_n = pre_samples + n_samples + post_samples
        rec_buf = np.zeros((rec_n, 1), dtype=np.float32)
        rec_pos = [0]

        def _rec_callback(indata, frames, time_info, status):
            end = min(rec_pos[0] + frames, rec_n)
            count = end - rec_pos[0]
            if count > 0:
                rec_buf[rec_pos[0]:end] = indata[:count, in_channel - 1 : in_channel]
            rec_pos[0] = end

        in_stream = sd.InputStream(
            device=in_dev,
            samplerate=sample_rate,
            channels=in_channel,
            dtype="float32",
            callback=_rec_callback,
        )

        # Force the chmap that has FC at index 4. Without this, the
        # vc4-hdmi driver picks `FL,FR,LFE,NA,RC,NA` for AVRs whose EDID
        # advertises back-center — that maps PCM ch 4 to NA, so any
        # Center sweep goes nowhere (AVR drops it or routes by fallback
        # to sub). With --chmap=FL,FR,LFE,FC,RL,RR explicit, ch 4 = FC,
        # ch 5 = RL, ch 6 = RR — all reachable. Verified 2026-05-07
        # against amixer numid=2 reading values 3,4,8,7,5,6,0,0.
        chmap_for_channels = {
            2: "FL,FR",
            4: "FL,FR,LFE,FC",
            6: "FL,FR,LFE,FC,RL,RR",
        }
        chmap_arg = chmap_for_channels.get(n_channels)
        aplay_cmd = [
            "aplay",
            "-D", self.alsa_device,
            "-c", str(n_channels),
            "-r", str(sample_rate),
            "-f", "S16_LE",
        ]
        if chmap_arg is not None:
            aplay_cmd.extend(["--chmap", chmap_arg])
        aplay_cmd += [
            "-t", "raw",
            "-q",  # quiet — don't pollute MCP logs with progress chatter
            "-",
        ]

        proc = None
        try:
            in_stream.start()
            _time.sleep(self.PRE_DELAY_S)
            proc = subprocess.Popen(
                aplay_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = proc.communicate(
                    input=pcm_bytes,
                    timeout=max(30.0, n_samples / sample_rate + 10.0),
                )
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise RuntimeError(
                    f"aplay -D {self.alsa_device!r} timed out — HDMI sink may be unplugged"
                )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"aplay -D {self.alsa_device!r} failed (rc={proc.returncode}): "
                    f"{stderr_b.decode('utf-8', errors='replace').strip()}"
                )
            # Drain any trailing room reverb tail.
            _time.sleep(self.POST_DELAY_S)
        finally:
            try:
                in_stream.stop()
            except Exception:
                pass
            try:
                in_stream.close()
            except Exception:
                pass
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass

        n_recorded = rec_pos[0]
        sweep_1d = sweep.timeSignal[:, 0]
        rec_1d = rec_buf[:n_recorded, 0].astype(np.float64)

        if len(rec_1d) > 0:
            peak = float(np.max(np.abs(rec_1d)))
            floor_n = min(int(0.5 * sample_rate), len(rec_1d) // 2)
            floor_rms = float(np.sqrt(np.mean(rec_1d[:floor_n] ** 2)))
            sig_rms = float(np.sqrt(np.mean(rec_1d[floor_n:] ** 2)))
            log.info(
                "HDMIAplayPlayback: device=%s ch=%d/%d pre=%.0fms n_sweep=%d "
                "rec_n=%d n_recorded=%d peak=%.1f dBFS floor=%.1f dBFS "
                "sig=%.1f dBFS SNR=%.1f dB",
                self.alsa_device, out_channel, n_channels,
                self.PRE_DELAY_S * 1000, n_samples, rec_n, n_recorded,
                20 * np.log10(peak + 1e-12),
                20 * np.log10(floor_rms + 1e-12),
                20 * np.log10(sig_rms + 1e-12),
                20 * np.log10(sig_rms / (floor_rms + 1e-12)),
            )
        else:
            log.warning("HDMIAplayPlayback: recording is empty (0 samples captured)")

        return sweep_1d, rec_1d


def playback_for_route(
    route: str,
    *,
    hdmi_alsa_device: str | None = None,
    hdmi_channels: int = 6,
) -> PlaybackStrategy:
    """Factory: return the right playback strategy for the configured route.

    HDMI path:
      - ``hdmi_alsa_device`` set → ``HDMIAplayPlayback`` (direct ALSA via
        the ``aplay`` subprocess; bypasses PortAudio's broken vc4hdmi0
        enumeration inside containers).
      - ``hdmi_alsa_device`` None → legacy PortAudio-based ``HDMIPlayback``,
        kept for back-compat with callers that don't yet plumb a device.
    """
    if route == "hdmi":
        if hdmi_alsa_device:
            return HDMIAplayPlayback(
                alsa_device=hdmi_alsa_device, channels=hdmi_channels,
            )
        return HDMIPlayback()
    return USBPlayback()
