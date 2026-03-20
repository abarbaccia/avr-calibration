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
