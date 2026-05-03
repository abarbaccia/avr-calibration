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
    impulse_response: Optional[list[float]] = None  # time-domain IR, gated to IR_GATE_S seconds
    phase: Optional[list[float]] = None  # radians, same grid as frequencies/spl
    recording_peak_dbfs: Optional[float] = None  # peak of raw recording before deconvolution
    recording_rms_dbfs: Optional[float] = None  # RMS of raw recording (sweep portion)
    coherence: Optional[list[float]] = None  # 0-1 per frequency, measurement reliability
    xcorr_peak_ms: Optional[float] = None  # cross-correlation peak time (propagation delay)

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "FrequencyResponse":
        data = json.loads(s)
        data.setdefault("warnings", [])     # backward compat
        data.setdefault("impulse_response", None)  # backward compat
        data.setdefault("phase", None)  # backward compat
        data.setdefault("recording_peak_dbfs", None)  # backward compat
        data.setdefault("recording_rms_dbfs", None)  # backward compat
        data.setdefault("coherence", None)  # backward compat
        data.setdefault("xcorr_peak_ms", None)  # backward compat
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


def _resolve_alsa_device_in_portaudio(
    alsa_name: str,
    devices,
    want_output: bool = True,
) -> tuple[int | None, dict | None]:
    """Resolve an ALSA-style device string (``hw:CardName,Dev,Sub``) to a PortAudio device.

    PortAudio names ALSA hw devices with the friendly form
    ``"<CardName>: <Description> (hw:<card_idx>,<dev>)"``, so a literal substring
    match for the original ALSA string ``"hw:CardName,Dev,Sub"`` fails. This
    helper reads ``/proc/asound/cards`` to map CardName → numeric index, then
    matches PortAudio devices by both card name (substring) and the
    ``(hw:<idx>,<dev>)`` suffix.

    Falls back to plain substring match if ``/proc/asound/cards`` is unavailable
    or the alsa_name has unexpected shape.

    Returns ``(idx, device_dict)`` on match, or ``(None, None)`` if no device
    in ``devices`` corresponds to the requested ALSA path. Caller is responsible
    for raising / logging — this helper never silently returns a wrong device.
    """
    if not alsa_name:
        return None, None
    needed_channels = "max_output_channels" if want_output else "max_input_channels"
    candidates_lit = [
        (idx, dev) for idx, dev in enumerate(devices)
        if dev.get(needed_channels, 0) > 0
        and alsa_name.lower() in str(dev.get("name", "")).lower()
    ]
    if candidates_lit:
        idx, dev = candidates_lit[0]
        return idx, dev

    if not alsa_name.lower().startswith("hw:"):
        return None, None
    parts = alsa_name[3:].split(",")
    if len(parts) < 2:
        return None, None
    card_name, dev_num = parts[0], parts[1]

    card_idx: int | None = None
    try:
        from pathlib import Path
        cards_text = Path("/proc/asound/cards").read_text()
    except (OSError, IOError):
        cards_text = ""
    for line in cards_text.splitlines():
        line = line.strip()
        if not line or not line[0].isdigit():
            continue
        bracket_open = line.find("[")
        bracket_close = line.find("]")
        if bracket_open == -1 or bracket_close == -1:
            continue
        idx_token = line[:bracket_open].strip().split()[0]
        name_token = line[bracket_open + 1:bracket_close].strip()
        if name_token.lower() == card_name.lower():
            try:
                card_idx = int(idx_token)
                break
            except ValueError:
                continue

    if card_idx is None:
        return None, None

    suffix = f"(hw:{card_idx},{dev_num})"
    name_substr = card_name.lower()
    for idx, dev in enumerate(devices):
        if dev.get(needed_channels, 0) <= 0:
            continue
        dname = str(dev.get("name", "")).lower()
        if name_substr in dname and suffix in dname:
            return idx, dev

    return None, None


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
    ) -> tuple[list[dict], int]:
        """
        Three-check quality gate. Returns ``(warnings, sweep_start_sample)``.

        The second element is the cross-correlation-derived sample index where
        the sweep actually begins in the recording. Callers must use this
        (not ``PRE_DELAY_S * sample_rate``) to align the recording before
        deconvolution — wall-clock alignment drifts with ALSA/PortAudio stream
        startup latency and introduces 20–50 ms of IR peak jitter on variable
        pipelines (ALSA Loopback + CamillaDSP being the worst offender).

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
        # The cross-correlation also pins down *where* the sweep starts in
        # the recording (lag_idx), which we return for alignment.
        #
        # A raw log-sweep×recording correlation is dominated by a broad
        # low-frequency hump that biases argmax toward lag=0 — that bias
        # varies per measurement (ALSA stream-start jitter + clipping
        # interacting differently on each run) and silently contaminates
        # the absolute IR peak time. We peak-detect on the envelope of a
        # bandpassed correlation so the start-of-sweep lag is repeatable.
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
        corr_search = corr[:n]
        try:
            from scipy.signal import butter, sosfiltfilt, hilbert
            sos = butter(4, [30.0, 150.0], btype="band", fs=sample_rate, output="sos")
            corr_bp = sosfiltfilt(sos, corr_search)
            corr_envelope = np.abs(hilbert(corr_bp))
        except Exception as _bxexc:
            corr_envelope = np.abs(corr_search)
            log.warning(
                "sweep-start bandpass/hilbert unavailable (%s); falling back to |corr|",
                _bxexc,
            )
        lag_idx = int(np.argmax(corr_envelope))  # circular lag in [0, n)
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

        return warnings_out, lag_idx

    async def measure(
        self,
        input_device_name: str | None = None,
        playback_device_override: str | None = None,
        freq_min: int | None = None,
        freq_max: int | None = None,
        out_channel_override: int | None = None,
        route: str | None = None,
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
            freq_min: optional lower frequency bound for the sweep. If None,
                falls back to ``config.measurement.freq_min`` (default 20 Hz —
                the right value for sub-only sweeps). Per-call override lets
                a recipe sweep wider for full-range mains characterization.
            freq_max: optional upper frequency bound for the sweep. If None,
                falls back to ``config.measurement.freq_max`` (default 200 Hz —
                sub-only band). Mains calibration should pass 20000 Hz here.
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
        if freq_min is None:
            freq_min = int(cfg.get("freq_min", 20))
        else:
            freq_min = int(freq_min)
        if freq_max is None:
            freq_max = int(cfg.get("freq_max", 200))
        else:
            freq_max = int(freq_max)
        duration: float = cfg.get("sweep_duration", 3.0)
        sample_rate: int = cfg.get("sample_rate", 48000)
        in_channel: int = cfg.get("input_channel", 1)
        out_channel: int = out_channel_override if out_channel_override is not None else cfg.get("output_channel", 1)
        # Caller-supplied route wins over cfg — eliminates the foot-gun where
        # mcp_server auto-routes to "hdmi" for sweep_channel=FL but the engine
        # then silently goes USB because cfg.measurement.playback_route="usb".
        if route is None:
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

        # Strategy selection.
        #   - cal-mode override → legacy PortAudio HDMI/USB strategy with the
        #     loopback device handed in via playback_device_override.
        #   - "hdmi" without cal-mode → direct-ALSA aplay subprocess (PortAudio
        #     inside the container can't see vc4hdmi0; aplay can).
        #   - "usb" → unchanged (PortAudio sees miniDSP fine).
        from .drivers.playback import playback_for_route

        use_aplay_hdmi = (
            route == "hdmi"
            and not playback_device_override  # cal-mode keeps PortAudio path
        )
        if use_aplay_hdmi:
            alsa_device = cfg.get("hdmi_playback_device") or "hdmi:CARD=vc4hdmi0,DEV=0"
            hdmi_channels = int(cfg.get("hdmi_channels", 8))
            # Ensure we always have room for the requested out_channel.
            hdmi_channels = max(hdmi_channels, out_channel)
            strategy = playback_for_route(
                route,
                hdmi_alsa_device=alsa_device,
                hdmi_channels=hdmi_channels,
            )
        else:
            strategy = playback_for_route(route)

        # Lock: protects sd.default.device (module-level global) and serializes
        # play_and_record() so concurrent measure() calls don't clobber each other.
        # Uses the module-level lock (not self._lock) so all instances share it.
        async with _MEASURE_LOCK:
            mic_name = input_device_name or self.config._data.get("mic", {}).get("name", "UMIK")
            try:
                import sounddevice as sd
                devices = sd.query_devices()

                # Input device: prefer explicit index, fall back to name search
                mic_idx_cfg = cfg.get("mic_device_index")
                if mic_idx_cfg is not None:
                    umik_idx = int(mic_idx_cfg)
                    log.info("Input device (by index): %s (index %d)", devices[umik_idx]["name"], umik_idx)
                else:
                    umik_idx = _find_umik_device(devices, name_substring=mic_name)
                    if umik_idx is not None:
                        log.info("Input device (by name): %s (index %d)", devices[umik_idx]["name"], umik_idx)
                if umik_idx is not None:
                    out_idx = int(sd.default.device[1])
                    sd.default.device = (umik_idx, out_idx)
            except ImportError:
                pass

            # Select output device based on route — prefer explicit index, fall back to name search
            # Cal-mode override takes priority over the configured route — when
            # a DSP driver is in cal mode, the sweep must be written into the
            # driver's loopback regardless of whether the configured route is
            # USB or HDMI. Without this the HDMI route would happily send the
            # sweep to the AVR while the caller assumed it was bypassing.
            override_handled = False
            if playback_device_override:
                try:
                    import sounddevice as sd
                    devices = sd.query_devices()
                    idx, dev = _resolve_alsa_device_in_portaudio(
                        playback_device_override, devices, want_output=True,
                    )
                    if idx is not None:
                        in_idx = int(sd.default.device[0])
                        sd.default.device = (in_idx, idx)
                        log.info(
                            "Output device (cal-mode override %r): %s (index %d)",
                            playback_device_override, dev["name"], idx,
                        )
                        override_handled = True
                    else:
                        raise RuntimeError(
                            f"cal-mode playback override {playback_device_override!r} "
                            f"did not resolve to any PortAudio output device. "
                            f"Available outputs: "
                            f"{[(i, d['name']) for i, d in enumerate(devices) if d.get('max_output_channels', 0) > 0]}"
                        )
                except ImportError:
                    pass

            if override_handled:
                pass  # device already set above
            elif route == "hdmi":
                try:
                    import sounddevice as sd
                    devices = sd.query_devices()
                    hdmi_idx_cfg = cfg.get("hdmi_device_index")
                    if hdmi_idx_cfg is not None:
                        idx = int(hdmi_idx_cfg)
                        in_idx = int(sd.default.device[0])
                        sd.default.device = (in_idx, idx)
                        log.info("Output device (HDMI by index): %s (index %d)", devices[idx]["name"], idx)
                    else:
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
                            log.info("Output device (HDMI by name): %s (index %d)", dev["name"], idx)
                except ImportError:
                    pass
            elif route == "usb":
                try:
                    import sounddevice as sd
                    devices = sd.query_devices()
                    usb_idx_cfg = cfg.get("usb_device_index")
                    if usb_idx_cfg is not None:
                        idx = int(usb_idx_cfg)
                        in_idx = int(sd.default.device[0])
                        sd.default.device = (in_idx, idx)
                        log.info("Output device (USB by index): %s (index %d)", devices[idx]["name"], idx)
                    else:
                        usb_name = cfg.get("playback_device") or "miniDSP"
                        candidates = [
                            (idx, dev) for idx, dev in enumerate(devices)
                            if dev.get("max_output_channels", 0) > 0 and usb_name.lower() in dev["name"].lower()
                        ]
                        if candidates:
                            idx, dev = candidates[0]
                            in_idx = int(sd.default.device[0])
                            sd.default.device = (in_idx, idx)
                            log.info("Output device (USB by name): %s (index %d)", dev["name"], idx)
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
        # its noise-floor check — the first 500ms must be silence. It also returns
        # the sample index where the sweep actually begins in the recording, derived
        # from an FFT cross-correlation against the reference sweep.
        _warnings, sweep_start_sample = self.validate_recording(
            np, sweep_1d, rec_1d, sample_rate, min_snr_db=min_snr,
        )

        # Align the reference sweep to the recording's layout by PADDING the
        # sweep with leading silence (not stripping the recording). Stripping
        # would truncate the sweep tail after min(len(sweep),len(rec)) capped
        # the usable window, killing low-freq accuracy and SPL. Padding keeps
        # the full sweep analyzable and gives every measurement a shared
        # time anchor so inter-sub differentials reflect pure geometric delay
        # (+ small back-to-back ALSA jitter).
        #
        # Amount padded: PRE_DELAY_S − SWEEP_SAFETY_S, so even if ALSA stream
        # start is ~50 ms late we still have some pre-sweep silence in the
        # deconvolver's view. IR peak lands at (SWEEP_SAFETY_S − jitter) +
        # travel_time; differential between subs cancels the offset.
        expected_offset = int(getattr(strategy, "PRE_DELAY_S", 0.0) * sample_rate)
        SWEEP_SAFETY_S = 0.1
        pre_pad_samples = max(0, expected_offset - int(SWEEP_SAFETY_S * sample_rate))
        if pre_pad_samples > 0:
            sweep_for_deconv = np.concatenate([
                np.zeros(pre_pad_samples, dtype=sweep_1d.dtype),
                sweep_1d,
            ])
        else:
            sweep_for_deconv = sweep_1d
        rec_for_deconv = rec_1d
        if expected_offset > 0:
            drift_ms = 1000.0 * (sweep_start_sample - expected_offset) / sample_rate
            log.info(
                "measurement alignment: xcorr sweep_start=%d samples (expected %d, "
                "drift=%.1f ms); pre_pad=%d samples (shared anchor via sweep pad)",
                sweep_start_sample, expected_offset, drift_ms, pre_pad_samples,
            )

        # Raw recording peak in dBFS (before deconvolution) — used by
        # calibrate_level to compute actual SPL via mic sensitivity.
        # Use rec_for_deconv (post-pre-delay) so the peak reflects the
        # sweep period, not transient noise during the 1s silence window.
        rec_peak_abs = float(np.max(np.abs(rec_for_deconv)))
        rec_peak_dbfs = round(20.0 * np.log10(rec_peak_abs + 1e-12), 1)
        rec_rms = float(np.sqrt(np.mean(rec_for_deconv ** 2)))
        rec_rms_dbfs = round(20.0 * np.log10(rec_rms + 1e-12), 1)

        # Load mic calibration curve if configured
        cal_curve = None
        cal_path = self.config._data.get("mic", {}).get("cal_file")
        if cal_path:
            try:
                cal_curve = parse_umik_cal_curve(cal_path)
                log.info("Loaded mic cal curve from %s (%d points)", cal_path, len(cal_curve))
            except Exception as exc:
                log.warning("Failed to load mic cal file %s: %s", cal_path, exc)

        frequencies, spl, ir_samples, phase, coherence, xcorr_peak_ms = self._compute_fr_arrays(
            np, sweep_for_deconv, rec_for_deconv, freq_min, freq_max, sample_rate,
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
            recording_rms_dbfs=rec_rms_dbfs,
            coherence=coherence,
            xcorr_peak_ms=xcorr_peak_ms,
        )

    # ── Internals ─────────────────────────────────────────────────────────

    # Default IR gate length in seconds.  The gate removes circular-FFT
    # wrap-around artifacts that appear as comb-like nulls every 3rd bin
    # above ~55 Hz.  500 ms gives 10+ cycles at 20 Hz (adequate spectral
    # resolution for bass) while cutting well before the artifact at
    # ~N/3 samples (~1.8 s).
    IR_GATE_S: float = 0.5
    IR_TAPER_S: float = 0.05
    """Half-Hanning taper duration at the gate boundary (seconds)."""

    def _compute_fr_arrays(
        self,
        np,
        sweep_array,   # 1-D float64 ndarray
        rec_array,     # 1-D float64 ndarray
        freq_min: int,
        freq_max: int,
        sample_rate: int,
        cal_curve: list[tuple[float, float]] | None = None,
    ) -> tuple[list[float], list[float], list[float], list[float], list[float] | None, float]:
        """
        Core deconvolution on raw numpy arrays.

        Deconvolves via H(f) = Y·X* / (|X|² + ε), then gates the
        impulse response in the time domain before converting back to
        the frequency domain for a clean FR.

        Also computes cross-correlation peak timing for IR onset detection.
        Cross-correlation (C = IFFT(conj(X)·Y)) has no division, so it
        avoids the DC artifact that Wiener deconvolution creates near t=0.

        The PyTTa sweep buffer contains leading/trailing silence.  When
        the room's reverb tail (T60 > 1 s for bass modes) wraps around
        in the circular FFT, the raw H(f) = Y/X shows comb-like nulls
        every 3 bins above ~55 Hz.  IR gating — deconvolve → IFFT →
        window the IR → FFT — is the standard fix used by REW and
        other measurement tools.

        Returns (frequencies, magnitude_db, ir_samples, phase_radians,
                 coherence, xcorr_peak_ms).
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

        # ── Cross-correlation for timing ─────────────────────────────
        # Plain C = IFFT(conj(X)·Y) is useless: a log sweep has most of its
        # energy at low frequencies, so the cross-correlation is dominated
        # by a broad low-frequency hump that peaks at lag 0 regardless of
        # actual travel time. We bandlimit to the sub's operating range
        # (30–150 Hz) and peak on the Hilbert envelope within a physical
        # travel-time window (≥1 ms, ≤20 ms → 0.3–6.9 m path).
        C_full = np.fft.irfft(np.conj(X) * Y, n=n)
        try:
            from scipy.signal import butter, sosfiltfilt, hilbert
            sos = butter(4, [30.0, 150.0], btype="band", fs=sample_rate, output="sos")
            C_bp = sosfiltfilt(sos, C_full)
            envelope = np.abs(hilbert(C_bp))
        except Exception as _xexc:  # scipy missing or filter edge case
            envelope = np.abs(C_full)
            log.warning("xcorr bandpass/hilbert unavailable (%s); using raw |C|", _xexc)
        # After fixed-strip in measure(), the IR is anchored at (PRE_DELAY_S −
        # SWEEP_SAFETY_S) relative to play start, so the direct arrival sits at
        # (~100 ms pre-sweep silence residue) + ALSA-start-jitter + sub→mic
        # travel. Search the whole first 200 ms to catch it even with jitter.
        lo_idx = max(1, int(0.001 * sample_rate))
        hi_idx = min(n, int(0.200 * sample_rate))
        if hi_idx <= lo_idx:
            hi_idx = min(n, lo_idx + 1)
        rel_idx = int(np.argmax(envelope[lo_idx:hi_idx]))
        xcorr_peak_idx = lo_idx + rel_idx
        xcorr_peak_ms = round(xcorr_peak_idx / sample_rate * 1000.0, 3)
        xcorr_peak_sign = 1 if C_full[xcorr_peak_idx] >= 0.0 else -1
        log.info(
            "xcorr peak: %.3f ms (sample %d of window [%d,%d)), sign=%+d",
            xcorr_peak_ms, xcorr_peak_idx, lo_idx, hi_idx, xcorr_peak_sign,
        )

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
        gate_samples = min(int(self.IR_GATE_S * sample_rate), n)
        ir_samples = ir_full[:gate_samples].tolist()  # store unwindowed for decay analysis
        taper_samples = min(int(self.IR_TAPER_S * sample_rate), gate_samples // 4)

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

        # Per-frequency reliability via IR-tail SNR.
        #
        # Welch coherence on a sweep stimulus is invalid: a swept sine is
        # non-stationary, so within any single sweep most Welch segments
        # contain no signal at any given frequency bin. The averaged
        # cross-power gets diluted by silent segments while the recording's
        # autopower stays inflated by ambient noise, pinning coherence near
        # zero independently of measurement quality. The effect worsens at
        # low frequency, where each band is visited briefly during the
        # sweep — exactly where reliability matters most for sub work.
        #
        # The right metric for a deconvolved sweep is per-bin SNR comparing
        # the early IR (signal) against the late IR tail (noise floor),
        # mapped to a coherence-like [0, 1] reliability via the standard
        # γ² = SNR / (1 + SNR). This is what REW and Smaart report as
        # "coherence" for swept-sine measurements.
        coh_list: list[float] | None = None
        try:
            tail_start = gate_samples
            tail_len = gate_samples
            if tail_start + tail_len <= n:
                sig_window = ir_full[:gate_samples]
                noise_window = ir_full[tail_start:tail_start + tail_len]
                H_sig = np.fft.rfft(sig_window, n=n)
                H_noise = np.fft.rfft(noise_window, n=n)
                sig_pow = np.abs(H_sig) ** 2
                noise_pow = np.abs(H_noise) ** 2
                snr = sig_pow / (noise_pow + 1e-30)
                gamma_sq = snr / (1.0 + snr)
                coh_list = [round(float(c), 3) for c in gamma_sq[mask]]
        except Exception as exc:
            log.warning("coherence computation failed: %s", exc)

        return freq_list, spl_list, ir_samples, phase_rad[mask].tolist(), coh_list, xcorr_peak_ms

    def _compute_fr(
        self,
        np,
        sweep,       # PyTTa SignalObj
        recording,   # PyTTa SignalObj
        freq_min: int,
        freq_max: int,
        sample_rate: int,
        cal_curve: list[tuple[float, float]] | None = None,
    ) -> tuple[list[float], list[float], list[float], list[float], list[float] | None, float]:
        """Wrapper that extracts numpy arrays from PyTTa SignalObj inputs."""
        x = sweep.timeSignal[:, 0]
        y = recording.timeSignal[:, 0]
        return self._compute_fr_arrays(np, x, y, freq_min, freq_max, sample_rate, cal_curve=cal_curve)


# ── IR onset detection constants ──────────────────────────────────────────────

IR_ONSET_BAND_HZ: tuple[float, float] = (15.0, 200.0)
"""Bandpass range for IR onset detection (fallback when xcorr_peak_ms unavailable).

Used only for legacy stored sessions that lack cross-correlation timing.
"""

IR_ONSET_THRESHOLD_DB: float = -20.0
"""Onset threshold relative to the absolute IR peak, in dB.

The first sample whose amplitude exceeds 10^(threshold/20) × peak is
reported as the onset.  -20 dB (×0.1) finds the first arrival even when a
later room mode is louder than the direct sound.
"""


def detect_ir_onset(
    ir: "np.ndarray",
    sample_rate: int,
    search_window_ms: float = 50.0,
    xcorr_peak_ms: float | None = None,
    min_sustained_ms: float = 4.0,
    fir_pre_delay_ms: float = 0.0,
) -> dict:
    """Detect the onset of an impulse response, returning peak timing and level.

    Primary path: uses cross-correlation peak timing (xcorr_peak_ms) computed
    during deconvolution. Cross-correlation (C = IFFT(conj(X)·Y)) has no
    division, so it doesn't suffer from the DC artifact that Wiener
    deconvolution creates near t=0.

    Fallback (legacy sessions without xcorr_peak_ms): bandpass filters the
    deconvolved IR to the sub's operating range to suppress the DC artifact.
    Searches the **full** IR rather than the first ``search_window_ms`` ms so
    that legacy sessions whose IR was recorded before the alignment fix (and
    therefore contains leading pre-sweep silence) return a non-zero peak time
    reflecting the actual travel time from the sub to the mic. Sessions
    recorded after the alignment fix have their pre-sweep silence stripped
    before the IR is computed, so their peak is within the first 50 ms.

    Returns:
        {peak_time_ms, peak_sign, spl_db, sample_rate}

    ``peak_time_ms`` is the absolute travel time from the sub to the mic in ms
    (measured from the start of the stored IR). Two calls on different solo-sub
    sessions produce values whose difference is the delay offset to apply via
    ``set_delay``.
    """
    import numpy as np

    # IR-domain onset detection: bandpass the deconvolved IR to suppress the
    # Wiener-deconvolution DC artifact, find the largest absolute peak in the
    # bandpassed result (the dominant feature — direct arrival when not
    # masked, room mode resonance when the listening position sits in a
    # null), then walk back to the FIRST sample whose absolute envelope
    # crosses ``IR_ONSET_THRESHOLD_DB`` below that peak. That ``first
    # crossing'' is the direct arrival.
    #
    # We deliberately DO NOT short-circuit on ``xcorr_peak_ms``. The xcorr
    # cross-correlation envelope's argmax tracks the LARGEST envelope
    # feature, which on transients-vs-resonance comparisons becomes the
    # resonance time, not the direct arrival. Using the IR-domain onset
    # threshold gives the right answer in both cases.
    # Skip the first ~1 ms of the IR to avoid the Wiener-deconvolution DC
    # artifact at t=0 without bandpassing. We previously bandpassed the IR to
    # suppress the DC artifact, but that destroys transient amplitudes —
    # a 60 Hz resonance burst (sustained tone, in-band) survives bandpass at
    # near-full magnitude while a sub's direct-arrival impulse loses 40+ dB.
    # The result was that any IR with a strong room mode would have its
    # bandpassed onset land in the resonance, not on the direct arrival.
    # Skipping the leading 1 ms gives us the same DC protection without the
    # transient destruction.
    # Skip the leading 1 ms to avoid the Wiener-deconvolution DC artifact
    # at t=0.
    skip_samples = int(0.001 * sample_rate)
    if len(ir) <= skip_samples + 8:
        skip_samples = 0
    ir_search = ir[skip_samples:]

    abs_ir = np.abs(ir_search)
    max_idx_local = int(np.argmax(abs_ir))
    max_idx = max_idx_local + skip_samples

    onset_ratio = 10.0 ** (IR_ONSET_THRESHOLD_DB / 20.0)  # 0.1 for -20 dB
    onset_threshold = abs_ir[max_idx_local] * onset_ratio
    onset_candidates_local = np.where(abs_ir >= onset_threshold)[0]
    # FIR-pre-ring guard (opt-in via fir_pre_delay_ms > 0): the FIR's
    # anti-pulse content sits in the window
    # ``[main_peak - fir_pre_delay_ms, main_peak]`` and would otherwise be
    # mistaken for the room's direct arrival. Drop any onset candidate
    # inside that window, leaving either (a) candidates earlier than the
    # FIR window (a legitimate earlier room arrival, e.g., a closer sub)
    # or (b) the main peak itself. Only applies when caller hints the FIR
    # pre-delay; default 0 preserves legacy behavior.
    if fir_pre_delay_ms > 0 and len(onset_candidates_local) > 0:
        fir_pre_samples = int(float(fir_pre_delay_ms) * 1e-3 * sample_rate)
        zone_start = max(0, max_idx_local - fir_pre_samples)
        in_zone = (onset_candidates_local >= zone_start) & (
            onset_candidates_local < max_idx_local
        )
        cleaned = onset_candidates_local[~in_zone]
        if len(cleaned) > 0:
            onset_candidates_local = cleaned
    peak_idx_local = (
        int(onset_candidates_local[0])
        if len(onset_candidates_local) > 0
        else max_idx_local
    )
    peak_idx = peak_idx_local + skip_samples
    # ``min_sustained_ms`` is reserved for a future sustained-energy gate.
    _ = min_sustained_ms

    # Polarity reads the dominant impulse, not the onset crossing.
    peak_sign = 1 if ir[max_idx] >= 0.0 else -1
    peak_time_s = peak_idx / sample_rate
    spl_db = float(20.0 * np.log10(abs_ir[max_idx_local] + 1e-12))

    return {
        "peak_time_ms": round(peak_time_s * 1000.0, 3),
        "peak_sign": peak_sign,
        "spl_db": round(spl_db, 1),
        "sample_rate": sample_rate,
    }


def compute_session_metadata(
    fr: FrequencyResponse,
    search_window_ms: float = 50.0,
    decay_freq_min: float = 20.0,
    decay_freq_max: float = 200.0,
    t60_threshold_ms: float = 300.0,
    fir_pre_delay_ms: float = 0.0,
) -> dict:
    """Compute IR-derived metadata from a FrequencyResponse at capture time.

    Returns a dict with:
      ir:           {peak_time_ms, peak_sign, spl_db, sample_rate}
      decay_modes:  [{freq_hz, t60_ms, peak_db, suggested_q, priority}, ...]
      group_delay:  {freq_hz: [...], delay_ms: [...]}
    """
    import numpy as np

    metadata: dict = {}
    sample_rate = fr.sample_rate or 48000

    # ── IR peak analysis (replaces analyze_ir) ────────────────────────────
    if fr.impulse_response:
        ir_arr = np.array(fr.impulse_response, dtype=np.float64)
        metadata["ir"] = detect_ir_onset(
            ir_arr, sample_rate, search_window_ms,
            xcorr_peak_ms=fr.xcorr_peak_ms,
            fir_pre_delay_ms=fir_pre_delay_ms,
        )

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
