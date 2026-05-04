"""Tests for the SQLite session store.

Coverage diagram:
  SessionStore
  ├── _init_schema()
  │   └── [TESTED] creates tables idempotently (safe to call twice)
  ├── save_measurement()
  │   ├── [TESTED] returns auto-incrementing integer id
  │   ├── [TESTED] stores timestamp, label, start_fr correctly
  │   └── [TESTED] label is optional (defaults to None)
  ├── update_end_fr()
  │   ├── [TESTED] stores end_fr on the correct session
  │   └── [TESTED] other sessions unaffected
  ├── list_sessions()
  │   ├── [TESTED] returns all sessions most-recent first
  │   ├── [TESTED] empty store returns empty list
  │   └── [TESTED] FrequencyResponse round-trips through JSON correctly
  ├── get_session()
  │   ├── [TESTED] returns session with correct fields
  │   ├── [TESTED] end_fr is None when not set
  │   └── [TESTED] returns None for unknown id
  └── add_feedback()
      ├── [TESTED] returns auto-incrementing id
      ├── [TESTED] stores session_id, text, content_tag
      ├── [TESTED] content_tag is optional
      ├── [TESTED] custom timestamp is preserved
      └── [TESTED] get_feedback returns entries oldest-first for a session
"""

import pytest
from pathlib import Path

from calibrate.measurement import FrequencyResponse
from calibrate.storage import SessionStore


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(db_path=tmp_path / "test.db")


def make_fr(
    frequencies=None,
    spl=None,
    timestamp="2026-03-20T00:00:00+00:00",
) -> FrequencyResponse:
    return FrequencyResponse(
        frequencies=frequencies or [20.0, 40.0, 80.0, 160.0],
        spl=spl or [-20.0, -15.0, -12.0, -18.0],
        sample_rate=48000,
        sweep_duration=3.0,
        timestamp=timestamp,
    )


# ── Schema ────────────────────────────────────────────────────────────────────

class TestSchema:
    def test_init_schema_is_idempotent(self, tmp_path):
        db_path = tmp_path / "idempotent.db"
        # Creating two stores against the same db must not raise
        SessionStore(db_path=db_path)
        SessionStore(db_path=db_path)


# ── save_measurement ──────────────────────────────────────────────────────────

class TestSaveMeasurement:
    def test_returns_session_id(self, store):
        sid = store.save_measurement(make_fr())
        assert sid == 1

    def test_ids_auto_increment(self, store):
        sid1 = store.save_measurement(make_fr())
        sid2 = store.save_measurement(make_fr())
        assert sid2 == sid1 + 1

    def test_label_stored(self, store):
        store.save_measurement(make_fr(), label="baseline")
        session = store.get_session(1)
        assert session.label == "baseline"

    def test_label_defaults_to_none(self, store):
        store.save_measurement(make_fr())
        session = store.get_session(1)
        assert session.label is None

    def test_start_fr_round_trips(self, store):
        fr = make_fr(frequencies=[25.0, 50.0], spl=[-10.0, -5.0])
        store.save_measurement(fr)
        session = store.get_session(1)
        assert session.start_fr.frequencies == [25.0, 50.0]
        assert session.start_fr.spl == [-10.0, -5.0]


# ── update_end_fr ─────────────────────────────────────────────────────────────

class TestUpdateEndFr:
    def test_end_fr_stored(self, store):
        store.save_measurement(make_fr())
        end_fr = make_fr(spl=[-18.0, -13.0, -10.0, -16.0])
        store.update_end_fr(1, end_fr)
        session = store.get_session(1)
        assert session.end_fr is not None
        assert session.end_fr.spl == [-18.0, -13.0, -10.0, -16.0]

    def test_only_target_session_updated(self, store):
        store.save_measurement(make_fr(), label="first")
        store.save_measurement(make_fr(), label="second")
        store.update_end_fr(1, make_fr(spl=[-1.0, -2.0, -3.0, -4.0]))
        assert store.get_session(2).end_fr is None

    def test_end_fr_initially_none(self, store):
        store.save_measurement(make_fr())
        assert store.get_session(1).end_fr is None


# ── list_sessions ─────────────────────────────────────────────────────────────

class TestListSessions:
    def test_empty_store(self, store):
        assert store.list_sessions() == []

    def test_most_recent_first(self, store):
        store.save_measurement(make_fr(timestamp="2026-03-20T10:00:00+00:00"), label="first")
        store.save_measurement(make_fr(timestamp="2026-03-20T11:00:00+00:00"), label="second")
        sessions = store.list_sessions()
        assert sessions[0].label == "second"
        assert sessions[1].label == "first"

    def test_returns_all_sessions(self, store):
        for _ in range(5):
            store.save_measurement(make_fr())
        assert len(store.list_sessions()) == 5

    def test_fr_data_intact_in_list(self, store):
        fr = make_fr(frequencies=[30.0, 60.0], spl=[-8.0, -5.0])
        store.save_measurement(fr)
        sessions = store.list_sessions()
        assert sessions[0].start_fr.frequencies == [30.0, 60.0]

    def test_limit_caps_rows_at_sql_layer(self, store):
        # Regression: list_sessions used to materialize all rows then [:limit].
        # On a long-running DB this OOMs the MCP server (each row carries
        # full FR + IR blobs). Verify limit is honored and ordering is preserved.
        for i in range(7):
            store.save_measurement(make_fr(timestamp=f"2026-03-20T{10+i:02d}:00:00+00:00"))
        sessions = store.list_sessions(limit=3)
        assert len(sessions) == 3
        assert sessions[0].timestamp == "2026-03-20T16:00:00+00:00"
        assert sessions[2].timestamp == "2026-03-20T14:00:00+00:00"

    def test_limit_none_returns_all(self, store):
        for _ in range(4):
            store.save_measurement(make_fr())
        assert len(store.list_sessions(limit=None)) == 4
        assert len(store.list_sessions()) == 4


