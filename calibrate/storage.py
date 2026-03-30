"""SQLite session store for measurement history.

Schema
------
sessions  — one row per calibration run; holds start/end frequency responses
            and any filters applied during the session.
feedback  — zero or more subjective feedback entries per session, with an
            optional content_tag for TODO-3 content-aware EQ profiles.

Migration
---------
_migrate_schema() is called after _init_schema() on every startup. It uses
PRAGMA table_info to detect missing columns and adds them with ALTER TABLE.
"""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

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

CREATE TABLE IF NOT EXISTS update_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT    NOT NULL,
    from_sha   TEXT,
    to_sha     TEXT,
    source     TEXT    NOT NULL,
    success    INTEGER NOT NULL DEFAULT 1,
    notes      TEXT
);
"""


_IR_DTYPE = "float32"
_IR_BYTES = 4  # bytes per float32


def _encode_ir(ir: list[float]) -> str:
    """Encode an IR sample list as a base64 float32 blob (TEXT column)."""
    import struct
    packed = struct.pack(f"<{len(ir)}f", *ir)
    return base64.b64encode(packed).decode("ascii")


def _decode_ir(blob: str) -> list[float]:
    """Decode a base64 float32 blob back to a list of floats."""
    import struct
    raw = base64.b64decode(blob)
    n = len(raw) // _IR_BYTES
    return list(struct.unpack(f"<{n}f", raw[:n * _IR_BYTES]))


@dataclass
class Session:
    id: int
    timestamp: str
    label: Optional[str]
    start_fr: FrequencyResponse
    end_fr: Optional[FrequencyResponse]
    filters_applied: Optional[list[dict]]
    notes: Optional[str]
    impulse_response: Optional[list[float]] = None  # time-domain IR (first 24 000 samples)


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
        self._migrate_schema()

    # ── Schema ───────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _migrate_schema(self) -> None:
        """Add columns to existing tables that were introduced after initial schema.

        Uses PRAGMA table_info to detect missing columns — idempotent on repeat runs.
        """
        with self._connect() as conn:
            existing = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
            if "impulse_response" not in existing:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN impulse_response TEXT DEFAULT NULL"
                )

    # ── Sessions ─────────────────────────────────────────────────────────────

    def save_measurement(
        self,
        fr: FrequencyResponse,
        label: Optional[str] = None,
    ) -> int:
        """Persist a measurement as a new session. Returns the new session id."""
        ir_blob = _encode_ir(fr.impulse_response) if fr.impulse_response else None
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sessions (timestamp, label, start_fr, impulse_response)"
                " VALUES (?, ?, ?, ?)",
                (fr.timestamp, label, fr.to_json(), ir_blob),
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

    # ── Update events ────────────────────────────────────────────────────────

    def log_update_event(
        self,
        from_sha: Optional[str],
        to_sha: Optional[str],
        source: str,
        success: bool = True,
        notes: Optional[str] = None,
    ) -> None:
        """Log an auto-update or manual upgrade event. Non-critical: swallows DB errors."""
        ts = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO update_events (timestamp, from_sha, to_sha, source, success, notes)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (ts, from_sha, to_sha, source, int(success), notes),
                )
        except sqlite3.OperationalError as exc:
            logger.warning("log_update_event: DB error (non-critical): %s", exc)

    def list_update_events(
        self,
        limit: int = 50,
    ) -> list[dict]:
        """Return recent update events, most recent first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM update_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _row_to_session(self, row: sqlite3.Row) -> Session:
        try:
            start_fr = FrequencyResponse.from_json(row["start_fr"])
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            logger.warning("session %d has corrupt start_fr; returning sentinel", row["id"])
            start_fr = FrequencyResponse(
                frequencies=[], spl=[], sample_rate=0, sweep_duration=0.0,
                timestamp=row["timestamp"],
            )
        try:
            end_fr = FrequencyResponse.from_json(row["end_fr"]) if row["end_fr"] else None
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            logger.warning("session %d has corrupt end_fr; ignoring", row["id"])
            end_fr = None

        try:
            filters_applied = json.loads(row["filters_applied"]) if row["filters_applied"] else None
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("session %d has corrupt filters_applied; ignoring", row["id"])
            filters_applied = None

        ir = None
        try:
            raw_ir = row["impulse_response"] if "impulse_response" in row.keys() else None
            if raw_ir:
                ir = _decode_ir(raw_ir)
        except Exception:
            logger.warning("session %d has corrupt impulse_response; ignoring", row["id"])

        return Session(
            id=row["id"],
            timestamp=row["timestamp"],
            label=row["label"],
            start_fr=start_fr,
            end_fr=end_fr,
            filters_applied=filters_applied,
            notes=row["notes"],
            impulse_response=ir,
        )
