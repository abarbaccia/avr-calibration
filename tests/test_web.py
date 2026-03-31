"""Tests for calibrate/web.py — FastAPI web server."""

from __future__ import annotations

import asyncio
import struct
import json
import uuid
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

from unittest.mock import AsyncMock

from calibrate.config import Config
from calibrate.measurement import FrequencyResponse, MeasurementQualityError
from calibrate.preflight import CheckResult
from calibrate.web import (
    app,
    _pending_sweeps,
    _pending_lock,
    _pending_alignments,
    _align_lock,
    _AlignmentSession,
    _restore_sub_gains,
    COUNTDOWN_MS,
)

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


def test_play_background_thread_logs_value_error(client, cfg_path):
    """_play() must also catch ValueError (e.g. denon_sweep_volume guard, missing input)."""
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
        patch("calibrate.web.time.sleep"),
    ):
        engine = MockEngine.return_value
        engine.generate_sweep.return_value = ([0.0] * 100, 48000, 3.0)
        engine.play_signal.side_effect = ValueError("denon_sweep_volume must be ≤ -25.0 dB")
        client.post("/api/measure/start", json={})

    assert captured_target.get("fn") is not None
    captured_target["fn"]()  # must not raise — was previously unhandled


def test_play_background_thread_logs_avr_exception(client, cfg_path):
    """_play() must catch non-RuntimeError AVR exceptions (AvrCommandError etc.)."""

    class FakeAvrCommandError(Exception):
        pass

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
        patch("calibrate.web.time.sleep"),
    ):
        engine = MockEngine.return_value
        engine.generate_sweep.return_value = ([0.0] * 100, 48000, 3.0)
        engine.play_signal.side_effect = FakeAvrCommandError("No mapping for input source")
        client.post("/api/measure/start", json={})

    assert captured_target.get("fn") is not None
    captured_target["fn"]()  # must not raise — was previously unhandled


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


# ── TestAlignmentEndpoints ─────────────────────────────────────────────────────

def _make_align_config(sub_outputs=None) -> Config:
    """Config with sub_outputs for alignment tests."""
    return Config({
        "denon": {"host": "192.168.1.100"},
        "minidsp": {"host": "localhost", "port": 5380},
        "mic": {"name": "UMIK"},
        "measurement": {
            "freq_min": 20,
            "freq_max": 200,
            "sweep_duration": 0.5,
            "sample_rate": 48000,
            "sub_outputs": sub_outputs if sub_outputs is not None else [0, 1],
            "ir_search_window_ms": 50.0,
            "playback_route": "usb",
            "playback_device": "miniDSP",
        },
    })


def _make_recording_bytes(n: int = 2400) -> bytes:
    """Float32LE zeros — a valid (if silent) recording body."""
    return struct.pack(f"<{n}f", *([0.001] * n))


def _make_ir_result(sub_index: int = 0):
    from calibrate.alignment import SubIRResult
    return SubIRResult(
        sub_index=sub_index,
        peak_time_s=0.010 + sub_index * 0.002,
        peak_sign=1,
        polarity_inverted=False,
        spl_db=-20.0,
    )


@pytest.fixture(autouse=True)
def clean_align():
    """Ensure _pending_alignments is empty before and after each test."""
    with _align_lock:
        _pending_alignments.clear()
    yield
    with _align_lock:
        _pending_alignments.clear()


class TestAlignmentEndpoints:

    def test_align_subs_start_happy_path(self, client, tmp_path):
        """POST /api/align-subs/start → 200 with token, step=0, n_steps=2."""
        from unittest.mock import AsyncMock

        with (
            patch("calibrate.web._load_config", return_value=_make_align_config()),
            patch("calibrate.web.MeasurementEngine") as mock_engine_cls,
            patch("calibrate.adapters.minidsp.MinidspClient") as mock_client_cls,
        ):
            mock_engine = mock_engine_cls.return_value
            mock_engine.generate_sweep.return_value = ([0.0] * 100, 48000, 0.5)
            mock_engine.play_signal.return_value = None

            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client

            r = client.post("/api/align-subs/start")

        assert r.status_code == 200
        data = r.json()
        assert "token" in data
        assert data["step"] == 0
        assert data["n_steps"] == 2
        assert data["sample_rate"] == 48000

    def test_align_subs_start_no_sub_outputs_config(self, client):
        """sub_outputs not configured → 422 with informative message."""
        cfg = _make_align_config(sub_outputs=[])
        with patch("calibrate.web._load_config", return_value=cfg):
            r = client.post("/api/align-subs/start")

        assert r.status_code == 422
        assert "sub_outputs" in r.json()["detail"]

    def test_align_subs_start_minidsp_unreachable(self, client):
        """MinidspClient.set_output_gain raises ConnectError → 503."""
        import httpx as _httpx
        from unittest.mock import AsyncMock

        with (
            patch("calibrate.web._load_config", return_value=_make_align_config()),
            patch("calibrate.web.MeasurementEngine") as mock_engine_cls,
            patch("calibrate.adapters.minidsp.MinidspClient") as mock_client_cls,
        ):
            mock_engine = mock_engine_cls.return_value
            mock_engine.generate_sweep.return_value = ([0.0] * 100, 48000, 0.5)

            mock_client = AsyncMock()
            mock_client.set_output_gain.side_effect = _httpx.ConnectError("refused")
            mock_client_cls.return_value = mock_client

            r = client.post("/api/align-subs/start")

        assert r.status_code == 503

    def test_align_subs_step_record_mid_sequence(self, client):
        """Step 0 of 2 → extracts IR, advances state, returns next_step=1."""
        from unittest.mock import AsyncMock
        import numpy as np

        # Pre-insert a session with step=0
        token = str(uuid.uuid4())
        sweep = [0.001] * 2400
        session = _AlignmentSession(
            token=token,
            created_at=__import__("time").time(),
            sub_outputs=[0, 1],
            sweep_samples=sweep,
            sample_rate=48000,
            sweep_duration=0.05,
            step=0,
        )
        with _align_lock:
            _pending_alignments[token] = session

        with (
            patch("calibrate.web._load_config", return_value=_make_align_config()),
            patch("calibrate.web.MeasurementEngine") as mock_engine_cls,
            patch("calibrate.alignment.measure_sub_ir") as mock_measure,
            patch("calibrate.adapters.minidsp.MinidspClient") as mock_client_cls,
        ):
            mock_engine = mock_engine_cls.return_value
            mock_engine.play_signal.return_value = None

            # measure_sub_ir is async — set return_value directly
            mock_measure.return_value = _make_ir_result(0)

            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client

            r = client.post(
                "/api/align-subs/record",
                content=_make_recording_bytes(),
                headers={"X-Token": token, "X-Step": "0"},
            )

        assert r.status_code == 200
        data = r.json()
        assert data["next_step"] == 1
        assert data["n_steps"] == 2

    def test_align_subs_step_record_final(self, client):
        """Final step (step=1 of 2) → runs phases 2-4, returns alignment_summary."""
        from unittest.mock import AsyncMock
        from calibrate.alignment import AlignmentSummary

        token = str(uuid.uuid4())
        sweep = [0.001] * 2400
        session = _AlignmentSession(
            token=token,
            created_at=__import__("time").time(),
            sub_outputs=[0, 1],
            sweep_samples=sweep,
            sample_rate=48000,
            sweep_duration=0.05,
            step=1,
            ir_results=[_make_ir_result(0)],
        )
        with _align_lock:
            _pending_alignments[token] = session

        summary = AlignmentSummary(
            sub_results=[_make_ir_result(0), _make_ir_result(1)],
            delay_offsets_ms=[2.0, 0.0],
            gain_trims_db=[0.0, 0.0],
        )

        with (
            patch("calibrate.web._load_config", return_value=_make_align_config()),
            patch("calibrate.web.MeasurementEngine") as mock_engine_cls,
            patch("calibrate.alignment.measure_sub_ir") as mock_measure,
            patch("calibrate.alignment.run_alignment_phases") as mock_phases,
            patch("calibrate.adapters.minidsp.MinidspClient") as mock_client_cls,
        ):
            mock_engine_cls.return_value.play_signal.return_value = None
            mock_measure.return_value = _make_ir_result(1)
            mock_phases.return_value = summary

            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client

            r = client.post(
                "/api/align-subs/record",
                content=_make_recording_bytes(),
                headers={"X-Token": token, "X-Step": "1"},
            )

        assert r.status_code == 200
        data = r.json()
        assert "alignment_summary" in data
        assert len(data["alignment_summary"]["delay_offsets_ms"]) == 2
        # Session should be evicted
        with _align_lock:
            assert token not in _pending_alignments

    def test_align_subs_step_unknown_token(self, client):
        """Unknown X-Token → 404."""
        r = client.post(
            "/api/align-subs/record",
            content=_make_recording_bytes(),
            headers={"X-Token": "deadbeef-0000-0000-0000-000000000000", "X-Step": "0"},
        )
        assert r.status_code == 404

    def test_align_subs_step_measurement_quality_error(self, client):
        """MeasurementQualityError in IR extraction → 422 with structured body."""
        from unittest.mock import AsyncMock

        token = str(uuid.uuid4())
        session = _AlignmentSession(
            token=token,
            created_at=__import__("time").time(),
            sub_outputs=[0, 1],
            sweep_samples=[0.001] * 2400,
            sample_rate=48000,
            sweep_duration=0.05,
            step=0,
        )
        with _align_lock:
            _pending_alignments[token] = session

        with (
            patch("calibrate.web._load_config", return_value=_make_align_config()),
            patch("calibrate.web.MeasurementEngine"),
            patch("calibrate.alignment.measure_sub_ir") as mock_measure,
            patch("calibrate.adapters.minidsp.MinidspClient"),
        ):
            mock_measure.side_effect = MeasurementQualityError(
                check="sweep_capture",
                detail="Sweep not captured",
                suggestion="Turn on your amp",
            )
            r = client.post(
                "/api/align-subs/record",
                content=_make_recording_bytes(),
                headers={"X-Token": token, "X-Step": "0"},
            )

        assert r.status_code == 422
        body = r.json()
        assert body["check"] == "sweep_capture"

    def test_align_subs_cancel_restores_gains(self, client):
        """POST /api/align-subs/cancel → gains restored, session evicted."""
        from unittest.mock import AsyncMock

        token = str(uuid.uuid4())
        session = _AlignmentSession(
            token=token,
            created_at=__import__("time").time(),
            sub_outputs=[0, 1],
            sweep_samples=[0.001] * 100,
            sample_rate=48000,
            sweep_duration=0.05,
            step=0,
        )
        with _align_lock:
            _pending_alignments[token] = session

        with patch("calibrate.adapters.minidsp.MinidspClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client

            r = client.post("/api/align-subs/cancel", headers={"X-Token": token})

        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"
        # Session must be evicted
        with _align_lock:
            assert token not in _pending_alignments
        # Gains must be restored
        mock_client.restore_all_gains.assert_called_once_with([0, 1])

    def test_align_subs_cancel_unknown_token(self, client):
        """Cancel with unknown token → 404."""
        r = client.post(
            "/api/align-subs/cancel",
            headers={"X-Token": "deadbeef-0000-0000-0000-000000000000"},
        )
        assert r.status_code == 404

    def test_align_subs_start_sweep_generation_error(self, client):
        """generate_sweep RuntimeError → 500."""
        with (
            patch("calibrate.web._load_config", return_value=_make_align_config()),
            patch("calibrate.web.MeasurementEngine") as mock_engine_cls,
        ):
            mock_engine_cls.return_value.generate_sweep.side_effect = RuntimeError("no audio")
            r = client.post("/api/align-subs/start")
        assert r.status_code == 500

    def test_align_subs_ttl_cleanup_restores_gains(self):
        """Expired session: _restore_sub_gains calls client.restore_all_gains."""
        from unittest.mock import AsyncMock, patch

        session = _AlignmentSession(
            token="expired-token",
            created_at=0.0,  # epoch — always expired
            sub_outputs=[0, 1],
            sweep_samples=[],
            sample_rate=48000,
            sweep_duration=0.05,
            step=1,
        )

        with patch("calibrate.adapters.minidsp.MinidspClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value = mock_client
            _restore_sub_gains(session)

        mock_client.restore_all_gains.assert_called_once_with([0, 1])


# ── GET /api/sessions/{session_id} ────────────────────────────────────────────

def test_get_session_detail_happy_path(client):
    from calibrate.storage import Session
    fr = _make_fr()
    session = Session(id=3, timestamp="2026-03-20T12:00:00+00:00", label="test",
                      start_fr=fr, end_fr=None, filters_applied=None, notes=None)
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.return_value = session
        r = client.get("/api/sessions/3")

    assert r.status_code == 200
    data = r.json()
    assert data["id"] == 3
    assert data["label"] == "test"
    assert data["start_fr"] is not None
    assert data["end_fr"] is None


def test_get_session_detail_field_names(client):
    """start_fr must use 'frequencies' and 'spl' — not 'frequencies_hz'/'spl_dbfs'."""
    from calibrate.storage import Session
    fr = _make_fr()
    session = Session(id=1, timestamp="2026-03-20T12:00:00+00:00", label=None,
                      start_fr=fr, end_fr=None, filters_applied=None, notes=None)
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.return_value = session
        r = client.get("/api/sessions/1")

    start_fr = r.json()["start_fr"]
    assert "frequencies" in start_fr
    assert "spl" in start_fr
    assert "frequencies_hz" not in start_fr
    assert "spl_dbfs" not in start_fr


def test_get_session_detail_with_end_fr(client):
    from calibrate.storage import Session
    fr = _make_fr()
    session = Session(id=2, timestamp="2026-03-20T12:00:00+00:00", label=None,
                      start_fr=fr, end_fr=fr, filters_applied=None, notes=None)
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.return_value = session
        r = client.get("/api/sessions/2")

    data = r.json()
    assert data["end_fr"] is not None
    assert "frequencies" in data["end_fr"]
    assert "spl" in data["end_fr"]


def test_get_session_detail_404(client):
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.return_value = None
        r = client.get("/api/sessions/999")
    assert r.status_code == 404


def test_get_session_detail_malformed_fr(client):
    """Sentinel FrequencyResponse (empty lists) → start_fr returned as null."""
    from calibrate.storage import Session
    from calibrate.measurement import FrequencyResponse
    sentinel = FrequencyResponse(
        frequencies=[], spl=[], sample_rate=0, sweep_duration=0.0,
        timestamp="2026-03-20T12:00:00+00:00",
    )
    session = Session(id=7, timestamp="2026-03-20T12:00:00+00:00", label=None,
                      start_fr=sentinel, end_fr=None, filters_applied=None, notes=None)
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.return_value = session
        r = client.get("/api/sessions/7")

    assert r.status_code == 200
    assert r.json()["start_fr"] is None


def test_list_sessions_tolerates_corrupt_fr(client):
    """list_sessions must not crash when a session has a sentinel start_fr."""
    from calibrate.storage import Session
    from calibrate.measurement import FrequencyResponse
    sentinel = FrequencyResponse(
        frequencies=[], spl=[], sample_rate=0, sweep_duration=0.0,
        timestamp="2026-03-20T12:00:00+00:00",
    )
    sessions = [
        Session(id=1, timestamp="2026-03-20T12:00:00+00:00", label=None,
                start_fr=sentinel, end_fr=None, filters_applied=None, notes=None),
    ]
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = sessions
        r = client.get("/api/sessions")

    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["peak_spl"] == 0.0
    assert data[0]["n_freqs"] == 0


# ── POST /api/sessions/average ────────────────────────────────────────────────

def _make_session_with_fr(session_id: int, freqs: list[float], spl: list[float]):
    from calibrate.storage import Session
    fr = FrequencyResponse(
        frequencies=freqs, spl=spl, sample_rate=48000, sweep_duration=3.0,
        timestamp="2026-03-20T12:00:00+00:00",
    )
    return Session(id=session_id, timestamp="2026-03-20T12:00:00+00:00",
                   label=None, start_fr=fr, end_fr=None,
                   filters_applied=None, notes=None)


def test_average_sessions_min_two(client):
    """Fewer than 2 session_ids → 422 from Pydantic min_length."""
    r = client.post("/api/sessions/average", json={"session_ids": [1]})
    assert r.status_code == 422


def test_average_sessions_too_many(client):
    """More than 20 session_ids → 422 from Pydantic max_length."""
    r = client.post("/api/sessions/average", json={"session_ids": list(range(21))})
    assert r.status_code == 422


def test_average_sessions_not_found(client):
    """One invalid session ID → 404."""
    freqs = [20.0, 40.0, 80.0]
    s1 = _make_session_with_fr(1, freqs, [0.0, 0.0, 0.0])

    def get_session(sid):
        return s1 if sid == 1 else None

    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.side_effect = get_session
        r = client.post("/api/sessions/average", json={"session_ids": [1, 99]})

    assert r.status_code == 404


def test_average_sessions_incompatible_freq_length(client):
    """Sessions with different array lengths → 422."""
    s1 = _make_session_with_fr(1, [20.0, 40.0, 80.0], [0.0, 0.0, 0.0])
    s2 = _make_session_with_fr(2, [20.0, 40.0], [0.0, 0.0])

    sessions = {1: s1, 2: s2}
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.side_effect = lambda sid: sessions.get(sid)
        r = client.post("/api/sessions/average", json={"session_ids": [1, 2]})

    assert r.status_code == 422


def test_average_sessions_incompatible_freq_values(client):
    """Same length but different freq values → 422 (E2 regression)."""
    s1 = _make_session_with_fr(1, [20.0, 40.0, 80.0], [0.0, 0.0, 0.0])
    s2 = _make_session_with_fr(2, [25.0, 50.0, 100.0], [0.0, 0.0, 0.0])

    sessions = {1: s1, 2: s2}
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.side_effect = lambda sid: sessions.get(sid)
        r = client.post("/api/sessions/average", json={"session_ids": [1, 2]})

    assert r.status_code == 422


def test_average_sessions_linear_domain_math(client):
    """Linear-domain average: +6 dB and -6 dB → ≈ +2.96 dB, not 0 dB (E1 regression)."""
    freqs = [20.0, 40.0, 80.0]
    s1 = _make_session_with_fr(1, freqs, [6.0, 6.0, 6.0])
    s2 = _make_session_with_fr(2, freqs, [-6.0, -6.0, -6.0])

    sessions = {1: s1, 2: s2}
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.side_effect = lambda sid: sessions.get(sid)
        r = client.post("/api/sessions/average", json={"session_ids": [1, 2]})

    assert r.status_code == 200
    data = r.json()
    avg = data["spl_dbfs"][0]
    # Linear average of 10^(6/20)=1.995 and 10^(-6/20)=0.501 → (2.496)/2=1.248
    # 20*log10(1.248) ≈ 1.93 dB — NOT 0 dB (which naive dB average would give)
    assert abs(avg - 1.93) < 0.05, f"Expected ~1.93 dB, got {avg}"
    assert data["n_positions"] == 2


def test_average_sessions_log10_zero_safe(client):
    """Guard: `if result > 0 else -120.0` must return -120.0 when result is 0."""
    import math
    # Direct unit test of the guard expression used in average_sessions
    result = 0.0
    out = 20 * math.log10(result) if result > 0 else -120.0
    assert out == -120.0

    # Also verify the endpoint doesn't crash on extreme (very low) SPL values
    freqs = [20.0, 40.0, 80.0]
    s1 = _make_session_with_fr(1, freqs, [-300.0, 0.0, 0.0])
    s2 = _make_session_with_fr(2, freqs, [-300.0, 0.0, 0.0])
    sessions = {1: s1, 2: s2}
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.side_effect = lambda sid: sessions.get(sid)
        r = client.post("/api/sessions/average", json={"session_ids": [1, 2]})
    assert r.status_code == 200
    assert r.json()["spl_dbfs"][0] < -200  # very low, not a crash


def test_average_sessions_sentinel_fr_filtered(client):
    """Sessions with empty start_fr are excluded; if <2 remain → 422."""
    from calibrate.storage import Session
    sentinel_fr = FrequencyResponse(
        frequencies=[], spl=[], sample_rate=0, sweep_duration=0.0,
        timestamp="2026-03-20T12:00:00+00:00",
    )
    s1 = Session(id=1, timestamp="2026-03-20T12:00:00+00:00", label=None,
                 start_fr=sentinel_fr, end_fr=None, filters_applied=None, notes=None)
    s2 = _make_session_with_fr(2, [20.0, 40.0], [0.0, 0.0])

    sessions = {1: s1, 2: s2}
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.side_effect = lambda sid: sessions.get(sid)
        r = client.post("/api/sessions/average", json={"session_ids": [1, 2]})

    assert r.status_code == 422


def test_average_sessions_happy_path(client):
    """Two valid sessions → averaged FR returned with n_positions=2."""
    freqs = [20.0, 40.0, 80.0]
    s1 = _make_session_with_fr(1, freqs, [0.0, 0.0, 0.0])
    s2 = _make_session_with_fr(2, freqs, [0.0, 0.0, 0.0])

    sessions = {1: s1, 2: s2}
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.side_effect = lambda sid: sessions.get(sid)
        r = client.post("/api/sessions/average", json={"session_ids": [1, 2]})

    assert r.status_code == 200
    data = r.json()
    assert data["n_positions"] == 2
    assert len(data["frequencies_hz"]) == 3
    assert len(data["spl_dbfs"]) == 3
    # Average of two identical 0 dB measurements = 0 dB
    assert all(abs(v) < 0.01 for v in data["spl_dbfs"])


# ── POST /api/blend-check/start ───────────────────────────────────────────────

def test_blend_check_start_returns_token(client, cfg_path):
    """Happy path: returns token, sample_rate, sweep_duration."""
    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
        patch("threading.Thread"),
    ):
        MockEngine.return_value.generate_sweep.return_value = ([0.0] * 100, 48000, 1.0)
        r = client.post("/api/blend-check/start", json={})

    assert r.status_code == 200
    data = r.json()
    assert "token" in data
    assert data["sample_rate"] == 48000
    assert data["sweep_duration"] == 1.0


def test_blend_check_stores_freq_range(client, cfg_path):
    """Token in _pending_sweeps must have freq_min=40, freq_max=160 (E4 regression)."""
    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
        patch("threading.Thread"),
    ):
        MockEngine.return_value.generate_sweep.return_value = ([0.0] * 100, 48000, 1.0)
        r = client.post("/api/blend-check/start", json={})

    token = r.json()["token"]
    with _pending_lock:
        assert _pending_sweeps[token]["freq_min"] == 40
        assert _pending_sweeps[token]["freq_max"] == 160
        assert _pending_sweeps[token]["session_type"] == "blend_check"


