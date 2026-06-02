"""Playback strategies for measurement sweep play+record.

Three strategies:
  USBPlayback       — PyTTa PlayRecMeasure (float32 duplex, both devices support it)
  HDMIPlayback      — split sd.rec() + sd.play() (legacy, HDMI only supports int16 output)
  HDMIPwCatPlayback — pw-cat native PipeWire playback + pw-record native PipeWire capture

Both return (sweep_1d, rec_1d) numpy arrays for deconvolution.
"""

from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


def _start_pw_record(
    node: str,
    sample_rate: int,
    channels: int = 1,
    channel_map: str | None = None,
) -> tuple:
    """Start a pw-record subprocess reading from a PipeWire source node.

    Returns (proc, chunks, reader_thread).  Caller must call _stop_pw_record
    when done.  pw-record writes raw f32le interleaved samples to stdout.

    channel_map: explicit PipeWire channel map string (e.g. "AUX0,AUX1,AUX2").
    Required for multichannel devices whose ports are named AUX0…AUXN rather
    than FL/FR — without it PipeWire only maps FL/FR and silences the rest.
    """
    import subprocess
    import threading

    # Multichannel devices (>2 ch) name their ports AUX0-AUXN. Without an
    # explicit channel map pw-record only maps FL/FR and silences the rest.
    effective_map = channel_map if channel_map is not None else (
        _aux_channel_map(channels) if channels > 2 else None
    )
    cmd = [
        "pw-record",
        "--target", node,
        "--channels", str(channels),
        "--rate", str(sample_rate),
        "--format", "f32",
    ]
    if effective_map:
        cmd += ["--channel-map", effective_map]
    cmd.append("-")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    chunks: list[bytes] = []

    def _read():
        try:
            while True:
                data = proc.stdout.read(4096)
                if not data:
                    break
                chunks.append(data)
        except Exception:
            pass

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    return proc, chunks, t


def _stop_pw_record(proc, reader_thread) -> None:
    """Terminate pw-record and wait for the reader thread to drain."""
    try:
        proc.terminate()
        proc.wait(timeout=5.0)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    reader_thread.join(timeout=3.0)


def _aux_channel_map(n: int) -> str:
    """Return a PipeWire AUX channel-map string for n channels (AUX0,AUX1,…,AUX{n-1}).

    Multichannel devices (e.g. Focusrite Scarlett 18i20) name their ports AUX0-AUXN.
    Without an explicit map pw-record only maps FL/FR and silences all other channels.
    """
    return ",".join(f"AUX{i}" for i in range(n))


