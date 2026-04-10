"""Tests for calibrate/cli.py — Click CLI entry point.

Covers the check, measure, history, show, web, and signal_path commands.
All hardware/network calls are mocked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from calibrate.cli import cli
from calibrate.config import Config
from calibrate.preflight import CheckResult


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_event_loop():
    """Ensure a fresh event loop before each test and restore/close after.

    The CLI 'check' and 'signal-path' commands call asyncio.run() which closes
    the current event loop.  This fixture creates a new loop before each test
    and sets a fresh one after, so subsequent tests still have a working loop.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    try:
        loop.close()
    except Exception:
        pass
    # Set a fresh loop (not None) so subsequent tests can use asyncio.get_event_loop()
    asyncio.set_event_loop(asyncio.new_event_loop())


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_config_file(tmp_path: Path, content: dict | None = None) -> Path:
    """Write a minimal valid config YAML and return its path."""
    p = tmp_path / "config.yaml"
    data = content or {
        "denon": {"host": "192.168.1.100"},
        "minidsp": {"host": "localhost", "port": 5380},
        "mic": {"name": "UMIK"},
    }
    p.write_text(yaml.dump(data))
    return p


# ── calibrate check ────────────────────────────────────────────────────────────

class TestCheckCommand:

    def test_check_no_config_creates_template(self, tmp_path):
        """Missing config → creates template and exits with code 1 (lines 35-41)."""
        missing = tmp_path / "missing.yaml"
        runner = CliRunner()
        with patch("calibrate.cli.CONFIG_PATH", missing):
            result = runner.invoke(cli, ["check"])
        assert result.exit_code == 1
        assert missing.exists()
        assert "template" in result.output.lower() or "Edit" in result.output

    def test_check_all_passed(self, tmp_path):
        """All checks pass → exit 0, success message."""
        cfg_path = _make_config_file(tmp_path)
        checks = [
            CheckResult(name="minidsp", passed=True, detail="OK"),
            CheckResult(name="mic", passed=True, detail="Found"),
        ]
        with (
            patch("calibrate.cli.CONFIG_PATH", cfg_path),
            patch("calibrate.cli.PreflightChecker") as MockChecker,
        ):
            MockChecker.return_value.run_all = AsyncMock(return_value=checks)
            result = CliRunner().invoke(cli, ["check"])
        assert result.exit_code == 0
        assert "All checks passed" in result.output

    def test_check_some_failures(self, tmp_path):
        """Some checks fail → exit 1, failure count in output (lines 65-69)."""
        cfg_path = _make_config_file(tmp_path)
        checks = [
            CheckResult(name="minidsp", passed=True, detail="OK"),
            CheckResult(name="mic", passed=False, detail="", error="UMIK not found"),
        ]
        with (
            patch("calibrate.cli.CONFIG_PATH", cfg_path),
            patch("calibrate.cli.PreflightChecker") as MockChecker,
        ):
            MockChecker.return_value.run_all = AsyncMock(return_value=checks)
            result = CliRunner().invoke(cli, ["check"])
        assert result.exit_code == 1
        assert "1 of 2 checks failed" in result.output

    def test_check_failure_shows_error(self, tmp_path):
        """Failed check with error string → error shown in output (lines 60-62)."""
        cfg_path = _make_config_file(tmp_path)
        checks = [
            CheckResult(name="minidsp", passed=False, detail="", error="Connection refused"),
        ]
        with (
            patch("calibrate.cli.CONFIG_PATH", cfg_path),
            patch("calibrate.cli.PreflightChecker") as MockChecker,
        ):
            MockChecker.return_value.run_all = AsyncMock(return_value=checks)
            result = CliRunner().invoke(cli, ["check"])
        assert "Connection refused" in result.output

    def test_check_custom_config_path(self, tmp_path):
        """--config flag uses the specified path."""
        cfg_path = _make_config_file(tmp_path)
        checks = [CheckResult(name="config", passed=True, detail="OK")]
        with patch("calibrate.cli.PreflightChecker") as MockChecker:
            MockChecker.return_value.run_all = AsyncMock(return_value=checks)
            result = CliRunner().invoke(cli, ["check", "--config", str(cfg_path)])
        assert result.exit_code == 0


# ── calibrate signal-path show ─────────────────────────────────────────────────