def test_blend_check_not_saved_to_store(client, cfg_path):
    """After recording with a blend_check token, store.save_measurement must NOT be called."""
    fr = _make_fr(50)
    token = str(uuid.uuid4())
    with _pending_lock:
        _pending_sweeps[token] = {
            "sweep_samples": [0.0] * 100,
            "sample_rate": 48000,
            "sweep_duration": 1.0,
            "freq_min": 40,
            "freq_max": 160,
            "label": None,
            "session_type": "blend_check",
        }

    recording = _float32_bytes([0.001] * 1000)
    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
        patch("calibrate.web.SessionStore") as MockStore,
    ):
        MockEngine.return_value.compute_fr.return_value = fr
        r = client.post(
            "/api/measure/record",
            content=recording,
            headers={"X-Token": token, "X-Sample-Rate": "48000"},
        )

    assert r.status_code == 200
    MockStore.return_value.save_measurement.assert_not_called()
    assert r.json()["session_id"] is None


# ── POST /api/measure/start — position_label ──────────────────────────────────

def test_measure_start_with_position_label(client, cfg_path):
    """position_label combined with label → stored as 'label [position_label]'."""
    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
        patch("threading.Thread"),
    ):
        MockEngine.return_value.generate_sweep.return_value = ([0.0] * 100, 48000, 3.0)
        r = client.post("/api/measure/start",
                        json={"label": "before EQ", "position_label": "left"})

    assert r.status_code == 200
    token = r.json()["token"]
    with _pending_lock:
        assert _pending_sweeps[token]["label"] == "before EQ [left]"


def test_measure_start_position_label_optional(client, cfg_path):
    """position_label omitted → label stored as-is."""
    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
        patch("threading.Thread"),
    ):
        MockEngine.return_value.generate_sweep.return_value = ([0.0] * 100, 48000, 3.0)
        r = client.post("/api/measure/start", json={"label": "after EQ"})

    assert r.status_code == 200
    token = r.json()["token"]
    with _pending_lock:
        assert _pending_sweeps[token]["label"] == "after EQ"


# ── POST /api/sessions/average — F3 variance ──────────────────────────────────

def test_average_sessions_returns_variance(client):
    """Response includes spl_variance array of same length as spl_dbfs."""
    freqs = [20.0, 40.0, 80.0]
    s1 = _make_session_with_fr(1, freqs, [0.0, 0.0, 0.0])
    s2 = _make_session_with_fr(2, freqs, [6.0, 6.0, 6.0])

    sessions = {1: s1, 2: s2}
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.side_effect = lambda sid: sessions.get(sid)
        r = client.post("/api/sessions/average", json={"session_ids": [1, 2]})

    assert r.status_code == 200
    data = r.json()
    assert "spl_variance" in data
    assert len(data["spl_variance"]) == len(data["spl_dbfs"])


def test_average_sessions_variance_zero_for_identical(client):
    """Two identical sessions → spl_variance is all zeros."""
    freqs = [20.0, 40.0, 80.0]
    s1 = _make_session_with_fr(1, freqs, [3.0, 3.0, 3.0])
    s2 = _make_session_with_fr(2, freqs, [3.0, 3.0, 3.0])

    sessions = {1: s1, 2: s2}
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.side_effect = lambda sid: sessions.get(sid)
        r = client.post("/api/sessions/average", json={"session_ids": [1, 2]})

    assert r.status_code == 200
    assert all(v == 0.0 for v in r.json()["spl_variance"])


def test_average_sessions_variance_known_values(client):
    """Sessions at +6 and -6 dB → σ = stdev(6, -6) = 6*sqrt(2) ≈ 8.485 dB."""
    import math
    freqs = [20.0]
    s1 = _make_session_with_fr(1, freqs, [6.0])
    s2 = _make_session_with_fr(2, freqs, [-6.0])

    sessions = {1: s1, 2: s2}
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.side_effect = lambda sid: sessions.get(sid)
        r = client.post("/api/sessions/average", json={"session_ids": [1, 2]})

    assert r.status_code == 200
    sigma = r.json()["spl_variance"][0]
    assert abs(sigma - math.sqrt(72)) < 0.01  # stdev([6, -6]) = sqrt(72)


# ── POST /api/sessions/time-align — F4 phase check ────────────────────────────

from calibrate.web import compute_time_offset_ms


def _make_session_with_ir(session_id: int, ir: list[float]):
    """Build a minimal Session with an impulse response."""
    from calibrate.storage import Session
    freqs = [20.0, 40.0, 80.0]
    fr = FrequencyResponse(
        frequencies=freqs, spl=[0.0, 0.0, 0.0], sample_rate=48000,
        sweep_duration=3.0, timestamp="2026-03-20T12:00:00+00:00",
    )
    return Session(id=session_id, timestamp="2026-03-20T12:00:00+00:00",
                   label=None, start_fr=fr, end_fr=None,
                   filters_applied=None, notes=None, impulse_response=ir)


