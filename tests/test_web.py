"""Tests for calibrate.web — observation deck dashboard.

Covers all endpoints:
  GET  /                     — HTML dashboard
  GET  /health               — health check
  GET  /api/version          — version info (mocked GHCR)
  POST /api/upgrade          — trigger upgrade file
  POST /api/sessions/average — average multiple sessions
  GET  /api/sessions         — list sessions with harman_delta_db + run_context
  GET  /api/sessions/{id}    — session detail
  GET  /api/sessions/overlay — overlay FR data
  GET  /api/runs             — list calibration runs with session_ids
  GET  /api/runs/{id}        — run detail with session_ids
  GET  /api/status           — system status
  GET  /api/activity         — activity timeline
  POST /api/feedback         — submit feedback
  GET  /api/feedback/{id}    — get feedback for session
  GET  /api/states           — list saved states
  POST /api/states           — save state
  GET  /api/states/{id}      — get state detail
  DELETE /api/states/{id}    — delete state
  GET  /api/dsp-state        — active DSP state
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
        with patch("calibrate.web.SessionStore", return_value=seeded_store):
            resp = client.get("/api/sessions")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    def test_harman_delta_none_without_target_curve(self, seeded_store: SessionStore, client: TestClient) -> None:
        """Sessions without a stored target_curve have harman_delta_db = None."""
        with patch("calibrate.web.SessionStore", return_value=seeded_store):
            resp = client.get("/api/sessions")

        data = resp.json()
        for session in data:
            assert "harman_delta_db" in session
            assert session["harman_delta_db"] is None

    def test_harman_delta_computed_from_stored_target(self, db_store: SessionStore, client: TestClient) -> None:
        """Session with stored target_curve returns a computed delta, not None."""
        tc = {"type": "harman", "reference_spl": 78.0, "band": [20.0, 200.0]}
        db_store.save_measurement(_make_fr(), label="calibrated", target_curve=tc)
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/sessions")

        data = resp.json()
        assert len(data) == 1
        assert data[0]["harman_delta_db"] is not None
        assert isinstance(data[0]["harman_delta_db"], float)

    def test_harman_delta_graceful_on_analysis_error(self, db_store: SessionStore, client: TestClient) -> None:
        """If rms_deviation raises for a stored target, harman_delta_db is None."""
        tc = {"type": "harman", "reference_spl": 78.0, "band": [20.0, 200.0]}
        db_store.save_measurement(_make_fr(), label="test", target_curve=tc)
        with (
            patch("calibrate.web.SessionStore", return_value=db_store),
            patch("calibrate.analysis.rms_deviation", side_effect=ValueError("bad data")),
        ):
            resp = client.get("/api/sessions")

        data = resp.json()
        assert data[0]["harman_delta_db"] is None

    def test_session_fields(self, seeded_store: SessionStore, client: TestClient) -> None:
        """Verify all expected fields are present in session list entries."""
        with patch("calibrate.web.SessionStore", return_value=seeded_store):
            resp = client.get("/api/sessions")

        entry = resp.json()[0]
        expected_keys = {"id", "timestamp", "label", "peak_spl", "freq_at_peak",
                         "n_freqs", "has_end_fr", "harman_delta_db", "run_context"}
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

    def test_session_detail_has_target_curve_field(self, db_store: SessionStore, client: TestClient) -> None:
        """Session detail includes target_curve field (null when not stored)."""
        db_store.save_measurement(_make_fr(), label="raw")
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/sessions/1")

        data = resp.json()
        assert "target_curve" in data
        assert data["target_curve"] is None

    def test_session_detail_target_curve_returned_when_stored(self, db_store: SessionStore, client: TestClient) -> None:
        """Session detail returns stored target_curve when present."""
        tc = {"type": "harman", "reference_spl": 72.5, "band": [20.0, 200.0]}
        sid = db_store.save_measurement(_make_fr(), label="calibrated", target_curve=tc)
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get(f"/api/sessions/{sid}")

        data = resp.json()
        assert data["target_curve"] == tc


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


# ── Saved states API ────────────────────────────────────────────────────────


class TestSavedStatesAPI:
    def test_list_states_empty(self, client: TestClient, db_store: SessionStore) -> None:
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/states")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_save_state(self, client: TestClient, db_store: SessionStore) -> None:
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.post("/api/states", json={"name": "Test State"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "saved"
        assert data["id"] > 0

    def test_save_and_list(self, client: TestClient, db_store: SessionStore) -> None:
        with patch("calibrate.web.SessionStore", return_value=db_store):
            client.post("/api/states", json={"name": "State A", "target_curve": "harman"})
            client.post("/api/states", json={"name": "State B", "rms_deviation": 2.1})
            resp = client.get("/api/states")
        states = resp.json()
        assert len(states) == 2
        assert states[0]["name"] == "State B"
        assert states[1]["name"] == "State A"

    def test_get_state_detail(self, client: TestClient, db_store: SessionStore) -> None:
        with patch("calibrate.web.SessionStore", return_value=db_store):
            save_resp = client.post("/api/states", json={
                "name": "Full",
                "eq_filters": [{"freq": 40, "gain_db": -3}],
                "delays": {"0": 1.5},
                "target_curve": "harman",
            })
            state_id = save_resp.json()["id"]
            resp = client.get(f"/api/states/{state_id}")
        data = resp.json()
        assert data["name"] == "Full"
        assert data["eq_filters"] == [{"freq": 40, "gain_db": -3}]
        assert data["delays"] == {"0": 1.5}

    def test_get_state_not_found(self, client: TestClient, db_store: SessionStore) -> None:
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/states/999")
        assert resp.status_code == 404

    def test_delete_state(self, client: TestClient, db_store: SessionStore) -> None:
        with patch("calibrate.web.SessionStore", return_value=db_store):
            save_resp = client.post("/api/states", json={"name": "Doomed"})
            state_id = save_resp.json()["id"]
            resp = client.delete(f"/api/states/{state_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

    def test_delete_state_not_found(self, client: TestClient, db_store: SessionStore) -> None:
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.delete("/api/states/999")
        assert resp.status_code == 404


# ── Overlay API ──────────────────────────────────────────────────────────────


class TestOverlayAPI:
    def test_overlay_sessions(self, client: TestClient, seeded_store: SessionStore) -> None:
        with patch("calibrate.web.SessionStore", return_value=seeded_store):
            resp = client.get("/api/sessions/overlay?ids=1,2")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["id"] == 1
        assert "frequencies" in data[0]
        assert "spl" in data[0]
        assert "label" in data[0]

    def test_overlay_invalid_ids(self, client: TestClient, db_store: SessionStore) -> None:
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/sessions/overlay?ids=abc")
        assert resp.status_code == 422

    def test_overlay_missing_sessions(self, client: TestClient, db_store: SessionStore) -> None:
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/sessions/overlay?ids=999")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_overlay_capped_at_6(self, client: TestClient, seeded_store: SessionStore) -> None:
        """Only first 6 ids are processed."""
        with patch("calibrate.web.SessionStore", return_value=seeded_store):
            resp = client.get("/api/sessions/overlay?ids=1,2,1,2,1,2,1,2")
        data = resp.json()
        assert len(data) <= 6


# ── GET /api/dsp-state ─────────────────────────────────────────────────────


class TestDspState:
    def test_empty_state(self, client: TestClient, db_store: SessionStore) -> None:
        cfg = _make_config()
        with patch("calibrate.web.SessionStore", return_value=db_store), \
             patch("calibrate.web._load_config", return_value=cfg):
            resp = client.get("/api/dsp-state")
        data = resp.json()
        assert data["active"] is False

    def test_with_persisted_state(self, client: TestClient, db_store: SessionStore) -> None:
        db_store.set_active_dsp("output_eq_1", {"filters": [{"freq": 47, "gain_db": -3, "q": 3.4, "type": "peaking"}]})
        db_store.set_active_dsp("delay_1", {"delay_ms": 12.2})
        db_store.set_active_dsp("polarity_2", {"inverted": True})
        db_store.set_active_dsp("gain_1", {"gain_db": 3.2})
        db_store.set_active_dsp("input_eq", {"filters": [{"freq": 18, "gain_db": 0, "q": 0.707, "type": "hpf"}]})

        cfg = Config({
            "denon": {"host": "192.168.1.100"},
            "minidsp": {
                "host": "localhost", "port": 5380,
                "output_slots": [
                    {"index": 1, "label": "Sub 1", "type": "sub"},
                    {"index": 2, "label": "Sub 2", "type": "sub"},
                    {"index": 3, "label": "Shaker", "type": "shaker"},
                ],
            },
            "mic": {"name": "UMIK"},
            "measurement": {"denon_sweep_input": "CD", "denon_sweep_volume": -25.0},
        })
        with patch("calibrate.web.SessionStore", return_value=db_store), \
             patch("calibrate.web._load_config", return_value=cfg):
            resp = client.get("/api/dsp-state")
        data = resp.json()
        assert data["active"] is True
        assert data["outputs"]["1"]["delay_ms"] == 12.2
        assert data["outputs"]["1"]["gain_db"] == 3.2
        assert data["outputs"]["1"]["eq"][0]["freq"] == 47
        assert data["outputs"]["2"]["polarity_inverted"] is True
        assert data["input_eq"]["filters"][0]["type"] == "hpf"


# ── GET /api/activity ─────────────────────────────────────────────────────


class TestActivity:
    def test_activity_empty(self, db_store: SessionStore, client: TestClient) -> None:
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/activity")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_activity_with_sessions(self, seeded_store: SessionStore, client: TestClient) -> None:
        with patch("calibrate.web.SessionStore", return_value=seeded_store):
            resp = client.get("/api/activity")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 2
        # Should contain measurement events
        types = [e["type"] for e in data]
        assert "measurement" in types

    def test_activity_with_runs(self, db_store: SessionStore, client: TestClient) -> None:
        """Runs appear as run_complete events in the timeline."""
        with db_store._connect() as conn:
            conn.execute(
                "INSERT INTO calibration_runs (timestamp, recipe_name, target, converged, final_rms) "
                "VALUES ('2025-01-15T12:00:00Z', 'bass-cal', 'harman', 1, 2.1)"
            )
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/activity")
        data = resp.json()
        types = [e["type"] for e in data]
        assert "run_complete" in types

    def test_activity_with_dsp_changes(self, db_store: SessionStore, client: TestClient) -> None:
        """DSP state changes appear as eq_applied events."""
        db_store.set_active_dsp("output_eq_0", {
            "filters": [{"freq": 45, "gain_db": -3, "q": 4, "type": "peaking"}]
        })
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/activity")
        data = resp.json()
        types = [e["type"] for e in data]
        assert "eq_applied" in types

    def test_activity_limit(self, seeded_store: SessionStore, client: TestClient) -> None:
        with patch("calibrate.web.SessionStore", return_value=seeded_store):
            resp = client.get("/api/activity?limit=1")
        assert resp.status_code == 200
        assert len(resp.json()) <= 1


# ── POST /api/feedback ───────────────────────────────────────────────────────


class TestFeedback:
    def test_submit_feedback_up(self, db_store: SessionStore, client: TestClient) -> None:
        db_store.save_measurement(_make_fr(), label="test")
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.post("/api/feedback", json={
                "session_id": 1, "rating": "up", "text": "sounds great"
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "saved"
        assert "id" in data

    def test_submit_feedback_down(self, db_store: SessionStore, client: TestClient) -> None:
        db_store.save_measurement(_make_fr(), label="test")
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.post("/api/feedback", json={
                "session_id": 1, "rating": "down", "text": "too boomy at 50Hz"
            })
        assert resp.status_code == 200

    def test_submit_feedback_invalid_rating(self, db_store: SessionStore, client: TestClient) -> None:
        db_store.save_measurement(_make_fr(), label="test")
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.post("/api/feedback", json={
                "session_id": 1, "rating": "meh"
            })
        assert resp.status_code == 422
        assert "rating" in resp.json()["detail"]

    def test_submit_feedback_session_not_found(self, db_store: SessionStore, client: TestClient) -> None:
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.post("/api/feedback", json={
                "session_id": 999, "rating": "up"
            })
        assert resp.status_code == 404

    def test_submit_feedback_no_text(self, db_store: SessionStore, client: TestClient) -> None:
        """Feedback without text stores just the rating tag."""
        db_store.save_measurement(_make_fr(), label="test")
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.post("/api/feedback", json={
                "session_id": 1, "rating": "up"
            })
        assert resp.status_code == 200

    def test_get_feedback(self, db_store: SessionStore, client: TestClient) -> None:
        db_store.save_measurement(_make_fr(), label="test")
        db_store.add_feedback(1, "[up] sounds great", content_tag="up")
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/feedback/1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["content_tag"] == "up"

    def test_get_feedback_empty(self, db_store: SessionStore, client: TestClient) -> None:
        db_store.save_measurement(_make_fr(), label="test")
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/feedback/1")
        assert resp.status_code == 200
        assert resp.json() == []


# ── Run context in sessions ──────────────────────────────────────────────────


class TestRunContext:
    def test_sessions_include_run_context(self, db_store: SessionStore, client: TestClient) -> None:
        """Sessions created during a run include run_context."""
        # Create a run, then a session with the same timestamp
        with db_store._connect() as conn:
            conn.execute(
                "INSERT INTO calibration_runs (timestamp, recipe_name, target, converged) "
                "VALUES ('2025-01-15T12:00:00Z', 'bass-cal', 'harman', 1)"
            )
        # Session created at the same time as the run
        fr = _make_fr()
        fr_data = fr.__class__(
            frequencies=fr.frequencies, spl=fr.spl,
            sample_rate=fr.sample_rate, sweep_duration=fr.sweep_duration,
            timestamp="2025-01-15T12:05:00Z",
        )
        db_store.save_measurement(fr_data, label="baseline")

        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/sessions")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["run_context"] is not None
        assert data[0]["run_context"]["run_id"] == 1
        assert data[0]["run_context"]["recipe_name"] == "bass-cal"

    def test_adhoc_sessions_null_run_context(self, seeded_store: SessionStore, client: TestClient) -> None:
        """Sessions not during any run have run_context=None."""
        with patch("calibrate.web.SessionStore", return_value=seeded_store):
            resp = client.get("/api/sessions")
        data = resp.json()
        for session in data:
            assert session["run_context"] is None


# ── Runs include session_ids ─────────────────────────────────────────────────


class TestRunSessionIds:
    def test_runs_include_session_ids(self, db_store: SessionStore, client: TestClient) -> None:
        with db_store._connect() as conn:
            conn.execute(
                "INSERT INTO calibration_runs (timestamp, recipe_name, target, converged) "
                "VALUES ('2025-01-15T12:00:00Z', 'bass-cal', 'harman', 1)"
            )
        with patch("calibrate.web.SessionStore", return_value=db_store):
            resp = client.get("/api/runs")
        data = resp.json()
        assert len(data) == 1
        assert "session_ids" in data[0]
        assert isinstance(data[0]["session_ids"], list)