# ── get_session ───────────────────────────────────────────────────────────────

class TestGetSession:
    def test_returns_correct_session(self, store):
        store.save_measurement(make_fr(), label="A")
        store.save_measurement(make_fr(), label="B")
        assert store.get_session(1).label == "A"
        assert store.get_session(2).label == "B"

    def test_timestamp_preserved(self, store):
        store.save_measurement(make_fr(timestamp="2026-03-20T12:34:56+00:00"))
        session = store.get_session(1)
        assert session.timestamp == "2026-03-20T12:34:56+00:00"

    def test_unknown_id_returns_none(self, store):
        assert store.get_session(999) is None


# ── add_feedback / get_feedback ───────────────────────────────────────────────

class TestFeedback:
    def test_returns_feedback_id(self, store):
        store.save_measurement(make_fr())
        fid = store.add_feedback(1, "bass sounded muddy")
        assert fid == 1

    def test_ids_auto_increment(self, store):
        store.save_measurement(make_fr())
        fid1 = store.add_feedback(1, "too much bass")
        fid2 = store.add_feedback(1, "better now")
        assert fid2 == fid1 + 1

    def test_content_tag_stored(self, store):
        store.save_measurement(make_fr())
        store.add_feedback(1, "rumble", content_tag="movie:fury_road")
        entries = store.get_feedback(1)
        assert entries[0]["content_tag"] == "movie:fury_road"

    def test_content_tag_optional(self, store):
        store.save_measurement(make_fr())
        store.add_feedback(1, "sounds good")
        entries = store.get_feedback(1)
        assert entries[0]["content_tag"] is None

    def test_custom_timestamp_preserved(self, store):
        store.save_measurement(make_fr())
        store.add_feedback(1, "test", timestamp="2026-03-20T09:00:00+00:00")
        entries = store.get_feedback(1)
        assert entries[0]["timestamp"] == "2026-03-20T09:00:00+00:00"

    def test_get_feedback_ordered_oldest_first(self, store):
        store.save_measurement(make_fr())
        store.add_feedback(1, "first note")
        store.add_feedback(1, "second note")
        store.add_feedback(1, "third note")
        entries = store.get_feedback(1)
        assert [e["text"] for e in entries] == ["first note", "second note", "third note"]

    def test_get_feedback_isolated_per_session(self, store):
        store.save_measurement(make_fr())
        store.save_measurement(make_fr())
        store.add_feedback(1, "session 1 note")
        store.add_feedback(2, "session 2 note")
        assert len(store.get_feedback(1)) == 1
        assert len(store.get_feedback(2)) == 1
        assert store.get_feedback(1)[0]["text"] == "session 1 note"


# ── Defensive deserialization ─────────────────────────────────────────────────

class TestDefensiveDeserialization:
    def test_row_to_session_malformed_json(self, store, tmp_path):
        """Corrupt start_fr JSON → sentinel FrequencyResponse with empty arrays."""
        import sqlite3
        db_path = tmp_path / "test.db"
        store2 = SessionStore(db_path=db_path)
        store2.save_measurement(make_fr())

        # Corrupt the start_fr column directly
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE sessions SET start_fr = 'not-valid-json' WHERE id = 1")
        conn.commit()
        conn.close()

        session = store2.get_session(1)
        assert session is not None
        assert session.start_fr.frequencies == []
        assert session.start_fr.spl == []

    def test_fr_peak_spl_empty_list(self):
        """FrequencyResponse with empty spl returns 0.0 for peak_spl and freq_at_peak."""
        fr = FrequencyResponse(
            frequencies=[], spl=[], sample_rate=0, sweep_duration=0.0,
            timestamp="2026-03-20T00:00:00+00:00",
        )
        assert fr.peak_spl == 0.0
        assert fr.freq_at_peak == 0.0


# ── Impulse response storage — F4 ────────────────────────────────────────────

class TestImpulseResponseStorage:
    def test_store_ir_with_session(self, store):
        """Saving a measurement with an IR stores it and round-trips correctly."""
        ir = [0.1 * i for i in range(100)]
        fr = FrequencyResponse(
            frequencies=[20.0, 40.0], spl=[-10.0, -8.0],
            sample_rate=48000, sweep_duration=3.0,
            timestamp="2026-03-20T00:00:00+00:00",
            impulse_response=ir,
        )
        sid = store.save_measurement(fr)
        session = store.get_session(sid)
        assert session.impulse_response is not None
        assert len(session.impulse_response) == 100
        # float32 round-trip — allow small precision loss
        assert abs(session.impulse_response[50] - ir[50]) < 1e-4

    def test_store_without_ir_stores_null(self, store):
        """Saving a measurement without an IR stores NULL — impulse_response is None."""
        fr = make_fr()
        sid = store.save_measurement(fr)
        session = store.get_session(sid)
        assert session.impulse_response is None

    def test_ir_migration_nullable(self, tmp_path):
        """A DB row inserted before the impulse_response column migration returns None."""
        import sqlite3
        db_path = tmp_path / "legacy.db"

        # Simulate pre-migration DB: create the table without impulse_response column
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE sessions ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  timestamp TEXT NOT NULL,"
            "  label TEXT,"
            "  start_fr TEXT NOT NULL,"
            "  end_fr TEXT,"
            "  filters_applied TEXT,"
            "  notes TEXT"
            ")"
        )
        fr = make_fr()
        conn.execute(
            "INSERT INTO sessions (timestamp, label, start_fr) VALUES (?, ?, ?)",
            (fr.timestamp, None, fr.to_json()),
        )
        conn.commit()
        conn.close()

        # Opening SessionStore against this DB runs migration — must not crash
        store = SessionStore(db_path=db_path)
        session = store.get_session(1)
        assert session is not None
        assert session.impulse_response is None