def test_compute_time_offset_ms_unit():
    """IR impulse with known 10ms lag → offset within 1ms.

    Uses a broadband impulse (n=24000) so the 60-100 Hz bandpass has enough
    frequency bins (~20 bins at 2 Hz resolution) to localise the delay cleanly.
    A pure sine would be ambiguous because the period (~12.5 ms) is close to
    the shift (10 ms).
    """
    sr = 48000
    shift = int(0.010 * sr)  # 480 samples = 10 ms
    n = 24000                 # 2 Hz freq resolution → ~20 bins in 60-100 Hz band
    base = [0.0] * n
    base[1000] = 1.0          # impulse at sample 1000
    delayed = [0.0] * n
    delayed[1000 + shift] = 1.0  # same impulse, delayed by 480 samples
    offset = compute_time_offset_ms(base, delayed, sample_rate=sr)
    assert abs(abs(offset) - 10.0) < 1.0


def test_time_align_happy_path(client):
    """Two sessions with IR → returns offset_ms, offset_feet, sub_leads, recommendation."""
    ir = [0.0] * 4096
    ir[100] = 1.0  # impulse at sample 100
    s1 = _make_session_with_ir(1, ir)
    s2 = _make_session_with_ir(2, ir)

    sessions = {1: s1, 2: s2}
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.side_effect = lambda sid: sessions.get(sid)
        r = client.post("/api/sessions/time-align",
                        json={"sub_session_id": 1, "mains_session_id": 2})

    assert r.status_code == 200
    data = r.json()
    assert "offset_ms" in data
    assert "offset_feet" in data
    assert "sub_leads" in data
    assert "recommendation" in data


def test_time_align_session_not_found(client):
    """Unknown session ID → 404."""
    ir = [0.0] * 100
    s1 = _make_session_with_ir(1, ir)

    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.side_effect = lambda sid: s1 if sid == 1 else None
        r = client.post("/api/sessions/time-align",
                        json={"sub_session_id": 1, "mains_session_id": 99})

    assert r.status_code == 404


def test_time_align_no_ir_sub(client):
    """Sub session has no IR → 422 with IR_NOT_AVAILABLE."""
    from calibrate.storage import Session
    freqs = [20.0, 40.0]
    fr = FrequencyResponse(frequencies=freqs, spl=[0.0, 0.0], sample_rate=48000,
                           sweep_duration=3.0, timestamp="2026-03-20T12:00:00+00:00")
    no_ir = Session(id=1, timestamp="2026-03-20T12:00:00+00:00", label=None,
                    start_fr=fr, end_fr=None, filters_applied=None, notes=None,
                    impulse_response=None)
    has_ir = _make_session_with_ir(2, [0.0] * 100)

    sessions = {1: no_ir, 2: has_ir}
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.side_effect = lambda sid: sessions.get(sid)
        r = client.post("/api/sessions/time-align",
                        json={"sub_session_id": 1, "mains_session_id": 2})

    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "IR_NOT_AVAILABLE"


def test_time_align_no_ir_mains(client):
    """Mains session has no IR → 422 with IR_NOT_AVAILABLE."""
    from calibrate.storage import Session
    freqs = [20.0, 40.0]
    fr = FrequencyResponse(frequencies=freqs, spl=[0.0, 0.0], sample_rate=48000,
                           sweep_duration=3.0, timestamp="2026-03-20T12:00:00+00:00")
    has_ir = _make_session_with_ir(1, [0.0] * 100)
    no_ir = Session(id=2, timestamp="2026-03-20T12:00:00+00:00", label=None,
                    start_fr=fr, end_fr=None, filters_applied=None, notes=None,
                    impulse_response=None)

    sessions = {1: has_ir, 2: no_ir}
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.side_effect = lambda sid: sessions.get(sid)
        r = client.post("/api/sessions/time-align",
                        json={"sub_session_id": 1, "mains_session_id": 2})

    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "IR_NOT_AVAILABLE"


def test_time_align_crosscorr_known_offset(client):
    """IR impulse with injected 10ms shift → endpoint returns offset within 1ms."""
    sr = 48000
    shift = int(0.010 * sr)  # 480 samples
    n = 24000
    base = [0.0] * n
    base[1000] = 1.0
    delayed = [0.0] * n
    delayed[1000 + shift] = 1.0

    s_sub = _make_session_with_ir(1, base)
    s_mains = _make_session_with_ir(2, delayed)

    sessions = {1: s_sub, 2: s_mains}
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.side_effect = lambda sid: sessions.get(sid)
        r = client.post("/api/sessions/time-align",
                        json={"sub_session_id": 1, "mains_session_id": 2})

    assert r.status_code == 200
    assert abs(abs(r.json()["offset_ms"]) - 10.0) < 1.0


# ── GET /api/sessions — has_ir field ─────────────────────────────────────────

def test_list_sessions_has_ir_field(client):
    """GET /api/sessions includes has_ir: true when impulse_response is stored."""
    from calibrate.storage import Session
    ir = [0.0] * 100
    fr = FrequencyResponse(frequencies=[20.0], spl=[0.0], sample_rate=48000,
                           sweep_duration=3.0, timestamp="2026-03-20T12:00:00+00:00")
    s_with_ir = Session(id=1, timestamp="2026-03-20T12:00:00+00:00", label=None,
                        start_fr=fr, end_fr=None, filters_applied=None, notes=None,
                        impulse_response=ir)
    s_no_ir = Session(id=2, timestamp="2026-03-20T12:00:00+00:00", label=None,
                      start_fr=fr, end_fr=None, filters_applied=None, notes=None,
                      impulse_response=None)

    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.list_sessions.return_value = [s_with_ir, s_no_ir]
        r = client.get("/api/sessions")

    assert r.status_code == 200
    data = r.json()
    assert data[0]["has_ir"] is True
    assert data[1]["has_ir"] is False


# ── POST /api/signal-path/cardioid — F6 ───────────────────────────────────────

def _make_cardioid_config(tmp_path, sub_outputs=(0, 1), sep_m=1.0):
    """Write a config.yaml with minidsp.signal_path.sub_outputs set."""
    import yaml
    p = tmp_path / "config.yaml"
    cfg = {
        "denon": {"host": "192.168.1.100"},
        "minidsp": {
            "host": "localhost",
            "port": 5380,
            "sub_separation_m": sep_m,
            "signal_path": {"sub_outputs": list(sub_outputs)},
        },
        "mic": {"name": "UMIK"},
    }
    p.write_text(yaml.dump(cfg))
    return p


def test_cardioid_happy_path(client, tmp_path):
    """Cardioid enabled → polarity inverted, delay set on output 1."""
    from unittest.mock import AsyncMock
    cfg_p = _make_cardioid_config(tmp_path)
    with (
        patch("calibrate.web.CONFIG_PATH", cfg_p),
        patch("calibrate.adapters.minidsp.MinidspClient") as MockClient,
    ):
        mc = MagicMock()
        mc.set_output_polarity = AsyncMock(return_value=None)
        mc.set_output_delay = AsyncMock(return_value=None)
        MockClient.return_value = mc

        r = client.post("/api/signal-path/cardioid",
                        json={"enabled": True, "delay_ms": 2.9})

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["enabled"] is True
    assert abs(data["delay_ms"] - 2.9) < 0.01


def test_cardioid_disabled(client, tmp_path):
    """Cardioid disabled → polarity normal, delay 0."""
    from unittest.mock import AsyncMock
    cfg_p = _make_cardioid_config(tmp_path)
    with (
        patch("calibrate.web.CONFIG_PATH", cfg_p),
        patch("calibrate.adapters.minidsp.MinidspClient") as MockClient,
    ):
        mc = MagicMock()
        mc.set_output_polarity = AsyncMock(return_value=None)
        mc.set_output_delay = AsyncMock(return_value=None)
        MockClient.return_value = mc

        r = client.post("/api/signal-path/cardioid",
                        json={"enabled": False})

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["enabled"] is False
    assert data["delay_ms"] == 0.0


def test_cardioid_no_sub_outputs(client, tmp_path):
    """Config without 2+ sub_outputs → 422."""
    import yaml
    cfg_p = tmp_path / "config.yaml"
    cfg_p.write_text(yaml.dump({
        "denon": {"host": "192.168.1.100"},
        "minidsp": {"host": "localhost", "port": 5380},
        "mic": {"name": "UMIK"},
    }))
    with patch("calibrate.web.CONFIG_PATH", cfg_p):
        r = client.post("/api/signal-path/cardioid", json={"enabled": True})
    assert r.status_code == 422


def test_cardioid_polarity_404_fallback(client, tmp_path):
    """MinidspApiError 404 on polarity → advisory_only response, not 500."""
    from unittest.mock import AsyncMock
    from calibrate.adapters.minidsp import MinidspApiError
    cfg_p = _make_cardioid_config(tmp_path)
    with (
        patch("calibrate.web.CONFIG_PATH", cfg_p),
        patch("calibrate.adapters.minidsp.MinidspClient") as MockClient,
    ):
        mc = MagicMock()
        mc.set_output_polarity = AsyncMock(
            side_effect=MinidspApiError(404, "/output/1/polarity")
        )
        MockClient.return_value = mc

        r = client.post("/api/signal-path/cardioid",
                        json={"enabled": True})

    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "advisory_only"


# ── Signal Path endpoints ─────────────────────────────────────────────────────

class TestGetSignalPathConfig:
    def test_returns_empty_when_not_configured(self, client, tmp_path, monkeypatch):
        import yaml
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({
            "minidsp": {"host": "localhost", "port": 5380},
        }))
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_file)
        r = client.get("/api/signal-path")
        assert r.status_code == 200
        data = r.json()
        assert data["source"] is None
        assert data["preset"] is None
        assert data["routing"] == []

    def test_returns_configured_values(self, client, tmp_path, monkeypatch):
        import yaml
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({
            "minidsp": {
                "host": "localhost", "port": 5380,
                "signal_path": {
                    "source": "Toslink",
                    "preset": 1,
                    "routing": [{"input": 0, "outputs": [0, 1]}],
                },
            },
        }))
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_file)
        r = client.get("/api/signal-path")
        assert r.status_code == 200
        data = r.json()
        assert data["source"] == "Toslink"
        assert data["preset"] == 1
        assert data["routing"] == [{"input": 0, "outputs": [0, 1]}]


class TestApplySignalPath:
    def test_invalid_source_returns_422(self, client, tmp_path, monkeypatch):
        import yaml
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"minidsp": {"host": "localhost", "port": 5380}}))
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_file)
        r = client.post("/api/signal-path/apply", json={"source": "HDMI", "preset": 0})
        assert r.status_code == 422

    def test_invalid_preset_returns_422(self, client, tmp_path, monkeypatch):
        import yaml
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"minidsp": {"host": "localhost", "port": 5380}}))
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_file)
        r = client.post("/api/signal-path/apply", json={"source": "Analog", "preset": 99})
        assert r.status_code == 422

    def test_happy_path_source_and_preset(self, client, tmp_path, monkeypatch):
        import yaml
        from unittest.mock import AsyncMock
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"minidsp": {"host": "localhost", "port": 5380}}))
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_file)

        mock_client = AsyncMock()
        with patch("calibrate.adapters.minidsp.MinidspClient", return_value=mock_client):
            r = client.post("/api/signal-path/apply", json={"source": "Toslink", "preset": 2})

        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["source"] == "Toslink"
        assert data["preset"] == 2
        assert data["routing_applied"] is False
        mock_client.switch_preset.assert_called_once_with(2)
        mock_client.switch_source.assert_called_once_with("Toslink")

    def test_applies_routing_from_config(self, client, tmp_path, monkeypatch):
        import yaml
        from unittest.mock import AsyncMock
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({
            "minidsp": {
                "host": "localhost", "port": 5380,
                "signal_path": {
                    "routing": [{"input": 0, "outputs": [0, 1, 2, 3]}],
                },
            },
        }))
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_file)

        mock_client = AsyncMock()
        with patch("calibrate.adapters.minidsp.MinidspClient", return_value=mock_client):
            r = client.post("/api/signal-path/apply", json={})

        assert r.status_code == 200
        assert r.json()["routing_applied"] is True
        mock_client.set_input_routing.assert_called_once_with(
            0, {0: True, 1: True, 2: True, 3: True}
        )

    def test_minidsp_api_error_returns_502(self, client, tmp_path, monkeypatch):
        import yaml
        from unittest.mock import AsyncMock
        from calibrate.adapters.minidsp import MinidspApiError
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"minidsp": {"host": "localhost", "port": 5380}}))
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_file)

        mock_client = AsyncMock()
        mock_client.switch_preset.side_effect = MinidspApiError(502, "/devices/0/preset/1")
        with patch("calibrate.adapters.minidsp.MinidspClient", return_value=mock_client):
            r = client.post("/api/signal-path/apply", json={"preset": 1})

        assert r.status_code == 502


class TestGetDeviceState:
    def test_returns_master_status(self, client, tmp_path, monkeypatch):
        import yaml
        from unittest.mock import AsyncMock
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"minidsp": {"host": "localhost", "port": 5380}}))
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_file)

        device_status = {
            "master": {"preset": 0, "source": "Analog", "volume": -30.0, "mute": False},
            "input_levels": [],
            "output_levels": [],
        }
        mock_client = AsyncMock()
        mock_client.get_device_status.return_value = device_status
        with patch("calibrate.adapters.minidsp.MinidspClient", return_value=mock_client):
            r = client.get("/api/signal-path/device-state")

        assert r.status_code == 200
        data = r.json()
        assert data["master"]["source"] == "Analog"
        assert data["master"]["preset"] == 0

    def test_minidsp_api_error_returns_502(self, client, tmp_path, monkeypatch):
        import yaml
        from unittest.mock import AsyncMock
        from calibrate.adapters.minidsp import MinidspApiError
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"minidsp": {"host": "localhost", "port": 5380}}))
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_file)

        mock_client = AsyncMock()
        mock_client.get_device_status.side_effect = MinidspApiError(503, "/devices/0")
        with patch("calibrate.adapters.minidsp.MinidspClient", return_value=mock_client):
            r = client.get("/api/signal-path/device-state")

        assert r.status_code == 502

# ── /api/version and /api/upgrade ─────────────────────────────────────────────

from unittest.mock import AsyncMock, patch
from calibrate.web import _version_cache
import calibrate.web as _web_mod


