"""Tests for calibrate/web.py — FastAPI web server."""

from __future__ import annotations

import struct
import json
import uuid
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from calibrate.config import Config
from calibrate.measurement import FrequencyResponse, MeasurementQualityError
from calibrate.web import app, _pending_sweeps, _pending_lock, COUNTDOWN_MS

# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_config(**extra) -> Config:
    base = {
        "denon": {"host": "192.168.1.100"},
        "minidsp": {"host": "localhost", "port": 5380},
        "mic": {"name": "UMIK"},
    }
    base.update(extra)
    return Config(base)


def _make_fr(n: int = 100) -> FrequencyResponse:
    freqs = np.linspace(20, 200, n).tolist()
    spl = np.random.uniform(-40, -20, n).tolist()
    return FrequencyResponse(
        frequencies=freqs,
        spl=spl,
        sample_rate=48000,
        sweep_duration=3.0,
        timestamp="2026-03-20T12:00:00+00:00",
    )


def _float32_bytes(samples: list[float]) -> bytes:
    return struct.pack(f"<{len(samples)}f", *samples)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def cfg_path(tmp_path):
    """Write a minimal config YAML and return its path."""
    import yaml
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump({
        "denon": {"host": "192.168.1.100"},
        "minidsp": {"host": "localhost", "port": 5380},
        "mic": {"name": "UMIK"},
    }))
    return p


@pytest.fixture(autouse=True)
def clean_pending():
    """Ensure _pending_sweeps is empty before each test."""
    with _pending_lock:
        _pending_sweeps.clear()
    yield
    with _pending_lock:
        _pending_sweeps.clear()


# ── GET / ─────────────────────────────────────────────────────────────────────

def test_index_returns_200(client):
    r = client.get("/")
    assert r.status_code == 200


def test_index_content_type_html(client):
    r = client.get("/")
    assert "text/html" in r.headers["content-type"]


def test_index_contains_chartjs(client):
    r = client.get("/")
    assert "chart.js" in r.text.lower()


def test_index_contains_measure_button(client):
    r = client.get("/")
    assert "startMeasurement" in r.text


# ── GET /health ────────────────────────────────────────────────────────────────

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ── POST /api/measure/start ────────────────────────────────────────────────────

def test_measure_start_success(client, cfg_path):
    sweep_samples = [0.1, 0.2, -0.1] * 48000  # 3s of fake sweep
    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
        patch("threading.Thread"),
    ):
        engine = MockEngine.return_value
        engine.generate_sweep.return_value = (sweep_samples, 48000, 3.0)

        r = client.post("/api/measure/start", json={"label": "test run"})

    assert r.status_code == 200
    data = r.json()
    assert "token" in data
    assert data["sample_rate"] == 48000
    assert data["sweep_duration"] == 3.0
    assert data["countdown_ms"] == COUNTDOWN_MS


def test_measure_start_stores_pending(client, cfg_path):
    sweep_samples = [0.0] * 1000
    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
        patch("threading.Thread"),
    ):
        engine = MockEngine.return_value
        engine.generate_sweep.return_value = (sweep_samples, 48000, 3.0)

        r = client.post("/api/measure/start", json={"label": None})

    token = r.json()["token"]
    with _pending_lock:
        assert token in _pending_sweeps
        assert _pending_sweeps[token]["sweep_samples"] == sweep_samples


def test_measure_start_no_label(client, cfg_path):
    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
        patch("threading.Thread"),
    ):
        engine = MockEngine.return_value
        engine.generate_sweep.return_value = ([0.0] * 100, 48000, 3.0)
        r = client.post("/api/measure/start", json={})

    assert r.status_code == 200
    token = r.json()["token"]
    with _pending_lock:
        assert _pending_sweeps[token]["label"] is None


def test_measure_start_engine_error(client, cfg_path):
    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
    ):
        engine = MockEngine.return_value
        engine.generate_sweep.side_effect = RuntimeError("pytta not available")

        r = client.post("/api/measure/start", json={})

    assert r.status_code == 500
    assert "pytta" in r.json()["detail"]


def test_measure_start_missing_config(client, tmp_path):
    missing = tmp_path / "missing.yaml"
    with patch("calibrate.web.CONFIG_PATH", missing):
        r = client.post("/api/measure/start", json={})
    assert r.status_code == 503


def test_measure_start_spawns_background_thread(client, cfg_path):
    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
        patch("calibrate.web.threading.Thread") as MockThread,
    ):
        engine = MockEngine.return_value
        engine.generate_sweep.return_value = ([0.0] * 100, 48000, 3.0)
        r = client.post("/api/measure/start", json={})

    assert r.status_code == 200
    MockThread.assert_called_once()
    _, kwargs = MockThread.call_args
    assert kwargs.get("daemon") is True