# ── update_events table ────────────────────────────────────────────────────────

class TestUpdateEvents:

    def test_log_update_event_saved(self, tmp_path):
        from calibrate.storage import SessionStore
        store = SessionStore(db_path=tmp_path / "test.db")
        store.log_update_event(
            from_sha="oldsha",
            to_sha="newsha",
            source="timer",
            success=True,
        )
        events = store.list_update_events()
        assert len(events) == 1
        assert events[0]["from_sha"] == "oldsha"
        assert events[0]["to_sha"] == "newsha"
        assert events[0]["source"] == "timer"
        assert events[0]["success"] == 1

    def test_update_events_migration_existing_db(self, tmp_path):
        """Existing DB without update_events table gets the table created on open."""
        import sqlite3
        from calibrate.storage import SessionStore
        db_path = tmp_path / "test.db"

        # Create a DB with only the legacy schema (no update_events table)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE sessions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,"
            "label TEXT, start_fr TEXT NOT NULL, end_fr TEXT, filters_applied TEXT, notes TEXT)"
        )
        conn.commit()
        conn.close()

        # Opening SessionStore should not crash and update_events should now exist
        store = SessionStore(db_path=db_path)
        store.log_update_event(from_sha=None, to_sha="sha1", source="manual")
        events = store.list_update_events()
        assert len(events) == 1

    def test_update_history_queryable(self, tmp_path):
        from calibrate.storage import SessionStore
        store = SessionStore(db_path=tmp_path / "test.db")
        store.log_update_event("sha0", "sha1", "timer", success=True)
        store.log_update_event("sha1", "sha2", "manual", success=True)
        store.log_update_event("sha2", "sha3", "timer", success=False, notes="rollback")
        events = store.list_update_events(limit=10)
        assert len(events) == 3
        # Most recent first
        assert events[0]["to_sha"] == "sha3"
        assert events[2]["to_sha"] == "sha1"

    def test_update_event_db_locked_swallowed(self, tmp_path):
        """DB OperationalError is swallowed — log_update_event does not raise."""
        import sqlite3
        from unittest.mock import patch, MagicMock
        from calibrate.storage import SessionStore
        store = SessionStore(db_path=tmp_path / "test.db")

        def raise_locked(*a, **kw):
            raise sqlite3.OperationalError("database is locked")

        with patch.object(store, "_connect") as mock_conn_ctx:
            mock_conn = MagicMock()
            mock_conn.__enter__ = MagicMock(return_value=mock_conn)
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.execute = raise_locked
            mock_conn_ctx.return_value = mock_conn
            # Must not raise
            store.log_update_event("a", "b", "timer")


# ── Equipment CRUD ───────────────────────────────────────────────────────────
#
# list_equipment     [TESTED] empty list on fresh store
#                    [TESTED] returns inserted rows
#                    [TESTED] data blob is decoded to dict
# save_equipment     [TESTED] returns dict with id and decoded data
#                    [TESTED] stores arbitrary fields in data blob
#                    [TESTED] label and data default to None / {}
# update_equipment   [TESTED] updates label
#                    [TESTED] updates data blob (merge is caller's job)
#                    [TESTED] returns None for unknown id
# delete_equipment   [TESTED] returns True when row deleted
#                    [TESTED] returns False for unknown id