class TestSignalPathShow:

    def test_show_no_config(self, tmp_path):
        """Missing config → error message, exit 1 (line 261)."""
        missing = tmp_path / "missing.yaml"
        with patch("calibrate.cli.CONFIG_PATH", missing):
            result = CliRunner().invoke(cli, ["signal-path", "show"])
        assert result.exit_code == 1
        assert "No config found" in result.output

    def test_show_with_config_live_state(self, tmp_path):
        """Happy path: shows configured and live device state."""
        cfg_path = _make_config_file(tmp_path, {
            "denon": {"host": "192.168.1.100"},
            "minidsp": {
                "host": "localhost",
                "port": 5380,
                "signal_path": {"source": "Analog", "preset": 0},
            },
            "mic": {"name": "UMIK"},
        })
        mock_driver = AsyncMock()
        mock_driver.get_state.return_value = {"source": "Analog", "preset": 0, "volume": -30.0, "mute": False}
        with (
            patch("calibrate.cli.CONFIG_PATH", cfg_path),
            patch("calibrate.cli.load_dsp_driver", return_value=mock_driver),
        ):
            result = CliRunner().invoke(cli, ["signal-path", "show"])
        assert result.exit_code == 0
        assert "Analog" in result.output

    def test_show_driver_error_shows_warning(self, tmp_path):
        """DriverError → yellow warning, no crash."""
        from calibrate.drivers.base import DriverError
        cfg_path = _make_config_file(tmp_path)
        mock_driver = AsyncMock()
        mock_driver.get_state.side_effect = DriverError("daemon not running")
        with (
            patch("calibrate.cli.CONFIG_PATH", cfg_path),
            patch("calibrate.cli.load_dsp_driver", return_value=mock_driver),
        ):
            result = CliRunner().invoke(cli, ["signal-path", "show"])
        assert result.exit_code == 0
        assert "Cannot read device state" in result.output

    def test_show_generic_exception_shows_warning(self, tmp_path):
        """Generic exception → cannot reach DSP warning."""
        cfg_path = _make_config_file(tmp_path)
        mock_driver = AsyncMock()
        mock_driver.get_state.side_effect = OSError("refused")
        with (
            patch("calibrate.cli.CONFIG_PATH", cfg_path),
            patch("calibrate.cli.load_dsp_driver", return_value=mock_driver),
        ):
            result = CliRunner().invoke(cli, ["signal-path", "show"])
        assert result.exit_code == 0
        assert "Cannot reach" in result.output

    def test_show_routing_displayed(self, tmp_path):
        """Routing config is displayed."""
        cfg_path = _make_config_file(tmp_path, {
            "denon": {"host": None},
            "minidsp": {
                "host": "localhost",
                "port": 5380,
                "signal_path": {
                    "source": "Analog",
                    "preset": 0,
                    "routing": [{"input": 0, "outputs": [0, 1]}],
                },
            },
            "mic": {"name": "UMIK"},
        })
        mock_driver = AsyncMock()
        mock_driver.get_state.return_value = {"source": "Analog", "preset": 0, "volume": -30.0, "mute": False}
        with (
            patch("calibrate.cli.CONFIG_PATH", cfg_path),
            patch("calibrate.cli.load_dsp_driver", return_value=mock_driver),
        ):
            result = CliRunner().invoke(cli, ["signal-path", "show"])
        assert "Routing" in result.output
        assert "Input 0" in result.output

    def test_show_no_routing_configured(self, tmp_path):
        """No routing in config → '— not configured —' shown."""
        cfg_path = _make_config_file(tmp_path)
        mock_driver = AsyncMock()
        mock_driver.get_state.return_value = {"source": "Analog", "preset": 0, "volume": -30.0, "mute": False}
        with (
            patch("calibrate.cli.CONFIG_PATH", cfg_path),
            patch("calibrate.cli.load_dsp_driver", return_value=mock_driver),
        ):
            result = CliRunner().invoke(cli, ["signal-path", "show"])
        assert "not configured" in result.output

    def test_show_muted_volume(self, tmp_path):
        """Muted device shows '(MUTED)' in output."""
        cfg_path = _make_config_file(tmp_path)
        mock_driver = AsyncMock()
        mock_driver.get_state.return_value = {"source": "USB", "preset": 1, "volume": -20.0, "mute": True}
        with (
            patch("calibrate.cli.CONFIG_PATH", cfg_path),
            patch("calibrate.cli.load_dsp_driver", return_value=mock_driver),
        ):
            result = CliRunner().invoke(cli, ["signal-path", "show"])
        assert "MUTED" in result.output


# ── calibrate signal-path apply ────────────────────────────────────────────────

