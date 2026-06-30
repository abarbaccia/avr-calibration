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


# ── Active DSP state key helpers ───────────────────────────────────────────────
#
# Keys live in one flat `active_dsp_state` column keyed by string. With the
# signal-graph abstraction, the same output index on two different processors
# needs distinct keys — so we namespace. The legacy flat shape (``output_eq_0``,
# ``delay_1``, ``input_eq``, ``gain_2``, ``polarity_3``) is migrated once on
# first open to the namespaced shape ``processor:<name>:output:<idx>:<field>``.
# Readers should prefer `parse_dsp_key` so they tolerate both shapes during
# transient states.


def dsp_output_key(processor: str, output_index: int, field: str) -> str:
    """Canonical key for a per-output DSP state entry."""
    return f"processor:{processor}:output:{output_index}:{field}"


def dsp_input_key(processor: str, field: str = "eq") -> str:
    """Canonical key for a per-input DSP state entry (typically shared input EQ)."""
    return f"processor:{processor}:input:{field}"


def dsp_input_channel_key(processor: str, input_index: int, field: str) -> str:
    """Canonical key for a per-input-channel DSP state entry (e.g. input gain)."""
    return f"processor:{processor}:input:{input_index}:{field}"


def dsp_master_key(processor: str, field: str = "gain") -> str:
    """Canonical key for the global master-gain DSP state entry.

    Master gain is a single global pipeline parameter (the sub level control on
    CamillaDSP), not per-output. Persisting it lets the operating level survive a
    reboot/MCP restart instead of falling back to the driver's init default.
    """
    return f"processor:{processor}:master:{field}"


def parse_dsp_key(key: str) -> dict | None:
    """Parse a namespaced or legacy DSP key into components.

    Returns a dict with some subset of ``{processor, kind, output_index, field}``
    on success; returns ``None`` for non-DSP keys (e.g. ``"target_curve"``).

    Tolerates both shapes:
      - namespaced: ``processor:<name>:output:<idx>:<field>`` /
                    ``processor:<name>:input:<field>``
      - legacy:     ``output_eq_N`` / ``delay_N`` / ``polarity_N`` /
                    ``gain_N`` / ``input_eq``
    """
    if key.startswith("processor:"):
        parts = key.split(":")
        if len(parts) == 5 and parts[2] == "output":
            try:
                return {
                    "processor": parts[1],
                    "kind": "output",
                    "output_index": int(parts[3]),
                    "field": parts[4],
                }
            except ValueError:
                return None
        if len(parts) == 4 and parts[2] == "input":
            return {
                "processor": parts[1],
                "kind": "input",
                "field": parts[3],
            }
        if len(parts) == 4 and parts[2] == "master":
            return {
                "processor": parts[1],
                "kind": "master",
                "field": parts[3],
            }
        if len(parts) == 5 and parts[2] == "input":
            try:
                return {
                    "processor": parts[1],
                    "kind": "input",
                    "input_index": int(parts[3]),
                    "field": parts[4],
                }
            except ValueError:
                return None
        return None

    # Legacy shapes — processor is unknown at this layer; callers map via the
    # registry's default DSP name.
    legacy_fields = {
        "output_eq_": ("output", "eq"),
        "delay_":     ("output", "delay"),
        "polarity_":  ("output", "polarity"),
        "gain_":      ("output", "gain"),
    }
    for prefix, (kind, field) in legacy_fields.items():
        if key.startswith(prefix):
            try:
                return {
                    "processor": None,
                    "kind": kind,
                    "output_index": int(key.removeprefix(prefix)),
                    "field": field,
                }
            except ValueError:
                return None
    if key == "input_eq":
        return {"processor": None, "kind": "input", "field": "eq"}
    return None


def _legacy_to_namespaced(old_key: str, processor: str) -> str | None:
    """Return the migrated key for an old flat key, or None if it doesn't match.

    ``target_curve`` and other non-processor keys are left alone (return None).
    """
    parsed = parse_dsp_key(old_key)
    if parsed is None or parsed["processor"] is not None:
        return None
    if parsed["kind"] == "output":
        return dsp_output_key(processor, parsed["output_index"], parsed["field"])
    if parsed["kind"] == "input":
        return dsp_input_key(processor, parsed["field"])
    return None

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