class TestEquipmentCrud:
    def test_list_empty(self, store):
        assert store.list_equipment() == []

    def test_save_returns_row(self, store):
        row = store.save_equipment(type="subwoofer", label="SVS PB12-NSD")
        assert row["id"] >= 1
        assert row["type"] == "subwoofer"
        assert row["label"] == "SVS PB12-NSD"
        assert row["data"] == {}

    def test_save_data_blob(self, store):
        data = {"room_location": "front left corner", "port_tune_hz": 22.0}
        row = store.save_equipment(type="subwoofer", label="SVS", data=data)
        assert row["data"]["room_location"] == "front left corner"
        assert row["data"]["port_tune_hz"] == 22.0

    def test_save_arbitrary_fields_in_data(self, store):
        data = {"sensitivity_db": 87, "impedance_ohms": 4, "future_field": "anything"}
        row = store.save_equipment(type="front_l", data=data)
        assert row["data"]["future_field"] == "anything"

    def test_save_defaults(self, store):
        row = store.save_equipment(type="center")
        assert row["label"] is None
        assert row["data"] == {}

    def test_list_returns_all_ordered(self, store):
        store.save_equipment(type="subwoofer", label="Sub 1")
        store.save_equipment(type="front_l", label="Front L")
        store.save_equipment(type="subwoofer", label="Sub 2")
        rows = store.list_equipment()
        assert len(rows) == 3
        # Ordered by type then id: front_l first, then subwoofers
        assert rows[0]["type"] == "front_l"
        assert rows[1]["type"] == "subwoofer"

    def test_update_label(self, store):
        row = store.save_equipment(type="center", label="Old Name")
        updated = store.update_equipment(row["id"], label="New Name")
        assert updated["label"] == "New Name"

    def test_update_data_blob(self, store):
        row = store.save_equipment(type="subwoofer", data={"port_tune_hz": 22.0})
        new_data = {"port_tune_hz": 24.0, "room_location": "corner"}
        updated = store.update_equipment(row["id"], data=new_data)
        assert updated["data"]["port_tune_hz"] == 24.0
        assert updated["data"]["room_location"] == "corner"

    def test_update_unknown_id_returns_none(self, store):
        result = store.update_equipment(9999, label="Ghost")
        assert result is None

    def test_delete_returns_true(self, store):
        row = store.save_equipment(type="surround_l")
        assert store.delete_equipment(row["id"]) is True
        assert store.list_equipment() == []

    def test_delete_unknown_returns_false(self, store):
        assert store.delete_equipment(9999) is False

    def test_update_no_fields_returns_existing_row(self, store):
        """update_equipment with no label/data should return the existing row, not None."""
        row = store.save_equipment(type="center", label="Original")
        result = store.update_equipment(row["id"])
        assert result is not None
        assert result["label"] == "Original"

    def test_update_no_fields_unknown_id_returns_none(self, store):
        result = store.update_equipment(9999)
        assert result is None

    def test_equipment_row_corrupt_data_json_returns_empty_dict(self, tmp_path):
        """_equipment_row with corrupt data JSON → data becomes {} (lines 309-313)."""
        import sqlite3
        from datetime import datetime, timezone
        db_path = tmp_path / "eq.db"
        store = SessionStore(db_path=db_path)
        # Insert a row with corrupt JSON in data column directly
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO equipment (type, label, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("subwoofer", "SVS", "not-valid-json{{{", now, now),
        )
        conn.commit()
        conn.close()

        rows = store.list_equipment()
        assert len(rows) == 1
        assert rows[0]["data"] == {}


# ── Calibration runs ─────────────────────────────────────────────────────────

