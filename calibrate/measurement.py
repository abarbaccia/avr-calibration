"""PyTTa-based acoustic measurement engine.

Measurement flow:
    generate log sweep → play+record (PyTTa) → deconvolve (numpy FFT) → FR

PyTTa and numpy are imported lazily so the module loads in CI/test
environments without PortAudio.

The web API uses the split methods:
    generate_sweep() → play_signal() + compute_fr()

play_signal() dispatches based on config.measurement.playback_route:
    "usb"  → _play_via_usb()   (Pi → miniDSP direct, Stage 1 sub alignment)
    "hdmi" → _play_via_hdmi()  (Pi → Denon → full chain, Stage 2 integration)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from .config import Config

log = logging.getLogger(__name__)


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
    spl: list[float]          # dBFS transfer-function magnitude
    sample_rate: int          # Hz
    sweep_duration: float     # seconds
    timestamp: str            # ISO-8601 UTC
    warnings: list[dict] = field(default_factory=list)  # non-fatal quality warnings

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "FrequencyResponse":
        data = json.loads(s)
        data.setdefault("warnings", [])  # backward compat — old sessions have no warnings field
        return cls(**data)

    @property
    def peak_spl(self) -> float:
        return max(self.spl)

    @property
    def freq_at_peak(self) -> float:
        idx = self.spl.index(self.peak_spl)
        return self.frequencies[idx]


class MeasurementEngine:
    """
    Runs a log-sweep measurement via PyTTa and returns a FrequencyResponse.

         generate log sweep  (pytta.generate.sweep)
                │
         play + record       (pytta.PlayRecMeasure)
                │
         deconvolve          H(f) = FFT(recording) / FFT(sweep)
                │
         trim to [freq_min, freq_max]
                │
         FrequencyResponse

    For the web API, the sweep can be split across requests:
        sweep_samples, sr, dur = engine.generate_sweep()
        engine.play_signal(sweep_samples, sr)    # non-blocking — 1s delay
        fr = engine.compute_fr(sweep_samples, recording_samples, sr=sr)

    play_signal() dispatches based on playback_route config:

        play_signal()
              │
              ├─[route=usb]──→ _play_via_usb()
              │                  └→ sounddevice.play(device="miniDSP")
              │
              └─[route=hdmi]─→ _play_via_hdmi()
                                 ├→ denonavr: switch to AUX1
                                 ├→ denonavr: set volume -25.0 dB
                                 ├→ sounddevice.play(device=HDMI)
                                 └→ denonavr: restore input + volume (always)
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    # ── Public split API (used by web server) ─────────────────────────────

    def generate_sweep(
        self,
        freq_min: int | None = None,
        freq_max: int | None = None,
        duration: float | None = None,
        sample_rate: int | None = None,
    ) -> tuple[list[float], int, float]:
        """
        Generate a log-sweep signal using numpy (no pytta dependency).

        Returns (samples, sample_rate, duration) where samples is a flat list
        of float32 values suitable for playback or JSON serialisation.

        Raises RuntimeError if numpy is unavailable.
        """
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("numpy is required — pip install numpy") from exc

        cfg = self.config.measurement
        f_min = freq_min if freq_min is not None else cfg.get("freq_min", 20)
        f_max = freq_max if freq_max is not None else cfg.get("freq_max", 200)
        dur = duration if duration is not None else cfg.get("sweep_duration", 3.0)
        sr = sample_rate if sample_rate is not None else cfg.get("sample_rate", 48000)

        # Logarithmic (exponential) sine sweep: y(t) = sin(2π·f_min·k·(e^(t/k) - 1))
        # where k = dur / ln(f_max / f_min)
        n = int(sr * dur)
        t = np.linspace(0.0, dur, n, endpoint=False)
        k = dur / np.log(f_max / f_min)
        phase = 2.0 * np.pi * f_min * k * (np.exp(t / k) - 1.0)
        samples: list[float] = np.sin(phase).astype(np.float32).tolist()
        return samples, sr, dur

    def play_signal(
        self,
        samples: list[float],
        sample_rate: int,
        out_channel: int | None = None,
    ) -> None:
        """
        Play samples through the configured output route (blocking).

        Dispatches to _play_via_usb() or _play_via_hdmi() based on
        config.measurement.playback_route (default: "usb").

        Call this from a background thread to leave the web server responsive.
        """
        route = self.config.measurement.get("playback_route", "usb")
        if route == "hdmi":
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(self._play_via_hdmi(samples, sample_rate))
            finally:
                loop.close()
        else:
            self._play_via_usb(samples, sample_rate, out_channel)

    def compute_fr(
        self,
        sweep_samples: list[float],
        recording_samples: list[float],
        freq_min: int | None = None,
        freq_max: int | None = None,
        sample_rate: int = 48000,
    ) -> FrequencyResponse:
        """
        Validate recording quality, then deconvolve sweep + browser recording
        into a FrequencyResponse.

        Raises MeasurementQualityError if the recording fails quality checks
        (sweep not captured, SNR too low). Returns warnings for non-fatal
        issues (noisy room).

        sweep_samples    — the sweep generated by generate_sweep()
        recording_samples — Float32 PCM captured by the browser
        sample_rate      — must match both arrays (browser records at this rate)
        """
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("numpy is required — pip install numpy") from exc

        cfg = self.config.measurement
        f_min = freq_min if freq_min is not None else cfg.get("freq_min", 20)
        f_max = freq_max if freq_max is not None else cfg.get("freq_max", 200)
        dur = len(sweep_samples) / sample_rate

        sweep_array = np.array(sweep_samples, dtype=np.float64)
        rec_array = np.array(recording_samples, dtype=np.float64)

        warnings = self.validate_recording(np, sweep_array, rec_array, sample_rate)

        frequencies, spl = self._compute_fr_arrays(
            np,
            sweep_array,
            rec_array,
            f_min,
            f_max,
            sample_rate,
        )
        return FrequencyResponse(
            frequencies=frequencies,
            spl=spl,
            sample_rate=sample_rate,
            sweep_duration=dur,
            timestamp=datetime.now(timezone.utc).isoformat(),
            warnings=warnings,
        )

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
        peak_idx = int(np.argmax(np.abs(rec_array)))
        half = floor_n // 2
        start = max(0, peak_idx - half)
        end = min(len(rec_array), peak_idx + half)
        peak_window = rec_array[start:end]
        signal_rms = np.sqrt(np.mean(peak_window ** 2)) if len(peak_window) > 0 else 0.0
        snr_db = 20.0 * np.log10(float(signal_rms) / (float(floor_rms) + 1e-12))

        if snr_db < min_snr_db:
            raise MeasurementQualityError(
                check="snr",
                detail=f"SNR {snr_db:.1f} dB < {min_snr_db} dB threshold",
                suggestion="Increase amplifier volume or check miniDSP signal routing",
            )

        return warnings_out

    # ── Legacy full-cycle API (used by CLI measure command) ────────────────

    def measure(self) -> FrequencyResponse:
        """Run a full sweep measurement. Raises RuntimeError if dependencies unavailable."""
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

        sweep = pytta.generate.sweep(
            freq_min=freq_min,
            freq_max=freq_max,
            duration=duration,
            Fs=sample_rate,
            method="log",
        )

        meas = pytta.PlayRecMeasure(
            excitation=sweep,
            samplingRate=sample_rate,
            numInChannels=1,
            numOutChannels=1,
            inChannels=[in_channel],
            outChannels=[out_channel],
        )
        recording = meas.run()

        frequencies, spl = self._compute_fr(
            np, sweep, recording, freq_min, freq_max, sample_rate
        )

        return FrequencyResponse(
            frequencies=frequencies,
            spl=spl,
            sample_rate=sample_rate,
            sweep_duration=duration,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # ── Playback routes ────────────────────────────────────────────────────

    def _play_via_usb(
        self,
        samples: list[float],
        sample_rate: int,
        out_channel: int | None = None,
    ) -> None:
        """Play via USB → miniDSP direct (Stage 1: sub alignment).

        Finds the output device by substring match on config.measurement.playback_device.
        Falls back to PortAudio default device if no name is configured or matched.
        """
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("sounddevice/numpy required for playback") from exc

        cfg = self.config.measurement
        device_name = cfg.get("playback_device", None)
        device = None
        if device_name:
            devices = sd.query_devices()
            for idx, dev in enumerate(devices):
                if (
                    dev["max_output_channels"] > 0
                    and device_name.lower() in dev["name"].lower()
                ):
                    device = idx
                    break

        arr = np.array(samples, dtype=np.float32).reshape(-1, 1)
        sd.play(arr, samplerate=sample_rate, device=device)
        sd.wait()

    async def _play_via_hdmi(
        self,
        samples: list[float],
        sample_rate: int,
    ) -> None:
        """Play via Pi HDMI → Denon → full signal chain (Stage 2: system integration).

        Sequence:
          1. Connect to Denon, capture current input + volume
          2. Switch to denon_sweep_input, set denon_sweep_volume
          3. Wait denon_settle_ms for input switch to settle
          4. Play sweep via HDMI sounddevice
          5. Always restore input + volume in finally block

        Volume safety: denon_sweep_volume must be ≤ -25.0 dB.
        Raises ValueError if misconfigured to prevent accidental loud sweeps.
        """
        try:
            import numpy as np
            import sounddevice as sd
            import denonavr
        except ImportError as exc:
            raise RuntimeError(
                "denonavr/sounddevice/numpy required for HDMI playback — pip install denonavr sounddevice"
            ) from exc

        cfg = self.config.measurement
        host = self.config.denon.get("host")
        if not host:
            raise RuntimeError("denon.host not configured — edit config.yaml")

        sweep_vol = float(cfg.get("denon_sweep_volume", -25.0))
        if sweep_vol > -25.0:
            raise ValueError(
                f"denon_sweep_volume must be ≤ -25.0 dB to prevent accidental loud sweeps, "
                f"got {sweep_vol}"
            )

        sweep_input = cfg.get("denon_sweep_input", "AUX1")
        settle_ms = cfg.get("denon_settle_ms", 800)
        hdmi_device = cfg.get("hdmi_playback_device", None)

        saved_input = None
        saved_volume = None

        receiver = denonavr.DenonAVR(host)
        await receiver.async_setup()

        try:
            saved_input = receiver.input_func
            saved_volume = receiver.volume

            await receiver.async_set_input_func(sweep_input)
            await receiver.async_set_volume(sweep_vol)
            await asyncio.sleep(settle_ms / 1000.0)

            arr = np.array(samples, dtype=np.float32).reshape(-1, 1)
            sd.play(arr, samplerate=sample_rate, device=hdmi_device)
            sd.wait()

        finally:
            if saved_input is not None:
                await receiver.async_set_input_func(saved_input)
            if saved_volume is not None:
                await receiver.async_set_volume(saved_volume)

    # ── Internals ─────────────────────────────────────────────────────────

    def _compute_fr_arrays(
        self,
        np,
        sweep_array,   # 1-D float64 ndarray
        rec_array,     # 1-D float64 ndarray
        freq_min: int,
        freq_max: int,
        sample_rate: int,
    ) -> tuple[list[float], list[float]]:
        """
        Core deconvolution on raw numpy arrays.

        H(f) = FFT(recording) / FFT(sweep)

        Arrays are zero-padded / truncated to the shorter length so they
        share the same FFT grid.
        """
        n = min(len(sweep_array), len(rec_array))
        X = np.fft.rfft(sweep_array[:n], n=n)
        Y = np.fft.rfft(rec_array[:n], n=n)

        with np.errstate(divide="ignore", invalid="ignore"):
            H = np.where(np.abs(X) > 1e-10, Y / X, 0.0 + 0.0j)

        freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
        mag_db = 20.0 * np.log10(np.abs(H) + 1e-12)

        mask = (freqs >= freq_min) & (freqs <= freq_max)
        return freqs[mask].tolist(), mag_db[mask].tolist()

    def _compute_fr(
        self,
        np,
        sweep,       # PyTTa SignalObj
        recording,   # PyTTa SignalObj
        freq_min: int,
        freq_max: int,
        sample_rate: int,
    ) -> tuple[list[float], list[float]]:
        """Wrapper that extracts numpy arrays from PyTTa SignalObj inputs."""
        x = sweep.timeSignal[:, 0]
        y = recording.timeSignal[:, 0]
        return self._compute_fr_arrays(np, x, y, freq_min, freq_max, sample_rate)
