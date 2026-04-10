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

CREATE TABLE IF NOT EXISTS equipment (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type       TEXT    NOT NULL,
    label      TEXT,
    data       TEXT,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS calibration_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    recipe_name     TEXT    NOT NULL,
    target          TEXT    NOT NULL,
    converged       INTEGER NOT NULL DEFAULT 0,
    iterations_run  INTEGER NOT NULL DEFAULT 0,
    baseline_rms    REAL,
    final_rms       REAL,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS calibration_iterations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES calibration_runs(id),
    iteration        INTEGER NOT NULL,
    rms_before       REAL    NOT NULL,
    rms_after        REAL    NOT NULL,
    filters_proposed TEXT,
    filters_applied  TEXT,
    safety_ok        INTEGER NOT NULL DEFAULT 1,
    safety_error     TEXT
);

CREATE TABLE IF NOT EXISTS saved_states (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT    NOT NULL,
    timestamp             TEXT    NOT NULL,
    eq_filters            TEXT,
    delays                TEXT,
    polarities            TEXT,
    gains                 TEXT,
    target_curve          TEXT,
    rms_deviation         REAL,
    measurement_session_id INTEGER REFERENCES sessions(id),
    notes                 TEXT
);

CREATE TABLE IF NOT EXISTS active_dsp_state (
    key         TEXT PRIMARY KEY,
    timestamp   TEXT NOT NULL,
    data        TEXT NOT NULL
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
    metadata: Optional[dict] = None  # IR-derived: ir peak, decay modes, group delay
    target_curve: Optional[dict] = None  # optimization target active at measurement time


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
            if "metadata" not in existing:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN metadata TEXT DEFAULT NULL"
                )
            if "target_curve" not in existing:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN target_curve TEXT DEFAULT NULL"
                )

            run_cols = {row[1] for row in conn.execute("PRAGMA table_info(calibration_runs)")}
            if "target_curve_data" not in run_cols:
                conn.execute(
                    "ALTER TABLE calibration_runs ADD COLUMN target_curve_data TEXT DEFAULT NULL"
                )

    # ── Sessions ─────────────────────────────────────────────────────────────

    def save_measurement(
        self,
        fr: FrequencyResponse,
        label: Optional[str] = None,
        metadata: Optional[dict] = None,
        target_curve: Optional[dict] = None,
    ) -> int:
        """Persist a measurement as a new session. Returns the new session id."""
        ir_blob = _encode_ir(fr.impulse_response) if fr.impulse_response else None
        meta_json = json.dumps(metadata) if metadata else None
        tc_json = json.dumps(target_curve) if target_curve else None
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sessions (timestamp, label, start_fr, impulse_response, metadata, target_curve)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (fr.timestamp, label, fr.to_json(), ir_blob, meta_json, tc_json),
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

    # ── Equipment ─────────────────────────────────────────────────────────────
    #
    # The `data` column is a JSON blob — no fixed schema beyond type + label.
    # Add any new fields (distance, sensitivity, port_tune_hz, etc.) to `data`
    # without schema migrations.

    def list_equipment(self) -> list[dict]:
        """Return all equipment rows ordered by type then id."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM equipment ORDER BY type, id"
            ).fetchall()
        return [self._equipment_row(r) for r in rows]

    def save_equipment(self, type: str, label: Optional[str] = None, data: Optional[dict] = None) -> dict:
        """Insert a new equipment record. `data` is an open JSON blob."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO equipment (type, label, data, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (type, label, json.dumps(data or {}), now, now),
            )
            row = conn.execute("SELECT * FROM equipment WHERE id=?", (cur.lastrowid,)).fetchone()
        return self._equipment_row(row)

    def update_equipment(self, equipment_id: int, label: Optional[str] = None, data: Optional[dict] = None) -> Optional[dict]:
        """Update label and/or data blob. Returns None if not found, or existing row if nothing to update."""
        fields: dict = {}
        if label is not None:
            fields["label"] = label
        if data is not None:
            fields["data"] = json.dumps(data)
        with self._connect() as conn:
            if not fields:
                # No-op: return current row (or None if not found)
                row = conn.execute("SELECT * FROM equipment WHERE id=?", (equipment_id,)).fetchone()
                return self._equipment_row(row) if row else None
            fields["updated_at"] = datetime.now(timezone.utc).isoformat()
            # TODO: Field names come from caller, not user input, but consider
            #       a whitelist check if this is ever exposed via API.
            sets = ", ".join(f"{k}=?" for k in fields)
            vals = list(fields.values()) + [equipment_id]
            conn.execute(f"UPDATE equipment SET {sets} WHERE id=?", vals)
            row = conn.execute("SELECT * FROM equipment WHERE id=?", (equipment_id,)).fetchone()
        return self._equipment_row(row) if row else None

    def delete_equipment(self, equipment_id: int) -> bool:
        """Delete equipment by id. Returns True if a row was deleted."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM equipment WHERE id=?", (equipment_id,))
        return cur.rowcount > 0

    def _equipment_row(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        if d.get("data"):
            try:
                d["data"] = json.loads(d["data"])
            except (json.JSONDecodeError, TypeError):
                logger.warning("equipment row %s has corrupt data JSON; returning {}", d.get("id"))
                d["data"] = {}
        else:
            d["data"] = {}
        return d

    # ── Calibration runs ────────────────────────────────────────────────────

    def save_run(self, recipe_name: str, target: str) -> int:
        """Create a new calibration run record. Returns the run id."""
        ts = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO calibration_runs (timestamp, recipe_name, target)"
                " VALUES (?, ?, ?)",
                (ts, recipe_name, target),
            )
            return cur.lastrowid

    def update_run(
        self,
        run_id: int,
        converged: bool,
        iterations_run: int,
        baseline_rms: float | None = None,
        final_rms: float | None = None,
        error: str = "",
        target_curve_data: dict | None = None,
    ) -> None:
        """Update a calibration run with final results."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE calibration_runs"
                " SET converged=?, iterations_run=?, baseline_rms=?, final_rms=?, error=?,"
                "     target_curve_data=?"
                " WHERE id=?",
                (int(converged), iterations_run, baseline_rms, final_rms, error or None,
                 json.dumps(target_curve_data) if target_curve_data else None, run_id),
            )

    def save_iteration(
        self,
        run_id: int,
        iteration: int,
        rms_before: float,
        rms_after: float,
        filters_proposed: list[dict],
        filters_applied: list[dict],
        safety_ok: bool,
        safety_error: str = "",
    ) -> int:
        """Save one iteration of a calibration run. Returns the iteration row id."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO calibration_iterations"
                " (run_id, iteration, rms_before, rms_after,"
                "  filters_proposed, filters_applied, safety_ok, safety_error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    iteration,
                    rms_before,
                    rms_after,
                    json.dumps(filters_proposed),
                    json.dumps(filters_applied),
                    int(safety_ok),
                    safety_error or None,
                ),
            )
            return cur.lastrowid

    def get_runs(self, limit: int = 20) -> list[dict]:
        """Return recent calibration runs, most recent first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM calibration_runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_run_detail(self, run_id: int) -> dict | None:
        """Return a calibration run with all its iterations, or None if not found."""
        with self._connect() as conn:
            run_row = conn.execute(
                "SELECT * FROM calibration_runs WHERE id=?", (run_id,)
            ).fetchone()
            if run_row is None:
                return None

            iter_rows = conn.execute(
                "SELECT * FROM calibration_iterations WHERE run_id=? ORDER BY iteration",
                (run_id,),
            ).fetchall()

        run = dict(run_row)
        iterations = []
        for r in iter_rows:
            d = dict(r)
            for key in ("filters_proposed", "filters_applied"):
                if d.get(key):
                    try:
                        d[key] = json.loads(d[key])
                    except (json.JSONDecodeError, TypeError):
                        d[key] = []
                else:
                    d[key] = []
            iterations.append(d)

        # Parse JSON columns
        if run.get("target_curve_data"):
            try:
                run["target_curve_data"] = json.loads(run["target_curve_data"])
            except (json.JSONDecodeError, TypeError):
                run["target_curve_data"] = None

        run["iterations"] = iterations
        return run

    # ── Saved states ────────────────────────────────────────────────────────

    def save_state(
        self,
        name: str,
        eq_filters: list[dict] | None = None,
        delays: dict | None = None,
        polarities: dict | None = None,
        gains: dict | None = None,
        target_curve: str | None = None,
        rms_deviation: float | None = None,
        measurement_session_id: int | None = None,
        notes: str | None = None,
    ) -> int:
        """Save a full DSP snapshot. Returns the new state id."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO saved_states"
                " (name, timestamp, eq_filters, delays, polarities, gains,"
                "  target_curve, rms_deviation, measurement_session_id, notes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    now,
                    json.dumps(eq_filters) if eq_filters else None,
                    json.dumps(delays) if delays else None,
                    json.dumps(polarities) if polarities else None,
                    json.dumps(gains) if gains else None,
                    target_curve,
                    rms_deviation,
                    measurement_session_id,
                    notes,
                ),
            )
            return cur.lastrowid

    def list_states(self) -> list[dict]:
        """Return all saved states, most recent first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, timestamp, target_curve, rms_deviation,"
                " measurement_session_id, notes"
                " FROM saved_states ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_state(self, state_id: int) -> dict | None:
        """Return a full saved state by id, or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM saved_states WHERE id=?", (state_id,)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        for key in ("eq_filters", "delays", "polarities", "gains"):
            if d.get(key):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    d[key] = None
        return d

    def delete_state(self, state_id: int) -> bool:
        """Delete a saved state. Returns True if a row was deleted."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM saved_states WHERE id=?", (state_id,))
            return cur.rowcount > 0

    # ── Active DSP state ────────────────────────────────────────────────────

    def set_active_dsp(self, key: str, data: dict) -> None:
        """Upsert an active DSP state entry (e.g. 'output_eq_1', 'input_eq', 'delay_1')."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO active_dsp_state (key, timestamp, data)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET timestamp=excluded.timestamp, data=excluded.data",
                (key, now, json.dumps(data)),
            )

    def get_active_dsp(self) -> dict[str, dict]:
        """Return all active DSP state entries as {key: {timestamp, ...data}}."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, timestamp, data FROM active_dsp_state ORDER BY key"
            ).fetchall()
        result = {}
        for r in rows:
            try:
                d = json.loads(r["data"])
            except (json.JSONDecodeError, TypeError):
                d = {}
            d["timestamp"] = r["timestamp"]
            result[r["key"]] = d
        return result

    def clear_active_dsp(self) -> None:
        """Clear all active DSP state (e.g. on factory reset)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM active_dsp_state")

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

        meta = None
        try:
            raw_meta = row["metadata"] if "metadata" in row.keys() else None
            if raw_meta:
                meta = json.loads(raw_meta)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("session %d has corrupt metadata; ignoring", row["id"])

        tc = None
        try:
            raw_tc = row["target_curve"] if "target_curve" in row.keys() else None
            if raw_tc:
                tc = json.loads(raw_tc)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("session %d has corrupt target_curve; ignoring", row["id"])

        return Session(
            id=row["id"],
            timestamp=row["timestamp"],
            label=row["label"],
            start_fr=start_fr,
            end_fr=end_fr,
            filters_applied=filters_applied,
            notes=row["notes"],
            impulse_response=ir,
            metadata=meta,
            target_curve=tc,
        )