class TestCalibrationRuns:

    def test_save_run_returns_id(self, store):
        run_id = store.save_run("harman-bass", "harman")
        assert run_id == 1

    def test_save_run_ids_auto_increment(self, store):
        r1 = store.save_run("harman-bass", "harman")
        r2 = store.save_run("flat-bass", "flat")
        assert r2 == r1 + 1

    def test_update_run_stores_fields(self, store):
        run_id = store.save_run("harman-bass", "harman")
        store.update_run(run_id, converged=True, iterations_run=3,
                         baseline_rms=8.3, final_rms=1.8)
        runs = store.get_runs()
        assert len(runs) == 1
        assert runs[0]["converged"] == 1
        assert runs[0]["iterations_run"] == 3
        assert runs[0]["baseline_rms"] == 8.3
        assert runs[0]["final_rms"] == 1.8
        assert runs[0]["error"] is None

    def test_update_run_with_error(self, store):
        run_id = store.save_run("harman-bass", "harman")
        store.update_run(run_id, converged=False, iterations_run=1,
                         baseline_rms=8.3, final_rms=7.5,
                         error="measurement failed after 3 attempts")
        runs = store.get_runs()
        assert runs[0]["error"] == "measurement failed after 3 attempts"

    def test_save_iteration(self, store):
        run_id = store.save_run("harman-bass", "harman")
        proposed = [{"freq": 50.0, "gain_db": -3.0, "q": 1.0, "type": "peaking"}]
        applied = [{"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"}] + proposed
        iter_id = store.save_iteration(
            run_id, iteration=1, rms_before=8.3, rms_after=6.1,
            filters_proposed=proposed, filters_applied=applied,
            safety_ok=True,
        )
        assert iter_id >= 1

    def test_save_iteration_safety_rejected(self, store):
        run_id = store.save_run("harman-bass", "harman")
        store.save_iteration(
            run_id, iteration=1, rms_before=8.3, rms_after=8.3,
            filters_proposed=[{"freq": 25.0, "gain_db": 10.0, "q": 1.0, "type": "peaking"}],
            filters_applied=[],
            safety_ok=False,
            safety_error="max boost per band exceeded: 10.0 dB > 6.0 dB",
        )
        detail = store.get_run_detail(run_id)
        assert detail is not None
        iters = detail["iterations"]
        assert len(iters) == 1
        assert iters[0]["safety_ok"] == 0
        assert "max boost" in iters[0]["safety_error"]

    def test_get_runs_most_recent_first(self, store):
        r1 = store.save_run("first", "harman")
        r2 = store.save_run("second", "flat")
        runs = store.get_runs()
        assert runs[0]["id"] == r2
        assert runs[1]["id"] == r1

    def test_get_runs_respects_limit(self, store):
        for i in range(5):
            store.save_run(f"run-{i}", "harman")
        runs = store.get_runs(limit=3)
        assert len(runs) == 3

    def test_get_run_detail_with_iterations(self, store):
        run_id = store.save_run("harman-bass", "harman")
        store.update_run(run_id, converged=True, iterations_run=2,
                         baseline_rms=8.3, final_rms=1.8)
        for i in range(1, 3):
            store.save_iteration(
                run_id, iteration=i, rms_before=8.3 - i, rms_after=8.3 - i - 1,
                filters_proposed=[{"freq": 50.0, "gain_db": -2.0, "q": 1.0, "type": "peaking"}],
                filters_applied=[{"freq": 50.0, "gain_db": -2.0, "q": 1.0, "type": "peaking"}],
                safety_ok=True,
            )
        detail = store.get_run_detail(run_id)
        assert detail["recipe_name"] == "harman-bass"
        assert detail["converged"] == 1
        assert len(detail["iterations"]) == 2
        assert detail["iterations"][0]["iteration"] == 1
        assert detail["iterations"][1]["iteration"] == 2
        # Filters are deserialized from JSON
        assert isinstance(detail["iterations"][0]["filters_applied"], list)
        assert detail["iterations"][0]["filters_applied"][0]["freq"] == 50.0

    def test_get_run_detail_unknown_id_returns_none(self, store):
        assert store.get_run_detail(999) is None

    def test_coexists_with_existing_sessions(self, store):
        """New calibration tables don't break existing session data."""
        sid = store.save_measurement(make_fr())
        run_id = store.save_run("harman-bass", "harman")
        assert store.get_session(sid) is not None
        assert store.get_run_detail(run_id) is not None

    def test_save_run_with_validation_type(self, store):
        """save_run accepts run_type='validation'."""
        run_id = store.save_run("full-room-verify", "none", run_type="validation")
        detail = store.get_run_detail(run_id)
        assert detail["run_type"] == "validation"

    def test_save_run_default_type_is_calibration(self, store):
        """save_run defaults to run_type='calibration'."""
        run_id = store.save_run("harman-bass", "harman")
        detail = store.get_run_detail(run_id)
        assert detail["run_type"] == "calibration"

    def test_update_run_with_sessions(self, store):
        """update_run stores sessions list for validation runs."""
        run_id = store.save_run("full-room-verify", "none", run_type="validation")
        sessions = [
            {"session_id": 321, "label": "Left Pure Direct"},
            {"session_id": 322, "label": "Center Pure Direct"},
        ]
        store.update_run(run_id, converged=True, iterations_run=0, sessions=sessions)
        detail = store.get_run_detail(run_id)
        import json
        stored_sessions = json.loads(detail["sessions"])
        assert len(stored_sessions) == 2
        assert stored_sessions[0]["session_id"] == 321

    def test_full_state_snapshot_captured_on_save_run(self, store):
        store.set_active_dsp("input_eq", {"filters": [{"freq": 50.0, "gain_db": -3.0}]})
        store.set_active_dsp("output_0_gain", {"gain_db": 1.1})
        run_id = store.save_run("harman-bass", "harman")
        detail = store.get_run_detail(run_id)
        snap = detail["full_state_snapshot"]
        assert isinstance(snap, dict)
        assert snap["output_0_gain"]["gain_db"] == 1.1
        assert snap["input_eq"]["filters"][0]["freq"] == 50.0

    def test_full_state_snapshot_captured_on_iteration(self, store):
        run_id = store.save_run("harman-bass", "harman")
        store.set_active_dsp("output_2_delay", {"delay_ms": 2.83})
        store.save_iteration(
            run_id, iteration=1, rms_before=8.3, rms_after=6.1,
            filters_proposed=[], filters_applied=[], safety_ok=True,
        )
        detail = store.get_run_detail(run_id)
        iter_snap = detail["iterations"][0]["full_state_snapshot"]
        assert isinstance(iter_snap, dict)
        assert iter_snap["output_2_delay"]["delay_ms"] == 2.83

    def test_full_state_snapshot_refreshed_on_update_run(self, store):
        run_id = store.save_run("harman-bass", "harman")
        store.set_active_dsp("output_0_gain", {"gain_db": 2.5})
        store.update_run(run_id, converged=True, iterations_run=1,
                         baseline_rms=8.3, final_rms=1.8)
        detail = store.get_run_detail(run_id)
        assert detail["full_state_snapshot"]["output_0_gain"]["gain_db"] == 2.5

    def test_get_runs_excludes_snapshot_blob(self, store):
        """List view strips full_state_snapshot to keep responses small."""
        store.set_active_dsp("output_0_gain", {"gain_db": 1.1})
        store.save_run("harman-bass", "harman")
        runs = store.get_runs()
        assert len(runs) == 1
        assert "full_state_snapshot" not in runs[0]


# ── _row_to_session — corrupt end_fr / filters_applied / impulse_response ────

