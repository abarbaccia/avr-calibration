"""Tests for the PyTTa measurement engine and the `calibrate measure` CLI command.

Coverage diagram:
  FrequencyResponse
  ├── [TESTED] to_json / from_json round-trip
  ├── [TESTED] peak_spl returns maximum SPL value
  └── [TESTED] freq_at_peak returns corresponding frequency

  MeasurementEngine.measure()
  ├── [TESTED] happy path — returns FrequencyResponse with correct fields
  ├── [TESTED] pytta.generate.sweep called with config values
  ├── [TESTED] pytta.PlayRecMeasure called with correct channel config
  ├── [TESTED] frequency range trimmed to [freq_min, freq_max]
  ├── [TESTED] RuntimeError raised when pytta not installed
  └── [TESTED] RuntimeError raised when numpy not installed

  MeasurementEngine._compute_fr()
  ├── [TESTED] deconvolution produces real-valued dB output
  ├── [TESTED] zero-division guard — near-zero sweep values produce finite output
  ├── [TESTED] output contains only frequencies in [freq_min, freq_max]
  └── [TESTED] empty result when no frequencies in band (edge case)

  calibrate measure (CLI)
  ├── [TESTED] happy path — prints summary and session id
  ├── [TESTED] --label flag passed through to SessionStore
  ├── [TESTED] exits 1 when config file missing
  └── [TESTED] exits 1 and prints error when MeasurementEngine raises RuntimeError
"""

import sys
import numpy as np
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from click.testing import CliRunner

from calibrate.cli import cli
from calibrate.config import Config
from calibrate.measurement import FrequencyResponse, MeasurementEngine


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_config(**measurement_overrides) -> Config:
    defaults = {
        "freq_min": 20,
        "freq_max": 200,
        "sweep_duration": 3.0,
        "sample_rate": 48000,
        "input_channel": 1,
        "output_channel": 1,
    }
    defaults.update(measurement_overrides)
    return Config({
        "denon": {"host": "192.168.1.100"},
        "minidsp": {"host": "localhost", "port": 5380},
        "mic": {"name": "UMIK"},
        "measurement": defaults,
    })


def make_signal(n_samples: int = 4800) -> MagicMock:
    """Return a mock PyTTa SignalObj with a .timeSignal numpy array."""
    sig = MagicMock()
    sig.timeSignal = np.random.default_rng(42).standard_normal((n_samples, 1))
    return sig


# ── FrequencyResponse ─────────────────────────────────────────────────────────

class TestFrequencyResponse:
    def test_json_round_trip(self):
        fr = FrequencyResponse(
            frequencies=[20.0, 40.0, 80.0, 160.0],
            spl=[-20.0, -15.0, -12.0, -18.0],
            sample_rate=48000,
            sweep_duration=3.0,
            timestamp="2026-03-20T00:00:00+00:00",
        )
        assert FrequencyResponse.from_json(fr.to_json()) == fr

    def test_peak_spl(self):
        fr = FrequencyResponse(
            frequencies=[20.0, 40.0, 80.0],
            spl=[-20.0, -10.0, -15.0],
            sample_rate=48000,
            sweep_duration=3.0,
            timestamp="2026-03-20T00:00:00+00:00",
        )
        assert fr.peak_spl == -10.0

    def test_freq_at_peak(self):
        fr = FrequencyResponse(
            frequencies=[20.0, 40.0, 80.0],
            spl=[-20.0, -10.0, -15.0],
            sample_rate=48000,
            sweep_duration=3.0,
            timestamp="2026-03-20T00:00:00+00:00",
        )
        assert fr.freq_at_peak == 40.0


# ── MeasurementEngine.measure() ───────────────────────────────────────────────

class TestMeasure:
    def _make_engine_with_mocks(self, config=None):
        """Return (engine, mock_pytta, mock_sweep, mock_recording)."""
        cfg = config or make_config()
        engine = MeasurementEngine(cfg)

        mock_sweep = make_signal(n_samples=cfg.measurement["sample_rate"] * 3)
        mock_recording = make_signal(n_samples=cfg.measurement["sample_rate"] * 3)

        mock_pytta = sys.modules["pytta"]
        mock_pytta.reset_mock()  # clear call history from previous tests
        mock_pytta.generate.sweep.return_value = mock_sweep
        mock_pytta.PlayRecMeasure.return_value.run.return_value = mock_recording

        return engine, mock_pytta, mock_sweep, mock_recording

    def test_happy_path_returns_frequency_response(self):
        engine, _, _, _ = self._make_engine_with_mocks()
        fr = engine.measure()
        assert isinstance(fr, FrequencyResponse)
        assert len(fr.frequencies) > 0
        assert len(fr.spl) == len(fr.frequencies)
        assert fr.sample_rate == 48000
        assert fr.sweep_duration == 3.0
        assert fr.timestamp  # non-empty ISO string

    def test_sweep_called_with_config_values(self):
        cfg = make_config(freq_min=30, freq_max=150, sweep_duration=5.0, sample_rate=44100)
        engine, mock_pytta, _, _ = self._make_engine_with_mocks(cfg)
        engine.measure()
        mock_pytta.generate.sweep.assert_called_once_with(
            freq_min=30,
            freq_max=150,
            duration=5.0,
            Fs=44100,
            method="log",
        )

    def test_playrecmeasure_called_with_channel_config(self):
        cfg = make_config(input_channel=2, output_channel=3)
        engine, mock_pytta, _, _ = self._make_engine_with_mocks(cfg)
        engine.measure()
        mock_pytta.PlayRecMeasure.assert_called_once_with(
            excitation=mock_pytta.generate.sweep.return_value,
            samplingRate=48000,
            numInChannels=1,
            numOutChannels=1,
            inChannels=[2],
            outChannels=[3],
        )

    def test_frequencies_trimmed_to_config_band(self):
        cfg = make_config(freq_min=40, freq_max=100)
        engine, _, _, _ = self._make_engine_with_mocks(cfg)
        fr = engine.measure()
        assert all(40 <= f <= 100 for f in fr.frequencies)

    def test_pytta_import_error_raises_runtime_error(self):
        engine = MeasurementEngine(make_config())
        with patch.dict(sys.modules, {"pytta": None}):
            with pytest.raises(RuntimeError, match="pytta is required"):
                engine.measure()

    def test_numpy_import_error_raises_runtime_error(self):
        engine = MeasurementEngine(make_config())
        with patch.dict(sys.modules, {"numpy": None}):
            with pytest.raises(RuntimeError, match="numpy is required"):
                engine.measure()


