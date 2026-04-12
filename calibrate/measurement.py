"""PyTTa-based acoustic measurement engine. Hardware-agnostic.

Single entry point: MeasurementEngine.measure()

    generate log sweep (PyTTa) → play+record → deconvolve (numpy FFT) → FR

Playback is delegated to PlaybackStrategy (calibrate.drivers.playback):
    USBPlayback  → explicit sd.InputStream + sd.OutputStream (recording-first, pre-delay)
    HDMIPlayback → explicit sd.InputStream + sd.OutputStream (HDMI requires int16 output)

This module has ZERO knowledge of AVR hardware or output format quirks.
Callers that need AVR input/volume switching should use DenonSweepContext
from calibrate.drivers.denon before/after calling measure().

PyTTa and numpy are imported lazily so the module loads in CI/test
environments without PortAudio.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import traceback as _traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from .config import Config

log = logging.getLogger(__name__)

_MEASUREMENT_TIMEOUT_S: float = 60.0
"""Timeout for a single play-and-record cycle. Prevents a hung audio device from
blocking the calibration loop indefinitely."""

_MEASURE_LOCK: asyncio.Lock = asyncio.Lock()
"""Module-level lock serializing all measure() calls across all MeasurementEngine instances.

MeasurementEngine is instantiated fresh per MCP tool call, so an instance-level lock
would not serialize concurrent callers. This lock is shared across all instances so
that sd.default.device (a sounddevice module-level global) is never overwritten by a
concurrent caller mid-measurement."""


class MeasurementQualityError(RuntimeError):
    """Raised when a recording fails quality validation before deconvolution.

    Attributes:
        check      -- which check failed ("sweep_capture" | "snr")
        detail     -- human-readable description of the failure
        suggestion -- actionable hint for the user
    """

    def __init__(self, check: str, detail: str, suggestion: str) -> None:
        self.check = check
        self.detail = detail
        self.suggestion = suggestion
        super().__init__(detail)


@dataclass
class FrequencyResponse:
    """Frequency response from a single log-sweep measurement."""

    frequencies: list[float]  # Hz, trimmed to calibration band
    spl: list[float]          # dBFS transfer-function magnitude (mic-corrected if cal file available)
    sample_rate: int          # Hz
    sweep_duration: float     # seconds
    timestamp: str            # ISO-8601 UTC
    warnings: list[dict] = field(default_factory=list)  # non-fatal quality warnings
    impulse_response: Optional[list[float]] = None  # time-domain IR, first 24 000 samples
    phase: Optional[list[float]] = None  # radians, same grid as frequencies/spl
    recording_peak_dbfs: Optional[float] = None  # peak of raw recording before deconvolution
    coherence: Optional[list[float]] = None  # 0-1 per frequency, measurement reliability

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "FrequencyResponse":
        data = json.loads(s)
        data.setdefault("warnings", [])     # backward compat
        data.setdefault("impulse_response", None)  # backward compat
        data.setdefault("phase", None)  # backward compat
        data.setdefault("recording_peak_dbfs", None)  # backward compat
        data.setdefault("coherence", None)  # backward compat
        return cls(**data)

    @property
    def peak_spl(self) -> float:
        return max(self.spl) if self.spl else 0.0

    @property
    def freq_at_peak(self) -> float:
        if not self.spl:
            return 0.0
        peak = max(self.spl)
        return self.frequencies[self.spl.index(peak)]


def parse_umik_sensitivity(cal_path: str) -> float:
    """Parse UMIK cal file and return dBFS-to-SPL offset.

    The UMIK-1 cal file header looks like:
        "Sens Factor =1.725dB, AGain =18dB, SERNO: 7079831"

    Base sensitivity at AGain=18dB is -18 dBFS/Pa (94 dB SPL produces -18 dBFS).
    Effective sensitivity = -18 + sens_factor dBFS/Pa.
    Offset = 94 - effective_sensitivity (to convert: SPL = dBFS + offset).

    Returns the offset such that: SPL_dB = peak_dBFS + offset
    """
    import re
    from pathlib import Path

    path = Path(cal_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"UMIK cal file not found: {cal_path}")

    header = path.read_text().splitlines()[0]
    m_sens = re.search(r"Sens Factor\s*=\s*([-\d.]+)\s*dB", header)
    m_gain = re.search(r"AGain\s*=\s*([-\d.]+)\s*dB", header)
    if not m_sens or not m_gain:
        raise ValueError(f"Cannot parse UMIK cal header: {header!r}")

    sens_factor = float(m_sens.group(1))
    analog_gain = float(m_gain.group(1))

    # UMIK-1 base sensitivity: at AGain dB analog gain, 94 dB SPL (1 Pa)
    # produces -(AGain) dBFS, adjusted by sens_factor.
    effective_sens_dbfs = -analog_gain + sens_factor  # dBFS at 94 dB SPL
    # SPL = dBFS - effective_sens + 94
    offset = 94.0 - effective_sens_dbfs
    return offset


def parse_umik_cal_curve(cal_path: str) -> list[tuple[float, float]]:
    """Parse UMIK cal file per-frequency correction data.

    The cal file has a header line (or two), then rows of:
        frequency_hz   correction_db

    Returns a list of (freq_hz, correction_db) pairs sorted by frequency.
    The correction should be ADDED to measured SPL to get true SPL.
    """
    from pathlib import Path

    path = Path(cal_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"UMIK cal file not found: {cal_path}")

    points: list[tuple[float, float]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('"'):
            continue  # Skip header lines (quoted strings)
        parts = line.split()
        if len(parts) >= 2:
            try:
                freq = float(parts[0])
                correction = float(parts[1])
                points.append((freq, correction))
            except ValueError:
                continue
    if not points:
        raise ValueError(f"No frequency correction data found in {cal_path}")
    return sorted(points, key=lambda p: p[0])


def apply_mic_correction(
    frequencies: list[float],
    spl: list[float],
    cal_curve: list[tuple[float, float]],
) -> list[float]:
    """Apply mic calibration correction to measured SPL values.

    Interpolates the cal curve (log-frequency, linear dB) to each measurement
    frequency and adds the correction. Returns corrected SPL list.
    """
    import math

    if not cal_curve:
        return spl

    cal_freqs = [p[0] for p in cal_curve]
    cal_corrections = [p[1] for p in cal_curve]

    corrected = []
    for freq, measured in zip(frequencies, spl):
        # Find surrounding cal points for log-frequency interpolation
        if freq <= cal_freqs[0]:
            correction = cal_corrections[0]
        elif freq >= cal_freqs[-1]:
            correction = cal_corrections[-1]
        else:
            # Binary search for surrounding points
            for i in range(len(cal_freqs) - 1):
                if cal_freqs[i] <= freq <= cal_freqs[i + 1]:
                    f0, c0 = cal_freqs[i], cal_corrections[i]
                    f1, c1 = cal_freqs[i + 1], cal_corrections[i + 1]
                    # Log-frequency interpolation
                    t = math.log(freq / f0) / math.log(f1 / f0)
                    correction = c0 + t * (c1 - c0)
                    break
            else:
                correction = 0.0
        corrected.append(round(measured + correction, 2))
    return corrected


def _find_umik_device(devices, name_substring: str = "UMIK") -> int | None:
    """Return the index of the first input device whose name contains name_substring.

    Args:
        devices: sequence of device dicts as returned by sounddevice.query_devices()
        name_substring: substring to match against device name (case-insensitive)

    Returns:
        Device index (int) or None if no matching input device is found.
    """
    for i, d in enumerate(devices):
        if name_substring.lower() in str(d.get("name", "")).lower() and d.get("max_input_channels", 0) > 0:
            return i
    return None


class MeasurementEngine:
    """Run a log-sweep measurement via PyTTa and return a FrequencyResponse.

    Single entry point: measure(). Route-aware: USB uses explicit sd.InputStream +
    sd.OutputStream (recording-first, 1s pre-delay); HDMI uses the same with int16
    output conversion.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        # Use the module-level _MEASURE_LOCK, not an instance lock — MeasurementEngine
        # is created fresh per MCP tool call, so an instance lock would not serialize
        # concurrent callers. The module lock is shared across all instances.

    def validate_recording(
        self,
        np,                                          # numpy module (lazy import pattern)
        sweep_array,                                 # reference sweep (float64 ndarray)
        rec_array,                                   # recording (float64 ndarray)
        sample_rate: int,
        noise_floor_window_ms: int = 500,
        correlation_threshold: float = 0.05,
        min_snr_db: float = 20.0,
    ) -> list[dict]:
        """
        Three-check quality gate. Returns a list of warning dicts (may be empty).
        Raises MeasurementQualityError on a hard failure.

        Check 1 — Floor noise gate:
            Measure RMS of the first noise_floor_window_ms ms of the recording
            (before the sweep arrives). If above -40 dBFS → warn (don't raise).

        Check 2 — Sweep capture (FFT cross-correlation, O(N log N)):
            Compute normalized cross-correlation peak between sweep and recording.
            If peak < correlation_threshold → raise (sweep wasn't captured).

        Check 3 — SNR:
            Compare signal peak window to floor noise.
            If SNR < min_snr_db → raise (signal too weak).
        """
        warnings_out: list[dict] = []

        # ── Check 1: Floor noise gate ──────────────────────────────────────
        floor_n = max(1, int(sample_rate * noise_floor_window_ms / 1000))
        floor_samples = rec_array[:floor_n]
        floor_rms = np.sqrt(np.mean(floor_samples ** 2))
        floor_db = 20.0 * np.log10(float(floor_rms) + 1e-12)

        if floor_db > -40.0:
            warnings_out.append({
                "check": "floor_noise",
                "detail": (
                    f"Floor noise {floor_db:.1f} dBFS > -40 dBFS threshold"
                    " — noisy room may affect measurement accuracy"
                ),
            })

        # ── Check 2: Sweep capture (FFT-based cross-correlation) ───────────
        # O(N log N) — np.correlate(..., mode='full') would be O(N²):
        # at 144k samples (3s @ 48kHz) that's ~100s on Pi Zero W.
        n = len(sweep_array)
        rec_t = rec_array[:n]
        fft_len = n * 2  # zero-pad to avoid circular wrap
        S = np.fft.fft(sweep_array, fft_len)
        R = np.fft.fft(rec_t, fft_len)
        corr = np.real(np.fft.ifft(np.conj(S) * R))
        norm = np.linalg.norm(sweep_array) * np.linalg.norm(rec_t)
        peak = float(np.max(np.abs(corr))) / (float(norm) + 1e-12)

        if peak < correlation_threshold:
            raise MeasurementQualityError(
                check="sweep_capture",
                detail="Sweep not detected in recording (cross-correlation peak too low)",
                suggestion="Verify amplifier is on and correct input is selected",
            )

        # ── Check 3: SNR ───────────────────────────────────────────────────
        # Use cross-correlation lag (from Check 2) to locate the sweep in the
        # recording. Do NOT use np.argmax(abs(rec_array)) — that returns the
        # first sample at maximum amplitude, which is fragile when the UMIK
        # clips (multiple samples hit 1.0) or when there are room transients:
        # the first clipping sample may be in the floor window, collapsing
        # signal_rms ≈ floor_rms and giving SNR ≈ 0 dB despite a valid sweep.
        lag_idx = int(np.argmax(np.abs(corr[:n])))  # circular lag in [0, n)
        sig_start = max(0, lag_idx)
        sig_end = min(len(rec_array), lag_idx + n)
        peak_window = rec_array[sig_start:sig_end]
        signal_rms = np.sqrt(np.mean(peak_window ** 2)) if len(peak_window) > 0 else 0.0
        snr_db = 20.0 * np.log10(float(signal_rms) / (float(floor_rms) + 1e-12))

        if snr_db < min_snr_db:
            raise MeasurementQualityError(
                check="snr",
                detail=f"SNR {snr_db:.1f} dB < {min_snr_db} dB threshold",
                suggestion="Increase amplifier volume or check miniDSP signal routing",
            )

        return warnings_out

    async def measure(
        self,
        input_device_name: str | None = None,
    ) -> FrequencyResponse:
        """Run a full sweep measurement. Hardware-agnostic.

        Generates a log sweep via PyTTa, plays+records based on the configured
        playback route, validates the recording, and deconvolves to FR.

        Route-aware playback:
          "usb"  — explicit sd.InputStream + sd.OutputStream (recording-first, 1s pre-delay)
          "hdmi" — explicit sd.InputStream + sd.OutputStream (int16 output conversion)

        The caller is responsible for any AVR lifecycle management (input switching,
        volume, power) before/after calling measure(). Use DenonSweepContext for that.

        Concurrent measure() calls are serialized by an asyncio.Lock to prevent
        sd.default.device (global state) from being overwritten mid-measurement.
        play_and_record() runs in a thread executor so the event loop stays responsive
        during the blocking PortAudio I/O. Raises RuntimeError if the audio device
        hangs for more than 60 seconds.

        Args:
            input_device_name: optional substring to select the recording device by name
                (e.g. "UMIK"). If None, uses config.mic.name (default "UMIK").
        """
        try:
            import pytta
        except ImportError as exc:
            raise RuntimeError(
                "pytta is required for measurements — pip install pytta"
            ) from exc

        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError(
                "numpy is required for measurements — pip install numpy"
            ) from exc

        cfg = self.config.measurement
        freq_min: int = cfg.get("freq_min", 20)
        freq_max: int = cfg.get("freq_max", 200)
        duration: float = cfg.get("sweep_duration", 3.0)
        sample_rate: int = cfg.get("sample_rate", 48000)
        in_channel: int = cfg.get("input_channel", 1)
        out_channel: int = cfg.get("output_channel", 1)
        route = cfg.get("playback_route", "usb")

        # Generate sweep before acquiring the lock — pure computation, no global state.
        # pytta 0.1.1 uses camelCase params and fftDegree instead of duration.
        # Also patches traceback.walk_stack to handle shallow stacks (Python 3.11 bug).
        _orig_walk_stack = _traceback.walk_stack

        def _safe_walk_stack(f):
            try:
                yield from _orig_walk_stack(f)
            except AttributeError:
                return

        _traceback.walk_stack = _safe_walk_stack
        try:
            sweep = pytta.generate.sweep(
                freqMin=freq_min,
                freqMax=freq_max,
                fftDegree=math.ceil(math.log2((duration + 1.0) * sample_rate)),
                samplingRate=sample_rate,
                method="logarithmic",
            )
        finally:
            _traceback.walk_stack = _orig_walk_stack

        from .drivers.playback import playback_for_route
        strategy = playback_for_route(route)

        # Lock: protects sd.default.device (module-level global) and serializes
        # play_and_record() so concurrent measure() calls don't clobber each other.
        # Uses the module-level lock (not self._lock) so all instances share it.
        async with _MEASURE_LOCK:
            mic_name = input_device_name or self.config._data.get("mic", {}).get("name", "UMIK")
            try:
                import sounddevice as sd
                devices = sd.query_devices()
                umik_idx = _find_umik_device(devices, name_substring=mic_name)
                if umik_idx is not None:
                    out_idx = int(sd.default.device[1])
                    sd.default.device = (umik_idx, out_idx)
                    log.info("Input device: %s (index %d)", devices[umik_idx]["name"], umik_idx)
            except ImportError:
                pass

            # Select output device based on route
            if route == "hdmi":
                try:
                    import sounddevice as sd
                    devices = sd.query_devices()
                    hdmi_name = cfg.get("hdmi_playback_device") or "hdmi"
                    candidates = [
                        (idx, dev) for idx, dev in enumerate(devices)
                        if dev["max_output_channels"] > 0 and hdmi_name.lower() in dev["name"].lower()
                    ]
                    # Sort: exact name match first, then shorter names (plugins) before hardware
                    candidates.sort(key=lambda x: (x[1]["name"].lower() != hdmi_name.lower(), len(x[1]["name"])))
                    if candidates:
                        idx, dev = candidates[0]
                        in_idx = int(sd.default.device[0])
                        sd.default.device = (in_idx, idx)
                        log.info("Output device (HDMI): %s (index %d)", dev["name"], idx)
                except ImportError:
                    pass
            elif route == "usb":
                try:
                    import sounddevice as sd
                    devices = sd.query_devices()
                    usb_name = cfg.get("playback_device") or "miniDSP"
                    candidates = [
                        (idx, dev) for idx, dev in enumerate(devices)
                        if dev.get("max_output_channels", 0) > 0 and usb_name.lower() in dev["name"].lower()
                    ]
                    if candidates:
                        idx, dev = candidates[0]
                        in_idx = int(sd.default.device[0])
                        sd.default.device = (in_idx, idx)
                        log.info("Output device (USB): %s (index %d)", dev["name"], idx)
                except ImportError:
                    pass

            # Run blocking play_and_record() in a thread executor so the asyncio event
            # loop stays responsive during PortAudio I/O. Times out after 60s to prevent
            # a hung audio device from blocking the calibration loop indefinitely.
            loop = asyncio.get_running_loop()
            try:
                sweep_1d, rec_1d = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, strategy.play_and_record, sweep, sample_rate, in_channel, out_channel
                    ),
                    timeout=_MEASUREMENT_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"Measurement timed out after {_MEASUREMENT_TIMEOUT_S:.0f}s — "
                    "audio device may be hung. Reconnect the USB cable and retry."
                )

        min_snr = float(cfg.get("min_snr_db", 20.0))
        # validate_recording uses the FULL recording (including pre-delay silence) for
        # its noise-floor check — the first 500ms must be silence.
        self.validate_recording(np, sweep_1d, rec_1d, sample_rate, min_snr_db=min_snr)

        # USBPlayback records PRE_DELAY_S (1s) of room noise BEFORE playing the sweep so
        # validate_recording has silence for its noise-floor window.  But passing this
        # pre-delayed recording directly to _compute_fr_arrays shifts the apparent IR
        # peak by pre_delay_samples in the circular FFT — moving it outside the stored
        # 24000-sample window and causing the peak-finder to return noise artifacts
        # (e.g. 0.25ms instead of the true ~40ms arrival time).
        # Strip the pre-delay so rec starts exactly when the sweep started playing.
        # HDMIPlayback has no PRE_DELAY_S, so this is a no-op for HDMI.
        pre_delay_samples = int(getattr(strategy, "PRE_DELAY_S", 0.0) * sample_rate)
        rec_for_deconv = rec_1d[pre_delay_samples:] if pre_delay_samples > 0 else rec_1d

        # Raw recording peak in dBFS (before deconvolution) — used by
        # calibrate_level to compute actual SPL via mic sensitivity.
        # Use rec_for_deconv (post-pre-delay) so the peak reflects the
        # sweep period, not transient noise during the 1s silence window.
        rec_peak_abs = float(np.max(np.abs(rec_for_deconv)))
        rec_peak_dbfs = round(20.0 * np.log10(rec_peak_abs + 1e-12), 1)

        # Load mic calibration curve if configured
        cal_curve = None
        cal_path = self.config._data.get("mic", {}).get("cal_file")
        if cal_path:
            try:
                cal_curve = parse_umik_cal_curve(cal_path)
                log.info("Loaded mic cal curve from %s (%d points)", cal_path, len(cal_curve))
            except Exception as exc:
                log.warning("Failed to load mic cal file %s: %s", cal_path, exc)

        frequencies, spl, ir_samples, phase, coherence = self._compute_fr_arrays(
            np, sweep_1d, rec_for_deconv, freq_min, freq_max, sample_rate,
            cal_curve=cal_curve,
        )

        return FrequencyResponse(
            frequencies=frequencies,
            spl=spl,
            sample_rate=sample_rate,
            sweep_duration=duration,
            timestamp=datetime.now(timezone.utc).isoformat(),
            impulse_response=ir_samples,
            phase=phase,
            recording_peak_dbfs=rec_peak_dbfs,
            coherence=coherence,
        )

    # ── Internals ─────────────────────────────────────────────────────────

    # Default IR gate length in seconds.  The gate removes circular-FFT
    # wrap-around artifacts that appear as comb-like nulls every 3rd bin
    # above ~55 Hz.  500 ms gives 10+ cycles at 20 Hz (adequate spectral
    # resolution for bass) while cutting well before the artifact at
    # ~N/3 samples (~1.8 s).
    IR_GATE_S: float = 0.5

    def _compute_fr_arrays(
        self,
        np,
        sweep_array,   # 1-D float64 ndarray
        rec_array,     # 1-D float64 ndarray
        freq_min: int,
        freq_max: int,
        sample_rate: int,
        cal_curve: list[tuple[float, float]] | None = None,
    ) -> tuple[list[float], list[float], list[float], list[float], list[float] | None]:
        """
        Core deconvolution on raw numpy arrays.

        Deconvolves via H(f) = Y·X* / (|X|² + ε), then gates the
        impulse response in the time domain before converting back to
        the frequency domain for a clean FR.

        The PyTTa sweep buffer contains leading/trailing silence.  When
        the room's reverb tail (T60 > 1 s for bass modes) wraps around
        in the circular FFT, the raw H(f) = Y/X shows comb-like nulls
        every 3 bins above ~55 Hz.  IR gating — deconvolve → IFFT →
        window the IR → FFT — is the standard fix used by REW and
        other measurement tools.

        Returns (frequencies, magnitude_db, ir_samples, phase_radians, coherence).
        coherence is None if scipy is unavailable or computation fails.
        Arrays are zero-padded / truncated to the shorter length so they
        share the same FFT grid.
        """
        n = min(len(sweep_array), len(rec_array))

        # Raw FFTs — no input-signal windowing.  Applying the same window
        # to both sides of a deconvolution (Y·W / X·W) does not cancel
        # and introduces its own spectral artifacts.
        X = np.fft.rfft(sweep_array[:n], n=n)
        Y = np.fft.rfft(rec_array[:n], n=n)

        # Regularised deconvolution (Wiener-style).
        # H = Y·conj(X) / (|X|² + ε)  — avoids amplifying noise where
        # the sweep has little energy (band edges, silence regions).
        X_power = np.abs(X) ** 2
        epsilon = max(float(np.max(X_power)) * 1e-6, 1e-20)
        with np.errstate(divide="ignore", invalid="ignore"):
            H = Y * np.conj(X) / (X_power + epsilon)

        # ── IR gate window ────────────────────────────────────────────
        # Convert to time domain, gate the IR to remove wrap-around
        # artifacts and late-time noise, then convert back.
        ir_full = np.fft.irfft(H, n=n)
        ir_samples = ir_full[:24000].tolist()  # store unwindowed for decay analysis

        gate_samples = min(int(self.IR_GATE_S * sample_rate), n)
        taper_samples = min(int(0.05 * sample_rate), gate_samples // 4)  # 50 ms taper

        ir_gated = np.zeros(n)
        ir_gated[:gate_samples] = ir_full[:gate_samples]
        # Half-Hanning taper at gate boundary for smooth roll-off
        if taper_samples > 0:
            taper = np.hanning(2 * taper_samples)[taper_samples:]
            ir_gated[gate_samples - taper_samples:gate_samples] *= taper

        H_gated = np.fft.rfft(ir_gated, n=n)

        freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
        mag_db = 20.0 * np.log10(np.abs(H_gated) + 1e-12)
        phase_rad = np.angle(H_gated)

        mask = (freqs >= freq_min) & (freqs <= freq_max)
        freq_list = freqs[mask].tolist()
        spl_list = mag_db[mask].tolist()

        # Apply mic calibration correction if cal curve provided
        if cal_curve:
            spl_list = apply_mic_correction(freq_list, spl_list, cal_curve)

        # Compute coherence using Welch's method
        coh_list: list[float] | None = None
        try:
            from scipy.signal import coherence as _scipy_coherence
            nperseg = min(n // 4, sample_rate)  # ~1s segments
            if nperseg >= 256:
                coh_freqs, coh_vals = _scipy_coherence(
                    sweep_array[:n], rec_array[:n],
                    fs=sample_rate, nperseg=nperseg,
                )
                # Interpolate coherence to our frequency grid
                coh_interp = np.interp(freqs[mask], coh_freqs, coh_vals)
                coh_list = [round(float(c), 3) for c in coh_interp]
        except (ImportError, Exception) as exc:
            log.warning("coherence computation failed: %s", exc)

        return freq_list, spl_list, ir_samples, phase_rad[mask].tolist(), coh_list

    def _compute_fr(
        self,
        np,
        sweep,       # PyTTa SignalObj
        recording,   # PyTTa SignalObj
        freq_min: int,
        freq_max: int,
        sample_rate: int,
        cal_curve: list[tuple[float, float]] | None = None,
    ) -> tuple[list[float], list[float], list[float], list[float], list[float] | None]:
        """Wrapper that extracts numpy arrays from PyTTa SignalObj inputs."""
        x = sweep.timeSignal[:, 0]
        y = recording.timeSignal[:, 0]
        return self._compute_fr_arrays(np, x, y, freq_min, freq_max, sample_rate, cal_curve=cal_curve)


def compute_session_metadata(
    fr: FrequencyResponse,
    search_window_ms: float = 50.0,
    decay_freq_min: float = 20.0,
    decay_freq_max: float = 200.0,
    t60_threshold_ms: float = 300.0,
) -> dict:
    """Compute IR-derived metadata from a FrequencyResponse at capture time.

    Returns a dict with:
      ir:           {peak_time_ms, peak_sign, spl_db}
      decay_modes:  [{freq_hz, t60_ms, peak_db, suggested_q, priority}, ...]
      group_delay:  {freq_hz: [...], delay_ms: [...]}
    """
    import numpy as np

    metadata: dict = {}
    sample_rate = fr.sample_rate or 48000

    # ── IR peak analysis (replaces analyze_ir) ────────────────────────────
    if fr.impulse_response:
        ir_arr = np.array(fr.impulse_response, dtype=np.float64)
        search_samples = max(1, int(search_window_ms / 1000.0 * sample_rate))
        search_samples = min(search_samples, len(ir_arr))
        search_window = ir_arr[:search_samples]

        abs_window = np.abs(search_window)
        # Skip first 0.5ms — Wiener deconvolution leaves a DC artifact near
        # t=0 that confuses onset detection.  The shortest sub-to-mic path is
        # always > 0.5ms (~17cm), so no real IR energy lives there.
        skip_samples = max(1, int(0.0005 * sample_rate))
        abs_window[:skip_samples] = 0.0
        max_idx = int(np.argmax(abs_window))
        # Onset detection: first sample within 20 dB of the absolute peak.
        # argmax(abs(ir)) finds the LOUDEST peak, which in rooms with strong
        # bass modes can be a late resonance (e.g. 48ms) instead of the direct
        # sound arrival (e.g. 8ms).  Onset detection finds the first arrival.
        onset_threshold = abs_window[max_idx] * 0.1  # -20 dB
        onset_candidates = np.where(abs_window >= onset_threshold)[0]
        peak_idx = int(onset_candidates[0]) if len(onset_candidates) > 0 else max_idx
        peak_sign = 1 if ir_arr[peak_idx] >= 0.0 else -1
        peak_time_s = peak_idx / sample_rate
        spl_db = float(20.0 * np.log10(abs_window[max_idx] + 1e-12))

        metadata["ir"] = {
            "peak_time_ms": round(peak_time_s * 1000.0, 3),
            "peak_sign": peak_sign,
            "spl_db": round(spl_db, 1),
            "sample_rate": sample_rate,
        }

    # ── Decay analysis (replaces analyze_decay) ──────────────────────────
    if fr.impulse_response:
        try:
            from .decay import analyze_decay as _analyze_decay

            modes = _analyze_decay(
                fr.impulse_response,
                sample_rate=sample_rate,
                t60_threshold_ms=t60_threshold_ms,
                freq_min=decay_freq_min,
                freq_max=decay_freq_max,
            )
            metadata["decay_modes"] = [
                {
                    "freq_hz": m.freq_hz,
                    "t60_ms": m.t60_ms,
                    "peak_db": m.peak_db,
                    "suggested_q": m.suggested_q,
                    "priority": m.priority,
                }
                for m in modes
            ]
        except (ValueError, ImportError):
            metadata["decay_modes"] = []

    # ── Group delay from phase ────────────────────────────────────────────
    if fr.phase and fr.frequencies and len(fr.phase) >= 2:
        freqs = np.array(fr.frequencies, dtype=np.float64)
        phase = np.unwrap(np.array(fr.phase, dtype=np.float64))
        # group_delay = -d(phase)/d(omega) = -d(phase)/d(freq) / (2*pi)
        d_freq = np.diff(freqs)
        d_phase = np.diff(phase)
        # Avoid division by zero
        with np.errstate(divide="ignore", invalid="ignore"):
            gd = np.where(d_freq > 0, -d_phase / (2.0 * np.pi * d_freq), 0.0)
        # Convert to ms; use midpoint frequencies
        mid_freqs = (freqs[:-1] + freqs[1:]) / 2.0
        metadata["group_delay"] = {
            "freq_hz": [round(f, 2) for f in mid_freqs.tolist()],
            "delay_ms": [round(d * 1000.0, 3) for d in gd.tolist()],
        }

    return metadata
