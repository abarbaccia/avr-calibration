"""Tests for POST /api/measure — Pi 5 headless measurement endpoint.

Coverage diagram:
  POST /api/measure
  ├── [TESTED] happy path: UMIK found → MeasurementEngine.measure() → session_id returned
  ├── [TESTED] no UMIK device found → HTTP 503
  ├── [TESTED] sounddevice not installed → HTTP 503
  ├── [TESTED] concurrent call while lock held → HTTP 409
  ├── [TESTED] measure() raises RuntimeError (PortAudioError) → HTTP 503
  ├── [TESTED] Denon: configured + powered off → power on, switch input, measure, restore, power off
  ├── [TESTED] Denon: configured + already on → switch input, measure, restore (no power off)
  └── [TESTED] Denon: unreachable → HTTP 503 before measurement
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio

import pytest
import yaml
from fastapi.testclient import TestClient

from calibrate import web
from calibrate.measurement import FrequencyResponse
from calibrate.web import app, _measurement_lock


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def cfg_path(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump({
        "denon": {"host": "192.168.1.100"},
        "minidsp": {"host": "localhost", "port": 5380},
        "mic": {"name": "UMIK"},
    }))
    return p


async def _async_return(val):
    return val


def _coro(val):
    """Return a coroutine that resolves to val — for mocking async methods."""
    return _async_return(val)


def _make_fr() -> FrequencyResponse:
    import numpy as np
    freqs = np.linspace(20, 200, 50).tolist()
    spl = [-30.0] * 50
    return FrequencyResponse(
        frequencies=freqs,
        spl=spl,
        sample_rate=48000,
        sweep_duration=3.0,
        timestamp="2026-04-01T00:00:00+00:00",
    )


class TestMeasureHeadlessEndpoint:
    def test_happy_path_returns_session_id(self, client, cfg_path):
        """UMIK found → measure() succeeds → session_id returned."""
        mock_sd = sys.modules["sounddevice"]
        mock_sd.query_devices.return_value = [
            {"name": "UMIK-1", "max_input_channels": 1},
        ]

        with (
            patch("calibrate.web.CONFIG_PATH", cfg_path),
            patch("calibrate.web.MeasurementEngine") as MockEngine,
            patch("calibrate.web.SessionStore") as MockStore,
        ):
            MockEngine.return_value.measure.return_value = _make_fr()
            MockStore.return_value.save_measurement.return_value = 42

            r = client.post("/api/measure", json={"label": "test"})

        assert r.status_code == 200
        data = r.json()
        assert data["session_id"] == 42
        assert data["status"] == "ok"

    def test_no_umik_returns_503(self, client, cfg_path):
        """No UMIK device → HTTP 503."""
        mock_sd = sys.modules["sounddevice"]
        mock_sd.query_devices.return_value = [
            {"name": "Built-in Microphone", "max_input_channels": 1},
        ]

        with patch("calibrate.web.CONFIG_PATH", cfg_path):
            r = client.post("/api/measure", json={})

        assert r.status_code == 503
        assert "UMIK" in r.json()["detail"] or "microphone" in r.json()["detail"].lower()

    def test_sounddevice_unavailable_returns_503(self, client, cfg_path):
        """sounddevice not installed → HTTP 503."""
        with (
            patch("calibrate.web.CONFIG_PATH", cfg_path),
            patch.dict(sys.modules, {"sounddevice": None}),
        ):
            r = client.post("/api/measure", json={})

        assert r.status_code == 503
        assert "sounddevice" in r.json()["detail"].lower() or "platform" in r.json()["detail"].lower()

    def test_concurrent_call_returns_409(self, client, cfg_path):
        """Second call while lock is held → HTTP 409."""
        with patch.object(_measurement_lock, "locked", return_value=True):
            with patch("calibrate.web.CONFIG_PATH", cfg_path):
                r = client.post("/api/measure", json={})

        assert r.status_code == 409
        assert "in progress" in r.json()["detail"].lower()

    def test_portaudio_error_returns_503(self, client, cfg_path):
        """measure() raising RuntimeError (PortAudioError) → HTTP 503."""
        mock_sd = sys.modules["sounddevice"]
        mock_sd.query_devices.return_value = [
            {"name": "UMIK-1", "max_input_channels": 1},
        ]

        with (
            patch("calibrate.web.CONFIG_PATH", cfg_path),
            patch("calibrate.web.MeasurementEngine") as MockEngine,
        ):
            MockEngine.return_value.measure.side_effect = RuntimeError("Audio device error: No output")

            r = client.post("/api/measure", json={})

        assert r.status_code == 503
        assert "Audio device error" in r.json()["detail"]

    def test_denon_powered_off_is_powered_on_and_restored(self, client, tmp_path):
        """Denon off → power on, switch input, set volume, measure, restore input/volume, power off."""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump({
            "denon": {"host": "192.168.1.209"},
            "minidsp": {"host": "localhost", "port": 5380},
            "mic": {"name": "UMIK"},
            "measurement": {
                "denon_sweep_input": "AUX1",
                "denon_sweep_volume": -25.0,
            },
        }))

        mock_sd = sys.modules["sounddevice"]
        mock_sd.query_devices.return_value = [
            {"name": "UMIK-1", "max_input_channels": 1},
        ]

        mock_receiver = MagicMock()
        mock_receiver.power = "OFF"
        mock_receiver.input_func = "SHIELD"
        mock_receiver.volume = -30.0
        mock_receiver.async_setup = AsyncMock()
        mock_receiver.async_update = AsyncMock()
        mock_receiver.async_power_on = AsyncMock()
        mock_receiver.async_power_off = AsyncMock()
        mock_receiver.async_set_input_func = AsyncMock()
        mock_receiver.async_set_volume = AsyncMock()

        with (
            patch("calibrate.web.CONFIG_PATH", cfg_path),
            patch("calibrate.web.MeasurementEngine") as MockEngine,
            patch("calibrate.web.SessionStore") as MockStore,
            patch("calibrate.web.asyncio.sleep", new_callable=AsyncMock),
            patch("denonavr.DenonAVR", return_value=mock_receiver),
        ):
            MockEngine.return_value.measure.return_value = _make_fr()
            MockStore.return_value.save_measurement.return_value = 99

            r = client.post("/api/measure", json={"label": "test"})

        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.json()}"
        mock_receiver.async_power_on.assert_called_once()
        mock_receiver.async_set_input_func.assert_any_call("AUX1")
        mock_receiver.async_set_volume.assert_any_call(-25.0)
        # restore: original input + volume, then power off
        mock_receiver.async_set_input_func.assert_called_with("SHIELD")
        mock_receiver.async_set_volume.assert_called_with(-30.0)
        mock_receiver.async_power_off.assert_called_once()

    def test_denon_already_on_no_power_cycle(self, client, tmp_path):
        """Denon already on → switch input, measure, restore input — no power off."""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump({
            "denon": {"host": "192.168.1.209"},
            "minidsp": {"host": "localhost", "port": 5380},
            "mic": {"name": "UMIK"},
            "measurement": {
                "denon_sweep_input": "AUX1",
                "denon_sweep_volume": -25.0,
            },
        }))

        mock_sd = sys.modules["sounddevice"]
        mock_sd.query_devices.return_value = [
            {"name": "UMIK-1", "max_input_channels": 1},
        ]

        mock_receiver = MagicMock()
        mock_receiver.power = "ON"
        mock_receiver.input_func = "CBL/SAT"
        mock_receiver.volume = -20.0
        mock_receiver.async_setup = AsyncMock()
        mock_receiver.async_update = AsyncMock()
        mock_receiver.async_power_on = AsyncMock()
        mock_receiver.async_power_off = AsyncMock()
        mock_receiver.async_set_input_func = AsyncMock()
        mock_receiver.async_set_volume = AsyncMock()

        with (
            patch("calibrate.web.CONFIG_PATH", cfg_path),
            patch("calibrate.web.MeasurementEngine") as MockEngine,
            patch("calibrate.web.SessionStore") as MockStore,
            patch("calibrate.web.asyncio.sleep", new_callable=AsyncMock),
            patch("denonavr.DenonAVR", return_value=mock_receiver),
        ):
            MockEngine.return_value.measure.return_value = _make_fr()
            MockStore.return_value.save_measurement.return_value = 7

            r = client.post("/api/measure", json={})

        assert r.status_code == 200
        mock_receiver.async_power_on.assert_not_called()
        mock_receiver.async_power_off.assert_not_called()
        mock_receiver.async_set_input_func.assert_any_call("AUX1")

    def test_denon_unreachable_returns_503(self, client, tmp_path):
        """Denon configured but unreachable → HTTP 503, no measurement attempted."""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.dump({
            "denon": {"host": "192.168.1.209"},
            "minidsp": {"host": "localhost", "port": 5380},
            "mic": {"name": "UMIK"},
            "measurement": {"denon_sweep_input": "AUX1"},
        }))

        mock_sd = sys.modules["sounddevice"]
        mock_sd.query_devices.return_value = [
            {"name": "UMIK-1", "max_input_channels": 1},
        ]

        mock_receiver = MagicMock()
        mock_receiver.async_setup = MagicMock(side_effect=ConnectionError("timeout"))

        with (
            patch("calibrate.web.CONFIG_PATH", cfg_path),
            patch("calibrate.web.MeasurementEngine") as MockEngine,
            patch("denonavr.DenonAVR", return_value=mock_receiver),
        ):
            r = client.post("/api/measure", json={})

        assert r.status_code == 503
        assert "Denon" in r.json()["detail"]
        MockEngine.return_value.measure.assert_not_called()
