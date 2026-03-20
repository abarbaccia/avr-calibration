"""Tests for calibrate.web and 'calibrate web' CLI command.

Coverage diagram:
  calibrate/web.py
  ├── [TESTED] GET / — returns 200 with HTML placeholder
  ├── [TESTED] GET /health — returns {"status": "ok"}
  └── [TESTED] response is HTML content-type

  calibrate web CLI command
  ├── [TESTED] --help works
  └── [TESTED] uvicorn.run called with correct host/port
"""

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from click.testing import CliRunner

from calibrate.web import app
from calibrate.cli import cli


# ── Web app ────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    return TestClient(app)


class TestWebApp:
    def test_index_returns_200(self, client):
        response = client.get("/")
        assert response.status_code == 200

    def test_index_returns_html(self, client):
        response = client.get("/")
        assert "text/html" in response.headers["content-type"]

    def test_index_contains_placeholder_text(self, client):
        response = client.get("/")
        assert "AVR Calibration" in response.text

    def test_health_returns_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


# ── calibrate web CLI command ──────────────────────────────────────────────

class TestWebCommand:
    def test_help(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["web", "--help"])
        assert result.exit_code == 0
        assert "--host" in result.output
        assert "--port" in result.output

    def test_starts_uvicorn_with_defaults(self):
        runner = CliRunner()
        with patch("uvicorn.run") as mock_run:
            runner.invoke(cli, ["web"])
            mock_run.assert_called_once_with(
                "calibrate.web:app",
                host="0.0.0.0",
                port=8000,
                reload=False,
            )

    def test_starts_uvicorn_with_custom_host_port(self):
        runner = CliRunner()
        with patch("uvicorn.run") as mock_run:
            runner.invoke(cli, ["web", "--host", "127.0.0.1", "--port", "9000"])
            mock_run.assert_called_once_with(
                "calibrate.web:app",
                host="127.0.0.1",
                port=9000,
                reload=False,
            )