# ── MeasurementEngine._compute_fr() ──────────────────────────────────────────

class TestComputeFr:
    """Unit tests for the deconvolution + dB computation."""

    def _engine(self):
        return MeasurementEngine(make_config())

    def test_output_is_finite_floats(self):
        engine = self._engine()
        n = 4800
        sweep = make_signal(n)
        recording = make_signal(n)
        freqs, spl = engine._compute_fr(np, sweep, recording, 20, 200, 48000)
        assert all(isinstance(f, float) for f in freqs)
        assert all(isinstance(s, float) for s in spl)
        assert all(np.isfinite(s) for s in spl)

    def test_zero_division_guard_produces_finite_output(self):
        """When sweep is near-zero, H(f) should be 0 (not inf/nan)."""
        engine = self._engine()
        n = 4800
        # Make sweep all zeros — worst case for division
        sweep = MagicMock()
        sweep.timeSignal = np.zeros((n, 1))
        recording = make_signal(n)
        freqs, spl = engine._compute_fr(np, sweep, recording, 20, 200, 48000)
        assert all(np.isfinite(s) for s in spl)

    def test_output_frequencies_in_requested_band(self):
        engine = self._engine()
        n = 4800
        freqs, spl = engine._compute_fr(np, make_signal(n), make_signal(n), 50, 120, 48000)
        assert all(50 <= f <= 120 for f in freqs)

    def test_no_frequencies_in_band_returns_empty(self):
        """If freq_min > Nyquist, result is empty — no crash."""
        engine = self._engine()
        n = 100
        # With sample_rate=1000 and n=100, max freq=500Hz; band [600,700] is empty
        freqs, spl = engine._compute_fr(np, make_signal(n), make_signal(n), 600, 700, 1000)
        assert freqs == []
        assert spl == []


# ── calibrate measure (CLI) ───────────────────────────────────────────────────

def make_fr_result(
    frequencies=None,
    spl=None,
) -> FrequencyResponse:
    return FrequencyResponse(
        frequencies=frequencies or [20.0, 40.0, 80.0],
        spl=spl or [-20.0, -15.0, -18.0],
        sample_rate=48000,
        sweep_duration=3.0,
        timestamp="2026-03-20T00:00:00+00:00",
    )


class TestMeasureCLI:
    def _run(self, args, config_path, store=None, fr=None, engine_error=None):
        runner = CliRunner()
        mock_fr = fr or make_fr_result()
        mock_store = store or MagicMock()
        mock_store.save_measurement.return_value = 1

        with (
            patch("calibrate.measurement.MeasurementEngine") as MockEngine,
            patch("calibrate.storage.SessionStore", return_value=mock_store),
        ):
            if engine_error:
                MockEngine.return_value.measure.side_effect = engine_error
            else:
                MockEngine.return_value.measure.return_value = mock_fr
            result = runner.invoke(cli, ["measure", "--config", str(config_path)] + args)
        return result, mock_store

    def test_happy_path_prints_summary(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("denon:\n  host: '192.168.1.1'\n")
        result, _ = self._run([], cfg_path)
        assert result.exit_code == 0
        assert "Measurement complete" in result.output
        assert "Session #1" in result.output
        assert "Peak:" in result.output

    def test_label_flag_passed_to_store(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("denon:\n  host: '192.168.1.1'\n")
        result, mock_store = self._run(["--label", "baseline"], cfg_path)
        assert result.exit_code == 0
        assert "baseline" in result.output
        mock_store.save_measurement.assert_called_once()
        _, kwargs = mock_store.save_measurement.call_args
        assert kwargs.get("label") == "baseline" or mock_store.save_measurement.call_args[0][1] == "baseline"

    def test_missing_config_exits_1(self, tmp_path):
        missing = tmp_path / "does_not_exist.yaml"
        runner = CliRunner()
        result = runner.invoke(cli, ["measure", "--config", str(missing)])
        assert result.exit_code == 1
        assert "No config found" in result.output

    def test_measurement_error_exits_1(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("denon:\n  host: '192.168.1.1'\n")
        result, _ = self._run([], cfg_path, engine_error=RuntimeError("pytta is required"))
        assert result.exit_code == 1
        assert "pytta is required" in result.output