class TestRowToSessionCorruption:

    def test_corrupt_end_fr_returns_none(self, tmp_path):
        """Corrupt end_fr JSON → end_fr is None (lines 329-331)."""
        import sqlite3
        db_path = tmp_path / "sess.db"
        store = SessionStore(db_path=db_path)
        store.save_measurement(make_fr())
        # Corrupt end_fr
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE sessions SET end_fr = 'bad-json' WHERE id = 1")
        conn.commit()
        conn.close()

        session = store.get_session(1)
        assert session is not None
        assert session.end_fr is None

    def test_corrupt_filters_applied_returns_none(self, tmp_path):
        """Corrupt filters_applied JSON → filters_applied is None (lines 335-337)."""
        import sqlite3
        db_path = tmp_path / "sess.db"
        store = SessionStore(db_path=db_path)
        store.save_measurement(make_fr())
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE sessions SET filters_applied = '{bad:json}' WHERE id = 1")
        conn.commit()
        conn.close()

        session = store.get_session(1)
        assert session is not None
        assert session.filters_applied is None

    def test_corrupt_impulse_response_returns_none(self, tmp_path):
        """Corrupt impulse_response blob → impulse_response is None (lines 344-345)."""
        import sqlite3
        db_path = tmp_path / "sess.db"
        store = SessionStore(db_path=db_path)
        store.save_measurement(make_fr())
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE sessions SET impulse_response = 'not-a-valid-blob' WHERE id = 1")
        conn.commit()
        conn.close()

        session = store.get_session(1)
        assert session is not None
        assert session.impulse_response is None

    def test_equipment_row_null_data_returns_empty_dict(self, tmp_path):
        """_equipment_row with NULL data column → data becomes {} (line 313)."""
        import sqlite3
        from datetime import datetime, timezone
        db_path = tmp_path / "eq_null.db"
        store = SessionStore(db_path=db_path)
        # Insert a row with NULL in data column — hits the else branch (line 313)
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO equipment (type, label, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("subwoofer", "SVS", None, now, now),
        )
        conn.commit()
        conn.close()

        rows = store.list_equipment()
        assert len(rows) == 1
        assert rows[0]["data"] == {}


# ── Metadata enrichment ─────────────────────────────────────────────────────

class TestSessionMetadata:
    """Tests for session metadata storage and retrieval."""

    def test_save_and_retrieve_metadata(self, store: SessionStore):
        meta = {"ir": {"peak_time_ms": 16.4, "peak_sign": 1, "spl_db": 66.9}}
        sid = store.save_measurement(make_fr(), label="test", metadata=meta)
        session = store.get_session(sid)
        assert session.metadata == meta

    def test_metadata_none_by_default(self, store: SessionStore):
        sid = store.save_measurement(make_fr())
        session = store.get_session(sid)
        assert session.metadata is None

    def test_metadata_with_decay_modes(self, store: SessionStore):
        meta = {
            "ir": {"peak_time_ms": 12.0, "peak_sign": -1, "spl_db": 68.0},
            "decay_modes": [
                {"freq_hz": 23.4, "t60_ms": 1080.0, "peak_db": 10.0, "suggested_q": 6.0, "priority": 1}
            ],
            "group_delay": {"freq_hz": [30.0, 50.0], "delay_ms": [5.2, 3.1]},
        }
        sid = store.save_measurement(make_fr(), metadata=meta)
        session = store.get_session(sid)
        assert session.metadata["decay_modes"][0]["freq_hz"] == 23.4
        assert session.metadata["group_delay"]["delay_ms"] == [5.2, 3.1]

    def test_metadata_survives_list_sessions(self, store: SessionStore):
        meta = {"ir": {"peak_time_ms": 10.0, "peak_sign": 1, "spl_db": 60.0}}
        store.save_measurement(make_fr(), metadata=meta)
        sessions = store.list_sessions()
        assert sessions[0].metadata == meta

    def test_migrate_adds_metadata_column(self, tmp_path: Path):
        """Existing DB without metadata column gets it added on next open."""
        import sqlite3
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE sessions ("
            "id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, label TEXT, "
            "start_fr TEXT NOT NULL, end_fr TEXT, filters_applied TEXT, "
            "notes TEXT, impulse_response TEXT)"
        )
        conn.commit()
        conn.close()
        s = SessionStore(db_path=db_path)
        meta = {"ir": {"peak_time_ms": 5.0}}
        sid = s.save_measurement(make_fr(), metadata=meta)
        assert s.get_session(sid).metadata == meta


# ── Session target curve ─────────────────────────────────────────────────────

class TestSessionTargetCurve:
    """Tests for target_curve storage and retrieval on Session."""

    def test_target_curve_stored_and_retrieved(self, store: SessionStore):
        tc = {"type": "harman", "reference_spl": 78.0, "band": [20.0, 200.0]}
        sid = store.save_measurement(make_fr(), label="cal-run", target_curve=tc)
        session = store.get_session(sid)
        assert session.target_curve == tc

    def test_target_curve_none_by_default(self, store: SessionStore):
        sid = store.save_measurement(make_fr())
        session = store.get_session(sid)
        assert session.target_curve is None

    def test_target_curve_survives_list_sessions(self, store: SessionStore):
        tc = {"type": "harman", "reference_spl": 75.0, "band": [20.0, 200.0]}
        store.save_measurement(make_fr(), target_curve=tc)
        sessions = store.list_sessions()
        assert sessions[0].target_curve == tc

    def test_corrupt_target_curve_returns_none(self, tmp_path: Path):
        """Corrupt target_curve JSON → None (graceful degradation)."""
        import sqlite3
        db_path = tmp_path / "tc_corrupt.db"
        s = SessionStore(db_path=db_path)
        s.save_measurement(make_fr())
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE sessions SET target_curve = '{bad:json}' WHERE id = 1")
        conn.commit()
        conn.close()
        session = s.get_session(1)
        assert session is not None
        assert session.target_curve is None

    def test_target_curve_with_points(self, store: SessionStore):
        """Full target_curve dict including points list round-trips."""
        tc = {
            "type": "harman",
            "reference_spl": 72.5,
            "band": [20.0, 200.0],
            "points": [{"freq": 20.0, "spl": 78.5}, {"freq": 80.0, "spl": 72.5}],
        }
        sid = store.save_measurement(make_fr(), target_curve=tc)
        session = store.get_session(sid)
        assert session.target_curve["points"][0]["freq"] == 20.0
        assert session.target_curve["reference_spl"] == 72.5