def test_play_background_thread_logs_runtime_error(client, cfg_path):
    """The _play() closure logs RuntimeError from play_signal instead of crashing."""
    captured_target = {}

    def capture_thread(target=None, daemon=False):
        captured_target["fn"] = target
        m = MagicMock()
        m.start = MagicMock()
        return m

    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
        patch("calibrate.web.threading.Thread", side_effect=capture_thread),
        patch("calibrate.web.time.sleep"),  # skip the countdown sleep
    ):
        engine = MockEngine.return_value
        engine.generate_sweep.return_value = ([0.0] * 100, 48000, 3.0)
        engine.play_signal.side_effect = RuntimeError("audio device unavailable")
        client.post("/api/measure/start", json={})

    # Call the actual _play() function synchronously — should log, not raise
    assert captured_target.get("fn") is not None
    captured_target["fn"]()  # must not raise


# ── POST /api/measure/record ───────────────────────────────────────────────────

def _inject_pending(token: str, sweep_samples=None):
    """Directly inject a pending sweep for testing the record endpoint."""
    if sweep_samples is None:
        sweep_samples = [0.1, -0.1] * 500
    with _pending_lock:
        _pending_sweeps[token] = {
            "sweep_samples": sweep_samples,
            "sample_rate": 48000,
            "sweep_duration": 3.0,
            "freq_min": 20,
            "freq_max": 200,
            "label": "unit test",
        }


def test_measure_record_success(client, cfg_path):
    token = str(uuid.uuid4())
    sweep = [0.1, -0.1] * 500
    _inject_pending(token, sweep)
    recording = [0.05, -0.05] * 600
    body = _float32_bytes(recording)

    fr = _make_fr()

    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
        patch("calibrate.web.SessionStore") as MockStore,
    ):
        engine = MockEngine.return_value
        engine.compute_fr.return_value = fr
        store = MockStore.return_value
        store.save_measurement.return_value = 42

        r = client.post(
            "/api/measure/record",
            content=body,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Token": token,
                "X-Sample-Rate": "48000",
            },
        )

    assert r.status_code == 200
    data = r.json()
    assert data["session_id"] == 42
    assert "frequencies_hz" in data
    assert "spl_dbfs" in data
    assert "peak_spl" in data
    assert "freq_at_peak" in data


def test_measure_record_removes_pending(client, cfg_path):
    token = str(uuid.uuid4())
    _inject_pending(token)
    body = _float32_bytes([0.0] * 100)

    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
        patch("calibrate.web.SessionStore") as MockStore,
    ):
        MockEngine.return_value.compute_fr.return_value = _make_fr()
        MockStore.return_value.save_measurement.return_value = 1
        client.post(
            "/api/measure/record",
            content=body,
            headers={"Content-Type": "application/octet-stream", "X-Token": token},
        )

    with _pending_lock:
        assert token not in _pending_sweeps


def test_measure_record_unknown_token(client):
    body = _float32_bytes([0.0] * 100)
    r = client.post(
        "/api/measure/record",
        content=body,
        headers={"Content-Type": "application/octet-stream", "X-Token": "bad-token"},
    )
    assert r.status_code == 404


def test_measure_record_empty_body(client):
    token = str(uuid.uuid4())
    _inject_pending(token)
    r = client.post(
        "/api/measure/record",
        content=b"",
        headers={"Content-Type": "application/octet-stream", "X-Token": token},
    )
    assert r.status_code == 400


def test_measure_record_compute_fr_error(client, cfg_path):
    token = str(uuid.uuid4())
    _inject_pending(token)
    body = _float32_bytes([0.1] * 100)

    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
    ):
        MockEngine.return_value.compute_fr.side_effect = RuntimeError("numpy missing")
        r = client.post(
            "/api/measure/record",
            content=body,
            headers={"Content-Type": "application/octet-stream", "X-Token": token},
        )

    assert r.status_code == 500


def test_measure_record_quality_error_returns_422(client, cfg_path):
    """MeasurementQualityError → 422 with structured error body."""
    token = str(uuid.uuid4())
    _inject_pending(token)
    body = _float32_bytes([0.01] * 100)

    exc = MeasurementQualityError("sweep_capture", "no sweep found", "check amp")

    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
    ):
        MockEngine.return_value.compute_fr.side_effect = exc
        r = client.post(
            "/api/measure/record",
            content=body,
            headers={"Content-Type": "application/octet-stream", "X-Token": token},
        )

    assert r.status_code == 422
    data = r.json()
    assert data["error"] == "measurement_quality"
    assert data["check"] == "sweep_capture"
    assert data["detail"] == "no sweep found"
    assert data["suggestion"] == "check amp"