-- Append-only history of every prior version of each active_dsp_state key.
-- Populated automatically by set_active_dsp before it overwrites a row.
-- Lets us recover destructive operations (clear_fir, apply_eq replacing a
-- known-good filter set, etc.) without manual snapshotting.
CREATE TABLE IF NOT EXISTS active_dsp_state_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT    NOT NULL,
    timestamp   TEXT    NOT NULL,  -- the prior row's timestamp
    data        TEXT    NOT NULL,  -- the prior row's data (full)
    archived_at TEXT    NOT NULL   -- when this archive entry was created
);

CREATE INDEX IF NOT EXISTS active_dsp_state_history_key
    ON active_dsp_state_history(key);
CREATE INDEX IF NOT EXISTS active_dsp_state_history_archived_at
    ON active_dsp_state_history(archived_at);

CREATE TABLE IF NOT EXISTS lessons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    run_id          INTEGER REFERENCES calibration_runs(id),
    scope           TEXT    NOT NULL,
    category        TEXT,
    claim           TEXT    NOT NULL,
    context         TEXT,
    confidence      REAL    NOT NULL DEFAULT 0.7,
    evidence        TEXT,
    state_hash      TEXT,
    target_curve    TEXT,
    status          TEXT    NOT NULL DEFAULT 'active',
    invalidated_at  TEXT,
    invalidated_by  TEXT,
    superseded_by   INTEGER REFERENCES lessons(id),
    promoted_to     TEXT,
    tags            TEXT
);