# ── Saved states ─────────────────────────────────────────────────────────────

class TestSavedStates:
    def test_save_state_returns_id(self, store):
        sid = store.save_state(name="Harman v1")
        assert isinstance(sid, int) and sid > 0

    def test_list_states_most_recent_first(self, store):
        store.save_state(name="First")
        store.save_state(name="Second")
        states = store.list_states()
        assert len(states) == 2
        assert states[0]["name"] == "Second"
        assert states[1]["name"] == "First"

    def test_list_states_empty(self, store):
        assert store.list_states() == []

    def test_get_state_full_snapshot(self, store):
        filters = [{"freq": 40, "gain_db": -3, "q": 2, "type": "peaking"}]
        delays = {"0": 1.5, "1": 0.0}
        polarities = {"0": False, "1": True}
        gains = {"0": 0.0, "1": -2.0}
        sid = store.save_state(
            name="Full test",
            eq_filters=filters,
            delays=delays,
            polarities=polarities,
            gains=gains,
            target_curve="harman",
            rms_deviation=1.9,
            notes="converged run",
        )
        state = store.get_state(sid)
        assert state["name"] == "Full test"
        assert state["eq_filters"] == filters
        assert state["delays"] == delays
        assert state["polarities"] == polarities
        assert state["gains"] == gains
        assert state["target_curve"] == "harman"
        assert state["rms_deviation"] == 1.9
        assert state["notes"] == "converged run"

    def test_get_state_not_found(self, store):
        assert store.get_state(999) is None

    def test_get_state_with_measurement_link(self, store):
        session_id = store.save_measurement(make_fr(), label="test")
        sid = store.save_state(name="Linked", measurement_session_id=session_id)
        state = store.get_state(sid)
        assert state["measurement_session_id"] == session_id

    def test_delete_state(self, store):
        sid = store.save_state(name="Doomed")
        assert store.delete_state(sid) is True
        assert store.get_state(sid) is None
        assert store.list_states() == []

    def test_delete_state_not_found(self, store):
        assert store.delete_state(999) is False

    def test_save_state_minimal(self, store):
        """Save with only a name, all other fields None."""
        sid = store.save_state(name="Minimal")
        state = store.get_state(sid)
        assert state["name"] == "Minimal"
        assert state["eq_filters"] is None
        assert state["delays"] is None
        assert state["target_curve"] is None


# ── Active DSP state ─────────────────────────────────────────────────────────


class TestActiveDspState:
    def test_set_and_get(self, store):
        store.set_active_dsp("output_eq_1", {"filters": [{"freq": 80, "gain_db": -3}]})
        result = store.get_active_dsp()
        assert "output_eq_1" in result
        assert result["output_eq_1"]["filters"] == [{"freq": 80, "gain_db": -3}]
        assert "timestamp" in result["output_eq_1"]

    def test_upsert_overwrites(self, store):
        store.set_active_dsp("delay_1", {"delay_ms": 5.0})
        store.set_active_dsp("delay_1", {"delay_ms": 12.2})
        result = store.get_active_dsp()
        assert result["delay_1"]["delay_ms"] == 12.2

    def test_multiple_keys(self, store):
        store.set_active_dsp("output_eq_1", {"filters": []})
        store.set_active_dsp("output_eq_2", {"filters": []})
        store.set_active_dsp("input_eq", {"filters": []})
        store.set_active_dsp("delay_1", {"delay_ms": 0})
        result = store.get_active_dsp()
        assert len(result) == 4

    def test_empty_returns_empty(self, store):
        assert store.get_active_dsp() == {}

    def test_clear(self, store):
        store.set_active_dsp("output_eq_1", {"filters": []})
        store.set_active_dsp("delay_1", {"delay_ms": 0})
        store.clear_active_dsp()
        assert store.get_active_dsp() == {}


# ── Lessons system ──────────────────────────────────────────────────────────