def test_measure_record_response_includes_warnings(client, cfg_path):
    """Successful record response includes warnings array from FrequencyResponse."""
    token = str(uuid.uuid4())
    _inject_pending(token)
    body = _float32_bytes([0.05] * 100)

    fr = _make_fr()
    fr.warnings = [{"check": "floor_noise", "detail": "noisy room"}]

    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
        patch("calibrate.web.SessionStore") as MockStore,
    ):
        MockEngine.return_value.compute_fr.return_value = fr
        MockStore.return_value.save_measurement.return_value = 1
        r = client.post(
            "/api/measure/record",
            content=body,
            headers={"Content-Type": "application/octet-stream", "X-Token": token},
        )

    assert r.status_code == 200
    assert r.json()["warnings"] == [{"check": "floor_noise", "detail": "noisy room"}]


def test_measure_record_uses_x_sample_rate_header(client, cfg_path):
    token = str(uuid.uuid4())
    _inject_pending(token)
    body = _float32_bytes([0.0] * 100)

    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
        patch("calibrate.web.SessionStore") as MockStore,
    ):
        MockEngine.return_value.compute_fr.return_value = _make_fr()
        MockStore.return_value.save_measurement.return_value = 7
        client.post(
            "/api/measure/record",
            content=body,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Token": token,
                "X-Sample-Rate": "44100",
            },
        )
        _, kwargs = MockEngine.return_value.compute_fr.call_args
        assert kwargs["sample_rate"] == 44100


# ── GET /api/sessions ─────────────────────────────────────────────────────────

def test_list_sessions_empty(client):
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = []
        r = client.get("/api/sessions")
    assert r.status_code == 200
    assert r.json() == []


def test_list_sessions_returns_sessions(client):
    from calibrate.storage import Session
    fr = _make_fr()
    sessions = [
        Session(id=1, timestamp="2026-03-20T12:00:00+00:00", label="run 1",
                start_fr=fr, end_fr=None, filters_applied=None, notes=None),
        Session(id=2, timestamp="2026-03-20T13:00:00+00:00", label=None,
                start_fr=fr, end_fr=fr, filters_applied=None, notes=None),
    ]
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = sessions
        r = client.get("/api/sessions")

    data = r.json()
    assert len(data) == 2
    assert data[0]["id"] == 1
    assert data[0]["label"] == "run 1"
    assert data[0]["has_end_fr"] is False
    assert data[1]["has_end_fr"] is True
    assert "peak_spl" in data[0]
    assert "n_freqs" in data[0]


# ── POST /api/feedback/{session_id} ───────────────────────────────────────────

def test_add_feedback_success(client):
    fr = _make_fr()
    from calibrate.storage import Session
    session = Session(id=5, timestamp="2026-03-20T12:00:00+00:00", label=None,
                      start_fr=fr, end_fr=None, filters_applied=None, notes=None)
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.return_value = session
        MockStore.return_value.add_feedback.return_value = 99
        r = client.post("/api/feedback/5", json={"text": "great bass", "content_tag": "movie"})

    assert r.status_code == 200
    assert r.json()["feedback_id"] == 99


def test_add_feedback_session_not_found(client):
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.return_value = None
        r = client.post("/api/feedback/999", json={"text": "test"})
    assert r.status_code == 404


def test_add_feedback_no_content_tag(client):
    fr = _make_fr()
    from calibrate.storage import Session
    session = Session(id=3, timestamp="2026-03-20T12:00:00+00:00", label=None,
                      start_fr=fr, end_fr=None, filters_applied=None, notes=None)
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.return_value = session
        MockStore.return_value.add_feedback.return_value = 1
        r = client.post("/api/feedback/3", json={"text": "muddy"})

    assert r.status_code == 200
    _, kwargs = MockStore.return_value.add_feedback.call_args
    assert kwargs["content_tag"] is None


# ── CLI web command ────────────────────────────────────────────────────────────

def test_web_command_help():
    from click.testing import CliRunner
    from calibrate.cli import cli
    result = CliRunner().invoke(cli, ["web", "--help"])
    assert result.exit_code == 0
    assert "--host" in result.output
    assert "--port" in result.output


def test_web_command_invokes_uvicorn():
    from click.testing import CliRunner
    from calibrate.cli import cli
    with patch("uvicorn.run") as mock_run:
        result = CliRunner().invoke(cli, ["web"])
    mock_run.assert_called_once_with(
        "calibrate.web:app", host="0.0.0.0", port=8000, reload=False
    )


def test_web_command_custom_host_port():
    from click.testing import CliRunner
    from calibrate.cli import cli
    with patch("uvicorn.run") as mock_run:
        CliRunner().invoke(cli, ["web", "--host", "127.0.0.1", "--port", "9000"])
    mock_run.assert_called_once_with(
        "calibrate.web:app", host="127.0.0.1", port=9000, reload=False
    )
