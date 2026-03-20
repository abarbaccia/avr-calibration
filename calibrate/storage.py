"""SQLite session store for measurement history.

Schema
------
sessions  — one row per calibration run; holds start/end frequency responses
            and any filters applied during the session.
feedback  — zero or more subjective feedback entries per session, with an
            optional content_tag for TODO-3 content-aware EQ profiles.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .measurement import FrequencyResponse

DB_PATH = Path.home() / ".avr-calibration" / "history.db"

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT    NOT NULL,
    label            TEXT,
    start_fr         TEXT    NOT NULL,
    end_fr           TEXT,
    filters_applied  TEXT,
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS feedback (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES sessions(id),
    timestamp    TEXT    NOT NULL,
    content_tag  TEXT,
    text         TEXT    NOT NULL
);
"""


@dataclass
class Session:
    id: int
    timestamp: str
    label: Optional[str]
    start_fr: FrequencyResponse
    end_fr: Optional[FrequencyResponse]
    filters_applied: Optional[list[dict]]
    notes: Optional[str]


class SessionStore:
    """
    Persistent store for calibration sessions and subjective feedback.

    Usage:
        store = SessionStore()                       # default path
        sid   = store.save_measurement(fr)           # opens new session
        store.add_feedback(sid, "bass sounded thin")
        store.update_end_fr(sid, final_fr)           # close session
        sessions = store.list_sessions()
    """

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── Schema ───────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ── Sessions ─────────────────────────────────────────────────────────────

    def save_measurement(
        self,
        fr: FrequencyResponse,
        label: Optional[str] = None,
    ) -> int:
        """Persist a measurement as a new session. Returns the new session id."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sessions (timestamp, label, start_fr) VALUES (?, ?, ?)",
                (fr.timestamp, label, fr.to_json()),
            )
            return cur.lastrowid

    def update_end_fr(self, session_id: int, fr: FrequencyResponse) -> None:
        """Record the final frequency response once calibration converges."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET end_fr = ? WHERE id = ?",
                (fr.to_json(), session_id),
            )

    def list_sessions(self) -> list[Session]:
        """Return all sessions, most recent first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY id DESC"
            ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def get_session(self, session_id: int) -> Optional[Session]:
        """Return a session by id, or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return self._row_to_session(row) if row is not None else None

    # ── Feedback ─────────────────────────────────────────────────────────────

    def add_feedback(
        self,
        session_id: int,
        text: str,
        content_tag: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> int:
        """
        Add a subjective feedback entry to a session.

        content_tag — optional, e.g. "movie:fury_road", "music:daft_punk".
                      Used by the AI analysis module for content-aware EQ.
        Returns the new feedback id.
        """
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO feedback (session_id, timestamp, content_tag, text)"
                " VALUES (?, ?, ?, ?)",
                (session_id, ts, content_tag, text),
            )
            return cur.lastrowid

    def get_feedback(self, session_id: int) -> list[dict]:
        """Return all feedback entries for a session, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _row_to_session(self, row: sqlite3.Row) -> Session:
        return Session(
            id=row["id"],
            timestamp=row["timestamp"],
            label=row["label"],
            start_fr=FrequencyResponse.from_json(row["start_fr"]),
            end_fr=FrequencyResponse.from_json(row["end_fr"]) if row["end_fr"] else None,
            filters_applied=json.loads(row["filters_applied"]) if row["filters_applied"] else None,
            notes=row["notes"],
        )