def _assemble_pw_recording(chunks: list) -> "np.ndarray":  # type: ignore[name-defined]
    """Concatenate pw-record stdout chunks into a float64 1D array."""
    import numpy as np
    raw = b"".join(chunks)
    if not raw:
        return np.zeros(0, dtype=np.float64)
    return np.frombuffer(raw, dtype=np.float32).astype(np.float64)


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

    def __init__(self, capture_pipewire_node: str | None = None) -> None:
        self.capture_pipewire_node = capture_pipewire_node

    def play_and_record(self, sweep, sample_rate, in_channel, out_channel):
        import time as _time

        import numpy as np
        import sounddevice as sd

        sweep_array = sweep.timeSignal[:, 0].astype(np.float32)
        n_samples = len(sweep_array)

        out_dev = int(sd.default.device[1])

        pre_samples = int(self.PRE_DELAY_S * sample_rate)
        post_samples = int(self.POST_DELAY_S * sample_rate)

        n_out_ch = max(2, out_channel)
        sweep_buf = np.zeros((n_samples, n_out_ch), dtype=np.float32)
        sweep_buf[:, out_channel - 1] = sweep_array  # 1-based → 0-based
        post_silence = np.zeros((post_samples, n_out_ch), dtype=np.float32)
        out_buf = np.vstack([sweep_buf, post_silence])

        sweep_1d = sweep.timeSignal[:, 0]
        out_stream = sd.OutputStream(
            device=out_dev,
            samplerate=sample_rate,
            channels=n_out_ch,
            dtype="float32",
        )

        if self.capture_pipewire_node:
            # Native PipeWire capture via pw-record subprocess.
            # Recording starts BEFORE the pre-delay sleep so the noise-floor
            # window (first 500ms) captures silence, not sweep energy.
            rec_proc, chunks, reader_t = _start_pw_record(
                self.capture_pipewire_node, sample_rate
            )
            try:
                _time.sleep(self.PRE_DELAY_S)
                out_stream.start()
                out_stream.write(out_buf)
                out_stream.stop()
                out_stream.close()
                _time.sleep(0.1)
            except Exception as exc:
                try:
                    out_stream.stop()
                except Exception:
                    pass
                try:
                    out_stream.close()
                except Exception:
                    pass
                _stop_pw_record(rec_proc, reader_t)
                raise RuntimeError(f"Audio device error: {exc}") from exc
            _stop_pw_record(rec_proc, reader_t)
            rec_1d = _assemble_pw_recording(chunks)
            n_recorded = len(rec_1d)
            rec_n = pre_samples + n_samples + post_samples
            log_prefix = f"USBPlayback[pw-record node={self.capture_pipewire_node}]"
        else:
            # PortAudio sd.InputStream capture (fallback when no PW node configured).
            in_dev = int(sd.default.device[0])
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
            try:
                in_stream.start()
                _time.sleep(self.PRE_DELAY_S)
                out_stream.start()
                out_stream.write(out_buf)
                out_stream.stop()
                out_stream.close()
                _time.sleep(0.1)
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
            rec_1d = rec_buf[:n_recorded, 0].astype(np.float64)
            log_prefix = f"USBPlayback[portaudio in_dev={in_dev}]"

        if len(rec_1d) > 0:
            peak = float(np.max(np.abs(rec_1d)))
            floor_n = min(int(0.5 * sample_rate), len(rec_1d) // 2)
            floor_rms = float(np.sqrt(np.mean(rec_1d[:floor_n] ** 2)))
            sig_rms = float(np.sqrt(np.mean(rec_1d[floor_n:] ** 2)))
            log.info(
                "%s: pre=%.0fms n_sweep=%d rec_n=%d n_recorded=%d "
                "out_dev=%d peak=%.1f dBFS floor=%.1f dBFS "
                "sig=%.1f dBFS SNR=%.1f dB",
                log_prefix,
                self.PRE_DELAY_S * 1000, n_samples, rec_n, n_recorded,
                out_dev,
                20 * np.log10(peak + 1e-12),
                20 * np.log10(floor_rms + 1e-12),
                20 * np.log10(sig_rms + 1e-12),
                20 * np.log10(sig_rms / (floor_rms + 1e-12)),
            )
        else:
            log.warning("%s: recording is empty (0 samples captured)", log_prefix)

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


class HDMIPwCatPlayback:
    """HDMI sweep playback via ``pw-cat`` (native PipeWire), capture via PortAudio.

    Why pw-cat instead of aplay:
        Inside the avr-calibration container on the Pi 5 (Docker, ``--privileged``,
        ``--network=host``) PortAudio does NOT enumerate ``vc4hdmi0``.  aplay with
        ALSA ``default:CARD=vc4hdmi0`` also fails inside the container because the
        ALSA ``default`` plugin with ``CARD=`` parameters is not available there.
        ``pw-cat --target <node>`` speaks PipeWire natively — no ALSA bridge,
        no plugin lookup — and reaches the vc4hdmi0 sink directly.

    Sequence (mirrors ``USBPlayback``):
        recording-first → sleep PRE_DELAY_S → spawn pw-cat → wait for pw-cat to
        exit → small POST_DELAY_S settle → stop recording. The 1 s pre-delay
        guarantees the noise-floor window in ``validate_recording`` lands on
        pre-sweep silence.

    Multi-channel layout:
        Builds an N-channel S16_LE buffer and writes the sweep onto channel
        ``out_channel - 1`` (1-based input). Other channels are silent. So
        ``out_channel=1, channels=6`` puts the sweep on FL only and zero-fills
        the rest of the 6-ch HDMI stream.
    """

    PRE_DELAY_S: float = 1.0
    """Seconds of recording before playback starts. Mirrors ``USBPlayback`` so
    the deconvolution alignment math (sweep-pad shared anchor) is identical."""

    POST_DELAY_S: float = 0.5
    """Seconds of trailing capture after pw-cat exits, to capture the room reverb tail."""

    HDMI_WARMUP_S: float = 5.0
    """Seconds of silent PCM sent to the AVR *before* the recording starts.
    The AVR's PCM detection engine needs 3-5 s to lock onto the incoming HDMI
    audio and begin routing to the speakers.  If the first pass of sweep
    audio arrives before the AVR locks, those samples are swallowed and the
    cross-correlation peak collapses below the "Sweep not detected" threshold.

    The warmup is a separate pw-cat subprocess that runs *outside* the
    recording window, so it does not shift the sweep inside rec_array.  After
    the warmup finishes, the AVR is in PCM-output mode; the recording then
    starts and captures the full sweep without any window mismatch."""

    def __init__(
        self,
        pipewire_node: str,
        channels: int = 6,
        capture_pipewire_node: str | None = None,
        skip_warmup: bool = False,
    ) -> None:
        if not pipewire_node:
            raise ValueError("HDMIPwCatPlayback requires a non-empty PipeWire node name")
        if channels < 1:
            raise ValueError(f"channels must be >= 1, got {channels}")
        self.pipewire_node = pipewire_node
        self.channels = int(channels)
        self.capture_pipewire_node = capture_pipewire_node
        self.skip_warmup = skip_warmup

    @staticmethod
    def _kill_stale_pw_streams(sink_name: str) -> None:
        """Destroy any orphaned pw-cat stream nodes writing to *sink_name*.

        After a crash or network outage, pw-cat's PipeWire session can
        outlive the process — the node stays registered and blocks the next
        writer.  Call this at the start of every sweep to clean up before
        opening a new stream.
        """
        import subprocess, re
        try:
            out = subprocess.check_output(
                ["pw-cli", "ls", "Node"],
                timeout=3.0,
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", errors="replace")
        except Exception:
            return
        # Find pw-cat Stream/Output/Audio nodes (orphaned playback streams).
        # pw-cli output groups each node as a block; scan for id lines followed
        # by application.name = "pw-cat" and media.class = "Stream/Output/Audio".
        current_id: str | None = None
        is_pwcat = False
        is_output_stream = False
        ids_to_kill: list[str] = []
        for line in out.splitlines():
            m_id = re.match(r"^\s*id\s+(\d+),", line)
            if m_id:
                if current_id and is_pwcat and is_output_stream:
                    ids_to_kill.append(current_id)
                current_id = m_id.group(1)
                is_pwcat = False
                is_output_stream = False
            if 'application.name = "pw-cat"' in line:
                is_pwcat = True
            if 'media.class = "Stream/Output/Audio"' in line:
                is_output_stream = True
        if current_id and is_pwcat and is_output_stream:
            ids_to_kill.append(current_id)
        for node_id in ids_to_kill:
            log.warning("HDMIPwCatPlayback: destroying stale pw-cat stream node %s", node_id)
            try:
                subprocess.run(
                    ["pw-cli", "destroy", node_id],
                    timeout=2.0, capture_output=True,
                )
            except Exception:
                pass

    def play_and_record(self, sweep, sample_rate, in_channel, out_channel):
        import subprocess
        import time as _time

        import numpy as np
        import sounddevice as sd

        # Clean up any orphaned pw-cat stream nodes from previous crashed sweeps.
        self._kill_stale_pw_streams(self.pipewire_node)

        sweep_array = sweep.timeSignal[:, 0].astype(np.float32)
        n_samples = len(sweep_array)
        n_channels = max(self.channels, out_channel)

        # Build N-channel int16 PCM with sweep on out_channel-1, others silent.
        sweep_int16 = (np.clip(sweep_array, -1.0, 1.0) * 32767).astype(np.int16)
        out_buf = np.zeros((n_samples, n_channels), dtype=np.int16)
        out_buf[:, out_channel - 1] = sweep_int16
        pcm_bytes = out_buf.tobytes()

        pre_samples = int(self.PRE_DELAY_S * sample_rate)
        post_samples = int(self.POST_DELAY_S * sample_rate)
        rec_n = pre_samples + n_samples + post_samples

        # Force the channel map so FC lands at the right PCM slot.
        # Without this, the vc4-hdmi driver picks FL,FR,LFE,NA,RC,NA for
        # AVRs whose EDID advertises back-center — FC is unreachable.
        # With FL,FR,LFE,FC,RL,RR explicit, ch 4 = FC, ch 5/6 = RL/RR.
        # Verified 2026-05-07 against amixer numid=2 values 3,4,8,7,5,6,0,0.
        chmap_for_channels = {
            2: "FL,FR",
            4: "FL,FR,LFE,FC",
            6: "FL,FR,LFE,FC,RL,RR",
        }
        chmap_arg = chmap_for_channels.get(n_channels)

        pw_cmd = [
            "pw-cat",
            "--playback",
            "--target", self.pipewire_node,
            "--channels", str(n_channels),
            "--rate", str(sample_rate),
            "--format", "s16",
        ]
        if chmap_arg is not None:
            pw_cmd.extend(["--channel-map", chmap_arg])
        pw_cmd += ["-"]  # read PCM from stdin

        # Warm up AVR PCM detection before the recording starts.
        # Skipped for null-sink (USB) targets — they need no AVR PCM lock time.
        if not self.skip_warmup:
            warmup_frames = int(self.HDMI_WARMUP_S * sample_rate)
            warmup_bytes = np.zeros((warmup_frames, n_channels), dtype=np.int16).tobytes()
            try:
                wu = subprocess.Popen(
                    pw_cmd,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                try:
                    wu.communicate(input=warmup_bytes, timeout=self.HDMI_WARMUP_S + 5.0)
                except Exception:
                    try:
                        wu.kill()
                        wu.communicate()
                    except Exception:
                        pass
                    raise
            except Exception as _wu_exc:
                log.warning("HDMIPwCatPlayback warmup failed (non-fatal): %s", _wu_exc)

        sweep_1d = sweep.timeSignal[:, 0]

        if self.capture_pipewire_node:
            # Native PipeWire capture via pw-record — no ALSA bridge.
            rec_proc, chunks, reader_t = _start_pw_record(
                self.capture_pipewire_node, sample_rate
            )
            proc = None
            try:
                _time.sleep(self.PRE_DELAY_S)
                proc = subprocess.Popen(
                    pw_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    _stdout_b, stderr_b = proc.communicate(
                        input=pcm_bytes,
                        timeout=max(30.0, n_samples / sample_rate + 10.0),
                    )
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    _stop_pw_record(rec_proc, reader_t)
                    raise RuntimeError(
                        f"pw-cat --target {self.pipewire_node!r} timed out — HDMI sink may be unplugged"
                    )
                if proc.returncode != 0:
                    _stop_pw_record(rec_proc, reader_t)
                    raise RuntimeError(
                        f"pw-cat --target {self.pipewire_node!r} failed (rc={proc.returncode}): "
                        f"{stderr_b.decode('utf-8', errors='replace').strip()}"
                    )
                _time.sleep(self.POST_DELAY_S)
            finally:
                if proc is not None and proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            _stop_pw_record(rec_proc, reader_t)
            rec_1d = _assemble_pw_recording(chunks)
            n_recorded = len(rec_1d)
        else:
            # Fallback: PortAudio sd.InputStream capture.
            import sounddevice as sd
            in_dev = int(sd.default.device[0])
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
            proc = None
            try:
                in_stream.start()
                _time.sleep(self.PRE_DELAY_S)
                proc = subprocess.Popen(
                    pw_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    _stdout_b, stderr_b = proc.communicate(
                        input=pcm_bytes,
                        timeout=max(30.0, n_samples / sample_rate + 10.0),
                    )
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    raise RuntimeError(
                        f"pw-cat --target {self.pipewire_node!r} timed out — HDMI sink may be unplugged"
                    )
                if proc.returncode != 0:
                    raise RuntimeError(
                        f"pw-cat --target {self.pipewire_node!r} failed (rc={proc.returncode}): "
                        f"{stderr_b.decode('utf-8', errors='replace').strip()}"
                    )
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
            rec_1d = rec_buf[:n_recorded, 0].astype(np.float64)

        cap_info = self.capture_pipewire_node or "portaudio"
        if len(rec_1d) > 0:
            peak = float(np.max(np.abs(rec_1d)))
            floor_n = min(int(0.5 * sample_rate), len(rec_1d) // 2)
            floor_rms = float(np.sqrt(np.mean(rec_1d[:floor_n] ** 2)))
            sig_rms = float(np.sqrt(np.mean(rec_1d[floor_n:] ** 2)))
            log.info(
                "HDMIPwCatPlayback: node=%s cap=%s ch=%d/%d warmup=%.0fms pre=%.0fms n_sweep=%d "
                "rec_n=%d n_recorded=%d peak=%.1f dBFS floor=%.1f dBFS "
                "sig=%.1f dBFS SNR=%.1f dB",
                self.pipewire_node, cap_info, out_channel, n_channels,
                self.HDMI_WARMUP_S * 1000, self.PRE_DELAY_S * 1000, n_samples, rec_n, n_recorded,
                20 * np.log10(peak + 1e-12),
                20 * np.log10(floor_rms + 1e-12),
                20 * np.log10(sig_rms + 1e-12),
                20 * np.log10(sig_rms / (floor_rms + 1e-12)),
            )
        else:
            log.warning("HDMIPwCatPlayback: recording is empty (0 samples captured) cap=%s", cap_info)

        return sweep_1d, rec_1d


# Backward-compat alias — callers that still reference HDMIAplayPlayback by name keep working.
HDMIAplayPlayback = HDMIPwCatPlayback


class LoopbackRefPlayback:
    """Decorator strategy: wrap any base PlaybackStrategy and capture a
    parallel electrical reference channel during sweep playback.

    The reference is an ALSA capture device (snd-aloop or a Scarlett
    input) that taps the audio chain downstream of the AVR (or
    downstream of CamillaDSP for sub measurements). Cross-correlating
    the recorded mic vs the recorded reference (instead of vs the
    analytical sweep template) isolates pure acoustic delay from
    upstream processing latency — AVR FIR pre-ring, CamillaDSP
    buffering, USB jitter. Sub-millisecond timing accuracy.

    Per project_loopback_alignment_rig.md, this is task #50's deliverable.

    Status: SKELETON — interface stable, ref capture not yet wired. The
    base strategy still runs as before; this class adds the capture
    plumbing once the ALSA / CamillaDSP YAML for the ref path is built.
    Production use requires:
      Phase 1a (sub):  CamillaDSP YAML adds a 2nd playback to
                       hw:Loopback,1,X carrying a copy of the sub-bus
                       signal; ref_device = "hw:Loopback,1,X" capture.
      Phase 1b (HDMI): asound.conf `multi` plugin fans aplay output to
                       both vc4hdmi0 AND hw:Loopback,1,X; same ref capture.
      Phase 2 (per-speaker): AVR pre-out → Scarlett input N; ref_device
                       = "hw:USB,0" with channel index N, captured in
                       parallel during sweep.

    Returns ``(sweep_1d, mic_1d, ref_1d)`` triple. Callers that don't
    care about ref bypass via ``measure(loopback_ref=False)`` and use
    the base strategy's 2-tuple shape.
    """

    def __init__(
        self,
        base: PlaybackStrategy,
        ref_device: str = "",
        ref_channels: int = 1,
        ref_channel_index: int = 1,  # 1-based
        ref_pipewire_node: str | None = None,
        ref_pw_channels: int = 1,
    ) -> None:
        pw_ch = max(ref_pw_channels, ref_channel_index)
        if ref_channels < 1:
            raise ValueError(f"ref_channels must be >= 1, got {ref_channels}")
        upper = pw_ch if ref_pipewire_node is not None else ref_channels
        if not (1 <= ref_channel_index <= upper):
            raise ValueError(
                f"ref_channel_index ({ref_channel_index}) must be >= 1"
                f" and <= {upper}"
            )
        self.base = base
        self.ref_device = ref_device
        self.ref_channels = int(ref_channels)
        self.ref_channel_index = int(ref_channel_index)
        self.ref_pipewire_node = ref_pipewire_node
        self.ref_pw_channels = pw_ch  # actual channel count passed to pw-record

    def play_and_record(self, sweep, sample_rate, in_channel, out_channel):
        """Run base playback + capture the loopback ref in parallel.

        Two capture paths:
          pw-record (preferred): ref_pipewire_node set → pw-record subprocess
            taps a PipeWire source node (e.g. Scarlett multichannel input
            carrying the AVR pre-out signal). No ALSA bridge.
          PortAudio (fallback): ref_device set → sd.InputStream on a named
            ALSA device.

        Both paths return a 3-tuple (sweep_1d, mic_1d, ref_1d). ref_1d is
        zeros if reference capture fails — fail-soft keeps the measurement
        working without the loopback rig.

        Timing model (pw-record path):
          ref capture starts when base.play_and_record() is called, which
          includes any warmup burst (HDMI_WARMUP_S).  After base returns the
          ref is trimmed to align with mic_1d: the first warmup_samples are
          dropped so both arrays share the same t=0 (start of recording window
          inside base).
        """
        import time as _time
        import threading

        import numpy as np

        ref_failed = [False]
        ref_error: list = [None]

        base_result: list = [None]
        base_exc: list = [None]

        def _base_runner():
            try:
                base_result[0] = self.base.play_and_record(
                    sweep, sample_rate, in_channel, out_channel,
                )
            except Exception as exc:
                base_exc[0] = exc

        if self.ref_pipewire_node:
            # Native PipeWire reference capture via pw-record.
            ref_proc = None
            ref_chunks: list[bytes] = []
            ref_reader = None
            try:
                ref_proc, ref_chunks, ref_reader = _start_pw_record(
                    self.ref_pipewire_node, sample_rate, channels=self.ref_pw_channels
                )
            except Exception as exc:
                ref_failed[0] = True
                ref_error[0] = f"pw-record start failed: {exc}"

            base_thread = threading.Thread(target=_base_runner, daemon=True)
            base_thread.start()
            base_thread.join(timeout=120.0)
            if base_thread.is_alive():
                if ref_proc is not None:
                    _stop_pw_record(ref_proc, ref_reader)
                raise RuntimeError("LoopbackRefPlayback: base strategy timeout (120s)")

            if ref_proc is not None:
                _stop_pw_record(ref_proc, ref_reader)

            if base_exc[0] is not None:
                raise base_exc[0]
            if base_result[0] is None:
                raise RuntimeError("LoopbackRefPlayback: base strategy returned no result")
            sweep_1d, mic_1d = base_result[0]

            if not ref_failed[0]:
                try:
                    raw = b"".join(ref_chunks)
                    if raw and len(raw) >= self.ref_pw_channels * 4:
                        all_ch = np.frombuffer(raw, dtype=np.float32).reshape(-1, self.ref_pw_channels)
                        ref_full = all_ch[:, self.ref_channel_index - 1].astype(np.float64)
                        # Drop HDMI_WARMUP_S leading samples so ref aligns with mic_1d.
                        # When skip_warmup=True (USB/null-sink path), no warmup was played;
                        # using the class-level HDMI_WARMUP_S would discard 5s of real data.
                        _actual_warmup_s = (
                            0.0 if getattr(self.base, "skip_warmup", False)
                            else getattr(self.base, "HDMI_WARMUP_S", 0.0)
                        )
                        warmup_n = int(_actual_warmup_s * sample_rate)
                        ref_aligned = ref_full[warmup_n:]
                        # Trim both to the same length.
                        n = min(len(ref_aligned), len(mic_1d))
                        ref_1d = ref_aligned[:n]
                        mic_1d = mic_1d[:n]
                    else:
                        ref_failed[0] = True
                        ref_error[0] = "pw-record returned no data"
                        ref_1d = np.zeros_like(mic_1d)
                except Exception as exc:
                    ref_failed[0] = True
                    ref_error[0] = f"parse failed: {exc}"
                    ref_1d = np.zeros_like(mic_1d)
            else:
                ref_1d = np.zeros_like(mic_1d)

        else:
            # PortAudio sd.InputStream fallback (ref_device substring match).
            import sounddevice as sd

            pre_s = getattr(self.base, "PRE_DELAY_S", 1.0)
            post_s = getattr(self.base, "POST_DELAY_S", 0.5)
            sweep_array_len = len(sweep.timeSignal[:, 0])
            rec_n = int(pre_s * sample_rate) + sweep_array_len + int(post_s * sample_rate)

            ref_buf = np.zeros((rec_n, self.ref_channels), dtype=np.float32)
            ref_pos = [0]

            def _ref_callback(indata, frames, time_info, status):
                end = min(ref_pos[0] + frames, rec_n)
                count = end - ref_pos[0]
                if count > 0:
                    ref_buf[ref_pos[0]:end] = indata[:count, :self.ref_channels]
                ref_pos[0] = end

            ref_dev_idx: int | str | None = None
            try:
                devices = sd.query_devices()
                for idx, dev in enumerate(devices):
                    if (dev.get("max_input_channels", 0) >= self.ref_channels and
                            self.ref_device.lower() in str(dev.get("name", "")).lower()):
                        ref_dev_idx = idx
                        break
            except Exception as exc:
                ref_failed[0] = True
                ref_error[0] = f"device lookup failed: {exc}"

            if ref_dev_idx is None and not ref_failed[0]:
                ref_dev_idx = self.ref_device

            ref_stream = None
            if not ref_failed[0]:
                try:
                    ref_stream = sd.InputStream(
                        device=ref_dev_idx,
                        samplerate=sample_rate,
                        channels=self.ref_channels,
                        dtype="float32",
                        callback=_ref_callback,
                    )
                except Exception as exc:
                    ref_failed[0] = True
                    ref_error[0] = f"InputStream open failed: {exc}"

            try:
                if ref_stream is not None:
                    ref_stream.start()
                base_thread = threading.Thread(target=_base_runner, daemon=True)
                base_thread.start()
                base_thread.join(timeout=120.0)
                if base_thread.is_alive():
                    if ref_stream is not None:
                        try: ref_stream.stop()
                        except Exception: pass
                        try: ref_stream.close()
                        except Exception: pass
                    raise RuntimeError("LoopbackRefPlayback: base strategy timeout (120s)")
                if ref_stream is not None:
                    _time.sleep(0.05)
                    try: ref_stream.stop()
                    except Exception: pass
                    try: ref_stream.close()
                    except Exception: pass
            except Exception as exc:
                ref_failed[0] = True
                ref_error[0] = f"runtime: {exc}"
                if ref_stream is not None:
                    try: ref_stream.stop()
                    except Exception: pass
                    try: ref_stream.close()
                    except Exception: pass

            if base_exc[0] is not None:
                raise base_exc[0]
            if base_result[0] is None:
                raise RuntimeError("LoopbackRefPlayback: base strategy returned no result")
            sweep_1d, mic_1d = base_result[0]

            n_recorded = min(ref_pos[0], len(mic_1d))
            if n_recorded > 0 and not ref_failed[0]:
                ref_1d = ref_buf[:n_recorded, self.ref_channel_index - 1].astype(np.float64)
                if len(ref_1d) < len(mic_1d):
                    mic_1d = mic_1d[:len(ref_1d)]
            else:
                ref_1d = np.zeros_like(mic_1d)

        ref_source = self.ref_pipewire_node or self.ref_device
        if not ref_failed[0] and np.any(ref_1d != 0):
            ref_peak_db = 20 * np.log10(np.max(np.abs(ref_1d)) + 1e-12)
            ref_rms_db = 20 * np.log10(np.sqrt(np.mean(ref_1d ** 2)) + 1e-12)
            log.info(
                "LoopbackRefPlayback: ref captured source=%s ch=%d n=%d "
                "peak=%.1f dBFS rms=%.1f dBFS",
                ref_source, self.ref_channel_index, len(ref_1d),
                ref_peak_db, ref_rms_db,
            )
        else:
            ref_1d = np.zeros_like(mic_1d)
            log.warning(
                "LoopbackRefPlayback: ref capture failed (source=%s err=%s) "
                "— ref_1d is zeros. Cross-correlation timing falls back to "
                "the analytical sweep template.",
                ref_source, ref_error[0],
            )

        return sweep_1d, mic_1d, ref_1d


def playback_for_route(
    route: str,
    *,
    hdmi_pipewire_node: str | None = None,
    hdmi_alsa_device: str | None = None,  # deprecated; ignored when hdmi_pipewire_node is set
    hdmi_channels: int = 6,
    capture_pipewire_node: str | None = None,
    loopback_ref_device: str | None = None,
    loopback_ref_channels: int = 1,
    loopback_ref_channel_index: int = 1,
    loopback_ref_pipewire_node: str | None = None,
    loopback_ref_pw_channels: int = 1,
    hdmi_skip_warmup: bool = False,
) -> PlaybackStrategy:
    """Factory: return the right playback strategy for the configured route.

    HDMI path:
      - ``hdmi_pipewire_node`` set → ``HDMIPwCatPlayback`` (native PipeWire
        via ``pw-cat --target <node>``; no ALSA bridge required).
      - ``hdmi_pipewire_node`` None → legacy PortAudio-based ``HDMIPlayback``,
        kept for back-compat with callers that don't yet plumb a node.

    Loopback ref:
      - ``loopback_ref_device`` set (e.g. ``"hw:Loopback,1,0"`` or
        ``"hw:USB,0"``) → wraps the base strategy in
        ``LoopbackRefPlayback`` to capture an electrical reference
        channel alongside the mic. Returns 3-tuples (sweep, mic, ref)
        instead of 2-tuples. See project_loopback_alignment_rig.md.
    """
    if route == "hdmi":
        node = hdmi_pipewire_node or hdmi_alsa_device  # hdmi_alsa_device kept for compat
        if node:
            base: PlaybackStrategy = HDMIPwCatPlayback(
                pipewire_node=node,
                channels=hdmi_channels,
                capture_pipewire_node=capture_pipewire_node,
                skip_warmup=hdmi_skip_warmup,
            )
        else:
            base = HDMIPlayback()
    else:
        base = USBPlayback(capture_pipewire_node=capture_pipewire_node)

    if loopback_ref_pipewire_node is not None or loopback_ref_device is not None:
        return LoopbackRefPlayback(
            base=base,
            ref_device=loopback_ref_device or "",
            ref_channels=loopback_ref_channels,
            ref_channel_index=loopback_ref_channel_index,
            ref_pipewire_node=loopback_ref_pipewire_node,
            ref_pw_channels=loopback_ref_pw_channels,
        )
    return base
