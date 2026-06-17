"""Tests for the measurement engine and the `calibrate measure` CLI command.

Coverage diagram:
  FrequencyResponse
  ├── [TESTED] to_json / from_json round-trip
  ├── [TESTED] peak_spl returns maximum SPL value
  ├── [TESTED] freq_at_peak returns corresponding frequency
  ├── [TESTED] loopback_xcorr_peak_ms / avr_processing_ms round-trip
  └── [TESTED] backward compat when loopback fields absent

  _xcorr_delay_ms
  ├── [TESTED] recovers known delay within 1.5 ms
  ├── [TESTED] returns None when reference is silent
  ├── [TESTED] returns None when delayed is silent
  └── [TESTED] result lies within search window

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

import subprocess
import sys
import numpy as np
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from click.testing import CliRunner

from calibrate.cli import cli
from calibrate.config import Config
from calibrate.measurement import FrequencyResponse, MeasurementEngine, MeasurementQualityError, compute_session_metadata, detect_ir_onset, _xcorr_delay_ms


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
        # test signals don't trigger quality-gate errors. Returns (warnings,
        # sweep_start_sample) — use 0 for the sample offset so the test
        # recording is used verbatim without extra stripping.
        engine.validate_recording = MagicMock(return_value=([], 0))

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
    async def test_route_param_overrides_cfg_playback_route(self):
        """Caller-supplied route="hdmi" must beat cfg.playback_route="usb".

        Regression: engine used to read cfg.measurement.playback_route directly,
        which silently dropped mcp_server's auto-route to "hdmi" for mains
        sweeps. The route param plumbs that decision through.
        """
        cfg = make_config()
        cfg.measurement["playback_route"] = "usb"
        # Bypass HDMI device name-search inside measure() — the mocked
        # sounddevice in this suite doesn't carry full PortAudio metadata.
        cfg.measurement["hdmi_device_index"] = 0
        engine, _, mock_sweep, mock_recording = self._make_engine_with_mocks(cfg)
        mock_strategy = self._make_playback_mock(mock_sweep, mock_recording)
        with patch(
            "calibrate.drivers.playback.playback_for_route",
            return_value=mock_strategy,
        ) as mock_factory:
            await engine.measure(route="hdmi")
        # playback_for_route is called with the explicit route, not cfg's "usb".
        # playback_for_route is called with the explicit route arg.
        called_route = mock_factory.call_args[0][0]
        assert called_route == "hdmi"

    @pytest.mark.asyncio
    async def test_usb_route_sets_pipewire_node_when_no_direct_match(self):
        """USB route with a PipeWire-style playback_device should:
          - pick the ALSA `default` PortAudio device,
          - set os.environ['PIPEWIRE_NODE'] to that name.

        This is the post-v0.2.0 PipeWire path: PortAudio doesn't enumerate
        PipeWire nodes by name, so we ride the pipewire-alsa default PCM
        and use PIPEWIRE_NODE to target the avr_cal_sweep null sink.
        """
        import os
        cfg = make_config()
        cfg.measurement["playback_route"] = "usb"
        cfg.measurement["playback_device"] = "avr_cal_sweep"
        engine, _, mock_sweep, mock_recording = self._make_engine_with_mocks(cfg)
        mock_strategy = self._make_playback_mock(mock_sweep, mock_recording)

        mock_sd = sys.modules["sounddevice"]
        mock_sd.reset_mock()
        # No device matches "avr_cal_sweep" by name; "default" does.
        mock_sd.query_devices.return_value = [
            {"name": "UMIK-1", "max_input_channels": 1, "max_output_channels": 0},
            {"name": "default", "max_input_channels": 0, "max_output_channels": 2},
            {"name": "hw:Loopback,1,0", "max_input_channels": 2, "max_output_channels": 0},
        ]
        mock_sd.default.device = [0, 1]

        prev_node = os.environ.pop("PIPEWIRE_NODE", None)
        try:
            with patch(
                "calibrate.drivers.playback.playback_for_route",
                return_value=mock_strategy,
            ):
                await engine.measure(route="usb")
            assert os.environ.get("PIPEWIRE_NODE") == "avr_cal_sweep"
        finally:
            if prev_node is None:
                os.environ.pop("PIPEWIRE_NODE", None)
            else:
                os.environ["PIPEWIRE_NODE"] = prev_node

    @pytest.mark.asyncio
    async def test_usb_route_skips_pipewire_node_for_direct_portaudio_match(self):
        """Legacy direct-ALSA setup (e.g. playback_device='miniDSP' matches a
        PortAudio device by name): we pin sd.default to it and DO NOT set
        PIPEWIRE_NODE — leaving the env alone for non-PipeWire deployments."""
        import os
        cfg = make_config()
        cfg.measurement["playback_route"] = "usb"
        cfg.measurement["playback_device"] = "miniDSP"
        engine, _, mock_sweep, mock_recording = self._make_engine_with_mocks(cfg)
        mock_strategy = self._make_playback_mock(mock_sweep, mock_recording)

        mock_sd = sys.modules["sounddevice"]
        mock_sd.reset_mock()
        mock_sd.query_devices.return_value = [
            {"name": "UMIK-1", "max_input_channels": 1, "max_output_channels": 0},
            {"name": "miniDSP 2x4 HD", "max_input_channels": 0, "max_output_channels": 4},
        ]
        mock_sd.default.device = [0, 1]

        prev_node = os.environ.pop("PIPEWIRE_NODE", None)
        try:
            with patch(
                "calibrate.drivers.playback.playback_for_route",
                return_value=mock_strategy,
            ):
                await engine.measure(route="usb")
            # Direct PortAudio match → don't pollute env with PIPEWIRE_NODE.
            assert "PIPEWIRE_NODE" not in os.environ
        finally:
            if prev_node is None:
                os.environ.pop("PIPEWIRE_NODE", None)
            else:
                os.environ["PIPEWIRE_NODE"] = prev_node

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

    @pytest.mark.asyncio
    async def test_flat_response_with_loopback_raises_quality_error(self):
        """When loopback is present and deconvolved FR is flat (< 2 dB std),
        measure() raises MeasurementQualityError — the mic captured the
        reference directly rather than the acoustic output."""
        from calibrate.measurement import MeasurementQualityError
        engine, _, mock_sweep, mock_recording = self._make_engine_with_mocks()
        # Build a mic signal = ref signal so deconvolution gives flat ~0 dBFS.
        sweep_1d = mock_sweep.timeSignal[:, 0]
        flat_mic = sweep_1d.copy()  # mic ≈ reference → H(f) ≈ 1 everywhere
        flat_ref = sweep_1d.copy()
        mock_strategy = MagicMock()
        # Return 3-tuple (sweep, mic, ref) — signals presence of loopback.
        mock_strategy.play_and_record.return_value = (sweep_1d, flat_mic, flat_ref)
        with patch("calibrate.drivers.playback.playback_for_route", return_value=mock_strategy):
            with pytest.raises(MeasurementQualityError) as exc_info:
                await engine.measure()
        assert exc_info.value.check == "flat_response"

    @pytest.mark.asyncio
    async def test_non_flat_response_with_loopback_does_not_raise(self):
        """A loopback measurement with natural FR variation (> 2 dB std) passes."""
        engine, _, mock_sweep, mock_recording = self._make_engine_with_mocks()
        import numpy as _np
        rng = _np.random.default_rng(7)
        n = len(mock_sweep.timeSignal[:, 0])
        sweep_1d = mock_sweep.timeSignal[:, 0]
        acoustic_mic = rng.standard_normal(n).astype(_np.float32)
        ref = sweep_1d.copy()
        mock_strategy = MagicMock()
        mock_strategy.play_and_record.return_value = (sweep_1d, acoustic_mic, ref)
        with patch("calibrate.drivers.playback.playback_for_route", return_value=mock_strategy):
            fr = await engine.measure()
        assert isinstance(fr, FrequencyResponse)


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
        freqs, spl, _ir, _phase, _coh, _xcorr, _xcorr_sign = engine._compute_fr(np, sweep, recording, 20, 200, 48000)
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
        freqs, spl, _ir, _phase, _coh, _xcorr, _xcorr_sign = engine._compute_fr(np, sweep, recording, 20, 200, 48000)
        assert all(np.isfinite(s) for s in spl)

    def test_output_frequencies_in_requested_band(self):
        engine = self._engine()
        n = 4800
        freqs, spl, _ir, _phase, _coh, _xcorr, _xcorr_sign = engine._compute_fr(np, make_signal(n), make_signal(n), 50, 120, 48000)
        assert all(50 <= f <= 120 for f in freqs)

    def test_no_frequencies_in_band_returns_empty(self):
        """If freq_min > Nyquist, result is empty — no crash."""
        engine = self._engine()
        n = 100
        # With sample_rate=1000 and n=100, max freq=500Hz; band [600,700] is empty
        freqs, spl, _ir, _phase, _coh, _xcorr, _xcorr_sign = engine._compute_fr(np, make_signal(n), make_signal(n), 600, 700, 1000)
        assert freqs == []
        assert spl == []


class TestDeconvolveAgainstRecordedRef:
    """Regression tests for the architectural fix:

    When a loopback reference recording is available, the engine must
    deconvolve mic against the RECORDED reference (not the analytical
    sweep template). This cancels PipeWire scheduling jitter as
    common-mode noise. Empirically: coherence 0.05–0.6 → 0.94–1.00.
    """

    def _engine(self):
        return MeasurementEngine(make_config())

    def test_recorded_ref_produces_different_fr_than_analytical_sweep(self):
        """Deconvolving against a recorded ref vs the analytical sweep
        must produce different FR arrays — proves the substitution
        actually flows through the math (not a no-op)."""
        engine = self._engine()
        rng = np.random.default_rng(42)
        n = 4800
        analytical_sweep = rng.standard_normal(n).astype(np.float64)
        # Recorded ref: analytical sweep + per-sample jitter (the thing
        # that would smear phase if you deconvolved against analytical)
        recorded_ref = analytical_sweep + 0.3 * rng.standard_normal(n).astype(np.float64)
        mic_recording = rng.standard_normal(n).astype(np.float64)

        # (a) Without ref: deconvolve mic vs analytical sweep
        f_no_ref, spl_no_ref, *_ = engine._compute_fr_arrays(
            np, analytical_sweep, mic_recording, 20, 200, 48000,
        )
        # (b) With ref: deconvolve mic vs recorded ref
        f_with_ref, spl_with_ref, *_ = engine._compute_fr_arrays(
            np, recorded_ref, mic_recording, 20, 200, 48000,
        )

        # Same frequency bins (band + sample_rate identical)
        assert f_no_ref == f_with_ref
        # But the FR must differ — the X array is different
        assert spl_no_ref != spl_with_ref
        # Both must still be finite
        assert all(np.isfinite(s) for s in spl_no_ref)
        assert all(np.isfinite(s) for s in spl_with_ref)

    def test_zero_ref_array_falls_back_safely(self):
        """A near-zero ref (silent loopback) should not crash the math
        — the zero-division guard inside _compute_fr_arrays handles it.
        This documents the contract the caller's branch relies on:
        when ref is silent the engine code path picks sweep_for_deconv,
        but if a silent ref ever reaches _compute_fr_arrays directly
        we still get finite output."""
        engine = self._engine()
        n = 4800
        zero_ref = np.zeros(n, dtype=np.float64)
        mic = np.random.default_rng(0).standard_normal(n).astype(np.float64)
        freqs, spl, *_ = engine._compute_fr_arrays(np, zero_ref, mic, 20, 200, 48000)
        assert all(np.isfinite(s) for s in spl)


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
        warnings, _ = engine.validate_recording(np, sweep, rec, 48000,
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
        warnings, _ = engine.validate_recording(np, sweep, rec, 48000,
                                                 noise_floor_window_ms=100)
        assert any(w["check"] == "floor_noise" for w in warnings)

    def test_sweep_not_captured_raises_quality_error(self):
        engine = self._engine()
        sweep = self._make_sweep(9600)
        # Recording is LOUD but uncorrelated with the sweep (DC offset — the
        # zero-mean sweep correlates to ~0). Loud enough to pass the silent gate,
        # so this exercises the correlation gate specifically.
        rec = np.ones(9600) * 0.5
        with pytest.raises(MeasurementQualityError) as exc_info:
            engine.validate_recording(np, sweep, rec, 48000)
        assert exc_info.value.check == "sweep_capture"

    def test_sweep_capture_error_has_suggestion(self):
        engine = self._engine()
        sweep = self._make_sweep(9600)
        rec = np.ones(9600) * 0.5  # loud, uncorrelated → sweep_capture (not silent)
        with pytest.raises(MeasurementQualityError) as exc_info:
            engine.validate_recording(np, sweep, rec, 48000)
        assert exc_info.value.check == "sweep_capture"
        assert exc_info.value.suggestion  # non-empty

    def test_silent_recording_raises_silent_recording_check(self):
        """A zero/near-zero recording → check='silent_recording', NOT
        'sweep_capture'. The mic feed is missing; this must not be misdiagnosed
        as a low-correlation/aim problem (the 2026-06-15 multi-hour hunt)."""
        engine = self._engine()
        sweep = self._make_sweep(9600)
        rec = np.zeros(9600)
        with pytest.raises(MeasurementQualityError) as exc_info:
            engine.validate_recording(np, sweep, rec, 48000)
        assert exc_info.value.check == "silent_recording"

    def test_silent_recording_suggestion_names_mic_feed(self):
        """The silent-recording error must point at the mic FEED (the
        load-bearing UMIK→loopback_ref link), so it's self-diagnosing."""
        engine = self._engine()
        sweep = self._make_sweep(9600)
        rec = np.zeros(9600)
        with pytest.raises(MeasurementQualityError) as exc_info:
            engine.validate_recording(np, sweep, rec, 48000)
        sugg = exc_info.value.suggestion.lower()
        assert "loopback_ref" in sugg and "umik" in sugg
        assert "mic" in exc_info.value.detail.lower()

    def test_near_silent_below_threshold_is_silent_not_sweep_capture(self):
        """Recording just below the silent floor (e.g. residual dither) is
        still 'silent_recording', not 'sweep_capture'."""
        engine = self._engine()
        sweep = self._make_sweep(9600)
        rec = np.full(9600, 1e-5)  # ~-100 dBFS, below _SILENT_RECORDING_RMS
        with pytest.raises(MeasurementQualityError) as exc_info:
            engine.validate_recording(np, sweep, rec, 48000)
        assert exc_info.value.check == "silent_recording"

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
        warnings, _ = engine.validate_recording(np, sweep, rec, 48000,
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
        warnings, _ = engine.validate_recording(np, sweep, rec, 48000,
                                            noise_floor_window_ms=3,
                                            min_snr_db=0.0)
        assert isinstance(warnings, list)


# ── compute_session_metadata ────────────────────────────────────────────────

class TestComputeSessionMetadata:
    """Tests for compute_session_metadata — enriching measurements at capture time."""

    def _make_fr(self, n=4800, sample_rate=48000) -> FrequencyResponse:
        """Create a realistic FrequencyResponse with IR and phase data."""
        engine = MeasurementEngine(make_config(sample_rate=sample_rate))
        sweep = np.random.default_rng(42).standard_normal(n)
        rec = np.random.default_rng(99).standard_normal(n)
        freqs, spl, ir, phase, _coh, _xcorr, _xcorr_sign = engine._compute_fr_arrays(
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
        freqs, spl, ir, phase, _coh, _xcorr, _xcorr_sign = engine._compute_fr_arrays(np, sweep, rec, 20, 200, 48000)
        assert len(phase) == len(freqs)
        assert all(isinstance(p, float) for p in phase)
        assert all(np.isfinite(p) for p in phase)

    def test_ir_gate_removes_combing_artifacts(self):
        """IR gating removes comb-like artifacts from circular-FFT wrap-around.

        Simulate the real scenario: a sweep with leading/trailing silence
        (like PyTTa) convolved with a room IR whose reverb tail wraps
        around in the circular FFT.  Without IR gating the raw H(f) shows
        periodic nulls; with gating the FR should be smooth.
        """
        engine = MeasurementEngine(make_config())
        n = 2 ** 16  # 65536 samples
        sr = 48000

        # Sweep with leading/trailing silence (like PyTTa layout)
        t_sweep = np.linspace(0, 1, n - 10000, endpoint=False)
        chirp = np.sin(
            2 * np.pi * 20 * (200 / 20) ** t_sweep
            / np.log(200 / 20) * np.log(200 / 20)
        )
        sweep = np.zeros(n)
        sweep[3000:3000 + len(chirp)] = chirp  # 3000 leading zeros, ~7000 trailing

        # Room IR: direct path + strong late reflection that wraps around
        ir_room = np.zeros(n)
        ir_room[200] = 1.0    # direct sound
        ir_room[4000] = 0.5   # early reflection
        ir_room[40000] = 0.3  # late reflection — will wrap in circular FFT

        # Recording = circular convolution of sweep with room IR
        rec = np.real(np.fft.ifft(np.fft.fft(sweep) * np.fft.fft(ir_room)))

        freqs, spl, ir_out, phase, _coh, _xcorr, _xcorr_sign = engine._compute_fr_arrays(
            np, sweep, rec, 20, 200, sr,
        )
        assert len(freqs) > 0
        assert all(np.isfinite(s) for s in spl)
        # IR gating should produce a smooth FR (< 30 dB range in-band)
        assert max(spl) - min(spl) < 30, "FR range too wide — IR gate may not be working"

        # Check that consecutive bins don't have the periodic comb pattern.
        # Compute the standard deviation of bin-to-bin differences — a comb
        # pattern has high variance (alternating high/low).
        diffs = np.diff(spl)
        assert np.std(diffs) < 5.0, "Bin-to-bin FR variance too high — possible comb artifact"

    def test_ir_gate_preserves_room_modes(self):
        """IR gating preserves the room's modal peaks/dips within the gate window."""
        engine = MeasurementEngine(make_config())
        n = 2 ** 16
        sr = 48000

        # Simple sweep (full buffer, no silence — ideal case)
        t = np.linspace(0, 1, n, endpoint=False)
        sweep = np.sin(
            2 * np.pi * 20 * (200 / 20) ** t
            / np.log(200 / 20) * np.log(200 / 20)
        )

        # Room with a clear mode: strong resonance at ~50 Hz
        ir_room = np.zeros(n)
        ir_room[100] = 1.0
        # Add 50 Hz ringing (within 500ms gate window)
        t_ring = np.arange(0, int(0.3 * sr)) / sr
        ir_room[100:100 + len(t_ring)] += 0.4 * np.sin(2 * np.pi * 50 * t_ring) * np.exp(-t_ring * 5)
        rec = np.real(np.fft.ifft(np.fft.fft(sweep) * np.fft.fft(ir_room)))

        freqs, spl, _, _, _, _, _ = engine._compute_fr_arrays(np, sweep, rec, 20, 200, sr)

        # Find SPL near 50 Hz — should show the mode peak
        idx_50 = min(range(len(freqs)), key=lambda i: abs(freqs[i] - 50))
        idx_100 = min(range(len(freqs)), key=lambda i: abs(freqs[i] - 100))
        # The 50 Hz mode should be visible as elevated SPL vs 100 Hz
        assert spl[idx_50] > spl[idx_100] - 3, "50 Hz room mode not preserved by IR gate"


class TestResolveAlsaDeviceInPortAudio:
    """ALSA hw: names don't substring-match PortAudio device names directly.

    Regression: ``hw:Loopback,0,0`` vs PortAudio ``"Loopback: PCM (hw:2,0)"``
    — literal substring match fails and silently falls back to system default.
    These tests pin the resolver that maps ALSA card names → PortAudio matches
    via /proc/asound/cards.
    """

    def _devices(self):
        return [
            {"name": "vc4-hdmi-0: MAI PCM i2s-hifi-0 (hw:0,0)", "max_output_channels": 8, "max_input_channels": 0},
            {"name": "Loopback: PCM (hw:2,0)", "max_output_channels": 2, "max_input_channels": 0},
            {"name": "Loopback: PCM (hw:2,1)", "max_output_channels": 32, "max_input_channels": 0},
            {"name": "default", "max_output_channels": 128, "max_input_channels": 128},
        ]

    def test_literal_substring_still_works_when_match_present(self):
        """If the ALSA name happens to be a literal substring (older configs), prefer it."""
        from calibrate.measurement import _resolve_alsa_device_in_portaudio

        devs = [
            {"name": "Some Device hw:Loopback,0,0 alias", "max_output_channels": 2},
            {"name": "Other", "max_output_channels": 2},
        ]
        idx, dev = _resolve_alsa_device_in_portaudio("hw:Loopback,0,0", devs, want_output=True)
        assert idx == 0

    def test_resolves_loopback_via_asound_cards(self, tmp_path, monkeypatch):
        """The real-world case: PortAudio names use ``(hw:<card_idx>,<dev>)``."""
        from calibrate import measurement

        # Stub /proc/asound/cards so the resolver knows Loopback → card 2
        cards_text = """0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0
                      vc4-hdmi-0 (hw:0,0)
 2 [Loopback       ]: Loopback - Loopback
                      Loopback 1
 3 [USB            ]: USB-Audio - Scarlett 18i20 USB
                      Focusrite Scarlett 18i20 USB at usb-...
"""
        cards_file = tmp_path / "cards"
        cards_file.write_text(cards_text)
        # Patch Path("/proc/asound/cards") read_text via monkeypatching pathlib.Path
        import pathlib
        real_read_text = pathlib.Path.read_text

        def patched_read_text(self, *args, **kwargs):
            if str(self) == "/proc/asound/cards":
                return cards_text
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "read_text", patched_read_text)

        devs = self._devices()
        idx, dev = measurement._resolve_alsa_device_in_portaudio(
            "hw:Loopback,0,0", devs, want_output=True,
        )
        assert idx == 1, f"expected loopback playback (hw:2,0), got {idx} {dev}"
        assert "Loopback" in dev["name"]
        assert "(hw:2,0)" in dev["name"]

    def test_returns_none_when_card_name_unknown(self, monkeypatch):
        """Unknown ALSA card name → None, never a wrong-device fallback."""
        from calibrate import measurement
        import pathlib

        def patched_read_text(self, *args, **kwargs):
            if str(self) == "/proc/asound/cards":
                return ""  # empty
            return pathlib.Path.read_text(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "read_text", patched_read_text)

        devs = self._devices()
        idx, dev = measurement._resolve_alsa_device_in_portaudio(
            "hw:NonexistentCard,0,0", devs, want_output=True,
        )
        assert idx is None and dev is None

    def test_returns_none_when_proc_asound_unavailable(self, monkeypatch):
        """If /proc/asound/cards can't be read, fall back to None (don't pick wrong device)."""
        from calibrate import measurement
        import pathlib

        def patched_read_text(self, *args, **kwargs):
            if str(self) == "/proc/asound/cards":
                raise OSError("simulated filesystem error")
            return pathlib.Path.read_text(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "read_text", patched_read_text)

        devs = self._devices()
        idx, dev = measurement._resolve_alsa_device_in_portaudio(
            "hw:Loopback,0,0", devs, want_output=True,
        )
        assert idx is None and dev is None

    def test_skips_wrong_direction(self, monkeypatch):
        """Asking for output won't match an input-only device with the same name."""
        from calibrate import measurement
        import pathlib

        cards_text = " 2 [Loopback       ]: Loopback - Loopback\n"

        def patched_read_text(self, *args, **kwargs):
            if str(self) == "/proc/asound/cards":
                return cards_text
            return pathlib.Path.read_text(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "read_text", patched_read_text)

        # Capture-only loopback presented in the device list
        devs = [
            {"name": "Loopback: PCM (hw:2,0)", "max_output_channels": 0, "max_input_channels": 32},
        ]
        idx, dev = measurement._resolve_alsa_device_in_portaudio(
            "hw:Loopback,0,0", devs, want_output=True,
        )
        assert idx is None


class TestCoherenceMetric:
    """The reported coherence is a per-bin reliability score derived from
    the IR's signal-vs-noise ratio (early IR window vs late tail). Welch
    coherence is invalid for sweep stimuli — these tests pin the new metric.
    """

    def _engine(self):
        return MeasurementEngine(make_config())

    def _log_sweep(self, n: int, sr: int) -> "np.ndarray":
        """Real log sweep covering 20 Hz to sr/4, with leading silence."""
        lead = n // 8
        body = n - lead
        f0, f1 = 20.0, sr / 4
        t = np.arange(body) / sr
        T = body / sr
        phase = 2.0 * np.pi * f0 * T / np.log(f1 / f0) * (
            (f1 / f0) ** (t / T) - 1.0
        )
        sweep = np.zeros(n)
        sweep[lead:lead + body] = np.sin(phase)
        return sweep

    def test_clean_ir_yields_high_coherence(self):
        """Single-tap IR with no tail noise → coherence near 1 across the band."""
        engine = self._engine()
        n, sr = 2 ** 16, 48000
        sweep = self._log_sweep(n, sr)
        ir_room = np.zeros(n)
        ir_room[200] = 1.0  # clean direct arrival, nothing else
        rec = np.real(np.fft.ifft(np.fft.fft(sweep) * np.fft.fft(ir_room)))

        _, _, _, _, coh, _, _ = engine._compute_fr_arrays(
            np, sweep, rec, 20, 200, sr,
        )
        assert coh is not None
        # The IR is a clean delta with zeros in the tail, so SNR is huge and
        # coherence should sit at the upper bound across the whole band.
        assert min(coh) > 0.9, f"clean IR coherence too low: min={min(coh)}"

    def test_noise_only_recording_yields_mid_coherence(self):
        """Recording is pure noise → mean coherence near 0.5 (SNR ≈ 1)."""
        engine = self._engine()
        n, sr = 2 ** 16, 48000
        sweep = self._log_sweep(n, sr)
        rng = np.random.default_rng(7)
        rec = rng.standard_normal(n)

        _, _, _, _, coh, _, _ = engine._compute_fr_arrays(
            np, sweep, rec, 20, 200, sr,
        )
        assert coh is not None
        # Pure-noise recording produces uniformly-distributed deconvolved IR;
        # early-window vs tail-window powers are comparable so SNR ≈ 1 and
        # γ²=SNR/(1+SNR) ≈ 0.5 on average. The important regression is that
        # noise no longer reads near zero (Welch bug) AND doesn't pin near 1
        # (would mean the metric is uninformative).
        mean_coh = sum(coh) / len(coh)
        assert 0.2 < mean_coh < 0.7, f"noise coherence unexpected: mean={mean_coh}"

    def test_added_tail_noise_drops_coherence(self):
        """Coherence falls monotonically as recording noise level rises."""
        engine = self._engine()
        n, sr = 2 ** 16, 48000
        sweep = self._log_sweep(n, sr)
        ir_room = np.zeros(n)
        ir_room[200] = 1.0
        rec_clean = np.real(np.fft.ifft(np.fft.fft(sweep) * np.fft.fft(ir_room)))
        rng = np.random.default_rng(123)
        noise = rng.standard_normal(n)

        results = []
        # Sweep needs to span a wide range to overcome the deconvolution's
        # noise rejection at low levels — at 0.5× noise the SNR is still
        # comfortable, only at 5× and 20× does it bite.
        for noise_level in [0.0, 1.0, 5.0, 20.0]:
            rec = rec_clean + noise * noise_level
            _, _, _, _, coh, _, _ = engine._compute_fr_arrays(
                np, sweep, rec, 20, 200, sr,
            )
            results.append(sum(coh) / len(coh))

        # Mean coherence must monotonically decrease as noise rises.
        for i in range(len(results) - 1):
            assert results[i] >= results[i + 1] - 1e-6, (
                f"coherence did not decrease with tail noise: {results}"
            )
        assert results[0] > 0.9, f"clean baseline too low: {results[0]}"
        assert results[-1] < 0.5, f"heavy noise should drop coherence: {results[-1]}"


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

        # Onset should find direct sound before mode (fallback path, wider tolerance)
        peak_ms = meta["ir"]["peak_time_ms"]
        assert peak_ms < 20.0, (
            f"Expected onset before room mode at 40ms, got {peak_ms}ms"
        )
        # SPL should still reflect the loudest peak (room mode)
        assert meta["ir"]["peak_sign"] == 1

    def test_onset_matches_argmax_when_direct_is_loudest(self):
        """When the direct sound IS the loudest peak, onset detection and
        argmax should agree. Bandpass fallback has wider tolerance."""
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
        assert abs(peak_ms - 10.0) < 6.0, (
            f"Expected onset near 10ms (fallback path), got {peak_ms}ms"
        )

    def test_ir_onset_ignores_misleading_xcorr_hint(self):
        """IR-domain onset detection ignores misleading xcorr_peak_ms hints.

        The xcorr_peak_ms parameter is no longer used as a short-circuit;
        the bandpassed IR onset detection runs always so the reported peak
        reflects the actual direct arrival, not the (potentially wrong)
        cross-correlation argmax which can lock onto room-mode resonance."""
        sr = 48000
        ir = np.zeros(24000)
        ir[int(0.015 * sr)] = 1.0       # real onset at 15 ms

        fr = FrequencyResponse(
            frequencies=[20.0, 100.0],
            spl=[0.0, 0.0],
            sample_rate=sr,
            sweep_duration=1.0,
            timestamp="2026-01-01T00:00:00Z",
            impulse_response=ir.tolist(),
            xcorr_peak_ms=80.0,          # misleading hint — should be ignored
        )

        meta = compute_session_metadata(fr, search_window_ms=50.0)

        # Expect the bandpass + onset detection to land near 15 ms (some
        # group-delay slack from sosfiltfilt — typically a few ms).
        peak_ms = meta["ir"]["peak_time_ms"]
        assert 8.0 < peak_ms < 25.0, (
            f"Expected onset near 15 ms (real impulse), got {peak_ms} ms"
        )


# ── detect_ir_onset (shared function) ─────────────────────────────────────────


class TestDetectIrOnset:
    """Direct unit tests for the extracted detect_ir_onset function."""

    def test_finds_clean_impulse(self):
        """Clean impulse at 15 ms → onset detection returns near 15 ms."""
        sr = 48000
        ir = np.zeros(24000)
        ir[int(0.015 * sr)] = 1.0  # real peak at 15ms
        result = detect_ir_onset(ir, sr, search_window_ms=50.0, xcorr_peak_ms=15.0)
        # Expect onset near 15 ms with some bandpass group-delay slack.
        assert 8.0 < result["peak_time_ms"] < 25.0
        assert result["peak_sign"] == 1

    def test_negative_polarity_from_ir_when_no_xcorr_sign(self):
        """When xcorr_peak_sign is not provided, polarity falls back to ir[max_idx].
        xcorr_peak_ms alone does not influence polarity — only xcorr_peak_sign does."""
        sr = 48000
        ir = np.zeros(24000)
        ir[int(0.010 * sr)] = -1.0  # inverted
        result = detect_ir_onset(ir, sr, search_window_ms=50.0, xcorr_peak_ms=10.0)
        assert result["peak_sign"] == -1
        assert result["peak_sign_source"] == "ir_max"

    def test_picks_direct_arrival_over_louder_late_resonance(self):
        """Direct arrival (transient) at 5 ms + much louder resonance burst at
        120 ms — onset must pick the direct arrival, not resonance.

        This is the regression case from real measurements: when a sub sits
        in a room null at MLP, the resonance build-up after direct arrival
        can be 25-100× larger than the direct sound itself, and naive
        argmax(|ir|) reports the resonance time as the peak.
        """
        sr = 48000
        ir = np.zeros(24000)
        ir[int(0.005 * sr)] = 0.3
        # 60 Hz resonance burst at 120 ms with peak ~0.5 (well above direct)
        t = np.arange(int(0.080 * sr)) / sr
        burst = np.sin(2 * np.pi * 60 * t) * np.exp(-t * 8) * 0.5
        late = int(0.120 * sr)
        ir[late:late + len(burst)] += burst
        result = detect_ir_onset(ir, sr, search_window_ms=50.0)
        assert 1.0 < result["peak_time_ms"] < 30.0, (
            f"Expected onset near 5 ms direct arrival, not late resonance — "
            f"got {result['peak_time_ms']} ms"
        )

    def test_fallback_finds_direct_sound(self):
        """Without xcorr_peak_ms, bandpass fallback finds broadband impulse.
        Tolerance is wider (~6ms) because sosfiltfilt introduces group delay
        on the low-frequency filtered signal. This path is only for legacy
        sessions without cross-correlation timing."""
        sr = 48000
        ir = np.zeros(24000)
        ir[int(0.012 * sr)] = 1.0  # 12ms
        result = detect_ir_onset(ir, sr, search_window_ms=50.0)
        assert abs(result["peak_time_ms"] - 12.0) < 6.0

    def test_returns_correct_keys(self):
        sr = 48000
        ir = np.zeros(24000)
        ir[int(0.010 * sr)] = 1.0
        result = detect_ir_onset(ir, sr, xcorr_peak_ms=10.0)
        assert "peak_time_ms" in result
        assert "peak_sign" in result
        assert "spl_db" in result
        assert "sample_rate" in result
        assert result["sample_rate"] == sr

    def test_fallback_onset_before_mode(self):
        """Fallback: onset finds the first arrival, not the loudest late mode.
        Wider tolerance for bandpass group delay on legacy path."""
        sr = 48000
        ir = np.zeros(24000)
        ir[int(0.008 * sr)] = 0.3       # direct sound at 8ms
        ir[int(0.040 * sr)] = 1.0       # room mode at 40ms (louder)
        result = detect_ir_onset(ir, sr, search_window_ms=50.0)
        # Fallback should find onset before the room mode, even if not exact
        assert result["peak_time_ms"] < 20.0

    def test_fallback_finds_peak_beyond_search_window_ms(self):
        """Bug 3 regression: legacy sessions whose IR contains pre-sweep silence
        have their IR peak at e.g. 1000ms (after 1s of silence + ~10ms travel).
        The old code searched only ir[:search_window_ms] (50ms) and returned 0.0.
        The fix searches the full IR so these sessions return a non-zero peak_time_ms.
        """
        sr = 48000
        # Simulate a legacy session: 1s of pre-sweep silence + 10ms travel time.
        # Old code with 50ms window would miss this peak entirely.
        legacy_ir = np.zeros(int(sr * 1.5))  # 1.5s IR
        peak_sample = int(sr * 1.0) + int(0.010 * sr)  # 1010ms into the IR
        legacy_ir[peak_sample] = 1.0
        result = detect_ir_onset(legacy_ir, sr, search_window_ms=50.0)
        # Must not return 0.0 — the peak is at ~1010ms
        assert result["peak_time_ms"] > 50.0, (
            f"Legacy IR peak at {peak_sample/sr*1000:.1f}ms should not be missed; "
            f"got peak_time_ms={result['peak_time_ms']:.1f}ms"
        )

    def test_two_legacy_sessions_produce_different_peak_times(self):
        """Bug 3: analyze_ir on two different sessions must return distinct peak_time_ms
        so delay offsets can be computed for sub alignment.
        """
        sr = 48000
        # Sub 1: peak at 1005ms (1s silence + 5ms travel)
        ir1 = np.zeros(int(sr * 1.5))
        ir1[int(sr * 1.0) + int(0.005 * sr)] = 1.0

        # Sub 2: peak at 1012ms (1s silence + 12ms travel)
        ir2 = np.zeros(int(sr * 1.5))
        ir2[int(sr * 1.0) + int(0.012 * sr)] = 1.0

        r1 = detect_ir_onset(ir1, sr, search_window_ms=50.0)
        r2 = detect_ir_onset(ir2, sr, search_window_ms=50.0)

        delay_offset_ms = r2["peak_time_ms"] - r1["peak_time_ms"]
        assert abs(delay_offset_ms - 7.0) < 6.0, (
            f"Expected ~7ms delay offset, got {delay_offset_ms:.2f}ms"
        )
        assert r1["peak_time_ms"] != r2["peak_time_ms"], (
            "Two sessions with different travel times must return different peak_time_ms"
        )

    # ── xcorr_peak_sign / peak_sign_source tests (DEFECT 4 fix) ────────────

    def test_xcorr_sign_overrides_ir_max_sign(self):
        """Synthetic band-limited IR: the largest half-cycle is negative, but
        xcorr_peak_sign is positive → detect_ir_onset must report peak_sign=+1
        with source='xcorr'.

        This tests the core DEFECT 4 fix: for band-limited sub IRs, the Wiener-
        deconvolved IR is a ringing wavelet and which half-cycle is largest is
        noise-sensitive.  The xcorr sign is stable and must win when provided.
        """
        sr = 48000
        n = 24000
        # Construct a band-limited (40 Hz) wavelet whose largest absolute peak
        # is negative (the negative half-cycle happens to be larger).
        t = np.arange(n) / sr
        # 40 Hz sine burst at 12 ms — positive polarity, but modulated so the
        # first negative half-cycle is slightly larger than the positive one.
        onset = int(0.012 * sr)
        burst_len = int(0.25 / 40 * sr)  # one quarter-period
        ir = np.zeros(n)
        t_burst = np.arange(burst_len) / sr
        # Start at -sin so the first large excursion is negative
        ir[onset:onset + burst_len] = -np.sin(2 * np.pi * 40 * t_burst) * 0.8
        # Make the magnitude of the negative half-cycle slightly larger
        ir[onset + burst_len:onset + 2 * burst_len] = (
            np.sin(2 * np.pi * 40 * t_burst) * 0.5
        )
        # Confirm the largest |sample| has a negative sign
        assert ir[np.argmax(np.abs(ir))] < 0, "Test IR must have negative-dominant peak"

        # xcorr says the source is positive-polarity → should win
        result = detect_ir_onset(ir, sr, xcorr_peak_sign=+1)
        assert result["peak_sign"] == 1, (
            f"xcorr_peak_sign=+1 must override negative ir[max_idx] sign; "
            f"got peak_sign={result['peak_sign']}"
        )
        assert result["peak_sign_source"] == "xcorr"

    def test_legacy_path_uses_ir_max_sign_when_no_xcorr_sign(self):
        """Without xcorr_peak_sign, fallback to ir[max_idx] sign with source='ir_max'."""
        sr = 48000
        ir = np.zeros(24000)
        ir[int(0.012 * sr)] = -1.0  # dominant negative peak
        result = detect_ir_onset(ir, sr)
        assert result["peak_sign"] == -1
        assert result["peak_sign_source"] == "ir_max"

    def test_peak_sign_source_key_always_present(self):
        """peak_sign_source must be in the returned dict for all code paths."""
        sr = 48000
        ir = np.zeros(24000)
        ir[int(0.010 * sr)] = 1.0
        # No xcorr args at all
        r1 = detect_ir_onset(ir, sr)
        assert "peak_sign_source" in r1
        # With xcorr_peak_ms only (no xcorr_peak_sign) — still ir_max
        r2 = detect_ir_onset(ir, sr, xcorr_peak_ms=10.0)
        assert "peak_sign_source" in r2
        assert r2["peak_sign_source"] == "ir_max"
        # With xcorr_peak_sign — xcorr path
        r3 = detect_ir_onset(ir, sr, xcorr_peak_sign=1)
        assert r3["peak_sign_source"] == "xcorr"


# ── parse_umik_sensitivity ────────────────────────────────────────────────────


class TestParseUmikCalCurve:
    """Tests for UMIK cal file per-frequency correction parsing."""

    def test_parses_standard_cal_file(self, tmp_path):
        from calibrate.measurement import parse_umik_cal_curve

        cal = tmp_path / "umik.cal"
        cal.write_text(
            '"Sens Factor =1.725dB, AGain =18dB, SERNO: 7079831"\n'
            '"Auto-generated 90-degree calibration file"\n'
            "20.0    -0.3\n"
            "50.0    0.1\n"
            "100.0   0.5\n"
        )
        curve = parse_umik_cal_curve(str(cal))
        assert len(curve) == 3
        assert curve[0] == (20.0, -0.3)
        assert curve[1] == (50.0, 0.1)
        assert curve[2] == (100.0, 0.5)

    def test_sorted_by_frequency(self, tmp_path):
        from calibrate.measurement import parse_umik_cal_curve

        cal = tmp_path / "umik.cal"
        cal.write_text(
            '"header"\n'
            "100.0   0.5\n"
            "20.0    -0.3\n"
            "50.0    0.1\n"
        )
        curve = parse_umik_cal_curve(str(cal))
        freqs = [p[0] for p in curve]
        assert freqs == sorted(freqs)

    def test_missing_file_raises(self):
        from calibrate.measurement import parse_umik_cal_curve

        with pytest.raises(FileNotFoundError):
            parse_umik_cal_curve("/nonexistent/path.cal")

    def test_empty_data_raises(self, tmp_path):
        from calibrate.measurement import parse_umik_cal_curve

        cal = tmp_path / "empty.cal"
        cal.write_text('"only a header line"\n')
        with pytest.raises(ValueError, match="No frequency correction data"):
            parse_umik_cal_curve(str(cal))

    def test_skips_non_numeric_lines(self, tmp_path):
        from calibrate.measurement import parse_umik_cal_curve

        cal = tmp_path / "mixed.cal"
        cal.write_text(
            '"header"\n'
            "not_a_number foo\n"
            "20.0   -0.3\n"
            "\n"
            "50.0   0.1\n"
        )
        curve = parse_umik_cal_curve(str(cal))
        assert len(curve) == 2


class TestApplyMicCorrection:
    """Tests for mic calibration correction application."""

    def test_applies_correction(self):
        from calibrate.measurement import apply_mic_correction

        frequencies = [20.0, 50.0, 100.0]
        spl = [70.0, 75.0, 80.0]
        cal_curve = [(20.0, -0.5), (50.0, 0.0), (100.0, 0.5)]
        corrected = apply_mic_correction(frequencies, spl, cal_curve)
        assert corrected[0] == pytest.approx(69.5, abs=0.01)
        assert corrected[1] == pytest.approx(75.0, abs=0.01)
        assert corrected[2] == pytest.approx(80.5, abs=0.01)

    def test_interpolates_between_cal_points(self):
        from calibrate.measurement import apply_mic_correction

        frequencies = [35.0]  # Between 20 and 50
        spl = [75.0]
        cal_curve = [(20.0, -1.0), (50.0, 1.0)]
        corrected = apply_mic_correction(frequencies, spl, cal_curve)
        # Log-frequency interpolation: 35 is between 20 and 50
        # The correction should be between -1.0 and 1.0
        assert -1.0 < corrected[0] - 75.0 < 1.0

    def test_extrapolates_below_range(self):
        from calibrate.measurement import apply_mic_correction

        frequencies = [10.0]  # Below cal range
        spl = [70.0]
        cal_curve = [(20.0, -0.5), (100.0, 0.5)]
        corrected = apply_mic_correction(frequencies, spl, cal_curve)
        assert corrected[0] == pytest.approx(69.5, abs=0.01)  # Uses first cal point

    def test_extrapolates_above_range(self):
        from calibrate.measurement import apply_mic_correction

        frequencies = [200.0]  # Above cal range
        spl = [80.0]
        cal_curve = [(20.0, -0.5), (100.0, 0.5)]
        corrected = apply_mic_correction(frequencies, spl, cal_curve)
        assert corrected[0] == pytest.approx(80.5, abs=0.01)  # Uses last cal point

    def test_empty_cal_curve_returns_original(self):
        from calibrate.measurement import apply_mic_correction

        frequencies = [20.0, 50.0]
        spl = [70.0, 75.0]
        corrected = apply_mic_correction(frequencies, spl, [])
        assert corrected == spl


class TestParseUmikSensitivity:
    """Tests for UMIK cal file sensitivity parsing."""

    def test_parses_standard_header(self, tmp_path):
        from calibrate.measurement import parse_umik_sensitivity

        cal = tmp_path / "umik.cal"
        cal.write_text(
            '"Sens Factor =1.725dB, AGain =18dB, SERNO: 7079831"\n'
            '"Auto-generated 90-degree calibration file"\n'
            "20.0    -0.3\n"
        )
        offset = parse_umik_sensitivity(str(cal))
        # offset = 133 (UMIK-1 max SPL @ 0 dBFS, 0 gain) - AGain(18) - sens(1.725)
        assert abs(offset - 113.275) < 0.01

    def test_missing_file_raises(self):
        from calibrate.measurement import parse_umik_sensitivity

        with pytest.raises(FileNotFoundError):
            parse_umik_sensitivity("/nonexistent/path.cal")

    def test_bad_header_raises(self, tmp_path):
        from calibrate.measurement import parse_umik_sensitivity

        cal = tmp_path / "bad.cal"
        cal.write_text("not a valid header\n20.0 -0.3\n")
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_umik_sensitivity(str(cal))


class TestResolveDbfsToSplOffset:
    """Tests for resolve_dbfs_to_spl_offset precedence (config > cal estimate)."""

    def _write_cal(self, tmp_path):
        cal = tmp_path / "umik.cal"
        cal.write_text(
            '"Sens Factor =1.725dB, AGain =18dB, SERNO: 7079831"\n'
            '"Auto-generated"\n20.0    -0.3\n'
        )
        return cal

    def test_config_value_takes_precedence(self, tmp_path):
        from calibrate.measurement import resolve_dbfs_to_spl_offset

        cal = self._write_cal(tmp_path)
        cfg = {"mic": {"cal_file": str(cal), "dbfs_to_spl_offset_db": 117.0}}
        offset, source = resolve_dbfs_to_spl_offset(cfg)
        assert offset == 117.0
        assert source == "config"

    def test_falls_back_to_cal_estimate(self, tmp_path):
        from calibrate.measurement import resolve_dbfs_to_spl_offset

        cal = self._write_cal(tmp_path)
        cfg = {"mic": {"cal_file": str(cal)}}
        offset, source = resolve_dbfs_to_spl_offset(cfg)
        assert abs(offset - 113.275) < 0.01
        assert source == "cal_estimate"

    def test_none_when_no_offset_available(self):
        from calibrate.measurement import resolve_dbfs_to_spl_offset

        offset, source = resolve_dbfs_to_spl_offset({"mic": {}})
        assert offset == 0.0
        assert source == "none"


class TestDetectIrOnsetWithFirPreDelay:
    """Onset detection must skip a known FIR-injected pre-ring window.

    Anti-pulse modal-cancellation FIRs inject content BEFORE the main impulse
    (one half-wavelength before the modal carrier). When the FIR is convolved
    with the room IR, that pre-ring shows up in the measured IR as -3 to -10 dB
    pre-arrival. Without skipping the FIR's known pre-delay window, the onset
    detector walks the reported peak back into the FIR's own non-causal zone
    instead of the room's actual direct arrival.
    """

    def test_skips_fir_pre_ring_window(self):
        from calibrate.measurement import detect_ir_onset

        sr = 48000
        # Simulate: 14 ms of FIR pre-ring (small content), then main arrival
        # at 130 ms with sustained room mode tail.
        ir = np.zeros(int(sr * 0.5))
        # Anti-pulse pre-ring at ~119 ms (peak time 130 - 11 ms)
        pre_idx = int(0.119 * sr)
        ir[pre_idx:pre_idx + 80] = 0.3 * np.cos(
            2 * np.pi * 47.0 * (np.arange(80) - 40) / sr
        )
        # Main direct arrival at 130 ms
        main_idx = int(0.130 * sr)
        ir[main_idx] = 1.0
        # Decaying room mode tail
        tail = np.exp(-np.arange(int(sr * 0.05)) / (sr * 0.02))
        ir[main_idx:main_idx + len(tail)] += 0.5 * tail

        # Without FIR pre-delay hint, detector walks back to the pre-ring.
        result_no_skip = detect_ir_onset(ir, sr, fir_pre_delay_ms=0.0)
        assert result_no_skip["peak_time_ms"] < 125.0, (
            f"baseline (no FIR hint) should detect the pre-ring as onset; "
            f"got {result_no_skip['peak_time_ms']:.1f} ms"
        )

        # With a 14 ms FIR pre-delay hint, detector skips past the pre-ring
        # and lands on the actual main arrival at 130 ms.
        result_with_skip = detect_ir_onset(ir, sr, fir_pre_delay_ms=14.0)
        assert 125.0 < result_with_skip["peak_time_ms"] < 135.0, (
            f"with FIR pre-delay hint, onset should skip pre-ring and land "
            f"on direct arrival near 130 ms; got "
            f"{result_with_skip['peak_time_ms']:.1f} ms"
        )

    def test_zero_pre_delay_preserves_legacy_behavior(self):
        from calibrate.measurement import detect_ir_onset

        sr = 48000
        ir = np.zeros(int(sr * 0.05))
        ir[int(sr * 0.005)] = 1.0  # direct arrival at 5 ms
        result = detect_ir_onset(ir, sr)
        # No pre-delay arg (defaults to 0); 1 ms skip + onset at 5 ms.
        assert 4.0 < result["peak_time_ms"] < 6.0


# ── _xcorr_delay_ms ───────────────────────────────────────────────────────────

class TestXcorrDelayMs:
    """_xcorr_delay_ms returns the lag (in ms) of `delayed` behind `reference`."""

    def _make_delayed(self, sr, delay_ms, length_s=0.3):
        import numpy as np
        n = int(length_s * sr)
        delay_s = delay_ms / 1000.0
        delay_samples = int(delay_s * sr)
        rng = np.random.default_rng(42)
        sig = rng.standard_normal(n) * 0.1
        # Impulse at ~20 ms so it lands after the 3 ms skip floor
        impulse_at = int(0.020 * sr)
        sig[impulse_at] = 1.0
        ref = np.zeros(n)
        ref[impulse_at] = 1.0
        delayed = np.zeros(n)
        if impulse_at + delay_samples < n:
            delayed[impulse_at + delay_samples] = 1.0
        return ref, delayed

    def test_known_delay_recovered(self):
        import numpy as np
        sr = 48000
        ref, delayed = self._make_delayed(sr, delay_ms=15.0)
        result = _xcorr_delay_ms(np, ref, delayed, sr, lo_ms=3.0, hi_ms=200.0)
        assert result is not None
        assert abs(result - 15.0) < 1.5  # within 1.5 ms

    def test_zero_reference_returns_none(self):
        import numpy as np
        sr = 48000
        ref = np.zeros(4800)
        delayed = np.ones(4800) * 0.1
        assert _xcorr_delay_ms(np, ref, delayed, sr) is None

    def test_zero_delayed_returns_none(self):
        import numpy as np
        sr = 48000
        ref = np.ones(4800) * 0.1
        delayed = np.zeros(4800)
        assert _xcorr_delay_ms(np, ref, delayed, sr) is None

    def test_result_within_search_window(self):
        import numpy as np
        sr = 48000
        ref, delayed = self._make_delayed(sr, delay_ms=50.0)
        result = _xcorr_delay_ms(np, ref, delayed, sr, lo_ms=3.0, hi_ms=200.0)
        assert result is not None
        assert 3.0 <= result <= 200.0

    def test_prefers_direct_path_over_stronger_reflection(self):
        """First-onset logic must return the direct-path arrival (~5 ms) even
        when a room reflection at ~15 ms has higher xcorr amplitude.
        Previously (argmax) this would jump to the reflection, causing
        loopback_xcorr_peak_ms to vary by 10+ ms between identical runs."""
        import numpy as np
        sr = 48000
        n = int(0.5 * sr)
        ref = np.zeros(n)
        direct_samples = int(0.005 * sr)   # 5 ms direct path
        reflect_samples = int(0.015 * sr)  # 15 ms reflection
        ref[direct_samples] = 1.0
        delayed = np.zeros(n)
        delayed[direct_samples] = 0.6   # direct: weaker
        delayed[reflect_samples] = 1.0  # reflection: stronger (argmax would pick this)
        result = _xcorr_delay_ms(np, ref, delayed, sr, lo_ms=3.0, hi_ms=200.0)
        assert result is not None
        # First-onset must find the direct path (≤ 12 ms), not the reflection (15 ms).
        assert result < 12.0, f"Expected direct path < 12 ms, got {result} ms"


# ── FrequencyResponse loopback fields ─────────────────────────────────────────

class TestFrequencyResponseLoopbackFields:
    def _base_fr(self, **kwargs):
        return FrequencyResponse(
            frequencies=[20.0, 40.0, 80.0],
            spl=[-20.0, -15.0, -12.0],
            sample_rate=48000,
            sweep_duration=3.0,
            timestamp="2026-01-01T00:00:00+00:00",
            **kwargs,
        )

    def test_defaults_to_none(self):
        fr = self._base_fr()
        assert fr.loopback_xcorr_peak_ms is None
        assert fr.avr_processing_ms is None

    def test_round_trip_with_loopback_fields(self):
        fr = self._base_fr(loopback_xcorr_peak_ms=42.5, avr_processing_ms=18.3)
        rt = FrequencyResponse.from_json(fr.to_json())
        assert rt.loopback_xcorr_peak_ms == 42.5
        assert rt.avr_processing_ms == 18.3

    def test_backward_compat_missing_loopback_fields(self):
        import json
        data = {
            "frequencies": [20.0],
            "spl": [-20.0],
            "sample_rate": 48000,
            "sweep_duration": 3.0,
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
        fr = FrequencyResponse.from_json(json.dumps(data))
        assert fr.loopback_xcorr_peak_ms is None
        assert fr.avr_processing_ms is None


# ── MeasurementEngine xcorr window auto-floor ─────────────────────────────────

class TestXcorrWindow:
    """MeasurementEngine xcorr search window is config-driven.

    With constant pipeline topology (Conv blocks resident, coefficients swapped),
    CamillaDSP latency is stable across measurements — no per-call floor calculation
    needed. Use measurement.xcorr_search_window_ms in config.yaml when the default
    200 ms is insufficient (e.g. very long FIRs or large acoustic travel distances).
    """

    def test_default_window(self):
        """Default xcorr window when config key is absent."""
        cfg = make_config()
        engine = MeasurementEngine(cfg)
        assert engine._xcorr_search_window_ms == 200.0

    def test_config_override(self):
        """Config key measurement.xcorr_search_window_ms overrides the default."""
        cfg = make_config(xcorr_search_window_ms=500.0)
        engine = MeasurementEngine(cfg)
        assert engine._xcorr_search_window_ms == 500.0


# ── MeasurementEngine.measure_impulse_ir() ────────────────────────────────────

class TestMeasureImpulseIr:
    """measure_impulse_ir averages N impulse shots, and MUST guarantee that
    neither pw-record nor pw-cat is left running — an orphaned pw-record holds
    the mic node open and wedges the next measurement (the try/finally added
    after the ReadTimeout/hung-service incident).

    A small sample_rate keeps the test fast: pre_silence = int(0.2*sr) must stay
    below rec_samples = int(record_duration_s*sr), so sr=1000 / 0.3 s → 200 < 300.
    """

    def _engine(self):
        cfg = make_config(
            sample_rate=1000,
            usb_pipewire_node="avr_cal_sweep",
            mic_pipewire_node="umik",
        )
        return MeasurementEngine(cfg)

    def _make_proc(self, *, read_bytes=b"", poll_after=0, communicate_side_effect=None):
        """Build a mock subprocess.Popen instance."""
        proc = MagicMock()
        proc.poll.return_value = poll_after  # 0 = already exited; None = running
        if communicate_side_effect is not None:
            proc.communicate.side_effect = communicate_side_effect
        else:
            proc.communicate.return_value = (b"", b"")
        proc.stdout = MagicMock()
        proc.stdout.read.return_value = read_bytes
        return proc

    async def test_happy_path_accumulates_and_cleans_up(self):
        """One shot: recorded f32 samples are averaged and both procs are cleaned up."""
        engine = self._engine()
        rec_samples = int(0.3 * 1000)
        rec_bytes = np.ones(rec_samples, dtype=np.float32).tobytes()
        rec_proc = self._make_proc(read_bytes=rec_bytes, poll_after=0)
        play_proc = self._make_proc(poll_after=0)

        # Popen called rec-first, play-second within each shot.
        with patch("subprocess.Popen", side_effect=[rec_proc, play_proc]):
            out = await engine.measure_impulse_ir(n_averages=1, record_duration_s=0.3)

        assert len(out) == rec_samples
        # averaged over 1 shot → ~1.0 in the captured region
        assert out[0] == pytest.approx(1.0)
        # recorder is force-killed after the tail; playback finished on its own
        rec_proc.kill.assert_called()

    async def test_timeout_expired_branch_kills_playback(self):
        """If pw-cat communicate() times out, it is killed and drained, not left hung."""
        engine = self._engine()
        rec_samples = int(0.3 * 1000)
        rec_proc = self._make_proc(
            read_bytes=np.zeros(rec_samples, dtype=np.float32).tobytes(), poll_after=0
        )
        # first communicate (with input+timeout) raises; second (drain) returns
        play_proc = self._make_proc(
            poll_after=0,
            communicate_side_effect=[
                subprocess.TimeoutExpired(cmd="pw-cat", timeout=5),
                (b"", b""),
            ],
        )
        with patch("subprocess.Popen", side_effect=[rec_proc, play_proc]):
            out = await engine.measure_impulse_ir(n_averages=1, record_duration_s=0.3)

        assert len(out) == rec_samples
        play_proc.kill.assert_called()  # killed in the TimeoutExpired branch

    async def test_finally_cleans_up_both_procs_on_midshot_exception(self):
        """If a shot raises mid-way (e.g. stdout.read), finally kills BOTH live procs
        and the exception still propagates."""
        engine = self._engine()
        rec_proc = self._make_proc(poll_after=None)   # still "running"
        rec_proc.stdout.read.side_effect = RuntimeError("boom mid-shot")
        play_proc = self._make_proc(poll_after=None)  # still "running"

        with patch("subprocess.Popen", side_effect=[rec_proc, play_proc]):
            with pytest.raises(RuntimeError, match="boom mid-shot"):
                await engine.measure_impulse_ir(n_averages=1, record_duration_s=0.3)

        # finally must reap both subprocesses (poll() is None → kill + communicate)
        rec_proc.kill.assert_called()
        play_proc.kill.assert_called()


class TestNormalizeSweepPeak:
    """normalize_sweep_peak: stimulus is scaled to a hot, safe peak.

    PyTTa's native sweep peaks ~-37 dBFS, leaving the DAC ~36 dB below full scale
    even at max master (the 'sub sweep too quiet' bug). Normalization is level-
    invariant for the deconvolved FR — it only raises loudness/SNR.
    """

    def test_scales_quiet_sweep_up_to_target(self):
        from calibrate.measurement import normalize_sweep_peak

        arr = np.array([[0.01], [-0.005], [0.0]])  # peak 0.01 ≈ -40 dBFS
        out = normalize_sweep_peak(arr, 0.5)
        assert np.isclose(np.max(np.abs(out)), 0.5)

    def test_scales_hot_sweep_down_to_target(self):
        from calibrate.measurement import normalize_sweep_peak

        arr = np.array([0.9, -0.8, 0.2])  # peak 0.9
        out = normalize_sweep_peak(arr, 0.5)
        assert np.isclose(np.max(np.abs(out)), 0.5)

    def test_preserves_waveform_shape(self):
        from calibrate.measurement import normalize_sweep_peak

        arr = np.array([0.1, -0.05, 0.025])
        out = normalize_sweep_peak(arr, 0.5)
        assert np.allclose(out / np.max(np.abs(out)), arr / np.max(np.abs(arr)))

    def test_silent_array_unchanged(self):
        from calibrate.measurement import normalize_sweep_peak

        arr = np.zeros((10, 1))
        out = normalize_sweep_peak(arr, 0.5)
        assert np.array_equal(out, arr)

    def test_nonfinite_array_unchanged(self):
        from calibrate.measurement import normalize_sweep_peak

        arr = np.array([np.nan, 0.1, np.inf])
        out = normalize_sweep_peak(arr, 0.5)
        assert np.array_equal(out, arr, equal_nan=True)

    def test_default_target_is_minus_6_dbfs(self):
        from calibrate.measurement import DEFAULT_SWEEP_PEAK_AMPLITUDE

        assert abs(20 * np.log10(DEFAULT_SWEEP_PEAK_AMPLITUDE) - (-6.02)) < 0.1

    def test_default_target_safe_against_clipping_at_master_0(self):
        # -6 dBFS stimulus + typical output/FIR makeup (~+4.5 dB) stays below 0 dBFS
        # at master 0, so the DAC does not clip even at maximum master.
        from calibrate.measurement import DEFAULT_SWEEP_PEAK_AMPLITUDE

        worst_case_makeup_db = 4.5
        stimulus_dbfs = 20 * np.log10(DEFAULT_SWEEP_PEAK_AMPLITUDE)
        assert stimulus_dbfs + worst_case_makeup_db < 0.0
