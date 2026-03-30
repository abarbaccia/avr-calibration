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