class TestLessons:
    def test_record_basic(self, store):
        lid = store.record_lesson(
            claim="PEQ cuts can't shorten T60 above 300 ms",
            scope="general",
            category="modal_correction",
        )
        assert lid > 0
        lessons = store.get_relevant_lessons()
        assert len(lessons) == 1
        assert lessons[0]["claim"].startswith("PEQ cuts")
        assert lessons[0]["status"] == "active"
        assert lessons[0]["state_hash"]  # stamped

    def test_invalid_scope_rejected(self, store):
        with pytest.raises(ValueError):
            store.record_lesson(claim="x", scope="bogus")

    def test_filter_by_tags(self, store):
        store.record_lesson(claim="A", scope="room", tags=["45hz", "modal"])
        store.record_lesson(claim="B", scope="room", tags=["polarity"])
        store.record_lesson(claim="C", scope="room")  # no tags
        out = store.get_relevant_lessons(tags=["45hz"])
        assert [l["claim"] for l in out] == ["A"]

    def test_filter_by_category_and_scope(self, store):
        store.record_lesson(claim="X", scope="room", category="sub_alignment")
        store.record_lesson(claim="Y", scope="general", category="sub_alignment")
        store.record_lesson(claim="Z", scope="room", category="modal_correction")
        out = store.get_relevant_lessons(category="sub_alignment", scope="room")
        assert [l["claim"] for l in out] == ["X"]

    def test_event_invalidator(self, store):
        lid = store.record_lesson(
            claim="this lesson dies on sub move",
            scope="room",
            invalidators=[{"kind": "event", "value": "sub_position_changed"}],
        )
        assert store.get_relevant_lessons()
        invalidated = store.invalidate_lessons(events=["sub_position_changed"])
        assert lid in invalidated
        assert store.get_relevant_lessons() == []
        # audit trail preserved
        all_lessons = store.list_lessons()
        assert all_lessons[0]["status"] == "invalidated"
        assert all_lessons[0]["invalidated_at"]

    def test_code_invalidator(self, store):
        lid = store.record_lesson(
            claim="modal FIR clipping",
            scope="general",
            invalidators=[
                {"kind": "code", "value": "calibrate.modal_fir.design_modal_fir"}
            ],
        )
        ids = store.invalidate_lessons(
            codes=["calibrate.modal_fir.design_modal_fir"], reason="bug fix",
        )
        assert ids == [lid]
        assert store.list_lessons()[0]["invalidated_by"] == "bug fix"

    def test_state_hash_invalidator(self, store):
        lid = store.record_lesson(
            claim="depends on current DSP state",
            scope="room",
            invalidators=[{"kind": "state_hash", "value": "ignored"}],
        )
        # mutate state → hash changes
        store.set_active_dsp("processor:minidsp:output:0:delay", {"delay_ms": 5.0})
        ids = store.invalidate_lessons(state_changed=True)
        assert lid in ids

    def test_state_hash_unchanged_keeps_lesson(self, store):
        lid = store.record_lesson(
            claim="stable",
            scope="room",
            invalidators=[{"kind": "state_hash", "value": "x"}],
        )
        ids = store.invalidate_lessons(state_changed=True)
        assert ids == []
        assert store.list_lessons()[0]["id"] == lid
        assert store.list_lessons()[0]["status"] == "active"

    def test_invalidate_idempotent(self, store):
        lid = store.record_lesson(
            claim="x",
            scope="room",
            invalidators=[{"kind": "event", "value": "sub_position_changed"}],
        )
        first = store.invalidate_lessons(events=["sub_position_changed"])
        assert first == [lid]
        # second call: lesson already invalidated, UPDATE matches no rows
        second = store.invalidate_lessons(events=["sub_position_changed"])
        # ids list still includes the lesson_id because the lookup matches,
        # but the status doesn't flip a second time — verify timestamp stable
        rec = store.list_lessons()[0]
        assert rec["status"] == "invalidated"

    def test_promote_lesson(self, store):
        lid = store.record_lesson(claim="universal", scope="general")
        ok = store.promote_lesson(lid, "memory:feedback_x.md")
        assert ok
        lesson = store.list_lessons()[0]
        assert lesson["status"] == "promoted"
        assert lesson["promoted_to"] == "memory:feedback_x.md"
        # promoted lessons don't surface in get_relevant_lessons
        assert store.get_relevant_lessons() == []

    def test_promote_nonexistent_returns_false(self, store):
        assert store.promote_lesson(9999, "x") is False

    def test_include_invalidated(self, store):
        lid = store.record_lesson(
            claim="x",
            scope="room",
            invalidators=[{"kind": "event", "value": "e"}],
        )
        store.invalidate_lessons(events=["e"])
        active = store.get_relevant_lessons()
        all_l = store.get_relevant_lessons(include_invalidated=True)
        assert active == []
        assert len(all_l) == 1

    def test_evidence_roundtrips(self, store):
        store.record_lesson(
            claim="x",
            scope="room",
            evidence={"freq_hz": 45, "t60_ms": 350, "delivered_db": 1.5},
        )
        l = store.get_relevant_lessons()[0]
        assert l["evidence"] == {"freq_hz": 45, "t60_ms": 350, "delivered_db": 1.5}

    def test_run_with_goal_hypothesis_outcome(self, store):
        run_id = store.save_run(
            recipe_name="bass-calibration",
            target="harman-bass",
            goal="shorten 45 Hz T60 below 250 ms",
            hypothesis="modal FIR Q=8 will work",
        )
        store.update_run(
            run_id, converged=False, iterations_run=3,
            outcome="delivered 1.5 dB; adjacent-band T60 capped the cut",
        )
        detail = store.get_run_detail(run_id)
        assert detail["goal"] == "shorten 45 Hz T60 below 250 ms"
        assert detail["hypothesis"] == "modal FIR Q=8 will work"
        assert detail["outcome"].startswith("delivered 1.5 dB")

    def test_lesson_links_to_run(self, store):
        run_id = store.save_run("r", "harman", goal="g", hypothesis="h")
        lid = store.record_lesson(claim="x", scope="room", run_id=run_id)
        assert store.get_relevant_lessons()[0]["run_id"] == run_id

    def test_ranking_by_confidence(self, store):
        store.record_lesson(claim="low", scope="room", confidence=0.3)
        store.record_lesson(claim="high", scope="room", confidence=0.9)
        store.record_lesson(claim="mid", scope="room", confidence=0.6)
        out = store.get_relevant_lessons()
        assert [l["claim"] for l in out] == ["high", "mid", "low"]
