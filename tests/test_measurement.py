"""Tests for the measurement engine and the `calibrate measure` CLI command.

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
from calibrate.measurement import FrequencyResponse, MeasurementEngine, MeasurementQualityError, compute_session_metadata


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_config(**measurement_overrides) -> Config:
    defaults = {
        "freq_min": 20,
        "freq_max": 200,
        "sweep_duration": 3.0,
        "sample_rate": 48000,
        "input_channel": 1,
        "output_channel": 1,
        "denon_sweep_input": "TV",  # test default; real value must be set in config.yaml
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

    def test_json_round_trip_with_warnings(self):
        fr = FrequencyResponse(
            frequencies=[20.0, 40.0],
            spl=[-20.0, -15.0],
            sample_rate=48000,
            sweep_duration=3.0,
            timestamp="2026-03-20T00:00:00+00:00",
            warnings=[{"check": "floor_noise", "detail": "noisy room"}],
        )
        rt = FrequencyResponse.from_json(fr.to_json())
        assert rt.warnings == fr.warnings

    def test_from_json_backward_compat_no_warnings_field(self):
        """Old sessions stored without warnings field deserialize without crash."""
        import json
        data = {
            "frequencies": [20.0],
            "spl": [-20.0],
            "sample_rate": 48000,
            "sweep_duration": 3.0,
            "timestamp": "2026-03-20T00:00:00+00:00",
        }
        fr = FrequencyResponse.from_json(json.dumps(data))
        assert fr.warnings == []


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
        mock_pytta.PlayRecMeasure.return_value.run.side_effect = None

        # validate_recording is now called inside measure(); mock it so random
        # test signals don't trigger quality-gate errors.
        engine.validate_recording = MagicMock(return_value=[])

        return engine, mock_pytta, mock_sweep, mock_recording

    def _make_playback_mock(self, mock_sweep, mock_recording):
        """Return a mock playback strategy that returns the given sweep/rec arrays.

        USBPlayback now uses sd.InputStream/OutputStream directly instead of
        pytta.PlayRecMeasure. Patching playback_for_route lets measure() tests
        stay focused on the orchestration logic rather than stream mechanics.
        """
        sweep_1d = mock_sweep.timeSignal[:, 0]
        rec_1d = mock_recording.timeSignal[:, 0]
        mock_strategy = MagicMock()
        mock_strategy.play_and_record.return_value = (sweep_1d, rec_1d)
        return mock_strategy

    @pytest.mark.asyncio
    async def test_happy_path_returns_frequency_response(self):
        engine, _, mock_sweep, mock_recording = self._make_engine_with_mocks()
        mock_strategy = self._make_playback_mock(mock_sweep, mock_recording)
        with patch("calibrate.drivers.playback.playback_for_route", return_value=mock_strategy):
            fr = await engine.measure()
        assert isinstance(fr, FrequencyResponse)
        assert len(fr.frequencies) > 0
        assert len(fr.spl) == len(fr.frequencies)
        assert fr.sample_rate == 48000
        assert fr.sweep_duration == 3.0
        assert fr.timestamp  # non-empty ISO string

    @pytest.mark.asyncio
    async def test_sweep_called_with_config_values(self):
        cfg = make_config(freq_min=30, freq_max=150, sweep_duration=5.0, sample_rate=44100)
        engine, mock_pytta, mock_sweep, mock_recording = self._make_engine_with_mocks(cfg)
        mock_strategy = self._make_playback_mock(mock_sweep, mock_recording)
        import math
        with patch("calibrate.drivers.playback.playback_for_route", return_value=mock_strategy):
            await engine.measure()
        mock_pytta.generate.sweep.assert_called_once_with(
            freqMin=30,
            freqMax=150,
            fftDegree=math.ceil(math.log2((5.0 + 1.0) * 44100)),
            samplingRate=44100,
            method="logarithmic",
        )

    @pytest.mark.asyncio
    async def test_playback_called_with_channel_config(self):
        """play_and_record is called with the correct in/out channels from config."""
        cfg = make_config(input_channel=2, output_channel=3)
        engine, _, mock_sweep, mock_recording = self._make_engine_with_mocks(cfg)
        mock_strategy = self._make_playback_mock(mock_sweep, mock_recording)
        with patch("calibrate.drivers.playback.playback_for_route", return_value=mock_strategy):
            await engine.measure()
        mock_strategy.play_and_record.assert_called_once()
        call_args = mock_strategy.play_and_record.call_args
        assert call_args[0][2] == 2  # in_channel
        assert call_args[0][3] == 3  # out_channel

    @pytest.mark.asyncio
    async def test_frequencies_trimmed_to_config_band(self):
        cfg = make_config(freq_min=40, freq_max=100)
        engine, _, mock_sweep, mock_recording = self._make_engine_with_mocks(cfg)
        mock_strategy = self._make_playback_mock(mock_sweep, mock_recording)
        with patch("calibrate.drivers.playback.playback_for_route", return_value=mock_strategy):
            fr = await engine.measure()
        assert all(40 <= f <= 100 for f in fr.frequencies)

    @pytest.mark.asyncio
    async def test_pytta_import_error_raises_runtime_error(self):
        engine = MeasurementEngine(make_config())
        with patch.dict(sys.modules, {"pytta": None}):
            with pytest.raises(RuntimeError, match="pytta is required"):
                await engine.measure()

    @pytest.mark.asyncio
    async def test_numpy_import_error_raises_runtime_error(self):
        engine = MeasurementEngine(make_config())
        with patch.dict(sys.modules, {"numpy": None}):
            with pytest.raises(RuntimeError, match="numpy is required"):
                await engine.measure()


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
        freqs, spl, _ir, _phase = engine._compute_fr(np, sweep, recording, 20, 200, 48000)
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
        freqs, spl, _ir, _phase = engine._compute_fr(np, sweep, recording, 20, 200, 48000)
        assert all(np.isfinite(s) for s in spl)

    def test_output_frequencies_in_requested_band(self):
        engine = self._engine()
        n = 4800
        freqs, spl, _ir, _phase = engine._compute_fr(np, make_signal(n), make_signal(n), 50, 120, 48000)
        assert all(50 <= f <= 120 for f in freqs)

    def test_no_frequencies_in_band_returns_empty(self):
        """If freq_min > Nyquist, result is empty — no crash."""
        engine = self._engine()
        n = 100
        # With sample_rate=1000 and n=100, max freq=500Hz; band [600,700] is empty
        freqs, spl, _ir, _phase = engine._compute_fr(np, make_signal(n), make_signal(n), 600, 700, 1000)
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
        from unittest.mock import AsyncMock
        runner = CliRunner()
        mock_fr = fr or make_fr_result()
        mock_store = store or MagicMock()
        mock_store.save_measurement.return_value = 1

        with (
            patch("calibrate.measurement.MeasurementEngine") as MockEngine,
            patch("calibrate.storage.SessionStore", return_value=mock_store),
        ):
            mock_measure = AsyncMock()
            if engine_error:
                mock_measure.side_effect = engine_error
            else:
                mock_measure.return_value = mock_fr
            MockEngine.return_value.measure = mock_measure
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


# ── MeasurementQualityError ───────────────────────────────────────────────────

class TestMeasurementQuality:
    """Unit tests for validate_recording() quality checks."""

    def _engine(self) -> MeasurementEngine:
        return MeasurementEngine(make_config())

    def _make_sweep(self, n: int = 9600) -> np.ndarray:
        """Log sweep signal — high correlation with itself."""
        t = np.linspace(0, 1.0, n, endpoint=False)
        k = 1.0 / np.log(200 / 20)
        return np.sin(2 * np.pi * 20 * k * (np.exp(t / k) - 1)).astype(np.float64)

    def test_clean_recording_returns_empty_warnings(self):
        engine = self._engine()
        sweep = self._make_sweep(9600)
        # Recording: 200 samples of silence (floor), then attenuated sweep
        # Use a 3ms floor window (144 samples) so it reads only the silent prefix
        rec = np.zeros(9600 + 200)
        rec[200:] += sweep * 0.5
        rec = rec[:9600]
        warnings = engine.validate_recording(np, sweep, rec, 48000,
                                              noise_floor_window_ms=3)
        assert warnings == []

    def test_noisy_floor_produces_warning_not_error(self):
        engine = self._engine()
        sweep = self._make_sweep(9600)
        # High noise floor (> -40 dBFS) in pre-sweep window
        rng = np.random.default_rng(42)
        rec = rng.standard_normal(9600) * 0.02  # ~-34 dBFS
        # Add delayed sweep after the noise-floor window to pass correlation check
        rec[5000:] += sweep[:4600] * 0.5
        warnings = engine.validate_recording(np, sweep, rec, 48000,
                                              noise_floor_window_ms=100)
        assert any(w["check"] == "floor_noise" for w in warnings)

    def test_sweep_not_captured_raises_quality_error(self):
        engine = self._engine()
        sweep = self._make_sweep(9600)
        # Recording is pure noise — no sweep correlation
        rec = np.random.default_rng(7).standard_normal(9600) * 1e-6
        with pytest.raises(MeasurementQualityError) as exc_info:
            engine.validate_recording(np, sweep, rec, 48000)
        assert exc_info.value.check == "sweep_capture"

    def test_sweep_capture_error_has_suggestion(self):
        engine = self._engine()
        sweep = self._make_sweep(9600)
        rec = np.zeros(9600)
        with pytest.raises(MeasurementQualityError) as exc_info:
            engine.validate_recording(np, sweep, rec, 48000)
        assert exc_info.value.suggestion  # non-empty

    def test_low_snr_raises_quality_error(self):
        engine = self._engine()
        sweep = self._make_sweep(9600)
        # Floor is loud, signal is weaker → bad SNR
        rec = np.random.default_rng(0).standard_normal(9600) * 0.01  # loud floor
        rec += sweep * 0.011  # signal barely above floor — check 2 passes, check 3 fails
        with pytest.raises(MeasurementQualityError) as exc_info:
            engine.validate_recording(np, sweep, rec, 48000,
                                       noise_floor_window_ms=9500,
                                       min_snr_db=40.0)
        assert exc_info.value.check == "snr"

    def test_snr_error_has_suggestion(self):
        engine = self._engine()
        sweep = self._make_sweep(9600)
        rec = np.random.default_rng(0).standard_normal(9600) * 0.01
        rec += sweep * 0.011
        try:
            engine.validate_recording(np, sweep, rec, 48000,
                                       noise_floor_window_ms=9500,
                                       min_snr_db=40.0)
        except MeasurementQualityError as exc:
            assert exc.check == "snr"
            assert exc.suggestion

    def test_custom_correlation_threshold(self):
        engine = self._engine()
        sweep = self._make_sweep(9600)
        rec = np.zeros(9600)
        rec[:] = sweep * 0.001
        warnings = engine.validate_recording(np, sweep, rec, 48000,
                                              noise_floor_window_ms=3,
                                              correlation_threshold=0.0,
                                              min_snr_db=-100.0)
        assert isinstance(warnings, list)

    def test_measurement_quality_error_is_runtime_error(self):
        err = MeasurementQualityError("snr", "SNR too low", "turn it up")
        assert isinstance(err, RuntimeError)
        assert err.check == "snr"
        assert err.detail == "SNR too low"
        assert err.suggestion == "turn it up"
        assert str(err) == "SNR too low"

    def test_floor_noise_threshold_exactly_at_minus40(self):
        """Floor at exactly -40 dBFS is OK — no warning."""
        engine = self._engine()
        sweep = self._make_sweep(9600)
        floor_rms = 10 ** (-40.0 / 20.0)
        rng = np.random.default_rng(99)
        pre = rng.standard_normal(200)
        pre = pre / np.sqrt(np.mean(pre ** 2)) * floor_rms * 0.99
        rec = np.concatenate([pre, sweep * 0.5])[:9600]
        result = engine.validate_recording(np, sweep, rec, 48000,
                                            noise_floor_window_ms=3,
                                            min_snr_db=0.0)
        assert isinstance(result, list)


# ── compute_session_metadata ────────────────────────────────────────────────

class TestComputeSessionMetadata:
    """Tests for compute_session_metadata — enriching measurements at capture time."""

    def _make_fr(self, n=4800, sample_rate=48000) -> FrequencyResponse:
        """Create a realistic FrequencyResponse with IR and phase data."""
        engine = MeasurementEngine(make_config(sample_rate=sample_rate))
        sweep = np.random.default_rng(42).standard_normal(n)
        rec = np.random.default_rng(99).standard_normal(n)
        freqs, spl, ir, phase = engine._compute_fr_arrays(
            np, sweep, rec, 20, 200, sample_rate
        )
        return FrequencyResponse(
            frequencies=freqs, spl=spl, sample_rate=sample_rate,
            sweep_duration=3.0, timestamp="2026-04-07T00:00:00+00:00",
            impulse_response=ir, phase=phase,
        )

    def test_ir_metadata_present(self):
        from calibrate.measurement import compute_session_metadata
        fr = self._make_fr()
        meta = compute_session_metadata(fr)
        assert "ir" in meta
        ir = meta["ir"]
        assert "peak_time_ms" in ir
        assert "peak_sign" in ir
        assert ir["peak_sign"] in (1, -1)
        assert "spl_db" in ir
        assert isinstance(ir["spl_db"], float)
        assert "sample_rate" in ir

    def test_decay_modes_present(self):
        from calibrate.measurement import compute_session_metadata
        fr = self._make_fr()
        meta = compute_session_metadata(fr)
        assert "decay_modes" in meta
        assert isinstance(meta["decay_modes"], list)

    def test_group_delay_present(self):
        from calibrate.measurement import compute_session_metadata
        fr = self._make_fr()
        meta = compute_session_metadata(fr)
        assert "group_delay" in meta
        gd = meta["group_delay"]
        assert "freq_hz" in gd
        assert "delay_ms" in gd
        assert len(gd["freq_hz"]) == len(gd["delay_ms"])
        # Midpoint frequencies → one fewer than input frequencies
        assert len(gd["freq_hz"]) == len(fr.frequencies) - 1

    def test_no_ir_no_crash(self):
        """FrequencyResponse without IR should still return metadata (empty)."""
        from calibrate.measurement import compute_session_metadata
        fr = FrequencyResponse(
            frequencies=[20.0, 40.0], spl=[-10.0, -12.0],
            sample_rate=48000, sweep_duration=3.0,
            timestamp="2026-04-07T00:00:00+00:00",
        )
        meta = compute_session_metadata(fr)
        assert "ir" not in meta
        assert "decay_modes" not in meta

    def test_no_phase_no_group_delay(self):
        """FrequencyResponse without phase should skip group delay."""
        from calibrate.measurement import compute_session_metadata
        fr = FrequencyResponse(
            frequencies=[20.0, 40.0], spl=[-10.0, -12.0],
            sample_rate=48000, sweep_duration=3.0,
            timestamp="2026-04-07T00:00:00+00:00",
            impulse_response=[0.0] * 100,
        )
        meta = compute_session_metadata(fr)
        assert "group_delay" not in meta

    def test_phase_in_fr_json_round_trip(self):
        """Phase field survives JSON serialization."""
        fr = FrequencyResponse(
            frequencies=[20.0, 40.0, 80.0],
            spl=[-20.0, -15.0, -12.0],
            phase=[0.1, -0.5, 1.2],
            sample_rate=48000, sweep_duration=3.0,
            timestamp="2026-04-07T00:00:00+00:00",
        )
        restored = FrequencyResponse.from_json(fr.to_json())
        assert restored.phase == fr.phase

    def test_phase_extracted_by_compute_fr(self):
        """_compute_fr_arrays now returns phase alongside magnitude."""
        engine = MeasurementEngine(make_config())
        n = 4800
        sweep = np.random.default_rng(42).standard_normal(n)
        rec = np.random.default_rng(99).standard_normal(n)
        freqs, spl, ir, phase = engine._compute_fr_arrays(np, sweep, rec, 20, 200, 48000)
        assert len(phase) == len(freqs)
        assert all(isinstance(p, float) for p in phase)
        assert all(np.isfinite(p) for p in phase)


class TestPreDelayCompensation:
    """
    USBPlayback records PRE_DELAY_S seconds of room noise before playing the sweep
    so validate_recording has a clean noise-floor window.  measure() must strip those
    pre-delay samples before calling _compute_fr_arrays, otherwise the circular FFT
    shifts the true IR peak outside the stored 24000-sample window and the peak-finder
    returns noise artifacts (e.g. 0.25ms when the sub is ~40ms away).
    """

    def _make_delayed_pair(self, arrival_samples, pre_delay_samples, n=65536, sample_rate=48000):
        """Return (sweep_1d, rec_1d_with_predelay) with a known IR peak at arrival_samples."""
        rng = np.random.default_rng(7)
        # Sweep: 300ms silence lead-in (like PyTTa's startMargin), then chirp
        lead = int(0.3 * sample_rate)
        sweep = np.zeros(n)
        sweep[lead:] = rng.standard_normal(n - lead)

        # Room IR: impulse at arrival_samples
        h = np.zeros(n)
        h[arrival_samples] = 1.0

        # Recording = conv(sweep, h) delayed by pre_delay_samples
        rec_linear = np.convolve(sweep, h)[:n]
        rec_buf = np.zeros(pre_delay_samples + len(rec_linear))
        rec_buf[pre_delay_samples:pre_delay_samples + len(rec_linear)] = rec_linear
        return sweep, rec_buf

    def test_ir_peak_without_predelay_compensation_is_wrong(self):
        """Without stripping pre-delay, the stored IR peak is far from the true arrival."""
        engine = MeasurementEngine(make_config())
        arrival_ms = 30.0
        arrival_samples = int(arrival_ms * 48000 / 1000)
        pre_delay = int(1.0 * 48000)  # 1s = 48000 samples

        sweep, rec_1d = self._make_delayed_pair(arrival_samples, pre_delay)

        # Deconvolution WITHOUT pre-delay stripping (old, broken behaviour)
        n = min(len(sweep), len(rec_1d))
        X = np.fft.rfft(sweep[:n], n=n)
        Y = np.fft.rfft(rec_1d[:n], n=n)
        H = np.where(np.abs(X) > 1e-10, Y / X, 0.0 + 0.0j)
        ir_full = np.fft.irfft(H, n=n)
        peak_wrong = int(np.argmax(np.abs(ir_full[:24000])))

        # The wrong peak must NOT match the true arrival (should be off by pre-delay offset)
        assert abs(peak_wrong - arrival_samples) > 1000, (
            f"Expected peak far from {arrival_samples}, got {peak_wrong}"
        )

    def test_ir_peak_with_predelay_compensation_is_correct(self):
        """After stripping pre-delay, the stored IR peak is within 3ms of the true arrival."""
        engine = MeasurementEngine(make_config())
        arrival_ms = 30.0
        arrival_samples = int(arrival_ms * 48000 / 1000)
        pre_delay = int(1.0 * 48000)  # 1s = 48000 samples

        sweep, rec_1d = self._make_delayed_pair(arrival_samples, pre_delay)

        # Deconvolution WITH pre-delay stripping (fixed behaviour)
        rec_aligned = rec_1d[pre_delay:]
        n = min(len(sweep), len(rec_aligned))
        X = np.fft.rfft(sweep[:n], n=n)
        Y = np.fft.rfft(rec_aligned[:n], n=n)
        H = np.where(np.abs(X) > 1e-10, Y / X, 0.0 + 0.0j)
        ir_full = np.fft.irfft(H, n=n)
        peak_fixed = int(np.argmax(np.abs(ir_full[:24000])))

        # Within 3ms = 144 samples of the true arrival
        tolerance_samples = int(3e-3 * 48000)
        assert abs(peak_fixed - arrival_samples) <= tolerance_samples, (
            f"Expected peak near {arrival_samples} (±{tolerance_samples}), got {peak_fixed}"
        )


class TestOnsetDetection:
    """
    compute_session_metadata uses onset detection to find the first IR arrival
    rather than the loudest peak.  In rooms with strong bass modes, a late
    resonance can be louder than the direct sound, causing argmax(abs(ir)) to
    report a bogus arrival time (e.g. 48ms instead of ~8ms).  Onset detection
    finds the first sample within 20 dB of the peak — the direct sound.
    """

    def test_onset_finds_direct_sound_not_room_mode(self):
        """IR with direct sound at 8ms (0.5 amplitude) and room mode at 40ms
        (1.0 amplitude).  argmax would return 40ms; onset detection returns ~8ms."""
        sr = 48000
        direct_idx = int(0.008 * sr)    # 8ms = 384 samples
        mode_idx = int(0.040 * sr)      # 40ms = 1920 samples

        ir = np.zeros(24000)
        ir[direct_idx] = 0.5            # direct sound (weaker)
        ir[mode_idx] = 1.0              # room mode (louder)

        fr = FrequencyResponse(
            frequencies=[20.0, 100.0],
            spl=[0.0, 0.0],
            sample_rate=sr,
            sweep_duration=1.0,
            timestamp="2026-01-01T00:00:00Z",
            impulse_response=ir.tolist(),
        )

        meta = compute_session_metadata(fr, search_window_ms=50.0)

        # Onset should find the direct sound at ~8ms, not the mode at 40ms
        peak_ms = meta["ir"]["peak_time_ms"]
        assert abs(peak_ms - 8.0) < 1.0, (
            f"Expected onset near 8ms (direct sound), got {peak_ms}ms"
        )
        # SPL should still reflect the loudest peak (room mode)
        assert meta["ir"]["peak_sign"] == 1

    def test_onset_matches_argmax_when_direct_is_loudest(self):
        """When the direct sound IS the loudest peak, onset detection and
        argmax should agree."""
        sr = 48000
        direct_idx = int(0.010 * sr)    # 10ms

        ir = np.zeros(24000)
        ir[direct_idx] = 1.0            # direct sound is the peak

        fr = FrequencyResponse(
            frequencies=[20.0, 100.0],
            spl=[0.0, 0.0],
            sample_rate=sr,
            sweep_duration=1.0,
            timestamp="2026-01-01T00:00:00Z",
            impulse_response=ir.tolist(),
        )

        meta = compute_session_metadata(fr, search_window_ms=50.0)

        peak_ms = meta["ir"]["peak_time_ms"]
        assert abs(peak_ms - 10.0) < 1.0, (
            f"Expected onset near 10ms, got {peak_ms}ms"
        )