class TestSignalPathApply:

    def test_apply_no_config(self, tmp_path):
        """Missing config → error message, exit 1 (line 323)."""
        missing = tmp_path / "missing.yaml"
        with patch("calibrate.cli.CONFIG_PATH", missing):
            result = CliRunner().invoke(cli, ["signal-path", "apply"])
        assert result.exit_code == 1
        assert "No config found" in result.output

    def test_apply_nothing_to_apply(self, tmp_path):
        """No source, preset, or routing → informative message, exit 0 (lines 362-365)."""
        cfg_path = _make_config_file(tmp_path)
        with patch("calibrate.cli.CONFIG_PATH", cfg_path):
            result = CliRunner().invoke(cli, ["signal-path", "apply"])
        assert result.exit_code == 0
        assert "Nothing to apply" in result.output

    def test_apply_invalid_source(self, tmp_path):
        """Invalid --source → error message, exit 1 (lines 331-333)."""
        cfg_path = _make_config_file(tmp_path)
        with patch("calibrate.cli.CONFIG_PATH", cfg_path):
            result = CliRunner().invoke(cli, ["signal-path", "apply", "--source", "HDMI"])
        assert result.exit_code == 1
        assert "source must be one of" in result.output

    def test_apply_invalid_preset(self, tmp_path):
        """Out-of-range --preset → error message, exit 1 (lines 334-336)."""
        cfg_path = _make_config_file(tmp_path)
        with patch("calibrate.cli.CONFIG_PATH", cfg_path):
            result = CliRunner().invoke(cli, ["signal-path", "apply", "--preset", "99"])
        assert result.exit_code == 1
        assert "preset must be" in result.output

    def test_apply_preset_success(self, tmp_path):
        """Valid --preset → set_preset called, 'Done.' printed."""
        cfg_path = _make_config_file(tmp_path)
        mock_driver = AsyncMock()
        with (
            patch("calibrate.cli.CONFIG_PATH", cfg_path),
            patch("calibrate.cli.load_dsp_driver", return_value=mock_driver),
        ):
            result = CliRunner().invoke(cli, ["signal-path", "apply", "--preset", "1"])
        assert result.exit_code == 0
        assert "Done" in result.output
        mock_driver.set_preset.assert_called_once_with(1)

    def test_apply_source_success(self, tmp_path):
        """Valid --source → set_source called."""
        cfg_path = _make_config_file(tmp_path)
        mock_driver = AsyncMock()
        with (
            patch("calibrate.cli.CONFIG_PATH", cfg_path),
            patch("calibrate.cli.load_dsp_driver", return_value=mock_driver),
        ):
            result = CliRunner().invoke(cli, ["signal-path", "apply", "--source", "Analog"])
        assert result.exit_code == 0
        mock_driver.set_source.assert_called_once_with("Analog")

    def test_apply_with_routing_from_config(self, tmp_path):
        """Config with routing → set_routing called once with full routing dict."""
        cfg_path = _make_config_file(tmp_path, {
            "denon": {"host": None},
            "minidsp": {
                "host": "localhost",
                "port": 5380,
                "signal_path": {
                    "source": "Analog",
                    "preset": 0,
                    "routing": [{"input": 0, "outputs": [0, 1]}],
                },
            },
            "mic": {"name": "UMIK"},
        })
        mock_driver = AsyncMock()
        with (
            patch("calibrate.cli.CONFIG_PATH", cfg_path),
            patch("calibrate.cli.load_dsp_driver", return_value=mock_driver),
        ):
            result = CliRunner().invoke(cli, ["signal-path", "apply"])
        assert result.exit_code == 0
        mock_driver.set_routing.assert_called_once()

    def test_apply_driver_error_exits_1(self, tmp_path):
        """DriverError → red error message, exit 1."""
        from calibrate.drivers.base import DriverError
        cfg_path = _make_config_file(tmp_path)
        mock_driver = AsyncMock()
        mock_driver.set_preset.side_effect = DriverError("write failed")
        with (
            patch("calibrate.cli.CONFIG_PATH", cfg_path),
            patch("calibrate.cli.load_dsp_driver", return_value=mock_driver),
        ):
            result = CliRunner().invoke(cli, ["signal-path", "apply", "--preset", "2"])
        assert result.exit_code == 1
        assert "Error" in result.output

    def test_apply_generic_exception_exits_1(self, tmp_path):
        """Generic exception → red error message, exit 1."""
        cfg_path = _make_config_file(tmp_path)
        mock_driver = AsyncMock()
        mock_driver.set_source.side_effect = OSError("network unreachable")
        with (
            patch("calibrate.cli.CONFIG_PATH", cfg_path),
            patch("calibrate.cli.load_dsp_driver", return_value=mock_driver),
        ):
            result = CliRunner().invoke(cli, ["signal-path", "apply", "--source", "USB"])
        assert result.exit_code == 1
        assert "Error" in result.output