CREATE TABLE IF NOT EXISTS lesson_invalidators (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id   INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL,
    value       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lessons_status ON lessons(status);
CREATE INDEX IF NOT EXISTS idx_lessons_scope ON lessons(scope);
CREATE INDEX IF NOT EXISTS idx_lesson_inv_lesson ON lesson_invalidators(lesson_id);

-- Designed AVR-format FIR coefficient sets. Persists what was previously an
-- in-memory-only cache (mcp_server._AVR_FIR_CACHE) — that volatility made the
-- run-41 corr1 mains calibration unrecoverable after a container restart.
-- Keyed by (cache_key, channel_id); coefficients stored as a JSON float array.
CREATE TABLE IF NOT EXISTS avr_fir_coefficients (
    cache_key     TEXT    NOT NULL,
    channel_id    TEXT    NOT NULL,
    coefficients  TEXT    NOT NULL,
    num_taps      INTEGER NOT NULL,
    updated_at    TEXT    NOT NULL,
    PRIMARY KEY (cache_key, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_lesson_inv_value ON lesson_invalidators(kind, value);
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
    impulse_response: Optional[list[float]] = None  # time-domain IR, gated to IR_GATE_S seconds
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
            if "device_state" not in run_cols:
                conn.execute(
                    "ALTER TABLE calibration_runs ADD COLUMN device_state TEXT DEFAULT NULL"
                )
            if "run_type" not in run_cols:
                conn.execute(
                    "ALTER TABLE calibration_runs ADD COLUMN run_type TEXT DEFAULT 'calibration'"
                )
            if "sessions" not in run_cols:
                conn.execute(
                    "ALTER TABLE calibration_runs ADD COLUMN sessions TEXT DEFAULT NULL"
                )
            if "full_state_snapshot" not in run_cols:
                conn.execute(
                    "ALTER TABLE calibration_runs ADD COLUMN full_state_snapshot TEXT DEFAULT NULL"
                )
            if "goal" not in run_cols:
                conn.execute(
                    "ALTER TABLE calibration_runs ADD COLUMN goal TEXT DEFAULT NULL"
                )
            if "hypothesis" not in run_cols:
                conn.execute(
                    "ALTER TABLE calibration_runs ADD COLUMN hypothesis TEXT DEFAULT NULL"
                )
            if "outcome" not in run_cols:
                conn.execute(
                    "ALTER TABLE calibration_runs ADD COLUMN outcome TEXT DEFAULT NULL"
                )

            iter_cols = {row[1] for row in conn.execute("PRAGMA table_info(calibration_iterations)")}
            if "full_state_snapshot" not in iter_cols:
                conn.execute(
                    "ALTER TABLE calibration_iterations ADD COLUMN full_state_snapshot TEXT DEFAULT NULL"
                )

        self._migrate_legacy_active_dsp_keys()

    def _migrate_legacy_active_dsp_keys(self) -> None:
        """Rewrite flat active_dsp_state keys into processor-namespaced form.

        One-shot — runs until there are no more legacy keys left. Uses the
        current config's default DSP processor name; if the config can't be
        loaded (tests, corrupted install), the migration skips cleanly and
        readers fall back to the legacy-key path via ``parse_dsp_key``.
        """
        with self._connect() as conn:
            legacy_rows = conn.execute(
                "SELECT key FROM active_dsp_state WHERE key NOT LIKE 'processor:%'"
            ).fetchall()
            if not legacy_rows:
                return

            try:
                from .config import Config
                graph = Config.load().signal_graph
                default_dsp = graph.default_processor("dsp")
                if default_dsp is None:
                    return
                processor_name = default_dsp.name
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "active_dsp_state migration skipped (config unreadable): %s", exc
                )
                return

            for row in legacy_rows:
                old_key = row["key"]
                new_key = _legacy_to_namespaced(old_key, processor_name)
                if not new_key or new_key == old_key:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO active_dsp_state (key, timestamp, data) "
                    "SELECT ?, timestamp, data FROM active_dsp_state WHERE key=?",
                    (new_key, old_key),
                )
                conn.execute("DELETE FROM active_dsp_state WHERE key=?", (old_key,))

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

    def list_sessions(self, limit: int | None = None) -> list[Session]:
        """Return sessions, most recent first.

        Pass *limit* to cap rows at the SQL layer. Each session deserializes
        full FR + IR + metadata blobs, so loading all rows from a long-running
        DB will OOM on a 4 GB Pi. Callers that only need the latest N
        measurements MUST pass limit.
        """
        with self._connect() as conn:
            if limit is None:
                rows = conn.execute(
                    "SELECT * FROM sessions ORDER BY id DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sessions ORDER BY id DESC LIMIT ?",
                    (int(limit),),
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

    def save_run(
        self,
        recipe_name: str,
        target: str,
        device_state: dict | None = None,
        run_type: str = "calibration",
        goal: str | None = None,
        hypothesis: str | None = None,
    ) -> int:
        """Create a new calibration run record. Returns the run id.

        *device_state* captures the AVR/DSP hardware state at the start of the
        run (volume, preset, input, EQ state) so it can be reviewed later.

        *run_type* is "calibration" (default) or "validation" — validation runs
        don't iterate or converge, they record a set of measurement sessions.
        """
        ts = datetime.now(timezone.utc).isoformat()
        ds_json = json.dumps(device_state) if device_state else None
        snapshot = self.snapshot_full_dsp_state()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO calibration_runs"
                " (timestamp, recipe_name, target, device_state, run_type,"
                "  full_state_snapshot, goal, hypothesis)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, recipe_name, target, ds_json, run_type, snapshot, goal, hypothesis),
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
        sessions: list[dict] | None = None,
        outcome: str | None = None,
    ) -> None:
        """Update a calibration run with final results.

        *sessions* is a list of {"session_id": N, "label": "..."} dicts for
        validation runs that record multiple measurement sessions.

        *outcome* is a free-form prose summary of what actually happened —
        especially how the result compared to the run's hypothesis. Used by
        the lessons system as the seed for record_lesson().
        """
        snapshot = self.snapshot_full_dsp_state()
        with self._connect() as conn:
            if outcome is not None:
                conn.execute(
                    "UPDATE calibration_runs"
                    " SET converged=?, iterations_run=?, baseline_rms=?, final_rms=?, error=?,"
                    "     target_curve_data=?, sessions=?, full_state_snapshot=?, outcome=?"
                    " WHERE id=?",
                    (int(converged), iterations_run, baseline_rms, final_rms, error or None,
                     json.dumps(target_curve_data) if target_curve_data else None,
                     json.dumps(sessions) if sessions else None, snapshot, outcome, run_id),
                )
            else:
                conn.execute(
                    "UPDATE calibration_runs"
                    " SET converged=?, iterations_run=?, baseline_rms=?, final_rms=?, error=?,"
                    "     target_curve_data=?, sessions=?, full_state_snapshot=?"
                    " WHERE id=?",
                    (int(converged), iterations_run, baseline_rms, final_rms, error or None,
                     json.dumps(target_curve_data) if target_curve_data else None,
                     json.dumps(sessions) if sessions else None, snapshot, run_id),
                )

    def save_avr_fir(self, cache_key: str, channel_id: str, coefficients: list[float]) -> None:
        """Persist a designed AVR-format FIR coefficient set (upsert on
        (cache_key, channel_id)). Survives container restarts so designs can be
        re-pushed without re-deriving — the gap that lost run-41 corr1."""
        ts = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO avr_fir_coefficients"
                " (cache_key, channel_id, coefficients, num_taps, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(cache_key, channel_id) DO UPDATE SET"
                "  coefficients=excluded.coefficients,"
                "  num_taps=excluded.num_taps,"
                "  updated_at=excluded.updated_at",
                (cache_key, channel_id, json.dumps(coefficients), len(coefficients), ts),
            )

    def get_avr_fir(self, cache_key: str, channel_id: str) -> list[float] | None:
        """Return a persisted FIR coefficient set, or None if absent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT coefficients FROM avr_fir_coefficients"
                " WHERE cache_key=? AND channel_id=?",
                (cache_key, channel_id),
            ).fetchone()
        return json.loads(row[0]) if row else None

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
        snapshot = self.snapshot_full_dsp_state()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO calibration_iterations"
                " (run_id, iteration, rms_before, rms_after,"
                "  filters_proposed, filters_applied, safety_ok, safety_error,"
                "  full_state_snapshot)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    iteration,
                    rms_before,
                    rms_after,
                    json.dumps(filters_proposed),
                    json.dumps(filters_applied),
                    int(safety_ok),
                    safety_error or None,
                    snapshot,
                ),
            )
            return cur.lastrowid

    def get_runs(self, limit: int = 20) -> list[dict]:
        """Return recent calibration runs, most recent first.

        Excludes `full_state_snapshot` (can be large); fetch via get_run_detail.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM calibration_runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d.pop("full_state_snapshot", None)
            results.append(d)
        return results

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
            if d.get("full_state_snapshot"):
                try:
                    d["full_state_snapshot"] = json.loads(d["full_state_snapshot"])
                except (json.JSONDecodeError, TypeError):
                    d["full_state_snapshot"] = None
            iterations.append(d)

        for json_col in ("target_curve_data", "device_state", "full_state_snapshot"):
            if run.get(json_col):
                try:
                    run[json_col] = json.loads(run[json_col])
                except (json.JSONDecodeError, TypeError):
                    run[json_col] = None

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
        """Upsert an active DSP state entry (e.g. 'output_eq_1', 'input_eq', 'delay_1').

        Before overwriting, the prior row (if any) is archived into
        ``active_dsp_state_history`` so destructive operations (clear_fir,
        apply_eq replacing a known-good filter set, container restart
        rehydration on top of an unintended state) are recoverable.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            # Archive the existing row before overwriting. Cheap (one row
            # copy per mutation) and bounded by how often DSP state changes.
            prior = conn.execute(
                "SELECT timestamp, data FROM active_dsp_state WHERE key=?",
                (key,),
            ).fetchone()
            if prior is not None:
                conn.execute(
                    "INSERT INTO active_dsp_state_history"
                    " (key, timestamp, data, archived_at)"
                    " VALUES (?, ?, ?, ?)",
                    (key, prior["timestamp"], prior["data"], now),
                )
            conn.execute(
                "INSERT INTO active_dsp_state (key, timestamp, data)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET timestamp=excluded.timestamp, data=excluded.data",
                (key, now, json.dumps(data)),
            )

    def list_active_dsp_history(
        self, key: str | None = None, limit: int = 50
    ) -> list[dict]:
        """List archived prior versions of active DSP state entries.

        If ``key`` is provided, only entries for that key are returned;
        otherwise all keys, newest-first up to ``limit`` rows.
        """
        with self._connect() as conn:
            if key is not None:
                rows = conn.execute(
                    "SELECT id, key, timestamp, archived_at, length(data) AS data_len"
                    " FROM active_dsp_state_history"
                    " WHERE key=? ORDER BY archived_at DESC LIMIT ?",
                    (key, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, key, timestamp, archived_at, length(data) AS data_len"
                    " FROM active_dsp_state_history"
                    " ORDER BY archived_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_active_dsp_history(self, history_id: int) -> dict | None:
        """Fetch a single archived row by id (full data included)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, key, timestamp, data, archived_at"
                " FROM active_dsp_state_history WHERE id=?",
                (history_id,),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["data"] = json.loads(d["data"])
        except (json.JSONDecodeError, TypeError):
            pass
        return d

    def restore_active_dsp_history(self, history_id: int) -> bool:
        """Restore a prior version of an active_dsp_state key. The current
        value is itself archived first (via set_active_dsp), so restore is
        also reversible. Returns True if the history row was found and
        restored, False otherwise.
        """
        entry = self.get_active_dsp_history(history_id)
        if entry is None:
            return False
        data = entry["data"]
        if isinstance(data, str):
            # data wasn't valid JSON when fetched; pass through as-is.
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                return False
        self.set_active_dsp(entry["key"], data)
        return True

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

    def snapshot_full_dsp_state(self) -> str:
        """Return a JSON blob capturing the full active DSP state for archival.

        Used by calibration runs/iterations to record the complete signal-chain
        state (input EQ, per-output EQ, delays, polarities, gains) at a point
        in time — so prior runs can be restored from archive, not just from
        the latest active_dsp_state (which is overwritten per-key).
        """
        return json.dumps(self.get_active_dsp())

    def state_hash(self) -> str:
        """SHA-256 of the current active DSP state — stamps lessons for invalidation.

        Two lessons recorded with different state_hashes describe different
        physical DSP states; if any field changed (delay, polarity, EQ filter,
        gain), the hash changes.
        """
        import hashlib
        return hashlib.sha256(self.snapshot_full_dsp_state().encode()).hexdigest()

    # ── Lessons ─────────────────────────────────────────────────────────────

    def record_lesson(
        self,
        claim: str,
        scope: str,
        run_id: int | None = None,
        category: str | None = None,
        context: str | None = None,
        confidence: float = 0.7,
        evidence: dict | list | None = None,
        target_curve: str | None = None,
        tags: list[str] | None = None,
        invalidators: list[dict] | None = None,
    ) -> int:
        """Record a lesson learned from a calibration run.

        *scope*: ``"room"`` (specific to this room/hardware/state) or
        ``"general"`` (universal acoustics/tooling rule — should be promoted
        to codebase or memory and then marked promoted).

        *invalidators*: list of ``{"kind": "...", "value": "..."}`` dicts.
        ``kind`` is one of:
          - ``"event"``      — fires when invalidate_lessons() is called with this value
          - ``"state_hash"`` — lesson invalid once active DSP state hash changes
          - ``"code"``       — lesson invalid once named code module/function changes

        ``value`` is the event name (e.g. ``"sub_position_changed"``,
        ``"target_curve_changed"``), the original state hash, or
        ``"calibrate.modal_fir.design_modal_fir"`` for code anchors.
        """
        if scope not in ("room", "general"):
            raise ValueError(f"scope must be 'room' or 'general', got {scope!r}")
        now = datetime.now(timezone.utc).isoformat()
        sh = self.state_hash()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO lessons"
                " (created_at, run_id, scope, category, claim, context, confidence,"
                "  evidence, state_hash, target_curve, tags)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    now, run_id, scope, category, claim, context, confidence,
                    json.dumps(evidence) if evidence is not None else None,
                    sh, target_curve,
                    json.dumps(tags) if tags else None,
                ),
            )
            lesson_id = cur.lastrowid
            for inv in invalidators or []:
                conn.execute(
                    "INSERT INTO lesson_invalidators (lesson_id, kind, value)"
                    " VALUES (?, ?, ?)",
                    (lesson_id, inv["kind"], inv["value"]),
                )
            return lesson_id

    def get_relevant_lessons(
        self,
        category: str | None = None,
        tags: list[str] | None = None,
        scope: str | None = None,
        target_curve: str | None = None,
        limit: int = 10,
        include_invalidated: bool = False,
    ) -> list[dict]:
        """Return active lessons relevant to a planned action.

        Ranking: confidence × recency. ``tags`` matches if ANY tag overlaps.
        Pass ``include_invalidated=True`` for the audit trail; default returns
        only ``status='active'`` lessons.
        """
        clauses: list[str] = []
        params: list = []
        if not include_invalidated:
            clauses.append("status = 'active'")
        if scope:
            clauses.append("scope = ?")
            params.append(scope)
        if category:
            clauses.append("category = ?")
            params.append(category)
        if target_curve:
            clauses.append("(target_curve IS NULL OR target_curve = ?)")
            params.append(target_curve)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT * FROM lessons {where} "
            f"ORDER BY confidence DESC, created_at DESC LIMIT ?"
        )
        params.append(max(limit * 4, limit))  # over-fetch for tag filter
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            inv_rows = conn.execute(
                "SELECT lesson_id, kind, value FROM lesson_invalidators"
            ).fetchall()
        invs_by_lesson: dict[int, list[dict]] = {}
        for ir in inv_rows:
            invs_by_lesson.setdefault(ir["lesson_id"], []).append(
                {"kind": ir["kind"], "value": ir["value"]}
            )
        results: list[dict] = []
        wanted_tags = set(tags or [])
        for r in rows:
            d = dict(r)
            for col in ("evidence", "tags"):
                if d.get(col):
                    try:
                        d[col] = json.loads(d[col])
                    except (json.JSONDecodeError, TypeError):
                        d[col] = None
            if wanted_tags:
                lesson_tags = set(d.get("tags") or [])
                if not (wanted_tags & lesson_tags):
                    continue
            d["invalidators"] = invs_by_lesson.get(d["id"], [])
            results.append(d)
            if len(results) >= limit:
                break
        return results

    def list_lessons(self, limit: int = 50, status: str | None = None) -> list[dict]:
        """List lessons with optional status filter — for audit/review UIs."""
        sql = "SELECT * FROM lessons"
        params: list = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for col in ("evidence", "tags"):
                if d.get(col):
                    try:
                        d[col] = json.loads(d[col])
                    except (json.JSONDecodeError, TypeError):
                        d[col] = None
            out.append(d)
        return out

    def invalidate_lessons(
        self,
        events: list[str] | None = None,
        codes: list[str] | None = None,
        state_changed: bool = False,
        reason: str | None = None,
    ) -> list[int]:
        """Mark lessons stale based on what changed.

        - ``events``: event-kind invalidator values that fired (e.g.
          ``["sub_position_changed", "target_curve_changed"]``).
        - ``codes``: code-kind invalidator values for modules/functions that
          were edited (e.g. ``["calibrate.modal_fir.design_modal_fir"]``).
        - ``state_changed``: if True, any lesson whose ``state_hash`` differs
          from the current store state_hash is invalidated.

        Returns the list of invalidated lesson ids. Lessons stay in the DB
        with ``status='invalidated'`` for the audit trail.
        """
        now = datetime.now(timezone.utc).isoformat()
        invalidated: set[int] = set()
        with self._connect() as conn:
            triggers: list[tuple[str, str]] = []
            for e in events or []:
                triggers.append(("event", e))
            for c in codes or []:
                triggers.append(("code", c))
            for kind, value in triggers:
                rows = conn.execute(
                    "SELECT DISTINCT lesson_id FROM lesson_invalidators"
                    " WHERE kind=? AND value=?",
                    (kind, value),
                ).fetchall()
                for r in rows:
                    invalidated.add(r["lesson_id"])
            if state_changed:
                current = self.state_hash()
                rows = conn.execute(
                    "SELECT l.id FROM lessons l"
                    " JOIN lesson_invalidators i ON i.lesson_id = l.id"
                    " WHERE i.kind = 'state_hash' AND l.state_hash != ?",
                    (current,),
                ).fetchall()
                for r in rows:
                    invalidated.add(r["id"])
            if invalidated:
                placeholders = ",".join("?" * len(invalidated))
                conn.execute(
                    f"UPDATE lessons SET status='invalidated',"
                    f" invalidated_at=?, invalidated_by=?"
                    f" WHERE id IN ({placeholders}) AND status='active'",
                    (now, reason or "auto", *invalidated),
                )
        return sorted(invalidated)

    def promote_lesson(self, lesson_id: int, promoted_to: str) -> bool:
        """Mark a general-scope lesson as promoted (codebase fix or memory file).

        ``promoted_to`` is a free-form pointer like
        ``"memory:feedback_xyz.md"`` or ``"code:calibrate/modal_fir.py:fix-X"``.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE lessons SET status='promoted', promoted_to=?"
                " WHERE id=? AND status='active'",
                (promoted_to, lesson_id),
            )
            return cur.rowcount > 0

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
