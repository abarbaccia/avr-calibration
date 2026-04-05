"""Tests for calibrate.web — simplified endpoint suite.

Covers all 11 endpoints remaining in the simplified web.py:
  GET  /              — HTML dashboard
  GET  /health        — health check
  GET  /api/version   — version info (mocked GHCR)
  POST /api/upgrade   — trigger upgrade file
  POST /api/measure   — headless measurement
  POST /api/sessions/average — average multiple sessions
  GET  /api/sessions  — list sessions with harman_delta_db
  GET  /api/sessions/{id} — session detail
  GET  /api/runs      — list calibration runs
  GET  /api/runs/{id} — run detail
  GET  /api/status    — system status
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from calibrate.config import Config
from calibrate.measurement import FrequencyResponse
from calibrate.storage import SessionStore
from calibrate.web import app

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_fr(
    freqs: list[float] | None = None,
    spl: list[float] | None = None,
) -> FrequencyResponse:
    """Build a minimal FrequencyResponse for testing."""
    if freqs is None:
        freqs = [20.0, 30.0, 50.0, 80.0, 100.0, 150.0, 200.0]
    if spl is None:
        spl = [70.0, 75.0, 80.0, 78.0, 76.0, 72.0, 68.0]
    return FrequencyResponse(
        frequencies=freqs,
        spl=spl,
        sample_rate=48000,
        sweep_duration=3.0,
        timestamp="2025-01-15T12:00:00Z",
    )


def _make_config(
    denon_host: str = "192.168.1.100",
    minidsp_host: str = "localhost",
    minidsp_port: int = 5380,
    mic_name: str = "UMIK",
) -> Config:
    return Config({
        "denon": {"host": denon_host},
        "minidsp": {"host": minidsp_host, "port": minidsp_port},
        "mic": {"name": mic_name},
        "measurement": {
            "denon_sweep_input": "CD",
            "denon_sweep_volume": -25.0,
        },
    })


@pytest.fixture()
def db_store(tmp_path: Path) -> SessionStore:
    """SessionStore backed by a temporary database."""
    return SessionStore(db_path=tmp_path / "test.db")


@pytest.fixture()
def seeded_store(db_store: SessionStore) -> SessionStore:
    """SessionStore with two measurement sessions pre-loaded."""
    fr1 = _make_fr()
    fr2 = _make_fr(
        spl=[72.0, 77.0, 82.0, 79.0, 75.0, 71.0, 67.0],
    )
    db_store.save_measurement(fr1, label="position-1")
    db_store.save_measurement(fr2, label="position-2")
    return db_store


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# ── GET / — HTML dashboard ──────────────────────────────────────────────────


class TestIndex:
    def test_returns_html(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_html_contains_title(self, client: TestClient) -> None:
        resp = client.get("/")
        assert "AVR Calibration" in resp.text

    def test_html_contains_chart_js(self, client: TestClient) -> None:
        resp = client.get("/")
        assert "chart.js" in resp.text


# ── GET /health ─────────────────────────────────────────────────────────────


class TestHealth:
    def test_health_ok(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ── GET /api/version ────────────────────────────────────────────────────────


class TestVersion:
    @patch.dict(os.environ, {"BUILD_SHA": "abc123"})
    @patch("calibrate.web._version_cache", {})
    @patch("calibrate.web._SEMANTIC_VERSION", None)
    @patch.dict(os.environ, {"APP_VERSION": "1.2.3"})
    def test_version_cold_cache(self, client: TestClient) -> None:
        """Cold cache triggers background fetch; returns checking=True."""
        resp = client.get("/api/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_sha"] == "abc123"
        assert data["semantic_version"] == "1.2.3"
        assert data["checking"] is True
        assert data["latest_sha"] is None

    @patch.dict(os.environ, {"BUILD_SHA": "abc123"})
    @patch("calibrate.web._SEMANTIC_VERSION", None)
    @patch.dict(os.environ, {"APP_VERSION": "2.0.0"})
    def test_version_warm_cache_up_to_date(self, client: TestClient) -> None:
        """Warm cache returns cached SHA; up_to_date when SHAs match."""
        import time

        with patch("calibrate.web._version_cache", {
            "result": {
                "latest_sha": "abc123",
                "expires": time.time() + 9999,
                "checked_at": time.time(),
            },
        }):
            resp = client.get("/api/version")
        data = resp.json()
        assert data["checking"] is False
        assert data["latest_sha"] == "abc123"
        assert data["up_to_date"] is True

    @patch.dict(os.environ, {"BUILD_SHA": "abc123"})
    @patch("calibrate.web._SEMANTIC_VERSION", None)
    @patch.dict(os.environ, {"APP_VERSION": "2.0.0"})
    def test_version_warm_cache_outdated(self, client: TestClient) -> None:
        """Warm cache with different SHA returns up_to_date=False."""
        import time

        with patch("calibrate.web._version_cache", {
            "result": {
                "latest_sha": "def456",
                "expires": time.time() + 9999,
                "checked_at": time.time(),
            },
        }):
            resp = client.get("/api/version")
        data = resp.json()
        assert data["up_to_date"] is False
        assert data["latest_sha"] == "def456"

    @patch.dict(os.environ, {"BUILD_SHA": "unknown"})
    @patch("calibrate.web._SEMANTIC_VERSION", None)
    @patch.dict(os.environ, {"APP_VERSION": "0.0.0"})
    def test_version_unknown_sha_not_up_to_date(self, client: TestClient) -> None:
        """When BUILD_SHA is 'unknown', up_to_date is always False."""
        import time

        with patch("calibrate.web._version_cache", {
            "result": {
                "latest_sha": "abc123",
                "expires": time.time() + 9999,
                "checked_at": time.time(),
            },
        }):
            resp = client.get("/api/version")
        assert resp.json()["up_to_date"] is False


# ── POST /api/upgrade ───────────────────────────────────────────────────────


class TestUpgrade:
    def test_upgrade_creates_trigger_file(self, tmp_path: Path, client: TestClient) -> None:
        with patch("calibrate.web._DATA_DIR", tmp_path):
            resp = client.post("/api/upgrade")
        assert resp.status_code == 202
        assert resp.json() == {"status": "upgrade_triggered"}
        assert (tmp_path / "upgrade-trigger").exists()

    def test_upgrade_conflict_if_trigger_exists(self, tmp_path: Path, client: TestClient) -> None:
        trigger = tmp_path / "upgrade-trigger"
        trigger.touch()
        with patch("calibrate.web._DATA_DIR", tmp_path):
            resp = client.post("/api/upgrade")
        assert resp.status_code == 409

    def test_upgrade_permission_error(self, tmp_path: Path, client: TestClient) -> None:
        with patch("calibrate.web._DATA_DIR", tmp_path):
            with patch.object(Path, "touch", side_effect=PermissionError("read-only")):
                resp = client.post("/api/upgrade")
        assert resp.status_code == 503
        assert "not writable" in resp.json()["detail"]

    def test_upgrade_disk_full(self, tmp_path: Path, client: TestClient) -> None:
        with patch("calibrate.web._DATA_DIR", tmp_path):
            with patch.object(Path, "touch", side_effect=OSError(28, "No space left")):
                resp = client.post("/api/upgrade")
        assert resp.status_code == 503
        assert "disk full" in resp.json()["detail"]


# ── POST /api/measure ───────────────────────────────────────────────────────


class TestMeasure:
    def _mock_sounddevice(self) -> MagicMock:
        """Create a sounddevice mock with a UMIK device."""
        sd = MagicMock()
        sd.query_devices.return_value = [
            {"name": "UMIK-1", "max_input_channels": 1},
            {"name": "Built-in Output", "max_input_channels": 0},
        ]
        return sd

    def _mock_denonavr(self) -> MagicMock:
        """Create a denonavr mock with a receiver."""
        denonavr = MagicMock()
        receiver = MagicMock()
        receiver.power = "ON"
        receiver.input_func = "Blu-ray"
        receiver.volume = -30.0
        receiver.input_func_list = ["CD", "Blu-ray", "Media Player"]
        receiver.model_name = "X3800H"

        # Make all async methods proper coroutines
        receiver.async_setup = AsyncMock()
        receiver.async_update = AsyncMock()
        receiver.async_power_on = AsyncMock()
        receiver.async_set_input_func = AsyncMock()
        receiver.async_set_volume = AsyncMock()
        receiver.async_power_off = AsyncMock()

        denonavr.DenonAVR.return_value = receiver
        return denonavr

    def test_measure_happy_path(self, tmp_path: Path, client: TestClient) -> None:
        sd_mock = self._mock_sounddevice()
        denonavr_mock = self._mock_denonavr()
        fr = _make_fr()
        engine_mock = MagicMock()
        engine_mock.measure.return_value = fr
        store = SessionStore(db_path=tmp_path / "test.db")

        with (
            patch.dict(sys.modules, {"sounddevice": sd_mock, "denonavr": denonavr_mock}),
            patch("calibrate.web._load_config", return_value=_make_config()),
            patch("calibrate.web.MeasurementEngine", return_value=engine_mock),
            patch("calibrate.web.SessionStore", return_value=store),
        ):
            resp = client.post("/api/measure", json={"label": "test-sweep"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["session_id"] == 1

    def test_measure_no_sounddevice(self, client: TestClient) -> None:
        """When sounddevice is not installed, return 503."""
        original = sys.modules.get("sounddevice")
        sys.modules["sounddevice"] = None  # type: ignore[assignment]
        try:
            resp = client.post("/api/measure", json={})
        finally:
            if original is not None:
                sys.modules["sounddevice"] = original
            else:
                sys.modules.pop("sounddevice", None)
        assert resp.status_code == 503
        assert "sounddevice" in resp.json()["detail"]

    def test_measure_no_umik(self, tmp_path: Path, client: TestClient) -> None:
        """When no UMIK device is found, return 503."""
        sd_mock = MagicMock()
        sd_mock.query_devices.return_value = [
            {"name": "Built-in Mic", "max_input_channels": 1},
        ]
        with (
            patch.dict(sys.modules, {"sounddevice": sd_mock}),
            patch("calibrate.web._load_config", return_value=_make_config()),
        ):
            resp = client.post("/api/measure", json={})

        assert resp.status_code == 503
        assert "UMIK" in resp.json()["detail"]

    def test_measure_concurrent_409(self, tmp_path: Path, client: TestClient) -> None:
        """When measurement lock is held, return 409."""
        import calibrate.web as web_mod

        # Simulate the lock being held
        loop = asyncio.new_event_loop()
        loop.run_until_complete(web_mod._measurement_lock.acquire())

        sd_mock = self._mock_sounddevice()
        try:
            with patch.dict(sys.modules, {"sounddevice": sd_mock}):
                resp = client.post("/api/measure", json={})
        finally:
            web_mod._measurement_lock.release()
            loop.close()

        assert resp.status_code == 409
        assert "already in progress" in resp.json()["detail"]

    def test_measure_runtime_error(self, tmp_path: Path, client: TestClient) -> None:
        """MeasurementEngine.measure raising RuntimeError returns 503."""
        sd_mock = self._mock_sounddevice()
        denonavr_mock = self._mock_denonavr()
        engine_mock = MagicMock()
        engine_mock.measure.side_effect = RuntimeError("sweep failed")

        with (
            patch.dict(sys.modules, {"sounddevice": sd_mock, "denonavr": denonavr_mock}),
            patch("calibrate.web._load_config", return_value=_make_config()),
            patch("calibrate.web.MeasurementEngine", return_value=engine_mock),
        ):
            resp = client.post("/api/measure", json={})

        assert resp.status_code == 503
        assert "sweep failed" in resp.json()["detail"]

    def test_measure_default_label(self, tmp_path: Path, client: TestClient) -> None:
        """When no label is provided, defaults to 'headless'."""
        sd_mock = self._mock_sounddevice()
        denonavr_mock = self._mock_denonavr()
        fr = _make_fr()
        engine_mock = MagicMock()
        engine_mock.measure.return_value = fr
        store = SessionStore(db_path=tmp_path / "test.db")

        with (
            patch.dict(sys.modules, {"sounddevice": sd_mock, "denonavr": denonavr_mock}),
            patch("calibrate.web._load_config", return_value=_make_config()),
            patch("calibrate.web.MeasurementEngine", return_value=engine_mock),
            patch("calibrate.web.SessionStore", return_value=store),
        ):
            resp = client.post("/api/measure", json={})

        assert resp.status_code == 200
        # Verify label stored as "headless"
        session = store.get_session(1)
        assert session is not None
        assert session.label == "headless"


# ── POST /api/sessions/average ──────────────────────────────────────────────


class TestSessionsAverage:
    def test_average_happy_path(self, seeded_store: SessionStore, client: TestClient) -> None:
        with patch("calibrate.web.SessionStore", return_value=seeded_store):
            resp = client.post("/api/sessions/average", json={"session_ids": [1, 2]})

        assert resp.status_code == 200
        data = resp.json()
        assert data["n_positions"] == 2
        assert len(data["frequencies_hz"]) == 7
        assert len(data["spl_dbfs"]) == 7
        assert len(data["spl_variance"]) == 7

    def test_average_session_not_found(self, db_store: SessionStore, client: TestClient) -> None:
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.post("/api/sessions/average", json={"session_ids": [999, 1000]})

        assert resp.status_code == 404
        assert "Session #999" in resp.json()["detail"]

    def test_average_fewer_than_two(self, db_store: SessionStore, client: TestClient) -> None:
        """Only one valid session should return 422."""
        fr = _make_fr()
        db_store.save_measurement(fr, label="only-one")
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.post("/api/sessions/average", json={"session_ids": [1]})

        assert resp.status_code == 422
        assert "Fewer than 2" in resp.json()["detail"]

    def test_average_mismatched_lengths(self, db_store: SessionStore, client: TestClient) -> None:
        """Sessions with different frequency array lengths return 422."""
        fr1 = _make_fr(freqs=[20.0, 30.0, 50.0], spl=[70.0, 75.0, 80.0])
        fr2 = _make_fr(freqs=[20.0, 30.0], spl=[72.0, 77.0])
        db_store.save_measurement(fr1, label="a")
        db_store.save_measurement(fr2, label="b")

        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.post("/api/sessions/average", json={"session_ids": [1, 2]})

        assert resp.status_code == 422
        assert "different frequency array lengths" in resp.json()["detail"]

    def test_average_incompatible_frequencies(self, db_store: SessionStore, client: TestClient) -> None:
        """Sessions with same length but different freq values return 422."""
        fr1 = _make_fr(freqs=[20.0, 30.0, 50.0], spl=[70.0, 75.0, 80.0])
        fr2 = _make_fr(freqs=[25.0, 35.0, 55.0], spl=[72.0, 77.0, 82.0])
        db_store.save_measurement(fr1, label="a")
        db_store.save_measurement(fr2, label="b")

        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.post("/api/sessions/average", json={"session_ids": [1, 2]})

        assert resp.status_code == 422
        assert "incompatible frequency ranges" in resp.json()["detail"]


# ── GET /api/sessions ───────────────────────────────────────────────────────


class TestListSessions:
    def test_empty_list(self, db_store: SessionStore, client: TestClient) -> None:
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/sessions")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_sessions_with_data(self, seeded_store: SessionStore, client: TestClient) -> None:
        with (
            patch("calibrate.web.SessionStore", return_value=seeded_store),
            patch("calibrate.analysis.harman_rms", return_value=3.5),
        ):
            resp = client.get("/api/sessions")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    def test_harman_delta_present(self, seeded_store: SessionStore, client: TestClient) -> None:
        with (
            patch("calibrate.web.SessionStore", return_value=seeded_store),
            patch("calibrate.analysis.harman_rms", return_value=4.2),
        ):
            resp = client.get("/api/sessions")

        data = resp.json()
        for session in data:
            assert "harman_delta_db" in session
            assert session["harman_delta_db"] == 4.2

    def test_harman_delta_none_on_error(self, seeded_store: SessionStore, client: TestClient) -> None:
        """If harman_rms raises, harman_delta_db is None (graceful degradation)."""
        with (
            patch("calibrate.web.SessionStore", return_value=seeded_store),
            patch("calibrate.analysis.harman_rms", side_effect=ValueError("bad data")),
        ):
            resp = client.get("/api/sessions")

        data = resp.json()
        for session in data:
            assert session["harman_delta_db"] is None

    def test_session_fields(self, seeded_store: SessionStore, client: TestClient) -> None:
        """Verify all expected fields are present in session list entries."""
        with (
            patch("calibrate.web.SessionStore", return_value=seeded_store),
            patch("calibrate.analysis.harman_rms", return_value=2.0),
        ):
            resp = client.get("/api/sessions")

        entry = resp.json()[0]
        expected_keys = {"id", "timestamp", "label", "peak_spl", "freq_at_peak",
                         "n_freqs", "has_end_fr", "harman_delta_db"}
        assert set(entry.keys()) == expected_keys


# ── GET /api/sessions/{session_id} ──────────────────────────────────────────


class TestGetSession:
    def test_session_detail(self, seeded_store: SessionStore, client: TestClient) -> None:
        with patch("calibrate.web.SessionStore", return_value=seeded_store):
            resp = client.get("/api/sessions/1")

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 1
        assert data["label"] == "position-1"
        assert data["start_fr"] is not None
        assert "frequencies" in data["start_fr"]
        assert "spl" in data["start_fr"]
        assert data["end_fr"] is None

    def test_session_not_found(self, db_store: SessionStore, client: TestClient) -> None:
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/sessions/999")

        assert resp.status_code == 404
        assert "Session #999" in resp.json()["detail"]

    def test_session_with_end_fr(self, db_store: SessionStore, client: TestClient) -> None:
        """Session with end_fr populated returns both FR objects."""
        fr_start = _make_fr()
        fr_end = _make_fr(spl=[65.0, 70.0, 75.0, 74.0, 73.0, 70.0, 66.0])
        sid = db_store.save_measurement(fr_start, label="calibrated")
        db_store.update_end_fr(sid, fr_end)

        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get(f"/api/sessions/{sid}")

        data = resp.json()
        assert data["start_fr"] is not None
        assert data["end_fr"] is not None
        assert data["end_fr"]["spl"][0] == 65.0


# ── GET /api/runs ───────────────────────────────────────────────────────────


class TestListRuns:
    def test_empty_runs(self, db_store: SessionStore, client: TestClient) -> None:
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/runs")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_runs_with_data(self, db_store: SessionStore, client: TestClient) -> None:
        """Insert a run directly via SQL and verify it appears."""
        with db_store._connect() as conn:
            conn.execute(
                "INSERT INTO calibration_runs (timestamp, recipe_name, target, converged) "
                "VALUES ('2025-01-15T12:00:00Z', 'default', 'harman', 1)"
            )

        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/runs")

        data = resp.json()
        assert len(data) == 1
        assert data[0]["target"] == "harman"
        assert data[0]["converged"] == 1

    def test_runs_limit_param(self, db_store: SessionStore, client: TestClient) -> None:
        """The limit query param restricts results."""
        with db_store._connect() as conn:
            for i in range(5):
                conn.execute(
                    "INSERT INTO calibration_runs (timestamp, recipe_name, target, converged) "
                    f"VALUES ('2025-01-1{i}T12:00:00Z', 'default', 'harman', 1)"
                )

        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/runs?limit=2")

        assert len(resp.json()) == 2


# ── GET /api/runs/{run_id} ──────────────────────────────────────────────────


class TestGetRunDetail:
    def test_run_detail(self, db_store: SessionStore, client: TestClient) -> None:
        with db_store._connect() as conn:
            conn.execute(
                "INSERT INTO calibration_runs (timestamp, recipe_name, target, converged) "
                "VALUES ('2025-01-15T12:00:00Z', 'default', 'harman', 1)"
            )

        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/runs/1")

        assert resp.status_code == 200
        data = resp.json()
        assert data["target"] == "harman"
        assert data["converged"] == 1

    def test_run_not_found(self, db_store: SessionStore, client: TestClient) -> None:
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/runs/999")

        assert resp.status_code == 404
        assert "Run #999" in resp.json()["detail"]


# ── GET /api/status ─────────────────────────────────────────────────────────


class TestSystemStatus:
    def _setup_mocks(self):
        """Return (config, denonavr_mock, sd_mock)."""
        cfg = _make_config()

        # denonavr mock
        denonavr_mock = MagicMock()
        receiver = MagicMock()
        receiver.model_name = "X3800H"
        receiver.input_func = "CD"
        receiver.volume = -25.0
        receiver.async_setup = AsyncMock()
        receiver.async_update = AsyncMock()
        denonavr_mock.DenonAVR.return_value = receiver

        # sounddevice mock
        sd_mock = MagicMock()
        sd_mock.query_devices.return_value = [
            {"name": "UMIK-1", "max_input_channels": 1},
        ]

        return cfg, denonavr_mock, sd_mock

    def test_status_all_connected(self, db_store: SessionStore, client: TestClient) -> None:
        cfg, denonavr_mock, sd_mock = self._setup_mocks()

        # httpx mock for miniDSP
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"master": {"preset": 0, "source": "USB"}}

        with (
            patch.dict(sys.modules, {"denonavr": denonavr_mock, "sounddevice": sd_mock}),
            patch("calibrate.web._load_config", return_value=cfg),
            patch("calibrate.web.SessionStore", return_value=db_store),
            patch("calibrate.web.httpx.AsyncClient") as httpx_cls,
        ):
            # Set up the async context manager for httpx
            httpx_instance = AsyncMock()
            httpx_instance.get.return_value = mock_response
            httpx_cls.return_value.__aenter__ = AsyncMock(return_value=httpx_instance)
            httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            resp = client.get("/api/status")

        assert resp.status_code == 200
        data = resp.json()
        assert "devices" in data
        assert "last_run" in data
        assert data["last_run"] is None  # no runs in db_store

        # Should have Denon, miniDSP, and UMIK devices
        names = [d["name"] for d in data["devices"]]
        assert any("X3800H" in n or "Denon" in n for n in names)
        assert any("miniDSP" in n for n in names)
        assert any("UMIK" in n for n in names)

    def test_status_denon_offline(self, db_store: SessionStore, client: TestClient) -> None:
        cfg = _make_config()
        denonavr_mock = MagicMock()
        denonavr_mock.DenonAVR.side_effect = Exception("Connection refused")

        sd_mock = MagicMock()
        sd_mock.query_devices.return_value = []

        with (
            patch.dict(sys.modules, {"denonavr": denonavr_mock, "sounddevice": sd_mock}),
            patch("calibrate.web._load_config", return_value=cfg),
            patch("calibrate.web.SessionStore", return_value=db_store),
            patch("calibrate.web.httpx.AsyncClient") as httpx_cls,
        ):
            httpx_instance = AsyncMock()
            httpx_instance.get.side_effect = Exception("Connection refused")
            httpx_cls.return_value.__aenter__ = AsyncMock(return_value=httpx_instance)
            httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            resp = client.get("/api/status")

        assert resp.status_code == 200
        data = resp.json()
        # At least one device should show connected=False
        disconnected = [d for d in data["devices"] if not d["connected"]]
        assert len(disconnected) >= 1

    def test_status_no_sounddevice(self, db_store: SessionStore, client: TestClient) -> None:
        """When sounddevice is not installed, UMIK shows as unavailable."""
        cfg = _make_config()
        denonavr_mock = MagicMock()
        denonavr_mock.DenonAVR.side_effect = Exception("offline")

        # Make sounddevice import fail
        original_sd = sys.modules.get("sounddevice")
        sys.modules["sounddevice"] = None  # type: ignore[assignment]

        try:
            with (
                patch.dict(sys.modules, {"denonavr": denonavr_mock}),
                patch("calibrate.web._load_config", return_value=cfg),
                patch("calibrate.web.SessionStore", return_value=db_store),
                patch("calibrate.web.httpx.AsyncClient") as httpx_cls,
            ):
                httpx_instance = AsyncMock()
                httpx_instance.get.side_effect = Exception("offline")
                httpx_cls.return_value.__aenter__ = AsyncMock(return_value=httpx_instance)
                httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

                resp = client.get("/api/status")
        finally:
            if original_sd is not None:
                sys.modules["sounddevice"] = original_sd
            else:
                sys.modules.pop("sounddevice", None)

        data = resp.json()
        umik_devices = [d for d in data["devices"] if "UMIK" in d["name"]]
        assert len(umik_devices) == 1
        assert umik_devices[0]["connected"] is False
        assert "sounddevice" in umik_devices[0]["detail"]

    def test_status_with_last_run(self, db_store: SessionStore, client: TestClient) -> None:
        """When a run exists, last_run is populated."""
        with db_store._connect() as conn:
            conn.execute(
                "INSERT INTO calibration_runs (timestamp, recipe_name, target, converged) "
                "VALUES ('2025-01-15T12:00:00Z', 'default', 'harman', 1)"
            )

        cfg = _make_config(denon_host="")  # skip Denon check
        sd_mock = MagicMock()
        sd_mock.query_devices.return_value = []

        with (
            patch.dict(sys.modules, {"sounddevice": sd_mock}),
            patch("calibrate.web._load_config", return_value=cfg),
            patch("calibrate.web.SessionStore", return_value=db_store),
            patch("calibrate.web.httpx.AsyncClient") as httpx_cls,
        ):
            httpx_instance = AsyncMock()
            httpx_instance.get.side_effect = Exception("offline")
            httpx_cls.return_value.__aenter__ = AsyncMock(return_value=httpx_instance)
            httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            resp = client.get("/api/status")

        data = resp.json()
        assert data["last_run"] is not None
        assert data["last_run"]["target"] == "harman"


# ── _load_config error ──────────────────────────────────────────────────────


class TestLoadConfigError:
    def test_missing_config_503(self, client: TestClient) -> None:
        """Endpoints that call _load_config with no config file return 503."""
        with patch("calibrate.web.CONFIG_PATH", Path("/nonexistent/config.yaml")):
            resp = client.get("/api/status")

        assert resp.status_code == 503
        assert "No config" in resp.json()["detail"]
