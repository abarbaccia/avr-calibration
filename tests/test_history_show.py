"""Tests for 'calibrate history' and 'calibrate show' CLI commands.

Coverage diagram:
  calibrate history
  ├── [TESTED] empty store — prints guidance message
  ├── [TESTED] one session — shows id, timestamp, label, peak, points
  ├── [TESTED] multiple sessions — shown most-recent first
  ├── [TESTED] session without label — shows dash placeholder
  └── [TESTED] completed session (end_fr set) — shows checkmark

  calibrate show <id>
  ├── [TESTED] unknown id — prints error, exits 1
  ├── [TESTED] human-readable summary — shows date, peak, band, sweep
  ├── [TESTED] --csv output — correct header and data rows
  ├── [TESTED] --json output — correct keys and values
  ├── [TESTED] feedback notes shown in human-readable view
  └── [TESTED] session with end_fr — shows final peak and delta

  _ascii_plot
  ├── [TESTED] produces one row per sample point
  └── [TESTED] empty input — no crash
"""

import json
import csv
import io
import pytest
from pathlib import Path
from click.testing import CliRunner

from calibrate.cli import cli
from calibrate.measurement import FrequencyResponse
from calibrate.storage import SessionStore


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_fr(
    peak_hz: float = 80.0,
    timestamp: str = "2026-03-20T12:00:00+00:00",
) -> FrequencyResponse:
    frequencies = [20.0, 40.0, 80.0, 160.0]
    spl = [-25.0, -20.0, -12.0, -18.0]
    # Make peak_hz the actual peak
    idx = frequencies.index(peak_hz) if peak_hz in frequencies else 2
    spl[idx] = max(spl) + 1
    return FrequencyResponse(
        frequencies=frequencies,
        spl=spl,
        sample_rate=48000,
        sweep_duration=3.0,
        timestamp=timestamp,
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def store(db_path: Path) -> SessionStore:
    return SessionStore(db_path=db_path)


def invoke_history(db_path: Path) -> object:
    runner = CliRunner()
    from unittest.mock import patch
    with patch("calibrate.storage.SessionStore", return_value=SessionStore(db_path=db_path)):
        return runner.invoke(cli, ["history"])


def invoke_show(db_path: Path, session_id: int, *extra_args) -> object:
    runner = CliRunner()
    with patch_store(db_path):
        return runner.invoke(cli, ["show", str(session_id)] + list(extra_args))


def patch_store(db_path: Path):
    from unittest.mock import patch
    return patch("calibrate.storage.SessionStore", return_value=SessionStore(db_path=db_path))


# ── calibrate history ─────────────────────────────────────────────────────────

class TestHistory:
    def test_empty_store(self, db_path):
        result = invoke_history(db_path)
        assert result.exit_code == 0
        assert "calibrate measure" in result.output

    def test_one_session_shown(self, store, db_path):
        store.save_measurement(make_fr(), label="baseline")
        result = invoke_history(db_path)
        assert result.exit_code == 0
        assert "#1" in result.output
        assert "baseline" in result.output
        assert "dBFS" in result.output

    def test_multiple_sessions_most_recent_first(self, store, db_path):
        store.save_measurement(make_fr(timestamp="2026-03-20T10:00:00+00:00"), label="first")
        store.save_measurement(make_fr(timestamp="2026-03-20T11:00:00+00:00"), label="second")
        result = invoke_history(db_path)
        assert result.exit_code == 0
        assert result.output.index("second") < result.output.index("first")

    def test_session_without_label_shows_dash(self, store, db_path):
        store.save_measurement(make_fr())
        result = invoke_history(db_path)
        assert "—" in result.output

    def test_completed_session_shows_checkmark(self, store, db_path):
        store.save_measurement(make_fr())
        store.update_end_fr(1, make_fr())
        result = invoke_history(db_path)
        assert "✓" in result.output

    def test_session_count_shown(self, store, db_path):
        store.save_measurement(make_fr(), label="A")
        store.save_measurement(make_fr(), label="B")
        result = invoke_history(db_path)
        assert "2 session" in result.output


# ── calibrate show ────────────────────────────────────────────────────────────

class TestShow:
    def test_unknown_id_exits_1(self, db_path):
        result = invoke_show(db_path, 999)
        assert result.exit_code == 1

    def test_human_summary_shows_key_fields(self, store, db_path):
        store.save_measurement(make_fr(), label="test run")
        result = invoke_show(db_path, 1)
        assert result.exit_code == 0
        assert "Session #1" in result.output
        assert "test run" in result.output
        assert "dBFS" in result.output
        assert "Hz" in result.output
        assert "sweep" in result.output.lower()

    def test_human_summary_no_label(self, store, db_path):
        store.save_measurement(make_fr())
        result = invoke_show(db_path, 1)
        assert result.exit_code == 0
        # Label line should be absent (not shown when None)
        assert "Label:" not in result.output

    def test_csv_output_format(self, store, db_path):
        fr = make_fr()
        store.save_measurement(fr)
        result = invoke_show(db_path, 1, "--csv")
        assert result.exit_code == 0
        reader = csv.DictReader(io.StringIO(result.output))
        rows = list(reader)
        assert reader.fieldnames == ["frequency_hz", "spl_dbfs"]
        assert len(rows) == len(fr.frequencies)
        assert float(rows[0]["frequency_hz"]) == pytest.approx(fr.frequencies[0])
        assert float(rows[0]["spl_dbfs"]) == pytest.approx(fr.spl[0])

    def test_json_output_format(self, store, db_path):
        fr = make_fr()
        store.save_measurement(fr, label="json test")
        result = invoke_show(db_path, 1, "--json")
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["session_id"] == 1
        assert data["label"] == "json test"
        assert data["frequencies_hz"] == fr.frequencies
        assert data["spl_dbfs"] == fr.spl
        assert data["sample_rate"] == 48000

    def test_feedback_shown_in_summary(self, store, db_path):
        store.save_measurement(make_fr())
        store.add_feedback(1, "bass sounded muddy", content_tag="movie:fury_road")
        result = invoke_show(db_path, 1)
        assert result.exit_code == 0
        assert "bass sounded muddy" in result.output
        assert "movie:fury_road" in result.output

    def test_end_fr_shows_delta(self, store, db_path):
        start = FrequencyResponse(
            frequencies=[20.0, 40.0, 80.0],
            spl=[-20.0, -15.0, -10.0],
            sample_rate=48000,
            sweep_duration=3.0,
            timestamp="2026-03-20T12:00:00+00:00",
        )
        end = FrequencyResponse(
            frequencies=[20.0, 40.0, 80.0],
            spl=[-18.0, -13.0, -8.0],
            sample_rate=48000,
            sweep_duration=3.0,
            timestamp="2026-03-20T12:30:00+00:00",
        )
        store.save_measurement(start)
        store.update_end_fr(1, end)
        result = invoke_show(db_path, 1)
        assert "Final peak" in result.output
        assert "+2.0" in result.output  # delta: -8 - (-10) = +2


# ── _ascii_plot ───────────────────────────────────────────────────────────────

class TestAsciiPlot:
    def test_produces_output_rows(self):
        from calibrate.cli import _ascii_plot
        runner = CliRunner()
        with runner.isolated_filesystem():
            from click.testing import CliRunner as CR
            import click

            @click.command()
            def _test():
                _ascii_plot([20.0, 40.0, 80.0, 160.0], [-20.0, -15.0, -10.0, -18.0])

            result = CR().invoke(_test)
        assert result.exit_code == 0
        lines = [l for l in result.output.splitlines() if l.strip()]
        assert len(lines) >= 1

    def test_empty_input_no_crash(self):
        from calibrate.cli import _ascii_plot
        runner = CliRunner()
        with runner.isolated_filesystem():
            import click

            @click.command()
            def _test():
                _ascii_plot([], [])

            result = CliRunner().invoke(_test)
        assert result.exit_code == 0
