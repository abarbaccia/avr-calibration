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
    loopback_xcorr_peak_ms: Optional[float] = None  # xcorr(ref, mic): CamillaDSP latency + acoustic travel
    avr_processing_ms: Optional[float] = None       # xcorr(sweep, ref): AVR processing delay

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
        data.setdefault("loopback_xcorr_peak_ms", None)  # backward compat
        data.setdefault("avr_processing_ms", None)  # backward compat
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


def design_weighting_filter(weighting: str, sample_rate: int):
    """Return (b, a) biquad-cascade coefficients for IEC 61672 weighting.

    Args:
        weighting: 'A', 'C', or 'Z' (case-insensitive). 'Z' returns identity.
        sample_rate: target audio sample rate in Hz.

    Returns:
        (b, a) for `scipy.signal.lfilter`.

    A-weighting: rolls off bass + extreme HF, models ear at quiet levels.
    C-weighting: ~flat 30 Hz - 10 kHz with mild rolloff at extremes, models
        ear at loud levels — used for cinema reference (Dolby/THX 75 dB target).
    Z-weighting: no weighting (linear).
    """
    import numpy as np
    from scipy.signal import bilinear

    w = weighting.upper()
    if w == "Z":
        return np.array([1.0]), np.array([1.0])

    pi = np.pi
    if w == "C":
        # C-weighting (IEC 61672): poles at ±f1 (×2) and ±f4 (×2).
        f1 = 20.598997
        f4 = 12194.217
        # Analog transfer: H(s) = K · s² / [(s + 2πf1)² · (s + 2πf4)²]
        # We use the gain that makes |H(jω)| = 1 at 1 kHz (the standard).
        nums = np.array([(2 * pi * f4) ** 2, 0.0, 0.0])
        dens = np.poly([
            -2 * pi * f1, -2 * pi * f1,
            -2 * pi * f4, -2 * pi * f4,
        ])
    elif w == "A":
        # A-weighting: poles at ±f1 (×2), ±f2, ±f3, ±f4 (×2).
        f1 = 20.598997
        f2 = 107.65265
        f3 = 737.86223
        f4 = 12194.217
        nums = np.array([(2 * pi * f4) ** 2, 0.0, 0.0, 0.0, 0.0])
        dens = np.poly([
            -2 * pi * f1, -2 * pi * f1,
            -2 * pi * f2,
            -2 * pi * f3,
            -2 * pi * f4, -2 * pi * f4,
        ])
    else:
        raise ValueError(f"weighting must be 'A', 'C', or 'Z', got {weighting!r}")

    # Bilinear transform to digital. scipy.signal.bilinear handles the prewarp.
    b, a = bilinear(nums, dens, fs=float(sample_rate))
    # Normalize so |H(j2π·1000)| = 1 (= 0 dB at 1 kHz, per the standards).
    from scipy.signal import freqz
    _, h_at_ref = freqz(b, a, worN=[1000.0], fs=float(sample_rate))
    gain_at_ref = np.abs(h_at_ref[0])
    if gain_at_ref > 0:
        b = b / gain_at_ref
    return b, a


