"""Tests for POST /api/measure — Pi 5 headless measurement endpoint.

Coverage diagram:
  POST /api/measure
  ├── [TESTED] happy path: UMIK found → MeasurementEngine.measure() → session_id returned
  ├── [TESTED] no UMIK device found → HTTP 503
  ├── [TESTED] sounddevice not installed → HTTP 503
  ├── [TESTED] concurrent call while lock held → HTTP 409
  └── [TESTED] measure() raises RuntimeError (PortAudioError) → HTTP 503
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

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