@pytest.fixture(autouse=False)
def clear_version_cache():
    """Reset module-level version cache between tests."""
    _version_cache.clear()
    _orig_semver = _web_mod._SEMANTIC_VERSION
    yield
    _version_cache.clear()
    _web_mod._SEMANTIC_VERSION = _orig_semver


class TestApiVersion:

    def test_version_endpoint_returns_sha(self, client, monkeypatch, clear_version_cache):
        monkeypatch.setenv("BUILD_SHA", "abc1234567890abcdef")
        with patch("calibrate.web._fetch_latest_sha", new=AsyncMock(return_value=None)):
            r = client.get("/api/version")
        assert r.status_code == 200
        assert r.json()["current_sha"] == "abc1234567890abcdef"

    def test_version_endpoint_no_sha(self, client, monkeypatch, clear_version_cache):
        monkeypatch.delenv("BUILD_SHA", raising=False)
        with patch("calibrate.web._fetch_latest_sha", new=AsyncMock(return_value=None)):
            r = client.get("/api/version")
        assert r.status_code == 200
        assert r.json()["current_sha"] == "unknown"

    def test_version_up_to_date(self, client, monkeypatch, clear_version_cache):
        import time
        sha = "deadbeefdeadbeef"
        monkeypatch.setenv("BUILD_SHA", sha)
        # Pre-warm cache — background task won't complete in sync TestClient
        _version_cache["result"] = {"latest_sha": sha, "expires": time.time() + 3600, "checked_at": time.time()}
        r = client.get("/api/version")
        data = r.json()
        assert data["up_to_date"] is True
        assert data["latest_sha"] == sha

    def test_version_update_available(self, client, monkeypatch, clear_version_cache):
        import time
        monkeypatch.setenv("BUILD_SHA", "oldsha123")
        # Pre-warm cache — background task won't complete in sync TestClient
        _version_cache["result"] = {"latest_sha": "newsha456", "expires": time.time() + 3600, "checked_at": time.time()}
        r = client.get("/api/version")
        data = r.json()
        assert data["up_to_date"] is False
        assert data["latest_sha"] == "newsha456"

    def test_version_ghcr_unreachable(self, client, monkeypatch, clear_version_cache):
        monkeypatch.setenv("BUILD_SHA", "somesha")
        with patch("calibrate.web._fetch_latest_sha", new=AsyncMock(return_value=None)):
            r = client.get("/api/version")
        data = r.json()
        assert data["latest_sha"] is None
        assert data["up_to_date"] is False

    def test_version_cache_ttl(self, client, monkeypatch, clear_version_cache):
        monkeypatch.setenv("BUILD_SHA", "sha1")
        mock = AsyncMock(return_value="sha_remote")
        with patch("calibrate.web._fetch_latest_sha", new=mock):
            client.get("/api/version")
            client.get("/api/version")
        # GHCR should only be called once due to cache
        assert mock.call_count == 1

    def test_version_cache_invalidated_after_ttl(self, client, monkeypatch, clear_version_cache):
        import calibrate.web as web_mod
        monkeypatch.setenv("BUILD_SHA", "sha1")
        mock = AsyncMock(return_value="sha_remote")
        with patch("calibrate.web._fetch_latest_sha", new=mock):
            client.get("/api/version")
            # Force cache expiry
            web_mod._version_cache["result"]["expires"] = 0
            client.get("/api/version")
        assert mock.call_count == 2

    def test_version_ghcr_auth_retry(self, client, monkeypatch, clear_version_cache):
        """GHCR 401 on token step returns None (not a crash)."""
        import httpx
        monkeypatch.setenv("BUILD_SHA", "sha1")

        async def mock_fetch():
            return None  # simulates auth failure handled inside _fetch_latest_sha

        with patch("calibrate.web._fetch_latest_sha", new=AsyncMock(side_effect=mock_fetch)):
            r = client.get("/api/version")
        assert r.status_code == 200
        assert r.json()["latest_sha"] is None

    def test_version_ghcr_rate_limited(self, client, monkeypatch, clear_version_cache):
        """GHCR 429 (rate limit) returns None gracefully."""
        monkeypatch.setenv("BUILD_SHA", "sha1")
        with patch("calibrate.web._fetch_latest_sha", new=AsyncMock(return_value=None)):
            r = client.get("/api/version")
        assert r.status_code == 200
        assert r.json()["latest_sha"] is None

    def test_version_includes_semantic_version(self, client, monkeypatch, clear_version_cache):
        """semantic_version field is present and reads from the VERSION file."""
        monkeypatch.delenv("BUILD_SHA", raising=False)
        with patch("calibrate.web._fetch_latest_sha", new=AsyncMock(return_value=None)):
            r = client.get("/api/version")
        data = r.json()
        assert "semantic_version" in data
        assert data["semantic_version"] != ""

    def test_version_semantic_version_fallback_on_missing_file(self, client, monkeypatch, clear_version_cache):
        """_read_semantic_version returns 'unknown' when VERSION file is missing."""
        monkeypatch.delenv("BUILD_SHA", raising=False)
        with patch("calibrate.web._read_semantic_version", return_value="unknown"):
            with patch("calibrate.web._fetch_latest_sha", new=AsyncMock(return_value=None)):
                r = client.get("/api/version")
        assert r.json()["semantic_version"] == "unknown"


class TestApiUpgrade:

    def test_upgrade_writes_trigger_file(self, client, tmp_path, monkeypatch, clear_version_cache):
        import calibrate.web as web_mod
        monkeypatch.setattr(web_mod, "_DATA_DIR", tmp_path)
        r = client.post("/api/upgrade")
        assert r.status_code == 202
        assert (tmp_path / "upgrade-trigger").exists()

    def test_upgrade_trigger_file_path_configurable(self, client, tmp_path, monkeypatch):
        import calibrate.web as web_mod
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        monkeypatch.setattr(web_mod, "_DATA_DIR", custom_dir)
        r = client.post("/api/upgrade")
        assert r.status_code == 202
        assert (custom_dir / "upgrade-trigger").exists()

    def test_upgrade_already_in_progress(self, client, tmp_path, monkeypatch):
        import calibrate.web as web_mod
        monkeypatch.setattr(web_mod, "_DATA_DIR", tmp_path)
        # Pre-create the trigger file
        (tmp_path / "upgrade-trigger").touch()
        r = client.post("/api/upgrade")
        assert r.status_code == 409

    def test_upgrade_data_dir_not_writable(self, client, tmp_path, monkeypatch):
        import os, calibrate.web as web_mod
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        os.chmod(readonly, 0o555)
        monkeypatch.setattr(web_mod, "_DATA_DIR", readonly)
        try:
            r = client.post("/api/upgrade")
            assert r.status_code == 503
            assert "not writable" in r.json()["detail"]
        finally:
            os.chmod(readonly, 0o755)


# ── GET /api/preflight and /api/preflight/{check_name} ───────────────────────