def measure_pink_spl(
    *,
    duration_s: float = 10.0,
    level_dbfs: float = -20.0,
    sample_rate: int = 48000,
    weighting: str = "C",
    play_buffer: "np.ndarray | None" = None,
    output_channel: int = 0,
    n_output_channels: int = 6,
    mic_device_index: int | None = None,
    hdmi_device_index: int | None = None,
    umik_cal_path: str | None = None,
    integration_time_s: float = 1.0,
) -> dict:
    """Play pink noise on one channel, record from UMIK, return absolute SPL.

    Generates ``duration_s`` of pink noise at ``level_dbfs`` (RMS), plays on
    the chosen output channel via HDMI, captures from the UMIK, and computes:

      - time-averaged dB SPL with the requested weighting (default C, cinema)
      - peak SPL
      - per-block time series of SPL values (``integration_time_s`` blocks)

    Returns dict with: ``spl_db``, ``spl_peak_db``, ``per_block_db``,
    ``weighting``, ``level_dbfs``, ``recording_peak_dbfs``, ``calibrated``.

    UMIK calibration: if ``umik_cal_path`` is provided AND the file's header
    parses cleanly, the dBFS→dB SPL offset is applied. Otherwise the result
    is reported as relative dBFS (``calibrated`` field will be False).
    """
    import numpy as np
    from scipy.signal import lfilter

    rng = np.random.default_rng(42)
    n_samples = int(duration_s * sample_rate)

    if play_buffer is None:
        # Pink noise via Voss-McCartney would be lower-overhead, but spectral
        # accuracy from FFT-shaping is more important for SPL measurement.
        white = rng.normal(size=n_samples).astype(np.float32)
        # Shape spectrum to 1/f (pink): multiply FFT by 1/sqrt(f).
        spec = np.fft.rfft(white)
        freqs = np.fft.rfftfreq(n_samples, 1.0 / sample_rate)
        freqs[0] = freqs[1]  # avoid div-by-zero at DC
        spec = spec / np.sqrt(freqs)
        pink = np.fft.irfft(spec, n=n_samples).astype(np.float32)
        # Normalise to target RMS = level_dbfs.
        target_rms = 10.0 ** (level_dbfs / 20.0)
        actual_rms = float(np.sqrt(np.mean(pink ** 2)))
        if actual_rms > 0:
            pink = pink * (target_rms / actual_rms)
        # Hard-clip to ±1.0 (rare for pink at -20 dBFS, but pathological
        # peaks happen at long durations).
        pink = np.clip(pink, -1.0, 1.0)
        # Build multichannel buffer with pink on the chosen channel only.
        # MultichannelPlayback expects int16 for HDMI (sounddevice quirk).
        buf_f32 = np.zeros((n_samples, n_output_channels), dtype=np.float32)
        buf_f32[:, output_channel] = pink
        buf = (buf_f32 * 32767.0).astype(np.int16)
    else:
        buf = play_buffer

    # Play + record using the same MultichannelPlayback path as
    # play_and_measure_fft.
    from .drivers.playback import MultichannelPlayback
    import sounddevice as sd

    devices = sd.query_devices()
    if mic_device_index is None:
        mic_device_index = _find_umik_device(devices)
    if mic_device_index is None:
        raise RuntimeError("UMIK not found")
    if hdmi_device_index is None:
        cands = [
            (i, d) for i, d in enumerate(devices)
            if d["max_output_channels"] > 0 and "hdmi" in d["name"].lower()
        ]
        cands.sort(key=lambda x: (x[1]["name"].lower() != "hdmi", len(x[1]["name"])))
        if cands:
            hdmi_device_index = cands[0][0]
    if hdmi_device_index is None:
        raise RuntimeError("No HDMI output device found")

    player = MultichannelPlayback()
    recording, n_rec = player.play_and_record(
        buf, sample_rate, mic_device_index, hdmi_device_index,
    )
    if len(recording) == 0:
        raise RuntimeError("Recording is empty — no audio captured")

    rec = np.asarray(recording, dtype=np.float64)
    # Apply weighting filter to the time-domain recording.
    b, a = design_weighting_filter(weighting, sample_rate)
    weighted = lfilter(b, a, rec) if len(b) > 1 else rec

    # Trim head + tail to skip start-up transients (typical SPL meter "slow"
    # is 1 s exponential; we approximate with hard 1-s window blocks after
    # trimming the first 0.5 s).
    skip = int(0.5 * sample_rate)
    if skip < len(weighted):
        weighted = weighted[skip:]

    block_size = int(integration_time_s * sample_rate)
    if block_size <= 0 or len(weighted) < block_size:
        block_size = len(weighted)
    n_blocks = max(1, len(weighted) // block_size)
    per_block_rms = []
    for i in range(n_blocks):
        chunk = weighted[i * block_size : (i + 1) * block_size]
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        per_block_rms.append(rms)
    overall_rms = float(np.sqrt(np.mean(weighted ** 2)))

    # Convert dBFS → dB SPL via UMIK calibration if available.
    offset = 0.0
    calibrated = False
    if umik_cal_path:
        try:
            offset = parse_umik_sensitivity(umik_cal_path)
            calibrated = True
        except (FileNotFoundError, ValueError):
            offset = 0.0
            calibrated = False

    def to_spl(rms: float) -> float:
        if rms <= 0:
            return -200.0
        dbfs = 20.0 * np.log10(rms)
        return float(dbfs + offset)

    overall_spl = to_spl(overall_rms)
    per_block_spl = [to_spl(r) for r in per_block_rms]
    peak_abs = float(np.max(np.abs(weighted)))
    peak_spl = (
        20.0 * np.log10(peak_abs) + offset if peak_abs > 0 else -200.0
    )

    return {
        "spl_db": round(overall_spl, 2),
        "spl_peak_db": round(peak_spl, 2),
        "per_block_db": [round(v, 2) for v in per_block_spl],
        "n_blocks": n_blocks,
        "weighting": weighting.upper(),
        "level_dbfs": level_dbfs,
        "duration_s": duration_s,
        "calibrated": calibrated,
        "umik_offset_db": round(offset, 2) if calibrated else None,
    }


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
        #   - "hdmi" → direct-ALSA aplay subprocess (PortAudio inside the
        #     container can't see vc4hdmi0; aplay can).
        #   - "usb" → PortAudio (sees the Focusrite fine).
        from .drivers.playback import playback_for_route

        use_aplay_hdmi = route == "hdmi"
        loopback_ref_device: str | None = cfg.get("loopback_ref_device")
        loopback_ref_channels: int = int(cfg.get("loopback_ref_channels", 1))
        loopback_ref_channel_index: int = int(cfg.get("loopback_ref_channel_index", 1))

        mic_pipewire_node = cfg.get("mic_pipewire_node") or None
        loopback_ref_pw_node = cfg.get("loopback_ref_pipewire_node") or None
        loopback_ref_pw_channels = int(cfg.get("loopback_ref_pw_channels", 1))

        # ⚠️  LOUD WARNING when running without a loopback reference.
        # Empirically (2026-05-27) causes 4–10 dB run-to-run SPL jitter and
        # collapses coherence, because PipeWire schedules play and record
        # streams independently → analytical-sweep deconvolution sees
        # per-stream jitter as phase smear. With loopback both reference and
        # mic share the same scheduling regime so jitter is common-mode.
        if not loopback_ref_pw_node and not loopback_ref_device:
            warning_msg = (
                "⚠️  LOOPBACK REFERENCE NOT CONFIGURED — measurement results "
                "will NOT be repeatable (4–10 dB run-to-run SPL jitter, "
                "coherence collapse). Filter A/B comparisons CANNOT be "
                "trusted. Set measurement.loopback_ref_pipewire_node in "
                "config.yaml to enable a recorded reference."
            )
            log.warning("=" * 78)
            log.warning(warning_msg)
            log.warning("=" * 78)

        usb_pipewire_node = cfg.get("usb_pipewire_node") or None
        usb_sweep_fifo = cfg.get("usb_sweep_fifo") or None

        # host_pipewire_pid_file / nsenter path kept for reference but superseded
        # by the FIFO approach (usb_sweep_fifo).  nsenter doesn't work from a
        # Docker container because the container's /proc doesn't expose host PIDs.
        host_pw_pid: int | None = None
        host_pw_pid_file = cfg.get("host_pipewire_pid_file") or None
        if host_pw_pid_file:
            try:
                host_pw_pid = int(open(host_pw_pid_file).read().strip())
            except Exception as _e:
                log.warning("Could not read host_pipewire_pid_file %s: %s", host_pw_pid_file, _e)

        if use_aplay_hdmi:
            # Use PipeWire natively via pw-cat --target <node>.  The ALSA
            # default:CARD= syntax fails inside the container, and aplay
            # routes through an ALSA bridge anyway.  pw-cat speaks PipeWire
            # directly — no bridge, no plugin lookup.
            #
            # Channel count cap at 6: the vc4-hdmi driver (Linux 6.8) has
            # limited chmaps.  6-ch with FL,FR,LFE,FC,RL,RR gets the AVR
            # into multichannel mode and makes FL/FR/FC/RL/RR all reachable.
            # 8-ch silently downmixes to stereo.
            hdmi_pipewire_node = cfg.get("hdmi_pipewire_node") or cfg.get("hdmi_playback_device")
            hdmi_channels = int(cfg.get("hdmi_channels", 6))
            # Ensure we always have room for the requested out_channel,
            # but cap at 6 — see comment above for the chmap rationale.
            hdmi_channels = min(max(hdmi_channels, out_channel), 6)
            strategy = playback_for_route(
                route,
                hdmi_pipewire_node=hdmi_pipewire_node,
                hdmi_channels=hdmi_channels,
                capture_pipewire_node=mic_pipewire_node,
                loopback_ref_device=loopback_ref_device,
                loopback_ref_channels=loopback_ref_channels,
                loopback_ref_channel_index=loopback_ref_channel_index,
                loopback_ref_pipewire_node=loopback_ref_pw_node,
                loopback_ref_pw_channels=loopback_ref_pw_channels,
            )
        elif usb_sweep_fifo:
            # FIFO bridge: write PCM bytes to a named pipe; the host-side
            # avr-sweep-player daemon reads and plays via pw-cat (PW 1.2.7).
            # This bypasses the PW 0.3.65 vs 1.2.7 version mismatch that prevents
            # the container from running pw-cat directly against avr_cal_sweep.
            # The FIFO write blocks at hardware rate — natural sync, no extra sleep.
            from .drivers.playback import FIFOPlayback, LoopbackRefPlayback
            base: PlaybackStrategy = FIFOPlayback(
                fifo_path=usb_sweep_fifo,
                channels=2,
                capture_pipewire_node=mic_pipewire_node,
            )
            if loopback_ref_pipewire_node is not None or loopback_ref_device is not None:
                strategy = LoopbackRefPlayback(
                    base=base,
                    ref_device=loopback_ref_device or "",
                    ref_channels=loopback_ref_channels,
                    ref_channel_index=loopback_ref_channel_index,
                    ref_pipewire_node=loopback_ref_pw_node,
                    ref_pw_channels=loopback_ref_pw_channels,
                )
            else:
                strategy = base
        elif usb_pipewire_node:
            # USB route targeting a PipeWire node (e.g. avr_cal_sweep null sink).
            # sounddevice/PortAudio cannot see PW virtual nodes, so use pw-cat
            # via the HDMI pw-cat path with the USB node as the target.
            # avr_cal_sweep is stereo — cap at 2 channels.
            # skip_warmup=True: null sinks need no AVR PCM lock warmup.
            # host_pw_pid: use nsenter so the HOST's pw-cat (1.2.7) runs instead
            # of the container's 0.3.65 — version mismatch causes scheduling hangs.
            pw_channels = min(max(2, out_channel), 2)
            strategy = playback_for_route(
                "hdmi",
                hdmi_pipewire_node=usb_pipewire_node,
                hdmi_channels=pw_channels,
                capture_pipewire_node=mic_pipewire_node,
                loopback_ref_device=loopback_ref_device,
                loopback_ref_channels=loopback_ref_channels,
                loopback_ref_channel_index=loopback_ref_channel_index,
                loopback_ref_pipewire_node=loopback_ref_pw_node,
                loopback_ref_pw_channels=loopback_ref_pw_channels,
                hdmi_skip_warmup=True,
                host_pipewire_pid=host_pw_pid,
            )
        else:
            strategy = playback_for_route(
                route,
                capture_pipewire_node=mic_pipewire_node,
                loopback_ref_device=loopback_ref_device,
                loopback_ref_channels=loopback_ref_channels,
                loopback_ref_channel_index=loopback_ref_channel_index,
                loopback_ref_pipewire_node=loopback_ref_pw_node,
                loopback_ref_pw_channels=loopback_ref_pw_channels,
            )

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
            if route == "hdmi":
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
                # USB route, post v0.2.0 PipeWire migration:
                #   sweep -> PortAudio -> pipewire-alsa default PCM -> the
                #   `avr_cal_sweep` PipeWire null sink (created by the host
                #   avr-cal-sweep-link.service) -> camilladsp_capture -> subs.
                #
                # PortAudio's ALSA backend doesn't enumerate PipeWire nodes by
                # name, but the pipewire-alsa shim honors PIPEWIRE_NODE on the
                # ALSA `default` PCM. We pick the default device + set the env
                # var so PipeWire routes our output to the named sink.
                #
                # Legacy path (pre-PipeWire, direct miniDSP USB) still works:
                # if the configured name resolves to a PortAudio device, we
                # pin sd.default.device to it and skip the PIPEWIRE_NODE trick.
                try:
                    import os
                    import sounddevice as sd
                    devices = sd.query_devices()
                    usb_idx_cfg = cfg.get("usb_device_index")
                    usb_name = cfg.get("playback_device") or "miniDSP"
                    if usb_idx_cfg is not None:
                        idx = int(usb_idx_cfg)
                        in_idx = int(sd.default.device[0])
                        sd.default.device = (in_idx, idx)
                        log.info("Output device (USB by index): %s (index %d)", devices[idx]["name"], idx)
                    else:
                        candidates = [
                            (idx, dev) for idx, dev in enumerate(devices)
                            if dev.get("max_output_channels", 0) > 0 and usb_name.lower() in dev["name"].lower()
                        ]
                        if candidates:
                            # Direct PortAudio match (e.g. "miniDSP" on a
                            # pre-PipeWire setup, or "pipewire" host API).
                            idx, dev = candidates[0]
                            in_idx = int(sd.default.device[0])
                            sd.default.device = (in_idx, idx)
                            log.info("Output device (USB by name): %s (index %d)", dev["name"], idx)
                        else:
                            # No direct match → assume this is a PipeWire
                            # node name and route via pipewire-alsa default.
                            default_idx = None
                            for idx, dev in enumerate(devices):
                                if (dev.get("max_output_channels", 0) > 0 and
                                        dev.get("name", "").lower() == "default"):
                                    default_idx = idx
                                    break
                            if default_idx is not None:
                                in_idx = int(sd.default.device[0])
                                sd.default.device = (in_idx, default_idx)
                                os.environ["PIPEWIRE_NODE"] = usb_name
                                log.info(
                                    "Output device (USB via PipeWire): default "
                                    "(index %d) PIPEWIRE_NODE=%s",
                                    default_idx, usb_name,
                                )
                            else:
                                log.warning(
                                    "USB route: no PortAudio match for %r and "
                                    "no ALSA `default` device available — sweep "
                                    "playback will use sd.default unchanged.",
                                    usb_name,
                                )
                except ImportError:
                    pass

            # Resolve optional loopback device. Loopback gives a clean
            # cross-correlation anchor (no room reflections) so sweep-start
            # Run blocking play_and_record() in a thread executor so the asyncio event
            # loop stays responsive during PortAudio I/O. Times out after 60s to prevent
            # a hung audio device from blocking the calibration loop indefinitely.
            loop = asyncio.get_running_loop()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, strategy.play_and_record, sweep, sample_rate, in_channel, out_channel
                    ),
                    timeout=_MEASUREMENT_TIMEOUT_S,
                )
                if len(result) == 3:
                    sweep_1d, rec_1d, ref_1d = result
                else:
                    sweep_1d, rec_1d = result
                    ref_1d = None
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
        #
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

        # Architectural fix: when a recorded loopback reference is available,
        # deconvolve mic against ref (not against the analytical sweep template).
        # PipeWire schedules play and record streams independently — using the
        # analytical sweep as the deconvolution reference makes any per-stream
        # jitter show up as phase smear → per-bin coherence collapses.
        # Cross-correlating two RECORDED signals (mic and ref) under the same
        # PipeWire scheduling regime cancels common-mode jitter.
        # See LoopbackRefPlayback docstring (drivers/playback.py:521-527).
        if ref_1d is not None and not np.all(ref_1d == 0):
            n_aligned = min(len(ref_1d), len(rec_1d))
            deconv_x = ref_1d[:n_aligned].astype(np.float64)
            deconv_y = rec_1d[:n_aligned].astype(np.float64)
            log.info(
                "deconvolution: using recorded loopback ref (n=%d) as X — "
                "PipeWire jitter common-mode cancels",
                n_aligned,
            )
        else:
            deconv_x = sweep_for_deconv
            deconv_y = rec_for_deconv

        frequencies, spl, ir_samples, phase, coherence, xcorr_peak_ms = self._compute_fr_arrays(
            np, deconv_x, deconv_y, freq_min, freq_max, sample_rate,
            cal_curve=cal_curve,
        )

        loopback_xcorr_peak_ms: Optional[float] = None
        avr_processing_ms: Optional[float] = None
        if ref_1d is not None and not np.all(ref_1d == 0):
            loopback_xcorr_peak_ms = _xcorr_delay_ms(np, ref_1d, rec_for_deconv, sample_rate)
            # Use unpadded sweep_1d: sweep_for_deconv includes pre_pad_samples offset
            # that would inflate avr_processing_ms by ~pre_delay_s seconds.
            avr_processing_ms = _xcorr_delay_ms(np, sweep_1d, ref_1d, sample_rate)
            log.info(
                "loopback: avr_processing=%.3f ms  loopback_xcorr=%.3f ms  "
                "sum=%.3f ms  xcorr_peak=%.3f ms  delta=%.3f ms",
                avr_processing_ms or 0.0,
                loopback_xcorr_peak_ms or 0.0,
                (avr_processing_ms or 0.0) + (loopback_xcorr_peak_ms or 0.0),
                xcorr_peak_ms or 0.0,
                ((avr_processing_ms or 0.0) + (loopback_xcorr_peak_ms or 0.0)) - (xcorr_peak_ms or 0.0),
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
            loopback_xcorr_peak_ms=loopback_xcorr_peak_ms,
            avr_processing_ms=avr_processing_ms,
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
        # actual travel time. Bandlimit the correlation to the sweep's
        # actual frequency range and peak on the Hilbert envelope within
        # a physical travel-time window.
        #
        # Bandpass selection (2026-05-08 fix): previously hardcoded 30-150 Hz
        # (sub band). For mains sweeps (100-5000 Hz), most direct sound
        # energy is ABOVE 150 Hz — the old bandpass filtered the signal
        # to noise, causing argmax to lock onto ALSA stream-startup
        # transients at lag ≈ 0. Now: use a band intersecting the sweep
        # range and the audible direct-sound band (~80-2000 Hz upper
        # limit gives broadband content while excluding HF reflections).
        bp_lo = max(30.0, float(freq_min) * 1.2)
        bp_hi = min(2000.0, float(freq_max) * 0.8, sample_rate * 0.4)
        if bp_hi <= bp_lo + 50.0:
            bp_lo = max(30.0, float(freq_min))
            bp_hi = min(sample_rate * 0.4, float(freq_max))
        C_full = np.fft.irfft(np.conj(X) * Y, n=n)
        try:
            from scipy.signal import butter, sosfiltfilt, hilbert
            sos = butter(4, [bp_lo, bp_hi], btype="band", fs=sample_rate, output="sos")
            C_bp = sosfiltfilt(sos, C_full)
            envelope = np.abs(hilbert(C_bp))
        except Exception as _xexc:  # scipy missing or filter edge case
            envelope = np.abs(C_full)
            log.warning("xcorr bandpass/hilbert unavailable (%s); using raw |C|", _xexc)
        # Floor at 3 ms (≈1 m acoustic) — skip ALSA stream-startup transients
        # that consistently produce a spurious argmax peak at lag ≈ 0.
        lo_idx = max(1, int(0.003 * sample_rate))
        hi_idx = min(n, int(0.200 * sample_rate))
        if hi_idx <= lo_idx:
            hi_idx = min(n, lo_idx + 1)
        # ── First-arrival onset detection (2026-05-08 fix) ─────────────
        # Replaces argmax (which locks on the LARGEST envelope peak — often
        # a strong reflection or AVR processing artifact) with first-arrival
        # detection: find the first sample where envelope rises above a
        # threshold proportional to the global peak. Direct sound is the
        # FIRST significant arrival; reflections/processing artifacts
        # come later. argmax IR analysis was producing absurd inter-channel
        # delta times (5 to 150 ms range across mains) because it picked
        # a different feature on each channel. Onset detection picks the
        # consistent first-arrival point.
        env_window = envelope[lo_idx:hi_idx]
        if len(env_window) > 0 and float(np.max(env_window)) > 0.0:
            global_peak = float(np.max(env_window))
            threshold = 0.10 * global_peak  # 10% of peak — catches onset edge
            above = env_window > threshold
            # First sample at or above threshold = direct-sound onset
            if np.any(above):
                rel_idx = int(np.argmax(above))
                # Refine: find the local maximum within ±2 ms of onset for
                # sub-sample-stable peak time (envelope plateau makes argmax
                # noisy; local max near the onset is more stable than the
                # threshold-crossing sample itself).
                refine_lo = max(0, rel_idx - int(0.002 * sample_rate))
                refine_hi = min(len(env_window), rel_idx + int(0.002 * sample_rate))
                rel_idx = refine_lo + int(np.argmax(env_window[refine_lo:refine_hi]))
            else:
                # Fallback: no signal above threshold — use argmax (legacy)
                rel_idx = int(np.argmax(env_window))
        else:
            rel_idx = 0
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


def _xcorr_delay_ms(
    np,
    reference,   # 1-D float64 — the earlier-arriving signal
    delayed,     # 1-D float64 — the later-arriving signal
    sample_rate: int,
    lo_ms: float = 3.0,
    hi_ms: float = 250.0,
) -> Optional[float]:
    """FFT cross-correlation: return how many ms `delayed` lags behind `reference`.

    Uses the same bandpass + onset-detection approach as ``_compute_fr_arrays``
    so timing numbers are consistent.  Returns None if either signal is silent.
    """
    if np.max(np.abs(reference)) < 1e-9 or np.max(np.abs(delayed)) < 1e-9:
        return None

    n = 1
    target = len(reference) + len(delayed)
    while n < target:
        n <<= 1

    X = np.fft.rfft(reference, n=n)
    Y = np.fft.rfft(delayed, n=n)
    C_full = np.fft.irfft(np.conj(X) * Y, n=n)

    try:
        from scipy.signal import butter, sosfiltfilt, hilbert
        bp_lo, bp_hi = 30.0, min(2000.0, sample_rate * 0.4)
        sos = butter(4, [bp_lo, bp_hi], btype="band", fs=sample_rate, output="sos")
        C_bp = sosfiltfilt(sos, C_full)
        envelope = np.abs(hilbert(C_bp))
    except Exception:
        envelope = np.abs(C_full)

    lo_idx = max(1, int(lo_ms / 1000.0 * sample_rate))
    hi_idx = min(n, int(hi_ms / 1000.0 * sample_rate))
    env_window = envelope[lo_idx:hi_idx]
    if len(env_window) == 0 or float(np.max(env_window)) == 0.0:
        return None

    global_peak = float(np.max(env_window))
    above = env_window > 0.10 * global_peak
    if np.any(above):
        rel_idx = int(np.argmax(above))
        refine_lo = max(0, rel_idx - int(0.002 * sample_rate))
        refine_hi = min(len(env_window), rel_idx + int(0.002 * sample_rate))
        rel_idx = refine_lo + int(np.argmax(env_window[refine_lo:refine_hi]))
    else:
        rel_idx = int(np.argmax(env_window))

    peak_idx = lo_idx + rel_idx
    return round(peak_idx / sample_rate * 1000.0, 3)


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
