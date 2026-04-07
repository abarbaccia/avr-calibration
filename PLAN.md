# Plan: Decay Analysis + Calibration Improvements

**Branch:** main (uncommitted)
**Date:** 2026-04-07

## Feature Brief

**One-liner:** Expose room-mode T60 decay analysis as an MCP tool so Claude can identify ringing bass frequencies, prioritize them, and recommend EQ correction Q values as part of the calibration loop.

**Problem being solved:** Frequency response measurements capture steady-state magnitude but not time behaviour. A room mode at 50 Hz can ring for 600 ms after a bass transient even if the magnitude peak is already cut by PEQ. `decay.py` identifies which frequencies ring and for how long, provides a priority ranking, and recommends a PEQ Q value to surgically target each mode. Claude uses this after each `measure` call to inform filter design.

**In scope:**
- `analyze_decay` MCP tool — loads IR from `SessionStore`, calls `analyze_decay()`, returns prioritised modes
- `configure_matrix` MCP tool — already added to `mcp_server.py`; finalize + add tests
- Recipe reference — add `analyze_decay` note to `harman-bass-persub.md` Phase 2
- Calibrate skill update — already done (recipe picker with hardware-aware recommendation)
- Tests for both new MCP tools

**Out of scope:**
- Waterfall / spectrogram web dashboard (TODO-R3 — P2 UI work, separate PR)
- `compare_decay` as a separate MCP tool — Claude interprets two sequential `analyze_decay` calls
- FIR filter generation (miniDSP 2x4 HD doesn't support FIR)

---

## What Already Exists

| Item | File | State |
|------|------|-------|
| `analyze_decay()` pure function | `calibrate/decay.py:29` | Done |
| `compare_decay()` pure function | `calibrate/decay.py:187` | Done |
| `DecayMode` dataclass | `calibrate/decay.py:20` | Done |
| `_t60_to_q()` mapping | `calibrate/decay.py:172` | Done |
| Unit tests for all decay functions | `tests/test_decay.py` | Done |
| `configure_matrix` tool impl | `mcp_server.py:482` | Done |
| `configure_matrix` Tool descriptor | `mcp_server.py:876` | Done |
| `configure_matrix` dispatch case | `mcp_server.py:971` | Done |
| `MinidspDriver.configure_active_input()` | `drivers/minidsp.py:275` | Done |
| IR stored per session in SQLite | `storage.py` | Done (`FrequencyResponse.impulse_response`, first 24 000 samples = 500 ms at 48 kHz) |
| `harman-bass-persub.md` recipe | `recipes/core/harman-bass-persub.md` | Done (Phases 0–3) |
| Recipe picker in `/calibrate` skill | `.claude/skills/calibrate/SKILL.md` | Done |

---

## Implementation Steps

### Step 1 — Add `_tool_analyze_decay()` async function to `mcp_server.py`

**Location:** After `_tool_configure_matrix()` (around line 493), before `_tool_check_system()`.

```python
async def _tool_analyze_decay(
    session_id: int | None = None,
    t60_threshold_ms: float = 300.0,
    freq_min: float = 20.0,
    freq_max: float = 200.0,
) -> dict:
    """Run T60 decay analysis on the impulse response from a stored session."""
    from .storage import SessionStore
    from .decay import analyze_decay as _analyze_decay

    try:
        store = SessionStore()
        sessions = store.list_sessions()
        if not sessions:
            return _err("no measurements found — run measure first")

        if session_id is not None:
            session = next((s for s in sessions if s.id == session_id), None)
            if session is None:
                return _err(f"session {session_id} not found")
        else:
            session = sessions[0]

        ir = session.impulse_response
        if not ir:
            return _err(
                f"session {session.id} has no impulse response stored — "
                "re-run measure to capture IR"
            )

        sample_rate = session.start_fr.sample_rate if session.start_fr else 48000
        modes = _analyze_decay(
            ir,
            sample_rate=sample_rate,
            t60_threshold_ms=t60_threshold_ms,
            freq_min=freq_min,
            freq_max=freq_max,
        )

        return _ok(
            session_id=session.id,
            mode_count=len(modes),
            modes=[
                {
                    "freq_hz": m.freq_hz,
                    "t60_ms": m.t60_ms,
                    "peak_db": m.peak_db,
                    "suggested_q": m.suggested_q,
                    "priority": m.priority,
                }
                for m in modes
            ],
        )
    except ValueError as exc:
        return _err(f"decay analysis error: {exc}")
    except Exception as exc:
        return _err(f"analyze_decay failed: {exc}")
```

**Notes on IR window:** 500 ms (24 000 samples) supports reliable T60 estimation up to ~1.5 s. Modes with T60 > 1.5 s will be underestimated, not silently missed. Adequate for typical room modes in the 300–800 ms range.

---

### Step 2 — Add `Tool` descriptor to `_TOOLS` list

**Location:** After `configure_matrix` Tool block (around line 893), before `check_system` Tool.

```python
Tool(
    name="analyze_decay",
    description=(
        "Analyze room-mode T60 decay in the impulse response from a stored measurement. "
        "Returns a list of ringing modes sorted by priority (T60 × peak_amplitude), "
        "each with freq_hz, t60_ms, peak_db, and suggested_q for EQ targeting. "
        "Modes with T60 > 300ms are flagged — PEQ cuts at suggested_q will reduce "
        "peak energy. "
        "Call after measure() to identify problem frequencies before designing EQ filters."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "integer",
                "description": "Session ID to analyse. Omit to analyse the most recent session.",
            },
            "t60_threshold_ms": {
                "type": "number",
                "description": "Minimum T60 in ms to flag as a ringing mode (default: 300).",
                "default": 300.0,
            },
            "freq_min": {
                "type": "number",
                "description": "Lower frequency bound in Hz (default: 20).",
                "default": 20.0,
            },
            "freq_max": {
                "type": "number",
                "description": "Upper frequency bound in Hz (default: 200).",
                "default": 200.0,
            },
        },
    },
),
```

---

### Step 3 — Add dispatch case in `call_tool()`

**Location:** After `configure_matrix` elif, before `check_system` elif:

```python
elif name == "analyze_decay":
    result = await _tool_analyze_decay(
        session_id=int(arguments["session_id"]) if "session_id" in arguments else None,
        t60_threshold_ms=float(arguments.get("t60_threshold_ms", 300.0)),
        freq_min=float(arguments.get("freq_min", 20.0)),
        freq_max=float(arguments.get("freq_max", 200.0)),
    )
```

---

### Step 4 — Fix `configure_matrix` Tool description (signal path write warning)

Per the hard rule in `mcp_server.py` docstring: all tools that write to hardware must carry `_SIGNAL_PATH_WRITE_WARNING` in their description. `configure_matrix` writes to the miniDSP routing matrix (hardware write) but is missing this warning. Fix the Tool descriptor description to append `_SIGNAL_PATH_WRITE_WARNING`.

---

### Step 5 — Add `analyze_decay` to calibrate SKILL.md tool list

In `.claude/skills/calibrate/SKILL.md` tool list (Step 3 section), add:
```
- `analyze_decay` — T60 decay analysis on the IR from a measurement; returns ringing modes with priority and suggested_q
```

---

### Step 6 — Add decay analysis note to `harman-bass-persub.md`

In Phase 2 (Per-Sub Room Correction), after applying per-sub EQ, add:

```markdown
**Optional: Decay analysis after per-sub EQ**
After applying per-sub corrections, call `analyze_decay(session_id)` on the most
recent solo measurement to check if any modes exhibit T60 > 500ms. If so, use
`suggested_q` from that mode when designing the PEQ cut — a narrower Q targets
the ringing frequency more surgically without over-cutting broadband.
```

---

### Step 7 — Tests

#### New tests for `_tool_analyze_decay` (add to `tests/test_mcp_server.py`)

| Test | Setup | Assert |
|------|-------|--------|
| `test_analyze_decay_latest_session` | Mock store with a session with a ringing IR | `ok=True`, `mode_count >= 1`, priority-1 mode has `freq_hz`, `t60_ms`, `suggested_q` |
| `test_analyze_decay_by_session_id` | Two sessions; pass `session_id` of older one | `result["session_id"] == older_id` |
| `test_analyze_decay_session_not_found` | Pass `session_id=9999`, store returns `[]` | `ok=False`, error contains "not found" |
| `test_analyze_decay_no_sessions` | `list_sessions()` returns `[]` | `ok=False`, error contains "no measurements found" |
| `test_analyze_decay_session_missing_ir` | Session with `impulse_response=None` | `ok=False`, error contains "no impulse response" |
| `test_analyze_decay_clean_ir` | Session with clean impulse (no modes > 300 ms) | `ok=True`, `mode_count=0`, `modes=[]` |
| `test_analyze_decay_threshold_param` | Mode with T60=400 ms; call with `t60_threshold_ms=500.0` | `mode_count=0` (filtered out) |
| `test_analyze_decay_freq_range_param` | Mode at 150 Hz; call with `freq_max=120.0` | Mode not returned |

#### New tests for `configure_matrix` (add to `tests/test_mcp_server.py`)

| Test | Setup | Assert |
|------|-------|--------|
| `test_configure_matrix_default_input` | Patch `_dsp.configure_active_input`; config `active_input: 0` | Called with `0`, `ok=True` |
| `test_configure_matrix_override_input` | Pass `active_input=1` | Called with `1` |
| `test_configure_matrix_driver_error` | `configure_active_input` raises `DriverError` | `ok=False` |

---

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| IR window (500 ms) underestimates T60 > 1.5 s | Low | Underestimated, not missed. Note in tool description. |
| `analyze_decay` compute cost on Pi 5 | Low | 24 000 samples × nperseg=2048 is tiny for scipy. |
| Claude calls `analyze_decay` with no prior `measure` | Low | Returns `_err("no measurements found")` |
| `configure_matrix` missing signal path write warning | Low | Fixed in Step 4. |

---

## Done Criteria

- [ ] `_tool_analyze_decay()` added to `mcp_server.py`
- [ ] `analyze_decay` Tool descriptor added to `_TOOLS`
- [ ] Dispatch elif added in `call_tool()` for `analyze_decay`
- [ ] `configure_matrix` Tool descriptor has `_SIGNAL_PATH_WRITE_WARNING` appended
- [ ] `analyze_decay` in calibrate SKILL.md tool list
- [ ] Decay analysis note added to `harman-bass-persub.md` Phase 2
- [ ] 8 tests for `_tool_analyze_decay` in `tests/test_mcp_server.py`
- [ ] 3 tests for `configure_matrix` in `tests/test_mcp_server.py`
- [ ] All existing `tests/test_decay.py` tests pass
- [ ] TODOS.md: TODO-R3 updated — `analyze_decay` MCP tool is the CLI/MCP primitive; web waterfall dashboard remains P2

---

## How Claude Uses `analyze_decay` in the Calibration Loop

```
measure()                     → session_id
analyze_decay(session_id)     → [{freq_hz: 50, t60_ms: 620, peak_db: +4.2, suggested_q: 2.8, priority: 1}, ...]
apply_eq(filters using suggested_q for priority modes)
measure()                     → new session_id
analyze_decay(new session_id) → verify T60 reduced
```

PEQ cuts at `suggested_q` reduce peak energy at the ringing frequency. This is the limit of what the miniDSP 2x4 HD supports (no FIR). Claude reports residual T60 after EQ and advises on room treatment if ringing persists.

---

## Previous Plan

The prior plan (`Pi 5 Headless Readiness`, 2026-04-01) covered arm64 CI, UMIK auto-detection, and the headless `/api/measure` endpoint. That plan is preserved in git history. The work in this plan is independent and can be merged to main without dependency on the Pi 5 infrastructure changes.