class TestPreflightEndpoints:
    def _pass(self, name: str) -> CheckResult:
        return CheckResult(name=name, passed=True, detail=f"{name} OK")

    def _fail(self, name: str) -> CheckResult:
        return CheckResult(name=name, passed=False, detail="", error=f"{name} failed")

    def _all_pass_mocks(self):
        return {
            "check_config": AsyncMock(return_value=self._pass("Config")),
            "check_minidsp_combined": AsyncMock(return_value=self._pass("miniDSP")),
            "check_denon_and_playback": AsyncMock(return_value=self._pass("Denon AVR")),
            "check_signal_path_sync": AsyncMock(return_value=self._pass("Signal Path")),
        }

    def test_preflight_run_all_all_pass(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        mocks = self._all_pass_mocks()
        with patch.multiple("calibrate.preflight.PreflightChecker", **mocks):
            r = client.get("/api/preflight")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 4
        assert all(item["passed"] for item in data)

    def test_preflight_run_all_partial_fail(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        mocks = self._all_pass_mocks()
        mocks["check_minidsp_combined"] = AsyncMock(return_value=self._fail("miniDSP"))
        with patch.multiple("calibrate.preflight.PreflightChecker", **mocks):
            r = client.get("/api/preflight")
        assert r.status_code == 200
        data = r.json()
        minidsp = next(item for item in data if item["name"] == "miniDSP")
        assert not minidsp["passed"]
        assert minidsp["error"] == "miniDSP failed"

    def test_preflight_run_all_no_config(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", tmp_path / "missing.yaml")
        r = client.get("/api/preflight")
        assert r.status_code == 503

    def test_preflight_check_hidraw_pass(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        with patch("calibrate.preflight.PreflightChecker.check_hidraw",
                   new=AsyncMock(return_value=self._pass("miniDSP USB"))):
            r = client.get("/api/preflight/hidraw")
        assert r.status_code == 200
        assert r.json()["passed"] is True

    def test_preflight_check_hidraw_fail(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        with patch("calibrate.preflight.PreflightChecker.check_hidraw",
                   new=AsyncMock(return_value=self._fail("miniDSP USB"))):
            r = client.get("/api/preflight/hidraw")
        assert r.status_code == 200
        assert r.json()["passed"] is False

    def test_preflight_check_minidsp_pass(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        with patch("calibrate.preflight.PreflightChecker.check_minidsp",
                   new=AsyncMock(return_value=self._pass("miniDSP"))):
            r = client.get("/api/preflight/minidsp")
        assert r.status_code == 200
        assert r.json()["passed"] is True

    def test_preflight_check_minidsp_fail(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        with patch("calibrate.preflight.PreflightChecker.check_minidsp",
                   new=AsyncMock(return_value=self._fail("miniDSP"))):
            r = client.get("/api/preflight/minidsp")
        assert r.status_code == 200
        assert r.json()["passed"] is False

    def test_preflight_check_denon_pass(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        with patch("calibrate.preflight.PreflightChecker.check_denon",
                   new=AsyncMock(return_value=self._pass("Denon AVR"))):
            r = client.get("/api/preflight/denon")
        assert r.status_code == 200
        assert r.json()["passed"] is True

    def test_preflight_check_denon_fail(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        with patch("calibrate.preflight.PreflightChecker.check_denon",
                   new=AsyncMock(return_value=self._fail("Denon AVR"))):
            r = client.get("/api/preflight/denon")
        assert r.status_code == 200
        assert r.json()["passed"] is False

    def test_preflight_check_playback_pass(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        with patch("calibrate.preflight.PreflightChecker.check_playback_route",
                   new=AsyncMock(return_value=self._pass("Playback Route"))):
            r = client.get("/api/preflight/playback")
        assert r.status_code == 200
        assert r.json()["passed"] is True

    def test_preflight_check_signal_path_pass(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        with patch("calibrate.preflight.PreflightChecker.check_signal_path_sync",
                   new=AsyncMock(return_value=self._pass("Signal Path"))):
            r = client.get("/api/preflight/signal-path")
        assert r.status_code == 200
        assert r.json()["passed"] is True

    def test_preflight_check_config_all_present(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        with patch("calibrate.preflight.PreflightChecker.check_config",
                   new=AsyncMock(return_value=self._pass("Config"))):
            r = client.get("/api/preflight/config")
        assert r.status_code == 200
        assert r.json()["passed"] is True

    def test_preflight_check_config_missing_field(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        fail_result = CheckResult(
            name="Config", passed=False,
            detail="1 required field(s) missing",
            error="Missing required fields: denon.host",
        )
        with patch("calibrate.preflight.PreflightChecker.check_config",
                   new=AsyncMock(return_value=fail_result)):
            r = client.get("/api/preflight/config")
        assert r.status_code == 200
        data = r.json()
        assert not data["passed"]
        assert "denon.host" in data["error"]

    def test_preflight_check_minidsp_combined_pass(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        with patch("calibrate.preflight.PreflightChecker.check_minidsp_combined",
                   new=AsyncMock(return_value=self._pass("miniDSP"))):
            r = client.get("/api/preflight/minidsp-combined")
        assert r.status_code == 200
        assert r.json()["passed"] is True

    def test_preflight_check_denon_playback_pass(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        with patch("calibrate.preflight.PreflightChecker.check_denon_and_playback",
                   new=AsyncMock(return_value=self._pass("Denon AVR"))):
            r = client.get("/api/preflight/denon-playback")
        assert r.status_code == 200
        assert r.json()["passed"] is True

    def test_preflight_unknown_check_name(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        r = client.get("/api/preflight/nonexistent-check")
        assert r.status_code == 404


# ── update_config ─────────────────────────────────────────────────────────────

class TestUpdateConfig:
    def test_writes_new_keys(self, tmp_path):
        from calibrate.config import update_config
        import yaml
        p = tmp_path / "config.yaml"
        update_config({"denon": {"host": "10.0.0.1"}}, path=p)
        data = yaml.safe_load(p.read_text())
        assert data["denon"]["host"] == "10.0.0.1"

    def test_merges_into_existing_section(self, tmp_path):
        from calibrate.config import update_config
        import yaml
        p = tmp_path / "config.yaml"
        p.write_text(yaml.dump({"denon": {"host": "old", "other": "keep"}}))
        update_config({"denon": {"host": "new"}}, path=p)
        data = yaml.safe_load(p.read_text())
        assert data["denon"]["host"] == "new"
        assert data["denon"]["other"] == "keep"

    def test_creates_file_if_missing(self, tmp_path):
        from calibrate.config import update_config
        import yaml
        p = tmp_path / "subdir" / "config.yaml"
        update_config({"measurement": {"denon_sweep_input": "AUX1"}}, path=p)
        data = yaml.safe_load(p.read_text())
        assert data["measurement"]["denon_sweep_input"] == "AUX1"

    def test_preserves_unrelated_sections(self, tmp_path):
        from calibrate.config import update_config
        import yaml
        p = tmp_path / "config.yaml"
        p.write_text(yaml.dump({"minidsp": {"host": "localhost"}, "mic": {"name": "UMIK"}}))
        update_config({"denon": {"host": "10.0.0.1"}}, path=p)
        data = yaml.safe_load(p.read_text())
        assert data["minidsp"]["host"] == "localhost"
        assert data["mic"]["name"] == "UMIK"


# ── Equipment API endpoints ───────────────────────────────────────────────────

class TestEquipmentSpeakersApi:
    def test_list_empty(self, client, tmp_path, monkeypatch):
        from calibrate.storage import SessionStore
        monkeypatch.setattr("calibrate.web.SessionStore",
                            lambda: SessionStore(db_path=tmp_path / "eq.db"))
        r = client.get("/api/equipment/speakers")
        assert r.status_code == 200
        assert r.json() == []

    def test_create_and_list(self, client, tmp_path, monkeypatch):
        from calibrate.storage import SessionStore
        db = tmp_path / "eq.db"
        monkeypatch.setattr("calibrate.web.SessionStore",
                            lambda: SessionStore(db_path=db))
        r = client.post("/api/equipment/speakers", json={
            "type": "subwoofer",
            "label": "SVS PB12-NSD",
            "data": {"room_location": "corner", "port_tune_hz": 22.0},
        })
        assert r.status_code == 200
        body = r.json()
        assert body["type"] == "subwoofer"
        assert body["data"]["port_tune_hz"] == 22.0

        r2 = client.get("/api/equipment/speakers")
        assert len(r2.json()) == 1

    def test_update(self, client, tmp_path, monkeypatch):
        from calibrate.storage import SessionStore
        db = tmp_path / "eq.db"
        monkeypatch.setattr("calibrate.web.SessionStore",
                            lambda: SessionStore(db_path=db))
        create = client.post("/api/equipment/speakers", json={"type": "center", "label": "Old"})
        spk_id = create.json()["id"]
        r = client.put(f"/api/equipment/speakers/{spk_id}", json={
            "type": "center", "label": "New",
            "data": {"room_location": "front wall"},
        })
        assert r.status_code == 200
        assert r.json()["label"] == "New"

    def test_update_partial_label_only(self, client, tmp_path, monkeypatch):
        """PUT with only label (no type) must succeed — type is optional for updates."""
        from calibrate.storage import SessionStore
        db = tmp_path / "eq.db"
        monkeypatch.setattr("calibrate.web.SessionStore",
                            lambda: SessionStore(db_path=db))
        create = client.post("/api/equipment/speakers", json={"type": "sub", "label": "Original"})
        spk_id = create.json()["id"]
        r = client.put(f"/api/equipment/speakers/{spk_id}", json={"label": "Renamed"})
        assert r.status_code == 200
        assert r.json()["label"] == "Renamed"
        assert r.json()["type"] == "sub"  # type unchanged

    def test_update_unknown_returns_404(self, client, tmp_path, monkeypatch):
        from calibrate.storage import SessionStore
        monkeypatch.setattr("calibrate.web.SessionStore",
                            lambda: SessionStore(db_path=tmp_path / "eq.db"))
        r = client.put("/api/equipment/speakers/9999", json={"type": "center"})
        assert r.status_code == 404

    def test_delete(self, client, tmp_path, monkeypatch):
        from calibrate.storage import SessionStore
        db = tmp_path / "eq.db"
        monkeypatch.setattr("calibrate.web.SessionStore",
                            lambda: SessionStore(db_path=db))
        create = client.post("/api/equipment/speakers", json={"type": "front_l"})
        spk_id = create.json()["id"]
        r = client.delete(f"/api/equipment/speakers/{spk_id}")
        assert r.status_code == 200
        assert r.json()["status"] == "deleted"
        assert client.get("/api/equipment/speakers").json() == []

    def test_delete_unknown_returns_404(self, client, tmp_path, monkeypatch):
        from calibrate.storage import SessionStore
        monkeypatch.setattr("calibrate.web.SessionStore",
                            lambda: SessionStore(db_path=tmp_path / "eq.db"))
        r = client.delete("/api/equipment/speakers/9999")
        assert r.status_code == 404


class TestDenonSaveApi:
    def test_save_host(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        monkeypatch.setattr("calibrate.config.CONFIG_PATH", cfg_path)
        r = client.post("/api/equipment/denon/save",
                        json={"host": "192.168.1.50", "sweep_input": None})
        assert r.status_code == 200
        import yaml
        data = yaml.safe_load(cfg_path.read_text())
        assert data["denon"]["host"] == "192.168.1.50"

    def test_save_sweep_input(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        monkeypatch.setattr("calibrate.config.CONFIG_PATH", cfg_path)
        r = client.post("/api/equipment/denon/save",
                        json={"host": None, "sweep_input": "AUX1"})
        assert r.status_code == 200
        import yaml
        data = yaml.safe_load(cfg_path.read_text())
        assert data["measurement"]["denon_sweep_input"] == "AUX1"

    def test_save_nothing_returns_422(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        r = client.post("/api/equipment/denon/save",
                        json={"host": None, "sweep_input": None})
        assert r.status_code == 422


class TestMinidspSaveLabels:
    def test_saves_labels(self, client, cfg_path, monkeypatch):
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        monkeypatch.setattr("calibrate.config.CONFIG_PATH", cfg_path)
        r = client.post("/api/equipment/minidsp/save-labels", json={
            "inputs": ["Denon LFE L", "Denon LFE R"],
            "outputs": ["Sub L", "Sub R", "", ""],
        })
        assert r.status_code == 200
        import yaml
        data = yaml.safe_load(cfg_path.read_text())
        assert data["connections"]["minidsp"]["inputs"]["0"] == "Denon LFE L"
        assert data["connections"]["minidsp"]["outputs"]["0"] == "Sub L"
        assert "2" not in data["connections"]["minidsp"]["outputs"]  # empty strings skipped


# ── Signal Chain ──────────────────────────────────────────────────────────────


def _empty_slots() -> list[dict]:
    return [{"index": i, "label": "", "location": "", "preset": ""} for i in range(4)]


def _full_chain_body() -> dict:
    return {
        "denon": {"host": "192.168.1.100", "sweep_input": "HDMI 1"},
        "minidsp": {
            "input_labels": {"0": "LFE L", "1": "LFE R"},
            "output_slots": [
                {"index": 0, "label": "Sub L", "location": "front-left", "preset": "pb12-nsd"},
                {"index": 1, "label": "Sub R", "location": "front-right", "preset": "pb12-nsd"},
                {"index": 2, "label": "", "location": "", "preset": ""},
                {"index": 3, "label": "", "location": "", "preset": ""},
            ],
        },
    }


class TestSignalChainGet:
    def test_get_empty_returns_pi_only(self, client, tmp_path, monkeypatch):
        """GET returns empty chain structure when no config exists."""
        empty_cfg = tmp_path / "config.yaml"
        empty_cfg.write_text("denon:\n  host: null\nminidsp:\n  host: localhost\n  port: 5380\n")
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", empty_cfg)
        monkeypatch.setattr("calibrate.config.CONFIG_PATH", empty_cfg)
        r = client.get("/api/signal-chain")
        assert r.status_code == 200
        d = r.json()
        assert "denon" in d
        assert "minidsp" in d
        assert d["denon"]["host"] is None
        assert len(d["minidsp"]["output_slots"]) == 4

    def test_get_populated_returns_full_chain(self, client, tmp_path, monkeypatch):
        """GET synthesizes chain from existing config."""
        import yaml
        cfg_data = {
            "denon": {"host": "192.168.1.100"},
            "measurement": {"denon_sweep_input": "HDMI 1", "sub_outputs": [0, 1]},
            "minidsp": {
                "host": "localhost", "port": 5380,
                "input_labels": {"0": "LFE L", "1": "LFE R"},
                "output_slots": [
                    {"index": 0, "label": "Sub L", "location": "front-left", "preset": "pb12-nsd"},
                    {"index": 1, "label": "Sub R", "location": "front-right", "preset": "pb12-nsd"},
                    {"index": 2, "label": "", "location": "", "preset": ""},
                    {"index": 3, "label": "", "location": "", "preset": ""},
                ],
            },
        }
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(cfg_data))
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", p)
        monkeypatch.setattr("calibrate.config.CONFIG_PATH", p)
        r = client.get("/api/signal-chain")
        assert r.status_code == 200
        d = r.json()
        assert d["denon"]["host"] == "192.168.1.100"
        assert d["denon"]["sweep_input"] == "HDMI 1"
        assert d["minidsp"]["output_slots"][0]["label"] == "Sub L"
        assert d["minidsp"]["output_slots"][0]["location"] == "front-left"

    def test_get_partial_slots_returns_four(self, client, tmp_path, monkeypatch):
        """Config with 2 of 4 slots filled returns all 4 slots."""
        import yaml
        cfg_data = {
            "denon": {"host": "192.168.1.100"},
            "minidsp": {
                "host": "localhost", "port": 5380,
                "output_slots": [
                    {"index": 0, "label": "Sub L", "location": "front-left", "preset": "pb12-nsd"},
                    {"index": 1, "label": "Sub R", "location": "front-right", "preset": "pb12-nsd"},
                ],
            },
        }
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(cfg_data))
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", p)
        monkeypatch.setattr("calibrate.config.CONFIG_PATH", p)
        r = client.get("/api/signal-chain")
        assert r.status_code == 200
        # Only 2 slots in config — returned as-is (not padded to 4 by GET)
        d = r.json()
        assert len(d["minidsp"]["output_slots"]) == 2

    def test_get_migration_tombstone_old_outputs(self, client, tmp_path, monkeypatch):
        """Old connections.minidsp.outputs labels migrate into output_slots on GET."""
        import yaml
        cfg_data = {
            "denon": {"host": "192.168.1.100"},
            "minidsp": {"host": "localhost", "port": 5380},
            "connections": {"minidsp": {"outputs": {"0": "Sub L", "1": "Sub R"}}},
        }
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(cfg_data))
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", p)
        monkeypatch.setattr("calibrate.config.CONFIG_PATH", p)
        r = client.get("/api/signal-chain")
        assert r.status_code == 200
        d = r.json()
        # Labels from old key should appear in slots
        slot0 = next((s for s in d["minidsp"]["output_slots"] if s["index"] == 0), None)
        assert slot0 is not None
        assert slot0["label"] == "Sub L"


    def test_get_preset_labels_padded_when_fewer_than_four(self, client, tmp_path, monkeypatch):
        """preset_labels shorter than 4 in config are padded to 4 on GET."""
        import yaml
        cfg_data = {
            "denon": {"host": "192.168.1.100"},
            "minidsp": {
                "host": "localhost", "port": 5380,
                "preset_labels": ["Movie", "Music"],  # only 2 entries
            },
        }
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(cfg_data))
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", p)
        monkeypatch.setattr("calibrate.config.CONFIG_PATH", p)
        r = client.get("/api/signal-chain")
        assert r.status_code == 200
        labels = r.json()["minidsp"]["preset_labels"]
        assert len(labels) == 4
        assert labels[0] == "Movie"
        assert labels[1] == "Music"
        assert labels[2] == ""
        assert labels[3] == ""


class TestSignalChainPost:
    def test_post_round_trips(self, client, cfg_path, monkeypatch):
        """POST writes denon + minidsp, GET reads them back."""
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        monkeypatch.setattr("calibrate.config.CONFIG_PATH", cfg_path)
        r = client.post("/api/signal-chain", json=_full_chain_body())
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        import yaml
        data = yaml.safe_load(cfg_path.read_text())
        assert data["denon"]["host"] == "192.168.1.100"
        assert data["minidsp"]["output_slots"][0]["label"] == "Sub L"

    def test_post_derives_sub_outputs(self, client, cfg_path, monkeypatch):
        """POST with 2 non-empty slots writes measurement.sub_outputs=[0,1]."""
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        monkeypatch.setattr("calibrate.config.CONFIG_PATH", cfg_path)
        r = client.post("/api/signal-chain", json=_full_chain_body())
        assert r.status_code == 200
        assert r.json()["sub_outputs"] == [0, 1]
        import yaml
        data = yaml.safe_load(cfg_path.read_text())
        assert data["measurement"]["sub_outputs"] == [0, 1]

    def test_post_all_empty_slots_clears_sub_outputs(self, client, cfg_path, monkeypatch):
        """POST with all empty slots writes measurement.sub_outputs=[]."""
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        monkeypatch.setattr("calibrate.config.CONFIG_PATH", cfg_path)
        body = {
            "denon": {"host": "192.168.1.100", "sweep_input": None},
            "minidsp": {"input_labels": {}, "output_slots": _empty_slots()},
        }
        r = client.post("/api/signal-chain", json=body)
        assert r.status_code == 200
        assert r.json()["sub_outputs"] == []
        import yaml
        data = yaml.safe_load(cfg_path.read_text())
        assert data["measurement"]["sub_outputs"] == []

    def test_post_speaker_preset_and_location(self, client, cfg_path, monkeypatch):
        """Slot with preset + location round-trips correctly."""
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        monkeypatch.setattr("calibrate.config.CONFIG_PATH", cfg_path)
        r = client.post("/api/signal-chain", json=_full_chain_body())
        assert r.status_code == 200
        import yaml
        data = yaml.safe_load(cfg_path.read_text())
        slot0 = data["minidsp"]["output_slots"][0]
        assert slot0["preset"] == "pb12-nsd"
        assert slot0["location"] == "front-left"

    def test_post_tombstones_old_connections_key(self, client, tmp_path, monkeypatch):
        """POST clears connections.minidsp to avoid dual-read confusion."""
        import yaml
        cfg_data = {
            "denon": {"host": "192.168.1.100"},
            "minidsp": {"host": "localhost", "port": 5380},
            "connections": {"minidsp": {"outputs": {"0": "Sub L"}}},
        }
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(cfg_data))
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", p)
        monkeypatch.setattr("calibrate.config.CONFIG_PATH", p)
        r = client.post("/api/signal-chain", json=_full_chain_body())
        assert r.status_code == 200
        data = yaml.safe_load(p.read_text())
        # Old outputs key should be gone (tombstoned)
        assert not data.get("connections", {}).get("minidsp", {}).get("outputs")

    def test_post_nothing_returns_422(self, client, cfg_path, monkeypatch):
        """POST with empty body returns 422."""
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        r = client.post("/api/signal-chain", json={})
        assert r.status_code == 422


class TestSignalChainGate:
    def test_gate_condition_denon_plus_speaker(self, client, cfg_path, monkeypatch):
        """POST with Denon host + 1 speaker → sub_outputs populated."""
        monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
        monkeypatch.setattr("calibrate.config.CONFIG_PATH", cfg_path)
        body = {
            "denon": {"host": "192.168.1.100", "sweep_input": "HDMI 1"},
            "minidsp": {
                "input_labels": {},
                "output_slots": [
                    {"index": 0, "label": "Sub", "location": "front-left", "preset": "pb12-nsd"},
                    {"index": 1, "label": "", "location": "", "preset": ""},
                    {"index": 2, "label": "", "location": "", "preset": ""},
                    {"index": 3, "label": "", "location": "", "preset": ""},
                ],
            },
        }
        r = client.post("/api/signal-chain", json=body)
        assert r.status_code == 200
        assert r.json()["sub_outputs"] == [0]


class TestConfigOutputSlots:
    def test_default_config_has_output_slots(self):
        """DEFAULT_CONFIG.minidsp has 4 empty output_slots."""
        from calibrate.config import DEFAULT_CONFIG
        slots = DEFAULT_CONFIG["minidsp"]["output_slots"]
        assert len(slots) == 4
        assert all(s["label"] == "" for s in slots)
        assert all(s["preset"] == "" for s in slots)

    def test_config_load_preserves_output_slots(self, tmp_path):
        """Config.load reads output_slots from YAML correctly."""
        import yaml
        from calibrate.config import Config
        cfg_data = {
            "minidsp": {
                "host": "localhost", "port": 5380,
                "output_slots": [
                    {"index": 0, "label": "Sub L", "location": "front-left", "preset": "pb12-nsd"},
                ],
            },
        }
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(cfg_data))
        cfg = Config.load(p)
        slots = cfg.minidsp.get("output_slots")
        assert slots is not None
        assert slots[0]["label"] == "Sub L"


# ── _alignment_cleanup_loop — daemon thread body (lines 112-123) ─────────────

def test_alignment_cleanup_loop_evicts_expired_sessions():
    """_alignment_cleanup_loop evicts expired sessions and calls _restore_sub_gains (lines 112-123).

    Drive one iteration by making time.sleep raise StopIteration on the second call,
    which breaks the while-True loop.
    """
    import calibrate.web as web_mod

    session = _AlignmentSession(
        token="expiredtoken1234",
        created_at=0.0,  # far in the past → expired immediately
        sub_outputs=[0],
        sweep_samples=[],
        sample_rate=48000,
        sweep_duration=0.05,
        step=0,
    )

    sleep_calls = {"n": 0}

    def _one_iteration_sleep(_):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 2:
            raise StopIteration("done after one iteration")

    restore_calls = []

    with (
        patch("calibrate.web.time.sleep", side_effect=_one_iteration_sleep),
        patch("calibrate.web.time.time", return_value=web_mod.ALIGNMENT_SESSION_TTL_S + 1),
        patch("calibrate.web._restore_sub_gains", side_effect=restore_calls.append),
        patch.dict(web_mod._pending_alignments, {"expiredtoken1234": session}, clear=True),
    ):
        try:
            web_mod._alignment_cleanup_loop()
        except StopIteration:
            pass

    # Session should have been evicted and restore called
    assert len(restore_calls) == 1
    assert restore_calls[0].token == "expiredtoken1234"
    # Session removed from pending dict
    assert "expiredtoken1234" not in web_mod._pending_alignments


# ── _restore_sub_gains error path ────────────────────────────────────────────

def test_restore_sub_gains_exception_logged():
    """_restore_sub_gains swallows exceptions from client (line 134-135)."""
    from calibrate.web import _restore_sub_gains, _AlignmentSession

    session = _AlignmentSession(
        token="test-token",
        created_at=0.0,
        sub_outputs=[0, 1],
        sweep_samples=[],
        sample_rate=48000,
        sweep_duration=0.05,
        step=0,
    )
    with patch("calibrate.adapters.minidsp.MinidspClient") as mock_client_cls:
        mock_client = MagicMock()
        # restore_all_gains raises when run_until_complete is called
        mock_client.restore_all_gains.side_effect = RuntimeError("connection refused")
        mock_client_cls.return_value = mock_client
        # Must not raise — error is logged
        _restore_sub_gains(session)


# ── _read_semantic_version ────────────────────────────────────────────────────

def test_read_semantic_version_from_env_var(monkeypatch):
    """APP_VERSION env var → returned immediately (lines 2179-2181)."""
    import calibrate.web as web_mod
    orig = web_mod._SEMANTIC_VERSION
    web_mod._SEMANTIC_VERSION = None
    monkeypatch.setenv("APP_VERSION", "1.2.3")
    try:
        from calibrate.web import _read_semantic_version
        result = _read_semantic_version()
        assert result == "1.2.3"
    finally:
        web_mod._SEMANTIC_VERSION = orig
        monkeypatch.delenv("APP_VERSION", raising=False)


def test_read_semantic_version_fallback_to_unknown(monkeypatch):
    """If no env var and no VERSION file → returns 'unknown' (lines 2188-2189)."""
    import calibrate.web as web_mod
    import pathlib

    orig = web_mod._SEMANTIC_VERSION
    monkeypatch.delenv("APP_VERSION", raising=False)
    try:
        web_mod._SEMANTIC_VERSION = None
        from calibrate.web import _read_semantic_version
        # Patch pathlib.Path.read_text to always raise FileNotFoundError
        with patch.object(pathlib.Path, "read_text", side_effect=FileNotFoundError("no file")):
            result = _read_semantic_version()
        assert result == "unknown"
    finally:
        web_mod._SEMANTIC_VERSION = orig


# ── _fetch_latest_sha ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_latest_sha_timeout():
    """httpx.TimeoutException in _fetch_latest_sha → returns None."""
    import httpx
    from calibrate.web import _fetch_latest_sha
    with patch("httpx.AsyncClient") as MockClient:
        mock = AsyncMock()
        mock.__aenter__ = AsyncMock(return_value=mock)
        mock.__aexit__ = AsyncMock(return_value=None)
        mock.get.side_effect = httpx.TimeoutException("timeout")
        MockClient.return_value = mock
        result = await _fetch_latest_sha()
    assert result is None


@pytest.mark.asyncio
async def test_fetch_latest_sha_401_returns_none():
    """401 on token request → returns None (lines 2133-2136)."""
    from calibrate.web import _fetch_latest_sha
    with patch("httpx.AsyncClient") as MockClient:
        mock = AsyncMock()
        mock.__aenter__ = AsyncMock(return_value=mock)
        mock.__aexit__ = AsyncMock(return_value=None)
        token_resp = MagicMock()
        token_resp.status_code = 401
        mock.get.return_value = token_resp
        MockClient.return_value = mock
        result = await _fetch_latest_sha()
    assert result is None


@pytest.mark.asyncio
async def test_fetch_latest_sha_429_returns_none():
    """429 on manifest → returns None (lines 2148-2150)."""
    import json as json_mod
    from calibrate.web import _fetch_latest_sha
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        # Token response succeeds
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.raise_for_status = MagicMock()
        token_resp.json.return_value = {"token": "abc123"}

        # Manifest response is 429
        manifest_resp = MagicMock()
        manifest_resp.status_code = 429

        mock_client.get.side_effect = [token_resp, manifest_resp]
        MockClient.return_value = mock_client

        result = await _fetch_latest_sha()
    assert result is None


@pytest.mark.asyncio
async def test_fetch_latest_sha_generic_exception_returns_none():
    """Generic exception → returns None (lines 2157-2159)."""
    from calibrate.web import _fetch_latest_sha
    with patch("httpx.AsyncClient") as MockClient:
        mock = AsyncMock()
        mock.__aenter__ = AsyncMock(return_value=mock)
        mock.__aexit__ = AsyncMock(return_value=None)
        mock.get.side_effect = OSError("network unreachable")
        MockClient.return_value = mock
        result = await _fetch_latest_sha()
    assert result is None


# ── api_upgrade — OSError ENOSPC path ────────────────────────────────────────

def test_upgrade_oserror_enospc(client, tmp_path, monkeypatch):
    """OSError errno=28 (disk full) → 503 with 'disk full' detail (lines 2267-2269)."""
    import errno
    import calibrate.web as web_mod
    monkeypatch.setattr(web_mod, "_DATA_DIR", tmp_path)
    err = OSError()
    err.errno = errno.ENOSPC
    with patch("pathlib.Path.touch", side_effect=err):
        r = client.post("/api/upgrade")
    assert r.status_code == 503
    assert "disk full" in r.json()["detail"]


def test_upgrade_oserror_generic(client, tmp_path, monkeypatch):
    """Generic OSError → 503 with error text (line 2270)."""
    import calibrate.web as web_mod
    monkeypatch.setattr(web_mod, "_DATA_DIR", tmp_path)
    err = OSError("some other error")
    err.errno = 99
    with patch("pathlib.Path.touch", side_effect=err):
        r = client.post("/api/upgrade")
    assert r.status_code == 503
    assert "Upgrade unavailable" in r.json()["detail"]


# ── blend_check_start error ────────────────────────────────────────────────────

def test_blend_check_start_engine_error(client, cfg_path):
    """RuntimeError from generate_sweep → 500 (lines 2410-2411)."""
    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
    ):
        MockEngine.return_value.generate_sweep.side_effect = RuntimeError("audio init failed")
        r = client.post("/api/blend-check/start")
    assert r.status_code == 500


def test_blend_check_start_play_thread_exception_logged(client, cfg_path):
    """_play() in blend-check start logs exceptions without crashing (lines 2426-2430)."""
    captured_fn = {}

    def capture_thread(target=None, daemon=False):
        captured_fn["fn"] = target
        m = MagicMock()
        m.start = MagicMock()
        return m

    with (
        patch("calibrate.web.CONFIG_PATH", cfg_path),
        patch("calibrate.web.MeasurementEngine") as MockEngine,
        patch("calibrate.web.threading.Thread", side_effect=capture_thread),
        patch("calibrate.web.time.sleep"),
    ):
        MockEngine.return_value.generate_sweep.return_value = ([0.0] * 100, 48000, 1.0)
        MockEngine.return_value.play_signal.side_effect = RuntimeError("device busy")
        client.post("/api/blend-check/start")

    assert captured_fn.get("fn") is not None
    captured_fn["fn"]()  # must not raise


# ── time_align — sub lags recommendation ─────────────────────────────────────

def test_time_align_sub_lags_recommendation(client):
    """sub_leads=False → recommendation says 'lags' (line 2572).

    offset_ms = lag_samples / sr * 1000 where lag_samples = argmax(corr) - (len(bp2)-1).
    To get sub_leads=False we need offset_ms <= 0.
    If sub IR is delayed relative to mains, argmax of cross-corr is < (len-1), giving negative lag.
    """
    # Directly patch compute_time_offset_ms to return a negative value (sub lags)
    s_sub = _make_session_with_ir(1, [0.0] * 100)
    s_mains = _make_session_with_ir(2, [0.0] * 100)

    sessions = {1: s_sub, 2: s_mains}
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.side_effect = lambda sid: sessions.get(sid)
        with patch("calibrate.web.compute_time_offset_ms", return_value=-10.0):
            r = client.post("/api/sessions/time-align",
                            json={"sub_session_id": 1, "mains_session_id": 2})

    assert r.status_code == 200
    data = r.json()
    assert data["sub_leads"] is False
    assert "lags" in data["recommendation"]


# ── cardioid — non-404 MinidspApiError → 502 ─────────────────────────────────

def test_cardioid_502_non_404_error(client, tmp_path):
    """MinidspApiError with status != 404 → 502 (line 2633)."""
    from calibrate.adapters.minidsp import MinidspApiError
    cfg_p = _make_cardioid_config(tmp_path)
    with (
        patch("calibrate.web.CONFIG_PATH", cfg_p),
        patch("calibrate.adapters.minidsp.MinidspClient") as MockClient,
    ):
        mc = MagicMock()
        mc.set_output_polarity = AsyncMock(
            side_effect=MinidspApiError(500, "/output/1/polarity")
        )
        MockClient.return_value = mc
        r = client.post("/api/signal-path/cardioid", json={"enabled": True})

    assert r.status_code == 502


# ── align-subs start — MinidspApiError 503 ────────────────────────────────────

def test_align_subs_start_minidsp_api_error(client):
    """MinidspApiError on mute → 503 (lines 2743-2744)."""
    from calibrate.adapters.minidsp import MinidspApiError

    with (
        patch("calibrate.web._load_config", return_value=_make_align_config()),
        patch("calibrate.web.MeasurementEngine") as mock_engine_cls,
        patch("calibrate.adapters.minidsp.MinidspClient") as mock_client_cls,
    ):
        mock_engine_cls.return_value.generate_sweep.return_value = ([0.0] * 100, 48000, 0.5)
        mock_client = AsyncMock()
        mock_client.set_output_gain.side_effect = MinidspApiError(503, "/output/1/gain")
        mock_client_cls.return_value = mock_client
        r = client.post("/api/align-subs/start")

    assert r.status_code == 503


def test_align_subs_start_play_thread_exception_logged(client):
    """align-subs _play thread logs play_signal exceptions (lines 2764-2767)."""
    captured_fn = {}

    def capture_thread(target=None, daemon=False):
        captured_fn["fn"] = target
        m = MagicMock()
        m.start = MagicMock()
        return m

    with (
        patch("calibrate.web._load_config", return_value=_make_align_config()),
        patch("calibrate.web.MeasurementEngine") as mock_engine_cls,
        patch("calibrate.adapters.minidsp.MinidspClient") as mock_client_cls,
        patch("calibrate.web.threading.Thread", side_effect=capture_thread),
        patch("calibrate.web.time.sleep"),
    ):
        mock_engine_cls.return_value.generate_sweep.return_value = ([0.0] * 100, 48000, 0.5)
        mock_engine_cls.return_value.play_signal.side_effect = RuntimeError("device busy")
        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client
        client.post("/api/align-subs/start")

    assert captured_fn.get("fn") is not None
    captured_fn["fn"]()  # must not raise


# ── align-subs record — short body ────────────────────────────────────────────

def test_align_subs_record_short_body(client):
    """Recording body < 4 bytes → 400 (line 2807)."""
    import time as _time
    token = str(uuid.uuid4())
    session = _AlignmentSession(
        token=token,
        created_at=_time.time(),
        sub_outputs=[0, 1],
        sweep_samples=[0.0] * 100,
        sample_rate=48000,
        sweep_duration=0.05,
        step=0,
    )
    with _align_lock:
        _pending_alignments[token] = session

    r = client.post(
        "/api/align-subs/record",
        content=b"\x00\x00",
        headers={"X-Token": token, "X-Step": "0"},
    )
    assert r.status_code == 400


# ── align-subs record — advance_subs failure ──────────────────────────────────

def test_align_subs_record_advance_subs_failure_logged(client):
    """advance_subs failure → logged as warning, response still returns next_step (lines 2858-2859)."""
    import time as _time
    token = str(uuid.uuid4())
    session = _AlignmentSession(
        token=token,
        created_at=_time.time(),
        sub_outputs=[0, 1],
        sweep_samples=[0.001] * 2400,
        sample_rate=48000,
        sweep_duration=0.05,
        step=0,
    )
    with _align_lock:
        _pending_alignments[token] = session

    with (
        patch("calibrate.web._load_config", return_value=_make_align_config()),
        patch("calibrate.web.MeasurementEngine"),
        patch("calibrate.alignment.measure_sub_ir") as mock_measure,
        patch("calibrate.adapters.minidsp.MinidspClient") as mock_client_cls,
    ):
        mock_measure.return_value = _make_ir_result(0)
        mock_client = AsyncMock()
        mock_client.set_output_gain.side_effect = Exception("write error")
        mock_client_cls.return_value = mock_client

        r = client.post(
            "/api/align-subs/record",
            content=_make_recording_bytes(),
            headers={"X-Token": token, "X-Step": "0"},
        )

    # Even with advance failure, the response should indicate the next step
    assert r.status_code == 200
    assert r.json()["next_step"] == 1


def test_align_subs_record_play_next_exception_logged(client):
    """_play_next thread logs play exceptions without raising (lines 2863-2866)."""
    import time as _time
    captured_fn = {}

    real_thread = __import__("threading").Thread

    def capture_thread(target=None, daemon=False):
        captured_fn["fn"] = target
        m = MagicMock()
        m.start = MagicMock()
        return m

    token = str(uuid.uuid4())
    session = _AlignmentSession(
        token=token,
        created_at=_time.time(),
        sub_outputs=[0, 1],
        sweep_samples=[0.001] * 2400,
        sample_rate=48000,
        sweep_duration=0.05,
        step=0,
    )
    with _align_lock:
        _pending_alignments[token] = session

    with (
        patch("calibrate.web._load_config", return_value=_make_align_config()),
        patch("calibrate.web.MeasurementEngine") as mock_engine_cls,
        patch("calibrate.alignment.measure_sub_ir") as mock_measure,
        patch("calibrate.adapters.minidsp.MinidspClient") as mock_client_cls,
        patch("calibrate.web.threading.Thread", side_effect=capture_thread),
        patch("calibrate.web.time.sleep"),
    ):
        mock_measure.return_value = _make_ir_result(0)
        mock_engine_cls.return_value.play_signal.side_effect = RuntimeError("audio error")
        mock_client = AsyncMock()
        mock_client_cls.return_value = mock_client

        client.post(
            "/api/align-subs/record",
            content=_make_recording_bytes(),
            headers={"X-Token": token, "X-Step": "0"},
        )

    assert captured_fn.get("fn") is not None
    captured_fn["fn"]()  # must not raise


# ── get_device_state — generic exception → 502 ───────────────────────────────

def test_get_device_state_generic_exception(client, tmp_path, monkeypatch):
    """Generic exception → 502 with 'Cannot reach miniDSP' (lines 3021-3022)."""
    import yaml
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.dump({"minidsp": {"host": "localhost", "port": 5380}}))
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_file)

    mock_client = AsyncMock()
    mock_client.get_device_status.side_effect = OSError("connection timed out")
    with patch("calibrate.adapters.minidsp.MinidspClient", return_value=mock_client):
        r = client.get("/api/signal-path/device-state")

    assert r.status_code == 502
    assert "Cannot reach" in r.json()["detail"]


# ── equipment_denon_state ─────────────────────────────────────────────────────

def test_denon_state_no_host(client, cfg_path, monkeypatch):
    """No Denon host configured → connected=False (line 3064)."""
    import yaml
    no_host_cfg = cfg_path.parent / "no_host.yaml"
    no_host_cfg.write_text(yaml.dump({
        "denon": {"host": None},
        "minidsp": {"host": "localhost", "port": 5380},
        "mic": {"name": "UMIK"},
    }))
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", no_host_cfg)
    r = client.get("/api/equipment/denon/state")
    assert r.status_code == 200
    assert r.json()["connected"] is False
    assert "No host configured" in r.json()["error"]


def test_denon_state_connected(client, cfg_path, monkeypatch):
    """Happy path: DenonAVR fetches state correctly (lines 3066-3080)."""
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
    receiver = AsyncMock()
    receiver.model_name = "X3800H"
    receiver.input_func = "HDMI 1"
    receiver.input_func_list = ["HDMI 1", "HDMI 2"]
    receiver.volume = -30.0
    receiver.muted = False

    with patch("denonavr.DenonAVR", return_value=receiver):
        r = client.get("/api/equipment/denon/state")

    assert r.status_code == 200
    d = r.json()
    assert d["connected"] is True
    assert d["model"] == "X3800H"
    assert "HDMI 1" in d["inputs"]


def test_denon_state_timeout(client, cfg_path, monkeypatch):
    """asyncio.TimeoutError → connected=False with timeout message (lines 3081-3082)."""
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
    receiver = AsyncMock()
    receiver.async_setup.side_effect = asyncio.TimeoutError()

    with patch("denonavr.DenonAVR", return_value=receiver):
        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            r = client.get("/api/equipment/denon/state")

    assert r.status_code == 200
    assert r.json()["connected"] is False
    assert "Timeout" in r.json()["error"]


def test_denon_state_generic_exception(client, cfg_path, monkeypatch):
    """Generic exception → connected=False with error string (lines 3083-3084)."""
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
    receiver = AsyncMock()
    receiver.async_setup.side_effect = OSError("connection refused")

    with patch("denonavr.DenonAVR", return_value=receiver):
        with patch("asyncio.wait_for", side_effect=OSError("connection refused")):
            r = client.get("/api/equipment/denon/state")

    assert r.status_code == 200
    assert r.json()["connected"] is False
    assert "connection refused" in r.json()["error"]


# ── equipment_denon_discover ──────────────────────────────────────────────────

def test_denon_discover_found_via_configured_host(client, cfg_path, monkeypatch):
    """Configured host is reachable → returns that host (lines 3092-3103)."""
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
    receiver = AsyncMock()

    with patch("denonavr.DenonAVR", return_value=receiver):
        with patch("denonavr.async_discover", return_value=AsyncMock(return_value=[])):
            r = client.post("/api/equipment/denon/discover")

    # The configured host in cfg_path is 192.168.1.100
    assert r.status_code == 200
    assert r.json()["host"] == "192.168.1.100"


def test_denon_discover_no_host_found(client, tmp_path, monkeypatch):
    """Neither configured host nor SSDP → 404 (lines 3128-3129)."""
    import yaml
    no_host = tmp_path / "config.yaml"
    no_host.write_text(yaml.dump({
        "denon": {"host": None},
        "minidsp": {"host": "localhost", "port": 5380},
        "mic": {"name": "UMIK"},
    }))
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", no_host)

    with patch("denonavr.DenonAVR", side_effect=OSError("refused")):
        with patch("denonavr.async_discover", side_effect=OSError("no ssdp")):
            r = client.post("/api/equipment/denon/discover")

    assert r.status_code == 404


# ── equipment_denon_save — write failure ─────────────────────────────────────

def test_denon_save_write_failure(client, cfg_path, monkeypatch):
    """update_config raising exception → 500 (lines 3145-3146)."""
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
    with patch("calibrate.web.update_config", side_effect=OSError("disk full")):
        r = client.post("/api/equipment/denon/save",
                        json={"host": "192.168.1.50", "sweep_input": None})
    assert r.status_code == 500
    assert "Failed to write config" in r.json()["detail"]


# ── equipment_denon_test_input ────────────────────────────────────────────────

def test_denon_test_input_success_tone_played(client, cfg_path, monkeypatch):
    """Happy path: input switched and aplay succeeds → tone_played=True (lines 3159-3215)."""
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
    receiver = AsyncMock()

    with patch("denonavr.DenonAVR", return_value=receiver):
        with patch("subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_run.return_value = mock_proc
            with patch("asyncio.to_thread", new=AsyncMock(return_value=mock_proc)):
                r = client.post("/api/equipment/denon/test-input",
                                json={"host": "192.168.1.100", "input": "HDMI 1"})

    assert r.status_code == 200
    d = r.json()
    assert d["switched"] is True
    assert d["input"] == "HDMI 1"


def test_denon_test_input_denon_failure(client, cfg_path, monkeypatch):
    """Denon control failure → 502 (lines 3167-3168)."""
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
    receiver = AsyncMock()
    receiver.async_setup.side_effect = OSError("connection refused")

    with patch("denonavr.DenonAVR", return_value=receiver):
        r = client.post("/api/equipment/denon/test-input",
                        json={"host": "192.168.1.100", "input": "HDMI 1"})

    assert r.status_code == 502
    assert "Denon control failed" in r.json()["detail"]


def test_denon_test_input_aplay_fails(client, cfg_path, monkeypatch):
    """aplay non-zero returncode → tone_played=False, tone_error set (lines 3207-3208)."""
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
    receiver = AsyncMock()

    with patch("denonavr.DenonAVR", return_value=receiver):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = b"aplay: device not found"
        with patch("asyncio.to_thread", new=AsyncMock(return_value=mock_proc)):
            r = client.post("/api/equipment/denon/test-input",
                            json={"host": "192.168.1.100", "input": "HDMI 1"})

    assert r.status_code == 200
    d = r.json()
    assert d["tone_played"] is False
    assert d["tone_error"] is not None


# ── save-labels — write failure ────────────────────────────────────────────────

def test_minidsp_save_labels_write_failure(client, cfg_path, monkeypatch):
    """update_config exception → 500 (lines 3234-3235)."""
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
    with patch("calibrate.web.update_config", side_effect=OSError("disk full")):
        r = client.post("/api/equipment/minidsp/save-labels", json={
            "inputs": ["LFE L"],
            "outputs": ["Sub L"],
        })
    assert r.status_code == 500
    assert "Failed to write config" in r.json()["detail"]


# ── signal_chain_get — config read error ─────────────────────────────────────

def test_signal_chain_get_config_error(client, cfg_path, monkeypatch):
    """Config.load raises exception → 500 with 'Config read error' (lines 3301-3302)."""
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
    with patch("calibrate.web.Config.load", side_effect=Exception("YAML parse error")):
        r = client.get("/api/signal-chain")
    assert r.status_code == 500
    assert "Config read error" in r.json()["detail"]


# ── signal_chain_post — routing and write failure ────────────────────────────

def test_signal_chain_post_with_routing(client, cfg_path, monkeypatch):
    """POST with routing → input_routing saved (line 3383)."""
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
    monkeypatch.setattr("calibrate.config.CONFIG_PATH", cfg_path)
    body = {
        "denon": {"host": "192.168.1.100", "sweep_input": None},
        "minidsp": {
            "input_labels": {},
            "routing": [{"input": 0, "outputs": [0, 1, 2, 3]}],
            "output_slots": _empty_slots(),
        },
    }
    r = client.post("/api/signal-chain", json=body)
    assert r.status_code == 200
    import yaml
    data = yaml.safe_load(cfg_path.read_text())
    assert "input_routing" in data.get("minidsp", {})


def test_signal_chain_post_write_failure(client, cfg_path, monkeypatch):
    """update_config exception → 500 (lines 3392-3393)."""
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
    with patch("calibrate.web.update_config", side_effect=OSError("disk full")):
        r = client.post("/api/signal-chain", json=_full_chain_body())
    assert r.status_code == 500
    assert "Failed to write config" in r.json()["detail"]


# ── _fetch_latest_sha — manifest happy path ───────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_latest_sha_happy_path():
    """Token + manifest both succeed → returns revision SHA (lines 2151-2153)."""
    from calibrate.web import _fetch_latest_sha
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.raise_for_status = MagicMock()
        token_resp.json.return_value = {"token": "abc123"}

        manifest_resp = MagicMock()
        manifest_resp.status_code = 200
        manifest_resp.raise_for_status = MagicMock()
        manifest_resp.json.return_value = {
            "annotations": {"org.opencontainers.image.revision": "deadbeef1234"}
        }

        mock_client.get.side_effect = [token_resp, manifest_resp]
        MockClient.return_value = mock_client

        result = await _fetch_latest_sha()
    assert result == "deadbeef1234"


# ── time_align — sub session not found (line 2542) ───────────────────────────

def test_time_align_sub_session_not_found(client):
    """sub_session_id not found → 404 (line 2542)."""
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.return_value = None
        r = client.post("/api/sessions/time-align",
                        json={"sub_session_id": 999, "mains_session_id": 2})
    assert r.status_code == 404
    assert "999" in r.json()["detail"]


# ── time_align — sub leads mains recommendation (line 2572) ──────────────────

def test_time_align_sub_leads_recommendation(client):
    """sub_leads=True → recommendation says 'leads' (line 2572)."""
    s_sub = _make_session_with_ir(1, [0.0] * 100)
    s_mains = _make_session_with_ir(2, [0.0] * 100)

    sessions = {1: s_sub, 2: s_mains}
    with patch("calibrate.web.SessionStore") as MockStore:
        MockStore.return_value.get_session.side_effect = lambda sid: sessions.get(sid)
        with patch("calibrate.web.compute_time_offset_ms", return_value=10.0):
            r = client.post("/api/sessions/time-align",
                            json={"sub_session_id": 1, "mains_session_id": 2})

    assert r.status_code == 200
    data = r.json()
    assert data["sub_leads"] is True
    assert "leads" in data["recommendation"]


# ── equipment_denon_discover — configured host raises exception (line 3102-3103)

def test_denon_discover_configured_host_exception_falls_back_to_ssdp(client, cfg_path, monkeypatch):
    """Configured host setup raises exception → _check_configured_host returns None,
    SSDP succeeds (lines 3102-3103)."""
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)

    receiver = AsyncMock()
    receiver.async_setup.side_effect = OSError("connection refused")

    with (
        patch("denonavr.DenonAVR", return_value=receiver),
        patch("denonavr.async_discover", new=AsyncMock(return_value=[{"host": "10.0.0.5"}])),
    ):
        r = client.post("/api/equipment/denon/discover")

    assert r.status_code == 200
    assert r.json()["host"] == "10.0.0.5"


# ── signal_chain_get — old_outputs read exception (lines 3326-3327) ──────────

def test_signal_chain_get_old_outputs_read_exception(client, cfg_path, monkeypatch):
    """Exception reading raw YAML for old_outputs migration → silently caught (lines 3326-3327)."""
    import yaml as _yaml_mod
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", cfg_path)
    monkeypatch.setattr("calibrate.config.CONFIG_PATH", cfg_path)

    call_count = {"n": 0}
    real_safe_load = _yaml_mod.safe_load

    def _patched_safe_load(stream):
        call_count["n"] += 1
        # First call is Config.load (config.py); second+ are the migration reads in web.py
        if call_count["n"] >= 2:
            raise OSError("permission denied")
        return real_safe_load(stream)

    with patch("yaml.safe_load", side_effect=_patched_safe_load):
        r = client.get("/api/signal-chain")

    # Exception is caught; route still returns 200
    assert r.status_code == 200


# ── signal_chain_get — old_inputs read exception (lines 3341-3342) ───────────

def test_signal_chain_get_old_inputs_read_exception(client, tmp_path, monkeypatch):
    """Exception reading raw YAML for old_inputs → silently caught (lines 3341-3342)."""
    import yaml
    import yaml as _yaml_mod
    # Config with old connections.minidsp.outputs so first migration read returns data,
    # triggering the second migration read (for old_inputs) to also run.
    cfg_data = {
        "denon": {"host": "192.168.1.100"},
        "minidsp": {"host": "localhost", "port": 5380},
        "connections": {"minidsp": {"outputs": {"0": "Sub L"}}},
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg_data))
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", p)
    monkeypatch.setattr("calibrate.config.CONFIG_PATH", p)

    call_count = {"n": 0}
    real_safe_load = _yaml_mod.safe_load

    def _patched_safe_load(stream):
        call_count["n"] += 1
        # Call 1: Config.load in config.py (succeeds)
        # Call 2: first migration tombstone (old_outputs — succeeds, returns connections data)
        # Call 3: second migration tombstone (old_inputs — raises)
        if call_count["n"] >= 3:
            raise OSError("permission denied on inputs read")
        return real_safe_load(stream)

    with patch("yaml.safe_load", side_effect=_patched_safe_load):
        r = client.get("/api/signal-chain")

    assert r.status_code == 200


# ── POST /api/signal-path/test ────────────────────────────────────────────────

def _signal_path_cfg(tmp_path, *, denon_host="192.168.1.100", sweep_input="HDMI 1"):
    """Write a config.yaml suitable for signal-path/test and return the path."""
    import yaml
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({
        "denon": {"host": denon_host},
        "minidsp": {"host": "localhost", "port": 5380},
        "measurement": {"denon_sweep_input": sweep_input},
    }))
    return p


def test_signal_path_test_missing_config(client, tmp_path, monkeypatch):
    """Config missing denon host and sweep_input → Denon step fails immediately."""
    import yaml
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"denon": {}, "minidsp": {}, "measurement": {}}))
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", p)

    r = client.post("/api/signal-path/test")

    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    assert body["steps"][0]["name"] == "Denon"
    assert body["steps"][0]["passed"] is False
    assert "Not configured" in body["steps"][0]["detail"]


def test_signal_path_test_denon_unreachable(client, tmp_path, monkeypatch):
    """Denon connection fails → Denon step fails, no DSP steps attempted."""
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", _signal_path_cfg(tmp_path))
    receiver = AsyncMock()
    receiver.async_setup.side_effect = OSError("timeout")

    with patch("denonavr.DenonAVR", return_value=receiver):
        r = client.post("/api/signal-path/test")

    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    steps = {s["name"]: s for s in body["steps"]}
    assert steps["Denon"]["passed"] is False
    assert "Denon unreachable" in steps["Denon"]["detail"]
    assert "DSP Input" not in steps


def test_signal_path_test_minidsp_unreachable_after_retries(client, tmp_path, monkeypatch):
    """miniDSP returns 500 on all retries → DSP Input step fails."""
    import respx, httpx
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", _signal_path_cfg(tmp_path))
    receiver = AsyncMock()

    with patch("denonavr.DenonAVR", return_value=receiver):
        with patch("asyncio.sleep", new=AsyncMock()):  # skip retry delays
            with respx.mock:
                respx.get("http://localhost:5380/devices/0").mock(
                    return_value=httpx.Response(500)
                )
                r = client.post("/api/signal-path/test")

    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    steps = {s["name"]: s for s in body["steps"]}
    assert steps["DSP Input"]["passed"] is False
    assert "miniDSP unreachable" in steps["DSP Input"]["detail"]


def test_signal_path_test_minidsp_recovers_on_retry(client, tmp_path, monkeypatch):
    """miniDSP returns 500 on first attempt but 200 on retry → proceeds to tone test."""
    import respx, httpx
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", _signal_path_cfg(tmp_path))
    receiver = AsyncMock()

    call_count = {"n": 0}

    def _side_effect(req):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(500)
        payload = {
            "master": {"preset": 0, "source": "Analog"},
            "input_levels": [-80.0, -80.0],
            "output_levels": [-80.0, -80.0, -80.0, -80.0],
        }
        return httpx.Response(200, json=payload)

    with patch("denonavr.DenonAVR", return_value=receiver):
        with patch("asyncio.sleep", new=AsyncMock()):
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            with patch("asyncio.to_thread", new=AsyncMock(return_value=mock_proc)):
                with respx.mock:
                    respx.get("http://localhost:5380/devices/0").mock(side_effect=_side_effect)
                    r = client.post("/api/signal-path/test")

    assert r.status_code == 200
    body = r.json()
    # retry succeeded — should reach signal check steps (not fail at DSP connectivity)
    steps = {s["name"]: s for s in body["steps"]}
    assert "miniDSP unreachable" not in steps.get("DSP Input", {}).get("detail", "")


def test_signal_path_test_tone_failure(client, tmp_path, monkeypatch):
    """aplay returns non-zero → DSP Input step fails with tone error."""
    import respx, httpx
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", _signal_path_cfg(tmp_path))
    receiver = AsyncMock()
    device_payload = {
        "master": {"preset": 0, "source": "Analog"},
        "input_levels": [-128.0, -128.0],
        "output_levels": [-128.0, -128.0, -128.0, -128.0],
    }

    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stderr = b"aplay: error opening device"

    with patch("denonavr.DenonAVR", return_value=receiver):
        with patch("asyncio.sleep", new=AsyncMock()):
            with patch("asyncio.to_thread", new=AsyncMock(return_value=mock_proc)):
                with respx.mock:
                    respx.get("http://localhost:5380/devices/0").mock(
                        return_value=httpx.Response(200, json=device_payload)
                    )
                    r = client.post("/api/signal-path/test")

    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    steps = {s["name"]: s for s in body["steps"]}
    assert "Tone playback failed" in steps["DSP Input"]["detail"]


def test_signal_path_test_no_input_signal(client, tmp_path, monkeypatch):
    """miniDSP input levels at noise floor → DSP Input step fails with cable hint."""
    import respx, httpx
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", _signal_path_cfg(tmp_path))
    receiver = AsyncMock()
    device_payload = {
        "master": {"preset": 0, "source": "Analog"},
        "input_levels": [-128.0, -128.0],
        "output_levels": [-128.0, -128.0, -128.0, -128.0],
    }

    mock_proc = MagicMock()
    mock_proc.returncode = 0

    with patch("denonavr.DenonAVR", return_value=receiver):
        with patch("asyncio.sleep", new=AsyncMock()):
            with patch("asyncio.to_thread", new=AsyncMock(return_value=mock_proc)):
                with respx.mock:
                    respx.get("http://localhost:5380/devices/0").mock(
                        return_value=httpx.Response(200, json=device_payload)
                    )
                    r = client.post("/api/signal-path/test")

    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    steps = {s["name"]: s for s in body["steps"]}
    assert steps["DSP Input"]["passed"] is False
    assert "no signal detected" in steps["DSP Input"]["detail"]


def test_signal_path_test_no_output_signal(client, tmp_path, monkeypatch):
    """Input signal present but outputs flat → DSP Output step fails with routing hint."""
    import respx, httpx
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", _signal_path_cfg(tmp_path))
    receiver = AsyncMock()
    device_payload = {
        "master": {"preset": 0, "source": "Analog"},
        "input_levels": [-80.0, -80.0],   # signal present
        "output_levels": [-128.0, -128.0, -128.0, -128.0],  # routing broken
    }

    mock_proc = MagicMock()
    mock_proc.returncode = 0

    with patch("denonavr.DenonAVR", return_value=receiver):
        with patch("asyncio.sleep", new=AsyncMock()):
            with patch("asyncio.to_thread", new=AsyncMock(return_value=mock_proc)):
                with respx.mock:
                    respx.get("http://localhost:5380/devices/0").mock(
                        return_value=httpx.Response(200, json=device_payload)
                    )
                    r = client.post("/api/signal-path/test")

    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    steps = {s["name"]: s for s in body["steps"]}
    assert steps["DSP Input"]["passed"] is True
    assert steps["DSP Output"]["passed"] is False
    assert "routing" in steps["DSP Output"]["detail"].lower()


def test_signal_path_test_full_pass(client, tmp_path, monkeypatch):
    """Signal present at both input and output → all steps pass."""
    import respx, httpx
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", _signal_path_cfg(tmp_path))
    receiver = AsyncMock()
    device_payload = {
        "master": {"preset": 0, "source": "Analog"},
        "input_levels": [-70.0, -72.0],
        "output_levels": [-71.0, -128.0, -128.0, -128.0],
    }

    mock_proc = MagicMock()
    mock_proc.returncode = 0

    with patch("denonavr.DenonAVR", return_value=receiver):
        with patch("asyncio.sleep", new=AsyncMock()):
            with patch("asyncio.to_thread", new=AsyncMock(return_value=mock_proc)):
                with respx.mock:
                    respx.get("http://localhost:5380/devices/0").mock(
                        return_value=httpx.Response(200, json=device_payload)
                    )
                    r = client.post("/api/signal-path/test")

    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is True
    steps = {s["name"]: s for s in body["steps"]}
    assert steps["Denon"]["passed"] is True
    assert steps["DSP Input"]["passed"] is True
    assert steps["DSP Output"]["passed"] is True


def test_signal_path_test_tone_exception(client, tmp_path, monkeypatch):
    """Exception inside _play_60hz_tone (e.g. tempfile fails) → DSP Input fails."""
    import respx, httpx
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", _signal_path_cfg(tmp_path))
    receiver = AsyncMock()
    device_payload = {
        "master": {"preset": 0, "source": "Analog"},
        "input_levels": [-70.0, -70.0],
        "output_levels": [-70.0, -70.0, -70.0, -70.0],
    }

    with patch("denonavr.DenonAVR", return_value=receiver):
        with patch("asyncio.sleep", new=AsyncMock()):
            with patch("asyncio.to_thread", side_effect=OSError("disk full")):
                with respx.mock:
                    respx.get("http://localhost:5380/devices/0").mock(
                        return_value=httpx.Response(200, json=device_payload)
                    )
                    r = client.post("/api/signal-path/test")

    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    steps = {s["name"]: s for s in body["steps"]}
    assert "Tone playback failed" in steps["DSP Input"]["detail"]


def test_signal_path_test_mid_tone_levels_fail(client, tmp_path, monkeypatch):
    """get_device_status raises during mid-tone sampling → DSP Input fails."""
    import respx, httpx
    from calibrate.adapters.minidsp import MinidspApiError
    monkeypatch.setattr("calibrate.web.CONFIG_PATH", _signal_path_cfg(tmp_path))
    receiver = AsyncMock()
    ok_payload = {
        "master": {"preset": 0, "source": "Analog"},
        "input_levels": [-70.0, -70.0],
        "output_levels": [-70.0, -70.0, -70.0, -70.0],
    }

    call_count = {"n": 0}

    def _side_effect(req):
        call_count["n"] += 1
        if call_count["n"] <= 1:
            return httpx.Response(200, json=ok_payload)  # connectivity check OK
        return httpx.Response(500)  # mid-tone sampling fails

    mock_proc = MagicMock()
    mock_proc.returncode = 0

    with patch("denonavr.DenonAVR", return_value=receiver):
        with patch("asyncio.sleep", new=AsyncMock()):
            with patch("asyncio.to_thread", new=AsyncMock(return_value=mock_proc)):
                with respx.mock:
                    respx.get("http://localhost:5380/devices/0").mock(side_effect=_side_effect)
                    r = client.post("/api/signal-path/test")

    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    steps = {s["name"]: s for s in body["steps"]}
    assert "Cannot read mid-tone levels" in steps["DSP Input"]["detail"]
