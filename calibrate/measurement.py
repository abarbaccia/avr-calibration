"""PyTTa-based acoustic measurement engine.

Measurement flow:
    generate log sweep → play+record (PyTTa) → deconvolve (numpy FFT) → FR

PyTTa and numpy are imported lazily so the module loads in CI/test
environments without PortAudio.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from .config import Config


@dataclass
class FrequencyResponse:
    """Frequency response from a single log-sweep measurement."""

    frequencies: list[float]  # Hz, trimmed to calibration band
    spl: list[float]          # dBFS transfer-function magnitude
    sample_rate: int          # Hz
    sweep_duration: float     # seconds
    timestamp: str            # ISO-8601 UTC

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "FrequencyResponse":
        return cls(**json.loads(s))

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
    """

    def __init__(self, config: Config) -> None:
        self.config = config

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

    def _compute_fr(
        self,
        np,
        sweep,
        recording,
        freq_min: int,
        freq_max: int,
        sample_rate: int,
    ) -> tuple[list[float], list[float]]:
        """
        Deconvolve sweep+recording into a dB transfer function.

        H(f) = FFT(recording) / FFT(sweep)

        PyTTa SignalObj exposes .timeSignal as an (N, channels) numpy array.
        """
        x = sweep.timeSignal[:, 0]
        y = recording.timeSignal[:, 0]

        n = len(x)
        X = np.fft.rfft(x, n=n)
        Y = np.fft.rfft(y, n=n)

        # Transfer function — guard against zero-division near DC/Nyquist.
        # errstate suppresses the expected RuntimeWarning from numpy evaluating
        # both branches of where() before applying the mask.
        with np.errstate(divide="ignore", invalid="ignore"):
            H = np.where(np.abs(X) > 1e-10, Y / X, 0.0 + 0.0j)

        freqs = np.fft.rfftfreq(n, d=1.0 / sample_rate)
        mag_db = 20.0 * np.log10(np.abs(H) + 1e-12)

        # Trim to calibration band
        mask = (freqs >= freq_min) & (freqs <= freq_max)
        return freqs[mask].tolist(), mag_db[mask].tolist()
